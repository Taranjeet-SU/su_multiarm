"""Configuration of daily log analytics (addendum A1 §3.5, §3.7, §3.8; slices L3a and L3b): ``logs``,
plus the write-path keys it needs, ``ingestion.line_recipe`` and ``ingestion.projections``.

``logs`` names one read-only source (``sources.<name>``), the partition store directory, the
partition clock, the reporting timezone, the compose options and the retention window. Nothing here
names code. Every key is consumed by ``ingestion/partitions.py``, ``logs_compose.py``,
``schedule.py``, ``retention.py`` or ``pins.py``.

**Partition clock and timezone (§3.8.2).**

* ``index_date`` (default): a partition is one *index date* of the source (the date in the index
  name, read in ``sources.<name>.timezone``). Documents carry ``date`` = that index date with
  ``date_basis: index_date`` and ``date_timezone`` = the source timezone. A reporting day in another
  timezone is **not** re-cut: answers state the index-date basis and its timezone.
* ``event_time``: a partition is one calendar day in ``logs.timezone``. Its window runs from local
  midnight to the next local midnight (23 or 25 hours across a DST change), converted to UTC; the
  source is asked for every index date that window overlaps, and each unit is sliced to the window
  by event time. Documents carry ``date_basis: event_time`` and ``date_timezone`` = ``logs.timezone``.
  ``event_time`` with ``timezone: UTC`` is the addendum's ``event_time_utc``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ..models import StrictModel

# Document kinds compose emits (addendum §3.7; ``issue_report`` arrives with the co-firing communities, L5).
LOG_DOCUMENT_KINDS = (
    "unit_day",
    "signature_day",
    "service_day",
    "signature_window",
    "signature_service_day",
    "signature_tenant_window",
    "signature_hour",
    "signature",
    *("log_day", "log_hour", "endpoint_day", "exception_day", "coverage_day", "incident", "signature_examples"),
)
# Text fields of compose documents that carry redacted log-line text (retention invariant: never embedded).
LINE_TEXT_FIELDS = {"signature": ("exemplars",), "signature_examples": ("exemplars",)}
# Kinds of the analytics profile (addendum A2 §4.1.1).
ANALYTICS_KINDS = ("log_day", "log_hour", "endpoint_day", "exception_day", "coverage_day", "incident", "signature_examples")
# Registry kinds with a pin registry (addendum §3.8.4 n4); ``logs`` is refused on any other.
PIN_REGISTRY_KINDS = ("memory", "sqlite", "postgres", "sqlalchemy")
KIND_NAME = r"^[a-z][a-z0-9_]{0,63}$"


def _zone(value: str) -> str:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown timezone {value!r}") from None
    return value


class LineRecipeConfig(StrictModel):
    """``ingestion.line_recipe``: the ``line`` chunk recipe (addendum §3.4.2 n1). Each listed field is
    cut into one chunk per log record (records are separated by one blank line), whatever
    ``chunking.size`` is, so a line-scoped fact never spans two chunks. A record longer than
    ``max_chunk_chars`` is a build error, never split."""

    fields: list[str] = Field(min_length=1)
    max_chunk_chars: int = Field(default=24_576, ge=256, le=1_048_576)

    @field_validator("fields")
    @classmethod
    def names(cls, value):
        for name in value:
            if not re.fullmatch(KIND_NAME, name) or name == "title":
                raise ValueError(f"line_recipe field {name!r} must be a field name other than title")
        if len(set(value)) != len(value):
            raise ValueError("line_recipe fields must be unique")
        return value


class ProjectionsConfig(StrictModel):
    """``ingestion.projections``: which document kinds stay out of a projection, and which fields of a
    kind are embedded. Catalog and records always hold every document."""

    exclude_kinds: dict[Literal["lexical", "vector", "graph"], list[str]] = Field(default_factory=dict)
    # kind -> embedded fields (``title`` included only when listed); kinds not listed embed every field
    embed_fields: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("exclude_kinds")
    @classmethod
    def kinds(cls, value):
        for projection, kinds in value.items():
            for kind in kinds:
                if not re.fullmatch(KIND_NAME, kind):
                    raise ValueError(f"exclude_kinds.{projection}: {kind!r} is not a kind name")
        return value

    @field_validator("embed_fields")
    @classmethod
    def fields(cls, value):
        for kind, names in value.items():
            if not re.fullmatch(KIND_NAME, kind):
                raise ValueError(f"embed_fields: {kind!r} is not a kind name")
            if not names:
                raise ValueError(f"embed_fields.{kind} is empty; exclude the kind from vector instead")
        return value

    def excluded(self, projection: str, kind: str) -> bool:
        return kind in self.exclude_kinds.get(projection, [])  # type: ignore[call-overload]

    def embeds(self, kind: str, field: str) -> bool:
        if self.excluded("vector", kind):
            return False
        allowed = self.embed_fields.get(kind)
        return allowed is None or field in allowed


class RawCopyConfig(StrictModel):
    """Optional raw-copy store (``ingestion/raw_store.py``): pulled units **unredacted**, so redaction
    and parsing can be re-run without the source. Off by default; kept ``retention.raw_days``."""

    enabled: bool = False
    path: Path | None = None  # outside the partition store, the backends and the pattern library
    compression: Literal["gzip", "none"] = "gzip"

    @model_validator(mode="after")
    def located(self):
        if self.enabled and self.path is None:
            raise ValueError("logs.partitions.raw.enabled needs a path")
        return self


class PartitionsConfig(StrictModel):
    path: Path  # partition store root (relative to the config file); outside any served directory
    clock: Literal["index_date", "event_time"] = "index_date"
    raw: RawCopyConfig = Field(default_factory=RawCopyConfig)


class ParseConfig(StrictModel):
    workers: int = Field(default=1, ge=1, le=32)  # >1: a process pool; every worker checks the library identity
    batch_size: int = Field(default=2000, ge=1, le=100_000)
    max_exemplars_per_signature: int = Field(default=3, ge=0, le=50)
    max_unparsed: int = Field(default=2000, ge=0, le=1_000_000)


class ComposeKinds(StrictModel):
    """Analytics kinds composed from format-2 partitions (addendum A2 §4.1.1–4.1.2). Off by default
    so a generic logs config composes exactly what it did; the ``logs_example`` profile turns them on."""

    log_day: bool = False  # day x tenant x env x service x level x status x signature
    log_hour: bool = False  # the same per UTC hour
    endpoint_day: bool = False  # day x tenant x env x service x endpoint template x method x status class
    exception_day: bool = False  # day x tenant x env x service x signature x exception class
    coverage_day: bool = False  # day x tenant x env x service: pulled lines by status
    incident: bool = False  # co-firing communities over hourly cells (``logs.communities.co_firing``)


class EndpointsConfig(StrictModel):
    normalise: Literal["path_template"] = "path_template"
    # Endpoint templates kept per (day, service); the rest fold into ``__other__`` (row approximate).
    max_per_service: int = Field(default=500, ge=1, le=100_000)


class ComposeConfig(StrictModel):
    signature_hour: bool = False  # opt-in rollup (hour x service x signature)
    max_exemplars_per_signature: int = Field(default=3, ge=0, le=50)
    kinds: ComposeKinds = Field(default_factory=ComposeKinds)
    # ``signature``: exemplars ride on the one-per-signature document (A1; lines of every tenant in
    # one document). ``per_tenant``: the ``signature`` document holds the template only and every
    # tenant gets its own ``signature_examples`` document (review B4: no cross-tenant lines in one
    # document, so a tenant-scoped principal can read them).
    exemplars: Literal["signature", "per_tenant"] = "signature"
    endpoints: EndpointsConfig = Field(default_factory=EndpointsConfig)


class CoFiringConfig(StrictModel):
    """Co-firing communities (addendum A2 §4.1.4, A1 N6): signatures that fire together in hourly
    cells. A cell is ``(tenant, env, hour)`` (``tenant_env_hour``, default; review N8), ``(deployment, hour)``
    (``deployment_hour``, format-2 cells) or ``(tenant, env,
    service, hour)`` (``service_hour``). Communities are computed **per tenant** (review B4), so an
    incident never mixes tenants."""

    min_support_cells: int = Field(default=3, ge=1, le=10_000)  # cells where both signatures fired
    min_lift: float = Field(default=2.0, gt=0)
    max_signatures: int = Field(default=20_000, ge=2, le=1_000_000)  # per tenant, top by lines; the rest reported
    resolution: float = Field(default=1.0, gt=0, le=100)
    seed: int = 42
    # components of the kept-pair graph up to this size are one incident; larger ones are split by
    # the deterministic Louvain constructor (``communities/cooccurrence.py``)
    split_above: int = Field(default=12, ge=2, le=100_000)
    unit: Literal["tenant_env_hour", "service_hour", "deployment_hour"] = "tenant_env_hour"


class LogsCommunitiesConfig(StrictModel):
    co_firing: CoFiringConfig = Field(default_factory=CoFiringConfig)


class LogsServingConfig(StrictModel):
    # ``false`` (default): under logs.intents an ad-hoc typed scope that is kind-less, negated on
    # kind or spans several log kinds is refused with ``intent_required`` (``decision/log_guard.py``).
    allow_adhoc_scopes: bool = False


class LogIntentsConfig(StrictModel):
    """The typed log intents and tools (addendum A2 §4.1.5). ``enabled`` makes every decision agent
    route recognised log questions through one typed intent step, and refuses free structured scopes
    over the rollups (review B5)."""

    enabled: bool = False
    default_window: str = Field(default="today", min_length=1, max_length=64)


class RetentionConfig(StrictModel):
    """Retention per data class.

    - ``keep_days`` (alias ``partition_days``): the masked day partitions (records, matches, unparsed
      queue), the N most recent complete days.
    - ``raw_days``: the raw-copy store (``partitions.raw``), shorter than ``keep_days``.
    - ``derived``: the composed arm data (day and hour rollups, signature catalog and exemplars, graph,
      communities, the documents of every projection). ``window`` (default; the single-window
      behaviour): derived data follows the partitions. ``forever``: every composed day stays in the
      serving generation after its partition expired. An integer: days of derived data kept
      (at least ``keep_days``).
    """

    keep_days: int = Field(default=10, ge=1, le=3660)
    raw_days: int = Field(default=3, ge=1, le=3660)
    derived: Literal["window", "forever"] | int = "window"
    # Log generations kept for rollback, the active one included. Rule (a): a generation that lists an
    # expired partition is deleted whatever this says.
    keep_generations: int = Field(default=2, ge=1, le=100)
    max_pin_wait_seconds: float = Field(default=900, ge=0, le=86_400)
    pin_retry_seconds: float = Field(default=15, gt=0, le=3600)
    verify_sample: int = Field(default=1000, ge=1, le=100_000)

    @model_validator(mode="before")
    @classmethod
    def partition_days_alias(cls, data):
        if isinstance(data, dict) and "partition_days" in data:
            data = dict(data)
            value = data.pop("partition_days")
            if "keep_days" in data and data["keep_days"] != value:
                raise ValueError("partition_days and keep_days name the same setting; give one")
            data["keep_days"] = value
        return data

    @model_validator(mode="after")
    def scopes(self):
        if isinstance(self.derived, int) and not isinstance(self.derived, bool):
            if self.derived < self.keep_days:
                raise ValueError("retention.derived (days) must be at least keep_days")
        return self

    def derived_days(self) -> int | None:
        """Days of derived data composed into the serving generation; ``None`` = every day."""
        if self.derived == "forever":
            return None
        if self.derived == "window":
            return self.keep_days
        return int(self.derived)


class PinsConfig(StrictModel):
    ttl_seconds: float = Field(default=120, gt=0, le=86_400)
    heartbeat_seconds: float = Field(default=30, gt=0, le=3600)

    @model_validator(mode="after")
    def renew_before_expiry(self):
        if self.heartbeat_seconds * 2 > self.ttl_seconds:
            raise ValueError("pins.heartbeat_seconds must be at most half of ttl_seconds")
        return self


class ScheduleConfig(StrictModel):
    lock_ttl_seconds: float = Field(default=3600, gt=0, le=86_400)


class SettleConfig(StrictModel):
    max_growth: float = Field(default=0.05, ge=0, le=100)  # a unit whose count grew by more is re-pulled


class FieldValidatorConfig(StrictModel):
    """A value check for one field (A2 §4.2.4, N17): numeric ``min``/``max``, an ``enum`` or an
    anchored ``regex`` (RE2 subset, linted). A value that fails it is ``range_invalid``."""

    min: float | None = None
    max: float | None = None
    enum: list[str] | None = Field(default=None, min_length=1)
    regex: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def coherent(self):
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min must not exceed max")
        if self.regex is not None:
            from ..extraction.patterns.engine import validate_regex

            errors = [i for i in validate_regex(self.regex) if i.severity == "error"]
            if errors:
                raise ValueError(f"validator regex refused: {errors[0].code}: {errors[0].message}")
        return self


class CoverageTargets(StrictModel):
    deterministic: float = Field(default=0.90, ge=0, le=1)  # regex-only, per core service
    extracted: float = Field(default=0.99, ge=0, le=1)  # with the model fallback
    info_tier: float = Field(default=0.80, ge=0, le=1)  # the capped INFO sample, never mixed in
    core_services: list[str] = Field(default_factory=list)  # empty: every service is core


class AccountingConfig(StrictModel):
    """Line accounting (addendum A2 §4.2.1): ``required_fields`` maps a signature category to the
    fields a ``parsed`` line of that category must carry with a valid value; a line an approved
    pattern matched without one is a field gap (``learning.extraction.fill``)."""

    required_fields: dict[str, list[str]] = Field(default_factory=dict)
    validators: dict[str, FieldValidatorConfig] = Field(default_factory=dict)
    # Owner targets (A2 O-A2-0): regex-only per core service and with the model fallback.
    targets: CoverageTargets = Field(default_factory=lambda: CoverageTargets())

    @field_validator("validators")
    @classmethod
    def validator_names(cls, value):
        for name in value:
            if not re.fullmatch(r"^[a-z][a-z0-9_]{0,63}$", name):
                raise ValueError(f"validator field {name!r} is not a field name")
        return value

    @field_validator("required_fields")
    @classmethod
    def names(cls, value):
        for category, fields in value.items():
            if not re.fullmatch(r"^[a-z][a-z0-9_.\-]{0,63}$", category):
                raise ValueError(f"category {category!r} is not an identifier")
            for name in fields:
                if not re.fullmatch(r"^[a-z][a-z0-9_]{0,63}$", name):
                    raise ValueError(f"required field {name!r} is not a field name")
            if len(set(fields)) != len(fields):
                raise ValueError(f"required_fields.{category} lists a field twice")
        return value


class HistoryConfig(StrictModel):
    """Serving window of derived data until the history tier (H1) lands: compose serves derived days
    only within ``serve_days`` (default: the masked window, ``retention.keep_days``); older derived
    snapshots stay archived for the history tier to import."""

    serve_days: int | None = Field(default=None, ge=1, le=3660)
    # Upper bound on aggregate rows (unit and signature rows, hourly cells) of the derived days served
    # beyond the masked window; the oldest such days are left archived first. ``None``: no bound.
    max_serve_rows: int | None = Field(default=None, ge=1)
    # H1 history tier (addendum A2 rev 4 §4.8; ``ingestion/history.py``): the day documents of every
    # served day are append-only day shards outside the generation, read through a shard filter; the
    # generation holds the window shard only. With the tier, ``serve_days`` defaults to everything
    # ``retention.derived`` keeps.
    tier: bool = False
    # the backend that holds the history rows (a declared backend serving records, e.g. opensearch);
    # ``None``: a sqlite file under the partition store. The shard index is always the sqlite sidecar.
    backend: str | None = Field(default=None, pattern=KIND_NAME)


class LogsConfig(StrictModel):
    source: str = Field(pattern=KIND_NAME)
    timezone: str = "UTC"  # what "today" means; the event_time clock cuts days here
    partitions: PartitionsConfig
    parse: ParseConfig = Field(default_factory=ParseConfig)
    compose: ComposeConfig = Field(default_factory=ComposeConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    pins: PinsConfig = Field(default_factory=PinsConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    settle: SettleConfig = Field(default_factory=SettleConfig)
    accounting: AccountingConfig = Field(default_factory=AccountingConfig)
    communities: LogsCommunitiesConfig = Field(default_factory=LogsCommunitiesConfig)
    intents: LogIntentsConfig = Field(default_factory=LogIntentsConfig)
    serving: LogsServingConfig = Field(default_factory=LogsServingConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)

    def serve_days(self) -> int | None:
        """Days of derived data served; ``None``: every day ``retention.derived`` keeps (history tier)."""
        if self.history.serve_days:
            return self.history.serve_days
        if self.history.tier:
            return self.retention.derived_days()
        return self.retention.keep_days

    @field_validator("timezone")
    @classmethod
    def known_zone(cls, value):
        return _zone(value)

    @model_validator(mode="after")
    def history_window(self):
        days = self.history.serve_days
        if days is not None:
            if days < self.retention.keep_days:
                raise ValueError("logs.history.serve_days must be at least retention.keep_days (the masked window is always served)")
            kept = self.retention.derived_days()
            if kept is not None and days > kept:
                raise ValueError(f"logs.history.serve_days ({days}) exceeds the derived data retention.derived keeps ({kept})")
        return self

    @model_validator(mode="after")
    def raw_tier(self):
        raw = self.partitions.raw
        if raw.enabled:
            if self.retention.raw_days >= self.retention.keep_days:
                raise ValueError("logs.retention.raw_days must be shorter than keep_days (raw copies are the most sensitive tier)")
            a, b = _absolute(raw.path), _absolute(self.partitions.path)  # type: ignore[arg-type]
            if a == b or a in b.parents or b in a.parents:
                raise ValueError("logs.partitions.raw.path must lie outside logs.partitions.path (and the reverse)")
        return self


def _absolute(path: Path) -> Path:
    import os

    return Path(os.path.abspath(path.expanduser()))
