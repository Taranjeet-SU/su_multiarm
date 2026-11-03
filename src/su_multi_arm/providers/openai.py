"""OpenAI Chat Completions provider (``POST /chat/completions``).

* Native structured output (registry ``native_kind: json_schema_strict``): ``response_format:
  {type: json_schema, json_schema: {name, schema, strict: true}}`` with the schema converted to
  strict form (every object closed, every property required). A schema strict mode cannot express
  (free-form mappings such as ``dict[str, ...]`` fields) is a ``CapabilityMismatch`` before any call:
  declare ``json_schema`` (``strict: false``) for that role instead. Never downgraded silently.
* ``json_schema`` sends the schema with ``strict: false``; ``json_mode`` sends ``{type: json_object}``;
  both also put the schema in the system prompt, and every reply is validated strictly.
* Prompt caching is passive (automatic prefix caching): ``usage.prompt_tokens_details.cached_tokens``
  is recorded; the package claims no control.
* ``max_completion_tokens`` is the output ceiling; ``reasoning_effort`` only for registry-declared
  reasoning models. ``finish_reason: length`` is ``ProviderIncomplete``; a ``refusal`` or
  ``content_filter`` is ``ProviderRefusal``.
* Streaming (``stream: true`` with ``stream_options: {include_usage: true}``): ``data:`` chunks with
  ``choices[0].delta.content``/``refusal`` and ``finish_reason``, a usage chunk, then ``[DONE]``;
  folded into the non-streaming reply and parsed by the same rules. A stream without a
  ``finish_reason`` or without ``[DONE]`` is ``ProviderIncomplete``.

Evidence level: contract-mocked (request/response shapes from the published API documentation).
"""

from __future__ import annotations

import json
from typing import Any, Callable, ClassVar

from pydantic import BaseModel

from ._chat import HttpChatProvider
from .base import (
    CallContext,
    CapabilityMismatch,
    MeteredCall,
    ProviderIncomplete,
    ProviderRefusal,
    ProviderResponseError,
    Usage,
    as_int,
    schema_instruction,
    schema_name,
    strict_schema,
)


def parse_usage(raw: Any) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    prompt_details = raw.get("prompt_tokens_details") if isinstance(raw.get("prompt_tokens_details"), dict) else {}
    completion_details = raw.get("completion_tokens_details") if isinstance(raw.get("completion_tokens_details"), dict) else {}
    return Usage(
        input_tokens=as_int(raw.get("prompt_tokens")),
        output_tokens=as_int(raw.get("completion_tokens")),
        cached_input_tokens=as_int(prompt_details.get("cached_tokens")),
        reasoning_tokens=as_int(completion_details.get("reasoning_tokens")),
        reported="prompt_tokens" in raw or "completion_tokens" in raw,
    )


class OpenAIProvider(HttpChatProvider):
    name: ClassVar[str] = "openai"
    default_base_url: ClassVar[str] = "https://api.openai.com/v1"

    def _auth(self, key: str | None) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _max_tokens_param(self) -> str:
        return self.caps.max_output_tokens_param

    def _reasoning_param(self) -> str | None:
        return self.caps.reasoning_effort_param

    def _request(self, system: str, user: str, schema: type[BaseModel] | None, mode: str):
        text = system
        body: dict[str, Any] = {"model": self.role.model, self._max_tokens_param(): self.role.max_tokens}
        if schema is not None:
            if mode == "native:json_schema_strict":
                strict = strict_schema(schema.model_json_schema())
                if strict is None:
                    raise CapabilityMismatch(
                        f"{schema.__name__} has free-form mappings that strict structured output cannot express; "
                        "declare structured_output: json_schema for this role"
                    )
                body["response_format"] = {"type": "json_schema", "json_schema": {"name": schema_name(schema), "schema": strict, "strict": True}}
            else:
                text = system + schema_instruction(schema)
                if mode == "json_schema":
                    body["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {"name": schema_name(schema), "schema": schema.model_json_schema(), "strict": False},
                    }
                elif mode == "json_mode":
                    body["response_format"] = {"type": "json_object"}
        if self.role.reasoning_effort is not None:
            param = self._reasoning_param()
            if param is None:  # pragma: no cover - rejected by check_role at construction
                raise CapabilityMismatch(f"reasoning_effort is not declared for ({self.name}, {self.role.model})")
            body[param] = self.role.reasoning_effort
        body["messages"] = [{"role": "system", "content": text}, {"role": "user", "content": user}]
        return "/chat/completions", {}, body

    def _stream_body(self, body: dict[str, Any]) -> dict[str, Any]:
        return {**body, "stream": True, "stream_options": {"include_usage": True}}

    def _stream_consumer(self, on_delta: Callable[[str], None], stage: str) -> "ChatStream":
        return ChatStream(on_delta, stage)

    def _parse(self, data: dict[str, Any], mode: str, ctx: CallContext, call: MeteredCall, body: dict[str, Any]) -> str:
        call.usage = parse_usage(data.get("usage"))
        call.returned_model = data.get("model") if isinstance(data.get("model"), str) else None
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderResponseError("reply has no choices", stage=ctx.stage)
        choice = choices[0]
        finish = choice.get("finish_reason")
        call.extra["stop_reason"] = finish
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        if message.get("refusal"):
            raise ProviderRefusal("model refused", stage=ctx.stage)
        if finish == "length":
            raise ProviderIncomplete("incomplete reply: truncated at the output token limit", stage=ctx.stage)
        if finish == "content_filter":
            raise ProviderRefusal("reply stopped by the provider's content filter", stage=ctx.stage)
        if finish != "stop":
            raise ProviderIncomplete(f"incomplete or unsupported model response (finish_reason {finish!r})", stage=ctx.stage)
        content = message.get("content")
        if not isinstance(content, str):
            raise ProviderResponseError("reply has no text content", stage=ctx.stage)
        return content


class ChatStream:
    """Folds Chat Completions stream chunks into the non-streaming reply shape."""

    def __init__(self, on_delta: Callable[[str], None], stage: str):
        self.on_delta, self.stage = on_delta, stage
        self.model: str | None = None
        self.content: list[str] = []
        self.refusal: list[str] = []
        self.finish: str | None = None
        self.usage: dict[str, Any] | None = None
        self.done = False

    def feed(self, event: str, data: str) -> None:
        if data.strip() == "[DONE]":
            self.done = True
            return
        try:
            chunk = json.loads(data)
        except ValueError as error:
            raise ProviderResponseError("stream chunk is not JSON", stage=self.stage) from error
        if not isinstance(chunk, dict):
            raise ProviderResponseError("stream chunk is not a JSON object", stage=self.stage)
        if isinstance(chunk.get("error"), dict):
            raise ProviderResponseError("provider stream error", stage=self.stage)
        if isinstance(chunk.get("model"), str):
            self.model = chunk["model"]
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            return
        for choice in choices:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            if isinstance(delta.get("content"), str) and delta["content"]:
                self.content.append(delta["content"])
                self.on_delta(delta["content"])
            if isinstance(delta.get("refusal"), str) and delta["refusal"]:
                self.refusal.append(delta["refusal"])
            if choice.get("finish_reason") is not None:
                self.finish = choice["finish_reason"]

    def result(self) -> dict[str, Any]:
        if self.finish is None or not self.done:
            raise ProviderIncomplete("incomplete reply: the stream ended without finish_reason and [DONE]", stage=self.stage)
        message: dict[str, Any] = {"role": "assistant", "content": "".join(self.content)}
        if self.refusal:
            message["refusal"] = "".join(self.refusal)
        reply: dict[str, Any] = {"model": self.model, "choices": [{"index": 0, "finish_reason": self.finish, "message": message}]}
        if self.usage is not None:
            reply["usage"] = self.usage
        return reply
