"""Generation registry for the postgres backend: pointer row, manifests, failures, write lock.

Mirrors ``ingestion/release.py::FileGenerationRegistry`` on the runtime ``meta`` table:
``(key='CURRENT', value=<generation>)`` is the pointer, replaced by a single-row upsert (atomic) and
only by ``activate``; ``record_failure`` writes ``failure:<generation>`` and never touches the
pointer. ``lock()`` takes a session-level ``pg_try_advisory_lock`` on a dedicated connection: a held
lock is a typed error, never a wait, and a crashed writer's lock is released with its connection.
Old generations stay until ``delete_generation`` removes them.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator

from ...core.errors import BackendError
from ._db import KIND

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import PostgresBackend

HEX32 = set("0123456789abcdef")


class PostgresGenerationRegistry:
    def __init__(self, backend: PostgresBackend):
        self.backend = backend

    def _get(self, key: str) -> Any:
        with self.backend.tx("registry") as cur:
            cur.execute(f"SELECT value FROM {self.backend.table('meta')} WHERE key = %s", (key,))
            row = cur.fetchone()
        return None if row is None else row[0]

    def _put(self, key: str, value: str, operation: str) -> None:
        with self.backend.tx(operation) as cur:
            cur.execute(
                f"INSERT INTO {self.backend.table('meta')}(key, value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                (key, value),
            )

    @contextmanager
    def lock(self) -> Iterator[None]:
        backend = self.backend
        with backend.tx("lock"):
            pass  # runtime schema exists before the lock is keyed on it
        errors = backend._errors
        try:
            conn = backend.new_connection()
            row = conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (f"su_multi_arm:{backend.prefix}:write",)).fetchone()
        except errors as exc:
            raise BackendError(KIND, "lock", f"{type(exc).__name__}: {exc}") from exc
        if not row or not row[0]:
            conn.close()
            raise BackendError(KIND, "lock", "write lock is held by another writer")
        try:
            yield
        finally:
            try:
                conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"su_multi_arm:{backend.prefix}:write",))
            except errors:  # pragma: no cover - closing the connection releases the lock anyway
                pass
            finally:
                conn.close()

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
        with self.backend.tx("deactivate") as cur:
            cur.execute(f"DELETE FROM {self.backend.table('meta')} WHERE key = 'CURRENT' AND value = %s", (generation,))

    def generations(self) -> list[str]:
        with self.backend.tx("generations") as cur:
            cur.execute(f"SELECT key FROM {self.backend.table('meta')} WHERE left(key, 9) = 'manifest:' ORDER BY key")
            return [r[0].split(":", 1)[1] for r in cur.fetchall()]

    def record_failure(self, failure: dict[str, Any]) -> None:
        self._put(f"failure:{failure['generation']}", json.dumps(failure, sort_keys=True, default=str), "record_failure")

    def failures(self) -> list[dict[str, Any]]:
        with self.backend.tx("failures") as cur:
            cur.execute(f"SELECT value FROM {self.backend.table('meta')} WHERE left(key, 8) = 'failure:' ORDER BY key")
            return [json.loads(r[0]) for r in cur.fetchall()]
