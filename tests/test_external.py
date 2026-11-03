"""Engine-level external tests: PostgreSQL through the v2 engine (``backends/postgres/``)."""

import os
import uuid

import pytest

from su_multi_arm import Engine, SearchRequest

# The neo4j cases that lived here (v1 adapters/neo4j.py, retired) are replaced by
# tests/conformance/test_neo4j_conformance.py and tests/services/test_neo4j_engine.py.


@pytest.mark.external
def test_postgres_engine_end_to_end(config, documents):
    """Replaces the v1 ``test_postgres_real``: catalog, records, ledger, sessions, cache, dense and
    lexical on PostgreSQL through the v2 engine; communities stay on the local sqlite backend."""
    from su_multi_arm.core.config import validate_config

    if not os.environ.get("SU_TEST_POSTGRES_DSN"):
        pytest.skip("set SU_TEST_POSTGRES_DSN for a disposable integration DB")
    data = config.model_dump(mode="json", exclude_none=True)
    prefix = "e2e_" + uuid.uuid4().hex[:8]
    data["backends"]["pg"] = {"kind": "postgres", "dsn_env": "SU_TEST_POSTGRES_DSN", "table_prefix": prefix}
    data["storage"].update({"catalog": "pg", "records": "pg", "ledger": "pg", "sessions": "pg", "cache": "pg"})
    for arm in ("dense", "lexical", "structured"):
        data["pipeline"]["arms"][arm]["backend"] = "pg"
    for arm in ("graph", "community"):  # graph is planned on postgres; these arms need the local catalog
        data["pipeline"]["arms"].pop(arm, None)
    engine = Engine(validate_config(data))
    try:
        manifest = engine.ingest(documents)
        assert manifest["status"] == "complete" and manifest["projections"]["vector"]["verified"] is True
        first = manifest["generation"]
        result = engine.search("exemption certificate")
        for arm in ("dense", "lexical"):
            assert "kb:certificate" in {h.document_id for h in result.arm_hits[arm]}
        filtered = engine.search(SearchRequest(query="certificate", filters={"source": "case"}))
        assert filtered.hits and all(h.source == "case" for h in filtered.hits)
        second = engine.ingest([], delete_ids=["kb:certificate"])["generation"]
        assert "kb:certificate" not in {h.document_id for h in engine.search("certificate").hits}
        registry = engine.backends["pg"].generation_registry()
        assert registry.current()["generation"] == second and first in registry.generations()  # old generation retained
    finally:
        engine.backends["pg"].drop_all()
        engine.backends["pg"].close()
