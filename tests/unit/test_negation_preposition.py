"""A negation cue may be followed by a preposition before the value ("not from X")."""

from __future__ import annotations

import pytest

from su_multi_arm.decision.qualifiers import _negation_start


def _cue(question: str, value: str) -> str | None:
    start = _negation_start(question, question.index(value))
    return None if start is None else question[start : question.index(value)].strip()


@pytest.mark.parametrize(
    ("question", "value", "cue"),
    [
        ("How many tickets are not from Corptax?", "Corptax", "not from"),
        ("How many tickets are not in Backlog?", "Backlog", "not in"),
        ("Tickets excluding those from Acme", "Acme", None),
        ("How many tickets are not Closed?", "Closed", "not"),
        ("Tickets other than Acme", "Acme", "other than"),
        ("Tickets without the Acme label", "Acme", "without the"),
    ],
)
def test_negation_cue_with_optional_preposition(question, value, cue):
    assert _cue(question, value) == cue


def test_a_preposition_alone_is_not_a_negation():
    assert _cue("How many tickets are from Corptax?", "Corptax") is None
    assert _cue("How many tickets are in Backlog?", "Backlog") is None
