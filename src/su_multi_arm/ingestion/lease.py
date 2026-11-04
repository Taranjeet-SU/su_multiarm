"""Write leases: one owner per dataset write path, renewed while a build runs (plan §3.14, risk R1).

A lease has an **owner token** (random, per acquisition). Every generation registry implements the
three primitive operations of ``LeaseStore`` atomically on its own storage (a lock row/document with
a compare-and-set on the owner, ``flock`` on a file, a PostgreSQL session advisory lock):

* ``lease_acquire(token, ttl)`` takes a free lease or one whose expiry has passed (stale takeover)
  and returns ``False`` while another owner's lease is live — a typed ``WriteConflict(held)``,
  never a wait;
* ``lease_renew(token, ttl)`` pushes the expiry forward **only while the token still owns the
  lease** and returns ``False`` otherwise;
* ``lease_release(token)`` removes the lease **only while the token owns it** (a writer whose lease
  was taken over never deletes the new holder's lease) and returns whether it did.

``Lease`` renews from a daemon heartbeat thread every ``ttl / RENEWALS_PER_TTL`` seconds, so a lease
expires only after ``RENEWALS_PER_TTL`` consecutive missed renewals. A renewal that reports another
owner, or no successful renewal for a whole ``ttl``, marks the lease **lost**; ``check()`` then
raises ``WriteConflict(lost)``. The write path checks the lease before activation, and activation
itself is a compare-and-set on the generation the build started from (``release.ANY`` is only for
direct registry use), so a writer that lost its lease, or whose base generation is no longer
``CURRENT``, fails typed and never switches the pointer.
"""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from typing import Iterator, Protocol, runtime_checkable

from ..core.errors import WriteConflict

LEASE_TTL_SECONDS = 120.0
RENEWALS_PER_TTL = 4


@runtime_checkable
class LeaseStore(Protocol):
    def lease_acquire(self, token: str, ttl: float) -> bool: ...

    def lease_renew(self, token: str, ttl: float) -> bool: ...

    def lease_release(self, token: str) -> bool: ...


class Lease:
    """One acquisition of a registry's write lease (see the module docstring)."""

    def __init__(self, store: LeaseStore, adapter: str, *, ttl: float = LEASE_TTL_SECONDS, held_message: str = "write lock is held by another writer", heartbeat: bool = True):
        if ttl <= 0:
            raise ValueError("lease ttl must be positive")
        self.store, self.adapter, self.ttl, self.held_message = store, adapter, float(ttl), held_message
        self.token = uuid.uuid4().hex
        self.renew_every = self.ttl / RENEWALS_PER_TTL
        self.heartbeat = heartbeat
        self.renewals = 0
        self.missed = 0
        self.lost: str | None = None
        self.released: bool | None = None
        self._last_renewed = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def acquire(self) -> "Lease":
        if not self.store.lease_acquire(self.token, self.ttl):
            raise WriteConflict(self.adapter, "lock", self.held_message, "held")
        self._last_renewed = time.monotonic()
        if self.heartbeat:
            self._thread = threading.Thread(target=self._beat, name=f"su-lease-{self.token[:8]}", daemon=True)
            self._thread.start()
        return self

    def _beat(self) -> None:
        while not self._stop.wait(self.renew_every):
            if not self.renew():
                return

    def renew(self) -> bool:
        """One renewal. ``False`` once the lease is lost (reported by ``check``)."""
        with self._lock:
            if self.lost is not None:
                return False
            try:
                owned = self.store.lease_renew(self.token, self.ttl)
            except Exception as exc:  # a missed renewal; lost only when a whole ttl passes without one
                self.missed += 1
                if time.monotonic() - self._last_renewed >= self.ttl:
                    self.lost = f"no successful renewal for {self.ttl:g}s ({type(exc).__name__}: {exc})"
                    return False
                return True
            if not owned:
                self.lost = "the lease expired and was taken over by another writer"
                return False
            self.renewals += 1
            self._last_renewed = time.monotonic()
            return True

    def check(self, operation: str = "activate") -> None:
        """One synchronous renewal, then ``WriteConflict(lost)`` when the lease is no longer this
        writer's (taken over, or no successful renewal for a whole ``ttl``)."""
        self.renew()
        with self._lock:
            if self.lost is None and time.monotonic() - self._last_renewed >= self.ttl:
                self.lost = f"no successful renewal for {self.ttl:g}s"
            lost = self.lost
        if lost is not None:
            raise WriteConflict(self.adapter, operation, f"write lease lost: {lost}", "lost")

    def release(self) -> bool:
        """Stop the heartbeat and remove the lease only while this token owns it."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(1.0, self.renew_every))
        self.released = bool(self.store.lease_release(self.token))
        return self.released

    def state(self) -> dict[str, object]:
        return {"token": self.token[:8], "ttl_seconds": self.ttl, "renewals": self.renewals, "missed_renewals": self.missed, "lost": self.lost}


@contextmanager
def hold(store: LeaseStore, adapter: str, *, ttl: float = LEASE_TTL_SECONDS, held_message: str = "write lock is held by another writer", heartbeat: bool = True) -> Iterator[Lease]:
    """Acquire, heartbeat and release (owner-only) one lease around a block."""
    lease = Lease(store, adapter, ttl=ttl, held_message=held_message, heartbeat=heartbeat).acquire()
    try:
        yield lease
    finally:
        lease.release()


def stale_base(adapter: str, actual: str | None, expected: str | None) -> WriteConflict:
    return WriteConflict(adapter, "activate", f"CURRENT names {actual or 'nothing'}, the build started from {expected or 'nothing'}; pointer unchanged", "stale_base")
