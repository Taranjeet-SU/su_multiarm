"""Request-path call metering: provider usage into ``RunContext.budget`` and ``RunContext.trace``.

Before a call: ``Budget.reserve_call`` reserves one call, the input-token estimate, the
``max_tokens`` output ceiling and (when a price table is loaded and prices the role) the USD cost of
that estimate. After the call, successful or not: every reservation is committed with the reported
usage (calls 1, input/output tokens, USD from ``PriceTable.cost`` with cached and cache-write rates).
When the provider reports no usage, the token and USD reservations are committed at their estimate
(conservative; never silently zero) and the trace says ``usage_reported: false``. The trace event
``model_call`` carries provider, requested and returned model, mode, outcome, token counts incl.
cached tokens, cost and timing; never prompts or replies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import CallEstimate

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime.context import RunContext


class RunBudgetMeter:
    def __init__(self, ctx: RunContext, stage: str, pricing_key: str | None):
        self.ctx, self.stage, self.pricing_key = ctx, stage, pricing_key
        self._reservations: list[Any] = []

    def remaining_seconds(self) -> float | None:
        return self.ctx.budget.remaining("wall_clock")

    def reserve(self, estimate: CallEstimate | None = None) -> None:
        estimate = estimate or CallEstimate(input_tokens=0, output_tokens=0)
        self._reservations = self.ctx.budget.reserve_call(
            stage=self.stage,
            pricing_key=self.pricing_key,
            input_tokens=estimate.input_tokens,
            output_tokens=estimate.output_tokens,
        )

    def _cost(self, usage: dict[str, Any]) -> float | None:
        table = self.ctx.budget.price_table
        if table is None or not self.pricing_key or self.pricing_key not in table.prices:
            return None
        return table.cost(
            self.pricing_key,
            int(usage.get("input_tokens") or 0),
            int(usage.get("output_tokens") or 0),
            int(usage.get("cached_input_tokens") or 0),
            int(usage.get("cache_creation_input_tokens") or 0),
        )

    def record(self, item: dict[str, Any]) -> None:
        usage = item.get("usage") or {}
        reported = bool(usage.get("reported"))
        cost = self._cost(usage) if reported else None
        budget = self.ctx.budget
        for reservation in self._reservations:
            if reservation.committed is not None:
                continue
            if reservation.kind == "calls":
                budget.commit(reservation, 1)
            elif not reported:
                budget.commit(reservation, reservation.estimate)
            elif reservation.kind == "input_tokens":
                budget.commit(reservation, int(usage.get("input_tokens") or 0))
            elif reservation.kind == "output_tokens":
                budget.commit(reservation, int(usage.get("output_tokens") or 0))
            elif reservation.kind == "usd":
                budget.commit(reservation, cost if cost is not None else reservation.estimate)
        self._reservations = []
        self.ctx.trace.event(
            "model_call",
            stage=item.get("stage"),
            provider=item.get("provider"),
            model=item.get("model"),
            returned_model=item.get("returned_model"),
            mode=item.get("mode"),
            success=item.get("success"),
            error_category=item.get("error_category"),
            stop_reason=item.get("stop_reason"),
            seconds=item.get("seconds"),
            usage_reported=reported,
            input_tokens=usage.get("input_tokens") if reported else None,
            output_tokens=usage.get("output_tokens") if reported else None,
            cached_input_tokens=usage.get("cached_input_tokens") if reported else None,
            cache_creation_input_tokens=usage.get("cache_creation_input_tokens") if reported else None,
            reasoning_tokens=usage.get("reasoning_tokens") if reported else None,
            usd=cost,
            pricing_key=self.pricing_key,
            prompt_caching=item.get("prompt_caching"),
            reasoning_effort=item.get("reasoning_effort"),
            max_tokens=item.get("max_tokens"),
        )
