"""Typed router (``pipeline.decision.typed_query.routing: router``) through the Engine.

``tests/typed_support.py`` synthetic two-tenant corpus. The router, the SQL writer, the typed IR
writer, the planner and the generator are fakes patched over ``JsonModel.call`` (each reply validated
with the schema the engine passes; every call reserves and records through its meter), plus
``httpx.MockTransport`` runs through the real provider for the byte-identity and metering checks.

* the main planner request under ``router`` is byte-identical to a deployment without typed queries;
* the router sees the question, the request filters and a compact catalog summary only (no samples);
* route typed executes before the planner; its evidence is packed ahead of the retrieval evidence;
* route retrieval runs the planner only; a typed route that does not execute and a router error are
  recorded ``DroppedStep`` records and the planner still runs;
* ACL comes from execution: tenant canary through the routed path, zero foreign rows;
* the router call is metered (stage ``typed_router``) and budget validation counts it;
* the default ``routing: planner`` is unchanged.
"""

from __future__ import annotations

import json
import random

import httpx
import pytest
from pydantic import ValidationError

pytest.importorskip("sqlglot")

from su_multi_arm import Engine, SearchRequest  # noqa: E402
from su_multi_arm.core.config import TypedQueryDecision, validate_config  # noqa: E402
from su_multi_arm.core.errors import ConfigError  # noqa: E402
from su_multi_arm.decision import LlmPlan  # noqa: E402
from su_multi_arm.decision.typed_router import TypedRoute  # noqa: E402
from su_multi_arm.models import Answer, Claim  # noqa: E402
from su_multi_arm.providers import JsonModel  # noqa: E402
from su_multi_arm.providers.base import SchemaViolation  # noqa: E402
from su_multi_arm.runtime.acl import Principal  # noqa: E402
from su_multi_arm.sql_surface.planner import SqlDraft  # noqa: E402

import typed_support as ts  # noqa: E402

FAKE = {"provider": "openai_compatible", "model": "fake"}
COUNT_Q = "How many open tickets are in module Billing?"
SUB_Q = "Count the tickets whose status is open and whose module is Billing."
COUNT_SQL = "SELECT count(*) AS n FROM ticket WHERE status = 'open' AND module = 'Billing'"
TYPED = {"route": "typed", "sub_question": SUB_Q, "reason": "A count over the ticket table."}
RETRIEVAL = {"route": "retrieval", "sub_question": None, "reason": "An explanation."}
LEXICAL = {"arm": "lexical", "query": "Billing tickets"}
IR_STEP = {"analysis": "Count open Billing tickets.", "answerability": "answerable", "gaps": [], "query": {"grain": "ticket", "measure": {"kind": "count"}, "where": [
    {"table": "ticket", "predicate": {"field": "status", "op": "eq", "value": "open"}},
    {"table": "ticket", "predicate": {"field": "module", "op": "eq", "value": "Billing"}},
]}}
ALPHA = Principal(id="alpha-reader", tenant="t_alpha", scopes=["search", "answer"])
BETA = Principal(id="beta-reader", tenant="t_beta", scopes=["search", "answer"])


def sql(text: str) -> dict:
    return {"thought": "Count rows.", "answerability": "answerable", "gaps": [], "sql_code": text, "response": "Counts the matching tickets."}


BAD = sql("SELECT count(*) FROM ticket WHERE colour = 'red'")  # unknown column: repairable, then refused


def config_data(tmp_path, *, mode="sql_surface", routing="router", typed=None, decision=None, budgets=None, arm=None, enabled=True):
    data = ts.config_dict(tmp_path / "state")
    typed_cfg = {"enabled": enabled, **({"routing": routing} if enabled else {}), **(typed or {})}
    data["pipeline"]["decision"] = {"kind": "llm", "typed_query": typed_cfg, **(decision or {})}
    data["pipeline"]["arms"]["typed_query"].update({"mode": mode, **(arm or {})})
    data.setdefault("models", {}).setdefault("roles", {}).update({"planner": FAKE, "generator": FAKE})
    if budgets:
        data["budgets"] = budgets
    return data


def engine_for(tmp_path, **kwargs):
    engine = Engine(validate_config(config_data(tmp_path, **kwargs)))
    engine.ingest(ts.documents())
    return engine


@pytest.fixture
def fake(monkeypatch):
    """``route``: router replies in order (dicts or exceptions); ``steps``: the planner's steps;
    ``sql``/``ir``: SQL-writer / typed IR replies in order; ``count``: the generator's claimed count."""
    seen: dict = {"typed_router": [], "planner": [], "sql_surface": [], "typed_ir": [], "generator": [], "order": [], "schemas": {}, "systems": {}, "roles": {},
                  "route": [TYPED], "steps": [LEXICAL], "sql": [sql(COUNT_SQL)], "ir": [IR_STEP], "count": None}

    def respond(self, system, payload, schema, budget, stage):
        budget.reserve()
        seen[stage].append(payload)
        seen["order"].append(stage)
        seen["schemas"][stage] = schema
        seen["systems"][stage] = system
        seen["roles"][stage] = self.config.model
        try:
            if stage == "typed_router":
                assert schema is TypedRoute
                reply = seen["route"].pop(0)
                if isinstance(reply, Exception):
                    raise reply
                return schema.model_validate(reply)
            if stage == "planner":
                assert issubclass(schema, LlmPlan)
                return schema.model_validate({"steps": seen["steps"]})
            if stage in {"sql_surface", "typed_ir"}:
                assert (schema is SqlDraft) == (stage == "sql_surface")
                reply = seen["sql" if stage == "sql_surface" else "ir"].pop(0)
                if isinstance(reply, Exception):
                    raise reply
                return schema.model_validate(reply)
            typed = [row for row in payload["evidence"] if row["source"] == "structured"]
            cited = typed[0]["id"] if typed else payload["evidence"][0]["id"]
            if typed and seen["count"] is not None:
                return Answer(answer=f"There is {seen['count']} matching ticket [{cited}].", claims=[Claim(text=f"{seen['count']} matching ticket", kind="count", evidence_ids=[cited])], citations=[cited])
            return Answer(answer=f"See the evidence [{cited}].", citations=[cited])
        finally:
            budget.record({"stage": stage, "provider": "fake", "model": "fake", "success": True, "usage": {"reported": True, "input_tokens": 100, "output_tokens": 10}})

    monkeypatch.setattr(JsonModel, "call", respond)
    return seen


def decision_of(result):
    return result.trace["decision"]


def assert_calls(order, expected):
    """The router chain runs concurrently with the planner (``typed_query.parallel``): the calls are
    the expected ones, the router precedes its SQL writer / typed IR call and the generator is last;
    the planner's position relative to the chain is not fixed."""
    assert sorted(order) == sorted(expected), order
    chain = [stage for stage in order if stage in {"typed_router", "sql_surface", "typed_ir"}]
    assert chain == [stage for stage in expected if stage in {"typed_router", "sql_surface", "typed_ir"}], order
    if "generator" in expected:
        assert order[-1] == "generator", order


# --- the planner request is unchanged under the router -------------------------------------------


def _wire(monkeypatch, router_reply):
    """Real provider behind ``httpx.MockTransport``: returns the captured request bodies."""
    bodies: list[tuple[str, bytes]] = []
    original = httpx.Client

    def handler(request):
        text = request.content.decode()
        if "typed_catalog" in text:
            bodies.append(("typed_router", request.content))
            reply, usage = router_reply, {"prompt_tokens": 300, "completion_tokens": 20}
        elif "Write one typed query" in text:
            bodies.append(("typed_ir", request.content))
            reply, usage = IR_STEP, {"prompt_tokens": 600, "completion_tokens": 40}
        elif "sql_code" in text:
            bodies.append(("sql_surface", request.content))
            reply, usage = sql(COUNT_SQL), {"prompt_tokens": 700, "completion_tokens": 40}
        else:
            bodies.append(("planner", request.content))
            reply, usage = {"steps": [LEXICAL]}, {"prompt_tokens": 500, "completion_tokens": 30}
        return httpx.Response(200, json={"model": "fake", "choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}], "usage": usage})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    return bodies


@pytest.mark.parametrize("mode", ["sql_surface", "ir"])
@pytest.mark.parametrize("reply", [RETRIEVAL, TYPED])
def test_planner_request_is_byte_identical_to_a_deployment_without_typed_queries(tmp_path, monkeypatch, mode, reply):
    bodies = _wire(monkeypatch, reply)
    routed = engine_for(tmp_path / "routed", mode=mode)
    routed.search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    plain = engine_for(tmp_path / "plain", mode=mode, enabled=False)
    plain.search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    planner = [body for stage, body in bodies if stage == "planner"]
    assert len(planner) == 2 and planner[0] == planner[1]
    assert b"typed_query" not in planner[0] and b"typed_catalog" not in planner[0]
    stages = [stage for stage, _ in bodies]
    chain = [stage for stage in stages if stage != "planner"]
    assert chain[0] == "typed_router" and stages.count("planner") == 2  # the router chain runs concurrently with the planner
    if reply is TYPED:
        assert chain[1] == ("sql_surface" if mode == "sql_surface" else "typed_ir")  # the typed route follows its router call


def test_router_payload_is_the_question_filters_and_a_compact_catalog(tmp_path, fake):
    fake["route"] = [RETRIEVAL]
    engine_for(tmp_path).search(SearchRequest(query=COUNT_Q, filters={"status": "open"}), principal=ALPHA)
    [payload] = fake["typed_router"]
    assert set(payload) == {"question", "request_filters", "typed_catalog"}
    assert payload["question"] == COUNT_Q and payload["request_filters"] == {"status": "open"}
    catalog = payload["typed_catalog"]
    assert set(catalog) == {"tables"} and set(catalog["tables"]) == {"ticket", "comment", "asset"}
    assert isinstance(catalog["tables"]["ticket"], list) and {"status", "module", "hours"} <= set(catalog["tables"]["ticket"])
    text = json.dumps(catalog)
    assert "Billing" not in text and '"open"' not in text and "samples" not in text and "t_beta" not in text  # no values, no samples
    schema = TypedRoute.model_json_schema()
    assert list(schema["properties"]) == ["route", "sub_question", "reason"]  # tiny schema


# --- route typed ---------------------------------------------------------------------------------


def test_route_typed_sql_executes_first_and_its_evidence_leads(tmp_path, fake):
    fake["count"] = 1
    engine = engine_for(tmp_path)
    result = engine.answer(SearchRequest(query=COUNT_Q), principal=ALPHA)
    assert_calls(fake["order"], ["typed_router", "sql_surface", "planner", "generator"])
    assert SUB_Q in fake["sql_surface"][0] and "typed_query" not in fake["planner"][0]["available_arms"]
    decision = result.retrieval.trace["decision"]
    assert decision["outcome"] == "decided" and decision["dropped_steps"] == []
    steps = result.retrieval.trace["steps"]
    assert steps[0]["arm"] == "typed_query" and steps[0]["typed_router"] is True and steps[0]["sql_route"]["status"] == "executed"
    assert [s["arm"] for s in steps[1:]] == ["lexical"]  # the planner's retrieval still ran
    evidence = fake["generator"][0]["evidence"]
    assert evidence[0]["source"] == "structured" and json.loads(evidence[0]["passage"])["value"] == 1
    assert any(row["source"] != "structured" for row in evidence[1:])  # retrieval evidence follows
    assert result.trace["evidence"]["leading_ids"] == [evidence[0]["id"]]
    assert result.contract == "passed" and [c.kind for c in result.answer.claims] == ["count"]


def test_route_typed_ir_executes_first_and_its_evidence_leads(tmp_path, fake):
    engine = engine_for(tmp_path, mode="ir")
    result = engine.answer(SearchRequest(query=COUNT_Q), principal=ALPHA)
    assert_calls(fake["order"], ["typed_router", "typed_ir", "planner", "generator"])
    section = fake["typed_ir"][0]["typed_query"]
    assert fake["typed_ir"][0]["question"] == SUB_Q and section["measures"] and "rule" in section
    [aggregate] = result.retrieval.aggregates
    assert aggregate["typed_query"]["grain"] == "ticket" and aggregate["value"] == 1 and aggregate["exactness"] == "exact"
    assert result.retrieval.trace["steps"][0]["typed_router"] is True and "typed" in result.retrieval.trace["steps"][0]
    evidence = fake["generator"][0]["evidence"]
    assert evidence[0]["source"] == "structured" and json.loads(evidence[0]["passage"])["arm"] == "typed_query"


def test_route_typed_acl_is_the_requests(tmp_path, fake):
    fake["route"] = [TYPED, TYPED]
    fake["sql"] = [sql("SELECT sum(hours) AS h FROM ticket"), sql("SELECT sum(hours) AS h FROM ticket")]
    engine = engine_for(tmp_path)
    alpha = engine.search(SearchRequest(query="What is the total hours of tickets?"), principal=ALPHA)
    beta = engine.search(SearchRequest(query="What is the total hours of tickets?"), principal=BETA)
    assert (alpha.aggregates[0]["value"], beta.aggregates[0]["value"]) == (6.5, 7.5)


def test_a_qualifier_the_sub_question_dropped_is_bounded(tmp_path, fake):
    fake["sql"] = [sql("SELECT count(*) FROM ticket WHERE status = 'open'")]  # module Billing dropped
    result = engine_for(tmp_path).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    [aggregate] = result.aggregates
    assert aggregate["exactness"] == "bounded" and any(u["reason"] == "qualifier_not_applied" for u in aggregate["unsupported"])
    assert any(u.reason == "qualifier_not_applied" for u in result.unsupported_constraints)


# --- route retrieval and failures ------------------------------------------------------------------


def test_route_retrieval_runs_the_planner_only(tmp_path, fake):
    fake["route"] = [RETRIEVAL]
    result = engine_for(tmp_path).search(SearchRequest(query="Why do Billing tickets stay open?"), principal=ALPHA)
    assert_calls(fake["order"], ["typed_router", "planner"])
    assert fake["sql_surface"] == []
    assert [s["arm"] for s in result.trace["steps"]] == ["lexical"] and result.aggregates == []
    assert decision_of(result)["dropped_steps"] == []


@pytest.mark.parametrize("mode,stage,replies,reason", [
    ("sql_surface", "sql", [BAD, BAD], "sql_refused"),
    ("sql_surface", "sql", [sql("SELECT count(*) FROM ticket WHERE module = 'Payroll'")], "sql_unsupported"),
    ("sql_surface", "sql", [{"thought": "x", "answerability": "not_answerable", "gaps": ["no such column"], "sql_code": None, "response": ""}], "planner_not_answerable"),
    # the one repair round also covers a schema violation (2026-11-08): two violations are needed
    ("sql_surface", "sql", [SchemaViolation("bad", raw="{}", paths=["sql_code"]), SchemaViolation("bad", raw="{}", paths=["sql_code"])], "sql_error"),
    ("ir", "ir", [{"analysis": "x", "answerability": "not_answerable", "gaps": ["no such column"], "query": None}], "planner_not_answerable"),
    ("ir", "ir", [{**IR_STEP, "query": {"grain": "ticket", "measure": {"kind": "count"}, "where": [{"table": "ticket", "predicate": {"field": "module", "op": "eq", "value": "Payroll"}}]}}], "step_not_executable"),
    ("ir", "ir", [SchemaViolation("bad", raw="{}", paths=["query"])], "step_not_executable"),
])
def test_a_typed_route_that_does_not_execute_is_dropped_and_the_planner_runs(tmp_path, fake, mode, stage, replies, reason):
    fake[stage] = list(replies)
    result = engine_for(tmp_path, mode=mode).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    decision = decision_of(result)
    assert decision["outcome"] == "decided" and [d["reason"] for d in decision["dropped_steps"]] == [reason]
    assert [s["arm"] for s in result.trace["steps"]] == ["lexical"] and result.aggregates == []
    reasons = {u.reason for u in result.unsupported_constraints}
    assert "aggregate_intent_without_scope" in reasons  # no count may rest on the dropped route
    assert "no such column" not in json.dumps(decision)  # model text never reaches the record
    assert "typed_router" in fake["order"] and "planner" in fake["order"]


def test_a_router_error_is_recorded_and_the_planner_runs(tmp_path, fake):
    fake["route"] = [SchemaViolation("bad", raw="{}", paths=["route"])]
    result = engine_for(tmp_path).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    decision = decision_of(result)
    assert [d["reason"] for d in decision["dropped_steps"]] == ["typed_router_error"] and "schema_violation" in decision["dropped_steps"][0]["detail"]
    assert sorted(fake["order"]) == ["planner", "typed_router"] and [s["arm"] for s in result.trace["steps"]] == ["lexical"]


def test_a_router_budget_failure_is_recorded_and_the_planner_runs(tmp_path, fake):
    from su_multi_arm.core.errors import BudgetExhausted

    fake["route"] = [BudgetExhausted("llm_calls", 1, 0)]
    result = engine_for(tmp_path).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    [dropped] = decision_of(result)["dropped_steps"]
    assert dropped["reason"] == "typed_router_error" and "budget_exhausted" in dropped["detail"]
    assert decision_of(result)["outcome"] == "decided" and [s["arm"] for s in result.trace["steps"]] == ["lexical"]


def test_router_reply_coherence():
    with pytest.raises(ValidationError):
        TypedRoute.model_validate({"route": "typed", "sub_question": None, "reason": ""})
    with pytest.raises(ValidationError):
        TypedRoute.model_validate({"route": "retrieval", "sub_question": "x", "reason": ""})


ABSTAINING_PLAN = [{"arm": "lexical", "query": "x", "filters": [{"field": "no_such_field", "value": "x"}]}]


@pytest.mark.parametrize("steps,failure", [(ABSTAINING_PLAN, "invalid_step"), ([{"arm": "no_such_arm", "query": "x"}], "schema_violation")])
def test_an_executed_route_is_kept_when_the_planner_abstains_on_an_aggregate_question(tmp_path, fake, steps, failure):
    """2026-11-08 parity fix (was: discarded). The planner's reply fails, the routed count executed:
    the typed result is the plan, the abstention is recorded, and the generator answers from it."""
    fake["steps"] = steps
    fake["count"] = 1
    result = engine_for(tmp_path).answer(SearchRequest(query=COUNT_Q), principal=ALPHA)
    decision = result.retrieval.trace["decision"]
    assert decision["outcome"] == "decided" and decision["policy_applied"] == "typed_route_only"
    assert decision["failure"] == failure and "planner abstained" in decision["note"]
    assert [d["reason"] for d in decision["dropped_steps"]] == []
    assert [s["arm"] for s in result.retrieval.trace["steps"]] == ["typed_query"]
    [aggregate] = result.retrieval.aggregates
    assert aggregate["value"] == 1 and aggregate["exactness"] == "exact"
    assert result.contract == "passed" and [c.kind for c in result.answer.claims] == ["count"]


def test_an_executed_route_is_discarded_when_the_planner_abstains_without_an_aggregate(tmp_path, fake):
    fake["steps"] = ABSTAINING_PLAN
    result = engine_for(tmp_path).search(SearchRequest(query="Show the Billing ticket owner"), principal=ALPHA)
    decision = decision_of(result)
    assert decision["outcome"] == "abstain" and decision["dropped_steps"][-1]["reason"] == "typed_route_discarded"
    assert result.aggregates == [] and result.trace["steps"] == []


def test_a_caller_typed_aggregate_never_keeps_the_route_over_an_abstention(tmp_path, fake):
    from su_multi_arm.models import Aggregate

    fake["steps"] = ABSTAINING_PLAN
    result = engine_for(tmp_path).search(SearchRequest(query=COUNT_Q, aggregate=Aggregate(operation="count")), principal=ALPHA)
    assert decision_of(result)["outcome"] == "abstain"


# --- tenant canary through the routed path --------------------------------------------------------

OWNED = {t: {d.id for d in ts.documents() if d.metadata.get("tenant") == t} for t in ts.TENANTS}
ALL_IDS = {d.id for d in ts.documents()}


def _canary_sql(rng: random.Random, principal: str) -> str:
    other = rng.choice([t for t in (*ts.TENANTS, ts.CANARY) if t != principal])
    table = rng.choice(["ticket", "comment", "asset"])
    return rng.choice([
        f"SELECT id, tenant, hours FROM {table} WHERE tenant = '{other}'",
        f"SELECT id, tenant FROM {table} WHERE tenant <> '{principal}' OR 1 = 1",
        f"SELECT count(*), sum(hours) FROM {table} WHERE 1 = 1",
        f"SELECT tenant, count(*) FROM {table} GROUP BY tenant",
        f"SELECT c.id, t.tenant FROM comment c LEFT JOIN ticket t ON c.parent_id = t.id WHERE t.tenant = '{other}' OR 1 = 1",
        f"SELECT id FROM {table} WHERE id IN (SELECT id FROM {table} WHERE tenant = '{other}')",
        f"SELECT max(hours) FROM {table}",
    ])


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def test_tenant_canary_through_the_router(tmp_path, fake):
    """300 routed requests (seeded) with the guard's ACL binding off: the per-tenant file and the row
    check keep every other tenant's ids, values and aggregates out of results and generator evidence."""
    rng = random.Random(20261108)
    engine = engine_for(tmp_path, arm={"sql_surface": {"repair_attempts": 0}})
    engine.pipelines["default"].arms["typed_query"].inject_acl = False
    executed = 0
    for _ in range(300):
        tenant = rng.choice(ts.TENANTS)
        principal = ALPHA if tenant == "t_alpha" else BETA
        statement = _canary_sql(rng, tenant)
        fake["route"], fake["sql"] = [TYPED], [sql(statement)]
        result = engine.search(SearchRequest(query="How many tickets are there per tenant?"), principal=principal)
        foreign = ALL_IDS - OWNED[tenant]
        for aggregate in result.aggregates:
            executed += 1
            for cell in _strings([r["values"] for r in aggregate["rows"]]):
                assert cell not in {*ts.TENANTS, ts.CANARY} - {tenant}, (statement, cell)
                assert cell not in foreign, (statement, cell)
            assert set(aggregate["document_ids"]) <= OWNED[tenant], statement
            for row in aggregate["rows"]:
                for value in row["values"].values():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        assert value < 99.0, (statement, value)  # the canary's 99.0 hours
        assert not ({h.document_id for h in result.hits} & foreign), statement
    assert executed >= 100


# --- metering and budgets ------------------------------------------------------------------------


def test_the_router_call_is_metered(tmp_path, fake):
    result = engine_for(tmp_path).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    assert [c["stage"] for c in result.trace["model_calls"]] == ["typed_router", "sql_surface", "planner"]


def test_router_metering_through_the_real_provider(tmp_path, monkeypatch):
    bodies = _wire(monkeypatch, TYPED)
    result = engine_for(tmp_path).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    calls = {c["stage"]: c for c in result.trace["model_calls"]}
    assert_calls([stage for stage, _ in bodies], ["typed_router", "sql_surface", "planner"])
    assert (calls["typed_router"]["input_tokens"], calls["typed_router"]["output_tokens"]) == (300, 20)
    assert result.aggregates[0]["value"] == 1


def test_budget_validation_counts_router_writer_repair_planner_and_generator(tmp_path):
    with pytest.raises(ConfigError, match="needs 5 model calls per answer"):
        validate_config(config_data(tmp_path, budgets={"max_llm_calls": 4}))
    validate_config(config_data(tmp_path, budgets={"max_llm_calls": 5}))
    validate_config(config_data(tmp_path, budgets={"max_llm_calls": 4}, arm={"sql_surface": {"repair_attempts": 0}}))
    with pytest.raises(ConfigError, match="needs 4 model calls per answer"):
        validate_config(config_data(tmp_path, mode="ir", budgets={"max_llm_calls": 3}))


def test_budget_exhaustion_on_the_router_call_is_recorded(tmp_path, fake):
    engine = engine_for(tmp_path)
    engine.config.budgets.max_llm_calls = 1  # lowered after validation, as when a run's budget is spent
    result = engine.search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    decision = decision_of(result)
    # the router used the only call: the SQL writer is refused by the budget (recorded drop) and so is the planner
    assert [d["reason"] for d in decision["dropped_steps"]] == ["sql_error"] and "budget_exhausted" in decision["dropped_steps"][0]["detail"]
    assert decision["outcome"] == "abstain" and decision["failure"] == "budget_exhausted"
    assert [c["stage"] for c in result.trace["model_calls"]] == ["typed_router"]


# --- configuration -------------------------------------------------------------------------------


def test_routing_configuration_is_coherent(tmp_path):
    assert TypedQueryDecision().routing == "planner" and TypedQueryDecision().router_role == "planner"
    with pytest.raises(ValidationError, match="needs typed_query.enabled"):
        TypedQueryDecision(routing="router")
    with pytest.raises(ValidationError, match="router_role is read only"):
        TypedQueryDecision(enabled=True, router_role="reviewer")
    with pytest.raises(ValidationError, match="repair_attempts"):
        TypedQueryDecision(enabled=True, routing="router", repair_attempts=1)
    with pytest.raises(ConfigError, match="models.roles.reviewer"):
        validate_config(config_data(tmp_path, typed={"router_role": "reviewer"}))


def test_router_role_is_used(tmp_path, fake):
    data = config_data(tmp_path, typed={"router_role": "reviewer"})
    data["models"]["roles"]["reviewer"] = {"provider": "openai_compatible", "model": "fake-router"}
    engine = Engine(validate_config(data))
    engine.ingest(ts.documents())
    fake["route"] = [RETRIEVAL]
    result = engine.search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    assert [c["stage"] for c in result.trace["model_calls"]] == ["typed_router", "planner"]
    assert fake["roles"]["typed_router"] == "fake-router" and fake["roles"]["planner"] == "fake"


def test_default_routing_is_unchanged(tmp_path, fake):
    """``routing: planner`` (default): no router call, the planner is offered typed_query (M6/SQL route)."""
    fake["steps"] = [{"arm": "typed_query", "typed": {"analysis": "Count.", "answerability": "answerable", "gaps": [], "question": SUB_Q}}]
    engine = engine_for(tmp_path, routing="planner")
    result = engine.search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    assert fake["typed_router"] == [] and fake["order"] == ["planner", "sql_surface"]
    assert "typed_query" in fake["planner"][0]["available_arms"] and "typed_router" not in result.trace["steps"][0]
