"""Synthetic batch identity, grounding, token-boundary and provider checks."""
import json
from pathlib import Path
import runpy

import pytest

API = runpy.run_path(str(Path(__file__).resolve().parents[2]/"tools/log_extraction_batch_audit.py"))


def records():
    return [{"id": "first", "message": "redis connection refused", "scope": {"service": "worker"}},
            {"id": "second", "message": "kafka timed out", "scope": {"service": "consumer"}}]


def result(identity="l0", **changes):
    return {"id": identity, "issue_family": "refused", "findings": [{"kind": "entity", "type": "dependency",
            "value": "redis", "quote": "redis connection refused"}], **changes}


def test_compact_ids_restore_canonical_identity_and_ground_each_record():
    out = API["assess_batch"](records(), {"results": [result(), result("l1")]})
    assert out["complete"]
    assert [r["id"] for r in out["records"]] == ["first", "second"]
    assert len(out["records"][0]["valid"]) == 1
    assert not out["records"][1]["valid"]
    assert out["records"][1]["rejected"][0]["reason"] == "quote_not_in_message"


@pytest.mark.parametrize("reply,error", [
    ([result()], "missing_result_id:l1"),
    ([result(), result(), result("l1")], "duplicate_result_id:l0"),
    ([result(), result("invented"), result("l1")], "unknown_result_id"),
])
def test_missing_duplicate_and_unknown_ids_cannot_pass(reply, error):
    out = API["assess_batch"](records(), {"results": reply})
    assert not out["complete"]
    assert error in out["contract_errors"]


def test_pack_uses_actual_token_measurements_and_exposes_short_tail():
    function = API["pack"]
    previous = function.__globals__["token_count"]
    function.__globals__["token_count"] = lambda endpoint, rows: 100*len(rows)
    try:
        rows = [{"id": str(i)} for i in range(9)]
        groups, tail = function("local", rows, target=300, maximum=400, max_records=4)
        assert [g["measured_input_tokens"] for g in groups] == [400, 400]
        assert [r["id"] for r in tail] == ["8"]
        assert len({r["id"] for g in groups for r in g["records"]}) == 8
    finally:
        function.__globals__["token_count"] = previous


def test_truncated_reply_keeps_raw_output_and_never_counts_complete():
    function = API["invoke"]
    previous = function.__globals__["post"]
    function.__globals__["post"] = lambda *args: {"choices": [{"finish_reason": "length", "message": {"content": "{"}}],
                                                    "usage": {"prompt_tokens": 40001, "completion_tokens": 22528}}
    try:
        out = function("local", "synthetic", {"records": records(), "measured_input_tokens": 40001}, 22528)
        assert not out["complete"]
        assert out["error"] == "non_stop_finish_reason"
        assert out["raw_reply"]["choices"][0]["message"]["content"] == "{"
        assert out["records"] == []
    finally:
        function.__globals__["post"] = previous


def test_provider_payload_contains_all_records_and_json_schema():
    function = API["invoke"]
    previous = function.__globals__["post"]
    def fake(endpoint, path, body):
        assert path == "/v1/chat/completions"
        assert [r["id"] for r in json.loads(body["messages"][1]["content"])["logs"]] == ["l0", "l1"]
        assert body["max_tokens"] == 22528
        assert body["response_format"]["json_schema"]["schema"]["properties"]["results"]["minItems"] == 2
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"results": [result(), result("l1", findings=[])]})}}],
                "usage": {"prompt_tokens": 40001, "completion_tokens": 100}}
    function.__globals__["post"] = fake
    try:
        out = function("local", "synthetic", {"records": records(), "measured_input_tokens": 40001}, 22528)
        assert out["complete"] and len(out["records"]) == 2
    finally:
        function.__globals__["post"] = previous
