"""Source-scoped knowledge half on a partitioned collection (staging findings F1/F2, 2026-11-10).

With the two-halves planner on and a live log member pinned, every knowledge-half step reads the
knowledge sources only, so a support-only question returns **exactly** the plan, hits (ids, order,
scores, text), per-arm candidates and aggregates it returns on the same collection without the log
member; the log units are not even opened. Without the scope (``scope_knowledge_steps: false``) log
records enter the hits, and coverage then says so (never ``not_served``). Coverage labels a ticket
count as the knowledge half. Synthetic F0 fixture (``tests/live_log_support.py``), rules decision,
hash embedder, no provider call.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from su_multi_arm import Engine, SearchRequest
from su_multi_arm.core.config import validate_config
from su_multi_arm.models import Aggregate
from tests.live_log_support import ARCHIVE_DAYS, QUERIES, arm_hits, at, build_support_store, embed_all, fixture, hits, seal_live_window, unified_data

# a caller-typed count runs as a structured step of the knowledge half
COUNTS = [Aggregate(operation="count"), Aggregate(operation="group_count", field="source"), Aggregate(operation="group_count", field="account")]


def scoped_collection(root: Path, fx: Any, *, logs: bool, scope: bool = True) -> Engine:
    """``tests.live_log_support.collection`` with ``crosssource.planner`` on."""
    original = build_support_store(root, fx)
    data = unified_data(root, fx, original, logs=logs)
    data["crosssource"] = {"planner": {"enabled": True, "scope_knowledge_steps": scope}}
    engine = Engine(validate_config(data))
    registry = engine.view.attach()
    registry.adopt("support")
    engine.compose()
    if not logs:
        return engine
    lives = registry.live
    ledger = lives.ledger("logs")
    for day_s in ARCHIVE_DAYS:
        day = date.fromisoformat(day_s)
        ledger.seal(day, "five_minute", fx.members[f"logs/day:{day_s}"], as_of=at(day, 23, 59))
        lives.close_day("logs", day, as_of=at(day, 23, 59))
    seal_live_window(ledger, fx)
    embed_all(engine)
    return engine


@pytest.fixture(scope="module")
def fx():
    return fixture()


@pytest.fixture(scope="module")
def with_logs(tmp_path_factory, fx):
    return scoped_collection(tmp_path_factory.mktemp("scoped_logs"), fx, logs=True)


@pytest.fixture(scope="module")
def without_logs(tmp_path_factory, fx):
    return scoped_collection(tmp_path_factory.mktemp("scoped_alone"), fx, logs=False)


@pytest.fixture(scope="module")
def unscoped(tmp_path_factory, fx):
    return scoped_collection(tmp_path_factory.mktemp("unscoped_logs"), fx, logs=True, scope=False)


def _plan(result) -> tuple[Any, list[Any]]:
    decision = {k: v for k, v in result.trace["decision"].items() if k != "seconds"}
    steps = [{k: v for k, v in s.items() if k != "seconds"} for s in result.trace["steps"]]
    return decision, steps


REQUESTS = [SearchRequest(query=q) for q in QUERIES] + [SearchRequest(query="records by source", aggregate=a) for a in COUNTS]


@pytest.mark.parametrize("request_", REQUESTS, ids=[r.query + (f"|{r.aggregate.operation}:{r.aggregate.field}" if r.aggregate else "") for r in REQUESTS])
def test_support_questions_are_identical_with_and_without_the_log_member(with_logs, without_logs, request_):
    assert with_logs.two_halves.scoped_arms and not without_logs.two_halves.scoped_arms
    query = request_.query
    x, y = with_logs.search(request_), without_logs.search(request_)
    assert _plan(x) == _plan(y), query  # the plan is never rewritten by the scope
    assert hits(x) == hits(y) and arm_hits(x) == arm_hits(y), query
    assert x.aggregates == y.aggregates, query
    assert all(h.source in {"ticket", "doc"} for h in x.hits)
    coverage = x.trace["source_coverage"]
    assert coverage["knowledge_scope"]["sources"] == ["ticket", "doc"] and "knowledge_scope" not in y.trace["source_coverage"]
    logs = coverage["sources"]["logs"]
    assert logs["candidates"] == 0 and logs["status"] == "intents_not_served" and logs["members"]
    assert logs["slices"] and all(s["member"] == "logs" for s in logs["slices"])  # the live slices are listed (F7)
    assert y.trace["source_coverage"]["sources"]["logs"]["status"] == "not_served"


def test_the_log_units_are_not_opened_for_a_knowledge_step(with_logs):
    backend = with_logs.backends["local"]
    with_logs.search(SearchRequest(query="crawler fetch timeout"))
    before = {k: v for k, v in backend.stats.items() if k.startswith("collection.unit_reads:") and ":logs/" in k or k.startswith("collection.unit_reads:slice:")}
    with_logs.search(SearchRequest(query="crawler fetch timeout"))
    after = {k: v for k, v in backend.stats.items() if k.startswith("collection.unit_reads:") and ":logs/" in k or k.startswith("collection.unit_reads:slice:")}
    assert before == after


def test_a_caller_source_filter_is_applied_as_given(with_logs):
    result = with_logs.search(SearchRequest(query="crawler fetch timeout", filters={"source": "logs"}))
    assert result.hits and all(h.source == "logs" for h in result.hits)
    assert "knowledge_scope" in result.trace["source_coverage"]  # the planner's steps are still knowledge steps
    assert result.trace["source_coverage"]["sources"]["logs"]["status"] == "records_only"


def test_without_the_scope_log_records_enter_the_hits_and_coverage_says_so(unscoped):
    found = None
    for query in QUERIES:
        result = unscoped.search(SearchRequest(query=query))
        if any(h.source == "logs" for h in result.hits):
            found = result
            break
    assert found is not None, "the F0 log member answers at least one support-shaped query"
    logs = found.trace["source_coverage"]["sources"]["logs"]
    assert logs["status"] == "records_only" and logs["candidates"] > 0  # never not_served with log records in the hits
    assert any("typed log intents are not enabled" in x for x in found.trace["source_coverage"]["limitations"])
    assert "knowledge_scope" not in found.trace["source_coverage"]


def test_a_count_is_the_knowledge_half_and_counts_no_log_record(with_logs, unscoped):
    request = SearchRequest(query="records by source", aggregate=Aggregate(operation="group_count", field="source"))
    result = with_logs.search(request)
    assert result.trace["source_coverage"]["plan"]["halves"] == ["knowledge"]  # F1: a structured step is not "operations"
    assert set(result.aggregates[0]["value"]) == {"ticket", "doc"}
    assert "logs" in unscoped.search(request).aggregates[0]["value"]  # what the scope prevents
