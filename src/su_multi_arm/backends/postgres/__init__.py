"""PostgreSQL adapter on the frozen contracts (plan §3.5 ``postgres`` row, Phase 1): catalog, full-text
lexical search, pgvector (exact by default, HNSW opt-in behind a filter-recall gate), typed records
with pushed-down predicates, cache, ledger with raw replies, sessions. One schema per generation;
graph is ``planned``. Supersedes the v1 ``adapters/postgres.py`` (dense + FTS only)."""

from .adapter import PostgresBackend
from .search import HnswSettings

__all__ = ["PostgresBackend", "HnswSettings"]
