"""The legacy-QA compatibility profile: a declarative mapping from a legacy question-answering HTTP
contract onto one package ``Engine`` per dataset.

Everything deployment-specific (dataset ids and labels, content-source labels, record-context key
names, source-view sections, the visibility rule, link policy, facet keys) lives in this profile, not
in code. The profile is data only: YAML/JSON, validated here; it never names Python objects to import.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator, model_validator

from ...models import StrictModel

DATASET_ID = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
LEGACY_MODES = ("hybrid", "agentic")


class ModeRoute(StrictModel):
    """How a legacy answer mode runs on the engine: an allowlisted ``SearchRequest.pipeline``/``mode``
    override (``None`` = the engine default)."""

    pipeline: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    mode: Literal["rules", "classifier", "llm", "cascade"] | None = None


class HideRule(StrictModel):
    """Visibility predicate: a record whose ``field`` (metadata key, or ``source`` / ``kind``) equals
    one of ``values`` (case-insensitive) is not shown. The source view answers it exactly like an
    unknown id; every other response drops it from its lists, and an answer whose evidence included
    it is withheld (``outgoing.py``)."""

    field: str = Field(min_length=1, max_length=64)
    values: list[str] = Field(min_length=1)


class Section(StrictModel):
    field: str = Field(min_length=1)
    heading: str = Field(min_length=1)


class AliasRule(StrictModel):
    """One reversible id mapping for the UI record page: package id ``<package_prefix><rest>`` <->
    UI id ``<alias_prefix><rest>``; ``rest`` must match ``UiLink.rest_pattern``."""

    package_prefix: str = Field(min_length=1, max_length=40)
    alias_prefix: str = Field(min_length=1, max_length=40)


class UiLink(StrictModel):
    """Record links an unmodified earlier UI can render.

    Some earlier record pages accept only a fixed dataset token and id scheme. For requests on the
    listed ``channels`` (``X-QA-Channel``), records without an absolute URL get a relative link
    ``<path>?dataset=<dataset>&id=<alias>``; ``GET /source`` resolves the alias back to the package id
    (only aliases produced by these rules; anything else stays "not available"). The token is a URL
    parameter only: the record is read from this served dataset."""

    dataset: str = Field(pattern=DATASET_ID)
    channels: list[str] = Field(default_factory=lambda: ["ui"], min_length=1)
    rest_pattern: str = r"^[A-Za-z0-9_-]{1,72}$"
    id_pattern: str = r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,79}$"  # what the UI page accepts (checked on every alias)
    rules: list[AliasRule] = Field(min_length=1)

    @field_validator("rest_pattern", "id_pattern")
    @classmethod
    def compiles(cls, value):
        re.compile(value)
        return value

    @model_validator(mode="after")
    def unambiguous(self):
        aliases = [r.alias_prefix for r in self.rules]
        packages = [r.package_prefix for r in self.rules]
        for group, name in ((aliases, "alias_prefix"), (packages, "package_prefix")):
            for i, a in enumerate(group):
                for b in group[i + 1 :]:
                    if a.startswith(b) or b.startswith(a):
                        raise ValueError(f"ui_link {name} values must not prefix each other: {a!r}, {b!r}")
        return self

    def alias(self, record_id: str) -> str | None:
        for rule in self.rules:
            if record_id.startswith(rule.package_prefix):
                rest = record_id[len(rule.package_prefix) :]
                alias = rule.alias_prefix + rest
                if re.fullmatch(self.rest_pattern, rest) and re.fullmatch(self.id_pattern, alias):
                    return alias
        return None

    def resolve(self, alias: str) -> str | None:
        for rule in self.rules:
            if alias.startswith(rule.alias_prefix):
                rest = alias[len(rule.alias_prefix) :]
                if re.fullmatch(self.rest_pattern, rest):
                    return rule.package_prefix + rest
        return None


class SourceView(StrictModel):
    enabled: bool = False
    path: str = "/source"  # the UI page that renders a record (relative citation links point here)
    id_pattern: str = r"^[A-Za-z0-9][A-Za-z0-9:_.\-]{0,79}$"
    ui_link: UiLink | None = None
    sections: list[Section] = Field(default_factory=list)  # empty: every document field, in stored order
    repeated_field_prefix: str | None = None  # fields named "<prefix>N" become "<Heading> N" sections
    section_limit: int = Field(default=40, ge=1, le=500)
    metadata_keys: list[str] = Field(default_factory=list)  # empty: every metadata key
    title_fallback_field: str | None = None  # title missing -> this field's first line (title_derived)
    kind_labels: dict[str, str] = Field(default_factory=dict)  # Document.source/kind -> displayed kind
    hide: list[HideRule] = Field(default_factory=list)
    redact: bool = True  # credential-like strings are masked in every outgoing text (outgoing.py)

    @field_validator("id_pattern")
    @classmethod
    def compiles(cls, value):
        re.compile(value)
        return value


class FacetKey(StrictModel):
    key: str = Field(min_length=1, max_length=64)  # the legacy aggregation key
    field: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")  # the package metadata field (groupable)
    label: str = ""


DEFAULT_GUIDANCE = (
    "Answer only from `facts` and `evidence` and cite every claim as [n] with the evidence numbers. "
    "Use `facts` and the structured blocks for counts, totals and rankings and state their exactness. "
    "If `notice.kind` is \"refusal\", decline with the notice text. If the evidence does not answer the "
    "question, say so instead of guessing."
)


class EnrichView(StrictModel):
    """``enrich: true`` on a search: one generator call writes a short relevance note per result on the
    page, from that result's text only. Off unless enabled; metered by the engine's budget and spend
    ledger; every note is attributed to its result id (unknown ids are dropped and counted)."""

    enabled: bool = False
    max_results: int = Field(default=10, ge=1, le=50)
    snippet_chars: int = Field(default=500, ge=100, le=4000)
    summary_chars: int = Field(default=300, ge=40, le=1000)


class SearchView(StrictModel):
    enabled: bool = False
    date_field: str | None = None  # sortable field used for sortBy date/post_time
    facets: list[FacetKey] = Field(default_factory=list)
    facet_scope: Literal["candidates", "filtered_corpus"] = "candidates"
    default_page_size: int = Field(default=20, ge=1, le=50)
    guidance: str | None = DEFAULT_GUIDANCE  # static usage text; deterministic per-request notes are appended
    enrich: EnrichView = Field(default_factory=EnrichView)


class SessionCache(StrictModel):
    """Follow-up answering from a session's prior evidence (the earlier service's session cache).

    A turn on a session (``X-QA-Session``) is answered from the evidence of the session's most recent
    full-search turn (the anchor) when every deterministic precondition holds: an anchor exists within
    ``ttl_seconds`` on the same generation, the record context is unchanged, the question names no
    record id absent from the anchor, and it matches a ``followup_cues`` pattern. The evidence is
    re-read by id from the catalog (never a cached answer) and the answer contract runs as usual. When
    the generator reports insufficient evidence the turn runs a full search; both outcomes are
    recorded in ``session_cache.decision``. Unclear turns run a full search."""

    enabled: bool = False
    ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    max_sessions: int = Field(default=5000, ge=1, le=1_000_000)
    max_evidence: int = Field(default=40, ge=1, le=200)
    followup_cues: list[str] = Field(
        default_factory=lambda: [
            r"(?i)\b(summari[sz]e|shorten|rephrase|reword|simplify|translate|reformat|bullet(ed)? list|in a table|tl;?dr)\b",
            r"(?i)\b(tell me more|more detail|elaborate|expand on|explain (that|this|it|the|step))\b",
            r"(?i)\b(the|that|this) (first|second|third|fourth|fifth|last|previous|above|same) (one|step|source|item|ticket|article|record|answer|point)\b",
            r"(?i)\b(source|citation|reference) ?\[?\d{1,2}\]?",
            r"(?i)^\s*(and|what about|how about|why|which one|is that|does that|can you)\b.{0,80}\b(it|that|this|those|these|them)\b",
            # 2026-11-09 cutover: "Show me the ticket numbers for those" / "list their owners"
            r"(?i)\b(for|of|from|about|in|on|behind|among) (those|these|them|the above|that one|these ones|those ones)\b",
            r"(?i)^\s*(show|list|give|name|tell)\b.{0,60}\b(those|these|them|their)\b",
        ]
    )
    record_id_patterns: list[str] = Field(default_factory=list)  # record ids named in a question (new id -> full search)

    @field_validator("followup_cues", "record_id_patterns")
    @classmethod
    def compile_all(cls, value):
        for pattern in value:
            re.compile(pattern)
        return value


class RecordContextMap(StrictModel):
    """Legacy context key -> declared package context key (``request.context_keys``). Keys outside the
    map are not forwarded and are listed in the response's ``compat.unsupported``."""

    keys: dict[str, str] = Field(default_factory=dict)


# ``contentSource`` labels of the unified view's non-support sources, used when the profile's
# ``content_source`` does not name them (the profile always wins).
UNIFIED_SOURCE_LABELS: dict[str, str] = {"logs": "Logs"}


class UnifiedView(StrictModel):
    """Staged cutover of one dataset id to the unified collection (unified merge M2b-2, §6.11).

    ``enabled: false`` (default): the dataset is served by ``DatasetProfile.config`` exactly as
    before. ``enabled: true``: the same dataset id, routes and envelope are served by ``config``
    (a schema v3 configuration with ``crosssource.planner.enabled``: one collection, the
    two-halves planner); answers gain the additive ``coverage`` key and per-citation ``source``,
    ``links`` and ``link_status``. Rollback: set ``enabled: false`` (or the previous profile) and
    restart; nothing else changes."""

    enabled: bool = False
    config: Path | None = None

    @model_validator(mode="after")
    def configured(self):
        if self.enabled and self.config is None:
            raise ValueError("unified.enabled needs unified.config (the merged-view configuration)")
        return self


class DatasetProfile(StrictModel):
    id: str = Field(pattern=DATASET_ID)
    label: str = ""
    default: bool = False
    retired: bool = False  # listed nowhere; every call naming it gets a "not available" 422
    retired_message: str | None = None
    config: Path | None = None  # package configuration file (an injected engine needs none)
    modes: dict[str, ModeRoute] = Field(default_factory=lambda: {"hybrid": ModeRoute()})
    citations: Literal["evidence", "cited"] = "evidence"  # legacy: every delivered evidence item, numbered
    citation_url: Literal["external_first", "source_first", "external_only"] = "external_first"
    content_source: dict[str, str] = Field(default_factory=dict)  # Document.source -> contentSource label
    content_source_default: str = "Other"
    record_context: RecordContextMap = Field(default_factory=RecordContextMap)
    source: SourceView = Field(default_factory=SourceView)
    search: SearchView = Field(default_factory=SearchView)
    session_cache: SessionCache = Field(default_factory=SessionCache)
    unified: UnifiedView = Field(default_factory=UnifiedView)

    def source_label(self, source: str | None) -> str:
        """The legacy ``contentSource`` label of a ``Document.source``: ``content_source`` first;
        on the unified view a log record (``logs``) is labelled ``UNIFIED_SOURCE_LABELS`` when the
        profile does not name it (staging F7: log records showed as ``Other``); else the default."""
        if source is None:
            return self.content_source_default
        if source in self.content_source:
            return self.content_source[source]
        if self.unified.enabled and source in UNIFIED_SOURCE_LABELS:
            return UNIFIED_SOURCE_LABELS[source]
        return self.content_source_default

    def served_config(self) -> Path | None:
        """The configuration this dataset id is served from (the unified one when switched on)."""
        return self.unified.config if self.unified.enabled else self.config

    @field_validator("modes")
    @classmethod
    def legacy_modes(cls, value):
        unknown = sorted(set(value) - set(LEGACY_MODES))
        if unknown:
            raise ValueError(f"unknown legacy modes {unknown}; valid: {list(LEGACY_MODES)}")
        if "hybrid" not in value:
            raise ValueError("modes must route 'hybrid' (the legacy default)")
        return value

    @model_validator(mode="after")
    def retired_or_served(self):
        if self.retired and self.default:
            raise ValueError(f"dataset {self.id!r}: a retired dataset cannot be the default")
        return self


class ClusterGroups(StrictModel):
    """``GET /api/clusters`` from package data: the records of ``dataset`` grouped by one groupable
    metadata field (exact counts from the records backend) with representative records. These are
    metadata groups, not the earlier service's embedding clusters (labelled as such)."""

    dataset: str = Field(pattern=DATASET_ID)
    field: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    top_groups: int = Field(default=20, ge=1, le=200)
    representatives: int = Field(default=5, ge=0, le=20)
    representative_sort: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")  # sortable field, newest first


class ModelEntry(StrictModel):
    id: str = Field(min_length=1, max_length=120)
    provider: str = Field(min_length=1, max_length=60)
    label: str | None = None


class LegacyProfile(StrictModel):
    schema_version: Literal[1] = 1
    datasets: list[DatasetProfile] = Field(min_length=1)
    models: list[ModelEntry] = Field(default_factory=list)  # GET /api/models; empty: the generator of the default dataset
    default_model: str | None = None
    # Research artefacts of the legacy service (clusters / evolution / replay runs): optional JSON
    # files served read-only; absent -> an empty, valid document flagged ``compat.status: pending``.
    research_files: dict[Literal["clusters", "evolution", "replay_runs"], Path] = Field(default_factory=dict)
    clusters: ClusterGroups | None = None  # package-data clusters (takes precedence over research_files.clusters)
    # Startup warm-up: load the query embedder of each served dataset and pre-open what its first
    # request would load (``Engine.warmup``: pinned units, live log strata, vector matrices; no
    # generator/planner call, no retrieval ranking). /health reports ``warmup``; /ready is not ready
    # until it is done — a failure, or a run still going after ``warmup_timeout_seconds``, is a
    # readiness problem (requests are not refused; they pay the cold loads themselves).
    warmup: bool = True
    warmup_timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    sessions: bool = True  # X-QA-Session / body session_id bind a package session
    session_header: str = "X-QA-Session"
    client_header: str = "X-QA-Client-Id"
    channel_header: str = "X-QA-Channel"
    request_log: Path | None = None  # JSONL: one summary row per call (no answer text)
    heartbeat_seconds: float = Field(default=10.0, gt=0, le=60)
    # Serving limits. ``max_concurrent_answers``: answers in flight at once (``/api/qa`` and
    # ``/api/qa/stream`` together); one more is refused with 503 and ``Retry-After`` (never queued).
    # ``answer_deadline_seconds``: the adapter's deadline for one answer, kept below the callers'
    # timeouts (a gateway that gives up at 180 s); past it the run is cancelled and answered 504. The
    # engine's own ``budgets.deadline_seconds`` still applies; the shorter one wins.
    max_concurrent_answers: int = Field(default=16, ge=1, le=1024)
    answer_deadline_seconds: float = Field(default=170.0, gt=0, le=3600)
    # Expired package sessions are deleted at most this often (opportunistically, on a request that
    # binds a session); an expired session named again is replaced, never refused.
    session_sweep_seconds: float = Field(default=600.0, ge=1, le=86400)

    @model_validator(mode="after")
    def one_default(self):
        ids = [d.id for d in self.datasets]
        if len(set(ids)) != len(ids):
            raise ValueError("dataset ids must be unique")
        served = [d for d in self.datasets if not d.retired]
        if not served:
            raise ValueError("at least one dataset must be served (not retired)")
        defaults = [d for d in served if d.default]
        if len(defaults) > 1:
            raise ValueError("at most one dataset may be the default")
        if not defaults:
            served[0].default = True
        if self.clusters is not None and self.clusters.dataset not in {d.id for d in served}:
            raise ValueError(f"clusters.dataset {self.clusters.dataset!r} is not a served dataset")
        tokens = [d.source.ui_link.dataset for d in served if d.source.ui_link is not None]
        if len(set(tokens)) != len(tokens):
            raise ValueError("source.ui_link.dataset tokens must be unique across served datasets")
        if set(tokens) & {d.id for d in served}:
            raise ValueError("a source.ui_link.dataset token must not be another served dataset's id")
        return self

    @property
    def default_dataset(self) -> DatasetProfile:
        return next(d for d in self.datasets if d.default and not d.retired)

    def dataset(self, dataset_id: str) -> DatasetProfile | None:
        return next((d for d in self.datasets if d.id == dataset_id), None)


def load_profile(path: str | Path) -> LegacyProfile:
    """Read a YAML/JSON profile; relative ``config``/``research_files``/``request_log`` paths resolve
    against the profile's directory."""
    path = Path(path).expanduser().resolve()
    data = yaml.safe_load(path.read_text()) or {}
    profile = LegacyProfile.model_validate(data)
    base = path.parent

    def resolve(p: Path | None) -> Path | None:
        return None if p is None else (p if p.is_absolute() else (base / p).resolve())

    for dataset in profile.datasets:
        dataset.config = resolve(dataset.config)
        dataset.unified.config = resolve(dataset.unified.config)
    profile.research_files = {k: resolve(v) for k, v in profile.research_files.items()}  # type: ignore[misc]
    profile.request_log = resolve(profile.request_log)
    return profile
