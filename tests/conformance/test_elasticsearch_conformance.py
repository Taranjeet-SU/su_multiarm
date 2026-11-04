"""Elasticsearch behaviour the shared conformance kit does not cover (``external``; plan §3.5, §6.1).

The kit itself runs over ``elasticsearch`` / ``elasticsearch_hnsw`` through ``conftest.py``. This
module adds: the server probe, generation isolation + listing + delete, the alias-backed generation
registry (activation, failed build leaving the alias, exclusive lock) and exact-vector parity with
the memory fake. Reads ``SU_TEST_ELASTICSEARCH_URL`` (default ``http://127.0.0.1:19201``): unset and
unreachable skips, set and unreachable fails. Every test uses its own index prefix and deletes
exactly the indices it created."""

from __future__ import annotations

import os
import secrets

import numpy as np
import pytest

from su_multi_arm.backends import capabilities as caps
from su_multi_arm.backends.elasticsearch import ElasticsearchBackend
from su_multi_arm.core.errors import BackendError
from su_multi_arm.testing import backend_conformance as kit

pytestmark = pytest.mark.external
if not os.environ.get("SU_TEST_ELASTICSEARCH_URL"):
    pytest.importorskip("elasticsearch")  # without the extra and without a configured server: skip; set: fail

URL = os.environ.get("SU_TEST_ELASTICSEARCH_URL") or "http://127.0.0.1:19201"
DECLARED = {"catalog", "lexical", "vector", "records"}


def delete_prefix(backend: ElasticsearchBackend) -> None:
    """Test-side cleanup of this test's own prefix (the adapter itself only deletes generations)."""
    rows = backend.transport.request("GET", f"/_cat/indices/{backend.prefix}_*", params={"format": "json", "h": "index"})
    names = [row["index"] for row in rows or []]
    if names:
        backend.transport.request("DELETE", "/" + ",".join(names))


@pytest.fixture(scope="module")
def server():
    probe = ElasticsearchBackend(URL, timeout_seconds=5)
    try:
        return probe.probe()
    except BackendError as exc:
        if os.environ.get("SU_TEST_ELASTICSEARCH_URL"):
            raise
        pytest.skip(f"no Elasticsearch at {URL} ({exc}); set SU_TEST_ELASTICSEARCH_URL to require one")
    finally:
        probe.close()


@pytest.fixture
def es_factory(server):
    prefix = f"su_test_{secrets.token_hex(4)}"
    clock = {"now": 100.0}
    created: list[ElasticsearchBackend] = []

    def make(**kwargs):
        backend = ElasticsearchBackend(URL, index_prefix=prefix, dataset="conformance", clock=lambda: clock["now"], **kwargs)
        created.append(backend)
        return backend

    make.clock = clock
    make.prefix = prefix
    yield make
    janitor = created[0] if created else make()
    delete_prefix(janitor)
    for backend in created:
        backend.close()


def test_server_is_elasticsearch_8_and_declared_capabilities_are_implemented(server, es_factory):
    assert server["distribution"] == "elasticsearch" and server["version"].startswith("8.")
    assert kit.check_protocols(es_factory()) == DECLARED
    assert {c for c, d in caps.MATRIX["elasticsearch"].capabilities.items() if d.status == "contracts"} == DECLARED


def test_generation_isolation_listing_and_delete(es_factory):
    backend = es_factory()
    kit.seed(backend, kit.GEN)
    kit.seed(backend, kit.GEN2)
    backend.catalog.put_documents(kit.GEN2, [kit.document(7)], [])
    assert backend.catalog.count_documents(kit.GEN) == 4 and backend.catalog.count_documents(kit.GEN2) == 5
    assert backend.generations() == [f"conformance/{kit.GEN.generation}/", f"conformance/{kit.GEN2.generation}/"]
    backend.catalog.delete_generation(kit.GEN)
    assert backend.catalog.count_documents(kit.GEN2) == 5 and backend.generations() == [f"conformance/{kit.GEN2.generation}/"]
    with pytest.raises(BackendError, match="elasticsearch.count_documents: unknown generation"):
        backend.catalog.count_documents(kit.GEN)


def test_registry_alias_switch_and_failed_build_leaves_the_live_alias(es_factory):
    backend = es_factory()
    registry = backend.generation_registry()
    assert registry.current() is None and registry.generations() == [] and registry.failures() == []
    kit.seed(backend, kit.GEN)
    manifest_a = {"generation": kit.GEN.generation, "dataset": "conformance", "index_format": 3, "fingerprint": kit.GEN.fingerprint}
    with registry.lock():
        registry.write_manifest(manifest_a)
        registry.activate(manifest_a)
    assert registry.current() == manifest_a and registry.generations() == [kit.GEN.generation]
    assert backend.active_generation("conformance") == kit.GEN.generation
    with pytest.raises(BackendError, match="served by the dataset alias"):
        backend.catalog.delete_generation(kit.GEN)
    # A build of GEN2 that fails: staged indices and manifest written, never activated, then removed.
    kit.seed(backend, kit.GEN2)
    manifest_b = {**manifest_a, "generation": kit.GEN2.generation}
    with registry.lock():
        registry.write_manifest(manifest_b)
        registry.record_failure({"generation": kit.GEN2.generation, "unit": "embeddings", "category": "provider"})
        backend.catalog.delete_generation(kit.GEN2)
    assert registry.current() == manifest_a and backend.active_generation("conformance") == kit.GEN.generation
    assert [f["generation"] for f in registry.failures()] == [kit.GEN2.generation]
    assert registry.generations() == [kit.GEN.generation, kit.GEN2.generation]
    # The lock is exclusive across registry objects of the same prefix/dataset.
    other = es_factory().generation_registry()
    with registry.lock():
        with pytest.raises(BackendError, match="another ingestion holds the lock"):
            with other.lock():
                pass
    with other.lock():
        pass


def test_exact_vector_scores_match_the_memory_fake(es_factory):
    """Phase 1 acceptance: identical membership and order to the fake for the same vectors and scope."""
    from su_multi_arm.backends.memory import MemoryBackend

    rng = np.random.default_rng(7)
    vectors = rng.normal(size=(4, 8)).astype(np.float32)
    query = rng.normal(size=8).astype(np.float32)
    results = []
    for adapter in (MemoryBackend(), es_factory()):
        kit.seed(adapter)
        adapter.vector.upsert(kit.GEN, ["c1", "c2", "c3", "c4"], vectors)
        adapter.vector.refresh(kit.GEN)
        results.append(adapter.vector.search(kit.GEN, query, kit.Scope(exclude_ids=["d2"]), 3))
    assert [h.chunk_id for h in results[0]] == [h.chunk_id for h in results[1]]
    assert all(abs(a.score - b.score) < 1e-5 for a, b in zip(*results))


def test_write_lease_fencing_and_alias_compare_and_set(es_factory):
    """Risk R1 on a real server: the lease document is created, renewed, taken over (only after
    expiry) and deleted with ``if_seq_no``/``if_primary_term`` and only by its owner; activation
    switches the aliases only from the expected generation (``must_exist`` removes)."""
    from .lease_kit import check_compare_and_set, check_lease

    backend = es_factory()
    clock = es_factory.clock

    def advance(seconds):
        clock["now"] += seconds

    check_lease(lambda: es_factory().generation_registry(), advance, backend.generation_registry().lock_ttl)
    gen3 = kit.GEN.model_copy(update={"generation": "c" * 32})
    for gen in (kit.GEN, kit.GEN2, gen3):
        kit.seed(backend, gen)
    manifests = [{"dataset": "conformance", "generation": g.generation, "index_format": 3, "fingerprint": g.fingerprint} for g in (kit.GEN, kit.GEN2, gen3)]
    check_compare_and_set(backend.generation_registry(), manifests)
    assert backend.active_generation("conformance") == kit.GEN2.generation


def test_a_concurrent_alias_switch_fails_the_whole_request(es_factory, monkeypatch):
    from su_multi_arm.core.errors import WriteConflict

    backend = es_factory()
    gen3 = kit.GEN.model_copy(update={"generation": "c" * 32})
    for gen in (kit.GEN, kit.GEN2, gen3):
        kit.seed(backend, gen)
    backend.activate(kit.GEN2)
    monkeypatch.setattr(backend, "active_generation", lambda dataset: kit.GEN.generation)  # stale read
    with pytest.raises(WriteConflict):
        backend.activate(gen3, expected=kit.GEN.generation)
    monkeypatch.undo()
    assert backend.active_generation("conformance") == kit.GEN2.generation
