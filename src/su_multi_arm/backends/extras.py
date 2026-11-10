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

from pydantic import Field

from ..communities.base import Community
from ..core.errors import BackendError
from ..core.types import Entity, GenerationId, Membership, RawReply, Scope
from ..models import StrictModel


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
    failed load never reaches it. Adapters still never read it on the request path.

    Since 2.0.0a2 the built-in pointers (neo4j, OpenSearch/Elasticsearch aliases) also accept
    ``activate_generation(gen, expected=<generation or None>)``, a compare-and-set that raises
    ``WriteConflict(stale_base)`` and leaves the pointer unchanged when it names anything else. The
    write path passes ``expected`` only when the adapter's signature has it; an injected pointer
    without it is switched after a read-and-compare (not atomic; the write lease still excludes a
    second writer)."""

    def activate_generation(self, gen: GenerationId) -> None: ...

    def deactivate_generation(self, gen: GenerationId) -> None: ...

    def active_generation(self, dataset: str) -> str | None: ...


@runtime_checkable
class GenerationIdentity(Protocol):
    """The backend-specific physical names of one generation (schema, index names, collection prefix,
    pointer key, snapshot folder) for the generation manifest. Never reads ``CURRENT``. Optional: the
    write path records ``{"key": gen.prefix()}`` for an adapter without it."""

    def generation_identity(self, gen: GenerationId) -> dict[str, Any]: ...


@runtime_checkable
class HitParts(Protocol):
    """``Catalog`` (optional, read path only): what an arm needs to turn chunk hits into document hits,
    without materialising whole documents. For ``chunk_ids`` in rank order it returns
    ``(chunks, heads)``: ``chunks[id] = (document_id, field, start, end, text)`` with ``text`` set only
    for the first chunk of each document (``None`` otherwise), and ``heads[document_id] = (title,
    source, url)`` for the documents that exist. Unknown ids are absent. Arms use it when present
    and ``get_chunks``/``get`` otherwise; both give identical hits."""

    def hit_parts(self, gen: GenerationId, chunk_ids: Sequence[str]) -> tuple[dict[str, tuple], dict[str, tuple]]: ...


class PurgeSpec(StrictModel):
    """What ``LedgerRetention.purge`` removes (addendum A1 §3.8.3, B6/n2), in this order:

    1. the reference rows of ``drop_refs_of`` (generations deleted by ``gc``);
    2. every raw entry tagged with any of ``tags`` (e.g. ``partition:<source>:<day>``), with its tags;
    3. every raw entry whose key starts with one of ``unreferenced_prefixes``, that no remaining
       ``ledger_refs`` row names and that carries no tag (a tagged entry, e.g. a learning reply,
       lives until one of its partitions expires).
    """

    drop_refs_of: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    unreferenced_prefixes: list[str] = Field(default_factory=list)


@runtime_checkable
class LedgerRetention(Protocol):
    """``LedgerStore`` extension for retention (addendum A1 §3.8.3; memory, sqlite, sqlalchemy,
    postgres, mongodb). ``put_raw(key, raw, valid, tags=...)`` tags an entry at write time;
    ``ledger_refs(generation, key)`` is a side table naming every ledger key a generation uses (its
    manifest keeps only the count and sha). ``drop_job`` removes a deleted generation's job entries
    (content-free by invariant). ``raw_entries`` pages through raw entries by key for the
    content-based verifier; it is bounded by ``limit``."""

    def tag_raw(self, key: str, tags: Sequence[str]) -> None: ...

    def raw_tags(self, key: str) -> list[str]: ...

    def tagged(self, tag: str) -> list[str]: ...

    def add_refs(self, generation: str, keys: Sequence[str]) -> None: ...

    def refs(self, generation: str) -> list[str]: ...

    def referencing_generations(self) -> list[str]: ...

    def purge(self, spec: PurgeSpec) -> dict[str, int]: ...

    def drop_job(self, job_id: str) -> int: ...

    def jobs(self) -> list[str]: ...

    def raw_entries(self, prefix: str, after: str | None, limit: int) -> list[RawReply]: ...


def require(adapter: Any, protocol: type, capability: str, operation: str) -> Any:
    """Return the facet when it structurally satisfies ``protocol``; otherwise a typed ``BackendError``."""
    facet = getattr(adapter, capability, None)
    if facet is None or not isinstance(facet, protocol):
        kind = getattr(adapter, "kind", type(adapter).__name__)
        raise BackendError(kind, operation, f"{capability} facet does not implement {protocol.__name__}")
    return facet
