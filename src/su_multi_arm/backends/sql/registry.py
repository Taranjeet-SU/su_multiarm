"""Generation registry for the generic SQL backend: pointer row, manifests, failures, write lease.

Mirrors ``ingestion/release.py::FileGenerationRegistry`` on ``<prefix>_meta``: the ``CURRENT`` row is
the pointer, replaced inside one transaction (delete + insert, atomic for readers) and only by
``activate``; ``record_failure`` writes ``failure:<generation>`` and never touches the pointer.
``lock()`` inserts a ``write_lock`` lease row (primary-key conflict = held, a typed error, never a
wait); a lease older than ``lock_ttl_seconds`` is treated as abandoned and taken over.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator

import sqlalchemy as sa

from ...core.errors import BackendError
from ._db import KIND

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import SqlBackend

HEX32 = set("0123456789abcdef")


class SqlGenerationRegistry:
    def __init__(self, backend: SqlBackend, lock_ttl_seconds: float = 600.0):
        self.backend = backend
        self.lock_ttl_seconds = lock_ttl_seconds

    @property
    def _meta(self) -> sa.Table:
        return self.backend.rt["meta"]

    def _get(self, key: str) -> str | None:
        m = self._meta
        with self.backend.tx("registry") as conn:
            row = conn.execute(sa.select(m.c.value).where(m.c.key == key)).first()
        return None if row is None else row[0]

    def _put(self, key: str, value: str, operation: str) -> None:
        m = self._meta
        with self.backend.tx(operation) as conn:
            conn.execute(m.delete().where(m.c.key == key))
            conn.execute(m.insert().values(key=key, value=value))

    @contextmanager
    def lock(self) -> Iterator[None]:
        m = self._meta
        now = self.backend.clock()
        token = json.dumps({"acquired_at": now})
        try:
            with self.backend.tx("lock") as conn:
                conn.execute(m.insert().values(key="write_lock", value=token))
        except BackendError as exc:
            if not isinstance(exc.__cause__, sa.exc.IntegrityError):
                raise
            with self.backend.tx("lock") as conn:
                row = conn.execute(sa.select(m.c.value).where(m.c.key == "write_lock")).first()
                if row is None or json.loads(row[0]).get("acquired_at", now) > now - self.lock_ttl_seconds:
                    raise BackendError(KIND, "lock", "write lock is held by another writer") from None
                taken = conn.execute(m.update().where(m.c.key == "write_lock", m.c.value == row[0]).values(value=token)).rowcount
                if taken != 1:  # pragma: no cover - lost a takeover race
                    raise BackendError(KIND, "lock", "write lock is held by another writer")
        try:
            yield
        finally:
            with self.backend.tx("unlock") as conn:
                conn.execute(m.delete().where(m.c.key == "write_lock", m.c.value == token))

    def current_generation(self) -> str | None:
        generation = self._get("CURRENT")
        if generation is None:
            return None
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
        raw = self._get(f"manifest:{generation}")
        return None if raw is None else json.loads(raw)

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        self._put(f"manifest:{manifest['generation']}", json.dumps(manifest, sort_keys=True, default=str), "write_manifest")

    def activate(self, manifest: dict[str, Any]) -> None:
        generation = manifest["generation"]
        if self.manifest(generation) is None:
            raise ValueError("cannot activate a generation without a written manifest")
        self._put("CURRENT", generation, "activate")

    def deactivate(self, generation: str) -> None:
        """Remove the pointer row only while it still names ``generation`` (one statement)."""
        m = self._meta
        with self.backend.tx("deactivate") as conn:
            conn.execute(m.delete().where(m.c.key == "CURRENT", m.c.value == generation))

    def _prefixed(self, prefix: str, operation: str) -> list[tuple[str, str]]:
        m = self._meta
        with self.backend.tx(operation) as conn:
            rows = conn.execute(sa.select(m.c.key, m.c.value).where(m.c.key.startswith(prefix, autoescape=True))).all()
        return sorted((r[0], r[1]) for r in rows)

    def generations(self) -> list[str]:
        return [key.split(":", 1)[1] for key, _ in self._prefixed("manifest:", "generations")]

    def record_failure(self, failure: dict[str, Any]) -> None:
        self._put(f"failure:{failure['generation']}", json.dumps(failure, sort_keys=True, default=str), "record_failure")

    def failures(self) -> list[dict[str, Any]]:
        return [json.loads(value) for _, value in self._prefixed("failure:", "failures")]
