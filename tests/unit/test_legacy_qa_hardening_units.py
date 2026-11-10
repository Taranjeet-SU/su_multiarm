"""Units of the legacy-QA adapter hardening (QA 2026-11-09): JSON string decoding of streamed
answers (surrogate pairs), credential masking of provisional text, the answer envelope's segment
(``cases``) panel, measured rerank time and planner-failure reporting, the stream failure log line,
and the spend ledger's bounded reservations. Synthetic values only (no credential is real)."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from su_multi_arm.compat.legacy_qa import LegacyProfile
from su_multi_arm.compat.legacy_qa.envelope import qa_envelope
from su_multi_arm.compat.legacy_qa.source import redact
from su_multi_arm.compat.legacy_qa.stream import AnswerTextExtractor, ProvisionalText, sse, utf8_safe
from su_multi_arm.core.errors import BudgetExhausted
from su_multi_arm.learning.patterns.spend import DaySpendLedger, TotalSpendLedger
from su_multi_arm.models import Answer, AnswerResult, Hit, SearchResult
from su_multi_arm.runtime.events import AnswerStream, ErrorEvent

RAW = '{"answer":"Fixed \\ud83d\\ude00 in caf\\u00e9 [r01].","claims":[],"citations":["r01"],"insufficient_evidence":false}'
TOKEN = "Zx9" + "q" * 30  # synthetic, shaped like an opaque token


# --- QA-02: surrogate pairs ------------------------------------------------------------------------


def test_extractor_decodes_a_json_surrogate_pair_to_one_character():
    extractor = AnswerTextExtractor()
    text = ""
    for i in range(0, len(RAW), 5):
        text = extractor.feed(RAW[i : i + 5])
    assert text.startswith("Fixed \U0001f600 in café")


@pytest.mark.parametrize("size", [1, 2, 3, 6, 7, 11, 13])
def test_extractor_matches_json_loads_for_every_chunking(size):
    extractor = AnswerTextExtractor()
    text = ""
    for i in range(0, len(RAW), size):
        text = extractor.feed(RAW[i : i + size])
    assert text == json.loads(RAW)["answer"]


def test_lone_surrogates_become_replacement_characters():
    raw = '{"answer":"a \\ud83d b \\ude00 c \\ud83d"}'
    text = AnswerTextExtractor().feed(raw)
    assert text == "a � b � c �"
    text.encode("utf-8")
    assert utf8_safe("x\ud83dy") == "x�y"
    sse("final", {"answer": json.loads('"\\ud83d"')}).encode("utf-8")  # a lone surrogate from json.loads


def test_streamed_token_events_are_utf8_encodable():
    provisional = ProvisionalText({"r01": 1})
    out = ""
    for i in range(0, len(RAW), 5):
        delta = provisional.feed(RAW[i : i + 5])
        if delta:
            sse("token", {"text": delta}).encode("utf-8")  # what Starlette does with every chunk
            out += delta
    assert out == "Fixed \U0001f600 in café [1]."


# --- M7: provisional text is masked as it is released ------------------------------------------------


def test_provisional_text_never_releases_part_of_a_credential():
    answer = f"Rotate it: the header was Authorization: Bearer {TOKEN} and password=hunter2xyz [r01]. Done."
    raw = json.dumps({"answer": answer, "claims": [], "citations": ["r01"], "insufficient_evidence": False})
    provisional = ProvisionalText({"r01": 1}, redact)
    streamed = ""
    for ch in raw:
        delta = provisional.feed(ch)
        streamed += delta
        assert TOKEN[:6] not in streamed and "hunter2" not in streamed
    assert streamed == redact(answer.replace("[r01]", "[1]"))


def test_provisional_text_holds_an_unterminated_private_key_block():
    body = "-----BEGIN PRIVATE KEY-----\nMIIEvQ synthetic body\n-----END PRIVATE KEY-----"
    raw = json.dumps({"answer": f"Key: {body} end"})
    provisional = ProvisionalText({}, redact)
    streamed = "".join(provisional.feed(ch) for ch in raw)
    assert "MIIEvQ" not in streamed and "[redacted]" in streamed


# --- M9 / M6: envelope -------------------------------------------------------------------------------


def _profile():
    return LegacyProfile.model_validate({"datasets": [{"id": "demo"}]}).datasets[0]


def _result(trace=None, arm_hits=None, insufficient=False, retrieval_trace=None):
    hit = Hit(document_id="r01", title="Billing r01", source="article", url=None, text="Segment text.", score=0.5, arms=["segment"], provenance=[{"segment_id": "s1", "segment_kind": "resolution"}])
    retrieval = SearchResult(query="q", generation="0" * 32, hits=[hit], arm_hits=arm_hits if arm_hits is not None else {"segment": [hit]}, trace=retrieval_trace or {})
    return AnswerResult(answer=Answer(answer="Answer [r01].", citations=["r01"], insufficient_evidence=insufficient), retrieval=retrieval, trace=trace or {})


def _envelope(result, selection=None):
    return qa_envelope(
        profile=_profile(), question="q", result=result, evidence_items=[{"id": "r01"}], legacy_mode="hybrid", answer_model="m", planner_model="p",
        embedder={}, selection=selection or {}, wall_seconds=1.0, warnings=[], unsupported={}, session_cache=None, context_used=False,
    )


def test_segment_hits_fill_the_legacy_cases_panel():
    env = _envelope(_result())
    case = env["cases"]["results"][0]
    assert case["ticket_id"] == "r01" and case["segment_type"] == "resolution" and case["text"] == "Segment text." and case["rank"] == 1
    assert "cases" not in env["compat"]["unsupported"]
    assert "cases" in _envelope(_result(arm_hits={}))["compat"]["unsupported"]


def test_rerank_ms_is_the_measured_rerank_time():
    env = _envelope(_result(), selection={"rerank": True, "rerank_detail": {"status": "reranked", "seconds": 0.25}})
    assert env["timings"]["rerank_ms"] == 250 and "timings.rerank_ms" not in env["compat"]["unsupported"]
    skipped = _envelope(_result(), selection={"rerank": True})
    assert skipped["timings"]["rerank_ms"] == 0 and "did not run" in skipped["compat"]["unsupported"]["timings.rerank_ms"]


def test_a_planner_failure_is_visible_and_never_a_grounded_answer():
    decision = {"kind": "llm", "outcome": "abstain", "failure": "runtime_unavailable", "policy_applied": "fail_closed"}
    env = _envelope(_result(trace={"decision": decision}))
    assert env["insufficient_evidence"] is True
    assert any("planning failed (runtime_unavailable)" in w and "not grounded" in w for w in env["warnings"])
    assert env["compat"]["planner_failure"]["failure"] == "runtime_unavailable"
    fallback = _envelope(_result(trace={"decision": {**decision, "outcome": "raw_all_arms", "policy_applied": "raw_all_arms"}}))
    assert fallback["insufficient_evidence"] is False and any("raw_all_arms" in w for w in fallback["warnings"])
    assert "planner_failure" not in _envelope(_result(trace={"decision": {"kind": "llm", "outcome": "decided"}}))["compat"]


# --- QA-03: the stream failure log line carries the reason -------------------------------------------


def test_stream_failure_log_line_names_the_error_and_reason(caplog):
    def run(emit, on_delta):
        raise KeyError("synthetic adapter bug")

    with caplog.at_level(logging.WARNING, logger="su_multi_arm.runtime.events"):
        event = AnswerStream(run).next_event(5)
    assert isinstance(event, ErrorEvent) and event.category == "internal"
    line = next(r.getMessage() for r in caplog.records if "answer stream failed" in r.getMessage())
    assert "error=KeyError" in line and "synthetic adapter bug" in line


def test_answer_stream_exposes_the_budget_usage():
    stream = AnswerStream(lambda emit, on_delta: (_ for _ in ()).throw(RuntimeError("x")), usage=lambda: {"used": {"usd": 0.01}})
    assert stream.usage() == {"used": {"usd": 0.01}}
    assert AnswerStream(lambda e, d: None).usage() is None


# --- M10: reservations of a crashed process expire (audited) ---------------------------------------------


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def test_a_stale_day_reservation_is_released_and_audited(tmp_path, caplog):
    clock = Clock()
    path = tmp_path / "spend.sqlite"
    crashed = DaySpendLedger(path, max_calls=10, max_usd=1.0, scope="s", role="r", clock=clock, stale_after_seconds=600)
    crashed.reserve(0.8)  # the process dies before commit/release
    live = DaySpendLedger(path, max_calls=10, max_usd=1.0, scope="s", role="r", clock=clock, stale_after_seconds=600)
    with pytest.raises(BudgetExhausted):
        live.reserve(0.5)  # the hold still counts within the bound
    clock.now += timedelta(seconds=601)
    with caplog.at_level(logging.WARNING):
        held = live.reserve(0.5)  # the stale hold is released first, then this one fits
    assert live.usage()["reserved_usd"] == pytest.approx(0.5) and live.usage()["calls"] == 2  # the crashed call stays counted
    assert [a["event"] for a in live.audit()] == ["stale_released"] and live.audit()[0]["usd"] == pytest.approx(0.8)
    assert any("released a reservation" in r.getMessage() for r in caplog.records)
    live.commit(held, 0.4)
    assert live.usage()["reserved_usd"] == 0 and live.usage()["committed_usd"] == pytest.approx(0.4)
    assert live.open_reservations() == []


def test_a_refused_reservation_does_not_undo_the_stale_release(tmp_path):
    clock = Clock()
    path = tmp_path / "spend.sqlite"
    ledger = DaySpendLedger(path, max_calls=10, max_usd=1.0, scope="s", role="r", clock=clock, stale_after_seconds=600)
    ledger.reserve(0.8)
    clock.now += timedelta(seconds=601)
    with pytest.raises(BudgetExhausted):
        ledger.reserve(1.5)  # over the cap on its own
    assert ledger.usage()["reserved_usd"] == 0 and len(ledger.audit()) == 1


def test_a_late_commit_after_release_records_spend_without_double_subtracting(tmp_path):
    clock = Clock()
    path = tmp_path / "spend.sqlite"
    ledger = DaySpendLedger(path, max_calls=10, max_usd=5.0, scope="s", role="r", clock=clock, stale_after_seconds=600)
    slow = ledger.reserve(0.5)
    clock.now += timedelta(seconds=601)
    assert ledger.expire_stale() == 1
    ledger.commit(slow, 0.3)
    usage = ledger.usage()
    assert usage["reserved_usd"] == 0 and usage["committed_usd"] == pytest.approx(0.3)
    assert [a["event"] for a in ledger.audit()] == ["stale_released", "late_commit"]


def test_total_ledger_reservations_expire_too_and_open_releases_at_startup(tmp_path):
    clock = Clock()
    path = tmp_path / "spend.sqlite"
    TotalSpendLedger(path, key="provider:x", max_usd=1.0, clock=clock, stale_after_seconds=600).reserve(0.9)
    clock.now += timedelta(seconds=601)
    reopened = TotalSpendLedger(path, key="provider:x", max_usd=1.0, clock=clock, stale_after_seconds=600)  # start-up reconcile
    assert reopened.usage()["remaining_usd"] == pytest.approx(1.0)
    assert [a["event"] for a in reopened.audit()] == ["stale_released"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM spend_open").fetchone()[0] == 0


def test_release_and_commit_keep_working_on_a_ledger_without_expiry(tmp_path):
    ledger = DaySpendLedger(tmp_path / "spend.sqlite", max_calls=10, max_usd=1.0, scope="s", role="r", stale_after_seconds=None)
    first = ledger.reserve(0.4)
    ledger.release(first)
    second = ledger.reserve(0.4)
    ledger.commit(second, 0.2)
    usage = ledger.usage()
    assert usage["reserved_usd"] == 0 and usage["committed_usd"] == pytest.approx(0.2) and usage["calls"] == 1


# --- sql panel: dict rows keep their values, in column order ---------------------------------------------


def test_sql_rows_given_as_mappings_are_values_in_column_order():
    from su_multi_arm.compat.legacy_qa.envelope import sql_block

    aggregate = {
        "sql": "SELECT project, n FROM t",
        "columns": ["project", "n"],
        "rows": [{"n": 3, "project": "Alpha", "contributor_ids": ["r01", "r03"]}, {"project": "Beta", "n": 1, "contributor_ids": []}],
    }
    block, note = sql_block([aggregate])
    assert note is None and block["columns"] == ["project", "n", "contributor_ids"]
    assert block["rows"] == [["Alpha", 3, "r01, r03"], ["Beta", 1, ""]]
    keyed, _ = sql_block([{"sql": "SELECT 1", "rows": [{"a": 1, "b": "x"}]}])  # no columns: the keys in order
    assert keyed["columns"] == ["a", "b"] and keyed["rows"] == [[1, "x"]]
    plain, _ = sql_block([{"sql": "SELECT 1", "columns": ["a"], "rows": [[1], [2]]}])  # list rows unchanged
    assert plain["rows"] == [[1], [2]]
