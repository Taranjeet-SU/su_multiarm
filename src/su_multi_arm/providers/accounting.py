"""Request-path call metering: provider usage into ``RunContext.budget`` and ``RunContext.trace``.

Before a call: ``Budget.reserve_call`` reserves one call, the input-token estimate, the
``max_tokens`` output ceiling and (when a price table is loaded and prices the role) the USD cost of
that estimate. After the call, successful or not: every reservation is committed with the reported
usage (calls 1, input/output tokens, USD from ``PriceTable.cost`` with cached and cache-write rates).
When the provider reports no usage, the token and USD reservations are committed at their estimate
(conservative; never silently zero) and the trace says ``usage_reported: false``. A call that
provably never reached the provider (``sent: false``: no connection to it or to the egress proxy)
commits its call slot but no tokens or USD, and its ``serving.answers`` reservation is released
(not booked against the daily cap); the trace event says ``sent: false``. The trace event
``model_call`` carries provider, requested and returned model, mode, outcome, token counts incl.
cached tokens, cost and timing; never prompts or replies.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .base import CallEstimate

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime.context import RunContext


LOGGER = logging.getLogger(__name__)


class RunBudgetMeter:
    def __init__(self, ctx: RunContext, stage: str, pricing_key: str | None):
        self.ctx, self.stage, self.pricing_key = ctx, stage, pricing_key
        self._reservations: list[Any] = []
        self._spend: Any = None  # the serving.answers reservation of the call in flight

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
        spend = getattr(self.ctx, "spend", None)
        if spend is not None:
            usd = sum(r.estimate for r in self._reservations if r.kind == "usd")
            try:
                self._spend = spend.reserve(usd, self.pricing_key)
            except Exception:
                for reservation in self._reservations:
                    self.ctx.budget.release(reservation)
                self._reservations = []
                raise

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
        unsent = item.get("sent") is False
        for reservation in self._reservations:
            if reservation.committed is not None:
                continue
            if reservation.kind == "calls":
                budget.commit(reservation, 1)  # an attempt counts against the request's call cap
            elif unsent:
                budget.commit(reservation, 0)  # the request never reached the provider: nothing spent
            elif not reported:
                budget.commit(reservation, reservation.estimate)
            elif reservation.kind == "input_tokens":
                budget.commit(reservation, int(usage.get("input_tokens") or 0))
            elif reservation.kind == "output_tokens":
                budget.commit(reservation, int(usage.get("output_tokens") or 0))
            elif reservation.kind == "usd":
                budget.commit(reservation, cost if cost is not None else reservation.estimate)
        estimated = sum(r.estimate for r in self._reservations if r.kind == "usd")
        self._reservations = []
        if self._spend is not None:
            if unsent:
                # staging F6: a call refused before sending (no connection to the provider or the
                # egress proxy) is released from the serving.answers day ledger, never booked
                self.ctx.spend.release(self._spend)
            else:  # serving.answers: the measured spend (the estimate when unreported)
                self.ctx.spend.commit(self._spend, cost if cost is not None else estimated)
            self._spend = None
        if item.get("success") is False:
            stop = item.get("stop_reason")
            cause = item.get("incomplete_cause")
            safe_stop = stop if (stop is None or isinstance(stop, str)) and stop in {"max_tokens", "length", "end_turn", "stop", "tool_use", "refusal", "pause_turn", "content_filter", None} else "other"
            LOGGER.warning("model call failed: stage=%s category=%s stop_reason=%s incomplete_cause=%s attempt=%s output_tokens=%s max_tokens=%s seconds=%.2f",
                           self.stage, item.get("error_category"), safe_stop, cause if cause in {"max_tokens", "stop", "stream_end"} else None, item.get("attempt", 1),
                           usage.get("output_tokens") if reported else None, item.get("max_tokens"), item.get("seconds") or 0)
        self.ctx.trace.event(
            "model_call",
            stage=item.get("stage"),
            provider=item.get("provider"),
            model=item.get("model"),
            returned_model=item.get("returned_model"),
            mode=item.get("mode"),
            success=item.get("success"),
            error_category=item.get("error_category"),
            **({"sent": False} if unsent else {}),
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
            # A recorded native -> prompt mode switch after ``ProviderSchemaTooComplex`` (else None).
            structured_output_fallback=item.get("structured_output_fallback"),
            # 2026-11-09: streamed calls carry the first reply token's time; thinking/effort what was sent
            **{k: item[k] for k in ("stream", "first_token_seconds", "thinking", "thinking_requested", "effort") if k in item},
            # M0 (§6.13): which attempt of a logical call this was, why it was retried, and why an
            # incomplete reply was incomplete (``max_tokens`` | ``stop`` | ``stream_end``)
            **{k: item[k] for k in ("attempt", "retry_reason", "incomplete_cause", "stream_stop_reason") if k in item},
        )


class RetryBudgetMeter(RunBudgetMeter):
    """``RunBudgetMeter`` of a caller-level retry (staging round 6: the generator's schema repair).
    Metered exactly like the first call; its call records say ``attempt`` and ``retry_reason``
    (the provider's own M0 retries inside it keep their own ``attempt``/``retry_reason``)."""

    def __init__(self, ctx: RunContext, stage: str, pricing_key: str | None, *, attempt: int, reason: str):
        super().__init__(ctx, stage, pricing_key)
        self.attempt, self.reason = attempt, reason

    def record(self, item: dict[str, Any]) -> None:
        super().record({"attempt": self.attempt, "retry_reason": self.reason, **item})
