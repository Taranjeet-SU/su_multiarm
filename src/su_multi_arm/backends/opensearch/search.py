"""OpenSearch ``LexicalIndex`` (BM25 over the ``standard`` analyzer, ``multi_match`` phrase, boolean,
highlights, title boost) and ``VectorIndex`` (exact cosine by default through a ``script_score``;
HNSW behind ``knn.method: hnsw`` with mandatory exact rescoring of the ANN candidates) facets.

Lexical semantics are those of the engine (plan §3.5): the ``standard`` analyzer tokenises and
lowercases; a phrase query matches only contiguous tokens *as analysed by Lucene*; ranking is BM25
per field summed over ``text`` and ``title`` (boosted 2). No parity with FTS5 or PostgreSQL text
search is claimed. Scope is a native ``bool.filter`` on the denormalised typed fields of the chunk,
never a post-filter; ordering is ``_score`` then ``chunk_id`` so results are deterministic.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, Sequence

import numpy as np

from ...core.errors import BackendError
from ...core.types import Chunk, GenerationId, LexicalQuery, Page, PageResult, Scope, ScoredChunk
from .._lucene_common import bulk_body, check_bulk, lexical_query, page_window, scope_filter
from .catalog import chunk_source

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import OpenSearchBackend

KIND = "opensearch"
LEXICAL_FIELDS = ("text", "title^2")
HIT_SOURCE = ["chunk_id", "document_id"]
SORT = [{"_score": "desc"}, {"chunk_id": "asc"}]
MAX_DIMENSIONS = 16000
ANN_OVERSAMPLE = 4
MAX_WINDOW = 10000
EXACT_SCRIPT = "Math.max(0.0, 1.0 + cosineSimilarity(params.query_value, doc[params.field]))"


def vector_field(namespace: str) -> str:
    if namespace == "default":
        return "vec_default"
    return "vec_" + hashlib.sha256(namespace.encode()).hexdigest()[:16]


class OpenSearchLexical:
    def __init__(self, backend: OpenSearchBackend):
        self._b = backend

    def index(self, gen: GenerationId, chunks: Sequence[Chunk], fields: Sequence[str]) -> None:
        wanted = [c for c in chunks if not fields or c.field in fields]
        if not wanted:
            self._b.ensure_index(gen, "chunks", "index")
            return
        documents = self._b.catalog._documents(gen, [c.document_id for c in wanted], "index")
        actions = []
        for chunk in wanted:
            document = documents.get(chunk.document_id)
            if document is None:
                raise BackendError(self._b.kind, "index", f"chunk {chunk.id} references unknown document")
            source = chunk_source(chunk, document)
            source["lexical"] = True
            actions.append(({"update": {"_id": chunk.id}}, {"doc": source, "doc_as_upsert": True}))
        name = self._b.ensure_index(gen, "chunks", "index")
        reply = self._b.call("index", "POST", f"/{name}/_bulk", body=bulk_body(actions))
        check_bulk(self._b.kind, "index", reply, lambda i: f"chunk {i!r}")

    def refresh(self, gen: GenerationId) -> None:
        self._b.refresh(gen, "refresh")

    def count(self, gen: GenerationId) -> int:
        name = self._b.require_index(gen, "chunks", "count")
        reply = self._b.call("count", "POST", f"/{name}/_count", body={"query": {"term": {"lexical": True}}})
        return int(reply["count"])

    def search(
        self, gen: GenerationId, query: LexicalQuery, scope: Scope | None, k: int, page: Page | None = None
    ) -> PageResult[ScoredChunk]:
        text_query = lexical_query(query, LEXICAL_FIELDS)
        if text_query is None:
            return PageResult(items=[], next_cursor=None)
        offset, size = page_window(self._b.kind, page, k)
        window = min(size, max(0, k - offset))
        if window == 0:
            return PageResult(items=[], next_cursor=None)
        filters: list[dict[str, Any]] = [{"term": {"lexical": True}}, *scope_filter(scope, "document_id")]
        if query.fields:
            filters.append({"terms": {"field": list(query.fields)}})
        body: dict[str, Any] = {
            "from": offset,
            "size": window + 1,
            "_source": HIT_SOURCE,
            "sort": SORT,
            "track_total_hits": False,
            "query": {"bool": {"must": [text_query], "filter": filters}},
            "highlight": {"fields": {"text": {}, "title": {}}, "number_of_fragments": 3},
        }
        name = self._b.require_index(gen, "chunks", "search")
        hits = self._b.call("search", "POST", f"/{name}/_search", body=body)["hits"]["hits"]
        rows = hits[:window]
        items = [
            ScoredChunk(
                chunk_id=h["_source"]["chunk_id"],
                document_id=h["_source"]["document_id"],
                score=float(h["_score"]),
                highlights=[f for fragments in (h.get("highlight") or {}).values() for f in fragments],
            )
            for h in rows
        ]
        more = len(hits) > len(rows) and offset + len(rows) < k
        return PageResult(items=items, next_cursor=str(offset + len(rows)) if more else None)


class OpenSearchVector:
    vector_type = "knn_vector"  # mapping type of a namespace field (engine-specific)
    dimension_key = "dimension"  # mapping key carrying the dimension (engine-specific)

    def __init__(self, backend: OpenSearchBackend):
        self._b = backend
        self._dimensions: dict[tuple[str, str], int] = {}

    def forget(self, gen: GenerationId) -> None:
        for key in [k for k in self._dimensions if k[0] == gen.prefix()]:
            del self._dimensions[key]

    def _field_mapping(self, dimension: int) -> dict[str, Any]:
        mapping: dict[str, Any] = {"type": "knn_vector", "dimension": dimension}
        if self._b.knn_method == "hnsw":
            mapping["method"] = {"name": "hnsw", "space_type": "cosinesimil", "engine": "lucene", "parameters": {"ef_construction": 128, "m": 16}}
        return mapping

    def _dimension(self, gen: GenerationId, namespace: str, operation: str) -> int | None:
        """Declared dimension of the namespace's field (``None`` when no vector was ever stored)."""
        key = (gen.prefix(), namespace)
        if key in self._dimensions:
            return self._dimensions[key]
        name = self._b.require_index(gen, "chunks", operation)
        reply = self._b.call(operation, "GET", f"/{name}/_mapping")
        properties = reply.get(name, {}).get("mappings", {}).get("properties", {})
        field = properties.get(vector_field(namespace))
        if field is None:
            return None
        if field.get("type") != self.vector_type or not isinstance(field.get(self.dimension_key), int):
            raise BackendError(self._b.kind, operation, f"field {vector_field(namespace)} is not a {self.vector_type} (mapping mismatch)")
        self._dimensions[key] = int(field[self.dimension_key])
        return self._dimensions[key]

    def upsert(self, gen: GenerationId, ids: Sequence[str], vectors: np.ndarray, namespace: str = "default") -> None:
        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] != len(ids):
            raise BackendError(self._b.kind, "upsert", "vectors must be a 2-D array with one row per id")
        if len(set(ids)) != len(ids):
            raise BackendError(self._b.kind, "upsert", "duplicate ids")
        if not np.isfinite(matrix).all():
            raise BackendError(self._b.kind, "upsert", "vectors must be finite")
        dimension = int(matrix.shape[1])
        if dimension < 1 or dimension > MAX_DIMENSIONS:
            raise BackendError(self._b.kind, "upsert", f"vector dimension {dimension} outside 1..{MAX_DIMENSIONS}")
        name = self._b.ensure_index(gen, "chunks", "upsert")
        field = vector_field(namespace)
        known = self._dimension(gen, namespace, "upsert")
        if known is None:
            self._b.call("upsert", "PUT", f"/{name}/_mapping", body={"properties": {field: self._field_mapping(dimension)}})
            self._dimensions[(gen.prefix(), namespace)] = dimension
        elif known != dimension:
            raise BackendError(self._b.kind, "upsert", f"vector dimension {dimension} differs from the stored vectors ({known})")
        if not ids:
            return
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        matrix = np.divide(matrix, norms, out=np.zeros_like(matrix), where=norms > 0)
        actions = [({"update": {"_id": cid}}, {"doc": {field: [float(x) for x in row]}}) for cid, row in zip(ids, matrix)]
        reply = self._b.call("upsert", "POST", f"/{name}/_bulk", body=bulk_body(actions))
        check_bulk(self._b.kind, "upsert", reply, lambda i: f"vector id {i!r}")

    def refresh(self, gen: GenerationId) -> None:
        self._b.refresh(gen, "refresh")

    def count(self, gen: GenerationId, namespace: str = "default") -> int:
        name = self._b.require_index(gen, "chunks", "count")
        reply = self._b.call("count", "POST", f"/{name}/_count", body={"query": {"exists": {"field": vector_field(namespace)}}})
        return int(reply["count"])

    def _exact_script(self, field: str) -> str:
        """Painless source of the exact cosine score (engine-specific function signature)."""
        return EXACT_SCRIPT

    def _ann_candidate_ids(self, name: str, field: str, values: list[float], filters: list[dict[str, Any]], candidates: int) -> list[str]:
        """Chunk ids of the ANN candidates (engine-specific k-NN query); the caller rescores them exactly."""
        knn: dict[str, Any] = {"vector": values, "k": candidates}
        if filters:
            knn["filter"] = {"bool": {"filter": filters}}
        body = {"size": candidates, "_source": ["chunk_id"], "track_total_hits": False, "query": {"knn": {field: knn}}}
        ann_hits = self._b.call("search", "POST", f"/{name}/_search", body=body)["hits"]["hits"]
        return [h["_source"]["chunk_id"] for h in ann_hits]

    def _exact_body(self, field: str, query: list[float], filters: list[dict[str, Any]], size: int) -> dict[str, Any]:
        return {
            "size": size,
            "_source": HIT_SOURCE,
            "sort": SORT,
            "track_total_hits": False,
            "query": {
                "script_score": {
                    "query": {"bool": {"filter": [{"exists": {"field": field}}, *filters]}},
                    "script": {"source": self._exact_script(field), "lang": "painless", "params": {"field": field, "query_value": query}},
                }
            },
        }

    def search(
        self, gen: GenerationId, vector: np.ndarray, scope: Scope | None, k: int, namespace: str = "default"
    ) -> list[ScoredChunk]:
        if k < 1:
            raise BackendError(self._b.kind, "search", "k must be positive")
        if k > MAX_WINDOW:
            raise BackendError(self._b.kind, "search", f"k above the result window {MAX_WINDOW}")
        dimension = self._dimension(gen, namespace, "search")
        if dimension is None:
            return []
        query = np.asarray(vector, dtype=np.float32)
        if query.shape != (dimension,):
            raise BackendError(self._b.kind, "search", "query vector dimension mismatch")
        norm = float(np.linalg.norm(query))
        if norm == 0:
            return []
        values = [float(x) for x in query / norm]
        field = vector_field(namespace)
        filters = scope_filter(scope, "document_id")
        name = self._b.index(gen, "chunks")
        if self._b.knn_method == "hnsw":
            candidates = min(MAX_WINDOW, max(50, ANN_OVERSAMPLE * k))
            candidate_ids = self._ann_candidate_ids(name, field, values, filters, candidates)
            if not candidate_ids:
                return []
            filters = [*filters, {"terms": {"chunk_id": candidate_ids}}]
        reply = self._b.call("search", "POST", f"/{name}/_search", body=self._exact_body(field, values, filters, k))
        return [
            ScoredChunk(chunk_id=h["_source"]["chunk_id"], document_id=h["_source"]["document_id"], score=float(h["_score"]) - 1.0)
            for h in reply["hits"]["hits"]
        ]
