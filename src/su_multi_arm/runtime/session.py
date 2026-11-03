"""Sessions keyed ``(principal, dataset, session_id)`` over a ``SessionStore`` (plan §3.4).

A session from another principal is a 403-class error (``AccessDenied``). Sessions store IDs and
hashes, not raw passages, unless ``memory.episodic.store_text`` is on. On a generation switch the
session continues: episodic IDs are re-validated at the next turn and ``generation_changed`` is
recorded in the trace.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Callable

from ..backends.contracts import SessionStore
from ..core.errors import AccessDenied, SessionNotFound
from ..core.types import GenerationId, SessionRecord, Turn
from .acl import Principal
from .memory import EpisodicMemory, Memory


@dataclass
class Session:
    record: SessionRecord
    memory: Memory

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

    def touch(self, session: Session, generation: GenerationId, turn: Turn | None = None) -> Session:
        """Persist the turn list and the generation of the latest turn."""
        turns = list(session.memory.episodic.turns)
        if turn is not None and turn not in turns:
            turns.append(turn)
        now = self.clock()
        record = session.record.model_copy(update={"turns": turns, "generation": generation.generation, "last_seen_at": now})
        self.store.touch(record.id, now, record)
        session.record = record
        return session

    def close(self, session: Session) -> None:
        record = session.record.model_copy(update={"closed": True})
        self.store.touch(record.id, self.clock(), record)
        session.record = record

    def expire(self) -> int:
        return self.store.expire(self.clock(), self.ttl_seconds)
