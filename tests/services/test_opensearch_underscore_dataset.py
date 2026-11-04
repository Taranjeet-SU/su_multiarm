"""O1 end to end on a real OpenSearch (``external``): a dataset and an index prefix that both
contain ``_`` build, activate (the activation reads the pointer back) and serve queries; synthetic
documents only. Uses ``SU_TEST_OPENSEARCH_URL`` (default stack in ``tests/services``)."""

from __future__ import annotations

import os
import secrets

import pytest

from service_probe import require_search_service
from su_multi_arm import Document, Engine, SearchRequest
from su_multi_arm.core.config import validate_config

pytestmark = pytest.mark.external


def test_an_underscore_dataset_builds_activates_and_serves(tmp_path, monkeypatch):
    pytest.importorskip("opensearchpy")
    url = require_search_service("opensearch")
    monkeypatch.setenv("SU_TEST_O1_HOSTS", url)
    prefix = f"su_test_o1_{secrets.token_hex(3)}"
    data = {
        "schema_version": 2,
        "dataset": "my_data",
        "backends": {"local": {"kind": "sqlite", "path": str(tmp_path / "state")}, "os": {"kind": "opensearch", "hosts_env": "SU_TEST_O1_HOSTS", "index_prefix": prefix}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 16}}},
        "pipeline": {"arms": {"lexical": {"backend": "os"}}},
    }
    engine = Engine(validate_config(data))
    try:
        documents = [Document(id=f"d{i}", dataset="my_data", title=f"synthetic note {i}", fields={"body": f"exemption certificate number {i}"}) for i in range(5)]
        manifest = engine.ingest(documents)
        os_backend = engine.backends["os"]
        assert os_backend.active_generation("my_data") == manifest["generation"]
        result = engine.search(SearchRequest(query="exemption certificate"))
        assert result.hits and result.generation == manifest["generation"]
    finally:
        engine.backends["os"].transport.request("DELETE", f"/{prefix}_*", params={"ignore_unavailable": "true"})
        assert os.environ.get("SU_TEST_O1_HOSTS")
