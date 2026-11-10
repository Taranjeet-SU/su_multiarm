"""Synthetic keyword policy, day snapshots and configs for the collection-policy tests.

Everything here is invented: family, phrase, rule, tenant, service and index names are synthetic."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from su_multi_arm.ingestion.episode_summaries import SummaryRule, compile_summary, episodes, validate_summary

FAMILY = {
    "family": "synthetic_ml",
    "phrases": ["model server unavailable", "training job failed", "connection refused"],
    "rules": [
        {"id": "ml.model_unavailable", "title": "Model server unavailable", "category": "ml_serving", "alternatives": [["model server unavailable"]]},
        {"id": "ml.training_failed", "title": "Training job failed", "category": "ml_training", "alternatives": [["training job failed"]]},
        {"id": "net.refused", "title": "Connection refused", "category": "connectivity", "alternatives": [["connection refused"]]},
    ],
}
RULES = [SummaryRule.model_validate(r) for r in FAMILY["rules"]]
PHRASES = list(FAMILY["phrases"])


def write_family(path: Path, family: dict[str, Any] | None = None) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(family or FAMILY, sort_keys=False))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def millis(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def day_start(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc)


def response(day: str, events: list[tuple[str, int, int]], *, unclassified: int = 1, rules=RULES, phrases=PHRASES) -> dict[str, Any]:
    """A complete synthetic source aggregation: ``events`` = (rule id, minute of day, count)."""
    start = day_start(day)
    by_rule: dict[str, list[dict[str, Any]]] = {r.id: [] for r in rules}
    for rule_id, minute, count in sorted(events, key=lambda e: e[1]):
        key = millis(start + timedelta(minutes=minute))
        by_rule[rule_id].append({"key": key, "doc_count": count, "first": {"value": key}, "last": {"value": key + 59_000}})
    known = sum(c for _, _, c in events)
    selected = known + unclassified
    return {
        "timed_out": False,
        "_shards": {"total": 1, "successful": 1, "failed": 0},
        "hits": {"total": {"value": selected, "relation": "eq"}},
        "aggregations": {
            "source_window": {"bounded": {"doc_count": selected + 100}},
            "unclassified": {"doc_count": unclassified},
            "keywords": {"buckets": {str(i): {"doc_count": selected if i == 0 else 0} for i in range(len(phrases))}},
            "rules": {"buckets": {rid: {"doc_count": sum(m["doc_count"] for m in cells), "minutes": {"buckets": cells}} for rid, cells in by_rule.items()}},
        },
    }


def snapshot(day: str, tenant: str, service: str, events: list[tuple[str, int, int]], *, phrases=PHRASES, rules=RULES, window: tuple[str, str] | None = None) -> dict[str, Any]:
    start = day_start(day)
    a, b = window or (start.isoformat(), (start + timedelta(days=1)).isoformat())
    compiled = compile_summary(phrases, rules, a, b)
    summary = validate_summary(response(day, events, rules=rules, phrases=phrases), compiled, rules, phrases)
    scope = {"index": f"{tenant}-{service}-{day.replace('-', '.')}", "tenant": tenant, "env": "prod", "service": service}
    rows = episodes(summary, scope, rules)
    for i, row in enumerate(rows):
        row["evidence"] = [{"id": f"src-{tenant}-{day}-{i}", "ts": row["first_failure"], "message": f"synthetic masked line: {row['issue'].lower()} at <host>"}]
    return {
        "scope": scope,
        "summary": summary,
        "episodes": rows,
        "observed_at": (start + timedelta(days=1, hours=1)).isoformat(),
        "evidence_documents": len(rows),
        "unclassified_examples": [{"message": "synthetic unclassified candidate"}],
    }


def day_snapshots(day: str, n: int) -> list[dict[str, Any]]:
    """Two tenants; the model service fails on every third day with a reason that cycles."""
    rule = ["ml.model_unavailable", "ml.training_failed", "net.refused"][n % 3]
    out = []
    for tenant in ("tenant-a", "tenant-b"):
        events = [(rule, 600 + n % 60, 5 + n)] if (n % 3 == 0 or tenant == "tenant-a") else []
        out.append(snapshot(day, tenant, "ml-serving", events))
    return out


def days(first: str, count: int) -> list[str]:
    start = date.fromisoformat(first)
    return [(start + timedelta(days=i)).isoformat() for i in range(count)]


def policy_logs(tmp: Path, sha: str, **collection: Any) -> dict[str, Any]:
    archive = {"enabled": True, **collection.pop("archive", {})}
    return {
        "history": {"tier": False},  # the archive replaces the compose history tier for this mode
        "retention": {"keep_days": 10, "raw_days": 3, "keep_generations": 2, "derived": "forever", "pin_retry_seconds": 1, "max_pin_wait_seconds": 0},
        "collection": {
            "schedule": {"summary_refresh_minutes": 15, "heavy_window_utc": {"start": "01:00", "end": "07:00"}, "day_close_delay_minutes": 60},
            "keywords": {"files": [{"path": str(tmp / "policy" / "ml.yaml"), "sha256": sha}]},
            "archive": archive,
            "staging": {"path": str(tmp / "staging")},
            "serving": {"max_window_days": 400},
            **collection,
        },
    }
