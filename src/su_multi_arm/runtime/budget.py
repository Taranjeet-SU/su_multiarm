"""Request budget with a reservation API (plan §3.4).

Kinds: ``calls``, ``input_tokens``, ``output_tokens``, ``usd``, ``wall_clock``. A reservation is
taken before every provider call and committed with the actual usage afterwards. USD accounting
requires an operator-supplied price table; there are no bundled prices. Exhaustion raises
``BudgetExhausted`` and the caller records it in the trace. No budget ever downgrades a call.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

import yaml
from pydantic import Field

from ..core.errors import BudgetExhausted, ConfigError, DeadlineExceeded, RunCancelled
from ..models import StrictModel

BudgetKind = Literal["calls", "input_tokens", "output_tokens", "usd", "wall_clock"]
KINDS: tuple[str, ...] = ("calls", "input_tokens", "output_tokens", "usd", "wall_clock")


class ModelPrice(StrictModel):
    input_per_million: float = Field(ge=0)
    output_per_million: float = Field(ge=0)
    cached_input_per_million: float | None = Field(default=None, ge=0)
    # Prompt-cache writes (Anthropic ``cache_creation_input_tokens``); defaults to the input rate.
    cache_write_input_per_million: float | None = Field(default=None, ge=0)


class PriceTable(StrictModel):
    """Operator-supplied, versioned. Keys are ``pricing_key`` values (usually the pinned model id)."""

    version: str = Field(min_length=1)
    currency: Literal["USD"] = "USD"
    prices: dict[str, ModelPrice] = Field(default_factory=dict)

    def cost(
        self, pricing_key: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0, cache_write_tokens: int = 0
    ) -> float:
        """USD for one call. ``input_tokens`` is the total prompt size including cached reads and
        cache writes (the providers' normalised ``Usage``)."""
        price = self.prices.get(pricing_key)
        if price is None:
            raise ConfigError(f"no price for {pricing_key!r} in price table {self.version}", "budgets.price_table")
        uncached = max(0, input_tokens - cached_tokens - cache_write_tokens)
        cached_rate = price.cached_input_per_million if price.cached_input_per_million is not None else price.input_per_million
        write_rate = price.cache_write_input_per_million if price.cache_write_input_per_million is not None else price.input_per_million
        return (
            uncached * price.input_per_million
            + cached_tokens * cached_rate
            + cache_write_tokens * write_rate
            + output_tokens * price.output_per_million
        ) / 1_000_000


def load_price_table(path: str | Path) -> PriceTable:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return PriceTable.model_validate(data)


class BudgetLimits(StrictModel):
    max_llm_calls: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_usd: float | None = Field(default=None, gt=0)
    deadline_seconds: float | None = Field(default=None, gt=0)

    def limit(self, kind: str) -> float | None:
        return {
            "calls": self.max_llm_calls,
            "input_tokens": self.max_input_tokens,
            "output_tokens": self.max_output_tokens,
            "usd": self.max_usd,
            "wall_clock": self.deadline_seconds,
        }[kind]


@dataclass
class Reservation:
    kind: str
    estimate: float
    committed: float | None = None
    stage: str | None = None
    pricing_key: str | None = None


@dataclass
class Usage:
    reserved: dict[str, float] = field(default_factory=lambda: {k: 0.0 for k in KINDS})
    used: dict[str, float] = field(default_factory=lambda: {k: 0.0 for k in KINDS})
    exhausted: list[dict] = field(default_factory=list)


class Budget:
    """Thread-safe reservations against ``BudgetLimits``; USD via ``PriceTable``."""

    def __init__(
        self,
        limits: BudgetLimits,
        price_table: PriceTable | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.limits = limits
        self.price_table = price_table
        self.clock = clock
        self.started = clock()
        self.usage = Usage()
        self._lock = threading.Lock()
        self.cancelled: str | None = None
        if limits.max_usd is not None and price_table is None:
            raise ConfigError("max_usd requires a price table", "budgets.price_table")

    @property
    def deadline(self) -> float | None:
        return None if self.limits.deadline_seconds is None else self.started + self.limits.deadline_seconds

    def remaining(self, kind: str) -> float | None:
        limit = self.limits.limit(kind)
        if limit is None:
            return None
        if kind == "wall_clock":
            return max(0.0, self.started + limit - self.clock())
        with self._lock:
            return limit - self.usage.reserved[kind]

    def cancel(self, reason: str = "cancelled") -> None:
        """Refuse every further reservation (a streaming client went away). Calls already in flight
        are committed as usual; nothing is refunded or hidden."""
        with self._lock:
            if self.cancelled is None:
                self.cancelled = reason
                self.usage.exhausted.append({"kind": "cancelled", "reason": reason, "at": self.clock() - self.started})

    def check_deadline(self) -> None:
        if self.cancelled is not None:
            raise RunCancelled(f"run cancelled ({self.cancelled})")
        deadline = self.deadline
        if deadline is not None and self.clock() >= deadline:
            with self._lock:
                self.usage.exhausted.append({"kind": "wall_clock", "at": self.clock() - self.started})
            raise DeadlineExceeded(f"deadline of {self.limits.deadline_seconds}s exceeded")

    def reserve(self, kind: str, estimate: float, *, stage: str | None = None, pricing_key: str | None = None) -> Reservation:
        if kind not in KINDS:
            raise ValueError(f"unknown budget kind {kind!r}")
        if estimate < 0:
            raise ValueError("estimate must be non-negative")
        if kind == "wall_clock":
            self.check_deadline()
            return Reservation(kind=kind, estimate=estimate, stage=stage)
        if self.cancelled is not None:
            raise RunCancelled(f"run cancelled ({self.cancelled})")
        if kind == "usd" and self.price_table is None:
            raise ConfigError("USD reservations require a price table", "budgets.price_table")
        with self._lock:
            limit = self.limits.limit(kind)
            if limit is not None and self.usage.reserved[kind] + estimate > limit:
                remaining = limit - self.usage.reserved[kind]
                self.usage.exhausted.append({"kind": kind, "requested": estimate, "remaining": remaining, "stage": stage})
                raise BudgetExhausted(kind, estimate, remaining)
            self.usage.reserved[kind] += estimate
        return Reservation(kind=kind, estimate=estimate, stage=stage, pricing_key=pricing_key)

    def reserve_call(
        self,
        *,
        stage: str,
        pricing_key: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> list[Reservation]:
        """Reserve everything one provider call needs. USD is reserved when a USD limit is configured
        (a missing price is then a ``ConfigError``) or when the loaded price table prices the key
        (so spend is accounted even without a limit)."""
        self.check_deadline()
        reservations = [self.reserve("calls", 1, stage=stage)]
        try:
            if input_tokens:
                reservations.append(self.reserve("input_tokens", input_tokens, stage=stage))
            if output_tokens:
                reservations.append(self.reserve("output_tokens", output_tokens, stage=stage))
            priced = self.price_table is not None and pricing_key is not None and pricing_key in self.price_table.prices
            if self.limits.max_usd is not None or priced:
                if not pricing_key:
                    raise ConfigError("a USD budget requires pricing_key on every model role", "budgets.max_usd")
                cost = self.price_table.cost(pricing_key, input_tokens, output_tokens)  # type: ignore[union-attr]
                reservations.append(self.reserve("usd", cost, stage=stage, pricing_key=pricing_key))
        except (BudgetExhausted, ConfigError):
            for reservation in reservations:
                self.release(reservation)
            raise
        return reservations

    def commit(self, reservation: Reservation, actual: float) -> None:
        """Replace the estimate by the measured usage. Overruns are recorded, never hidden."""
        if actual < 0:
            raise ValueError("actual must be non-negative")
        if reservation.committed is not None:
            raise ValueError("reservation already committed")
        with self._lock:
            self.usage.reserved[reservation.kind] += actual - reservation.estimate
            self.usage.used[reservation.kind] += actual
            reservation.committed = actual

    def release(self, reservation: Reservation) -> None:
        if reservation.committed is not None:
            return
        with self._lock:
            self.usage.reserved[reservation.kind] -= reservation.estimate
            reservation.committed = 0.0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "limits": self.limits.model_dump(),
                "reserved": dict(self.usage.reserved),
                "used": dict(self.usage.used),
                "exhausted": list(self.usage.exhausted),
                "cancelled": self.cancelled,
                "elapsed_seconds": self.clock() - self.started,
            }
