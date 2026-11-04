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

**Analytics profile (addendum A2 §4.1; slices L5a/L5b).** With ``logs.compose.kinds`` on, compose
also builds the analytics kinds (``log_rollups.py``), the per-tenant exemplar documents and the
co-firing incidents (``communities/cooccurrence.py``). A partition sealed before
``PARTITION_FORMAT`` is reparsed from its retained records first (review B6); a library change is a
**delta** reparse (``reparse.py``, review B3) when that is provably enough.

**Derived data and the analytics kinds.** Days kept by ``logs.retention.derived`` beyond the
partition window are composed from their per-day derived snapshots (``write_derived``) exactly like
window days, so the analytics kinds of every kept day are rebuilt deterministically; day documents
are content-addressed (their ids name the day and dimensions, never the partition version), so an
unchanged day keeps its document ids and content hashes and the write path reuses them. A derived
day snapshotted before ``PARTITION_FORMAT`` has no typed values: its analytics kinds are reported
under ``analytics_unavailable`` (tools answer ``source_unavailable`` for it, never zero).

**Day mapping (§3.8.2).** ``day_window`` turns a partition key into its UTC window and the source
index dates it reads; see ``core/config_logs.py`` for the two clocks.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..core.errors import SuError
from ..core.types import Document
from .log_rollups import analytics_day_documents, endpoint_window_documents, signature_catalog_documents
from .logs_parse import PARTITION_FORMAT
from .partitions import FilePartitionStore, PartitionManifest
from .rollups import DayInputs, day_documents, window_documents

COMPOSE_FORMAT = 1
DERIVED = "derived"


def derived_dir(store: FilePartitionStore) -> Path:
    """``<partitions>/derived/<source>``: one masked derived snapshot per composed day."""
    return store.root / DERIVED / store.source


DERIVED_FORMAT = 2
# Sealed artifacts that hold line-level text or per-line rows: never part of a derived snapshot.
LINE_LEVEL = ("matches.jsonl.gz", "unparsed.jsonl.gz", "manifest.json")
TYPED_MARKERS = ("typed", "endpoint", "exception", "coverage")


def snapshot_identity(manifest: PartitionManifest, overlay: dict[str, Any] | None = None) -> str:
    """``sha(partition key, version, library identity, partition format, fallback identity, compose
    format)`` (A2 §4.8.6): the snapshot is rewritten whenever this changes while the partition exists."""
    data = [manifest.key, manifest.version, manifest.library_identity, getattr(manifest, "format", 1), (overlay or {}).get("identity"), COMPOSE_FORMAT]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()


def _overlay(store: FilePartitionStore, manifest: PartitionManifest) -> dict[str, Any] | None:
    """The fallback overlay of the sealed version, read through ``OverlayStore.rows`` (the most
    recently sealed overlay of this version): its identity, its per-unit accounting and report, and
    content-free aggregates of its rows (counts by final status, reason, signature, extraction
    source and field source). Never field values: a derived snapshot holds no line-level rows."""
    from collections import Counter

    from .overlay import OverlayStore

    found = OverlayStore(store).rows(manifest.key, version=manifest.version)
    if found is None:
        return None
    overlay, heads = found
    by_status: Counter[str] = Counter()
    by_signature: Counter[str] = Counter()
    by_field_source: Counter[str] = Counter()
    for head in heads:
        by_status[f"{head.final_status}:{head.reason}" if head.reason else head.final_status] += 1
        if head.signature_id and head.extraction_source:
            by_signature[f"{head.signature_id}|{head.extraction_source}"] += 1
        by_field_source.update(f"{f.name}|{f.source}" for f in head.fields)
    from .log_rollups import overlay_aggregates

    return {
        "identity": overlay.identity,
        "format": overlay.format,
        "library_identity": overlay.library_identity,
        "created_at": overlay.created_at.isoformat(),
        "accounting": {unit: account.model_dump(mode="json") for unit, account in overlay.accounting.items()},
        "report": overlay.report,
        "rows": {
            "heads": len(heads),
            "by_status": dict(sorted(by_status.items())),
            "by_signature_source": dict(sorted(by_signature.items())),
            "by_field_source": dict(sorted(by_field_source.items())),
        },
        # the model-assisted heads aggregated like the deterministic aggregates (content-free rows):
        # what compose turns into ``extraction_source: model_assisted`` rollup rows (L5/B7)
        "aggregates": overlay_aggregates(store, manifest, heads),
    }


def write_derived(store: FilePartitionStore, manifest: PartitionManifest) -> dict[str, Any]:
    """Snapshot (``derived_format: 2``) the derived inputs of a sealed partition: what compose needs to keep a day
    in the serving generation, and what the history tier (H1) will import, after the partition
    expired (``logs.retention.derived``). It holds every aggregate-level artifact (aggregates, hourly
    cells, masked exemplars and any typed-value artifact: endpoint, exception, coverage data), the
    fallback overlay, and a content-free accounting summary; never line-level rows. Rewritten only
    when its identity changes."""
    folder = derived_dir(store)
    path = folder / f"{manifest.key}.json.gz"
    overlay = _overlay(store, manifest)
    identity = snapshot_identity(manifest, overlay)
    entry = {"key": manifest.key, "version": manifest.version, "library_identity": manifest.library_identity, "derived_format": DERIVED_FORMAT, "identity": identity}
    if path.exists() and read_derived(store, manifest.key).get("identity") == identity:
        return entry
    artifacts = {}
    for name in sorted(manifest.artifacts):
        if name.startswith("units/") or name in LINE_LEVEL or not name.endswith(".json"):
            continue
        artifacts[name] = store.read(manifest.key, name)
    typed = {k: v for k, v in artifacts.items() if any(m in k for m in TYPED_MARKERS)}
    payload = {
        **entry,
        "partition_format": getattr(manifest, "format", 1),
        "overlay_identity": overlay["identity"] if overlay else None,
        "overlay": overlay or {"status": "source_unavailable", "reason": "no fallback overlay for this partition version"},
        "aggregates": artifacts.get("aggregates.json", {}),
        "hourly_cells": artifacts.get("hourly_cells.json", []),
        "exemplars": artifacts.get("exemplars.json", {}),
        "artifacts": {k: v for k, v in artifacts.items() if k not in {"aggregates.json", "hourly_cells.json", "exemplars.json"}},
        "typed_inputs": typed or {"status": "source_unavailable", "reason": f"partition format {getattr(manifest, 'format', 1)} holds no typed values"},
        "accounting": {
            "counts": dict(manifest.counts),
            "unit_counts": dict(manifest.unit_counts),
            "sample_methods": dict(manifest.sample_methods),
            "capped": manifest.capped,
            "redaction": {k: v for k, v in (manifest.redaction or {}).items() if k in {"mode", "engine", "stamp", "counts"}},
        },
    }
    folder.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(gzip.compress(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode(), mtime=0))
    os.replace(tmp, path)
    return entry


def derived_keys(store: FilePartitionStore) -> list[str]:
    folder = derived_dir(store)
    return sorted(p.name[: -len(".json.gz")] for p in folder.glob("*.json.gz")) if folder.is_dir() else []


def read_derived(store: FilePartitionStore, key: str) -> dict[str, Any]:
    """A day's snapshot. A format-1 snapshot (before ``derived_format`` existed: aggregates, hourly
    cells and exemplars only) is read and marked ``derived_format: 1, legacy: true`` with an identity
    derived from its key, version and library identity; its typed inputs, accounting and overlay
    are ``source_unavailable``."""
    payload = json.loads(gzip.decompress((derived_dir(store) / f"{key}.json.gz").read_bytes()))
    if "derived_format" not in payload:
        basis = {"derived_format": 1, "key": payload["key"], "version": payload["version"], "library_identity": payload["library_identity"]}
        unavailable = {"status": "source_unavailable", "reason": "format-1 snapshot"}
        payload.update(
            derived_format=1,
            legacy=True,
            identity=hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest(),
            typed_inputs=unavailable,
            accounting=unavailable,
            overlay=None,
            overlay_identity=None,
        )
    return payload


def _doc_cache(store: FilePartitionStore) -> Path:
    return derived_dir(store) / "docs"


def cached_day_documents(store: FilePartitionStore, day: "DayInputs", identity: str, recipe: dict[str, Any], build: Any) -> tuple[list[Document], bool]:
    """The day's documents from the cache keyed by ``(snapshot identity, compose recipe)``; built
    (and cached, replacing older entries of the day) only for a new or changed day."""
    key = hashlib.sha256(json.dumps({"identity": identity, "recipe": recipe}, sort_keys=True).encode()).hexdigest()[:24]
    folder = _doc_cache(store)
    path = folder / f"{day.key}.{key}.json.gz"
    if path.exists():
        return [Document.model_validate(d) for d in json.loads(gzip.decompress(path.read_bytes()))], True
    documents = build(day)
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob(f"{day.key}.*.json.gz"):
        old.unlink()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(gzip.compress(json.dumps([d.model_dump(mode="json") for d in documents], sort_keys=True, ensure_ascii=False).encode(), mtime=0))
    os.replace(tmp, path)
    return documents, False


def expire_derived(store: FilePartitionStore, key: str) -> bool:
    path = derived_dir(store) / f"{key}.json.gz"
    for cached in _doc_cache(store).glob(f"{key}.*.json.gz") if _doc_cache(store).is_dir() else []:
        cached.unlink()
    if path.exists():
        path.unlink()
        return True
    return False


def _identity_span(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """The rendering flag for answers over days built with different library versions: present only
    when the served days span more than one library identity."""
    identities = sorted({e["library_identity"] for e in entries if e.get("library_identity")})
    if len(identities) <= 1:
        return {}
    return {"library_identities": identities, "identity_span": {"identities": len(identities), "note": f"the served days span {len(identities)} library identities"}}


def _rows(snapshot: dict[str, Any]) -> int:
    """Aggregate rows a served day contributes (unit and signature rows, hourly cells)."""
    aggregates = snapshot.get("aggregates") or {}
    return len(aggregates.get("units", [])) + len(aggregates.get("signatures", [])) + len(snapshot.get("hourly_cells") or [])


def downsample(day: "DayInputs", age_days: int) -> "DayInputs":
    """Hook for a later hourly -> daily downsampling policy of old derived days (not implemented:
    every derived day keeps its hourly cells)."""
    return day


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
    """The first key of the compose window ending at ``as_of`` (its last day, included)."""
    return (as_of - timedelta(days=keep_days - 1)).isoformat()


def expiry_cutoff(today: date, keep_days: int) -> str:
    """The oldest partition key retention keeps on calendar day ``today`` (in ``logs.timezone``).

    ``keep_days: N`` keeps the **N most recent complete days**: ``today - N`` through ``today - 1``.
    Today itself is not complete and is not counted. A partition is expired by the first retention
    run on calendar day ``key + N + 1`` (with ``keep_days: 10``, day 1 is deleted on day 12 and day
    2 is the oldest day served after that run). The daily run for yesterday composes exactly this
    window (``window_keys(today - 1, N)``)."""
    return (today - timedelta(days=keep_days)).isoformat()


@dataclass
class ComposeResult:
    status: str  # "built" | "unchanged"
    as_of: str
    identity: str
    partitions: list[dict[str, Any]] = field(default_factory=list)
    reparsed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    derived: list[dict[str, Any]] = field(default_factory=list)  # days composed from derived snapshots only
    reused_days: list[str] = field(default_factory=list)  # day documents taken from the cache (unchanged days)
    archived_unserved: list[str] = field(default_factory=list)  # in serve_days but over max_serve_rows
    documents: int = 0
    generation: str | None = None
    manifest: dict[str, Any] | None = None
    reparse: dict[str, dict[str, Any]] = field(default_factory=dict)  # key -> delta/full plan
    history_rows: int = 0  # H1: day-shard rows written by this compose
    history_appended: list[str] = field(default_factory=list)  # days whose shard was new
    history_gc: list[str] = field(default_factory=list)  # shards deleted (no generation references them)
    analytics_unavailable: list[str] = field(default_factory=list)  # derived days below format 2
    co_firing: dict[str, Any] = field(default_factory=dict)

    def report(self) -> dict[str, Any]:
        out = {
            "status": self.status,
            "as_of": self.as_of,
            "identity": self.identity,
            "partitions": self.partitions,
            "derived": [d["key"] for d in self.derived],
            "reused_days": self.reused_days,
            "archived_unserved": self.archived_unserved,
            "reparsed": self.reparsed,
            "missing": self.missing,
            "documents": self.documents,
            "generation": self.generation,
        }
        for name in ("reparse", "analytics_unavailable", "co_firing", "history_rows", "history_appended", "history_gc"):
            if getattr(self, name):
                out[name] = getattr(self, name)
        return out


def compose_recipe(config: Any) -> dict[str, Any]:
    logs = config.logs
    return {
        "format": COMPOSE_FORMAT,
        "source": logs.source,
        "keep_days": logs.retention.keep_days,
        "clock": logs.partitions.clock,
        "timezone": logs.timezone,
        "compose": logs.compose.model_dump(mode="json"),
        **({"co_firing": logs.communities.co_firing.model_dump(mode="json")} if logs.compose.kinds.incident else {}),
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
    kinds = logs.compose.kinds
    analytics = any(kinds.model_dump().values()) or logs.compose.exemplars == "per_tenant"
    manifests: list[PartitionManifest] = []
    result = ComposeResult(status="built", as_of=as_of.isoformat(), identity="")
    for key in keys:
        manifest = store.get(key)
        if manifest is None:
            result.missing.append(key)
            continue
        if analytics and manifest.format < PARTITION_FORMAT:
            # review B6: older artefacts lack typed values; reparse from the retained records
            old_format = manifest.format
            manifest = store.reparse(key, library, reparse)
            result.reparsed.append(key)
            result.reparse[key] = {"mode": "full", "reason": f"partition format {old_format} below {PARTITION_FORMAT}"}
        if manifest.library_identity != identity_now:
            from .reparse import reparse_partition

            manifest, plan = reparse_partition(store, key, library, reparse)
            result.reparsed.append(key)
            result.reparse[key] = plan.report()
        if manifest.library_identity != identity_now:
            raise ComposeRefused(f"partition {key} carries library {manifest.library_identity} after reparse, compose uses {identity_now}")
        manifests.append(manifest)
    if not manifests:
        raise ComposeRefused(f"no sealed partition of source {logs.source!r} in the window ending {as_of.isoformat()}")
    result.partitions = [{"key": m.key, "version": m.version, "library_identity": m.library_identity} for m in manifests]
    # the window days' snapshot identities include their fallback overlay: a newly sealed overlay
    # changes the composition even when no partition version changed
    snapshot_identities = {m.key: write_derived(store, m)["identity"] for m in manifests}
    # Derived days (``retention.derived`` beyond the partition window): composed from their masked
    # derived snapshots; a day whose library identity is stale cannot be reparsed (its partition is
    # gone) and is served as composed, listed under ``derived`` with its identity.
    # Derived days beyond the masked window are served only within ``logs.history.serve_days``
    # (default: the masked window) until the history tier lands; older snapshots stay archived.
    serve = logs.serve_days()
    span = logs.retention.derived_days()
    span = serve if span is None else (span if serve is None else min(span, serve))
    first = "0000-00-00" if span is None else (as_of - timedelta(days=span - 1)).isoformat()
    window = set(keys)
    result.derived = []
    for key in sorted(derived_keys(store), reverse=True):  # most recent first: the row bound keeps the newest
        if key in window or key > as_of.isoformat() or key < first:
            continue
        snapshot = read_derived(store, key)
        result.derived.append(
            {"key": key, "version": snapshot["version"], "library_identity": snapshot["library_identity"], "derived_format": snapshot["derived_format"], "identity": snapshot["identity"], "rows": _rows(snapshot)}
        )
    max_rows = logs.history.max_serve_rows
    if max_rows is not None:
        budget = max_rows - sum(_rows(read_derived(store, k)) for k in keys if (derived_dir(store) / f"{k}.json.gz").exists())
        kept = []
        for entry in result.derived:
            if entry["rows"] > budget:
                result.archived_unserved.append(entry["key"])
                continue
            budget -= entry["rows"]
            kept.append(entry)
        result.derived = kept
    result.derived.sort(key=lambda e: e["key"])
    recipe = compose_recipe(config)
    identity_input: dict[str, Any] = {"partitions": result.partitions, "recipe": recipe, "snapshots": snapshot_identities}
    if result.derived:
        identity_input["derived"] = result.derived  # absent under the single window: identities unchanged
    result.identity = hashlib.sha256(json.dumps(identity_input, sort_keys=True).encode()).hexdigest()
    active = write_path.registry.current()
    if active is not None and (active.get("logs") or {}).get("compose_identity") == result.identity:
        result.status = "unchanged"
        result.generation = active["generation"]
        return result
    documents: list[Document] = []
    masked_days = []
    cells: list[dict[str, Any]] = []
    doc_recipe = {"dataset": config.dataset, "signature_hour": logs.compose.signature_hour, **date_basis, "format": COMPOSE_FORMAT}
    if analytics:  # the analytics kinds are part of a day's cached documents
        doc_recipe["analytics"] = {"kinds": kinds.model_dump(), "endpoints": logs.compose.endpoints.model_dump(), "format": PARTITION_FORMAT}

    def build_day(day: DayInputs, unavailable: bool) -> list[Document]:
        docs = day_documents(config.dataset, day, signature_hour=logs.compose.signature_hour)
        if analytics and not unavailable:
            docs.extend(analytics_day_documents(config.dataset, day, kinds, max_endpoints_per_service=logs.compose.endpoints.max_per_service))
        return docs

    result.reused_days = []
    tier = logs.history.tier
    history = None
    if tier:
        from .history import content_shard_id, open_history, source_key

        history = open_history(config, getattr(write_path, "backends", {}) or {})
    shard_refs: dict[str, str] = {}
    identities: dict[str, str] = {}
    for entry in sorted([*result.derived, *result.partitions], key=lambda e: e["key"]):
        snapshot = read_derived(store, entry["key"])
        identities[entry["key"]] = str(snapshot.get("library_identity"))
        age = (as_of - date.fromisoformat(entry["key"])).days
        aggregates = snapshot["aggregates"]
        model_rows = ((snapshot.get("overlay") or {}).get("aggregates")) if isinstance(snapshot.get("overlay"), dict) else None
        if model_rows:  # the fallback overlay's model-assisted rows ride with the day (B7)
            aggregates = {**aggregates, "model_assisted": model_rows}
        day = downsample(DayInputs(entry["key"], aggregates, snapshot["hourly_cells"], snapshot["exemplars"], date_basis), age)
        unavailable = analytics and int(day.aggregates.get("format", 1)) < PARTITION_FORMAT
        if unavailable:
            # a derived day snapshotted before format 2 whose partition is gone: the analytics
            # kinds are unavailable for it (``source_unavailable``), never zero
            result.analytics_unavailable.append(day.key)
        if kinds.incident:
            cells.extend(day.hourly_cells)
        if entry["key"] in window:
            masked_days.append(day)  # window rollups cover the masked window only
        if history is not None:
            # H1: the day's documents are one content-addressed, append-only day shard; a known
            # shard is referenced without reading or writing a row
            key = source_key(day.key, snapshot["identity"], doc_recipe)
            shard = history.lookup(key)
            if shard is None:
                built, reused = cached_day_documents(store, day, snapshot["identity"], doc_recipe, lambda d, u=unavailable: build_day(d, u))
                shard = content_shard_id(day.key, built)
                written = history.append(shard, day.key, built, config.metadata_schema)  # 0 when identical content exists
                history.remember(key, shard)
                if written:
                    result.history_rows += written
                    result.history_appended.append(day.key)
            shard_refs[day.key] = shard
            continue
        built, reused = cached_day_documents(store, day, snapshot["identity"], doc_recipe, lambda d, u=unavailable: build_day(d, u))
        documents.extend(built)
        if reused:
            result.reused_days.append(day.key)
    documents.extend(
        window_documents(config.dataset, masked_days, library, max_exemplars=logs.compose.max_exemplars_per_signature, exemplars_mode=logs.compose.exemplars)
    )
    basis = dict(date_basis)
    if history is not None:
        from .log_rollups import catalog_from_summaries, graph_link_documents, signature_history_documents, signature_lineage_documents

        summaries = list(history.summaries(sorted(shard_refs.values())).values())
        documents.extend(catalog_from_summaries(config.dataset, summaries, basis))
        documents.extend(graph_link_documents(config.dataset, summaries, basis))
        documents.extend(signature_history_documents(config.dataset, summaries, identities, basis))
        documents.extend(signature_lineage_documents(config.dataset, library))
        if kinds.endpoint_day:
            window_shards = [shard_refs[k] for k in sorted(shard_refs) if k in window]
            documents.extend(endpoint_window_documents(config.dataset, _endpoint_rows(history, window_shards), logs.compose.endpoints.max_per_service, basis))
    elif analytics:
        if kinds.endpoint_day:
            documents.extend(endpoint_window_documents(config.dataset, [d for d in documents if d.kind == "endpoint_day" and d.metadata.get("date", "") >= keys[0]], logs.compose.endpoints.max_per_service, basis))
        # every served day's signature_day documents: first/last seen over the whole kept history
        documents.extend(signature_catalog_documents(config.dataset, [d for d in documents if d.kind == "signature_day"], basis))
    if kinds.incident:
        from ..communities.cooccurrence import co_firing, incident_documents

        report = co_firing(cells, logs.communities.co_firing)
        result.co_firing = report.summary()
        known = {d.id for d in documents}
        documents.extend(d for d in incident_documents(config.dataset, report, basis) if d.id not in known)
    days_composed = sorted(e["key"] for e in [*result.derived, *result.partitions] if e["key"] not in set(result.analytics_unavailable))
    del masked_days, cells  # the partition aggregates are not needed while the write path builds (N10)
    result.documents = len(documents)
    section = {
        "source": logs.source,
        "as_of": as_of.isoformat(),
        "composed_at": datetime.now(timezone.utc).isoformat(),
        "window": {"first": keys[0], "last": keys[-1], "keep_days": logs.retention.keep_days},
        "compose_identity": result.identity,
        "partitions": result.partitions,
        **({"derived": result.derived, "derived_scope": logs.retention.derived, "serve_days": logs.serve_days()} if result.derived else {}),
        **_identity_span([*result.partitions, *result.derived]),
        "library_identity": identity_now,
        "missing": list(result.missing),
        # every day with derived documents in this generation (window and derived): a day of a
        # requested range outside this list is ``source_unavailable``, never zero
        "days": days_composed,
        **({"history": {"tier": "sqlite", "shards": dict(sorted(shard_refs.items()))}} if history is not None else {}),
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
    if history is not None:
        result.history_gc = history.gc(referenced_shards(write_path))
    return result


def _endpoint_rows(history: Any, shards: list[str]) -> list[Document]:
    """The ``endpoint_day`` documents of the window's day shards (for ``endpoint_window``)."""
    from ..core.types import MetadataSchema, Predicate, ScopeSpec

    if not shards:
        return []
    spec = ScopeSpec(predicates=[Predicate(field="kind", op="eq", value="endpoint_day")])
    result = history.execute(shards, spec, [], MetadataSchema())
    return history.documents(shards, result.matched_ids)


def referenced_shards(write_path: Any) -> set[str]:
    """Day shards referenced by any generation still in the registry (the active one, rollback
    generations, pinned ones): gc by reference."""
    registry = write_path.registry
    out: set[str] = set()
    for generation in registry.generations():
        manifest = registry.manifest(generation) or {}
        out.update(((manifest.get("logs") or {}).get("history") or {}).get("shards", {}).values())
    return out
