"""``ScopeCompiler`` (plan §3.7.2): turns a draft scope into an executable ``ScopeSpec`` or refuses.

A draft is a ``ScopeSpec``-shaped proposal from the rules grammar or an LLM planner: its values are
unverified. Compilation resolves every predicate through ``ValueResolver``, prepends the caller's
request filters (immutable; a draft predicate that contradicts one is an ``invalid_step``), checks
``group_by``/``sort`` flags and the measure rules, and returns a ``CompiledScope``. Any unsupported
predicate makes the operation non-executable (``abstain``): a count over a scope with a dropped
predicate would be a different question. A ``measure_unsupported`` is **never** substituted by
``count``.
"""

from __future__ import annotations

from pydantic import Field

from ..core.types import JsonScalar, Measure, MetadataSchema, Predicate, ScopeSpec, Sort, UnsupportedConstraint
from ..models import StrictModel
from .base import DecisionFailure
from .values import ValueResolver, resolve_request_filters

NUMERIC = {"integer", "number"}
TEMPORAL = {"datetime", "date"}
NUMERIC_MEASURES = {"sum", "avg", "min", "max"}
TEMPORAL_MEASURES = {"first_seen", "last_seen", "bucket"}


class ScopeDraft(StrictModel):
    """Unverified proposal; the same shape as ``ScopeSpec`` with a free-text ``reason``."""

    predicates: list[Predicate] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    top_n: int | None = Field(default=None, ge=1)
    sort: Sort | None = None
    measure: Measure | None = None
    time_basis: str | None = None
    population: str = "canonical_record"
    reason: str | None = None


class CompiledScope(StrictModel):
    spec: ScopeSpec | None = None  # None when the operation cannot be executed as asked
    unsupported: list[UnsupportedConstraint] = Field(default_factory=list)
    zero_match_fields: list[str] = Field(default_factory=list)  # caller filters with no observed value

    def executable(self) -> bool:
        return self.spec is not None and not self.unsupported


class ScopeCompiler:
    def __init__(self, schema: MetadataSchema, resolver: ValueResolver):
        self.schema = schema
        self.resolver = resolver

    def compile(self, draft: ScopeDraft | ScopeSpec, request_filters: dict[str, JsonScalar] | None = None) -> CompiledScope:
        filters = dict(request_filters or {})
        unsupported: list[UnsupportedConstraint] = []
        predicates: list[Predicate] = []
        resolved_filters = resolve_request_filters(self.resolver, filters)
        unsupported.extend(resolved_filters.unsupported)
        predicates.extend(resolved_filters.predicates)
        for predicate in draft.predicates:
            if predicate.field in filters:
                if predicate.op != "eq" or predicate.value != filters[predicate.field] or predicate.negate:
                    raise DecisionFailure("invalid_step", f"predicate on {predicate.field!r} overrides a request filter")
                continue  # identical to the caller's filter: already applied
            resolution = self.resolver.resolve(predicate)
            if resolution.unsupported is not None:
                unsupported.append(resolution.unsupported)
            else:
                predicates.append(resolution.predicate)  # type: ignore[arg-type]
        for field in draft.group_by:
            if field not in self.schema.groupable():
                unsupported.append(self._flag(f"group by {field}", "field_not_filterable", f"declare {field!r} as groupable"))
        if draft.sort is not None and draft.sort.field not in self.schema.sortable():
            unsupported.append(self._flag(f"sort by {draft.sort.field}", "field_not_filterable", f"declare {draft.sort.field!r} as sortable"))
        time_basis = draft.time_basis
        if time_basis is not None:
            spec = self.schema.field(time_basis)
            if spec is None or spec.type not in TEMPORAL or spec.time_basis is None:
                unsupported.append(self._flag(f"time basis {time_basis}", "measure_unsupported", "declare a datetime/date field with time_basis"))
                time_basis = None
        measure = draft.measure
        if measure is not None:
            problem = self._check_measure(measure, time_basis)
            if problem is not None:
                unsupported.append(problem)
        if unsupported:
            return CompiledScope(spec=None, unsupported=unsupported, zero_match_fields=resolved_filters.zero_match_fields)
        if measure is not None and measure.kind in TEMPORAL_MEASURES and measure.field is None:
            measure = measure.model_copy(update={"field": time_basis})
        spec = ScopeSpec(
            predicates=predicates,
            group_by=list(draft.group_by),
            top_n=draft.top_n,
            sort=draft.sort,
            measure=measure,
            time_basis=time_basis,
            population=draft.population,  # type: ignore[arg-type]
        )
        return CompiledScope(spec=spec, zero_match_fields=resolved_filters.zero_match_fields)

    def _check_measure(self, measure: Measure, time_basis: str | None) -> UnsupportedConstraint | None:
        kind = measure.kind
        if kind in NUMERIC_MEASURES:
            spec = self.schema.field(measure.field or "")
            if spec is None or spec.type not in NUMERIC:
                return self._flag(
                    f"{kind}({measure.field})", "measure_unsupported", f"{kind} needs a declared integer/number field; it is never replaced by a count"
                )
        elif kind in TEMPORAL_MEASURES:
            field = measure.field or time_basis
            spec = self.schema.field(field or "")
            if time_basis is None or spec is None or spec.type not in TEMPORAL or spec.time_basis is None:
                return self._flag(f"{kind}({field})", "measure_unsupported", f"{kind} needs a declared time_basis")
            if measure.field is not None and measure.field != time_basis:
                return self._flag(f"{kind}({field})", "measure_unsupported", "measure field must be the declared time_basis")
        elif kind == "count_distinct" and measure.unit is not None:
            if self.schema.field(measure.unit) is None and measure.unit != self.schema.canonical_unit:
                return self._flag(f"count_distinct({measure.unit})", "measure_unsupported", "unit must be a declared field or the canonical unit")
        return None

    @staticmethod
    def _flag(text: str, reason: str, suggestion: str) -> UnsupportedConstraint:
        return UnsupportedConstraint(constraint_text=text, reason=reason, suggestion=suggestion)  # type: ignore[arg-type]
