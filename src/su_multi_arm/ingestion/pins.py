"""Cross-process generation pins (addendum A1 §3.8.4; completeness review F9).

A pin says "a live reader still serves this generation". It is a row ``pin:<generation>:<holder>``
with an expiry on the clock of the generation registry's own store, so a separate process running
retention sees it:

=============================== ================================================================
registry                        pin storage
=============================== ================================================================
``FileGenerationRegistry``      sqlite sidecar ``<root>/pins.sqlite`` (sqlite backend)
``MemoryGenerationRegistry``    in-process dict (tests)
``SqlGenerationRegistry``       ``<prefix>_meta`` rows ``pin:<generation>:<holder>``
``PostgresGenerationRegistry``  ``<prefix>.meta`` rows ``pin:<generation>:<holder>``
MongoDB, Elasticsearch          ``planned``: ``pin_registry`` raises ``PinsUnsupported`` and
                                config validation refuses ``logs:`` there
=============================== ================================================================

``gc`` asks ``pinned(generation)`` before it deletes anything and raises ``GenerationPinned`` with
the live holders. Pins expire by TTL, so a crashed reader never blocks retention for longer than
the TTL. ``ProcessPins`` keeps **one** registry pin per (serving process, generation), renewed by a
heartbeat, with an in-process refcount of the requests and session turns that use it; registry
writes therefore scale with heartbeats, not requests (n9). The in-process ``protect()`` of the
backends stays the fast path inside one process.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol, runtime_checkable

from ..core.errors import ConfigError, SuError
from ..models import StrictModel


class PinToken(StrictModel):
    generation: str
    holder: str
    expires_at: float


class GenerationPinned(SuError):
    """``gc`` refused: live pins hold the generation."""

    def __init__(self, generation: str, holders: list[str]):
        self.generation, self.holders = generation, holders
        super().__init__(f"generation {generation} is pinned by {holders}")


class PinsUnsupported(ConfigError):
    """The generation registry has no pin storage (``planned``); never a silent no-op."""


@runtime_checkable
class PinRegistry(Protocol):
    def pin(self, generation: str, holder: str, ttl: float) -> PinToken: ...

    def renew(self, token: PinToken, ttl: float) -> PinToken: ...

    def release(self, token: PinToken) -> None: ...

    def pinned(self, generation: str, now: float | None = None) -> list[PinToken]: ...


class _RowPins:
    """Pins as key/value rows ``pin:<generation>:<holder>`` -> ``{"expires_at": t}`` on a store with
    ``put``, ``delete`` and ``prefixed``; subclasses supply the three."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock

    @staticmethod
    def _key(generation: str, holder: str) -> str:
        if ":" in generation or not holder:
            raise ValueError("pin generation must not contain ':' and the holder must be set")
        return f"pin:{generation}:{holder}"

    def _put(self, key: str, value: str) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _delete(self, key: str) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _prefixed(self, prefix: str) -> list[tuple[str, str]]:  # pragma: no cover - abstract
        raise NotImplementedError

    def pin(self, generation: str, holder: str, ttl: float) -> PinToken:
        if ttl <= 0:
            raise ValueError("pin ttl must be positive")
        token = PinToken(generation=generation, holder=holder, expires_at=self.clock() + ttl)
        self._put(self._key(generation, holder), json.dumps({"expires_at": token.expires_at}))
        return token

    def renew(self, token: PinToken, ttl: float) -> PinToken:
        return self.pin(token.generation, token.holder, ttl)

    def release(self, token: PinToken) -> None:
        self._delete(self._key(token.generation, token.holder))

    def pinned(self, generation: str, now: float | None = None) -> list[PinToken]:
        now = self.clock() if now is None else now
        live = []
        for key, value in self._prefixed(f"pin:{generation}:"):
            try:
                expires_at = float(json.loads(value)["expires_at"])
            except (ValueError, KeyError, TypeError):
                expires_at = float("inf")  # an unreadable pin is treated as live, never ignored
            if expires_at > now:
                live.append(PinToken(generation=generation, holder=key.split(":", 2)[2], expires_at=expires_at))
        return sorted(live, key=lambda t: t.holder)

    def sweep(self, now: float | None = None) -> int:
        """Remove expired pin rows (housekeeping; ``pinned`` ignores them anyway)."""
        now = self.clock() if now is None else now
        removed = 0
        for key, value in self._prefixed("pin:"):
            try:
                if float(json.loads(value)["expires_at"]) <= now:
                    self._delete(key)
                    removed += 1
            except (ValueError, KeyError, TypeError):
                continue
        return removed


class MemoryPins(_RowPins):
    def __init__(self, clock: Callable[[], float] = time.time):
        super().__init__(clock)
        self._rows: dict[str, str] = {}
        self._lock = threading.Lock()

    def _put(self, key: str, value: str) -> None:
        with self._lock:
            self._rows[key] = value

    def _delete(self, key: str) -> None:
        with self._lock:
            self._rows.pop(key, None)

    def _prefixed(self, prefix: str) -> list[tuple[str, str]]:
        with self._lock:
            return sorted((k, v) for k, v in self._rows.items() if k.startswith(prefix))


class SqlitePins(_RowPins):
    """The file registry's sidecar: ``<root>/pins.sqlite`` (one table, WAL, short transactions)."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time, timeout: float = 30.0):
        super().__init__(clock)
        self.path, self.timeout = Path(path), timeout

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=self.timeout)) as db:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS pins(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                yield db

    def _put(self, key: str, value: str) -> None:
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO pins VALUES (?,?)", (key, value))

    def _delete(self, key: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM pins WHERE key=?", (key,))

    def _prefixed(self, prefix: str) -> list[tuple[str, str]]:
        with self._db() as db:
            return [(r[0], r[1]) for r in db.execute("SELECT key, value FROM pins WHERE substr(key, 1, ?)=? ORDER BY key", (len(prefix), prefix))]


class SqlMetaPins(_RowPins):
    """Pins as rows of the generic SQL registry's ``<prefix>_meta`` table."""

    def __init__(self, registry: Any, clock: Callable[[], float] = time.time):
        super().__init__(clock)
        self.registry = registry

    def _put(self, key: str, value: str) -> None:
        self.registry._put(key, value, "pin")

    def _delete(self, key: str) -> None:
        m = self.registry._meta
        with self.registry.backend.tx("unpin") as conn:
            conn.execute(m.delete().where(m.c.key == key))

    def _prefixed(self, prefix: str) -> list[tuple[str, str]]:
        return self.registry._prefixed(prefix, "pinned")


class PostgresMetaPins(_RowPins):
    """Pins as rows of the PostgreSQL registry's ``<prefix>.meta`` table."""

    def __init__(self, registry: Any, clock: Callable[[], float] = time.time):
        super().__init__(clock)
        self.registry = registry

    def _put(self, key: str, value: str) -> None:
        self.registry._put(key, value, "pin")

    def _delete(self, key: str) -> None:
        backend = self.registry.backend
        with backend.tx("unpin") as cur:
            cur.execute(f"DELETE FROM {backend.table('meta')} WHERE key = %s", (key,))

    def _prefixed(self, prefix: str) -> list[tuple[str, str]]:
        backend = self.registry.backend
        with backend.tx("pinned") as cur:
            cur.execute(f"SELECT key, value FROM {backend.table('meta')} WHERE left(key, %s) = %s ORDER BY key", (len(prefix), prefix))
            return [(r[0], r[1]) for r in cur.fetchall()]


def pin_registry(registry: Any, clock: Callable[[], float] | None = None) -> _RowPins:
    """The pin registry stored next to a generation registry (one instance per registry object)."""
    existing = getattr(registry, "_su_pins", None)
    if existing is not None:
        return existing
    from .release import FileGenerationRegistry, MemoryGenerationRegistry

    clock = clock or time.time
    name = type(registry).__name__
    if isinstance(registry, FileGenerationRegistry):
        pins: _RowPins = SqlitePins(registry.root / "pins.sqlite", clock)
    elif isinstance(registry, MemoryGenerationRegistry):
        pins = MemoryPins(registry.clock if clock is time.time else clock)
    elif name == "SqlGenerationRegistry":
        pins = SqlMetaPins(registry, clock)
    elif name == "PostgresGenerationRegistry":
        pins = PostgresMetaPins(registry, clock)
    else:
        raise PinsUnsupported(f"generation registry {name} has no pin storage (planned); retention cannot honour cross-process pins there", "storage.catalog")
    registry._su_pins = pins
    return pins


def holder_id() -> str:
    """``<host>:<pid>:<random>``: unique per serving process."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class ProcessPins:
    """One registry pin per (this process, generation), refcounted in process and renewed by a
    heartbeat (n9). ``acquire(gen)`` before serving a request or session turn from ``gen``,
    ``release(gen)`` after; the registry pin is dropped once the refcount is 0 **and** ``gen`` is no
    longer current (``current()``), so the active generation stays pinned between requests."""

    def __init__(
        self,
        pins: PinRegistry,
        *,
        ttl: float = 120.0,
        heartbeat: float = 30.0,
        current: Callable[[], str | None] = lambda: None,
        holder: str | None = None,
        start: bool = False,
    ):
        if heartbeat * 2 > ttl:
            raise ValueError("heartbeat must be at most half of the pin ttl")
        self.pins, self.ttl, self.heartbeat, self.current = pins, ttl, heartbeat, current
        self.holder = holder or holder_id()
        self.refcount: dict[str, int] = {}
        self.tokens: dict[str, PinToken] = {}
        self.writes = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if start:
            self.start()

    @classmethod
    def for_config(cls, config: Any, registry: Any, current: Callable[[], str | None], **kwargs: Any) -> "ProcessPins":
        """The serving process's pins for ``config.logs.pins`` (``ttl_seconds``, ``heartbeat_seconds``)."""
        settings = config.logs.pins
        return cls(pin_registry(registry), ttl=settings.ttl_seconds, heartbeat=settings.heartbeat_seconds, current=current, **kwargs)

    def acquire(self, generation: str) -> None:
        with self._lock:
            self.refcount[generation] = self.refcount.get(generation, 0) + 1
            if generation not in self.tokens:
                self.tokens[generation] = self.pins.pin(generation, self.holder, self.ttl)
                self.writes += 1

    def release(self, generation: str) -> None:
        with self._lock:
            count = self.refcount.get(generation, 0) - 1
            if count < 0:
                raise ValueError(f"release without acquire for {generation}")
            self.refcount[generation] = count
            self._drop_idle()

    def _drop_idle(self) -> None:
        current = self.current()
        for generation in [g for g, n in self.refcount.items() if n == 0 and g != current]:
            token = self.tokens.pop(generation, None)
            self.refcount.pop(generation, None)
            if token is not None:
                self.pins.release(token)
                self.writes += 1

    def beat(self) -> None:
        """One heartbeat: renew every held pin, drop idle non-current ones."""
        with self._lock:
            self._drop_idle()
            for generation, token in list(self.tokens.items()):
                self.tokens[generation] = self.pins.renew(token, self.ttl)
                self.writes += 1

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="su-pins", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.heartbeat):
            try:
                self.beat()
            except Exception:  # a missed beat; the pin expires by TTL if beats keep failing
                continue

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.heartbeat)
        with self._lock:
            for token in self.tokens.values():
                self.pins.release(token)
            self.tokens.clear()
            self.refcount.clear()
