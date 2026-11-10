"""AnswerContract fixes from the 2026-11-08 production-workload parity rerun (synthetic shapes).

Defect 1: answers that carried the correct exact aggregate were refused because the generator also
showed a few example records, or wrote a disclaimer such as "this is not a complete list".

* An ``examples`` claim (a bounded sample of cited records, no completeness) next to a count claim
  backed by an exact aggregate lets an enumerated sample block pass on a list question.
* A completeness phrase that the sentence denies or only advises is not an exhaustive-list
  statement; a completeness phrase without such a denial still needs an exact exhaustive scope.
* An ``examples`` claim without record citations, over the cap, or claiming completeness is
  ``examples_claim_invalid`` and never backs anything.
"""

from __future__ import annotations

import pytest

from su_multi_arm.core.types import ScopeResult, UnsupportedConstraint
from su_multi_arm.decision import detect_intents
from su_multi_arm.generation import AggregateEvidence, Answer, AnswerContract, Claim
from su_multi_arm.generation.contract import MAX_EXAMPLES, answer_schema
from su_multi_arm.generation.statements import completeness_disclaimed, detect_statements, units_for

TARGETS = ["record", "records", "status", "module", "region"]
EXACT = AggregateEvidence(id="aggregate:1", result=ScopeResult(exactness="exact", denominator=400, value=35, matched_ids=["r1", "r2"], matched_ids_truncated=True))
BOUNDED = AggregateEvidence(
    id="aggregate:1",
    result=ScopeResult(exactness="bounded", denominator=400, value=35, unsupported=[UnsupportedConstraint(constraint_text="north", reason="qualifier_not_applied")]),
)
LIST_Q = "List every open record in module Alpha."
COUNT_Q = "How many open records are in module Alpha?"
LIST = detect_intents(LIST_Q, targets=TARGETS)
COUNT = detect_intents(COUNT_Q, targets=TARGETS)
EVIDENCE = {"r1", "r2", "r3", "aggregate:1"}

SAMPLE_TEXT = (
    "Exactly 35 open records are in module Alpha [aggregate:1].\n\n"
    "Examples of matching records (a sample, not the complete list):\n"
    "- r1: login fails after reset [r1]\n"
    "- r2: export times out [r2]\n"
    "- r3: missing translation [r3]\n"
)


def sample_answer(text: str = SAMPLE_TEXT, examples_text: str = "Three example records: r1, r2 and r3.", ids: list[str] | None = None) -> Answer:
    return Answer(
        answer=text,
        claims=[
            Claim(text="35 open records are in module Alpha.", kind="count", evidence_ids=["aggregate:1"]),
            Claim(text=examples_text, kind="examples", evidence_ids=ids if ids is not None else ["r1", "r2", "r3"]),
        ],
        citations=["aggregate:1", "r1", "r2", "r3"],
    )


def enforce(answer: Answer, *, aggregates=(EXACT,), intents=LIST, question=LIST_Q, evidence=EVIDENCE):
    return AnswerContract().enforce(answer, evidence_ids=set(evidence), aggregates=list(aggregates), intents=intents, question=question, targets=TARGETS)


# --- the examples claim (structural fix) ------------------------------------------------------------


def test_an_exact_count_with_a_framed_sample_list_passes():
    result = enforce(sample_answer())
    assert result.contract == "passed" and result.violations == []
    assert [c.kind for c in result.answer.claims] == ["count", "examples"]


def test_a_sample_list_without_an_examples_claim_is_still_refused():
    answer = sample_answer()
    answer = answer.model_copy(update={"claims": answer.claims[:1]})
    result = enforce(answer)
    assert result.contract == "abstained" and "answer_text_without_exact_scope" in {v.rule for v in result.violations}


def test_a_sample_list_next_to_a_bounded_count_is_refused():
    result = enforce(sample_answer(), aggregates=(BOUNDED,))
    assert result.contract == "abstained"


def test_a_sample_list_that_is_not_framed_as_examples_is_refused():
    text = "Exactly 35 open records are in module Alpha [aggregate:1].\n\n- r1 [r1]\n- r2 [r2]\n- r3 [r3]\n"
    result = enforce(sample_answer(text=text))
    assert result.contract == "abstained" and "answer_text_without_exact_scope" in {v.rule for v in result.violations}


def test_a_completeness_claim_still_needs_an_exact_exhaustive_scope():
    text = SAMPLE_TEXT + "\nThat is the complete list."
    result = enforce(sample_answer(text=text))
    assert result.contract == "abstained" and "answer_text_without_exact_scope" in {v.rule for v in result.violations}


@pytest.mark.parametrize(
    "claim_text,ids,detail",
    [
        ("These are all the matching records.", ["r1", "r2"], "states completeness"),
        ("The complete list of open records.", ["r1"], "states completeness"),
        ("Three examples.", ["aggregate:1"], "no record evidence"),
        ("Many examples.", [f"r{i}" for i in range(MAX_EXAMPLES + 1)], "more than"),
    ],
)
def test_an_invalid_examples_claim_backs_nothing(claim_text, ids, detail):
    evidence = EVIDENCE | set(ids)
    result = enforce(sample_answer(examples_text=claim_text, ids=ids), evidence=evidence)
    rules = {v.rule for v in result.violations}
    assert "examples_claim_invalid" in rules and any(detail in v.detail for v in result.violations)
    assert result.contract == "abstained"  # the enumerated block lost its backing on a list question


def test_an_invalid_examples_claim_on_a_count_question_is_stripped():
    text = "Exactly 35 open records are in module Alpha [aggregate:1]. For example r1 [r1]."
    answer = sample_answer(text=text, examples_text="These are all the matching records.", ids=["r1"])
    result = enforce(answer, intents=COUNT, question=COUNT_Q)
    assert result.contract == "stripped" and [c.kind for c in result.answer.claims] == ["count"]


def test_the_examples_claim_never_backs_a_count_or_an_absence():
    answer = Answer(
        answer="There are 35 open records [r1].",
        claims=[Claim(text="35 open records", kind="examples", evidence_ids=["r1"])],
        citations=["r1"],
    )
    result = enforce(answer, intents=COUNT, question=COUNT_Q)
    assert result.contract == "abstained"


def test_the_generator_schema_offers_the_examples_kind():
    schema = answer_schema(["aggregate:1", "r1"]).model_json_schema()
    kinds = schema["$defs"]["Claim"]["properties"]["kind"]["enum"]
    assert "examples" in kinds and "count" in kinds
    narrative_only = answer_schema(["r1"], ("narrative",)).model_json_schema()["$defs"]["Claim"]["properties"]["kind"]
    assert "examples" not in str(narrative_only)  # no aggregate evidence: narrative claims only (QA-08)


# --- disclaimers are not completeness claims -------------------------------------------------------

UNITS = units_for(TARGETS, COUNT_Q)


def kinds(text: str, question: str = COUNT_Q, list_intent: bool = False) -> list[str]:
    return [s.kind for s in detect_statements(text, units=units_for(TARGETS, question), list_intent=list_intent, fields=TARGETS)]


@pytest.mark.parametrize(
    "text",
    [
        "The lineage keeps 8 of the matched ids, so this is not a complete list.",
        "No complete list of the matching records is available in the evidence.",
        "This answer does not give a complete list of the matching records.",
        "The breakdown shows 10 groups, so it is not a complete list of regions.",
        "The full list is not available in this evidence.",
        "Advice, not evidence: to get the full list of matching records, run a filtered export.",
        "Re-run the count with an exact aggregate and a full list of matched record IDs.",
        "Suggested check (advice): rerun the ranking with the full list of modules shown.",
        "These are examples only and are not the full set.",
        "This answer does not claim they are the full set.",
        "The tie can be confirmed against the full set.",
        "The retrieved passages are a selection, not a complete list.",
    ],
)
def test_denied_or_advised_completeness_is_not_a_statement(text):
    assert "exhaustive_list" not in kinds(text)


@pytest.mark.parametrize(
    "text",
    [
        "That is the complete list.",
        "Here is the full list of open records.",
        "These are all the open records.",
        "Not only is this the complete list, it is current.",
    ],
)
def test_asserted_completeness_is_still_a_statement(text):
    assert "exhaustive_list" in kinds(text)


def test_completeness_disclaimed_reads_the_sentence_only():
    text = "Nothing is missing here. That is the complete list."
    start = text.index("complete")
    assert not completeness_disclaimed(text, start, start + len("complete list"))


@pytest.mark.parametrize(
    "text",
    [
        "Missing or invalid values: 0. Zero match: false.",
        "The result is exact, with no zero match and no missing values.",
        "None of the supplied record passages mention the error.",
    ],
)
def test_labels_double_negatives_and_passage_references_are_not_absences(text):
    assert "absence" not in kinds(text)


def test_records_without_a_value_are_a_count_not_an_absence():
    assert kinds("Six records have no status recorded in this snapshot.") == ["count"]
    assert kinds("There are no open records.") == ["absence"]  # a real absence is still found


def test_auxiliary_words_after_how_many_are_not_units():
    question = "How many are classified as defects?"
    assert "are" not in units_for(TARGETS, question)
    assert kinds("The evidence lists no individual record, so none are named here.", question=question) == []
