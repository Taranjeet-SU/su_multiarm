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
