"""Synthetic fixtures of the I1 day ledger (plan K): archived, provisional, partial and gap days,
cross-midnight chains and rolling snapshots. Everything is invented (tenants, services, indices)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from su_multi_arm.core.config import validate_config
from su_multi_arm.ingestion.episode_summaries import summary_documents
from tests.log_policy_support import RULES, day_snapshots, policy_logs, snapshot, write_family
from tests.logs_gold_support import gold_config
from tests.logs_support import write_library


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def configure(tmp_path: Path, *, provisional: bool = True, tools: list[str] | None = None, **collection: Any):
    """The archive configuration of ``test_episode_archive_serving`` plus the provisional store."""
    sha = write_family(tmp_path / "policy" / "ml.yaml")
    write_library(tmp_path / "patterns")
    data = gold_config(tmp_path)
    data["dataset"] = "synthetic-archive"
    if provisional:
        collection.setdefault("provisional", {"enabled": True})
    data["logs"].update(policy_logs(tmp_path, sha, **collection))
    base = validate_config(data).model_dump(mode="json")
    base["tools"]["enabled"] = tools or ["incident_episodes", "issue_history", "log_top", "aggregate", "get_document"]
    sample = summary_documents("synthetic-archive", day_snapshots("2026-01-01", 0), RULES, citations=True)
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
    return validate_config(base)


def rolling(day: str, tenant: str, service: str, events: list[tuple[str, int, int]], until_minute: int) -> dict[str, Any]:
    """A rolling (unclosed-day) snapshot: window ``[D 00:00, D + until_minute)``."""
    start = utc(day)
    kept = [e for e in events if e[1] < until_minute]
    snap = snapshot(day, tenant, "ml-serving" if service is None else service, kept, window=(start.isoformat(), (start + timedelta(minutes=until_minute)).isoformat()))
    snap["observed_at"] = (start + timedelta(minutes=until_minute)).isoformat()
    return snap


def stage(root: Path, day: str, snapshots: list[dict[str, Any]], *, expected: list[str] | None = None) -> Path:
    folder = root / day
    folder.mkdir(parents=True, exist_ok=True)
    for snap in snapshots:
        (folder / f"{snap['scope']['index']}.json").write_text(json.dumps(snap))
    if expected is not None:
        (folder / "_expected.json").write_text(json.dumps({"scopes": expected}))
    return folder


def occurrences(snapshots: list[dict[str, Any]]) -> int:
    return sum(c["count"] for s in snapshots for c in s["summary"]["series"])
