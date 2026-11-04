"""The ``serving.answers`` spend scope (addendum A2 rev 4): answer-time calls reserve on a persistent
per-day ledger (scope ``serving.answers``), separate from the learning scopes, and on the provider's
lifetime cap (``budgets.providers.<provider>.max_usd_total``) when one is configured. It is built
from config only (no code is named). ``RunBudgetMeter`` reserves before each call and commits the
measured spend after it; a refusal raises ``BudgetExhausted`` before the request is sent."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCOPE = "serving.answers"
ANSWER_ROLES = ("planner", "generator", "reviewer", "community")


@dataclass
class AnswerReservation:
    day: Any
    totals: list[tuple[Any, Any]] = field(default_factory=list)


class AnswerSpend:
    def __init__(self, day_ledger: Any, totals: dict[str, Any], providers: dict[str, str]):
        self.day_ledger = day_ledger  # DaySpendLedger(scope=serving.answers)
        self.totals = totals  # provider -> TotalSpendLedger
        self.providers = providers  # pricing key -> provider of a paid answer-time role

    def reserve(self, usd: float, pricing_key: str | None) -> AnswerReservation:
        day = self.day_ledger.reserve(usd)
        held: list[tuple[Any, Any]] = []
        provider = self.providers.get(pricing_key or "")
        ledger = self.totals.get(provider) if provider else None
        if ledger is not None:
            try:
                held.append((ledger, ledger.reserve(usd)))
            except Exception:
                self.day_ledger.release(day)
                raise
        return AnswerReservation(day, held)

    def commit(self, reservation: AnswerReservation, usd: float) -> None:
        self.day_ledger.commit(reservation.day, usd)
        for ledger, held in reservation.totals:
            ledger.commit(held, usd)

    def usage(self) -> dict[str, Any]:
        return {"day": self.day_ledger.usage(), "totals": {p: t.usage() for p, t in sorted(self.totals.items())}}


def answers_spend_from_config(config: Any) -> AnswerSpend | None:
    from ..core.config_learning import loopback_role
    from ..learning.patterns.spend import DaySpendLedger, TotalSpendLedger

    settings = config.serving.answers
    if not settings.enabled:
        return None
    path = Path(settings.ledger) if settings.ledger is not None else _default_path(config)
    day = DaySpendLedger(path, max_calls=settings.max_calls_per_day, max_usd=settings.max_usd_per_day, tz=settings.timezone, scope=SCOPE, role="answers")
    providers: dict[str, str] = {}
    totals: dict[str, Any] = {}
    for name in ANSWER_ROLES:
        role = getattr(config.models.roles, name, None)
        if role is None or loopback_role(role):
            continue
        providers[role.price_key()] = role.provider
        cap = config.budgets.providers.get(role.provider)
        if cap is not None and role.provider not in totals:
            totals[role.provider] = TotalSpendLedger(path, key=f"provider:{role.provider}", max_usd=cap.max_usd_total, tz=settings.timezone)
    return AnswerSpend(day, totals, providers)


def _default_path(config: Any) -> Path:
    for backend in config.backends.values():
        if getattr(backend, "kind", None) == "sqlite" and getattr(backend, "path", None):
            return Path(backend.path) / ".answers_spend.sqlite"
    return Path(".answers_spend.sqlite")
