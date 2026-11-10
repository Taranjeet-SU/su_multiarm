"""Synthetic filtered-scroll reconciliation and no-fallback tests."""

import copy
import importlib.util
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location(
    "filtered_capture_test", SCRIPTS / "filtered_incident_capture.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
INDEX = "logs-synthetic-prod-api-2026.01.01"
SCOPE = {"index": INDEX, "day": "2026-01-01", "tenant": "synthetic", "env": "prod", "service": "api"}
COMPILED = {
    "policy_id": "synthetic-policy",
    "query_id": "synthetic-query",
    "request": {
        "query": {"bool": {"should": [{"match_phrase": {"message": "failed"}}], "minimum_should_match": 1}},
        "aggs": {"source_scope": {"global": {}}},
        "size": 0,
        "_source": False,
    },
}


def page(ids, total=2, source=10):
    return {
        "_scroll_id": "synthetic-owned-scroll",
        "timed_out": False,
        "_shards": {"total": 1, "successful": 1, "failed": 0},
        "hits": {
            "total": {"value": total, "relation": "eq"},
            "hits": [
                {"_index": INDEX, "_id": i, "_source": {"message": "private fixture value"}} for i in ids
            ],
        },
        "aggregations": {"source_scope": {"doc_count": source}},
    }


def validate(response, compiled, scope):
    selected = m.exact_total(response)
    source = response["aggregations"]["source_scope"]["doc_count"]
    if selected > source:
        raise m.CaptureError("invalid source count")
    return {
        "source_total": source,
        "selected_total": selected,
        "policy_id": compiled["policy_id"],
        "query_id": compiled["query_id"],
    }


class Client:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []
        self.cleared = []

    def request(self, method, path, body, **kwargs):
        self.calls.append((method, path, body))
        return next(self.pages)

    def scroll(self, sid, keepalive):
        return next(self.pages)

    def clear_scroll(self, sid):
        self.cleared.append(sid)


def run(tmp_path, pages, compiled=None, sanitizer=None):
    client, emitted = Client(pages), []

    def capture():
        return m.capture_filtered_seed(
            client,
            SCOPE,
            compiled or COMPILED,
            validate,
            emitted.append,
            sanitizer or (lambda hit: {"id": hit["_id"], "message": "MASKED"}),
            work_dir=tmp_path,
        )

    return capture, client, emitted


def test_filtered_scroll_exact_subset_and_only_sanitized_output(tmp_path):
    capture, client, emitted = run(tmp_path, [page(["a"]), page(["b"])])
    report = capture()
    assert report["source_total"] == 10 and report["selected_total"] == report["unique_source_ids"] == 2
    assert report["complete_for_selected_seeds"] and not report["complete_for_scope"]
    assert not report["full_capture"] and report["context_pending"]
    assert all(e["record"]["message"] == "MASKED" for e in emitted)
    request = client.calls[0][2]
    assert request["query"] == COMPILED["request"]["query"] and request["size"] == 1000
    assert request["_source"]["includes"] == m.SOURCE_FIELDS
    assert client.cleared == ["synthetic-owned-scroll"]


@pytest.mark.parametrize(
    "failure", ["duplicate", "early_end", "count_drift", "wrong_index", "shards", "timeout", "source_count"]
)
def test_incomplete_filtered_capture_fails_and_clears_owned_scroll(tmp_path, failure):
    first, second = page(["a"]), page(["b"])
    if failure == "duplicate":
        second = page(["a"])
    if failure == "early_end":
        second = page([])
    if failure == "count_drift":
        second = page(["b"], total=3)
    if failure == "wrong_index":
        second["hits"]["hits"][0]["_index"] = "other"
    if failure == "shards":
        second["_shards"]["failed"] = 1
    if failure == "timeout":
        second["timed_out"] = True
    if failure == "source_count":
        first["aggregations"]["source_scope"]["doc_count"] = 1
    capture, client, _ = run(tmp_path, [first, second])
    with pytest.raises(m.CaptureError):
        capture()
    assert len(client.calls) == 1
    assert client.cleared == ["synthetic-owned-scroll"]


def test_unfiltered_query_rejected_without_transport(tmp_path):
    compiled = copy.deepcopy(COMPILED)
    compiled["request"]["query"] = {"match_all": {}}
    capture, client, _ = run(tmp_path, [], compiled)
    with pytest.raises(m.CaptureError, match="positive seed"):
        capture()
    assert client.calls == []


def test_zero_selected_is_exact_subset_not_full_capture(tmp_path):
    capture, _, emitted = run(tmp_path, [page([], total=0)])
    report = capture()
    assert report["selected_total"] == 0 and report["source_total"] == 10 and emitted == []
    assert report["complete_for_selected_seeds"] and not report["full_capture"]


def test_sanitizer_failure_never_emits_raw_record(tmp_path):
    def failing(hit):
        raise RuntimeError("synthetic privacy failure")

    capture, client, emitted = run(tmp_path, [page(["a"])], sanitizer=failing)
    with pytest.raises(RuntimeError, match="privacy"):
        capture()
    assert not emitted and client.cleared


def test_windowed_capture_preserves_both_query_and_population_bounds(tmp_path):
    compiled = copy.deepcopy(COMPILED)
    window = {"start": "2026-01-01T01:00:00Z", "end": "2026-01-01T02:00:00Z"}
    bounded = {"range": {"@timestamp": {"gte": window["start"], "lt": window["end"]}}}
    compiled["window"] = window
    compiled["request"]["query"] = {"bool": {"must": [compiled["request"]["query"]], "filter": [bounded]}}
    compiled["request"]["aggs"]["source_scope"] = {"global": {}, "aggs": {"window": {"filter": bounded}}}
    capture, client, _ = run(tmp_path, [page(["a", "b"])], compiled)
    assert capture()["records_fetched"] == 2
    assert client.calls[0][2]["query"] == compiled["request"]["query"]
    bad = copy.deepcopy(compiled)
    bad["request"]["aggs"]["source_scope"] = {"global": {}}
    failed, untouched, _ = run(tmp_path, [], bad)
    with pytest.raises(m.CaptureError, match="same-window"):
        failed()
    assert untouched.calls == []
    bad = copy.deepcopy(compiled)
    bad["window"]["start"] = "2025-12-31T00:00:00Z"
    failed, untouched, _ = run(tmp_path, [], bad)
    with pytest.raises(m.CaptureError, match="window differs"):
        failed()
    assert untouched.calls == []
