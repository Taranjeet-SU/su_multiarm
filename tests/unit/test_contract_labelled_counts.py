"""Persona-eval follow-ups (fix5) on the answer contract. Evidence: unit (synthetic).

* A count stated with its label ("approximately 3", "at least 3") on an approximate or bounded
  aggregate passes; unlabelled, with a mismatching number, or as a complete list / absence it does
  not (the exact-claim protections stay).
* Sentences end at a terminator followed by whitespace: a strip never cuts an identifier
  ("gen.error_word") or a decimal, so the kept text is whole sentences."""

from __future__ import annotations

import pytest

from su_multi_arm.core.types import ScopeResult, UnsupportedConstraint
from su_multi_arm.decision import detect_intents
from su_multi_arm.generation import AggregateEvidence, Answer, AnswerContract, Claim
from su_multi_arm.generation.contract import _SENTENCE, has_label

APPROX = AggregateEvidence(id="aggregate:2", result=ScopeResult(exactness="approximate", denominator=5, value=3))
BOUNDED = AggregateEvidence(id="aggregate:4", result=ScopeResult(exactness="bounded", denominator=5, value=3))
DROPPED = AggregateEvidence(
    id="aggregate:5",
    result=ScopeResult(exactness="bounded", denominator=5, value=3, unsupported=[UnsupportedConstraint(constraint_text="in Texas", reason="field_not_filterable")]),
)
COUNT_Q = detect_intents("How many records have status open?")


def count_answer(text: str, agg: str, claim: str | None = None, kind: str = "count") -> Answer:
    return Answer(answer=text, claims=[Claim(text=claim or text, kind=kind, evidence_ids=[agg])], citations=[agg])


@pytest.mark.parametrize(
    "aggregate,text",
    [
        (APPROX, "There are approximately 3 open records."),
        (APPROX, "About 3 open records (approximate, sampled)."),
        (BOUNDED, "There are at least 3 open records."),
        (BOUNDED, "There are 3 open records in the parsed rows; the source counts more (bounded)."),
    ],
)
def test_a_labelled_count_on_an_approximate_or_bounded_aggregate_passes(aggregate, text):
    result = AnswerContract().enforce(count_answer(text, aggregate.id), evidence_ids=set(), aggregates=[aggregate], intents=COUNT_Q)
    assert result.contract == "passed" and result.answer.answer == text and not result.answer.insufficient_evidence
    assert result.unsupported_constraints  # still says no exact scope was executed


@pytest.mark.parametrize("aggregate", [APPROX, BOUNDED])
def test_an_unlabelled_count_on_a_non_exact_aggregate_still_abstains(aggregate):
    result = AnswerContract().enforce(count_answer("There are 3 open records.", aggregate.id), evidence_ids=set(), aggregates=[aggregate], intents=COUNT_Q)
    assert result.contract == "abstained" and "claim_without_exact_scope" in {v.rule for v in result.violations}


def test_a_labelled_count_with_the_wrong_number_abstains():
    result = AnswerContract().enforce(count_answer("There are approximately 7 open records.", APPROX.id), evidence_ids=set(), aggregates=[APPROX], intents=COUNT_Q)
    assert result.contract == "abstained" and "answer_number_mismatch" in {v.rule for v in result.violations}


def test_a_dropped_predicate_never_backs_even_a_labelled_count():
    result = AnswerContract().enforce(count_answer("There are approximately 3 open records.", DROPPED.id), evidence_ids=set(), aggregates=[DROPPED], intents=COUNT_Q)
    assert result.contract == "abstained"


def test_lists_and_absences_still_need_an_exact_scope():
    listing = count_answer("These are all the open records, approximately: r1, r3 and r4.", APPROX.id, kind="exhaustive_list")
    assert AnswerContract().enforce(listing, evidence_ids=set(), aggregates=[APPROX], intents=detect_intents("List all open records")).contract == "abstained"
    zero = AggregateEvidence(id="aggregate:6", result=ScopeResult(exactness="bounded", denominator=5, value=0))
    absent = count_answer("There are no open records, at least in the parsed rows.", zero.id, kind="absence")
    assert AnswerContract().enforce(absent, evidence_ids=set(), aggregates=[zero], intents=detect_intents("are there any open records?")).contract == "abstained"


def test_labels():
    assert has_label("approximately 304") and has_label("at least 12 lines") and has_label("~40 lines") and has_label("bounded.")
    assert not has_label("There are 3 open records.") and not has_label("information about the service")


# --- sentence boundaries and strips -----------------------------------------------------------------


def sentences(text: str) -> list[str]:
    return [m.group(0).strip() for m in _SENTENCE.finditer(text) if m.group(0).strip()]


def test_identifiers_and_decimals_do_not_end_a_sentence():
    text = "The top signature was gen.error_word with 1317 lines. Coverage was 99.0% on sig.py.traceback! Ends here\nnext"
    assert sentences(text) == ["The top signature was gen.error_word with 1317 lines.", "Coverage was 99.0% on sig.py.traceback!", "Ends here", "next"]


def test_a_strip_keeps_whole_sentences_around_identifiers():
    """The live garbling: an unbacked count sentence naming ``gen.error_word`` was cut at the dot,
    leaving "The largest signatures were gen." and a fragment starting mid-sentence."""
    text = "The service logs many failures. The largest signatures were gen.error_word and sig.py.traceback with 12 records. Check the deployment."
    answer = Answer(answer=text, claims=[Claim(text="overview", kind="narrative", evidence_ids=["kb:a"])], citations=["kb:a"])
    result = AnswerContract().enforce(answer, evidence_ids={"kb:a"}, aggregates=[], intents=detect_intents("Give me a health summary"))
    assert result.contract == "stripped"
    assert result.answer.answer == "The service logs many failures. Check the deployment."
    for sentence in sentences(result.answer.answer):  # whole sentences: capitalised start, terminator end
        assert sentence[0].isupper() and sentence.endswith((".", "!", "?"))
