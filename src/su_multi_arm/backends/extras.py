"""Adapter extension points used by the arms and the write path **beyond** the frozen contracts.

The eight protocols in ``contracts.py`` are frozen. Phase 0 needs a few additional, read-mostly
operations that the plan assigns to the same stores (mention lookup for the graph arm, community
objects for the community arm, row counts for per-projection validation). They live here as
separately named protocols so the freeze stays honest: an adapter that implements a contract but not
an extra is still conformant, and the arms/write path check for the extra explicitly and fail with a
typed error instead of guessing. Promotion into the contracts is a changelog decision (plan §3.5).
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable

from ..communities.base import Community
from ..core.errors import BackendError
from ..core.types import Entity, GenerationId, Membership, Scope


@runtime_checkable
class CatalogCounts(Protocol):
    def count_documents(self, gen: GenerationId) -> int: ...

    def count_chunks(self, gen: GenerationId) -> int: ...


@runtime_checkable
class Countable(Protocol):
    """``LexicalIndex``/``RecordStore``: visible row count. ``VectorIndex``: per namespace."""

    def count(self, gen: GenerationId) -> int: ...


@runtime_checkable
class GraphExtras(Protocol):
    def find_entities(self, gen: GenerationId, tokens: Sequence[str], scope: Scope | None) -> list[Entity]:
        """Distinct entities (one representative mention each) whose name tokens are all in ``tokens``
        and that have at least one mention inside ``scope``. Bounded by the query, not the corpus."""
        ...

    def mentions(self, gen: GenerationId, entity_ids: Sequence[str], scope: Scope | None, limit: int) -> list[Entity]:
        """Mentions (span-anchored) of the given entities inside ``scope``, at most ``limit``."""
        ...

    def memberships(self, gen: GenerationId) -> list[Membership]: ...

    def put_communities(self, gen: GenerationId, communities: Sequence[Community]) -> None: ...

    def communities(self, gen: GenerationId) -> list[Community]: ...


@runtime_checkable
class GenerationPointer(Protocol):
    """A backend that keeps its **own** persisted active-generation pointer next to the authoritative
    ``CURRENT`` of the catalog's generation registry (neo4j: one pointer node per dataset). The write
    path moves it just before the authoritative switch and moves it back if that switch fails; a
    failed load never reaches it. Adapters still never read it on the request path."""

    def activate_generation(self, gen: GenerationId) -> None: ...

    def deactivate_generation(self, gen: GenerationId) -> None: ...

    def active_generation(self, dataset: str) -> str | None: ...


@runtime_checkable
class GenerationIdentity(Protocol):
    """The backend-specific physical names of one generation (schema, index names, collection prefix,
    pointer key, snapshot folder) for the generation manifest. Never reads ``CURRENT``. Optional: the
    write path records ``{"key": gen.prefix()}`` for an adapter without it."""

    def generation_identity(self, gen: GenerationId) -> dict[str, Any]: ...


def require(adapter: Any, protocol: type, capability: str, operation: str) -> Any:
    """Return the facet when it structurally satisfies ``protocol``; otherwise a typed ``BackendError``."""
    facet = getattr(adapter, capability, None)
    if facet is None or not isinstance(facet, protocol):
        kind = getattr(adapter, "kind", type(adapter).__name__)
        raise BackendError(kind, operation, f"{capability} facet does not implement {protocol.__name__}")
    return facet
