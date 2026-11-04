"""``JsonModel``: the stage-facing facade over a ``ChatProvider`` (names kept from the alpha).

``call`` returns the validated pydantic object; ``call_text`` returns the raw reply (JSON text; for
native tool use the serialised tool input) so callers can keep invalid replies for replay (plan B8).
Provider selection is by the role's ``provider``; there is no fallback between providers.
"""

from __future__ import annotations

from typing import Any, Callable

import httpx
from pydantic import BaseModel

from .anthropic import AnthropicProvider
from .base import CallContext, CallMeter, ChatProvider, T
from .none import NoneProvider
from .openai import OpenAIProvider
from .openai_compatible import OpenAICompatibleProvider

CHAT_PROVIDER_CLASSES: dict[str, type] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "openai_compatible": OpenAICompatibleProvider,
}


def chat_provider(config: Any, transport: httpx.BaseTransport | None = None) -> ChatProvider:
    """The provider for a role config (v2 ``ChatRole`` or v1 ``ModelConfig``). ``transport`` is for
    tests and the Python API only; config can never name one."""
    if config is None or config.provider == "none":
        return NoneProvider(config)
    try:
        cls = CHAT_PROVIDER_CLASSES[config.provider]
    except KeyError:
        raise ValueError(f"unknown chat provider {config.provider!r}") from None
    return cls(config, transport=transport)


class JsonModel:
    """Explicit provider call; no hidden retries, fallback, repair or shared conversation state."""

    def __init__(self, config: Any, transport: httpx.BaseTransport | None = None):
        self.config = config
        self.provider: ChatProvider = chat_provider(config, transport)

    def call(self, system: str, payload: Any, schema: type[T], budget: CallMeter, stage: str) -> T:
        """Validated call: the reply must parse as ``schema`` (``SchemaViolation`` otherwise)."""
        return self.provider.complete_json(system, payload, schema, budget, CallContext(stage))[0]

    def call_text(self, system: str, payload: Any, schema: type[BaseModel], budget: CallMeter, stage: str) -> str:
        """Raw variant for replay: success in the call record means transport + completion."""
        return self.provider.complete_json_raw(system, payload, schema, budget, CallContext(stage))[0]

    def text(self, system: str, payload: Any, budget: CallMeter, stage: str) -> str:
        return self.provider.complete_text(system, payload, budget, CallContext(stage))[0]

    def enforces_schema(self) -> bool:
        """The provider enforces the reply schema on the wire (native structured output: OpenAI
        strict json_schema, Anthropic forced tool use), so enum restrictions in it bind the model."""
        mode = getattr(self.provider, "structured_mode", None)
        return callable(mode) and str(mode()).startswith("native:")

    def streams(self) -> bool:
        """The role declares ``capabilities.streaming`` (never for ``provider: none``)."""
        return self.provider.name != "none" and bool(getattr(self.provider.role, "streaming", False))

    def call_stream(self, system: str, payload: Any, schema: type[T], budget: CallMeter, stage: str, on_delta: Callable[[str], None]) -> T:
        """Streamed ``call``: ``on_delta`` receives provisional raw reply fragments; the return value
        is validated exactly like ``call``'s."""
        return self.provider.complete_json_stream(system, payload, schema, budget, CallContext(stage), on_delta)[0]  # type: ignore[attr-defined]
