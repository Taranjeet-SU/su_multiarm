"""The v2 engine with its graph slot (graph + community arms, community storage) on a real Neo4j
(plan Phase 1 neo4j row, §6.1 real-service tier). Replaces the v1 ``tests/test_external.py``
``test_neo4j_real`` and ``test_neo4j_explicit_multihop_relations`` (which drove the retired
``adapters/neo4j.py``): same edge cases (graph hits on the synthetic corpus, request filters honoured,
deletions visible after re-ingest, two-hop fact traversal over a bridge document, exclusion of the
bridge cutting the path), plus result parity with the sqlite graph facet and the generation-pointer
rules. Marked ``external``; reads ``SU_TEST_NEO4J_URI`` / ``SU_TEST_NEO4J_PASSWORD`` (default: the
throwaway compose stack, Bolt port 17687). Each test uses its own namespace and drops it."""

from __future__ import annotations

import os
import uuid

import pytest

from su_multi_arm import Document, Engine, SearchRequest
from su_multi_arm.core.config import ChatRole, validate_config
from su_multi_arm.core.errors import BackendError
from su_multi_arm.ingestion.write import IngestError
from su_multi_arm.providers import JsonModel

pytestmark = pytest.mark.external

DEFAULT_URI = "bolt://127.0.0.1:17687"
PASSWORD_ENV = "SU_TEST_NEO4J_PASSWORD"


@pytest.fixture
def neo4j_env(monkeypatch):
    neo4j = pytest.importorskip("neo4j")
    configured = os.environ.get("SU_TEST_NEO4J_URI")
    uri = configured or DEFAULT_URI
    if not os.environ.get(PASSWORD_ENV):
        monkeypatch.setenv(PASSWORD_ENV, "su_test_password")
    driver = neo4j.GraphDatabase.driver(uri, auth=("neo4j", os.environ[PASSWORD_ENV]), connection_timeout=5)
    try:
        driver.verify_connectivity()
    except (neo4j.exceptions.Neo4jError, neo4j.exceptions.DriverError, OSError) as exc:
        if configured:
            raise
        pytest.skip(f"SU_TEST_NEO4J_URI unset and the default test stack is not reachable: {exc}")
    finally:
        driver.close()
    return uri


@pytest.fixture
def engines():
    made = []
    yield made
    for engine in made:
        for backend in engine.backends.values():
            if getattr(backend, "kind", None) == "neo4j":
                backend.drop_namespace()
                backend.close()


def on_neo4j(config, uri, state=None):
    data = config.model_dump(mode="json")
    if state is not None:
        data["backends"]["local"]["path"] = str(state)
    data["backends"]["graph"] = {"kind": "neo4j", "uri": uri, "password_env": PASSWORD_ENV, "namespace": f"e{uuid.uuid4().hex[:12]}", "timeout_seconds": 20}
    data["pipeline"]["arms"]["graph"]["backend"] = "graph"
    data["pipeline"]["arms"]["community"]["backend"] = "graph"
    data["storage"]["communities"] = "graph"
    return validate_config(data)


def graph_ids(result, arm="graph"):
    return [h.document_id for h in result.arm_hits[arm]]


def test_graph_and_community_arms_on_neo4j_match_sqlite(config, documents, neo4j_env, engines, tmp_path):
    reference = Engine(config)
    reference.ingest(documents)
    engine = Engine(on_neo4j(config, neo4j_env, tmp_path / "neo"))
    engines.append(engine)
    manifest = engine.ingest(documents)
    neo = engine.backends["graph"]
    assert neo.kind == "neo4j" and manifest["backends"]["graph"] == "graph" and manifest["backends"]["communities"] == "graph"
    assert neo.active_generation(manifest["dataset"]) == manifest["generation"]
    for query in ("exemption certificate", "CloudTax rounding", "certificate"):
        mine, theirs = engine.search(query), reference.search(query)
        for arm in ("graph", "community"):
            assert graph_ids(mine, arm) == graph_ids(theirs, arm), (query, arm)
            assert [h.provenance for h in mine.arm_hits[arm]] == [h.provenance for h in theirs.arm_hits[arm]], (query, arm)
    assert "kb:certificate" in graph_ids(engine.search("exemption certificate"))
    # request filters are predicates: resolved to document ids through the catalog, then pushed down
    for query in ("exemption certificate", "CloudTax rounding"):
        request = SearchRequest(query=query, filters={"source": "case"})
        filtered, expected = engine.search(request), reference.search(request)
        assert graph_ids(filtered) == ["case:certificate"]
        for arm in ("graph", "community"):
            assert all(h.source == "case" for h in filtered.arm_hits[arm])
            assert graph_ids(filtered, arm) == graph_ids(expected, arm), (query, arm)
    # deletions are visible after re-ingest; the pointer follows the new generation
    second = engine.ingest([], delete_ids=["kb:certificate"])
    assert "kb:certificate" not in graph_ids(engine.search("exemption certificate"))
    assert neo.active_generation(second["dataset"]) == second["generation"] != manifest["generation"]


def test_two_hop_fact_traversal_over_a_bridge(config, neo4j_env, engines, monkeypatch):
    data = on_neo4j(config, neo4j_env).model_dump(mode="json")
    data["models"]["roles"]["extractor"] = ChatRole(provider="openai_compatible", model="fixture").model_dump(mode="json")
    data["extraction"]["extractors"] = ["llm"]
    data["pipeline"]["arms"]["graph"]["hops"] = 2
    engine_config = validate_config(data)
    import json

    def extract(self, system, payload, *args, **kwargs):
        text = payload["passage"] if isinstance(payload, dict) and "passage" in payload else str(payload)
        names = [n for n in ("Alpha", "Beta", "Gamma") if n in text]
        entities = [{"name": n, "type": "component", "quote": n} for n in names]
        relations = []
        for left, right in (("Alpha", "Beta"), ("Beta", "Gamma")):
            sentence = f"{left} uses {right}"
            if sentence in text:
                relations.append({"subject": left, "predicate": "USES", "object": right, "quote": sentence})
        return json.dumps({"entities": entities, "relations": relations})

    monkeypatch.setattr(JsonModel, "call_text", extract)
    engine = Engine(engine_config)
    engines.append(engine)
    manifest = engine.ingest(
        [
            Document(id="ab", dataset=engine_config.dataset, fields={"body": "Alpha uses Beta"}),
            Document(id="bc", dataset=engine_config.dataset, fields={"body": "Beta uses Gamma"}),
            Document(id="g", dataset=engine_config.dataset, fields={"body": "Gamma"}),
        ]
    )
    assert manifest["relations"] == 2
    result = engine.search("Alpha")
    assert set(graph_ids(result)) == {"ab", "bc", "g"}
    paths = result.trace["steps"]
    assert next(s for s in paths if s["arm"] == "graph")["paths"] == 2  # Alpha-Beta, Alpha-Beta-Gamma
    without_bridge = engine.search(SearchRequest(query="Alpha", exclude_ids=["bc"]))
    assert "g" not in set(graph_ids(without_bridge))


def test_failed_loads_never_move_the_pointer(config, documents, neo4j_env, engines, monkeypatch):
    engine = Engine(on_neo4j(config, neo4j_env))
    engines.append(engine)
    first = engine.ingest(documents)
    neo = engine.backends["graph"]
    # 1. the graph load itself fails: typed error, old generation stays active everywhere
    from su_multi_arm.backends.neo4j.graph import Neo4jGraph

    original = Neo4jGraph.upsert

    def broken(self, gen, entities, relations, memberships):
        raise BackendError("neo4j", "upsert", "injected failure")

    monkeypatch.setattr(Neo4jGraph, "upsert", broken)
    with pytest.raises(IngestError, match="injected failure"):
        engine.ingest(documents, replace=True)
    assert neo.active_generation(first["dataset"]) == first["generation"] == engine.manifest()["generation"]
    monkeypatch.setattr(Neo4jGraph, "upsert", original)
    # 2. the load succeeds but the authoritative CURRENT switch fails: the pointer is moved back
    registry = engine.write_path.registry

    def refuse(manifest):
        raise OSError("injected CURRENT failure")

    monkeypatch.setattr(registry, "activate", refuse)
    with pytest.raises(IngestError, match="injected CURRENT failure") as info:
        engine.ingest(documents, replace=True)
    failed = info.value.failure["generation"]
    assert neo.active_generation(first["dataset"]) == first["generation"]
    assert f"{first['dataset']}/{failed}/" not in neo.generations()  # cleanup removed the failed load
    assert info.value.failure["left_for_cleanup"] == []
