"""Persistent review spend ledger (unified merge plan §6.9, §6.13; NB15).

Model review of non-exact identity aliases (M2b-1) and of candidate links (M3) draws on **one
cumulative ceiling** (``crosssource.links.review.usd_ceiling``, at most US$10, owner-approved). The
ledger is a SQLite file with append-only tables (``UPDATE``/``DELETE`` are refused by triggers):

* ``ceiling``: the approved figure and its approval reference, recorded at **first use** from the
  configuration. Later a lower configured figure applies (tightening is always allowed), a higher
  one is ignored and reported: a configuration edit alone never raises the approved ceiling; only
  ``approve_ceiling`` with a new approval reference does (still capped at US$10).
* ``reservation``: one row per model call, reserved at its **worst case** (input = characters / 3,
  output = the role's ``max_tokens``) inside ``BEGIN IMMEDIATE`` before the request is sent; a
  reservation that would exceed the ceiling raises ``BudgetExhausted`` and nothing is sent.
* ``settlement``: the committed amount (provider-reported usage priced with the price table; the
  reserved worst case when usage is unreported, conservative) or a release (call never sent).

A reservation that is never settled (a crashed process) stays counted: the ledger only ever errs on
the side of spending less. Amounts are integer micro-dollars. Nothing is retried here.
"""

from __future__ import annotations

import math
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from ..core.config_crosssource import REVIEW_USD_CEILING_MAX
from ..core.errors import BudgetExhausted, ConfigError
from ..providers.base import CallEstimate

SCHEMA = """
CREATE TABLE IF NOT EXISTS ceiling (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    usd_micro INTEGER NOT NULL CHECK (usd_micro > 0),
    approval_ref TEXT NOT NULL CHECK (length(approval_ref) > 0),
    source TEXT NOT NULL CHECK (source IN ('first_use', 'approval')),
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reservation (
    reservation_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    subject TEXT NOT NULL,
    model TEXT,
    pricing_key TEXT NOT NULL,
    reserved_micro INTEGER NOT NULL CHECK (reserved_micro >= 0),
    opened_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlement (
    reservation_id TEXT PRIMARY KEY REFERENCES reservation (reservation_id),
    outcome TEXT NOT NULL CHECK (outcome IN ('committed', 'released')),
    usd_micro INTEGER NOT NULL CHECK (usd_micro >= 0),
    usage_reported INTEGER NOT NULL,
    settled_at TEXT NOT NULL
);
"""
APPEND_ONLY = ("ceiling", "reservation", "settlement")


def append_only_triggers(tables: tuple[str, ...]) -> str:
    return "\n".join(
        f"CREATE TRIGGER IF NOT EXISTS {t}_no_{op.lower()} BEFORE {op} ON {t} BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;"
        for t in tables
        for op in ("UPDATE", "DELETE")
    )


def read_only_uri(path: str | Path) -> str:
    """A SQLite URI that opens ``path`` read-only (the file is never modified)."""
    return "file:" + quote(str(Path(path).resolve())) + "?mode=ro"


def micro(usd: float, *, up: bool = True) -> int:
    value = usd * 1_000_000
    return math.ceil(value - 1e-6) if up else round(value)


def usd_of(value: int) -> float:
    return round(value / 1_000_000, 6)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    usd_micro: int


class ReviewLedger:
    """The shared cumulative review ledger. ``usd_ceiling`` / ``approval_ref`` come from
    ``crosssource.links.review``; the first use records them."""

    def __init__(self, path: str | Path, *, usd_ceiling: float, approval_ref: str, now: Callable[[], datetime] = utc_now):
        if not 0 < usd_ceiling <= REVIEW_USD_CEILING_MAX:
            raise ConfigError(f"review usd_ceiling must be in (0, {REVIEW_USD_CEILING_MAX}]", "crosssource.links.review.usd_ceiling")
        if not approval_ref or not approval_ref.strip():
            raise ConfigError("the review ceiling needs an approval reference", "crosssource.links.review.approval_ref")
        self.path = Path(path)
        self.now = now
        self.configured_micro = micro(usd_ceiling, up=False)
        self.configured_ref = approval_ref
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript(SCHEMA + append_only_triggers(APPEND_ONLY))
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT count(*) FROM ceiling").fetchone()[0] == 0:
                db.execute("INSERT INTO ceiling (usd_micro, approval_ref, source, recorded_at) VALUES (?, ?, 'first_use', ?)",
                           (self.configured_micro, approval_ref, self._stamp()))
            db.execute("COMMIT")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        db.execute("PRAGMA journal_mode=DELETE")
        return db

    def _stamp(self) -> str:
        return self.now().astimezone(timezone.utc).isoformat()

    @staticmethod
    def _approved(db: sqlite3.Connection) -> tuple[int, str]:
        row = db.execute("SELECT usd_micro, approval_ref FROM ceiling ORDER BY row_id DESC LIMIT 1").fetchone()
        return int(row[0]), str(row[1])

    def _effective(self, db: sqlite3.Connection) -> int:
        return min(self._approved(db)[0], self.configured_micro)

    @staticmethod
    def _spent(db: sqlite3.Connection) -> tuple[int, int]:
        committed = db.execute("SELECT coalesce(sum(usd_micro), 0) FROM settlement WHERE outcome = 'committed'").fetchone()[0]
        open_ = db.execute(
            "SELECT coalesce(sum(r.reserved_micro), 0) FROM reservation r LEFT JOIN settlement s USING (reservation_id) WHERE s.reservation_id IS NULL"
        ).fetchone()[0]
        return int(committed), int(open_)

    def approve_ceiling(self, usd: float, approval_ref: str) -> None:
        """Record a new owner-approved ceiling (the only way to raise it); capped at US$10 and the
        approval reference must differ from the current one."""
        if not 0 < usd <= REVIEW_USD_CEILING_MAX:
            raise ConfigError(f"an approved review ceiling must be in (0, {REVIEW_USD_CEILING_MAX}]", "crosssource.links.review.usd_ceiling")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            if not approval_ref.strip() or approval_ref == self._approved(db)[1]:
                db.execute("ROLLBACK")
                raise ConfigError("a new review ceiling needs a new approval reference", "crosssource.links.review.approval_ref")
            db.execute("INSERT INTO ceiling (usd_micro, approval_ref, source, recorded_at) VALUES (?, ?, 'approval', ?)",
                       (micro(usd, up=False), approval_ref, self._stamp()))
            db.execute("COMMIT")

    def reserve(self, usd: float, *, scope: str, subject: str, model: str | None, pricing_key: str) -> Reservation:
        amount = micro(usd)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            committed, open_ = self._spent(db)
            ceiling = self._effective(db)
            if committed + open_ + amount > ceiling:
                db.execute("ROLLBACK")
                raise BudgetExhausted("review_usd_ceiling", usd_of(amount), usd_of(max(0, ceiling - committed - open_)))
            reservation = Reservation(uuid.uuid4().hex, amount)
            db.execute("INSERT INTO reservation VALUES (?, ?, ?, ?, ?, ?, ?)", (reservation.reservation_id, scope, subject, model, pricing_key, amount, self._stamp()))
            db.execute("COMMIT")
        return reservation

    def _settle(self, reservation: Reservation, outcome: str, amount: int, reported: bool) -> None:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO settlement VALUES (?, ?, ?, ?, ?)", (reservation.reservation_id, outcome, amount, int(reported), self._stamp()))
            db.execute("COMMIT")

    def commit(self, reservation: Reservation, usd: float | None) -> float:
        """Commit the measured cost (``None``: usage unreported -> the reserved worst case)."""
        amount = reservation.usd_micro if usd is None else micro(usd, up=False)
        self._settle(reservation, "committed", amount, usd is not None)
        return usd_of(amount)

    def release(self, reservation: Reservation) -> None:
        """Only for a call that was never sent."""
        self._settle(reservation, "released", 0, False)

    def usage(self) -> dict[str, Any]:
        with closing(self._connect()) as db:
            return ledger_usage(db, self.configured_micro)


def ledger_usage(db: sqlite3.Connection, configured_micro: int | None = None) -> dict[str, Any]:
    approved, ref = ReviewLedger._approved(db)
    ceiling = approved if configured_micro is None else min(approved, configured_micro)
    committed, open_ = ReviewLedger._spent(db)
    calls = db.execute("SELECT count(*) FROM reservation").fetchone()[0]
    by_scope = {
        scope: usd_of(total)
        for scope, total in db.execute(
            "SELECT r.scope, sum(s.usd_micro) FROM reservation r JOIN settlement s USING (reservation_id) WHERE s.outcome = 'committed' GROUP BY r.scope ORDER BY r.scope"
        )
    }
    notes = []
    if configured_micro is not None and configured_micro > approved:
        notes.append(f"configured ceiling {usd_of(configured_micro)} exceeds the approved {usd_of(approved)} ({ref}); the approved figure applies")
    return {
        "approved_usd": usd_of(approved),
        "approval_ref": ref,
        "ceiling_usd": usd_of(ceiling),
        "committed_usd": usd_of(committed),
        "open_reserved_usd": usd_of(open_),
        "remaining_usd": usd_of(max(0, ceiling - committed - open_)),
        "calls": int(calls),
        "committed_by_scope": by_scope,
        "notes": notes,
    }


def read_usage(path: str | Path, configured_usd: float | None = None) -> dict[str, Any] | None:
    """Usage of an existing ledger opened read-only (``None`` when the file does not exist)."""
    path = Path(path)
    if not path.is_file():
        return None
    with closing(sqlite3.connect(read_only_uri(path), uri=True)) as db:
        return ledger_usage(db, None if configured_usd is None else micro(configured_usd, up=False))


def worst_case_usd(price_table: Any, pricing_key: str, prompt_chars: int, max_tokens: int) -> float:
    """§6.13: input tokens = characters / 3 (conservative), output = the role's ``max_tokens``."""
    return float(price_table.cost(pricing_key, max(1, math.ceil(prompt_chars / 3)), max_tokens))


class RunCap:
    """A per-run USD cap on top of the cumulative ceiling (``identity review --max-usd``)."""

    def __init__(self, max_usd: float):
        if max_usd <= 0:
            raise ValueError("--max-usd must be positive")
        self.max_micro = micro(max_usd, up=False)
        self.spent_micro = 0

    def check(self, amount: int) -> None:
        if self.spent_micro + amount > self.max_micro:
            raise BudgetExhausted("review_run_usd", usd_of(amount), usd_of(max(0, self.max_micro - self.spent_micro)))


class ReviewMeter:
    """``providers.CallMeter`` for one review call: reserves the worst case on the ledger (and the
    run cap) before the request is sent and commits the priced usage afterwards, also for a
    failed call (the reserved worst case when usage is unreported)."""

    def __init__(self, ledger: ReviewLedger, price_table: Any, pricing_key: str, *, scope: str, subject: str, model: str | None,
                 prompt_chars: int, max_tokens: int, run_cap: RunCap | None = None):
        if price_table is None or pricing_key not in getattr(price_table, "prices", {}):
            raise ConfigError(f"review needs a price for {pricing_key!r} in budgets.price_table (no bundled prices)", "budgets.price_table")
        self.ledger, self.price_table, self.pricing_key = ledger, price_table, pricing_key
        self.scope, self.subject, self.model = scope, subject, model
        self.floor_input = max(1, math.ceil(prompt_chars / 3))
        self.max_tokens = max_tokens
        self.run_cap = run_cap
        self._reservation: Reservation | None = None
        self.committed_usd = 0.0
        self.calls = 0

    def reserve(self, estimate: CallEstimate | None = None) -> None:
        estimate = estimate or CallEstimate(0, 0)
        usd = float(self.price_table.cost(self.pricing_key, max(estimate.input_tokens, self.floor_input), max(estimate.output_tokens, self.max_tokens)))
        if self.run_cap is not None:
            self.run_cap.check(micro(usd))
        self._reservation = self.ledger.reserve(usd, scope=self.scope, subject=self.subject, model=self.model, pricing_key=self.pricing_key)
        if self.run_cap is not None:
            self.run_cap.spent_micro += self._reservation.usd_micro

    def record(self, item: dict[str, Any]) -> None:
        if self._reservation is None:
            return
        usage = item.get("usage") or {}
        cost = None
        if usage.get("reported"):
            cost = float(self.price_table.cost(self.pricing_key, int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0),
                                               int(usage.get("cached_input_tokens") or 0), int(usage.get("cache_creation_input_tokens") or 0)))
        reservation, self._reservation = self._reservation, None
        committed = self.ledger.commit(reservation, cost)
        if self.run_cap is not None:  # the run cap holds the committed amount, not the worst case
            self.run_cap.spent_micro += micro(committed, up=False) - reservation.usd_micro
        self.committed_usd += committed
        self.calls += 1
