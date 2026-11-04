"""Shared machinery of the HTTP chat providers (one exchange per call, metered, typed failures).

Streaming (``complete_json_stream``/``complete_text_stream``): the same request with ``stream:
true``; server-sent events are folded back into the provider's non-streaming reply shape by a
per-provider consumer and then parsed by the same ``_parse`` (stop reasons, truncation, refusal,
usage), so a streamed call fails exactly where the non-streamed one would. Each text fragment is
handed to ``on_delta`` as it arrives; deltas are provisional raw reply text (JSON in structured
modes) and the validated reply is the only result. Streaming requires the role to declare
``capabilities.streaming: true`` (``CapabilityMismatch`` otherwise, before any call).

Output ceiling (QQ-01): a reply whose reported output tokens exceed the role's ``max_tokens`` is
``ProviderLimitIgnored`` (the server did not honour the ceiling the package sent); the call is
still metered with the reported usage.
"""

from __future__ import annotations

import os
import threading
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
    ProviderLimitIgnored,
    ProviderSchemaTooComplex,
    RoleSpec,
    SchemaViolation,
    StrictSchemaError,
    T,
    Usage,
    timeout_for,
    user_content,
    output_format_limit_problems,
    output_format_schema,
    validate_reply,
)
from .capabilities import ModelCapabilities, check_role, lookup


def _complexity_problems(schema: type[BaseModel]) -> list[str]:
    """``output_format`` complexity problems of ``schema`` (free-form mappings are left to
    ``_request``, which refuses them with ``StrictSchemaError`` before any call)."""
    try:
        wire = output_format_schema(schema.model_json_schema(), schema.__name__)
    except StrictSchemaError:
        return []
    return output_format_limit_problems(wire, schema.__name__)


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
            max_tokens=self.role.max_tokens,
        )
        if problems:
            raise CapabilityMismatch("; ".join(f"{key}: {message}" for key, message in problems))
        self.http = HttpCall(transport)
        self._prompt_fallback: dict[str, dict[str, str]] = {}  # schema name -> {from, reason} of a recorded switch to prompt mode
        self._last = threading.local()  # the stop reason of this thread's last call (for SchemaViolation)

    # --- public API ----------------------------------------------------------------------------

    def capabilities(self) -> ModelCapabilities:
        return self.caps

    def structured_mode(self, schema: type[BaseModel] | None = None) -> str:
        """Wire mode for schema calls: ``native:<kind>``, ``json_schema``, ``json_mode`` or ``prompt``.
        ``prompt`` for a schema this provider already switched away from native (see
        ``_schema_exchange``)."""
        declared = self.role.structured_output
        if declared == "native":
            if schema is not None and schema.__name__ in self._prompt_fallback:
                return "prompt"
            return f"native:{self.caps.native_kind}"
        return "prompt" if declared == "none" else declared

    def complete_json(self, system: str, payload: Any, schema: type[T], budget: CallMeter, ctx: CallContext) -> tuple[T, Usage]:
        raw, usage = self.complete_json_raw(system, payload, schema, budget, ctx)
        return self._validated(schema, raw, ctx), usage

    def _validated(self, schema: type[T], raw: str, ctx: CallContext) -> T:
        try:
            return validate_reply(schema, raw, ctx.stage)
        except SchemaViolation as error:
            error.stop_reason = getattr(self._last, "stop_reason", None)  # this thread's last call
            raise

    def complete_json_raw(self, system: str, payload: Any, schema: type[BaseModel], budget: CallMeter, ctx: CallContext) -> tuple[str, Usage]:
        return self._schema_exchange(system, user_content(payload), schema, budget, ctx)

    def _schema_exchange(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        budget: CallMeter,
        ctx: CallContext,
        on_delta: Callable[[str], None] | None = None,
    ) -> tuple[str, Usage]:
        """One schema call, with the structured-output fallback of the 2.0.0a3 parity fix.

        * Pre-flight (no network): a native ``output_format`` schema over the provider's complexity
          ceilings (``output_format_problems``) is sent in prompt mode when the role's ``native`` is
          only the registry default, recorded as ``structured_output_fallback`` (reason
          ``schema_too_complex_preflight``); a declared ``native`` is a ``CapabilityMismatch``
          before any call.
        * At run time: a native call refused as ``ProviderSchemaTooComplex`` (HTTP 400, nothing
          generated) is sent **once** more in prompt mode, again only for a registry-default
          ``native``: an explicit, recorded mode switch (reason ``schema_too_complex``), never a
          retry of the same request. A declared ``native`` fails with the typed error.

        The record is in the call record and the ``model_call`` trace event; later calls of this
        provider with the same schema go straight to prompt mode and carry it too."""
        mode = self.structured_mode(schema)
        if mode == "prompt" and self.role.structured_output == "native":
            return self._exchange(system, user, schema, mode, budget, ctx, on_delta=on_delta, extra=self._fallback_record(schema))
        if mode == "native:output_format":
            problems = _complexity_problems(schema)
            if problems and not self.role.structured_output_defaulted:
                raise CapabilityMismatch("; ".join(problems), stage=ctx.stage)
            if problems:
                self._prompt_fallback[schema.__name__] = {"from": mode, "reason": "schema_too_complex_preflight"}
                return self._exchange(system, user, schema, "prompt", budget, ctx, on_delta=on_delta, extra=self._fallback_record(schema))
        try:
            return self._exchange(system, user, schema, mode, budget, ctx, on_delta=on_delta)
        except ProviderSchemaTooComplex:
            if not (mode.startswith("native:") and self.role.structured_output_defaulted):
                raise
        self._prompt_fallback[schema.__name__] = {"from": mode, "reason": ProviderSchemaTooComplex.category}
        return self._exchange(system, user, schema, "prompt", budget, ctx, on_delta=on_delta, extra=self._fallback_record(schema))

    def _fallback_record(self, schema: type[BaseModel]) -> dict[str, Any]:
        return {"structured_output_fallback": {**self._prompt_fallback[schema.__name__], "to": "prompt", "schema": schema.__name__}}

    def complete_text(self, system: str, payload: Any, budget: CallMeter, ctx: CallContext) -> tuple[str, Usage]:
        return self._exchange(system, user_content(payload), None, "text", budget, ctx)

    def complete_json_stream(
        self, system: str, payload: Any, schema: type[T], budget: CallMeter, ctx: CallContext, on_delta: Callable[[str], None]
    ) -> tuple[T, Usage]:
        raw, usage = self._schema_exchange(system, user_content(payload), schema, budget, ctx, on_delta=self._require_streaming(on_delta, ctx))
        return self._validated(schema, raw, ctx), usage

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
        extra: dict[str, Any] | None = None,
    ) -> tuple[str, Usage]:
        path, headers, body = self._request(system, user, schema, mode)  # CapabilityMismatch before any call
        if on_delta is not None:
            body = self._stream_body(body)
        key = secret(self.role.api_key_env, ctx.stage)
        headers = {**headers, **self._auth(key)}
        url = (self.role.base_url or self.default_base_url).rstrip("/") + path
        estimate = CallEstimate.of(len(system) + len(user) + len(str(body.get("tools") or body.get("response_format") or body.get("output_config") or "")), self.role.max_tokens)
        with MeteredCall(self, budget, ctx, mode, estimate) as call:
            call.extra.update(extra or {})
            if on_delta is None:
                data = self.http.post(url, headers=headers, body=body, timeout=timeout_for(self.role, budget), stage=ctx.stage)
            else:
                consumer = self._stream_consumer(on_delta, ctx.stage)
                call.extra["stream"] = True
                data = self.http.post_stream(url, headers=headers, body=body, timeout=timeout_for(self.role, budget), stage=ctx.stage, consumer=consumer)
            text = self._parse(data, mode, ctx, call, body)
            self._last.stop_reason = call.extra.get("stop_reason")
            if call.usage.reported and call.usage.output_tokens > self.role.max_tokens:
                raise ProviderLimitIgnored(
                    f"reply used {call.usage.output_tokens} output tokens, more than max_tokens {self.role.max_tokens} "
                    f"sent as {self._ceiling_param(body)}; the server ignored the output ceiling",
                    stage=ctx.stage,
                )
        if mode.startswith("native:") or mode == "text":
            return text, call.usage
        return FENCE.sub("", text.strip()), call.usage

    @staticmethod
    def _ceiling_param(body: dict[str, Any]) -> str:
        return next((k for k in ("max_tokens", "max_completion_tokens") if k in body), "max_tokens")

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
