"""Daily log operation (addendum A1 §3.8.1; slices L3a and L3b): ``run_daily``, ``backfill``, ``settle``.

``run_daily(D)`` holds **only the schedule lock** (the partition store's lease; never the registry
write lease) and runs, each step resumable:

1. refuse a mapping without a redactor (nothing unredacted is ever persisted; the CLI uses the
   safe redactor of ``privacy.engine: safe``);
2. source self-check (an ``error`` diagnostic stops the run);
3. ``discover`` the units of D (the partition clock decides which index dates and slices);
4. per unit not yet stored: ``count``, stratified ``pull``, map, multiline, ``put_unit`` (atomic,
   so a crash resumes at the next unit without duplicates);
5. parse (process pool when ``logs.parse.workers > 1``) and ``seal`` (refused on an accounting
   imbalance, A2 B1: the version stays open and resumable);
6. ``learn patterns run``: not in this slice (L4b), reported as ``not_run``;
6a. ``fallback`` (A2 rev 2 §4.2.8, when ``learning.extraction.enabled``): the LLM fallback over the
   sealed residue of D and of up to ``carry_forward_days`` earlier days with deferred heads, written
   as overlays (``ingestion/overlay.py``). A failure is recorded (``ok: false``) and never stops
   compose: the sealed partition serves without the overlay, its heads ``fallback_pending``;
6b. ``apply_decisions``: when the fallback auto-approved entries (the library identity on disk
   changed), D is reparsed into ``v+1`` once and its overlay rebuilt from the reply cache with no
   model call; compose then uses the new library;
7. compose the window and activate (``WritePath`` takes the write lease itself);
8. retention: only when compose succeeded (data is never deleted before its replacement serves);
9. ``retention verify``;
10. a content-free run report under ``<partitions>/reports/``, rotated at the window length.

Running ``run_daily`` again for a sealed day with the same source query and library identity
pulls nothing and composes nothing new (idempotent).
"""

from __future__ import annotations

import json
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..core.errors import ConfigError, SuError
from ..sources.contracts import SourceWindow, stable_sha
from ..sources.logs import LogMapping
from ..sources.multiline import assemble
from .accounting import AccountingPolicy
from .logs_compose import ComposeResult, compose, day_window, expiry_cutoff
from .logs_parse import parse_units
from .partitions import FilePartitionStore, PartitionManifest, UnitArtifacts

REPORTS = "reports"


class RedactionRequired(ConfigError):
    """The log mapping has no redactor: nothing would be redacted before persistence (B11)."""


class SourceCheckFailed(SuError):
    pass


@dataclass
class RunReport:
    day: str
    started_at: str
    steps: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"day": self.day, "started_at": self.started_at, "ok": self.ok, "error": self.error, "steps": self.steps}


class LogsPipeline:
    """Partitions, compose and retention of one configured log source (``config.logs``)."""

    def __init__(
        self,
        config: Any,
        *,
        source: Any,
        write_path: Any,
        redactor: Callable[[str], str] | None,
        backends: dict[str, Any] | None = None,
        store: FilePartitionStore | None = None,
        library_loader: Callable[[], Any] | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = _time.sleep,
        pins: Any = None,
        fallback: Any = None,
    ):
        if config.logs is None:
            raise ConfigError("no logs: section is configured", "logs")
        self.config = config
        self.logs = config.logs
        self.source_config = config.sources[self.logs.source]
        self.source = source
        self.write_path = write_path
        self.backends = backends or getattr(write_path, "backends", {})
        self.mapping = LogMapping(self.source_config, redactor)
        self.store = store or FilePartitionStore(self.logs.partitions.path, self.logs.source)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self._library_loader = library_loader
        self._pins = pins
        self._fallback = fallback
        from .raw_reparse import open_raw

        self.raw: Any = open_raw(config)  # None unless logs.partitions.raw.enabled

    # --- helpers -------------------------------------------------------------------------------

    def library(self) -> Any:
        """The pattern library compiled now (``CompiledLibrary``); lint errors raise."""
        if self._library_loader is not None:
            return self._library_loader()
        from ..extraction.patterns.extractor import PatternExtractor

        return PatternExtractor(self.config.extraction.pattern_library, None).load()

    def window(self, day: date):
        return day_window(day, self.logs.partitions.clock, self.logs.timezone, self.source_config.timezone)

    def date_basis(self) -> dict[str, str]:
        if self.logs.partitions.clock == "event_time":
            return {"date_basis": "event_time", "date_timezone": self.logs.timezone}
        return {"date_basis": "index_date", "date_timezone": self.source_config.timezone}

    def today(self) -> date:
        """The calendar day in ``logs.timezone`` right now."""
        return self.now().astimezone(ZoneInfo(self.logs.timezone)).date()

    def require_redaction(self) -> None:
        if self.mapping.redaction == "none":
            raise RedactionRequired(
                "the log mapping has no redactor; partitions, exemplars and the unparsed queue would hold unredacted text "
                "(configure privacy.engine: safe with redactors)",
                "privacy",
            )

    def parse(self, units: list[UnitArtifacts], library: Any, *, reuse: dict[str, Any] | None = None):
        parse = self.logs.parse
        return parse_units(
            units,
            library,
            reuse=reuse,
            workers=parse.workers,
            batch_size=parse.batch_size,
            ref=self.config.extraction.pattern_library,
            max_exemplars=parse.max_exemplars_per_signature,
            max_unparsed=parse.max_unparsed,
            policy=AccountingPolicy.from_config(self.config),
        )

    # --- steps ---------------------------------------------------------------------------------

    def self_check(self) -> list[dict[str, Any]]:
        found = [d.model_dump(mode="json") for d in self.source.self_check()]
        errors = [d for d in found if d["severity"] == "error"]
        if errors:
            raise SourceCheckFailed(f"source self-check failed: {errors[0]['message']}")
        return found

    def pull(self, day: date, library: Any) -> tuple[PartitionManifest, dict[str, Any]]:
        """Steps 3–5 for one day: discover, count, pull, map, put units, parse, seal."""
        self.require_redaction()
        key = day.isoformat()
        window = self.window(day)
        source_window = SourceWindow(partition_key=key, start=window.start, end=window.end)
        index_dates = window.index_dates if self.logs.partitions.clock == "event_time" else None
        units = self.source.discover(source_window, index_dates=index_dates) if index_dates else self.source.discover(source_window)
        query_sha = stable_sha({"units": sorted(u.query_sha for u in units), "clock": self.logs.partitions.clock, "window": [window.start.isoformat(), window.end.isoformat()]})
        sealed = self.store.get(key)
        if sealed is not None and sealed.source_query_sha == query_sha:
            latest = self.store.latest(key)
            if latest is not None and latest.status == "sealed":
                return sealed, {"status": "sealed_already", "version": sealed.version, "units": len(sealed.units)}
        manifest = self.store.begin(key, query_sha, library.provenance(), clock=self.logs.partitions.clock, tz=window.timezone, window=(window.start, window.end))
        pulled = resumed = 0
        budget = self.source.partition_budget() if hasattr(self.source, "partition_budget") else None
        for unit in units:
            if self.store.has_unit(key, unit.id):
                resumed += 1
                continue
            self.store.put_unit(key, unit.id, self._pull_unit(unit, key, budget, library))
            pulled += 1
        units_now = list(self.store.units(key))
        parsed = self.parse(units_now, library)
        redaction = {**self.mapping.redaction_report(), "mapping": self.mapping.stats.as_dict()}
        manifest = self.store.seal(key, parsed, provenance=library.provenance(), redaction=redaction)
        excluded = dict(getattr(self.source, "excluded_scope", {}) or {})
        return manifest, {"status": "sealed", "version": manifest.version, "units": len(units), "pulled": pulled, "resumed": resumed, "counts": manifest.counts, "excluded_scope": excluded}

    def _pull_unit(self, unit: Any, key: str, budget: Any, library: Any = None) -> UnitArtifacts:
        cfg = self.source_config
        count = self.source.count(unit)
        sample = self.source.plan_sample(unit)
        docs = []
        capped = False
        restarts = 0
        for batch in self.source.pull(unit, None, cfg.pull.page_size, sample, budget):
            docs.extend(batch.docs)
            capped = capped or batch.capped
            restarts = max(restarts, batch.restarts)
        if self.raw is not None:  # unredacted copy (logs.partitions.raw), written before redaction
            from .raw_reparse import write_unit

            write_unit(self.raw, key, unit, count, sample, capped, restarts, docs)
        return self._artifacts(unit, key, count, sample, docs, capped, restarts, library)

    def _artifacts(self, unit: Any, key: str, count: Any, sample: Any, docs: list[Any], capped: bool, restarts: int, library: Any) -> UnitArtifacts:
        """Redact (``LogMapping``), assemble multiline records and package one unit."""
        cfg = self.source_config
        malformed_before = self.mapping.stats.malformed
        records = self.mapping.map(docs, unit, key)
        malformed = self.mapping.stats.malformed - malformed_before
        # built-in continuation rules plus the library's approved ``continuation`` entries
        assembled, multiline = assemble(records, cfg.multiline, library=library)
        name = cfg.pull.records_query
        levels = list(cfg.counts[name].levels) if name else []
        return UnitArtifacts(
            unit=unit,
            count=count,
            sample=sample,
            records=assembled,
            fragments=multiline.fragments,
            fragment_samples=multiline.fragment_samples,
            multiline=multiline.as_dict(),
            mapping=self.mapping.stats.as_dict(),
            capped=capped,
            restarts=restarts,
            records_query=name,
            records_levels=levels,
            records_basis=count.basis.get(name) if name else None,
            docs_pulled=len(docs),
            malformed=malformed,
        )

    def fallback_session(self) -> Any:
        """The ``FallbackSession`` (injected, or built from config); ``None`` when disabled."""
        if self._fallback is not None:
            return self._fallback
        settings = self.config.learning.extraction
        if settings is None or not settings.enabled:
            return None
        from ..learning.extraction import FallbackSession

        self._fallback = FallbackSession.from_config(self.config, load=self.library)
        return self._fallback

    def fallback(self, day: date, library: Any, *, allow_calls: bool = True) -> dict[str, Any]:
        """Step 6a: the fallback over D plus earlier in-window days whose overlay deferred heads, is
        missing or is stale (re-ranked together, N5). Expired partitions' replies leave the cache."""
        session = self.fallback_session()
        if session is None:
            return {"status": "not_run", "reason": "learning.extraction is not enabled"}
        key = day.isoformat()
        oldest = expiry_cutoff(self.today(), self.logs.retention.keep_days)
        purged = session.cache.purge(oldest)
        keys = [key]
        for earlier in sorted((k for k in self.store.keys() if oldest <= k < key), reverse=True):
            if len(keys) > session.settings.carry_forward_days:
                break
            if session.status(self.store, earlier, library)["status"] in {"deferred", "missing", "stale"}:
                keys.append(earlier)
        report = session.run(self.store, keys, library, allow_calls=allow_calls, now=self.now())
        report["cache_purged"] = purged
        return report

    def apply_decisions(self, day: date, library: Any) -> tuple[Any, dict[str, Any]]:
        """Step 6b: one identity change per run. When the library on disk differs from the run's
        (entries the fallback auto-approved), reparse D into ``v+1`` and rebuild its overlay from
        the reply cache (no model call); compose uses the new library."""
        if self.fallback_session() is None:
            return library, {"status": "not_run"}
        current = self.library()
        if current.identity == library.identity:
            return library, {"status": "unchanged"}
        manifest = self.store.reparse(day.isoformat(), current, self.parse)
        rebuilt = self.fallback(day, current, allow_calls=False)
        return current, {"status": "reparsed", "version": manifest.version, "library_identity": current.identity, "fallback": rebuilt}

    def compose(self, as_of: date, library: Any) -> ComposeResult:
        return compose(self.config, self.store, self.write_path, library, as_of, reparse=self.parse, date_basis=self.date_basis())

    def retention(self, as_of: date) -> dict[str, Any]:
        from .retention import Retention

        return Retention(self.config, self.store, self.write_path, backends=self.backends, now=self.now, sleep=self.sleep, pins=self._pins).run(as_of)

    def verify(self, as_of: date) -> dict[str, Any]:
        from .retention import Retention

        return Retention(self.config, self.store, self.write_path, backends=self.backends, now=self.now, sleep=self.sleep, pins=self._pins).verify(as_of)

    # --- runs ----------------------------------------------------------------------------------

    def run_daily(self, day: date | None = None, *, retention: bool = True) -> RunReport:
        day = day or (self.today() - timedelta(days=1))
        report = RunReport(day=day.isoformat(), started_at=self.now().isoformat())
        with self.store.schedule_lock(self.logs.schedule.lock_ttl_seconds):
            try:
                self.require_redaction()
                today = self.today()
                if day.isoformat() < expiry_cutoff(today, self.logs.retention.keep_days):
                    raise ConfigError(
                        f"day {day.isoformat()} is outside the retention window ({self.logs.retention.keep_days} complete days before {today.isoformat()})",
                        "logs.retention.keep_days",
                    )
                report.steps["self_check"] = self.self_check()
                library = self.library()
                _, report.steps["partition"] = self.pull(day, library)
                report.steps["rederive"] = self.rederive(library)
                report.steps["learn"] = {"status": "not_run", "reason": "the LLM learning loop is slice L4b"}
                try:
                    report.steps["fallback"] = self.fallback(day, library)
                except Exception as exc:  # recorded, never silent: the run reports ok=false
                    fallback_error = f"{type(exc).__name__}: {exc}"
                    report.steps["fallback"] = {"status": "failed", "error": fallback_error}
                library, report.steps["apply_decisions"] = self.apply_decisions(day, library)
                # the retained window ends at the last complete day (or at ``day`` when it is later)
                composed = self.compose(max(day, today - timedelta(days=1)), library)
                report.steps["compose"] = composed.report()
            except Exception as exc:
                report.ok, report.error = False, f"{type(exc).__name__}: {exc}"
                report.steps.setdefault("retention", {"status": "skipped", "reason": "an earlier step failed; nothing is deleted before its replacement serves"})
                self._write_report(report)
                raise
            if retention:
                try:
                    report.steps["retention"] = self.retention(today)
                    report.steps["verify"] = self.verify(today)
                    report.ok = bool(report.steps["verify"]["ok"])
                except Exception as exc:
                    report.ok, report.error = False, f"{type(exc).__name__}: {exc}"
                    self._write_report(report)
                    raise
            if report.steps.get("fallback", {}).get("status") == "failed":
                report.ok, report.error = False, f"fallback: {report.steps['fallback']['error']}"
            self._write_report(report)
        return report

    def coverage(self, first: date, last: date) -> dict[str, Any]:
        """``logs coverage``: per day, the sealed (regex-only) accounting and the current overlay
        (with the model) side by side, with fragment accounting; ``ok`` is false when a day is open
        (unsealed: an imbalance refused its seal or a pull is unfinished) or an identity fails."""
        from .accounting import UnitAccounting, coverage_report
        from .overlay import OverlayStore

        session = self.fallback_session()
        library = self.library() if session is not None else None
        days: dict[str, Any] = {}
        ok = True
        current = first
        while current <= last:
            key = current.isoformat()
            current += timedelta(days=1)
            latest = self.store.latest(key)
            if latest is None:
                days[key] = {"status": "missing"}
                continue
            if latest.status != "sealed":
                ok = False
                days[key] = {"status": "open", "version": latest.version}
                continue
            units = [UnitAccounting.model_validate(u) for u in (latest.accounting.get("units") or {}).values()]
            overlay = None
            fallback = {"status": "not_run"}
            if session is not None:
                state = session.status(self.store, key, library)
                fallback = {"status": state["status"]}
                if state.get("identity"):
                    overlay = OverlayStore(self.store).read(key, latest.version, state["identity"])
            llm = list(overlay.accounting.values()) if overlay is not None else None
            report = coverage_report(units, llm, self.logs.accounting.targets) if units else {"status": "not_recorded"}
            if units and any(u.violations() for u in units + (llm or [])):
                ok = False
            days[key] = {"status": "sealed", "version": latest.version, "fallback": fallback, **report}
        return {"ok": ok, "days": days}

    def backfill(self, first: date, last: date) -> RunReport:
        """Pull and seal every day from ``first`` to ``last`` (oldest first), then compose once."""
        report = RunReport(day=last.isoformat(), started_at=self.now().isoformat())
        with self.store.schedule_lock(self.logs.schedule.lock_ttl_seconds):
            self.require_redaction()
            report.steps["self_check"] = self.self_check()
            library = self.library()
            days = {}
            current = first
            while current <= last:
                _, days[current.isoformat()] = self.pull(current, library)
                current += timedelta(days=1)
            report.steps["partitions"] = days
            report.steps["compose"] = self.compose(last, library).report()
            self._write_report(report, name=f"backfill-{last.isoformat()}")
        return report

    def settle(self, day: date) -> dict[str, Any]:
        """Recount the units of a sealed day; units whose count grew by more than
        ``settle.max_growth`` are pulled again into ``v+1`` (``repull``), then the day is resealed."""
        with self.store.schedule_lock(self.logs.schedule.lock_ttl_seconds):
            self.require_redaction()
            key = day.isoformat()
            sealed = self.store.get(key)
            if sealed is None:
                raise SuError(f"partition {key} is not sealed; run run-daily first")
            window = self.window(day)
            source_window = SourceWindow(partition_key=key, start=window.start, end=window.end)
            index_dates = window.index_dates if self.logs.partitions.clock == "event_time" else None
            units = {u.id: u for u in (self.source.discover(source_window, index_dates=index_dates) if index_dates else self.source.discover(source_window))}
            grown = []
            for unit_id in sealed.units:
                unit = units.get(unit_id)
                if unit is None:
                    continue
                before = sealed.unit_counts[unit_id]["total"]
                after = self.source.count(unit).total
                if after > before * (1 + self.logs.settle.max_growth):
                    grown.append(unit_id)
            if not grown:
                return {"status": "settled", "repulled": []}
            library = self.library()
            self.store.repull(key, grown)
            budget = self.source.partition_budget() if hasattr(self.source, "partition_budget") else None
            for unit_id in grown:
                self.store.put_unit(key, unit_id, self._pull_unit(units[unit_id], key, budget, library))
            manifest = self.store.seal(key, self.parse(list(self.store.units(key)), library), provenance=library.provenance(), redaction=self.mapping.redaction_report())
            return {"status": "repulled", "repulled": grown, "version": manifest.version}

    def rederive(self, library: Any) -> dict[str, Any]:
        """Re-derive days whose redaction changed, from raw, where the raw tier still holds them."""
        from .raw_reparse import rederive

        return rederive(self, library)

    def reparse(self, day: date, *, from_raw: bool = False) -> dict[str, Any]:
        """``logs reparse``: a new sealed version of day ``day``. Without ``from_raw`` the retained
        redacted records are parsed again under the current library. With ``from_raw`` redaction,
        multiline assembly and parsing re-run from the raw copies (``logs.partitions.raw``), so a
        changed redaction applies without the source; a missing or incomplete raw copy is a typed
        ``RawUnavailable`` (pull the units again with ``logs settle``)."""
        key = day.isoformat()
        with self.store.schedule_lock(self.logs.schedule.lock_ttl_seconds):
            sealed = self.store.get(key)
            if sealed is None:
                raise SuError(f"partition {key} is not sealed; run run-daily first")
            library = self.library()
            if not from_raw:
                manifest = self.store.reparse(key, library, self.parse)
                return {"status": "reparsed", "version": manifest.version, "from_raw": False}
            from .raw_reparse import reparse_from_raw

            return reparse_from_raw(self, key, sealed, library)

    @classmethod
    def from_engine(cls, engine: Any, *, redactor: Callable[[str], str] | None = None, source: Any = None, **kwargs: Any) -> "LogsPipeline":
        """The pipeline over a constructed ``Engine`` (its write path and backends)."""
        config = engine.config
        if redactor is None:
            from ..privacy import safe_redactor

            redactor = safe_redactor(config.privacy)  # None under privacy.engine: legacy (refused at run time)
        if source is None:
            from ..sources import build_source

            source = build_source(config.logs.source, config.sources[config.logs.source])
        return cls(config, source=source, write_path=engine.write_path, redactor=redactor, backends=engine.backends, **kwargs)

    # --- reports -------------------------------------------------------------------------------

    def reports_dir(self) -> Path:
        return self.logs.partitions.path / REPORTS / self.logs.source

    def _write_report(self, report: RunReport, name: str | None = None) -> None:
        """Content-free by invariant (counts, ids, hashes, statuses); rotated at the window length."""
        folder = self.reports_dir()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name or 'run-' + report.day}.json").write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True, default=str))
        oldest = expiry_cutoff(self.today(), self.logs.retention.keep_days)
        for path in folder.glob("*.json"):
            stamp = path.stem.rsplit("-", 3)
            day = "-".join(stamp[-3:]) if len(stamp) >= 3 else ""
            if day and day < oldest:
                path.unlink()


def logs_cli(args: Any, config: Any) -> dict[str, Any]:
    """``su-multi-arm logs run-daily|compose|backfill|settle`` and ``retention report|verify|run``.

    The pulling commands (``run-daily``, ``backfill``, ``settle``) redact with the configured safe
    redactor (``privacy.engine: safe``); without one they refuse with ``RedactionRequired`` before
    any request. ``compose`` and ``retention`` read sealed partitions only."""
    from ..engine import Engine

    if config.logs is None:
        raise ConfigError("no logs: section is configured", "logs")
    engine = Engine(config)
    pipe = LogsPipeline.from_engine(engine)
    day = date.fromisoformat(args.date) if getattr(args, "date", None) else None
    if args.command == "logs":
        if args.logs_command == "run-daily":
            return pipe.run_daily(day).as_dict()
        if args.logs_command == "backfill":
            if day is None or not args.date_from:
                raise ConfigError("logs backfill needs --from and --date", "logs")
            return pipe.backfill(date.fromisoformat(args.date_from), day).as_dict()
        if day is None:
            raise ConfigError(f"logs {args.logs_command} needs --date", "logs")
        if args.logs_command == "settle":
            return pipe.settle(day)
        if args.logs_command == "coverage":
            first = date.fromisoformat(args.date_from) if args.date_from else day
            return pipe.coverage(first, day)
        if args.logs_command == "reparse":
            return pipe.reparse(day, from_raw=bool(getattr(args, "from_raw", False)))
        with pipe.store.schedule_lock(pipe.logs.schedule.lock_ttl_seconds):
            return pipe.compose(day, pipe.library()).report()
    from .retention import Retention

    retention = Retention(config, pipe.store, pipe.write_path, backends=engine.backends, now=pipe.now)
    as_of = day or pipe.today()
    if args.retention_command == "report":
        return retention.report(as_of)
    if args.retention_command == "verify":
        return retention.verify(as_of)
    with pipe.store.schedule_lock(pipe.logs.schedule.lock_ttl_seconds):
        return {"retention": retention.run(as_of), "verify": retention.verify(as_of)}
