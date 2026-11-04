"""LLM planner (plan §3.7): validated plan schema, observed values under the cap only, monkeypatched call."""

from __future__ import annotations

from su_multi_arm.core.component import Dependencies, Environment
from su_multi_arm.core.config import ChatRole
from su_multi_arm.core.types import Measure
from su_multi_arm.decision import Capabilities, DecisionRequest, LlmAgent, LlmConfig, LlmPlan
from su_multi_arm.providers import JsonModel


def agent(backend, **cfg):
    return LlmAgent(LlmConfig(role=ChatRole(provider="openai_compatible", model="fake"), **cfg), Dependencies(backends={"records": backend.records}))


def test_payload_exposes_only_listable_values_under_cap_and_plan_is_compiled(v2_ctx, v2_backend, v2_schema, v2_gen, monkeypatch):
    seen = []

    def respond(self, system, payload, schema, budget, stage):
        budget.reserve()  # the real JsonModel.call reserves before the request and records after it
        seen.append((system, payload, schema, stage))
        budget.record({"stage": stage, "provider": "openai_compatible", "model": "fake", "success": True, "seconds": 0.0, "usage": {}})
        return schema.model_validate(
            {
                "steps": [
                    {"arm": "structured", "scope": {"predicates": [{"field": "status", "op": "eq", "value": "open"}], "measure": {"kind": "count"}}},
                    {"arm": "lexical", "query": "open records", "filters": {"project": "alpha"}},
                ]
            }
        )

    monkeypatch.setattr(JsonModel, "call", respond)
    caps = Capabilities(arms=["dense", "lexical", "structured"], metadata_schema=v2_schema)
    request = DecisionRequest(query="How many records have status open?", record_context={"subject": "ignored data"})
    decision = agent(v2_backend).decide(v2_ctx, request, None, caps)
    system, payload, schema, stage = seen[0]
    assert schema is LlmPlan and stage == "planner" and "DATA" in system
    assert set(payload["observed_values"]) == {"kind", "project", "status"}  # never external_id (cap 0) or tag (over cap)
    assert payload["caller_context_unverified"] == {"subject": "ignored data"} and payload["detected_intents"] == ["count"]
    assert {f["name"] for f in payload["filterable_fields"]} == {f.name for f in v2_schema.fields if f.filterable}
    assert decision.decided() and decision.plan.arms() == ["structured", "lexical"]
    assert decision.scope.measure == Measure(kind="count") and decision.scope.predicates[0].value == "open"
    assert decision.plan.steps[1].filters == {"project": "Alpha"}  # casefold-matched to the observed value
    result = v2_backend.records.execute(v2_gen, decision.scope, [])
    assert result.exactness == "exact" and result.value == 3
    events = [e for s in v2_ctx.trace.spans for e in s.events if e["name"] == "model_call"]
    assert events and events[0]["stage"] == "planner" and v2_ctx.budget.usage.used["calls"] == 1


def test_total_question_answered_with_a_count_is_refused(v2_ctx, v2_backend, v2_schema, monkeypatch):
    def respond(self, system, payload, schema, budget, stage):
        budget.reserve()
        return schema.model_validate({"steps": [{"arm": "structured", "scope": {"measure": {"kind": "count"}}}]})

    monkeypatch.setattr(JsonModel, "call", respond)
    caps = Capabilities(arms=["lexical", "structured"], metadata_schema=v2_schema)
    decision = agent(v2_backend).decide(v2_ctx, DecisionRequest(query="What is the total of all records?"), None, caps)
    assert decision.outcome == "abstain" and decision.failure == "unsupported_measure"
    assert decision.unsupported[0].reason == "measure_unsupported" and decision.scope is None


def test_explicit_caller_aggregate_cannot_be_changed_and_validate_checks_secret(v2_ctx, v2_backend, v2_schema, monkeypatch):
    def respond(self, system, payload, schema, budget, stage):
        budget.reserve()
        assert payload["explicit_aggregate"] == {"kind": "sum", "field": "amount", "unit": None, "granularity": None, "of": None}
        return schema.model_validate({"steps": [{"arm": "structured", "scope": {"measure": {"kind": "count"}}}]})

    monkeypatch.setattr(JsonModel, "call", respond)
    caps = Capabilities(arms=["lexical", "structured"], metadata_schema=v2_schema)
    decision = agent(v2_backend).decide(v2_ctx, DecisionRequest(query="amount", aggregate=Measure(kind="sum", field="amount")), None, caps)
    assert decision.outcome == "abstain" and decision.failure == "invalid_step"
    keyed = LlmAgent(LlmConfig(role=ChatRole(provider="openai_compatible", model="m", api_key_env="SU_TEST_KEY")), Dependencies())
    assert [d.code for d in keyed.validate(Environment(env={}))] == ["missing_secret"]
    assert keyed.validate(Environment(env={"SU_TEST_KEY": "x"})) == []
