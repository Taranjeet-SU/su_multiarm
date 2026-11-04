"""AnswerContract on the answer text (QA-04) and claim-level stripping (QA-08); 2.0.0a2.

The contract finds count, exhaustive-list and absence statements in the user-facing ``answer``
(deterministic, English-scoped, ``generation/statements.py``) and requires a typed claim of the same
kind backed by an exact ScopeResult. Numbers that are not record counts (versions, dates, ids,
decimals, percentages, durations, steps, quantities quoted from evidence) are not statements. A
spurious non-narrative claim on an answer whose text states no count/list/absence is stripped and
recorded instead of abstaining the whole answer.
"""

from __future__ import annotations

import pytest

from su_multi_arm.core.types import ScopeResult, UnsupportedConstraint
from su_multi_arm.decision import detect_intents
from su_multi_arm.generation import AggregateEvidence, Answer, AnswerContract, AnswerContractViolation, Claim
from su_multi_arm.generation.contract import answer_schema
from su_multi_arm.generation.statements import detect_statements, units_for

TARGETS = ["document", "records", "record", "status", "product"]
UNITS = units_for(TARGETS, "How do I fix a rounding problem?")
EXACT = AggregateEvidence(id="aggregate:1", result=ScopeResult(exactness="exact", denominator=36, value=12, matched_ids=["c1"]))
ZERO = AggregateEvidence(id="aggregate:2", result=ScopeResult(exactness="exact", denominator=36, value=0))
BOUNDED = AggregateEvidence(
    id="aggregate:3",
    result=ScopeResult(exactness="bounded", denominator=36, value=12, unsupported=[UnsupportedConstraint(constraint_text="CloudTax", reason="qualifier_not_applied")]),
)
PLAIN = detect_intents("How do I fix a rounding problem?", targets=TARGETS)
COUNT = detect_intents("How many records have status open?", targets=TARGETS)
LIST = detect_intents("List every pending LedgerOne record.", targets=TARGETS)
EVIDENCE = {"d1", "d2"}


def kinds(text: str, **kw) -> list[str]:
    return [s.kind for s in detect_statements(text, units=UNITS, **kw)]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("There are 12 open CloudTax tickets.", ["count"]),
        ("26 issues reported regarding the CloudTax nexus threshold.", ["count"]),
        ("Three records are open.", ["count"]),
        ("A total of 40 were processed.", ["count"]),
        ("There are exactly 2 pending LedgerOne tickets: case:0005 and case:0014. That is the complete list.", ["count", "exhaustive_list"]),
        ("These are all the open tickets.", ["exhaustive_list"]),
        ("The only pending tickets are case:1 and case:2.", ["exhaustive_list"]),
        ("There are no priority 3 tickets.", ["absence"]),
        ("No matching records were found.", ["absence"]),
        ("None of the CloudTax tickets mention it.", ["absence"]),
    ],
)
def test_count_list_and_absence_statements_are_found(text, expected):
    assert kinds(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Upgrade to version 2.3.1 and restart the service [d1].",  # version
        "The fix shipped on 2025-01-03 in release v2 [d1].",  # date, version
        "See case:0005 and #1277 for details [d1].",  # identifiers
        "Set the rounding precision to 2 decimal places for invoices [d1].",  # quantity
        "Wait 30 seconds, then retry step 3 [d1].",  # duration, step number
        "Rounding errors affect about 40% of line totals [d1].",  # percentage
        "The error appears at 10:30 every day [d1].",  # time
        "There are 3 steps: open settings, choose rounding, save [d1].",  # steps, not records
        "There is no need to restart the service [d1].",  # not an absence of records
        "Follow all the steps below [d1].",
        "Use 1 rule per product line and 2.5 as the threshold [d1].",
    ],
)
def test_numbers_that_are_not_record_counts_are_not_statements(text):
    assert kinds(text) == []


def test_a_quantity_quoted_from_evidence_is_not_a_corpus_count():
    text = "This affects 3 tickets according to the report [d1]."
    assert kinds(text) == ["count"]
    assert kinds(text, passages=["Known issue: this affects 3 tickets according to the report."]) == []


def test_list_intent_makes_an_enumeration_an_exhaustive_list():
    text = "The pending LedgerOne tickets are as follows:\n1. case:0001\n2. case:0004"
    assert kinds(text) == []
    assert set(kinds(text, list_intent=True)) == {"exhaustive_list"}


def test_question_nouns_are_units():
    units = units_for([], "How many widgets failed?")
    assert [s.kind for s in detect_statements("We saw 4 broken widgets.", units=units)] == ["count"]


# --- contract: answer text (QA-04) ----------------------------------------------------------------


def test_count_in_text_under_a_narrative_claim_abstains():
    answer = Answer(answer="There are 12 open tickets [d1].", claims=[Claim(text="open tickets exist", kind="narrative", evidence_ids=["d1"])], citations=["d1"])
    result = AnswerContract().enforce(answer, evidence_ids=EVIDENCE, aggregates=[EXACT], intents=PLAIN, targets=TARGETS)
    assert result.contract == "abstained" and [v.rule for v in result.violations] == ["answer_text_without_exact_scope"]


def test_count_in_text_backed_by_an_exact_count_claim_passes():
    answer = Answer(answer="There are 12 open records [aggregate:1].", claims=[Claim(text="12 open records", kind="count", evidence_ids=["aggregate:1"])], citations=["aggregate:1"])
    assert AnswerContract().enforce(answer, evidence_ids=EVIDENCE, aggregates=[EXACT], intents=COUNT, targets=TARGETS).contract == "passed"
    bounded = answer.model_copy(update={"claims": [Claim(text="12 open records", kind="count", evidence_ids=["aggregate:3"])], "citations": ["aggregate:3"]})
    result = AnswerContract().enforce(bounded, evidence_ids=EVIDENCE, aggregates=[BOUNDED], intents=COUNT, targets=TARGETS)
    assert result.contract == "abstained" and "answer_text_without_exact_scope" in {v.rule for v in result.violations}
    assert [u.reason for u in result.unsupported_constraints] == ["qualifier_not_applied"]


def test_absence_needs_an_exhaustive_empty_scope():
    absent = Answer(answer="There are no priority 3 records [aggregate:1].", claims=[Claim(text="none", kind="count", evidence_ids=["aggregate:1"])], citations=["aggregate:1"])
    result = AnswerContract().enforce(absent, evidence_ids=EVIDENCE, aggregates=[EXACT], intents=COUNT, targets=TARGETS)
    assert result.contract == "abstained"  # a count of 12 never backs "there are no ..."
    zero = absent.model_copy(update={"claims": [Claim(text="none", kind="absence", evidence_ids=["aggregate:2"])], "citations": ["aggregate:2"]})
    assert AnswerContract().enforce(zero, evidence_ids=EVIDENCE, aggregates=[ZERO], intents=COUNT, targets=TARGETS).contract == "passed"
    claim_only = zero.model_copy(update={"answer": "See the aggregate [aggregate:1].", "claims": [Claim(text="none", kind="absence", evidence_ids=["aggregate:1"])], "citations": ["aggregate:1"]})
    stripped = AnswerContract().enforce(claim_only, evidence_ids=EVIDENCE, aggregates=[EXACT], intents=COUNT, targets=TARGETS)
    assert stripped.contract == "stripped" and stripped.violations[0].rule == "claim_without_exact_scope"


def test_strip_mode_removes_only_the_unbacked_sentences():
    answer = Answer(
        answer="Enable per-line rounding in the tax settings [d1]. There are 12 open tickets about it. Then re-run the invoice [d2].",
        claims=[Claim(text="enable per-line rounding", kind="narrative", evidence_ids=["d1", "d2"])],
        citations=["d1", "d2"],
    )
    result = AnswerContract(on_violation="strip").enforce(answer, evidence_ids=EVIDENCE, aggregates=[], intents=PLAIN, targets=TARGETS)
    assert result.contract == "stripped" and "12" not in result.answer.answer
    assert result.answer.answer == "Enable per-line rounding in the tax settings [d1]. Then re-run the invoice [d2]."
    assert [(v.rule, v.action) for v in result.violations] == [("answer_text_without_exact_scope", "strip")]
    only = answer.model_copy(update={"answer": "There are 12 open tickets."})
    assert AnswerContract(on_violation="strip").enforce(only, evidence_ids=EVIDENCE, aggregates=[], intents=PLAIN, targets=TARGETS).contract == "abstained"
    with pytest.raises(AnswerContractViolation, match="answer_text_without_exact_scope"):
        AnswerContract(on_violation="fail").enforce(answer, evidence_ids=EVIDENCE, aggregates=[], intents=PLAIN, targets=TARGETS)


# --- contract: claim stripping on narrative answers (QA-08) -------------------------------------------


def test_spurious_count_claim_on_a_narrative_answer_is_stripped_not_abstained():
    answer = Answer(
        answer="Enable per-line rounding in the tax settings and re-run the invoice [d1].",
        claims=[
            Claim(text="Enable per-line rounding", kind="narrative", evidence_ids=["d1"]),
            Claim(text="rounding problems", kind="count", evidence_ids=["d2"]),
            Claim(text="all fixes", kind="exhaustive_list", evidence_ids=["d1"]),
        ],
        citations=["d1", "d2"],
    )
    result = AnswerContract().enforce(answer, evidence_ids=EVIDENCE, aggregates=[], intents=PLAIN, targets=TARGETS)
    assert result.contract == "stripped" and result.answer.answer == answer.answer and not result.answer.insufficient_evidence
    assert [c.kind for c in result.answer.claims] == ["narrative"] and result.answer.citations == ["d1"]
    assert {(v.rule, v.action) for v in result.violations} == {("claim_without_exact_scope", "strip")}
    with pytest.raises(AnswerContractViolation):
        AnswerContract(on_violation="fail").enforce(answer, evidence_ids=EVIDENCE, aggregates=[], intents=PLAIN, targets=TARGETS)


def test_invented_ids_still_abstain():
    answer = Answer(answer="Enable per-line rounding [d1].", claims=[Claim(text="x", kind="narrative", evidence_ids=["case:1277"])], citations=["d1"])
    result = AnswerContract().enforce(answer, evidence_ids=EVIDENCE, aggregates=[], intents=PLAIN, targets=TARGETS)
    assert result.contract == "abstained" and [v.rule for v in result.violations] == ["unknown_evidence"]


def test_answer_schema_restricts_ids_and_kinds():
    schema = answer_schema(["d1", "aggregate:1"], ["narrative"])
    reply = schema.model_validate({"answer": "a", "claims": [{"text": "t", "kind": "narrative", "evidence_ids": ["d1"]}], "citations": ["aggregate:1"]})
    assert Answer.model_validate(reply.model_dump(mode="json")).citations == ["aggregate:1"]
    with pytest.raises(ValueError):
        schema.model_validate({"answer": "a", "citations": ["case:1277"]})
    with pytest.raises(ValueError):
        schema.model_validate({"answer": "a", "claims": [{"text": "t", "kind": "count", "evidence_ids": ["d1"]}]})
    claim = schema.model_json_schema()["$defs"]["Claim"]["properties"]
    assert claim["kind"].get("enum", [claim["kind"].get("const")]) == ["narrative"]
    assert schema.__name__ == "Answer"


def test_an_empty_factual_answer_abstains():
    """Observed once in the 2.0.0a2 paid QA-08 rerun: an empty answer string with a cited narrative claim."""
    empty = Answer(answer=" ", claims=[Claim(text="x", kind="narrative", evidence_ids=["d1"])], citations=["d1"])
    result = AnswerContract().enforce(empty, evidence_ids=EVIDENCE, aggregates=[], intents=PLAIN, targets=TARGETS)
    assert result.contract == "abstained" and [v.rule for v in result.violations] == ["empty_answer"]


# --- 2.0.0a2 release review N1: detector precision; N2: stated number vs aggregate value -----------

NOT_STATEMENTS = [
    "A 404 error means the page was not found [d1].",
    "The 500 error page appears after a timeout [d1].",
    "Windows 10 users should install the update first [d1].",
    "Python 3 files need the new header [d1].",
    "If there is no error message, check the log [d1].",
    "There is no error code in the response [d1].",
    "The only file you need is settings.yaml [d1].",
    "Here is the complete list of supported currencies: USD, EUR [d1].",
]


@pytest.mark.parametrize("text", NOT_STATEMENTS)
def test_reviewer_false_positives_are_not_statements(text):
    assert kinds(text) == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Only 12 tickets are open [d1].", ["count"]),
        ("Overall, 12 tickets are open [d1].", ["count"]),
        ("One ticket is open [d1].", ["count"]),
        ("There are no error tickets [d1].", ["absence"]),
        ("That is the complete list of open tickets [d1].", ["exhaustive_list"]),
        ("The only open tickets are case:1 and case:2 [d1].", ["exhaustive_list"]),
    ],
)
def test_precision_rules_keep_the_real_statements(text, expected):
    assert kinds(text) == expected


def count_answer(text: str, evidence: str = "aggregate:1") -> Answer:
    return Answer(answer=text, claims=[Claim(text=text, kind="count", evidence_ids=[evidence])], citations=[evidence])


def test_a_stated_number_must_equal_the_backing_aggregate():
    wrong = AnswerContract().enforce(count_answer("There are 99 open records [aggregate:1]."), evidence_ids=EVIDENCE, aggregates=[EXACT], intents=COUNT, targets=TARGETS)
    assert wrong.contract == "abstained" and [v.rule for v in wrong.violations] == ["answer_number_mismatch"]
    right = AnswerContract().enforce(count_answer("There are 12 open records [aggregate:1]."), evidence_ids=EVIDENCE, aggregates=[EXACT], intents=COUNT, targets=TARGETS)
    assert right.contract == "passed"
    words = AnswerContract().enforce(count_answer("Twelve records are open [aggregate:1]."), evidence_ids=EVIDENCE, aggregates=[EXACT], intents=COUNT, targets=TARGETS)
    assert words.contract == "passed"
    share = AnswerContract().enforce(count_answer("12 of 36 records are open [aggregate:1]."), evidence_ids=EVIDENCE, aggregates=[EXACT], intents=COUNT, targets=TARGETS)
    assert share.contract == "passed"
    denominator_alone = AnswerContract().enforce(count_answer("There are 36 open records [aggregate:1]."), evidence_ids=EVIDENCE, aggregates=[EXACT], intents=COUNT, targets=TARGETS)
    assert denominator_alone.contract == "abstained"


# --- 2.0.0a3 parity fix: the population of a ratio is not the stated count -------------------------

TICKETS = AggregateEvidence(id="aggregate:1", result=ScopeResult(exactness="exact", denominator=2048, value=1400, matched_ids=["c1"]))
TICKET_TARGETS = ["ticket", "record"]
TICKET_COUNT = detect_intents("How many tickets are there?", targets=TICKET_TARGETS)


def ticket_check(text: str, aggregate: AggregateEvidence = TICKETS):
    return AnswerContract().enforce(count_answer(text), evidence_ids=EVIDENCE, aggregates=[aggregate], intents=TICKET_COUNT, targets=TICKET_TARGETS)


@pytest.mark.parametrize(
    "text",
    [
        "There are 1400 tickets [aggregate:1], out of 2048 records in the snapshot.",
        "There are 1,400 ticket records, out of a denominator of 2,048 records [aggregate:1].",
        "1400 ticket records match [aggregate:1]. The aggregate's denominator is 2048 records.",
        "1400 tickets match; denominator: 2048 records [aggregate:1].",
        "1400 tickets match, with 2,048 filtered records as the denominator [aggregate:1].",
        "1400 tickets match among 2,048 canonical records in the snapshot [aggregate:1].",
        "The count covers 1400 tickets over 2048 records in this snapshot [aggregate:1].",
        "1400 tickets match, which is 72% of 2048 records [aggregate:1].",
        "1400/2048 records are tickets [aggregate:1].",
        "1400 of 2000 records are tickets [aggregate:1].",
        "These sum to 1400 ticket records out of 5000 records [aggregate:1].",
    ],
)
def test_a_cited_denominator_or_ratio_population_is_not_a_mismatch(text):
    result = ticket_check(text)
    assert result.contract == "passed", (text, result.violations)


@pytest.mark.parametrize(
    "text",
    [
        "There are 1450 tickets [aggregate:1], out of 2048 records in the snapshot.",  # wrong count, right denominator
        "There are 2048 tickets [aggregate:1].",  # the population stated as the count
        "A total of 2048 tickets match [aggregate:1].",
        "There are 1400 tickets and 1500 records [aggregate:1].",  # a second, unbacked number
        "1450 ticket records match among 2,048 canonical records [aggregate:1].",
    ],
)
def test_a_wrong_count_next_to_a_population_still_abstains(text):
    result = ticket_check(text)
    assert result.contract == "abstained" and "answer_number_mismatch" in [v.rule for v in result.violations], text


def test_group_values_totals_and_their_sum_are_values_of_the_evidence():
    from su_multi_arm.core.types import GroupRow

    groups = [GroupRow(key={"status": "closed"}, value=1250, count=1250), GroupRow(key={"status": "open"}, value=150, count=150)]
    grouped = AggregateEvidence(id="aggregate:1", result=ScopeResult(exactness="exact", denominator=2048, value={"closed": 1250, "open": 150}, groups=groups, total=1400))
    for text in ("150 tickets are open [aggregate:1].", "These sum to 1400 tickets [aggregate:1].", "1250 tickets are closed out of 2048 records [aggregate:1]."):
        assert ticket_check(text, grouped).contract == "passed", text
    assert ticket_check("151 tickets are open [aggregate:1].", grouped).contract == "abstained"
