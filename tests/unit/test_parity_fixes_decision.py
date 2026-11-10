"""Decision fixes from the 2026-11-08 production-workload parity rerun (synthetic shapes).

* Defect 5 (V3.2 ``unsolicited_structured``): ranking, superlative, ordinal, range, distinct,
  existence and trend phrasings are aggregate requests; the planner's structured step is kept
  (and every guard still applies). Lookup questions keep the old drop.
* Defect 6a (rules): a ranking question is never answered by one total labelled exact; the grammar
  reads "which <field> currently has / has raised / filed the most ..." as a ranking.
* Defect 6b (rules): a *mention* count ("records that mention X", "X-mention records") is a literal
  lexical match, a different counting basis: ``bounded`` with the reason, never ``exact``. A quoted
  phrase or ``containing <words>`` is the literal match the question defines and stays exact.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from su_multi_arm import SearchRequest
from su_multi_arm.core.component import Dependencies
from su_multi_arm.decision import DecisionRequest, LlmAgent, LlmConfig, LlmPlan
from su_multi_arm.decision.intent import detect_intents, mentions_aggregate_operator
from su_multi_arm.runtime.context import new_context

from conftest import V2_GEN  # tests/conftest.py

sys.path.insert(0, str(Path(__file__).parent))
from test_query_compiler import engine_with_text, grammar_setup  # noqa: E402,F401  (fixture reuse)

ROLE = {"provider": "openai_compatible", "model": "fake"}
COUNT_BY_PROJECT = {"arm": "structured", "scope": {"group_by": ["project"], "measure": {"kind": "count"}, "order": "desc", "nth": 1}}


def _planner(monkeypatch, v2_backend, steps):
    agent = LlmAgent(LlmConfig(role=ROLE), Dependencies(backends={"records": v2_backend.records}))
    monkeypatch.setattr(LlmAgent, "call_model", lambda self, ctx, payload: LlmPlan.model_validate({"steps": steps}))
    return agent


# --- defect 5: the planner's count/list step is not "unsolicited" for these phrasings ---------------

ASKS_AGGREGATE = [
    "Which project has raised the most records?",
    "Which project filed the most records on 2024-03-05?",
    "What is the date range of the records?",
    "Does any record description mention rounding?",
    "What are the next two highest open ratios among projects with at least 2 records?",
    "Who is second for invoice mentions?",
    "Which project spans the most distinct statuses?",
    "Which kind contains the most archived records?",
    "Are any invoice-mention records currently open or pending?",
    "Which project mentions rounding most?",
    "Who is second for open record volume?",
    "How concentrated are invoice mentions across projects?",
    "How did invoice mentions change across half-years?",
    "Did invoice mentions increase continuously over time?",
    "Which status appears most often among reports?",
    "How reliable is the amount field for open records?",
]


@pytest.mark.parametrize("question", ASKS_AGGREGATE)
def test_ranking_range_existence_and_trend_phrasings_are_aggregate_requests(question):
    assert mentions_aggregate_operator(question)


@pytest.mark.parametrize("question", ASKS_AGGREGATE[:6])
def test_the_planner_structured_step_is_kept_for_these_phrasings(monkeypatch, v2_backend, v2_caps, question):
    agent = _planner(monkeypatch, v2_backend, [COUNT_BY_PROJECT])
    ctx = new_context(V2_GEN)
    decision = agent.decide(ctx, DecisionRequest(query=question), detect_intents(question, [], v2_caps.structured_targets()), v2_caps)
    assert all(d.reason != "unsolicited_structured" for d in decision.dropped_steps)


@pytest.mark.parametrize(
    "question",
    [
        "How do I rotate the signing key on the staging gateway?",
        "Why does the export stop at the second page?",
        "Explain the most important setting for the export job.",
    ],
)
def test_lookup_questions_still_drop_an_invented_structured_step(monkeypatch, v2_backend, v2_caps, question):
    agent = _planner(monkeypatch, v2_backend, [COUNT_BY_PROJECT])
    ctx = new_context(V2_GEN)
    decision = agent.decide(ctx, DecisionRequest(query=question), detect_intents(question, [], v2_caps.structured_targets()), v2_caps)
    assert [d.reason for d in decision.dropped_steps] == ["unsolicited_structured"]


# --- defect 6a: a ranking is never answered by one total --------------------------------------------


def test_a_ranking_the_grammar_cannot_group_is_refused_not_counted(grammar_setup):  # noqa: F811
    decision, _ = grammar_setup("Which of the projects has the most open records?")
    assert decision.outcome == "abstain" and decision.scope is None
    assert any(u.reason == "measure_unsupported" and "ranking" in (u.suggestion or "") for u in decision.unsupported)


@pytest.mark.parametrize(
    "question",
    ["Which project currently has the most open records?", "Which project has opened the most records?", "Which project filed the most records?"],
)
def test_record_verbs_and_adverbs_still_read_as_a_ranking(grammar_setup, question):  # noqa: F811
    decision, result = grammar_setup(question)
    assert decision.outcome == "decided" and decision.scope.group_by == ["project"] and decision.scope.nth == 1
    assert result.groups and result.exactness == "exact"


def test_a_state_verb_is_never_swallowed_by_the_ranking(grammar_setup):  # noqa: F811
    # "escalated" is no record verb: the question is not compiled into a ranking that ignores it
    decision, _ = grammar_setup("Which project escalated the most records?")
    assert decision.scope is None


def test_ungrouped_totals_are_unchanged(grammar_setup):  # noqa: F811
    decision, result = grammar_setup("How many open records are there?")
    assert decision.outcome == "decided" and result.value == 3 and result.exactness == "exact"


# --- defect 6b: mention counts are bounded ----------------------------------------------------------


@pytest.mark.parametrize("question", ["How many records mention billing?", "How many invoice-mention records are open?", "How many records are about billing?"])
def test_a_mention_count_is_bounded_with_its_reason(grammar_setup, question):  # noqa: F811
    decision, _ = grammar_setup(question)
    [step] = decision.plan.steps
    assert decision.scope.text is not None
    assert any(u.reason == "qualifier_not_applied" and "counting basis" in (u.suggestion or "") for u in step.unapplied)
    assert any("counting basis" in (u.suggestion or "") for u in decision.unsupported)


@pytest.mark.parametrize("question", ['How many records contain "billing dispute"?', 'How many records contain "billing"?'])
def test_a_literal_containment_count_stays_exact(grammar_setup, question):  # noqa: F811
    decision, _ = grammar_setup(question)
    [step] = decision.plan.steps
    assert decision.scope.text is not None and not any("counting basis" in (u.suggestion or "") for u in step.unapplied)


def test_engine_reports_a_mention_count_as_bounded_and_a_quoted_count_as_exact():
    engine = engine_with_text()
    [mention] = engine.search(SearchRequest(query="How many records mention billing?", mode="rules")).aggregates
    assert mention["value"] == 2 and mention["exactness"] == "bounded"
    assert any(u["reason"] == "qualifier_not_applied" for u in mention["unsupported"])
    [quoted] = engine.search(SearchRequest(query='How many records contain "billing dispute"?', mode="rules")).aggregates
    assert quoted["value"] == 1 and quoted["exactness"] == "exact"
