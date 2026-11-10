"""Source transport allow-list (addendum A1 §3.1.1, slice L1): every non-listed ``(method, path)`` is
refused before any I/O; ``DELETE`` only on the two close rows with ids this source opened and the
cursor lists; script bodies refused; no redirects; bounded keepalive. Synthetic fake cluster only."""

from __future__ import annotations

import itertools
import re

import httpx
import pytest

from su_multi_arm.sources.errors import SourceRequestRefused, SourceUnavailable
from su_multi_arm.sources.transport import AllowList, SourceTransport
from su_multi_arm.testing.fixtures.logs import FakeLogCluster
from tests.sources_support import wired

KNOWN = {"t001-prod-gateway-2026.01.05"}
IDX = "t001-prod-gateway-2026.01.05"
METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")
PATHS = (
    "/",
    f"/{IDX}/_search",
    f"/{IDX}/_count",
    "/t001-*/_count",
    f"/{IDX}/_mapping",
    "/_resolve/index/t001-*",
    "/t001-*/_alias",
    "/_search/scroll",
    f"/{IDX}/_pit",
    f"/{IDX}/_search/point_in_time",
    "/_pit",
    "/_search/point_in_time",
    "/_security/user/_has_privileges",
    "/_plugins/_security/api/account",
    # never listed
    f"/{IDX}/_doc/1",
    f"/{IDX}/_update/1",
    f"/{IDX}/_delete_by_query",
    f"/{IDX}/_update_by_query",
    f"/{IDX}/_bulk",
    "/_bulk",
    f"/{IDX}",
    f"/{IDX}/_settings",
    f"/{IDX}/_close",
    f"/{IDX}/_forcemerge",
    "/_all/_search",
    "/_search",
    "/t001-*/_search",
    "/_cluster/settings",
    "/_cluster/health",
    "/_cat/indices",
    "/_snapshot/repo",
    "/_reindex",
    "/_scripts/s1",
    "/_search/scroll/_all",
    "/_pit/_all",
    "/_search/point_in_time/_all",
    "/_security/user/reader",
    "/_tasks",
    "/_nodes",
    "/_ingest/pipeline/p",
    f"/{IDX}/_search/template",
    "/_msearch",
    f"/{IDX}/_msearch",
    "/_sql",
    f"/{IDX}/_explain/1",
    f"/{IDX}/_termvectors/1",
)

# Independent oracle: the allow-list table of §3.1.1 written out for these paths.
EXPECTED_ALLOWED = {
    ("GET", "/"),
    ("GET", f"/{IDX}/_search"), ("POST", f"/{IDX}/_search"),
    ("GET", f"/{IDX}/_count"), ("POST", f"/{IDX}/_count"),
    ("GET", "/t001-*/_count"), ("POST", "/t001-*/_count"),
    ("GET", f"/{IDX}/_mapping"),
    ("GET", "/_resolve/index/t001-*"),
    ("GET", "/t001-*/_alias"),
    ("POST", "/_search/scroll"), ("DELETE", "/_search/scroll"),
    ("POST", f"/{IDX}/_pit"), ("POST", f"/{IDX}/_search/point_in_time"),
    ("DELETE", "/_pit"), ("DELETE", "/_search/point_in_time"),
    ("GET", "/_security/user/_has_privileges"), ("POST", "/_security/user/_has_privileges"),
    ("GET", "/_plugins/_security/api/account"),
}


def transport(cluster: FakeLogCluster, keepalive_max: float = 120.0) -> SourceTransport:
    t = SourceTransport("http://source.invalid:9200", http_transport=cluster.transport(), keepalive_max_seconds=keepalive_max)
    t.register_indices(KNOWN)
    return t


def empty_cluster() -> FakeLogCluster:
    return FakeLogCluster(indices={IDX: []}, mappings={IDX: {"mappings": {"properties": {}}}})


@pytest.mark.parametrize("method, path", [(m, p) for m, p in itertools.product(METHODS, PATHS) if (m, p) not in EXPECTED_ALLOWED])
def test_every_non_listed_method_and_path_is_refused_before_io(method, path):
    cluster = empty_cluster()
    t = transport(cluster)
    with pytest.raises(SourceRequestRefused) as caught:
        t.request(method, path, body={"query": {"match_all": {}}} if method in {"POST", "PUT"} else None)
    assert cluster.requests == [], "a refused request reached the server"
    assert t.sent == 0
    assert caught.value.method == method


def test_the_oracle_rows_are_accepted_by_the_check():
    allow = AllowList(120)
    bodies = {
        "/_search/scroll": None,
        "/_pit": None,
        "/_search/point_in_time": None,
    }
    for method, path in EXPECTED_ALLOWED:
        if path in bodies and method == "DELETE" or path == "/_search/scroll":
            continue  # need own ids; covered below
        params = {"keep_alive": "1m"} if path.endswith(("_pit", "point_in_time")) else None
        allow.check(method, path, params, None, known_indices=KNOWN, own_ids=())


@pytest.mark.parametrize(
    "path",
    [
        "/t001-prod-gateway-2026.01.06/_search",  # not discovered
        "/T001-PROD/_search",
        "/_all/_mapping",
        "/.security/_search",
        "/%5Fall/_search",
        "/%2e%2e/_search",
        f"/{IDX}/%2e%2e/_bulk",
        f"/{IDX}/_search%3Fq=x",
        "/%252e/_search",
        f"//{IDX}/_search",
        f"/{IDX}/_search?scroll=1m",
        f"/{IDX}/_search#x",
        f"/{IDX}/_search ",
        "/_resolve/index/_all",
        "/_resolve/index/-t001*",
        "/_all/_count",
        "/.ds-logs/_count",
        "relative/_search",
    ],
)
def test_path_tricks_and_undiscovered_indices_are_refused(path):
    cluster = empty_cluster()
    with pytest.raises(SourceRequestRefused):
        transport(cluster).request("POST" if "_search" in path or "_count" in path else "GET", path, body=None)
    assert cluster.requests == []


@pytest.mark.parametrize(
    "body",
    [
        {"query": {"script": {"script": {"source": "true"}}}},
        {"query": {"function_score": {"query": {"match_all": {}}, "script_score": {"script": "1"}}}},
        {"query": {"function_score": {"functions": [{"script_score": {"script": {"id": "stored"}}}]}}},
        {"query": {"match_all": {}}, "script_fields": {"x": {"script": "1"}}},
        {"query": {"match_all": {}}, "runtime_mappings": {"x": {"type": "keyword"}}},
        {"query": {"match_all": {}}, "aggs": {"x": {"terms": {"field": "level"}}}},
        {"query": {"match_all": {}}, "_update": {}},
        {"query": {"match_all": {}}, "from": 10},
        {"query": {"match_all": {}}, "size": 50_000},
        {"query": {"match_all": {}}, "_source": {"excludes": ["x"]}},
        {"query": {"match_all": {}}, "pit": {"id": "someone-elses"}},
        ["not", "an", "object"],
    ],
)
def test_search_bodies_with_scripts_or_unlisted_keys_are_refused(body):
    cluster = empty_cluster()
    with pytest.raises(SourceRequestRefused):
        transport(cluster).request("POST", f"/{IDX}/_search", body=body)
    assert cluster.requests == []


def test_count_body_takes_only_a_query_and_random_score_without_script_is_accepted():
    cluster = empty_cluster()
    t = transport(cluster)
    with pytest.raises(SourceRequestRefused):
        t.request("POST", f"/{IDX}/_count", body={"query": {"match_all": {}}, "size": 1})
    t.request("POST", f"/{IDX}/_search", body={"query": {"function_score": {"query": {"match_all": {}}, "random_score": {"seed": 1, "field": "_seq_no"}}}, "size": 1})
    assert [r.path for r in cluster.requests] == [f"/{IDX}/_search"]


@pytest.mark.parametrize(
    "params",
    [{"scroll": "10m"}, {"scroll": "1h"}, {"scroll": "forever"}, {"q": "level:error"}, {"size": "99999"}, {"filter_path": "a b"}, {"_source_excludes": "x"}, {"allow_partial_search_results": "true"}],
)
def test_query_parameters_are_allow_listed_and_keepalive_is_bounded(params):
    cluster = empty_cluster()
    with pytest.raises(SourceRequestRefused):
        transport(cluster, keepalive_max=120).request("POST", f"/{IDX}/_search", params=params, body={"query": {"match_all": {}}})
    assert cluster.requests == []


def test_pit_open_needs_a_bounded_keep_alive():
    cluster = empty_cluster()
    t = transport(cluster)
    for params in (None, {"keep_alive": "30m"}):
        with pytest.raises(SourceRequestRefused):
            t.request("POST", f"/{IDX}/_pit", params=params)
    assert cluster.requests == []


def test_delete_only_closes_ids_this_source_opened_and_the_cursor_lists():
    cluster = FakeLogCluster(indices={IDX: [{"_id": f"d{i}", "_source": {"@timestamp": "2026-01-05T00:00:00Z", "message": "m"}} for i in range(5)]}, mappings={IDX: {"mappings": {"properties": {}}}})
    t = transport(cluster)
    cluster.scrolls["foreign-1"] = {"rest": [], "size": 1, "pages": 1}
    _, payload = t.request("POST", f"/{IDX}/_search", params={"scroll": "1m"}, body={"query": {"match_all": {}}, "size": 2})
    mine = payload["_scroll_id"]
    sent = len(cluster.requests)
    refused = [
        ({"scroll_id": "foreign-1"}, ["foreign-1"]),  # listed but not opened here
        ({"scroll_id": mine}, []),  # opened here but not listed by the cursor
        ({"scroll_id": "_all"}, ["_all"]),
        ({"scroll_id": [mine, "foreign-1"]}, [mine, "foreign-1"]),
        ({"scroll_id": []}, []),
        ({"scroll_id": mine, "extra": 1}, [mine]),
        (None, [mine]),
    ]
    for body, listed in refused:
        with pytest.raises(SourceRequestRefused):
            t.request("DELETE", "/_search/scroll", body=body, close_ids=listed)
    assert len(cluster.requests) == sent
    with pytest.raises(SourceRequestRefused):
        t.request("POST", "/_search/scroll", body={"scroll": "1m", "scroll_id": "foreign-1"})
    t.request("DELETE", "/_search/scroll", body={"scroll_id": [mine]}, close_ids=[mine])
    assert "foreign-1" in cluster.scrolls and mine not in cluster.scrolls
    assert mine not in t.own_ids


def test_pit_close_rows_check_ownership_and_shape():
    cluster = empty_cluster()
    t = transport(cluster)
    _, payload = t.request("POST", f"/{IDX}/_pit", params={"keep_alive": "1m"})
    pit = payload["id"]
    for path, body in (("/_pit", {"id": "other"}), ("/_pit", {"id": [pit]}), ("/_search/point_in_time", {"pit_id": pit}), ("/_pit", {"id": "_all"})):
        with pytest.raises(SourceRequestRefused):
            t.request("DELETE", path, body=body, close_ids=[pit, "other", "_all"])
    t.request("DELETE", "/_pit", body={"id": pit}, close_ids=[pit])
    assert cluster.pits == set()


def test_redirects_are_never_followed():
    cluster = empty_cluster()
    cluster.redirect_paths = ("/",)
    t = transport(cluster)
    with pytest.raises(SourceUnavailable, match="redirect"):
        t.request("GET", "/")
    assert len(cluster.requests) == 1


def test_source_url_with_credentials_is_refused_and_secrets_never_leak():
    from su_multi_arm.sources.errors import SourceAuthError

    with pytest.raises(SourceAuthError):
        SourceTransport("http://reader:hunter2@source.invalid:9200")
    secret = "s3cr3t-synthetic-password"
    source, cluster, _ = wired(env={"SU_TEST_SOURCE_URL": "http://source.invalid:9200", "SU_TEST_USER": "reader", "SU_TEST_PASS": secret}, auth="basic", username_env="SU_TEST_USER", password_env="SU_TEST_PASS")
    source.capabilities()
    assert cluster.requests[0].headers["authorization"].startswith("Basic ")
    with pytest.raises(SourceRequestRefused) as caught:
        source.transport.request("DELETE", "/t001-prod-gateway-2026.01.05")
    texts = [str(caught.value), repr(source), repr(source.transport), *(repr(r) for r in source.transport.records)]
    assert not any(secret in text or "source.invalid" in text for text in texts)


def test_unit_paths_from_other_processes_are_refused():
    source, cluster, _ = wired()
    from su_multi_arm.sources import SourceUnit

    with pytest.raises(SourceRequestRefused):
        list(source.pull(SourceUnit(id="x", index=IDX, query_sha="q")))
    assert cluster.requests == []


def test_route_table_has_no_unanchored_pattern():
    from su_multi_arm.sources.transport import ROUTES

    for route in ROUTES:
        assert route.pattern.pattern.startswith("^") and route.pattern.pattern.endswith("$")
        assert not re.search(r"\.\*", route.pattern.pattern)
        assert "DELETE" not in route.methods or route.row in {7, 9}


def test_transport_does_not_follow_env_proxies(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:1")
    cluster = empty_cluster()
    t = transport(cluster)
    assert t._client._trust_env is False
    assert isinstance(t._client, httpx.Client)
