"""Anthropic thinking / effort control and first-token latency (2026-11-09), mocked HTTP only.

Per-model rules (registry; the thinking types and effort levels are the ones the provider's
model-capabilities endpoint reported on 2026-11-09): Sonnet 5.x cannot disable thinking, so
``thinking: off`` is sent as ``between_tools``; Haiku 5.x sends ``disabled`` and accepts ``off`` only
at effort ``high`` or lower; Haiku 4.5 sends no thinking field unless a budget is given (``enabled``)
and has no effort levels; a forced tool call (native ``tool_use``) cannot think. ``effort`` is sent as
``output_config.effort``. A streamed call records ``first_token_seconds``. Haiku 4.5 as the generator:
native forced tool use, metered at an operator price table of $1/$5 per MTok. No network here.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel, Field, ValidationError

from su_multi_arm.core.config import ChatRole
from su_multi_arm.core.types import GenerationId
from su_multi_arm.models import Answer
from su_multi_arm.providers import AnthropicProvider, CallBudget, CallContext, JsonModel, RunBudgetMeter
from su_multi_arm.providers.capabilities import check_role, lookup, thinking_wire
from su_multi_arm.runtime.budget import BudgetLimits, PriceTable
from su_multi_arm.runtime.context import new_context
from su_multi_arm.testing.provider_fakes import anthropic_sse

KEY = "SU_TEST_PROVIDER_KEY"
GEN = GenerationId(dataset="demo", generation="c" * 32, index_format=3, fingerprint="f" * 64)
HAIKU45 = PriceTable(version="operator-example", prices={"claude-haiku-4-5": {"input_per_million": 1.0, "output_per_million": 5.0}})


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv(KEY, "sk-test-not-a-real-key")


class Reply(BaseModel):
    answer: str
    citations: list[str] = Field(default_factory=list)


def role(model: str, **kw) -> ChatRole:
    return ChatRole(provider="anthropic", model=model, api_key_env=KEY, **kw)


class Recorder:
    def __init__(self, reply: dict | None = None, stream: bytes | None = None):
        self.reply, self.stream, self.bodies = reply, stream, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        if self.stream is not None:
            return httpx.Response(200, content=self.stream, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=self.reply)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def text_message(text: str, model: str) -> dict:
    usage = {"input_tokens": 1000, "output_tokens": 200, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    return {"model": model, "type": "message", "role": "assistant", "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "usage": usage}


def tool_message(payload: dict, name: str, model: str) -> dict:
    usage = {"input_tokens": 1000, "output_tokens": 200}
    return {"model": model, "type": "message", "role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": name, "input": payload}], "stop_reason": "tool_use", "usage": usage}


# --- validation per model -----------------------------------------------------------------------


def test_registry_declares_the_reported_thinking_types():
    assert lookup("anthropic", "claude-haiku-4-5").thinking_types == {"enabled", "disabled"}
    assert lookup("anthropic", "claude-haiku-5-5").thinking_types == {"adaptive", "disabled"}
    assert lookup("anthropic", "claude-sonnet-5-5").thinking_types == {"adaptive", "between_tools"}
    assert lookup("anthropic", "claude-haiku-4-5").effort_levels == ()


@pytest.mark.parametrize(
    "model, kw, wire",
    [
        ("claude-sonnet-5-5", {"thinking": "off"}, {"type": "between_tools"}),
        ("claude-sonnet-5-5", {"thinking": "adaptive", "effort": "low"}, {"type": "adaptive"}),
        ("claude-haiku-5-5", {"thinking": "off", "effort": "high"}, {"type": "disabled"}),
        ("claude-haiku-5-5", {"thinking": "adaptive", "effort": "max"}, {"type": "adaptive"}),
        ("claude-haiku-4-5", {"thinking": "off"}, None),
        ("claude-haiku-4-5", {"thinking_budget_tokens": 2048, "max_tokens": 4096, "capabilities": {"structured_output": "none"}}, {"type": "enabled", "budget_tokens": 2048}),
    ],
)
def test_accepted_settings_and_their_wire_form(model, kw, wire):
    spec = role(model, **kw)
    assert thinking_wire(lookup("anthropic", model), spec.thinking, spec.thinking_budget_tokens) == wire


@pytest.mark.parametrize(
    "model, kw, match",
    [
        ("claude-haiku-5-5", {"thinking": "off", "effort": "xhigh"}, "only at effort high or lower"),
        ("claude-haiku-5-5", {"thinking": "between_tools"}, "thinking: between_tools is not declared"),
        ("claude-sonnet-5-5", {"thinking": "enabled", "thinking_budget_tokens": 2048}, "thinking: enabled is not declared"),
        ("claude-haiku-4-5", {"thinking": "adaptive"}, "thinking: adaptive is not declared"),
        ("claude-haiku-4-5", {"effort": "low"}, "effort: low is not declared"),
        ("claude-haiku-4-5", {"thinking": "enabled", "max_tokens": 4096, "capabilities": {"structured_output": "none"}}, "needs thinking_budget_tokens"),
        ("claude-haiku-4-5", {"thinking_budget_tokens": 4096, "max_tokens": 4096, "capabilities": {"structured_output": "none"}}, "must be below max_tokens"),
        ("claude-haiku-4-5", {"thinking_budget_tokens": 2048, "max_tokens": 4096}, "forced tool call, which cannot think"),
        ("claude-sonnet-4-5", {"thinking": "off"}, "thinking: off is not declared"),
    ],
)
def test_rejected_settings_name_the_model_rule(model, kw, match):
    with pytest.raises(ValidationError, match=match):
        role(model, **kw)


def test_thinking_and_effort_are_anthropic_keys():
    with pytest.raises(ValidationError, match="Anthropic role key"):
        ChatRole(provider="openai", model="gpt-4.1-mini", api_key_env=KEY, thinking="off")
    assert check_role("openai", "gpt-5", "none", "none", effort="low")[0][0] == "effort"


def test_measure_first_token_needs_streaming():
    with pytest.raises(ValidationError, match="measure_first_token"):
        role("claude-haiku-5-5", measure_first_token=True)
    assert role("claude-haiku-5-5", measure_first_token=True, capabilities={"streaming": True}).measure_first_token


# --- request bodies -----------------------------------------------------------------------------


def test_sonnet_off_is_sent_as_between_tools_with_effort():
    rec = Recorder(text_message(json.dumps({"answer": "ok"}), "claude-sonnet-5-5"))
    budget = CallBudget(1)
    AnthropicProvider(role("claude-sonnet-5-5", thinking="off", effort="low"), transport=rec.transport).complete_json("S", {}, Reply, budget, CallContext("generator"))
    body = rec.bodies[0]
    assert body["thinking"] == {"type": "between_tools"} and body["output_config"]["effort"] == "low" and "format" in body["output_config"]
    assert budget.calls[0]["thinking"] == "between_tools" and budget.calls[0]["thinking_requested"] == "off" and budget.calls[0]["effort"] == "low"


def test_haiku55_off_is_disabled_and_default_sends_nothing():
    rec = Recorder(text_message(json.dumps({"answer": "ok"}), "claude-haiku-5-5"))
    AnthropicProvider(role("claude-haiku-5-5", thinking="off"), transport=rec.transport).complete_json("S", {}, Reply, CallBudget(1), CallContext("generator"))
    AnthropicProvider(role("claude-haiku-5-5"), transport=rec.transport).complete_json("S", {}, Reply, CallBudget(1), CallContext("generator"))
    assert rec.bodies[0]["thinking"] == {"type": "disabled"} and "effort" not in rec.bodies[0]["output_config"]
    assert "thinking" not in rec.bodies[1]


def test_haiku45_off_sends_no_thinking_field():
    rec = Recorder(tool_message({"answer": "ok"}, "Reply", "claude-haiku-4-5-20251001"))
    budget = CallBudget(1)
    AnthropicProvider(role("claude-haiku-4-5", thinking="off"), transport=rec.transport).complete_json("S", {}, Reply, budget, CallContext("generator"))
    assert "thinking" not in rec.bodies[0] and "output_config" not in rec.bodies[0] and budget.calls[0]["thinking"] == "not_sent"


# --- Haiku 4.5 as the generator -----------------------------------------------------------------


def test_haiku45_generator_native_tool_use_and_price_table_cost():
    spec = role("claude-haiku-4-5", max_tokens=2600)
    assert spec.capabilities.structured_output == "native" and AnthropicProvider(spec).structured_mode() == "native:tool_use"
    answer = {"answer": "Synthetic answer [d1].", "claims": [], "citations": ["d1"], "insufficient_evidence": False}
    rec = Recorder(tool_message(answer, "Answer", "claude-haiku-4-5-20251001"))
    ctx = new_context(GEN, limits=BudgetLimits(max_llm_calls=2, max_usd=1.0), price_table=HAIKU45)
    reply = JsonModel(spec, transport=rec.transport).call("S", {"q": "x"}, Answer, RunBudgetMeter(ctx, "generator", spec.price_key()), "generator")
    assert reply.citations == ["d1"]
    body = rec.bodies[0]
    assert body["model"] == "claude-haiku-4-5" and body["tool_choice"] == {"type": "tool", "name": "Answer"} and "thinking" not in body
    (event,) = [e for s in ctx.trace.spans for e in s.events if e["name"] == "model_call"]
    assert event["usd"] == pytest.approx((1000 * 1.0 + 200 * 5.0) / 1_000_000) and event["mode"] == "native:tool_use"


# --- first-token latency ------------------------------------------------------------------------


def test_a_streamed_call_records_first_token_seconds(monkeypatch):
    # a fake monotonic clock (every read advances 0.25 s): the timings are exact, not wall-clock
    # dependent (the test flaked under load when the 4-decimal first-token rounding crossed the total)
    import time

    ticks = iter(range(1, 1_000_000))
    monkeypatch.setattr(time, "perf_counter", lambda: next(ticks) * 0.25)
    rec = Recorder(stream=anthropic_sse(json.dumps({"answer": "ok"})))
    budget = CallBudget(1)
    spec = role("claude-haiku-5-5", thinking="off", capabilities={"streaming": True})
    AnthropicProvider(spec, transport=rec.transport).complete_json_stream("S", {}, Reply, budget, CallContext("generator"), lambda _t: None)
    call = budget.calls[0]
    assert call["stream"] is True and 0 < call["first_token_seconds"] <= call["seconds"]
    assert call["first_token_seconds"] % 0.25 == 0 and call["seconds"] % 0.25 == 0  # read from the clock, never estimated
    plain = CallBudget(1)
    AnthropicProvider(role("claude-haiku-5-5"), transport=Recorder(text_message(json.dumps({"answer": "ok"}), "claude-haiku-5-5")).transport).complete_json("S", {}, Reply, plain, CallContext("generator"))
    assert "first_token_seconds" not in plain.calls[0]
