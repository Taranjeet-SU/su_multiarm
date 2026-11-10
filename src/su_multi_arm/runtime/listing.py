"""Paged search listings (plan §3.15 "page / page_size / sort / facets"): ``Engine.search_page``.

A ``SearchPageRequest`` wraps an ordinary ``SearchRequest`` with listing controls. The search runs
exactly as ``Engine.search`` does; the listing is then computed over the **retrieved candidates** —
the selection's bounded hit list, never the corpus (principle 7) — and every number says so:

* ``total`` is the number of candidates, ``total_relation: "candidates"``: the corpus may hold more
  matching records than retrieval selected (it is a lower bound of the query's matches).
* ``sort``: ``relevance`` (the selection order) or a metadata field declared ``sortable``; records
  without a usable value go last in both orders and are counted in ``sort_missing``; ties keep the
  relevance order.
* ``any_of``: multi-value filters, OR within a field and AND across fields. They are pushed down to
  every arm, value listing and aggregate as ``in`` predicates of the run's access scope (the same
  path as ACL constraints), so they narrow retrieval itself instead of post-filtering a page. Each
  value is validated like ``SearchRequest.filters`` (QA-02): a coerced value is applied typed, an
  invalid or unobserved one is applied as given and reported in ``unsupported_constraints``.
* ``facets``: terms counts of ``groupable`` fields. ``facet_scope: candidates`` counts over the
  candidates (``exactness: bounded`` — a search-result facet); ``facet_scope: filtered_corpus``
  runs a ``group_by`` aggregate over the records matching the filters and access scope, without the
  query text (exactness from the records backend).
* ``text_chars``: a per-request budget for the hit texts on the page, spent in page order; a cut
  text is marked ``text_truncated`` and listed in ``text_budget.truncated_ids``.

Nothing here changes ``Engine.search``; the listing is an additive, opt-in call.
"""

from __future__ import annotations

import dataclasses
import math
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, field_validator

from ..core.types import JsonScalar, Predicate, ScopeSpec
from ..models import Hit, SearchRequest, SearchResult, StrictModel, UnsupportedConstraint

if TYPE_CHECKING:  # pragma: no cover
    from .context import RunContext

RELEVANCE = "relevance"


class SearchPageRequest(StrictModel):
    request: SearchRequest
    page: int = Field(default=1, ge=1, le=10_000)
    page_size: int = Field(default=10, ge=1, le=500)  # also bounded by serving.http.page_size_max
    sort: str = Field(default=RELEVANCE, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    order: Literal["desc", "asc"] = "desc"
    any_of: dict[str, list[JsonScalar]] = Field(default_factory=dict)
    facets: list[str] = Field(default_factory=list, max_length=20)
    facet_scope: Literal["candidates", "filtered_corpus"] = "candidates"
    facet_limit: int = Field(default=20, ge=1, le=1000)
    text_chars: int | None = Field(default=None, ge=200, le=2_000_000)
    include_items: bool = True  # false: total and facets only (a "filter options" call)

    @field_validator("any_of")
    @classmethod
    def bounded_values(cls, value):
        for field, values in value.items():
            if not values:
                raise ValueError(f"any_of[{field!r}] needs at least one value")
            if len(values) > 100:
                raise ValueError(f"any_of[{field!r}] accepts at most 100 values")
        return value

    @field_validator("facets")
    @classmethod
    def unique_facets(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("facets must be unique")
        return value


class PageHit(Hit):
    rank: int = Field(ge=1)  # position in the sorted candidate list (1-based, across pages)
    metadata: dict[str, JsonScalar] = Field(default_factory=dict)
    text_truncated: bool = False


class FacetValue(StrictModel):
    value: JsonScalar
    count: int = Field(ge=0)


class Facet(StrictModel):
    field: str
    values: list[FacetValue]
    other: int = Field(default=0, ge=0)  # records whose value is beyond facet_limit
    missing: int = Field(default=0, ge=0)  # records without a value (candidates scope only)
    exactness: Literal["exact", "approximate", "bounded"]
    basis: Literal["candidates", "filtered_corpus"]


class TextBudget(StrictModel):
    chars: int | None = None
    used: int = 0
    truncated_ids: list[str] = Field(default_factory=list)


class SearchPage(StrictModel):
    query: str
    generation: str
    page: int
    page_size: int
    pages: int
    total: int
    total_relation: Literal["candidates"] = "candidates"
    sort: str
    order: Literal["desc", "asc"]
    sort_missing: int = 0
    items: list[PageHit] = Field(default_factory=list)
    facets: list[Facet] = Field(default_factory=list)
    any_of: dict[str, list[JsonScalar]] = Field(default_factory=dict)  # as applied (after coercion)
    text_budget: TextBudget = Field(default_factory=TextBudget)
    aggregates: list[dict[str, Any]] = Field(default_factory=list)
    unsupported_constraints: list[UnsupportedConstraint] = Field(default_factory=list)
    trace: dict[str, Any] = Field(default_factory=dict)


class RequestError(ValueError):
    """A listing control the configuration cannot serve (mapped to ``RequestRejected`` by the engine)."""


def check(config: Any, request: SearchPageRequest) -> None:
    """Static checks against the configuration: page size, sortable/groupable/filterable fields."""
    from ..backends._scope import RECORD_KEYS

    schema = config.metadata_schema
    page_max = config.serving.http.page_size_max
    if request.page_size > page_max:
        raise RequestError(f"page_size exceeds serving.http.page_size_max ({page_max})")
    if request.sort != RELEVANCE:
        spec = schema.field(request.sort)
        if spec is None or not spec.sortable:
            raise RequestError(f"sort field {request.sort!r} is not declared sortable in metadata_schema")
    for name in request.facets:
        spec = schema.field(name)
        if spec is None or not spec.groupable:
            raise RequestError(f"facet field {name!r} is not declared groupable in metadata_schema")
    for name in request.any_of:
        spec = schema.field(name)
        if spec is None and name not in RECORD_KEYS:
            raise RequestError(f"any_of field {name!r} is not declared in metadata_schema")
        if spec is not None and not spec.filterable:
            raise RequestError(f"any_of field {name!r} is not filterable")


class _Narrowed:
    """The run's access policy plus the request's ``any_of`` predicates (pushed down everywhere the
    access constraints are)."""

    def __init__(self, base: Any, predicates: list[Predicate]):
        self.base, self.predicates = base, list(predicates)

    def constraints(self, principal: Any) -> list[Predicate]:
        return [*self.base.constraints(principal), *self.predicates]


def narrowed(ctx: RunContext, schema: Any, records: Any, request: SearchPageRequest) -> tuple[RunContext, dict[str, list[JsonScalar]], list[UnsupportedConstraint]]:
    """The context whose constraints carry ``any_of``; values validated like request filters."""
    if not request.any_of:
        return ctx, {}, []
    from ..decision.values import ValueResolver, check_request_filters

    resolver = ValueResolver(schema, records, ctx.generation, ctx.constraints())
    applied: dict[str, list[JsonScalar]] = {}
    unsupported: list[UnsupportedConstraint] = []
    for field, values in request.any_of.items():
        kept: list[JsonScalar] = []
        for value in values:
            result = check_request_filters(resolver, {field: value})
            kept.append(result.applied[field])
            unsupported.extend(result.unsupported)
        applied[field] = list(dict.fromkeys(kept))
    predicates = [Predicate(field=f, op="in", value=v) if len(v) > 1 else Predicate(field=f, op="eq", value=v[0]) for f, v in applied.items()]
    ctx.trace.event("listing_any_of", fields=sorted(applied), values=sum(len(v) for v in applied.values()))
    return dataclasses.replace(ctx, acl=_Narrowed(ctx.acl, predicates)), applied, unsupported


def _sort_value(spec: Any, value: Any) -> Any:
    """A comparable key for a sortable field's stored value, or ``None`` when unusable."""
    if value is None or (isinstance(value, bool) and spec.type != "boolean"):
        return None
    kind = spec.type
    if kind in {"date", "datetime"}:
        if isinstance(value, (int, float)):
            return float(value)
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.combine(date.fromisoformat(str(value).strip()[:10]), datetime.min.time())
            except ValueError:
                return None
        if parsed.tzinfo is None:  # naive values are read as UTC so naive and aware values compare
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    if kind in {"integer", "number"}:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return str(value).casefold()


def _ordered(hits: list[Hit], metadata: dict[str, dict[str, Any]], spec: Any, request: SearchPageRequest) -> tuple[list[tuple[Hit, int]], int]:
    """(hit, relevance position) in the requested order and the number without a sort value."""
    indexed = list(enumerate(hits))
    if spec is None:
        ordered = indexed if request.order == "desc" else list(reversed(indexed))
        return [(h, i) for i, h in ordered], 0
    keyed = [(i, h, _sort_value(spec, metadata.get(h.document_id, {}).get(spec.name))) for i, h in indexed]
    present = [k for k in keyed if k[2] is not None]
    missing = [k for k in keyed if k[2] is None]
    present.sort(key=lambda k: k[0])  # ties keep relevance order (stable sorts below)
    present.sort(key=lambda k: k[2], reverse=request.order == "desc")
    return [(h, i) for i, h, _ in [*present, *missing]], len(missing)


def _candidate_facets(field: str, hits: list[Hit], metadata: dict[str, dict[str, Any]], limit: int) -> Facet:
    counts: dict[Any, int] = {}
    missing = 0
    for hit in hits:
        value = metadata.get(hit.document_id, {}).get(field)
        if value is None or value == "":
            missing += 1
            continue
        counts[value] = counts.get(value, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
    kept = ranked[:limit]
    return Facet(
        field=field,
        values=[FacetValue(value=v, count=c) for v, c in kept],
        other=sum(c for _, c in ranked[limit:]),
        missing=missing,
        exactness="bounded",
        basis="candidates",
    )


def _corpus_facet(engine: Any, ctx: RunContext, field: str, filters: dict[str, Any], limit: int) -> tuple[Facet, list[UnsupportedConstraint]]:
    result = engine.aggregate_in(ctx, ScopeSpec(group_by=[field]), dict(filters))
    rows = sorted(result.groups, key=lambda g: (-g.count, str(g.key.get(field))))
    kept = [r for r in rows if g_value(r, field) is not None][:limit]
    other = sum(r.count for r in rows if g_value(r, field) is not None) - sum(r.count for r in kept)
    facet = Facet(
        field=field,
        values=[FacetValue(value=g_value(r, field), count=r.count) for r in kept],
        other=max(0, other),
        missing=sum(r.count for r in rows if g_value(r, field) is None),
        exactness=result.exactness,
        basis="filtered_corpus",
    )
    return facet, list(result.unsupported)


def g_value(row: Any, field: str) -> Any:
    value = row.key.get(field)
    return None if value == "" else value


def build_page(engine: Any, ctx: RunContext, request: SearchPageRequest, result: SearchResult, applied: dict[str, list[JsonScalar]], unsupported: list[UnsupportedConstraint]) -> SearchPage:
    """The page over ``result.hits`` (see the module docstring for every number's meaning)."""
    schema = engine.config.metadata_schema
    sort_spec = schema.field(request.sort) if request.sort != RELEVANCE else None
    metadata: dict[str, dict[str, Any]] = {}
    if result.hits:  # bounded by the hit count; the catalog is read once for metadata on every item
        ids = [h.document_id for h in result.hits]
        metadata = {d.id: dict(d.metadata) for d in engine.documents_in(ctx, ids)}
    ordered, sort_missing = _ordered(result.hits, metadata, sort_spec, request)
    total = len(ordered)
    start = (request.page - 1) * request.page_size
    window = ordered[start : start + request.page_size] if request.include_items else []
    budget = TextBudget(chars=request.text_chars)
    remaining = request.text_chars
    items: list[PageHit] = []
    for position, (hit, _relevance) in enumerate(window, start=start + 1):
        text, cut = hit.text, False
        if remaining is not None:
            if len(text) > remaining:
                text, cut = text[: max(0, remaining)], True
                budget.truncated_ids.append(hit.document_id)
            remaining -= len(text)
            budget.used += len(text)
        items.append(PageHit(**{**hit.model_dump(), "text": text}, rank=position, metadata=metadata.get(hit.document_id, {}), text_truncated=cut))
    facets: list[Facet] = []
    facet_unsupported: list[UnsupportedConstraint] = []
    for field in request.facets:
        if request.facet_scope == "candidates":
            facets.append(_candidate_facets(field, result.hits, metadata, request.facet_limit))
        else:
            facet, extra = _corpus_facet(engine, ctx, field, dict(request.request.filters), request.facet_limit)
            facets.append(facet)
            facet_unsupported.extend(extra)
    unsupported_all = list(result.unsupported_constraints) + list(unsupported)
    for item in facet_unsupported:
        if item not in unsupported_all:
            unsupported_all.append(item)
    trace = dict(result.trace)
    trace["listing"] = {
        "candidates": total,
        "sort": request.sort,
        "sort_missing": sort_missing,
        "facet_scope": request.facet_scope,
        "any_of_fields": sorted(applied),
        "text_truncated": len(budget.truncated_ids),
    }
    return SearchPage(
        query=result.query,
        generation=result.generation,
        page=request.page,
        page_size=request.page_size,
        pages=math.ceil(total / request.page_size) if total else 0,
        total=total,
        sort=request.sort,
        order=request.order,
        sort_missing=sort_missing,
        items=items,
        facets=facets,
        any_of=applied,
        text_budget=budget,
        aggregates=list(result.aggregates),
        unsupported_constraints=unsupported_all,
        trace=trace,
    )
