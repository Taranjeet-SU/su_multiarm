"""Startup warm-up state shared by the serving front ends (``serving/http.py`` ``/ready`` and the
legacy adapter's ``/ready``, ``compat/legacy_qa/app.py``).

A ``WarmUp`` runs one callable once (in a background thread at startup) and reports its state:
``not_run`` -> ``running`` -> ``done`` | ``failed``; a run still going after ``timeout_seconds``
is reported ``timed_out`` (with the seconds elapsed) until it ends. Readiness is granted only on
``done``: every other state is a readiness problem with a clear text (``problem``; a failure
names the exception class and adapter/operation, the message goes to the server log only, as
``/ready`` is public). Nothing is
retried and a failure is never turned into success; a late finish after a time-out reports ``done``
with ``late: true`` (the warm state is real, only the bound was missed).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 300.0


class WarmUp:
    def __init__(self, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS, *, clock: Callable[[], float] = time.monotonic, label: str = "warm-up"):
        if timeout_seconds <= 0:
            raise ValueError("warm-up timeout_seconds must be > 0")
        self.timeout_seconds = float(timeout_seconds)
        self.clock = clock
        self.label = label
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"status": "not_run"}
        self._started: float | None = None

    def run(self, func: Callable[[], dict[str, Any] | None]) -> dict[str, Any]:
        """Run ``func`` now (in the calling thread) and record its outcome; returns the state."""
        with self._lock:
            if self._started is not None:
                raise RuntimeError(f"{self.label} already started")
            self._started = self.clock()
            self._state = {"status": "running"}
        try:
            report = func() or {}
        except Exception as exc:  # noqa: BLE001 - reported as a readiness problem, never swallowed
            elapsed = self.clock() - self._started
            LOGGER.warning("%s failed after %.1f s: %s: %s", self.label, elapsed, type(exc).__name__, str(exc)[:300])
            failed: dict[str, Any] = {"status": "failed", "error": type(exc).__name__, "ms": int(round(elapsed * 1000))}
            for key in ("adapter", "operation"):
                if getattr(exc, key, None):
                    failed[key] = str(getattr(exc, key))
            with self._lock:
                self._state = failed
            return self.state()
        elapsed = self.clock() - self._started
        with self._lock:
            self._state = {"status": "done", "ms": int(round(elapsed * 1000)), **report}
            if elapsed > self.timeout_seconds:
                self._state["late"] = True
        return self.state()

    def start(self, func: Callable[[], dict[str, Any] | None], name: str = "warm-up") -> threading.Thread:
        """Run ``func`` in a daemon thread (startup); the state is ``running`` from now on."""
        with self._lock:
            if self._started is None:
                self._state = {"status": "running"}
        thread = threading.Thread(target=self.run, args=(func,), name=name, daemon=True)
        thread.start()
        return thread

    def state(self) -> dict[str, Any]:
        """The current state; a run past its bound is ``timed_out`` (``seconds``: elapsed)."""
        with self._lock:
            state = dict(self._state)
            started = self._started
        if state["status"] == "running" and started is not None:
            elapsed = self.clock() - started
            if elapsed > self.timeout_seconds:
                return {"status": "timed_out", "seconds": round(elapsed, 1), "timeout_seconds": self.timeout_seconds}
        return state

    def problem(self) -> str | None:
        """The readiness problem of the current state (``None`` when done)."""
        state = self.state()
        status = state["status"]
        if status == "done":
            return None
        if status == "failed":
            where = ".".join(state[k] for k in ("adapter", "operation") if state.get(k))
            return f"{self.label} failed ({state['error']}{' in ' + where if where else ''}; see the server log)"
        if status == "timed_out":
            return f"{self.label} timed out after {state['seconds']} s (bound {self.timeout_seconds:g} s; still running)"
        return f"{self.label} {status}"
