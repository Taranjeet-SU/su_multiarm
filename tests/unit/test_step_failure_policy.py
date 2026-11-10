"""Step-level failure policy (typed-query plan M1) and dropped-step qualifiers (V3.2 follow-up).

Synthetic; fake planner replies through a patched ``LlmAgent.call_model`` (no provider).

* ``on_step_failure: abstain`` (default) keeps today's behaviour: the first non-executable
  structured step fails the decision.
* ``partial``: that step becomes ``DroppedStep(reason="step_not_executable")``, its constraints and
  one ``aggregate_intent_without_scope`` per aggregate intent go to ``Decision.unsupported``, the
  other steps run; nothing left -> ``retrieval_after_dropped_steps``.
* ``retrieval_only``: every structured step is dropped, the planner's retrieval steps run.
* Caller-typed aggregates/scopes, disabled arms and coherence errors still fail closed.
* A dropped unsolicited scope never silently loses a question qualifier it had bound: a positive
  exact value on a filterable field travels as a typed ``eq`` retrieval filter; everything else is
  ``qualifier_not_applied`` on the decision.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from su_multi_arm.core.component import Dependencies
from su_multi_arm.core.config import DecisionConfig
from su_multi_arm.core.types import Measure
from su_multi_arm.decision import DecisionRequest, LlmAgent, LlmConfig, LlmPlan
from su_multi_arm.decision.base import Capabilities
from su_multi_arm.decision.intent import detect_intents
from su_multi_arm.generation.contract import AnswerContract
from su_multi_arm.models import Answer, Claim
from su_multi_arm.runtime.context import new_context

from conftest import V2_GEN  # tests/conftest.py

ROLE = {"provider": "openai_compatible", "model": "fake"}
GOOD_COUNT = {"arm": "structured", "scope": {"predicates": [{"field": "project", "op": "eq", "value": "Alpha"}], "measure": {"kind": "count"}}}
BAD_COUNT = {"arm": "structured", "scope": {"predicates": [{"field": "project", "op": "eq", "value": "NoSuchProject"}], "measure": {"kind": "count"}}}
BAD_SUM = {"arm": "structured", "scope": {"measure": {"kind": "sum", "field": "status"}}}
LEXICAL = {"arm": "lexical", "query": "project records"}
COUNT_Q = "How many records are in project Alpha and project Zeta?"


def _agent(monkeypatch, backend, steps, **cfg):
    agent = LlmAgent(LlmConfig(role=ROLE, **cfg), Dependencies(backends={"records": backend.records}))
    monkeypatch.setattr(LlmAgent, "call_model", lambda self, ctx, payload: LlmPlan.model_validate({"steps": steps}))
    return agent


def _decide(agent, caps, question, **request):
    ctx = new_context(V2_GEN)
    decision = agent.decide(ctx, DecisionRequest(query=question, **request), detect_intents(question, [], caps.structured_targets()), caps)
    span = next(s for s in ctx.trace.spans if s.name == "decision")
    events = [e for s in ctx.trace.spans for e in s.events if e["name"] == "planner_steps_dropped"]
    return decision, span.attributes, events


# --- on_step_failure ------------------------------------------------------------------------------


def test_default_abstain_is_unchanged(monkeypatch, v2_backend, v2_caps):
    decision, _, events = _decide(_agent(monkeypatch, v2_backend, [BAD_COUNT, LEXICAL]), v2_caps, COUNT_Q)
    assert decision.outcome == "abstain" and decision.failure == "unresolvable_value" and decision.dropped_steps == [] and events == []
    assert LlmConfig(role=ROLE).on_step_failure == "abstain" and DecisionConfig().on_step_failure == "abstain"


def test_partial_drops_the_failed_step_and_runs_the_others(monkeypatch, v2_backend, v2_caps):
    agent = _agent(monkeypatch, v2_backend, [GOOD_COUNT, BAD_COUNT, LEXICAL], on_step_failure="partial")
    decision, span, events = _decide(agent, v2_caps, COUNT_Q)
    assert decision.outcome == "decided" and decision.failure is None and decision.fallback is None
    assert [s.arm for s in decision.plan.steps] == ["structured", "lexical"]
    assert decision.scope is not None and decision.scope.predicates[0].value == "Alpha"
    assert [(d.arm, d.reason) for d in decision.dropped_steps] == [("structured", "step_not_executable")]
    assert "NoSuchProject" not in decision.dropped_steps[0].detail  # field names only
    reasons = {u.reason for u in decision.unsupported}
    assert {"value_not_observed", "aggregate_intent_without_scope"} <= reasons
    assert span["dropped_steps"][0]["reason"] == "step_not_executable" and events[0]["on_step_failure"] == "partial"


def test_partial_with_nothing_left_falls_back_to_retrieval_and_never_yields_an_exact_count(monkeypatch, v2_backend, v2_caps):
    agent = _agent(monkeypatch, v2_backend, [BAD_COUNT], on_step_failure="partial")
    decision, _, events = _decide(agent, v2_caps, COUNT_Q)
    assert decision.outcome == "decided" and decision.fallback == "retrieval_after_dropped_steps"
    assert decision.plan.arms() == ["dense", "lexical"] and decision.scope is None and not decision.plan.structured_steps()
    assert any(u.reason == "aggregate_intent_without_scope" for u in decision.unsupported)
    assert events[0]["fallback"] == "retrieval_after_dropped_steps"
    # the AnswerContract refuses a count that no exact scope backs
    answer = Answer(answer="There are 3 records.", claims=[Claim(text="3 records", kind="count", evidence_ids=["r1"])], citations=["r1"])
    violations = AnswerContract("abstain_with_reason").check(answer, evidence_ids={"r1"}, aggregates=[], intents=decision.intents)
    assert {v.rule for v in violations} >= {"claim_without_exact_scope", "aggregate_intent_without_scope"}


def test_unsupported_measure_is_dropped_under_partial_and_never_replaced_by_a_count(monkeypatch, v2_backend, v2_caps):
    agent = _agent(monkeypatch, v2_backend, [BAD_SUM, LEXICAL], on_step_failure="partial")
    decision, _, _ = _decide(agent, v2_caps, "What is the total status of all records?")
    assert decision.decided() and decision.plan.arms() == ["lexical"]
    assert any(u.reason == "measure_unsupported" for u in decision.unsupported)


def test_retrieval_only_drops_every_structured_step(monkeypatch, v2_backend, v2_caps):
    agent = _agent(monkeypatch, v2_backend, [GOOD_COUNT, BAD_COUNT, LEXICAL], on_step_failure="retrieval_only")
    decision, span, _ = _decide(agent, v2_caps, COUNT_Q)
    assert decision.decided() and decision.plan.arms() == ["lexical"] and decision.scope is None
    assert len(decision.dropped_steps) == 2 and {d.reason for d in decision.dropped_steps} == {"step_not_executable"}
    assert len(span["dropped_steps"]) == 2  # the trace lists every dropped step


def test_retrieval_only_without_a_failure_runs_the_plan(monkeypatch, v2_backend, v2_caps):
    agent = _agent(monkeypatch, v2_backend, [GOOD_COUNT, LEXICAL], on_step_failure="retrieval_only")
    decision, _, _ = _decide(agent, v2_caps, COUNT_Q)
    assert decision.plan.arms() == ["structured", "lexical"] and decision.dropped_steps == []


@pytest.mark.parametrize("policy", ["partial", "retrieval_only"])
def test_caller_typed_aggregates_still_fail_closed(monkeypatch, v2_backend, v2_caps, policy):
    agent = _agent(monkeypatch, v2_backend, [BAD_COUNT, LEXICAL], on_step_failure=policy)
    decision, _, _ = _decide(agent, v2_caps, "records please", aggregate=Measure(kind="count"))
    assert decision.outcome == "abstain" and decision.failure == "unresolvable_value"


@pytest.mark.parametrize("policy", ["partial", "retrieval_only"])
def test_disabled_arms_and_overridden_filters_still_fail_closed(monkeypatch, v2_backend, v2_schema, policy):
    caps = Capabilities(arms=["lexical", "structured"], metadata_schema=v2_schema)
    decision, _, _ = _decide(_agent(monkeypatch, v2_backend, [BAD_COUNT, {"arm": "dense", "query": "x"}], on_step_failure=policy), caps, COUNT_Q)
    assert decision.outcome == "abstain" and decision.failure == "invalid_step"
    override = {"arm": "structured", "scope": {"predicates": [{"field": "project", "op": "eq", "value": "Beta"}], "measure": {"kind": "count"}}}
    decision, _, _ = _decide(_agent(monkeypatch, v2_backend, [override], on_step_failure=policy), caps, COUNT_Q, filters={"project": "Alpha"})
    assert decision.outcome == "abstain" and decision.failure == "invalid_step"


def test_no_retrieval_arm_left_keeps_the_step_failure(monkeypatch, v2_backend, v2_schema):
    caps = Capabilities(arms=["structured", "graph"], metadata_schema=v2_schema)
    decision, _, _ = _decide(_agent(monkeypatch, v2_backend, [BAD_COUNT], on_step_failure="partial"), caps, COUNT_Q)
    assert decision.outcome == "abstain" and decision.failure == "unresolvable_value"
    assert any(u.reason == "value_not_observed" for u in decision.unsupported)


def test_config_surface():
    assert DecisionConfig(kind="llm", on_step_failure="partial").on_step_failure == "partial"
    assert DecisionConfig(kind="cascade", stages=["rules", "llm"], on_step_failure="retrieval_only").on_step_failure == "retrieval_only"
    with pytest.raises(ValidationError, match="llm planner only"):
        DecisionConfig(kind="rules", on_step_failure="partial")
    with pytest.raises(ValidationError):
        DecisionConfig(kind="llm", on_step_failure="repair")


# --- qualifiers of a dropped unsolicited scope ---------------------------------------------------

BETA_LIST = {"arm": "structured", "scope": {"predicates": [{"field": "project", "op": "eq", "value": "Beta"}], "limit": 5}}


def test_a_bound_value_qualifier_travels_as_a_retrieval_filter(monkeypatch, v2_backend, v2_caps):
    decision, _, events = _decide(_agent(monkeypatch, v2_backend, [BETA_LIST]), v2_caps, "Where is the setup guide for project Beta?")
    assert decision.fallback == "retrieval_after_dropped_steps" and decision.unsupported == []
    assert all(s.filters == {"project": "Beta"} for s in decision.plan.steps)
    assert decision.dropped_steps[0].carried_filters == {"project": "Beta"} and events[0]["carried_filters"] == ["project"]


def test_an_invented_predicate_is_not_carried(monkeypatch, v2_backend, v2_caps):
    decision, _, _ = _decide(_agent(monkeypatch, v2_backend, [BETA_LIST]), v2_caps, "Where is the setup guide?")
    assert all(s.filters == {} for s in decision.plan.steps) and decision.unsupported == []


def test_a_negated_qualifier_is_reported_not_applied(monkeypatch, v2_backend, v2_caps):
    negated = {"arm": "structured", "scope": {"predicates": [{"field": "project", "op": "eq", "value": "Alpha", "negate": True}], "limit": 5}}
    decision, _, _ = _decide(_agent(monkeypatch, v2_backend, [negated]), v2_caps, "Where is the design doc for projects other than Alpha?")
    assert all(s.filters == {} for s in decision.plan.steps)
    assert [u.reason for u in decision.unsupported] == ["qualifier_not_applied"] and "Alpha" in decision.unsupported[0].constraint_text
    assert decision.dropped_steps[0].unapplied == decision.unsupported


def test_a_bound_time_qualifier_is_reported_not_applied(monkeypatch, v2_backend, v2_caps):
    timed = {"arm": "structured", "scope": {"windows": [{"field": "created_at", "expression": "2024"}], "limit": 5}}
    decision, _, _ = _decide(_agent(monkeypatch, v2_backend, [timed]), v2_caps, "What changed in the gateway setup in 2024?")
    assert [u.constraint_text for u in decision.unsupported] == ["2024"] and decision.unsupported[0].reason == "qualifier_not_applied"


def test_a_request_filter_already_applies_the_qualifier(monkeypatch, v2_backend, v2_caps):
    decision, _, _ = _decide(_agent(monkeypatch, v2_backend, [BETA_LIST]), v2_caps, "Where is the setup guide for project Beta?", filters={"project": "Beta"})
    assert all(s.filters == {} for s in decision.plan.steps) and decision.unsupported == []


def test_a_qualifier_contradicting_a_request_filter_is_reported(monkeypatch, v2_backend, v2_caps):
    decision, _, _ = _decide(_agent(monkeypatch, v2_backend, [BETA_LIST]), v2_caps, "Where is the setup guide for project Beta?", filters={"project": "Alpha"})
    assert all(s.filters == {} for s in decision.plan.steps)
    assert [u.reason for u in decision.unsupported] == ["qualifier_not_applied"]


def test_planner_retrieval_steps_without_the_qualifier_get_it_reported(monkeypatch, v2_backend, v2_caps):
    decision, _, _ = _decide(_agent(monkeypatch, v2_backend, [{"arm": "lexical", "query": "setup guide"}, BETA_LIST]), v2_caps, "Where is the setup guide for project Beta?")
    assert decision.fallback is None and decision.plan.steps[0].filters == {}
    assert [u.reason for u in decision.unsupported] == ["qualifier_not_applied"] and "Beta" in decision.unsupported[0].constraint_text
    with_filter = {"arm": "lexical", "query": "setup guide", "filters": [{"field": "project", "value": "Beta"}]}
    decision, _, _ = _decide(_agent(monkeypatch, v2_backend, [with_filter, BETA_LIST]), v2_caps, "Where is the setup guide for project Beta?")
    assert decision.unsupported == []
