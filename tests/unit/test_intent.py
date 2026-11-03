"""Intent pre-pass (plan §3.7): deterministic grammar, never fails, English only."""

from __future__ import annotations

import pytest

from su_multi_arm.decision import detect_intents

ID = r"\bREC-\d{3,}\b"


@pytest.mark.parametrize(
    ("query", "kinds"),
    [
        ("How many records have status open?", ["count"]),
        ("What is the total number of notes?", ["count"]),
        ("What is the total amount for project Alpha?", ["total"]),
        ("List all open records", ["list"]),
        ("Are there any records for project Omega?", ["absence"]),
        ("When was the earliest created record?", ["earliest"]),
        ("Which record was most recent?", ["latest"]),
        ("How many records exist and list all of them", ["count", "list"]),
        ("Explain the certificate renewal procedure", []),
        ("Combien de dossiers sont ouverts ?", []),  # non-English: no intent, never a wrong one
    ],
)
def test_aggregate_grammar(query, kinds):
    intents = detect_intents(query)
    assert intents.kinds() == kinds
    assert intents.language == "en"
    assert all(query[a.start : a.end] == a.text for a in intents.aggregates)


def test_identifiers_quotes_and_follow_up_markers():
    intents = detect_intents('Show REC-1001 and REC-1002 with "certificate error" and \'rounding\'', [ID])
    assert intents.exact_ids == ["REC-1001", "REC-1002"]
    assert intents.quoted_phrases == ["certificate error", "rounding"]
    assert intents.follow_up is False and intents.primary() == "lookup"
    assert detect_intents("and what about the same for Beta?").follow_up is True
    assert detect_intents("REC-9 details", [ID]).exact_ids == []  # pattern requires three digits
    assert detect_intents("plain question").primary() == "retrieve"
    assert detect_intents("how many").primary() == "aggregate"


TARGETS = ["record", "records", "documents", "status", "project", "amount", "created at", "created", "resolved on", "resolved"]
PLAIN_QUESTIONS = [
    "Show me all the steps to renew a certificate",
    "What happens every time the service restarts?",
    "Which article explains the latest release notes?",
    "Why does the export fail after every upgrade?",
    "What is the total cost of ownership described in the guide?",
    "When did the oldest version stop receiving updates?",
    "Is there any way to reset the connector token?",
    "Give me the most recent guidance on rounding",
    "Does the first time setup require admin rights?",
    "Summarize all of the configuration options for the exporter",
    "What was the last recorded message in the log before the crash?",
    "Are there any known workarounds for the sync error?",
]


@pytest.mark.parametrize("query", PLAIN_QUESTIONS)
def test_plain_questions_with_aggregate_words_are_retrieval_when_targets_are_known(query):
    intents = detect_intents(query, targets=TARGETS)
    assert intents.aggregates == [] and intents.primary() == "retrieve", query


@pytest.mark.parametrize(
    ("query", "kinds"),
    [
        ("How many records have status open?", ["count"]),
        ("What is the total amount for project Alpha?", ["total"]),
        ("What is the total of all records?", ["total"]),
        ("List all open records", ["list"]),
        ("Show me every record for project Beta", ["list"]),
        ("Are there any records for project Omega?", ["absence"]),
        ("Are there any pending records?", ["absence"]),
        ("When was the earliest created record?", ["earliest"]),
        ("When was the earliest record?", ["earliest"]),
        ("Which record was most recent?", ["latest"]),
        ("List all records about billing", ["list"]),
    ],
)
def test_targeted_grammar_keeps_structured_shapes(query, kinds):
    assert detect_intents(query, targets=TARGETS).kinds() == kinds


@pytest.mark.parametrize("query", ["???", "   x", "éèê 中文", "'", '""', "a" * 16000])
def test_pre_pass_never_fails(query):
    intents = detect_intents(query, [ID, r"[A-Z]+-\d+"])
    assert intents.model_dump()["language"] == "en"
