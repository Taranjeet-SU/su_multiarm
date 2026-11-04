"""Cursor hygiene (addendum A1 §3.1.1): every scroll this process opens is registered, closed in a
``finally`` by the pull loop, and closed again (idempotently) from an ``atexit`` hook and, when the
CLI installs it, a ``SIGTERM``/``SIGINT`` handler. A context leaked by a hard kill expires on the
server because the keepalive is bounded by ``pull.keepalive_max``. A process holds at most
``max_concurrency × slices`` open contexts; opening another is refused before any I/O.
"""

from __future__ import annotations

import atexit
import signal
import threading
import weakref
from typing import Callable

from .contracts import SourceCursor
from .errors import SourceError, SourceRequestRefused

_REGISTRIES: "weakref.WeakSet[CursorRegistry]" = weakref.WeakSet()
_HOOKED = False
_LOCK = threading.Lock()


class CursorRegistry:
    """Open cursors of one source. ``closer`` sends the close request for one cursor."""

    def __init__(self, max_open: int, closer: Callable[[SourceCursor], None]):
        self.max_open = max_open
        self.closer = closer
        self._open: dict[str, SourceCursor] = {}
        self._lock = threading.Lock()
        _register_exit_hook(self)

    def reserve(self, path: str) -> None:
        """Called before a request that opens a context."""
        with self._lock:
            if len(self._open) >= self.max_open:
                raise SourceRequestRefused("POST", path, f"open cursor limit reached ({self.max_open} = max_concurrency x slices)")

    def track(self, cursor: SourceCursor) -> None:
        with self._lock:
            self._open[cursor.unit_id] = cursor

    def release(self, cursor: SourceCursor) -> None:
        with self._lock:
            self._open.pop(cursor.unit_id, None)

    def open_cursors(self) -> list[SourceCursor]:
        with self._lock:
            return list(self._open.values())

    def close_all(self) -> list[str]:
        """Close every open cursor; returns the unit ids whose close failed (best effort at exit)."""
        failed = []
        for cursor in self.open_cursors():
            try:
                self.closer(cursor)
            except SourceError:
                failed.append(cursor.unit_id)
            finally:
                self.release(cursor)
        return failed


def _close_everything() -> None:
    for registry in list(_REGISTRIES):
        registry.close_all()


def _register_exit_hook(registry: CursorRegistry) -> None:
    global _HOOKED
    with _LOCK:
        _REGISTRIES.add(registry)
        if not _HOOKED:
            atexit.register(_close_everything)
            _HOOKED = True


def install_signal_handlers() -> None:
    """Close open cursors on SIGTERM/SIGINT, then exit with the conventional status. Only from the
    main thread (the CLI); library callers keep their own handlers."""
    if threading.current_thread() is not threading.main_thread():
        return

    def handler(signum, frame):  # pragma: no cover - exercised by the CLI process test
        _close_everything()
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handler)
