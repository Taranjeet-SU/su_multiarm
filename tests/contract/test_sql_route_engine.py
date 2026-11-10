"""Planner routing to the ``typed_query`` SQL route through the Engine (synthetic two-tenant corpus).

``tests/typed_support.py`` corpus; ``pipeline.arms.typed_query.mode: sql_surface`` with
``pipeline.decision.typed_query.enabled``. The planner, the SQL-writing role and the generator are
fakes patched over ``JsonModel.call`` (each reply validated with the schema the engine passes; every
call reserves and records through the meter it is given), plus one ``httpx.MockTransport`` run that
goes through the real provider and ``RunBudgetMeter``.

* the planner offers ``arm: typed_query`` with a sub-question (no IR identifiers) and the catalog;
* the sub-question reaches the SQL writer; the executed SQL becomes typed evidence with lineage,
  packed for the generator and backing an exact count through the ``AnswerContract``;
* ACL comes from execution: the arm gets the request's ACL; the same plan reads each tenant's file;
* route failures are recorded drops; ``on_step_failure`` decides (abstain default, partial,
  retrieval_only), with the recorded ``retrieval_after_dropped_steps`` fallback;
* qualifiers the executed SQL drops make the result ``bounded``;
* the SQL-writing call is metered under the stage ``sql_surface`` and counts in the run's budget;
* tenant canary through the routed path with the guard's ACL binding off: zero foreign rows;
* mode ir is unchanged (no ``mode`` key in the payload, typed drafts).
"""

from __future__ import annotations

import json
import random

import httpx
import pytest

pytest.importorskip("sqlglot")

from su_multi_arm import Engine, SearchRequest  # noqa: E402
from su_multi_arm.core.config import validate_config  # noqa: E402
from su_multi_arm.core.errors import ConfigError  # noqa: E402
from su_multi_arm.decision import LlmPlan  # noqa: E402
from su_multi_arm.models import Answer, Claim  # noqa: E402
from su_multi_arm.providers import JsonModel  # noqa: E402
from su_multi_arm.runtime.acl import Principal  # noqa: E402
from su_multi_arm.sql_surface import route as route_module  # noqa: E402
from su_multi_arm.sql_surface.planner import SqlDraft  # noqa: E402

import typed_support as ts  # noqa: E402

FAKE = {"provider": "openai_compatible", "model": "fake"}
COUNT_Q = "How many open tickets are in module Billing?"
SUB_Q = "Count the tickets whose status is open and whose module is Billing."
COUNT_SQL = "SELECT count(*) AS n FROM ticket WHERE status = 'open' AND module = 'Billing'"
ROUTE = {"arm": "typed_query", "typed": {"analysis": "Count open Billing tickets.", "answerability": "answerable", "gaps": [], "question": SUB_Q}}
ALPHA = Principal(id="alpha-reader", tenant="t_alpha", scopes=["search", "answer"])
BETA = Principal(id="beta-reader", tenant="t_beta", scopes=["search", "answer"])
OPERATOR = Principal(id="operator", scopes=["search", "answer"])


def sql(text: str, thought: str = "Count rows.") -> dict:
    return {"thought": thought, "answerability": "answerable", "gaps": [], "sql_code": text, "response": "Counts the matching tickets."}


def config_data(tmp_path, *, mode="sql_surface", decision=None, budgets=None, arm=None):
    data = ts.config_dict(tmp_path / "state")
    data["pipeline"]["decision"] = {"kind": "llm", "typed_query": {"enabled": True}, **(decision or {})}
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
    """``steps``: the planner's steps; ``sql``: SQL-writer replies in order (dicts or exceptions);
    ``count``: the count the generator claims from the first structured evidence."""
    seen: dict = {"planner": [], "sql_surface": [], "generator": [], "schemas": {}, "steps": [ROUTE], "sql": [sql(COUNT_SQL)], "count": None}

    def respond(self, system, payload, schema, budget, stage):
        budget.reserve()
        seen[stage].append(payload)
        seen["schemas"][stage] = schema
        try:
            if stage == "planner":
                assert issubclass(schema, LlmPlan)
                return schema.model_validate({"steps": seen["steps"]})
            if stage == "sql_surface":
                assert schema is SqlDraft
                reply = seen["sql"].pop(0)
                if isinstance(reply, Exception):
                    raise reply
                return schema.model_validate(reply)
            typed = [row for row in payload["evidence"] if row["source"] == "structured"]
            if typed and seen["count"] is not None:
                cited = typed[0]["id"]
                return Answer(answer=f"There is {seen['count']} matching ticket [{cited}].", claims=[Claim(text=f"{seen['count']} matching ticket", kind="count", evidence_ids=[cited])], citations=[cited])
            cited = payload["evidence"][0]["id"]
            return Answer(answer=f"See the evidence [{cited}].", citations=[cited])
        finally:
            budget.record({"stage": stage, "provider": "fake", "model": "fake", "success": True, "usage": {"reported": True, "input_tokens": 100, "output_tokens": 10}})

    monkeypatch.setattr(JsonModel, "call", respond)
    return seen


def decision_of(result):
    return result.trace["decision"]


# --- routing ------------------------------------------------------------------------------------


def test_planner_offers_the_sql_route_with_the_catalog_and_no_ir(tmp_path, fake):
    engine = engine_for(tmp_path)
    engine.search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    payload = fake["planner"][0]
    section = payload["typed_query"]
    assert "typed_query" in payload["available_arms"] and section["mode"] == "sql_surface"
    assert "ticket" in section["catalog"]["tables"] and section["catalog"]["tables"]["ticket"]["columns"]["status"]["description"]
    assert "samples" in section["catalog"]["tables"]["ticket"]["columns"]["module"] and "measures" not in section
    step = fake["schemas"]["planner"].model_json_schema()
    typed = json.dumps(step)
    assert '"question"' in typed and '"grain"' not in typed and '"where"' not in typed  # no IR identifiers on the wire


def test_count_routes_end_to_end_per_tenant(tmp_path, fake):
    fake["sql"] = [sql(COUNT_SQL) for _ in range(3)]
    engine = engine_for(tmp_path)
    alpha = engine.search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    assert decision_of(alpha)["outcome"] == "decided" and decision_of(alpha)["dropped_steps"] == []
    [step] = alpha.trace["steps"]
    assert step["arm"] == "typed_query" and step["sql_route"]["status"] == "executed" and "typed" not in step
    [aggregate] = alpha.aggregates
    assert aggregate["operation"] == "sql_aggregate" and aggregate["value"] == 1 and aggregate["exactness"] == "exact"
    assert aggregate["file_grant"] == "tenant" and aggregate["sql_surface"]["acl_applied"] is True
    assert ":p_" in aggregate["sql_surface"]["sql"] and "'Billing'" not in aggregate["sql_surface"]["sql"]
    # the sub-question (not the original question) reached the SQL writer
    assert SUB_Q in fake["sql_surface"][0] and COUNT_Q not in fake["sql_surface"][0]
    assert engine.search(SearchRequest(query=COUNT_Q), principal=BETA).aggregates[0]["value"] == 1
    assert engine.search(SearchRequest(query=COUNT_Q), principal=OPERATOR).aggregates[0]["value"] == 3  # all-rows file
    assert len(fake["planner"]) == 3 and len(fake["sql_surface"]) == 3


def test_answer_packs_sql_evidence_and_backs_an_exact_count(tmp_path, fake):
    fake["count"] = 1
    engine = engine_for(tmp_path)
    result = engine.answer(SearchRequest(query=COUNT_Q), principal=ALPHA)
    assert result.contract == "passed" and not result.answer.insufficient_evidence
    assert [c.kind for c in result.answer.claims] == ["count"]
    [typed] = [row for row in fake["generator"][0]["evidence"] if row["source"] == "structured"]
    packed = json.loads(typed["passage"])
    assert packed["arm"] == "typed_query" and packed["operation"] == "sql_aggregate" and packed["value"] == 1
    assert packed["sql_surface"]["fingerprint"] and ":p_" in packed["sql_surface"]["sql"] and "t_alpha" not in typed["passage"]
    # 2026-11-08: a scalar aggregate over one table carries its contributors (ids + total)
    lineage = packed["contributor_lineage"]
    assert lineage["scope"] == "aggregate_contributors" and lineage["total_matched_records"] == 1 and lineage["document_ids"] == ["tk-1"]
    assert lineage["truncated"] is False
    assert result.trace["evidence"]["evidence_characters"] >= len(typed["passage"])


def test_wrong_number_against_sql_evidence_is_refused(tmp_path, fake):
    fake["count"] = 3  # the cross-tenant total; the principal's exact count is 1
    engine = engine_for(tmp_path)
    result = engine.answer(SearchRequest(query=COUNT_Q), principal=ALPHA)
    assert result.contract != "passed" and any(v.rule == "answer_number_mismatch" for v in result.violations)


def test_rows_are_packed_with_citations(tmp_path, fake):
    fake["steps"] = [{"arm": "typed_query", "typed": {"analysis": "Look up tk-1.", "answerability": "answerable", "question": "What are the status and priority of ticket tk-1?"}}]
    fake["sql"] = [sql("SELECT status, priority FROM ticket WHERE id = 'tk-1'")]
    engine = engine_for(tmp_path)
    engine.answer(SearchRequest(query="What is the status and priority of tk-1?"), principal=ALPHA)
    [typed] = [row for row in fake["generator"][0]["evidence"] if row["source"] == "structured"]
    packed = json.loads(typed["passage"])
    [row] = packed["rows"]
    assert row["document_id"] == "tk-1" and row["values"] == {"status": "open", "priority": 1}
    assert ["tk-1", "ticket.status"] in row["cited"] and packed["contributor_lineage"]["scope"] == "rows_cited"


def test_a_qualifier_the_executed_sql_drops_is_bounded(tmp_path, fake):
    fake["sql"] = [sql("SELECT count(*) FROM ticket WHERE status = 'open'")]  # module Billing dropped
    engine = engine_for(tmp_path)
    result = engine.search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    [aggregate] = result.aggregates
    assert aggregate["value"] == 2 and aggregate["exactness"] == "bounded"
    assert any(u["reason"] == "qualifier_not_applied" for u in aggregate["unsupported"])
    assert any(u.reason == "qualifier_not_applied" for u in result.unsupported_constraints)


@pytest.mark.parametrize("statement", [
    "SELECT count(*) FROM ticket WHERE status = 'open' AND (module = 'Billing' OR 1 = 1)",
    "SELECT count(*) FROM ticket WHERE status = 'open' AND module NOT IN ('Billing')",
    "SELECT sum(CASE WHEN module = 'Billing' THEN 1 ELSE 0 END) FROM ticket WHERE status = 'open'",
])
def test_a_qualifier_only_mentioned_outside_a_filter_is_bounded(tmp_path, fake, statement):
    fake["sql"] = [sql(statement)]
    result = engine_for(tmp_path).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    [aggregate] = result.aggregates
    assert aggregate["exactness"] == "bounded" and any(u["reason"] == "qualifier_not_applied" for u in aggregate["unsupported"])


def test_partial_answerability_is_bounded(tmp_path, fake):
    fake["steps"] = [{"arm": "typed_query", "typed": {"analysis": "x", "answerability": "partial", "gaps": ["no severity column"], "question": SUB_Q}}]
    result = engine_for(tmp_path).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    assert result.aggregates[0]["exactness"] == "bounded"


# --- ACL at execution ----------------------------------------------------------------------------


def test_acl_comes_from_execution_not_the_decision(tmp_path, fake, monkeypatch):
    from su_multi_arm.arms.typed_query import TypedQueryArm

    seen = []
    original = TypedQueryArm.run

    def spy(self, ctx, inputs):
        seen.append((inputs.sql, list(inputs.acl)))
        return original(self, ctx, inputs)

    monkeypatch.setattr(TypedQueryArm, "run", spy)
    fake["sql"] = [sql("SELECT sum(hours) AS h FROM ticket") for _ in range(2)]
    engine = engine_for(tmp_path)
    alpha = engine.search(SearchRequest(query="What is the total hours of tickets?"), principal=ALPHA)
    beta = engine.search(SearchRequest(query="What is the total hours of tickets?"), principal=BETA)
    assert [acl[0].value for _, acl in seen] == ["t_alpha", "t_beta"] and all(s for s, _ in seen)
    assert (alpha.aggregates[0]["value"], beta.aggregates[0]["value"]) == (6.5, 7.5)
    assert alpha.aggregates[0]["sql_surface"]["acl_key"] != beta.aggregates[0]["sql_surface"]["acl_key"]
    # the planner's payload carries no SQL and no other tenant's samples
    assert "t_beta" not in json.dumps(fake["planner"][0]["typed_query"]) and "SELECT" not in json.dumps(fake["planner"][0])


def test_no_tenant_principal_reads_an_empty_grant(tmp_path, fake):
    nobody = Principal(id="nobody", scopes=["search", "answer"])
    engine = engine_for(tmp_path)
    # an empty grant observes no value: 'Billing' cannot be grounded for it (never another tenant's)
    grounded = engine.search(SearchRequest(query=COUNT_Q), principal=nobody)
    assert decision_of(grounded)["outcome"] == "abstain" and any(u.reason == "value_not_observed" for u in grounded.unsupported_constraints)
    fake["sql"] = [sql("SELECT count(*) FROM ticket")]
    result = engine.search(SearchRequest(query="How many tickets are there?"), principal=nobody)
    assert result.aggregates[0]["value"] == 0 and result.aggregates[0]["file_grant"] == "none"


# --- route failures and on_step_failure ------------------------------------------------------------


BAD = sql("SELECT count(*) FROM ticket WHERE colour = 'red'")  # unknown column: repairable


def test_refusal_abstains_by_default(tmp_path, fake):
    fake["sql"] = [BAD, BAD]  # one repair round (default), still refused
    result = engine_for(tmp_path).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    decision = decision_of(result)
    assert decision["outcome"] == "abstain" and decision["failure"] == "invalid_step"
    assert [d["reason"] for d in decision["dropped_steps"]] == ["sql_refused"]
    assert "2 model call(s)" in decision["dropped_steps"][0]["detail"] and "unknown_column" in decision["dropped_steps"][0]["detail"]
    assert result.aggregates == [] and result.hits == [] and result.trace["steps"] == []
    reasons = {u.reason for u in result.unsupported_constraints}
    assert {"sql_refused", "aggregate_intent_without_scope"} <= reasons
    assert len(fake["sql_surface"]) == 2


def test_refusal_under_partial_falls_back_to_retrieval(tmp_path, fake):
    fake["sql"] = [BAD, BAD]
    result = engine_for(tmp_path, decision={"on_step_failure": "partial"}).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    decision = decision_of(result)
    assert decision["outcome"] == "decided" and decision["planner_fallback"] == "retrieval_after_dropped_steps"
    assert [d["reason"] for d in decision["dropped_steps"]] == ["sql_refused"]
    assert {s["arm"] for s in result.trace["steps"]} <= {"lexical", "dense"} and result.trace["steps"]
    assert result.aggregates == [] and any(u.reason == "aggregate_intent_without_scope" for u in result.unsupported_constraints)


def test_partial_keeps_the_planners_other_steps(tmp_path, fake):
    fake["steps"] = [ROUTE, {"arm": "lexical", "query": "Billing tickets"}]
    fake["sql"] = [BAD, BAD]
    result = engine_for(tmp_path, decision={"on_step_failure": "partial"}).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    assert decision_of(result)["planner_fallback"] is None and [s["arm"] for s in result.trace["steps"]] == ["lexical"]


def test_retrieval_only_drops_every_structured_and_typed_step(tmp_path, fake):
    second = {"arm": "typed_query", "typed": {"analysis": "x", "answerability": "answerable", "question": "How many tickets are there?"}}
    fake["steps"] = [ROUTE, second, {"arm": "lexical", "query": "Billing tickets"}]
    fake["sql"] = [BAD, BAD, sql("SELECT count(*) FROM ticket")]
    result = engine_for(tmp_path, decision={"on_step_failure": "retrieval_only"}).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    decision = decision_of(result)
    assert sorted(d["reason"] for d in decision["dropped_steps"]) == ["sql_refused", "step_not_executable"]
    assert [s["arm"] for s in result.trace["steps"]] == ["lexical"] and result.aggregates == []


def test_writer_not_answerable_is_recorded(tmp_path, fake):
    fake["sql"] = [{"thought": "no such data", "answerability": "not_answerable", "gaps": ["no column for it"], "sql_code": None, "response": ""}]
    result = engine_for(tmp_path, decision={"on_step_failure": "partial"}).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    decision = decision_of(result)
    assert [d["reason"] for d in decision["dropped_steps"]] == ["planner_not_answerable"] and decision["planner_fallback"] == "retrieval_after_dropped_steps"
    assert "no column for it" not in json.dumps(decision)  # model text never reaches the record
    assert any(u.reason == "planner_not_answerable" for u in result.unsupported_constraints)


def test_unknown_value_is_unsupported_and_never_repaired(tmp_path, fake):
    fake["sql"] = [sql("SELECT count(*) FROM ticket WHERE module = 'Payroll'")]
    result = engine_for(tmp_path).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    decision = decision_of(result)
    assert decision["outcome"] == "abstain" and decision["failure"] == "unresolvable_value"
    assert [d["reason"] for d in decision["dropped_steps"]] == ["sql_unsupported"] and len(fake["sql_surface"]) == 1
    assert any(u.reason == "value_not_observed" for u in result.unsupported_constraints)


def test_writer_schema_failure_is_an_error_drop(tmp_path, fake):
    from su_multi_arm.providers.base import SchemaViolation

    # the one repair round covers a schema violation (2026-11-08): two violations make the error drop
    fake["sql"] = [SchemaViolation("bad reply", raw="{}", paths=["sql_code"]), SchemaViolation("bad reply", raw="{}", paths=["sql_code"])]
    result = engine_for(tmp_path, decision={"on_step_failure": "partial"}).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    decision = decision_of(result)
    assert [d["reason"] for d in decision["dropped_steps"]] == ["sql_error"] and "schema_violation" in decision["dropped_steps"][0]["detail"]
    assert any(u.reason == "sql_route_failed" for u in result.unsupported_constraints)


def test_planner_not_answerable_is_unchanged(tmp_path, fake):
    fake["steps"] = [{"arm": "typed_query", "typed": {"analysis": "x", "answerability": "not_answerable", "gaps": ["no data"], "question": None}}]
    result = engine_for(tmp_path).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    decision = decision_of(result)
    assert [d["reason"] for d in decision["dropped_steps"]] == ["planner_not_answerable"] and decision["planner_fallback"] == "retrieval_after_dropped_steps"
    assert fake["sql_surface"] == []  # nothing routed


def test_raw_all_arms_on_failure_applies(tmp_path, fake):
    fake["sql"] = [BAD, BAD]
    result = engine_for(tmp_path, decision={"on_failure": "raw_all_arms"}).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    decision = decision_of(result)
    assert decision["outcome"] == "raw_all_arms" and decision["dropped_steps"][0]["reason"] == "sql_refused"
    assert "typed_query" not in {s["arm"] for s in result.trace["steps"]}


# --- spend ---------------------------------------------------------------------------------------


def test_the_sql_call_is_metered_in_the_run_budget(tmp_path, fake):
    fake["sql"] = [BAD, sql(COUNT_SQL)]  # one repair: two metered SQL calls
    result = engine_for(tmp_path).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    stages = sorted(c["stage"] for c in result.trace["model_calls"])
    assert stages == ["planner", "sql_surface", "sql_surface"]
    assert result.aggregates[0]["value"] == 1


def with_call_limit(engine, calls):
    """The per-request call limit lowered after validation (as when a run's budget is already spent):
    validation requires room for planner + SQL writer + generator."""
    engine.config.budgets.max_llm_calls = calls
    return engine


def test_a_budget_without_room_for_the_sql_call_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="needs 3 model calls per answer"):
        validate_config(config_data(tmp_path, budgets={"max_llm_calls": 2}))
    validate_config(config_data(tmp_path, mode="ir", budgets={"max_llm_calls": 2}))  # mode ir: planner + generator


def test_budget_exhaustion_on_the_sql_call_is_a_recorded_drop(tmp_path, fake):
    engine = with_call_limit(engine_for(tmp_path, decision={"on_step_failure": "partial"}), 1)
    result = engine.search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    decision = decision_of(result)
    assert [d["reason"] for d in decision["dropped_steps"]] == ["sql_error"] and "budget_exhausted" in decision["dropped_steps"][0]["detail"]
    assert fake["sql_surface"] == [] and [c["stage"] for c in result.trace["model_calls"]] == ["planner"]
    abstained = with_call_limit(engine_for(tmp_path / "b"), 1).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    assert decision_of(abstained)["outcome"] == "abstain" and decision_of(abstained)["failure"] == "budget_exhausted"


def test_metering_through_the_real_provider(tmp_path, monkeypatch):
    """``httpx.MockTransport`` behind the openai_compatible provider: the SQL call is a ``model_call``
    with the stage ``sql_surface`` and its reported usage, counted in the run's call budget."""
    bodies = []
    original = httpx.Client

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        text = json.dumps(body)
        if "sql_code" in text:
            reply, usage = sql(COUNT_SQL), {"prompt_tokens": 700, "completion_tokens": 40}
        else:
            reply, usage = {"steps": [ROUTE]}, {"prompt_tokens": 500, "completion_tokens": 30}
        return httpx.Response(200, json={"model": "fake", "choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}], "usage": usage})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    engine = engine_for(tmp_path)
    result = engine.search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    calls = {c["stage"]: c for c in result.trace["model_calls"]}
    assert set(calls) == {"planner", "sql_surface"} and len(bodies) == 2
    assert (calls["sql_surface"]["input_tokens"], calls["sql_surface"]["output_tokens"]) == (700, 40)
    assert result.aggregates[0]["value"] == 1
    blocked = with_call_limit(engine_for(tmp_path / "b"), 1).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    assert decision_of(blocked)["failure"] == "budget_exhausted" and len(bodies) == 3  # the SQL call was never sent


# --- tenant canary through the routed path -----------------------------------------------------------

OWNED = {t: {d.id for d in ts.documents() if d.metadata.get("tenant") == t} for t in ts.TENANTS}
ALL_IDS = {d.id for d in ts.documents()}


def _canary_sql(rng: random.Random, principal: str) -> str:
    other = rng.choice([t for t in (*ts.TENANTS, ts.CANARY) if t != principal])
    table = rng.choice(["ticket", "comment", "asset"])
    return rng.choice(
        [
            f"SELECT id, tenant, hours FROM {table} WHERE tenant = '{other}'",
            f"SELECT id, tenant FROM {table} WHERE tenant <> '{principal}' OR 1 = 1",
            f"SELECT count(*), sum(hours) FROM {table} WHERE 1 = 1",
            f"SELECT tenant, count(*) FROM {table} GROUP BY tenant",
            f"SELECT c.id, t.tenant FROM comment c LEFT JOIN ticket t ON c.parent_id = t.id WHERE t.tenant = '{other}' OR 1 = 1",
            f"SELECT id FROM {table} WHERE id IN (SELECT id FROM {table} WHERE tenant = '{other}')",
            f"SELECT max(hours) FROM {table}",
            f"SELECT * FROM {table} UNION SELECT * FROM {table}",
            f"SELECT id FROM {table}; DELETE FROM {table}",
            f"SELECT id FROM main.{table}",
        ]
    )


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def test_tenant_canary_through_the_routed_path(tmp_path, fake):
    """300 routed requests (seeded), guard ACL binding off: the per-tenant file and the row check
    keep every other tenant's ids, values and aggregates out of results and generator evidence."""
    rng = random.Random(20261108)
    engine = engine_for(tmp_path, decision={"on_step_failure": "partial"}, arm={"sql_surface": {"repair_attempts": 0}})
    engine.pipelines["default"].arms["typed_query"].inject_acl = False
    executed = 0
    for _ in range(300):
        tenant = rng.choice(ts.TENANTS)
        principal = ALPHA if tenant == "t_alpha" else BETA
        statement = _canary_sql(rng, tenant)
        fake["sql"] = [sql(statement)]
        result = engine.search(SearchRequest(query="How many tickets are there per tenant?"), principal=principal)
        foreign = ALL_IDS - OWNED[tenant]
        for aggregate in result.aggregates:
            if aggregate.get("refused"):
                continue
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


# --- mode ir unchanged and configuration ------------------------------------------------------------


def test_mode_ir_payload_and_steps_are_unchanged(tmp_path, fake):
    fake["steps"] = [{"arm": "typed_query", "typed": {"analysis": "Count.", "answerability": "answerable", "query": {"grain": "ticket", "measure": {"kind": "count"}, "where": [
        {"table": "ticket", "predicate": {"field": "status", "op": "eq", "value": "open"}},
        {"table": "ticket", "predicate": {"field": "module", "op": "eq", "value": "Billing"}},
    ]}}}]
    engine = engine_for(tmp_path, mode="ir")
    result = engine.search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    section = fake["planner"][0]["typed_query"]
    assert "mode" not in section and section["measures"] and "rule" in section
    assert result.aggregates[0]["typed_query"]["grain"] == "ticket" and "sql_route" not in result.trace["steps"][0]
    assert fake["sql_surface"] == []


def test_ir_repair_attempts_are_refused_in_sql_surface_mode(tmp_path):
    data = config_data(tmp_path, decision={"typed_query": {"enabled": True, "repair_attempts": 1}})
    with pytest.raises(ConfigError, match="sql_surface.repair_attempts"):
        validate_config(data)


def test_route_failure_mapping_is_total():
    from su_multi_arm.sql_surface.planner import SqlSurfaceResult

    for status in ("not_answerable", "refused", "unsupported", "error"):
        failure = route_module.Failure(step=None, result=SqlSurfaceResult(status=status, catalog_fingerprint="x", error={"code": "timeout"} if status == "error" else None))
        assert failure.dropped().reason == route_module.DROP_REASON[status] and failure.constraints() and failure.category()
