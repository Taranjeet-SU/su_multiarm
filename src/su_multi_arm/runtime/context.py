"""``RunContext``: the immutable per-request harness object every component receives (plan §3.4).

Carries the principal, session, memory, budget, trace, the generation pinned at request start,
the deadline and the access policy. One context per request; multi-hop and cached answers reuse
it, so every hop reads the same generation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.errors import DeadlineExceeded
from ..core.types import GenerationId, ScopePredicates
from .acl import ANONYMOUS, AccessPolicy, NoConstraints, Principal
from .budget import Budget, BudgetLimits, PriceTable
from .memory import Memory
from .session import Session
from .trace import Trace


@dataclass(frozen=True)
class RunContext:
    generation: GenerationId
    principal: Principal = field(default_factory=lambda: ANONYMOUS)
    session: Session | None = None
    memory: Memory = field(default_factory=Memory)
    budget: Budget = field(default_factory=lambda: Budget(BudgetLimits()))
    trace: Trace = field(default_factory=Trace)
    acl: AccessPolicy = field(default_factory=NoConstraints)
    profile: str = "neutral"
    config_fingerprint: str | None = None
    clock: Callable[[], float] = time.monotonic
    # ``serving.answers`` (A2 rev 4): a persistent spend scope every answer-time call reserves on
    # (``runtime/answers_spend.AnswerSpend``); ``None`` = per-request budgets only.
    spend: Any = None

    def __post_init__(self):
        self.trace.root.set(
            generation=self.generation.generation,
            dataset=self.generation.dataset,
            index_format=self.generation.index_format,
            principal=self.principal.key(),
            session_id=None if self.session is None else self.session.id,
            profile=self.profile,
            config_fingerprint=self.config_fingerprint,
        )
        if self.session is not None:
            change = self.session.generation_change(self.generation)
            if change:
                self.trace.root.set(generation_changed=change)

    @property
    def deadline(self) -> float | None:
        return self.budget.deadline

    def remaining_seconds(self) -> float | None:
        deadline = self.deadline
        return None if deadline is None else max(0.0, deadline - self.budget.clock())

    def check_deadline(self) -> None:
        try:
            self.budget.check_deadline()
        except DeadlineExceeded:
            self.trace.event("deadline_exceeded", deadline_seconds=self.budget.limits.deadline_seconds)
            raise

    def constraints(self) -> ScopePredicates:
        """Access constraints for this principal; applied inside every arm, hop and value listing."""
        return self.acl.constraints(self.principal)


def new_context(
    generation: GenerationId,
    *,
    principal: Principal = ANONYMOUS,
    session: Session | None = None,
    limits: BudgetLimits | None = None,
    price_table: PriceTable | None = None,
    acl: AccessPolicy | None = None,
    profile: str = "neutral",
    config_fingerprint: str | None = None,
    trace: Trace | None = None,
    spend: Any = None,
) -> RunContext:
    """Build a context, reusing the session's memory so episodic exclusions carry across turns."""
    return RunContext(
        generation=generation,
        principal=principal,
        session=session,
        memory=session.memory if session is not None else Memory(),
        budget=Budget(limits or BudgetLimits(), price_table),
        trace=trace or Trace(),
        acl=acl or NoConstraints(),
        profile=profile,
        config_fingerprint=config_fingerprint,
        spend=spend,
    )
