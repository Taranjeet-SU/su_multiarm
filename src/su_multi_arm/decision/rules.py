"""``rules`` decision agent (plan §3.7): a deterministic intent grammar, English only.

* aggregate intents (``count``, ``total``, ``list``, ``absence``, ``earliest``, ``latest``) become a
  structured step whose scope is compiled through ``ScopeCompiler``; predicates come only from
  observed values of listable fields (or enum values) that appear verbatim in the question, and from
  the caller's request filters. Any residual qualifier the grammar cannot bind to a field is an
  ``UnsupportedConstraint`` and the agent fails (``no_plan``) rather than counting a wider population.
* ``total`` becomes ``sum(field)`` only when a declared numeric field is named; it is never a count.
* ``earliest``/``latest`` need a declared ``time_basis`` field named or unique in the schema.
* exact identifiers (``Capabilities.id_patterns``) become an ``exact_id`` step (or ``lexical``).
* everything else is the raw question on every enabled non-structured arm (today's raw mode).

Standalone failure follows ``on_failure`` (default ``fail_closed`` = abstain). Inside a cascade the
stage policy applies (default ``fallback_to_next_stage``).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, ClassVar, Literal


from ..core.component import Dependencies
from ..core.types import Measure, MetadataSchema, UnsupportedConstraint
from ..models import StrictModel
from .base import Capabilities, Decision, DecisionAgent, DecisionFailure, DecisionRequest, Plan, RetrievalStep, StructuredStep
from .intent import Intents
from .grammar import QueryGrammar
from .scope import CompiledScope, ScopeCompiler, ScopeDraft
from .values import ValueResolver

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime.context import RunContext

STOP_WORDS: frozenset[str] = frozenset(
    """a an and are be by can could did do does for from give has have how i is it its list many me
    much my number of on or our please records record show tell that the their there these this
    those to total us was we were what which who whose will with you your count all every any at
    exist exists exists recorded there earliest latest first last most recent oldest newest seen
    known reported time sum summed altogether in into between during when where why entries items
    documents document rows row results result""".split()
)
WORD = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)
DEFAULT_LIST_LIMIT = 50  # records a ``list`` question shows; the total is always reported


class RulesConfig(StrictModel):
    on_failure: Literal["fail_closed", "raw_all_arms"] = "fail_closed"
    default_arms: list[str] | None = None  # retrieval arms for plain questions; None = all enabled


class RulesAgent(DecisionAgent):
    name: ClassVar[str] = "rules"
    config_model: ClassVar[type[StrictModel]] = RulesConfig

    def __init__(self, config: StrictModel, deps: Dependencies):
        super().__init__(config, deps)
        self.records = deps.backends.get("records")

    def resolver(self, ctx: RunContext, schema: MetadataSchema, anchor: Any = None) -> ValueResolver:
        return ValueResolver(schema, self.records, ctx.generation, ctx.constraints(), anchor=anchor)

    def attempt(self, ctx: RunContext, request: DecisionRequest, intents: Intents, capabilities: Capabilities) -> Decision:
        if request.scope is None and (intents.aggregate() or request.aggregate is not None):
            return self._aggregate(ctx, request, intents, capabilities)
        arms = self._arms(capabilities)
        steps: list[RetrievalStep | StructuredStep] = []
        if intents.exact_ids and "exact_id" in capabilities.arms:
            steps.append(RetrievalStep(arm="exact_id", query=" ".join(intents.exact_ids)))
            steps.extend(RetrievalStep(arm=a, query=request.query) for a in arms if a != "exact_id")
        else:
            steps.extend(RetrievalStep(arm=a, query=request.query) for a in arms if a != "exact_id")
        scope = None
        if request.scope is not None and capabilities.has_structured():
            # The caller typed the scope (today's raw mode with an explicit aggregate): it runs as given
            # next to the raw retrieval steps; the arm and the record store report its exactness.
            scope = request.scope
            steps.append(StructuredStep(arm="structured", scope=scope))
        if not steps:
            raise DecisionFailure("no_plan", "no non-structured arm is enabled for a retrieval question")
        return Decision(plan=Plan(steps=steps), scope=scope, outcome="decided", agent=self.name, policy_applied="rules", intents=intents)

    def _arms(self, capabilities: Capabilities) -> list[str]:
        wanted = self.config.default_arms  # type: ignore[attr-defined]
        enabled = capabilities.retrieval_arms()
        if wanted is None:
            return enabled
        unknown = [a for a in wanted if a not in enabled]
        if unknown:
            raise DecisionFailure("invalid_step", f"default_arms {unknown} are not enabled")
        return [a for a in wanted]

    # --- aggregate grammar ------------------------------------------------------------------------

    def _aggregate(self, ctx: RunContext, request: DecisionRequest, intents: Intents, capabilities: Capabilities) -> Decision:
        if not capabilities.has_structured():
            flagged = [
                UnsupportedConstraint(constraint_text=a.text, reason="aggregate_intent_without_scope", suggestion="enable the structured arm")
                for a in intents.aggregates
            ] or [UnsupportedConstraint(constraint_text="aggregate", reason="aggregate_intent_without_scope", suggestion="enable the structured arm")]
            raise DecisionFailure("no_plan", "aggregate intent but the structured arm is not enabled", flagged)
        schema = capabilities.metadata_schema
        resolver = self.resolver(ctx, schema, request.as_of)
        compiler = ScopeCompiler(schema, resolver)
        if request.aggregate is not None:
            draft = ScopeDraft(measure=request.aggregate, time_basis=self._time_basis(schema, request.query, request.aggregate))
            compiled = compiler.compile(draft, request.filters)
            return self._finish(compiled, intents)
        kinds = intents.kinds()
        grammar = QueryGrammar(schema, resolver, request.as_of, set(request.filters))
        parsed = grammar.parse(request.query, kinds)
        draft = parsed.draft
        unsupported: list[UnsupportedConstraint] = list(parsed.unsupported)
        if parsed.residual:
            unsupported.append(
                UnsupportedConstraint(
                    constraint_text=" ".join(parsed.residual),
                    reason="field_not_filterable",
                    suggestion="no declared filterable field expresses this qualifier; the count would ignore it",
                )
            )
        if draft.measure is None and draft.sort is None:
            measure, time_basis, problem = self._measure(kinds, request.query, schema)
            if problem is not None and not (draft.group_by or draft.periods):
                unsupported.append(problem)
            elif problem is None:
                draft.measure = measure
                draft.time_basis = draft.time_basis or time_basis
        if not (draft.group_by or draft.periods) and draft.limit is None and "list" in kinds and "count" not in kinds and "absence" not in kinds:
            if draft.measure is None:
                draft.limit = DEFAULT_LIST_LIMIT
        if unsupported:
            raise DecisionFailure("unresolvable_value" if parsed.residual else "unsupported_measure", "grammar left unbound qualifiers", unsupported)
        compiled = compiler.compile(draft, request.filters)
        return self._finish(compiled, intents)

    def _finish(self, compiled: CompiledScope, intents: Intents) -> Decision:
        if not compiled.executable():
            category = "unsupported_measure" if any(u.reason == "measure_unsupported" for u in compiled.unsupported) else "unresolvable_value"
            raise DecisionFailure(category, "scope cannot be executed as asked", compiled.unsupported)
        note = None
        if compiled.zero_match_fields:
            note = f"request filters with no observed value: {compiled.zero_match_fields}"
        return Decision(
            plan=Plan(steps=[StructuredStep(arm="structured", scope=compiled.spec)]),  # type: ignore[arg-type]
            scope=compiled.spec,
            outcome="decided",
            agent=self.name,
            policy_applied="rules",
            intents=intents,
            note=note,
        )

    def _measure(self, kinds: list[str], query: str, schema: MetadataSchema) -> tuple[Measure | None, str | None, UnsupportedConstraint | None]:
        words = set(WORD.findall(query.casefold()))
        if "total" in kinds and "count" not in kinds:
            numeric = [f for f in schema.fields if f.type in {"integer", "number"} and f.name.casefold() in words]
            if len(numeric) != 1:
                return None, None, UnsupportedConstraint(
                    constraint_text="total",
                    reason="measure_unsupported",
                    suggestion="a total needs exactly one declared numeric field named in the question; a record count is not a total",
                )
            return Measure(kind="sum", field=numeric[0].name), None, None
        if "earliest" in kinds or "latest" in kinds:
            basis = self._time_basis(schema, query, None)
            if basis is None:
                return None, None, UnsupportedConstraint(
                    constraint_text="earliest" if "earliest" in kinds else "latest",
                    reason="measure_unsupported",
                    suggestion="declare a datetime/date field with time_basis (and name it when several exist)",
                )
            kind = "first_seen" if "earliest" in kinds else "last_seen"
            return Measure(kind=kind, field=basis), basis, None
        # count, list and absence are exact population operations; list keeps no measure (records).
        if "list" in kinds and "count" not in kinds and "absence" not in kinds:
            return None, None, None
        return Measure(kind="count"), None, None

    @staticmethod
    def _time_basis(schema: MetadataSchema, query: str, measure: Measure | None) -> str | None:
        candidates = [f for f in schema.fields if f.type in {"datetime", "date"} and f.time_basis]
        if measure is not None and measure.field:
            return measure.field if any(f.name == measure.field for f in candidates) else None
        words = set(WORD.findall(query.casefold()))
        named = [f for f in candidates if f.name.casefold() in words or (f.time_basis or "").casefold() in words]
        if len(named) == 1:
            return named[0].name
        if not named and len(candidates) == 1:
            return candidates[0].name
        return None
