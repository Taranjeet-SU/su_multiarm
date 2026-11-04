"""``llm`` decision agent (plan §3.7): a planner with per-arm step schemas, validated end to end.

The model receives the question, the caller context (as untrusted data), the enabled arms, the
immutable request filters, the filterable fields with their types and operations, and observed
values **only** for listable fields under their cardinality cap (ACL-scoped). It returns an
``LlmPlan`` validated against a strict schema: a retrieval step carries a query and typed ``eq``
filters; only a ``structured`` step carries a scope draft. Every step is then checked (enabled arm,
filterable fields, no override of request filters, step cap) and every scope compiled through
``ScopeCompiler``, so an unresolvable value or an unsupported measure is a planning failure with
the ``UnsupportedConstraint`` attached — never a silently widened count.

Failure categories (plan §3.7.1 ``llm`` row): ``schema_violation``, ``timeout``,
``budget_exhausted``, ``provider_error``, ``invalid_step``, ``unresolvable_value``,
``unsupported_measure``. The default outcome is ``abstain``; ``raw_all_arms`` is an explicit opt-in.
The prompt is neutral: it carries no domain authority rules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal

import httpx
from pydantic import Field, ValidationError, model_validator

from ..core.component import Dependencies, Diagnostic, Environment
from ..core.config import ChatRole
from ..core.errors import BudgetExhausted, DeadlineExceeded, SuError
from ..core.types import JsonScalar, MetadataSchema, Predicate, UnsupportedConstraint
from ..models import StrictModel
from ..providers.accounting import RunBudgetMeter
from ..providers.base import ProviderError, ProviderTimeout, SchemaViolation
from .base import Capabilities, Decision, DecisionAgent, DecisionFailure, DecisionRequest, Plan, RetrievalStep, StructuredStep
from .intent import Intents
from .scope import ScopeCompiler, ScopeDraft
from .values import ValueResolver

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime.context import RunContext

from ..profiles.prompts import PLANNER_PROMPT  # noqa: E402  (neutral prompt; profiles may overlay it)


class LlmFilter(StrictModel):
    """One typed ``eq`` filter on a retrieval step (a list of pairs, not a free-form mapping, so the
    plan schema stays expressible in OpenAI strict mode)."""

    field: str = Field(min_length=1)
    value: JsonScalar


LLM_ARMS = ("exact_id", "dense", "lexical", "graph", "community", "structured")


class LlmStep(StrictModel):
    """One planned step. A retrieval arm carries ``query`` (and optional ``filters``); only the
    ``structured`` arm carries ``scope``. One flat object instead of a discriminated union: strict
    structured output refuses ``oneOf``/``discriminator`` (QA-15), and the coherence rules below
    are checked on every reply whatever the wire mode."""

    arm: Literal["exact_id", "dense", "lexical", "graph", "community", "structured"]
    query: str | None = Field(default=None, max_length=16000)
    filters: list[LlmFilter] = Field(default_factory=list)
    scope: ScopeDraft | None = None

    @model_validator(mode="before")
    @classmethod
    def _mapping_filters(cls, data: Any) -> Any:
        # Replies recorded before 2.0.0a2 carried ``filters`` as a mapping; the same typed pairs.
        if isinstance(data, dict) and isinstance(data.get("filters"), dict):
            data = {**data, "filters": [{"field": k, "value": v} for k, v in data["filters"].items()]}
        return data

    @model_validator(mode="after")
    def _coherent(self):
        if self.arm == "structured":
            if self.scope is None:
                raise ValueError("a structured step needs a scope")
            if self.filters or (self.query or "").strip():
                raise ValueError("a structured step carries its constraints in scope, not query/filters")
        else:
            if self.scope is not None:
                raise ValueError(f"only the structured arm carries a scope, not {self.arm}")
            if not (self.query or "").strip():
                raise ValueError(f"a {self.arm} step needs a non-empty query")
        names = [f.field for f in self.filters]
        if len(set(names)) != len(names):
            raise ValueError("a step filters each field at most once")
        return self

    def filter_map(self) -> dict[str, JsonScalar]:
        return {f.field: f.value for f in self.filters}


class LlmPlan(StrictModel):
    """Wire schema the model must satisfy. Validated before any step is accepted. Strict-expressible
    (checked at config validation for native OpenAI planners)."""

    steps: list[LlmStep] = Field(min_length=1, max_length=20)


class LlmConfig(StrictModel):
    on_failure: Literal["fail_closed", "raw_all_arms"] = "fail_closed"
    role: ChatRole
    max_steps: int = Field(default=10, ge=1, le=20)
    prompt: str | None = None


# Request-path metering (budget + trace) now lives in ``providers.accounting``; the old names stay.
_BudgetBridge = RunBudgetMeter
BudgetBridge = RunBudgetMeter


class LlmAgent(DecisionAgent):
    name: ClassVar[str] = "llm"
    config_model: ClassVar[type[StrictModel]] = LlmConfig

    def __init__(self, config: StrictModel, deps: Dependencies):
        super().__init__(config, deps)
        self.records = deps.backends.get("records")

    def validate(self, env: Environment, probe: bool = False) -> list[Diagnostic]:
        role: ChatRole = self.config.role  # type: ignore[attr-defined]
        if role.api_key_env and not env.has(role.api_key_env):
            return [Diagnostic(severity="error", code="missing_secret", message=f"{role.api_key_env} is not set", path="role.api_key_env", component=self.name)]
        return []

    def resolver(self, ctx: RunContext, schema: MetadataSchema) -> ValueResolver:
        return ValueResolver(schema, self.records, ctx.generation, ctx.constraints())

    def payload(self, request: DecisionRequest, intents: Intents, capabilities: Capabilities, resolver: ValueResolver) -> dict[str, Any]:
        schema = capabilities.metadata_schema
        fields = []
        for spec in schema.fields:
            if not spec.filterable:
                continue
            ops = ["eq", "in", "missing"] if spec.nullable else ["eq", "in"]
            if spec.type in {"integer", "number"}:
                ops.append("range")
            if spec.type in {"datetime", "date"}:
                ops.extend(["range", "date_interval"])
            fields.append(
                {
                    "name": spec.name,
                    "type": spec.type,
                    "operations": ops,
                    "enum_values": spec.enum_values,
                    "groupable": spec.groupable,
                    "sortable": spec.sortable,
                    "time_basis": spec.time_basis,
                }
            )
        return {
            "question": request.query,
            "caller_context_unverified": request.record_context,
            "canonical_record": request.canonical_record,
            "available_arms": capabilities.arms,
            "request_filters": request.filters,
            "filterable_fields": fields,
            "observed_values": resolver.planner_values(),
            "canonical_unit": schema.canonical_unit,
            "explicit_aggregate": request.aggregate.model_dump() if request.aggregate else None,
            "explicit_scope": request.scope.model_dump(mode="json") if request.scope else None,
            "detected_intents": intents.kinds(),
            "max_steps": min(capabilities.max_steps, self.config.max_steps),  # type: ignore[attr-defined]
        }

    def call_model(self, ctx: RunContext, payload: dict[str, Any]) -> LlmPlan:
        from ..providers import JsonModel

        role: ChatRole = self.config.role  # type: ignore[attr-defined]
        bridge = RunBudgetMeter(ctx, "planner", role.price_key())
        prompt = self.config.prompt or PLANNER_PROMPT  # type: ignore[attr-defined]
        try:
            return JsonModel(role).call(prompt, payload, LlmPlan, bridge, "planner")
        except BudgetExhausted as error:
            raise DecisionFailure("budget_exhausted", str(error)) from error
        except DeadlineExceeded as error:
            raise DecisionFailure("timeout", str(error)) from error
        except (ProviderTimeout, httpx.TimeoutException) as error:
            raise DecisionFailure("timeout", f"planner timed out: {type(error).__name__}") from error
        except SchemaViolation as error:
            raise DecisionFailure("schema_violation", f"planner reply violates the plan schema ({error.error_count} errors)") from error
        except ValidationError as error:
            raise DecisionFailure("schema_violation", f"planner reply violates the plan schema ({error.error_count()} errors)") from error
        except SuError:
            raise  # RunCancelled, BackendError, ...: typed failures propagate; never a fail-policy "provider_error"
        except (ProviderError, httpx.HTTPError) as error:
            raise DecisionFailure("provider_error", str(error)) from error

    def attempt(self, ctx: RunContext, request: DecisionRequest, intents: Intents, capabilities: Capabilities) -> Decision:
        schema = capabilities.metadata_schema
        resolver = self.resolver(ctx, schema)
        plan = self.call_model(ctx, self.payload(request, intents, capabilities, resolver))
        max_steps = min(capabilities.max_steps, self.config.max_steps)  # type: ignore[attr-defined]
        if len(plan.steps) > max_steps:
            raise DecisionFailure("invalid_step", f"planner exceeded max_steps ({len(plan.steps)} > {max_steps})")
        compiler = ScopeCompiler(schema, resolver)
        steps: list[RetrievalStep | StructuredStep] = []
        scope = None
        note = None
        for step in plan.steps:
            if step.arm not in capabilities.arms:
                raise DecisionFailure("invalid_step", f"planner selected disabled arm {step.arm!r}")
            if step.arm == "structured":
                assert step.scope is not None  # LlmStep._coherent
                if request.aggregate is not None and step.scope.measure is not None and step.scope.measure != request.aggregate:
                    raise DecisionFailure("invalid_step", "planner changed the caller's explicit aggregate")
                kinds = intents.kinds()
                if "total" in kinds and "count" not in kinds and step.scope.measure is not None and step.scope.measure.kind == "count":
                    raise DecisionFailure(
                        "unsupported_measure",
                        "a total is not a record count",
                        [
                            UnsupportedConstraint(
                                constraint_text=next(a.text for a in intents.aggregates if a.kind == "total"),
                                reason="measure_unsupported",
                                suggestion="a total needs a declared numeric field; the planner proposed a record count instead",
                            )
                        ],
                    )
                compiled = compiler.compile(step.scope, request.filters)
                if not compiled.executable():
                    category = "unsupported_measure" if any(u.reason == "measure_unsupported" for u in compiled.unsupported) else "unresolvable_value"
                    raise DecisionFailure(category, "structured step cannot be executed as planned", compiled.unsupported)
                if compiled.zero_match_fields:
                    note = f"request filters with no observed value: {compiled.zero_match_fields}"
                if scope is None:
                    scope = compiled.spec
                steps.append(StructuredStep(arm="structured", scope=compiled.spec))  # type: ignore[arg-type]
            else:
                steps.append(RetrievalStep(arm=step.arm, query=step.query or "", filters=self._step_filters(step, request, resolver)))  # type: ignore[arg-type]
        return Decision(
            plan=Plan(steps=steps),
            scope=scope,
            outcome="decided",
            agent=self.name,
            policy_applied="llm",
            intents=intents,
            note=note,
        )

    @staticmethod
    def _step_filters(step: LlmStep, request: DecisionRequest, resolver: ValueResolver) -> dict[str, JsonScalar]:
        filters: dict[str, JsonScalar] = {}
        unsupported: list[UnsupportedConstraint] = []
        for field, value in step.filter_map().items():
            if field in request.filters:
                if request.filters[field] != value:
                    raise DecisionFailure("invalid_step", f"planner cannot override request filter {field!r}")
                continue
            resolution = resolver.resolve(Predicate(field=field, op="eq", value=value))
            if resolution.unsupported is not None:
                unsupported.append(resolution.unsupported)
                continue
            filters[field] = resolution.predicate.value  # type: ignore[union-attr]
        if unsupported:
            if any(u.reason == "field_not_filterable" for u in unsupported):
                raise DecisionFailure("invalid_step", "planner filter on a non-filterable field", unsupported)
            raise DecisionFailure("unresolvable_value", "planner filter value could not be resolved", unsupported)
        return filters
