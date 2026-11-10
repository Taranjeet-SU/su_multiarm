"""Generation-pinned episode tools over canonical records, with ordinary ACL checks.

Failure views (``incident_episodes``, the summary-mode ``log_top``/``log_trend``/``log_affected``)
exclude success heartbeats (``category: success``, O6) unless a success rule is named explicitly;
``log_liveness`` (``runtime/liveness.py``) is the view of the success heartbeats."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
import json
from typing import ClassVar

from pydantic import Field

from ..core.types import Measure, Predicate, ScopeSpec, Sort
from ..decision.log_intents import INPUT_MODELS, LogEvidenceScope, resolve_window
from ..ingestion.episode_summaries import CONTRACT, SUCCESS
from ..models import StrictModel
from .builtin_tools import _BuiltinTool
from .tools import ToolResult


class EpisodeInput(LogEvidenceScope):
    signature_id: str | None = None
    top_n: int = Field(default=100, ge=1, le=500)


class EpisodeOutput(StrictModel):
    status: str
    exactness: str = "bounded"
    rows: list[dict] = Field(default_factory=list)
    statistics: dict = Field(default_factory=dict)
    keywords: dict[str, int] = Field(default_factory=dict)
    window: dict[str, str] = Field(default_factory=dict)
    source_unavailable: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    truncated: bool = False
    rendering: str


def filters(args):
    out = []
    if getattr(args, "tenant", None):
        out.append(Predicate(field="tenant", op="eq", value=args.tenant))
    if getattr(args, "service", None):
        out.append(Predicate(field="service", op="in", value=[args.service] if isinstance(args.service, str) else args.service))
    if getattr(args, "signature_id", None):
        out.append(Predicate(field="signature_id", op="eq", value=args.signature_id))
    return out


def failure_only(ops, args=None) -> list[Predicate]:
    """The predicate that keeps success heartbeats out of a failure view: ``category`` not
    ``success`` (rows without a category kept). Nothing when the caller names a rule explicitly
    (``signature_id``: the named rule is shown whatever its category) or when ``category`` is not a
    filterable field (no success rule can then be recorded: ``load_keyword_recipe`` refuses it)."""
    if args is not None and getattr(args, "signature_id", None):
        return []
    schema = getattr(getattr(ops, "config", None), "metadata_schema", None)
    spec = schema.field("category") if schema is not None else None
    if spec is None or not spec.filterable:
        return []
    return [Predicate(field="category", op="in", value=[SUCCESS], negate=True)]


def max_window_days(ops) -> int:
    """``logs.collection.serving.max_window_days`` (31 without the long-term archive)."""
    logs = getattr(getattr(ops, "config", None), "logs", None)
    return logs.collection.serving.max_window_days if logs is not None else 31


def window_of(ops, ctx, args):
    window = resolve_window(args, ops.log_context(ctx))
    limit = max_window_days(ops)
    if (window.end-window.start).days > limit:
        raise ValueError(f"summary queries support at most {limit} days; narrow the window")
    return window


def epoch_notes(manifest: dict, window) -> list[str]:
    """Keyword recipe epochs and gap records of the archive that the window overlaps (stated, never mixed silently)."""
    archive = (manifest.get("logs") or {}).get("archive") or {}
    first, last = window.start.isoformat(), (window.end - timedelta(days=1)).isoformat()
    epochs = sorted((span["first"], span["last"], recipe[:12]) for recipe, span in (archive.get("recipes") or {}).items()
                    if span["first"] <= last and span["last"] >= first)
    notes = []
    if len(epochs) > 1:
        notes.append("The range spans keyword recipe epochs captured with different keyword families: "
                     + "; ".join(f"{a} to {b} (recipe {r})" for a, b, r in epochs) + ". Compare counts within one epoch.")
    gaps = {d: reason for d, reason in (archive.get("gaps") or {}).items() if first <= d <= last}
    if gaps:
        notes.append("Days recorded as collection gaps (not zero incidents): " + ", ".join(sorted(gaps)))
    return notes


def stitch(rows: list[dict], gap_seconds: int) -> list[dict]:
    """Archived episodes are closed per UTC day; an episode of one tenant/env/service/rule that
    continues across midnight (the next day's first failure within ``gap_seconds`` of the previous
    day's last failure) is shown as one row, with both evidence ids. Same-day rows are untouched."""
    from ..ingestion.episode_summaries import timestamp

    out: list[dict] = []
    open_rows: dict[tuple, dict] = {}
    for row in sorted(rows, key=lambda r: (str(r.get("first_failure")), r["id"])):
        key = (row.get("tenant"), row.get("env"), row.get("service"), row.get("signature_id"))
        prev = open_rows.get(key)
        if (prev is not None and str(prev["last_failure"])[:10] < str(row["first_failure"])[:10]
                and (timestamp(row["first_failure"]) - timestamp(prev["last_failure"])).total_seconds() <= gap_seconds):
            prev["last_failure"] = max(prev["last_failure"], row["last_failure"])
            prev["occurrences"] = (prev.get("occurrences") or 0) + (row.get("occurrences") or 0)
            prev["observed_window_seconds"] = (timestamp(prev["last_failure"]) - timestamp(prev["first_failure"])).total_seconds()
            prev["evidence"] = "\n\n".join(e for e in (prev.get("evidence"), row.get("evidence")) if e)
            prev["evidence_count"] = (prev.get("evidence_count") or 0) + (row.get("evidence_count") or 0)
            prev["evidence_truncated"] = bool(prev.get("evidence_truncated") or row.get("evidence_truncated"))
            prev.setdefault("joined_evidence_ids", [prev["evidence_id"]]).append(row["evidence_id"])
            prev["crosses_day_boundary"] = True
            continue
        open_rows[key] = row
        out.append(row)
    return out


def query(ops, ctx, args: EpisodeInput) -> EpisodeOutput:
    window = window_of(ops, ctx, args)
    day_pred = window.predicate()
    coverage = ops.log_execute(ctx, ScopeSpec(predicates=[Predicate(field="kind", op="eq", value="summary_coverage"), day_pred,
                                                         *[p for p in filters(args) if p.field != "signature_id"]], limit=10000))
    covered = ops.documents_in(ctx, [row.id for row in coverage.listed])
    keys = ("source_documents", "keyword_documents", "rule_documents", "unclassified_documents", "evidence_documents", "source_requests", "response_bytes", "sample_completion_documents")
    stats = Counter({name: 0 for name in keys})
    keywords = Counter()
    observed = []
    for doc in covered:
        for name in keys:
            stats[name] += int(doc.metadata.get(name, 0))
        keywords.update(dict(json.loads(doc.fields.get("keyword_counts", "[]"))))
        if doc.metadata.get("observed_at"):
            observed.append(str(doc.metadata["observed_at"]))
    stats["source_scopes"] = len(covered)
    if stats["keyword_documents"]:
        stats["logs_not_downloaded"] = max(0, stats["keyword_documents"]-stats["evidence_documents"])
        stats["document_download_reduction_percent"] = 100 * stats["logs_not_downloaded"] / stats["keyword_documents"]
    preds = [Predicate(field="kind", op="eq", value="incident"),
             Predicate(field="last_hour", op="range", lower=f"{window.start}T00:00:00+00:00"),
             Predicate(field="first_hour", op="range", upper=f"{window.end-timedelta(days=1)}T23:59:59.999999+00:00"),
             *filters(args), *failure_only(ops, args)]
    result = ops.log_execute(ctx, ScopeSpec(predicates=preds, limit=args.top_n, sort=Sort(field="last_hour", descending=True)))
    docs = ops.documents_in(ctx, [row.id for row in result.listed])
    rows = []
    for doc in docs:
        m = doc.metadata
        rows.append({"id": doc.id, "tenant": m.get("tenant"), "service": m.get("service"), "env": m.get("env"),
                     "issue": m.get("issue", doc.title), "category": m.get("category"), "signature_id": m.get("signature_id"),
                     "first_failure": m.get("first_hour"), "last_failure": m.get("last_hour"),
                     "occurrences": m.get("occurrences"), "observed_window_seconds": m.get("observed_window_seconds"),
                     "recovered_at": None, "resolution_seconds": None, "status": m.get("episode_status"),
                     "evidence": doc.fields.get("exemplars", ""), "evidence_count": m.get("evidence_count", 0),
                     "evidence_truncated": bool(m.get("evidence_truncated")),
                     "sample_completion": doc.fields.get("sample_completion", ""), "recovery_basis": m.get("recovery_basis"),
                     "count_scope": "full_observed_episode", "evidence_id": doc.id})
    # per-day pieces (the episode archive, or the live log member of a partitioned collection) are stitched across midnight
    archived = ((ops.log_manifest(ctx).get("logs") or {}).get("history") or {}).get("store") in {"episode_archive", "collection_member"}
    if archived:
        logs = getattr(getattr(ops, "config", None), "logs", None)
        rows = stitch(rows, logs.collection.keywords.gap_seconds if logs is not None else 300)
        rows.sort(key=lambda r: str(r.get("last_failure")), reverse=True)
    known_days = {str(d.metadata.get("date")) for d in covered}
    missing = [d.isoformat() for d in window.days() if d.isoformat() not in known_days]
    truncated = bool(result.unsupported or coverage.unsupported or (result.total or 0) > len(result.listed)
                     or (coverage.total or 0) > len(coverage.listed))
    stats["episodes_displayed"] = len(rows)
    stats["episodes_total"] = result.total if result.total is not None else len(rows)
    pending = ops.log_execute(ctx, ScopeSpec(predicates=[*preds, Predicate(field="evidence_count", op="eq", value=0)], measure=Measure(kind="count")))
    if not pending.unsupported:
        stats["episodes_without_evidence"] = int(pending.value or 0)
    details = dict(stats)
    policy = getattr(ops, "acl", None)
    if not args.tenant and not args.service and (policy is None or not policy.constraints(ctx.principal)):
        collection = ops.log_manifest(ctx).get("logs") or {}
        details.update(inventory_scopes=collection.get("summary_inventory_count"),
                       capture_failures=collection.get("summary_capture_failures", 0),
                       collection_run_status=collection.get("summary_last_run_status", "unknown"),
                       collection_last_run=collection.get("summary_last_run_time"))
    details["latest_capture"] = max(observed) if observed else None
    details["statistics_complete_for_returned_scopes"] = not bool(coverage.unsupported or (coverage.total or 0) > len(coverage.listed))
    notes = ["Counts describe keyword-filtered source snapshots, not all-source incident completeness.",
             "Specific detection rules classify observed signals; neither a keyword nor a quiet gap proves an outage or recovery.",
             "Observed window is last failure minus first failure. Recovery duration remains unknown.",
             "Rows show full overlapping episode counts; a row crossing the requested boundary is not a clipped-window count.",
             "Classification groups are tenant/environment/service/rule sessions; concurrent operations within a rule may be combined."]
    if missing:
        notes.append("Missing days are not zero incidents: " + ", ".join(missing))
    if archived:
        notes.extend(epoch_notes(ops.log_manifest(ctx), window))
    return EpisodeOutput(status="bounded" if rows or covered else "empty", rows=rows, statistics=details,
                         keywords=dict(keywords), window={"start": window.start.isoformat(), "end_exclusive": window.end.isoformat()},
                         source_unavailable=missing, truncated=truncated, notes=notes,
                         columns=["tenant", "service", "issue", "first_failure", "last_failure", "occurrences", "observed_window_seconds", "resolution_seconds"],
                         document_ids=[d.id for d in docs],
                         rendering=f"{len(rows)} observed rule episodes displayed; {stats['keyword_documents']:,} keyword matches represented by {stats['evidence_documents']:,} retained examples. Outage status and recovery duration are not established.")


class EpisodeTool(_BuiltinTool):
    name: ClassVar[str] = "incident_episodes"
    description: ClassVar[str] = "Observed incident-rule episodes: client, service, issue, first/last failure, repeat count, bounded evidence, unknown recovery duration, and collection savings."
    input_model: ClassVar[type[StrictModel]] = EpisodeInput
    output_model: ClassVar[type[StrictModel]] = EpisodeOutput

    def invoke(self, ctx, args):
        output = query(self.engine, ctx, args)
        return ToolResult(output=output, evidence_ids=output.document_ids)


def run_summary_intent(ops, ctx, call):
    """Existing planner/HTTP/MCP intents share one scoped summary execution path."""
    from .log_tools import LogToolOutput

    if call.intent == "issue_history":
        from .issue_history import run_issue_history

        return run_issue_history(ops, ctx, call)
    if call.intent == "log_liveness":
        from .liveness import run_liveness

        return run_liveness(ops, ctx, call)
    args = INPUT_MODELS[call.intent].model_validate(call.args)
    window = window_of(ops, ctx, args)
    interval = {"start": window.start.isoformat(), "end": (window.end-timedelta(days=1)).isoformat(), "label": window.label}
    def refused(reason):
        return LogToolOutput(intent=call.intent, args=call.args, status="refused", exactness="bounded", refusal=reason,
                             window=interval, rendering=reason)
    if getattr(args, "deployment_id", None):
        return refused("Summary mode does not aggregate deployment-instance filters; use a client and service.")
    if getattr(args, "owner", "any") != "any":
        return refused("Pattern ownership is not a measurement retained by source-rule summaries.")
    if call.intent == "log_coverage":
        return refused("Summary mode counts source matches and downloaded examples; it does not claim per-log parser coverage. Use incident_episodes for collection statistics.")
    if call.intent not in {"log_top", "log_trend", "log_affected", "log_signature"}:
        return refused("This measurement is not retained by incident-summary collection. Use incident_episodes for observed failures and evidence.")
    if getattr(args, "metric", None) == "error_rate":
        return refused("Rule-matched messages cannot establish a whole-source failure rate.")
    levels = {v.casefold() for v in (getattr(args, "level", None) or [])}
    if levels and levels != {"error", "fatal"}:
        return refused("Summary counters classify failure rules, not exact source severity levels. Use incident_episodes for failures.")
    if call.intent == "log_signature":
        output = query(ops, ctx, EpisodeInput(**{k: v for k, v in args.model_dump(exclude_none=True).items()
                                                if k in EpisodeInput.model_fields}))
        return LogToolOutput(intent=call.intent, args=call.args, status=output.status, exactness="bounded", rows=output.rows,
                             window=interval, source_unavailable=output.source_unavailable, notes=output.notes,
                             rendering=output.rendering, document_ids=output.document_ids, columns=output.columns, truncated=output.truncated)
    if call.intent == "log_top":
        dims = args.group_by or {"service": ["service"], "tenant": ["tenant"], "signature": ["signature_id"],
                               "service_signature": ["service", "signature_id"], "tenant_signature": ["tenant", "signature_id"]}.get(args.by)
        if not dims:
            return refused("Endpoint, exception-class and instance metrics are not retained by summary collection.")
    elif call.intent == "log_affected":
        dims = ["tenant"]
    else:
        dims = ["ts_hour"] if getattr(args, "granularity", "day") == "hour" else ["date"]
    preds = [Predicate(field="kind", op="eq", value="episode_cell"), window.predicate(), *filters(args), *failure_only(ops, args)]
    if getattr(args, "tenant", None) or getattr(args, "service", None):
        covered = ops.log_execute(ctx, ScopeSpec(predicates=[Predicate(field="kind", op="eq", value="summary_coverage"), window.predicate(),
                                                            *[p for p in filters(args) if p.field != "signature_id"]], measure=Measure(kind="count")))
        if not covered.value:
            return refused("No completed summary capture is available for this client/service and window. Missing coverage is not evidence of zero failures.")
    group = [dim for dim in dims if dim != "level"]
    if not group:
        return refused("Source severity distribution is not retained by rule summaries; group by service, client or issue rule.")
    spec = ScopeSpec(predicates=preds, group_by=group, measure=Measure(kind="sum", field="occurrences"),
                     order="desc", top_n=getattr(args, "top_n", 100))
    result = ops.log_execute(ctx, spec)
    label_rows = ops.log_execute(ctx, ScopeSpec(predicates=[Predicate(field="kind", op="eq", value="signature")], limit=1000))
    labels = {d.metadata.get("signature_id"): (d.fields.get("template", d.title), d.id)
              for d in ops.documents_in(ctx, [r.id for r in label_rows.listed])}
    rows = []
    evidence = []
    for bucket in result.groups:
        row = {**bucket.key, "lines": int(bucket.value or 0), "error_count": int(bucket.value or 0),
               "matched_log_count": int(bucket.value or 0), "level": "RULE", "collection_scope": CONTRACT,
               "count_basis": "exclusive_rule_matches_in_source_time_buckets"}
        if row.get("signature_id") in labels:
            row["error_type"], row["evidence_id"] = labels[row["signature_id"]]
            row["supporting_template"] = row["error_type"]
            evidence.append(row["evidence_id"])
        rows.append(row)
    missing = [d.isoformat() for d in window.days() if d.isoformat() not in set((ops.log_manifest(ctx).get("logs") or {}).get("days", []))]
    notes = ["Counts are rule-matched source messages, not confirmed outages, affected requests, or source ERROR/FATAL severity counts.",
             "RULE denotes the detection-rule signal. Unclassified keyword candidates remain separate."]
    return LogToolOutput(intent=call.intent, args=call.args, status="bounded" if rows else "empty", exactness="bounded",
                         rows=rows, result=result, scope=spec, window=interval, source_unavailable=missing,
                         basis=["source_rule_query"], notes=notes, document_ids=sorted(set(evidence)),
                         columns=getattr(args, "columns", None) or [*[d for d in dims if d != "signature_id" or not labels],
                                  *( ["error_type"] if "signature_id" in dims and labels else []), "error_count"],
                         truncated=bool(result.unsupported or result.ties),
                         rendering=f"{len(rows)} groups of rule-matched failure signals in {window.label}. Counts include repeats without downloading each log. Outages and recovery durations are not established.")
