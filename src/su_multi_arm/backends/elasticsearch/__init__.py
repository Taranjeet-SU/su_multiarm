"""Elasticsearch adapter on the frozen contracts (plan §3.5 ``elasticsearch`` row, Phase 1): the
OpenSearch adapter classes over ``backends/_lucene_common.py`` with the engine-specific parts
(official client transport, 8.x probe, ``dense_vector`` exact/HNSW vectors, alias-backed generation
registry) replaced. Needs the ``elasticsearch`` extra."""

from .adapter import ElasticsearchBackend

__all__ = ["ElasticsearchBackend"]
