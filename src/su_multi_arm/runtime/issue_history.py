"""``issue_history`` (plan K §4.1/§4.7, increment I1): one call over every day of a range, archived
or provisional, with per-tier aggregate pushdown and merge (``ingestion/episode_history``).

The range defaults to the last ``logs.collection.serving.default_view_days`` days (a default, not a
limit; ``serving.max_window_days`` bounds it). Each day is read from exactly one tier: the pinned
generation's archive shards, else the provisional store's pinned projection file, else it is a
recorded gap or missing; gap and missing days are reported, never counted as zero. Episodes follow
the cross-midnight count rule; partial and provisional contributions are flagged on every row and
in the coverage block. Operator-only: a caller with access-policy constraints is refused (the
history tables are not tenant-scoped in this phase).

On a partitioned collection (staging fix B1) the same tables are built per request from the live log
member's pinned archive members and slices (``runtime/member_logs.MemberHistory``).

Success heartbeats (``category: success``, O6) are not failures: they are excluded unless the call
asks for that category explicitly (``category: [success]``); the answer states the exclusion.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from ..decision.log_intents import IssueHistoryInput, resolve_window
from ..ingestion.episode_history import DayTier, HistorySpec, HistoryUnavailable, TieredAggregate, tier_map
from ..ingestion.episode_summaries import SUCCESS
from .member_logs import MEMBER_STORE, MemberHistory


def provisional_ledger(config: Any) -> dict[str, dict[str, Any]]:
    from ..ingestion.provisional import open_provisional, provisional_enabled

    return open_provisional(config).current() if provisional_enabled(config) else {}


def run_issue_history(ops: Any, ctx: Any, call: Any) -> Any:
    from .log_tools import LogToolOutput

    args = IssueHistoryInput.model_validate(call.args)
    config = ops.config
    manifest = ops.log_manifest(ctx)
    section = manifest.get("logs") or {}

    def refused(reason: str, window: dict[str, str] | None = None) -> Any:
        return LogToolOutput(intent=call.intent, args=call.args, status="refused", exactness="bounded", refusal=reason, window=window, rendering=f"Not answered: {reason}.")

    store = (section.get("history") or {}).get("store")
    if store not in {"episode_archive", MEMBER_STORE}:
        return refused("issue_history reads the episode archive (logs.collection.archive) and the provisional days; this generation does not serve them")
    if list(ctx.constraints()):
        return refused("issue_history is operator-only in this phase: the history tables are not scoped by an access policy")
    if store == MEMBER_STORE:
        # a partitioned collection: the live log member's archive members and pinned slices
        with MemberHistory(ops, ctx) as member:
            return _run(ops, ctx, call, args, manifest, refused, member=member)
    # the provisional ledger is read before the archive pointer: a day dropped from the provisional
    # store in between is already served by the pointer read after it (never lost; archive wins)
    return _run(ops, ctx, call, args, manifest, refused, provisional=provisional_ledger(config))


def _run(ops: Any, ctx: Any, call: Any, args: Any, manifest: dict[str, Any], refused: Any, *, provisional: dict[str, Any] | None = None, member: Any = None) -> Any:
    from .episode_tools import epoch_notes, max_window_days
    from .log_tools import LogToolOutput

    collection = ops.config.logs.collection
    compile_ctx = ops.log_context(ctx).model_copy(update={"default_window": f"last {collection.serving.default_view_days} days", **({"latest_day": None} if collection.provisional.enabled or member is not None else {})})
    window = resolve_window(args, compile_ctx)
    limit = max_window_days(ops)
    if (window.end - window.start).days > limit:
        raise ValueError(f"summary queries support at most {limit} days; narrow the window")
    first, last = window.start.isoformat(), (window.end - timedelta(days=1)).isoformat()
    interval = {"start": first, "end": last, "label": window.label}
    if member is not None:
        tiers = member.tiers(first, last)
    else:
        route = ops.history_route(ctx.generation)
        view = route[0].served_view() if route is not None and hasattr(route[0], "served_view") else None
        tiers = tier_map(manifest, provisional or {}, first, last, view)
        for day, tier in tiers.items():  # a provisional file that is gone: that day only is unavailable
            if tier.tier == "provisional" and not Path(str(tier.source)).exists():
                tiers[day] = DayTier("missing", reason="provisional projection file unavailable")
    filters = {"tenant": [args.tenant] if args.tenant else [], "service": list(args.service or []), "signature_id": list(args.signature_id or []), "category": list(args.category or [])}
    exclude_success = SUCCESS not in (args.category or [])  # failure view unless success is asked for
    spec = HistorySpec(first=first, last=last, filters={k: v for k, v in filters.items() if v}, group_by=list(args.group_by), list_k=args.list_episodes,
                       exclude={"category": [SUCCESS]} if exclude_success else {})
    try:
        with ctx.trace.span("issue_history", days=len(tiers)):
            if member is not None:
                result = member.aggregate(tiers, collection.keywords.gap_seconds).execute(spec)
            else:
                result = TieredAggregate(Path(route[0].path) if route is not None else None, tiers).execute(spec)
    except HistoryUnavailable as exc:
        return refused(str(exc), interval)
    by_tier: dict[str, list[str]] = {}
    for day, tier in sorted(tiers.items()):
        by_tier.setdefault(tier.tier, []).append(day)
    partial = {d: t.missing_scopes for d, t in tiers.items() if t.tier == "archive" and t.closure == "partial"}
    provisional_days = {d: t.as_of for d, t in tiers.items() if t.tier == "provisional"}
    gaps = {d: t.reason for d, t in tiers.items() if t.tier == "gap"}
    missing = {d: t.reason for d, t in tiers.items() if t.tier == "missing"}
    coverage = {
        "days": len(tiers),
        "archive_days": len(by_tier.get("archive", [])),
        "partial_days": partial,
        "provisional_days": provisional_days,
        "provisional_missing_scopes": {d: t.missing_scopes for d, t in tiers.items() if t.tier == "provisional" and t.missing_scopes},
        "gap_days": gaps,
        "missing_days": sorted(missing),
        "boundary_unknown_pieces": result.boundary_unknown,
        "tiers": {d: t.tier if t.closure != "partial" else "archive_partial" for d, t in sorted(tiers.items())},
    }
    rows = []
    for group in result.groups[: args.top_n]:
        row = dict(group)
        for flag in ("partial", "provisional"):
            row[flag] = bool(row.get(flag))
        rows.append(row)
    episodes = sum(g["episodes"] for g in result.groups)
    occurrences = sum(g["occurrences"] for g in result.groups)
    notes = [
        "Counts are observed rule episodes and rule-matched source messages from keyword-filtered summaries, not confirmed outages.",
        "The reason shown is the detection rule's category and issue: a classified signal, not an established cause.",
        "An episode continued across midnight is counted once, on its first day inside the range; occurrences are counted inside the range only.",
    ]
    if provisional_days:
        notes.append("Provisional days (not yet closed; counts can change until the day closes): " + ", ".join(f"{d} (as of {a})" for d, a in sorted(provisional_days.items())))
    if partial:
        notes.append("Days archived partial (missing or stale source scopes; counts are a lower bound): " + ", ".join(f"{d} ({len(m)} missing scopes)" for d, m in sorted(partial.items())))
    if gaps:
        notes.append("Days recorded as collection gaps (not zero incidents): " + ", ".join(sorted(gaps)))
    if missing:
        notes.append("Days without served data (not zero incidents): " + ", ".join(sorted(missing)))
    if result.boundary_unknown:
        notes.append(f"{result.boundary_unknown} episode pieces start just after a midnight whose previous day had no rows to compare with; each is counted as a new episode.")
    if args.category:
        notes.append("Category filter: " + ", ".join(args.category) + ".")
    if exclude_success and result.excluded_occurrences:
        notes.append(f"{result.excluded_occurrences:,} success-heartbeat messages (category success) in this range are not failures and are not counted; log_liveness answers whether a job is running or recovered.")
    notes.extend(epoch_notes(manifest, window))
    columns = [*["issue" if g == "signature_id" else g for g in args.group_by], "episodes", "occurrences", "first_failure", "last_failure", "active_days"]
    unavailable = sorted({*gaps, *missing})
    status = "bounded" if rows else "empty"
    rendering = (
        f"{len(result.groups)} issue groups in {window.label}: {episodes:,} observed episodes, {occurrences:,} rule-matched messages. "
        f"Days: {coverage['archive_days']} archived ({len(partial)} partial), {len(provisional_days)} provisional, {len(gaps)} gaps, {len(missing)} without data. "
        "Outage status, cause and recovery are not established."
    )
    document_ids = sorted({p["doc_id"] for p in result.pieces if p["tier"] == "archive"})
    return LogToolOutput(
        intent=call.intent,
        args=call.args,
        status=status,
        exactness="bounded",
        rows=rows,
        window=interval,
        source_unavailable=unavailable,
        notes=notes,
        rendering=rendering,
        document_ids=document_ids,
        columns=columns,
        truncated=len(result.groups) > args.top_n,
        history={"coverage": coverage, "episodes": result.pieces, "rows_read": result.rows_read},
    )
