"""OpenSearch adapter on the frozen contracts (plan §3.5 ``opensearch`` row, Phase 1): catalog
(document index), BM25 lexical with phrase/highlights/field boost, exact k-NN (HNSW opt-in with
exact rescore), typed records with native filter pushdown. Generation-named indices, alias
activation. Needs the ``opensearch`` extra (``opensearch-py``)."""

from .adapter import OpenSearchBackend

__all__ = ["OpenSearchBackend"]
