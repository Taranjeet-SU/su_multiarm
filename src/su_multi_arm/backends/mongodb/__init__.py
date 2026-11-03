"""MongoDB adapter on the frozen contracts (plan §3.5 ``mongodb`` row, Phase 1): catalog, text-index
lexical, typed records with pushed-down predicates, cache, ledger with raw replies, sessions.
Graph is not declared; vector search is Atlas-only and stays ``planned`` (execution log, assumed
default #8) because it cannot be exercised against a community server."""

from .adapter import MongoBackend

__all__ = ["MongoBackend"]
