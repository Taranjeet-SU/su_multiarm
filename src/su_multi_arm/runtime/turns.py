"""Session turns through the engine (plan §3.4): open, budget, re-validate, record.

``SessionTurns`` wraps a ``SessionManager`` over the configured session store. Per turn it:

1. opens the session for ``(principal, dataset, session_id)`` (unknown/expired -> ``SessionNotFound``,
   another principal -> ``AccessDenied``; never a silent new session);
2. clips the request budget to what the session budget has left and holds the clipped amount for
   the turn in one compare-and-set (an exhausted session refuses the turn with ``BudgetExhausted``
   before anything runs; concurrent turns see each other's holds);
3. on a generation switch, re-validates the episodic IDs against the newly pinned generation; IDs
   that no longer exist are dropped and listed (``generation_changed`` is set on the trace root by
   ``RunContext``);
4. optionally narrows the request with the IDs already shown (``sessions.memory.exclude_shown``),
   logged;
5. records the turn (query hash, shown IDs, answer id, trace id, generation) and the usage, merged
   into the freshest stored record with a compare-and-set, so concurrent turns are never lost.

Memory is never evidence: nothing here reaches the generator; the engine's ``AnswerContract`` only
accepts citations of evidence supplied in the current turn.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

from ..core.errors import BudgetExhausted
from ..core.types import SessionRecord, SessionReservation
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

# A session-budget hold outlives its request deadline by this much before it stops counting.
RESERVATION_GRACE_SECONDS = 300.0


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

    def remaining(self, session: Session | SessionRecord, now: float | None = None) -> dict[str, float]:
        """What the session budget has left per kind (only kinds with a session limit): the limit
        minus recorded usage minus the live reservations of in-flight turns."""
        if self.session_budget is None:
            return {}
        record = session.record if isinstance(session, Session) else session
        now = self.manager.clock() if now is None else now
        live = [r.amounts for r in record.reservations.values() if r.expires_at > now]
        left: dict[str, float] = {}
        for kind, attribute in USAGE_KINDS.items():
            limit = getattr(self.session_budget, attribute)
            if limit is not None:
                left[kind] = limit - record.usage.get(kind, 0.0) - sum(a.get(kind, 0.0) for a in live)
        return left

    def clip(self, session: Session | SessionRecord, limits: BudgetLimits, now: float | None = None) -> BudgetLimits:
        """Request limits clipped to the session's remainder; an exhausted kind refuses the turn."""
        update: dict[str, Any] = {}
        for kind, left in self.remaining(session, now).items():
            if left <= 0:
                raise BudgetExhausted(f"session_{kind}", 0, left)
            attribute = USAGE_KINDS[kind]
            current = getattr(limits, attribute)
            value = left if current is None else min(current, left)
            if kind != "usd":
                value = int(value)
                if value < 1:  # less than one whole call/token left
                    raise BudgetExhausted(f"session_{kind}", 1, left)
            update[attribute] = value if kind != "usd" else float(value)
        return limits.model_copy(update=update) if update else limits

    def reserve(self, session: Session, limits: BudgetLimits) -> BudgetLimits:
        """Clip ``limits`` to the session remainder and hold them for this turn, atomically.

        The clip and the reservation are one compare-and-set on the stored record (QA-05), so
        concurrent turns each see the others' holds and together never exceed ``budgets.session``.
        ``finish`` (or ``release``) replaces the hold with the actual usage. A hold expires after the
        request deadline plus ``RESERVATION_GRACE_SECONDS`` so a turn that never finished (a stream
        that was never consumed, a killed worker) cannot block the session forever."""
        if self.session_budget is None:
            return limits
        key = uuid.uuid4().hex
        clipped: list[BudgetLimits] = []

        def mutate(record: SessionRecord) -> SessionRecord:
            now = self.manager.clock()
            clipped[:] = [self.clip(record, limits, now)]
            amounts = {kind: float(getattr(clipped[0], attribute)) for kind, attribute in USAGE_KINDS.items() if getattr(clipped[0], attribute) is not None}
            expires = now + (limits.deadline_seconds or 0.0) + RESERVATION_GRACE_SECONDS
            live = {k: r for k, r in record.reservations.items() if r.expires_at > now}
            live[key] = SessionReservation(amounts=amounts, expires_at=expires)
            return record.model_copy(update={"reservations": live})

        session.record = self.manager.update(session.id, mutate)
        session.reservation = key
        return clipped[0]

    def release(self, session: Session) -> None:
        """Drop this turn's hold without recording usage (the turn failed before it ran)."""
        key = session.reservation
        if key is None:
            return
        session.reservation = None

        def mutate(record: SessionRecord) -> SessionRecord:
            return record.model_copy(update={"reservations": {k: r for k, r in record.reservations.items() if k != key}})

        session.record = self.manager.update(session.id, mutate)

    def prepare(self, ctx: RunContext, session: Session, exists: Callable[[list[str]], set[str]]) -> dict[str, Any]:
        """Re-validate memory after a generation switch; returns the session trace block."""
        change = session.generation_change(ctx.generation)
        dropped: list[str] = []
        if change is not None:
            dropped = session.memory.episodic.revalidate(ctx.generation.generation, exists)["dropped"]
            session.dropped = list(dropped)
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
        """Record the turn (when ``query`` is given) and the usage; persists the session.

        One compare-and-set on the freshest stored record: this turn's usage is added, its hold is
        released, IDs dropped by this turn's re-validation are removed and the turn is appended, so
        concurrent turns of the same session are merged, never overwritten (QA-05)."""
        used = ctx.budget.snapshot()["used"]
        key, session.reservation = session.reservation, None
        dropped = set(session.dropped)
        turn = None
        if query is not None and self.episodic:
            turn = session.memory.episodic.record(query, shown_ids, ctx.generation.generation, ctx.trace.id, answer_id=answer_id)

        def mutate(record: SessionRecord) -> SessionRecord:
            usage = dict(record.usage)
            for kind in USAGE_KINDS:
                usage[kind] = usage.get(kind, 0.0) + float(used.get(kind, 0.0))
            turns = list(record.turns)
            if dropped:
                turns = [t.model_copy(update={"shown_ids": [i for i in t.shown_ids if i not in dropped]}) for t in turns]
            if turn is not None:
                turns.append(turn)
            reservations = {k: r for k, r in record.reservations.items() if k != key}
            return record.model_copy(update={"usage": usage, "turns": turns, "reservations": reservations, "generation": ctx.generation.generation})

        session.record = self.manager.update(session.id, mutate)
        session.memory.episodic.turns = list(session.record.turns)
        return dict(session.record.usage)
