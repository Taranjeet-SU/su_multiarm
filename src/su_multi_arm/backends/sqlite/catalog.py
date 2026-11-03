"""SQLite ``Catalog`` and ``RecordStore`` facets."""

from __future__ import annotations

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
from ._db import KIND, field_row, placeholders, predicate_sql, scope_sql

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import SqliteBackend


def page_window(page: Page | None, default_size: int) -> tuple[int, int]:
    if default_size < 1:
        raise BackendError(KIND, "page", "k must be positive")
    try:
        page = page or Page(size=default_size)
    except ValueError as exc:
        raise BackendError(KIND, "page", f"invalid page: {exc}") from None
    offset = int(page.cursor) if page.cursor else 0
    if offset < 0:
        raise BackendError(KIND, "page", "invalid cursor")
    return offset, page.size


class SqliteCatalog:
    def __init__(self, backend: SqliteBackend):
        self._b = backend

    def put_documents(self, gen: GenerationId, documents: Sequence[Document], chunks: Sequence[Chunk]) -> None:
        with self._b.catalog_db(gen, create=True) as db:
            for document in documents:
                db.execute("INSERT OR REPLACE INTO documents VALUES (?,?)", (document.id, document.model_dump_json()))
                db.execute("DELETE FROM document_fields WHERE document_id=?", (document.id,))
                db.executemany(
                    "INSERT INTO document_fields VALUES (?,?,?,?,?,?,?)",
                    [field_row(document.id, key, value) for key, value in document_values(document).items()],
                )
            for chunk in chunks:
                if db.execute("SELECT 1 FROM documents WHERE id=?", (chunk.document_id,)).fetchone() is None:
                    raise BackendError(KIND, "put_documents", f"chunk {chunk.id} references unknown document")
                db.execute(
                    "INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?,?)",
                    (chunk.id, chunk.document_id, chunk.field, chunk.start, chunk.end, chunk.text, chunk.recipe),
                )

    def get(self, gen: GenerationId, ids: Sequence[str]) -> list[Document]:
        if not ids:
            return []
        self._b.stats["catalog.get"] += 1
        self._b.stats["catalog.get.ids"] += len(ids)
        with self._b.catalog_db(gen) as db:
            rows = db.execute(f"SELECT id, data FROM documents WHERE id IN ({placeholders(list(ids))})", list(ids)).fetchall()
        found = {row["id"]: Document.model_validate_json(row["data"]) for row in rows}
        return [found[i] for i in ids if i in found]

    def get_chunks(self, gen: GenerationId, chunk_ids: Sequence[str]) -> list[Chunk]:
        if not chunk_ids:
            return []
        self._b.stats["catalog.get_chunks"] += 1
        with self._b.catalog_db(gen) as db:
            rows = db.execute(f"SELECT * FROM chunks WHERE id IN ({placeholders(list(chunk_ids))})", list(chunk_ids)).fetchall()
        found = {row["id"]: Chunk.model_validate(dict(row)) for row in rows}
        return [found[i] for i in chunk_ids if i in found]

    def iter_scope(self, gen: GenerationId, scope: Scope | None, page: Page) -> PageResult[Document]:
        offset, size = page_window(page, 50)
        where, params = scope_sql("d.id", scope)
        with self._b.catalog_db(gen) as db:
            rows = db.execute(
                f"SELECT d.data FROM documents d WHERE {where} ORDER BY d.id LIMIT ? OFFSET ?", [*params, size + 1, offset]
            ).fetchall()
        items = [Document.model_validate_json(r["data"]) for r in rows[:size]]
        return PageResult(items=items, next_cursor=str(offset + size) if len(rows) > size else None)

    def count_documents(self, gen: GenerationId) -> int:
        with self._b.catalog_db(gen) as db:
            return int(db.execute("SELECT count(*) FROM documents").fetchone()[0])

    def count_chunks(self, gen: GenerationId) -> int:
        with self._b.catalog_db(gen) as db:
            return int(db.execute("SELECT count(*) FROM chunks").fetchone()[0])

    def delete_generation(self, gen: GenerationId) -> None:
        self._b.delete_generation(gen)


class SqliteRecords:
    def __init__(self, backend: SqliteBackend):
        self._b = backend

    def _schema(self, db) -> MetadataSchema:
        row = db.execute("SELECT value FROM meta WHERE key='metadata_schema'").fetchone()
        return MetadataSchema.model_validate_json(row["value"]) if row else MetadataSchema()

    def upsert(self, gen: GenerationId, records: Sequence[StructuredRecord], schema: MetadataSchema) -> None:
        with self._b.catalog_db(gen, create=True) as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('metadata_schema', ?)", (schema.model_dump_json(),))
            for record in records:
                violations = record_violations(schema, record)
                if violations:
                    raise BackendError(
                        KIND, "upsert", f"record {record.id}: {violations[0].field} {violations[0].category}: {violations[0].message}"
                    )
                db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?)", (record.id, record.schema_id, record.model_dump_json()))
                db.execute("DELETE FROM record_fields WHERE record_id=?", (record.id,))
                db.executemany(
                    "INSERT INTO record_fields VALUES (?,?,?,?,?,?,?)",
                    [field_row(record.id, key, value) for key, value in record.values.items()],
                )

    def count(self, gen: GenerationId) -> int:
        with self._b.catalog_db(gen) as db:
            return int(db.execute("SELECT count(*) FROM records").fetchone()[0])

    def _casefold(self, schema: MetadataSchema, field: str) -> bool:
        spec = schema.field(field)
        return bool(spec and spec.casefold_match)

    def _where(self, schema: MetadataSchema, predicates: Sequence[Predicate]) -> tuple[str, list[Any]]:
        clauses, params = ["1"], []
        for predicate in predicates:
            sql, extra = predicate_sql("record_fields", "record_id", "r.id", predicate, self._casefold(schema, predicate.field), schema.field(predicate.field))
            clauses.append(sql)
            params.extend(extra)
        return " AND ".join(clauses), params

    def execute(self, gen: GenerationId, scope: ScopeSpec, acl: ScopePredicates) -> ScopeResult:
        with self._b.catalog_db(gen) as db:
            schema = self._schema(db)
            applied, unsupported = [], []
            for predicate in scope.predicates:
                reason = unsupported_reason(schema, predicate)
                if reason is not None:
                    unsupported.append(UnsupportedConstraint(constraint_text=predicate.field, reason=reason))
                else:
                    applied.append(predicate)
            acl_where, acl_params = self._where(schema, acl)
            population = int(db.execute(f"SELECT count(*) FROM records r WHERE {acl_where}", acl_params).fetchone()[0])
            where, params = self._where(schema, [*acl, *applied])
            cap = matched_cap(self._b)
            rows = db.execute(f"SELECT r.data FROM records r WHERE {where} ORDER BY r.id LIMIT ?", [*params, cap + 1]).fetchall()
        check_matched(KIND, rows, cap)
        matched = [StructuredRecord.model_validate_json(r["data"]) for r in rows]
        return scope_result(scope, matched, population, schema, applied, unsupported)

    def exists(self, gen: GenerationId, field: str, value: Any, acl: ScopePredicates) -> bool:
        with self._b.catalog_db(gen) as db:
            schema = self._schema(db)
            where, params = self._where(schema, [*acl, Predicate(field=field, op="eq", value=value)])
            return db.execute(f"SELECT 1 FROM records r WHERE {where} LIMIT 1", params).fetchone() is not None

    def values(self, gen: GenerationId, field: str, acl: ScopePredicates, limit: int) -> list[str]:
        with self._b.catalog_db(gen) as db:
            schema = self._schema(db)
            spec = schema.field(field)
            if spec is None or not spec.listable:
                raise BackendError(KIND, "values", f"field {field!r} is not listable")
            where, params = self._where(schema, acl)
            rows = db.execute(
                f"SELECT DISTINCT f.text_value, f.num_value, f.kind FROM record_fields f JOIN records r ON r.id=f.record_id "
                f"WHERE f.field=? AND f.is_null=0 AND {where}",
                [field, *params],
            ).fetchall()
        seen = set()
        for row in rows:
            if row["kind"] in {"integer", "number"}:
                number = row["num_value"]
                seen.add(str(int(number)) if row["kind"] == "integer" else str(number))
            else:
                seen.add(str(row["text_value"]))
        return sorted(seen)[: max(0, limit)]
