"""``generation.contract.on_violation: annotate`` (2026-11-09, opt-in; default stays ``strip``).

The generator's evidence-cited answer is kept and one deterministic limitation note per violated
claim/statement is appended (``contract: annotated``, ``limitations``). Hard guarantees hold in
annotate mode too: unknown evidence (also inline), a factual answer without citations and an empty
answer abstain; a number that contradicts the exact aggregate is removed with its sentence and the
exact result is stated; an uncited aggregate claim, an ``examples`` claim citing no record and an
unbacked statement whose paragraph cites nothing are removed. Synthetic data only.
"""

from __future__ import annotations

import pytest

from su_multi_arm.core.config import AnswerContractConfig
from su_multi_arm.core.types import ScopeResult
from su_multi_arm.decision.intent import detect_intents
from su_multi_arm.generation.contract import LIMITATIONS_HEADING, NOTE_REMOVED, AggregateEvidence, AnswerContract, AnswerContractViolation
from su_multi_arm.models import Answer, Claim
from su_multi_arm.runtime.trace import Trace

TARGETS = ["ticket", "record", "records", "status", "module", "organization"]
QUESTION = "How many tickets mention the login error?"


def aggregate(value: int, exactness: str = "exact", ident: str = "aggregate:1") -> AggregateEvidence:
    unsupported = [] if exactness == "exact" else [{"constraint_text": "login error", "reason": "qualifier_not_applied"}]
    result = ScopeResult(exactness=exactness, denominator=200, value=value, total=value, unsupported=unsupported)
    return AggregateEvidence(id=ident, result=result, rendering=f"records: {value} ({exactness}; {value} records matched).")


def run(answer: Answer, aggregates: list[AggregateEvidence], mode: str = "annotate", question: str = QUESTION, evidence: set[str] = frozenset({"ticket:1", "ticket:2"}), trace=None):
    ids = set(evidence) | {a.id for a in aggregates}
    return AnswerContract(mode).enforce(answer, evidence_ids=ids, aggregates=aggregates, intents=detect_intents(question, targets=TARGETS), question=question, targets=TARGETS, trace=trace)


BOUNDED_ANSWER = Answer(
    answer="About the login error: 5 tickets mention it [aggregate:1]. Ticket 1 shows the error page [ticket:1].",
    claims=[Claim(text="5 tickets mention the login error", kind="count", evidence_ids=["aggregate:1"]), Claim(text="Ticket 1 shows it", evidence_ids=["ticket:1"])],
    citations=["aggregate:1", "ticket:1"],
)


def test_config_accepts_annotate_and_keeps_strip_as_default():
    assert AnswerContractConfig().on_violation == "strip"
    assert AnswerContractConfig(on_violation="annotate").on_violation == "annotate"


def test_a_count_on_a_bounded_aggregate_is_kept_with_a_note():
    trace = Trace()
    result = run(BOUNDED_ANSWER, [aggregate(5, "bounded")], trace=trace)
    assert result.contract == "annotated"
    assert result.answer.answer.startswith(BOUNDED_ANSWER.answer)  # the generator's text stands
    assert result.limitations == [
        'I can\'t verify this count is exact for all records: "5 tickets mention the login error".',
        'I can\'t verify this count is exact for all records: "5 tickets".',
    ]
    assert result.answer.answer.endswith(LIMITATIONS_HEADING + "\n- " + "\n- ".join(result.limitations))
    assert {v.action for v in result.violations} == {"annotate"} and result.answer.claims == BOUNDED_ANSWER.claims
    event = [e for s in trace.spans for e in s.events if e.get("name") == "answer_contract"][-1]
    assert event["outcome"] == "annotated" and event["limitations"] == 2


def test_the_same_answer_abstains_under_strip_for_an_aggregate_question():
    assert run(BOUNDED_ANSWER, [aggregate(5, "bounded")], mode="strip").contract == "abstained"


def test_an_unbacked_list_and_absence_get_their_notes():
    text = "These are all the tickets: ticket 1 and ticket 2 [ticket:1] [ticket:2].\n\nThere are no other tickets about it [aggregate:1]."
    answer = Answer(answer=text, citations=["ticket:1", "ticket:2", "aggregate:1"])
    result = run(answer, [aggregate(2, "bounded")], question="List all tickets about the login error.")
    assert result.contract == "annotated"
    assert any(n.startswith("This list may not be complete") for n in result.limitations)


def test_hard_guarantees_abstain_in_annotate_mode():
    unknown = BOUNDED_ANSWER.model_copy(update={"citations": ["aggregate:1", "ticket:9"]})
    assert run(unknown, [aggregate(5, "bounded")]).contract == "abstained"
    inline_unknown = BOUNDED_ANSWER.model_copy(update={"answer": BOUNDED_ANSWER.answer + " See [ticket:9]."})
    assert run(inline_unknown, [aggregate(5, "bounded")]).violations[0].rule == "unknown_evidence"
    uncited = Answer(answer="5 tickets mention the login error.", claims=[], citations=[])
    assert run(uncited, [aggregate(5, "bounded")]).contract == "abstained"
    empty = Answer(answer="  ", citations=["aggregate:1"])
    assert run(empty, [aggregate(5)]).contract == "abstained"


def test_a_number_contradicting_the_exact_aggregate_is_removed_and_corrected():
    answer = Answer(
        answer="7 tickets mention the login error [aggregate:1]. Ticket 1 shows the error page [ticket:1].",
        claims=[Claim(text="7 tickets", kind="count", evidence_ids=["aggregate:1"])],
        citations=["aggregate:1", "ticket:1"],
    )
    result = run(answer, [aggregate(5)])
    assert result.contract == "annotated" and "7 tickets" not in result.answer.answer.split(LIMITATIONS_HEADING)[0]
    assert result.limitations == ["A stated count that differs from the exact result was removed; the exact result is: records: 5 (exact; 5 records matched). [aggregate:1]"]
    assert [v.action for v in result.violations] == ["strip"]


def test_uncited_statements_and_claims_are_removed_not_annotated():
    answer = Answer(
        answer="Ticket 1 shows the error page [ticket:1].\n\nAbout 40 tickets were affected overall.",
        claims=[Claim(text="40 tickets", kind="count", evidence_ids=[]), Claim(text="Ticket 1", evidence_ids=["ticket:1"])],
        citations=["ticket:1"],
    )
    result = run(answer, [aggregate(5, "bounded")], question="What does ticket 1 say about the login error?")
    assert result.contract == "annotated"
    body = result.answer.answer.split(LIMITATIONS_HEADING)[0]
    assert "40 tickets" not in body and "Ticket 1 shows the error page" in body
    assert NOTE_REMOVED in result.limitations and all(c.evidence_ids for c in result.answer.claims)


def test_an_examples_claim_citing_no_record_is_removed():
    answer = Answer(
        answer="5 tickets mention it [aggregate:1]. Examples: ticket 1 [ticket:1].",
        claims=[Claim(text="5 tickets", kind="count", evidence_ids=["aggregate:1"]), Claim(text="examples", kind="examples", evidence_ids=["aggregate:1"])],
        citations=["aggregate:1", "ticket:1"],
    )
    result = run(answer, [aggregate(5)])
    assert result.contract == "annotated" and [c.kind for c in result.answer.claims] == ["count"]


def test_a_clean_answer_passes_unchanged_in_annotate_mode():
    answer = Answer(answer="5 tickets mention the login error [aggregate:1].", claims=[Claim(text="5 tickets", kind="count", evidence_ids=["aggregate:1"])], citations=["aggregate:1"])
    result = run(answer, [aggregate(5)])
    assert result.contract == "passed" and result.answer == answer and result.limitations == []


def test_fail_mode_still_raises():
    with pytest.raises(AnswerContractViolation):
        run(BOUNDED_ANSWER, [aggregate(5, "bounded")], mode="fail")
