"""Generic SQL ``CacheStore`` (exact keys only), ``LedgerStore`` and ``SessionStore`` facets."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Sequence

import sqlalchemy as sa

from ...core.errors import BackendError
from ...core.types import CacheEntry, LedgerEntry, RawReply, SessionRecord
from .._sessions import lost, next_record
from ..memory import CACHE_KEY
from ._db import KIND

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import SqlBackend


class SqlCache:
    def __init__(self, backend: SqlBackend):
        self._b = backend

    def get(self, key: str) -> CacheEntry | None:
        c = self._b.rt["cache"]
        with self._b.tx("get") as conn:
            row = conn.execute(sa.select(c.c.value, c.c.expires_at).where(c.c.key == key)).first()
            if row is None:
                return None
            if row[1] <= self._b.clock():
                conn.execute(c.delete().where(c.c.key == key))
                return None
        return CacheEntry(key=key, value=json.loads(row[0]), expires_at=row[1])

    def put(self, key: str, value: Any, ttl: float) -> None:
        if not CACHE_KEY.match(key):
            raise BackendError(KIND, "put", "cache keys must start with a GenerationId prefix")
        if ttl <= 0:
            raise BackendError(KIND, "put", "ttl must be positive")
        if len(key) > 768:
            raise BackendError(KIND, "put", "cache key longer than 768 characters")
        c = self._b.rt["cache"]
        with self._b.tx("put") as conn:
            conn.execute(c.delete().where(c.c.key == key))
            conn.execute(c.insert().values(key=key, value=json.dumps(value, sort_keys=True, default=str), expires_at=self._b.clock() + ttl))

    def invalidate(self, prefix: str) -> int:
        c = self._b.rt["cache"]
        with self._b.tx("invalidate") as conn:
            return int(conn.execute(c.delete().where(c.c.key.startswith(prefix, autoescape=True))).rowcount)


class SqlLedger:
    def __init__(self, backend: SqlBackend):
        self._b = backend

    def append(self, job_id: str, entry: LedgerEntry) -> None:
        ledger, jobs = self._b.rt["ledger"], self._b.rt["jobs"]
        with self._b.tx("append") as conn:
            last = conn.execute(sa.select(sa.func.max(ledger.c.sequence)).where(ledger.c.job_id == job_id)).scalar()
            if last is not None and entry.sequence <= last:
                raise BackendError(KIND, "append", "ledger sequence must increase")
            conn.execute(
                ledger.insert().values(
                    job_id=job_id, sequence=entry.sequence, kind=entry.kind, at=entry.at, payload=json.dumps(entry.payload, sort_keys=True, default=str)
                )
            )
            if conn.execute(sa.select(jobs.c.job_id).where(jobs.c.job_id == job_id)).first() is None:
                conn.execute(jobs.insert().values(job_id=job_id, status="pending"))

    def replay(self, job_id: str) -> list[LedgerEntry]:
        ledger = self._b.rt["ledger"]
        with self._b.tx("replay") as conn:
            rows = conn.execute(
                sa.select(ledger.c.sequence, ledger.c.kind, ledger.c.at, ledger.c.payload).where(ledger.c.job_id == job_id).order_by(ledger.c.sequence)
            ).all()
        return [LedgerEntry(sequence=r[0], kind=r[1], at=r[2], payload=json.loads(r[3])) for r in rows]

    def mark(self, job_id: str, status: str) -> None:
        jobs = self._b.rt["jobs"]
        with self._b.tx("mark") as conn:
            if conn.execute(sa.select(jobs.c.job_id).where(jobs.c.job_id == job_id)).first() is None:
                raise BackendError(KIND, "mark", f"unknown job {job_id}")
            conn.execute(jobs.update().where(jobs.c.job_id == job_id).values(status=status))

    def status(self, job_id: str) -> str | None:
        jobs = self._b.rt["jobs"]
        with self._b.tx("status") as conn:
            row = conn.execute(sa.select(jobs.c.status).where(jobs.c.job_id == job_id)).first()
        return None if row is None else row[0]

    def put_raw(self, key: str, raw_reply: str, valid: bool, tags: Sequence[str] = ()) -> None:
        raw = self._b.rt["raw_replies"]
        with self._b.tx("put_raw") as conn:
            conn.execute(raw.delete().where(raw.c.key == key))
            conn.execute(raw.insert().values(key=key, raw_reply=raw_reply, valid=bool(valid)))
        if tags:
            self.tag_raw(key, tags)

    def get_raw(self, key: str) -> RawReply | None:
        raw = self._b.rt["raw_replies"]
        with self._b.tx("get_raw") as conn:
            row = conn.execute(sa.select(raw.c.raw_reply, raw.c.valid).where(raw.c.key == key)).first()
        return None if row is None else RawReply(key=key, raw_reply=row[0], valid=bool(row[1]))


    # retention (``extras.LedgerRetention``) ----------------------------------------------------
    def tag_raw(self, key: str, tags: Sequence[str]) -> None:
        raw, t = self._b.rt["raw_replies"], self._b.rt["raw_tags"]
        with self._b.tx("tag_raw") as conn:
            if conn.execute(sa.select(raw.c.key).where(raw.c.key == key)).first() is None:
                raise BackendError(KIND, "tag_raw", f"unknown raw entry {key}")
            have = {r[0] for r in conn.execute(sa.select(t.c.tag).where(t.c.key == key))}
            for tag in sorted(set(tags) - have):
                conn.execute(t.insert().values(key=key, tag=tag))

    def raw_tags(self, key: str) -> list[str]:
        t = self._b.rt["raw_tags"]
        with self._b.tx("raw_tags") as conn:
            return [r[0] for r in conn.execute(sa.select(t.c.tag).where(t.c.key == key).order_by(t.c.tag))]

    def tagged(self, tag: str) -> list[str]:
        t = self._b.rt["raw_tags"]
        with self._b.tx("tagged") as conn:
            return [r[0] for r in conn.execute(sa.select(t.c.key).where(t.c.tag == tag).order_by(t.c.key))]

    def add_refs(self, generation: str, keys: Sequence[str]) -> None:
        refs = self._b.rt["ledger_refs"]
        with self._b.tx("add_refs") as conn:
            have = {r[0] for r in conn.execute(sa.select(refs.c.key).where(refs.c.generation == generation))}
            fresh = sorted(set(keys) - have)
            if fresh:
                conn.execute(refs.insert(), [{"generation": generation, "key": k} for k in fresh])

    def refs(self, generation: str) -> list[str]:
        refs = self._b.rt["ledger_refs"]
        with self._b.tx("refs") as conn:
            return [r[0] for r in conn.execute(sa.select(refs.c.key).where(refs.c.generation == generation).order_by(refs.c.key))]

    def referencing_generations(self) -> list[str]:
        refs = self._b.rt["ledger_refs"]
        with self._b.tx("referencing_generations") as conn:
            return [r[0] for r in conn.execute(sa.select(refs.c.generation).distinct().order_by(refs.c.generation))]

    def purge(self, spec: Any) -> dict[str, int]:
        raw, t, refs = self._b.rt["raw_replies"], self._b.rt["raw_tags"], self._b.rt["ledger_refs"]
        with self._b.tx("purge") as conn:
            dropped = sum(int(conn.execute(refs.delete().where(refs.c.generation == g)).rowcount) for g in spec.drop_refs_of)
            tagged = 0
            for tag in spec.tags:
                keys = [r[0] for r in conn.execute(sa.select(t.c.key).where(t.c.tag == tag))]
                for key in keys:
                    tagged += int(conn.execute(raw.delete().where(raw.c.key == key)).rowcount)
                    conn.execute(t.delete().where(t.c.key == key))
            unreferenced = 0
            for prefix in spec.unreferenced_prefixes:
                unreferenced += int(
                    conn.execute(raw.delete().where(raw.c.key.startswith(prefix, autoescape=True), raw.c.key.not_in(sa.select(refs.c.key)), raw.c.key.not_in(sa.select(t.c.key)))).rowcount
                )
            conn.execute(t.delete().where(t.c.key.not_in(sa.select(raw.c.key))))
        return {"refs_dropped": dropped, "tagged": tagged, "unreferenced": unreferenced}

    def drop_job(self, job_id: str) -> int:
        ledger, jobs = self._b.rt["ledger"], self._b.rt["jobs"]
        with self._b.tx("drop_job") as conn:
            removed = int(conn.execute(ledger.delete().where(ledger.c.job_id == job_id)).rowcount)
            conn.execute(jobs.delete().where(jobs.c.job_id == job_id))
        return removed

    def jobs(self) -> list[str]:
        ledger = self._b.rt["ledger"]
        with self._b.tx("jobs") as conn:
            return [r[0] for r in conn.execute(sa.select(ledger.c.job_id).distinct().order_by(ledger.c.job_id))]

    def raw_entries(self, prefix: str, after: str | None, limit: int) -> list[RawReply]:
        raw = self._b.rt["raw_replies"]
        with self._b.tx("raw_entries") as conn:
            query = sa.select(raw.c.key, raw.c.raw_reply, raw.c.valid).where(raw.c.key.startswith(prefix, autoescape=True))
            if after is not None:
                query = query.where(raw.c.key > after)
            rows = conn.execute(query.order_by(raw.c.key).limit(limit)).all()
        return [RawReply(key=r[0], raw_reply=r[1], valid=bool(r[2])) for r in rows]


class SqlSessions:
    def __init__(self, backend: SqlBackend):
        self._b = backend

    def create(self, record: SessionRecord) -> None:
        s = self._b.rt["sessions"]
        with self._b.tx("create") as conn:
            if conn.execute(sa.select(s.c.id).where(s.c.id == record.id)).first() is not None:
                raise BackendError(KIND, "create", f"session {record.id} already exists")
            conn.execute(s.insert().values(id=record.id, data=record.model_dump_json(), last_seen_at=record.last_seen_at))

    def get(self, session_id: str) -> SessionRecord | None:
        s = self._b.rt["sessions"]
        with self._b.tx("get") as conn:
            row = conn.execute(sa.select(s.c.data).where(s.c.id == session_id)).first()
        return None if row is None else SessionRecord.model_validate_json(row[0])

    def touch(self, session_id: str, now: float, record: SessionRecord | None = None, expected_version: int | None = None) -> None:
        s = self._b.rt["sessions"]
        with self._b.tx("touch") as conn:
            row = conn.execute(sa.select(s.c.data).where(s.c.id == session_id)).first()
            if row is None:
                raise BackendError(KIND, "touch", f"unknown session {session_id}")
            updated = next_record(session_id, SessionRecord.model_validate_json(row[0]), now, record, expected_version)
            result = conn.execute(
                s.update().where(s.c.id == session_id, s.c.data == row[0]).values(data=updated.model_dump_json(), last_seen_at=now)
            )
            if result.rowcount != 1:
                raise lost(session_id, expected_version)

    def expire(self, now: float, ttl_seconds: float) -> int:
        s = self._b.rt["sessions"]
        with self._b.tx("expire") as conn:
            return int(conn.execute(s.delete().where(s.c.last_seen_at <= now - ttl_seconds)).rowcount)
