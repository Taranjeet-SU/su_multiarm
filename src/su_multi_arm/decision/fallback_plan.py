"""Deterministic fallback plan of the two-halves planner (docs/plans/unified_merge.md §6.11, §6.12;
increment M2b-2).

When the planner fails after its one repair (schema violation, provider error, timeout, an invalid
or empty plan) and ``crosssource.planner.fallback`` is ``deterministic_plan``, the request is not
left empty: the knowledge half is the question on the enabled lexical, dense and segment arms (no
scope, no invented filter; the request filters still apply) and the operations half is the typed
log-intent steps the deterministic grounding already planned. The plan is labelled
``Decision.fallback = deterministic_plan`` (trace ``decision.planner_fallback``) and stated in the
answer's limitations; every aggregate intent of the question is reported unsupported, so the answer
contract never turns retrieval into a count (as ``raw_all_arms``).

Deliberate refusals are **not** replaced: an unresolvable value or qualifier, an exhausted budget
and a caller-typed aggregate or scope keep today's fail-closed decision. A plan that would have no
step (no retrieval arm enabled and no operations step) is never produced: ``None`` is returned and
the caller keeps the abstention.
"""

from __future__ import annotations

from typing import Any

from ..models import UnsupportedConstraint
from .base import Capabilities, Decision, DecisionRequest, Plan, RetrievalStep
from .intent import Intents

# Planner failures the fallback replaces (the planner could not produce a usable plan); every other
# category is a deliberate refusal and stays fail closed.
FALLBACK_FAILURES = frozenset({"schema_violation", "provider_error", "timeout", "invalid_step", "no_plan", "low_confidence"})
# Knowledge-half arms of the fallback: question-text arms only (no structured scope, no graph seeds).
FALLBACK_ARMS = ("lexical", "dense", "segment")
POLICY = "deterministic_fallback_plan"
LIMITATION = "The planner could not form a valid plan ({failure}); a labelled deterministic plan was used instead: the question text on {arms}{operations}."


def eligible(decision: Decision, request: DecisionRequest) -> bool:
    """Whether ``decision`` is a planner failure the fallback may replace."""
    if request.aggregate is not None or request.scope is not None:
        return False  # a caller-typed operation fails closed
    if decision.outcome == "raw_all_arms":
        return True  # the operator's fail-open policy: the labelled two-halves plan replaces it
    return decision.outcome == "abstain" and decision.failure in FALLBACK_FAILURES


def deterministic_plan(
    request: DecisionRequest,
    intents: Intents,
    capabilities: Capabilities,
    *,
    failed: Decision,
    operations: list[Any] | None = None,
    agent: str = "two_halves",
) -> Decision | None:
    """The labelled fallback decision replacing ``failed``, or ``None`` when it would be empty."""
    operations = list(operations or [])
    arms = [arm for arm in FALLBACK_ARMS if arm in capabilities.arms]
    budget = capabilities.max_steps - len(operations)
    steps: list[Any] = [RetrievalStep(arm=arm, query=request.query, filters={}) for arm in arms[: max(0, budget)]]
    steps.extend(operations)
    if not steps:
        return None
    flagged = [
        UnsupportedConstraint(constraint_text=a.text, reason="aggregate_intent_without_scope", suggestion="the planner failed; the fallback plan answers from retrieved records and typed log intents only")
        for a in intents.aggregates
    ]
    known = {(u.constraint_text, u.reason) for u in failed.unsupported}
    unsupported = [*failed.unsupported, *(u for u in flagged if (u.constraint_text, u.reason) not in known)]
    failure = failed.failure or "no_plan"
    used = ", ".join(arms[: max(0, budget)]) or "no retrieval arm"
    note = LIMITATION.format(failure=failure, arms=used, operations=f" plus {len(operations)} typed log-intent step(s)" if operations else "")
    scope = next((s.scope for s in operations if getattr(s, "arm", None) == "structured"), None)
    return Decision(
        plan=Plan(steps=steps),
        scope=scope,
        unsupported=unsupported,
        outcome="decided",
        confidence=None,
        agent=agent,
        stage=failed.stage,
        policy_applied=POLICY,
        intents=intents,
        failure=failure,
        note=note,
        fallback="deterministic_plan",
    )
