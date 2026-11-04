"""``ScopeCompiler`` (plan §3.7.2): turns a draft scope into an executable ``ScopeSpec`` or refuses.

A draft is a ``ScopeSpec``-shaped proposal from the rules grammar or an LLM planner: its values are
unverified. Compilation resolves every predicate through ``ValueResolver``, prepends the caller's
request filters (immutable; a draft predicate that contradicts one is an ``invalid_step``), checks
``group_by``/``sort`` flags and the measure rules, and returns a ``CompiledScope``. Any unsupported
predicate makes the operation non-executable (``abstain``): a count over a scope with a dropped
predicate would be a different question. A ``measure_unsupported`` is **never** substituted by
``count``.

Query-compiler operations (2.0.0a3): calendar ``windows`` become half-open ``date_interval``
predicates on one time basis (``ValueResolver.window``, anchored); ``periods`` become
``ScopeSpec.periods`` plus one pushed-down span predicate; ``numerator`` predicates resolve like
scope predicates; a temporal ``group_by`` needs a ``granularity``; ranking (``order``/``top_n``/
``nth``), ``with_share``, ``text`` and ``limit`` pass through and are checked by ``ScopeSpec``
(an incoherent combination is ``measure_unsupported``, never repaired).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, ValidationError

from ..core.types import Granularity, JsonScalar, Measure, MetadataSchema, Period, Predicate, ScopeSpec, Sort, TextMatch, UnsupportedConstraint
from ..models import StrictModel
from .base import DecisionFailure
from .values import ValueResolver, resolve_request_filters

NUMERIC = {"integer", "number"}
TEMPORAL = {"datetime", "date"}
NUMERIC_MEASURES = {"sum", "avg", "min", "max"}
TEMPORAL_MEASURES = {"first_seen", "last_seen", "bucket"}


class WindowDraft(StrictModel):
    """A calendar expression on a temporal field ("this month", "last 7 days", "October",
    "2026-10"), resolved by ``ValueResolver.window`` against the request anchor. ``field`` None:
    the draft's ``time_basis``, else the schema's only ``time_basis`` field."""

    field: str | None = None
    expression: str = Field(min_length=1, max_length=128)
    label: str | None = Field(default=None, max_length=128)


class ScopeDraft(StrictModel):
    """Unverified proposal; the same shape as ``ScopeSpec`` with a free-text ``reason``, plus
    unresolved calendar ``windows`` (predicates) and ``periods`` (a comparison)."""

    predicates: list[Predicate] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list, max_length=2)
    top_n: int | None = Field(default=None, ge=1, le=1000)
    sort: Sort | None = None
    measure: Measure | None = None
    time_basis: str | None = None
    population: str = "canonical_record"
    reason: str | None = None
    order: Literal["desc", "asc"] | None = None
    nth: int | None = Field(default=None, ge=1, le=1000)
    granularity: Granularity | None = None
    windows: list[WindowDraft] = Field(default_factory=list, max_length=4)
    periods: list[WindowDraft] = Field(default_factory=list, max_length=12)
    with_share: bool = False
    numerator: list[Predicate] = Field(default_factory=list)
    text: TextMatch | None = None
    limit: int | None = Field(default=None, ge=1, le=10000)


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
        granularity = getattr(draft, "granularity", None)
        for field in draft.group_by:
            if field not in self.schema.groupable():
                unsupported.append(self._flag(f"group by {field}", "field_not_filterable", f"declare {field!r} as groupable"))
            elif self._temporal(field) and granularity is None:
                unsupported.append(self._flag(f"group by {field}", "measure_unsupported", "a datetime/date group needs a calendar granularity (day/month/year)"))
        if granularity is not None and not any(self._temporal(f) for f in draft.group_by):
            unsupported.append(self._flag(f"by {granularity}", "measure_unsupported", "a calendar granularity needs a datetime/date group_by field"))
        sort, order = draft.sort, getattr(draft, "order", None)
        grouped_by_measure = sort is not None and bool(draft.group_by) and sort.field not in draft.group_by and self._sorts_by_measure(sort, draft.measure)
        if sort is not None and (grouped_by_measure or (sort.field not in self.schema.sortable() and self._sorts_by_measure(sort, draft.measure))):
            # "sort by count" (or by the aggregate's measure) on a group_count / top_n: the group
            # ranking (``order``), the same operation the grammar's "most common <field>" compiles to.
            wanted = "desc" if sort.descending else "asc"
            if not draft.group_by:
                unsupported.append(
                    self._flag(f"sort by {sort.field}", "measure_unsupported", "sorting by a count or measure ranks groups: it needs group_by (records have no count)")
                )
            elif order is not None and order != wanted:
                unsupported.append(self._flag(f"sort by {sort.field}", "measure_unsupported", f"sort direction {wanted} contradicts order {order}"))
            else:
                sort, order = None, wanted
        elif sort is not None and sort.field not in self.schema.sortable():
            unsupported.append(self._flag(f"sort by {sort.field}", "field_not_filterable", f"declare {sort.field!r} as sortable"))
        time_basis = draft.time_basis
        if time_basis is not None:
            spec = self.schema.field(time_basis)
            if spec is None or spec.type not in TEMPORAL or spec.time_basis is None:
                unsupported.append(self._flag(f"time basis {time_basis}", "measure_unsupported", "declare a datetime/date field with time_basis"))
                time_basis = None
        windows = list(getattr(draft, "windows", []))
        period_drafts = list(draft.periods) if isinstance(draft, ScopeDraft) else []
        periods: list[Period] = [] if isinstance(draft, ScopeDraft) else list(draft.periods)
        if windows or period_drafts:
            fields = {w.field for w in [*windows, *period_drafts] if w.field} | ({time_basis} if time_basis else set())
            if len(fields) > 1:
                unsupported.append(self._flag("time basis", "measure_unsupported", f"windows name several time fields {sorted(fields)}; one basis per scope"))
            basis = next(iter(fields)) if len(fields) == 1 else self._only_time_basis()
            if basis is None:
                unsupported.append(self._flag("time window", "measure_unsupported", "name the datetime/date field the window applies to (several or none declare time_basis)"))
            else:
                time_basis = time_basis or basis
                for window in windows:
                    resolution = self.resolver.window(basis, window.expression)
                    if resolution.unsupported is not None:
                        unsupported.append(resolution.unsupported)
                    else:
                        predicates.append(resolution.predicate)  # type: ignore[arg-type]
                resolved: list[Predicate] = []
                for draft_period in period_drafts:
                    resolution = self.resolver.window(basis, draft_period.expression)
                    if resolution.unsupported is not None:
                        unsupported.append(resolution.unsupported)
                        continue
                    window_predicate = resolution.predicate
                    resolved.append(window_predicate)  # type: ignore[arg-type]
                    periods.append(Period(label=draft_period.label or draft_period.expression, lower=str(window_predicate.lower), upper=str(window_predicate.upper)))  # type: ignore[union-attr]
                if period_drafts and len(period_drafts) < 2:
                    unsupported.append(self._flag("period comparison", "measure_unsupported", "a comparison needs at least two periods"))
                if resolved and len(resolved) == len(period_drafts):
                    # Push down the span of the periods; each period is re-applied to the matched rows.
                    lower = min(resolved, key=lambda p: self._instant(p.lower)).lower
                    upper = max(resolved, key=lambda p: self._instant(p.upper)).upper
                    predicates.append(Predicate(field=basis, op="date_interval", lower=lower, upper=upper, granularity="day"))
        numerator: list[Predicate] = []
        for predicate in getattr(draft, "numerator", []):
            resolution = self.resolver.resolve(predicate)
            if resolution.unsupported is not None:
                unsupported.append(resolution.unsupported)
            else:
                numerator.append(resolution.predicate)  # type: ignore[arg-type]
        measure = draft.measure
        if measure is not None:
            problem = self._check_measure(measure, time_basis)
            if problem is not None:
                unsupported.append(problem)
        if unsupported:
            return CompiledScope(spec=None, unsupported=unsupported, zero_match_fields=resolved_filters.zero_match_fields)
        if measure is not None and measure.kind in TEMPORAL_MEASURES and measure.field is None:
            measure = measure.model_copy(update={"field": time_basis})
        try:
            spec = ScopeSpec(
                predicates=predicates,
                group_by=list(draft.group_by),
                top_n=draft.top_n,
                sort=sort,
                measure=measure,
                time_basis=time_basis,
                population=draft.population,  # type: ignore[arg-type]
                order=order,
                nth=getattr(draft, "nth", None),
                granularity=granularity,
                periods=periods,
                with_share=getattr(draft, "with_share", False),
                numerator=numerator,
                text=getattr(draft, "text", None),
                limit=getattr(draft, "limit", None),
            )
        except ValidationError as error:
            problem = "; ".join(e["msg"] for e in error.errors())
            return CompiledScope(spec=None, unsupported=[self._flag("scope", "measure_unsupported", f"incoherent scope: {problem}")], zero_match_fields=resolved_filters.zero_match_fields)
        return CompiledScope(spec=spec, zero_match_fields=resolved_filters.zero_match_fields)

    def _sorts_by_measure(self, sort: Sort, measure: Measure | None) -> bool:
        """``sort.field`` names the group's count or the aggregate measure rather than a declared
        field: ``count`` (count and count_distinct measures, or none), the measure kind
        (``sum``, ``avg`` ...), the measure's numeric field, ``measure`` or ``value``. A declared
        sortable field of that name wins (the caller checks ``sortable()`` first)."""
        name = sort.field.casefold()
        kind = measure.kind if measure is not None else "count"
        names = {"measure", "value", kind}
        if kind in {"count", "count_distinct"}:
            names |= {"count", "record_count", "group_count", "records"}
        if measure is not None and measure.field and kind in NUMERIC_MEASURES:
            names.add(measure.field.casefold())
        return name in names

    def _temporal(self, field: str) -> bool:
        spec = self.schema.field(field)
        return spec is not None and spec.type in TEMPORAL

    def _only_time_basis(self) -> str | None:
        candidates = [f.name for f in self.schema.fields if f.type in TEMPORAL and f.time_basis and f.filterable]
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _instant(bound: JsonScalar) -> str:
        from ..backends._scope import instant

        moment = instant(bound)
        return "" if moment is None else moment.isoformat()

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
