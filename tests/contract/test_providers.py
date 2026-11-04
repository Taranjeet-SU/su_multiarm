"""Provider contracts with mocked HTTP (``httpx.MockTransport``), plan §3.6.

Hosted providers are contract-mocked only: request shapes, usage parsing, error mapping and
accounting are checked against the documented wire formats, never against a paid endpoint.
"""

from __future__ import annotations

import json

import httpx
import numpy as np
import pytest
from pydantic import BaseModel, Field

from su_multi_arm.compat.config_v1 import ModelConfig
from su_multi_arm.core.config import ChatRole, EmbedderRole
from su_multi_arm.core.errors import BudgetExhausted
from su_multi_arm.core.types import GenerationId
from su_multi_arm.decision import Capabilities, DecisionRequest, LlmAgent, LlmConfig
from su_multi_arm.core.component import Dependencies
from su_multi_arm.providers import (
    AnthropicProvider,
    CallBudget,
    CallContext,
    CapabilityMismatch,
    Embedder,
    JsonModel,
    MissingSecret,
    NoneProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
    ProviderAuthError,
    ProviderConnectionError,
    ProviderIncomplete,
    ProviderRateLimited,
    ProviderRefusal,
    ProviderRequestError,
    ProviderResponseError,
    ProviderServerError,
    ProviderTimeout,
    ProviderUnavailable,
    RunBudgetMeter,
    SchemaViolation,
    chat_provider,
)
from su_multi_arm.runtime.budget import BudgetLimits, PriceTable
from su_multi_arm.runtime.context import new_context

KEY = "SU_TEST_PROVIDER_KEY"
SECRET = "sk-test-not-a-real-key"
GEN = GenerationId(dataset="demo", generation="c" * 32, index_format=3, fingerprint="f" * 64)  # = conftest V2_GEN


class Reply(BaseModel):
    answer: str
    citations: list[str] = Field(default_factory=list)


class Loose(BaseModel):
    filters: dict[str, str] = Field(default_factory=dict)


class Recorder:
    """Mock transport that records every request and answers with a fixed response or exception."""

    def __init__(self, response=None, *, status: int = 200, raises: Exception | None = None, headers=None, text: str | None = None):
        self.requests: list[httpx.Request] = []
        self.response, self.status, self.raises, self.headers, self.text = response, status, raises, headers or {}, text

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        if self.text is not None:
            return httpx.Response(self.status, text=self.text, headers=self.headers)
        return httpx.Response(self.status, json=self.response, headers=self.headers)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def body(self, i: int = 0) -> dict:
        return json.loads(self.requests[i].content)


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv(KEY, SECRET)


def anthropic_role(**caps) -> ChatRole:
    return ChatRole(provider="anthropic", model="claude-sonnet-4-5", api_key_env=KEY, max_tokens=512, capabilities=caps)


def openai_role(model: str = "gpt-4.1-mini", **kw) -> ChatRole:
    caps = kw.pop("capabilities", {})
    return ChatRole(provider="openai", model=model, api_key_env=KEY, max_tokens=256, capabilities=caps, **kw)


def local_role(**kw) -> ChatRole:
    caps = kw.pop("capabilities", {})
    return ChatRole(provider="openai_compatible", model="local-model", base_url="http://127.0.0.1:9/v1", capabilities=caps, **kw)


def anthropic_text(text: str, stop: str = "end_turn", usage=None) -> dict:
    return {
        "model": "claude-sonnet-4-5-20250929",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop,
        "usage": usage or {"input_tokens": 10, "output_tokens": 4},
    }


def anthropic_tool(payload: dict, name: str = "Reply", stop: str = "tool_use", usage=None) -> dict:
    return {
        "model": "claude-sonnet-4-5-20250929",
        "content": [{"type": "tool_use", "id": "toolu_1", "name": name, "input": payload}],
        "stop_reason": stop,
        "usage": usage or {"input_tokens": 10, "output_tokens": 4},
    }


def chat(content: str | None, finish: str = "stop", usage=None, refusal=None) -> dict:
    message = {"role": "assistant", "content": content}
    if refusal is not None:
        message["refusal"] = refusal
    return {
        "model": "returned-model",
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": usage if usage is not None else {"prompt_tokens": 20, "completion_tokens": 5},
    }


GOOD = {"answer": "ok", "citations": ["a"]}


# --- Anthropic ---------------------------------------------------------------------------------


def test_anthropic_prompt_mode_request_shape_and_usage():
    rec = Recorder(anthropic_text("```json\n" + json.dumps(GOOD) + "\n```"))
    provider = AnthropicProvider(anthropic_role(structured_output="none"), transport=rec.transport)
    budget = CallBudget(2)
    obj, usage = provider.complete_json("SYS", {"q": "x"}, Reply, budget, CallContext("generator"))
    assert obj == Reply(**GOOD) and usage.input_tokens == 10 and usage.output_tokens == 4 and usage.reported
    request = rec.requests[0]
    assert request.url == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == SECRET and request.headers["anthropic-version"] == "2023-06-01"
    body = rec.body()
    assert body["max_tokens"] == 512 and "tools" not in body and "tool_choice" not in body
    assert isinstance(body["system"], str) and body["system"].startswith("SYS") and "matching this schema" in body["system"]
    assert body["messages"] == [{"role": "user", "content": json.dumps({"q": "x"})}]
    record = budget.calls[0]
    assert record["mode"] == "prompt" and record["success"] and record["returned_model"] == "claude-sonnet-4-5-20250929"
    assert SECRET not in json.dumps(budget.calls)


def test_anthropic_native_tool_use_with_cache_control_and_cached_usage():
    usage = {"input_tokens": 50, "output_tokens": 30, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 100}
    rec = Recorder(anthropic_tool(GOOD, usage=usage))
    provider = AnthropicProvider(anthropic_role(structured_output="native", prompt_caching="controlled"), transport=rec.transport)
    obj, got = provider.complete_json("SYS", {"q": "x"}, Reply, CallBudget(1), CallContext("planner"))
    assert obj.answer == "ok"
    body = rec.body()
    assert body["system"] == [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]
    (tool,) = body["tools"]
    assert tool["name"] == "Reply" and tool["input_schema"] == Reply.model_json_schema()
    assert tool["cache_control"] == {"type": "ephemeral"}
    assert body["tool_choice"] == {"type": "tool", "name": "Reply"}
    assert got.input_tokens == 1050 and got.cached_input_tokens == 900 and got.cache_creation_input_tokens == 100 and got.output_tokens == 30


def test_anthropic_native_without_the_tool_call_is_a_failure_not_a_text_fallback():
    rec = Recorder(anthropic_text(json.dumps(GOOD)))  # valid JSON as text, but no tool call
    provider = AnthropicProvider(anthropic_role(structured_output="native"), transport=rec.transport)
    budget = CallBudget(1)
    with pytest.raises(ProviderResponseError, match="stop_reason"):
        provider.complete_json("SYS", {}, Reply, budget, CallContext("planner"))
    rec2 = Recorder(anthropic_tool(GOOD, name="Other"))
    with pytest.raises(ProviderResponseError, match="expected exactly one"):
        AnthropicProvider(anthropic_role(structured_output="native"), transport=rec2.transport).complete_json("S", {}, Reply, CallBudget(1), CallContext("planner"))
    assert len(rec.requests) == 1 and not budget.calls[0]["success"] and budget.calls[0]["error_category"] == "bad_response"


def test_anthropic_truncation_and_refusal_are_failures_and_usage_is_still_recorded():
    rec = Recorder(anthropic_text('{"answer": "o', stop="max_tokens", usage={"input_tokens": 7, "output_tokens": 512}))
    budget = CallBudget(1)
    with pytest.raises(ProviderIncomplete, match="incomplete"):
        AnthropicProvider(anthropic_role(), transport=rec.transport).complete_json("S", {}, Reply, budget, CallContext("generator"))
    assert budget.calls[0]["usage"]["output_tokens"] == 512 and budget.calls[0]["error_category"] == "incomplete"
    rec = Recorder(anthropic_text("", stop="refusal"))
    with pytest.raises(ProviderRefusal):
        AnthropicProvider(anthropic_role(), transport=rec.transport).complete_json("S", {}, Reply, CallBudget(1), CallContext("generator"))


def test_schema_invalid_native_reply_is_a_categorised_failure_with_the_raw_reply():
    rec = Recorder(anthropic_tool({"citations": "not-a-list"}))
    model = JsonModel(anthropic_role(structured_output="native"), transport=rec.transport)
    with pytest.raises(SchemaViolation) as error:
        model.call("S", {}, Reply, CallBudget(2), "generator")
    assert error.value.category == "schema_violation" and json.loads(error.value.raw) == {"citations": "not-a-list"}
    assert isinstance(error.value, ValueError)
    # call_text returns the raw reply for the ledger; nothing is repaired and only one request was sent per call.
    assert json.loads(model.call_text("S", {}, Reply, CallBudget(1), "extraction")) == {"citations": "not-a-list"}
    assert len(rec.requests) == 2


# --- OpenAI ------------------------------------------------------------------------------------


def test_openai_native_strict_json_schema_request_shape_and_cached_usage():
    usage = {"prompt_tokens": 2000, "completion_tokens": 40, "prompt_tokens_details": {"cached_tokens": 1536}, "completion_tokens_details": {"reasoning_tokens": 0}}
    rec = Recorder(chat(json.dumps(GOOD), usage=usage))
    provider = OpenAIProvider(openai_role(capabilities={"structured_output": "native", "prompt_caching": "passive"}), transport=rec.transport)
    obj, got = provider.complete_json("SYS", {"q": "x"}, Reply, CallBudget(1), CallContext("generator"))
    assert obj.citations == ["a"] and got.cached_input_tokens == 1536 and got.input_tokens == 2000 and got.output_tokens == 40
    request = rec.requests[0]
    assert request.url == "https://api.openai.com/v1/chat/completions" and request.headers["authorization"] == f"Bearer {SECRET}"
    body = rec.body()
    assert body["max_completion_tokens"] == 256 and "max_tokens" not in body and "reasoning_effort" not in body
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True and fmt["json_schema"]["name"] == "Reply"
    schema = fmt["json_schema"]["schema"]
    assert schema["additionalProperties"] is False and schema["required"] == ["answer", "citations"]
    assert body["messages"][0] == {"role": "system", "content": "SYS"}  # no prompt-schema in native mode


def test_openai_native_with_an_unstrictable_schema_fails_before_any_request_or_reservation():
    rec = Recorder(chat("{}"))
    budget = CallBudget(1)
    provider = OpenAIProvider(openai_role(capabilities={"structured_output": "native"}), transport=rec.transport)
    with pytest.raises(CapabilityMismatch, match="json_schema"):
        provider.complete_json("S", {}, Loose, budget, CallContext("planner"))
    assert rec.requests == [] and budget.calls == [] and budget._reserved == 0


@pytest.mark.parametrize(
    "declared,expected",
    [("json_schema", {"type": "json_schema", "strict": False}), ("json_mode", {"type": "json_object"}), ("none", None)],
)
def test_openai_non_native_modes_send_the_declared_hint_and_the_prompt_schema(declared, expected):
    rec = Recorder(chat(json.dumps(GOOD)))
    provider = OpenAIProvider(openai_role(model="gpt-3.5-turbo", capabilities={"structured_output": declared}), transport=rec.transport)
    provider.complete_json("SYS", {}, Loose if declared == "json_schema" else Reply, CallBudget(1), CallContext("generator"))
    body = rec.body()
    assert "matching this schema" in body["messages"][0]["content"]
    if expected is None:
        assert "response_format" not in body
    elif expected["type"] == "json_object":
        assert body["response_format"] == {"type": "json_object"}
    else:
        assert body["response_format"]["json_schema"]["strict"] is False
        assert body["response_format"]["json_schema"]["schema"] == Loose.model_json_schema()


def test_openai_reasoning_effort_only_on_declared_models():
    rec = Recorder(chat(json.dumps(GOOD)))
    OpenAIProvider(openai_role(model="o3-mini", reasoning_effort="low"), transport=rec.transport).complete_json("S", {}, Reply, CallBudget(1), CallContext("planner"))
    assert rec.body()["reasoning_effort"] == "low"
    with pytest.raises(CapabilityMismatch, match="reasoning_effort"):
        OpenAIProvider(chat_role_unchecked(provider="openai", model="gpt-4o", reasoning_effort="low"))


def chat_role_unchecked(**kw):
    from su_multi_arm.providers import RoleSpec

    return RoleSpec(**kw)


@pytest.mark.parametrize(
    "response,error,category",
    [
        (chat('{"answer": "o', finish="length"), ProviderIncomplete, "incomplete"),
        (chat(None, refusal="cannot help"), ProviderRefusal, "refusal"),
        (chat("", finish="content_filter"), ProviderRefusal, "refusal"),
        (chat("{}", finish="tool_calls"), ProviderIncomplete, "incomplete"),
        ({"model": "m", "choices": []}, ProviderResponseError, "bad_response"),
    ],
)
def test_openai_non_final_replies_are_typed_failures(response, error, category):
    budget = CallBudget(1)
    with pytest.raises(error):
        OpenAIProvider(openai_role(), transport=Recorder(response).transport).complete_json("S", {}, Reply, budget, CallContext("generator"))
    assert budget.calls[0]["error_category"] == category and not budget.calls[0]["success"]


# --- OpenAI-compatible ------------------------------------------------------------------------


def test_openai_compatible_prompt_mode_uses_role_token_param_and_no_auth_without_key():
    rec = Recorder(chat(json.dumps(GOOD), usage={}))
    provider = OpenAICompatibleProvider(local_role(max_tokens_parameter="max_tokens"), transport=rec.transport)
    obj, usage = provider.complete_json("SYS", {}, Reply, CallBudget(1), CallContext("extraction"))
    assert obj.answer == "ok" and not usage.reported
    request = rec.requests[0]
    assert str(request.url) == "http://127.0.0.1:9/v1/chat/completions" and "authorization" not in request.headers
    body = rec.body()
    assert body["max_tokens"] == 2048 and "max_completion_tokens" not in body and "response_format" not in body


def test_openai_compatible_json_schema_only_where_declared():
    rec = Recorder(chat(json.dumps(GOOD)))
    OpenAICompatibleProvider(local_role(capabilities={"structured_output": "json_schema"}), transport=rec.transport).complete_json(
        "S", {}, Reply, CallBudget(1), CallContext("extraction")
    )
    fmt = rec.body()["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is False


def test_v1_model_config_and_none_provider_through_the_facade():
    rec = Recorder(chat(json.dumps(GOOD)))
    model = JsonModel(ModelConfig(provider="openai_compatible", model="m", base_url="http://127.0.0.1:9/v1"), transport=rec.transport)
    assert isinstance(model.provider, OpenAICompatibleProvider) and model.call("S", {}, Reply, CallBudget(1), "generator").answer == "ok"
    assert isinstance(chat_provider(ModelConfig()), NoneProvider)
    with pytest.raises(ProviderUnavailable, match="generator requires a model"):
        JsonModel(ModelConfig()).call("S", {}, Reply, CallBudget(1), "generator")
    text_rec = Recorder(chat("plain text"))
    assert JsonModel(local_role(), transport=text_rec.transport).text("S", "hello", CallBudget(1), "summary") == "plain text"
    assert text_rec.body()["messages"][1] == {"role": "user", "content": "hello"} and "response_format" not in text_rec.body()


# --- error mapping (every provider) -------------------------------------------------------------

PROVIDERS = [
    pytest.param(lambda t: AnthropicProvider(anthropic_role(), transport=t), id="anthropic"),
    pytest.param(lambda t: OpenAIProvider(openai_role(), transport=t), id="openai"),
    pytest.param(lambda t: OpenAICompatibleProvider(local_role(api_key_env=KEY), transport=t), id="openai_compatible"),
]


@pytest.mark.parametrize("make", PROVIDERS)
@pytest.mark.parametrize(
    "rec,error,category",
    [
        (lambda: Recorder({"error": "leaked passage text"}, status=401), ProviderAuthError, "auth"),
        (lambda: Recorder({}, status=403), ProviderAuthError, "auth"),
        (lambda: Recorder({}, status=429, headers={"retry-after": "7"}), ProviderRateLimited, "rate_limited"),
        (lambda: Recorder({}, status=500), ProviderServerError, "server_error"),
        (lambda: Recorder({}, status=529), ProviderServerError, "server_error"),
        (lambda: Recorder({}, status=400), ProviderRequestError, "bad_request"),
        (lambda: Recorder(raises=httpx.ReadTimeout("slow")), ProviderTimeout, "timeout"),
        (lambda: Recorder(raises=httpx.ConnectError("refused")), ProviderConnectionError, "connection"),
        (lambda: Recorder(text="<html>gateway</html>"), ProviderResponseError, "bad_response"),
    ],
)
def test_errors_are_typed_and_never_retried(make, rec, error, category):
    recorder = rec()
    budget = CallBudget(5)
    with pytest.raises(error) as raised:
        make(recorder.transport).complete_json("S", {}, Reply, budget, CallContext("generator"))
    assert len(recorder.requests) == 1  # no retry
    assert raised.value.category == category and isinstance(raised.value, RuntimeError)
    assert "leaked passage text" not in str(raised.value) and SECRET not in str(raised.value)
    assert budget.calls[0]["error_category"] == category and not budget.calls[0]["success"]
    if category == "rate_limited":
        assert raised.value.retry_after == "7"


def test_missing_key_fails_before_the_request_and_the_reservation(monkeypatch):
    monkeypatch.delenv(KEY)
    rec = Recorder(chat("{}"))
    budget = CallBudget(1)
    with pytest.raises(MissingSecret, match=KEY):
        OpenAIProvider(openai_role(), transport=rec.transport).complete_json("S", {}, Reply, budget, CallContext("generator"))
    assert rec.requests == [] and budget._reserved == 0


# --- budget and trace accounting ----------------------------------------------------------------

PRICES = PriceTable(
    version="test-1",
    prices={"claude-sonnet-4-5": {"input_per_million": 3.0, "output_per_million": 15.0, "cached_input_per_million": 0.3, "cache_write_input_per_million": 3.75}},
)


def test_usage_flows_into_budget_usd_and_trace_without_prompt_text():
    usage = {"input_tokens": 50, "output_tokens": 30, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 100}
    ctx = new_context(GEN, limits=BudgetLimits(max_llm_calls=3, max_usd=1.0), price_table=PRICES)
    rec = Recorder(anthropic_tool(GOOD, usage=usage))
    model = JsonModel(anthropic_role(structured_output="native", prompt_caching="controlled"), transport=rec.transport)
    model.call("SECRET SYSTEM PROMPT", {"q": "private question"}, Reply, RunBudgetMeter(ctx, "generator", "claude-sonnet-4-5"), "generator")
    expected = (50 * 3.0 + 900 * 0.3 + 100 * 3.75 + 30 * 15.0) / 1_000_000
    snap = ctx.budget.snapshot()
    assert snap["used"]["calls"] == 1 and snap["used"]["input_tokens"] == 1050 and snap["used"]["output_tokens"] == 30
    assert snap["used"]["usd"] == pytest.approx(expected) and snap["reserved"]["usd"] == pytest.approx(expected)
    (event,) = [e for s in ctx.trace.spans for e in s.events if e["name"] == "model_call"]
    assert event["cached_input_tokens"] == 900 and event["cache_creation_input_tokens"] == 100 and event["usd"] == pytest.approx(expected)
    assert event["mode"] == "native:tool_use" and event["returned_model"] == "claude-sonnet-4-5-20250929" and event["success"]
    exported = json.dumps(ctx.trace.to_dict())
    assert "SECRET SYSTEM PROMPT" not in exported and "private question" not in exported and SECRET not in exported


def test_budget_exhaustion_blocks_the_call_before_any_request():
    ctx = new_context(GEN, limits=BudgetLimits(max_llm_calls=1))
    rec = Recorder(chat(json.dumps(GOOD)))
    model = JsonModel(openai_role(), transport=rec.transport)
    model.call("S", {}, Reply, RunBudgetMeter(ctx, "generator", "gpt-4.1-mini"), "generator")
    with pytest.raises(BudgetExhausted):
        model.call("S", {}, Reply, RunBudgetMeter(ctx, "generator", "gpt-4.1-mini"), "generator")
    assert len(rec.requests) == 1
    # the output ceiling is reserved up front: a smaller output budget refuses the call outright
    tight = new_context(GEN, limits=BudgetLimits(max_output_tokens=100))
    with pytest.raises(BudgetExhausted, match="output_tokens"):
        model.call("S", {}, Reply, RunBudgetMeter(tight, "generator", "gpt-4.1-mini"), "generator")
    assert len(rec.requests) == 1


def test_unreported_usage_keeps_the_estimate_and_failed_calls_are_recorded():
    ctx = new_context(GEN, limits=BudgetLimits(max_llm_calls=5, max_output_tokens=10_000))
    rec = Recorder(chat(json.dumps(GOOD), usage={}))
    JsonModel(local_role(), transport=rec.transport).call("S", {}, Reply, RunBudgetMeter(ctx, "extraction", None), "extraction")
    snap = ctx.budget.snapshot()
    assert snap["used"]["output_tokens"] == 2048 and snap["used"]["calls"] == 1  # estimate kept, never zero
    failing = Recorder({}, status=503)
    with pytest.raises(ProviderServerError):
        JsonModel(local_role(), transport=failing.transport).call("S", {}, Reply, RunBudgetMeter(ctx, "extraction", None), "extraction")
    events = [e for s in ctx.trace.spans for e in s.events if e["name"] == "model_call"]
    assert [e["success"] for e in events] == [True, False] and events[1]["error_category"] == "server_error"
    assert all(e["usage_reported"] is False for e in events) and ctx.budget.snapshot()["used"]["calls"] == 2


def test_planner_failures_map_to_decision_categories(monkeypatch, v2_backend, v2_schema):
    """``llm`` agent row of §3.7.1: timeout / schema_violation / provider_error, no retry."""
    agent = LlmAgent(LlmConfig(role=local_role()), Dependencies(backends={"records": v2_backend.records}))
    caps = Capabilities(arms=["lexical"], metadata_schema=v2_schema)
    original = httpx.Client
    cases = [
        (Recorder(raises=httpx.ReadTimeout("slow")), "timeout"),
        (Recorder(chat('{"steps": "nope"}')), "schema_violation"),
        (Recorder({}, status=401), "provider_error"),
        (Recorder(chat('{"steps": [', finish="length")), "provider_error"),
    ]
    for recorder, category in cases:
        monkeypatch.setattr(httpx, "Client", lambda **kw: original(transport=recorder.transport, **kw))
        decision = agent.decide(new_context(GEN), DecisionRequest(query="find open records"), None, caps)
        assert decision.outcome == "abstain" and decision.failure == category, (category, decision)
        assert len(recorder.requests) == 1


# --- embeddings ---------------------------------------------------------------------------------


def embedding_reply(n: int, dims: int, usage=True) -> dict:
    data = [{"index": i, "embedding": list(np.eye(dims)[i % dims])} for i in range(n)]
    return {"data": list(reversed(data)), **({"usage": {"prompt_tokens": 3 * n, "total_tokens": 3 * n}} if usage else {})}


def test_openai_embeddings_request_shape_order_and_usage():
    rec = Recorder(embedding_reply(2, 16))
    cfg = EmbedderRole(provider="openai", model="text-embedding-3-small", dimensions=16, api_key_env=KEY)
    embedder = Embedder(cfg, transport=rec.transport)
    vectors = embedder.encode(["a", "b"])
    assert vectors.shape == (2, 16) and vectors[0][0] == pytest.approx(1.0) and vectors[1][1] == pytest.approx(1.0)
    assert rec.requests[0].headers["authorization"] == f"Bearer {SECRET}"
    assert rec.body() == {"model": "text-embedding-3-small", "input": ["a", "b"], "dimensions": 16}
    assert embedder.provider.last_usage.input_tokens == 6 and embedder.recipe()["provider"] == "openai"


def test_openai_compatible_embeddings_and_typed_errors():
    rec = Recorder(embedding_reply(1, 16, usage=False))
    cfg = EmbedderRole(provider="openai_compatible", model="local-embed", dimensions=16, base_url="http://127.0.0.1:9/v1")
    assert Embedder(cfg, transport=rec.transport).encode(["a"]).shape == (1, 16)
    assert "dimensions" not in rec.body() and "authorization" not in rec.requests[0].headers
    limited = Recorder({}, status=429)
    with pytest.raises(ProviderRateLimited):
        Embedder(cfg, transport=limited.transport).encode(["a"])
    assert len(limited.requests) == 1
    with pytest.raises(ValueError, match="shape"):
        Embedder(cfg.model_copy(update={"dimensions": 32}), transport=Recorder(embedding_reply(1, 16)).transport).encode(["a"])
