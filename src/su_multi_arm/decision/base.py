"""Decision agent contract (plan §3.7) and the fail-policy machinery (§3.7.1).

``DecisionAgent.decide`` never raises for a planning failure: it applies the configured policy and
returns a ``Decision`` whose ``outcome`` is ``decided``, ``abstain`` or ``raw_all_arms``. Inside a
cascade the stage-level ``attempt`` raises ``DecisionFailure`` and the cascade applies the per-stage
policy. ``raw_all_arms`` is the only fail-open outcome and is explicit opt-in: its plan is the raw
query on every enabled non-structured arm, ``scope`` is ``None``, the trace records
``decision_fallback: true`` and one ``UnsupportedConstraint`` per aggregate intent is attached so the
``AnswerContract`` (§3.9a) can never turn the answer into a count. Request filters are immutable by
every agent. Not its job: executing anything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal, Union

from pydantic import ConfigDict, Field, model_validator

from ..core.component import Dependencies, Diagnostic, Environment
from ..core.types import JsonScalar, Measure, MetadataSchema, ScopeSpec, UnsupportedConstraint
from ..models import StrictModel
from .intent import Intents, detect_intents

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime.context import RunContext

RETRIEVAL_ARMS: tuple[str, ...] = ("exact_id", "dense", "lexical", "graph", "community")
ALL_ARMS: tuple[str, ...] = (*RETRIEVAL_ARMS, "structured", "cache")
Outcome = Literal["decided", "abstain", "raw_all_arms"]
FailureCategory = Literal[
    "no_plan",
    "low_confidence",
    "artifact_invalid",
    "runtime_unavailable",
    "schema_violation",
    "timeout",
    "budget_exhausted",
    "provider_error",
    "invalid_step",
    "unresolvable_value",
    "unsupported_measure",
    "all_stages_failed",
]


class DecisionRequest(StrictModel):
    """What the agent sees. ``filters`` are the caller's explicit constraints and are immutable."""

    query: str = Field(min_length=1, max_length=16000)
    filters: dict[str, JsonScalar] = Field(default_factory=dict)
    exclude_ids: list[str] = Field(default_factory=list)
    record_context: dict[str, str] = Field(default_factory=dict)
    aggregate: Measure | None = None  # explicit typed measure from the caller (never inferred from text)
    scope: ScopeSpec | None = None  # explicit caller-typed scope (the engine maps the v1 ``aggregate`` onto it)
    canonical_record: dict[str, str] | None = None  # engine-verified catalog record named by the caller context


class RetrievalStep(StrictModel):
    arm: Literal["exact_id", "dense", "lexical", "graph", "community"]
    query: str = Field(min_length=1, max_length=16000)
    filters: dict[str, JsonScalar] = Field(default_factory=dict)  # typed eq filters on filterable fields


class StructuredStep(StrictModel):
    """Only a structured step carries a scope; an aggregate on any other arm is a schema violation."""

    arm: Literal["structured"]
    scope: ScopeSpec


PlanStep = Annotated[Union[RetrievalStep, StructuredStep], Field(discriminator="arm")]


class Plan(StrictModel):
    steps: list[PlanStep] = Field(min_length=1, max_length=20)

    def arms(self) -> list[str]:
        return list(dict.fromkeys(step.arm for step in self.steps))

    def structured_steps(self) -> list[StructuredStep]:
        return [s for s in self.steps if isinstance(s, StructuredStep)]


class Capabilities(StrictModel):
    """What the deployment allows: enabled arms, the metadata schema, the step cap, ID patterns."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    arms: list[str] = Field(min_length=1)
    metadata_schema: MetadataSchema = Field(default_factory=MetadataSchema)
    max_steps: int = Field(default=10, ge=1, le=20)
    id_patterns: list[str] = Field(default_factory=list)  # exact-identifier regexes from the alias map

    @model_validator(mode="after")
    def known_arms(self):
        unknown = [a for a in self.arms if a not in ALL_ARMS]
        if unknown:
            raise ValueError(f"unknown arms {unknown}; known: {list(ALL_ARMS)}")
        if len(set(self.arms)) != len(self.arms):
            raise ValueError("arms must be unique")
        return self

    def retrieval_arms(self) -> list[str]:
        return [a for a in self.arms if a in RETRIEVAL_ARMS]

    def has_structured(self) -> bool:
        return "structured" in self.arms

    def structured_targets(self) -> list[str]:
        """Words an aggregate operator must govern to count as structured intent (plan §3.7):
        the canonical unit, the generic record words, declared field names and time-basis labels."""
        from .intent import GENERIC_UNITS

        schema = self.metadata_schema
        targets = [schema.canonical_unit, *GENERIC_UNITS]
        for spec in schema.fields:
            targets.append(spec.name.replace("_", " "))
            if spec.time_basis:
                targets.append(spec.time_basis)
        return list(dict.fromkeys(t for t in targets if t))


class Decision(StrictModel):
    plan: Plan | None = None
    scope: ScopeSpec | None = None  # the structured scope; None under raw_all_arms and abstain
    unsupported: list[UnsupportedConstraint] = Field(default_factory=list)
    outcome: Outcome
    confidence: float | None = Field(default=None, ge=0, le=1)
    agent: str = Field(min_length=1)
    stage: str | None = None
    policy_applied: str = Field(min_length=1)
    intents: Intents = Field(default_factory=Intents)
    failure: FailureCategory | None = None
    note: str | None = None

    @model_validator(mode="after")
    def coherent(self):
        if self.outcome == "decided" and self.plan is None:
            raise ValueError("a decided outcome needs a plan")
        if self.outcome == "abstain" and self.plan is not None:
            raise ValueError("an abstain outcome carries no plan")
        if self.outcome == "raw_all_arms":
            if self.scope is not None or self.plan is None:
                raise ValueError("raw_all_arms carries a raw plan and no scope")
            if any(s.arm == "structured" for s in self.plan.steps):
                raise ValueError("raw_all_arms never runs the structured arm")
            if len(self.unsupported) < len(self.intents.aggregates):
                raise ValueError("raw_all_arms must flag every aggregate intent as unsupported")
        if self.scope is not None and (self.plan is None or not self.plan.structured_steps()):
            raise ValueError("a scope belongs to a structured step of the plan")
        return self

    def decided(self) -> bool:
        return self.outcome == "decided"


class DecisionFailure(Exception):
    """A planning failure with its category; the policy layer turns it into a ``Decision``."""

    def __init__(
        self,
        category: FailureCategory,
        message: str,
        unsupported: list[UnsupportedConstraint] | None = None,
        confidence: float | None = None,
    ):
        self.category = category
        self.unsupported = list(unsupported or [])
        self.confidence = confidence
        super().__init__(f"{category}: {message}")


def raw_all_arms(
    request: DecisionRequest,
    intents: Intents,
    capabilities: Capabilities,
    *,
    agent: str,
    stage: str | None,
    failure: FailureCategory | None,
    unsupported: list[UnsupportedConstraint] | None = None,
    confidence: float | None = None,
) -> Decision:
    """The only fail-open decision: raw query on every enabled non-structured arm; aggregates flagged."""
    arms = capabilities.retrieval_arms()
    already = {(u.constraint_text, u.reason) for u in unsupported or []}
    flagged = [
        UnsupportedConstraint(
            constraint_text=a.text,
            reason="aggregate_intent_without_scope",
            suggestion="ask with an explicit typed aggregate or declare a filterable field",
        )
        for a in intents.aggregates
        if (a.text, "aggregate_intent_without_scope") not in already
    ]
    if not arms:
        return abstain(
            intents,
            agent=agent,
            stage=stage,
            failure=failure,
            policy="raw_all_arms",
            unsupported=[*(unsupported or []), *flagged],
            note="no non-structured arm is enabled",
            confidence=confidence,
        )
    return Decision(
        plan=Plan(steps=[RetrievalStep(arm=arm, query=request.query, filters={}) for arm in arms]),
        scope=None,
        unsupported=[*(unsupported or []), *flagged],
        outcome="raw_all_arms",
        agent=agent,
        stage=stage,
        policy_applied="raw_all_arms",
        intents=intents,
        failure=failure,
        confidence=confidence,
    )


def abstain(
    intents: Intents,
    *,
    agent: str,
    stage: str | None,
    failure: FailureCategory | None,
    policy: str = "fail_closed",
    unsupported: list[UnsupportedConstraint] | None = None,
    note: str | None = None,
    confidence: float | None = None,
) -> Decision:
    return Decision(
        outcome="abstain",
        agent=agent,
        stage=stage,
        policy_applied=policy,
        intents=intents,
        failure=failure,
        unsupported=list(unsupported or []),
        note=note,
        confidence=confidence,
    )


class DecisionAgentConfig(StrictModel):
    on_failure: Literal["fail_closed", "raw_all_arms"] = "fail_closed"


class DecisionAgent:
    """Base class: ``attempt`` plans or raises ``DecisionFailure``; ``decide`` applies the policy."""

    name: ClassVar[str] = "decision"
    kind: ClassVar[str] = "decision"
    config_model: ClassVar[type[StrictModel]] = DecisionAgentConfig

    def __init__(self, config: StrictModel, deps: Dependencies):
        self.config = config
        self.deps = deps

    def validate(self, env: Environment, probe: bool = False) -> list[Diagnostic]:
        return []

    def recipe_fingerprint(self) -> str | None:
        return None  # decision agents never affect index identity

    def intents(self, request: DecisionRequest, capabilities: Capabilities) -> Intents:
        return detect_intents(request.query, capabilities.id_patterns, capabilities.structured_targets())

    def attempt(self, ctx: RunContext, request: DecisionRequest, intents: Intents, capabilities: Capabilities) -> Decision:
        raise NotImplementedError

    def decide(
        self, ctx: RunContext, request: DecisionRequest, intents: Intents | None, capabilities: Capabilities
    ) -> Decision:
        intents = intents if intents is not None else self.intents(request, capabilities)
        on_failure = getattr(self.config, "on_failure", "fail_closed")
        with ctx.trace.span("decision", agent=self.name, policy=on_failure) as span:
            try:
                decision = self.attempt(ctx, request, intents, capabilities)
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

    def run(self, ctx: RunContext, inputs: Any) -> Decision:
        request, capabilities = inputs
        return self.decide(ctx, request, None, capabilities)
