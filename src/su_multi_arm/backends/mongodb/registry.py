"""Generation registry for the mongodb backend: pointer document, manifests, failures, write lock.

Mirrors ``ingestion/release.py::FileGenerationRegistry`` on one ``<prefix>_meta`` collection:
``{_id: "CURRENT", generation}`` is the pointer, replaced in a single-document write (atomic in
MongoDB) and only by ``activate``; ``record_failure`` writes ``failure:<generation>`` and never
touches the pointer. ``lock()`` takes a lease document; a lease older than ``lock_ttl_seconds`` is
treated as abandoned and taken over, otherwise a held lock is a typed error, never a wait.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator

from ...core.errors import BackendError
from ._db import KIND, mapped

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import MongoBackend

HEX32 = set("0123456789abcdef")


class MongoGenerationRegistry:
    def __init__(self, backend: MongoBackend, lock_ttl_seconds: float = 600.0):
        self.backend = backend
        self.lock_ttl_seconds = lock_ttl_seconds

    def _meta(self) -> Any:
        return self.backend.collection("meta")

    @contextmanager
    def lock(self) -> Iterator[None]:
        from pymongo.errors import DuplicateKeyError

        now = self.backend.clock()
        with mapped("lock"):
            try:
                self._meta().insert_one({"_id": "write_lock", "acquired_at": now})
            except DuplicateKeyError:
                taken = self._meta().find_one_and_update(
                    {"_id": "write_lock", "acquired_at": {"$lte": now - self.lock_ttl_seconds}},
                    {"$set": {"acquired_at": now}},
                )
                if taken is None:
                    raise BackendError(KIND, "lock", "write lock is held by another writer") from None
        try:
            yield
        finally:
            with mapped("unlock"):
                self._meta().delete_one({"_id": "write_lock", "acquired_at": now})

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

    def activate(self, manifest: dict[str, Any]) -> None:
        generation = manifest["generation"]
        if self.manifest(generation) is None:
            raise ValueError("cannot activate a generation without a written manifest")
        with mapped("activate"):
            self._meta().replace_one({"_id": "CURRENT"}, {"_id": "CURRENT", "generation": generation}, upsert=True)

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
