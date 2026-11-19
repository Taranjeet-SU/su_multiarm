"""Typed-query planning on the ``llm`` planner (typed-query plan §5.4/§5.8, MVP M6).

Synthetic projection (``tests/typed_support.py``) on the memory backend; the planner is a fake
provider behind ``httpx.MockTransport`` (actual wire bodies, local validation of replies) or a
patched ``LlmAgent.call_model``. No live API.

* the wire schema offers ``typed_query`` only with ``Capabilities.typed`` and enumerates only the
  catalog's described table/column/relation/operation names; both provider dialects convert it;
* an unknown name from a provider that does not enforce the schema is a ``schema_violation``;
* the payload carries reviewed descriptions and ACL-scoped samples of listable columns only;
* ``not_answerable`` is a recorded decision (dropped step, unsupported constraints), never a count;
* compile errors follow ``on_step_failure``; a contradicted request filter always fails closed;
* the opt-in repair re-plans once and is rejected when it drops a bound qualifier;
* without typed planning the planner request is byte-identical to the previous release.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

import httpx
import pytest
from pydantic import ValidationError

from su_multi_arm.backends.memory import MemoryBackend
from su_multi_arm.core.component import Dependencies
from su_multi_arm.core.config import ChatRole, DecisionConfig
from su_multi_arm.core.config_projection import ProjectionConfig
from su_multi_arm.core.types import GenerationId
from su_multi_arm.decision import Capabilities, DecisionRequest, LlmAgent, LlmConfig, TypedStep
from su_multi_arm.decision.intent import detect_intents
from su_multi_arm.decision.llm import capability_plan_schema
from su_multi_arm.decision.typed_plan import SAMPLE_CAP, TypedPlanning, typed_key, typed_payload
from su_multi_arm.decision.values import ValueResolver
from su_multi_arm.generation.contract import AnswerContract
from su_multi_arm.models import Answer, Claim
from su_multi_arm.projection import ProjectionBuilder, ProjectionSpec
from su_multi_arm.projection.catalog import ProjectionCatalog
from su_multi_arm.providers import JsonModel
from su_multi_arm.providers.base import output_format_schema, strict_schema
from su_multi_arm.runtime.acl import Principal, StaticPolicy
from su_multi_arm.runtime.context import new_context

import typed_support as ts
from conftest import V2_GEN, V2_ID_PATTERN

GEN = GenerationId(dataset="demo", generation="d" * 32, index_format=3, fingerprint="e" * 64)
ARMS = ["dense", "lexical", "structured"]
COUNT_Q = "How many open tickets are in module Billing?"
COUNT_STEP = {
    "arm": "typed_query",
    "typed": {
        "analysis": "Count ticket rows with status open and module Billing.",
        "answerability": "answerable",
        "query": {"grain": "ticket", "measure": {"kind": "count"}, "where": [
            {"table": "ticket", "predicate": {"field": "status", "op": "eq", "value": "open"}},
            {"table": "ticket", "predicate": {"field": "module", "op": "eq", "value": "Billing"}},
        ]},
    },
}


def step_with(where=None, measure=None, **typed):
    step = json.loads(json.dumps(COUNT_STEP))
    if where is not None:
        step["typed"]["query"]["where"] = where
    if measure is not None:
        step["typed"]["query"]["measure"] = measure
    step["typed"].update(typed)
    return step


def eq(field, value, table="ticket"):
    return {"table": table, "predicate": {"field": field, "op": "eq", "value": value}}


NOT_ANSWERABLE = {"arm": "typed_query", "typed": {"analysis": "No column holds customer sentiment.", "answerability": "not_answerable", "gaps": ["sentiment has no column"], "query": None}}
LEXICAL = {"arm": "lexical", "query": "billing tickets"}


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
    assert catalog is not None
    return backend, schema, catalog


def caps(world, *, typed=True, arms=ARMS, catalog=None):
    _, schema, default = world
    return Capabilities(arms=list(arms), metadata_schema=schema, typed=TypedPlanning(catalog=catalog or default, row_limit=50, max_groups=100) if typed else None)


def ctx_for(tenant="t_alpha"):
    principal = Principal(id="reader", tenant=tenant, scopes=["search", "answer"]) if tenant else Principal(id="operator", scopes=["search", "answer"])
    return new_context(GEN, principal=principal, acl=StaticPolicy(field="tenant", attribute="tenant") if tenant else None)


def wire_agent(monkeypatch, world, replies, structured_output="json_schema", **cfg):
    """An ``LlmAgent`` whose provider is a MockTransport answering ``replies`` in order."""
    backend = world[0]
    seen: list[dict] = []
    original = JsonModel
    queue = list(replies)

    def handler(request):
        seen.append(json.loads(request.content))
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(200, json={"model": "synthetic", "choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 10}})

    monkeypatch.setattr("su_multi_arm.providers.JsonModel", lambda role: original(role, transport=httpx.MockTransport(handler)))
    role = ChatRole(provider="openai_compatible", model="synthetic", capabilities={"structured_output": structured_output})
    return LlmAgent(LlmConfig(role=role, **cfg), Dependencies(backends={"records": backend.records})), seen


def decide(agent, capabilities, question, ctx=None, **request):
    ctx = ctx or ctx_for()
    intents = detect_intents(question, [], capabilities.structured_targets())
    decision = agent.decide(ctx, DecisionRequest(query=question, **request), intents, capabilities)
    events = [e for s in ctx.trace.spans for e in s.events]
    return decision, events


def user_payload(body):
    return json.loads(body["messages"][-1]["content"])


def wire_step(body):
    return body["response_format"]["json_schema"]["schema"]["$defs"]


# --- wire schema --------------------------------------------------------------------------------


def test_wire_schema_enumerates_only_catalog_names(monkeypatch, world):
    agent, seen = wire_agent(monkeypatch, world, [{"steps": [COUNT_STEP]}])
    decision, _ = decide(agent, caps(world), COUNT_Q)
    assert decision.decided() and isinstance(decision.plan.steps[0], TypedStep)
    defs = wire_step(seen[0])
    assert set(defs["CapabilityLlmStep"]["properties"]["arm"]["enum"]) == {"dense", "lexical", "structured", "typed_query"}
    catalog = world[2]
    tables = sorted(catalog.tables)
    columns = sorted({c for t in catalog.tables.values() for c in t.columns})
    assert sorted(defs["CatalogTypedQueryDraft"]["properties"]["grain"]["enum"]) == tables
    assert sorted(defs["CatalogTypedPredicate"]["properties"]["table"]["enum"]) == tables
    assert sorted(defs["CatalogPredicate"]["properties"]["field"]["enum"]) == columns
    assert sorted(defs["CatalogColumnRef"]["properties"]["column"]["enum"]) == columns
    assert set(defs["CatalogPredicate"]["properties"]["op"]["enum"]) == {"eq", "in", "range", "date_interval", "missing"}
    join = defs["CatalogTypedQueryDraft"]["properties"]["join"]
    assert {v for branch in join.get("anyOf", [join]) for v in branch.get("enum", [])} == {"comment_parent", None}
    measure = defs["CatalogMeasure"]["properties"]
    assert "share" not in measure["kind"]["enum"] and "bucket" not in measure["kind"]["enum"]
    names = {v for branch in measure["field"].get("anyOf", [measure["field"]]) for v in branch.get("enum", [])}
    assert names == set(columns) | {f"{t}.{c}" for t in tables for c in catalog.tables[t].columns} | {None}
    # ``analysis`` precedes the IR on the wire (reason, then the typed query)
    assert list(defs["CatalogPlannedTypedStep"]["properties"])[:2] == ["analysis", "answerability"]
    assert "text" in defs["CatalogTypedQueryDraft"]["properties"] and defs["CatalogTypedQueryDraft"]["properties"]["text"]["type"] == "null"


def test_undescribed_columns_are_neither_shown_nor_accepted(monkeypatch, world):
    catalog = world[2]
    hidden = catalog.model_copy(deep=True)
    hidden.tables["ticket"].columns["title_text"].description = ""
    agent, seen = wire_agent(monkeypatch, world, [{"steps": [LEXICAL]}])
    decide(agent, caps(world, catalog=hidden), COUNT_Q)
    payload = user_payload(seen[0])["typed_query"]
    assert "title_text" not in payload["catalog"]["tables"]["ticket"]["columns"]
    key = typed_key(payload)
    assert "title_text" not in key[1] and "ticket.title_text" not in key[2]


def test_both_provider_dialects_convert_the_typed_plan_schema(world):
    payload = typed_payload(TypedPlanning(catalog=world[2]), world[1], ValueResolver(world[1], world[0].records, GEN, []))
    schema = capability_plan_schema(("dense", "lexical", "structured"), (), typed_key(payload)).model_json_schema()
    strict = strict_schema(schema, "plan")
    assert strict["$defs"]["CatalogTypedQueryDraft"]["additionalProperties"] is False
    assert output_format_schema(schema, "plan")["$defs"]["CatalogPlannedTypedStep"]["additionalProperties"] is False


@pytest.mark.parametrize("bad", [
    step_with(where=[eq("severity", "high")]),  # not a catalog column
    {**COUNT_STEP, "typed": {**COUNT_STEP["typed"], "query": {"grain": "invoice", "measure": {"kind": "count"}}}},
    {**COUNT_STEP, "typed": {**COUNT_STEP["typed"], "query": {"grain": "ticket", "join": "ticket_owner", "measure": {"kind": "count"}}}},
    step_with(measure={"kind": "share", "field": "status"}),
    {"arm": "typed_query", "query": "SELECT * FROM ticket"},  # free text is never a typed plan
    {"arm": "lexical", "query": "billing", "typed": COUNT_STEP["typed"]},
    {"arm": "typed_query", "typed": {"analysis": "x", "answerability": "not_answerable", "query": None}},  # no reason given
])
def test_non_enforcing_provider_reply_with_unknown_names_is_a_schema_violation(monkeypatch, world, bad):
    agent, seen = wire_agent(monkeypatch, world, [{"steps": [bad]}], structured_output="none")
    decision, _ = decide(agent, caps(world), COUNT_Q)
    assert decision.outcome == "abstain" and decision.failure == "schema_violation"
    assert len(seen) == 1 and "response_format" not in seen[0]


def test_typed_query_is_not_offered_without_typed_planning(monkeypatch, world):
    agent, seen = wire_agent(monkeypatch, world, [{"steps": [COUNT_STEP]}], structured_output="none")
    decision, _ = decide(agent, caps(world, typed=False), COUNT_Q)
    assert decision.outcome == "abstain" and decision.failure == "schema_violation"
    payload = user_payload(seen[0])
    assert "typed_query" not in payload and "typed_query" not in payload["available_arms"]


# --- payload ------------------------------------------------------------------------------------


def test_payload_has_descriptions_and_acl_scoped_samples_of_listable_columns_only(monkeypatch, world):
    agent, seen = wire_agent(monkeypatch, world, [{"steps": [LEXICAL]}])
    decide(agent, caps(world), COUNT_Q, ctx=ctx_for("t_beta"))
    section = user_payload(seen[0])["typed_query"]
    ticket = section["catalog"]["tables"]["ticket"]["columns"]
    assert ticket["priority"]["description"].startswith("Priority 1")
    assert ticket["tenant"]["samples"] == ["t_beta"]  # ACL-scoped: never another tenant's or the canary's
    assert ticket["status"]["samples"] == ["open", "pending"]
    for column in ("id", "priority", "hours", "opened_at", "title_text"):
        assert "samples" not in ticket[column], column  # key, non-listable, high-cardinality
    assert "samples" not in section["catalog"]["tables"]["comment"]["columns"]["parent_id"]
    assert section["catalog"]["relations"]["comment_parent"] == {"from": "comment.parent_id", "to": "ticket.id", "cardinality": "many_to_one", "description": "A comment belongs to one ticket."}
    text = json.dumps(section)
    assert "t_alpha" not in text and ts.CANARY not in text and ts.INJECTION not in text
    assert "rows" not in section["catalog"]["tables"]["ticket"]  # manifest counts span every tenant
    assert all(len(c.get("samples", [])) <= SAMPLE_CAP for t in section["catalog"]["tables"].values() for c in t["columns"].values())


# --- decisions ----------------------------------------------------------------------------------


def test_answerable_step_is_validated_and_carries_the_original_draft(monkeypatch, world):
    agent, _ = wire_agent(monkeypatch, world, [{"steps": [COUNT_STEP]}])
    decision, events = decide(agent, caps(world), COUNT_Q)
    step = decision.plan.steps[0]
    assert isinstance(step, TypedStep) and step.unapplied == [] and decision.unsupported == []
    assert step.draft.grain == "ticket" and type(step.draft).__name__ == "TypedQueryDraft"
    names = {e["name"]: e for e in events}
    assert names["typed_query_validated"]["errors"] == []
    planner = names["typed_planner_step"]
    assert type(planner["analysis"]).__name__ == "Sensitive" and planner["answerability"] == "answerable"


def test_not_answerable_is_a_recorded_decision_and_never_a_count(monkeypatch, world):
    agent, _ = wire_agent(monkeypatch, world, [{"steps": [NOT_ANSWERABLE]}])
    decision, events = decide(agent, caps(world), "How many tickets mention unhappy customers?")
    assert decision.decided() and decision.fallback == "retrieval_after_dropped_steps"
    assert [d.reason for d in decision.dropped_steps] == ["planner_not_answerable"]
    assert {s.arm for s in decision.plan.steps} == {"dense", "lexical"} and not decision.plan.typed_steps()
    reasons = {u.reason for u in decision.unsupported}
    assert {"planner_not_answerable", "aggregate_intent_without_scope"} <= reasons
    assert "sentiment" not in json.dumps([d.model_dump() for d in decision.dropped_steps])  # model text stays masked
    assert next(e for e in events if e["name"] == "planner_steps_dropped")["reasons"] == ["planner_not_answerable"]
    # a count over the fallback evidence is refused by the answer contract
    answer = Answer(answer="There are 4 such tickets [h1].", claims=[Claim(text="4 tickets", kind="count", evidence_ids=["h1"])], citations=["h1"])
    checked = AnswerContract("strip").enforce(answer, evidence_ids={"h1"}, aggregates=[], intents=decision.intents, unsupported=decision.unsupported)
    assert checked.answer.insufficient_evidence and not checked.answer.claims


def test_not_answerable_without_lexical_or_dense_abstains(monkeypatch, world):
    agent, _ = wire_agent(monkeypatch, world, [{"steps": [NOT_ANSWERABLE]}])
    decision, _ = decide(agent, caps(world, arms=["structured"]), "How many tickets mention unhappy customers?")
    assert decision.outcome == "abstain" and decision.failure == "no_plan"
    assert any(u.reason == "planner_not_answerable" for u in decision.unsupported)


def test_partial_answerability_bounds_the_step(monkeypatch, world):
    partial = step_with(answerability="partial", gaps=["the question's region has no column"])
    agent, _ = wire_agent(monkeypatch, world, [{"steps": [partial]}])
    decision, _ = decide(agent, caps(world), COUNT_Q)
    step = decision.plan.steps[0]
    assert step.answerability == "partial" and step.unapplied and step.unapplied[0].reason == "qualifier_not_applied"
    assert step.unapplied[0] in decision.unsupported


def test_typed_step_missing_a_question_qualifier_is_bounded(monkeypatch, world):
    agent, _ = wire_agent(monkeypatch, world, [{"steps": [step_with(where=[eq("status", "open")])]}])
    decision, _ = decide(agent, caps(world), COUNT_Q)
    step = decision.plan.steps[0]
    assert [u.constraint_text for u in step.unapplied] == ["Billing"] and step.unapplied[0].reason == "qualifier_not_applied"


def test_compile_error_abstains_by_default_and_is_dropped_under_partial(monkeypatch, world):
    bad = step_with(where=[eq("status", "open"), eq("module", "Nowhere")])
    agent, _ = wire_agent(monkeypatch, world, [{"steps": [bad, LEXICAL]}])
    decision, _ = decide(agent, caps(world), COUNT_Q)
    assert decision.outcome == "abstain" and decision.failure == "unresolvable_value"
    assert any(u.reason == "value_not_observed" for u in decision.unsupported)
    agent, _ = wire_agent(monkeypatch, world, [{"steps": [bad, LEXICAL]}], on_step_failure="partial")
    decision, _ = decide(agent, caps(world), COUNT_Q)
    assert decision.decided() and [s.arm for s in decision.plan.steps] == ["lexical"]
    assert [(d.arm, d.reason) for d in decision.dropped_steps] == [("typed_query", "step_not_executable")]
    assert "Nowhere" not in decision.dropped_steps[0].detail
    assert {"value_not_observed", "aggregate_intent_without_scope"} <= {u.reason for u in decision.unsupported}


def test_unknown_value_from_another_tenant_is_refused_by_grounding(monkeypatch, world):
    # "Reports" exists only in t_beta: the ACL-scoped ValueResolver refuses it for t_alpha
    agent, _ = wire_agent(monkeypatch, world, [{"steps": [step_with(where=[eq("module", "Reports")])]}])
    decision, _ = decide(agent, caps(world), "How many tickets are in module Reports?", ctx=ctx_for("t_alpha"))
    assert decision.outcome == "abstain" and decision.failure == "unresolvable_value"
    decision, _ = decide(agent, caps(world), "How many tickets are in module Reports?", ctx=ctx_for("t_beta"))
    assert decision.decided()


def test_contradicting_a_request_filter_fails_closed_even_under_partial(monkeypatch, world):
    agent, _ = wire_agent(monkeypatch, world, [{"steps": [step_with(where=[eq("status", "closed")]), LEXICAL]}], on_step_failure="partial")
    decision, _ = decide(agent, caps(world), "How many closed tickets?", filters={"status": "open"})
    assert decision.outcome == "abstain" and decision.failure == "invalid_step"


def test_retrieval_only_drops_typed_steps_too(monkeypatch, world):
    bad = step_with(where=[eq("module", "Nowhere")])
    agent, _ = wire_agent(monkeypatch, world, [{"steps": [COUNT_STEP, bad, LEXICAL]}], on_step_failure="retrieval_only")
    decision, _ = decide(agent, caps(world), COUNT_Q)
    assert [s.arm for s in decision.plan.steps] == ["lexical"]
    assert [d.reason for d in decision.dropped_steps] == ["step_not_executable", "step_not_executable"]


# --- repair -------------------------------------------------------------------------------------

BAD_MEASURE = step_with(measure={"kind": "sum", "field": "status"})  # status is not numeric


def test_repair_is_off_by_default(monkeypatch, world):
    agent, seen = wire_agent(monkeypatch, world, [{"steps": [BAD_MEASURE]}, {"steps": [COUNT_STEP]}])
    decision, _ = decide(agent, caps(world), COUNT_Q)
    assert decision.outcome == "abstain" and decision.failure == "unsupported_measure" and len(seen) == 1


def test_repair_feeds_typed_errors_back_once(monkeypatch, world):
    agent, seen = wire_agent(monkeypatch, world, [{"steps": [BAD_MEASURE]}, {"steps": [COUNT_STEP]}], typed_repair_attempts=1)
    decision, events = decide(agent, caps(world), COUNT_Q)
    assert decision.decided() and isinstance(decision.plan.steps[0], TypedStep) and len(seen) == 2
    repair = user_payload(seen[1])["typed_repair"]
    assert repair["compile_errors"][0][0]["reason"] == "measure_unsupported" and repair["previous_steps"][0]["grain"] == "ticket"
    assert "did not compile" in seen[1]["messages"][0]["content"]
    statuses = [e["status"] for e in events if e["name"] == "typed_query_repair"]
    assert statuses == ["started", "accepted"]


def test_repair_cannot_drop_a_detected_qualifier(monkeypatch, world):
    dropped_billing = step_with(where=[eq("status", "open")])
    agent, seen = wire_agent(monkeypatch, world, [{"steps": [BAD_MEASURE]}, {"steps": [dropped_billing]}], typed_repair_attempts=1)
    decision, events = decide(agent, caps(world), COUNT_Q)
    assert len(seen) == 2 and decision.outcome == "abstain" and decision.failure == "unsupported_measure"
    rejected = [e for e in events if e["name"] == "typed_query_repair" and e["status"] == "rejected"]
    assert rejected and rejected[0]["reason"] == "qualifier_dropped"


def test_repair_that_drops_the_typed_step_is_rejected(monkeypatch, world):
    agent, _ = wire_agent(monkeypatch, world, [{"steps": [BAD_MEASURE]}, {"steps": [LEXICAL]}], typed_repair_attempts=1, on_step_failure="partial")
    decision, events = decide(agent, caps(world), COUNT_Q)
    assert [e.get("reason") for e in events if e["name"] == "typed_query_repair"][-1] == "typed_step_dropped"
    assert [d.reason for d in decision.dropped_steps] == ["step_not_executable"]  # the original failure, recorded


def test_repaired_step_that_still_fails_is_not_repaired_again(monkeypatch, world):
    agent, seen = wire_agent(monkeypatch, world, [{"steps": [BAD_MEASURE]}, {"steps": [BAD_MEASURE]}, {"steps": [COUNT_STEP]}], typed_repair_attempts=1)
    decision, _ = decide(agent, caps(world), COUNT_Q)
    assert len(seen) == 2 and decision.outcome == "abstain"


# --- configuration --------------------------------------------------------------------------------


def test_decision_config_typed_query_rules():
    assert DecisionConfig().typed_query.enabled is False and DecisionConfig().typed_query.repair_attempts == 0
    DecisionConfig(kind="llm", typed_query={"enabled": True, "repair_attempts": 1})
    DecisionConfig(kind="cascade", stages=["rules", "llm"], typed_query={"enabled": True})
    with pytest.raises(ValidationError, match="llm planner"):
        DecisionConfig(kind="rules", typed_query={"enabled": True})
    with pytest.raises(ValidationError, match="needs typed_query.enabled"):
        DecisionConfig(kind="llm", typed_query={"repair_attempts": 1})
    with pytest.raises(ValidationError):
        DecisionConfig(kind="llm", typed_query={"enabled": True, "repair_attempts": 2})


def test_typed_planning_needs_the_typed_query_arm(tmp_path):
    from su_multi_arm.core.config import validate_config
    from su_multi_arm.core.errors import ConfigError

    data = ts.config_dict(tmp_path / "state", typed_arm=False)
    data["pipeline"]["decision"] = {"kind": "llm", "typed_query": {"enabled": True}}
    data.setdefault("models", {}).setdefault("roles", {})["planner"] = {"provider": "openai_compatible", "model": "fake"}
    with pytest.raises(ConfigError, match="pipeline.decision.typed_query.enabled: typed_query planning needs the typed_query arm"):
        validate_config(data)


# --- defaults unchanged ---------------------------------------------------------------------------

# sha256 of the planner HTTP request bodies (json_schema, then prompt mode) for a fixed request
# without typed planning, recorded at e1e925b (before M6). Any byte change to the payload, the
# prompt or the wire schema of existing configurations fails here. Re-recorded 2026-11-09 (B5
# q043) for one intended change only: the scope draft's new optional ``min_count`` field (the
# groups' record threshold) and its description in the wire schema; the prompt is unchanged
# (previous: e04ffaeb.../20881 and cf8cf8f7.../13985). Re-recorded again for one intended change
# only: the built-in planner prompt now ends with a guide to choosing the arm and writing a structured
# scope, and eighteen worked plans (``profiles/prompts.py``, ``_PLANNER_GUIDE``); the wire schema is
# unchanged and the request grew by the same 22200 bytes in both modes (previous: 32c28137.../21445 and
# 0dffc514.../14288).
BASELINE = [
    ("979a9f3d042f23fd621cf151f870bf039a6b59c7f2356671d0e5a8047ae90765", 43645),
    ("fcebc9b82ce22101bd72c1d31b6bae049bdf16416cf27d3b51439ff3eccbb47f", 36488),
]


def test_planner_request_is_byte_identical_without_typed_planning(monkeypatch, v2_backend, v2_schema):
    seen = []
    original = JsonModel
    reply = {"steps": [{"arm": "lexical", "query": "alpha records"}]}

    def handler(request):
        seen.append(request.content)
        return httpx.Response(200, json={"model": "synthetic", "choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    monkeypatch.setattr("su_multi_arm.providers.JsonModel", lambda role: original(role, transport=httpx.MockTransport(handler)))
    question = "How many records are in project Alpha?"
    for structured_output in ("json_schema", "none"):
        role = ChatRole(provider="openai_compatible", model="synthetic", capabilities={"structured_output": structured_output})
        agent = LlmAgent(LlmConfig(role=role), Dependencies(backends={"records": v2_backend.records}))
        capabilities = Capabilities(arms=["exact_id", "dense", "lexical", "structured", "graph", "community"], metadata_schema=v2_schema, id_patterns=[V2_ID_PATTERN])
        intents = detect_intents(question, [V2_ID_PATTERN], capabilities.structured_targets())
        agent.decide(new_context(V2_GEN), DecisionRequest(query=question, filters={"kind": "record"}, as_of=date(2024, 4, 2)), intents, capabilities)
    assert [(hashlib.sha256(body).hexdigest(), len(body)) for body in seen] == BASELINE
