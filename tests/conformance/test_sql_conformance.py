"""Conformance kit over the generic SQL adapter (config kind ``sqlalchemy``, ``backends/sql/``) on its
tested dialects: ``sql[sqlite]`` runs on every push (a real SQLite database through SQLAlchemy) and
``sql[postgres]`` is marked ``external`` (the PostgreSQL test server, see ``postgres_dsn``)."""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("sqlalchemy")

from su_multi_arm.backends import capabilities as caps  # noqa: E402
from su_multi_arm.core.errors import BackendError  # noqa: E402
from su_multi_arm.testing import backend_conformance as kit  # noqa: E402

from .conftest import sqlalchemy_postgres_url  # noqa: E402

SERVED = {"catalog", "records", "cache", "ledger", "session"}
DIALECTS = ["sqlite", pytest.param("postgres", marks=pytest.mark.external)]


@pytest.fixture(params=DIALECTS)
def sql_factory(request, tmp_path):
    from su_multi_arm.backends.sql import SqlBackend

    if request.param == "postgres":
        url = sqlalchemy_postgres_url(request.getfixturevalue("postgres_dsn"))
    else:
        url = f"sqlite:///{tmp_path / 'sql.db'}"
    clock = {"now": 100.0}
    made = []

    def make(**overrides):
        backend = SqlBackend(url, f"s{uuid.uuid4().hex[:10]}", timeout_seconds=10, clock=lambda: clock["now"], **overrides)
        made.append(backend)
        return backend

    make.clock = clock
    make.dialect = request.param
    yield make
    for backend in made:
        backend.drop_all()
        backend.close()


def test_dialect_is_real_and_tested(sql_factory):
    info = sql_factory().ping()
    assert info["dialect"] == {"sqlite": "sqlite", "postgres": "postgresql"}[sql_factory.dialect]
    assert info["dialect_tested"] is True and info["server_version"]


def test_every_served_capability_is_implemented_and_search_is_absent(sql_factory):
    adapter = sql_factory()
    assert kit.check_protocols(adapter) == SERVED
    for capability in ("lexical", "vector"):
        assert not hasattr(adapter, capability) and caps.status_of("sqlalchemy", capability) == "planned"
    assert caps.status_of("sqlalchemy", "graph") is None


@pytest.mark.parametrize("capability", sorted(SERVED - {"cache"}))
def test_capability(sql_factory, capability):
    kit.CHECKS[capability](sql_factory())


def test_cache(sql_factory):
    kit.check_cache(sql_factory(), sql_factory.clock)


def test_run_all(sql_factory):
    assert kit.run_all(sql_factory, sql_factory.clock) == {c: "passed" for c in SERVED}


def test_generation_scoped_tables_and_isolation(sql_factory):
    import sqlalchemy as sa

    from su_multi_arm.backends.sql._db import generation_token

    backend = sql_factory()
    kit.seed(backend, kit.GEN)
    kit.seed(backend, kit.GEN2)
    backend.catalog.put_documents(kit.GEN2, [kit.document(7)], [])
    assert backend.catalog.count_documents(kit.GEN) == 4 and backend.catalog.count_documents(kit.GEN2) == 5
    token = generation_token(kit.GEN.dataset, kit.GEN.generation)
    names = set(sa.inspect(backend.engine).get_table_names())
    assert {f"{backend.prefix}_{token}_docs", f"{backend.prefix}_{token}_rfields"} <= names
    assert all(len(n) <= 63 for n in names)
    assert backend.generations() == sorted([kit.GEN.prefix(), kit.GEN2.prefix()])
    backend.cache.put(kit.GEN.prefix() + "x", 1, 10)
    backend.cache.put(kit.GEN2.prefix() + "x", 2, 10)
    backend.catalog.delete_generation(kit.GEN)
    assert f"{backend.prefix}_{token}_docs" not in set(sa.inspect(backend.engine).get_table_names())
    assert backend.catalog.count_documents(kit.GEN2) == 5
    assert backend.cache.get(kit.GEN.prefix() + "x") is None and backend.cache.get(kit.GEN2.prefix() + "x").value == 2
    with pytest.raises(BackendError, match="unknown generation"):
        backend.records.execute(kit.GEN, kit.ScopeSpec(), [])


def test_values_cardinality_cap_fails_closed(sql_factory):
    backend = sql_factory(max_values_cardinality=1)
    kit.seed(backend)
    rows = [
        kit.StructuredRecord(schema_id="document", id="d1", values={"product": "CloudTax"}, provenance=kit.PROV),
        kit.StructuredRecord(schema_id="document", id="d2", values={"product": "SearchDesk"}, provenance=kit.PROV),
    ]
    backend.records.upsert(kit.GEN, rows, kit.SCHEMA)
    with pytest.raises(BackendError, match="more than 1 distinct values"):
        backend.records.values(kit.GEN, "product", [], 10)
    assert backend.records.values(kit.GEN, "product", [kit.Predicate(field="product", op="eq", value="cloudtax")], 10) == ["CloudTax"]


def test_ordering_is_byte_order_on_every_dialect(sql_factory):
    """Pages follow Python's ``sorted`` (code-point order), not a locale collation."""
    backend = sql_factory()
    ids = ["b", "B", "a", "_z", "A1"]
    backend.catalog.put_documents(kit.GEN, [kit.Document(id=i, dataset="conformance") for i in ids], [])
    assert [d.id for d in backend.catalog.iter_scope(kit.GEN, None, kit.Page(size=10)).items] == sorted(ids)


def test_generation_registry_pointer_is_atomic_and_failures_never_touch_it(sql_factory):
    backend = sql_factory()
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
    with registry.lock():
        with pytest.raises(BackendError, match="write lock is held"):
            with type(registry)(backend).lock():
                pass
    with registry.lock():
        pass
    sql_factory.clock["now"] += 10_000  # an abandoned lease is taken over after lock_ttl_seconds
    stale = type(registry)(backend)
    meta = backend.rt["meta"]
    with backend.tx("test") as conn:
        conn.execute(meta.insert().values(key="write_lock", value='{"acquired_at": 0}'))
    with stale.lock():
        pass


def test_from_config_reads_the_url_from_the_environment(tmp_path, monkeypatch):
    from su_multi_arm.backends.sql import SqlBackend
    from su_multi_arm.core.config import SqlAlchemyBackend as SqlConfig

    spec = SqlConfig(kind="sqlalchemy", url_env="SU_TEST_SQL_URL_FOR_THIS_TEST", table_prefix="cfg")
    monkeypatch.delenv("SU_TEST_SQL_URL_FOR_THIS_TEST", raising=False)
    with pytest.raises(BackendError, match="url_env"):
        SqlBackend.from_config(spec)
    monkeypatch.setenv("SU_TEST_SQL_URL_FOR_THIS_TEST", f"sqlite:///{tmp_path / 'cfg.db'}")
    backend = SqlBackend.from_config(spec)
    assert backend.ping()["prefix"] == "cfg"
    backend.close()


def test_unusable_urls_and_unreachable_servers_are_typed_errors(tmp_path):
    from su_multi_arm.backends.sql import SqlBackend

    for url in ("not a url", "nosuchdialect://x/y"):
        with pytest.raises(BackendError, match="cannot build an engine") as info:
            SqlBackend(url)
        assert info.value.adapter == "sqlalchemy" and info.value.operation == "init"
    missing_dir = SqlBackend(f"sqlite:///{tmp_path / 'absent' / 'x.db'}")
    with pytest.raises(BackendError) as info:
        missing_dir.catalog.get(kit.GEN, ["d1"])
    assert info.value.adapter == "sqlalchemy" and info.value.operation == "get"
    long_chunk = kit.Chunk(id="c" * 600, document_id="x", field="b", start=0, end=1, text="y")
    with pytest.raises(BackendError, match="longer than 512"):
        SqlBackend(f"sqlite:///{tmp_path / 'ok.db'}").catalog.put_documents(kit.GEN, [kit.Document(id="x", dataset="c")], [long_chunk])
