"""The multi-backend write path against real services (plan Phase 1 row "Multi-backend write path",
§6.1 real-service tier): catalog, records and dense on PostgreSQL (authoritative ``CURRENT`` pointer
row), lexical on OpenSearch (dataset aliases as its pointer), graph + communities on Neo4j (pointer
node); ledger, sessions and cache on the PostgreSQL runtime tables.

Covers: staging on every backend, the manifest's per-backend identity and analyzer ids, request
filters pushed down on the OpenSearch scope mirror and resolved for Neo4j, activation compensation
(the authoritative switch fails after the alias and pointer node moved: both are put back), reader
refusal on a disagreeing alias, and ``delete_generation`` across the three services.

Marked ``external``. Defaults are the throwaway stack in ``tests/services/docker-compose.yml``:
``SU_TEST_POSTGRES_DSN`` (``127.0.0.1:15432``), ``SU_TEST_OPENSEARCH_URL`` (``127.0.0.1:19200``),
``SU_TEST_NEO4J_URI`` / ``SU_TEST_NEO4J_PASSWORD`` (``127.0.0.1:17687``). A service that is not
reachable at its default is skipped with a reason; an explicitly configured unreachable one fails.
Each run uses its own table prefix, index prefix and namespace, and removes them afterwards.
"""

from __future__ import annotations

import os
import uuid

import pytest

from su_multi_arm import Document, Engine, SearchRequest
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.errors import GenerationDisagreement, GenerationInUse
from su_multi_arm.ingestion.release import generation_id
from su_multi_arm.ingestion.write import IngestError

pytestmark = pytest.mark.external

PG_DEFAULT = "postgresql://su_test:su_test_password@127.0.0.1:15432/su_test"
OS_DEFAULT = "http://127.0.0.1:19200"
NEO4J_DEFAULT = "bolt://127.0.0.1:17687"

DOCS = [
    Document(id="kb:certificate", dataset="demo", source="article", title="Add an exemption certificate", fields={"details": "Open the record and add the exemption certificate. CloudTax rounding applies per line."}, metadata={"product": "CloudTax"}),
    Document(id="case:certificate", dataset="demo", source="case", title="Certificate expired", fields={"description": "An exemption certificate stopped applying in CloudTax."}, metadata={"product": "CloudTax"}),
    Document(id="kb:rounding", dataset="demo", source="article", title="Rounding configuration", fields={"details": "CloudTax rounding applies per invoice line."}, metadata={"product": "CloudTax"}),
    Document(id="kb:source", dataset="demo", source="manual", title="Create a content source", fields={"body": "In SearchDesk a content source connects a repository to the index."}, metadata={"product": "SearchDesk"}),
]


def _reachable(variable: str, default: str, probe) -> str:
    configured = os.environ.get(variable)
    target = configured or default
    try:
        probe(target)
    except Exception as exc:  # noqa: BLE001 - any connection failure means "not available"
        if configured:
            raise
        pytest.skip(f"{variable} unset and the default test stack is not reachable: {exc}")
    return target


@pytest.fixture
def services(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    pytest.importorskip("opensearchpy")
    neo4j = pytest.importorskip("neo4j")
    import urllib.request

    dsn = _reachable("SU_TEST_POSTGRES_DSN", PG_DEFAULT, lambda t: psycopg.connect(t, connect_timeout=5).close())
    url = _reachable("SU_TEST_OPENSEARCH_URL", OS_DEFAULT, lambda t: urllib.request.urlopen(t, timeout=5).read())
    if not os.environ.get("SU_TEST_NEO4J_PASSWORD"):
        monkeypatch.setenv("SU_TEST_NEO4J_PASSWORD", "su_test_password")

    def neo_probe(target):
        driver = neo4j.GraphDatabase.driver(target, auth=("neo4j", os.environ["SU_TEST_NEO4J_PASSWORD"]), connection_timeout=5)
        try:
            driver.verify_connectivity()
        finally:
            driver.close()

    uri = _reachable("SU_TEST_NEO4J_URI", NEO4J_DEFAULT, neo_probe)
    monkeypatch.setenv("SU_TEST_MB_PG_DSN", dsn)
    monkeypatch.setenv("SU_TEST_MB_OS_HOSTS", url)
    return uri


@pytest.fixture
def engine(services):
    tag = uuid.uuid4().hex[:8]
    data = {
        "schema_version": 2,
        "dataset": "demo",
        "backends": {
            "pg": {"kind": "postgres", "dsn_env": "SU_TEST_MB_PG_DSN", "table_prefix": f"mb_{tag}"},
            "os": {"kind": "opensearch", "hosts_env": "SU_TEST_MB_OS_HOSTS", "index_prefix": f"su_test_mb_{tag}"},
            "graph": {"kind": "neo4j", "uri": services, "password_env": "SU_TEST_NEO4J_PASSWORD", "namespace": f"mb{tag}", "timeout_seconds": 20},
        },
        "storage": {"catalog": "pg", "records": "pg", "ledger": "pg", "sessions": "pg", "cache": "pg", "communities": "graph"},
        "metadata_schema": {"fields": [{"name": "product", "type": "string", "groupable": True}]},
        "pipeline": {
            "arms": {
                "dense": {"backend": "pg"},
                "lexical": {"backend": "os"},
                "structured": {"backend": "pg"},
                "graph": {"backend": "graph"},
                "community": {"backend": "graph"},
            }
        },
        "extraction": {"extractors": ["gazetteer"], "gazetteer": {"topic": ["exemption certificate", "rounding"], "product": ["CloudTax", "SearchDesk"]}},
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 32}}},
    }
    built = Engine(validate_config(data))
    yield built
    pg, search, graph = built.backends["pg"], built.backends["os"], built.backends["graph"]
    try:
        rows = search.transport.request("GET", f"/_cat/indices/{search.prefix}_*", params={"format": "json", "h": "index"})
        names = [row["index"] for row in rows or []]
        if names:
            search.transport.request("DELETE", "/" + ",".join(names))
    finally:
        search.close()
        graph.drop_namespace()
        graph.close()
        pg.drop_all()
        pg.close()


def ids(result, arm):
    return {h.document_id for h in result.arm_hits.get(arm, [])}


def test_catalog_on_postgres_lexical_on_opensearch_graph_on_neo4j(engine):
    pg, search, graph = engine.backends["pg"], engine.backends["os"], engine.backends["graph"]
    manifest = engine.ingest(DOCS)
    first = manifest["generation"]
    assert manifest["status"] == "complete" and all(p["verified"] for p in manifest["projections"].values())
    generations = manifest["generations"]
    assert generations["activation_order"] == ["os", "graph", "pg"]
    entries = generations["backends"]
    assert entries["pg"]["role"] == "authoritative" and entries["pg"]["activation"] == "current"
    assert entries["pg"]["identity"]["text_search_config"] == "english" and entries["pg"]["identity"]["schema"]
    assert entries["os"]["activation"] == "pointer" and entries["os"]["scope_mirror"] is True and entries["os"]["analyzer_id"] == "lucene_standard"
    assert entries["os"]["identity"]["indices"][0] == f"{search.prefix}_demo_{first}_docs"
    assert entries["graph"]["activation"] == "pointer" and entries["graph"]["scope_mirror"] is False
    assert entries["graph"]["identity"]["key"] == f"{graph.namespace}|demo|{first}"
    assert manifest["projections"]["lexical"] == {"backend": "os", "rows": manifest["chunks"], "verified": True, "analyzer_id": "lucene_standard"}
    # every backend serves the new generation
    assert pg.generation_registry().current_generation() == first
    assert search.active_generation("demo") == first and graph.active_generation("demo") == first

    result = engine.search("exemption certificate")
    assert result.generation == first
    for arm in ("dense", "lexical", "graph"):
        assert "kb:certificate" in ids(result, arm), arm
    filtered = engine.search(SearchRequest(query="exemption certificate", filters={"source": "case"}))
    for arm in ("dense", "lexical", "graph", "community"):
        assert ids(filtered, arm) <= {"case:certificate"}, arm
    assert ids(filtered, "lexical") == {"case:certificate"} and ids(filtered, "graph") == {"case:certificate"}
    answer = engine.answer("exemption certificate")
    assert answer.retrieval.generation == first

    # the authoritative switch fails after the alias and the pointer node moved: both are put back
    registry = engine.write_path.registry
    original = registry.activate

    def refuse(manifest, **_):
        raise OSError("injected CURRENT failure")

    registry.activate = refuse
    try:
        with pytest.raises(IngestError, match="activation failed on 'pg'") as info:
            engine.ingest(DOCS[:2], replace=True)
    finally:
        registry.activate = original
    failure = info.value.failure
    assert [c["backend"] for c in failure["compensation"]] == ["graph", "os"] and all(c["ok"] for c in failure["compensation"])
    assert failure["left_for_cleanup"] == []
    assert search.active_generation("demo") == first and graph.active_generation("demo") == first
    assert pg.generation_registry().current_generation() == first
    failed = failure["generation"]
    assert f"demo/{failed}/" not in graph.generations() and f"demo/{failed}/" not in search.generations()
    assert engine.search("exemption certificate").generation == first

    # a second successful build; then the alias is switched back by hand: readers are refused
    second = engine.ingest([], delete_ids=["kb:rounding"])["generation"]
    assert search.active_generation("demo") == second and graph.active_generation("demo") == second
    first_gen = generation_id(registry.manifest(first))
    search.activate_generation(first_gen)
    with pytest.raises(GenerationDisagreement) as refused:
        engine.search("exemption certificate")
    assert refused.value.pointers == {"os": first, "graph": second}
    with pytest.raises(GenerationInUse, match=r"\['os'\]"):
        engine.write_path.delete_generation(first)
    search.activate_generation(generation_id(registry.manifest(second)))
    assert engine.search("rounding").generation == second

    # clean-up across the three services; the active generation is refused
    with pytest.raises(GenerationInUse, match="CURRENT"):
        engine.write_path.delete_generation(second)
    report = engine.write_path.delete_generation(first)
    assert report == {"generation": first, "deleted": ["os", "graph", "pg"], "left_for_cleanup": []}
    assert f"demo/{first}/" not in pg.generations() and f"demo/{first}/" not in search.generations() and f"demo/{first}/" not in graph.generations()
    assert f"demo/{second}/" in pg.generations() and f"demo/{second}/" in search.generations() and f"demo/{second}/" in graph.generations()
