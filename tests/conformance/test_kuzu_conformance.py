"""Conformance kit over the embedded kuzu adapter (plan §3.5 graph contract). No service: the database
is a file under the pytest temporary directory. Skips only when the optional ``kuzu`` extra is absent."""

from __future__ import annotations

import pytest

from su_multi_arm.backends import capabilities as caps
from su_multi_arm.core.errors import BackendError, WriteConflict
from su_multi_arm.core.types import Membership, TraversalSpec
from su_multi_arm.testing import backend_conformance as kit

kuzu = pytest.importorskip("kuzu")


@pytest.fixture
def adapter_factory(tmp_path):
    from su_multi_arm.backends.kuzu import KuzuBackend

    clock = {"now": 100.0}
    made = []
    counter = {"n": 0}

    def make(path=None, namespace="t"):
        counter["n"] += 1
        backend = KuzuBackend(path or tmp_path / f"g{counter['n']}.kuzu", namespace=namespace, timeout_seconds=20, buffer_pool_mb=64, clock=lambda: clock["now"])
        made.append(backend)
        return backend

    make.clock = clock
    make.kind = "kuzu"
    yield make
    for backend in made:
        backend.close()


def test_only_graph_is_declared_and_implemented(adapter_factory):
    adapter = adapter_factory()
    assert kit.check_protocols(adapter) == {"graph"}
    assert caps.status_of("kuzu", "graph") == "contracts" and caps.serves("kuzu", "graph", "memberships")
    assert not caps.flags("kuzu", "graph")["predicate_scope"]


def test_graph_conformance(adapter_factory):
    kit.check_graph(adapter_factory())


def test_run_all(adapter_factory):
    assert kit.run_all(adapter_factory, adapter_factory.clock) == {"graph": "passed"}


def test_merge_is_idempotent(adapter_factory):
    adapter = adapter_factory()
    entities, relations = kit.graph_fixture()
    memberships = [Membership(community_id="k1", member_id="d6", basis="entity")]
    adapter.graph.upsert(kit.GEN, entities, relations, memberships)
    spec = TraversalSpec(seed_entity_ids=["aacta"], hops=3, edge_kinds=["fact", "co_occurrence"])
    before = adapter.graph.traverse(kit.GEN, spec, None)
    mentions = adapter.graph.mentions(kit.GEN, ["film", "gala"], None, 100)
    adapter.graph.upsert(kit.GEN, entities, relations, memberships)
    assert adapter.graph.traverse(kit.GEN, spec, None) == before
    assert adapter.graph.mentions(kit.GEN, ["film", "gala"], None, 100) == mentions
    assert len(adapter.graph.memberships(kit.GEN)) == 1


def test_rejected_upsert_is_atomic(adapter_factory):
    adapter = adapter_factory()
    entities, relations = kit.graph_fixture()
    adapter.graph.upsert(kit.GEN, entities[:3], relations[:1], [])
    with pytest.raises(BackendError, match="endpoints"):
        adapter.graph.upsert(kit.GEN, [entities[5]], [relations[0].model_copy(update={"object_id": "ghost"})], [])
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
    assert adapter.active_generation(kit.GEN.dataset) is None
    adapter.activate_generation(kit.GEN, expected=None)
    assert adapter.active_generation(kit.GEN.dataset) == kit.GEN.generation
    with pytest.raises(WriteConflict):
        adapter.activate_generation(kit.GEN2, expected=None)  # the pointer names GEN: stale base
    assert adapter.active_generation(kit.GEN.dataset) == kit.GEN.generation
    with pytest.raises(BackendError, match="active"):
        adapter.delete_generation(kit.GEN)
    adapter.delete_generation(kit.GEN2)
    with pytest.raises(BackendError, match="unknown generation"):
        adapter.graph.traverse(kit.GEN2, spec, None)
    assert len(adapter.graph.traverse(kit.GEN, spec, None)) == 2
    adapter.protect(kit.GEN2)
    with pytest.raises(BackendError, match="pinned"):
        adapter.delete_generation(kit.GEN2)
    adapter.deactivate_generation(kit.GEN)
    assert adapter.active_generation(kit.GEN.dataset) is None


def test_namespaces_share_one_database(adapter_factory, tmp_path):
    first = adapter_factory(tmp_path / "shared.kuzu", namespace="one")
    entities, relations = kit.graph_fixture()
    first.graph.upsert(kit.GEN, entities, relations, [])
    first.close()
    second = adapter_factory(tmp_path / "shared.kuzu", namespace="two")
    with pytest.raises(BackendError, match="unknown generation"):
        second.graph.traverse(kit.GEN, TraversalSpec(seed_entity_ids=["aacta"]), None)
    second.graph.upsert(kit.GEN, entities[:2], relations[:1], [])
    second.drop_namespace()
    second.close()
    third = adapter_factory(tmp_path / "shared.kuzu", namespace="one")  # reopened: the data persisted
    assert len(third.graph.traverse(kit.GEN, TraversalSpec(seed_entity_ids=["aacta"], hops=2), None)) == 2


def test_only_fixed_templates_run(adapter_factory):
    adapter = adapter_factory()
    with pytest.raises(BackendError, match="fixed template"):
        adapter.run("query", "MATCH (n) DETACH DELETE n")
