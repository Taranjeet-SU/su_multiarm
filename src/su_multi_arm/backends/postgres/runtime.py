"""PostgreSQL ``CacheStore``, ``LedgerStore`` and ``SessionStore`` facets (runtime schema ``<prefix>``).

Cache entries carry ``expires_at`` in clock seconds; ``get`` never returns an expired entry and
deletes it. ``invalidate(prefix)`` compares the literal key prefix (``left(key, n) = prefix``), so
``%``/``_`` in a dataset name are never pattern characters.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ...core.errors import BackendError
from ...core.types import CacheEntry, LedgerEntry, RawReply, SessionRecord
from ..memory import CACHE_KEY
from ._db import KIND

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import PostgresBackend


class PostgresCache:
    def __init__(self, backend: PostgresBackend):
        self._b = backend

    def get(self, key: str) -> CacheEntry | None:
        table = self._b.table("cache")
        with self._b.tx("get") as cur:
            cur.execute(f"SELECT value, expires_at FROM {table} WHERE key = %s", (key,))
            row = cur.fetchone()
            if row is None:
                return None
            if row[1] <= self._b.clock():
                cur.execute(f"DELETE FROM {table} WHERE key = %s", (key,))
                return None
        return CacheEntry(key=key, value=json.loads(row[0]), expires_at=row[1])

    def put(self, key: str, value: Any, ttl: float) -> None:
        if not CACHE_KEY.match(key):
            raise BackendError(KIND, "put", "cache keys must start with a GenerationId prefix")
        if ttl <= 0:
            raise BackendError(KIND, "put", "ttl must be positive")
        with self._b.tx("put") as cur:
            cur.execute(
                f"INSERT INTO {self._b.table('cache')}(key, value, expires_at) VALUES (%s,%s,%s) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, expires_at=EXCLUDED.expires_at",
                (key, json.dumps(value, sort_keys=True, default=str), self._b.clock() + ttl),
            )

    def invalidate(self, prefix: str) -> int:
        with self._b.tx("invalidate") as cur:
            cur.execute(f"DELETE FROM {self._b.table('cache')} WHERE left(key, %s) = %s", (len(prefix), prefix))
            return int(cur.rowcount)


class PostgresLedger:
    def __init__(self, backend: PostgresBackend):
        self._b = backend

    def append(self, job_id: str, entry: LedgerEntry) -> None:
        with self._b.tx("append") as cur:
            cur.execute(f"SELECT max(sequence) FROM {self._b.table('ledger')} WHERE job_id = %s", (job_id,))
            last = cur.fetchone()[0]
            if last is not None and entry.sequence <= last:
                raise BackendError(KIND, "append", "ledger sequence must increase")
            cur.execute(
                f"INSERT INTO {self._b.table('ledger')}(job_id, sequence, kind, at, payload) VALUES (%s,%s,%s,%s,%s)",
                (job_id, entry.sequence, entry.kind, entry.at, json.dumps(entry.payload, sort_keys=True, default=str)),
            )
            cur.execute(f"INSERT INTO {self._b.table('jobs')}(job_id, status) VALUES (%s, 'pending') ON CONFLICT DO NOTHING", (job_id,))

    def replay(self, job_id: str) -> list[LedgerEntry]:
        with self._b.tx("replay") as cur:
            cur.execute(f"SELECT sequence, kind, at, payload FROM {self._b.table('ledger')} WHERE job_id = %s ORDER BY sequence", (job_id,))
            rows = cur.fetchall()
        return [LedgerEntry(sequence=r[0], kind=r[1], at=r[2], payload=json.loads(r[3])) for r in rows]

    def mark(self, job_id: str, status: str) -> None:
        with self._b.tx("mark") as cur:
            cur.execute(f"UPDATE {self._b.table('jobs')} SET status = %s WHERE job_id = %s", (status, job_id))
            if cur.rowcount == 0:
                raise BackendError(KIND, "mark", f"unknown job {job_id}")

    def status(self, job_id: str) -> str | None:
        with self._b.tx("status") as cur:
            cur.execute(f"SELECT status FROM {self._b.table('jobs')} WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
        return None if row is None else row[0]

    def put_raw(self, key: str, raw_reply: str, valid: bool) -> None:
        with self._b.tx("put_raw") as cur:
            cur.execute(
                f"INSERT INTO {self._b.table('raw_replies')}(key, raw_reply, valid) VALUES (%s,%s,%s) "
                "ON CONFLICT (key) DO UPDATE SET raw_reply=EXCLUDED.raw_reply, valid=EXCLUDED.valid",
                (key, raw_reply, bool(valid)),
            )

    def get_raw(self, key: str) -> RawReply | None:
        with self._b.tx("get_raw") as cur:
            cur.execute(f"SELECT raw_reply, valid FROM {self._b.table('raw_replies')} WHERE key = %s", (key,))
            row = cur.fetchone()
        return None if row is None else RawReply(key=key, raw_reply=row[0], valid=bool(row[1]))


class PostgresSessions:
    def __init__(self, backend: PostgresBackend):
        self._b = backend

    def create(self, record: SessionRecord) -> None:
        with self._b.tx("create") as cur:
            cur.execute(
                f"INSERT INTO {self._b.table('sessions')}(id, data, last_seen_at) VALUES (%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                (record.id, record.model_dump_json(), record.last_seen_at),
            )
            if cur.rowcount == 0:
                raise BackendError(KIND, "create", f"session {record.id} already exists")

    def get(self, session_id: str) -> SessionRecord | None:
        with self._b.tx("get") as cur:
            cur.execute(f"SELECT data FROM {self._b.table('sessions')} WHERE id = %s", (session_id,))
            row = cur.fetchone()
        return None if row is None else SessionRecord.model_validate_json(row[0])

    def touch(self, session_id: str, now: float, record: SessionRecord | None = None) -> None:
        with self._b.tx("touch") as cur:
            cur.execute(f"SELECT data FROM {self._b.table('sessions')} WHERE id = %s FOR UPDATE", (session_id,))
            row = cur.fetchone()
            if row is None:
                raise BackendError(KIND, "touch", f"unknown session {session_id}")
            updated = (record or SessionRecord.model_validate_json(row[0])).model_copy(update={"last_seen_at": now})
            cur.execute(
                f"UPDATE {self._b.table('sessions')} SET data = %s, last_seen_at = %s WHERE id = %s",
                (updated.model_dump_json(), now, session_id),
            )

    def expire(self, now: float, ttl_seconds: float) -> int:
        with self._b.tx("expire") as cur:
            cur.execute(f"DELETE FROM {self._b.table('sessions')} WHERE last_seen_at + %s <= %s", (ttl_seconds, now))
            return int(cur.rowcount)
