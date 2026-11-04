"""Compose sealed day partitions into one serving generation (addendum A1 §3.7, §3.8.2; slice L3a).

``compose(as_of)`` reads the sealed partitions of the window ``[as_of - keep_days + 1, as_of]``:

1. **Identity check (N11).** Every partition must carry the current library identity; a stale one
   is reparsed first from its retained redacted records (``PartitionStore.reparse``, ``v+1``). A
   partition parsed under another identity is never mixed into a generation.
2. **Compose identity.** ``sha256`` of the window's ``(day, version, library identity)`` list and
   the compose recipe. When the active generation was composed with the same identity, compose
   is a no-op (``unchanged``): only new or changed days lead to a build, and within a build the
   write path reuses extraction and embeddings of unchanged chunks from the ledger.
3. **Build.** Per-day documents and window rollups (``rollups.py``) go through
   ``WritePath.ingest(replace=True)``, which takes and releases the registry write lease itself
   (compose never holds it across steps). The manifest records a content-free ``logs`` section:
   source, window, partitions with versions and library identity, and the date basis.

**Day mapping (§3.8.2).** ``day_window`` turns a partition key into its UTC window and the source
index dates it reads; see ``core/config_logs.py`` for the two clocks.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..core.errors import SuError
from .partitions import FilePartitionStore, PartitionManifest
from .rollups import DayInputs, day_documents, window_documents

COMPOSE_FORMAT = 1


class ComposeRefused(SuError):
    """Compose cannot build a generation that satisfies its invariants (mixed identities, empty window)."""


@dataclass(frozen=True)
class DayWindow:
    key: str
    start: datetime  # UTC
    end: datetime  # UTC, exclusive
    index_dates: list[date]
    date_basis: str
    timezone: str

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


def day_window(day: date, clock: str, logs_timezone: str, source_timezone: str) -> DayWindow:
    """The UTC window of a partition key and the index dates it reads.

    * ``index_date``: the key **is** an index date; the window is that day in the source timezone
      (only used for event-time bounds of hourly strata); one index date.
    * ``event_time``: the key is a calendar day in ``logs.timezone``: local midnight to the next
      local midnight (23 or 25 hours across DST), converted to UTC; the index dates are every
      source-timezone date the window overlaps.
    """
    if clock == "index_date":
        zone = ZoneInfo(source_timezone)
        start = datetime.combine(day, time(0), tzinfo=zone).astimezone(timezone.utc)
        end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=zone).astimezone(timezone.utc)
        return DayWindow(day.isoformat(), start, end, [day], "index_date", source_timezone)
    if clock != "event_time":
        raise ValueError(f"unknown partition clock {clock!r}")
    zone = ZoneInfo(logs_timezone)
    start = datetime.combine(day, time(0), tzinfo=zone).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=zone).astimezone(timezone.utc)
    source_zone = ZoneInfo(source_timezone)
    first = start.astimezone(source_zone).date()
    last = (end - timedelta(microseconds=1)).astimezone(source_zone).date()
    dates = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    return DayWindow(day.isoformat(), start, end, dates, "event_time", logs_timezone)


def window_keys(as_of: date, keep_days: int) -> list[str]:
    return [(as_of - timedelta(days=offset)).isoformat() for offset in range(keep_days - 1, -1, -1)]


def cutoff(as_of: date, keep_days: int) -> str:
    """The oldest key still in the window; partitions with an older key are expired."""
    return (as_of - timedelta(days=keep_days - 1)).isoformat()


@dataclass
class ComposeResult:
    status: str  # "built" | "unchanged"
    as_of: str
    identity: str
    partitions: list[dict[str, Any]] = field(default_factory=list)
    reparsed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    documents: int = 0
    generation: str | None = None
    manifest: dict[str, Any] | None = None

    def report(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "as_of": self.as_of,
            "identity": self.identity,
            "partitions": self.partitions,
            "reparsed": self.reparsed,
            "missing": self.missing,
            "documents": self.documents,
            "generation": self.generation,
        }


def compose_recipe(config: Any) -> dict[str, Any]:
    logs = config.logs
    return {
        "format": COMPOSE_FORMAT,
        "source": logs.source,
        "keep_days": logs.retention.keep_days,
        "clock": logs.partitions.clock,
        "timezone": logs.timezone,
        "compose": logs.compose.model_dump(mode="json"),
    }


def compose(
    config: Any,
    store: FilePartitionStore,
    write_path: Any,
    library: Any,
    as_of: date,
    *,
    reparse: Any,
    date_basis: dict[str, str],
) -> ComposeResult:
    """See the module docstring. ``reparse(units, library)`` builds a ``ParsedPartition``;
    ``date_basis`` is ``{"date_basis": ..., "date_timezone": ...}``."""
    logs = config.logs
    keys = window_keys(as_of, logs.retention.keep_days)
    identity_now = library.identity
    manifests: list[PartitionManifest] = []
    result = ComposeResult(status="built", as_of=as_of.isoformat(), identity="")
    for key in keys:
        manifest = store.get(key)
        if manifest is None:
            result.missing.append(key)
            continue
        if manifest.library_identity != identity_now:
            manifest = store.reparse(key, library, reparse)
            result.reparsed.append(key)
        if manifest.library_identity != identity_now:
            raise ComposeRefused(f"partition {key} carries library {manifest.library_identity} after reparse, compose uses {identity_now}")
        manifests.append(manifest)
    if not manifests:
        raise ComposeRefused(f"no sealed partition of source {logs.source!r} in the window ending {as_of.isoformat()}")
    result.partitions = [{"key": m.key, "version": m.version, "library_identity": m.library_identity} for m in manifests]
    recipe = compose_recipe(config)
    result.identity = hashlib.sha256(json.dumps({"partitions": result.partitions, "recipe": recipe}, sort_keys=True).encode()).hexdigest()
    active = write_path.registry.current()
    if active is not None and (active.get("logs") or {}).get("compose_identity") == result.identity:
        result.status = "unchanged"
        result.generation = active["generation"]
        return result
    days = [
        DayInputs(m.key, store.read(m.key, "aggregates.json"), store.read(m.key, "hourly_cells.json"), store.read(m.key, "exemplars.json"), date_basis)
        for m in manifests
    ]
    documents = []
    for day in days:
        documents.extend(day_documents(config.dataset, day, signature_hour=logs.compose.signature_hour))
    documents.extend(window_documents(config.dataset, days, library, max_exemplars=logs.compose.max_exemplars_per_signature))
    result.documents = len(documents)
    section = {
        "source": logs.source,
        "as_of": as_of.isoformat(),
        "composed_at": datetime.now(timezone.utc).isoformat(),
        "window": {"first": keys[0], "last": keys[-1], "keep_days": logs.retention.keep_days},
        "compose_identity": result.identity,
        "partitions": result.partitions,
        "library_identity": identity_now,
        **date_basis,
    }
    manifest = write_path.ingest(documents, replace=True, rebuild=True, manifest_extra={"logs": section})
    built = (manifest.get("pattern_provenance") or {}).get("library_identity")
    if built is not None and built != identity_now:
        # The library changed between the identity check and the build: the generation is active
        # (it is internally consistent) but the run reports the drift; the next run reparses.
        raise ComposeRefused(f"the write path built with library {built[:12]}, the partitions were checked against {identity_now[:12]}")
    result.generation = manifest["generation"]
    result.manifest = manifest
    return result
