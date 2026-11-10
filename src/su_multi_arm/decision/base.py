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

from datetime import date
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal, Union

from pydantic import ConfigDict, Field, model_validator

from ..core.component import Dependencies, Diagnostic, Environment
from ..core.config_graph import GraphIntent
from ..core.types import JsonScalar, Measure, MetadataSchema, Predicate, ScopeSpec, UnsupportedConstraint
from ..core.types_typed import TypedQueryDraft
from ..models import StrictModel
from .intent import Intents, detect_intents
from .log_intents import LogEvidenceScope, LogIntentCall

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime.context import RunContext

RETRIEVAL_ARMS: tuple[str, ...] = ("exact_id", "dense", "lexical", "graph", "community", "segment")
ALL_ARMS: tuple[str, ...] = (*RETRIEVAL_ARMS, "structured", "cache", "typed_query")
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
    # Anchor day for relative calendar expressions, in ``metadata_schema.timezone`` (the engine sets
    # it from ``SearchRequest.as_of`` or the clock). None: relative windows are refused.
    as_of: date | None = None
    # Where ``as_of`` came from: ``request`` (the caller named the day) or ``clock`` (the engine's
    # default). Only a caller-named day is pinned into a log intent's arguments; a clock default
    # leaves an undated log question to the latest-served-day anchor (``log_intents.resolve_window``).
    as_of_source: Literal["request", "clock"] = "request"

    def pinned_as_of(self) -> date | None:
        """The anchor day a log intent may carry: the caller's ``as_of`` only, never a clock default."""
        return self.as_of if self.as_of_source == "request" else None


class RetrievalStep(StrictModel):
    arm: Literal["exact_id", "dense", "lexical", "graph", "community", "segment"]
    query: str = Field(min_length=1, max_length=16000)
    filters: dict[str, JsonScalar] = Field(default_factory=dict)  # typed eq filters on filterable fields
    # Graph arm only: a declared traversal template by name plus its seeds (typed; never query text).
    graph: GraphIntent | None = None
    log_scope: LogEvidenceScope | None = None

    @model_validator(mode="after")
    def graph_intent_on_graph_arm(self):
        if self.log_scope is not None and self.arm not in {"graph", "community"}:
            raise ValueError("log investigation scope is only for graph/community")
        if self.graph is not None and self.arm != "graph":
            raise ValueError(f"only the graph arm takes a graph intent, not {self.arm}")
        return self


class StructuredStep(StrictModel):
    """Only a structured step carries a scope; an aggregate on any other arm is a schema violation."""

    arm: Literal["structured"]
    scope: ScopeSpec
    # Question qualifiers this scope does not express (``decision/qualifiers.py``); the engine
    # reports the step's aggregate as ``bounded`` with these attached.
    unapplied: list[UnsupportedConstraint] = Field(default_factory=list)
    # A typed log question (addendum A2 §4.1.5, review B5): the engine runs the intent through
    # ``runtime/log_tools.py`` and ``scope`` is only the compiled main scope, for the trace.
    intent: LogIntentCall | None = None
    # How the question was read (M0, ``decision/log_question.py``: default view, question clock,
    # vocabulary): stated with the step's aggregate; informational, never bounding.
    notes: list[str] = Field(default_factory=list)


class TypedStep(StrictModel):
    """A typed query over the projection catalog (typed-query plan §5.4, M6). Exactly one of:

    * ``draft`` (arm ``mode: ir``): compiled by the decision for validation only; the ``typed_query``
      arm recompiles it with the principal's ACL before anything executes;
    * ``question`` (arm ``mode: sql_surface``): the planner's self-contained sub-question for the SQL
      route; at execution the engine has the configured ``sql_surface.role`` write one SQLite
      ``SELECT`` for it, which the arm validates, binds to the request ACL and filters and executes
      (``sql_surface/route.py``). Untrusted model text; traced masked.

    ``unapplied``: question qualifiers the step does not express and the planner's ``partial`` gaps;
    the engine reports the step's evidence ``bounded`` with them."""

    arm: Literal["typed_query"]
    draft: TypedQueryDraft | None = None
    question: str | None = Field(default=None, min_length=1, max_length=2000)
    answerability: Literal["answerable", "partial"] = "answerable"
    unapplied: list[UnsupportedConstraint] = Field(default_factory=list)

    @model_validator(mode="after")
    def one_input(self):
        if (self.draft is None) == (self.question is None):
            raise ValueError("a typed_query step carries exactly one of draft (mode ir) or question (mode sql_surface)")
        return self


PlanStep = Annotated[Union[RetrievalStep, StructuredStep, TypedStep], Field(discriminator="arm")]


class Plan(StrictModel):
    steps: list[PlanStep] = Field(min_length=1, max_length=20)

    def arms(self) -> list[str]:
        return list(dict.fromkeys(step.arm for step in self.steps))

    def structured_steps(self) -> list[StructuredStep]:
        return [s for s in self.steps if isinstance(s, StructuredStep)]

    def typed_steps(self) -> list[TypedStep]:
        return [s for s in self.steps if isinstance(s, TypedStep)]


class Capabilities(StrictModel):
    """What the deployment allows: enabled arms, the metadata schema, the step cap, ID patterns."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    arms: list[str] = Field(min_length=1)
    metadata_schema: MetadataSchema = Field(default_factory=MetadataSchema)
    max_steps: int = Field(default=10, ge=1, le=20)
    id_patterns: list[str] = Field(default_factory=list)  # exact-identifier regexes from the alias map
    # Declared graph traversal templates (name -> description) a plan may select on the graph arm.
    graph_templates: dict[str, str] = Field(default_factory=dict)
    # Logs profile (``logs.intents.enabled``): log questions go through a typed intent step, and a
    # free structured scope that could span several log kinds is refused (``intent_required``).
    log_intents: bool = False
    # The logs planner context pack (``decision/log_context.py``), built on first use by the llm
    # planner: a zero-argument callable returning the pack, or None. Never serialised.
    log_context: Any = Field(default=None, exclude=True)
    # Logs profile cascade: when the rules find no log intent, go to the llm stage directly.
    log_cascade_to_planner: bool = False
    # Episode archive (``logs.collection.archive``): count/which/when questions about outages,
    # incidents or failures over an explicit period go to ``issue_history`` (rules first, I1).
    log_history: bool = False
    # Typed-query planning (M6; ``decision/typed_plan.TypedPlanning``): set by the engine only when
    # ``pipeline.decision.typed_query.enabled``, the arm is bound and the pinned generation has a
    # sealed projection of the configured spec. Only the llm planner reads it. Never serialised.
    typed: Any = Field(default=None, exclude=True)
    # Arm cards (``pipeline.decision.arm_cards``; ``decision/arm_cards.py``): the configured limits
    # per enabled arm, set by the engine only when cards are on; None keeps the payload unchanged.
    arm_limits: Any = Field(default=None, exclude=True)
    # M0 question binding of log intents (``decision/log_question.QuestionSettings``), set by the
    # engine under ``logs.intents.enabled``; None: no default view, question clock or vocabulary.
    log_question: Any = Field(default=None, exclude=True)
    # M2b-2 source cards (``decision/source_cards.py``): set by the engine only for a two-halves
    # question under ``crosssource.planner``; the llm planner adds them to its payload. None keeps
    # the payload unchanged. Never serialised.
    source_cards: Any = Field(default=None, exclude=True)
    # M2b-2 knowledge half (staging round 2, 2026-11-10): conditions added to the ACL of every value
    # listing the agent makes (planner values, value grounding, qualifier checks), so a support
    # question is grounded on the knowledge sources' values only, as without a log member. Set by
    # ``two_halves`` only when the knowledge steps are source-scoped. Never serialised.
    value_scope: list[Predicate] = Field(default_factory=list, exclude=True)

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
                if spec.groupable:  # "by month", "per year": calendar groups of the time basis
                    targets.extend(["day", "month", "year"])
        return list(dict.fromkeys(t for t in targets if t))


# ``sql_*``: a ``typed_query`` SQL-route step (arm ``mode: sql_surface``) whose execution-time SQL
# was refused by the guard (``sql_refused``), grounded no filter value (``sql_unsupported``) or could
# not be written (``sql_error``: provider, schema, budget or availability failure); the SQL writer's
# own ``not_answerable`` is ``planner_not_answerable``.
# ``typed_router_error``: the typed router call (``routing: router``) returned no valid reply; the
# planner ran as usual. ``typed_route_discarded``: a routed typed result executed but the planner's
# decision did not proceed (abstain), so it is not used.
DroppedReason = Literal["unsolicited_structured", "step_not_executable", "planner_not_answerable", "sql_refused", "sql_unsupported", "sql_error", "typed_router_error", "typed_route_discarded"]
StepFailurePolicy = Literal["abstain", "partial", "retrieval_only"]


class DroppedStep(StrictModel):
    """A planner step the agent did not execute, recorded on the decision (never silent).

    ``unsolicited_structured``: a structured scope (a count/list/measure) on a question that asks
    no aggregate and carries no caller-typed aggregate or scope; the planner invented the operation.
    ``step_not_executable``: a structured step that did not compile, dropped under
    ``on_step_failure: partial | retrieval_only`` (its constraints are on ``Decision.unsupported``).
    ``planner_not_answerable``: a typed step the planner marked not answerable with the projection
    catalog (M6); its gaps are traced masked and the aggregate intents stay unsupported.

    ``carried_filters``: question qualifiers the dropped scope had bound to a resolved value that
    the fallback retrieval applies as typed ``eq`` filters (V3.2 follow-up); every other qualifier
    it had bound is in ``unapplied`` and on ``Decision.unsupported`` (``qualifier_not_applied``)."""

    arm: str = Field(min_length=1)
    reason: DroppedReason
    detail: str = Field(min_length=1)
    carried_filters: dict[str, JsonScalar] = Field(default_factory=dict)
    unapplied: list[UnsupportedConstraint] = Field(default_factory=list)


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
    # Planner steps that were not executed and why (``llm`` agent, V3.2); empty for every other path.
    dropped_steps: list[DroppedStep] = Field(default_factory=list)
    # ``retrieval_after_dropped_steps``: every planner step was dropped and the plan is the original
    # query on the enabled lexical/dense arms (request filters still apply). Not ``raw_all_arms``.
    # ``deterministic_plan`` (M2b-2, ``decision/fallback_plan.py``): the planner failed after its
    # repair and the two-halves planner served a labelled deterministic plan; ``failure`` says why.
    fallback: Literal["retrieval_after_dropped_steps", "deterministic_plan"] | None = None

    @model_validator(mode="after")
    def coherent(self):
        if self.outcome == "decided" and self.plan is None:
            raise ValueError("a decided outcome needs a plan")
        if self.fallback == "retrieval_after_dropped_steps" and (self.outcome != "decided" or not self.dropped_steps):
            raise ValueError("a dropped-step fallback needs a decided outcome and the dropped steps")
        if self.fallback == "deterministic_plan" and (self.outcome != "decided" or self.failure is None):
            raise ValueError("a deterministic fallback plan needs a decided outcome and the planner failure it replaces")
        if self.outcome == "abstain" and self.plan is not None:
            raise ValueError("an abstain outcome carries no plan")
        if self.outcome == "raw_all_arms":
            if self.scope is not None or self.plan is None:
                raise ValueError("raw_all_arms carries a raw plan and no scope")
            if any(s.arm in {"structured", "typed_query"} for s in self.plan.steps):
                raise ValueError("raw_all_arms never runs the structured or typed_query arm")
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

    def log_step(self, ctx: RunContext, request: DecisionRequest, intents: Intents, capabilities: Capabilities) -> Decision | None:
        """The typed log-intent step every agent goes through first (review B5): a recognised log
        question is decided here, deterministically, for ``rules``, ``classifier``, ``llm`` and
        ``cascade`` alike; ``None`` lets the agent plan."""
        if not capabilities.log_intents or "structured" not in capabilities.arms or request.scope is not None or request.aggregate is not None:
            return None
        from .log_intents import detect, detect_issue_history
        from .log_communities import shared_error_query
        import re

        records = (getattr(self.deps, "backends", None) or {}).get("records")
        schema = capabilities.metadata_schema

        def values(field: str) -> list[str]:
            spec = schema.field(field)
            if records is None or spec is None or not spec.listable:
                return []
            found = [str(v) for v in records.values(ctx.generation, field, list(ctx.constraints()), 5000)]
            return capabilities.log_question.keep(field, found) if capabilities.log_question is not None else found

        shared = shared_error_query(request.query)
        if capabilities.log_history and not shared:
            try:
                history = detect_issue_history(request.query, as_of=request.as_of, values=values, pin=request.as_of_source == "request")
            except ValueError as error:
                ctx.trace.event("log_intent_unrecognised", message=str(error))
                history = None
            if history is not None:
                return log_decision(self.bind_log_question(ctx, request, capabilities, self.bind_log_scope(ctx, request, capabilities, history)), intents, agent=self.name)
        if shared or re.search(r"\b(?:outages?|downtime)\b", request.query, re.I):
            needed = {"graph", "community"}
            if not needed <= set(capabilities.arms) or capabilities.max_steps < 2:
                raise DecisionFailure("invalid_step", "outage investigations require graph and community arms")
            call = self.bind_log_scope(ctx, request, capabilities, LogIntentCall(intent="log_top", args={"as_of": request.pinned_as_of().isoformat()} if request.pinned_as_of() else {}))
            question = self.bind_log_question(ctx, request, capabilities, call)  # M0: default view, clock, vocabulary
            call = question.calls[0]
            fields = LogEvidenceScope.model_fields
            scope = LogEvidenceScope.model_validate({k: v for k, v in call.args.items() if k in fields})
            note = "Scoped signature-seeded community and graph; pattern membership is not confirmed outage or cause"
            return Decision(plan=Plan(steps=[RetrievalStep(arm=arm, query=request.query, log_scope=scope) for arm in ("community", "graph")]), outcome="decided", agent=self.name, policy_applied="log_shared_errors" if shared else "log_outage_investigation", intents=intents, unsupported=list(question.unapplied), note=" ".join([note, *question.notes]))

        try:
            call = detect(request.query, as_of=request.as_of, values=values, pin=request.as_of_source == "request")
        except ValueError as error:
            ctx.trace.event("log_intent_unrecognised", message=str(error))
            return None
        if call is None:
            return None
        call = self.bind_log_scope(ctx, request, capabilities, call)
        return log_decision(self.bind_log_question(ctx, request, capabilities, call), intents, agent=self.name)

    def bind_log_scope(self, ctx: RunContext, request: DecisionRequest, capabilities: Capabilities, call: LogIntentCall) -> LogIntentCall:
        from .log_intents import bind_question_scope

        records = self.deps.backends.get("records")

        def values(field: str) -> list[str]:
            spec = capabilities.metadata_schema.field(field)
            if records is None or spec is None or not spec.listable:
                return []
            found = [str(v) for v in records.values(ctx.generation, field, list(ctx.constraints()), 5000)]
            return capabilities.log_question.keep(field, found) if capabilities.log_question is not None else found

        try:
            bound = bind_question_scope(call, request.query, as_of=request.as_of, values=values)
        except ValueError as error:
            unsupported = UnsupportedConstraint(constraint_text=request.query, reason="qualifier_not_applied", suggestion=str(error))
            raise DecisionFailure("unresolvable_value", "the log intent cannot honour the question's filters", [unsupported]) from error
        if bound.args != call.args:
            ctx.trace.event("log_question_scope_bound", fields=sorted(k for k in bound.args if bound.args.get(k) != call.args.get(k)))
        return bound

    def bind_log_question(self, ctx: RunContext, request: DecisionRequest, capabilities: Capabilities, call: LogIntentCall) -> Any:
        """M0 question binding (``decision/log_question.bind_question``): the default view of an
        undated outage/presence question, the question clock and the vocabulary. Returns a
        ``BoundQuestion`` (one call, or one per reading of an ambiguous hour for ``log_around``)."""
        from .log_question import BoundQuestion, bind_question

        settings = capabilities.log_question
        if settings is None:
            return BoundQuestion(calls=[call])
        pinned = request.pinned_as_of()
        if pinned is not None:  # a caller-named day: an undated clock hour is read on that day, not on today
            from dataclasses import replace
            from datetime import datetime, time
            from zoneinfo import ZoneInfo

            end = datetime.combine(pinned, time(23, 59, 59), ZoneInfo(settings.timezone))
            settings = replace(settings, now=lambda: end)
        records = self.deps.backends.get("records")

        def values(field: str) -> list[str]:
            spec = capabilities.metadata_schema.field(field)
            if records is None or spec is None or not spec.listable:
                return []
            return [str(v) for v in records.values(ctx.generation, field, list(ctx.constraints()), 5000)]

        try:
            bound = bind_question(call, request.query, settings, as_of=request.as_of, values=values)
        except ValueError as error:
            unsupported = UnsupportedConstraint(constraint_text=request.query, reason="qualifier_not_applied", suggestion=str(error))
            raise DecisionFailure("unresolvable_value", "the log intent cannot honour the question's terms", [unsupported]) from error
        if bound.notes or bound.unapplied or len(bound.calls) != 1 or bound.calls[0].args != call.args:
            ctx.trace.event("log_question_bound", calls=len(bound.calls), notes=len(bound.notes), unapplied=len(bound.unapplied),
                            fields=sorted({k for c in bound.calls for k in c.args if c.args.get(k) != call.args.get(k)}))
        return bound

    def require_intent(self, request: DecisionRequest, capabilities: Capabilities, decision: Decision) -> Decision:
        """Under the logs profile a structured step without an intent must pin one document kind
        (``log_guard``); otherwise the plan is refused with ``intent_required``."""
        if not capabilities.log_intents or decision.plan is None or request.scope is not None or request.aggregate is not None:
            return decision
        from .log_guard import log_scope_guard

        for step in decision.plan.structured_steps():
            if step.intent is None and (refused := log_scope_guard(step.scope)) is not None:
                raise DecisionFailure("invalid_step", "a free structured scope over the log rollups needs a log intent", [refused])
        return decision

    def check_qualifiers(self, ctx: RunContext, request: DecisionRequest, intents: Intents, capabilities: Capabilities, decision: Decision) -> Decision:
        """Qualifier coverage of every agent-compiled structured step (QA-01, ``qualifiers.py``).
        Unapplied qualifiers are attached to the step and the decision; nothing is re-planned."""
        if decision.outcome != "decided" or decision.plan is None or not (decision.plan.structured_steps() or decision.plan.typed_steps()):
            return decision
        if request.aggregate is not None or request.scope is not None:
            return decision  # the caller typed the operation: its scope is the caller's filters
        from .intent import untargeted_constraint
        from .qualifiers import NO_OPERATOR, qualifier_candidates, unapplied
        from .values import ValueResolver

        schema = capabilities.metadata_schema
        backends = getattr(self.deps, "backends", None) or {}
        resolver = ValueResolver(schema, backends.get("records"), ctx.generation, [*ctx.constraints(), *capabilities.value_scope], value_scoped=bool(capabilities.value_scope))
        candidates = qualifier_candidates(request.query, schema, resolver, intents)
        known = {(u.constraint_text, u.reason) for u in decision.unsupported}
        added: list[UnsupportedConstraint] = []
        steps: list[RetrievalStep | StructuredStep | TypedStep] = []
        for step in decision.plan.steps:
            if isinstance(step, TypedStep) and step.draft is None:
                pass  # SQL route: checked against the executed SQL by the engine (``sql_surface/route.py``)
            elif isinstance(step, TypedStep):
                from .typed_plan import typed_unapplied

                missing = typed_unapplied(candidates, step.draft, request.filters, schema)
                if not intents.aggregate() and (step.draft.measure is not None or step.draft.group_by):
                    # a typed lookup/list answers a lookup question; a typed aggregate needs an operator
                    missing.extend([untargeted_constraint(u) for u in intents.untargeted] or [NO_OPERATOR])
                if missing:
                    step = step.model_copy(update={"unapplied": [*step.unapplied, *missing]})
                    for item in missing:
                        if (item.constraint_text, item.reason) not in known:
                            known.add((item.constraint_text, item.reason))
                            added.append(item)
            elif isinstance(step, StructuredStep) and step.intent is None:
                missing = unapplied(candidates, step.scope, request.filters, schema)
                if not intents.aggregate():
                    # QQ-04: a list of an undeclared record noun is an operator, just not a typed one.
                    missing.extend([untargeted_constraint(u) for u in intents.untargeted] or [NO_OPERATOR])
                if missing:
                    step = step.model_copy(update={"unapplied": [*step.unapplied, *missing]})
                    for item in missing:
                        if (item.constraint_text, item.reason) not in known:
                            known.add((item.constraint_text, item.reason))
                            added.append(item)
            steps.append(step)
        if not added:
            return decision.model_copy(update={"plan": Plan(steps=steps)})
        ctx.trace.event("qualifiers_not_applied", count=len(added), reasons=sorted({u.reason for u in added}))
        return decision.model_copy(update={"plan": Plan(steps=steps), "unsupported": [*decision.unsupported, *added]})

    def decide(
        self, ctx: RunContext, request: DecisionRequest, intents: Intents | None, capabilities: Capabilities
    ) -> Decision:
        intents = intents if intents is not None else self.intents(request, capabilities)
        on_failure = getattr(self.config, "on_failure", "fail_closed")
        with ctx.trace.span("decision", agent=self.name, policy=on_failure) as span:
            try:
                decision = self.log_step(ctx, request, intents, capabilities)
                if decision is None:
                    decision = self.attempt(ctx, request, intents, capabilities)
                    decision = self.require_intent(request, capabilities, decision)
                    decision = self.check_qualifiers(ctx, request, intents, capabilities, decision)
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
                dropped_steps=[d.model_dump() for d in decision.dropped_steps],
                planner_fallback=decision.fallback,
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


def log_decision(call: Any, intents: Intents, *, agent: str, policy: str = "log_intent") -> Decision:
    """A decided plan of typed log intents. ``call``: one ``LogIntentCall`` or a
    ``log_question.BoundQuestion`` (M0: one step per call, its notes and unapplied qualifiers on
    every step and on the decision). The step's scope is the compiled main scope (for the trace);
    the engine executes the intent itself, never this scope."""
    from datetime import date as _date

    from .log_intents import CompileContext, compile_intent

    calls, notes, unapplied = ([call], [], []) if isinstance(call, LogIntentCall) else (call.calls, call.notes, call.unapplied)
    steps = []
    for item in calls:
        compiled = compile_intent(item, CompileContext(today=_date.fromisoformat(item.args["as_of"]) if item.args.get("as_of") else _date.today()))
        steps.append(StructuredStep(arm="structured", scope=compiled.scopes.get("main") or ScopeSpec(), intent=item, notes=list(notes), unapplied=list(unapplied)))
    return Decision(plan=Plan(steps=steps), scope=steps[0].scope, outcome="decided", agent=agent, policy_applied=policy, intents=intents, unsupported=list(unapplied))
