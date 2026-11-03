"""Neo4j adapter on the frozen contracts (plan §3.5 ``neo4j`` row): the ``graph`` capability with
generation-scoped nodes, provenance on edges, fixed parameterised Cypher templates only, memberships
and communities as nodes, and an atomic per-dataset generation pointer node."""

from .adapter import Neo4jBackend

__all__ = ["Neo4jBackend"]
