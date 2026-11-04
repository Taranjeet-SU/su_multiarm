"""Native structured output on oversized schemas (2.0.0a3 parity fix; mocked HTTP, no network).

The real API refused the planner's plan schema as Anthropic ``output_config.format`` with HTTP 400
"compiled grammar is too large". Contracts:

* a deterministic pre-flight counts the wire schema against ``OUTPUT_FORMAT_LIMITS``; at config
  time a planner whose ``native`` is only the registry default resolves to prompt mode with a
  ``structured_output_fallback`` warning, and a declared ``native`` is a validation error;
* that 400 is the typed ``ProviderSchemaTooComplex``; a registry-default role switches once, recorded
  (call record and ``model_call`` trace event), to prompt mode; a declared ``native`` fails;
* no other 400 and no other error triggers a switch, and nothing is retried twice.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from su_multi_arm.core.config import ChatRole, ModelRoles, validate_config
from su_multi_arm.decision.llm import LlmPlan
from su_multi_arm.pipeline.validate import config_diagnostics
from su_multi_arm.providers import (
    AnthropicProvider,
    CallBudget,
    CallContext,
    CapabilityMismatch,
    JsonModel,
    ProviderRequestError,
    ProviderSchemaTooComplex,
    output_format_complexity,
    output_format_problems,
    output_format_schema,
)
from su_multi_arm.providers.accounting import RunBudgetMeter
from su_multi_arm.providers.base import OUTPUT_FORMAT_LIMITS
from su_multi_arm.runtime.budget import BudgetLimits
from su_multi_arm.runtime.context import new_context

from conftest import V2_GEN  # tests/conftest.py

KEY = "SU_TEST_PROVIDER_KEY"
GRAMMAR_400 = {"type": "error", "error": {"type": "invalid_request_error", "message": "The compiled grammar is too large for this schema."}}
OTHER_400 = {"type": "error", "error": {"type": "invalid_request_error", "message": "max_tokens: field required"}}


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv(KEY, "sk-test-not-a-real-key")


class Small(BaseModel):
    arm: str
    note: str | None = None


def wide_model(optional: int) -> type[BaseModel]:
    """A synthetic schema with ``optional`` optional union-typed properties."""
    fields = {f"f{i}": (str | None, None) for i in range(optional)}
    from pydantic import create_model

    return create_model("Wide", **fields)  # type: ignore[call-overload]


def message(text: str) -> dict:
    usage = {"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    return {"model": "claude-haiku-5-5", "type": "message", "role": "assistant", "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "usage": usage}


class Server:
    """Answers each request from a queue of ``(status, body)``; records every request body."""

    def __init__(self, *replies: tuple[int, dict]):
        self.replies, self.bodies = list(replies), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        status, body = self.replies.pop(0)
        return httpx.Response(status, json=body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def defaulted(model: str = "claude-haiku-5-5") -> ChatRole:
    return ChatRole(provider="anthropic", model=model, api_key_env=KEY, max_tokens=2048)


def declared(model: str = "claude-haiku-5-5") -> ChatRole:
    return ChatRole(provider="anthropic", model=model, api_key_env=KEY, max_tokens=2048, capabilities={"structured_output": "native"})


# --- pre-flight estimate ---------------------------------------------------------------------------


def test_complexity_counts_optional_and_union_properties_of_the_wire_schema():
    counts = output_format_complexity(output_format_schema(Small.model_json_schema()))
    assert counts == {"properties": 2, "optional_properties": 1, "union_properties": 1}
    assert output_format_problems(Small.model_json_schema(), "Small") == []
    limit = OUTPUT_FORMAT_LIMITS["union_properties"]
    assert output_format_problems(wide_model(limit).model_json_schema(), "Wide") == []  # at the ceiling: fits
    [problem] = output_format_problems(wide_model(limit + 1).model_json_schema(), "Wide")
    assert f"union_properties {limit + 1} > {limit}" in problem and "structured_output: none" in problem
    plan = output_format_complexity(output_format_schema(LlmPlan.model_json_schema()))
    assert plan["optional_properties"] > OUTPUT_FORMAT_LIMITS["optional_properties"] and plan["union_properties"] > OUTPUT_FORMAT_LIMITS["union_properties"]


def test_a_defaulted_planner_resolves_to_prompt_mode_with_a_visible_diagnostic():
    roles = ModelRoles(planner=defaulted("claude-sonnet-5-5"), generator=defaulted(), reviewer=defaulted())
    assert roles.planner.capabilities.structured_output == "none" and "LlmPlan is too complex" in roles.planner.structured_output_note
    assert roles.generator.capabilities.structured_output == "native" and roles.generator.structured_output_note is None
    assert roles.reviewer.capabilities.structured_output == "native"
    # Pairs whose native form is forced tool use keep native: no grammar is compiled for them.
    assert ModelRoles(planner=defaulted("claude-sonnet-4-5")).planner.capabilities.structured_output == "native"


def hosted(planner: dict) -> dict:
    return {
        "schema_version": 2,
        "dataset": "demo",
        "metadata_schema": {"fields": [{"name": "status", "type": "string", "filterable": True}]},
        "models": {"roles": {"planner": planner}},
        "pipeline": {"decision": {"kind": "llm"}, "arms": {"dense": {"backend": "memory"}}},
        "backends": {"memory": {"kind": "memory"}},
        "storage": {k: "memory" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
    }


def test_validate_warns_for_a_defaulted_planner_and_refuses_a_declared_native_one():
    base = {"provider": "anthropic", "model": "claude-sonnet-5-5", "api_key_env": KEY}
    config = validate_config(hosted(base))
    assert config.models.roles.planner.capabilities.structured_output == "none"
    [warning] = [d for d in config_diagnostics(config) if d.code == "structured_output_fallback"]
    assert warning.severity == "warning" and warning.path == "models.roles.planner.capabilities.structured_output"
    with pytest.raises(Exception, match="LlmPlan is too complex for output_config.format"):
        validate_config(hosted({**base, "capabilities": {"structured_output": "native"}}))
    # A dumped and reloaded config declares the resolved mode and is equal to the original.
    reloaded = validate_config(config.model_dump(mode="json"))
    assert reloaded == config and reloaded.models.roles.planner.capabilities.structured_output == "none"


# --- run time --------------------------------------------------------------------------------------


def test_the_grammar_400_is_a_typed_error_and_a_declared_native_role_does_not_switch():
    server = Server((400, GRAMMAR_400))
    budget = CallBudget(5)
    with pytest.raises(ProviderSchemaTooComplex) as raised:
        AnthropicProvider(declared(), transport=server.transport).complete_json("S", {"q": 1}, Small, budget, CallContext("planner"))
    assert raised.value.category == "schema_too_complex" and raised.value.status == 400
    assert "compiled grammar" not in str(raised.value)  # the body is never echoed
    assert len(server.bodies) == 1 and [c["error_category"] for c in budget.calls] == ["schema_too_complex"]


def test_a_defaulted_role_switches_once_to_prompt_mode_and_records_it():
    server = Server((400, GRAMMAR_400), (200, message('{"arm": "dense"}')), (200, message('{"arm": "lexical"}')))
    budget = CallBudget(5)
    provider = AnthropicProvider(defaulted(), transport=server.transport)
    reply, _ = provider.complete_json("S", {"q": 1}, Small, budget, CallContext("planner"))
    assert reply.arm == "dense" and len(server.bodies) == 2
    first, second = server.bodies
    assert "output_config" in first and "output_config" not in second and "Return only one JSON object" in second["system"]
    assert [c["mode"] for c in budget.calls] == ["native:output_format", "prompt"]
    assert budget.calls[0]["error_category"] == "schema_too_complex" and "structured_output_fallback" not in budget.calls[0]
    fallback = budget.calls[1]["structured_output_fallback"]
    assert fallback == {"from": "native:output_format", "to": "prompt", "reason": "schema_too_complex", "schema": "Small"}
    # The next call with the same schema goes straight to prompt mode (no second 400) and says so.
    again, _ = provider.complete_json("S", {"q": 2}, Small, budget, CallContext("planner"))
    assert again.arm == "lexical" and len(server.bodies) == 3 and "output_config" not in server.bodies[2]
    assert budget.calls[2]["structured_output_fallback"]["reason"] == "schema_too_complex"


def test_another_400_or_a_failed_prompt_call_is_never_retried():
    server = Server((400, OTHER_400))
    budget = CallBudget(5)
    with pytest.raises(ProviderRequestError) as raised:
        AnthropicProvider(defaulted(), transport=server.transport).complete_json("S", {}, Small, budget, CallContext("planner"))
    assert not isinstance(raised.value, ProviderSchemaTooComplex) and len(server.bodies) == 1
    server = Server((400, GRAMMAR_400), (400, GRAMMAR_400))
    with pytest.raises(ProviderSchemaTooComplex):
        AnthropicProvider(defaulted(), transport=server.transport).complete_json("S", {}, Small, CallBudget(5), CallContext("planner"))
    assert len(server.bodies) == 2  # one explicit switch, then the typed failure


def test_pre_flight_sends_an_oversized_schema_in_prompt_mode_without_a_native_attempt():
    wide = wide_model(OUTPUT_FORMAT_LIMITS["union_properties"] + 4)
    server = Server((200, message("{}")))
    budget = CallBudget(5)
    AnthropicProvider(defaulted(), transport=server.transport).complete_json("S", {}, wide, budget, CallContext("planner"))
    assert len(server.bodies) == 1 and "output_config" not in server.bodies[0]
    assert budget.calls[0]["mode"] == "prompt" and budget.calls[0]["structured_output_fallback"]["reason"] == "schema_too_complex_preflight"
    refused = Server()
    with pytest.raises(CapabilityMismatch, match="too complex"):
        AnthropicProvider(declared(), transport=refused.transport).complete_json("S", {}, wide, CallBudget(5), CallContext("planner"))
    assert refused.bodies == []  # before any call or reservation


def test_streamed_calls_map_the_grammar_400_too():
    server = Server((400, GRAMMAR_400))
    role = ChatRole(provider="anthropic", model="claude-haiku-5-5", api_key_env=KEY, max_tokens=2048, capabilities={"structured_output": "native", "streaming": True})
    with pytest.raises(ProviderSchemaTooComplex):
        AnthropicProvider(role, transport=server.transport).complete_json_stream("S", {}, Small, CallBudget(5), CallContext("generator"), lambda _: None)


def test_the_switch_is_in_the_model_call_trace_event():
    server = Server((400, GRAMMAR_400), (200, message('{"arm": "dense"}')))
    ctx = new_context(V2_GEN, limits=BudgetLimits(max_llm_calls=3))
    JsonModel(defaulted(), transport=server.transport).call("S", {}, Small, RunBudgetMeter(ctx, "planner", None), "planner")
    events = [e for s in ctx.trace.spans for e in s.events if e["name"] == "model_call"]
    assert [e["success"] for e in events] == [False, True] and events[0]["error_category"] == "schema_too_complex"
    assert events[0]["structured_output_fallback"] is None and events[1]["structured_output_fallback"]["to"] == "prompt"
    assert ctx.budget.snapshot()["used"]["calls"] == 2  # both calls are budgeted
