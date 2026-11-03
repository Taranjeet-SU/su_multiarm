"""Conformance kit over the neo4j adapter against a real Neo4j (plan §3.5 ``neo4j`` row, Phase 1
acceptance "graph passes the predicate-supported one- and two-hop traversal on sqlite and neo4j",
§6.1 real-service tier). Marked ``external``: reads ``SU_TEST_NEO4J_URI`` / ``SU_TEST_NEO4J_PASSWORD``
(default: the throwaway stack in ``tests/services/docker-compose.yml``, Bolt port 17687). Every
adapter instance uses a fresh namespace and drops only that namespace afterwards."""

from __future__ import annotations

import os
import uuid

import pytest

from su_multi_arm.backends import capabilities as caps
from su_multi_arm.core.errors import BackendError
from su_multi_arm.core.types import Membership, TraversalSpec
from su_multi_arm.testing import backend_conformance as kit

pytestmark = pytest.mark.external

DEFAULT_URI = "bolt://127.0.0.1:17687"
DEFAULT_PASSWORD = "su_test_password"


@pytest.fixture(scope="session")
def neo4j_server():
    neo4j = pytest.importorskip("neo4j")
    configured = os.environ.get("SU_TEST_NEO4J_URI")
    uri = configured or DEFAULT_URI
    password = os.environ.get("SU_TEST_NEO4J_PASSWORD", DEFAULT_PASSWORD)
    driver = neo4j.GraphDatabase.driver(uri, auth=("neo4j", password), connection_timeout=5)
    try:
        driver.verify_connectivity()
    except (neo4j.exceptions.Neo4jError, neo4j.exceptions.DriverError, OSError) as exc:
        driver.close()
        if configured:
            raise  # an explicitly configured server that does not answer is a failure, not a skip
        pytest.skip(f"SU_TEST_NEO4J_URI unset and the default test stack is not reachable: {exc}")
    driver.close()
    return uri, password


@pytest.fixture
def adapter_factory(neo4j_server):
    from su_multi_arm.backends.neo4j import Neo4jBackend

    uri, password = neo4j_server
    clock = {"now": 100.0}
    made = []

    def make(**overrides):
        backend = Neo4jBackend(uri, "neo4j", password, namespace=f"t{uuid.uuid4().hex[:12]}", timeout_seconds=20, clock=lambda: clock["now"], **overrides)
        made.append(backend)
        return backend

    make.clock = clock
    make.kind = "neo4j"
    yield make
    for backend in made:
        backend.drop_namespace()
        backend.close()


def test_server_is_real_and_versioned(adapter_factory):
    info = adapter_factory().ping()
    assert info["agent"] and info["agent"].startswith("Neo4j/")


def test_only_graph_is_declared_and_implemented(adapter_factory):
    adapter = adapter_factory()
    assert kit.check_protocols(adapter) == {"graph"}
    assert caps.status_of("neo4j", "graph") == "contracts" and caps.serves("neo4j", "graph", "memberships")
    assert not caps.flags("neo4j", "graph")["predicate_scope"]


def test_graph_conformance(adapter_factory):
    kit.check_graph(adapter_factory())


def test_run_all(adapter_factory):
    assert kit.run_all(adapter_factory, adapter_factory.clock) == {"graph": "passed"}


def test_merge_is_idempotent(adapter_factory):
    adapter = adapter_factory()
    entities, relations = kit.graph_fixture()
    memberships = [Membership(community_id="k1", member_id="d6", basis="entity")]
    adapter.graph.upsert(kit.GEN, entities, relations, memberships)
    before = adapter.graph.traverse(kit.GEN, TraversalSpec(seed_entity_ids=["aacta"], hops=3, edge_kinds=["fact", "co_occurrence"]), None)
    mentions = adapter.graph.mentions(kit.GEN, ["film", "gala"], None, 100)
    adapter.graph.upsert(kit.GEN, entities, relations, memberships)
    assert adapter.graph.traverse(kit.GEN, TraversalSpec(seed_entity_ids=["aacta"], hops=3, edge_kinds=["fact", "co_occurrence"]), None) == before
    assert adapter.graph.mentions(kit.GEN, ["film", "gala"], None, 100) == mentions
    assert len(adapter.graph.memberships(kit.GEN)) == 1


def test_rejected_upsert_is_atomic(adapter_factory):
    adapter = adapter_factory()
    entities, relations = kit.graph_fixture()
    adapter.graph.upsert(kit.GEN, entities[:3], relations[:1], [])
    extra = entities[5]  # paris: written in the same call as a dangling relation
    with pytest.raises(BackendError, match="endpoints"):
        adapter.graph.upsert(kit.GEN, [extra], [relations[0].model_copy(update={"object_id": "ghost"})], [])
    assert adapter.graph.mentions(kit.GEN, ["paris"], None, 10) == []


def test_generation_isolation_pointer_and_delete(adapter_factory):
    adapter = adapter_factory()
    entities, relations = kit.graph_fixture()
    adapter.graph.upsert(kit.GEN, entities, relations, [])
    adapter.graph.upsert(kit.GEN2, entities[:2], relations[:1], [])
    spec = TraversalSpec(seed_entity_ids=["aacta"], hops=2)
    assert [p.nodes for p in adapter.graph.traverse(kit.GEN2, spec, None)] == [["aacta", "film"]]
    assert len(adapter.graph.traverse(kit.GEN, spec, None)) == 2
    assert adapter.active_generation(kit.GEN.dataset) is None
    unloaded = kit.GEN.model_copy(update={"generation": "c" * 32})
    with pytest.raises(BackendError, match="no committed load"):
        adapter.activate_generation(unloaded)
    assert adapter.active_generation(kit.GEN.dataset) is None  # a refused activation never moves the pointer
    adapter.activate_generation(kit.GEN)
    assert adapter.active_generation(kit.GEN.dataset) == kit.GEN.generation
    with pytest.raises(BackendError, match="active"):
        adapter.delete_generation(kit.GEN)  # the pointer protects it in every process
    adapter.protect(kit.GEN2)
    with pytest.raises(BackendError, match="pinned"):
        adapter.delete_generation(kit.GEN2)
    adapter.unprotect(kit.GEN2)
    adapter.delete_generation(kit.GEN2)
    with pytest.raises(BackendError, match="unknown generation"):
        adapter.graph.traverse(kit.GEN2, spec, None)
    assert len(adapter.graph.traverse(kit.GEN, spec, None)) == 2
    assert adapter.generations() == [kit.GEN.prefix()]
    adapter.deactivate_generation(kit.GEN2)  # names another generation: no effect
    assert adapter.active_generation(kit.GEN.dataset) == kit.GEN.generation
    adapter.deactivate_generation(kit.GEN)
    assert adapter.active_generation(kit.GEN.dataset) is None


def test_namespaces_are_isolated(adapter_factory):
    first, second = adapter_factory(), adapter_factory()
    entities, relations = kit.graph_fixture()
    first.graph.upsert(kit.GEN, entities, relations, [])
    with pytest.raises(BackendError, match="unknown generation"):
        second.graph.traverse(kit.GEN, TraversalSpec(seed_entity_ids=["aacta"], hops=1), None)
    second.drop_namespace()
    assert len(first.graph.traverse(kit.GEN, TraversalSpec(seed_entity_ids=["aacta"], hops=1), None)) == 1


def test_unreachable_server_is_a_typed_error():
    pytest.importorskip("neo4j")
    from su_multi_arm.backends.neo4j import Neo4jBackend

    backend = Neo4jBackend("bolt://127.0.0.1:1", "neo4j", "x", namespace="unreachable", timeout_seconds=2)
    with pytest.raises(BackendError) as info:
        backend.graph.traverse(kit.GEN, TraversalSpec(seed_entity_ids=["a"], hops=1), None)
    assert info.value.adapter == "neo4j"
    with pytest.raises(BackendError):
        backend.ping()


def test_wrong_password_is_a_typed_error(neo4j_server):
    from su_multi_arm.backends.neo4j import Neo4jBackend

    uri, _ = neo4j_server
    backend = Neo4jBackend(uri, "neo4j", "not-the-password", namespace="badauth", timeout_seconds=5)
    with pytest.raises(BackendError, match="neo4j"):
        backend.graph.memberships(kit.GEN)
