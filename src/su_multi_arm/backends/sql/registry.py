"""Generation registry for the generic SQL backend: pointer row, manifests, failures, write lease.

Mirrors ``ingestion/release.py::FileGenerationRegistry`` on ``<prefix>_meta``: the ``CURRENT`` row is
the pointer, replaced inside one transaction (delete + insert, atomic for readers) and only by
``activate``; with ``expected`` the switch is a compare-and-set (``UPDATE ... WHERE value =
expected``, or an ``INSERT`` that conflicts when a pointer already exists). ``record_failure``
writes ``failure:<generation>`` and never touches the pointer. ``lock()`` takes the write lease
(``ingestion/lease.py``): a ``write_lock`` row holding ``{owner, expires_at}``; a primary-key
conflict with a live lease is a typed ``WriteConflict(held)``, never a wait. Takeover of an expired
lease, renewal and release are conditional on the row's exact previous value, so only the owner
renews or deletes it.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator

import sqlalchemy as sa

from ...core.errors import BackendError
from ...ingestion.lease import LEASE_TTL_SECONDS, hold, stale_base
from ...ingestion.release import ANY
from ._db import KIND

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import SqlBackend

HEX32 = set("0123456789abcdef")


class SqlGenerationRegistry:
    def __init__(self, backend: SqlBackend, lock_ttl_seconds: float = LEASE_TTL_SECONDS):
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

    @staticmethod
    def _expires(raw: str, ttl: float) -> float:
        """Expiry of a stored lease; a row written before owner tokens (``acquired_at`` only)
        expires ``ttl`` after it was taken; an unreadable row never expires by itself."""
        try:
            value = json.loads(raw)
        except ValueError:
            return float("inf")
        if "expires_at" in value:
            return float(value["expires_at"])
        return float(value.get("acquired_at", float("inf"))) + ttl

    def lease_acquire(self, token: str, ttl: float) -> bool:
        m = self._meta
        now = self.backend.clock()
        lease = json.dumps({"owner": token, "expires_at": now + ttl})
        try:
            with self.backend.tx("lock") as conn:
                conn.execute(m.insert().values(key="write_lock", value=lease))
            return True
        except BackendError as exc:
            if not isinstance(exc.__cause__, sa.exc.IntegrityError):
                raise
        with self.backend.tx("lock") as conn:
            row = conn.execute(sa.select(m.c.value).where(m.c.key == "write_lock")).first()
            if row is None or self._expires(row[0], ttl) > now:
                return False
            # stale takeover: only while the row still holds the expired value just read
            return conn.execute(m.update().where(m.c.key == "write_lock", m.c.value == row[0]).values(value=lease)).rowcount == 1

    def _owned(self, conn: Any, token: str) -> str | None:
        m = self._meta
        row = conn.execute(sa.select(m.c.value).where(m.c.key == "write_lock")).first()
        if row is None:
            return None
        try:
            owner = json.loads(row[0]).get("owner")
        except ValueError:
            return None
        return row[0] if owner == token else None

    def lease_renew(self, token: str, ttl: float) -> bool:
        m = self._meta
        with self.backend.tx("renew") as conn:
            raw = self._owned(conn, token)
            if raw is None:
                return False
            lease = json.dumps({"owner": token, "expires_at": self.backend.clock() + ttl})
            return conn.execute(m.update().where(m.c.key == "write_lock", m.c.value == raw).values(value=lease)).rowcount == 1

    def lease_release(self, token: str) -> bool:
        m = self._meta
        with self.backend.tx("unlock") as conn:
            raw = self._owned(conn, token)
            if raw is None:
                return False
            return conn.execute(m.delete().where(m.c.key == "write_lock", m.c.value == raw)).rowcount == 1

    @contextmanager
    def lock(self) -> Iterator[Any]:
        with hold(self, KIND, ttl=self.lock_ttl_seconds) as lease:
            yield lease

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

    def activate(self, manifest: dict[str, Any], *, expected: str | None = ANY) -> None:
        generation = manifest["generation"]
        if self.manifest(generation) is None:
            raise ValueError("cannot activate a generation without a written manifest")
        if expected is ANY:
            self._put("CURRENT", generation, "activate")
            return
        m = self._meta
        if expected is None:
            try:
                with self.backend.tx("activate") as conn:
                    conn.execute(m.insert().values(key="CURRENT", value=generation))
                return
            except BackendError as exc:
                if not isinstance(exc.__cause__, sa.exc.IntegrityError):
                    raise
        else:
            with self.backend.tx("activate") as conn:
                if conn.execute(m.update().where(m.c.key == "CURRENT", m.c.value == expected).values(value=generation)).rowcount == 1:
                    return
        raise stale_base(KIND, self.current_generation(), expected)

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
