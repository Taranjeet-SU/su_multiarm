"""Native aggregation pushdown on real Lucene engines (``external``): ``records.aggregation: client``
and ``native`` give identical ``ScopeResult`` values for every parity spec, an ACL-hidden record never
contributes to an aggregate, and the native caps are reported, never silent
(``su_multi_arm.testing.aggregation_parity``). The whole records kit also runs natively through the
``opensearch_native`` / ``elasticsearch_native`` adapters of ``conftest.py``.

Reads ``SU_TEST_OPENSEARCH_URL`` / ``SU_TEST_ELASTICSEARCH_URL`` like the rest of the suite; each test
uses its own index prefix and deletes exactly the indices it created."""

from __future__ import annotations

import secrets

import pytest

from tests.conformance.conftest import elasticsearch_backend, opensearch_backend
from service_probe import require_search_service
from su_multi_arm.testing import aggregation_parity as parity

pytestmark = pytest.mark.external


@pytest.fixture(params=["opensearch", "elasticsearch"])
def pair(request):
    require_search_service(request.param)
    build = opensearch_backend if request.param == "opensearch" else elasticsearch_backend
    prefix = f"su_test_{secrets.token_hex(4)}"
    client, native = build(f"{prefix}_c"), build(f"{prefix}_n", aggregation="native")
    yield client, native
    for backend in (client, native):
        rows = backend.transport.request("GET", f"/_cat/indices/{backend.prefix}_*", params={"format": "json", "h": "index"})
        names = [row["index"] for row in rows or []]
        if names:
            backend.transport.request("DELETE", "/" + ",".join(names))
        backend.close()


def test_client_and_native_results_are_identical(pair):
    client, native = pair
    assert parity.check_parity(client, native) == 2 * len(parity.parity_specs())
    assert native.stats["records.execute.native"] > 0 and client.stats["records.execute.native"] == 0


def test_an_acl_hidden_record_never_contributes_to_a_native_aggregate(pair):
    parity.check_native_acl(pair[1])


def test_native_caps_are_reported_never_silent(pair):
    parity.check_native_bounds(pair[1])
