"""``cascade`` decision agent (plan §3.7, §3.7.1): ordered stages; the first ``decided`` wins.

Each stage is a ``DecisionAgent`` injected through ``Dependencies.components`` under its stage name.
A failed stage follows ``stage_policies.<stage>``: ``fallback_to_next_stage`` (default for ``rules``
and ``classifier``) or ``abstain`` (default for ``llm``). When every stage failed the cascade's own
``on_failure`` applies (default ``fail_closed`` = abstain; ``raw_all_arms`` is opt-in) and the
decision carries the last stage's failure. Request filters stay immutable throughout.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal

from pydantic import Field, model_validator

from ..core.component import Dependencies, Diagnostic, Environment
from ..core.config_decision import STAGE_NAMES, StagePolicies
from ..models import StrictModel
from .base import Capabilities, Decision, DecisionAgent, DecisionFailure, DecisionRequest, abstain, raw_all_arms
from .intent import Intents

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime.context import RunContext


class CascadeConfig(StrictModel):
    on_failure: Literal["fail_closed", "raw_all_arms"] = "fail_closed"
    stages: list[Literal["rules", "classifier", "llm"]] = Field(min_length=1)
    stage_policies: StagePolicies = Field(default_factory=StagePolicies)

    @model_validator(mode="after")
    def distinct(self):
        if len(set(self.stages)) != len(self.stages):
            raise ValueError("cascade stages must be distinct")
        return self


class _StageAbstain(Exception):
    def __init__(self, stage: str, failure: DecisionFailure):
        self.stage, self.failure = stage, failure
        super().__init__(str(failure))


class CascadeAgent(DecisionAgent):
    name: ClassVar[str] = "cascade"
    config_model: ClassVar[type[StrictModel]] = CascadeConfig

    def __init__(self, config: StrictModel, deps: Dependencies):
        super().__init__(config, deps)
        self.stages: dict[str, DecisionAgent] = {}
        for stage in self.config.stages:  # type: ignore[attr-defined]
            agent = deps.components.get(stage)
            if agent is None:
                raise ValueError(f"cascade stage {stage!r} has no agent in Dependencies.components")
            self.stages[stage] = agent

    def validate(self, env: Environment, probe: bool = False) -> list[Diagnostic]:
        diagnostics = []
        for stage, agent in self.stages.items():
            if stage not in STAGE_NAMES:
                diagnostics.append(Diagnostic(severity="error", code="unknown_stage", message=stage, component=self.name))
            diagnostics.extend(agent.validate(env, probe))
        return diagnostics

    def attempt(self, ctx: RunContext, request: DecisionRequest, intents: Intents, capabilities: Capabilities) -> Decision:
        policies: StagePolicies = self.config.stage_policies  # type: ignore[attr-defined]
        last: DecisionFailure | None = None
        stages = dict(self.stages)
        if capabilities.log_intents and capabilities.log_cascade_to_planner and "llm" in stages:
            # Logs profile: the rules already looked for a log intent (``log_step``) and found none;
            # the llm planner (with the logs context pack) plans next instead of a generic rules plan.
            names = list(stages)
            skipped = names[: names.index("llm")]
            if skipped:
                ctx.trace.event("stages_skipped", stages=skipped, reason="logs profile: no log intent found by the rules; the llm planner plans")
            stages = {name: stages[name] for name in names[names.index("llm") :]}
        for stage, agent in stages.items():
            try:
                decision = agent.attempt(ctx, request, intents, capabilities)
            except DecisionFailure as failure:
                last = failure
                policy = policies.for_stage(stage)
                ctx.trace.event("stage_failed", stage=stage, category=failure.category, policy=policy, message=str(failure))
                if policy == "abstain":
                    raise _StageAbstain(stage, failure)
                continue
            return decision.model_copy(update={"stage": stage, "policy_applied": f"cascade:{stage}"})
        assert last is not None
        raise DecisionFailure("all_stages_failed", f"every stage failed; last: {last}", last.unsupported, last.confidence)

    def decide(self, ctx: RunContext, request: DecisionRequest, intents: Intents | None, capabilities: Capabilities) -> Decision:
        intents = intents if intents is not None else self.intents(request, capabilities)
        on_failure = self.config.on_failure  # type: ignore[attr-defined]
        with ctx.trace.span("decision", agent=self.name, policy=on_failure, stages=list(self.stages)) as span:
            try:
                decision = self.log_step(ctx, request, intents, capabilities)
                if decision is None:
                    decision = self.attempt(ctx, request, intents, capabilities)
                    decision = self.require_intent(request, capabilities, decision)
                    decision = self.check_qualifiers(ctx, request, intents, capabilities, decision)
            except _StageAbstain as stop:
                decision = abstain(
                    intents,
                    agent=self.name,
                    stage=stop.stage,
                    failure=stop.failure.category,
                    policy=f"stage_abstain:{stop.stage}",
                    unsupported=stop.failure.unsupported,
                    note=str(stop.failure),
                    confidence=stop.failure.confidence,
                )
            except DecisionFailure as failure:
                span.event("decision_failed", category=failure.category, message=str(failure))
                if on_failure == "raw_all_arms":
                    decision = raw_all_arms(
                        request,
                        intents,
                        capabilities,
                        agent=self.name,
                        stage=None,
                        failure=failure.category,
                        unsupported=failure.unsupported,
                        confidence=failure.confidence,
                    )
                else:
                    decision = abstain(
                        intents,
                        agent=self.name,
                        stage=None,
                        failure=failure.category,
                        unsupported=failure.unsupported,
                        note=str(failure),
                        confidence=failure.confidence,
                    )
            span.set(
                outcome=decision.outcome,
                policy_applied=decision.policy_applied,
                failure=decision.failure,
                decision_fallback=decision.outcome == "raw_all_arms",
                unsupported=decision.unsupported,
                arms=decision.plan.arms() if decision.plan else [],
                aggregate_intents=decision.intents.kinds(),
                stage=decision.stage,
                confidence=decision.confidence,
            )
        return decision
