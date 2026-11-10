"""``TypedQueryCompiler`` (typed-query plan §5.3, MVP M5): a ``TypedQueryDraft`` -> ``CompiledTypedQuery``.

Every failure is a typed ``CompileError`` (never a silent repair): names must exist in the
``ProjectionCatalog``; ``join`` must be a declared relation touching the grain; predicate values are
resolved by the ``ValueResolver`` (unknown values refused, never fuzzy-matched; the key ``id`` is a
literal); windows go through ``ValueResolver.window``; request filters apply to every table alias
that has the column (none: ``filter_not_applicable``), and a draft predicate contradicting one
raises ``TypedStepInvalid`` (``invalid_step``); the ACL predicates are applied to every alias
(``inject_acl``; the test-only switch that proves the per-tenant file layer). Measures follow
``ScopeCompiler._check_measure``.

Fan-out rules (Looker-style): relations are declared ``many_to_one`` with the parent key ``id``;
joining the "many" (child) grain to its parent is safe; a "one" (parent) grain with its children
joined is ``distinct_guarded``: counts become ``count(distinct grain.id)``, lists are distinct grain
rows of grain columns only, and ``sum``/``avg`` are ``fanout_unsafe``; ``sum``/``avg`` over the
parent's column from a child grain repeat the parent value and are ``fanout_unsafe`` too. Outer
joins are not offered (an inner join: the population is the rows that have the related row).

The fingerprint is ``sha256(IR, catalog fingerprint, generation, acl_key)``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..core.config_projection import KEY
from ..core.types import JsonScalar, Measure, Predicate, ScopePredicates
from ..core.types_typed import ColumnRef, CompiledTypedQuery, CompileError, TypedPredicate, TypedQueryDraft
from ..projection.catalog import CatalogColumn, ProjectionCatalog
from ..runtime.acl import acl_key
from .values import ValueResolver

NUMERIC = {"integer", "number"}
TEMPORAL = {"datetime", "date"}
UNSUPPORTED_MEASURES = {"share", "bucket"}


class TypedStepInvalid(ValueError):
    """A draft contradicts an immutable request filter (the decision's ``invalid_step``)."""


def fingerprint(query: TypedQueryDraft, catalog: ProjectionCatalog, acl: ScopePredicates) -> str:
    data = [query.model_dump(mode="json"), catalog.fingerprint, catalog.generation, acl_key(list(acl))]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class TypedQueryCompiler:
    def __init__(self, catalog: ProjectionCatalog, resolver: ValueResolver, *, row_limit: int = 200, max_groups: int = 500):
        self.catalog = catalog
        self.resolver = resolver
        self.row_limit = row_limit
        self.max_groups = max_groups

    # --- helpers ---------------------------------------------------------------------------------

    def _column(self, table: str, column: str) -> CatalogColumn | None:
        entry = self.catalog.tables.get(table)
        return None if entry is None else entry.columns.get(column)

    def _measure_column(self, name: str | None, grain: str, aliases: dict[str, str]) -> tuple[str, str] | None:
        """``<table>.<column>`` of the joined table, else a grain column."""
        if not name:
            return None
        table, _, column = name.rpartition(".")
        table = table or grain
        if table not in aliases or self._column(table, column) is None:
            return None
        return table, column

    # --- compile ---------------------------------------------------------------------------------

    def compile(self, draft: TypedQueryDraft, request_filters: dict[str, JsonScalar] | None, acl: ScopePredicates, *, inject_acl: bool = True) -> CompiledTypedQuery:
        filters = dict(request_filters or {})
        errors: list[CompileError] = []
        grain = draft.grain
        acl = list(acl)

        def done(query: TypedQueryDraft, applied: list[TypedPredicate], fanout: str) -> CompiledTypedQuery:
            return CompiledTypedQuery(
                query=query,
                acl_key=acl_key(acl),
                acl_applied=inject_acl,
                generation=self.catalog.generation,
                catalog_fingerprint=self.catalog.fingerprint,
                filters_applied=applied,
                fanout=fanout,  # type: ignore[arg-type]
                errors=errors,
                fingerprint=fingerprint(query, self.catalog, acl),
            )

        if grain not in self.catalog.tables:
            errors.append(CompileError(path="grain", reason="column_not_projectable", allowed=sorted(self.catalog.tables)))
            return done(draft, [], "none")
        aliases = {grain: "g"}
        fanout = "none"
        joined = None
        if draft.join is not None:
            relation = self.catalog.relations.get(draft.join)
            if relation is None or grain not in {relation.child, relation.parent}:
                touching = sorted(n for n, r in self.catalog.relations.items() if grain in {r.child, r.parent})
                errors.append(CompileError(path="join", reason="join_not_declared", allowed=touching))
                return done(draft, [], "none")
            joined = relation.parent if grain == relation.child else relation.child
            aliases[joined] = "j"
            if grain == relation.parent:
                fanout = "distinct_guarded"

        where: list[TypedPredicate] = []
        for index, item in enumerate(draft.where):
            path = f"where[{index}]"
            predicate = item.predicate
            column = self._column(item.table, predicate.field) if item.table in aliases else None
            if item.table not in aliases:
                errors.append(CompileError(path=f"{path}.table", reason="filter_not_applicable", allowed=sorted(aliases)))
                continue
            if column is None:
                errors.append(CompileError(path=f"{path}.predicate.field", reason="column_not_projectable", allowed=sorted(self.catalog.tables[item.table].columns)))
                continue
            if not column.filterable:
                errors.append(CompileError(path=f"{path}.predicate.field", reason="field_not_filterable"))
                continue
            if predicate.field in filters:
                if predicate.op != "eq" or predicate.negate or predicate.value != filters[predicate.field]:
                    raise TypedStepInvalid(f"predicate on {item.table}.{predicate.field} overrides a request filter")
                continue  # identical to the caller's filter, applied below
            resolution = self.resolver.resolve(predicate)
            if resolution.unsupported is not None or resolution.predicate is None:
                reason = resolution.unsupported.reason if resolution.unsupported is not None else "value_not_observed"
                errors.append(CompileError(path=path, reason=reason, detail=resolution.unsupported.suggestion if resolution.unsupported else None))
                continue
            if resolution.predicate.field != predicate.field:
                # the question-grounded cross-field move (2026-11-09) is offered to structured scopes
                # and the SQL route only; the arm recompiles an IR draft without the question
                errors.append(CompileError(path=path, reason="value_not_observed", detail=f"{predicate.field}: {predicate.value!r} is not an observed value"))
                continue
            where.append(TypedPredicate(table=item.table, predicate=resolution.predicate))
        for index, window in enumerate(draft.windows):
            path = f"windows[{index}]"
            column = self._column(window.table, window.column) if window.table in aliases else None
            if column is None or column.type not in TEMPORAL:
                errors.append(CompileError(path=path, reason="filter_not_applicable", detail="a window needs a datetime/date column of the grain or joined table"))
                continue
            resolution = self.resolver.window(window.column, window.expression)
            if resolution.predicate is None:
                errors.append(CompileError(path=path, reason=resolution.unsupported.reason if resolution.unsupported else "unparseable_bound"))
                continue
            where.append(TypedPredicate(table=window.table, predicate=resolution.predicate))

        applied: list[TypedPredicate] = []
        for field, value in sorted(filters.items()):
            tables = [t for t in aliases if self._column(t, field) is not None]
            if not tables:
                errors.append(CompileError(path=f"request_filters.{field}", reason="filter_not_applicable", allowed=sorted(aliases)))
                continue
            resolution = self.resolver.resolve(Predicate(field=field, op="eq", value=value), from_request=True)
            if resolution.predicate is None:
                errors.append(CompileError(path=f"request_filters.{field}", reason=resolution.unsupported.reason if resolution.unsupported else "value_not_observed"))
                continue
            applied.extend(TypedPredicate(table=t, predicate=resolution.predicate) for t in tables)
        if inject_acl:
            for predicate in acl:
                tables = [t for t in aliases if self._column(t, predicate.field) is not None]
                if len(tables) != len(aliases):
                    # fail closed: an ACL predicate must constrain every table the query reads
                    errors.append(CompileError(path="acl", reason="filter_not_applicable", detail="an access constraint names a column some table lacks"))
                    continue
                applied.extend(TypedPredicate(table=t, predicate=predicate) for t in tables)

        measure = draft.measure
        if measure is not None:
            problem = self._check_measure(measure, grain, aliases, joined, fanout)
            if problem is not None:
                errors.append(problem)
        for index, ref in enumerate(draft.group_by):
            column = self._column(ref.table, ref.column) if ref.table in aliases else None
            if column is None:
                errors.append(CompileError(path=f"group_by[{index}]", reason="column_not_projectable", allowed=sorted(aliases)))
            elif not column.groupable and ref.column != KEY:
                errors.append(CompileError(path=f"group_by[{index}]", reason="field_not_filterable", detail="declare the field groupable"))
            elif column.type in TEMPORAL:
                errors.append(CompileError(path=f"group_by[{index}]", reason="measure_unsupported", detail="a temporal group needs a calendar granularity, which the MVP IR does not offer"))
        if draft.top_n is not None and draft.top_n > self.max_groups:
            errors.append(CompileError(path="top_n", reason="result_truncated", allowed=[str(self.max_groups)]))
        select: list[ColumnRef] = list(draft.select)
        if measure is None and not draft.group_by:
            if not select:
                select = [ColumnRef(table=grain, column=c) for c in self.catalog.tables[grain].columns if c != KEY and self.catalog.tables[grain].columns[c].projectable]
            for index, ref in enumerate(select):
                column = self._column(ref.table, ref.column) if ref.table in aliases else None
                if column is None or not column.projectable:
                    errors.append(CompileError(path=f"select[{index}]", reason="column_not_projectable", allowed=sorted(aliases)))
                elif fanout == "distinct_guarded" and ref.table != grain:
                    errors.append(CompileError(path=f"select[{index}]", reason="fanout_unsafe", detail="a parent grain lists distinct parent rows; child columns would repeat them"))
            if draft.limit is not None and draft.limit > self.row_limit:
                errors.append(CompileError(path="limit", reason="result_truncated", allowed=[str(self.row_limit)]))
        query = draft.model_copy(update={"where": where, "windows": [], "select": select})
        return done(query, applied, fanout)

    def _check_measure(self, measure: Measure, grain: str, aliases: dict[str, str], joined: str | None, fanout: str) -> CompileError | None:
        kind = measure.kind
        if kind in UNSUPPORTED_MEASURES:
            return CompileError(path="measure.kind", reason="measure_unsupported", detail=f"{kind} is not offered by the typed MVP")
        if kind == "count":
            return None
        name = measure.unit if kind == "count_distinct" else measure.field
        if kind == "count_distinct" and name is None:
            return None  # distinct grain keys
        ref = self._measure_column(name, grain, aliases)
        if ref is None:
            return CompileError(path="measure.field", reason="column_not_projectable", allowed=sorted(aliases))
        column = self._column(*ref)
        assert column is not None
        if kind in {"sum", "avg", "min", "max"}:
            if column.type not in NUMERIC:
                return CompileError(path="measure.field", reason="measure_unsupported", detail=f"{kind} needs an integer/number column; never replaced by a count")
            if kind in {"sum", "avg"}:
                if not column.additive:
                    return CompileError(path="measure.field", reason="measure_unsupported", detail="the column is declared additive: false")
                if fanout == "distinct_guarded" or (joined is not None and ref[0] == joined):
                    return CompileError(path="measure.field", reason="fanout_unsafe", detail="the join repeats rows of this column")
        if kind in {"first_seen", "last_seen"} and (column.type not in TEMPORAL or column.time_basis is None):
            return CompileError(path="measure.field", reason="measure_unsupported", detail=f"{kind} needs a declared time_basis column")
        return None


def operation(query: TypedQueryDraft) -> str:
    if query.group_by:
        return "rank" if query.order is not None or query.top_n is not None else "group_count"
    if query.measure is None:
        return "lookup" if query.limit == 1 else "list"
    return query.measure.kind


def describe(query: TypedQueryDraft) -> dict[str, Any]:
    """Shape of a typed query for traces (names only, never values)."""
    return {
        "grain": query.grain,
        "join": query.join,
        "operation": operation(query),
        "where": sorted({f"{p.table}.{p.predicate.field}" for p in query.where}),
        "group_by": [f"{c.table}.{c.column}" for c in query.group_by],
        "text": query.text is not None,
    }
