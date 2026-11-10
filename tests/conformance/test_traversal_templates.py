"""Typed traversal templates mean the same thing on every graph backend (plan §3.8 graph arm, Phase 3
"traversal extensions"): one fixture, one template library, identical signatures (node ids,
predicates, edge kinds, span offsets, provenance) on the in-memory fake, sqlite and the embedded kuzu
adapter; neo4j joins under ``external`` when its test server answers."""

from __future__ import annotations

import os
import uuid

import pytest

from su_multi_arm.backends.memory import MemoryBackend
from su_multi_arm.backends.sqlite import SqliteBackend
from su_multi_arm.testing import backend_conformance as kit


def _signatures(adapter):
    return kit.run_traversal_cases(adapter)


def test_identical_semantics_on_memory_sqlite_and_kuzu(tmp_path):
    pytest.importorskip("kuzu")
    from su_multi_arm.backends.kuzu import KuzuBackend

    kuzu = KuzuBackend(tmp_path / "t.kuzu", buffer_pool_mb=64)
    try:
        results = {
            "memory": _signatures(MemoryBackend()),
            "sqlite": _signatures(SqliteBackend(tmp_path / "s")),
            "kuzu": _signatures(kuzu),
        }
    finally:
        kuzu.close()
    assert results["memory"] == results["sqlite"] == results["kuzu"]
    kit.check_traversal_templates(MemoryBackend())  # and they are the documented results


@pytest.mark.external
def test_identical_semantics_on_neo4j(tmp_path):
    neo4j = pytest.importorskip("neo4j")
    from su_multi_arm.backends.neo4j import Neo4jBackend

    uri = os.environ.get("SU_TEST_NEO4J_URI", "bolt://127.0.0.1:17687")
    password = os.environ.get("SU_TEST_NEO4J_PASSWORD", "su_test_password")
    try:
        driver = neo4j.GraphDatabase.driver(uri, auth=("neo4j", password), connection_timeout=5)
        driver.verify_connectivity()
        driver.close()
    except (neo4j.exceptions.Neo4jError, neo4j.exceptions.DriverError, OSError) as exc:
        if os.environ.get("SU_TEST_NEO4J_URI"):
            raise
        pytest.skip(f"neo4j test server not reachable: {exc}")
    backend = Neo4jBackend(uri, "neo4j", password, namespace=f"t{uuid.uuid4().hex[:12]}", timeout_seconds=20)
    try:
        assert _signatures(backend) == _signatures(MemoryBackend())
    finally:
        backend.drop_namespace()
        backend.close()


def test_an_attached_graph_layer_traverses_like_its_store(tmp_path):
    """Unified merge M1b (§6.4): a graph served as an attached layer (``LayerBackend``/``MappedGraph``)
    answers a unified generation with its store generation's traversals, unchanged; writes refused."""
    from su_multi_arm.arms.traversal import run_template
    from su_multi_arm.core.config_graph import GraphIntent
    from su_multi_arm.core.errors import BackendError
    from su_multi_arm.core.types import GenerationId
    from su_multi_arm.ingestion.layers import MappedGraph

    store = SqliteBackend(tmp_path / "s")
    expected = _signatures(store)  # loads the fixture into the store generation (kit.GEN3)
    unified = GenerationId(dataset=kit.GEN3.dataset, generation="c" * 32, index_format=kit.GEN3.index_format, fingerprint="d" * 64)
    graph = MappedGraph(store.graph, lambda gen: kit.GEN3 if gen == unified else gen, "support_graph")
    templates = kit.traversal_templates()
    got = [
        kit.traversal_signature(run_template(graph, unified, name, templates[name], GraphIntent(template=name, seeds=list(seeds)), scope))
        for name, seeds, scope in kit.TRAVERSAL_CASES
    ]
    assert got == expected
    with pytest.raises(BackendError, match="attached read-only"):
        graph.upsert(unified, [], [], [])
