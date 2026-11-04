"""Conformance kit over the postgres adapter against a real PostgreSQL + pgvector server (plan §3.5
``postgres`` row, §6.1 real-service tier). Marked ``external``: reads ``SU_TEST_POSTGRES_DSN``
(default: the throwaway stack in ``tests/services/docker-compose.yml``, port 15432). Every adapter
uses a fresh random table prefix and drops only the schemas it created."""

from __future__ import annotations

import uuid

import numpy as np
import pytest

from su_multi_arm.backends import capabilities as caps
from su_multi_arm.core.errors import BackendError
from su_multi_arm.testing import backend_conformance as kit

pytestmark = pytest.mark.external

SERVED = {"catalog", "lexical", "vector", "records", "cache", "ledger", "session"}


@pytest.fixture
def pg_factory(postgres_dsn):
    from su_multi_arm.backends.postgres import PostgresBackend

    clock = {"now": 100.0}
    made = []

    def make(**overrides):
        backend = PostgresBackend(postgres_dsn, f"c{uuid.uuid4().hex[:10]}", timeout_seconds=10, clock=lambda: clock["now"], **overrides)
        made.append(backend)
        return backend

    make.clock = clock
    make.kind = "postgres"
    yield make
    for backend in made:
        backend.drop_all()
        backend.close()


def test_server_is_real_and_versioned(pg_factory):
    backend = pg_factory()
    info = backend.ping()
    assert info["version"] and info["prefix"] == backend.prefix


def test_every_served_capability_is_implemented_and_graph_is_planned(pg_factory):
    adapter = pg_factory()
    assert kit.check_protocols(adapter) == SERVED
    assert not hasattr(adapter, "graph") and caps.status_of("postgres", "graph") == "planned"


@pytest.mark.parametrize("capability", sorted(SERVED - {"cache"}))
def test_capability(pg_factory, capability):
    kit.CHECKS[capability](pg_factory())


def test_cache(pg_factory):
    kit.check_cache(pg_factory(), pg_factory.clock)


def test_run_all(pg_factory):
    assert kit.run_all(pg_factory, pg_factory.clock) == {c: "passed" for c in SERVED}


def test_run_all_with_hnsw(pg_factory):
    """The opt-in HNSW index passes the same kit (exact order on the kit's tiny fixture)."""
    assert kit.run_all(lambda: pg_factory(vector_index="hnsw"), pg_factory.clock) == {c: "passed" for c in SERVED}


def test_one_schema_per_generation_and_isolation(pg_factory):
    backend = pg_factory()
    kit.seed(backend, kit.GEN)
    kit.seed(backend, kit.GEN2)
    backend.catalog.put_documents(kit.GEN2, [kit.document(7)], [])
    assert backend.catalog.count_documents(kit.GEN) == 4 and backend.catalog.count_documents(kit.GEN2) == 5
    expected = f"{backend.prefix}_conformance_{kit.GEN.generation}"

    def schemas():
        with backend.tx("test") as cur:
            cur.execute("SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE %s", (backend.prefix + "%",))
            return {r[0] for r in cur.fetchall()}

    assert {backend.prefix, expected, f"{backend.prefix}_conformance_{kit.GEN2.generation}"} <= schemas()
    assert backend.generations() == sorted([kit.GEN.prefix(), kit.GEN2.prefix()])
    backend.cache.put(kit.GEN.prefix() + "x", 1, 10)
    backend.catalog.delete_generation(kit.GEN)
    assert expected not in schemas() and backend.catalog.count_documents(kit.GEN2) == 5
    assert backend.cache.get(kit.GEN.prefix() + "x") is None
    for call in (lambda: backend.catalog.count_documents(kit.GEN), lambda: backend.records.execute(kit.GEN, kit.ScopeSpec(), [])):
        with pytest.raises(BackendError, match="unknown generation"):
            call()


def test_long_dataset_names_use_a_digest_schema(pg_factory):
    backend = pg_factory()
    gen = kit.GEN.model_copy(update={"dataset": "A-Dataset/with a long and unusual name"})
    backend.catalog.put_documents(gen, [kit.Document(id="x", dataset=gen.dataset)], [])
    assert backend.catalog.count_documents(gen) == 1
    with backend.tx("test") as cur:
        assert backend.generation_info(cur, gen)["schema_name"].startswith(f"{backend.prefix}_g")


def test_lexical_analyzer_semantics_and_scope_pushdown(pg_factory):
    backend = pg_factory()
    kit.seed(backend)
    assert backend.analyzer_id(kit.GEN) == "pg_ts_english"
    query = kit.LexicalQuery(text="rounding certificate exemption")
    everything = backend.lexical.search(kit.GEN, query, None, 10)
    assert {h.document_id for h in everything.items} == {"d1", "d2", "d3"}
    scores = [h.score for h in everything.items]
    assert scores == sorted(scores, reverse=True) and all(s > 0 for s in scores)
    scope = kit.Scope(predicates=[kit.Predicate(field="created_at", op="range", lower="2024-01-02T00:00:00+00:00")])
    assert {h.document_id for h in backend.lexical.search(kit.GEN, query, scope, 10).items} == {"d2", "d3"}
    assert backend.lexical.search(kit.GEN, kit.LexicalQuery(text="rounding", fields=["other"]), None, 10).items == []
    # english configuration: stemming conflates inflections, stop words never match.
    assert {h.document_id for h in backend.lexical.search(kit.GEN, kit.LexicalQuery(text="certificates"), None, 10).items} == {"d1", "d3"}
    assert backend.lexical.search(kit.GEN, kit.LexicalQuery(text="the"), None, 10).items == []
    # User text is never tsquery syntax.
    hostile = backend.lexical.search(kit.GEN, kit.LexicalQuery(text="certificate & !rounding | 'x':* <-> )("), None, 10)
    assert {h.document_id for h in hostile.items} == {"d1", "d2", "d3"}


def test_text_search_config_is_recorded_per_generation(pg_factory, postgres_dsn):
    from su_multi_arm.backends.postgres import PostgresBackend

    simple = pg_factory(text_search_config="simple")
    kit.seed(simple)
    assert simple.analyzer_id(kit.GEN) == "pg_ts_simple"
    # 'simple' does not stem: the plural does not match, the exact token does.
    assert simple.lexical.search(kit.GEN, kit.LexicalQuery(text="certificates"), None, 10).items == []
    assert len(simple.lexical.search(kit.GEN, kit.LexicalQuery(text="certificate"), None, 10).items) == 2
    # A backend configured differently still searches the generation with the analyzer it was built with.
    other = PostgresBackend(postgres_dsn, simple.prefix, text_search_config="english")
    try:
        assert other.analyzer_id(kit.GEN) == "pg_ts_simple"
        assert other.lexical.search(kit.GEN, kit.LexicalQuery(text="certificates"), None, 10).items == []
    finally:
        other.close()
    with pytest.raises(BackendError, match="text_search_config"):
        PostgresBackend(postgres_dsn, "x", text_search_config="english; drop table x")
    broken = pg_factory(text_search_config="no_such_config")
    with pytest.raises(BackendError) as info:
        kit.seed(broken)
    assert info.value.operation == "index"


def test_vector_exact_matches_numpy_and_pushes_scope_down(pg_factory):
    backend = pg_factory()
    rng = np.random.default_rng(7)
    docs = [kit.Document(id=f"d{i:03d}", dataset="conformance", source="case" if i % 3 else "manual") for i in range(60)]
    chunks = [kit.Chunk(id=f"c{i:03d}", document_id=f"d{i:03d}", field="body", start=0, end=1, text="x") for i in range(60)]
    backend.catalog.put_documents(kit.GEN, docs, chunks)
    matrix = rng.normal(size=(60, 16)).astype(np.float32)
    backend.vector.upsert(kit.GEN, [c.id for c in chunks], matrix)
    backend.vector.refresh(kit.GEN)
    normed = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
    query = rng.normal(size=16).astype(np.float32)
    scores = normed @ (query / np.linalg.norm(query))
    manual = [i for i in range(60) if i % 3 == 0]
    expected = sorted(manual, key=lambda i: -scores[i])[:5]
    scope = kit.Scope(predicates=[kit.Predicate(field="source", op="eq", value="manual")])
    hits = backend.vector.search(kit.GEN, query, scope, 5)
    assert [h.chunk_id for h in hits] == [f"c{i:03d}" for i in expected]
    assert np.allclose([h.score for h in hits], [scores[i] for i in expected], atol=1e-5)
    with pytest.raises(BackendError, match="not a catalogued chunk"):
        backend.vector.upsert(kit.GEN, ["ghost"], np.ones((1, 16)))
    with pytest.raises(BackendError, match="dimension differs"):
        backend.vector.upsert(kit.GEN, ["c001"], np.ones((1, 8)))
    with pytest.raises(BackendError, match="finite"):
        backend.vector.upsert(kit.GEN, ["c001"], np.full((1, 16), np.nan))


def test_hnsw_is_opt_in_and_gated_by_filter_recall(pg_factory):
    rng = np.random.default_rng(11)
    docs = [kit.Document(id=f"d{i:04d}", dataset="conformance", source="case" if i % 2 else "manual") for i in range(400)]
    chunks = [kit.Chunk(id=f"c{i:04d}", document_id=f"d{i:04d}", field="body", start=0, end=1, text="x") for i in range(400)]
    matrix = rng.normal(size=(400, 32)).astype(np.float32)

    def build(**overrides):
        backend = pg_factory(**overrides)
        backend.catalog.put_documents(kit.GEN, docs, chunks)
        backend.vector.upsert(kit.GEN, [c.id for c in chunks], matrix)
        return backend

    exact = build()
    exact.vector.refresh(kit.GEN)
    with exact.tx("test") as cur:
        cur.execute("SELECT count(*) FROM pg_indexes WHERE schemaname = %s AND indexdef ILIKE %s", (f"{exact.prefix}_conformance_{kit.GEN.generation}", "%hnsw%"))
        assert cur.fetchone()[0] == 0  # exact never builds an ANN index

    from su_multi_arm.backends.postgres import HnswSettings

    hnsw = build(vector_index="hnsw", hnsw=HnswSettings(min_filter_recall=0.9))
    hnsw.vector.refresh(kit.GEN)  # builds the index and passes the gate
    with hnsw.tx("test") as cur:
        cur.execute("SELECT count(*) FROM pg_indexes WHERE schemaname = %s AND indexdef ILIKE %s", (f"{hnsw.prefix}_conformance_{kit.GEN.generation}", "%hnsw%"))
        assert cur.fetchone()[0] == 1
    measured = hnsw.vector.filter_recall(kit.GEN)
    assert measured["queries"] > 0 and measured["recall"] >= 0.9
    query = matrix[3]
    scope = kit.Scope(predicates=[kit.Predicate(field="source", op="eq", value="manual")])
    approx = hnsw.vector.search(kit.GEN, query, scope, 10)
    assert approx and all(int(h.document_id[1:]) % 2 == 0 for h in approx)  # scope holds under ANN, never widened
    # A floor no index can meet fails the refresh with a typed error (the build never activates).
    strict = build(vector_index="hnsw", hnsw=HnswSettings(ef_search=1, m=2, ef_construction=4, min_filter_recall=1.0))
    with pytest.raises(BackendError, match="filter recall"):
        strict.vector.refresh(kit.GEN)


def test_values_cardinality_cap_fails_closed(pg_factory):
    backend = pg_factory(max_values_cardinality=1)
    kit.seed(backend)
    rows = [
        kit.StructuredRecord(schema_id="document", id="d1", values={"product": "CloudTax"}, provenance=kit.PROV),
        kit.StructuredRecord(schema_id="document", id="d2", values={"product": "SearchDesk"}, provenance=kit.PROV),
    ]
    backend.records.upsert(kit.GEN, rows, kit.SCHEMA)
    with pytest.raises(BackendError, match="more than 1 distinct values"):
        backend.records.values(kit.GEN, "product", [], 10)
    assert backend.records.values(kit.GEN, "product", [kit.Predicate(field="product", op="eq", value="cloudtax")], 10) == ["CloudTax"]


def test_records_use_native_types(pg_factory):
    backend = pg_factory()
    kit.check_records(backend)
    with backend.tx("test") as cur:
        schema = backend.generation_info(cur, kit.GEN)["schema"]
        cur.execute(f"SELECT pg_typeof(ts_value)::text, ts_value FROM {schema}.record_fields WHERE owner_id = 'd2' AND field = 'touched_at'")
        kind, value = cur.fetchone()
    assert kind == "timestamp with time zone" and value.isoformat().startswith("2025-01-02T08:00:00")


def test_ordering_is_byte_order(pg_factory):
    backend = pg_factory()
    ids = ["b", "B", "a", "_z", "A1"]
    backend.catalog.put_documents(kit.GEN, [kit.Document(id=i, dataset="conformance") for i in ids], [])
    assert [d.id for d in backend.catalog.iter_scope(kit.GEN, None, kit.Page(size=10)).items] == sorted(ids)


def test_generation_registry_pointer_is_atomic_and_failures_never_touch_it(pg_factory):
    backend = pg_factory()
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
    with backend.tx("test") as cur:
        cur.execute(f"SELECT count(*) FROM {backend.table('meta')} WHERE key = 'CURRENT'")
        assert cur.fetchone()[0] == 1
    with registry.lock():
        with pytest.raises(BackendError, match="write lock is held"):
            with type(registry)(backend).lock():
                pass
    with registry.lock():
        pass


def test_from_config_reads_the_dsn_from_the_environment(postgres_dsn, monkeypatch):
    from su_multi_arm.backends.postgres import PostgresBackend
    from su_multi_arm.core.config import PostgresBackend as PostgresConfig

    spec = PostgresConfig(kind="postgres", dsn_env="SU_TEST_PG_DSN_FOR_THIS_TEST", table_prefix="cfg_" + uuid.uuid4().hex[:8], vector={"index": "hnsw", "hnsw_ef_search": 50})
    monkeypatch.delenv("SU_TEST_PG_DSN_FOR_THIS_TEST", raising=False)
    with pytest.raises(BackendError, match="dsn_env"):
        PostgresBackend.from_config(spec)
    monkeypatch.setenv("SU_TEST_PG_DSN_FOR_THIS_TEST", postgres_dsn)
    backend = PostgresBackend.from_config(spec)
    try:
        assert backend.ping()["prefix"] == spec.table_prefix and backend.vector_index == "hnsw" and backend.hnsw.ef_search == 50
    finally:
        backend.drop_all()
        backend.close()


def test_unreachable_server_is_a_typed_error():
    pytest.importorskip("psycopg")
    from su_multi_arm.backends.postgres import PostgresBackend

    backend = PostgresBackend("postgresql://su_test:x@127.0.0.1:1/none", "dead", timeout_seconds=1)
    with pytest.raises(BackendError) as info:
        backend.catalog.get(kit.GEN, ["d1"])
    assert info.value.adapter == "postgres" and info.value.operation == "get"


def test_write_lease_fencing_and_compare_and_set(pg_factory):
    """Risk R1 on a real server: the advisory-lock lease refuses a second session, the heartbeat
    confirms the session still holds it (``pg_locks``), a non-owner release unlocks nothing, and
    activation is a compare-and-set on the expected generation. Advisory locks end with their
    session, so there is no expiry/takeover step."""
    from .lease_kit import check_compare_and_set, check_lease

    backend = pg_factory()
    check_lease(backend.generation_registry, None, 120.0)
    registry = backend.generation_registry()
    with registry.lock() as lease:
        assert registry.lease_renew(lease.token, 120.0) is True  # the pg_locks query sees this session's lock
    assert registry.lease_renew(lease.token, 120.0) is False  # released: no session holds it for this token
    manifests = [{"dataset": "conformance", "generation": g.generation, "index_format": 3, "fingerprint": g.fingerprint} for g in (kit.GEN, kit.GEN2, kit.GEN.model_copy(update={"generation": "c" * 32}))]
    check_compare_and_set(pg_factory().generation_registry(), manifests)


def test_pins_live_in_the_registry_meta_table(pg_factory, postgres_dsn):
    """Addendum A1 §3.8.4 (n4): cross-process pins on the PostgreSQL registry (``pin:`` meta rows),
    seen through a second backend handle on the same prefix."""
    from su_multi_arm.backends.postgres import PostgresBackend
    from su_multi_arm.ingestion.pins import PostgresMetaPins, pin_registry

    clock = {"t": 1000.0}
    backend = pg_factory()
    pins = pin_registry(backend.generation_registry(), clock=lambda: clock["t"])
    assert isinstance(pins, PostgresMetaPins)
    token = pins.pin("a" * 32, "proc-a", 60)
    pins.pin("a" * 32, "proc-b", 5)
    second = PostgresBackend(postgres_dsn, backend.prefix, timeout_seconds=10)
    try:
        other = pin_registry(second.generation_registry(), clock=lambda: clock["t"])
        assert [t.holder for t in other.pinned("a" * 32)] == ["proc-a", "proc-b"]
        clock["t"] += 10
        assert [t.holder for t in other.pinned("a" * 32)] == ["proc-a"]
        pins.release(token)
        assert other.pinned("a" * 32) == [] and other.sweep() == 1
    finally:
        second.close()
