"""Cross-encoder rerank stage (``selection/rerank.py``): ordering, pins, top-k in/out, latency budget
and the explicit fail-open policy. Fake scorers only (no model download)."""

from __future__ import annotations

import threading

import pytest

from su_multi_arm.core.config import validate_config
from su_multi_arm.core.config_selection import RerankOptions
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.selection.rerank import Reranker, RerankError, passage_text, resolve_device
from su_multi_arm.selection.types import Selected

from tests.unit.test_config_v2 import minimal


class Scorer:
    """Scores a passage by the number of times ``word`` occurs in it."""

    def __init__(self, word="alpha", fail=None, block=None, wrong_length=False):
        self.word, self.fail, self.block, self.wrong_length = word, fail, block, wrong_length
        self.loads = 0
        self.seen: list[tuple[str, str]] = []

    def load(self):
        self.loads += 1
        if self.fail == "load":
            raise RuntimeError("weights missing")
        return {"loaded": self.loads == 1, "device": "cpu", "dtype": "float32"}

    def score(self, pairs):
        self.seen.extend(pairs)
        if self.block is not None:
            self.block.wait(5)
        if self.fail == "score":
            raise ValueError("bad tensor")
        scores = [float(p.count(self.word)) for _q, p in pairs]
        return scores[:-1] if self.wrong_length else scores


TEXT = {"a": "beta", "b": "alpha alpha", "c": "alpha", "d": "alpha alpha alpha", "p": "gamma"}


def selection(pinned=False):
    rows = [Selected(record_id=r, score=1.0 / (i + 1), arms=["dense"]) for i, r in enumerate("abcd")]
    if pinned:
        rows.insert(0, Selected(record_id="p", score=9.0, arms=["exact_id"], pinned=True))
    return rows


def run(scorer, options=None, pinned=False, pins_first=True):
    reranker = Reranker(scorer, model="fake")
    return reranker.rerank("q", selection(pinned), lambda rid: TEXT[rid], options or RerankOptions(), pins_first)


def test_reranks_by_scorer_and_keeps_pins_first():
    ordered, detail = run(Scorer(), pinned=True)
    assert [r.record_id for r in ordered] == ["p", "d", "b", "c", "a"]
    assert detail["status"] == "reranked" and detail["scored"] == 4 and detail["dropped"] == 0
    assert detail["device"] == "cpu" and "seconds" in detail and "load_seconds" in detail
    assert all("rerank:cross_encoder" in r.reasons for r in ordered[1:])
    assert ordered[0].score == 9.0  # the pin is never scored


def test_pins_are_scored_when_pins_are_disabled():
    ordered, _ = run(Scorer(word="gamma"), pinned=True, pins_first=False)
    assert ordered[0].record_id == "p"  # the only gamma passage wins on its score
    assert ordered[0].reasons == ["rerank:cross_encoder"]


def test_top_k_in_scores_only_the_head_and_keeps_the_tail_in_fusion_order():
    scorer = Scorer()
    ordered, detail = run(scorer, RerankOptions(top_k_in=2))
    assert [r.record_id for r in ordered] == ["b", "a", "c", "d"]
    assert len(scorer.seen) == 2 and detail["scored"] == 2
    assert "rerank:not_scored_past_top_k_in" in ordered[2].reasons


def test_top_k_out_is_an_explicit_cut_reported_in_the_trace():
    ordered, detail = run(Scorer(), RerankOptions(top_k_out=2))
    assert [r.record_id for r in ordered] == ["d", "b"]
    assert detail["dropped"] == 2


@pytest.mark.parametrize("fail,reason", [("load", "scoring_failed"), ("score", "scoring_failed")])
def test_errors_raise_under_fail(fail, reason):
    with pytest.raises(RerankError) as info:
        run(Scorer(fail=fail))
    assert info.value.reason == reason


@pytest.mark.parametrize("fail", ["load", "score"])
def test_errors_fail_open_to_fusion_order_with_an_explicit_trace(fail):
    ordered, detail = run(Scorer(fail=fail), RerankOptions(on_error="fusion_order"))
    assert [r.record_id for r in ordered] == ["a", "b", "c", "d"]
    assert detail["status"] == "failed_open" and detail["reason"] == "scoring_failed"
    assert all(any(x.startswith("rerank:failed_open") for x in r.reasons) for r in ordered)


def test_wrong_number_of_scores_is_a_bad_response():
    _ordered, detail = run(Scorer(wrong_length=True), RerankOptions(on_error="fusion_order"))
    assert detail["reason"] == "bad_response"
    with pytest.raises(RerankError):
        run(Scorer(wrong_length=True))


def test_timeout_is_the_latency_budget():
    gate = threading.Event()
    try:
        ordered, detail = run(Scorer(block=gate), RerankOptions(timeout_ms=50, on_error="fusion_order"))
        assert detail["status"] == "failed_open" and detail["reason"] == "timeout"
        assert [r.record_id for r in ordered] == ["a", "b", "c", "d"]
        with pytest.raises(RerankError) as info:
            run(Scorer(block=gate), RerankOptions(timeout_ms=50))
        assert info.value.reason == "timeout"
    finally:
        gate.set()


def test_an_overrun_call_holds_the_scorer_without_queueing_later_calls():
    import time

    gate = threading.Event()
    scorer = Scorer(block=gate)
    reranker = Reranker(scorer, model="fake")
    options = RerankOptions(timeout_ms=50, on_error="fusion_order")
    _, first = reranker.rerank("q", selection(), lambda rid: TEXT[rid], options, True)
    started = time.perf_counter()
    _, second = reranker.rerank("q", selection(), lambda rid: TEXT[rid], options, True)
    assert first["reason"] == second["reason"] == "timeout"
    assert "busy" in second["message"] and time.perf_counter() - started < 1.0
    assert len(scorer.seen) == 4  # the second call never reached the scorer
    gate.set()
    for _ in range(100):  # the overrun call finishes and releases the scorer
        if reranker._busy.acquire(timeout=0.05):
            reranker._busy.release()
            break
    scorer.block = None
    ordered, third = reranker.rerank("q", selection(), lambda rid: TEXT[rid], options, True)
    assert third["status"] == "reranked" and ordered[0].record_id == "d"


def test_passage_text_is_bounded_and_titled():
    assert passage_text("T", "x" * 50, 10) == "T\n" + "x" * 8
    assert passage_text("", "abc", 10) == "abc"


def test_resolve_device():
    assert resolve_device("cpu") == "cpu"
    assert resolve_device("auto") in {"cpu", "cuda"}


def test_config_defaults_and_reranking_without_rerank_is_refused():
    reranker = {"provider": "sentence_transformers", "model": "some/cross-encoder"}
    config = validate_config(minimal(models={"roles": {"reranker": reranker}}, pipeline={"arms": {"lexical": {"backend": "local"}}, "selection": {"rerank": True}}))
    role, options = config.models.roles.reranker, config.pipeline.selection.reranking
    assert (role.device, role.dtype, role.batch_size, role.max_length) == ("auto", "auto", 32, None)
    assert options == RerankOptions() and options.on_error == "fail"
    with pytest.raises(ConfigError, match="reranking"):
        validate_config(minimal(pipeline={"arms": {"lexical": {"backend": "local"}}, "selection": {"reranking": {"top_k_in": 5}}}))
    with pytest.raises(ValueError):
        validate_config(minimal(models={"roles": {"reranker": {**reranker, "device": "tpu"}}}))
