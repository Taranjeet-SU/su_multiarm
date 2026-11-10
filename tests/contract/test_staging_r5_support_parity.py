"""A support-only question gets today's model inputs on the unified collection (staging round 5,
N12/N13; 2026-11-10).

The unified side is the adopted support store (its own typed projection, sealed without the log
member's fields) plus the live log member, ``logs.intents`` and the two-halves planner, under an llm
planner with the typed router and the SQL surface; the "today" side is the same adopted store
alone. For support questions every model input is identical, lineage aside:

* the planner payload: ``filterable_fields`` / ``observed_values`` list the knowledge members'
  fields only (the log member's fields made the unified payload ≈ 7k tokens longer in the private
  round-5 run, and the planner planned differently: N13);
* the typed router payload (no log-intent arm text for a support question);
* the SQL writer payload: the projection's tables only, no log or identity views (≈ 1.5k tokens
  longer before; the writer answered ``not_answerable`` where today wrote the ratio query: N13);
* the generator payload (evidence; the SQL lineage's generation and catalog fingerprint aside) and
  the answer contract's targets (N12).

An operations question still gets the cross-source catalog and the log fields. Synthetic fixtures,
fake ``JsonModel.call`` replies; no provider call, no service.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

pytest.importorskip("sqlglot")

import tests.live_log_support as L  # noqa: E402
import tests.staging_support as S  # noqa: E402
from su_multi_arm import SearchRequest  # noqa: E402
from su_multi_arm.decision import LlmPlan  # noqa: E402
from su_multi_arm.decision.typed_router import TypedRoute  # noqa: E402
from su_multi_arm.engine import Engine  # noqa: E402
from su_multi_arm.models import Answer  # noqa: E402
from su_multi_arm.providers import JsonModel  # noqa: E402
from tests import two_halves_support as th  # noqa: E402

STATUS_SQL = "SELECT status, COUNT(*) AS n FROM ticket GROUP BY status ORDER BY n DESC, status"
QUESTIONS = {
    "How many tickets are there per status?": "Count the tickets per status.",
    "Which account has the most high priority tickets?": "Count the high priority tickets per account.",
    # a failure word passes the operations gate, but no operations half is planned: still support-only
    "How many tickets mention an error per status?": "Count the tickets that mention an error per status.",
}
LINEAGE = {"generation", "catalog", "fingerprint", "manifest", "acl_key"}


def _respond(seen: dict[str, list[Any]]):
    def respond(self, system, payload, schema, budget, stage):
        budget.reserve()
        try:
            seen.setdefault(stage, []).append(copy.deepcopy(payload))
            if stage == "typed_router":
                question = payload["question"] if isinstance(payload, dict) else ""
                return TypedRoute.model_validate({"route": "typed", "sub_question": QUESTIONS.get(question, "Count the tickets."), "reason": "A count."})
            if stage == "planner":
                assert issubclass(schema, LlmPlan)
                return schema.model_validate({"steps": [{"arm": "lexical", "query": payload["question"]}]})
            if stage == "sql_surface":
                return schema.model_validate({"thought": "Group by status.", "answerability": "answerable", "gaps": [], "sql_code": STATUS_SQL, "response": "Counts tickets."})
            if stage == "generator":
                ids = [e["id"] for e in payload["evidence"]][:2]
                return Answer(answer=" ".join(f"See [{i}]." for i in ids), citations=ids)
            raise AssertionError(stage)
        finally:
            budget.record({"stage": stage, "provider": "fake", "model": "fake", "success": True, "usage": {"reported": True, "input_tokens": 10, "output_tokens": 5}})

    return respond


def _models(data: dict[str, Any]) -> dict[str, Any]:
    """The llm planner with the typed router, the SQL surface and a generator (both sides)."""
    data["models"]["roles"]["planner"] = dict(S.SQL_ROLE)
    data["models"]["roles"]["generator"] = dict(th.GENERATOR_ROLE)
    data["pipeline"]["arms"]["typed_query"] = {"mode": "sql_surface", "sql_surface": {"role": "planner"}}
    data["pipeline"]["decision"] = {**th.LLM_DECISION, "arm_cards": True, "typed_query": {"enabled": True, "routing": "router", "router_role": "planner"}}
    return data


def _with_router(original):
    def unified_data(*args, **kwargs):
        kwargs["sql"] = True
        return _models(original(*args, **kwargs))

    return unified_data


@pytest.fixture(scope="module")
def clock():
    mp = pytest.MonkeyPatch()
    mp.setattr(Engine, "_clock", staticmethod(lambda zone: th.NOW.astimezone(zone)))
    yield
    mp.undo()


@pytest.fixture(scope="module")
def unified(tmp_path_factory, clock):
    mp = pytest.MonkeyPatch()
    mp.setattr(S, "unified_data", _with_router(S.unified_data))
    try:
        return S.heartbeat_collection(tmp_path_factory.mktemp("r5_unified"), L.fixture(), planner=True)
    finally:
        mp.undo()


@pytest.fixture(scope="module")
def today(tmp_path_factory, clock):
    """Today's engine: the support store served by its own configuration (its own schema, sealed
    without the log member's fields), no collection, no two-halves planner."""
    import yaml

    from su_multi_arm.core.config import validate_config

    original = S.build_support_store(tmp_path_factory.mktemp("r5_today"), L.fixture())
    return Engine(validate_config(_models(yaml.safe_load(original.read_text()))))


def _scrub(value: Any) -> Any:
    """Lineage aside: the SQL lineage's generation and catalog/manifest fingerprints name the store."""
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k not in LINEAGE}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, str) and value.startswith("{"):
        try:
            return json.dumps(_scrub(json.loads(value)), sort_keys=True)
        except ValueError:
            return value
    return value


def _inputs(engine, question: str, monkeypatch) -> tuple[dict[str, list[Any]], Any]:
    seen: dict[str, list[Any]] = {}
    monkeypatch.setattr(JsonModel, "call", _respond(seen))
    result = engine.answer(SearchRequest(query=question, mode="llm"))
    return seen, result


def _text(payload: Any) -> str:
    return payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True, default=str)


def test_the_unified_side_holds_log_fields_the_support_store_does_not(unified, today):
    unified_fields = {f.name for f in unified.config.metadata_schema.fields}
    today_fields = {f.name for f in today.config.metadata_schema.fields}
    assert set(S.LOG_ONLY) <= unified_fields - today_fields
    assert unified.two_halves is not None and today.two_halves is None


@pytest.mark.parametrize("question", list(QUESTIONS))
def test_a_support_question_gets_today_s_model_inputs(unified, today, question, monkeypatch):
    x, a = _inputs(unified, question, monkeypatch)
    y, b = _inputs(today, question, monkeypatch)
    assert sorted(x) == sorted(y) == ["generator", "planner", "sql_surface", "typed_router"], (sorted(x), sorted(y))
    # the planner payload: the knowledge members' fields and values only (N13)
    assert _text(x["planner"]) == _text(y["planner"]), question
    names = {f["name"] for f in x["planner"][0]["filterable_fields"]}
    assert not names & set(S.LOG_ONLY)
    # the router and the SQL writer: today's catalog (N13)
    assert _text(x["typed_router"]) == _text(y["typed_router"]), question
    assert _text(x["sql_surface"]) == _text(y["sql_surface"]), question
    for table in ("issue_day", "coverage_day"):
        assert table not in _text(x["sql_surface"])
    # the generator input, lineage aside (N12)
    assert _scrub(x["generator"]) == _scrub(y["generator"]), question
    assert a.trace["decision"]["policy_applied"] == b.trace["decision"]["policy_applied"]
    assert a.answer == b.answer and a.contract == b.contract and a.violations == b.violations
    assert a.trace["source_coverage"]["plan"]["halves"] == ["knowledge"]


def test_the_answer_contract_reads_today_s_targets_for_a_support_question(unified, today):
    request = SearchRequest(query="How many tickets are there per status?")
    assert unified._support_only(request) and not today._support_only(request)
    schema = unified.two_halves.knowledge_schema(unified._context_for(None).generation, unified.config.metadata_schema)
    assert [f.name for f in schema.fields] == [f.name for f in today.config.metadata_schema.fields]


def test_an_operations_question_keeps_the_cross_source_catalog_and_log_fields(unified, monkeypatch):
    seen, result = _inputs(unified, "Has the crawl stopped? It is supposed to run every 30 minutes.", monkeypatch)
    assert result.trace["source_coverage"]["plan"]["halves"] == ["knowledge", "operations"]
    # the knowledge half still plans over the knowledge fields; the SQL writer sees the log tables
    names = {f["name"] for f in seen["planner"][0]["filterable_fields"]}
    assert not names & set(S.LOG_ONLY)
    assert "issue_day" in _text(seen["sql_surface"][0])
