"""``provider: none`` — a stage that needs a model fails with a typed error, never an empty reply."""

from __future__ import annotations

from typing import Any, Callable, ClassVar

from pydantic import BaseModel

from .base import CallContext, CallMeter, ProviderUnavailable, RoleSpec, T, Usage
from .capabilities import ModelCapabilities


class NoneProvider:
    name: ClassVar[str] = "none"

    def __init__(self, config: Any = None):
        self.role = RoleSpec(provider="none", model=getattr(config, "model", "") or "")

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(provider="none", pattern="*")

    def _fail(self, ctx: CallContext):
        raise ProviderUnavailable(f"{ctx.stage} requires a model (provider: none)", stage=None)

    def complete_json(self, system: str, payload: Any, schema: type[T], budget: CallMeter, ctx: CallContext) -> tuple[T, Usage]:
        self._fail(ctx)

    def complete_json_raw(self, system: str, payload: Any, schema: type[BaseModel], budget: CallMeter, ctx: CallContext) -> tuple[str, Usage]:
        self._fail(ctx)

    def complete_text(self, system: str, payload: Any, budget: CallMeter, ctx: CallContext) -> tuple[str, Usage]:
        self._fail(ctx)

    def complete_json_stream(
        self, system: str, payload: Any, schema: type[T], budget: CallMeter, ctx: CallContext, on_delta: Callable[[str], None]
    ) -> tuple[T, Usage]:
        self._fail(ctx)

    def complete_text_stream(self, system: str, payload: Any, budget: CallMeter, ctx: CallContext, on_delta: Callable[[str], None]) -> tuple[str, Usage]:
        self._fail(ctx)
