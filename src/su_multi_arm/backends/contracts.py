"""Backend capability contracts (plan §3.5). **Frozen in Phase 0.**

Any change after the freeze needs a plan changelog entry and a conformance-kit update. Invariants
shared by every contract:

* every operation is generation-scoped (``gen: GenerationId``); adapters never read ``CURRENT``;
* index/table names are ``<prefix>_<dataset>_<generation>``; ``refresh(gen)`` makes writes visible;
* the only destructive operation is ``delete_generation(gen)`` and callers must refuse it for the
  active generation or one pinned by a live session;
* reads return pages or ``k`` hits, never the corpus; the catalog read count per request is bounded
  by the number of hits (principle 2.1.7);
* failures raise ``BackendError(adapter, operation, message)``; timeouts are configured per adapter.

Not a backend's job: ranking fusion, eligibility logic beyond the pushed-down scope, any ML.
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable

import numpy as np

from ..core.types import (
    CacheEntry,
    Chunk,
    Document,
    Entity,
    GenerationId,
    LedgerEntry,
    LexicalQuery,
    Membership,
    MetadataSchema,
    Page,
    PageResult,
    Path,
    RawReply,
    Relation,
    Scope,
    ScopePredicates,
    ScopeResult,
    ScopeSpec,
    ScoredChunk,
    SessionRecord,
    StructuredRecord,
    TraversalSpec,
)

__all__ = [
    "Catalog",
    "LexicalIndex",
    "VectorIndex",
    "RecordStore",
    "GraphStore",
    "CacheStore",
    "LedgerStore",
    "SessionStore",
    "CONTRACT_NAMES",
]


@runtime_checkable
class Catalog(Protocol):
    """Canonical documents and chunks per generation."""

    def put_documents(self, gen: GenerationId, documents: Sequence[Document], chunks: Sequence[Chunk]) -> None: ...

    def get(self, gen: GenerationId, ids: Sequence[str]) -> list[Document]: ...

    def get_chunks(self, gen: GenerationId, chunk_ids: Sequence[str]) -> list[Chunk]: ...

    def iter_scope(self, gen: GenerationId, scope: Scope | None, page: Page) -> PageResult[Document]: ...

    def delete_generation(self, gen: GenerationId) -> None: ...


@runtime_checkable
class LexicalIndex(Protocol):
    """Capability flags: ``phrase``, ``highlight``, ``field_boost``, ``analyzer_id``."""

    def index(self, gen: GenerationId, chunks: Sequence[Chunk], fields: Sequence[str]) -> None: ...

    def refresh(self, gen: GenerationId) -> None: ...

    def search(
        self, gen: GenerationId, query: LexicalQuery, scope: Scope | None, k: int, page: Page | None = None
    ) -> PageResult[ScoredChunk]: ...


@runtime_checkable
class VectorIndex(Protocol):
    """Capability flags: ``exact``, ``ann`` (method), ``filter_pushdown``, ``max_dimensions``, ``namespaces``."""

    def upsert(
        self, gen: GenerationId, ids: Sequence[str], vectors: np.ndarray, namespace: str = "default"
    ) -> None: ...

    def refresh(self, gen: GenerationId) -> None: ...

    def search(
        self, gen: GenerationId, vector: np.ndarray, scope: Scope | None, k: int, namespace: str = "default"
    ) -> list[ScoredChunk]: ...


@runtime_checkable
class RecordStore(Protocol):
    """Typed records; ``ScopeResult`` carries exactness, denominator, applied/unsupported predicates."""

    def upsert(self, gen: GenerationId, records: Sequence[StructuredRecord], schema: MetadataSchema) -> None: ...

    def execute(self, gen: GenerationId, scope: ScopeSpec, acl: ScopePredicates) -> ScopeResult: ...

    def exists(self, gen: GenerationId, field: str, value: Any, acl: ScopePredicates) -> bool: ...

    def values(self, gen: GenerationId, field: str, acl: ScopePredicates, limit: int) -> list[str]: ...


@runtime_checkable
class GraphStore(Protocol):
    """Templated traversal only; ``Path`` edges carry provenance and edge kind."""

    def upsert(
        self,
        gen: GenerationId,
        entities: Sequence[Entity],
        relations: Sequence[Relation],
        memberships: Sequence[Membership],
    ) -> None: ...

    def traverse(self, gen: GenerationId, spec: TraversalSpec, scope: Scope | None) -> list[Path]: ...


@runtime_checkable
class CacheStore(Protocol):
    """Keys always start with ``GenerationId.prefix()``; adapters reject other keys."""

    def get(self, key: str) -> CacheEntry | None: ...

    def put(self, key: str, value: Any, ttl: float) -> None: ...

    def invalidate(self, prefix: str) -> int: ...


@runtime_checkable
class LedgerStore(Protocol):
    """Ingestion job ledger with raw model replies (valid or not) kept for replay."""

    def append(self, job_id: str, entry: LedgerEntry) -> None: ...

    def replay(self, job_id: str) -> list[LedgerEntry]: ...

    def mark(self, job_id: str, status: str) -> None: ...

    def put_raw(self, key: str, raw_reply: str, valid: bool) -> None: ...

    def get_raw(self, key: str) -> RawReply | None: ...


@runtime_checkable
class SessionStore(Protocol):
    def create(self, record: SessionRecord) -> None: ...

    def get(self, session_id: str) -> SessionRecord | None: ...

    def touch(self, session_id: str, now: float, record: SessionRecord | None = None, expected_version: int | None = None) -> None:
        """Store ``record`` (or the stored one) with ``last_seen_at=now`` and ``version`` bumped.
        With ``expected_version`` this is an atomic compare-and-set: ``SessionConflict`` when the
        stored version differs (amendment 2.0.0a2, QA-05; see ``backends/_sessions.py``)."""
        ...

    def expire(self, now: float, ttl_seconds: float) -> int: ...


CONTRACT_NAMES: dict[str, type] = {
    "catalog": Catalog,
    "lexical": LexicalIndex,
    "vector": VectorIndex,
    "records": RecordStore,
    "graph": GraphStore,
    "cache": CacheStore,
    "ledger": LedgerStore,
    "session": SessionStore,
}
