"""Synthetic actual-wire contracts for capability-bound planner schemas; no live API."""
import json

import httpx
import pytest
from pydantic import ValidationError

from su_multi_arm.core.component import Dependencies
from su_multi_arm.core.config import ChatRole
from su_multi_arm.decision import Capabilities, DecisionRequest, LlmAgent, LlmConfig
from su_multi_arm.decision.llm import capability_plan_schema
from su_multi_arm.providers import JsonModel


def mocked_agent(monkeypatch, backend, reply):
    seen = []
    original = JsonModel

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "synthetic", "choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 10}})

    def factory(role):
        return original(role, transport=httpx.MockTransport(handler))

    monkeypatch.setattr("su_multi_arm.providers.JsonModel", factory)
    role = ChatRole(provider="openai_compatible", model="synthetic", capabilities={"structured_output": "json_schema"})
    return LlmAgent(LlmConfig(role=role), Dependencies(backends={"records": backend.records})), seen


def step_schema(body):
    wire = body["response_format"]["json_schema"]["schema"]
    return wire["$defs"]["CapabilityLlmStep"]["properties"]


def test_actual_neutral_wire_closes_arms_and_reserved_log_fields(v2_ctx, v2_backend, v2_schema, monkeypatch):
    agent, seen = mocked_agent(monkeypatch, v2_backend, {"steps": [{"arm": "lexical", "query": "synthetic issue"}]})
    decision = agent.decide(v2_ctx, DecisionRequest(query="Find synthetic issue evidence"), None, Capabilities(arms=["lexical", "dense"], metadata_schema=v2_schema))
    assert decision.decided()
    props = step_schema(seen[0])
    assert set(props["arm"]["enum"]) == {"dense", "lexical"}
    assert props["log_intent"]["type"] == "null" and props["log_intent"]["const"] is None
    assert props["log_args"]["maxItems"] == 0


@pytest.mark.parametrize("bad", [
    {"arm": "lexical", "query": "synthetic issue", "log_intent": "rationale_not_an_intent"},
    {"arm": "exact_id", "query": "SYN-123"},
    {"arm": "lexical", "query": "synthetic issue", "log_args": [{"field": "by", "value": "service"}]},
])
def test_non_enforcing_provider_reply_still_fails_closed(v2_ctx, v2_backend, v2_schema, monkeypatch, bad):
    agent, seen = mocked_agent(monkeypatch, v2_backend, {"steps": [bad]})
    decision = agent.decide(v2_ctx, DecisionRequest(query="Find synthetic issue evidence"), None, Capabilities(arms=["lexical"], metadata_schema=v2_schema))
    assert decision.outcome == "abstain" and decision.failure == "schema_violation"
    assert decision.plan is None and len(seen) == 1


def test_known_log_intents_remain_supported_and_wire_enum_is_closed(v2_ctx, v2_backend, monkeypatch):
    reply = {"steps": [{"arm": "structured", "log_intent": "log_top", "log_args": [{"field": "by", "value": "service"}]}]}
    agent, seen = mocked_agent(monkeypatch, v2_backend, reply)
    plan = agent.call_model(v2_ctx, {"available_arms": ["structured", "lexical"], "log_intents": {"log_top": [], "log_trend": []}})
    assert plan.steps[0].log_intent == "log_top" and plan.steps[0].log_args[0].value == "service"
    props = step_schema(seen[0])
    assert set(props["log_intent"]["enum"]) == {None, "log_top", "log_trend"}
    assert props["log_args"]["maxItems"] == 20
    with pytest.raises(ValidationError):
        type(plan).model_validate({"steps": [{"arm": "structured", "log_intent": "invented_intent"}]})


def test_capability_binding_preserves_all_original_step_coherence():
    schema = capability_plan_schema(("lexical", "graph", "structured"), ("log_top",))
    cases = [
        {"arm": "lexical", "query": "issue", "scope": {}},
        {"arm": "structured", "scope": {}, "query": "not allowed"},
        {"arm": "structured", "scope": {}, "log_intent": "log_top"},
        {"arm": "graph", "query": "issue", "template": "edge", "seeds": []},
    ]
    for step in cases:
        with pytest.raises(ValidationError):
            schema.model_validate({"steps": [step]})
