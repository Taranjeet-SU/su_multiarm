"""M0 provider resilience (docs/plans/unified_merge.md §6.13, §10.1) with mocked HTTP.

``on_incomplete`` (retry_shorter / retry_larger) is one reserved, metered retry after a reply cut
at ``max_tokens``, then ``ProviderIncomplete``; ``retry_transient`` retries 5xx/429/timeouts and a
stream cut before its final event, only inside the request deadline and only when the budget can
reserve the attempt; every failure records whether the reply was truncated (``max_tokens``) or the
stream ended early (``stream_end``). Contract-mocked; no provider endpoint is called."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from su_multi_arm.core.config import ChatRole
from su_multi_arm.core.errors import BudgetExhausted
from su_multi_arm.providers import AnthropicProvider, CallBudget, CallContext, ProviderAuthError, ProviderIncomplete, ProviderServerError
from su_multi_arm.providers._chat import SHORTER_INSTRUCTION
from su_multi_arm.providers.accounting import RunBudgetMeter
from su_multi_arm.runtime.budget import Budget, BudgetLimits
from su_multi_arm.testing.provider_fakes import anthropic_sse

KEY = "SU_TEST_PROVIDER_KEY"


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv(KEY, "sk-test-not-a-real-key")


def message(text: str = "done", stop: str = "end_turn", output_tokens: int = 5) -> dict:
    return {"model": "claude-sonnet-4-5", "content": [{"type": "text", "text": text}], "stop_reason": stop, "usage": {"input_tokens": 10, "output_tokens": output_tokens}}


class Script:
    """Replies in order: a dict (JSON 200), an int (HTTP status), or bytes (an event stream)."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        reply = self.replies.pop(0)
        if isinstance(reply, int):
            return httpx.Response(reply, json={"type": "error"})
        if isinstance(reply, bytes):
            return httpx.Response(200, content=reply, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=reply)


def provider(script: Script, **role) -> AnthropicProvider:
    spec = {"provider": "anthropic", "model": "claude-sonnet-4-5", "api_key_env": KEY, "max_tokens": 256, **role}
    p = AnthropicProvider(ChatRole(**spec), transport=httpx.MockTransport(script))
    p.slept = []
    p._sleep = p.slept.append  # type: ignore[method-assign]
    return p


class Meter(CallBudget):
    def __init__(self, remaining: float | None = None, refuse_after: int | None = None):
        super().__init__(10)
        self.remaining, self.refuse_after = remaining, refuse_after

    def remaining_seconds(self):
        return self.remaining

    def reserve(self, estimate=None):
        if self.refuse_after is not None and self._reserved >= self.refuse_after:
            raise BudgetExhausted("usd", 0.1, 0.0)
        super().reserve(estimate)


def text(p, meter=None, deltas=None):
    meter = meter or Meter()
    if deltas is None:
        return p.complete_text("system", "question", meter, CallContext("generator"))[0], meter
    return p.complete_text_stream("system", "question", meter, CallContext("generator"), deltas.append)[0], meter


def test_truncation_fails_by_default_and_records_the_cause():
    script = Script(message(stop="max_tokens", output_tokens=256))
    with pytest.raises(ProviderIncomplete) as caught:
        text(provider(script))
    assert caught.value.cause == "max_tokens" and len(script.requests) == 1


def test_retry_shorter_is_one_reserved_attempt_with_a_brevity_instruction():
    script = Script(message(stop="max_tokens", output_tokens=256), message("short"))
    reply, meter = text(provider(script, on_incomplete="retry_shorter"))
    assert reply == "short" and len(meter.calls) == 2
    assert meter.calls[0]["incomplete_cause"] == "max_tokens" and meter.calls[0]["success"] is False
    assert meter.calls[1]["attempt"] == 2 and meter.calls[1]["retry_reason"] == "incomplete:retry_shorter"
    first, second = script.requests
    assert SHORTER_INSTRUCTION.strip() in second["system"] and SHORTER_INSTRUCTION.strip() not in first["system"]
    assert first["max_tokens"] == second["max_tokens"] == 256


def test_a_second_truncation_is_provider_incomplete():
    script = Script(message(stop="max_tokens", output_tokens=256), message(stop="max_tokens", output_tokens=256))
    p = provider(script, on_incomplete="retry_shorter")
    with pytest.raises(ProviderIncomplete):
        text(p)
    assert len(script.requests) == 2  # reserved once, never a third call


def test_retry_larger_sends_the_larger_ceiling():
    script = Script(message(stop="max_tokens", output_tokens=256), message("long enough", output_tokens=600))
    reply, meter = text(provider(script, on_incomplete="retry_larger", incomplete_retry_max_tokens=1024))
    assert reply == "long enough"
    assert [r["max_tokens"] for r in script.requests] == [256, 1024]
    assert meter.calls[1]["max_tokens"] == 1024  # the ceiling actually sent is recorded


def test_transient_errors_are_not_retried_without_a_policy():
    script = Script(503)
    with pytest.raises(ProviderServerError):
        text(provider(script))
    assert len(script.requests) == 1


def test_one_transient_retry_with_backoff():
    script = Script(503, message("ok"))
    p = provider(script, retry_transient={"attempts": 1, "backoff_seconds": 2})
    reply, meter = text(p)
    assert reply == "ok" and p.slept == [2] and meter.calls[1]["retry_reason"] == "transient:server_error"


def test_transient_retry_only_inside_the_deadline():
    script = Script(503, message("never sent"))
    p = provider(script, retry_transient={"attempts": 1, "backoff_seconds": 2})
    with pytest.raises(ProviderServerError):
        text(p, Meter(remaining=2.5))  # backoff 2 + 1 s floor does not fit
    assert len(script.requests) == 1 and p.slept == []


def test_auth_failures_are_never_retried():
    script = Script(401, message())
    with pytest.raises(ProviderAuthError):
        text(provider(script, retry_transient={"attempts": 2, "backoff_seconds": 0}))
    assert len(script.requests) == 1


def test_a_retry_the_budget_refuses_leaves_the_original_error():
    script = Script(503, message())
    with pytest.raises(ProviderServerError):
        text(provider(script, retry_transient={"attempts": 1, "backoff_seconds": 0}), Meter(refuse_after=1))
    assert len(script.requests) == 1


def test_stream_end_is_recorded_and_retried_as_transient():
    cut = anthropic_sse("partial", stop="end_turn", message_stop=False)
    script = Script(cut, anthropic_sse("complete reply"))
    deltas: list[str] = []
    reply, meter = text(provider(script, retry_transient={"attempts": 1, "backoff_seconds": 0}, capabilities={"streaming": True}), deltas=deltas)
    assert reply == "complete reply"
    assert meter.calls[0]["incomplete_cause"] == "stream_end" and meter.calls[1]["retry_reason"] == "transient:incomplete:stream_end"
    assert "".join(deltas) == "partial"  # provisional deltas of the cut attempt only; the retry's are not forwarded


def test_a_stream_cut_after_a_max_tokens_stop_is_a_truncation():
    cut = anthropic_sse("partial", stop="max_tokens", message_stop=False)
    script = Script(cut)
    with pytest.raises(ProviderIncomplete) as caught:
        text(provider(script, retry_transient={"attempts": 1, "backoff_seconds": 0}, capabilities={"streaming": True}), deltas=[])
    assert caught.value.cause == "max_tokens" and len(script.requests) == 1  # not transient; on_incomplete is fail


def test_the_request_trace_carries_attempts_and_causes():
    events: list[tuple[str, dict]] = []
    ctx = SimpleNamespace(budget=Budget(BudgetLimits(max_llm_calls=5)), trace=SimpleNamespace(event=lambda name, **kw: events.append((name, kw))), spend=None)
    script = Script(message(stop="max_tokens", output_tokens=256), message("short"))
    p = provider(script, on_incomplete="retry_shorter")
    p.complete_text("system", "q", RunBudgetMeter(ctx, "generator", "claude-sonnet-4-5"), CallContext("generator"))
    calls = [kw for name, kw in events if name == "model_call"]
    assert calls[0]["incomplete_cause"] == "max_tokens" and calls[1]["attempt"] == 2
    assert ctx.budget.usage.used["calls"] == 2  # both attempts are reserved and committed


def test_role_validation():
    with pytest.raises(ValueError, match="incomplete_retry_max_tokens"):
        ChatRole(provider="openai_compatible", model="m", on_incomplete="retry_larger")
    with pytest.raises(ValueError, match="larger than max_tokens"):
        ChatRole(provider="openai_compatible", model="m", max_tokens=2048, on_incomplete="retry_larger", incomplete_retry_max_tokens=1024)
    with pytest.raises(ValueError, match="only with on_incomplete: retry_larger"):
        ChatRole(provider="openai_compatible", model="m", incomplete_retry_max_tokens=4096)
    with pytest.raises(ValueError):
        ChatRole(provider="openai_compatible", model="m", retry_transient={"attempts": 3})
    role = ChatRole(provider="openai_compatible", model="m")
    assert role.on_incomplete == "fail" and role.retry_transient is None  # defaults: no retry
