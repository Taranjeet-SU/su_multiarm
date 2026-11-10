"""The ``llm`` planner's SQL route (``typed_query`` arm in ``mode: sql_surface``), decision side.

Synthetic projection (``tests/typed_support.py``) on the memory backend; the planner is a fake
provider behind ``httpx.MockTransport`` (actual wire bodies, local validation of replies). No live API.

* the wire step offers ``typed`` = ``{analysis, answerability, gaps, question}`` (no IR identifiers);
  both provider dialects convert it; IR fields from a non-enforcing provider are schema violations;
* the payload carries the same described catalog and ACL-scoped samples as mode ir, plus the route rule;
* answerable/partial -> ``TypedStep(question=...)`` (nothing compiled or executed at decision time);
  ``not_answerable`` -> the recorded M6 drop and fallback;
* ``TypedStep`` carries exactly one of ``draft`` / ``question``.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from su_multi_arm.backends.memory import MemoryBackend
from su_multi_arm.core.component import Dependencies
from su_multi_arm.core.config import ChatRole
from su_multi_arm.core.config_projection import ProjectionConfig
from su_multi_arm.core.types import GenerationId
from su_multi_arm.core.types_typed import PlannedSqlRoute, TypedQueryDraft
from su_multi_arm.decision import Capabilities, DecisionRequest, LlmAgent, LlmConfig, TypedStep
from su_multi_arm.decision.intent import detect_intents
from su_multi_arm.decision.llm import capability_plan_schema
from su_multi_arm.decision.typed_plan import SQL_ROUTE_KEY, TypedPlanning, typed_key, typed_payload
from su_multi_arm.decision.values import ValueResolver
from su_multi_arm.projection import ProjectionBuilder, ProjectionSpec
from su_multi_arm.projection.catalog import ProjectionCatalog
from su_multi_arm.providers import JsonModel
from su_multi_arm.providers.base import output_format_schema, strict_schema
from su_multi_arm.runtime.acl import Principal, StaticPolicy
from su_multi_arm.runtime.context import new_context

import typed_support as ts

GEN = GenerationId(dataset="demo", generation="d" * 32, index_format=3, fingerprint="e" * 64)
ARMS = ["dense", "lexical", "structured"]
COUNT_Q = "How many open tickets are in module Billing?"
SUB_Q = "Count the tickets whose status is open and whose module is Billing."
ROUTE = {"arm": "typed_query", "typed": {"analysis": "Count open Billing tickets.", "answerability": "answerable", "gaps": [], "question": SUB_Q}}


@pytest.fixture(scope="module")
def world():
    schema = ts.schema()
    backend = MemoryBackend()
    backend.records.upsert(GEN, ts.records(), schema)
    spec = ProjectionSpec.build(ProjectionConfig.model_validate(ts.PROJECTION), schema, "tenant")
    builder = ProjectionBuilder(spec, schema)
    builder.add(ts.records())
    files, manifest = builder.seal(GEN)
    ProjectionBuilder.write(backend.relational, GEN, files, manifest)
    catalog = ProjectionCatalog.load(backend.relational, GEN, spec)
    return backend, schema, catalog


def caps(world, mode="sql_surface"):
    _, schema, catalog = world
    return Capabilities(arms=list(ARMS), metadata_schema=schema, typed=TypedPlanning(catalog=catalog, row_limit=50, max_groups=100, mode=mode))


def agent_for(monkeypatch, world, replies, structured_output="json_schema", **cfg):
    seen: list[dict] = []
    original = JsonModel
    queue = list(replies)

    def handler(request):
        seen.append(json.loads(request.content))
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(200, json={"model": "synthetic", "choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 10}})

    monkeypatch.setattr("su_multi_arm.providers.JsonModel", lambda role: original(role, transport=httpx.MockTransport(handler)))
    role = ChatRole(provider="openai_compatible", model="synthetic", capabilities={"structured_output": structured_output})
    return LlmAgent(LlmConfig(role=role, **cfg), Dependencies(backends={"records": world[0].records})), seen


def decide(agent, capabilities, question=COUNT_Q):
    ctx = new_context(GEN, principal=Principal(id="reader", tenant="t_alpha", scopes=["search", "answer"]), acl=StaticPolicy(field="tenant", attribute="tenant"))
    intents = detect_intents(question, [], capabilities.structured_targets())
    decision = agent.decide(ctx, DecisionRequest(query=question), intents, capabilities)
    return decision, [e for s in ctx.trace.spans for e in s.events]


def test_wire_step_is_a_sub_question_without_ir(monkeypatch, world):
    agent, seen = agent_for(monkeypatch, world, [{"steps": [ROUTE]}])
    decision, events = decide(agent, caps(world))
    [step] = decision.plan.steps
    assert isinstance(step, TypedStep) and step.question == SUB_Q and step.draft is None and step.unapplied == []
    defs = seen[0]["response_format"]["json_schema"]["schema"]["$defs"]
    assert list(defs["PlannedSqlRoute"]["properties"]) == ["analysis", "answerability", "gaps", "question"]
    assert not any(name.startswith("Catalog") for name in defs)  # no IR enums on the wire
    planned = next(e for e in events if e["name"] == "typed_planner_step")
    assert planned["shape"] == "sql route sub-question"


def test_payload_has_the_catalog_samples_and_route_rule(monkeypatch, world):
    agent, seen = agent_for(monkeypatch, world, [{"steps": [ROUTE]}])
    decide(agent, caps(world))
    payload = json.loads(seen[0]["messages"][-1]["content"])
    section = payload["typed_query"]
    assert set(section) == {"mode", "catalog", "rule"} and section["mode"] == "sql_surface"
    ir = typed_payload(TypedPlanning(catalog=world[2]), world[1], ValueResolver(world[1], world[0].records, GEN, [ts_tenant("t_alpha")]))
    assert section["catalog"] == json.loads(json.dumps(ir["catalog"]))  # the same described catalog as mode ir
    assert "t_beta" not in json.dumps(section)  # ACL-scoped samples
    assert "sub-question" in section["rule"] and "Write no SQL" in section["rule"]


def ts_tenant(value):
    from su_multi_arm.core.types import Predicate

    return Predicate(field="tenant", op="eq", value=value)


def test_partial_is_bounded_and_not_answerable_is_the_recorded_drop(monkeypatch, world):
    partial = {"arm": "typed_query", "typed": {"analysis": "x", "answerability": "partial", "gaps": ["no severity column"], "question": SUB_Q}}
    agent, _ = agent_for(monkeypatch, world, [{"steps": [partial]}])
    decision, _ = decide(agent, caps(world))
    assert decision.plan.steps[0].answerability == "partial" and decision.plan.steps[0].unapplied[0].reason == "qualifier_not_applied"
    refusal = {"arm": "typed_query", "typed": {"analysis": "x", "answerability": "not_answerable", "gaps": ["no sentiment column"], "question": None}}
    agent, _ = agent_for(monkeypatch, world, [{"steps": [refusal]}])
    decision, _ = decide(agent, caps(world))
    assert [d.reason for d in decision.dropped_steps] == ["planner_not_answerable"] and decision.fallback == "retrieval_after_dropped_steps"
    assert "sentiment" not in json.dumps(decision.model_dump(mode="json"))


@pytest.mark.parametrize("bad", [
    {"arm": "typed_query", "typed": {"analysis": "x", "answerability": "answerable", "query": {"grain": "ticket"}}},  # IR in sql mode
    {"arm": "typed_query", "typed": {"analysis": "x", "answerability": "answerable", "question": None}},
    {"arm": "typed_query", "typed": {"analysis": "x", "answerability": "not_answerable", "gaps": ["g"], "question": "q"}},
    {"arm": "typed_query", "typed": {"analysis": "x", "answerability": "partial", "question": "q"}},  # no gap named
    {"arm": "typed_query", "query": "SELECT * FROM ticket"},
])
def test_invalid_route_replies_are_schema_violations(monkeypatch, world, bad):
    agent, _ = agent_for(monkeypatch, world, [{"steps": [bad]}], structured_output="none")
    decision, _ = decide(agent, caps(world))
    assert decision.outcome == "abstain" and decision.failure == "schema_violation"


def test_both_provider_dialects_convert_the_route_schema():
    schema = capability_plan_schema(("dense", "lexical", "structured"), (), SQL_ROUTE_KEY).model_json_schema()
    assert strict_schema(schema, "plan")["$defs"]["PlannedSqlRoute"]["additionalProperties"] is False
    assert output_format_schema(schema, "plan")["$defs"]["PlannedSqlRoute"]["additionalProperties"] is False


def test_typed_key_of_the_route_section(world):
    payload = typed_payload(TypedPlanning(catalog=world[2], mode="sql_surface"), world[1], ValueResolver(world[1], world[0].records, GEN, []))
    assert typed_key(payload) == SQL_ROUTE_KEY
    ir = typed_payload(TypedPlanning(catalog=world[2]), world[1], ValueResolver(world[1], world[0].records, GEN, []))
    assert typed_key(ir) != SQL_ROUTE_KEY and "mode" not in ir


def test_planned_route_and_typed_step_contracts():
    with pytest.raises(ValidationError):
        PlannedSqlRoute(analysis="x", answerability="answerable", question="  ")
    with pytest.raises(ValidationError):
        PlannedSqlRoute(analysis="x", answerability="partial", question="q", gaps=["g" * 301])
    assert PlannedSqlRoute(analysis="x", answerability="not_answerable", gaps=["g"]).question is None
    with pytest.raises(ValidationError):
        TypedStep(arm="typed_query")
    with pytest.raises(ValidationError):
        TypedStep(arm="typed_query", question="q", draft=TypedQueryDraft(grain="ticket"))
    assert TypedStep(arm="typed_query", question="q").draft is None
