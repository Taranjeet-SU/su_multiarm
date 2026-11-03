"""Shared machinery of the HTTP chat providers (one exchange per call, metered, typed failures).

Streaming (``complete_json_stream``/``complete_text_stream``): the same request with ``stream:
true``; server-sent events are folded back into the provider's non-streaming reply shape by a
per-provider consumer and then parsed by the same ``_parse`` (stop reasons, truncation, refusal,
usage), so a streamed call fails exactly where the non-streamed one would. Each text fragment is
handed to ``on_delta`` as it arrives; deltas are provisional raw reply text (JSON in structured
modes) and the validated reply is the only result. Streaming requires the role to declare
``capabilities.streaming: true`` (``CapabilityMismatch`` otherwise, before any call).
"""

from __future__ import annotations

import os
from typing import Any, Callable, ClassVar

import httpx
from pydantic import BaseModel

from .base import (
    FENCE,
    CallContext,
    CallEstimate,
    CallMeter,
    CapabilityMismatch,
    HttpCall,
    MeteredCall,
    ProviderError,
    RoleSpec,
    T,
    Usage,
    timeout_for,
    user_content,
    validate_reply,
)
from .capabilities import ModelCapabilities, check_role, lookup


class MissingSecret(ProviderError, ValueError):
    """The role's ``api_key_env`` is not set. Keys are only ever read from the named variable."""

    category = "missing_secret"


def secret(name: str | None, stage: str) -> str | None:
    if not name:
        return None
    value = os.environ.get(name)
    if not value:
        raise MissingSecret(f"required environment variable {name} is not set", stage=stage)
    return value


class HttpChatProvider:
    """Base for ``anthropic``, ``openai`` and ``openai_compatible``.

    Subclasses implement ``_request(system, user, schema, mode) -> (url_path, headers, body)`` and
    ``_parse(data, mode, ctx, call, body) -> text`` (``_parse`` must set ``call.usage`` before raising
    on a non-final stop so truncated calls are still accounted)."""

    name: ClassVar[str] = ""
    default_base_url: ClassVar[str] = ""

    def __init__(self, config: Any, transport: httpx.BaseTransport | None = None):
        self.role = config if isinstance(config, RoleSpec) else RoleSpec.of(config)
        if self.role.provider != self.name:
            raise CapabilityMismatch(f"{type(self).__name__} cannot serve provider {self.role.provider!r}")
        self.caps: ModelCapabilities = lookup(self.role.provider, self.role.model)
        problems = check_role(
            self.role.provider,
            self.role.model,
            self.role.structured_output,
            self.role.prompt_caching,
            self.role.reasoning_effort,
        )
        if problems:
            raise CapabilityMismatch("; ".join(f"{key}: {message}" for key, message in problems))
        self.http = HttpCall(transport)

    # --- public API ----------------------------------------------------------------------------

    def capabilities(self) -> ModelCapabilities:
        return self.caps

    def structured_mode(self) -> str:
        """Wire mode for schema calls: ``native:<kind>``, ``json_schema``, ``json_mode`` or ``prompt``."""
        declared = self.role.structured_output
        if declared == "native":
            return f"native:{self.caps.native_kind}"
        return "prompt" if declared == "none" else declared

    def complete_json(self, system: str, payload: Any, schema: type[T], budget: CallMeter, ctx: CallContext) -> tuple[T, Usage]:
        raw, usage = self.complete_json_raw(system, payload, schema, budget, ctx)
        return validate_reply(schema, raw, ctx.stage), usage

    def complete_json_raw(self, system: str, payload: Any, schema: type[BaseModel], budget: CallMeter, ctx: CallContext) -> tuple[str, Usage]:
        return self._exchange(system, user_content(payload), schema, self.structured_mode(), budget, ctx)

    def complete_text(self, system: str, payload: Any, budget: CallMeter, ctx: CallContext) -> tuple[str, Usage]:
        return self._exchange(system, user_content(payload), None, "text", budget, ctx)

    def complete_json_stream(
        self, system: str, payload: Any, schema: type[T], budget: CallMeter, ctx: CallContext, on_delta: Callable[[str], None]
    ) -> tuple[T, Usage]:
        raw, usage = self._exchange(system, user_content(payload), schema, self.structured_mode(), budget, ctx, on_delta=self._require_streaming(on_delta, ctx))
        return validate_reply(schema, raw, ctx.stage), usage

    def complete_text_stream(self, system: str, payload: Any, budget: CallMeter, ctx: CallContext, on_delta: Callable[[str], None]) -> tuple[str, Usage]:
        return self._exchange(system, user_content(payload), None, "text", budget, ctx, on_delta=self._require_streaming(on_delta, ctx))

    def _require_streaming(self, on_delta: Callable[[str], None], ctx: CallContext) -> Callable[[str], None]:
        if not self.role.streaming:
            raise CapabilityMismatch(f"role for ({self.name}, {self.role.model}) does not declare capabilities.streaming", stage=ctx.stage)
        if not self.caps.streaming:  # pragma: no cover - every registry entry streams today
            raise CapabilityMismatch(f"({self.name}, {self.role.model}) does not stream", stage=ctx.stage)
        return on_delta

    # --- exchange ------------------------------------------------------------------------------

    def _exchange(
        self,
        system: str,
        user: str,
        schema: type[BaseModel] | None,
        mode: str,
        budget: CallMeter,
        ctx: CallContext,
        on_delta: Callable[[str], None] | None = None,
    ) -> tuple[str, Usage]:
        path, headers, body = self._request(system, user, schema, mode)  # CapabilityMismatch before any call
        if on_delta is not None:
            body = self._stream_body(body)
        key = secret(self.role.api_key_env, ctx.stage)
        headers = {**headers, **self._auth(key)}
        url = (self.role.base_url or self.default_base_url).rstrip("/") + path
        estimate = CallEstimate.of(len(system) + len(user) + len(str(body.get("tools") or body.get("response_format") or "")), self.role.max_tokens)
        with MeteredCall(self, budget, ctx, mode, estimate) as call:
            if on_delta is None:
                data = self.http.post(url, headers=headers, body=body, timeout=timeout_for(self.role, budget), stage=ctx.stage)
            else:
                consumer = self._stream_consumer(on_delta, ctx.stage)
                call.extra["stream"] = True
                data = self.http.post_stream(url, headers=headers, body=body, timeout=timeout_for(self.role, budget), stage=ctx.stage, consumer=consumer)
            text = self._parse(data, mode, ctx, call, body)
        if mode.startswith("native:") or mode == "text":
            return text, call.usage
        return FENCE.sub("", text.strip()), call.usage

    def _auth(self, key: str | None) -> dict[str, str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def _request(self, system: str, user: str, schema: type[BaseModel] | None, mode: str) -> tuple[str, dict[str, str], dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError

    def _parse(self, data: dict[str, Any], mode: str, ctx: CallContext, call: MeteredCall, body: dict[str, Any]) -> str:  # pragma: no cover
        raise NotImplementedError

    def _stream_body(self, body: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def _stream_consumer(self, on_delta: Callable[[str], None], stage: str) -> Any:  # pragma: no cover
        raise NotImplementedError
