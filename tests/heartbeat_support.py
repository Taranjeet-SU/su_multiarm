"""Synthetic success-heartbeat fixtures (O6): a success keyword family after the failure family of
``log_policy_support``, snapshots carrying the collector's 5-minute ``heartbeats`` block, and an
archive configuration with both families pinned. Everything is invented (phrases, rules, tenants,
services, indices)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from su_multi_arm.core.config import validate_config
from su_multi_arm.ingestion.episode_summaries import SummaryRule, heartbeat_buckets, summary_documents
from tests.log_policy_support import FAMILY, PHRASES, RULES, day_snapshots, day_start, policy_logs, snapshot, write_family
from tests.logs_gold_support import gold_config
from tests.logs_support import write_library

SUCCESS_FAMILY = {
    "family": "synthetic_success",
    "phrases": ["crawl run finished ok", "index build done", "report mailed ok", "worker tick seen"],
    "rules": [
        {"id": "success.crawl_completed", "title": "Crawl completed", "category": "success", "alternatives": [["crawl run finished ok"]]},
        {"id": "success.index_completed", "title": "Index build completed", "category": "success", "alternatives": [["index build done"]]},
        {"id": "success.report_delivered", "title": "Report delivered", "category": "success", "alternatives": [["report mailed ok"]]},
        {"id": "success.scheduler_tick", "title": "Scheduler tick", "category": "success", "alternatives": [["worker tick seen"]]},
    ],
}
SUCCESS_RULES = [SummaryRule.model_validate(r) for r in SUCCESS_FAMILY["rules"]]
ALL_RULES = [*RULES, *SUCCESS_RULES]
ALL_PHRASES = [*PHRASES, *SUCCESS_FAMILY["phrases"]]
SUCCESS_IDS = {r.id for r in SUCCESS_RULES}


def hb_snapshot(day: str, tenant: str, service: str, events: list[tuple[str, int, int]], *, until_minute: int | None = None, heartbeats: bool = True) -> dict[str, Any]:
    """A snapshot under the failure + success recipe; ``until_minute``: a rolling (unclosed-day)
    capture ``[D 00:00, D + until_minute)``; ``heartbeats``: carry the collector's 5-minute block."""
    start = day_start(day)
    window = None
    if until_minute is not None:
        events = [e for e in events if e[1] < until_minute]
        window = (start.isoformat(), (start + timedelta(minutes=until_minute)).isoformat())
    snap = snapshot(day, tenant, service, events, phrases=ALL_PHRASES, rules=ALL_RULES, window=window)
    for row in snap["episodes"]:
        if row["rule_id"] in SUCCESS_IDS:
            row["evidence"] = []  # the collector never pulls lines for a success rule
            row["evidence_status"] = "not_collected_success_heartbeat"
    snap["evidence_documents"] = sum(len(r["evidence"]) for r in snap["episodes"])
    if until_minute is not None:
        snap["observed_at"] = (start + timedelta(minutes=until_minute)).isoformat()
    if heartbeats:
        snap["heartbeats"] = heartbeat_buckets(snap["summary"]["series"], SUCCESS_IDS)
        snap["heartbeat_bucket_seconds"] = 300
    return snap


def every(rule_id: str, minutes: int, start: int = 0, stop: int = 1440) -> list[tuple[str, int, int]]:
    """One success of ``rule_id`` every ``minutes`` minutes in ``[start, stop)`` (minute of day)."""
    return [(rule_id, m, 1) for m in range(start, stop, minutes)]


def write_families(tmp: Path, *, success: bool = True) -> list[dict[str, str]]:
    files = [{"path": str(tmp / "policy" / "ml.yaml"), "sha256": write_family(tmp / "policy" / "ml.yaml", FAMILY)}]
    if success:
        files.append({"path": str(tmp / "policy" / "success.yaml"), "sha256": write_family(tmp / "policy" / "success.yaml", SUCCESS_FAMILY)})
    return files


def configure_hb(tmp: Path, *, success: bool = True, effective_from: str | None = None, provisional: bool = True, category_filterable: bool = True, **collection: Any):
    """The archive configuration of ``history_support.configure`` with the success family pinned
    after the failure family (``success``), ``keywords.effective_from`` and ``log_liveness`` on."""
    files = write_families(tmp, success=success)
    write_library(tmp / "patterns")
    data = gold_config(tmp)
    data["dataset"] = "synthetic-archive"
    if provisional:
        collection.setdefault("provisional", {"enabled": True})
    logs = policy_logs(tmp, files[0]["sha256"], **collection)
    logs["collection"]["keywords"] = {"files": files, **({"effective_from": effective_from} if effective_from else {})}
    data["logs"].update(logs)
    base = validate_config(data).model_dump(mode="json")
    base["tools"]["enabled"] = ["incident_episodes", "issue_history", "log_liveness", "log_top", "aggregate", "get_document"]
    sample = summary_documents("synthetic-archive", day_snapshots("2026-01-01", 0), RULES, citations=True)
    sample += summary_documents("synthetic-archive", [hb_snapshot("2026-01-01", "tenant-x", "svc", every("success.crawl_completed", 60))], ALL_RULES, citations=True)
    fields = base["metadata_schema"]["fields"]
    existing = {f["name"]: f for f in fields}
    if existing.get("kind", {}).get("enum_values") is not None:
        existing["kind"]["enum_values"] = sorted(set(existing["kind"]["enum_values"]) | {d.kind for d in sample})
    for doc in sample:
        for name, value in {**doc.metadata, "archive_text_trimmed": True}.items():
            if name in existing:
                continue
            kind = "datetime" if name in {"window_start", "window_end", "observed_at"} else "boolean" if isinstance(value, bool) else "integer" if isinstance(value, int) else "number" if isinstance(value, float) else "string"
            existing[name] = {"name": name, "type": kind, "nullable": True, "filterable": True, "groupable": True, "sortable": True}
            fields.append(existing[name])
    for f in fields:
        if f["name"] == "category" and not category_filterable:
            f.update(filterable=False, listable=False, groupable=False)
        elif f["name"] in {"category", "tenant", "service", "signature_id"} and f.get("filterable"):
            f["listable"] = True
    return validate_config(base)
