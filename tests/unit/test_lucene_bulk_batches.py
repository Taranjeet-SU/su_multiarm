"""Synthetic regression checks for request-size rejection during large publication."""
import json

import pytest

from su_multi_arm.backends._lucene_common import bulk_batches, bulk_body
from su_multi_arm.backends.opensearch import OpenSearchBackend
from su_multi_arm.core.errors import BackendError


def test_byte_budget_preserves_unicode_actions_sources_deletes_and_order():
    actions = [({"index": {"_id": "one"}}, {"text": "界"*20 + "\u2028"}),
               ({"delete": {"_id": "two"}}, None),
               ({"update": {"_id": "three"}}, {"doc": {"text": "é"*20}})]
    body = bulk_body(actions)
    budget = max(len(bulk_body([a]).encode()) for a in actions)
    batches = list(bulk_batches(body, budget))
    assert len(batches) > 1
    assert "".join(batches) == body
    assert all(len(batch.encode()) <= budget for batch in batches)
    assert all(batch.endswith("\n") for batch in batches)


def test_single_oversized_operation_stays_intact_and_missing_source_is_refused():
    body = bulk_body([({"index": {"_id": "one"}}, {"text": "x"*200})])
    assert list(bulk_batches(body, 10)) == [body]
    with pytest.raises(ValueError, match="no source"):
        list(bulk_batches('{"index":{"_id":"missing"}}\n', 100))


class BoundedTransport:
    def __init__(self, fail_at=None):
        self.requests = []
        self.fail_at = fail_at
    def request(self, method, path, params=None, body=None):
        self.requests.append(body)
        if len(body.encode()) > 150:
            pytest.fail("oversized request reached transport")
        lines = body.strip().split("\n")
        ids = [json.loads(lines[i])["index"]["_id"] for i in range(0,len(lines),2)]
        failed = len(self.requests) == self.fail_at
        return {"errors": failed, "items": [{"index": {"_id": i, "status": 429 if failed else 201,
                                                       **({"error": {"type": "rejected", "reason": "synthetic"}} if failed else {})}} for i in ids]}


def test_adapter_bounds_requests_and_preserves_all_item_results(monkeypatch):
    monkeypatch.setattr("su_multi_arm.backends.opensearch.adapter.MAX_BULK_BYTES",150)
    transport = BoundedTransport()
    backend = OpenSearchBackend(transport=transport)
    actions = [({"index": {"_id": str(i)}}, {"text": "界"*20}) for i in range(6)]
    result = backend.call("index", "POST", "/synthetic/_bulk", body=bulk_body(actions))
    assert len(transport.requests) > 1
    assert [item["index"]["_id"] for item in result["items"]] == [str(i) for i in range(6)]
    assert backend.stats["request.index"] == len(transport.requests)


def test_later_batch_failure_is_never_reported_as_success(monkeypatch):
    monkeypatch.setattr("su_multi_arm.backends.opensearch.adapter.MAX_BULK_BYTES",150)
    backend = OpenSearchBackend(transport=BoundedTransport(fail_at=2))
    actions = [({"index": {"_id": str(i)}}, {"text": "x"*70}) for i in range(6)]
    with pytest.raises(BackendError, match="synthetic"):
        backend.call("index", "POST", "/synthetic/_bulk", body=bulk_body(actions))
