"""Anthropic Messages API provider.

* Native structured output (registry ``native_kind: tool_use``): one tool whose ``input_schema`` is
  the pydantic schema, forced with ``tool_choice``; the reply is the tool call's ``input``. Only for
  ``(anthropic, model)`` pairs the registry declares; otherwise the role must use ``none``
  (schema in the system prompt, strict validation of the text reply). The Messages API has no JSON
  mode, so ``json_mode``/``json_schema`` are rejected at validation.
* Prompt caching (``prompt_caching: controlled``): ``cache_control: {type: ephemeral}`` on the system
  block and on the tool definition. Usage reports ``cache_read_input_tokens`` (counted as cached)
  and ``cache_creation_input_tokens`` (cache writes); ``input_tokens`` excludes both, so the
  normalised ``Usage.input_tokens`` is the sum of the three.
* Truncation (``stop_reason: max_tokens``) is ``ProviderIncomplete``; ``refusal`` is
  ``ProviderRefusal``; a native call that ends without the tool call is ``ProviderResponseError``.
* Streaming (``stream: true``): ``message_start`` / ``content_block_start`` / ``content_block_delta``
  (``text_delta``, ``input_json_delta``) / ``content_block_stop`` / ``message_delta`` (stop reason,
  output usage) / ``message_stop`` are folded into the non-streaming message and parsed by the same
  rules. A stream without ``message_stop`` is ``ProviderIncomplete``; an ``error`` event is a typed
  ``ProviderError`` (its message is not echoed).

Evidence level: contract-mocked (request/response shapes from the published API documentation).
"""

from __future__ import annotations

import json
from typing import Any, Callable, ClassVar

from pydantic import BaseModel

from ._chat import HttpChatProvider
from .base import (
    CallContext,
    MeteredCall,
    ProviderError,
    ProviderIncomplete,
    ProviderRateLimited,
    ProviderRefusal,
    ProviderResponseError,
    ProviderServerError,
    Usage,
    as_int,
    schema_instruction,
    schema_name,
)

API_VERSION = "2023-06-01"
EPHEMERAL = {"type": "ephemeral"}


def parse_usage(raw: Any) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    uncached = as_int(raw.get("input_tokens"))
    read = as_int(raw.get("cache_read_input_tokens"))
    write = as_int(raw.get("cache_creation_input_tokens"))
    return Usage(
        input_tokens=uncached + read + write,
        output_tokens=as_int(raw.get("output_tokens")),
        cached_input_tokens=read,
        cache_creation_input_tokens=write,
        reported="input_tokens" in raw or "output_tokens" in raw,
    )


class AnthropicProvider(HttpChatProvider):
    name: ClassVar[str] = "anthropic"
    default_base_url: ClassVar[str] = "https://api.anthropic.com/v1"

    def _auth(self, key: str | None) -> dict[str, str]:
        return {"x-api-key": key} if key else {}

    def _request(self, system: str, user: str, schema: type[BaseModel] | None, mode: str):
        controlled = self.role.prompt_caching == "controlled"
        text = system if (schema is None or mode == "native:tool_use") else system + schema_instruction(schema)
        body: dict[str, Any] = {
            "model": self.role.model,
            self.caps.max_output_tokens_param: self.role.max_tokens,
            "system": [{"type": "text", "text": text, "cache_control": EPHEMERAL}] if controlled else text,
            "messages": [{"role": "user", "content": user}],
        }
        if mode == "native:tool_use":
            tool: dict[str, Any] = {
                "name": schema_name(schema),  # type: ignore[arg-type]
                "description": "Return the result as the input of this tool.",
                "input_schema": schema.model_json_schema(),  # type: ignore[union-attr]
            }
            if controlled:
                tool["cache_control"] = EPHEMERAL
            body["tools"] = [tool]
            body["tool_choice"] = {"type": "tool", "name": tool["name"]}
        return "/messages", {"anthropic-version": API_VERSION}, body

    def _stream_body(self, body: dict[str, Any]) -> dict[str, Any]:
        return {**body, "stream": True}

    def _stream_consumer(self, on_delta: Callable[[str], None], stage: str) -> "AnthropicStream":
        return AnthropicStream(on_delta, stage)

    def _parse(self, data: dict[str, Any], mode: str, ctx: CallContext, call: MeteredCall, body: dict[str, Any]) -> str:
        call.usage = parse_usage(data.get("usage"))
        call.returned_model = data.get("model") if isinstance(data.get("model"), str) else None
        stop = data.get("stop_reason")
        call.extra["stop_reason"] = stop
        content = data.get("content")
        if not isinstance(content, list):
            raise ProviderResponseError("reply has no content list", stage=ctx.stage)
        if stop == "max_tokens":
            raise ProviderIncomplete("incomplete reply: truncated at max_tokens", stage=ctx.stage)
        if stop == "refusal":
            raise ProviderRefusal("model refused", stage=ctx.stage)
        if mode == "native:tool_use":
            if stop != "tool_use":
                raise ProviderResponseError(f"native call ended with stop_reason {stop!r} instead of the schema tool call", stage=ctx.stage)
            name = body["tool_choice"]["name"]
            calls = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == name]
            if len(calls) != 1 or not isinstance(calls[0].get("input"), dict):
                raise ProviderResponseError(f"native call returned {len(calls)} tool calls; expected exactly one", stage=ctx.stage)
            return json.dumps(calls[0]["input"], ensure_ascii=False)
        if stop != "end_turn":
            raise ProviderIncomplete(f"incomplete or unsupported model response (stop_reason {stop!r})", stage=ctx.stage)
        return "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")


STREAM_ERRORS: dict[str, type[ProviderError]] = {
    "overloaded_error": ProviderServerError,
    "api_error": ProviderServerError,
    "rate_limit_error": ProviderRateLimited,
}


class AnthropicStream:
    """Folds Messages API stream events into the non-streaming message shape."""

    def __init__(self, on_delta: Callable[[str], None], stage: str):
        self.on_delta, self.stage = on_delta, stage
        self.message: dict[str, Any] = {"content": [], "usage": {}, "stop_reason": None}
        self.buffers: dict[int, list[str]] = {}
        self.stopped = False

    def _block(self, index: Any) -> dict[str, Any]:
        if not isinstance(index, int) or index < 0 or index >= len(self.message["content"]):
            raise ProviderResponseError("stream delta for an unknown content block", stage=self.stage)
        return self.message["content"][index]

    def feed(self, event: str, data: str) -> None:
        try:
            payload = json.loads(data)
        except ValueError as error:
            raise ProviderResponseError("stream event is not JSON", stage=self.stage) from error
        if not isinstance(payload, dict):
            raise ProviderResponseError("stream event is not a JSON object", stage=self.stage)
        kind = payload.get("type") or event
        if kind == "message_start":
            message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
            self.message["model"] = message.get("model")
            if isinstance(message.get("usage"), dict):
                self.message["usage"].update(message["usage"])
        elif kind == "content_block_start":
            block = payload.get("content_block")
            if not isinstance(block, dict) or payload.get("index") != len(self.message["content"]):
                raise ProviderResponseError("stream content blocks out of order", stage=self.stage)
            block = dict(block)
            if block.get("type") == "text":
                block["text"] = block.get("text") or ""
            self.message["content"].append(block)
            self.buffers[payload["index"]] = []
        elif kind == "content_block_delta":
            block = self._block(payload.get("index"))
            delta = payload.get("delta") if isinstance(payload.get("delta"), dict) else {}
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                block["text"] = block.get("text", "") + delta["text"]
                self.on_delta(delta["text"])
            elif delta.get("type") == "input_json_delta" and isinstance(delta.get("partial_json"), str):
                self.buffers[payload["index"]].append(delta["partial_json"])
                self.on_delta(delta["partial_json"])
        elif kind == "content_block_stop":
            block = self._block(payload.get("index"))
            if block.get("type") == "tool_use":
                text = "".join(self.buffers.get(payload["index"], []))
                try:
                    block["input"] = json.loads(text) if text else {}
                except ValueError as error:
                    raise ProviderResponseError("streamed tool input is not JSON", stage=self.stage) from error
        elif kind == "message_delta":
            delta = payload.get("delta") if isinstance(payload.get("delta"), dict) else {}
            if "stop_reason" in delta:
                self.message["stop_reason"] = delta["stop_reason"]
            if isinstance(payload.get("usage"), dict):
                self.message["usage"].update(payload["usage"])
        elif kind == "message_stop":
            self.stopped = True
        elif kind == "error":
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            cls = STREAM_ERRORS.get(str(error.get("type")), ProviderResponseError)
            raise cls(f"provider stream error ({error.get('type')})", stage=self.stage)
        # ``ping`` and unknown event types are ignored (forward compatibility).

    def result(self) -> dict[str, Any]:
        if not self.stopped:
            raise ProviderIncomplete("incomplete reply: the stream ended before message_stop", stage=self.stage)
        return self.message
