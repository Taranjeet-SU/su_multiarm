"""SQLite ``CacheStore``, ``LedgerStore`` and ``SessionStore`` facets over ``runtime.sqlite``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ...core.errors import BackendError
from ...core.types import CacheEntry, LedgerEntry, RawReply, SessionRecord
from .._sessions import lost, next_record
from ..memory import CACHE_KEY
from ._db import KIND

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import SqliteBackend


class SqliteCache:
    def __init__(self, backend: SqliteBackend):
        self._b = backend

    def get(self, key: str) -> CacheEntry | None:
        with self._b.runtime_db() as db:
            row = db.execute("SELECT value, expires_at FROM cache WHERE key=?", (key,)).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= self._b.clock():
                db.execute("DELETE FROM cache WHERE key=?", (key,))
                return None
        return CacheEntry(key=key, value=json.loads(row["value"]), expires_at=row["expires_at"])

    def put(self, key: str, value: Any, ttl: float) -> None:
        if not CACHE_KEY.match(key):
            raise BackendError(KIND, "put", "cache keys must start with a GenerationId prefix")
        if ttl <= 0:
            raise BackendError(KIND, "put", "ttl must be positive")
        with self._b.runtime_db() as db:
            db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, json.dumps(value, sort_keys=True, default=str), self._b.clock() + ttl))

    def invalidate(self, prefix: str) -> int:
        with self._b.runtime_db() as db:
            cursor = db.execute("DELETE FROM cache WHERE substr(key, 1, ?)=?", (len(prefix), prefix))
            return int(cursor.rowcount)


class SqliteLedger:
    def __init__(self, backend: SqliteBackend):
        self._b = backend

    def append(self, job_id: str, entry: LedgerEntry) -> None:
        with self._b.runtime_db() as db:
            last = db.execute("SELECT max(sequence) FROM ledger WHERE job_id=?", (job_id,)).fetchone()[0]
            if last is not None and entry.sequence <= last:
                raise BackendError(KIND, "append", "ledger sequence must increase")
            db.execute(
                "INSERT INTO ledger VALUES (?,?,?,?,?)",
                (job_id, entry.sequence, entry.kind, entry.at, json.dumps(entry.payload, sort_keys=True, default=str)),
            )
            db.execute("INSERT OR IGNORE INTO jobs VALUES (?, 'pending')", (job_id,))

    def replay(self, job_id: str) -> list[LedgerEntry]:
        with self._b.runtime_db() as db:
            rows = db.execute("SELECT sequence, kind, at, payload FROM ledger WHERE job_id=? ORDER BY sequence", (job_id,)).fetchall()
        return [LedgerEntry(sequence=r["sequence"], kind=r["kind"], at=r["at"], payload=json.loads(r["payload"])) for r in rows]

    def mark(self, job_id: str, status: str) -> None:
        with self._b.runtime_db() as db:
            if db.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone() is None:
                raise BackendError(KIND, "mark", f"unknown job {job_id}")
            db.execute("UPDATE jobs SET status=? WHERE job_id=?", (status, job_id))

    def status(self, job_id: str) -> str | None:
        with self._b.runtime_db() as db:
            row = db.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return None if row is None else row["status"]

    def put_raw(self, key: str, raw_reply: str, valid: bool) -> None:
        with self._b.runtime_db() as db:
            db.execute("INSERT OR REPLACE INTO raw_replies VALUES (?,?,?)", (key, raw_reply, 1 if valid else 0))

    def get_raw(self, key: str) -> RawReply | None:
        with self._b.runtime_db() as db:
            row = db.execute("SELECT raw_reply, valid FROM raw_replies WHERE key=?", (key,)).fetchone()
        return None if row is None else RawReply(key=key, raw_reply=row["raw_reply"], valid=bool(row["valid"]))


class SqliteSessions:
    def __init__(self, backend: SqliteBackend):
        self._b = backend

    def create(self, record: SessionRecord) -> None:
        with self._b.runtime_db() as db:
            if db.execute("SELECT 1 FROM sessions WHERE id=?", (record.id,)).fetchone() is not None:
                raise BackendError(KIND, "create", f"session {record.id} already exists")
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (record.id, record.model_dump_json(), record.last_seen_at))

    def get(self, session_id: str) -> SessionRecord | None:
        with self._b.runtime_db() as db:
            row = db.execute("SELECT data FROM sessions WHERE id=?", (session_id,)).fetchone()
        return None if row is None else SessionRecord.model_validate_json(row["data"])

    def touch(self, session_id: str, now: float, record: SessionRecord | None = None, expected_version: int | None = None) -> None:
        with self._b.runtime_db() as db:
            row = db.execute("SELECT data FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise BackendError(KIND, "touch", f"unknown session {session_id}")
            updated = next_record(session_id, SessionRecord.model_validate_json(row["data"]), now, record, expected_version)
            cursor = db.execute(
                "UPDATE sessions SET data=?, last_seen_at=? WHERE id=? AND data=?", (updated.model_dump_json(), now, session_id, row["data"])
            )
            if cursor.rowcount != 1:
                raise lost(session_id, expected_version)

    def expire(self, now: float, ttl_seconds: float) -> int:
        with self._b.runtime_db() as db:
            cursor = db.execute("DELETE FROM sessions WHERE last_seen_at + ? <= ?", (ttl_seconds, now))
            return int(cursor.rowcount)
