"""``ReviewStage`` (plan §3.9b): bounded evidence review with re-planning through the decision agent.

Runs after selection and before generation when ``pipeline.review.enabled``. Each round asks the
``reviewer`` role for ``Review{sufficient, missing, followup_query}`` (today's ``REVIEW_PROMPT``) and
stops with one of ``sufficient | no_actionable_gap | repeated_query | hop_limit | budget_exhausted |
replan_abstained``. A follow-up query is re-planned through the configured decision agent with the
request filters and ACL immutable and the generation pinned (same ``RunContext``); a re-plan that
does not return ``decided`` — including ``raw_all_arms`` — stops the loop (``replan_abstained``):
review never widens retrieval to raw-all-arms. New hits merge through ``selection.merge_pools``
with one vote per record per arm. Follow-up queries are recorded under the masked ``followup_query``
key. The stage executes nothing itself: retrieval and evidence packing are injected callables.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, ClassVar, Literal, Protocol, runtime_checkable

from pydantic import Field, model_validator

from ..core.component import Dependencies, Diagnostic, Environment
from ..core.errors import BudgetExhausted, DeadlineExceeded
from ..decision.base import Capabilities, Decision, DecisionAgent, DecisionRequest
from ..models import StrictModel
from ..selection.types import PerArm
from ..selection.union import merge_pools

if TYPE_CHECKING:  # pragma: no cover
    from ..core.config import ChatRole
    from ..runtime.context import RunContext

from ..profiles.prompts import REVIEW_PROMPT  # noqa: E402  (neutral prompt; profiles may overlay it)

NO_GAP: frozenset[str] = frozenset({"", "none", "null", "n/a"})
StopReason = Literal["sufficient", "no_actionable_gap", "repeated_query", "hop_limit", "budget_exhausted", "replan_abstained"]


class Review(StrictModel):
    sufficient: bool
    missing: str = ""
    followup_query: str = ""


class ReviewStageConfig(StrictModel):
    enabled: bool = False
    max_hops: int = Field(default=2, ge=1, le=5)  # total rounds incl. the first search; reviews happen in rounds 2..max_hops

    @model_validator(mode="after")
    def reviews_at_least_once(self):
        if self.enabled and self.max_hops < 2:
            raise ValueError("review enabled with max_hops 1 would never review; use max_hops >= 2 or disable review")
        return self


class ReviewRound(StrictModel):
    hop: int = Field(ge=1)
    sufficient: bool | None = None
    gap_present: bool = False
    followup_present: bool = False
    decision_outcome: str | None = None
    new_records: int = 0
    stop_reason: StopReason | None = None


class ReviewOutcome(StrictModel):
    rounds: list[ReviewRound] = Field(default_factory=list)
    stop_reason: StopReason
    hops_executed: int = Field(default=0, ge=0)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    pools: dict[str, list[Any]] = Field(default_factory=dict)
    queries: list[str] = Field(default_factory=list)  # follow-up queries (masked in trace exports)


@runtime_checkable
class Reviewer(Protocol):
    def review(self, ctx: RunContext, question: str, evidence: list[dict[str, Any]]) -> Review: ...


class JsonModelReviewer:
    """Default reviewer: ``providers.JsonModel`` on the ``reviewer`` role with the ``Review`` schema."""

    def __init__(self, role: ChatRole, prompt: str | None = None):
        self.role, self.prompt = role, prompt or REVIEW_PROMPT

    def review(self, ctx: RunContext, question: str, evidence: list[dict[str, Any]]) -> Review:
        from ..providers import JsonModel, RunBudgetMeter

        bridge = RunBudgetMeter(ctx, "review", self.role.price_key())
        return JsonModel(self.role).call(self.prompt, {"question": question, "evidence": evidence}, Review, bridge, "review")


class ReviewInput(StrictModel):
    """Everything the loop needs; callables are engine-provided and never widen the scope."""

    model_config = {"extra": "forbid", "arbitrary_types_allowed": True}

    request: DecisionRequest
    capabilities: Capabilities
    pools: dict[str, list[Any]]  # PerArm after the first pass
    evidence: list[dict[str, Any]]
    execute: Callable[[Decision], dict[str, list[Any]]]  # runs a decided plan -> per-arm candidates
    repack: Callable[[dict[str, list[Any]]], list[dict[str, Any]]]  # pools -> evidence passages


class ReviewStage:
    name: ClassVar[str] = "review"
    kind: ClassVar[str] = "reviewer"
    config_model: ClassVar[type[StrictModel]] = ReviewStageConfig

    def __init__(self, config: StrictModel, deps: Dependencies):
        self.config: ReviewStageConfig = config  # type: ignore[assignment]
        self.deps = deps
        self.reviewer: Reviewer | None = deps.components.get("reviewer")
        self.decision: DecisionAgent | None = deps.components.get("decision")

    def validate(self, env: Environment, probe: bool = False) -> list[Diagnostic]:
        diagnostics = []
        if self.config.enabled and self.reviewer is None:
            diagnostics.append(Diagnostic(severity="error", code="missing_reviewer", message="review needs a reviewer", component=self.name))
        if self.config.enabled and self.decision is None:
            diagnostics.append(Diagnostic(severity="error", code="missing_decision", message="review re-plans through a decision agent", component=self.name))
        return diagnostics

    def recipe_fingerprint(self) -> str | None:
        return None

    def run(self, ctx: RunContext, inputs: ReviewInput) -> ReviewOutcome:
        if self.reviewer is None or self.decision is None:
            raise ValueError("review stage is not wired: reviewer and decision agent are required")
        pools: PerArm = dict(inputs.pools)
        evidence = list(inputs.evidence)
        rounds: list[ReviewRound] = []
        queries: list[str] = []
        seen = {inputs.request.query.casefold().strip()}
        stop: StopReason = "hop_limit"
        hops = 0
        with ctx.trace.span("review", max_hops=self.config.max_hops) as span:
            for hop in range(1, self.config.max_hops):
                record = ReviewRound(hop=hop)
                rounds.append(record)
                try:
                    ctx.check_deadline()
                    review = self.reviewer.review(ctx, inputs.request.query, evidence)
                except (BudgetExhausted, DeadlineExceeded) as error:
                    record.stop_reason = stop = "budget_exhausted"
                    span.event("review_stopped", reason=stop, detail=type(error).__name__)
                    break
                record.sufficient = review.sufficient
                missing, query = review.missing.strip(), review.followup_query.strip()
                record.gap_present = missing.casefold() not in NO_GAP
                record.followup_present = bool(query)
                if review.sufficient:
                    record.stop_reason = stop = "sufficient"
                    break
                if not record.gap_present or not query:
                    record.stop_reason = stop = "no_actionable_gap"
                    break
                if query.casefold() in seen:
                    record.stop_reason = stop = "repeated_query"
                    break
                seen.add(query.casefold())
                queries.append(query)
                span.event("followup", hop=hop, followup_query=query)
                followup = inputs.request.model_copy(update={"query": query})  # filters/ACL untouched
                decision = self.decision.decide(ctx, followup, None, inputs.capabilities)
                record.decision_outcome = decision.outcome
                if not decision.decided():
                    record.stop_reason = stop = "replan_abstained"
                    span.event("review_stopped", reason=stop, decision_outcome=decision.outcome)
                    break
                before = {c.record_id for cs in pools.values() for c in cs}
                pools = merge_pools(pools, inputs.execute(decision))
                after = {c.record_id for cs in pools.values() for c in cs}
                record.new_records = len(after - before)
                evidence = inputs.repack(pools)
                hops += 1
            span.set(stop_reason=stop, hops_executed=hops, rounds=len(rounds))
        return ReviewOutcome(rounds=rounds, stop_reason=stop, hops_executed=hops, evidence=evidence, pools=pools, queries=queries)
