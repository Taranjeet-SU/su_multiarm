"""QA findings of the 2026-11-09 staging run (QA-07, QA-08), fixed on v2/phase-0. Synthetic text
reproducing the reported shapes only; no service, no key.

* QA-07: the strip contract removed a scope disclaimer ("This is one ticket's resolution, not a
  general fix ...") because "one ticket" read as an unbacked corpus count.
* QA-08: strip removed sentences, not Markdown list items: a stripped first sentence of a bullet
  took the "- **Label:**" marker with it and left the bullet's next sentence as a dangling line.
"""

from __future__ import annotations

import pytest

from su_multi_arm.decision.intent import detect_intents
from su_multi_arm.generation import Answer, AnswerContract, Claim
from su_multi_arm.generation.statements import detect_statements

pytestmark = pytest.mark.qa

TARGETS = ["record", "status", "product"]


@pytest.mark.parametrize(
    "sentence",
    [
        "This is one ticket's resolution, not a general fix for every cause of this symptom.",
        "It shows a record's history only.",
    ],
)
def test_qa07_a_possessive_one_ticket_is_not_a_count_statement(sentence):
    assert detect_statements(sentence, units=["ticket", "tickets", "record", "records"]) == []


def test_qa07_the_disclaimer_survives_the_strip_contract():
    text = "Restart the indexer [kb:a]. This is one ticket's resolution, not a general fix for every cause of this symptom."
    answer = Answer(answer=text, claims=[Claim(text="Restart the indexer.", kind="narrative", evidence_ids=["kb:a"])], citations=["kb:a"])
    question = "How was the indexer stall fixed?"
    result = AnswerContract().enforce(answer, evidence_ids={"kb:a"}, aggregates=[], intents=detect_intents(question, [], ["ticket"]), question=question, targets=["ticket"])
    assert result.contract == "passed" and result.answer.answer == text


def test_qa08_stripping_a_bullets_first_sentence_keeps_the_list_structure():
    text = (
        "Check these leads.\n\n"
        "- **Check the parser update:** There are 14 open tickets about it. The parser was disabled in one environment [kb:a].\n"
        "- **Check the connection:** Re-authenticate the content source [kb:b]."
    )
    answer = Answer(answer=text, claims=[Claim(text="Narrative.", kind="narrative", evidence_ids=["kb:a", "kb:b"])], citations=["kb:a", "kb:b"])
    question = "What should I check?"
    result = AnswerContract().enforce(answer, evidence_ids={"kb:a", "kb:b"}, aggregates=[], intents=detect_intents(question, [], TARGETS), question=question, targets=TARGETS)
    assert result.contract == "stripped"
    lines = [line for line in result.answer.answer.splitlines() if line.strip()]
    assert lines == [
        "Check these leads.",
        "- **Check the parser update:** The parser was disabled in one environment [kb:a].",
        "- **Check the connection:** Re-authenticate the content source [kb:b].",
    ]
