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
)
# Text fields of compose documents that carry redacted log-line text (retention invariant: never embedded).
LINE_TEXT_FIELDS = {"signature": ("exemplars",)}
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


class PartitionsConfig(StrictModel):
    path: Path  # partition store root (relative to the config file); outside any served directory
    clock: Literal["index_date", "event_time"] = "index_date"


class ParseConfig(StrictModel):
    workers: int = Field(default=1, ge=1, le=32)  # >1: a process pool; every worker checks the library identity
    batch_size: int = Field(default=2000, ge=1, le=100_000)
    max_exemplars_per_signature: int = Field(default=3, ge=0, le=50)
    max_unparsed: int = Field(default=2000, ge=0, le=1_000_000)


class ComposeConfig(StrictModel):
    signature_hour: bool = False  # opt-in rollup (hour x service x signature)
    max_exemplars_per_signature: int = Field(default=3, ge=0, le=50)


class RetentionConfig(StrictModel):
    keep_days: int = Field(default=10, ge=1, le=3660)
    # Log generations kept for rollback, the active one included. Rule (a): a generation that lists an
    # expired partition is deleted whatever this says.
    keep_generations: int = Field(default=2, ge=1, le=100)
    max_pin_wait_seconds: float = Field(default=900, ge=0, le=86_400)
    pin_retry_seconds: float = Field(default=15, gt=0, le=3600)
    verify_sample: int = Field(default=1000, ge=1, le=100_000)


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

    @field_validator("timezone")
    @classmethod
    def known_zone(cls, value):
        return _zone(value)
