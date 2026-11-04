"""PostgreSQL ``CacheStore``, ``LedgerStore`` and ``SessionStore`` facets (runtime schema ``<prefix>``).

Cache entries carry ``expires_at`` in clock seconds; ``get`` never returns an expired entry and
deletes it. ``invalidate(prefix)`` compares the literal key prefix (``left(key, n) = prefix``), so
``%``/``_`` in a dataset name are never pattern characters.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Sequence

from ...core.errors import BackendError
from ...core.types import CacheEntry, LedgerEntry, RawReply, SessionRecord
from .._sessions import lost, next_record
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

    def put_raw(self, key: str, raw_reply: str, valid: bool, tags: Sequence[str] = ()) -> None:
        with self._b.tx("put_raw") as cur:
            cur.execute(
                f"INSERT INTO {self._b.table('raw_replies')}(key, raw_reply, valid) VALUES (%s,%s,%s) "
                "ON CONFLICT (key) DO UPDATE SET raw_reply=EXCLUDED.raw_reply, valid=EXCLUDED.valid",
                (key, raw_reply, bool(valid)),
            )
        if tags:
            self.tag_raw(key, tags)

    def get_raw(self, key: str) -> RawReply | None:
        with self._b.tx("get_raw") as cur:
            cur.execute(f"SELECT raw_reply, valid FROM {self._b.table('raw_replies')} WHERE key = %s", (key,))
            row = cur.fetchone()
        return None if row is None else RawReply(key=key, raw_reply=row[0], valid=bool(row[1]))


    # retention (``extras.LedgerRetention``) ----------------------------------------------------
    def tag_raw(self, key: str, tags: Sequence[str]) -> None:
        with self._b.tx("tag_raw") as cur:
            cur.execute(f"SELECT 1 FROM {self._b.table('raw_replies')} WHERE key = %s", (key,))
            if cur.fetchone() is None:
                raise BackendError(KIND, "tag_raw", f"unknown raw entry {key}")
            for tag in sorted(set(tags)):
                cur.execute(f"INSERT INTO {self._b.table('raw_tags')}(key, tag) VALUES (%s,%s) ON CONFLICT DO NOTHING", (key, tag))

    def _column(self, operation: str, sql: str, params: tuple) -> list[str]:
        with self._b.tx(operation) as cur:
            cur.execute(sql, params)
            return [r[0] for r in cur.fetchall()]

    def raw_tags(self, key: str) -> list[str]:
        return self._column("raw_tags", f"SELECT tag FROM {self._b.table('raw_tags')} WHERE key = %s ORDER BY tag", (key,))

    def tagged(self, tag: str) -> list[str]:
        return self._column("tagged", f"SELECT key FROM {self._b.table('raw_tags')} WHERE tag = %s ORDER BY key", (tag,))

    def add_refs(self, generation: str, keys: Sequence[str]) -> None:
        with self._b.tx("add_refs") as cur:
            for key in sorted(set(keys)):
                cur.execute(f"INSERT INTO {self._b.table('ledger_refs')}(generation, key) VALUES (%s,%s) ON CONFLICT DO NOTHING", (generation, key))

    def refs(self, generation: str) -> list[str]:
        return self._column("refs", f"SELECT key FROM {self._b.table('ledger_refs')} WHERE generation = %s ORDER BY key", (generation,))

    def referencing_generations(self) -> list[str]:
        return self._column("referencing_generations", f"SELECT DISTINCT generation FROM {self._b.table('ledger_refs')} ORDER BY generation", ())

    def purge(self, spec: Any) -> dict[str, int]:
        raw, tags, refs = self._b.table("raw_replies"), self._b.table("raw_tags"), self._b.table("ledger_refs")
        with self._b.tx("purge") as cur:
            dropped = 0
            for generation in spec.drop_refs_of:
                cur.execute(f"DELETE FROM {refs} WHERE generation = %s", (generation,))
                dropped += int(cur.rowcount)
            tagged = 0
            for tag in spec.tags:
                cur.execute(f"DELETE FROM {raw} WHERE key IN (SELECT key FROM {tags} WHERE tag = %s)", (tag,))
                tagged += int(cur.rowcount)
            unreferenced = 0
            for prefix in spec.unreferenced_prefixes:
                cur.execute(
                    f"DELETE FROM {raw} r WHERE left(r.key, %s) = %s AND NOT EXISTS (SELECT 1 FROM {refs} x WHERE x.key = r.key) "
                    f"AND NOT EXISTS (SELECT 1 FROM {tags} g WHERE g.key = r.key)",
                    (len(prefix), prefix),
                )
                unreferenced += int(cur.rowcount)
            cur.execute(f"DELETE FROM {tags} t WHERE NOT EXISTS (SELECT 1 FROM {raw} r WHERE r.key = t.key)")
        return {"refs_dropped": dropped, "tagged": tagged, "unreferenced": unreferenced}

    def drop_job(self, job_id: str) -> int:
        with self._b.tx("drop_job") as cur:
            cur.execute(f"DELETE FROM {self._b.table('ledger')} WHERE job_id = %s", (job_id,))
            removed = int(cur.rowcount)
            cur.execute(f"DELETE FROM {self._b.table('jobs')} WHERE job_id = %s", (job_id,))
        return removed

    def jobs(self) -> list[str]:
        return self._column("jobs", f"SELECT DISTINCT job_id FROM {self._b.table('ledger')} ORDER BY job_id", ())

    def raw_entries(self, prefix: str, after: str | None, limit: int) -> list[RawReply]:
        with self._b.tx("raw_entries") as cur:
            cur.execute(
                f"SELECT key, raw_reply, valid FROM {self._b.table('raw_replies')} WHERE left(key, %s) = %s AND key > %s ORDER BY key LIMIT %s",
                (len(prefix), prefix, after or "", limit),
            )
            rows = cur.fetchall()
        return [RawReply(key=r[0], raw_reply=r[1], valid=bool(r[2])) for r in rows]


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

    def touch(self, session_id: str, now: float, record: SessionRecord | None = None, expected_version: int | None = None) -> None:
        with self._b.tx("touch") as cur:
            cur.execute(f"SELECT data FROM {self._b.table('sessions')} WHERE id = %s FOR UPDATE", (session_id,))
            row = cur.fetchone()
            if row is None:
                raise BackendError(KIND, "touch", f"unknown session {session_id}")
            # ``FOR UPDATE`` holds the row until commit; the ``data`` guard keeps the write a CAS anyway.
            updated = next_record(session_id, SessionRecord.model_validate_json(row[0]), now, record, expected_version)
            cur.execute(
                f"UPDATE {self._b.table('sessions')} SET data = %s, last_seen_at = %s WHERE id = %s AND data = %s",
                (updated.model_dump_json(), now, session_id, row[0]),
            )
            if cur.rowcount != 1:
                raise lost(session_id, expected_version)

    def expire(self, now: float, ttl_seconds: float) -> int:
        with self._b.tx("expire") as cur:
            cur.execute(f"DELETE FROM {self._b.table('sessions')} WHERE last_seen_at + %s <= %s", (ttl_seconds, now))
            return int(cur.rowcount)
