"""Elasticsearch ``VectorIndex`` facet: the OpenSearch facet with ``dense_vector`` fields.

Exact (default): ``dense_vector`` with ``index: false`` scored by ``cosineSimilarity`` in a
``script_score`` over the pushed-down filter (identical results to the memory/sqlite adapters).
``knn.method: hnsw``: ``index: true`` with an explicit float ``hnsw`` graph (``index_options.type:
hnsw``, never the engine's default int8 quantisation), candidates from the top-level ``knn`` search
option with the scope as its pre-filter, then the same exact rescore as OpenSearch. The lexical
facet is inherited unchanged (``multi_match`` over the ``standard`` analyzer).
"""

from __future__ import annotations

from typing import Any

from ..opensearch.search import MAX_WINDOW, OpenSearchLexical, OpenSearchVector

MAX_DIMENSIONS = 4096  # Elasticsearch 8.x float dense_vector limit


class ElasticsearchLexical(OpenSearchLexical):
    pass


class ElasticsearchVector(OpenSearchVector):
    vector_type = "dense_vector"
    dimension_key = "dims"

    def _field_mapping(self, dimension: int) -> dict[str, Any]:
        if self._b.knn_method == "hnsw":
            return {
                "type": "dense_vector",
                "dims": dimension,
                "index": True,
                "similarity": "cosine",
                "index_options": {"type": "hnsw", "m": 16, "ef_construction": 128},
            }
        return {"type": "dense_vector", "dims": dimension, "index": False}

    def _exact_script(self, field: str) -> str:
        # ``field`` is always ``vec_default`` or ``vec_<16 hex>`` (``vector_field``): safe to inline.
        return f"Math.max(0.0, 1.0 + cosineSimilarity(params.query_value, '{field}'))"

    def _ann_candidate_ids(self, name: str, field: str, values: list[float], filters: list[dict[str, Any]], candidates: int) -> list[str]:
        knn: dict[str, Any] = {"field": field, "query_vector": values, "k": candidates, "num_candidates": min(MAX_WINDOW, 2 * candidates)}
        if filters:
            knn["filter"] = {"bool": {"filter": filters}}
        body = {"size": candidates, "_source": ["chunk_id"], "track_total_hits": False, "knn": knn}
        ann_hits = self._b.call("search", "POST", f"/{name}/_search", body=body)["hits"]["hits"]
        return [h["_source"]["chunk_id"] for h in ann_hits]
