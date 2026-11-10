"""Optional incident compiler checks using only synthetic policies and responses."""
import copy
import importlib.util
import os
import json
from pathlib import Path

import pytest

location = os.environ.get("SU_INCIDENT_QUERY_PATH")
if not location:
    pytest.skip("private incident compiler not supplied", allow_module_level=True)
spec = importlib.util.spec_from_file_location("tested_incident_query", Path(location).resolve())
compiler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compiler)
PolicyError = compiler.PolicyError
compile_query = compiler.compile_query
count_scope = compiler.count_scope
identity = compiler.identity
validate_policy = compiler.validate_policy
validate_response = compiler.validate_response
validate_scope = compiler.validate_scope


POLICY = {
    "version": 1,
    "families": [{"id": "failure", "phrases": ["error", "failed"]}],
    "severity_values": ["ERROR", "WARN"],
    "http_status_min": 500,
    "http_status_max": 599,
    "context_seconds": 120,
    "context_phrases": ["connection established"],
}
PROPS = {
    "message": {"type": "text"},
    "level": {"type": "keyword"},
    "http": {"properties": {"response": {"properties": {"status_code": {"type": "integer"}}}}},
}
SCOPE = {
    "index": "test-service-2026.11.02",
    "day": "2026-11-02",
    "tenant": "test",
    "env": "dev",
    "service": "service",
}


def reply(compiled, selected=3, source=10):
    return {
        "timed_out": False,
        "_shards": {"total": 1, "successful": 1, "failed": 0},
        "hits": {"total": {"value": selected, "relation": "eq"}, "hits": []},
        "aggregations": {
            "source_scope": {"doc_count": source},
            "signals": {"buckets": {name: {"doc_count": selected} for name in compiled["family_ids"]}},
        },
    }


def test_policy_literal_and_context_semantics():
    policy = copy.deepcopy(POLICY)
    policy["families"][0]["phrases"].append('error* OR {"match_all":{}}')
    out = compile_query(policy, PROPS)
    phrases = out["request"]["query"]["bool"]["should"][0]["bool"]["should"]
    assert phrases[-1] == {"match_phrase": {"message": {"query": 'error* OR {"match_all":{}}', "slop": 0}}}
    assert "connection established" not in json.dumps(out["request"])
    assert out["request"]["_source"] is False
    assert out["request"]["size"] == 0
    assert "match_all" not in json.dumps(compile_query(POLICY, PROPS)["request"])


def test_mapping_records_missing_branches():
    out = compile_query(POLICY, {"message": {"type": "text"}})
    assert out["family_ids"] == ["phrase_failure"]
    assert out["signal_mapping"]["severity"]["used"] == []
    assert "level" in out["signal_mapping"]["severity"]["unavailable"]
    assert out["signal_mapping"]["http_status"]["used"] == []


@pytest.mark.parametrize(
    "message", [{}, {"type": "keyword"}, {"type": "text", "index": False}, {"type": "alias", "path": "other"}]
)
def test_phrase_mapping_fails_closed(message):
    with pytest.raises(PolicyError):
        compile_query(POLICY, {"message": message})


def test_nested_and_normalized_native_fields_not_queried_silently():
    props = {
        "message": {"type": "text"},
        "level": {"type": "keyword", "normalizer": "lower"},
        "log": {"type": "nested", "properties": {"level": {"type": "keyword"}}},
        "status": {"type": "keyword"},
    }
    out = compile_query(POLICY, props)
    assert out["family_ids"] == ["phrase_failure"]


def test_native_filters_and_mapping_identity():
    out = compile_query(POLICY, PROPS)
    assert out["signal_mapping"]["http_status"]["used"] == ["http.response.status_code"]
    assert out["signal_mapping"]["severity"]["used"] == ["level"]
    assert "Error" in json.dumps(out["request"])
    assert out["query_id"] != compile_query(POLICY, {"message": {"type": "text"}})["query_id"]
    other = copy.deepcopy(POLICY)
    other["context_seconds"] = 60
    assert out["policy_id"] != identity(other)
    assert out["query_id"] != compile_query(other, PROPS)["query_id"]


@pytest.mark.parametrize(
    "change",
    [
        {"query": {"match_all": {}}},
        {"version": True},
        {"context_seconds": -1},
        {"context_seconds": 901},
        {"http_status_min": True},
        {"http_status_max": 600},
        {"context_phrases": ["a\nb"]},
        {"severity_values": ["error"]},
        {"families": []},
        {"families": [{"id": "a", "phrases": []}]},
        {"families": [{"id": "a", "phrases": ["ok", "ok"]}]},
        {"families": [{"id": "a", "phrases": ["ok"], "query": {}}]},
    ],
)
def test_rejects_invalid_policy(change):
    with pytest.raises(PolicyError):
        validate_policy({**POLICY, **change})


def test_exact_count_contract_is_not_full_capture_or_incident_precision():
    compiled = compile_query(POLICY, PROPS)
    result = validate_response(reply(compiled), compiled, SCOPE)
    assert result["source_total"] == 10
    assert result["selected_total"] == 3
    assert result["selected_fraction"] == 0.3
    assert result["records_fetched"] == 0
    assert not result["full_capture"] and not result["complete_for_scope"]
    assert result["context_status"] == "not_fetched"
    assert result["count_is_not_incident_precision"]
    assert sum(result["signal_counts_nonexclusive"].values()) == 9


@pytest.mark.parametrize(
    "case",
    [
        "timeout",
        "early",
        "failed",
        "partial",
        "gte",
        "missing",
        "over",
        "negative",
        "bucketmissing",
        "bucketover",
        "bucketgap",
    ],
)
def test_bad_counts_never_accepted(case):
    compiled = compile_query(POLICY, PROPS)
    data = reply(compiled)
    if case == "timeout":
        data["timed_out"] = True
    if case == "early":
        data["terminated_early"] = True
    if case == "failed":
        data["_shards"]["failed"] = 1
    if case == "partial":
        data["_shards"]["successful"] = 0
    if case == "gte":
        data["hits"]["total"]["relation"] = "gte"
    if case == "missing":
        data["aggregations"].pop("source_scope")
    if case == "over":
        data["hits"]["total"]["value"] = 11
    if case == "negative":
        data["aggregations"]["source_scope"]["doc_count"] = -1
    if case == "bucketmissing":
        data["aggregations"]["signals"]["buckets"].pop("phrase_failure")
    if case == "bucketover":
        data["aggregations"]["signals"]["buckets"]["phrase_failure"]["doc_count"] = 4
    if case == "bucketgap":
        for bucket in data["aggregations"]["signals"]["buckets"].values():
            bucket["doc_count"] = 0
    with pytest.raises(PolicyError):
        validate_response(data, compiled, SCOPE)


def test_count_only_uses_one_search_no_hits_and_no_fallback():
    calls = []
    compiled = compile_query(POLICY, PROPS)

    class ES:
        def request(self, method, path, body=None, **kwargs):
            calls.append((method, path, body))
            if method == "GET":
                return {SCOPE["index"]: {"mappings": {"properties": PROPS}}}
            return reply(compiled)

    count_scope(ES(), SCOPE, POLICY)
    assert len(calls) == 2
    assert calls[1][2]["size"] == 0
    assert calls[1][2]["_source"] is False
    assert "allow_partial_search_results=false" in calls[1][1]


@pytest.mark.parametrize("index", ["*", "foo,bar", "foo/_search", "../secret", "Foo"])
def test_index_scope_not_wildcard_or_multiscope(index):
    with pytest.raises(PolicyError):
        validate_scope({**SCOPE, "index": index})



WINDOW = {"start": "2026-11-02T16:45:00Z", "end": "2026-11-05T16:45:00Z"}


def test_half_open_window_filters_selected_and_global_denominator():
    props = {**PROPS, "@timestamp": {"type": "date"}}
    compiled = compile_query(POLICY, props, window=WINDOW)
    bounded = {"range": {"@timestamp": {"gte": WINDOW["start"], "lt": WINDOW["end"]}}}
    assert compiled["request"]["query"]["bool"]["filter"] == [bounded]
    assert compiled["request"]["aggs"]["source_scope"] == {"global": {}, "aggs": {"window": {"filter": bounded}}}
    data = reply(compiled, selected=3, source=500)
    data["aggregations"]["source_scope"]["window"] = {"doc_count": 10}
    result = validate_response(data, compiled, SCOPE)
    assert result["source_total"] == 10 and result["selected_total"] == 3
    assert result["window"] == WINDOW
    assert compiled["query_id"] != compile_query(POLICY, props)["query_id"]


def test_missing_window_population_not_replaced_by_whole_index_total():
    compiled = compile_query(POLICY, {**PROPS, "@timestamp": {"type": "date"}}, window=WINDOW)
    with pytest.raises(PolicyError):
        validate_response(reply(compiled), compiled, SCOPE)


@pytest.mark.parametrize("window", [
    {"start": "2026-11-02T16:45:00", "end": WINDOW["end"]},
    {"start": "2026-11-01T16:45:00Z", "end": WINDOW["end"]},
    {"start": WINDOW["end"], "end": WINDOW["start"]},
    {"start": WINDOW["start"], "end": WINDOW["start"]},
    {"start": "2026-11-02T16:45:00+01:00", "end": WINDOW["end"]},
    {**WINDOW, "query": {}},
])
def test_invalid_window_rejected(window):
    with pytest.raises(PolicyError):
        compiler.validate_window(window)


def test_window_requires_date_mapping():
    with pytest.raises(PolicyError):
        compile_query(POLICY, PROPS, window=WINDOW)


def test_same_instant_utc_spelling_has_same_identity():
    alternative = copy.deepcopy(WINDOW)
    alternative["start"] = alternative["start"].replace("Z", "+00:00")
    assert compiler.validate_window(alternative) == WINDOW
