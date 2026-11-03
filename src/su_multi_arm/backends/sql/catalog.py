"""Generic SQL ``Catalog`` and ``RecordStore`` facets (SQLAlchemy Core).

Predicates compile to correlated ``EXISTS`` clauses over the typed-field tables; population counts
and ``exists`` run in SQL; ``values`` is a ``SELECT DISTINCT`` under a cardinality cap (more distinct
values than the cap is a typed error). Measures and groups are computed over the matched rows with
the shared ``backends/_scope.py`` helpers, as every other adapter does, so ``ScopeResult`` values are
identical across adapters.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

import sqlalchemy as sa

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
from ._db import KIND, binary_order, check_ids, field_row, page_window, predicate_clause, row_as_string, scope_clause

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import SqlBackend

CHUNK_BATCH = 500  # bounded IN lists (SQLite's variable limit, MySQL packet size)


def _batches(values: Sequence[Any]) -> list[list[Any]]:
    items = list(values)
    return [items[i : i + CHUNK_BATCH] for i in range(0, len(items), CHUNK_BATCH)]


def _chunk_row(chunk: Chunk) -> dict[str, Any]:
    return {
        "id": chunk.id,
        "document_id": chunk.document_id,
        "field": chunk.field,
        "start_offset": chunk.start,
        "end_offset": chunk.end,
        "text": chunk.text,
        "recipe": chunk.recipe,
    }


class SqlCatalog:
    def __init__(self, backend: SqlBackend):
        self._b = backend

    def put_documents(self, gen: GenerationId, documents: Sequence[Document], chunks: Sequence[Chunk]) -> None:
        check_ids([d.id for d in documents] + [c.id for c in chunks], "put_documents")
        with self._b.tx("put_documents") as conn:
            t = self._b.tables(conn, gen, create=True)
            docs, fields, chunk_table = t["documents"], t["document_fields"], t["chunks"]
            for batch in _batches([d.id for d in documents]):
                conn.execute(docs.delete().where(docs.c.id.in_(batch)))
                conn.execute(fields.delete().where(fields.c.owner_id.in_(batch)))
            if documents:
                conn.execute(docs.insert(), [{"id": d.id, "data": d.model_dump_json()} for d in documents])
                rows = [field_row(d.id, k, v) for d in documents for k, v in document_values(d).items()]
                if rows:
                    conn.execute(fields.insert(), rows)
            if chunks:
                known: set[str] = set()
                for batch in _batches(sorted({c.document_id for c in chunks})):
                    known |= {r[0] for r in conn.execute(sa.select(docs.c.id).where(docs.c.id.in_(batch)))}
                for chunk in chunks:
                    if chunk.document_id not in known:
                        raise BackendError(KIND, "put_documents", f"chunk {chunk.id} references unknown document")
                for batch in _batches([c.id for c in chunks]):
                    conn.execute(chunk_table.delete().where(chunk_table.c.id.in_(batch)))
                conn.execute(chunk_table.insert(), [_chunk_row(c) for c in chunks])

    def get(self, gen: GenerationId, ids: Sequence[str]) -> list[Document]:
        if not ids:
            return []
        self._b.stats["catalog.get"] += 1
        self._b.stats["catalog.get.ids"] += len(ids)
        found: dict[str, Document] = {}
        with self._b.tx("get") as conn:
            docs = self._b.tables(conn, gen)["documents"]
            for batch in _batches(list(ids)):
                for row in conn.execute(sa.select(docs.c.id, docs.c.data).where(docs.c.id.in_(batch))):
                    found[row[0]] = Document.model_validate_json(row[1])
        return [found[i] for i in ids if i in found]

    def get_chunks(self, gen: GenerationId, chunk_ids: Sequence[str]) -> list[Chunk]:
        if not chunk_ids:
            return []
        self._b.stats["catalog.get_chunks"] += 1
        found: dict[str, Chunk] = {}
        with self._b.tx("get_chunks") as conn:
            c = self._b.tables(conn, gen)["chunks"]
            for batch in _batches(list(chunk_ids)):
                for r in conn.execute(sa.select(c).where(c.c.id.in_(batch))).mappings():
                    found[r["id"]] = Chunk(
                        id=r["id"], document_id=r["document_id"], field=r["field"], start=r["start_offset"], end=r["end_offset"], text=r["text"], recipe=r["recipe"]
                    )
        return [found[i] for i in chunk_ids if i in found]

    def iter_scope(self, gen: GenerationId, scope: Scope | None, page: Page) -> PageResult[Document]:
        offset, size = page_window(page, 50)
        with self._b.tx("iter_scope") as conn:
            t = self._b.tables(conn, gen)
            docs = t["documents"]
            query = (
                sa.select(docs.c.data)
                .where(scope_clause(t["document_fields"], docs.c.id, scope))
                .order_by(binary_order(docs.c.id, self._b.dialect))
                .limit(size + 1)
                .offset(offset)
            )
            rows = conn.execute(query).all()
        items = [Document.model_validate_json(r[0]) for r in rows[:size]]
        return PageResult(items=items, next_cursor=str(offset + size) if len(rows) > size else None)

    def _count(self, gen: GenerationId, table: str, operation: str) -> int:
        with self._b.tx(operation) as conn:
            t = self._b.tables(conn, gen)[table]
            return int(conn.execute(sa.select(sa.func.count()).select_from(t)).scalar_one())

    def count_documents(self, gen: GenerationId) -> int:
        return self._count(gen, "documents", "count_documents")

    def count_chunks(self, gen: GenerationId) -> int:
        return self._count(gen, "chunks", "count_chunks")

    def delete_generation(self, gen: GenerationId) -> None:
        self._b.delete_generation(gen)


class SqlRecords:
    def __init__(self, backend: SqlBackend):
        self._b = backend

    def upsert(self, gen: GenerationId, records: Sequence[StructuredRecord], schema: MetadataSchema) -> None:
        for record in records:  # validate everything before the first write
            violations = record_violations(schema, record)
            if violations:
                raise BackendError(
                    KIND, "upsert", f"record {record.id}: {violations[0].field} {violations[0].category}: {violations[0].message}"
                )
        check_ids([r.id for r in records], "upsert")
        g = self._b.rt["generations"]
        with self._b.tx("upsert") as conn:
            t = self._b.tables(conn, gen, create=True)
            conn.execute(g.update().where(g.c.key == gen.prefix()).values(metadata_schema=schema.model_dump_json()))
            if not records:
                return
            recs, fields = t["records"], t["record_fields"]
            for batch in _batches([r.id for r in records]):
                conn.execute(recs.delete().where(recs.c.id.in_(batch)))
                conn.execute(fields.delete().where(fields.c.owner_id.in_(batch)))
            conn.execute(recs.insert(), [{"id": r.id, "schema_id": r.schema_id, "data": r.model_dump_json()} for r in records])
            rows = [field_row(r.id, k, v) for r in records for k, v in r.values.items()]
            if rows:
                conn.execute(fields.insert(), rows)

    def count(self, gen: GenerationId) -> int:
        with self._b.tx("count") as conn:
            t = self._b.tables(conn, gen)["records"]
            return int(conn.execute(sa.select(sa.func.count()).select_from(t)).scalar_one())

    @staticmethod
    def _where(t: dict[str, sa.Table], schema: MetadataSchema, predicates: Sequence[Predicate]) -> Any:
        clauses = []
        for predicate in predicates:
            spec = schema.field(predicate.field)
            clauses.append(predicate_clause(t["record_fields"], t["records"].c.id, predicate, bool(spec and spec.casefold_match), spec))
        return sa.and_(sa.true(), *clauses)

    def execute(self, gen: GenerationId, scope: ScopeSpec, acl: ScopePredicates) -> ScopeResult:
        with self._b.tx("execute") as conn:
            t = self._b.tables(conn, gen)
            schema = self._b.metadata_schema(conn, gen)
            applied, unsupported = [], []
            for predicate in scope.predicates:
                reason = unsupported_reason(schema, predicate)
                if reason is not None:
                    unsupported.append(UnsupportedConstraint(constraint_text=predicate.field, reason=reason))
                else:
                    applied.append(predicate)
            recs = t["records"]
            population = int(conn.execute(sa.select(sa.func.count()).select_from(recs).where(self._where(t, schema, acl))).scalar_one())
            cap = matched_cap(self._b)
            query = sa.select(recs.c.data).where(self._where(t, schema, [*acl, *applied])).order_by(binary_order(recs.c.id, self._b.dialect)).limit(cap + 1)
            rows = conn.execute(query).all()
        check_matched(KIND, rows, cap)
        matched = [StructuredRecord.model_validate_json(r[0]) for r in rows]
        return scope_result(scope, matched, population, schema, applied, unsupported)

    def exists(self, gen: GenerationId, field: str, value: Any, acl: ScopePredicates) -> bool:
        with self._b.tx("exists") as conn:
            t = self._b.tables(conn, gen)
            schema = self._b.metadata_schema(conn, gen)
            where = self._where(t, schema, [*acl, Predicate(field=field, op="eq", value=value)])
            return conn.execute(sa.select(t["records"].c.id).where(where).limit(1)).first() is not None

    def values(self, gen: GenerationId, field: str, acl: ScopePredicates, limit: int) -> list[str]:
        cap = self._b.max_values_cardinality
        with self._b.tx("values") as conn:
            t = self._b.tables(conn, gen)
            schema = self._b.metadata_schema(conn, gen)
            spec = schema.field(field)
            if spec is None or not spec.listable:
                raise BackendError(KIND, "values", f"field {field!r} is not listable")
            f, recs = t["record_fields"], t["records"]
            query = (
                sa.select(f.c.kind, f.c.text_value, f.c.num_value)
                .distinct()
                .select_from(f.join(recs, recs.c.id == f.c.owner_id))
                .where(f.c.field == field, f.c.is_null.is_(False), self._where(t, schema, acl))
                .limit(cap + 1)
            )
            rows = conn.execute(query).all()
        if len(rows) > cap:
            raise BackendError(KIND, "values", f"field {field!r} has more than {cap} distinct values; refusing to enumerate")
        return sorted({row_as_string(*r) for r in rows})[: max(0, limit)]
