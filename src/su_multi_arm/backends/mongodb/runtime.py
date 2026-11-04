"""MongoDB ``CacheStore``, ``LedgerStore`` and ``SessionStore`` facets (prefix-wide collections).

Cache entries carry ``expires_at`` (clock seconds, authoritative for ``get``) and ``expires_date``
for a server-side TTL index (``expireAfterSeconds=0``) that sweeps expired entries in the
background; ``get`` never returns an expired entry even before the sweep. Sessions store
``last_seen_at`` and ``last_seen_date``; a TTL index is created only when the backend was built with
``session_ttl_seconds`` (``expire(now, ttl)`` is the contract's explicit path either way).
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from ...core.errors import BackendError
from ...core.types import CacheEntry, LedgerEntry, RawReply, SessionRecord
from .._sessions import lost, next_record
from ..memory import CACHE_KEY
from ._db import KIND, as_date, mapped

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import MongoBackend


class MongoCache:
    def __init__(self, backend: MongoBackend):
        self._b = backend
        self._ready = False

    def _collection(self) -> Any:
        collection = self._b.collection("cache")
        if not self._ready:
            with mapped("index"):
                collection.create_index([("expires_date", 1)], expireAfterSeconds=0, name="ttl")
            self._ready = True
        return collection

    def get(self, key: str) -> CacheEntry | None:
        with mapped("get"):
            row = self._collection().find_one({"_id": key})
            if row is None:
                return None
            if row["expires_at"] <= self._b.clock():
                self._collection().delete_one({"_id": key})
                return None
        return CacheEntry(key=key, value=json.loads(row["value"]), expires_at=row["expires_at"])

    def put(self, key: str, value: Any, ttl: float) -> None:
        if not CACHE_KEY.match(key):
            raise BackendError(KIND, "put", "cache keys must start with a GenerationId prefix")
        if ttl <= 0:
            raise BackendError(KIND, "put", "ttl must be positive")
        expires_at = self._b.clock() + ttl
        with mapped("put"):
            self._collection().replace_one(
                {"_id": key},
                {
                    "_id": key,
                    "value": json.dumps(value, sort_keys=True, default=str),
                    "expires_at": expires_at,
                    "expires_date": as_date(expires_at),
                },
                upsert=True,
            )

    def invalidate(self, prefix: str) -> int:
        with mapped("invalidate"):
            result = self._collection().delete_many({"_id": {"$regex": "^" + re.escape(prefix)}})
            return int(result.deleted_count)


class MongoLedger:
    def __init__(self, backend: MongoBackend):
        self._b = backend
        self._ready = False

    def _prepare(self) -> None:
        if not self._ready:
            with mapped("index"):
                self._b.collection("ledger").create_index([("job_id", 1), ("sequence", 1)], unique=True, name="job_sequence")
            self._ready = True

    def append(self, job_id: str, entry: LedgerEntry) -> None:
        self._prepare()
        ledger = self._b.collection("ledger")
        with mapped("append"):
            last = ledger.find_one({"job_id": job_id}, {"sequence": 1}, sort=[("sequence", -1)])
            if last is not None and entry.sequence <= last["sequence"]:
                raise BackendError(KIND, "append", "ledger sequence must increase")
            ledger.insert_one(
                {
                    "job_id": job_id,
                    "sequence": entry.sequence,
                    "kind": entry.kind,
                    "at": entry.at,
                    "payload": json.dumps(entry.payload, sort_keys=True, default=str),
                }
            )
            self._b.collection("jobs").update_one({"_id": job_id}, {"$setOnInsert": {"status": "pending"}}, upsert=True)

    def replay(self, job_id: str) -> list[LedgerEntry]:
        with mapped("replay"):
            rows = list(self._b.collection("ledger").find({"job_id": job_id}).sort("sequence", 1))
        return [LedgerEntry(sequence=r["sequence"], kind=r["kind"], at=r["at"], payload=json.loads(r["payload"])) for r in rows]

    def mark(self, job_id: str, status: str) -> None:
        with mapped("mark"):
            result = self._b.collection("jobs").update_one({"_id": job_id}, {"$set": {"status": status}})
            if result.matched_count == 0:
                raise BackendError(KIND, "mark", f"unknown job {job_id}")

    def status(self, job_id: str) -> str | None:
        with mapped("status"):
            row = self._b.collection("jobs").find_one({"_id": job_id})
        return None if row is None else row["status"]

    def put_raw(self, key: str, raw_reply: str, valid: bool) -> None:
        with mapped("put_raw"):
            self._b.collection("raw_replies").replace_one(
                {"_id": key}, {"_id": key, "raw_reply": raw_reply, "valid": bool(valid)}, upsert=True
            )

    def get_raw(self, key: str) -> RawReply | None:
        with mapped("get_raw"):
            row = self._b.collection("raw_replies").find_one({"_id": key})
        return None if row is None else RawReply(key=key, raw_reply=row["raw_reply"], valid=bool(row["valid"]))


class MongoSessions:
    def __init__(self, backend: MongoBackend):
        self._b = backend
        self._ready = False

    def _collection(self) -> Any:
        collection = self._b.collection("sessions")
        if not self._ready:
            ttl = self._b.session_ttl_seconds
            if ttl is not None:
                with mapped("index"):
                    collection.create_index([("last_seen_date", 1)], expireAfterSeconds=int(ttl), name="ttl")
            self._ready = True
        return collection

    def create(self, record: SessionRecord) -> None:
        from pymongo.errors import DuplicateKeyError

        with mapped("create"):
            try:
                self._collection().insert_one(
                    {
                        "_id": record.id,
                        "data": record.model_dump_json(),
                        "last_seen_at": record.last_seen_at,
                        "last_seen_date": as_date(record.last_seen_at),
                    }
                )
            except DuplicateKeyError:
                raise BackendError(KIND, "create", f"session {record.id} already exists") from None

    def get(self, session_id: str) -> SessionRecord | None:
        with mapped("get"):
            row = self._collection().find_one({"_id": session_id})
        return None if row is None else SessionRecord.model_validate_json(row["data"])

    def touch(self, session_id: str, now: float, record: SessionRecord | None = None, expected_version: int | None = None) -> None:
        with mapped("touch"):
            row = self._collection().find_one({"_id": session_id})
        if row is None:
            raise BackendError(KIND, "touch", f"unknown session {session_id}")
        updated = next_record(session_id, SessionRecord.model_validate_json(row["data"]), now, record, expected_version)
        with mapped("touch"):
            # Filtering on the raw ``data`` just read makes this a single-document compare-and-set.
            result = self._collection().update_one(
                {"_id": session_id, "data": row["data"]},
                {"$set": {"data": updated.model_dump_json(), "last_seen_at": now, "last_seen_date": as_date(now)}},
            )
        if result.matched_count != 1:
            raise lost(session_id, expected_version)

    def expire(self, now: float, ttl_seconds: float) -> int:
        with mapped("expire"):
            result = self._collection().delete_many({"last_seen_at": {"$lte": now - ttl_seconds}})
            return int(result.deleted_count)
