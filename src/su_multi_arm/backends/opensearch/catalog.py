"""OpenSearch ``Catalog`` and ``RecordStore`` facets.

Catalog reads that go through a search (``iter_scope``, counts) refresh the generation's indices
first so the facet is read-your-writes like the sqlite and memory adapters; ``get``/``get_chunks``
use real-time ``_mget``. Records are written with ``refresh=true`` because ``RecordStore`` has no
``refresh`` method on the contract.

``execute`` pushes the ACL and every supported predicate down as a native ``bool.filter``; the
denominator is a ``_count``; the matched records are then scanned (``search_after`` pages, bounded by
``max_matched``, typed error above it) and the measure is computed by ``_scope.scope_result`` so the
``ScopeResult`` is exact and identical to the other adapters for the same data. That is the default
``records.aggregation: client``. Under ``native`` the operations are compiled to aggregations by
``_lucene_aggs.NativeAggregation`` (no record scan, no ``max_matched`` bound; ``matched_ids`` capped at
``matched_ids_cap`` with ``matched_ids_truncated``; ``cardinality`` only above the exact distinct cap,
labelled ``approximate``).
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
from .._lucene_aggs import NativeAggregation
from .._lucene_common import MAX_VALUES, SCAN_PAGE, bulk_body, check_bulk, predicates_filter, scope_filter, typed_fields
from .._scope import document_values, record_violations, scope_result, unsupported_reason

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import OpenSearchBackend

KIND = "opensearch"
CHUNK_SOURCE = ["chunk_id", "document_id", "field", "start", "end", "text", "recipe"]


def chunk_source(chunk: Chunk, document: Document | None) -> dict[str, Any]:
    source: dict[str, Any] = {
        "chunk_id": chunk.id,
        "document_id": chunk.document_id,
        "field": chunk.field,
        "start": chunk.start,
        "end": chunk.end,
        "text": chunk.text,
        "recipe": chunk.recipe,
    }
    if document is not None:
        source["title"] = document.title
        source.update(typed_fields(document_values(document)))
    return source


def document_source(document: Document) -> dict[str, Any]:
    return {"id": document.id, "data": document.model_dump(mode="json"), **typed_fields(document_values(document))}


class OpenSearchCatalog:
    def __init__(self, backend: OpenSearchBackend):
        self._b = backend

    def _documents(self, gen: GenerationId, ids: Sequence[str], operation: str) -> dict[str, Document]:
        """Real-time lookup bounded by ``ids``."""
        if not ids:
            return {}
        docs = self._b.require_index(gen, "docs", operation)
        reply = self._b.call(operation, "POST", f"/{docs}/_mget", params={"_source": "data"}, body={"ids": list(dict.fromkeys(ids))})
        found = {}
        for item in reply.get("docs", []):
            if item.get("found"):
                found[item["_id"]] = Document.model_validate(item["_source"]["data"])
        return found

    def put_documents(self, gen: GenerationId, documents: Sequence[Document], chunks: Sequence[Chunk]) -> None:
        docs = self._b.ensure_index(gen, "docs", "put_documents")
        chunk_index = self._b.ensure_index(gen, "chunks", "put_documents")
        by_id = {d.id: d for d in documents}
        needed = [c.document_id for c in chunks if c.document_id not in by_id]
        known = self._documents(gen, needed, "put_documents") if needed else {}
        for chunk in chunks:
            if chunk.document_id not in by_id and chunk.document_id not in known:
                raise BackendError(self._b.kind, "put_documents", f"chunk {chunk.id} references unknown document")
        if documents:
            # Documents re-put within a generation: their chunks carry denormalised scope fields and are re-derived.
            existing = self._documents(gen, list(by_id), "put_documents")
            reply = self._b.call(
                "put_documents",
                "POST",
                f"/{docs}/_bulk",
                body=bulk_body([({"index": {"_id": d.id}}, document_source(d)) for d in documents]),
            )
            check_bulk(self._b.kind, "put_documents", reply, lambda i: f"document {i!r}")
            for document_id in sorted(existing):
                self._redenormalise(gen, chunk_index, by_id[document_id])
        if chunks:
            actions = []
            for chunk in chunks:
                document = by_id.get(chunk.document_id) or known[chunk.document_id]
                actions.append(({"update": {"_id": chunk.id}}, {"doc": chunk_source(chunk, document), "doc_as_upsert": True}))
            reply = self._b.call("put_documents", "POST", f"/{chunk_index}/_bulk", body=bulk_body(actions))
            check_bulk(self._b.kind, "put_documents", reply, lambda i: f"chunk {i!r}")

    def _redenormalise(self, gen: GenerationId, chunk_index: str, document: Document) -> None:
        self._b.call("put_documents", "POST", f"/{chunk_index}/_refresh")
        fields = typed_fields(document_values(document))
        script = {
            "source": "ctx._source.title = params.title; for (k in params.fields.keySet()) { ctx._source[k] = params.fields[k]; }",
            "lang": "painless",
            "params": {"title": document.title, "fields": fields},
        }
        self._b.call(
            "put_documents",
            "POST",
            f"/{chunk_index}/_update_by_query",
            params={"conflicts": "abort", "refresh": "true"},
            body={"query": {"term": {"document_id": document.id}}, "script": script},
        )

    def get(self, gen: GenerationId, ids: Sequence[str]) -> list[Document]:
        if not ids:
            return []
        self._b.stats["catalog.get"] += 1
        self._b.stats["catalog.get.ids"] += len(ids)
        found = self._documents(gen, ids, "get")
        return [found[i] for i in ids if i in found]

    def get_chunks(self, gen: GenerationId, chunk_ids: Sequence[str]) -> list[Chunk]:
        if not chunk_ids:
            return []
        self._b.stats["catalog.get_chunks"] += 1
        chunk_index = self._b.require_index(gen, "chunks", "get_chunks")
        reply = self._b.call("get_chunks", "POST", f"/{chunk_index}/_mget", params={"_source": ",".join(CHUNK_SOURCE)}, body={"ids": list(dict.fromkeys(chunk_ids))})
        found = {}
        for item in reply.get("docs", []):
            if item.get("found"):
                source = item["_source"]
                found[item["_id"]] = Chunk(id=source["chunk_id"], **{k: source[k] for k in CHUNK_SOURCE if k != "chunk_id"})
        return [found[i] for i in chunk_ids if i in found]

    def iter_scope(self, gen: GenerationId, scope: Scope | None, page: Page) -> PageResult[Document]:
        size = page.size
        docs = self._b.require_index(gen, "docs", "iter_scope")
        self._b.call("iter_scope", "POST", f"/{docs}/_refresh")
        body: dict[str, Any] = {
            "size": size + 1,
            "_source": ["data"],
            "sort": [{"id": "asc"}],
            "query": {"bool": {"filter": scope_filter(scope, "id")}},
            "track_total_hits": False,
        }
        if page.cursor:
            body["search_after"] = [page.cursor]
        reply = self._b.call("iter_scope", "POST", f"/{docs}/_search", body=body)
        hits = reply["hits"]["hits"]
        items = [Document.model_validate(h["_source"]["data"]) for h in hits[:size]]
        next_cursor = items[-1].id if len(hits) > size else None
        return PageResult(items=items, next_cursor=next_cursor)

    def _count(self, gen: GenerationId, index_kind: str, operation: str, query: dict[str, Any] | None = None) -> int:
        name = self._b.require_index(gen, index_kind, operation)
        self._b.call(operation, "POST", f"/{name}/_refresh")
        reply = self._b.call(operation, "POST", f"/{name}/_count", body={"query": query} if query else None)
        return int(reply["count"])

    def count_documents(self, gen: GenerationId) -> int:
        return self._count(gen, "docs", "count_documents")

    def count_chunks(self, gen: GenerationId) -> int:
        return self._count(gen, "chunks", "count_chunks")

    def delete_generation(self, gen: GenerationId) -> None:
        self._b.delete_generation(gen)


class OpenSearchRecords:
    def __init__(self, backend: OpenSearchBackend):
        self._b = backend
        self._schemas: dict[str, MetadataSchema] = {}

    def _schema(self, gen: GenerationId, operation: str) -> MetadataSchema:
        name = self._b.require_index(gen, "records", operation)
        if name in self._schemas:
            return self._schemas[name]
        reply = self._b.call(operation, "GET", f"/{name}/_mapping")
        raw = reply.get(name, {}).get("mappings", {}).get("_meta", {}).get("metadata_schema")
        schema = MetadataSchema.model_validate(raw) if raw else MetadataSchema()
        self._schemas[name] = schema
        return schema

    def upsert(self, gen: GenerationId, records: Sequence[StructuredRecord], schema: MetadataSchema) -> None:
        name = self._b.ensure_index(gen, "records", "upsert")
        for record in records:
            violations = record_violations(schema, record)
            if violations:
                raise BackendError(
                    KIND, "upsert", f"record {record.id}: {violations[0].field} {violations[0].category}: {violations[0].message}"
                )
        current = self._b.call("upsert", "GET", f"/{name}/_mapping")
        meta_block = dict(current.get(name, {}).get("mappings", {}).get("_meta", {}))
        meta_block["metadata_schema"] = schema.model_dump(mode="json")
        self._b.call("upsert", "PUT", f"/{name}/_mapping", body={"_meta": meta_block})
        self._schemas[name] = schema
        if not records:
            return
        actions = [
            ({"index": {"_id": r.id}}, {"id": r.id, "schema_id": r.schema_id, "data": r.model_dump(mode="json"), **typed_fields(r.values, schema.field)})
            for r in records
        ]
        reply = self._b.call("upsert", "POST", f"/{name}/_bulk", params={"refresh": "true"}, body=bulk_body(actions))
        check_bulk(self._b.kind, "upsert", reply, lambda i: f"record {i!r}")

    def _casefold(self, schema: MetadataSchema, field: str) -> bool:
        spec = schema.field(field)
        return bool(spec and spec.casefold_match)

    def _filter(self, schema: MetadataSchema, predicates: Sequence[Predicate]) -> dict[str, Any]:
        return {"bool": {"filter": predicates_filter(predicates, lambda f: self._casefold(schema, f), schema.field)}}

    def count(self, gen: GenerationId) -> int:
        name = self._b.require_index(gen, "records", "count")
        return int(self._b.call("count", "POST", f"/{name}/_count")["count"])

    def _scan(self, gen: GenerationId, name: str, query: dict[str, Any], operation: str) -> list[StructuredRecord]:
        """Every matched record, in id order, bounded by ``max_matched`` (typed error above it)."""
        total = int(self._b.call(operation, "POST", f"/{name}/_count", body={"query": query})["count"])
        if total > self._b.max_matched:
            raise BackendError(self._b.kind, operation, f"scope matches {total} records, above max_matched {self._b.max_matched}; narrow the scope")
        rows: list[StructuredRecord] = []
        cursor: list[Any] | None = None
        while True:
            body: dict[str, Any] = {"size": SCAN_PAGE, "_source": ["data"], "sort": [{"id": "asc"}], "query": query, "track_total_hits": False}
            if cursor is not None:
                body["search_after"] = cursor
            hits = self._b.call(operation, "POST", f"/{name}/_search", body=body)["hits"]["hits"]
            rows.extend(StructuredRecord.model_validate(h["_source"]["data"]) for h in hits)
            if len(hits) < SCAN_PAGE:
                return rows
            cursor = hits[-1]["sort"]

    def execute(self, gen: GenerationId, scope: ScopeSpec, acl: ScopePredicates) -> ScopeResult:
        schema = self._schema(gen, "execute")
        name = self._b.index(gen, "records")
        applied, unsupported = [], []
        for predicate in scope.predicates:
            reason = unsupported_reason(schema, predicate)
            if reason is not None:
                unsupported.append(UnsupportedConstraint(constraint_text=predicate.field, reason=reason))
            else:
                applied.append(predicate)
        if self._b.aggregation == "native":
            self._b.stats["records.execute.native"] += 1
            return NativeAggregation(self._b, name, schema, lambda f: self._casefold(schema, f)).execute(scope, acl, applied, unsupported)
        population = int(self._b.call("execute", "POST", f"/{name}/_count", body={"query": self._filter(schema, acl)})["count"])
        matched = self._scan(gen, name, self._filter(schema, [*acl, *applied]), "execute")
        return scope_result(scope, matched, population, schema, applied, unsupported)

    def exists(self, gen: GenerationId, field: str, value: Any, acl: ScopePredicates) -> bool:
        schema = self._schema(gen, "exists")
        name = self._b.index(gen, "records")
        query = self._filter(schema, [*acl, Predicate(field=field, op="eq", value=value)])
        return int(self._b.call("exists", "POST", f"/{name}/_count", body={"query": query})["count"]) > 0

    def values(self, gen: GenerationId, field: str, acl: ScopePredicates, limit: int) -> list[str]:
        schema = self._schema(gen, "values")
        spec = schema.field(field)
        if spec is None or not spec.listable:
            raise BackendError(self._b.kind, "values", f"field {field!r} is not listable")
        if limit > MAX_VALUES:
            raise BackendError(self._b.kind, "values", f"limit {limit} exceeds the cardinality cap {MAX_VALUES}")
        if limit < 1:
            return []
        name = self._b.index(gen, "records")
        body = {
            "size": 0,
            "query": self._filter(schema, acl),
            "aggs": {"values": {"terms": {"field": f"v.{field}", "size": limit, "order": {"_key": "asc"}}}},
        }
        reply = self._b.call("values", "POST", f"/{name}/_search", body=body)
        return [str(b["key"]) for b in reply["aggregations"]["values"]["buckets"]]
