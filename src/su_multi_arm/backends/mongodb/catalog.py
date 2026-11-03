"""MongoDB ``Catalog`` and ``RecordStore`` facets.

Predicates (request scope and ACL) are pushed down as query filters; population counts use
``count_documents``; ``exists`` is a single ``find_one``; ``values`` groups distinct typed rows
server-side under a cardinality cap. Measures and groups are computed over the matched rows with the
shared ``backends/_scope.py`` helpers, exactly as the sqlite adapter does, because ``ScopeResult``
carries ``matched_ids`` and the rows must be fetched anyway; this keeps results identical across
adapters (plan Phase 1 acceptance) at no extra round trip.
"""

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
from ._db import KIND, combine, field_rows, mapped, page_window, predicate_filter, row_as_string, scope_filter

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import MongoBackend


def _chunk_doc(chunk: Chunk) -> dict[str, Any]:
    return {
        "_id": chunk.id,
        "document_id": chunk.document_id,
        "field": chunk.field,
        "start": chunk.start,
        "end": chunk.end,
        "text": chunk.text,
        "recipe": chunk.recipe,
    }


def chunk_from_doc(doc: dict[str, Any]) -> Chunk:
    return Chunk(
        id=doc["_id"],
        document_id=doc["document_id"],
        field=doc["field"],
        start=doc["start"],
        end=doc["end"],
        text=doc["text"],
        recipe=doc.get("recipe", "default"),
    )


class MongoCatalog:
    def __init__(self, backend: MongoBackend):
        self._b = backend

    def put_documents(self, gen: GenerationId, documents: Sequence[Document], chunks: Sequence[Chunk]) -> None:
        from pymongo import ReplaceOne

        self._b.require_generation(gen, create=True)
        docs = self._b.gen_collection(gen, "documents")
        with mapped("put_documents"):
            if documents:
                docs.bulk_write(
                    [
                        ReplaceOne(
                            {"_id": d.id},
                            {"_id": d.id, "doc": d.model_dump_json(), "fields": field_rows(document_values(d))},
                            upsert=True,
                        )
                        for d in documents
                    ],
                    ordered=True,
                )
            if chunks:
                wanted = sorted({c.document_id for c in chunks})
                known = {d["_id"] for d in docs.find({"_id": {"$in": wanted}}, {"_id": 1})}
                for chunk in chunks:
                    if chunk.document_id not in known:
                        raise BackendError(KIND, "put_documents", f"chunk {chunk.id} references unknown document")
                self._b.gen_collection(gen, "chunks").bulk_write(
                    [ReplaceOne({"_id": c.id}, _chunk_doc(c), upsert=True) for c in chunks], ordered=True
                )

    def get(self, gen: GenerationId, ids: Sequence[str]) -> list[Document]:
        if not ids:
            return []
        self._b.require_generation(gen)
        self._b.stats["catalog.get"] += 1
        self._b.stats["catalog.get.ids"] += len(ids)
        with mapped("get"):
            rows = list(self._b.gen_collection(gen, "documents").find({"_id": {"$in": list(ids)}}, {"doc": 1}))
        found = {row["_id"]: Document.model_validate_json(row["doc"]) for row in rows}
        return [found[i] for i in ids if i in found]

    def get_chunks(self, gen: GenerationId, chunk_ids: Sequence[str]) -> list[Chunk]:
        if not chunk_ids:
            return []
        self._b.require_generation(gen)
        self._b.stats["catalog.get_chunks"] += 1
        with mapped("get_chunks"):
            rows = list(self._b.gen_collection(gen, "chunks").find({"_id": {"$in": list(chunk_ids)}}))
        found = {row["_id"]: chunk_from_doc(row) for row in rows}
        return [found[i] for i in chunk_ids if i in found]

    def iter_scope(self, gen: GenerationId, scope: Scope | None, page: Page) -> PageResult[Document]:
        offset, size = page_window(page, 50)
        self._b.require_generation(gen)
        query = scope_filter(scope)
        if query is None:
            return PageResult(items=[], next_cursor=None)
        with mapped("iter_scope"):
            rows = list(
                self._b.gen_collection(gen, "documents").find(query, {"doc": 1}).sort("_id", 1).skip(offset).limit(size + 1)
            )
        items = [Document.model_validate_json(r["doc"]) for r in rows[:size]]
        return PageResult(items=items, next_cursor=str(offset + size) if len(rows) > size else None)

    def count_documents(self, gen: GenerationId) -> int:
        self._b.require_generation(gen)
        with mapped("count_documents"):
            return int(self._b.gen_collection(gen, "documents").count_documents({}))

    def count_chunks(self, gen: GenerationId) -> int:
        self._b.require_generation(gen)
        with mapped("count_chunks"):
            return int(self._b.gen_collection(gen, "chunks").count_documents({}))

    def delete_generation(self, gen: GenerationId) -> None:
        self._b.delete_generation(gen)


class MongoRecords:
    def __init__(self, backend: MongoBackend):
        self._b = backend

    def upsert(self, gen: GenerationId, records: Sequence[StructuredRecord], schema: MetadataSchema) -> None:
        from pymongo import ReplaceOne

        self._b.require_generation(gen, create=True)
        self._b.set_metadata_schema(gen, schema)
        operations = []
        for record in records:
            violations = record_violations(schema, record)
            if violations:
                raise BackendError(
                    KIND, "upsert", f"record {record.id}: {violations[0].field} {violations[0].category}: {violations[0].message}"
                )
            operations.append(
                ReplaceOne(
                    {"_id": record.id},
                    {"_id": record.id, "schema_id": record.schema_id, "rec": record.model_dump_json(), "fields": field_rows(record.values)},
                    upsert=True,
                )
            )
        if operations:
            with mapped("upsert"):
                self._b.gen_collection(gen, "records").bulk_write(operations, ordered=True)

    def count(self, gen: GenerationId) -> int:
        self._b.require_generation(gen)
        with mapped("count"):
            return int(self._b.gen_collection(gen, "records").count_documents({}))

    @staticmethod
    def _casefold(schema: MetadataSchema, field: str) -> bool:
        spec = schema.field(field)
        return bool(spec and spec.casefold_match)

    def _filter(self, schema: MetadataSchema, predicates: Sequence[Predicate]) -> dict[str, Any]:
        return combine(*[predicate_filter(p, self._casefold(schema, p.field), spec=schema.field(p.field)) for p in predicates])

    def execute(self, gen: GenerationId, scope: ScopeSpec, acl: ScopePredicates) -> ScopeResult:
        schema = self._b.metadata_schema(gen)
        applied, unsupported = [], []
        for predicate in scope.predicates:
            reason = unsupported_reason(schema, predicate)
            if reason is not None:
                unsupported.append(UnsupportedConstraint(constraint_text=predicate.field, reason=reason))
            else:
                applied.append(predicate)
        collection = self._b.gen_collection(gen, "records")
        with mapped("execute"):
            population = int(collection.count_documents(self._filter(schema, acl)))
            cap = matched_cap(self._b)
            rows = list(collection.find(self._filter(schema, [*acl, *applied]), {"rec": 1}).sort("_id", 1).limit(cap + 1))
        check_matched(KIND, rows, cap)
        matched = [StructuredRecord.model_validate_json(r["rec"]) for r in rows]
        return scope_result(scope, matched, population, schema, applied, unsupported)

    def exists(self, gen: GenerationId, field: str, value: Any, acl: ScopePredicates) -> bool:
        schema = self._b.metadata_schema(gen)
        query = self._filter(schema, [*acl, Predicate(field=field, op="eq", value=value)])
        with mapped("exists"):
            return self._b.gen_collection(gen, "records").find_one(query, {"_id": 1}) is not None

    def values(self, gen: GenerationId, field: str, acl: ScopePredicates, limit: int) -> list[str]:
        schema = self._b.metadata_schema(gen)
        spec = schema.field(field)
        if spec is None or not spec.listable:
            raise BackendError(KIND, "values", f"field {field!r} is not listable")
        cap = self._b.max_values_cardinality
        pipeline: list[dict[str, Any]] = [
            {"$match": self._filter(schema, acl)},
            {"$unwind": "$fields"},
            {"$match": {"fields.name": field, "fields.null": False}},
            {"$group": {"_id": {"k": "$fields.k", "t": "$fields.t", "n": "$fields.n"}}},
            {"$limit": cap + 1},
        ]
        with mapped("values"):
            groups = list(self._b.gen_collection(gen, "records").aggregate(pipeline))
        if len(groups) > cap:
            raise BackendError(KIND, "values", f"field {field!r} has more than {cap} distinct values; refusing to enumerate")
        seen = {row_as_string(g["_id"]) for g in groups}
        return sorted(seen)[: max(0, limit)]
