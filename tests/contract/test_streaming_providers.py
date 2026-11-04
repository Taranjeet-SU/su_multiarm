"""Streaming provider contracts with mocked HTTP (plan §3.6 "stream parsing"; execution-log #12).

Anthropic Messages and OpenAI / OpenAI-compatible Chat Completions event streams are parsed into
the same reply the non-streaming call would see, so truncation, refusal, missing terminal events and
provider error events fail exactly as before; deltas are handed out as they arrive; usage is metered
from the stream; a role must declare ``capabilities.streaming``; ``provider: none`` never streams.
Contract-mocked only: shapes from the published API documentation, never a paid endpoint."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel, Field

from su_multi_arm.core.config import ChatRole
from su_multi_arm.core.errors import RunCancelled
from su_multi_arm.providers import (
    AnthropicProvider,
    CallBudget,
    CallContext,
    CapabilityMismatch,
    JsonModel,
    OpenAICompatibleProvider,
    OpenAIProvider,
    ProviderIncomplete,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderServerError,
    ProviderUnavailable,
)
from su_multi_arm.providers.base import iter_sse
from su_multi_arm.testing.provider_fakes import anthropic_sse, openai_sse

KEY = "SU_TEST_PROVIDER_KEY"
REPLY = {"answer": "Synthetic streamed answer.", "citations": ["d1"]}


class Reply(BaseModel):
    answer: str
    citations: list[str] = Field(default_factory=list)


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv(KEY, "sk-test-not-a-real-key")


class Stream:
    def __init__(self, body: bytes, content_type: str = "text/event-stream", status: int = 200):
        self.body, self.content_type, self.status = body, content_type, status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, content=self.body, headers={"content-type": self.content_type})

    @property
    def transport(self):
        return httpx.MockTransport(self)

    def sent(self) -> dict:
        return json.loads(self.requests[0].content)


def role(provider: str, streaming: bool = True, **kw) -> ChatRole:
    base = {"anthropic": {"model": "claude-sonnet-4-5", "api_key_env": KEY}, "openai": {"model": "gpt-4.1-mini", "api_key_env": KEY}}
    spec = base.get(provider, {"model": "local-model", "base_url": "http://127.0.0.1:9/v1"})
    caps = {"streaming": streaming, **kw.pop("capabilities", {})}
    return ChatRole(provider=provider, max_tokens=256, capabilities=caps, **spec, **kw)


def run(provider, deltas=None, budget=None):
    deltas = [] if deltas is None else deltas
    budget = budget or CallBudget(5)
    reply, usage = provider.complete_json_stream("system", {"q": 1}, Reply, budget, CallContext("generator"), deltas.append)
    return reply, usage, deltas, budget


def test_openai_stream_is_parsed_metered_and_requested_with_usage():
    text = json.dumps(REPLY)
    mock = Stream(openai_sse(text, usage={"prompt_tokens": 30, "completion_tokens": 9, "prompt_tokens_details": {"cached_tokens": 4}}))
    reply, usage, deltas, budget = run(OpenAIProvider(role("openai"), transport=mock.transport))
    assert reply == Reply(**REPLY) and "".join(deltas) == text and len(deltas) > 1
    body = mock.sent()
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True} and body["max_completion_tokens"] == 256
    assert mock.requests[0].headers["accept"] == "text/event-stream"
    assert usage.input_tokens == 30 and usage.cached_input_tokens == 4 and usage.reported
    call = budget.calls[0]
    assert call["success"] and call["stream"] is True and call["stop_reason"] == "stop" and call["returned_model"] == "fake-model"


def test_openai_compatible_stream_uses_the_role_declared_token_parameter():
    mock = Stream(openai_sse(json.dumps(REPLY)))  # a server that reports no usage
    provider = OpenAICompatibleProvider(role("openai_compatible", max_tokens_parameter="max_tokens"), transport=mock.transport)
    _, usage, deltas, budget = run(provider)
    assert mock.sent()["max_tokens"] == 256 and mock.sent()["stream"] is True and deltas
    assert not usage.reported and budget.calls[0]["success"]


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (openai_sse(json.dumps(REPLY), finish="length"), ProviderIncomplete),  # truncation is a failure
        (openai_sse(json.dumps(REPLY), finish=None), ProviderIncomplete),  # no finish_reason
        (openai_sse(json.dumps(REPLY), done=False), ProviderIncomplete),  # no [DONE]
        (b"data: not json\n\n", ProviderResponseError),
    ],
    ids=["length", "no_finish", "no_done", "garbage"],
)
def test_openai_stream_failures_are_typed(body, error):
    budget = CallBudget(5)
    with pytest.raises(error):
        run(OpenAIProvider(role("openai"), transport=Stream(body).transport), budget=budget)
    assert budget.calls and budget.calls[0]["success"] is False  # the failed call is still recorded


def test_non_event_stream_reply_is_refused_not_downgraded():
    mock = Stream(json.dumps({"choices": []}).encode(), content_type="application/json")
    with pytest.raises(ProviderResponseError, match="not an event stream"):
        run(OpenAIProvider(role("openai"), transport=mock.transport))


def test_streaming_must_be_declared_by_the_role():
    mock = Stream(openai_sse(json.dumps(REPLY)))
    budget = CallBudget(5)
    with pytest.raises(CapabilityMismatch, match="capabilities.streaming"):
        run(OpenAIProvider(role("openai", streaming=False), transport=mock.transport), budget=budget)
    assert mock.requests == [] and budget.calls == []
    assert JsonModel(role("openai", streaming=False)).streams() is False and JsonModel(role("openai")).streams() is True


def test_anthropic_text_stream_folds_usage_from_start_and_delta_events():
    text = json.dumps(REPLY)
    mock = Stream(anthropic_sse(text, usage_in={"input_tokens": 12, "cache_read_input_tokens": 5}, output_tokens=7))
    reply, usage, deltas, budget = run(AnthropicProvider(role("anthropic", capabilities={"structured_output": "none"}), transport=mock.transport))
    assert reply == Reply(**REPLY) and "".join(deltas) == text
    assert mock.sent()["stream"] is True and "stream_options" not in mock.sent()
    assert usage.input_tokens == 17 and usage.cached_input_tokens == 5 and usage.output_tokens == 7
    assert budget.calls[0]["stop_reason"] == "end_turn"


def test_anthropic_native_tool_use_stream_assembles_the_tool_input():
    mock = Stream(anthropic_sse(tool=("Reply", REPLY), stop="tool_use"))
    provider = AnthropicProvider(role("anthropic", capabilities={"structured_output": "native"}), transport=mock.transport)
    reply, _, deltas, _ = run(provider)
    assert reply == Reply(**REPLY) and json.loads("".join(deltas)) == REPLY
    assert mock.sent()["tool_choice"] == {"type": "tool", "name": "Reply"}


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (anthropic_sse(json.dumps(REPLY), stop="max_tokens"), ProviderIncomplete),
        (anthropic_sse(json.dumps(REPLY), message_stop=False), ProviderIncomplete),
        (b'event: error\ndata: {"type": "error", "error": {"type": "overloaded_error", "message": "secret detail"}}\n\n', ProviderServerError),
        (b'event: error\ndata: {"type": "error", "error": {"type": "rate_limit_error"}}\n\n', ProviderRateLimited),
        (b'event: content_block_delta\ndata: {"type": "content_block_delta", "index": 3, "delta": {"type": "text_delta", "text": "x"}}\n\n', ProviderResponseError),
    ],
    ids=["max_tokens", "no_message_stop", "overloaded", "rate_limited", "unknown_block"],
)
def test_anthropic_stream_failures_are_typed(body, error):
    with pytest.raises(error) as raised:
        run(AnthropicProvider(role("anthropic"), transport=Stream(body).transport))
    assert "secret detail" not in str(raised.value)


def test_a_delta_consumer_that_cancels_closes_the_call_and_records_it():
    budget = CallBudget(5)

    def cancel(text):
        raise RunCancelled("run cancelled by the caller")

    with pytest.raises(RunCancelled):
        OpenAIProvider(role("openai"), transport=Stream(openai_sse(json.dumps(REPLY))).transport).complete_json_stream("s", {}, Reply, budget, CallContext("generator"), cancel)
    assert budget.calls[0]["success"] is False and budget.calls[0]["error_category"] == "cancelled"


def test_provider_none_never_streams():
    model = JsonModel(None)
    assert model.streams() is False
    with pytest.raises(ProviderUnavailable):
        model.call_stream("s", {}, Reply, CallBudget(1), "generator", lambda text: None)


def test_sse_parser_handles_comments_multiline_data_and_crlf():
    lines = [": keep-alive", "event: a", "data: one", "data: two", "", "data: three\r", "\r", "id: 7", "data: tail"]
    assert list(iter_sse(lines)) == [("a", "one\ntwo"), ("message", "three"), ("message", "tail")]
