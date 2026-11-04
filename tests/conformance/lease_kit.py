"""Write-lease and compare-and-set checks shared by every generation registry (risk R1).

``check_lease`` and ``check_compare_and_set`` take registry factories, so the same assertions run on
the memory/file registries (unit), the generic SQL registry on SQLite (every push) and the
PostgreSQL, MongoDB and Elasticsearch registries against real servers (``external``).
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from su_multi_arm.core.errors import BackendError, WriteConflict


def check_lease(make_registry: Callable[[], Any], advance: Callable[[float], None] | None, ttl: float) -> None:
    """A live lease refuses a second writer (typed, never a wait); a release or renewal by a token
    that does not own the lease changes nothing; with ``advance`` (leases that expire) the lease is
    taken over only after ``ttl``, the old owner then learns it lost the lease and its release does
    not delete the new holder's lease."""
    first, second = make_registry(), make_registry()
    with first.lock() as lease_a:
        assert lease_a.token
        with pytest.raises(WriteConflict) as info:
            with second.lock():
                pass
        assert info.value.reason == "held" and isinstance(info.value, BackendError)
        assert second.lease_release("not-the-owner") is False
        assert second.lease_renew("not-the-owner", ttl) is False
        lease_a.check()  # still the owner after the refused release
        if advance is None:
            return
        advance(ttl / 2)
        lease_a.check()  # renewed: the expiry moved forward
        advance(ttl / 2 + 1)
        with pytest.raises(WriteConflict):
            with second.lock():
                pass  # renewed half a ttl ago: not expired yet
        advance(ttl)
        with second.lock() as lease_b:  # stale takeover after a whole ttl without renewal
            with pytest.raises(WriteConflict) as info:
                lease_a.check()
            assert info.value.reason == "lost"
            assert first.lease_release(lease_a.token) is False
            lease_b.check()  # the new holder kept its lease
    assert lease_a.released is False
    with first.lock():  # B released its own lease on exit
        pass


def check_compare_and_set(registry: Any, manifests: list[dict[str, Any]]) -> None:
    """``activate(expected=...)`` switches only from the expected generation (``None`` = no pointer)
    and otherwise raises ``WriteConflict(stale_base)`` with the pointer unchanged."""
    m1, m2, m3 = manifests
    for manifest in manifests:
        registry.write_manifest(manifest)
    registry.activate(m1, expected=None)
    assert registry.current_generation() == m1["generation"]
    with pytest.raises(WriteConflict) as info:
        registry.activate(m2, expected=None)  # a second "first" release
    assert info.value.reason == "stale_base" and registry.current_generation() == m1["generation"]
    registry.activate(m2, expected=m1["generation"])
    assert registry.current_generation() == m2["generation"]
    with pytest.raises(WriteConflict, match="stale|started from"):
        registry.activate(m3, expected=m1["generation"])  # built on m1, but m2 is CURRENT now
    assert registry.current_generation() == m2["generation"]
