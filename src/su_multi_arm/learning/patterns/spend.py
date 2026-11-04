"""Spend caps of the pattern learner (addendum §3.6.4, the subset L4b needs): per run and per day.

- **Per run**: a ``runtime.budget.Budget`` with ``max_calls_per_run`` and ``max_usd_per_run`` (the
  existing reservation and USD accounting, priced from ``budgets.price_table``).
- **Per day**: ``DaySpendLedger``, a SQLite table keyed by ``(scope, role, day)`` with the day taken
  in the configured timezone. ``reserve`` is one conditional ``UPDATE`` inside ``BEGIN IMMEDIATE``
  (``committed + reserved + usd <= cap and calls + 1 <= max_calls``), so two processes cannot
  overshoot the cap together. A crash between ``reserve`` and ``commit`` leaves the reservation
  counted: it is never refunded silently.

``LearnerMeter`` is the ``providers.CallMeter`` handed to the provider: it reserves the run budget
and then the day ledger **before** the request is sent (either refusal raises ``BudgetExhausted``
and nothing is sent), and commits the provider-reported usage (the estimate when none is reported)
to both afterwards, also for a failed call. Nothing is retried.

Amounts are stored as integer micro-dollars, so comparisons against the cap are exact.
"""

from __future__ import annotations

import math
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ...core.errors import BudgetExhausted, ConfigError
from ...providers.base import CallEstimate
from ...runtime.budget import Budget, BudgetLimits, PriceTable

SCOPE = "learning.patterns"
ROLE = "pattern_learner"
MICRO = 1_000_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spend (
    scope TEXT NOT NULL, role TEXT NOT NULL, day TEXT NOT NULL,
    committed_micro INTEGER NOT NULL DEFAULT 0, reserved_micro INTEGER NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, role, day)
)
"""


def micro(usd: float) -> int:
    """USD to micro-dollars, rounded up (a reservation never under-counts)."""
    return int(math.ceil(round(usd * MICRO, 6)))


@dataclass
class DayReservation:
    day: str
    micro: int
    open: bool = True


class DaySpendLedger:
    """Persistent per-day caps for one ``(scope, role)``. ``max_usd`` ``None`` means calls only."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_calls: int,
        max_usd: float | None,
        tz: str = "UTC",
        scope: str = SCOPE,
        role: str = ROLE,
        clock: Callable[[], datetime] | None = None,
    ):
        self.path = Path(path)
        self.max_calls, self.max_usd = max_calls, max_usd
        self.tz = ZoneInfo(tz)
        self.scope, self.role = scope, role
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.execute("PRAGMA busy_timeout = 30000")
        return db

    def day(self) -> str:
        return self.clock().astimezone(self.tz).date().isoformat()

    def reserve(self, usd: float, calls: int = 1) -> DayReservation:
        day, amount = self.day(), micro(usd)
        cap = None if self.max_usd is None else micro(self.max_usd)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO spend (scope, role, day) VALUES (?, ?, ?)", (self.scope, self.role, day))
            changed = db.execute(
                "UPDATE spend SET reserved_micro = reserved_micro + ?, calls = calls + ? "
                "WHERE scope = ? AND role = ? AND day = ? AND calls + ? <= ? "
                "AND (? IS NULL OR committed_micro + reserved_micro + ? <= ?)",
                (amount, calls, self.scope, self.role, day, calls, self.max_calls, cap, amount, cap),
            ).rowcount
            if changed != 1:
                row = db.execute(
                    "SELECT committed_micro, reserved_micro, calls FROM spend WHERE scope = ? AND role = ? AND day = ?", (self.scope, self.role, day)
                ).fetchone()
                db.execute("ROLLBACK")
                committed, reserved, used_calls = row
                if used_calls + calls > self.max_calls:
                    raise BudgetExhausted("day_calls", calls, self.max_calls - used_calls)
                raise BudgetExhausted("day_usd", usd, ((cap or 0) - committed - reserved) / MICRO)
            db.execute("COMMIT")
        finally:
            db.close()
        return DayReservation(day, amount)

    def commit(self, reservation: DayReservation, usd: float) -> None:
        """Replace the reservation by the measured spend (an overrun is recorded, never hidden)."""
        if not reservation.open:
            return
        with closing(self._connect()) as db:
            db.execute(
                "UPDATE spend SET reserved_micro = reserved_micro - ?, committed_micro = committed_micro + ? WHERE scope = ? AND role = ? AND day = ?",
                (reservation.micro, micro(usd), self.scope, self.role, reservation.day),
            )
        reservation.open = False

    def release(self, reservation: DayReservation) -> None:
        """Undo a reservation whose call was never sent (the call count is returned as well)."""
        if not reservation.open:
            return
        with closing(self._connect()) as db:
            db.execute(
                "UPDATE spend SET reserved_micro = reserved_micro - ?, calls = calls - 1 WHERE scope = ? AND role = ? AND day = ?",
                (reservation.micro, self.scope, self.role, reservation.day),
            )
        reservation.open = False

    def usage(self, day: str | date | None = None) -> dict[str, Any]:
        key = (day.isoformat() if isinstance(day, date) else day) or self.day()
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT committed_micro, reserved_micro, calls FROM spend WHERE scope = ? AND role = ? AND day = ?", (self.scope, self.role, key)
            ).fetchone()
        committed, reserved, calls = row or (0, 0, 0)
        return {
            "scope": self.scope,
            "role": self.role,
            "day": key,
            "committed_usd": committed / MICRO,
            "reserved_usd": reserved / MICRO,
            "calls": calls,
            "max_calls": self.max_calls,
            "max_usd": self.max_usd,
        }


class LearnerMeter:
    """``providers.CallMeter`` for the ``pattern_learner`` role: run budget plus day ledger."""

    def __init__(self, *, max_calls: int, max_usd: float | None, price_table: PriceTable | None, pricing_key: str | None, ledger: DaySpendLedger):
        if max_usd is not None and (price_table is None or pricing_key not in price_table.prices):
            raise ConfigError(f"a USD cap needs a price for {pricing_key!r} in budgets.price_table", "models.roles.pattern_learner.pricing_key")
        if ledger.max_usd is not None and (price_table is None or pricing_key not in price_table.prices):
            raise ConfigError(f"max_usd_per_day needs a price for {pricing_key!r} in budgets.price_table", "models.roles.pattern_learner.pricing_key")
        self.budget = Budget(BudgetLimits(max_llm_calls=max_calls, max_usd=max_usd), price_table)
        self.price_table, self.pricing_key, self.ledger = price_table, pricing_key, ledger
        self.calls: list[dict[str, Any]] = []
        self._run: list[Any] = []
        self._day: DayReservation | None = None
        self._lock = threading.Lock()

    def priced(self) -> bool:
        return self.price_table is not None and self.pricing_key is not None and self.pricing_key in self.price_table.prices

    def estimate_usd(self, estimate: CallEstimate) -> float:
        return self.price_table.cost(self.pricing_key, estimate.input_tokens, estimate.output_tokens) if self.priced() else 0.0  # type: ignore[union-attr, arg-type]

    def reserve(self, estimate: CallEstimate | None = None) -> None:
        estimate = estimate or CallEstimate(input_tokens=0, output_tokens=0)
        run = self.budget.reserve_call(stage=ROLE, pricing_key=self.pricing_key, input_tokens=estimate.input_tokens, output_tokens=estimate.output_tokens)
        usd = sum(r.estimate for r in run if r.kind == "usd")
        try:
            day = self.ledger.reserve(usd)
        except BudgetExhausted:
            for reservation in run:
                self.budget.release(reservation)
            raise
        with self._lock:
            self._run, self._day = run, day

    def record(self, item: dict[str, Any]) -> None:
        usage = item.get("usage") or {}
        reported = bool(usage.get("reported"))
        actual_usd = 0.0
        with self._lock:
            run, day = self._run, self._day
            self._run, self._day = [], None
        for reservation in run:
            if reservation.committed is not None:
                continue
            if reservation.kind == "calls":
                self.budget.commit(reservation, 1)
            elif not reported:
                self.budget.commit(reservation, reservation.estimate)
                if reservation.kind == "usd":
                    actual_usd = reservation.estimate
            elif reservation.kind == "input_tokens":
                self.budget.commit(reservation, int(usage.get("input_tokens") or 0))
            elif reservation.kind == "output_tokens":
                self.budget.commit(reservation, int(usage.get("output_tokens") or 0))
            elif reservation.kind == "usd":
                actual_usd = self.price_table.cost(  # type: ignore[union-attr]
                    reservation.pricing_key,
                    int(usage.get("input_tokens") or 0),
                    int(usage.get("output_tokens") or 0),
                    int(usage.get("cached_input_tokens") or 0),
                    int(usage.get("cache_creation_input_tokens") or 0),
                )
                self.budget.commit(reservation, actual_usd)
        if day is not None:
            self.ledger.commit(day, actual_usd)
        with self._lock:
            self.calls.append({**item, "usd": actual_usd if self.priced() else None})

    def summary(self) -> dict[str, Any]:
        snapshot = self.budget.snapshot()
        return {
            "calls": len(self.calls),
            "usd": snapshot["used"]["usd"] if self.priced() else None,
            "limits": {"max_calls": self.budget.limits.max_llm_calls, "max_usd": self.budget.limits.max_usd},
            "exhausted": snapshot["exhausted"],
            "day": self.ledger.usage(),
        }
