"""PostgreSQL ``Catalog`` and ``RecordStore`` facets.

Predicates (request scope and ACL) compile to ``EXISTS`` subqueries over the typed-field tables;
population counts and ``exists`` run in SQL; ``values`` is a ``SELECT DISTINCT`` under a cardinality
cap (more distinct values than the cap is a typed error, never a silent truncation). Measures and
groups are computed over the matched rows with the shared ``backends/_scope.py`` helpers, exactly as
the sqlite and mongodb adapters do: ``ScopeResult`` carries ``matched_ids`` so the rows are fetched
anyway, and identical helpers keep results identical across adapters (plan Phase 1 acceptance).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Sequence

from ...core.errors import BackendError
from ...core.types import (
    Chunk,
    Document,
    GenerationId,
    MetadataSchema,
    Page,
    PageResult,
    Predicate,
    Scope,
    ScopePredicates,
    ScopeResult,
    ScopeSpec,
    StructuredRecord,
    UnsupportedConstraint,
)
from .._scope import check_matched, document_values, matched_cap, record_violations, scope_result, unsupported_reason
from ._db import C, KIND, field_row, page_window, predicate_sql, row_as_string, scope_sql

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import PostgresBackend

FIELDS_INSERT = "INSERT INTO {table}(owner_id, field, kind, text_value, fold_value, num_value, ts_value, is_null) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"


def chunk_row(chunk: Chunk) -> tuple:
    return (chunk.id, chunk.document_id, chunk.field, chunk.start, chunk.end, chunk.text, chunk.recipe)


def insert_chunks(cur: Any, schema: str, chunks: Sequence[Chunk], replace: bool) -> None:
    conflict = (
        "ON CONFLICT (id) DO UPDATE SET document_id=EXCLUDED.document_id, field=EXCLUDED.field, start_offset=EXCLUDED.start_offset, "
        "end_offset=EXCLUDED.end_offset, text=EXCLUDED.text, recipe=EXCLUDED.recipe"
        if replace
        else "ON CONFLICT (id) DO NOTHING"
    )
    cur.executemany(
        f"INSERT INTO {schema}.chunks(id, document_id, field, start_offset, end_offset, text, recipe) VALUES (%s,%s,%s,%s,%s,%s,%s) {conflict}",
        [chunk_row(c) for c in chunks],
    )


def known_documents(cur: Any, schema: str, ids: Sequence[str]) -> dict[str, str]:
    """``id -> data`` for the requested documents that exist."""
    cur.execute(f"SELECT id, data FROM {schema}.documents WHERE id = ANY(%s)", (list(ids),))
    return {r[0]: r[1] for r in cur.fetchall()}


class PostgresCatalog:
    def __init__(self, backend: PostgresBackend):
        self._b = backend

    def put_documents(self, gen: GenerationId, documents: Sequence[Document], chunks: Sequence[Chunk]) -> None:
        with self._b.tx("put_documents") as cur:
            s = self._b.require_generation(cur, gen, create=True)
            if documents:
                cur.executemany(
                    f"INSERT INTO {s}.documents(id, data) VALUES (%s,%s) ON CONFLICT (id) DO UPDATE SET data=EXCLUDED.data",
                    [(d.id, d.model_dump_json()) for d in documents],
                )
                cur.execute(f"DELETE FROM {s}.document_fields WHERE owner_id = ANY(%s)", ([d.id for d in documents],))
                rows = [field_row(d.id, key, value) for d in documents for key, value in document_values(d).items()]
                if rows:
                    cur.executemany(FIELDS_INSERT.format(table=f"{s}.document_fields"), rows)
            if chunks:
                known = known_documents(cur, s, sorted({c.document_id for c in chunks}))
                for chunk in chunks:
                    if chunk.document_id not in known:
                        raise BackendError(KIND, "put_documents", f"chunk {chunk.id} references unknown document")
                insert_chunks(cur, s, chunks, replace=True)

    def get(self, gen: GenerationId, ids: Sequence[str]) -> list[Document]:
        if not ids:
            return []
        self._b.stats["catalog.get"] += 1
        self._b.stats["catalog.get.ids"] += len(ids)
        with self._b.tx("get") as cur:
            s = self._b.require_generation(cur, gen)
            found = known_documents(cur, s, list(ids))
        return [Document.model_validate_json(found[i]) for i in ids if i in found]

    def get_chunks(self, gen: GenerationId, chunk_ids: Sequence[str]) -> list[Chunk]:
        if not chunk_ids:
            return []
        self._b.stats["catalog.get_chunks"] += 1
        with self._b.tx("get_chunks") as cur:
            s = self._b.require_generation(cur, gen)
            cur.execute(
                f"SELECT id, document_id, field, start_offset, end_offset, text, recipe FROM {s}.chunks WHERE id = ANY(%s)",
                (list(chunk_ids),),
            )
            rows = cur.fetchall()
        found = {
            r[0]: Chunk(id=r[0], document_id=r[1], field=r[2], start=r[3], end=r[4], text=r[5], recipe=r[6]) for r in rows
        }
        return [found[i] for i in chunk_ids if i in found]

    def iter_scope(self, gen: GenerationId, scope: Scope | None, page: Page) -> PageResult[Document]:
        offset, size = page_window(page, 50)
        with self._b.tx("iter_scope") as cur:
            s = self._b.require_generation(cur, gen)
            where, params = scope_sql(s, "d.id", scope)
            cur.execute(
                f"SELECT d.data FROM {s}.documents d WHERE {where} ORDER BY d.id {C} LIMIT %s OFFSET %s", [*params, size + 1, offset]
            )
            rows = cur.fetchall()
        items = [Document.model_validate_json(r[0]) for r in rows[:size]]
        return PageResult(items=items, next_cursor=str(offset + size) if len(rows) > size else None)

    def count_documents(self, gen: GenerationId) -> int:
        with self._b.tx("count_documents") as cur:
            s = self._b.require_generation(cur, gen)
            cur.execute(f"SELECT count(*) FROM {s}.documents")
            return int(cur.fetchone()[0])

    def count_chunks(self, gen: GenerationId) -> int:
        with self._b.tx("count_chunks") as cur:
            s = self._b.require_generation(cur, gen)
            cur.execute(f"SELECT count(*) FROM {s}.chunks")
            return int(cur.fetchone()[0])

    def delete_generation(self, gen: GenerationId) -> None:
        self._b.delete_generation(gen)


class PostgresRecords:
    def __init__(self, backend: PostgresBackend):
        self._b = backend

    def upsert(self, gen: GenerationId, records: Sequence[StructuredRecord], schema: MetadataSchema) -> None:
        for record in records:  # validate everything before the first write
            violations = record_violations(schema, record)
            if violations:
                raise BackendError(
                    KIND, "upsert", f"record {record.id}: {violations[0].field} {violations[0].category}: {violations[0].message}"
                )
        with self._b.tx("upsert") as cur:
            s = self._b.require_generation(cur, gen, create=True)
            cur.execute(f"UPDATE {self._b.table('generations')} SET metadata_schema = %s WHERE key = %s", (schema.model_dump_json(), gen.prefix()))
            if not records:
                return
            cur.executemany(
                f"INSERT INTO {s}.records(id, schema_id, data) VALUES (%s,%s,%s) "
                "ON CONFLICT (id) DO UPDATE SET schema_id=EXCLUDED.schema_id, data=EXCLUDED.data",
                [(r.id, r.schema_id, r.model_dump_json()) for r in records],
            )
            cur.execute(f"DELETE FROM {s}.record_fields WHERE owner_id = ANY(%s)", ([r.id for r in records],))
            rows = [field_row(r.id, key, value) for r in records for key, value in r.values.items()]
            if rows:
                cur.executemany(FIELDS_INSERT.format(table=f"{s}.record_fields"), rows)

    def count(self, gen: GenerationId) -> int:
        with self._b.tx("count") as cur:
            s = self._b.require_generation(cur, gen)
            cur.execute(f"SELECT count(*) FROM {s}.records")
            return int(cur.fetchone()[0])

    @staticmethod
    def _where(s: str, schema: MetadataSchema, predicates: Sequence[Predicate]) -> tuple[str, list[Any]]:
        clauses, params = ["TRUE"], []
        for predicate in predicates:
            spec = schema.field(predicate.field)
            sql, extra = predicate_sql(f"{s}.record_fields", "r.id", predicate, bool(spec and spec.casefold_match), spec)
            clauses.append(sql)
            params.extend(extra)
        return " AND ".join(clauses), params

    def execute(self, gen: GenerationId, scope: ScopeSpec, acl: ScopePredicates) -> ScopeResult:
        with self._b.tx("execute") as cur:
            info = self._b.generation_info(cur, gen)
            s, schema = info["schema"], info["metadata_schema"]
            applied, unsupported = [], []
            for predicate in scope.predicates:
                reason = unsupported_reason(schema, predicate)
                if reason is not None:
                    unsupported.append(UnsupportedConstraint(constraint_text=predicate.field, reason=reason))
                else:
                    applied.append(predicate)
            acl_where, acl_params = self._where(s, schema, acl)
            cur.execute(f"SELECT count(*) FROM {s}.records r WHERE {acl_where}", acl_params)
            population = int(cur.fetchone()[0])
            where, params = self._where(s, schema, [*acl, *applied])
            cap = matched_cap(self._b)
            cur.execute(f"SELECT r.data FROM {s}.records r WHERE {where} ORDER BY r.id {C} LIMIT %s", [*params, cap + 1])
            rows = cur.fetchall()
        check_matched(KIND, rows, cap)
        matched = [StructuredRecord.model_validate_json(r[0]) for r in rows]
        return scope_result(scope, matched, population, schema, applied, unsupported)

    def exists(self, gen: GenerationId, field: str, value: Any, acl: ScopePredicates) -> bool:
        with self._b.tx("exists") as cur:
            info = self._b.generation_info(cur, gen)
            where, params = self._where(info["schema"], info["metadata_schema"], [*acl, Predicate(field=field, op="eq", value=value)])
            cur.execute(f"SELECT 1 FROM {info['schema']}.records r WHERE {where} LIMIT 1", params)
            return cur.fetchone() is not None

    def values(self, gen: GenerationId, field: str, acl: ScopePredicates, limit: int) -> list[str]:
        cap = self._b.max_values_cardinality
        with self._b.tx("values") as cur:
            info = self._b.generation_info(cur, gen)
            s, schema = info["schema"], info["metadata_schema"]
            spec = schema.field(field)
            if spec is None or not spec.listable:
                raise BackendError(KIND, "values", f"field {field!r} is not listable")
            where, params = self._where(s, schema, acl)
            cur.execute(
                f"SELECT DISTINCT f.kind, f.text_value, f.num_value FROM {s}.record_fields f JOIN {s}.records r ON r.id = f.owner_id "
                f"WHERE f.field = %s AND NOT f.is_null AND {where} LIMIT %s",
                [field, *params, cap + 1],
            )
            rows = cur.fetchall()
        if len(rows) > cap:
            raise BackendError(KIND, "values", f"field {field!r} has more than {cap} distinct values; refusing to enumerate")
        return sorted({row_as_string(*r) for r in rows})[: max(0, limit)]


def document_title(data: str) -> str:
    return json.loads(data).get("title") or ""
