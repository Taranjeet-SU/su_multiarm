"""Scale check of native aggregation (``external``, opt-in): ``SU_TEST_AGG_SCALE_DOCS`` synthetic log
records (e.g. 200000) in one records index; every question of ``scripts/bench_native_aggregation.py``
must agree between ``records.aggregation: client`` and ``native`` (float sums within 1e-9 relative;
the native id list is the client list's prefix, flagged truncated), and native must be faster in total.

This size is where a small fixture is not enough: OpenSearch 3.x searches a one-shard index in
concurrent segment slices, and a count-ordered ``terms`` reply then reports a nonzero
``doc_count_error_upper_bound`` (found by this check; the adapter now grows the request until the
engine reports 0). Unset variable: skipped. Uses ``SU_TEST_OPENSEARCH_URL`` / ``SU_TEST_ELASTICSEARCH_URL``;
the script creates and deletes its own random index prefix."""

from __future__ import annotations

import importlib.util
import os
import secrets
from pathlib import Path

import pytest

from service_probe import require_search_service

pytestmark = pytest.mark.external
DOCS = int(os.environ.get("SU_TEST_AGG_SCALE_DOCS") or 0)
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bench_native_aggregation.py"


def _bench():
    spec = importlib.util.spec_from_file_location("bench_native_aggregation", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.mark.skipif(not DOCS, reason="set SU_TEST_AGG_SCALE_DOCS (e.g. 200000) to run the native aggregation scale check")
@pytest.mark.parametrize("engine", ["opensearch", "elasticsearch"])
def test_native_agrees_with_client_and_is_faster_at_scale(engine):
    url = require_search_service(engine)
    bench = _bench()
    if engine == "opensearch":
        from su_multi_arm.backends.opensearch import OpenSearchBackend as Backend
    else:
        from su_multi_arm.backends.elasticsearch import ElasticsearchBackend as Backend  # type: ignore[assignment]
    backend = Backend(url, index_prefix=f"su_test_{secrets.token_hex(4)}", timeout_seconds=300, max_matched=DOCS + 1)
    try:
        data = bench.rows(DOCS)
        backend.catalog.put_documents(bench.GEN, [], [])
        for start in range(0, len(data), 5000):
            backend.records.upsert(bench.GEN, data[start : start + 5000], bench.SCHEMA)
        client_total = native_total = 0.0
        for label, spec in bench.questions():
            backend.aggregation = "client"
            seconds, client = bench.timed(lambda: backend.records.execute(bench.GEN, spec, list(bench.ACL)), 1)
            client_total += seconds
            backend.aggregation = "native"
            seconds, native = bench.timed(lambda: backend.records.execute(bench.GEN, spec, list(bench.ACL)), 1)
            native_total += seconds
            assert bench.close(bench.comparable(client), bench.comparable(native)), label
            assert native.matched_ids == client.matched_ids[: len(native.matched_ids)], label
            assert native.matched_ids_truncated == (client.total > backend.matched_ids_cap), label
        assert native_total < client_total
    finally:
        rows = backend.transport.request("GET", f"/_cat/indices/{backend.prefix}_*", params={"format": "json", "h": "index"})
        names = [row["index"] for row in rows or []]
        if names:
            backend.transport.request("DELETE", "/" + ",".join(names))
        backend.close()
