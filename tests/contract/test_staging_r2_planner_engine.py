"""Support-only questions keep today's plan on the unified collection (staging round 2, 2026-11-10).

The unified side is the adopted support store plus a live log member with ``logs.intents`` and the
two-halves planner (``staging_support.heartbeat_collection``) under a fake llm planner; the "today"
side is the same adopted support store alone (``staging_support.collection(logs=False)``) under the
same fake planner. For support questions the decision (policy, plan, unsupported, failure, note),
the executed steps and the aggregates are identical:

* N1: a planned support aggregate naming no ``kind`` is not refused by the log scope guard (its
  knowledge-half scope admits no log source); one that names the log source still is;
* N2: "how many are new?" and "did <tenant> mentions increase?" get no operations half;
* value listings of the knowledge half do not see the log member's values (no qualifier leaks);
* N5: an executed typed route answers a support aggregate alone when the planner's reply fails, as
  today, instead of the labelled fallback (single store with the two-halves planner, typed router).

Synthetic fixtures, fake ``JsonModel.call`` replies; no provider call, no service.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import tests.live_log_support as L
import tests.staging_support as S
from su_multi_arm import SearchRequest
from su_multi_arm.decision import LlmPlan
from su_multi_arm.engine import Engine
from su_multi_arm.providers import JsonModel
from tests import two_halves_support as th

COUNT_SOLVED = {"arm": "structured", "scope": {"predicates": [{"field": "status", "op": "eq", "value": "solved"}], "measure": {"kind": "count"}}}
COUNT_ALL = {"arm": "structured", "scope": {"measure": {"kind": "count"}}}
COUNT_HIGH = {"arm": "structured", "scope": {"predicates": [{"field": "priority", "op": "eq", "value": "high"}], "measure": {"kind": "count"}}}
LOG_SCOPE = {"arm": "structured", "scope": {"predicates": [{"field": "source", "op": "eq", "value": "logs"}], "measure": {"kind": "count"}}}
PLANS: dict[str, list[dict[str, Any]]] = {
    "How many solved tickets are there?": [COUNT_SOLVED],
    "How many are new?": [COUNT_ALL],
    "How many high priority tickets are about search?": [COUNT_HIGH],
    "Did brightmoor portal mentions increase per day?": [{"arm": "lexical", "query": "brightmoor"}],
    "How many log records are there?": [LOG_SCOPE],
    "Has the crawl stopped? It is supposed to run every 30 minutes.": [{"arm": "lexical", "query": "crawl stopped"}],
}
SUPPORT_QUESTIONS = list(PLANS)[:4]


@pytest.fixture(scope="module")
def planner_calls():
    """A fake planner answering ``PLANS``; records every planner payload."""
    seen: list[dict[str, Any]] = []
    mp = pytest.MonkeyPatch()

    def respond(self, system, payload, schema, budget, stage):
        budget.reserve()
        try:
            assert stage == "planner" and issubclass(schema, LlmPlan), stage
            seen.append(payload)
            return schema.model_validate({"steps": PLANS[payload["question"]]})
        finally:
            budget.record({"stage": stage, "provider": "fake", "model": "fake", "success": True, "usage": {"reported": True, "input_tokens": 10, "output_tokens": 5}})

    mp.setattr(JsonModel, "call", respond)
    mp.setattr(Engine, "_clock", staticmethod(lambda zone: th.NOW.astimezone(zone)))
    yield seen
    mp.undo()


def _llm(original):
    def unified_data(*args, **kwargs):
        data = original(*args, **kwargs)
        data["models"]["roles"]["planner"] = dict(th.PLANNER_ROLE)
        data["pipeline"]["decision"] = dict(th.LLM_DECISION)
        return data

    return unified_data


@pytest.fixture(scope="module")
def unified(tmp_path_factory, planner_calls):
    mp = pytest.MonkeyPatch()
    mp.setattr(S, "unified_data", _llm(S.unified_data))
    try:
        return S.heartbeat_collection(tmp_path_factory.mktemp("r2_unified"), L.fixture(), planner=True)
    finally:
        mp.undo()


@pytest.fixture(scope="module")
def today(tmp_path_factory, planner_calls):
    mp = pytest.MonkeyPatch()
    mp.setattr(S, "unified_data", _llm(S.unified_data))
    try:
        return S.collection(tmp_path_factory.mktemp("r2_today"), L.fixture(), logs=False)
    finally:
        mp.undo()


def decision_of(result) -> dict[str, Any]:
    return {k: v for k, v in result.trace["decision"].items() if k != "seconds"}


def executed(result) -> list[tuple[Any, ...]]:
    return [(s.get("arm"), s.get("intent")) for s in result.trace["steps"]]


def values_of(result) -> list[tuple[Any, ...]]:
    return [(a.get("operation"), a.get("value"), a.get("exactness")) for a in result.aggregates]


def test_the_unified_side_has_log_intents_and_the_guard(unified, today):
    assert unified.config.logs.intents.enabled and unified.two_halves is not None and today.two_halves is None
    assert unified.two_halves.log_sources == {"logs"}


@pytest.mark.parametrize("question", SUPPORT_QUESTIONS)
def test_support_questions_keep_today_s_decision(unified, today, question):
    x = unified.search(SearchRequest(query=question, mode="llm"))
    y = today.search(SearchRequest(query=question, mode="llm"))
    assert decision_of(x) == decision_of(y), question
    assert decision_of(x)["policy_applied"] == "llm" and decision_of(x)["planner_fallback"] is None
    assert executed(x) == executed(y) and values_of(x) == values_of(y), question
    coverage = x.trace["source_coverage"]
    assert coverage["plan"]["halves"] == ["knowledge"] and coverage["plan"]["origin"] is None  # no operations half (N2)
    assert all(h.source in {"ticket", "doc"} for h in x.hits)


def test_a_kindless_support_count_counts_the_tickets_only(unified, today):
    x = unified.search(SearchRequest(query="How many solved tickets are there?", mode="llm"))
    [aggregate] = x.aggregates
    assert aggregate["value"] == today.search(SearchRequest(query="How many solved tickets are there?", mode="llm")).aggregates[0]["value"] == 6
    assert not any(u["reason"] == "intent_required" for u in decision_of(x)["unsupported"])


def test_the_knowledge_half_does_not_see_log_values(unified, planner_calls):
    planner_calls.clear()
    unified.search(SearchRequest(query="How many high priority tickets are about search?", mode="llm"))
    payload = json.dumps(planner_calls[-1], default=str)
    # log-only values (tenants, signature ids, categories of the log member) never reach the planner
    for value in ("alderbank-stg", "crawler.fetch_timeout", "auth.unauthorized", "report.queue_stalled"):
        assert value not in payload, value


@pytest.mark.parametrize("question", ["How many are new?", "Did brightmoor portal mentions increase per day?"])
def test_generic_words_start_no_log_detector(unified, question):
    result = unified.search(SearchRequest(query=question, mode="llm"))
    assert not [a for a in result.aggregates if a.get("intent")]  # no log_new / log_trend / log_compare
    assert result.trace["source_coverage"]["sources"]["logs"]["status"] == "not_asked"


def test_a_planned_scope_over_the_log_source_is_still_guarded(unified):
    result = unified.search(SearchRequest(query="How many log records are there?", mode="llm"))
    decision = decision_of(result)
    assert any(u["reason"] == "intent_required" for u in decision["unsupported"])
    assert decision["policy_applied"] == "deterministic_fallback_plan" and decision["planner_fallback"] == "deterministic_plan"


def test_an_operations_question_still_gets_its_operations_half(unified):
    result = unified.search(SearchRequest(query="Has the crawl stopped? It is supposed to run every 30 minutes.", mode="llm"))
    assert result.trace["source_coverage"]["plan"]["halves"] == ["knowledge", "operations"]
    assert "log_liveness" in {a.get("intent") for a in result.aggregates}


# --- N5: the typed route answers a support aggregate alone, as today ------------------------------

def _typed_engine(tmp_path, *, two_halves: bool):
    from su_multi_arm.core.config import validate_config
    from tests.contract.test_typed_router_engine import config_data
    import typed_support as ts

    data = config_data(tmp_path)
    if two_halves:
        data["schema_version"] = 3
        data["crosssource"] = {"planner": {"enabled": True, "knowledge_sources": sorted({d.source for d in ts.documents()})}}
    engine = Engine(validate_config(data))
    engine.ingest(ts.documents())
    return engine


@pytest.fixture
def typed_fake(monkeypatch):
    from tests.contract.test_typed_router_engine import COUNT_SQL, IR_STEP, TYPED, sql
    from su_multi_arm.decision.typed_router import TypedRoute
    from su_multi_arm.providers.base import SchemaViolation

    def respond(self, system, payload, schema, budget, stage):
        budget.reserve()
        try:
            if stage == "typed_router":
                return TypedRoute.model_validate(TYPED)
            if stage == "planner":
                raise SchemaViolation("bad plan", raw='{"steps": "x"}', stage="planner", error_count=1, paths=["steps (list_type)"])
            if stage == "sql_surface":
                return schema.model_validate(sql(COUNT_SQL))
            return schema.model_validate(IR_STEP)
        finally:
            budget.record({"stage": stage, "provider": "fake", "model": "fake", "success": True, "usage": {"reported": True, "input_tokens": 10, "output_tokens": 5}})

    monkeypatch.setattr(JsonModel, "call", respond)


def test_the_typed_route_answers_alone_when_the_planner_fails(tmp_path, typed_fake):
    pytest.importorskip("sqlglot")
    from tests.contract.test_typed_router_engine import ALPHA, COUNT_Q

    x = _typed_engine(tmp_path / "halves", two_halves=True).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    y = _typed_engine(tmp_path / "today", two_halves=False).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    assert decision_of(y)["policy_applied"] == "typed_route_only"
    assert decision_of(x) == decision_of(y)
    assert executed(x) == executed(y) == [("typed_query", None)]
    assert values_of(x) == values_of(y)
    coverage = x.trace["source_coverage"]
    assert coverage["plan"]["fallback"] is None and coverage["plan"]["policy"] == "typed_route_only"
    assert not any("deterministic plan" in text for text in coverage["limitations"])


# --- N9 (staging round 3): the typed route's qualifier check reads knowledge values only ----------

def test_the_typed_route_resolver_does_not_see_log_values(unified, today, monkeypatch):
    import su_multi_arm.decision.llm as engine_module
    from su_multi_arm.decision.values import ValueResolver

    seen_gens: list[Any] = []

    class Spy(ValueResolver):
        def __init__(self, schema, records, gen, *a, **k):
            seen_gens.append(gen)
            super().__init__(schema, records, gen, *a, **k)

    monkeypatch.setattr(engine_module, "ValueResolver", Spy)
    unified.search(SearchRequest(query="How many high priority tickets are about search?", mode="llm"))
    assert seen_gens

    scope = unified._knowledge_value_scope()
    assert scope and scope[0].field == "source" and scope[0].op == "in"
    assert today._knowledge_value_scope() == []  # no two-halves planner: today's resolver unchanged
    gen = seen_gens[-1]
    schema = unified.config.metadata_schema
    scoped = ValueResolver(schema, unified.records, gen, scope)
    for field in ("signature_id", "category"):
        seen = json.dumps(scoped.observed(field) or [])
        for value in ("crawler.fetch_timeout", "auth.unauthorized", "report.queue_stalled"):
            assert value not in seen, (field, value)
