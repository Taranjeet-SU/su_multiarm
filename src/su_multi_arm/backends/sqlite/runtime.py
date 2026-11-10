"""SQLite ``CacheStore``, ``LedgerStore`` and ``SessionStore`` facets over ``runtime.sqlite``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Sequence

from ...core.errors import BackendError
from ...core.types import CacheEntry, LedgerEntry, RawReply, SessionRecord
from .._sessions import lost, next_record
from ..memory import CACHE_KEY
from ._db import KIND, RETENTION_SCHEMA

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
        self._retention_ready = False

    def _retention_db(self):
        """The runtime database with the retention side tables (created once in an older file)."""
        if not self._retention_ready:
            with self._b.runtime_db() as db:
                db.executescript(RETENTION_SCHEMA)
            self._retention_ready = True
        return self._b.runtime_db()

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

    def put_raw(self, key: str, raw_reply: str, valid: bool, tags: Sequence[str] = ()) -> None:
        with self._b.runtime_db() as db:
            db.execute("INSERT OR REPLACE INTO raw_replies VALUES (?,?,?)", (key, raw_reply, 1 if valid else 0))
        if tags:
            self.tag_raw(key, tags)

    def get_raw(self, key: str) -> RawReply | None:
        with self._b.runtime_db() as db:
            row = db.execute("SELECT raw_reply, valid FROM raw_replies WHERE key=?", (key,)).fetchone()
        return None if row is None else RawReply(key=key, raw_reply=row["raw_reply"], valid=bool(row["valid"]))

    # retention (``extras.LedgerRetention``) ----------------------------------------------------
    def tag_raw(self, key: str, tags: Sequence[str]) -> None:
        with self._retention_db() as db:
            if db.execute("SELECT 1 FROM raw_replies WHERE key=?", (key,)).fetchone() is None:
                raise BackendError(KIND, "tag_raw", f"unknown raw entry {key}")
            db.executemany("INSERT OR IGNORE INTO raw_tags VALUES (?,?)", [(key, t) for t in tags])

    def raw_tags(self, key: str) -> list[str]:
        with self._retention_db() as db:
            return [r[0] for r in db.execute("SELECT tag FROM raw_tags WHERE key=? ORDER BY tag", (key,))]

    def tagged(self, tag: str) -> list[str]:
        with self._retention_db() as db:
            return [r[0] for r in db.execute("SELECT key FROM raw_tags WHERE tag=? ORDER BY key", (tag,))]

    def add_refs(self, generation: str, keys: Sequence[str]) -> None:
        with self._retention_db() as db:
            db.executemany("INSERT OR IGNORE INTO ledger_refs VALUES (?,?)", [(generation, k) for k in keys])

    def refs(self, generation: str) -> list[str]:
        with self._retention_db() as db:
            return [r[0] for r in db.execute("SELECT key FROM ledger_refs WHERE generation=? ORDER BY key", (generation,))]

    def referencing_generations(self) -> list[str]:
        with self._retention_db() as db:
            return [r[0] for r in db.execute("SELECT DISTINCT generation FROM ledger_refs ORDER BY generation")]

    def purge(self, spec: Any) -> dict[str, int]:
        """One transaction: drop the deleted generations' refs, the tagged entries, then the
        unreferenced entries under the given prefixes."""
        with self._retention_db() as db:
            dropped = sum(db.execute("DELETE FROM ledger_refs WHERE generation=?", (g,)).rowcount for g in spec.drop_refs_of)
            tagged = 0
            for tag in spec.tags:
                keys = [r[0] for r in db.execute("SELECT key FROM raw_tags WHERE tag=?", (tag,))]
                for key in keys:
                    tagged += db.execute("DELETE FROM raw_replies WHERE key=?", (key,)).rowcount
                    db.execute("DELETE FROM raw_tags WHERE key=?", (key,))
            unreferenced = 0
            for prefix in spec.unreferenced_prefixes:
                unreferenced += db.execute(
                    "DELETE FROM raw_replies WHERE substr(key, 1, ?)=? AND key NOT IN (SELECT key FROM ledger_refs) AND key NOT IN (SELECT key FROM raw_tags)",
                    (len(prefix), prefix),
                ).rowcount
            db.execute("DELETE FROM raw_tags WHERE key NOT IN (SELECT key FROM raw_replies)")
        return {"refs_dropped": dropped, "tagged": tagged, "unreferenced": unreferenced}

    def drop_job(self, job_id: str) -> int:
        with self._b.runtime_db() as db:
            removed = db.execute("DELETE FROM ledger WHERE job_id=?", (job_id,)).rowcount
            db.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
        return int(removed)

    def jobs(self) -> list[str]:
        with self._b.runtime_db() as db:
            return [r[0] for r in db.execute("SELECT DISTINCT job_id FROM ledger ORDER BY job_id")]

    def raw_entries(self, prefix: str, after: str | None, limit: int) -> list[RawReply]:
        with self._b.runtime_db() as db:
            rows = db.execute(
                "SELECT key, raw_reply, valid FROM raw_replies WHERE substr(key, 1, ?)=? AND key > ? ORDER BY key LIMIT ?",
                (len(prefix), prefix, after or "", limit),
            ).fetchall()
        return [RawReply(key=r["key"], raw_reply=r["raw_reply"], valid=bool(r["valid"])) for r in rows]


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
