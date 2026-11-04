"""Generation registry for the mongodb backend: pointer document, manifests, failures, write lock.

Mirrors ``ingestion/release.py::FileGenerationRegistry`` on one ``<prefix>_meta`` collection:
``{_id: "CURRENT", generation}`` is the pointer, replaced in a single-document write (atomic in
MongoDB) and only by ``activate`` (with ``expected``: a conditional single-document update/insert,
i.e. a compare-and-set on the generation the build started from). ``record_failure`` writes
``failure:<generation>`` and never touches the pointer. ``lock()`` takes the write lease
(``ingestion/lease.py``): a ``write_lock`` document ``{owner, expires_at}``; renewal and release
filter on the owner token, and a lease is taken over only once ``expires_at`` has passed (after
``RENEWALS_PER_TTL`` missed heartbeats). A live lease of another writer is a typed
``WriteConflict(held)``, never a wait.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator

from ...ingestion.lease import LEASE_TTL_SECONDS, hold, stale_base
from ...ingestion.release import ANY
from ._db import KIND, mapped

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import MongoBackend

HEX32 = set("0123456789abcdef")


class MongoGenerationRegistry:
    def __init__(self, backend: MongoBackend, lock_ttl_seconds: float = LEASE_TTL_SECONDS):
        self.backend = backend
        self.lock_ttl_seconds = lock_ttl_seconds

    def _meta(self) -> Any:
        return self.backend.collection("meta")

    def lease_acquire(self, token: str, ttl: float) -> bool:
        from pymongo.errors import DuplicateKeyError

        now = self.backend.clock()
        lease = {"owner": token, "expires_at": now + ttl}
        with mapped("lock"):
            try:
                self._meta().insert_one({"_id": "write_lock", **lease})
                return True
            except DuplicateKeyError:
                pass
            # Stale takeover in one conditional write; a lease written before owner tokens existed
            # (``acquired_at`` only) expires ``ttl`` after it was taken.
            expired = {"$or": [{"expires_at": {"$lte": now}}, {"expires_at": {"$exists": False}, "acquired_at": {"$lte": now - ttl}}]}
            taken = self._meta().find_one_and_update({"_id": "write_lock", **expired}, {"$set": lease, "$unset": {"acquired_at": ""}})
        return taken is not None

    def lease_renew(self, token: str, ttl: float) -> bool:
        with mapped("renew"):
            return self._meta().update_one({"_id": "write_lock", "owner": token}, {"$set": {"expires_at": self.backend.clock() + ttl}}).matched_count == 1

    def lease_release(self, token: str) -> bool:
        with mapped("unlock"):
            return self._meta().delete_one({"_id": "write_lock", "owner": token}).deleted_count == 1

    @contextmanager
    def lock(self) -> Iterator[Any]:
        with hold(self, KIND, ttl=self.lock_ttl_seconds) as lease:
            yield lease

    def current_generation(self) -> str | None:
        with mapped("current"):
            pointer = self._meta().find_one({"_id": "CURRENT"})
        if pointer is None:
            return None
        generation = str(pointer.get("generation", ""))
        if len(generation) != 32 or set(generation) - HEX32:
            raise ValueError("invalid index pointer")
        return generation

    def current(self) -> dict[str, Any] | None:
        generation = self.current_generation()
        if generation is None:
            return None
        manifest = self.manifest(generation)
        if manifest is None:
            raise ValueError(f"CURRENT names generation {generation} but its manifest is missing")
        return manifest

    def manifest(self, generation: str) -> dict[str, Any] | None:
        with mapped("manifest"):
            row = self._meta().find_one({"_id": f"manifest:{generation}"})
        return None if row is None else dict(row["manifest"])

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        key = f"manifest:{manifest['generation']}"
        with mapped("write_manifest"):
            self._meta().replace_one({"_id": key}, {"_id": key, "manifest": dict(manifest)}, upsert=True)

    def activate(self, manifest: dict[str, Any], *, expected: str | None = ANY) -> None:
        from pymongo.errors import DuplicateKeyError

        generation = manifest["generation"]
        if self.manifest(generation) is None:
            raise ValueError("cannot activate a generation without a written manifest")
        with mapped("activate"):
            if expected is ANY:
                self._meta().replace_one({"_id": "CURRENT"}, {"_id": "CURRENT", "generation": generation}, upsert=True)
                return
            if expected is None:
                try:
                    self._meta().insert_one({"_id": "CURRENT", "generation": generation})
                    return
                except DuplicateKeyError:
                    switched = False
            else:
                switched = self._meta().update_one({"_id": "CURRENT", "generation": expected}, {"$set": {"generation": generation}}).matched_count == 1
        if not switched:
            raise stale_base(KIND, self.current_generation(), expected)

    def deactivate(self, generation: str) -> None:
        """Remove the pointer document only while it still names ``generation`` (single-document write)."""
        with mapped("deactivate"):
            self._meta().delete_one({"_id": "CURRENT", "generation": generation})

    def generations(self) -> list[str]:
        with mapped("generations"):
            rows = self._meta().find({"_id": {"$regex": "^manifest:"}}, {"_id": 1})
            return sorted(r["_id"].split(":", 1)[1] for r in rows)

    def record_failure(self, failure: dict[str, Any]) -> None:
        key = f"failure:{failure['generation']}"
        with mapped("record_failure"):
            self._meta().replace_one({"_id": key}, {"_id": key, "failure": dict(failure)}, upsert=True)

    def failures(self) -> list[dict[str, Any]]:
        with mapped("failures"):
            rows = self._meta().find({"_id": {"$regex": "^failure:"}}).sort("_id", 1)
            return [dict(r["failure"]) for r in rows]
