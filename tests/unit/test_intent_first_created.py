"""'The first Acme ticket created' / 'the last ticket opened' name the earliest / latest record, so a structured step for them is
not dropped as unsolicited. Plain sentences with the word 'first' stay retrieval questions. Synthetic data only."""

from __future__ import annotations

import pytest

from su_multi_arm.decision import detect_intents
from su_multi_arm.decision.intent import GENERIC_UNITS

TARGETS = ["ticket", "assignee", *GENERIC_UNITS]


@pytest.mark.parametrize(
    "query",
    [
        "Which was the first Acme ticket created?",
        "Which was the first Hunter Douglas ticket created?",
        "What is the first ticket opened for Acme?",
        "Which was the first Globex ticket filed?",
    ],
)
def test_first_record_created_is_an_earliest_question(query):
    assert "earliest" in detect_intents(query, targets=TARGETS).kinds()


@pytest.mark.parametrize(
    "query",
    [
        "Which was the last Acme ticket created?",
        "What is the last ticket opened for Acme?",
    ],
)
def test_last_record_created_is_a_latest_question(query):
    assert "latest" in detect_intents(query, targets=TARGETS).kinds()


@pytest.mark.parametrize(
    "query",
    [
        "What is the first thing to check when the page loads slowly?",
        "How do I create my first ticket?",
        "The first step is to restart the service, then it was created again",
    ],
)
def test_other_uses_of_first_stay_retrieval_questions(query):
    assert detect_intents(query, targets=TARGETS).kinds() == []
