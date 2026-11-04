"""Daily log operation (addendum A1 §3.8.1; slices L3a and L3b): ``run_daily``, ``backfill``, ``settle``.

``run_daily(D)`` holds **only the schedule lock** (the partition store's lease; never the registry
write lease) and runs, each step resumable:

1. refuse a mapping without a redactor (nothing unredacted is ever persisted);
2. source self-check (an ``error`` diagnostic stops the run);
3. ``discover`` the units of D (the partition clock decides which index dates and slices);
4. per unit not yet stored: ``count``, stratified ``pull``, map, multiline, ``put_unit`` (atomic,
   so a crash resumes at the next unit without duplicates);
5. parse (process pool when ``logs.parse.workers > 1``) and ``seal``;
6. ``learn patterns run``: not in this slice (L4b), reported as ``not_run``;
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
from .logs_compose import ComposeResult, compose, cutoff, day_window
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
                "(the safe redaction contract, slice B11, supplies one)",
                "privacy",
            )

    def parse(self, units: list[UnitArtifacts], library: Any):
        parse = self.logs.parse
        return parse_units(
            units,
            library,
            workers=parse.workers,
            batch_size=parse.batch_size,
            ref=self.config.extraction.pattern_library,
            max_exemplars=parse.max_exemplars_per_signature,
            max_unparsed=parse.max_unparsed,
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
            self.store.put_unit(key, unit.id, self._pull_unit(unit, key, budget))
            pulled += 1
        units_now = list(self.store.units(key))
        parsed = self.parse(units_now, library)
        redaction = {"mode": self.mapping.redaction, "mapping": self.mapping.stats.as_dict()}
        manifest = self.store.seal(key, parsed, provenance=library.provenance(), redaction=redaction)
        return manifest, {"status": "sealed", "version": manifest.version, "units": len(units), "pulled": pulled, "resumed": resumed, "counts": manifest.counts}

    def _pull_unit(self, unit: Any, key: str, budget: Any) -> UnitArtifacts:
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
        records = self.mapping.map(docs, unit, key)
        assembled, multiline = assemble(records, cfg.multiline)
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
        )

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
                if day.isoformat() < cutoff(self.today(), self.logs.retention.keep_days):
                    raise ConfigError(f"day {day.isoformat()} is outside the retention window ({self.logs.retention.keep_days} days)", "logs.retention.keep_days")
                report.steps["self_check"] = self.self_check()
                library = self.library()
                _, report.steps["partition"] = self.pull(day, library)
                report.steps["learn"] = {"status": "not_run", "reason": "the LLM learning loop is slice L4b"}
                composed = self.compose(day, library)
                report.steps["compose"] = composed.report()
            except Exception as exc:
                report.ok, report.error = False, f"{type(exc).__name__}: {exc}"
                report.steps.setdefault("retention", {"status": "skipped", "reason": "an earlier step failed; nothing is deleted before its replacement serves"})
                self._write_report(report)
                raise
            if retention:
                try:
                    report.steps["retention"] = self.retention(day)
                    report.steps["verify"] = self.verify(day)
                    report.ok = bool(report.steps["verify"]["ok"])
                except Exception as exc:
                    report.ok, report.error = False, f"{type(exc).__name__}: {exc}"
                    self._write_report(report)
                    raise
            self._write_report(report)
        return report

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
                self.store.put_unit(key, unit_id, self._pull_unit(units[unit_id], key, budget))
            manifest = self.store.seal(key, self.parse(list(self.store.units(key)), library), provenance=library.provenance(), redaction={"mode": self.mapping.redaction})
            return {"status": "repulled", "repulled": grown, "version": manifest.version}

    @classmethod
    def from_engine(cls, engine: Any, *, redactor: Callable[[str], str] | None = None, source: Any = None, **kwargs: Any) -> "LogsPipeline":
        """The pipeline over a constructed ``Engine`` (its write path and backends)."""
        config = engine.config
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
        oldest = cutoff(date.fromisoformat(report.day), self.logs.retention.keep_days)
        for path in folder.glob("*.json"):
            stamp = path.stem.rsplit("-", 3)
            day = "-".join(stamp[-3:]) if len(stamp) >= 3 else ""
            if day and day < oldest:
                path.unlink()


def logs_cli(args: Any, config: Any) -> dict[str, Any]:
    """``su-multi-arm logs run-daily|compose|backfill|settle`` and ``retention report|verify|run``.

    The CLI has no redactor until the safe redaction contract (slice B11) lands, so every command
    that pulls from the source (``run-daily``, ``backfill``, ``settle``) refuses with
    ``RedactionRequired`` before any request; ``compose`` and ``retention`` read sealed partitions
    only."""
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
