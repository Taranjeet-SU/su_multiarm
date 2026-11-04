"""Conformance kit over the mongodb adapter against a real MongoDB (plan §3.5 ``mongodb`` row, §6.1
real-service tier). Marked ``external``: reads ``SU_TEST_MONGO_URI`` (default: the throwaway stack
in ``tests/services/docker-compose.yml``, port 27018). Uses its own database and a fresh collection
prefix per adapter instance; drops only what it created."""

from __future__ import annotations

import os
import uuid

import pytest

from su_multi_arm.backends import capabilities as caps
from su_multi_arm.core.errors import BackendError
from su_multi_arm.testing import backend_conformance as kit

pytestmark = pytest.mark.external

DEFAULT_URI = "mongodb://su_test:su_test_password@127.0.0.1:27018/?authSource=admin"
DATABASE = "su_test_conformance"
SERVED = {"catalog", "lexical", "records", "cache", "ledger", "session"}


@pytest.fixture(scope="session")
def mongo_uri():
    pymongo = pytest.importorskip("pymongo")
    configured = os.environ.get("SU_TEST_MONGO_URI")
    uri = configured or DEFAULT_URI
    client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=3000)
    try:
        client.admin.command("ping")
    except pymongo.errors.PyMongoError as exc:
        if configured:
            raise  # an explicitly configured server that does not answer is a failure, not a skip
        pytest.skip(f"SU_TEST_MONGO_URI unset and the default test stack is not reachable: {exc}")
    yield uri
    client.drop_database(DATABASE)
    client.close()


@pytest.fixture
def adapter_factory(mongo_uri):
    from su_multi_arm.backends.mongodb import MongoBackend

    clock = {"now": 100.0}
    made = []

    def make(**overrides):
        prefix = f"c{uuid.uuid4().hex[:10]}"
        backend = MongoBackend(mongo_uri, DATABASE, prefix, timeout_seconds=10, clock=lambda: clock["now"], **overrides)
        made.append(backend)
        return backend

    make.clock = clock
    make.kind = "mongodb"
    yield make
    for backend in made:
        backend.drop_all()
        backend.client.close()


def test_server_is_real_and_versioned(adapter_factory):
    info = adapter_factory().ping()
    assert info["version"] and info["database"] == DATABASE


def test_every_served_capability_is_implemented_and_vector_is_absent(adapter_factory):
    adapter = adapter_factory()
    assert kit.check_protocols(adapter) == SERVED
    assert not hasattr(adapter, "vector") and caps.status_of("mongodb", "vector") == "planned"
    assert not hasattr(adapter, "graph") and caps.status_of("mongodb", "graph") is None


@pytest.mark.parametrize("capability", sorted(SERVED - {"cache"}))
def test_capability(adapter_factory, capability):
    kit.CHECKS[capability](adapter_factory())


def test_cache(adapter_factory):
    kit.check_cache(adapter_factory(), adapter_factory.clock)


def test_run_all(adapter_factory):
    assert kit.run_all(adapter_factory, adapter_factory.clock) == {c: "passed" for c in SERVED}


def test_generation_scoped_collections_and_isolation(adapter_factory):
    backend = adapter_factory()
    kit.seed(backend, kit.GEN)
    kit.seed(backend, kit.GEN2)
    backend.catalog.put_documents(kit.GEN2, [kit.document(7)], [])
    assert backend.catalog.count_documents(kit.GEN) == 4 and backend.catalog.count_documents(kit.GEN2) == 5
    names = set(backend.db.list_collection_names())
    expected = f"{backend.prefix}_conformance_{kit.GEN.generation}_documents"
    assert expected in names and f"{backend.prefix}_conformance_{kit.GEN2.generation}_lexical" in names
    assert backend.generations() == sorted([kit.GEN.prefix(), kit.GEN2.prefix()])
    backend.catalog.delete_generation(kit.GEN)
    assert backend.catalog.count_documents(kit.GEN2) == 5
    assert expected not in set(backend.db.list_collection_names())
    with pytest.raises(BackendError, match="unknown generation"):
        backend.catalog.count_documents(kit.GEN)
    with pytest.raises(BackendError, match="unknown generation"):
        backend.records.execute(kit.GEN, kit.ScopeSpec(), [])


def test_lexical_scope_predicates_are_pushed_down_through_documents(adapter_factory):
    backend = adapter_factory()
    kit.seed(backend)
    query = kit.LexicalQuery(text="rounding certificate exemption")
    everything = backend.lexical.search(kit.GEN, query, None, 10)
    assert {h.document_id for h in everything.items} == {"d1", "d2", "d3"}
    scope = kit.Scope(predicates=[kit.Predicate(field="product", op="eq", value="CloudTax")])
    assert {h.document_id for h in backend.lexical.search(kit.GEN, query, scope, 10).items} == {"d1", "d3"}
    scope = kit.Scope(predicates=[kit.Predicate(field="created_at", op="range", lower="2024-01-02T00:00:00+00:00")])
    assert {h.document_id for h in backend.lexical.search(kit.GEN, query, scope, 10).items} == {"d2", "d3"}
    fielded = backend.lexical.search(kit.GEN, kit.LexicalQuery(text="rounding", fields=["other"]), None, 10)
    assert fielded.items == []
    # Scores are MongoDB textScores (term frequency), never claimed to be BM25: only ordering invariants hold.
    scores = [h.score for h in everything.items]
    assert scores == sorted(scores, reverse=True) and all(s > 0 for s in scores)


def test_values_cardinality_cap_fails_closed(adapter_factory):
    backend = adapter_factory(max_values_cardinality=1)
    kit.seed(backend)
    rows = [
        kit.StructuredRecord(schema_id="document", id="d1", values={"product": "CloudTax"}, provenance=kit.PROV),
        kit.StructuredRecord(schema_id="document", id="d2", values={"product": "SearchDesk"}, provenance=kit.PROV),
    ]
    backend.records.upsert(kit.GEN, rows, kit.SCHEMA)
    with pytest.raises(BackendError, match="more than 1 distinct values"):
        backend.records.values(kit.GEN, "product", [], 10)
    assert backend.records.values(kit.GEN, "product", [kit.Predicate(field="product", op="eq", value="cloudtax")], 10) == ["CloudTax"]


def test_ttl_indexes(adapter_factory):
    backend = adapter_factory(session_ttl_seconds=60)
    backend.cache.put(kit.GEN.prefix() + "x", 1, 10)
    cache_indexes = backend.collection("cache").index_information()
    assert cache_indexes["ttl"]["expireAfterSeconds"] == 0
    backend.session.create(kit.SessionRecord(id="s", principal_id="p", dataset="conformance", created_at=0.0, last_seen_at=0.0))
    assert backend.collection("sessions").index_information()["ttl"]["expireAfterSeconds"] == 60


def test_generation_registry_pointer_is_atomic_and_failures_never_touch_it(adapter_factory):
    backend = adapter_factory()
    registry = backend.generation_registry()
    assert registry.current() is None and registry.generations() == [] and registry.failures() == []
    m1 = {"dataset": "conformance", "generation": kit.GEN.generation, "index_format": 3, "fingerprint": kit.GEN.fingerprint}
    m2 = {**m1, "generation": kit.GEN2.generation}
    with pytest.raises(ValueError, match="without a written manifest"):
        registry.activate(m1)
    registry.write_manifest(m1)
    registry.activate(m1)
    assert registry.current() == m1
    registry.write_manifest(m2)
    registry.record_failure({"generation": kit.GEN2.generation, "unit": "embed", "category": "provider"})
    assert registry.current() == m1 and registry.generations() == sorted([m1["generation"], m2["generation"]])
    assert registry.failures()[0]["unit"] == "embed"
    registry.activate(m2)
    assert registry.current() == m2
    assert backend.collection("meta").count_documents({"_id": "CURRENT"}) == 1
    with registry.lock():
        with pytest.raises(BackendError, match="write lock is held"):
            with type(registry)(backend).lock():
                pass
    with registry.lock():
        pass


def test_from_config_reads_the_uri_from_the_environment(mongo_uri, monkeypatch):
    from su_multi_arm.backends.mongodb import MongoBackend
    from su_multi_arm.core.config import MongoBackend as MongoBackendConfig

    spec = MongoBackendConfig(kind="mongodb", uri_env="SU_TEST_MONGO_URI_FOR_THIS_TEST", database=DATABASE, collection_prefix="cfg")
    monkeypatch.delenv("SU_TEST_MONGO_URI_FOR_THIS_TEST", raising=False)
    with pytest.raises(BackendError, match="uri_env"):
        MongoBackend.from_config(spec)
    monkeypatch.setenv("SU_TEST_MONGO_URI_FOR_THIS_TEST", mongo_uri)
    backend = MongoBackend.from_config(spec)
    try:
        assert backend.ping()["prefix"] == "cfg"
    finally:
        backend.client.close()


def test_unreachable_server_is_a_typed_error():
    pytest.importorskip("pymongo")
    from su_multi_arm.backends.mongodb import MongoBackend

    backend = MongoBackend("mongodb://127.0.0.1:1/?directConnection=true", DATABASE, "dead", timeout_seconds=0.5)
    with pytest.raises(BackendError) as info:
        backend.catalog.get(kit.GEN, ["d1"])
    assert info.value.adapter == "mongodb" and info.value.operation == "generation"
    backend.client.close()


def test_write_lease_fencing_and_compare_and_set(adapter_factory):
    """Risk R1 on a real server: owner-token lease, takeover only after expiry, release/renewal by a
    non-owner refused, activation as compare-and-set on the expected generation."""
    from .lease_kit import check_compare_and_set, check_lease

    backend = adapter_factory()
    clock = adapter_factory.clock

    def advance(seconds):
        clock["now"] += seconds

    check_lease(backend.generation_registry, advance, backend.generation_registry().lock_ttl_seconds)
    manifests = [{"dataset": "conformance", "generation": g.generation, "index_format": 3, "fingerprint": g.fingerprint} for g in (kit.GEN, kit.GEN2, kit.GEN.model_copy(update={"generation": "c" * 32}))]
    check_compare_and_set(adapter_factory().generation_registry(), manifests)
