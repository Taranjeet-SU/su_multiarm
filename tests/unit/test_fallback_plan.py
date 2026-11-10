"""Deterministic fallback plan of the two-halves planner (unified merge M2b-2, §6.11, §6.12): replaces
a failed planner (after its repair) with a labelled plan of both halves, never empty, never for a
deliberate refusal or a caller-typed operation; aggregate intents stay unsupported. Synthetic only."""

from __future__ import annotations

import pytest

from su_multi_arm.core.types import Measure, ScopeSpec
from su_multi_arm.decision.base import Capabilities, Decision, DecisionRequest, StructuredStep, abstain, log_decision, raw_all_arms
from su_multi_arm.decision.fallback_plan import FALLBACK_FAILURES, POLICY, deterministic_plan, eligible
from su_multi_arm.decision.intent import detect_intents
from su_multi_arm.decision.log_intents import LogIntentCall

CAPS = Capabilities(arms=["lexical", "dense", "segment", "graph", "structured"])
REQUEST = DecisionRequest(query="the connector rejects calls despite correct credentials")


def failed(category: str) -> Decision:
    return abstain(detect_intents(REQUEST.query), agent="llm", stage=None, failure=category, note=f"{category}: synthetic")


def operations() -> list[StructuredStep]:
    call = LogIntentCall(intent="issue_history", args={"window": "last 3 days", "service": ["svc-auth"], "as_of": "2026-11-09"})
    return list(log_decision(call, detect_intents(REQUEST.query), agent="two_halves").plan.steps)


@pytest.mark.parametrize("category", sorted(FALLBACK_FAILURES))
def test_planner_failures_are_replaced_by_a_labelled_plan_of_both_halves(category):
    decision = failed(category)
    assert eligible(decision, REQUEST)
    plan = deterministic_plan(REQUEST, decision.intents, CAPS, failed=decision, operations=operations())
    assert plan.outcome == "decided" and plan.fallback == "deterministic_plan" and plan.policy_applied == POLICY
    assert plan.failure == category and "deterministic plan" in plan.note
    assert [s.arm for s in plan.plan.steps] == ["lexical", "dense", "segment", "structured"]  # no graph seeds, no invented scope
    assert all(getattr(s, "query", REQUEST.query) == REQUEST.query and not getattr(s, "filters", {}) for s in plan.plan.steps)
    assert plan.scope is plan.plan.steps[-1].scope and plan.plan.steps[-1].intent.intent == "issue_history"


@pytest.mark.parametrize("category", ["unresolvable_value", "budget_exhausted", "unsupported_measure", "artifact_invalid"])
def test_deliberate_refusals_stay_fail_closed(category):
    assert not eligible(failed(category), REQUEST)


def test_a_caller_typed_operation_is_never_replaced():
    typed = REQUEST.model_copy(update={"aggregate": Measure(kind="count")})
    assert not eligible(failed("schema_violation"), typed)
    scoped = REQUEST.model_copy(update={"scope": ScopeSpec()})
    assert not eligible(failed("schema_violation"), scoped)


def test_raw_all_arms_is_replaced_and_aggregates_stay_unsupported():
    request = DecisionRequest(query="how many tickets mention token rotation")
    intents = detect_intents(request.query, targets=["ticket", "tickets"])
    raw = raw_all_arms(request, intents, CAPS, agent="llm", stage=None, failure="schema_violation")
    assert eligible(raw, request)
    plan = deterministic_plan(request, intents, CAPS, failed=raw)
    assert plan.fallback == "deterministic_plan" and not plan.plan.structured_steps()
    flagged = [u for u in plan.unsupported if u.reason == "aggregate_intent_without_scope"]
    assert flagged and len({(u.constraint_text, u.reason) for u in flagged}) == len(flagged)  # not duplicated


def test_the_fallback_is_never_empty_and_respects_the_step_cap():
    nothing = Capabilities(arms=["graph", "structured"])
    assert deterministic_plan(REQUEST, failed("timeout").intents, nothing, failed=failed("timeout")) is None
    only_ops = deterministic_plan(REQUEST, failed("timeout").intents, nothing, failed=failed("timeout"), operations=operations())
    assert [s.arm for s in only_ops.plan.steps] == ["structured"]
    capped = Capabilities(arms=["lexical", "dense", "segment", "structured"], max_steps=2)
    plan = deterministic_plan(REQUEST, failed("timeout").intents, capped, failed=failed("timeout"), operations=operations())
    assert [s.arm for s in plan.plan.steps] == ["lexical", "structured"]


def test_the_decision_label_needs_the_failure_it_replaces():
    plan = deterministic_plan(REQUEST, failed("no_plan").intents, CAPS, failed=failed("no_plan"))
    with pytest.raises(ValueError, match="planner failure"):
        Decision.model_validate({**plan.model_dump(), "failure": None})
    with pytest.raises(ValueError, match="dropped steps"):
        Decision.model_validate({**plan.model_dump(), "fallback": "retrieval_after_dropped_steps"})
