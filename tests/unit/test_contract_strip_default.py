"""2.0.0a3 owner decision: ``on_violation: strip`` is the default (generation/contract.py).

A narrative answer loses only its unbacked count/list/absence sentences (recorded); an answer to an
aggregate question whose text states an unbacked count/list/absence still abstains.
"""

from __future__ import annotations

from su_multi_arm.core.config import AnswerContractConfig
from su_multi_arm.core.types import ScopeResult
from su_multi_arm.decision.intent import detect_intents
from su_multi_arm.generation import Answer, AnswerContract, Claim
from su_multi_arm.generation.contract import AggregateEvidence

TARGETS = ["record", "status", "product"]


def narrative(text: str, ids=("kb:a",)) -> Answer:
    return Answer(answer=text, claims=[Claim(text="Narrative.", kind="narrative", evidence_ids=list(ids))], citations=list(ids))


def enforce(answer: Answer, question: str, contract: AnswerContract | None = None, aggregates=()):
    return (contract or AnswerContract()).enforce(
        answer, evidence_ids={"kb:a", "kb:b"}, aggregates=list(aggregates), intents=detect_intents(question, [], TARGETS), question=question, targets=TARGETS
    )


def test_strip_is_the_default_in_config_and_contract():
    assert AnswerContractConfig().on_violation == "strip" and AnswerContract().on_violation == "strip"


def test_narrative_answer_loses_the_unbacked_sentence_and_records_it():
    text = "Reset the rounding rule under Settings [kb:a]. There are 14 open tickets about it. Then re-run the export."
    result = enforce(narrative(text), "How do I reset the rounding rule?")
    assert result.contract == "stripped"
    assert result.answer.answer == "Reset the rounding rule under Settings [kb:a]. Then re-run the export."
    assert [(v.rule, v.action) for v in result.violations] == [("answer_text_without_exact_scope", "strip")]
    assert not result.answer.insufficient_evidence and result.answer.citations == ["kb:a"]


def test_the_same_answer_abstains_under_abstain_with_reason():
    text = "Reset the rounding rule under Settings [kb:a]. There are 14 open tickets about it."
    result = enforce(narrative(text), "How do I reset the rounding rule?", AnswerContract("abstain_with_reason"))
    assert result.contract == "abstained" and result.answer.insufficient_evidence


def test_an_unbacked_count_answering_a_count_question_still_abstains():
    result = enforce(narrative("There are 14 open records [kb:a]."), "How many open records are there?")
    assert result.contract == "abstained" and result.answer.insufficient_evidence
    assert result.unsupported_constraints and result.unsupported_constraints[0].reason == "aggregate_intent_without_scope"


def test_an_unbacked_exact_claim_on_an_aggregate_question_is_stripped_when_the_text_states_no_count():
    answer = Answer(
        answer="Several open records discuss rounding [kb:a].",
        claims=[Claim(text="Several.", kind="narrative", evidence_ids=["kb:a"]), Claim(text="14 open.", kind="count", evidence_ids=["kb:a"])],
        citations=["kb:a"],
    )
    result = enforce(answer, "How many open records are there?")
    assert result.contract == "stripped" and [c.kind for c in result.answer.claims] == ["narrative"]


def test_a_backed_count_passes_and_a_mismatched_one_abstains():
    aggregate = AggregateEvidence(id="aggregate:1", result=ScopeResult(exactness="exact", denominator=36, value=12))
    backed = Answer(answer="There are 12 open records [aggregate:1].", claims=[Claim(text="12 open.", kind="count", evidence_ids=["aggregate:1"])], citations=["aggregate:1"])
    assert enforce(backed, "How many open records are there?", aggregates=[aggregate]).contract == "passed"
    wrong = backed.model_copy(update={"answer": "There are 13 open records [aggregate:1]."})
    result = enforce(wrong, "How many open records are there?", aggregates=[aggregate])
    assert result.contract == "abstained" and [v.rule for v in result.violations] == ["answer_number_mismatch"]


def test_nothing_left_after_stripping_abstains():
    result = enforce(narrative("There are 14 open tickets about rounding [kb:a]."), "What is the rounding policy?")
    assert result.contract == "abstained" and result.answer.insufficient_evidence


def test_unknown_evidence_and_missing_citations_always_abstain_under_strip():
    unknown = Answer(answer="Reset it [kb:z].", claims=[Claim(text="x", kind="narrative", evidence_ids=["kb:z"])], citations=["kb:z"])
    assert enforce(unknown, "How do I reset it?").contract == "abstained"
    uncited = Answer(answer="Reset it under Settings.")
    assert enforce(uncited, "How do I reset it?").contract == "abstained"
