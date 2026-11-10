"""Incident-overlap scope for graph/community outage investigations; no semantic-search fallback.

Bounded selection (M0, docs/plans/unified_merge.md §9): when more incidents overlap the window than
one selection reads (500), the most recent 500 are considered and the answer says so, with a grouped
incident count per client over the whole window (the backend's exactness), instead of refusing the
request with 503. A backend that cannot express the selection still refuses (fail closed).
Vocabulary terms (``question.vocabulary``) are not required as literal words of the incidents.
Success heartbeats (``category: success``, O6) are excluded from the incident selection."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from ..core.errors import BackendError
from ..core.types import Measure, Page, Predicate, Scope, ScopeSpec, Sort
from ..decision.log_intents import resolve_window
from ..decision.log_question import strip_terms
from ..decision.windows import find_windows
from ..models import Answer, Claim
from ..providers import tokens

_GENERIC = set("what which when where why how was were is are did do does the a an and or in on of for from to with by at me my our us please show list tell explain investigate summarize analyse analyze about happened caused cause root impact affected recent latest last days day outages outage downtime incidents incident issues issue errors error services service clients client tenants tenant customers customer accounts account all any there have has been over during between through until since".split())


def prepare(ops, ctx, requested, scope: Scope, query: str):
    window = resolve_window(requested, ops.log_context(ctx))
    preds = [Predicate(field="kind", op="eq", value="incident"),
             Predicate(field="last_hour", op="range", lower=f"{window.start}T00:00:00+00:00"),
             Predicate(field="first_hour", op="range", upper=f"{window.end}T00:00:00+00:00", inclusive=False)]
    if requested.tenant:
        preds.append(Predicate(field="tenant", op="eq", value=requested.tenant))
    summaries = (ops.log_manifest(ctx).get("logs") or {}).get("collection_scope") == "incident_episode_summaries_v1"
    if summaries and requested.service:
        preds.append(Predicate(field="service", op="in", value=requested.service))
    if summaries:  # success heartbeats (O6) are not incidents
        from .episode_tools import failure_only

        preds.extend(failure_only(ops))
    result = ops.log_execute(ctx, ScopeSpec(predicates=preds, limit=SELECTION_LIMIT, sort=Sort(field="last_hour", descending=True)))
    over_limit = result.total is not None and result.total > len(result.listed)
    if result.unsupported:
        raise BackendError("logs", "incident_scope", "incident selection was incomplete; narrow the window or client")
    notes = question_notes(ops, requested, query)
    grouped = None
    if over_limit:
        # numbers stay in the report (``selection_total``/``selection_considered``): the answer text
        # states no count that no exact scope backs (answer contract)
        notes.append("Bounded selection: more incidents overlap the window than one selection reads, so only the most recent were considered. Name a client, service or shorter window for a complete selection.")
        grouped = _grouped(ops, ctx, preds)
    ids = [row.id for row in result.listed]
    if scope.document_ids is not None:
        ids = sorted(set(ids) & set(scope.document_ids))
    eligible = Scope(predicates=scope.predicates, document_ids=ids, exclude_ids=scope.exclude_ids)
    docs, cursor = [], None
    while ids:
        page = ops.backends[ops.config.storage.catalog].catalog.iter_scope(ctx.generation, eligible, Page(size=500, cursor=cursor))
        docs.extend(page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    words = strip_terms(query, getattr(ops, "question_vocabulary", ()) or ())
    for found in find_windows(words, requested.as_of or ops.log_context(ctx).today):
        words = words.replace(found.text, " ")
    for name in [requested.tenant, *(requested.service or [])]:
        if name:
            words = re.sub(re.escape(name), " ", words, flags=re.I)
    terms = set(tokens(words)) - _GENERIC
    terms = {term for term in terms if not term.isdigit()}
    selected = []
    for doc in docs:
        services = str(doc.metadata.get("services", "")).split(",")
        if requested.service and not set(requested.service).intersection(services):
            continue
        if terms and not terms <= set(tokens(" ".join(doc.fields.values()))):
            continue  # an unknown named subject must not become an unrelated all-client result
        selected.append(doc)
    selected.sort(key=lambda doc: (str(doc.metadata.get("last_hour", "")), doc.id), reverse=True)
    omitted = len(selected) > 20 or over_limit
    selected = selected[:20]
    signatures = sorted({sig for doc in selected for sig in str(doc.metadata.get("signatures", "")).split(",") if sig})
    days = set((ops.log_manifest(ctx).get("logs") or {}).get("days") or [])
    wanted = [(window.start + timedelta(days=n)).isoformat() for n in range((window.end-window.start).days)]
    report = {"kind": "log_investigation_scope", "start": window.start.isoformat(), "end_exclusive": window.end.isoformat(),
              "missing_days": [day for day in wanted if day not in days], "incident_documents": len(selected),
              "limited": omitted or len(signatures) > 100,
              "basis": "Overlapping stored incident intervals; co-firing associations are not confirmed outages or causes. Incident totals describe their full intervals, not just this window.",
              "notes": notes, **({"bounded": True, "selection_total": result.total, "selection_considered": len(result.listed)} if over_limit else {}),
              **({"grouped": grouped} if grouped is not None else {})}
    if (ops.log_manifest(ctx).get("logs") or {}).get("collection_scope") == "incident_episode_summaries_v1":
        report["basis"] = "Overlapping observed detection-rule episodes. Counts describe full intervals, not clipped-window counts; recovery and outage status are unverified."
    return Scope(predicates=scope.predicates, document_ids=[d.id for d in selected], exclude_ids=scope.exclude_ids), " ".join(signatures[:100]) or query, report


SELECTION_LIMIT = 500


def question_notes(ops, requested, query: str) -> list[str]:
    """How the question was read (M0): vocabulary statements and the default view."""
    from ..decision.log_question import PRESENCE, _names_period, ground_terms

    vocabulary = getattr(ops, "question_vocabulary", ()) or ()
    notes = []
    if vocabulary:
        notes.extend(ground_terms(query, vocabulary, list(requested.service or [])).statements)
    window = requested.window or ""
    if requested.start is None and window.startswith("last ") and not _names_period(query, requested.as_of) and PRESENCE.search(query):
        notes.append("No period was named, so the default view is shown (the requested window above).")
    return notes


def _grouped(ops, ctx, preds) -> dict | None:
    """Incidents per client over the whole window (one grouped count; its exactness as returned)."""
    try:
        result = ops.log_execute(ctx, ScopeSpec(predicates=preds, group_by=["tenant"], measure=Measure(kind="count"), order="desc", top_n=20))
    except Exception as exc:  # noqa: BLE001 - reported, never hidden: the bounded note stands
        return {"by": "tenant", "error": type(exc).__name__}
    if result.unsupported:
        return {"by": "tenant", "error": "not_supported_by_backend"}
    rows = [{"tenant": g.key.get("tenant"), "incidents": g.count} for g in result.groups]
    return {"by": "tenant", "exactness": "bounded" if result.ties else result.exactness, "rows": rows, "top_n": 20}


def answer(ops, ctx, evidence, reports):
    """A concise, citation-backed incident table; no generated narrative or full-interval counts."""
    report = next((r for r in reports if r.get("kind") == "log_investigation_scope"), {})
    ids = [row["id"] for row in evidence if row.get("source") != "structured"]
    docs = [doc for doc in ops.documents_in(ctx, ids) if doc.kind == "incident"]
    docs.sort(key=lambda doc: (str(doc.metadata.get("last_hour", "")), doc.id), reverse=True)
    docs = docs[:10]
    scope = f"Requested window: {report.get('start', 'unknown')} through {report.get('end_exclusive', 'unknown')} (end exclusive)."
    caveat = "These are observed co-firing incident signals, not confirmed outages or root causes. First/last timestamps describe the full overlapping incident."
    summaries = (ops.log_manifest(ctx).get("logs") or {}).get("collection_scope") == "incident_episode_summaries_v1"
    if summaries:
        caveat = "These are observed detection-rule signals, not confirmed outages or root causes. Counts cover each full overlapping episode. First/last errors describe an observed error window; recovery duration remains unknown."
    if report.get("missing_days"):
        caveat += " Missing indexed days: " + ", ".join(report["missing_days"]) + "."
    if report.get("notes"):
        scope += "\n\n" + " ".join(report["notes"])
    grouped = report.get("grouped") or {}
    if grouped.get("rows"):
        rows = "\n".join(f"| {row['tenant']} | {row['incidents']} |" for row in grouped["rows"])
        scope += f"\n\nIncidents overlapping the whole window, by client ({grouped.get('exactness', 'bounded')}; most frequent first):\n\n| Client | Incidents |\n| --- | --- |\n" + rows
    if not docs:
        return Answer(answer="No matching stored incident/community evidence was found for this scope. This does not establish that no outage occurred.\n\n" + scope + "\n\n" + caveat, insufficient_evidence=True)
    def cell(value):
        return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    def when(value):
        try:
            return datetime.fromisoformat(str(value)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return str(value)
    def patterns(value):
        return "; ".join(sig.removeprefix("sig.").replace("_", " ").replace(".", ": ") for sig in str(value).split(","))
    lines = ["Observed incident signals from community and graph evidence.", "", scope, "", "| Client | Services | Shared issue patterns | Observed interval (UTC) | Evidence |", "| --- | --- | --- | --- | --- |"]
    if summaries:
        lines = ["Observed failure signals from source summaries.", "", scope, "", "| Client | Service | Issue | First → last observed (UTC) | Matching logs | Recovery duration | Evidence |", "| --- | --- | --- | --- | --- | --- | --- |"]
    claims = []
    for doc in docs:
        m = doc.metadata
        values = [m.get("tenant", "unknown"), m.get("services", "unknown"), m.get("signatures", "unknown"), m.get("first_hour", "unknown"), m.get("last_hour", "unknown")]
        display = [values[0], str(values[1]).replace(",", ", "), patterns(values[2]), f"{when(values[3])} → {when(values[4])}"]
        if summaries:
            display = [values[0], values[1], m.get("issue", doc.title), display[-1], m.get("occurrences", "unknown"), "Unknown"]
        lines.append("| " + " | ".join(cell(v) for v in display) + f" | [{doc.id}] |")
        claims.append(Claim(text=f"Stored incident for {values[0]} associates {values[1]} with {values[2]} during {values[3]} to {values[4]}.", evidence_ids=[doc.id], kind="narrative"))
    lines += ["", caveat, "", ("Showing a limited selection of observed rule episodes and retained evidence. Classification coverage is incomplete."
                               if summaries else "Showing a limited selection of retrieved incident evidence; coverage is incomplete. Graph links describe co-occurrence. Evidence comes from stored incident records and their linked patterns.")]
    return Answer(answer="\n".join(lines), citations=[d.id for d in docs], claims=claims)
