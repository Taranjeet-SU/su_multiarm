"""The unified collection block (``collection``; schema v3) — docs/plans/unified_merge.md §6, §7.

``schema_version: 3`` with ``collection.mode: partitioned`` declares **one collection** composed of
member partitions (support snapshot, docs, log days/months, analytics days, tracker snapshots),
attached layers (an existing graph, a segment sidecar, imported communities, identity, links) and
the live log path (provisional delta slices). The existing ``sources`` key (read-only log sources)
is unchanged; ``collection.members.<id>.producer.source`` may name one of them.

Increment S0 made the block **validated but inert**. The partition core (M1a) serves it: members
built by the shared write path (``ingestion/members.py``), the id directory, compose by reference
(``ingestion/compose.py``) and the partitioned facets (``backends/sqlite/members.py``). M1b adds
read-only adoption (``adopt``; ``ingestion/adopt_member.py``) and attached layers (``layers[]``;
``ingestion/layers.py``); the M2a wiring serves the live log member (``cadence: minutes``: archive
members + slices, ``ingestion/log_collection.py``). ``runtime/collection.py`` still refuses what
later increments serve (``crosssource``; layer kinds other than ``graph_attach``,
``segment_sidecar`` and ``communities_imported``; live members it cannot serve), so a
configuration is never silently served as if its members were one monolithic store.

Writers (plan §10.7.4): L0 (S0) → LA (M1a/M1b member fields) → LB (M2a slice fields); additive only.
Nothing here can name executable code (no factories, no import paths); every path is a data file.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ..models import StrictModel
from .config_logs import LINE_TEXT_FIELDS
from .config_selection import check_source_quotas

IDENTIFIER = r"^[a-z][a-z0-9_]{0,63}$"
SHA256 = r"^[0-9a-f]{64}$"

# How a member names its instances (``MemberRef.key`` in ``core/types_members.py``).
MemberKeyScheme = Literal["adopted", "snapshot", "day", "month"]
MemberCadence = Literal["adopted", "minutes", "hourly", "daily", "weekly", "close_day", "manual"]
TimeRole = Literal["event_time", "update_time", "ingest_time"]
# Kinds of attached layer (§6.4); ``LayerRef.kind`` uses the same names.
LayerKind = Literal[
    "graph_attach",
    "segment_sidecar",
    "communities_imported",
    "communities_shared_errors",
    "communities_unified",
    "identity",
    "links",
    "graph_overlay",
]
LayerArm = Literal["lexical", "dense", "structured", "typed_query", "graph", "community", "segment", "exact_id"]
# Statistics model of one lexical stratum (§6.5).
StratumStats = Literal["adopted", "query_time", "member_rrf"]
# Core fields every member maps or declares absent (§8.2 D3).
CORE_FIELDS: tuple[str, ...] = (
    "source",
    "kind",
    "title",
    "text",
    "event_time",
    "customer_ref",
    "service",
    "component",
    "product",
    "version",
    "status",
    "url",
    "visibility",
)
# Kinds whose ids do not depend on the day (§6.1 catalog kinds): held in one catalog member per
# source, never in day members (``catalog_kinds`` adds to this set per member).
DEFAULT_CATALOG_KINDS: tuple[str, ...] = ("signature",)


def _identifiers(values, what: str) -> None:
    for value in values:
        if not re.fullmatch(IDENTIFIER, value):
            raise ValueError(f"{what} {value!r} must be an identifier")


class MemberProducer(StrictModel):
    """Who builds the member's partitions: ``write_path`` (the shared write path, in this process),
    ``log_host`` (a sealed handoff from the log host, sha256 verified), ``upload`` (a daily file
    upload) or ``adopted`` (read-only adoption of an existing store). ``source`` names an existing
    ``sources.<name>`` read-only log source when the member is fed by one."""

    kind: Literal["write_path", "log_host", "upload", "adopted"] = "write_path"
    source: str | None = Field(default=None, pattern=IDENTIFIER)


class MemberAdoption(StrictModel):
    """``collection.members.<id>.adopt`` (§6.3): a validated store served read-only, never written,
    re-stamped or relocated. ``original_config`` is the configuration that built it, pinned by
    ``original_config_sha256``; its recipe is recomputed backend-free (``ingestion/recipes.py``)
    and checked against the store's own manifest when the member is registered (M1b)."""

    store: Path
    original_config: Path
    original_config_sha256: str = Field(pattern=SHA256)
    # The adopted store's own embedding-space id (its path-derived default, e.g. ``bge-m3``) declared
    # equivalent to the collection's declared ``embedding_space`` (§6.2): the model identity and the
    # input template must be equal (only the id/epoch differ) and the equivalence must be verified
    # (``collection verify-space --member``: re-embedded sample, mean/min cosine of
    # ``embedding_space.equivalence``; result stored in the member registry) before compose accepts
    # the member. Without it a declared space id other than the store's refuses compose.
    embedding_space: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class CollectionMember(StrictModel):
    """One declared member (§6.1, §8 output). Its instances (``MemberRef``) are built, adopted or
    handed off later; this block only declares their shape."""

    kind: str = Field(pattern=IDENTIFIER)  # e.g. support, ticket, doc, logs, analytics, tracker
    # Values of the ``source`` field this member's documents carry (one stratum may hold several,
    # e.g. the adopted support member: ticket and doc, §6.5 R1).
    sources: list[str] = Field(min_length=1, max_length=16)
    record_kinds: list[str] = Field(min_length=1, max_length=64)
    cadence: MemberCadence = "daily"
    key: MemberKeyScheme = "snapshot"
    producer: MemberProducer = Field(default_factory=MemberProducer)
    # Id prefixes owned by the member (``ticket:``, a doc URL prefix, ``logs:``); used for the
    # uniqueness check at registration and never for routing (routing reads the id directory).
    id_namespaces: list[str] = Field(min_length=1, max_length=16)
    identity_namespace: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}\.[a-z][a-z0-9_]{0,63}$")
    time_roles: dict[str, dict[TimeRole, str]] = Field(default_factory=dict)  # kind -> role -> field
    canonical_units: dict[str, str] = Field(default_factory=dict)  # kind -> unit (never summed across units)
    catalog_kinds: list[str] = Field(default_factory=list, max_length=16)
    field_map: dict[str, str | None] = Field(default_factory=dict)  # core field -> member field (None: declared absent)
    visibility: Literal["operator"] = "operator"  # operator-only phase (plan §14); tenant scoping is not offered
    embed_fields: dict[str, list[str]] = Field(default_factory=dict)  # kind -> embedded fields
    adopt: MemberAdoption | None = None
    required: bool = False  # an unavailable required member makes ``/ready`` not ready (§6.12)
    # Write-path members only (§8.2 D5; outage onboarding 2026-11-10). How the member's chunks get
    # vectors: ``import`` applies the embedder role's precomputed import as configured (its
    # ``on_missing`` included); ``online`` embeds every chunk with the embedder role's model and the
    # engine template and never consults the import (a new dataset's text is not in it), in the
    # collection's embedding space, as the live log member's text is. Recorded in the member's
    # partition recipe.
    vectors: Literal["import", "online"] = "import"
    # Write-path members only (§8.2 D3). The metadata keys the member's documents carry. When set,
    # the member store is sealed with the collection schema restricted to these fields (plus
    # ``source``, ``kind`` and the equivalence field when the collection declares them), so the
    # knowledge half's schema (``declared_fields``) holds this member's own fields, not the whole
    # collection's (log fields included); a document carrying another metadata key is refused, and
    # the member build neither builds a typed projection (a write-path member serves none) nor
    # attaches ``ingestion.attach`` columns outside these fields. Unset: today's build.
    metadata_fields: list[str] | None = Field(default=None, max_length=256)

    @field_validator("sources", "record_kinds", "catalog_kinds")
    @classmethod
    def unique_identifiers(cls, value, info):
        _identifiers(value, info.field_name.removesuffix("s"))
        if len(set(value)) != len(value):
            raise ValueError(f"{info.field_name} must be unique")
        return value

    @field_validator("metadata_fields")
    @classmethod
    def metadata_names(cls, value):
        if value is None:
            return value
        _identifiers(value, "metadata field")
        if len(set(value)) != len(value):
            raise ValueError("metadata_fields must be unique")
        return value

    @field_validator("id_namespaces")
    @classmethod
    def namespaces(cls, value):
        for prefix in value:
            if not prefix or prefix != prefix.strip() or len(prefix) > 200:
                raise ValueError(f"id namespace {prefix!r} must be a non-empty prefix without surrounding spaces")
        if len(set(value)) != len(value):
            raise ValueError("id_namespaces must be unique")
        return value

    @model_validator(mode="after")
    def coherent(self):
        kinds = set(self.record_kinds)
        for name, mapping in (("time_roles", self.time_roles), ("canonical_units", self.canonical_units), ("embed_fields", self.embed_fields)):
            unknown = sorted(set(mapping) - kinds)
            if unknown:
                raise ValueError(f"{name} names kinds that are not record_kinds: {unknown}")
        unknown = sorted(set(self.catalog_kinds) - kinds)
        if unknown:
            raise ValueError(f"catalog_kinds must be record_kinds: {unknown}")
        _identifiers(self.canonical_units.values(), "canonical unit")
        for kind, roles in self.time_roles.items():
            _identifiers(roles.values(), f"time role field of {kind}")
        for kind, fields in self.embed_fields.items():
            _identifiers(fields, f"embed field of {kind}")
            line_text = sorted(set(fields) & set(LINE_TEXT_FIELDS.get(kind, ())))
            if line_text:
                raise ValueError(f"embed_fields.{kind} lists log-line text {line_text}; log-line text is never embedded (retention invariant)")
        unknown = sorted(set(self.field_map) - set(CORE_FIELDS))
        if unknown:
            raise ValueError(f"field_map keys must be core fields {list(CORE_FIELDS)}; unknown: {unknown}")
        _identifiers([v for v in self.field_map.values() if v is not None], "field_map value")
        if (self.adopt is not None) != (self.key == "adopted"):
            raise ValueError("adopt is required by, and only read with, key: adopted")
        if self.adopt is not None and (self.producer.kind != "adopted" or self.cadence != "adopted"):
            raise ValueError("an adopted member needs producer.kind: adopted and cadence: adopted")
        if self.producer.kind == "adopted" and self.adopt is None:
            raise ValueError("producer.kind: adopted needs an adopt block")
        if (self.vectors != "import" or self.metadata_fields is not None) and (self.producer.kind != "write_path" or self.cadence == "minutes"):
            raise ValueError("vectors and metadata_fields shape a member built by the shared write path (producer.kind: write_path, not the live log member)")
        if self.metadata_fields is not None:
            fields = set(self.metadata_fields)
            for kind, roles in self.time_roles.items():
                missing = sorted(set(roles.values()) - fields)
                if missing:
                    raise ValueError(f"time_roles.{kind} names fields outside metadata_fields: {missing}")
        return self

    def build_recipe(self) -> dict[str, object]:
        """What this member's write-path build adds to its partition recipe (empty by default, so
        the recipes of members without these settings are unchanged). Only members built by the
        shared write path: a log-host or live log member's recipe (its slice ledger's identity) never
        changes here."""
        out: dict[str, object] = {}
        if self.producer.kind != "write_path" or self.cadence == "minutes":
            return out
        if self.vectors != "import":
            out["vectors"] = self.vectors
        if self.metadata_fields is not None:
            out["metadata_fields"] = sorted(self.metadata_fields)
        if self.embed_fields:
            out["embed_fields"] = {kind: list(fields) for kind, fields in sorted(self.embed_fields.items())}
        return out

    def all_catalog_kinds(self) -> tuple[str, ...]:
        """Declared catalog kinds plus the default ones among this member's record kinds."""
        defaults = [k for k in DEFAULT_CATALOG_KINDS if k in self.record_kinds and k not in self.catalog_kinds]
        return (*self.catalog_kinds, *defaults)


class PrecomputedEquivalence(StrictModel):
    """Precomputed ↔ online equivalence of a member's vectors (§6.2): a verify sample of ``sample``
    chunks (stratified by kind) re-embedded online must reach the mean and minimum cosine."""

    sample: int = Field(default=200, ge=200, le=100000)
    mean_cosine: float = Field(default=0.995, gt=0, le=1)
    min_cosine: float = Field(default=0.98, gt=0, le=1)

    @model_validator(mode="after")
    def ordered(self):
        if self.min_cosine > self.mean_cosine:
            raise ValueError("min_cosine cannot exceed mean_cosine")
        return self


class EmbeddingSpace(StrictModel):
    """``collection.embedding_space`` (§6.2): one space for every member the dense arm reads. The
    model identity is ``models.roles.embedder``'s; ``template`` names the declared input template
    (title + field + chunk text order). A member from another space is refused by the dense arm."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")
    template: str = Field(default="engine_template", pattern=IDENTIFIER)
    epoch: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")
    equivalence: PrecomputedEquivalence = Field(default_factory=PrecomputedEquivalence)


class LexicalStratum(StrictModel):
    """``collection.lexical.<member>`` (§6.5): the statistics model of one stratum. ``adopted``
    reads the adopted store's own files (support stratum); ``query_time`` sums statistics over the
    pinned units per query; ``member_rrf`` (archive months only) fuses per-member lists."""

    stats: StratumStats


class CollectionSelection(StrictModel):
    source_quotas: dict[str, float] | None = None

    @field_validator("source_quotas")
    @classmethod
    def quotas(cls, value):
        return None if value is None else check_source_quotas(value)


class CollectionLayer(StrictModel):
    """``collection.layers[]`` (§6.4): a read-only store outside the write path, pinned by digest,
    covering ``sources`` and serving ``arms``. ``backend`` names an existing backend instance when
    the layer is one (``graph_attach``: the ``kuzu`` ``mode: attach`` backend); otherwise ``path``
    and ``sha256`` pin the file or the sealed directory manifest."""

    name: str = Field(pattern=IDENTIFIER)
    kind: LayerKind
    sources: list[str] = Field(min_length=1, max_length=16)
    arms: list[LayerArm] = Field(default_factory=list, max_length=8)
    backend: str | None = Field(default=None, pattern=IDENTIFIER)
    path: Path | None = None
    sha256: str | None = Field(default=None, pattern=SHA256)
    required: bool = False
    # M1b: the adopted member whose store the layer belongs to — ``graph_attach`` answers for that
    # store's generation; ``communities_imported`` is the community file that store materialised.
    member: str | None = Field(default=None, pattern=IDENTIFIER)

    @model_validator(mode="after")
    def located(self):
        _identifiers(self.sources, "layer source")
        if (self.backend is None) == (self.path is None):
            raise ValueError(f"layer {self.name!r}: set exactly one of backend or path")
        if self.path is not None and self.sha256 is None:
            raise ValueError(f"layer {self.name!r}: a file layer is pinned by sha256")
        if self.kind == "graph_attach" and self.backend is None:
            raise ValueError(f"layer {self.name!r}: graph_attach names the attached graph backend")
        return self


class SliceCompaction(StrictModel):
    """Live-slice compaction ladder (§6.7): five-minute slices of the current hour become one hourly
    slice; every ``day_base_hours`` the day's slices become one day base."""

    refresh_seconds: int = Field(default=300, ge=60, le=3600)
    hourly_seconds: int = Field(default=3600, ge=300, le=86400)
    day_base_hours: int = Field(default=6, ge=1, le=24)

    @model_validator(mode="after")
    def ladder(self):
        if self.hourly_seconds <= self.refresh_seconds or self.hourly_seconds % self.refresh_seconds:
            raise ValueError("hourly_seconds must be a multiple of refresh_seconds and larger than it")
        if (self.day_base_hours * 3600) % self.hourly_seconds:
            raise ValueError("day_base_hours must be a whole number of hourly slices")
        return self

    def max_units_per_day(self) -> int:
        """1 base + hourly slices of one base period + refresh slices of one hour (19 by default)."""
        return 1 + (self.day_base_hours * 3600) // self.hourly_seconds + self.hourly_seconds // self.refresh_seconds


class SliceSettings(StrictModel):
    """``collection.slices`` (§6.7): the live log path. ``max_units_per_day`` bounds the slices a
    request pins per day; ``gc_grace_seconds`` keeps a retired, unpinned slice at least that long
    (never below the request deadline, checked against ``budgets.deadline_seconds``)."""

    compaction: SliceCompaction = Field(default_factory=SliceCompaction)
    max_units_per_day: int = Field(default=19, ge=1, le=200)
    embed_lag_seconds: int = Field(default=900, ge=0, le=86400)
    gc_grace_seconds: int = Field(default=3600, ge=60, le=7 * 86400)

    @model_validator(mode="after")
    def bounded(self):
        needed = self.compaction.max_units_per_day()
        if self.max_units_per_day < needed:
            raise ValueError(f"max_units_per_day {self.max_units_per_day} is below what the compaction ladder produces ({needed})")
        return self


class IdDirectoryConfig(StrictModel):
    """``collection.id_directory`` (§6.1): the persistent id → unit table. ``path`` unset: under the
    member registry (decided by the partition core)."""

    path: Path | None = None


class CollectionConfig(StrictModel):
    """``collection`` (schema v3). ``mode: partitioned`` is the only mode."""

    mode: Literal["partitioned"]
    members: dict[str, CollectionMember] = Field(min_length=1, max_length=64)
    embedding_space: EmbeddingSpace | None = None
    lexical: dict[str, LexicalStratum] = Field(default_factory=dict)
    selection: CollectionSelection = Field(default_factory=CollectionSelection)
    layers: list[CollectionLayer] = Field(default_factory=list, max_length=32)
    slices: SliceSettings = Field(default_factory=SliceSettings)
    id_directory: IdDirectoryConfig = Field(default_factory=IdDirectoryConfig)

    @field_validator("members")
    @classmethod
    def member_names(cls, value):
        _identifiers(value, "member id")
        return value

    @model_validator(mode="after")
    def coherent(self):
        owners: dict[str, str] = {}
        for name, member in self.members.items():
            for prefix in member.id_namespaces:
                for other, owner in owners.items():
                    if owner != name and (prefix.startswith(other) or other.startswith(prefix)):
                        raise ValueError(f"id namespace {prefix!r} of member {name!r} overlaps {other!r} of member {owner!r}")
                owners[prefix] = name
        sources = self.sources()
        for stratum, spec in self.lexical.items():
            member = self.members.get(stratum)
            if member is None:
                raise ValueError(f"collection.lexical.{stratum}: no member {stratum!r}")
            if (spec.stats == "adopted") != (member.adopt is not None):
                raise ValueError(f"collection.lexical.{stratum}: stats adopted is required by, and only valid for, an adopted member")
            if spec.stats == "member_rrf" and member.key not in {"day", "month"}:
                raise ValueError(f"collection.lexical.{stratum}: member_rrf is only for archive day/month members")
        quotas = self.selection.source_quotas or {}
        unknown = sorted(set(quotas) - sources)
        if unknown:
            raise ValueError(f"collection.selection.source_quotas names sources no member declares: {unknown}")
        names = [layer.name for layer in self.layers]
        if len(set(names)) != len(names):
            raise ValueError("collection.layers names must be unique")
        for layer in self.layers:
            unknown = sorted(set(layer.sources) - sources)
            if unknown:
                raise ValueError(f"collection.layers {layer.name!r} covers sources no member declares: {unknown}")
            if layer.member is not None:
                bound = self.members.get(layer.member)
                if bound is None:
                    raise ValueError(f"collection.layers {layer.name!r}: no member {layer.member!r}")
                if bound.adopt is None:
                    raise ValueError(f"collection.layers {layer.name!r}: member {layer.member!r} is not adopted (a layer binds to an adopted store)")
                if not set(layer.sources) <= set(bound.sources):
                    raise ValueError(f"collection.layers {layer.name!r} covers sources member {layer.member!r} does not carry")
        for name, member in self.members.items():
            if member.adopt is not None and member.adopt.embedding_space is not None:
                if self.embedding_space is None:
                    raise ValueError(f"collection.members.{name}.adopt.embedding_space maps the adopted store's space onto the collection's declared one: declare collection.embedding_space")
                if member.adopt.embedding_space == self.embedding_space.id:
                    raise ValueError(f"collection.members.{name}.adopt.embedding_space equals collection.embedding_space.id: no mapping is needed")
        if self.slices != SliceSettings() and not any(m.cadence == "minutes" for m in self.members.values()):
            raise ValueError("collection.slices is read only when a member has cadence: minutes (the live log path)")
        return self

    def sources(self) -> set[str]:
        return {source for member in self.members.values() for source in member.sources}

    def stats_of(self, stratum: str) -> StratumStats:
        """The declared statistics model of a member stratum, else its default (§7)."""
        if stratum in self.lexical:
            return self.lexical[stratum].stats
        return "adopted" if self.members[stratum].adopt is not None else "query_time"


# --- communities.layers (§6.4; schema v3) ---------------------------------------------------------


class CommunityLayer(StrictModel):
    """``communities.layers[]`` (schema v3): one community layer of a unified collection. The single
    ``communities.strategy`` keeps working for v2 configs; ``layers`` is refused there."""

    name: str = Field(pattern=IDENTIFIER)
    strategy: Literal["imported", "declared", "shared_errors", "unified_epoch"]
    sources: list[str] = Field(min_length=1, max_length=16)
    layer: str | None = Field(default=None, pattern=IDENTIFIER)  # the collection layer that stores it

    @field_validator("sources")
    @classmethod
    def source_names(cls, value):
        _identifiers(value, "community layer source")
        return value
