"""Shared compare-and-set rule for every ``SessionStore`` adapter (QA-05).

``touch(session_id, now, record, expected_version)`` writes ``record`` (or the stored record) with
``last_seen_at = now`` and ``version = stored.version + 1``. With ``expected_version`` it is a
compare-and-set: a stored version that differs raises ``SessionConflict`` and nothing is written.
Adapters make the read-compare-write atomic: SQL stores update ``WHERE id = ? AND data = <the raw
row just read>`` (a concurrent writer changes ``data``, so the update matches no row), MongoDB filters
on the raw ``data`` the same way, and the in-memory store holds a lock.
"""

from __future__ import annotations

from ..core.errors import SessionConflict
from ..core.types import SessionRecord


def next_record(
    session_id: str, current: SessionRecord, now: float, record: SessionRecord | None, expected_version: int | None
) -> SessionRecord:
    """The record to store, or ``SessionConflict`` when ``expected_version`` is stale."""
    if expected_version is not None and current.version != expected_version:
        raise SessionConflict(f"session {session_id}: stored version {current.version}, expected {expected_version}")
    return (record or current).model_copy(update={"last_seen_at": now, "version": current.version + 1})


def lost(session_id: str, expected_version: int | None) -> SessionConflict:
    """The atomic update matched no row: another writer changed the record after it was read."""
    return SessionConflict(f"session {session_id}: changed concurrently (expected version {expected_version})")
