"""AnswerContract (plan §3.9a): count/exhaustive-list/absence claims only against an exact ScopeResult;
unsupported constraints are engine-injected and the generator cannot set them."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from su_multi_arm.core.types import Predicate, ScopeResult, UnsupportedConstraint
from su_multi_arm.decision import detect_intents
from su_multi_arm.generation import AggregateEvidence, Answer, AnswerContract, AnswerContractViolation, AnswerResult, Claim, answer_from_generator
from su_multi_arm.runtime.trace import Trace

EXACT = AggregateEvidence(id="aggregate:1", result=ScopeResult(exactness="exact", denominator=5, value=3, matched_ids=["r1", "r3", "r4"]))
APPROX = AggregateEvidence(id="aggregate:2", result=ScopeResult(exactness="approximate", denominator=5, value=3))
PARTIAL = AggregateEvidence(
    id="aggregate:3",
    result=ScopeResult(
        exactness="exact",
        denominator=5,
        value=0,
        unsupported=[UnsupportedConstraint(constraint_text="in Texas", reason="field_not_filterable")],
        zero_match=True,
    ),
)
COUNT_Q = detect_intents("How many records have status open?")
PLAIN_Q = detect_intents("Explain the renewal procedure")
EVIDENCE = {"kb:a", "kb:b"}


def count_answer(evidence_ids):
    return Answer(answer="There are 3 open records.", claims=[Claim(text="3 open records", kind="count", evidence_ids=evidence_ids)], citations=evidence_ids)


def test_count_claim_without_exact_scope_abstains_with_reason():
    contract = AnswerContract()
    result = contract.enforce(count_answer(["kb:a"]), evidence_ids=EVIDENCE, aggregates=[], intents=COUNT_Q)
    assert result.contract == "abstained" and result.answer.insufficient_evidence is True and result.answer.claims == []
    assert {v.rule for v in result.violations} == {"claim_without_exact_scope", "aggregate_intent_without_scope"}
    assert result.unsupported_constraints and result.unsupported_constraints[0].reason == "aggregate_intent_without_scope"
    assert not any(ch.isdigit() for ch in result.answer.answer)
    approx = contract.enforce(count_answer(["aggregate:2"]), evidence_ids=EVIDENCE, aggregates=[APPROX], intents=COUNT_Q)
    assert approx.contract == "abstained" and "claim_without_exact_scope" in {v.rule for v in approx.violations}
    with pytest.raises(AnswerContractViolation, match="claim_without_exact_scope"):
        AnswerContract(on_violation="fail").enforce(count_answer(["kb:a"]), evidence_ids=EVIDENCE, aggregates=[], intents=COUNT_Q)


def test_count_claim_with_exact_scope_passes_and_trace_records_rules():
    trace = Trace()
    result = AnswerContract().enforce(count_answer(["aggregate:1"]), evidence_ids=EVIDENCE, aggregates=[EXACT], intents=COUNT_Q, trace=trace)
    assert result.contract == "passed" and result.violations == [] and result.unsupported_constraints == []
    event = trace.root.events[0]
    assert event["name"] == "answer_contract" and event["exact_scope"] is True and event["violations"] == []


def test_absence_requires_exhaustive_scope():
    absent = Answer(answer="None exist.", claims=[Claim(text="none", kind="absence", evidence_ids=["aggregate:3"])], citations=["aggregate:3"])
    result = AnswerContract().enforce(absent, evidence_ids=set(), aggregates=[PARTIAL], intents=detect_intents("are there any records in Texas?"))
    assert result.contract == "abstained" and result.violations[0].rule == "claim_without_exact_scope"
    assert [u.reason for u in result.unsupported_constraints] == ["field_not_filterable"]  # carried from the ScopeResult
    zero = AggregateEvidence(id="aggregate:4", result=ScopeResult(exactness="exact", denominator=5, value=0, zero_match=True, applied=[Predicate(field="status", op="eq", value="gone")]))
    absent = absent.model_copy(update={"claims": [Claim(text="none", kind="absence", evidence_ids=["aggregate:4"])], "citations": ["aggregate:4"]})
    assert AnswerContract().enforce(absent, evidence_ids=set(), aggregates=[zero], intents=COUNT_Q).contract == "passed"


def test_aggregate_intent_without_exact_scope_allows_only_narrative_with_flag():
    narrative = Answer(answer="The open records discuss renewals [kb:a].", claims=[Claim(text="renewals", kind="narrative", evidence_ids=["kb:a"])], citations=["kb:a"])
    result = AnswerContract().enforce(narrative, evidence_ids=EVIDENCE, aggregates=[], intents=COUNT_Q)
    assert result.contract == "passed" and len(result.unsupported_constraints) == 1
    injected = [UnsupportedConstraint(constraint_text="how many", reason="aggregate_intent_without_scope", suggestion="decision")]
    result = AnswerContract().enforce(narrative, evidence_ids=EVIDENCE, aggregates=[], intents=COUNT_Q, unsupported=injected)
    assert result.unsupported_constraints == injected  # engine-supplied constraints are kept, not duplicated
    listing = narrative.model_copy(update={"claims": [Claim(text="all of them", kind="exhaustive_list", evidence_ids=["kb:a"])]})
    result = AnswerContract().enforce(listing, evidence_ids=EVIDENCE, aggregates=[], intents=COUNT_Q)
    assert {v.rule for v in result.violations} == {"claim_without_exact_scope", "aggregate_intent_without_scope"}


def test_unknown_evidence_and_missing_citations_are_violations():
    bad = Answer(answer="made up", citations=["not-retrieved"])
    result = AnswerContract().enforce(bad, evidence_ids=EVIDENCE, aggregates=[], intents=PLAIN_Q)
    assert [v.rule for v in result.violations] == ["unknown_evidence"]
    uncited = Answer(answer="a fact")
    assert [v.rule for v in AnswerContract().enforce(uncited, evidence_ids=EVIDENCE, aggregates=[], intents=PLAIN_Q).violations] == ["factual_without_citations"]
    honest = Answer(answer="No evidence.", insufficient_evidence=True)
    assert AnswerContract().enforce(honest, evidence_ids=EVIDENCE, aggregates=[], intents=PLAIN_Q).contract == "passed"


def test_generator_cannot_set_unsupported_constraints():
    with pytest.raises(ValidationError):
        answer_from_generator({"answer": "x", "citations": [], "unsupported_constraints": [{"constraint_text": "y", "reason": "type_mismatch"}]})
    with pytest.raises(ValidationError):
        Answer(answer="x", unsupported_constraints=[])  # type: ignore[call-arg]
    assert "unsupported_constraints" not in Answer.model_json_schema()["properties"]
    assert AnswerResult(answer=Answer(answer="x", insufficient_evidence=True)).unsupported_constraints == []
