"""Legacy typed search (``POST /api/search``) on ``Engine.search_page``.

Request: ``{searchString, page, pageSize, maxChars, sortBy, aggregations, facetsOnly, enrich,
record_context, dataset}``. ``aggregations`` entries (``{type, filter[]}`` or ``{key, values[]}``)
become multi-value ``any_of`` filters through the profile's facet keys; ``sortBy`` ``date`` /
``post_time`` sorts on the profile's ``date_field``; ``maxChars`` is the per-request text budget of the
page; ``facetsOnly`` returns totals and facet counts only.

Response numbers keep the package's meaning: ``totalResults`` counts retrieved candidates
(``totalRelation: "candidates"``), and facet counts carry their scope and exactness.

Envelope extras, each grounded or absent:

* ``evidence``: the page's results numbered ``n`` = 1..k, then one structured block per aggregate
  the search computed (``S1``, ``S2``, ... with their own ``n``).
* ``facts``: one fact per aggregate rendering (the typed/structured result the engine computed),
  citing the ``n`` of its structured block; ``exactness`` is the aggregate's own. No other facts.
* ``guidance``: the profile's static usage text plus deterministic notes about this response
  (candidate-bounded totals, filters applied, text cut by ``maxChars``, facet scope).
* ``notice``: ``{kind: "limitation", text}`` when constraints were not applied, the request could
  not be planned, coverage is incomplete or nothing matched; otherwise ``null``. The package has
  no policy refusal on the search path (``kind: "refusal"`` never occurs; reported unsupported).
* ``enrichedSummary``: only with ``enrich: true`` on a dataset whose profile enables it — one
  metered generator call (``runtime/result_notes.py``); ``summary`` stays the retrieved text.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...models import SearchRequest
from ...runtime.listing import SearchPage, SearchPageRequest
from .envelope import aggregate_title, citation_url, content_source, legacy_modes, source_link, sql_block
from .errors import LegacyError
from .profile import DatasetProfile

SUMMARY_CHARS = 300
FACT_CHARS = 4000


class LegacySearchQ(BaseModel):
    """The legacy request body (unknown keys ignored, as the legacy service did)."""

    model_config = ConfigDict(extra="ignore")

    searchString: str = Field(min_length=1, max_length=2000)

    @field_validator("searchString")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("searchString must contain a non-whitespace character")
        return value
    record_context: dict[str, Any] | None = None
    page: int = Field(default=1, ge=1, le=100)
    pageSize: int | None = Field(default=None, ge=1, le=50)
    maxChars: int | None = Field(default=None, ge=2000, le=120000)
    sortBy: Literal["relevance", "_score", "date", "post_time"] = "relevance"
    aggregations: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    enrich: bool = False
    facetsOnly: bool = False
    dataset: str | None = None


def page_request(profile: DatasetProfile, body: LegacySearchQ, search: SearchRequest) -> tuple[SearchPageRequest, dict[str, str]]:
    view = profile.search
    unsupported: dict[str, str] = {}
    by_key = {f.key: f for f in view.facets}
    any_of: dict[str, list[Any]] = {}
    for entry in body.aggregations:
        key = entry.get("type", entry.get("key"))
        values = entry.get("filter", entry.get("values")) or []
        if not isinstance(key, str) or key not in by_key:
            raise LegacyError(422, f"unknown aggregation key {key!r}; valid: {sorted(by_key)}")
        if not isinstance(values, list) or not all(isinstance(v, (str, int, float, bool)) for v in values):
            raise LegacyError(422, f"aggregation {key!r}: filter values must be a list of scalars")
        if values:
            field = by_key[key].field
            any_of[field] = list(dict.fromkeys([*any_of.get(field, []), *values]))
    sort, order = "relevance", "desc"
    if body.sortBy in {"date", "post_time"}:
        if not view.date_field:
            raise LegacyError(422, f"sortBy {body.sortBy!r} is not available for dataset {profile.id!r}")
        sort = view.date_field
    if body.enrich and not view.enrich.enabled:
        unsupported["enrich"] = "model-written result notes are not enabled for this dataset; summary is the retrieved text"
    if body.enrich and body.facetsOnly:
        unsupported["enrich"] = "facetsOnly returns no results to annotate"
    request = SearchPageRequest(
        request=search,
        page=body.page,
        page_size=body.pageSize or view.default_page_size,
        sort=sort,
        order=order,
        any_of=any_of,
        facets=[f.field for f in view.facets],
        facet_scope=view.facet_scope,
        text_chars=body.maxChars,
        include_items=not body.facetsOnly,
    )
    return request, unsupported


def structured_blocks(aggregates: list[dict[str, Any]], first_n: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """``(structured blocks, their evidence items, facts)`` — one block per aggregate the search
    computed, numbered after the result evidence; each fact cites its block."""
    blocks, evidence, facts = [], [], []
    n = first_n
    for index, aggregate in enumerate(aggregates, start=1):
        sql, _note = sql_block([aggregate])
        rendering = str(aggregate.get("rendering") or "").strip()
        if sql is None and not rendering:
            continue
        block_id = f"S{index}"
        exactness = aggregate.get("exactness") or (sql or {}).get("exactness")
        block = {
            "kind": "sql",
            "id": block_id,
            "n": n,
            "query": (sql or {}).get("generated") or "",
            "columns": (sql or {}).get("columns") or [],
            "rows": (sql or {}).get("rows") or [],
            "rowCount": len((sql or {}).get("rows") or []),
            "status": exactness or "ok",
            "aggregateId": aggregate.get("id"),
            "citations": [n],
        }
        blocks.append(block)
        evidence.append({"n": n, "id": block_id, "type": "sql", "title": aggregate_title(aggregate), "url": None, "contentSource": "Structured result", "via": "structured", "text": rendering or f"{block['columns']} {block['rows'][:20]}"})
        if rendering:
            text = rendering if len(rendering) <= FACT_CHARS else rendering[:FACT_CHARS].rstrip() + "…"
            facts.append({"kind": "aggregate", "text": text, "cites": [n], "exactness": exactness, "aggregateId": aggregate.get("id"), **({"truncated": True} if len(rendering) > FACT_CHARS else {})})
        n += 1
    return blocks, evidence, facts


def guidance_text(profile: DatasetProfile, page: SearchPage, body: LegacySearchQ) -> str | None:
    """Static profile guidance plus deterministic notes about this response (no model call)."""
    lines = [profile.search.guidance] if profile.search.guidance else []
    lines.append(f"totalResults ({page.total}) counts the candidates retrieval selected for this query, not every matching record in the corpus.")
    if page.any_of:
        applied = "; ".join(f"{field} in [{', '.join(str(v) for v in values)}]" for field, values in sorted(page.any_of.items()))
        lines.append(f"Filters applied to retrieval: {applied}.")
    if page.text_budget.truncated_ids:
        lines.append(f"{len(page.text_budget.truncated_ids)} result text(s) were cut to fit maxChars={page.text_budget.chars}; textTruncated marks them.")
    if page.facets:
        scope = page.facets[0].basis
        lines.append("Aggregation counts are over the retrieved candidates (bounded)." if scope == "candidates" else "Aggregation counts are over all records matching the filters (no query text).")
    if body.sortBy in {"date", "post_time"} and page.sort_missing:
        lines.append(f"{page.sort_missing} candidate(s) have no date and are listed last.")
    return " ".join(lines) if lines else None


def _listed(items: list[Any]) -> str:
    parts = [f"'{u.constraint_text}' ({u.reason})" for u in items[:5]]
    more = len(items) - len(parts)
    return ", ".join(parts) + (f" and {more} more" if more > 0 else "")


def notice_for(page: SearchPage) -> dict[str, Any] | None:
    """A deterministic limitation notice consistent with the body, or ``None``.

    Request filters (``aggregations``) are always applied as given; a value the data never holds is
    reported as such (the results *are* narrowed by it). Constraints from the query text that no
    exact scope expressed are reported as not applied. "No records matched" is said only when the
    search returned neither records nor a computed aggregate (``facts``)."""
    decision = (page.trace or {}).get("decision") or {}
    if decision.get("outcome") == "abstain":
        return {"kind": "limitation", "text": f"The search could not be planned ({decision.get('failure') or 'no plan'}); no records were searched."}
    filters = tuple(f"{field} eq " for field in page.any_of)
    as_given = [u for u in page.unsupported_constraints if filters and u.constraint_text.startswith(filters)]
    not_applied = [u for u in page.unsupported_constraints if u not in as_given]
    if not_applied:
        return {"kind": "limitation", "text": "Some constraints were not applied: " + _listed(not_applied) + ". Results are not narrowed by them."}
    coverage = (page.trace or {}).get("coverage") or {}
    if coverage.get("incomplete"):
        return {"kind": "limitation", "text": "Some indexing stages are incomplete; results may miss records."}
    empty = page.total == 0 and not page.aggregates
    if as_given:
        lead = "No records matched this search. " if empty else ""
        return {"kind": "limitation", "text": lead + "Filters were applied as given, but some values do not occur in the data: " + _listed(as_given) + "."}
    if empty:
        return {"kind": "limitation", "text": "No records matched this search."}
    return None


def search_envelope(
    profile: DatasetProfile,
    body: LegacySearchQ,
    page: SearchPage,
    *,
    wall_seconds: float,
    planner_model: str | None,
    unsupported: dict[str, str],
    channel: str | None = None,
    notes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    results, evidence = [], []
    enriched = (notes or {}).get("notes") or {}
    for n, item in enumerate(page.items, start=1):
        label = content_source(profile, item.source)
        url = citation_url(profile, item.document_id, item.url, channel)
        arm = "+".join(legacy_modes(item.arms))
        result = {
            "rank": item.rank,
            "n": n,
            "id": item.document_id,
            "type": item.source,
            "title": item.title or item.document_id,
            "summary": item.text[:SUMMARY_CHARS],
            "url": url,
            "viewerUrl": source_link(profile, item.document_id, channel) or url,
            "contentSource": label,
            "score": item.score,
            "arm": arm,
            "metadata": item.metadata,
            "textTruncated": item.text_truncated,
        }
        if item.document_id in enriched:
            result["enrichedSummary"] = enriched[item.document_id]
        results.append(result)
        evidence.append({"n": n, "id": item.document_id, "type": item.source, "title": item.title or item.document_id, "url": url, "contentSource": label, "via": arm, "text": item.text})
    labels = {f.field: (f.key, f.label or f.key) for f in profile.search.facets}
    aggregations = []
    for facet in page.facets:
        key, label = labels.get(facet.field, (facet.field, facet.field))
        aggregations.append(
            {
                "key": key,
                "label": label,
                "values": [{"value": v.value, "displayName": str(v.value), "count": v.count} for v in facet.values],
                "other": facet.other,
                "missing": facet.missing,
                "exactness": facet.exactness,
                "scope": facet.basis,
            }
        )
    first_page = page.page == 1 and not body.facetsOnly
    structured, block_evidence, facts = structured_blocks(page.aggregates, len(evidence) + 1) if first_page else ([], [], [])
    evidence.extend(block_evidence)
    approximations = {"totalResults": "retrieved candidates (bounded by selection), not a corpus-wide match count"}
    if any(not str(a.get("sql") or "") for a in page.aggregates) and structured:
        approximations["structured.query"] = "a typed aggregate has no SQL text; query describes it"
    unsupported = {**unsupported, "notice.refusal": "no policy refusal on the package search path; notice is a limitation or null"}
    if not facts:
        unsupported["facts"] = "facts are the search's own aggregate results; this search computed none"
    trace = page.trace or {}
    decision = trace.get("decision") or {}
    steps = trace.get("steps") or []
    out = {
        "searchString": body.searchString,
        "page": page.page,
        "pageSize": page.page_size,
        "totalResults": page.total,
        "totalRelation": page.total_relation,
        "pages": page.pages,
        "sortBy": body.sortBy,
        "guidance": guidance_text(profile, page, body),
        "notice": notice_for(page),
        "facts": facts,
        "evidence": evidence,
        "results": results,
        "structured": structured,
        "aggregations": aggregations,
        "budget": {"maxChars": page.text_budget.chars, "usedChars": page.text_budget.used, "truncatedIds": page.text_budget.truncated_ids},
        "trace": {
            "latencyMs": int(round(wall_seconds * 1000)),
            "modes": legacy_modes([s.get("arm") for s in steps if s.get("arm")]),
            "plannerModel": planner_model if decision.get("kind") in {"llm", "cascade"} else decision.get("kind"),
            "rerankMs": int(round(float(((trace.get("selection") or {}).get("rerank_detail") or {}).get("seconds") or 0) * 1000)),
            "sortMissing": page.sort_missing,
            "enrich": notes is not None,
            "enrichModel": (notes or {}).get("model"),
        },
        "compat": {
            "adapter": "su_multi_arm.compat.legacy_qa",
            "generation": page.generation,
            "unsupported": dict(sorted(unsupported.items())),
            "approximations": approximations,
            "unsupported_constraints": [u.model_dump(mode="json") for u in page.unsupported_constraints],
        },
    }
    search_calls = trace.get("model_calls") or []
    enrich_calls = (notes or {}).get("model_calls") or []
    out["compat"]["usage"] = {"model_calls": len(search_calls) + len(enrich_calls), "usd": round(sum(float(c.get("usd") or 0) for c in [*search_calls, *enrich_calls]), 6)}
    if notes is not None:
        calls = enrich_calls
        out["compat"]["enrich"] = {
            "label": "model-written relevance notes (enrichedSummary); not evidence",
            "notes": len(enriched),
            "dropped_ids": list(notes.get("dropped_ids") or []),
            "usage": {"model_calls": len(calls), "usd": round(sum(float(c.get("usd") or 0) for c in calls), 6)},
        }
    return out
