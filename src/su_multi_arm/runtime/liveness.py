"""``log_liveness`` (O6, 2026-11-09): "has X halted", "when did X last complete", "did it recover".

Read from the **success heartbeats** of the episode archive and the provisional days: the episode
pieces of rules with ``category: success`` (``ingestion/episode_summaries``). A success *run* is a
set of successes separated by at most ``keywords.gap_seconds`` (pieces continued across midnight
are one run). Per tenant x env x service x success rule:

* **last success**: the end of the latest run;
* **observed until**: the latest captured instant of that source scope (``coverage_day``
  ``window_end``), never later than "now"; the quiet time is measured up to it, so a collection
  that is behind never reads as a halt;
* **cadence**: the median interval between the starts of consecutive runs over the lookback
  (``logs.collection.liveness.cadence_days``), from at least ``min_intervals`` intervals; an
  interval that spans a day without heartbeat data (gap, missing, or captured before heartbeat
  recording) is not used. Runs that never fall quiet for longer than ``gap_seconds`` are
  ``continuous`` (cadence = ``gap_seconds``); otherwise too few intervals leave it ``unknown``;
* **status**: ``halted`` when the quiet time exceeds ``halt_factor`` x the cadence, ``running``
  when it does not, ``cadence_unknown``, or ``no_heartbeats`` (a named client/service with captured
  data but no success heartbeat of the asked kinds in the lookback).

With ``recovery``: the failure episodes (every other category) of the lookback and, per episode,
the first success heartbeat of the same client, env and service after its last failure
(``recovered_at``, time to recovery from the first failure). That is a temporal sequence in one
scope, not proof that the failed operation itself recovered; it is stated.

Heartbeat recording has a start: archived days captured with another keyword recipe than the pinned
one, and days before ``keywords.effective_from``, carry no success heartbeat; they are listed and
their silence is never read as a halt. A recipe without success rules is refused. Operator-only.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..core.errors import ConfigError
from ..decision.log_intents import LivenessInput
from ..ingestion.episode_history import DayTier, HistorySpec, HistoryUnavailable, TieredAggregate, day_recipes, tier_map
from ..ingestion.episode_summaries import SUCCESS, is_success, timestamp

STATUS_ORDER = {"halted": 0, "cadence_unknown": 1, "no_heartbeats": 2, "running": 3}
COLUMNS = ["tenant", "env", "service", "event", "last_success", "observed_until", "quiet", "cadence", "cadence_basis", "intervals", "status"]
RECOVERY_COLUMNS = ["tenant", "env", "service", "issue", "first_failure", "last_failure", "status", "recovered_at", "recovered_by", "time_to_recovery"]


def duration(seconds: float | None) -> str | None:
    """``93541`` -> ``"1d 1h 59m"``; under a minute ``"45s"``."""
    if seconds is None:
        return None
    total = int(seconds)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if not (days or hours or minutes):
        return f"{secs}s"
    return " ".join(f"{n}{unit}" for n, unit in ((days, "d"), (hours, "h"), (minutes, "m")) if n) or "0m"


def matches(rule_id: str, event: str) -> bool:
    """``event`` names the rule: its id, or its event part (after the last dot)."""
    part = rule_id.rsplit(".", 1)[-1]
    return event.casefold() in {rule_id.casefold(), part.casefold()}


def runs(pieces: list[dict[str, Any]], gap_seconds: int) -> list[dict[str, Any]]:
    """Pieces (one series, ``first_failure`` order) merged into runs: a piece starting at most
    ``gap_seconds`` after the run's last success continues it."""
    out: list[dict[str, Any]] = []
    for p in pieces:
        first, last = timestamp(p["first_failure"]), timestamp(p["last_failure"])
        if out and (first - out[-1]["last"]).total_seconds() <= gap_seconds:
            run = out[-1]
            run["last"] = max(run["last"], last)
            run["occurrences"] += int(p["occurrences"])
            run["pieces"].append(p)
            continue
        out.append({"first": first, "last": last, "occurrences": int(p["occurrences"]), "pieces": [p], "signature_id": p["signature_id"], "issue": p.get("issue")})
    return out


def days_between(a: datetime, b: datetime) -> set[str]:
    day, out = a.date(), set()
    while day <= b.date():
        out.add(day.isoformat())
        day += timedelta(days=1)
    return out


def cadence(series: list[dict[str, Any]], unusable: set[str], min_intervals: int, gap_seconds: int) -> tuple[float | None, str, int]:
    """(cadence seconds, basis, intervals used) of one series' runs (module docstring)."""
    intervals = [
        (b["first"] - a["first"]).total_seconds()
        for a, b in zip(series, series[1:])
        if not days_between(a["last"], b["first"]) & unusable
    ]
    if len(intervals) >= min_intervals:
        return float(statistics.median(intervals)), "median_interval", len(intervals)
    if not intervals and max((r["last"] - r["first"]).total_seconds() for r in series) >= min_intervals * gap_seconds:
        return float(gap_seconds), "continuous", 0
    return None, "insufficient_intervals", len(intervals)


def iso(t: datetime | None) -> str | None:
    return t.isoformat() if t is not None else None


def run_liveness(ops: Any, ctx: Any, call: Any) -> Any:
    """``log_liveness`` (module doc). On a partitioned collection the history tables are the live
    log member's, built per request (``runtime/member_logs.MemberHistory``)."""
    from .member_logs import MEMBER_STORE, MemberHistory

    if ((ops.log_manifest(ctx).get("logs") or {}).get("history") or {}).get("store") == MEMBER_STORE and not list(ctx.constraints()):
        with MemberHistory(ops, ctx) as member:
            return _liveness(ops, ctx, call, member)
    return _liveness(ops, ctx, call, None)


def _liveness(ops: Any, ctx: Any, call: Any, member: Any) -> Any:
    from ..ingestion.log_policy import load_keyword_recipe
    from .member_logs import MEMBER_STORE
    from .episode_tools import max_window_days
    from .issue_history import provisional_ledger
    from .log_tools import LogToolOutput

    args = LivenessInput.model_validate(call.args)
    config = ops.config
    collection = config.logs.collection
    settings = collection.liveness
    gap_seconds = collection.keywords.gap_seconds
    manifest = ops.log_manifest(ctx)
    section = manifest.get("logs") or {}

    def refused(reason: str, window: dict[str, str] | None = None) -> Any:
        return LogToolOutput(intent=call.intent, args=call.args, status="refused", exactness="bounded", refusal=reason, window=window, rendering=f"Not answered: {reason}.")

    if (section.get("history") or {}).get("store") not in {"episode_archive", MEMBER_STORE}:
        return refused("log_liveness reads the success heartbeats of the episode archive (logs.collection.archive) and the provisional days; this generation does not serve them")
    if list(ctx.constraints()):
        return refused("log_liveness is operator-only in this phase: the history tables are not scoped by an access policy")
    try:
        recipe = load_keyword_recipe(config)
    except ConfigError as exc:
        return refused(f"the keyword recipe cannot be read: {exc}")
    success_rules = [r for r in recipe.rules if is_success(r)]
    if not success_rules:
        return refused("the keyword recipe records no success heartbeats (no rule with category success); whether a job runs cannot be read from failure rules, and an absence of failures is not evidence that it ran")
    chosen = [r for r in success_rules if not args.event or any(matches(r.id, e) for e in args.event)]
    ignored = [e for e in args.event or [] if not any(matches(r.id, e) for r in success_rules)]
    if not chosen:
        return refused(f"no success heartbeat rule matches {', '.join(args.event or [])}; the success heartbeat rules are {', '.join(r.id for r in success_rules)}")

    clock = getattr(ops, "_clock", None)
    current = clock(timezone.utc) if callable(clock) else datetime.now(timezone.utc)
    now = args.at.astimezone(timezone.utc) if args.at is not None else current
    if args.at is None and args.as_of is not None:
        now = min(now, datetime.combine(args.as_of + timedelta(days=1), datetime.min.time(), timezone.utc))
    lookback = args.lookback_days or settings.cadence_days
    limit = max_window_days(ops)
    if lookback > limit:
        return refused(f"liveness reads at most {limit} days (serving.max_window_days); narrow the lookback")
    last_day = (now - timedelta(microseconds=1)).date()
    first_day = last_day - timedelta(days=lookback - 1)
    first, last = first_day.isoformat(), last_day.isoformat()
    interval = {"start": first, "end": last, "label": f"last {lookback} days to {now.isoformat()}"}

    archive_path: Path | None = None
    built: Any = None
    if member is not None:
        # the member's units record no per-day keyword recipe: a day whose coverage records carry
        # success counts was captured with success rules (``MemberHistory.recorded_days``)
        tiers = member.tiers(first, last)
        try:
            built = member.aggregate(tiers, gap_seconds)
        except HistoryUnavailable as exc:
            return refused(str(exc), interval)
        archived = sorted(d for d, t in tiers.items() if t.tier == "archive")
        recipes = {d: recipe.identity for d in member.recorded_days(archived)}
    else:
        provisional = provisional_ledger(config)
        route = ops.history_route(ctx.generation)
        view = route[0].served_view() if route is not None and hasattr(route[0], "served_view") else None
        archive_path = Path(route[0].path) if route is not None else None
        tiers = tier_map(manifest, provisional, first, last, view)
        for day, tier in tiers.items():
            if tier.tier == "provisional" and not Path(str(tier.source)).exists():
                tiers[day] = DayTier("missing", reason="provisional projection file unavailable")
        archived = sorted(d for d, t in tiers.items() if t.tier == "archive")
        try:
            recipes = day_recipes(archive_path, archived) if archived and archive_path is not None else {}
        except HistoryUnavailable as exc:
            return refused(str(exc), interval)
    effective = collection.keywords.effective_from
    pre = sorted(d for d in archived if recipes.get(d) != recipe.identity)
    if effective is not None:
        pre = sorted({*pre, *(d for d in tiers if d < effective.isoformat())})
    unobserved = sorted(d for d, t in tiers.items() if t.tier in {"gap", "missing"})
    heartbeat_days = sorted(d for d, t in tiers.items() if t.tier in {"archive", "provisional"} and d not in pre)
    unusable = set(pre) | set(unobserved)
    coverage = {"days": len(tiers), "heartbeat_days": heartbeat_days, "pre_heartbeat_days": pre, "unobserved_days": unobserved,
                "tiers": {d: t.tier for d, t in sorted(tiers.items())}, "recipe": recipe.identity, "effective_from": effective.isoformat() if effective else None}
    notes = [
        "Liveness is read from success heartbeats (rules with category success): a halt is inferred from silence, "
        f"no success for more than {settings.halt_factor:g} x the median interval between success runs. It is not proof: "
        "a paused schedule or a changed success message also silences the heartbeat.",
        f"Successes less than keywords.gap_seconds ({gap_seconds} s) apart form one run; shorter intervals are not visible.",
    ]
    if pre:
        start = f" (keywords.effective_from {effective.isoformat()})" if effective else ""
        notes.append(f"Days before heartbeat recording{start}, captured without the success rules: {', '.join(pre)}. No success heartbeat could be recorded on them; their silence is not evidence of a halt.")
    if unobserved:
        notes.append("Days without served data (gaps or missing; not evidence of a halt): " + ", ".join(unobserved))
    if ignored:
        notes.append("Not success heartbeat rules, ignored: " + ", ".join(ignored) + ". Success heartbeat rules: " + ", ".join(r.id for r in success_rules) + ".")
    if not heartbeat_days:
        reason = "the lookback predates heartbeat recording" if pre and not set(tiers) - set(pre) - set(unobserved) else "no day of the lookback has served heartbeat data"
        return LogToolOutput(intent=call.intent, args=call.args, status="empty", exactness="bounded", window=interval, source_unavailable=unobserved, notes=notes,
                             rendering=f"Liveness unknown: {reason} ({first} to {last}). Heartbeats absent is not evidence that a job ran or halted.",
                             columns=COLUMNS, history={"coverage": coverage, "now": now.isoformat(), "recovery": []})

    scope_filters = {"tenant": [args.tenant] if args.tenant else [], "service": list(args.service or [])}
    scope_filters = {k: v for k, v in scope_filters.items() if v}
    aggregate = built if built is not None else TieredAggregate(archive_path, tiers)
    heartbeat_tiers = {d: tiers[d] for d in heartbeat_days}
    try:
        success = aggregate.pieces(HistorySpec(first=first, last=last, filters={**scope_filters, "category": [SUCCESS], "signature_id": [r.id for r in chosen]}))
        horizons = aggregate.horizons(HistorySpec(first=first, last=last, filters=scope_filters))
        recorded = (member.subset(heartbeat_tiers) if member is not None else TieredAggregate(archive_path, heartbeat_tiers)).horizons(HistorySpec(first=first, last=last, filters=scope_filters))
        failures = aggregate.pieces(HistorySpec(first=first, last=last, filters=scope_filters, exclude={"category": [SUCCESS]})) if args.recovery else []
    except HistoryUnavailable as exc:
        return refused(str(exc), interval)

    series: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for p in success:
        series.setdefault((p["tenant"], p["env"], p["service"], p["signature_id"]), []).append(p)
    rows, cited = [], set()
    by_scope: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for key, pieces in sorted(series.items()):
        merged = runs(pieces, gap_seconds)
        by_scope.setdefault(key[:3], []).extend(merged)
        latest = merged[-1]
        horizon = horizons.get(key[:3])
        observed = min(now, timestamp(horizon)) if horizon else latest["last"]
        observed = max(observed, latest["last"])
        quiet = (observed - latest["last"]).total_seconds()
        seconds, basis, used = cadence(merged, unusable, settings.min_intervals, gap_seconds)
        threshold = settings.halt_factor * seconds if seconds is not None else None
        status = "cadence_unknown" if threshold is None else "halted" if quiet > threshold else "running"
        spans = sorted(days_between(latest["last"], observed) & set(unobserved))
        cited.update(p["doc_id"] for p in latest["pieces"] if p["tier"] == "archive")
        rows.append({
            "tenant": key[0], "env": key[1], "service": key[2], "event": key[3], "issue": latest["issue"],
            "last_success": iso(latest["last"]), "observed_until": iso(observed), "quiet_seconds": quiet, "quiet": duration(quiet),
            "cadence_seconds": seconds, "cadence": duration(seconds), "cadence_basis": basis, "intervals": used, "threshold_seconds": threshold,
            "status": status, "runs": len(merged), "first_success_in_lookback": iso(merged[0]["first"]),
            "collection_behind_seconds": max(0.0, (now - observed).total_seconds()), "quiet_spans_unobserved_days": spans,
            "provisional": any(p["tier"] == "provisional" for p in latest["pieces"]),
        })
    if scope_filters:  # a named client/service captured on heartbeat days without any heartbeat of the asked kinds
        before = sorted(k for k in horizons if k not in recorded and k not in by_scope)
        if before:
            notes.append(f"{len(before)} named scopes were captured only before heartbeat recording; their liveness cannot be read: " + ", ".join("/".join(k) for k in before[:10]) + ".")
        for key, horizon in sorted(recorded.items()):
            if key in by_scope:
                continue
            rows.append({"tenant": key[0], "env": key[1], "service": key[2], "event": None, "issue": None, "last_success": None,
                         "observed_until": iso(min(now, timestamp(horizon))), "quiet_seconds": None, "cadence_seconds": None,
                         "cadence_basis": None, "intervals": 0, "threshold_seconds": None, "status": "no_heartbeats", "runs": 0})
    rows.sort(key=lambda r: (STATUS_ORDER[r["status"]], r["tenant"], r["env"], r["service"], str(r["event"])))
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in STATUS_ORDER}

    recovery: list[dict[str, Any]] = []
    if args.recovery:
        episodes: list[dict[str, Any]] = []
        failure_series: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
        for p in failures:
            failure_series.setdefault((p["tenant"], p["env"], p["service"], p["signature_id"]), []).append(p)
        for key, pieces in failure_series.items():
            for run in runs(pieces, gap_seconds):
                episodes.append({"key": key, **run, "category": run["pieces"][0].get("category")})
        episodes.sort(key=lambda e: (e["last"], e["key"]), reverse=True)
        for ep in episodes[: args.top_n]:
            scope_runs = sorted(by_scope.get(ep["key"][:3], []), key=lambda r: r["first"])
            after = next((r for r in scope_runs if r["first"] > ep["last"]), None)
            during = any(r["first"] <= ep["last"] and r["last"] >= ep["first"] for r in scope_runs)
            if after is not None:
                status = "recovered"
            elif ep["last"].date().isoformat() < heartbeat_days[0] or ep["last"].date().isoformat() in pre:
                status = "predates_heartbeats"
            elif not scope_runs:
                status = "heartbeats_absent"
            else:
                status = "not_recovered"
            horizon = horizons.get(ep["key"][:3])
            cited.update(p["doc_id"] for p in ep["pieces"] if p["tier"] == "archive")
            if after is not None:
                cited.update(p["doc_id"] for p in after["pieces"] if p["tier"] == "archive")
            recovery.append({
                "tenant": ep["key"][0], "env": ep["key"][1], "service": ep["key"][2], "issue": ep.get("issue"), "signature_id": ep["key"][3],
                "category": ep["category"], "first_failure": iso(ep["first"]), "last_failure": iso(ep["last"]), "occurrences": ep["occurrences"],
                "status": status, "recovered_at": iso(after["first"]) if after else None, "recovered_by": after["signature_id"] if after else None,
                "seconds_after_last_failure": (after["first"] - ep["last"]).total_seconds() if after else None,
                "time_to_recovery_seconds": (after["first"] - ep["first"]).total_seconds() if after else None,
                "time_to_recovery": duration((after["first"] - ep["first"]).total_seconds()) if after else None,
                "success_during_episode": during, "observed_until": iso(min(now, timestamp(horizon))) if horizon else None,
            })
        notes.append("Recovery is the first success heartbeat of the same client, env and service after the failure episode's last failure; "
                     "time to recovery runs from the first failure. A later success is a temporal sequence, not proof that the failed operation itself recovered.")
    rendering = (
        f"{len(rows)} heartbeat series as of {now.isoformat()} (lookback {first} to {last}): {counts['halted']} halted, {counts['running']} running, "
        f"{counts['cadence_unknown']} with an unknown cadence, {counts['no_heartbeats']} without heartbeats. "
        f"Halted means no success heartbeat for more than {settings.halt_factor:g} x the usual interval: inferred from silence, not proven."
    )
    if args.recovery:
        rec = {s: sum(1 for r in recovery if r["status"] == s) for s in ("recovered", "not_recovered", "heartbeats_absent", "predates_heartbeats")}
        rendering += (f" Failure episodes: {len(recovery)} shown, {rec['recovered']} followed by a success heartbeat, {rec['not_recovered']} not (yet), "
                      f"{rec['heartbeats_absent']} in scopes without heartbeats, {rec['predates_heartbeats']} before heartbeat recording. "
                      "A later success of the same client and service is a temporal sequence, not proof that the failed operation recovered.")
    if not rows and not recovery:
        rendering = f"No success heartbeat of {', '.join(r.id for r in chosen)} in {first} to {last} for this scope. Heartbeats absent is not evidence that a job ran or halted."
    # a recovery question shows the failure episodes and their recovery; the series stay in ``history``
    shown, columns = (recovery, RECOVERY_COLUMNS) if args.recovery else (rows, COLUMNS)
    return LogToolOutput(
        intent=call.intent, args=call.args, status="bounded" if rows or recovery else "empty", exactness="bounded",
        rows=shown[: args.top_n], window=interval, source_unavailable=unobserved, notes=notes, rendering=rendering,
        document_ids=sorted(cited), columns=columns, truncated=len(rows) > args.top_n,
        history={"coverage": coverage, "now": now.isoformat(), "series": rows[: args.top_n], "recovery": recovery, "rows_read": aggregate.rows_read},
    )
