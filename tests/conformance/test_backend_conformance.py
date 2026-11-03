"""Conformance kit v0 over the in-memory fake, the sqlite adapter and (``external``) OpenSearch (plan §3.5, §6.1).

Each adapter is checked for exactly the capabilities it declares in ``backends/capabilities.py``;
a check for an undeclared capability is skipped, never silently passed.
"""

from __future__ import annotations

import pytest

from su_multi_arm.backends import capabilities as caps
from su_multi_arm.testing import backend_conformance as kit


def declared(adapter) -> set[str]:
    return set(caps.adapter(adapter.kind).capabilities)


def test_every_declared_capability_is_implemented(adapter_factory):
    adapter = adapter_factory()
    assert kit.check_protocols(adapter) == declared(adapter)
    if adapter.kind in {"memory", "sqlite"}:
        assert declared(adapter) == set(caps.CAPABILITIES)


@pytest.mark.parametrize("capability", sorted(kit.CHECKS))
def test_capability(adapter_factory, capability):
    adapter = adapter_factory()
    if capability not in declared(adapter):
        pytest.skip(f"{adapter.kind} does not declare {capability}")
    kit.CHECKS[capability](adapter)


def test_cache(adapter_factory):
    adapter = adapter_factory()
    if "cache" not in declared(adapter):
        pytest.skip(f"{adapter.kind} does not declare cache")
    kit.check_cache(adapter, adapter_factory.clock)


def test_run_all(adapter_factory):
    results = kit.run_all(adapter_factory, adapter_factory.clock)
    assert results == {c: "passed" for c in declared(adapter_factory())}


def test_sqlite_generation_isolation_and_listing(tmp_path):
    from su_multi_arm.backends.sqlite import SqliteBackend
    from su_multi_arm.core.errors import BackendError

    backend = SqliteBackend(tmp_path / "s")
    kit.seed(backend, kit.GEN)
    kit.seed(backend, kit.GEN2)
    backend.catalog.put_documents(kit.GEN2, [kit.document(7)], [])
    assert backend.catalog.count_documents(kit.GEN) == 4 and backend.catalog.count_documents(kit.GEN2) == 5
    assert (backend.root / "snapshots" / kit.GEN.generation / "catalog.sqlite").exists()
    backend.catalog.delete_generation(kit.GEN)
    assert backend.catalog.count_documents(kit.GEN2) == 5
    with pytest.raises(BackendError, match="unknown generation"):
        backend.catalog.count_documents(kit.GEN)
