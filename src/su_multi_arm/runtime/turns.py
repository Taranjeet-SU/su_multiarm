"""Session turns through the engine (plan §3.4): open, budget, re-validate, record.

``SessionTurns`` wraps a ``SessionManager`` over the configured session store. Per turn it:

1. opens the session for ``(principal, dataset, session_id)`` (unknown/expired -> ``SessionNotFound``,
   another principal -> ``AccessDenied``; never a silent new session);
2. clips the request budget to what the session budget has left (an exhausted session refuses the
   turn with ``BudgetExhausted`` before anything runs);
3. on a generation switch, re-validates the episodic IDs against the newly pinned generation; IDs
   that no longer exist are dropped and listed (``generation_changed`` is set on the trace root by
   ``RunContext``);
4. optionally narrows the request with the IDs already shown (``sessions.memory.exclude_shown``),
   logged;
5. records the turn (query hash, shown IDs, answer id, trace id, generation) and the usage.

Memory is never evidence: nothing here reaches the generator; the engine's ``AnswerContract`` only
accepts citations of evidence supplied in the current turn.
"""

from __future__ import annotations

from typing import Any, Callable

from ..core.errors import BudgetExhausted
from .acl import Principal
from .budget import BudgetLimits
from .context import RunContext
from .session import Session, SessionManager

USAGE_KINDS: dict[str, str] = {
    "calls": "max_llm_calls",
    "input_tokens": "max_input_tokens",
    "output_tokens": "max_output_tokens",
    "usd": "max_usd",
}


class SessionTurns:
    def __init__(self, manager: SessionManager, dataset: str, session_budget: Any = None, exclude_shown: bool = False, episodic: bool = True):
        self.manager = manager
        self.dataset = dataset
        self.session_budget = session_budget
        self.exclude_shown = exclude_shown
        self.episodic = episodic

    def create(self, principal: Principal, session_id: str | None = None) -> Session:
        return self.manager.create(principal, self.dataset, session_id)

    def open(self, principal: Principal, session_id: str) -> Session:
        return self.manager.get(principal, self.dataset, session_id)

    def close(self, principal: Principal, session_id: str) -> None:
        self.manager.close(self.open(principal, session_id))

    def remaining(self, session: Session) -> dict[str, float]:
        """What the session budget has left per kind (only kinds with a session limit)."""
        if self.session_budget is None:
            return {}
        left: dict[str, float] = {}
        for kind, attribute in USAGE_KINDS.items():
            limit = getattr(self.session_budget, attribute)
            if limit is not None:
                left[kind] = limit - session.record.usage.get(kind, 0.0)
        return left

    def clip(self, session: Session, limits: BudgetLimits) -> BudgetLimits:
        """Request limits clipped to the session's remainder; an exhausted kind refuses the turn."""
        update: dict[str, Any] = {}
        for kind, left in self.remaining(session).items():
            if left <= 0:
                raise BudgetExhausted(f"session_{kind}", 0, left)
            attribute = USAGE_KINDS[kind]
            current = getattr(limits, attribute)
            value = left if current is None else min(current, left)
            update[attribute] = int(value) if kind != "usd" else float(value)
        return limits.model_copy(update=update) if update else limits

    def prepare(self, ctx: RunContext, session: Session, exists: Callable[[list[str]], set[str]]) -> dict[str, Any]:
        """Re-validate memory after a generation switch; returns the session trace block."""
        change = session.generation_change(ctx.generation)
        dropped: list[str] = []
        if change is not None:
            dropped = session.memory.episodic.revalidate(ctx.generation.generation, exists)["dropped"]
            ctx.trace.event("memory_revalidated", dropped=dropped, generation=ctx.generation.generation)
        return {
            "id": session.id,
            "turn": len(session.memory.episodic.turns) + 1,
            "generation": ctx.generation.generation,
            "generation_changed": change,
            "dropped_ids": dropped,
            "exclusions_added": [],
        }

    def exclusions(self, ctx: RunContext, session: Session, already: list[str]) -> list[str]:
        """IDs to add to ``exclude_ids`` when ``exclude_shown`` is on (logged); never widens."""
        if not self.exclude_shown:
            return []
        added = [i for i in session.memory.exclusions() if i not in already]
        if added:
            ctx.trace.event("memory_exclusions", ids=added)
        return added

    def finish(
        self,
        ctx: RunContext,
        session: Session,
        query: str | None,
        shown_ids: list[str],
        answer_id: str | None = None,
    ) -> dict[str, float]:
        """Record the turn (when ``query`` is given) and the usage; persists the session."""
        used = ctx.budget.snapshot()["used"]
        usage = dict(session.record.usage)
        for kind in USAGE_KINDS:
            usage[kind] = usage.get(kind, 0.0) + float(used.get(kind, 0.0))
        session.record = session.record.model_copy(update={"usage": usage})
        turn = None
        if query is not None and self.episodic:
            turn = session.memory.episodic.record(query, shown_ids, ctx.generation.generation, ctx.trace.id, answer_id=answer_id)
        self.manager.touch(session, ctx.generation, turn)
        return usage
