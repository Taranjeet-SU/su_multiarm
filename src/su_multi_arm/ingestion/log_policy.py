"""The declared log collection policy at run time (``logs.collection``; 2026-11-08 standard).

* ``load_keyword_recipe``: the keyword families in force, read from the pinned files (a file whose
  sha256 differs from its pin is refused) and their content identity (the *recipe*).
* Schedule rules: ``heavy_window_open`` (heavy pulls only inside ``heavy_window_utc``),
  ``day_complete`` (a UTC day closes ``day_close_delay_minutes`` after its end), ``planned_jobs``
  and ``emit_systemd`` (timer/service unit text derived from the policy; nothing is installed).
* ``policy_report``: the effective policy, the recipe and file hashes, retention per data class,
  the archive state and the emitted schedule.
* ``verify_policy``: a deployment's state against the policy (closed days archived or explicitly
  gapped, no archived day deleted, generations within ``keep_generations``, raw/masked/staging
  ages within bounds, recipe pins intact, the serving generation referencing every archived day).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError

from ..core.config_log_policy import CollectionPolicyConfig, archive_path, minutes
from ..core.errors import ConfigError
from ..models import StrictModel
from .episode_summaries import CONTRACT, SummaryRule, check_precedence, identity, is_success


class KeywordFamily(StrictModel):
    """One keyword-family file: literal phrases (the keyword union) and detection rules."""

    family: str = Field(pattern=r"^[a-z][a-z0-9_.\-]{0,63}$")
    phrases: list[str] = Field(min_length=1, max_length=512)
    rules: list[SummaryRule] = Field(default_factory=list, max_length=32)


@dataclass
class KeywordRecipe:
    families: list[str]
    phrases: list[str]
    rules: list[SummaryRule]
    files: list[dict[str, str]]
    gap_seconds: int
    identity: str
    effective_from: str | None = None
    problems: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "families": self.families,
            "phrases": len(self.phrases),
            "rules": [r.id for r in self.rules],
            "files": self.files,
            "gap_seconds": self.gap_seconds,
            "effective_from": self.effective_from,
        }


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_keyword_recipe(config: Any) -> KeywordRecipe:
    """The keyword recipe of ``logs.collection.keywords`` (``ConfigError`` naming the file on an
    unreadable, invalid or re-hashed file; a changed file is never used under its old pin)."""
    import yaml

    policy = config.logs.collection.keywords
    if not policy.files:
        raise ConfigError("logs.collection.keywords.files is empty: no keyword recipe is declared", "logs.collection.keywords.files")
    families, phrases, rules, files = [], [], [], []
    for i, item in enumerate(policy.files):
        where = f"logs.collection.keywords.files.{i}"
        try:
            data = item.path.read_bytes()
        except OSError as exc:
            raise ConfigError(f"cannot read keyword file {item.path.name}: {exc.strerror}", where) from exc
        actual = hashlib.sha256(data).hexdigest()
        if actual != item.sha256:
            raise ConfigError(
                f"keyword file {item.path.name} has sha256 {actual}, the policy pins {item.sha256}: a changed keyword list needs a new pin "
                "(and keywords.effective_from once days are archived); it is never mixed into the old recipe",
                f"{where}.sha256",
            )
        try:
            family = KeywordFamily.model_validate(yaml.safe_load(data.decode("utf-8")) or {})
        except (ValidationError, yaml.YAMLError, UnicodeDecodeError) as exc:
            raise ConfigError(f"invalid keyword file {item.path.name}: {exc}", where) from exc
        families.append(family.family)
        phrases.extend(family.phrases)
        rules.extend(family.rules)
        files.append({"file": item.path.name, "family": family.family, "sha256": actual})
    if len(set(families)) != len(families):
        raise ConfigError("two keyword files declare the same family", "logs.collection.keywords.files")
    if len(set(phrases)) != len(phrases):
        raise ConfigError("a keyword phrase is listed twice across the keyword families", "logs.collection.keywords.files")
    if not rules or len({r.id for r in rules}) != len(rules) or len(rules) > 32:
        raise ConfigError("the keyword families need 1-32 rules with unique ids", "logs.collection.keywords.files")
    try:
        check_precedence(rules)  # success heartbeats (O6) after every failure rule
    except ValueError as exc:
        raise ConfigError(str(exc), "logs.collection.keywords.files") from exc
    category = config.metadata_schema.field("category")
    if any(is_success(r) for r in rules) and (category is None or not category.filterable):
        raise ConfigError("success heartbeat rules need metadata_schema field 'category' (filterable): failure views exclude category success by it", "metadata_schema.fields")
    recipe_identity = identity({"contract": CONTRACT, "phrases": phrases, "rules": [r.model_dump() for r in rules], "gap_seconds": policy.gap_seconds})
    return KeywordRecipe(
        families=families,
        phrases=phrases,
        rules=rules,
        files=files,
        gap_seconds=policy.gap_seconds,
        identity=recipe_identity,
        effective_from=policy.effective_from.isoformat() if policy.effective_from else None,
    )


# --- schedule ----------------------------------------------------------------------------------


def _utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("an aware datetime is required")
    return now.astimezone(timezone.utc)


def heavy_window_open(policy: CollectionPolicyConfig, now: datetime) -> bool:
    window = policy.schedule.heavy_window_utc
    if window is None:
        return True
    t = _utc(now)
    current = t.hour * 60 + t.minute
    return minutes(window.start) <= current < minutes(window.end)


def day_complete(policy: CollectionPolicyConfig, day: date, now: datetime) -> bool:
    """A UTC day is complete ``day_close_delay_minutes`` after its end (late arrivals settle)."""
    end = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1)
    return _utc(now) >= end + timedelta(minutes=policy.schedule.day_close_delay_minutes)


def last_complete_day(policy: CollectionPolicyConfig, now: datetime) -> date:
    t = _utc(now) - timedelta(minutes=policy.schedule.day_close_delay_minutes)
    return t.date() - timedelta(days=1)


def _hhmm(total: int) -> str:
    return f"{total // 60:02d}:{total % 60:02d}"


def planned_jobs(config: Any, config_path: str = "CONFIG.yaml") -> list[dict[str, Any]]:
    """The jobs the policy implies. ``refresh``: the deployment's summary collector, every
    ``summary_refresh_minutes`` (light: today's per-index summaries). ``daily``: the heavy pass,
    started at the later of the window start and the day-close delay, bounded to end inside the
    window (and before midnight UTC), so it never crosses a day boundary."""
    logs = config.logs
    policy = logs.collection
    schedule = policy.schedule
    window = schedule.heavy_window_utc
    start = max(minutes(window.start) if window else 0, schedule.day_close_delay_minutes)
    end = minutes(window.end) if window else 24 * 60
    base = f"su-multi-arm --config {config_path}"
    refresh = ["@COLLECTOR_REFRESH_COMMAND@"]
    if policy.provisional.enabled:
        refresh.append(f"{base} logs policy refresh")
    if policy.archive.enabled:
        commands = [f"{base} logs policy import", f"{base} retention run", f"{base} logs policy verify"]
    else:
        commands = [f"{base} logs run-daily", f"{base} logs policy verify"]
    return [
        {
            "name": "refresh",
            "class": "light",
            "every_minutes": schedule.summary_refresh_minutes,
            "commands": refresh,
            "note": "the deployment's summary collector (rolling per-index summaries of every unclosed day into staging); not part of the package"
            + ("; then the provisional refresh (light, no generation rebuild)" if policy.provisional.enabled else ""),
        },
        {
            "name": "daily",
            "class": "heavy",
            "start_utc": _hhmm(start),
            "end_utc": _hhmm(end),
            "max_runtime_seconds": (end - start) * 60,
            "commands": commands,
            "note": f"closes due UTC days in order at D+{schedule.close_after_days} (complete, or partial after {schedule.grace_days} grace days), one at a time (never across a day boundary), then retention and verification",
        },
    ]


def emit_systemd(config: Any, config_path: str = "CONFIG.yaml", prefix: str = "su-multi-arm-logs") -> str:
    """Unit text (two oneshot services and their timers) derived from the policy. Printed only."""
    out = []
    for job in planned_jobs(config, config_path):
        name = f"{prefix}-{job['name']}"
        service = ["[Unit]", f"Description={name} ({job['class']}; derived from logs.collection)", "", "[Service]", "Type=oneshot"]
        service += [f"ExecStart={command}" for command in job["commands"]]
        if job["class"] == "heavy":
            service.append(f"RuntimeMaxSec={job['max_runtime_seconds']}")
        timer = ["[Unit]", f"Description={name} timer", "", "[Timer]"]
        if job["class"] == "heavy":
            timer += [f"OnCalendar=*-*-* {job['start_utc']}:00 UTC", "Persistent=false", "AccuracySec=1min"]
        else:
            timer += ["OnBootSec=2min", f"OnUnitActiveSec={job['every_minutes']}min", "AccuracySec=30s"]
        timer += ["", "[Install]", "WantedBy=timers.target"]
        out.append(f"# --- {name}.service ---\n" + "\n".join(service) + "\n")
        out.append(f"# --- {name}.timer ---\n" + "\n".join(timer) + "\n")
    return "\n".join(out)


# --- report ------------------------------------------------------------------------------------


def retention_classes(config: Any) -> dict[str, Any]:
    logs = config.logs
    retention, policy = logs.retention, logs.collection
    return {
        "raw": {"keep_days": retention.raw_days, "enabled": logs.partitions.raw.enabled},
        "masked_partitions": {"keep_days": retention.keep_days},
        "serving_generations": {"keep": retention.keep_generations},
        "derived": {"scope": retention.derived},
        "keyword_episode_aggregates": {"scope": "forever" if policy.archive.enabled else retention.derived, "archive": policy.archive.enabled},
        "live_window_documents": {"keep_days": retention.keep_days, "note": "every summary document as built (untrimmed); unclassified_example exists only here"},
        "staging": {"path_set": policy.staging.path is not None, "delete_after_verified_import": policy.staging.delete_after_import},
    }


def policy_report(config: Any, now: datetime | None = None, config_path: str = "CONFIG.yaml") -> dict[str, Any]:
    if config.logs is None:
        raise ConfigError("no logs: section is configured", "logs")
    now = now or datetime.now(timezone.utc)
    logs = config.logs
    policy = logs.collection
    report: dict[str, Any] = {
        "policy": policy.model_dump(mode="json", exclude={"keywords": {"files"}, "archive": {"path"}, "staging": {"path"}}),
        "source_rate_limit": _source_limit(config),
        "retention": retention_classes(config),
        "schedule": {"now": _utc(now).isoformat(), "heavy_window_open": heavy_window_open(policy, now), "last_complete_day": last_complete_day(policy, now).isoformat(), "jobs": planned_jobs(config, config_path)},
    }
    pins = []
    for item in policy.keywords.files:
        try:
            actual = file_sha256(item.path)
        except OSError as exc:
            actual = f"unreadable: {exc.strerror}"
        pins.append({"file": item.path.name, "pinned": item.sha256, "actual": actual, "ok": actual == item.sha256})
    report["keyword_files"] = pins
    if policy.keywords.files and all(p["ok"] for p in pins):
        try:
            report["recipe"] = load_keyword_recipe(config).summary()
        except ConfigError as exc:
            report["recipe"] = {"error": str(exc)}
    if policy.archive.enabled:
        from .episode_archive import open_archive

        store = open_archive(config)
        days, gaps = store.days(), store.gaps()
        recipes: dict[str, dict[str, Any]] = {}
        for d, row in days.items():
            entry = recipes.setdefault(row["recipe"], {"first": d, "last": d, "days": 0})
            entry["last"], entry["days"] = d, entry["days"] + 1
        partial = sorted(d for d, row in days.items() if row.get("closure") == "partial")
        report["archive"] = {"days": len(days), "first": min(days) if days else None, "last": max(days) if days else None, "gaps": gaps, "recipes": recipes, "rows": store.row_count(), "partial_days": partial, "history_days": len(store.history_days())}
    if policy.provisional.enabled:
        from .provisional import open_provisional

        current = open_provisional(config).current()
        report["provisional"] = {d: {k: e.get(k) for k in ("as_of", "scopes", "scopes_expected", "missing_scopes", "rolling_scopes")} for d, e in sorted(current.items())}
    return report


def _source_limit(config: Any) -> dict[str, Any] | None:
    source = config.sources.get(config.logs.source)
    if source is None:
        return None
    return {"source": config.logs.source, "requests_per_second": source.limits.requests_per_second, "key": f"sources.{config.logs.source}.limits.requests_per_second"}


# --- verify ------------------------------------------------------------------------------------


def _days(first: date, last: date) -> list[str]:
    out, current = [], first
    while current <= last:
        out.append(current.isoformat())
        current += timedelta(days=1)
    return out


def _is_day(text: str) -> bool:
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def verify_policy(config: Any, *, registry: Any, now: datetime | None = None, archive: Any = None, store: Any = None, raw: Any = None) -> dict[str, Any]:
    """Check a deployment's state against ``logs.collection`` and ``logs.retention``."""
    from .logs_compose import expiry_cutoff

    if config.logs is None:
        raise ConfigError("no logs: section is configured", "logs")
    now = now or datetime.now(timezone.utc)
    logs = config.logs
    policy, retention = logs.collection, logs.retention
    today = _utc(now).date()
    problems: list[str] = []
    checks: dict[str, Any] = {}
    # 1. keyword recipe pins
    recipe = None
    if policy.keywords.files:
        try:
            recipe = load_keyword_recipe(config)
            checks["recipe"] = recipe.identity
        except ConfigError as exc:
            problems.append(f"keyword recipe: {exc}")
    # 2. generations within keep_generations
    generations = []
    for generation in registry.generations():
        manifest = registry.manifest(generation) or {}
        if (manifest.get("logs") or {}).get("source") == logs.source:
            generations.append(generation)
    checks["generations"] = len(generations)
    if len(generations) > retention.keep_generations:
        problems.append(f"{len(generations)} log generations exceed retention.keep_generations ({retention.keep_generations})")
    # 3. masked partitions and raw copies within their ages
    masked_cut = expiry_cutoff(today, retention.keep_days)
    if store is not None:
        stale = [k for k in store.keys() if k < masked_cut]
        checks["masked_days"] = len(store.keys())
        if stale:
            problems.append(f"masked partitions {stale} are older than keep_days ({retention.keep_days}; cutoff {masked_cut})")
    if raw is not None:
        raw_cut = expiry_cutoff(today, retention.raw_days)
        stale_raw = [k for k in raw.keys() if k < raw_cut]
        checks["raw_days"] = len(raw.keys())
        if stale_raw:
            problems.append(f"raw copies {stale_raw} are older than raw_days ({retention.raw_days}; cutoff {raw_cut})")
    # 4. archive: every closed day archived or gapped, nothing deleted, recipe epochs not mixed
    if policy.archive.enabled:
        from .episode_archive import EpisodeArchiveStore

        archive = archive if archive is not None else EpisodeArchiveStore(archive_path(policy, logs))
        days, gaps = archive.days(), archive.gaps()
        problems.extend(archive.check_chain())
        known = sorted(set(days) | set(gaps))
        last = last_complete_day(policy, now)
        if known:
            missing = [d for d in _days(date.fromisoformat(known[0]), last) if d not in days and d not in gaps]
            if missing:
                problems.append(f"closed days without a rollup or a gap record: {missing[:10]}{' ...' if len(missing) > 10 else ''} ({len(missing)} days)")
        checks["archive"] = {"days": len(days), "gaps": len(gaps), "first": known[0] if known else None, "last_complete_day": last.isoformat()}
        if recipe is not None:
            floor = policy.keywords.effective_from.isoformat() if policy.keywords.effective_from else ""
            mixed = sorted(d for d, row in days.items() if d >= floor and row["recipe"] != recipe.identity)
            if mixed:
                problems.append(f"archived days {mixed[:5]} were captured with another keyword recipe than the pinned one (set keywords.effective_from after them for a new epoch)")
        stale_bounded = [d for d in archive.bounded_days() if d < masked_cut]
        if stale_bounded:
            problems.append(f"live-window rows of days {stale_bounded} are older than keep_days")
        staged = policy.staging.path
        if staged is not None and staged.exists():
            leftover = sorted(p.name for p in staged.iterdir() if p.is_dir() and p.name in days)
            if leftover and policy.staging.delete_after_import:
                problems.append(f"staging copies of archived days {leftover} were not deleted")
        unbuilt = sorted(set(days) - archive.history_days())
        if unbuilt:
            problems.append(f"archived days {unbuilt[:5]} ({len(unbuilt)}) have no history tables (issue_history refuses them); run logs policy import")
        overdue_after = policy.schedule.close_after_days + policy.schedule.grace_days
        if policy.provisional.enabled:
            from pathlib import Path as _Path

            from .provisional import open_provisional

            provisional = open_provisional(config)
            current = provisional.current()
            lost = sorted(d for d, e in current.items() if not _Path(e["file"]).exists())
            if lost:
                problems.append(f"provisional days {lost} name projection files that do not exist (served as unavailable); run logs policy refresh")
            unclosed = sorted(set(provisional.days()) - set(days) - set(gaps))
            checks["provisional"] = {"days": len(current), "unclosed": len(unclosed)}
        else:
            unclosed = []
        if staged is not None and staged.exists():
            unclosed = sorted(set(unclosed) | {p.name for p in staged.iterdir() if p.is_dir() and p.name not in days and p.name not in gaps})
        overdue = [d for d in unclosed if _is_day(d) and date.fromisoformat(d) + timedelta(days=overdue_after) < today]
        if overdue:
            problems.append(f"days {overdue[:5]} are past close_after_days + grace_days and neither archived (complete or partial) nor gapped; run logs policy import")
        active = registry.current()
        view = archive.served_view()
        if view is not None:  # I1: the archive serving pointer serves the archived days
            unserved = sorted(d for d, row in days.items() if view["shards"].get(d) != row["shard_id"])
            if unserved:
                problems.append(f"the archive serving pointer does not serve archived days {unserved[:5]} ({len(unserved)}); run logs policy import")
        elif active is not None and active["generation"] in generations:
            served = (((registry.manifest(active["generation"]) or {}).get("logs") or {}).get("history") or {}).get("shards") or {}
            unserved = sorted(d for d, row in days.items() if served.get(d) != row["shard_id"])
            if unserved:
                problems.append(f"the active generation does not serve archived days {unserved[:5]} ({len(unserved)}); run logs policy import")
    return {"ok": not problems, "as_of": today.isoformat(), "problems": problems, "checks": checks}


# --- CLI ---------------------------------------------------------------------------------------


def policy_cli(args: Any, config: Any) -> tuple[dict[str, Any] | str, bool]:
    """``logs policy report|verify|import|refresh|record-gap|gate``: (output, ok)."""
    if config.logs is None:
        raise ConfigError("no logs: section is configured", "logs")
    command = getattr(args, "policy_command", None) or "report"
    config_path = str(getattr(args, "config", "CONFIG.yaml"))
    if command == "report":
        if getattr(args, "emit", None) == "systemd":
            return emit_systemd(config, config_path), True
        return policy_report(config, config_path=config_path), True
    if command == "gate":
        now = datetime.now(timezone.utc)
        open_ = heavy_window_open(config.logs.collection, now)
        return {"heavy_window_open": open_, "now": now.isoformat()}, open_
    if command == "refresh":
        from .episode_archive import EpisodeArchive

        return EpisodeArchive(config).refresh_provisional(), True
    from ..engine import Engine

    engine = Engine(config)
    if command == "verify":
        from .partitions import FilePartitionStore
        from .raw_reparse import open_raw

        result = verify_policy(config, registry=engine.write_path.registry, store=FilePartitionStore(config.logs.partitions.path, config.logs.source), raw=open_raw(config))
        return result, bool(result["ok"])
    from .episode_archive import EpisodeArchive

    archive = EpisodeArchive(config)
    if command == "record-gap":
        if not getattr(args, "date", None) or not getattr(args, "reason", None):
            raise ConfigError("logs policy record-gap needs --date and --reason", "logs")
        return archive.record_gap(args.date, args.reason), True
    # import: close staged days, then prune the live window and publish the serving generation
    report = archive.import_staging()
    if report["status"] == "outside_heavy_window":
        return report, True
    report["publish"] = archive.publish(engine.write_path, datetime.now(timezone.utc).date())
    return report, True
