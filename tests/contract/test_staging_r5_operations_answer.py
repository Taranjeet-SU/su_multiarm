"""The operations half of a two-halves answer is rendered, not generated (staging round 5, N14).

On the synthetic two-halves engine (``tests/two_halves_support.py``: support documents plus the
episode archive's live window, typed log intents, the two-halves planner) a fake generator writes the
misstatements the private round-5 evaluation found: a current outage asserted from earlier records,
earlier records labelled as recent incidents, a restatement of the operations evidence and an
automatic recovery no operations item states. The answer must:

* start with the operations section (``Current logs:``): every operations aggregate's rendering and
  rows verbatim, cited, with the operations half's notes (window, clock reading, refusals);
* keep the generator's part under ``Past tickets/docs:``, without the flagged sentences
  (violations recorded; ``annotate`` states them as limitations);
* never send the operations aggregates to the generator as evidence: it gets ``operations_shown``
  without ids and the operations rule;
* still show the operations section when the generator abstains, and a refusal for a customer
  without a matched tenant;
* leave support-only answers exactly as before (no section, no rule, no ``operations_shown``).

Fake providers only (``JsonModel.call``); no service.
"""

from __future__ import annotations

from typing import Any

import pytest

from su_multi_arm import SearchRequest
from su_multi_arm.engine import Engine
from su_multi_arm.models import Answer, Claim
from su_multi_arm.profiles.prompts import OPERATIONS_RULE
from su_multi_arm.providers import JsonModel
from tests import two_halves_support as th

HALTED = "Has the crawl of the help-centre articles stopped? It is supposed to run every 30 minutes."
UNAUTHORIZED = "Brightmoor gets unauthorized errors connecting the MCP connector even though the credentials are correct"
REFUSED_CUSTOMER = "Cobaltfield Systems says search is slow, is there an outage?"
SUPPORT = "How do I split a large sitemap?"


@pytest.fixture(scope="module")
def pinned_clock():
    mp = pytest.MonkeyPatch()
    mp.setattr(Engine, "_clock", staticmethod(lambda zone: th.NOW.astimezone(zone)))
    yield
    mp.undo()


@pytest.fixture(scope="module")
def unified(tmp_path_factory, pinned_clock):
    tmp = tmp_path_factory.mktemp("r5_ops")
    return th.build(tmp, th.unified_config(tmp))


def misstating(seen: list[dict[str, Any]], *, abstain: bool = False):
    """A generator that misstates the operations half the way the round-5 answers did."""

    def respond(self, system, payload, schema, budget, stage):
        assert stage == "generator", stage
        budget.reserve()
        seen.append({"system": system, "payload": payload})
        records = [e["id"] for e in payload["evidence"] if e.get("source") != "structured"]
        if abstain or not records:
            return Answer(answer="The evidence does not answer this.", insufficient_evidence=True)
        r = records[0]
        text = (
            f"Yes, there is an ongoing outage affecting the service [{r}].\n\n"
            "### Recent Incidents\n\n"
            f"- Customers are currently experiencing the same failures [{r}].\n\n"
            "### Earlier records\n\n"
            f"An earlier record describes how a similar failure was fixed by rotating the credentials [{r}]. "
            f"The job recovered automatically after the timeout [{r}]. "
            "The operations evidence lists the failure episodes [aggregate:1]."
        )
        claims = [Claim(text="There is an ongoing outage", evidence_ids=[r]), Claim(text="Rotating the credentials fixed a similar failure", evidence_ids=[r])]
        return Answer(answer=text, claims=claims, citations=[r], short_answer="Yes, there is an outage right now.")

    return respond


def ask(engine, question: str):
    return engine.answer(SearchRequest(query=question, mode="rules"))


def operations_ids(result) -> list[str]:
    return [a["id"] for a in result.retrieval.aggregates if a.get("intent")]


def test_the_operations_half_is_rendered_first_and_verbatim(unified, monkeypatch):
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(JsonModel, "call", misstating(seen))
    result = ask(unified, HALTED)
    text = result.answer.answer
    ids = operations_ids(result)
    assert ids and text.startswith("**Current logs:**")
    past = text.index("**Past tickets/docs:**")
    for aggregate in result.retrieval.aggregates:
        if aggregate.get("intent"):
            shown = aggregate["rendering"].rstrip(".")
            assert shown in text and text.index(shown) < past, aggregate["intent"]
            assert f"[{aggregate['id']}]" in text[:past]
    assert set(ids) <= set(result.answer.citations)
    assert [c.evidence_ids for c in result.answer.claims][: len(ids)] == [[i] for i in ids]
    # the clock / default view notes of the operations half are in the section
    notes = result.trace["source_coverage"]["sources"]["logs"].get("notes", [])
    for note in notes:
        assert " ".join(note.split()) in text[:past] or " ".join(note.split()) in " ".join(a["rendering"] for a in result.retrieval.aggregates if a.get("intent"))


def test_the_generator_gets_no_operations_aggregate_and_the_operations_rule(unified, monkeypatch):
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(JsonModel, "call", misstating(seen))
    result = ask(unified, HALTED)
    [call] = seen
    evidence_ids = {e["id"] for e in call["payload"]["evidence"]}
    assert not evidence_ids & set(operations_ids(result))
    shown = call["payload"]["operations_shown"]
    assert shown.startswith("**Current logs:**") and "[aggregate:" not in shown
    assert call["system"].endswith(OPERATIONS_RULE)


def test_misstatements_are_removed_and_recorded(unified, monkeypatch):
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(JsonModel, "call", misstating(seen))
    result = ask(unified, UNAUTHORIZED)
    text = result.answer.answer
    past = text[text.index("**Past tickets/docs:**") :]
    assert "ongoing outage" not in text and "Recent Incidents" not in text and "currently experiencing" not in text
    assert "recovered automatically" not in text and "lists the failure episodes" not in text
    assert "fixed by rotating the credentials" in past and "### Earlier records" in past
    rules = {v.rule for v in result.violations}
    assert {"operations_claim_unbacked", "records_presented_as_current", "operations_restated"} <= rules
    assert [c.text for c in result.answer.claims if not any(e.startswith("aggregate:") for e in c.evidence_ids)] == ["Rotating the credentials fixed a similar failure"]
    assert result.answer.short_answer is None and result.contract == "stripped"


def test_annotate_states_each_removal_as_a_limitation(unified, monkeypatch):
    from su_multi_arm.generation import AnswerContract
    from su_multi_arm.generation.operations import NOTES

    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(JsonModel, "call", misstating(seen))
    pipe = unified.pipelines[unified.config.default_pipeline]
    monkeypatch.setattr(pipe, "contract", AnswerContract("annotate"))
    result = ask(unified, UNAUTHORIZED)
    assert result.contract == "annotated"
    for rule in ("operations_claim_unbacked", "records_presented_as_current", "operations_restated"):
        assert NOTES[rule] in result.limitations and NOTES[rule] in result.answer.answer


def test_the_section_stands_when_the_generator_abstains(unified, monkeypatch):
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(JsonModel, "call", misstating(seen, abstain=True))
    result = ask(unified, UNAUTHORIZED)
    assert result.answer.answer.startswith("**Current logs:**")
    assert not result.answer.insufficient_evidence and set(operations_ids(result)) <= set(result.answer.citations)


def test_a_refused_customer_is_stated_in_the_section(unified, monkeypatch):
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(JsonModel, "call", misstating(seen))
    result = ask(unified, REFUSED_CUSTOMER)
    assert operations_ids(result) == []
    text = result.answer.answer
    assert text.startswith("**Current logs:**")
    section = text[: text.index("**Past tickets/docs:**")] if "**Past tickets/docs:**" in text else text
    assert "Cobaltfield Systems" in section and "no matched log tenant" in section
    assert "ongoing outage" not in text


def test_a_support_question_is_answered_as_before(unified, monkeypatch):
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(JsonModel, "call", th.citing_generator(seen))
    result = ask(unified, SUPPORT)
    [call] = seen
    assert "operations_shown" not in call["payload"]
    assert "Current logs" not in result.answer.answer and "Past tickets" not in result.answer.answer
    assert not any(v.rule.startswith("operations_") or v.rule == "records_presented_as_current" for v in result.violations)
