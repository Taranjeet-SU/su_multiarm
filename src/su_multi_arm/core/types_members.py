"""Frozen member, slice and layer types of the unified collection (docs/plans/unified_merge.md §6).

These are the **interfaces the parallel lanes build on** (S0, plan §10.7): the partition core (LA:
``ingestion/{members,recipes,compose,id_directory}.py``), the live log path (LB:
``ingestion/log_slices.py``, ``backends/sqlite/query_stats.py``) and the cross-source lanes (LD, LE)
exchange these values and nothing else. Every model is immutable (``frozen``), strict (unknown keys
refused) and carries data only; no field can name code.

* ``PartitionRecipe`` — the per-member decomposition of today's whole-config index recipe (§6.2).
* ``MemberRef`` — one immutable member partition (adopted store, snapshot, archive day/month,
  per-source catalog member) with facet locations and file digests (§6.1).
* ``SliceRef`` — one sealed provisional delta slice of the live log path with its supersede and
  **tombstone** lists (§6.7, B12).
* ``IdDirectoryEntry`` — one row of the persistent id directory (§6.1, B9): id → unit, with
  ``superseded_by`` / ``deleted_by`` / ``catalog_provisional``.
* ``LayerRef`` — one attached layer pinned by digest (§6.4).
* ``PinnedView`` — what one request reads: generation + members + slice version vector + layers +
  registry/link versions (§6.7 request pin).
* Protocols: ``IdDirectory`` (LA implements), ``SliceLedger`` (LB implements), ``StratumScorer``
  (LA: adopted stratum; LB: query-time statistics).

Invariants stated here and enforced by the implementations: non-catalog document ids are unique
across every member and slice of a pinned view (a slice may supersede an earlier version of the
same day); catalog-kind ids are unique per source catalog member; a log day is in exactly one tier;
superseded and tombstoned rows are excluded from candidates, records, counts and statistics.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, field_validator, model_validator

from ..models import StrictModel
from .config_collection import DEFAULT_CATALOG_KINDS, LayerArm, LayerKind

IDENTIFIER = r"^[a-z][a-z0-9_]{0,63}$"
SHA256 = r"^[0-9a-f]{64}$"
# Version of the partition protocol; enters the collection recipe (§6.2).
PARTITION_PROTOCOL = 1
# ``MemberRef.key`` forms: ``adopted:<hex digest prefix>``, ``snapshot:YYYY-MM-DD``,
# ``day:YYYY-MM-DD``, ``month:YYYY-MM`` and ``catalog`` (the per-source catalog member).
MEMBER_KEY = r"^(adopted:[0-9a-f]{8,64}|snapshot:\d{4}-\d{2}-\d{2}|day:\d{4}-\d{2}-\d{2}|month:\d{4}-\d{2}|catalog)$"
CATALOG_MEMBER_KEY = "catalog"
SLICE_ID = r"^slice:\d{4}-\d{2}-\d{2}:[a-z0-9][a-z0-9_.-]{0,63}$"

MemberTier = Literal["adopted", "snapshot", "archive", "partial", "catalog"]
SliceLevel = Literal["five_minute", "hourly", "base", "day"]
UnitKind = Literal["member", "slice"]
FacetName = Literal["catalog", "records", "lexical", "vector", "projection", "graph", "segment", "exact_id"]
# Status of a member or layer as served (§6.12): ``unavailable`` fails closed for that unit only.
UnitStatus = Literal["available", "stale", "unavailable"]


class FrozenModel(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def canonical_sha256(data: Any) -> str:
    """sha256 of the canonical JSON of ``data`` (sorted keys, no whitespace)."""
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


# --- recipes ---------------------------------------------------------------------------------------


class EmbeddingSpaceRef(FrozenModel):
    """Model identity + declared input template (§6.2). Equal across every member the dense arm
    reads; a member from another space is refused by the dense arm (lexical/records still serve)."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")
    model: str = Field(min_length=1, max_length=200)
    revision: str | None = None
    dimensions: int = Field(ge=1, le=65536)
    normalization: Literal["l2", "none"] = "l2"
    template: str = Field(default="engine_template", pattern=IDENTIFIER)
    epoch: str | None = None


class AnalyzerRecipe(FrozenModel):
    """Lexical analyzer of a member; equal **within a source** (statistics are per stratum, §6.5)."""

    engine: Literal["bm25", "fts5"] = "bm25"  # fts5 only for adopted members (refused for new ones, M1a)
    tokenizer: str = Field(default="ascii_alnum", pattern=IDENTIFIER)
    k1: float | None = None
    b: float | None = None
    epsilon: float | None = None


class PartitionRecipe(FrozenModel):
    """The member part of the decomposed index recipe (§6.2). ``chunking``, ``privacy``,
    ``content`` and ``projection`` are the recipe fragments today's ``index_recipe`` hashes, scoped
    to the member (``content``: extractor fingerprint, keyword-policy epoch, attach digests)."""

    embedding_space: EmbeddingSpaceRef | None = None  # None: the member is not read by the dense arm
    analyzer: AnalyzerRecipe = Field(default_factory=AnalyzerRecipe)
    chunking: dict[str, Any] = Field(default_factory=dict)
    privacy: dict[str, Any] = Field(default_factory=dict)
    content: dict[str, Any] = Field(default_factory=dict)
    projection: dict[str, Any] | None = None

    def fingerprint(self) -> str:
        """Member fingerprint = hash(embedding space, analyzer, chunking, privacy, content, projection)."""
        return canonical_sha256(self.model_dump(mode="json"))


class FacetLocation(FrozenModel):
    """Where one facet of a member/slice lives: a backend kind and a location **relative to the
    member registry** (never an absolute host path in a manifest)."""

    backend: str = Field(pattern=IDENTIFIER)  # e.g. sqlite, kuzu, segments
    location: str = Field(min_length=1, max_length=500)
    read_only: bool = True

    @field_validator("location")
    @classmethod
    def relative(cls, value):
        if value.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[\\/]", value) or ".." in value.split("/"):
            raise ValueError("facet location must be relative to the member registry (no absolute path, no '..')")
        return value


def _digests(value: Mapping[str, str]) -> Mapping[str, str]:
    for name, digest in value.items():
        if not re.fullmatch(SHA256, digest):
            raise ValueError(f"digest of {name!r} must be a sha256 hex string")
    return value


# --- members ---------------------------------------------------------------------------------------


class MemberRef(FrozenModel):
    """One immutable member partition (§6.1). ``partition_id`` = sha over document ids + content +
    partition recipe; caches are keyed by it, never by generation. ``facets`` and ``digests`` cover
    every backing file; a digest mismatch at serve time makes the member ``unavailable``."""

    member: str = Field(pattern=IDENTIFIER)  # declared member id (``collection.members.<id>``)
    key: str = Field(pattern=MEMBER_KEY)
    partition_id: str = Field(pattern=SHA256)
    recipe: PartitionRecipe
    tier: MemberTier
    as_of: datetime
    sources: tuple[str, ...] = Field(min_length=1)
    counts: dict[str, int] = Field(default_factory=dict)  # per kind
    catalog_kinds: tuple[str, ...] = ()  # kinds held by this (catalog) member; excluded from day members
    stats: dict[str, Any] = Field(default_factory=dict)  # per-stratum lexical stats reference (§6.5)
    facets: dict[FacetName, FacetLocation] = Field(default_factory=dict)
    digests: dict[str, str] = Field(default_factory=dict)  # backing file / directory listing -> sha256
    keyword_epoch: str | None = None

    @field_validator("digests")
    @classmethod
    def sha256_digests(cls, value):
        return _digests(value)

    @model_validator(mode="after")
    def coherent(self):
        if any(n < 0 for n in self.counts.values()):
            raise ValueError("counts are non-negative")
        if (self.key == CATALOG_MEMBER_KEY) != (self.tier == "catalog"):
            raise ValueError("key catalog is the catalog tier and only it")
        if self.key.startswith("adopted:") != (self.tier == "adopted"):
            raise ValueError("an adopted key is the adopted tier and only it")
        if self.tier == "catalog" and not self.catalog_kinds:
            raise ValueError("a catalog member declares the catalog kinds it holds")
        if self.tier != "catalog" and self.catalog_kinds:
            raise ValueError("only a catalog member holds catalog kinds")
        return self

    @property
    def unit_id(self) -> str:
        """The unit id used by the id directory and caches: ``<member>/<key>``."""
        return f"{self.member}/{self.key}"

    def day(self) -> date | None:
        """The day of a ``day:`` / ``snapshot:`` member, else ``None``."""
        head, _, rest = self.key.partition(":")
        return date.fromisoformat(rest) if head in {"day", "snapshot"} else None

    def covers(self, day: date) -> bool:
        """Whether this archive/partial member holds ``day`` (a ``day:`` member its day, a
        ``month:`` member every day of its month); other tiers hold no day of the live window."""
        if self.tier not in {"archive", "partial"}:
            return False
        head, _, rest = self.key.partition(":")
        if head == "day":
            return date.fromisoformat(rest) == day
        if head == "month":
            return rest == day.strftime("%Y-%m")
        return False


# --- slices ----------------------------------------------------------------------------------------


class SliceRef(FrozenModel):
    """One sealed provisional delta slice of a live member (§6.7), registered in the day's slice
    ledger without a generation activation.

    * ``replaces``: earlier slice ids of the same day this one compacts or rebuilds.
    * ``supersedes``: document ids whose earlier version (in an earlier slice of the day) this slice
      replaces (latest version per id wins).
    * ``tombstones``: document ids live in the day's earlier slices but absent from this full-day
      build (gap-filling merges); marked ``deleted_by`` in the id directory and never served.
    * ``excluded_stats``: counts (``documents``, ``chunks``, ``tokens``) of the superseded and
      tombstoned rows, so query-time statistics subtract them (§6.5)."""

    slice_id: str = Field(pattern=SLICE_ID)
    member: str = Field(pattern=IDENTIFIER)
    source: str = Field(pattern=IDENTIFIER)
    day: date
    level: SliceLevel
    sequence: int = Field(ge=0)  # monotonic per (member, day); the slice vector orders by it
    as_of: datetime
    recipe: PartitionRecipe
    keyword_epoch: str | None = None
    documents: int = Field(ge=0)
    counts: dict[str, int] = Field(default_factory=dict)
    replaces: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()
    tombstones: tuple[str, ...] = ()
    catalog_provisional: tuple[str, ...] = ()  # catalog-kind ids first seen on this provisional day
    excluded_stats: dict[str, int] = Field(default_factory=dict)
    facets: dict[FacetName, FacetLocation] = Field(default_factory=dict)
    digests: dict[str, str] = Field(default_factory=dict)
    sha256: str = Field(pattern=SHA256)  # of the sealed handoff (verified on registration)
    dense_pending: bool = False  # servable for lexical/records/projection before its vectors exist

    @field_validator("digests")
    @classmethod
    def sha256_digests(cls, value):
        return _digests(value)

    @model_validator(mode="after")
    def coherent(self):
        if not self.slice_id.startswith(f"slice:{self.day.isoformat()}:"):
            raise ValueError("slice_id must carry the slice's day")
        if self.slice_id in self.replaces:
            raise ValueError("a slice cannot replace itself")
        for name in ("replaces", "supersedes", "tombstones", "catalog_provisional"):
            values = getattr(self, name)
            if len(set(values)) != len(values):
                raise ValueError(f"{name} must be unique")
        if set(self.supersedes) & set(self.tombstones):
            raise ValueError("an id is either superseded or tombstoned, not both")
        return self

    @property
    def unit_id(self) -> str:
        return self.slice_id


# --- id directory ----------------------------------------------------------------------------------


class IdDirectoryEntry(FrozenModel):
    """One row of the persistent id directory (§6.1): ``(doc_id, kind, unit_id, unit_kind, day,
    version, superseded_by, deleted_by, catalog_provisional)``. A row with ``superseded_by`` or
    ``deleted_by`` set is excluded like a superseded row everywhere (candidates, records, counts,
    statistics). ``catalog_provisional``: a catalog-kind document carried by a provisional slice
    until the catalog member supersedes it at close (exempt from the cross-day uniqueness check)."""

    doc_id: str = Field(min_length=1, max_length=512)
    kind: str = Field(pattern=IDENTIFIER)
    unit_id: str = Field(min_length=1, max_length=300)
    unit_kind: UnitKind
    day: date | None = None
    version: int = Field(default=1, ge=1)
    superseded_by: str | None = None
    deleted_by: str | None = None
    catalog_provisional: bool = False

    @model_validator(mode="after")
    def coherent(self):
        if self.superseded_by is not None and self.deleted_by is not None:
            raise ValueError("a row is superseded or deleted, not both")
        if self.catalog_provisional and self.unit_kind != "slice":
            raise ValueError("catalog_provisional rows live in slices")
        return self

    @property
    def live(self) -> bool:
        return self.superseded_by is None and self.deleted_by is None


def is_catalog_kind(kind: str, declared: Sequence[str] = ()) -> bool:
    """Catalog kinds (day-independent ids): the defaults plus a member's declared ``catalog_kinds``."""
    return kind in DEFAULT_CATALOG_KINDS or kind in declared


# --- layers ----------------------------------------------------------------------------------------


class LayerRef(FrozenModel):
    """One attached layer pinned by digest in the unified manifest (§6.4). Exempt from the
    write-path backend rules; arms read layers together with member facets."""

    name: str = Field(pattern=IDENTIFIER)
    kind: LayerKind
    sources: tuple[str, ...] = Field(min_length=1)
    arms: tuple[LayerArm, ...] = ()
    digest: str = Field(pattern=SHA256)
    version: str | None = None  # registry_version / link_version / epoch, when the layer has one
    location: FacetLocation | None = None
    as_of: datetime | None = None


# --- the request pin -------------------------------------------------------------------------------


class PinnedView(FrozenModel):
    """What one request reads (§6.7): the generation, its members and layers, the slice version
    vector of every live member ``{member: {day: (slice ids…)}}`` read once per request, and the
    registry/link versions. Units outside the view are never opened. ``generation`` is ``None`` only
    for the single-member view before any index exists. A day of a member is in exactly one tier:
    archived (a ``day:``/``month:`` archive or partial member) or provisional (slices), never both."""

    generation: str | None = None
    members: tuple[MemberRef, ...] = ()
    slices: dict[str, dict[date, tuple[str, ...]]] = Field(default_factory=dict)
    layers: tuple[LayerRef, ...] = ()
    history_seq: int | None = None
    registry_version: str | None = None
    link_version: str | None = None

    @model_validator(mode="after")
    def coherent(self):
        units = [m.unit_id for m in self.members]
        if len(set(units)) != len(units):
            raise ValueError("a member unit appears twice in the view")
        seen: set[str] = set()
        for member, vector in self.slices.items():
            if not re.fullmatch(IDENTIFIER, member):
                raise ValueError(f"slice vector key {member!r} must be a member id")
            for day, ids in vector.items():
                for slice_id in ids:
                    if not re.fullmatch(SLICE_ID, slice_id) or not slice_id.startswith(f"slice:{day.isoformat()}:"):
                        raise ValueError(f"slice {slice_id!r} is not a slice of {day}")
                    if slice_id in seen:
                        raise ValueError(f"slice {slice_id!r} is pinned twice")
                    seen.add(slice_id)
                both = [m.key for m in self.members if m.member == member and m.covers(day)]
                if both:
                    raise ValueError(f"day {day} of member {member!r} is both archived ({both[0]}) and provisional (a day is in exactly one tier)")
        names = [layer.name for layer in self.layers]
        if len(set(names)) != len(names):
            raise ValueError("a layer appears twice in the view")
        return self

    def unit_ids(self) -> tuple[str, ...]:
        """Every pinned unit: member units, then slice ids by member and day."""
        slices = (s for member in sorted(self.slices) for day in sorted(self.slices[member]) for s in self.slices[member][day])
        return (*(m.unit_id for m in self.members), *slices)

    def members_for(self, sources: Sequence[str] | None = None) -> tuple[MemberRef, ...]:
        """Members whose sources intersect ``sources`` (all when ``None``): out-of-scope members are
        not opened (§6.1 scope contract)."""
        if sources is None:
            return self.members
        wanted = set(sources)
        return tuple(m for m in self.members if wanted & set(m.sources))


# --- protocols the lanes implement -------------------------------------------------------------------


@runtime_checkable
class IdDirectory(Protocol):
    """The persistent id directory (LA, ``ingestion/id_directory.py``). Maintained incrementally:
    cost O(documents of the new unit), never O(collection). Registration refuses a duplicate
    non-catalog id (except a same-day slice supersede, recorded as ``superseded_by``); when the
    directory is unavailable or inconsistent, registration fails closed (§6.12)."""

    def register(self, unit_id: str, unit_kind: UnitKind, entries: Sequence[IdDirectoryEntry]) -> None: ...

    def supersede(self, doc_ids: Sequence[str], by: str) -> None: ...

    def tombstone(self, doc_ids: Sequence[str], by: str) -> None: ...

    def retire(self, unit_id: str) -> None: ...

    def resolve(self, doc_id: str, view: PinnedView) -> IdDirectoryEntry | None:
        """One indexed lookup filtered to the view's units; unresolvable → ``None`` (never a scan)."""
        ...

    def live_ids(self, member: str, day: date) -> frozenset[str]:
        """Live ids of a member's day across its slices (the tombstone diff at sealing)."""
        ...


@runtime_checkable
class SliceLedger(Protocol):
    """The provisional slice ledger of one live member (LB, ``ingestion/log_slices.py``): ``CURRENT``
    per day outside generation activation (I1 mechanism). ``seal`` computes the tombstone list as the
    diff between the new full-day build's ids and ``IdDirectory.live_ids``; ``vector`` is read once
    per request; ``retire`` deletes only unpinned slices older than ``gc_grace_seconds``."""

    def vector(self) -> dict[date, tuple[str, ...]]: ...

    def get(self, slice_id: str) -> SliceRef: ...

    def seal(self, day: date, level: SliceLevel, documents: Sequence[Any], *, replaces: Sequence[str] = ()) -> SliceRef: ...

    def retire(self, slice_id: str) -> bool: ...


class StratumRanking(FrozenModel):
    """One stratum's ranked candidates (§6.5). Scores are comparable **within** the stratum only;
    selection fuses strata by rank (RRF) under ``source_quotas``."""

    stratum: str = Field(pattern=IDENTIFIER)
    ids: tuple[str, ...] = ()
    scores: tuple[float, ...] = ()
    units: tuple[str, ...] = ()  # units consulted (pruned units are absent)
    approximations: tuple[str, ...] = ()  # declared approximations, e.g. "average_idf_lag"

    @model_validator(mode="after")
    def aligned(self):
        if len(self.ids) != len(self.scores):
            raise ValueError("ids and scores are aligned")
        if list(self.scores) != sorted(self.scores, reverse=True):
            raise ValueError("a ranking is in descending score order")
        return self


@runtime_checkable
class StratumScorer(Protocol):
    """Scores one lexical (or dense) stratum over the pinned view (§6.5): the adopted support stratum
    reads today's files and statistics unchanged (LA); a log stratum computes query-time statistics
    over the pinned units minus superseded and tombstoned rows (LB, ``backends/sqlite/query_stats.py``)."""

    stratum: str

    def rank(self, query: str, k: int, view: PinnedView) -> StratumRanking: ...
