"""Synthetic unified collection of the staging fixes (2026-11-10): an adopted support store **with
its own typed projection** whose sealed schema lacks the log-only fields (as a real support store
adopted before the log member joined), plus the live log member (archive day members closed by
``LiveLogs.close_day`` and the F0 live slices) and the v2 ``logs`` block.

Everything is synthetic (``tests/fixtures/unified``): sqlite with ``lexical_engine: bm25``, the hash
embedder, rules decision, no provider call. ``support_only`` builds the same adopted store without
the log member (the "today" side of the support-identity comparisons).
"""

from __future__ import annotations

import copy
from datetime import date
from pathlib import Path
from typing import Any

import yaml

import tests.live_log_support as L
from su_multi_arm import Engine
from su_multi_arm.core.config import validate_config

# fields the log member brings: the adopted support store was sealed without them
LOG_ONLY = ("tenant", "category", "signature_id", "env")
PROJECTION = {
    "enabled": True,
    "backend": "local",
    "tables": {
        "ticket": {
            "kind": "ticket",
            "description": "One row per support ticket.",
            "columns": ["source", "account", "product", "service", "status", "priority", "created_at"],
            "descriptions": {
                "source": "Record source (ticket).",
                "account": "Customer account name of the ticket.",
                "product": "Product the ticket is about.",
                "status": "Ticket workflow state.",
                "priority": "Ticket priority.",
                "created_at": "When the ticket was opened.",
            },
        }
    },
}
SQL_ROLE = {"provider": "openai_compatible", "model": "fake-sql-writer", "base_url": "http://127.0.0.1:9/v1"}


def support_schema(fx: Any) -> dict[str, Any]:
    schema = copy.deepcopy(fx.manifest["metadata_schema"])
    schema["fields"] = [f for f in schema["fields"] if f["name"] not in LOG_ONLY]
    return schema


def build_support_store(root: Path, fx: Any, *, projection: bool = True) -> Path:
    """The validated support store (a v2 configuration; projection on) and its configuration file."""
    root.mkdir(parents=True, exist_ok=True)
    data = L.original_data(root, fx, ("lexical", "dense", "structured"))
    data["metadata_schema"] = support_schema(fx)
    if projection:
        data["projection"] = copy.deepcopy(PROJECTION)
    Engine(validate_config(data)).ingest(L.support_documents(fx))
    path = root / "original.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


TOOLS = ["search", "get_document", "list_values", "aggregate", "incident_episodes", "issue_history", "log_liveness", "log_top", "log_trend", "log_affected"]


def logs_block(root: Path) -> dict[str, Any]:
    return {"source": "log_host", "partitions": {"path": str(root / "log_parts")}, "intents": {"enabled": True},
            "compose": {"exemplars": "per_tenant", "kinds": {"log_day": True, "endpoint_day": True, "exception_day": True, "coverage_day": True}}}


def add_logs(root: Path, data: dict[str, Any]) -> None:
    """The v2 ``logs`` block and the keys it requires (a configured log source, the pattern
    extractor and the line recipe), as a log deployment's configuration carries them."""
    from tests.logs_gold_support import gold_config, write_gold_library

    gold = gold_config(root)
    write_gold_library(root / "patterns")
    data["sources"] = {"log_host": gold["sources"]["gold"]}
    data["extraction"] = {**data.get("extraction", {}), "extractors": ["pattern"], "pattern_library": gold["extraction"]["pattern_library"]}
    data.setdefault("ingestion", {})["line_recipe"] = {"fields": ["exemplars"]}
    # the retention invariant of a log deployment: log-line text is never embedded
    data["ingestion"]["projections"] = {"embed_fields": {"signature": ["template"], "incident": ["summary"], "shared_error": ["summary"]},
                                         "exclude_kinds": {"vector": ["signature_examples"]}}
    data["logs"] = logs_block(root)
    data["privacy"] = {**data.get("privacy", {}), "trace_masking": "privacy"}
    data["tools"] = {"enabled": list(TOOLS)}


def unified_data(root: Path, fx: Any, original: Path, *, logs: bool, projection: bool = True, sql: bool = False) -> dict[str, Any]:
    data = L.unified_data(root, fx, original, logs=logs)
    if projection:
        data["projection"] = copy.deepcopy(PROJECTION)
    if logs:
        add_logs(root, data)
    if sql:
        data["models"]["roles"]["planner"] = dict(SQL_ROLE)
        data["pipeline"]["arms"]["typed_query"] = {"mode": "sql_surface", "sql_surface": {"role": "planner"}}
    return data


def collection(root: Path, fx: Any, *, logs: bool = True, projection: bool = True, sql: bool = False, live: bool = True, embed: bool = False) -> Engine:
    """Adopt the support store, compose, and (``logs``) close the F0 archive days and seal the F0
    live window into the log member's ledger."""
    original = build_support_store(root, fx, projection=projection)
    engine = Engine(validate_config(unified_data(root, fx, original, logs=logs, projection=projection, sql=sql)))
    registry = engine.view.attach()
    registry.adopt("support")
    engine.compose()
    if not logs:
        return engine
    lives = registry.live
    ledger = lives.ledger("logs")
    for day_s in L.ARCHIVE_DAYS:
        day = date.fromisoformat(day_s)
        ledger.seal(day, "five_minute", fx.members[f"logs/day:{day_s}"], as_of=L.at(day, 23, 59))
        lives.close_day("logs", day, as_of=L.at(day, 23, 59))
    if live:
        L.seal_live_window(ledger, fx)
    if embed:
        L.embed_all(engine)
    return engine


# --- a log member produced from episode summaries with success heartbeats -------------------------------


def summaries(snapshots: list[dict[str, Any]]) -> list[Any]:
    from su_multi_arm.ingestion.episode_summaries import summary_documents
    from tests import two_halves_support as th

    return [d.model_copy(update={"dataset": "unified"}) for d in summary_documents("unified", snapshots, th.RULES, citations=True)]


def heartbeat_days() -> dict[str, list[Any]]:
    """The two-halves synthetic episode summaries per day: closed 2026-11-04..08, provisional
    2026-11-09 up to 10:30 UTC (failure and success-heartbeat families)."""
    from tests import two_halves_support as th

    days = {day: summaries(th.day_snapshots(day)) for day in th.CLOSED}
    days[th.TODAY] = summaries(th.day_snapshots(th.TODAY, until_minute=630))
    return days


def heartbeat_collection(root: Path, fx: Any, *, planner: bool = False) -> Engine:
    """The adopted support store plus a live log member built from ``heartbeat_days`` (closed days
    as archive day members, today as a live slice) and a ``logs`` block whose keyword recipe pins
    both families (liveness)."""
    from tests import two_halves_support as th
    from tests.log_policy_support import policy_logs, write_family

    original = build_support_store(root, fx)
    data = unified_data(root, fx, original, logs=True)
    files = [
        {"path": str(root / "policy" / "ops.yaml"), "sha256": write_family(root / "policy" / "ops.yaml", th.FAILURES)},
        {"path": str(root / "policy" / "success.yaml"), "sha256": write_family(root / "policy" / "success.yaml", th.SUCCESSES)},
    ]
    policy = policy_logs(root, files[0]["sha256"])
    policy["collection"]["keywords"] = {"files": files}
    data["logs"].update(policy)
    if planner:  # the two-halves planner with the vocabulary of the owner-question shapes
        data["question"] = {"timezone": "UTC", "vocabulary": {"terms": th.VOCABULARY}}
        data["crosssource"] = {"planner": {"enabled": True}}
    days = heartbeat_days()
    fields = {f["name"] for f in data["metadata_schema"]["fields"]}
    for docs in days.values():
        for doc in docs:
            for name, value in doc.metadata.items():
                if name not in fields:
                    kind = "datetime" if name in {"window_start", "window_end", "observed_at"} else "boolean" if isinstance(value, bool) else "integer" if isinstance(value, int) else "number" if isinstance(value, float) else "string"
                    data["metadata_schema"]["fields"].append({"name": name, "type": kind, "nullable": True, "filterable": True, "groupable": True, "sortable": True})
                    fields.add(name)
    engine = Engine(validate_config(data))
    registry = engine.view.attach()
    registry.adopt("support")
    engine.compose()
    lives = registry.live
    ledger = lives.ledger("logs")
    for day_s in th.CLOSED:
        day = date.fromisoformat(day_s)
        ledger.seal(day, "day", days[day_s], full_day=True, as_of=L.at(day, 23, 59))
        lives.close_day("logs", day, as_of=L.at(day, 23, 59))
    today = date.fromisoformat(th.TODAY)
    ledger.seal(today, "day", days[th.TODAY], full_day=True, as_of=L.at(today, 10, 30))
    return engine
