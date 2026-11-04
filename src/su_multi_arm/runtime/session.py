"""Sessions keyed ``(principal, dataset, session_id)`` over a ``SessionStore`` (plan §3.4).

A session from another principal is a 403-class error (``AccessDenied``). Sessions store IDs and
hashes, not raw passages, unless ``memory.episodic.store_text`` is on. On a generation switch the
session continues: episodic IDs are re-validated at the next turn and ``generation_changed`` is
recorded in the trace.
"""

from __future__ import annotations

import random
import threading
import time
import uuid
import zlib
from dataclasses import dataclass, field
from typing import Callable

from ..backends.contracts import SessionStore
from ..core.errors import AccessDenied, SessionConflict, SessionNotFound
from ..core.types import GenerationId, SessionRecord, Turn
from .acl import Principal
from .memory import EpisodicMemory, Memory

# Bounded compare-and-set attempts per update (the in-process stripe lock removes same-process
# contention; these cover writers in other processes) and the number of in-process lock stripes.
CAS_ATTEMPTS = 50
LOCK_STRIPES = 64


@dataclass
class Session:
    record: SessionRecord
    memory: Memory
    # Per-turn state: the session-budget hold taken by ``SessionTurns.reserve`` and the IDs this
    # turn's generation re-validation dropped (applied to the stored turns at ``finish``).
    reservation: str | None = None
    dropped: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.record.id

    def generation_change(self, generation: GenerationId) -> dict | None:
        """``{"from", "to"}`` when the pinned generation differs from the one recorded on the session."""
        previous = self.record.generation
        if previous is None or previous == generation.generation:
            return None
        return {"from": previous, "to": generation.generation}


class SessionManager:
    def __init__(self, store: SessionStore, ttl_seconds: float, store_text: bool = False, clock: Callable[[], float] = time.time):
        self.store, self.ttl_seconds, self.store_text, self.clock = store, ttl_seconds, store_text, clock
        self._locks = [threading.Lock() for _ in range(LOCK_STRIPES)]

    def create(self, principal: Principal, dataset: str, session_id: str | None = None) -> Session:
        now = self.clock()
        record = SessionRecord(
            id=session_id or uuid.uuid4().hex,
            principal_id=principal.key(),
            dataset=dataset,
            created_at=now,
            last_seen_at=now,
        )
        self.store.create(record)
        return Session(record=record, memory=Memory(episodic=EpisodicMemory(store_text=self.store_text)))

    def get(self, principal: Principal, dataset: str, session_id: str) -> Session:
        record = self.store.get(session_id)
        if record is None or record.closed or record.last_seen_at + self.ttl_seconds <= self.clock():
            raise SessionNotFound(f"unknown or expired session {session_id}")
        if record.principal_id != principal.key() or record.dataset != dataset:
            # Do not reveal whether the session exists for another principal.
            raise AccessDenied("session does not belong to this principal and dataset")
        episodic = EpisodicMemory(turns=list(record.turns), store_text=self.store_text)
        return Session(record=record, memory=Memory(episodic=episodic))

    def update(self, session_id: str, mutate: Callable[[SessionRecord], SessionRecord]) -> SessionRecord:
        """Apply ``mutate`` to the freshest stored record with a compare-and-set (QA-05).

        Turns of one session in this process are serialised on a lock stripe; writers in other
        processes are caught by the store's CAS (``SessionConflict``), after which ``mutate`` is
        re-applied to the newly stored record. ``mutate`` must be a pure function of the record (it
        may raise, e.g. ``BudgetExhausted``, and then nothing is written). Contention that outlasts
        ``CAS_ATTEMPTS`` raises ``SessionConflict``; nothing is ever written from a stale read."""
        with self._stripe(session_id):
            for attempt in range(CAS_ATTEMPTS):
                current = self.store.get(session_id)
                if current is None:
                    raise SessionNotFound(f"unknown or expired session {session_id}")
                changed = mutate(current)
                now = self.clock()
                try:
                    self.store.touch(session_id, now, changed, expected_version=current.version)
                except SessionConflict:
                    time.sleep(min(0.05, 0.002 * (attempt + 1)) * (0.5 + random.random()))
                    continue
                return changed.model_copy(update={"last_seen_at": now, "version": current.version + 1})
        raise SessionConflict(f"session {session_id}: {CAS_ATTEMPTS} concurrent writers won the compare-and-set first")

    def touch(self, session: Session, generation: GenerationId, turn: Turn | None = None) -> Session:
        """Append ``turn`` and set the generation of the latest turn (CAS on the stored record:
        concurrent turns are merged, never overwritten)."""

        def mutate(record: SessionRecord) -> SessionRecord:
            turns = list(record.turns)
            if turn is not None and turn not in turns:
                turns.append(turn)
            return record.model_copy(update={"turns": turns, "generation": generation.generation})

        session.record = self.update(session.id, mutate)
        session.memory.episodic.turns = list(session.record.turns)
        return session

    def close(self, session: Session) -> None:
        session.record = self.update(session.id, lambda record: record.model_copy(update={"closed": True}))

    def _stripe(self, session_id: str) -> threading.Lock:
        return self._locks[zlib.crc32(session_id.encode()) % len(self._locks)]

    def expire(self) -> int:
        return self.store.expire(self.clock(), self.ttl_seconds)
