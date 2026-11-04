"""``AnswerContract.rendered_answer`` (fix5): a pure ranking/count answer is the aggregates'
deterministic renderings; the generator's optional sentence may only restate their numbers.
Evidence: unit (synthetic)."""

from __future__ import annotations

import pytest

from su_multi_arm.core.types import ScopeResult
from su_multi_arm.decision.intent import Intents
from su_multi_arm.generation.contract import AggregateEvidence, AnswerContract, AnswerContractViolation, labelled, numbers_outside

RENDERING = "Top 2 service by source ERROR lines for index date 2026-03-10: svc-a: 304 source, 3 parsed, 99.0% unparsed; svc-b: 40 source. bounded."


def aggregate(exactness: str = "bounded", rendering: str = RENDERING, agg_id: str = "agg-1") -> AggregateEvidence:
    return AggregateEvidence(id=agg_id, result=ScopeResult(exactness=exactness, denominator=0), rendering=rendering)


def test_numbers_outside_accepts_restated_numbers_only():
    assert numbers_outside("svc-a has 304 source lines, 99% unparsed, on 2026-03-10.", RENDERING) == []
    assert numbers_outside("svc-a has 1,000 lines", RENDERING) == ["1,000"]
    assert numbers_outside("about five services", RENDERING) == ["five"]
    assert numbers_outside("the two services", RENDERING) == []  # "Top 2"


def test_labelled_appends_a_missing_exactness_label():
    assert labelled(aggregate()) == RENDERING
    assert labelled(aggregate("exact", "svc-a: 3.")) == "svc-a: 3 (exact)."


def test_the_rendering_is_the_answer_without_a_narrative():
    out = AnswerContract().rendered_answer([aggregate()], intents=Intents())
    assert out.answer.answer == RENDERING and out.answer.citations == ["agg-1"] and out.contract == "passed"
    assert out.answer.claims[0].kind == "narrative"  # a bounded rendering backs no count claim
    exact = AnswerContract().rendered_answer([aggregate("exact", "svc-a: 3; exact.")], intents=Intents())
    assert exact.answer.claims[0].kind == "count" and not exact.answer.insufficient_evidence


def test_a_restating_sentence_is_kept_after_the_rendering():
    out = AnswerContract().rendered_answer([aggregate()], intents=Intents(), narrative="svc-a fails the most, 304 source lines.")
    assert out.answer.answer == RENDERING + "\n\nsvc-a fails the most, 304 source lines." and out.answer.short_answer == "svc-a fails the most, 304 source lines."
    assert out.contract == "passed" and out.answer.claims[-1].kind == "narrative"


@pytest.mark.parametrize("text", ["svc-a fails the most. Restart it.", "svc-a fails.\nsvc-b too."])
def test_more_than_one_sentence_is_dropped(text):
    out = AnswerContract().rendered_answer([aggregate()], intents=Intents(), narrative=text)
    assert out.answer.answer == RENDERING and out.answer.short_answer is None and out.contract == "passed"


def test_a_foreign_number_is_an_answer_number_mismatch():
    out = AnswerContract().rendered_answer([aggregate()], intents=Intents(), narrative="svc-a has 572000 errors.")
    assert out.answer.answer == RENDERING and out.contract == "stripped"
    assert [(v.rule, v.action) for v in out.violations] == [("answer_number_mismatch", "strip")]
    with pytest.raises(AnswerContractViolation):
        AnswerContract("fail").rendered_answer([aggregate()], intents=Intents(), narrative="svc-a has 572000 errors.")


def test_a_refused_aggregate_is_insufficient_evidence_and_needs_a_rendering():
    out = AnswerContract().rendered_answer([aggregate(rendering="Not answered: no count query.")], intents=Intents(), refused=True)
    assert out.answer.insufficient_evidence and out.answer.answer.startswith("Not answered")
    with pytest.raises(ValueError):
        AnswerContract().rendered_answer([aggregate(rendering=None)], intents=Intents())  # type: ignore[arg-type]
