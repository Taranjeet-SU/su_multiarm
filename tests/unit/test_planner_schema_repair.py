"""Explicit planner repair: bounded, metered and still checked by the scope compiler."""

import json

import pytest

from su_multi_arm.core.component import Dependencies
from su_multi_arm.core.config import ChatRole, DecisionConfig
from su_multi_arm.core.errors import BudgetExhausted
from su_multi_arm.decision import Capabilities, DecisionRequest, LlmAgent, LlmConfig
from su_multi_arm.providers import JsonModel, SchemaViolation


def make_agent(backend, attempts=1):
    return LlmAgent(LlmConfig(role=ChatRole(provider="openai_compatible", model="synthetic"), schema_repair_attempts=attempts), Dependencies(backends={"records": backend.records}))


@pytest.mark.parametrize("attempts,expected", [(0, 1), (1, 2)])
def test_repeated_invalid_replies_stop_at_the_configured_bound(v2_ctx, v2_backend, v2_schema, monkeypatch, attempts, expected, caplog):
    calls = []

    def invalid(self, system, payload, schema, budget, stage):
        budget.reserve()
        budget.record({"stage": stage, "provider": "openai_compatible", "model": "synthetic", "success": False, "seconds": 0.0, "usage": {}})
        calls.append(payload)
        raise SchemaViolation("bad plan", raw="SYNTHETIC_PRIVATE_REPLY", error_count=1, paths=["steps.0.arm (literal_error)"])

    monkeypatch.setattr(JsonModel, "call", invalid)
    result = make_agent(v2_backend, attempts).decide(v2_ctx, DecisionRequest(query="recent synthetic-provider outages"), None, Capabilities(arms=["lexical"], metadata_schema=v2_schema))
    assert result.outcome == "abstain" and result.failure == "schema_violation"
    assert len(calls) == expected and v2_ctx.budget.usage.used["calls"] == expected
    assert "SYNTHETIC_PRIVATE_REPLY" not in caplog.text
    assert "steps.0.arm" in caplog.text


@pytest.mark.parametrize("changed_filter", [False, True])
def test_repair_preserves_original_payload_and_cannot_override_filters(v2_ctx, v2_backend, v2_schema, monkeypatch, changed_filter):
    seen = []

    def respond(self, system, payload, schema, budget, stage):
        seen.append(payload)
        if len(seen) == 1:
            raise SchemaViolation("bad plan", raw='{"steps":[{"arm":"search"}]}', paths=["steps.0.arm (literal_error)"])
        assert payload["question"] == "recent synthetic-provider outages"
        assert payload["request_filters"] == {"project": "Alpha"}
        assert payload["schema_repair"]["previous_reply"] == '{"steps":[{"arm":"search"}]}'
        assert "Preserve the original question" in system
        return schema.model_validate({"steps": [{"arm": "lexical", "query": payload["question"], "filters": {"project": "Beta" if changed_filter else "Alpha"}}]})

    monkeypatch.setattr(JsonModel, "call", respond)
    result = make_agent(v2_backend).decide(v2_ctx, DecisionRequest(query="recent synthetic-provider outages", filters={"project": "Alpha"}), None, Capabilities(arms=["lexical"], metadata_schema=v2_schema))
    assert len(seen) == 2
    assert result.outcome == ("abstain" if changed_filter else "decided")
    if changed_filter:
        assert result.failure == "invalid_step"
    assert "previous_reply" not in json.dumps([s.events for s in v2_ctx.trace.spans], default=str)


def test_budget_exhaustion_during_repair_is_not_retried(v2_ctx, v2_backend, v2_schema, monkeypatch):
    calls = []

    def respond(self, system, payload, schema, budget, stage):
        calls.append(payload)
        if len(calls) == 1:
            raise SchemaViolation("bad plan", raw="{}")
        raise BudgetExhausted("calls", 1, 0)

    monkeypatch.setattr(JsonModel, "call", respond)
    result = make_agent(v2_backend).decide(v2_ctx, DecisionRequest(query="outages"), None, Capabilities(arms=["lexical"], metadata_schema=v2_schema))
    assert result.failure == "budget_exhausted" and len(calls) == 2
    assert DecisionConfig().schema_repair_attempts == 0
