"""``annotate``: a count stated from a cited aggregate is kept with its note wherever the inline
marker sits (staging round 5, N12 hardening; ``AnswerContract._cited_number``).

The private round-5 q048 shape, synthetic: a ranked SQL result (one row, a bounded count of 17 for
the second organisation) cited by the answer's claim and citations. Today's sample put ``[aggregate:1]``
in the count's sentence and kept the count with a limitation note; the unified sample put it in an
earlier paragraph and the count was removed as uncited. Both now keep it with the note.
"""

from __future__ import annotations

from su_multi_arm.core.types import ScopeResult, UnsupportedConstraint
from su_multi_arm.decision.intent import Intents
from su_multi_arm.generation import AggregateEvidence, AnswerContract
from su_multi_arm.models import Answer, Claim

FLAG = UnsupportedConstraint(constraint_text="structured scope without an aggregate operator in the question", reason="qualifier_not_applied", suggestion="not an exact answer")
RANKED = AggregateEvidence(
    id="aggregate:1",
    result=ScopeResult(exactness="bounded", denominator=1, total=1, unsupported=[FLAG]),
    rendering="SQL result: organization=Northwind Ltd, tickets=17 (bounded; 1 row; ranked position 2 of the ordering: OFFSET 1 skipped the first 1).",
    rows=[{"values": {"organization": "Northwind Ltd", "tickets": 17}}],
    columns=["organization", "tickets"],
)
QUESTION = "Who is second for widget mentions?"


def enforce(text: str) -> object:
    answer = Answer(
        answer=text,
        claims=[Claim(text="Northwind Ltd is second for widget mentions with 17 tickets", kind="narrative", evidence_ids=["aggregate:1"])],
        citations=["aggregate:1"],
    )
    return AnswerContract("annotate").enforce(answer, evidence_ids={"aggregate:1"}, aggregates=[RANKED], intents=Intents(), question=QUESTION, targets=["ticket"])


def test_inline_marker_in_the_count_sentence_keeps_the_count_with_a_note():
    result = enforce("**Northwind Ltd** is second for widget mentions with 17 tickets [aggregate:1].")
    assert result.contract == "annotated" and "17 tickets" in result.answer.answer
    assert any(n.startswith("I can't verify this count is exact") for n in result.limitations)


def test_the_marker_in_another_paragraph_keeps_the_count_too():
    result = enforce("**Northwind Ltd** is second for widget mentions [aggregate:1].\n\nAccording to the result, Northwind Ltd has 17 tickets mentioning widgets.")
    assert result.contract == "annotated" and "17 tickets" in result.answer.answer
    assert any(n.startswith("I can't verify this count is exact") for n in result.limitations)
    assert "A statement that cited no supplied evidence was removed." not in result.limitations


def test_a_number_the_cited_aggregate_does_not_hold_is_still_removed():
    result = enforce("**Northwind Ltd** is second for widget mentions [aggregate:1].\n\nNorthwind Ltd has 23 tickets mentioning widgets.")
    assert "23 tickets" not in result.answer.answer
    assert "A statement that cited no supplied evidence was removed." in result.limitations
