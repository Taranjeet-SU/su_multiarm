"""The operations-half answer contract (staging round 5, N14; ``generation/operations.py``).

Neutral, synthetic sentences in the shapes the private round-5 evaluation found: a current halt
asserted from an earlier record, an opening "Yes ... disruptions", earlier records labelled as
recent incidents, a failure count tied to a cause, and a restatement of the operations evidence.
"""

from __future__ import annotations

import pytest

from su_multi_arm.core.types import ScopeResult
from su_multi_arm.generation.contract import AggregateEvidence
from su_multi_arm.generation.operations import NOTES, OperationsSection, backing_of, compose, drop_empty_headings, section_labels
from su_multi_arm.models import Answer, Claim
from su_multi_arm.profiles import prompts

RENDERING = "4 issue groups in last 3 days (rolling, to 2026-11-09): 12 observed episodes, 340 rule-matched messages. Outage status, cause and recovery are not established."
SHOWN = RENDERING.rstrip(".")  # ``contract.labelled`` appends the exactness label before the final period
REFUSED = "Not answered: no success heartbeats are recorded; whether a job runs cannot be read from failure rules."


def aggregate(id_: str, rendering: str, rows: list[dict] | None = None) -> AggregateEvidence:
    return AggregateEvidence(id=id_, result=ScopeResult(exactness="bounded", denominator=0), rendering=rendering, rows=rows or [], columns=list((rows or [{}])[0]))


def section(backing: set[str] | None = None, notes: list[str] | None = None) -> OperationsSection:
    rows = [{"service": "crawler", "signature_id": "crawler.fetch_timeout", "episodes": 3}]
    return OperationsSection(
        aggregates=[aggregate("aggregate:1", REFUSED), aggregate("aggregate:2", RENDERING, rows)],
        notes=notes if notes is not None else ["No period was named, so the default live view is shown: last 3 days."],
        label="Current logs:",
        knowledge_label="Past tickets/docs:",
        backing=backing or set(),
    )


@pytest.mark.parametrize(
    "sentence,rule",
    [
        ("The evidence confirms that the crawl of the help articles has halted [ticket:7].", "operations_claim_unbacked"),
        ("The evidence confirms the crawl has halted when it should run every 30 minutes [ticket:7].", "operations_claim_unbacked"),
        ("Yes, there is evidence of slow search and service disruptions:", "operations_claim_unbacked"),
        ("The crawler is down right now [ticket:7].", "operations_claim_unbacked"),
        ("The job recovered automatically after the timeout [ticket:7].", "operations_claim_unbacked"),
        ("The queue delay is currently caused by the failures in the analytics services.", "operations_claim_unbacked"),
        ("### Recent Incidents", "records_presented_as_current"),
        ("Several customers are currently experiencing similar crawl problems [ticket:3].", "records_presented_as_current"),
        ("The logs record 12 failure episodes [aggregate:2].", "operations_restated"),
        ("Authentication failures are the largest category [aggregate:1, ticket:4].", "operations_restated"),
    ],
)
def test_flagged_sentences(sentence, rule):
    assert section().flags(sentence) == rule


@pytest.mark.parametrize(
    "sentence",
    [
        "An earlier ticket reported that the crawl had stopped after a credential change [ticket:7].",  # past narrative
        "Crawls can stop when the previous run is still marked as running [ticket:8].",  # hypothetical
        "No outage is established by the evidence.",  # negated
        "Whether the job recovered cannot be read from these records.",  # negated
        "The fix in that ticket was to rotate the token [ticket:2].",
        "Check the scheduler settings for the content source.",
        "The documentation explains how to restart a stuck crawl [doc:1].",
        "Users have reported that the usage reports are getting stuck in the export queue [ticket:3].",  # what a record reports
        "Users have experienced reports remaining in the queue for over a day [ticket:3].",
        "You are experiencing an authentication failure when connecting the connector.",  # the asker's own report
        "Four closed tickets document outages with a root cause analysis [aggregate:9].",  # earlier records, not the operations half  # 'explains' about a doc, hypothetical-free but no state assertion
    ],
)
def test_record_narrative_negations_and_hypotheticals_are_kept(sentence):
    assert section().flags(sentence) is None


def test_a_state_an_operations_item_states_is_kept():
    stated = section(backing={"halt"})
    assert stated.flags("The crawl has halted.") is None
    assert stated.flags("The crawl has halted and there is an outage.") == "operations_claim_unbacked"  # the outage is not stated


def test_backing_reads_liveness_rows_and_series_only():
    assert backing_of([{"intent": "log_liveness", "rows": [{"status": "halted"}], "history": {"recovery": [{"status": "recovered"}]}}]) == {"halt", "recovery"}
    assert backing_of([{"intent": "log_liveness", "rows": [{"status": "running"}], "rendering": "0 halted, 1 running"}]) == set()
    assert backing_of([{"intent": "issue_history", "rendering": RENDERING}]) == set()


def test_check_removes_flagged_sentences_claims_short_answer_and_empty_headings():
    text = (
        "Yes, there is an ongoing outage of the crawler [ticket:7].\n\n"
        "### Recent Incidents\n\n"
        "- Customers are currently experiencing crawl failures [ticket:3].\n\n"
        "### Earlier fixes\n\n"
        "An earlier ticket fixed fetch timeouts by raising the limit [ticket:7]. The logs record 12 episodes [aggregate:2]."
    )
    answer = Answer(
        answer=text,
        claims=[Claim(text="There is an ongoing outage", evidence_ids=["ticket:7"]), Claim(text="12 episodes", evidence_ids=["aggregate:2"]), Claim(text="Raising the limit fixed it", evidence_ids=["ticket:7"])],
        citations=["ticket:7", "ticket:3", "aggregate:2"],
        short_answer="Yes, the crawler is down right now.",
    )
    checked, violations = section().check(answer)
    assert checked.answer == "### Earlier fixes\n\nAn earlier ticket fixed fetch timeouts by raising the limit [ticket:7]."
    assert [c.text for c in checked.claims] == ["Raising the limit fixed it"]
    assert checked.citations == ["ticket:7", "ticket:3"] and checked.short_answer is None
    assert {v.rule for v in violations} == {"operations_claim_unbacked", "records_presented_as_current", "operations_restated"}
    assert all(v.action == "strip" for v in violations)


def test_a_clean_answer_is_unchanged():
    answer = Answer(answer="An earlier ticket fixed fetch timeouts by raising the limit [ticket:7].", citations=["ticket:7"])
    assert section().check(answer) == (answer, [])


def test_the_section_shows_renderings_rows_and_notes_verbatim_first():
    s = section(notes=["No period was named, so the default live view is shown: last 3 days.", '"at 2" names no am/pm: read as 02:00 and 14:00 UTC.'])
    composed = compose(s, Answer(answer="An earlier ticket fixed it [ticket:7].", citations=["ticket:7"]), answered=True)
    text = composed.answer
    assert text.startswith("**Current logs:**")
    assert REFUSED.rstrip(".") in text and SHOWN in text and "[aggregate:2]" in text and "| crawler |" in text.replace("\\", "")
    assert "- No period was named" in text and "02:00 and 14:00 UTC" in text
    assert text.index("**Current logs:**") < text.index(SHOWN) < text.index("**Past tickets/docs:**") < text.index("An earlier ticket")
    assert composed.citations == ["aggregate:1", "aggregate:2", "ticket:7"]
    assert [c.evidence_ids for c in composed.claims][:2] == [["aggregate:1"], ["aggregate:2"]]
    assert not composed.insufficient_evidence


def test_the_generator_copy_has_no_evidence_ids():
    shown = section().text(cited=False)
    assert "[aggregate:" not in shown and SHOWN in shown


def test_an_abstained_or_empty_knowledge_part_keeps_the_section():
    empty = compose(section(), Answer(answer="", insufficient_evidence=True), answered=True)
    assert "Past tickets/docs" not in empty.answer and SHOWN in empty.answer and not empty.insufficient_evidence
    refused_only = compose(section(), Answer(answer="", insufficient_evidence=True), answered=False)
    assert refused_only.insufficient_evidence


def test_labels_come_from_the_configured_sources():
    assert section_labels(["logs"], ["ticket", "doc"]) == ("Current logs:", "Past tickets/docs:")
    assert section_labels([], []) == ("Current operations evidence:", "Past records:")


def test_drop_empty_headings():
    assert drop_empty_headings("## A\n\n## B\n\ntext\n\n**C:**") == "## B\n\ntext"


def test_every_rule_has_a_note():
    assert set(NOTES) == {"operations_claim_unbacked", "records_presented_as_current", "operations_restated"}


def test_the_operations_rule_is_domain_neutral():
    rule = prompts.OPERATIONS_RULE.lower()
    for word in ("ticket", "doc ", "docs", "log ", "logs", "crawl", "tenant", "customer"):
        assert word not in rule, word
