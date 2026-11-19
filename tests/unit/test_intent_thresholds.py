"""Group-size thresholds ("more than 150 tickets", "at least 50", "between 70 and 80") are aggregate questions when they sit next to a
structured target, so a structured step for them is not dropped as unsolicited. Plain sentences with the same words stay retrieval
questions. Synthetic data only."""

from __future__ import annotations

import pytest

from su_multi_arm.decision import detect_intents
from su_multi_arm.decision.intent import GENERIC_UNITS

TARGETS = ["ticket", "assignee", "project", *GENERIC_UNITS]


@pytest.mark.parametrize(
    "query",
    [
        "Which assignees have more than 150 tickets?",
        "Which projects have fewer than 10 tickets?",
        "Which assignees have at least 50 tickets?",
        "Which projects have at most 5 tickets?",
        "Which projects have between 70 and 80 tickets?",
        "Which assignees have less than 20 tickets?",
    ],
)
def test_a_threshold_next_to_a_target_is_an_aggregate_question(query):
    assert detect_intents(query, targets=TARGETS).kinds() == ["count"]


@pytest.mark.parametrize(
    "query",
    [
        "The service restarts more than 3 times a day, how do I fix it?",
        "Explain why the page loads at least 2 seconds slower than before",
        "Our uploads fail at most of the time on large files",
    ],
)
def test_the_same_words_without_a_target_stay_a_retrieval_question(query):
    assert detect_intents(query, targets=TARGETS).kinds() == []


def test_without_a_schema_the_permissive_grammar_is_unchanged():
    assert detect_intents("Which assignees have more than 150 tickets?").kinds() == []
    assert detect_intents("How many tickets have more than 3 comments?").kinds() == ["count"]  # explicit count phrases still fire alone
