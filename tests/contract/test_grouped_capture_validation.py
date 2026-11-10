"""Independent synthetic pagination/reconciliation tests; never contacts a source."""
from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.grouped_log_capture import CaptureError, capture_index

INDEX = "alpha-prod-query-gateway-2026.01.05"
INFO = {"index": INDEX, "date": "2026-01-05", "tenant": "alpha", "env": "prod", "service": "query-gateway"}
HOUR = 1767607200000
PROPS = {"@timestamp": {"type": "date"}, "message": {"type": "text", "fields": {"keyword": {"type": "keyword", "ignore_above": 256}}}}


def response(total, *, hits=None, buckets=None, after=None):
    out = {"timed_out": False, "_shards": {"total": 1, "successful": 1, "failed": 0},
           "hits": {"total": {"value": total, "relation": "eq"}, "hits": hits or []}}
    if buckets is not None:
        out["aggregations"] = {"groups": {"buckets": buckets}}
        if after is not None:
            out["aggregations"]["groups"]["after_key"] = after
    return out


def hit(identity="example", message="synthetic timeout", sort=1):
    return {"_index": INDEX, "_id": identity, "sort": [sort],
            "_source": {"@timestamp": "2026-01-05T10:00:00Z", "message": message}}


def bucket(count=3, *, identity="example", message="synthetic timeout", unsafe=0):
    return {"key": {"message": message, "hour": HOUR}, "doc_count": count,
            "unsafe": {"value": unsafe}, "first_seen": {"value": HOUR}, "last_seen": {"value": HOUR + 2000},
            "example": {"hits": {"hits": [hit(identity, message)]}}}


class Snapshot:
    def __init__(self, responses):
        self.responses = deepcopy(responses)
        self.requests = []
        self.closed = False

    def search(self, body, *, note):
        self.requests.append((body, note))
        assert self.responses, "Unexpected additional source request"
        return self.responses.pop(0)

    def metadata(self):
        return {"protocol": "synthetic_snapshot", "snapshot_consistent": True}

    def close(self):
        self.closed = True


def capture(snapshot, tmp_path, props=PROPS, *, default_gate=False):
    emitted = []

    def sanitize(raw):
        return {"id": raw["_id"], "message": "MASKED", "ts": raw["_source"]["@timestamp"]}

    gate = {} if default_gate else {"min_first_page_reduction": 1}
    stats = capture_index(None, INFO, props, emitted.append, sanitize,
                          page_size=2, snapshot_factory=lambda *_: snapshot, work_dir=tmp_path, **gate)
    return stats, emitted


def test_default_compression_gate_switches_same_snapshot_to_raw_before_any_group_emit(tmp_path):
    first, second = bucket(1, identity="one"), bucket(1, identity="two", message="different timeout")
    snapshot = Snapshot([
        response(2), response(2, buckets=[first, second], after=second["key"]),
        response(2, hits=[hit("one", sort=1), hit("two", "different timeout", sort=2)]), response(2),
    ])
    stats, emitted = capture(snapshot, tmp_path, default_gate=True)
    assert snapshot.closed and not snapshot.responses
    assert stats["compression_gate_raw"] is True
    assert stats["source_total"] == stats["raw_occurrences"] == stats["downloaded_documents"] == 2
    assert stats["groups"] == stats["grouped_occurrences"] == stats["downloaded_examples"] == 0
    assert [row["kind"] for row in emitted] == ["raw", "raw"]
    assert [note for _, note in snapshot.requests] == [
        "grouped_snapshot_count", "grouped_composite_page", "grouped_raw_fallback", "grouped_raw_fallback",
    ]
    assert all(body["query"] == {"match_all": {}} for body, note in snapshot.requests if note == "grouped_raw_fallback")
    assert not list(tmp_path.iterdir())


def test_complete_groups_and_missing_keyword_raw_fallback_reconcile(tmp_path):
    missing = bucket(1, identity="long", message=None, unsafe=1)
    snapshot = Snapshot([
        response(4), response(4, buckets=[bucket(), missing], after=missing["key"]),
        response(1, hits=[hit("long", "x" * 300)]), response(1), response(4, buckets=[]),
    ])
    stats, emitted = capture(snapshot, tmp_path)
    assert snapshot.closed and not snapshot.responses
    assert not list(tmp_path.iterdir())
    assert stats["source_total"] == 4
    assert stats["grouped_occurrences"] == 3 and stats["raw_occurrences"] == 1
    assert stats["downloaded_documents"] == 2
    assert [row["kind"] for row in emitted] == ["group", "raw"]
    assert emitted[0]["examples"][0]["message"] == emitted[1]["record"]["message"] == "MASKED"
    composite = [body for body, note in snapshot.requests if note == "grouped_composite_page"]
    assert composite[1]["aggs"]["groups"]["composite"]["after"] == missing["key"]


def test_multivalued_raw_fallback_is_deduplicated_across_overlapping_buckets(tmp_path):
    one, two = bucket(1, message="a", unsafe=1), bucket(1, message="b", unsafe=1)
    snapshot = Snapshot([response(1), response(1, buckets=[one, two]),
                         response(1, hits=[hit("array", "masked array")]), response(1),
                         response(1, hits=[hit("array", "masked array")]), response(1)])
    stats, emitted = capture(snapshot, tmp_path)
    assert stats["source_total"] == stats["raw_occurrences"] == len(emitted) == 1
    assert stats["raw_duplicate_hits"] == 1
    assert snapshot.closed


@pytest.mark.parametrize("failure", ["timeout", "failed_shard", "inexact_total", "missing_shards"])
def test_incomplete_source_response_never_completes(failure, tmp_path):
    item = response(3)
    if failure == "timeout":
        item["timed_out"] = True
    elif failure == "failed_shard":
        item["_shards"].update(failed=1, successful=0)
    elif failure == "inexact_total":
        item["hits"]["total"]["relation"] = "gte"
    else:
        item.pop("_shards")
    snapshot = Snapshot([item])
    with pytest.raises(CaptureError):
        capture(snapshot, tmp_path)
    assert snapshot.closed


@pytest.mark.parametrize("failure", ["repeated_bucket", "repeated_cursor", "under_count"])
def test_composite_cannot_complete_with_duplicate_pages_or_missing_occurrences(failure, tmp_path):
    first = bucket(1)
    second = bucket(1, identity="second", message="different timeout")
    if failure == "repeated_bucket":
        pages = [response(2, buckets=[first], after=first["key"]), response(2, buckets=[first])]
    elif failure == "repeated_cursor":
        pages = [response(2, buckets=[first], after=first["key"]),
                 response(2, buckets=[second], after=first["key"])]
    else:
        pages = [response(2, buckets=[first])]
    snapshot = Snapshot([response(2), *pages])
    with pytest.raises(CaptureError):
        capture(snapshot, tmp_path)
    assert snapshot.closed


def test_raw_only_mapping_requires_all_occurrences_and_advancing_cursors(tmp_path):
    snapshot = Snapshot([response(2), response(2, hits=[hit("one")]), response(2)])
    with pytest.raises(CaptureError, match="before exact count"):
        capture(snapshot, tmp_path, props={})
    assert snapshot.closed
    snapshot = Snapshot([response(2), response(2, hits=[hit("one")]), response(2, hits=[hit("two")])])
    with pytest.raises(CaptureError, match="cursor repeated"):
        capture(snapshot, tmp_path, props={})
    assert snapshot.closed


def test_no_snapshot_consistency_claim_without_positive_attestation(tmp_path):
    snapshot = Snapshot([response(0), response(0, buckets=[])])
    snapshot.metadata = lambda: {"protocol": "synthetic_unstable", "snapshot_consistent": False}
    with pytest.raises(CaptureError, match="snapshot consistency"):
        capture(snapshot, tmp_path)
    assert snapshot.closed
    assert not list(tmp_path.iterdir())


def test_duplicate_raw_identity_with_new_cursor_cannot_replace_missing_document(tmp_path):
    snapshot = Snapshot([response(2), response(2, hits=[hit("same", sort=1)]),
                         response(2, hits=[hit("same", sort=2)])])
    with pytest.raises(CaptureError, match="repeated a document"):
        capture(snapshot, tmp_path, props={})
    assert snapshot.closed
    assert not list(tmp_path.iterdir())
