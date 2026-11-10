"""Optional bounded context checks; synthetic fixtures, no source requests."""
import copy
import importlib.util
import os
from pathlib import Path
import sys

import pytest

location = os.environ.get("SU_INCIDENT_CONTEXT_PATH")
if not location:
    pytest.skip("private context helper not supplied", allow_module_level=True)
path = Path(location).resolve()
sys.path.insert(0, str(path.parent))
spec = importlib.util.spec_from_file_location("tested_incident_context", path)
context = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context)
context_plan = context.context_plan
fetch_context = context.fetch_context
PolicyError = context.PolicyError

SCOPE = dict(index="demo-2026.11.02", day="2026-11-02", tenant="demo", env="test", service="api")
SEED = {**SCOPE, "id": "safe-1", "ts": "2026-11-02T12:00:00Z", "container": "worker", "host": "node"}
PROPS = {"@timestamp": {"type": "date"}, "container": {"properties": {"name": {"type": "keyword"}}},
         "host": {"properties": {"name": {"type": "keyword"}}}}


def response(total=2):
    return {"timed_out": False, "_shards": {"total": 1, "successful": 1, "failed": 0},
            "hits": {"total": {"relation": "eq", "value": total}, "hits": [
                {"_index": SCOPE["index"], "_id": str(i), "_source": {}} for i in range(1, min(total, 2) + 1)]}}


class ES:
    def __init__(self, data):
        self.data, self.calls = data, []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.data


def sanitize(hit):
    return {**SCOPE, "id": "safe-" + hit["_id"], "message": "masked"}


def test_exact_stream_window_and_separate_snapshot():
    out = context_plan(SEED, SCOPE, PROPS)
    assert out["window"] == {"gte": "2026-11-02T11:58:00+00:00", "lte": "2026-11-02T12:02:00+00:00"}
    filters = out["request"]["query"]["bool"]["filter"]
    assert {"term": {"container.name": "worker"}} in filters
    assert {"term": {"host.name": "node"}} in filters
    assert out["context_snapshot"] == "separate_from_seed_snapshot"


def test_missing_stream_defers_without_source_call():
    es = ES({})
    out = fetch_context(es, {**SEED, "container": None}, SCOPE, PROPS, sanitize)
    assert out["manifest"]["context_status"] == "deferred_no_exact_stream"
    assert es.calls == [] and out["records"] == []


def test_over_budget_discards_partial_hits():
    es = ES(response(1001))
    out = fetch_context(es, SEED, SCOPE, PROPS, sanitize)
    assert out["manifest"]["context_total"] == 1001
    assert out["manifest"]["context_status"] == "deferred_budget"
    assert not out["manifest"]["context_complete"] and out["records"] == []


def test_overlap_dedup_retains_seed_provenance_and_all_members():
    out = fetch_context(ES(response()), SEED, SCOPE, PROPS, sanitize, seen_ids={"safe-1"})
    assert [row["id"] for row in out["records"]] == ["safe-2"]
    assert out["manifest"]["seed_ids"] == ["safe-1"]
    assert out["manifest"]["context_record_ids"] == ["safe-1", "safe-2"]
    assert out["manifest"]["context_complete"]


@pytest.mark.parametrize("case", ["timeout", "early", "shard", "gte", "short", "duplicate", "wrongindex", "wrongprojection"])
def test_invalid_response_has_no_partial_result(case):
    data = response()
    if case == "timeout":
        data["timed_out"] = True
    if case == "early":
        data["terminated_early"] = True
    if case == "shard":
        data["_shards"]["failed"] = 1
    if case == "gte":
        data["hits"]["total"]["relation"] = "gte"
    if case == "short":
        data["hits"]["hits"].pop()
    if case == "duplicate":
        data["hits"]["hits"][1] = copy.deepcopy(data["hits"]["hits"][0])
    if case == "wrongindex":
        data["hits"]["hits"][0]["_index"] = "other"
    project = sanitize
    if case == "wrongprojection":
        def project(hit):
            return {**sanitize(hit), "tenant": "other"}
    with pytest.raises(PolicyError):
        fetch_context(ES(data), SEED, SCOPE, PROPS, project)


def test_changed_snapshot_missing_seed_is_explicit():
    out = fetch_context(ES(response()), {**SEED, "id": "missing"}, SCOPE, PROPS, sanitize)
    assert out["manifest"]["context_status"] == "deferred_seed_not_observed"
    assert out["records"] == []
