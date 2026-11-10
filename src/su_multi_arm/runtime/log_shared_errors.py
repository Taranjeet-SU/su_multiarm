"""Scoped shared-error evidence with deterministic, cited tabular presentation."""
from __future__ import annotations

import re
from datetime import timedelta

from ..core.errors import BackendError
from ..core.types import Page, Predicate, Scope, ScopeSpec, Sort
from ..decision.log_intents import resolve_window
from ..decision.log_question import strip_terms
from ..decision.windows import find_windows
from ..models import Answer, Claim
from ..providers import tokens
from .log_investigation import _GENERIC


def prepare(ops, ctx, requested, scope: Scope, query: str):
    window = resolve_window(requested, ops.log_context(ctx))
    predicates = [Predicate(field="kind", op="eq", value="shared_error"), window.predicate("date")]
    if requested.tenant:
        predicates.append(Predicate(field="tenant", op="eq", value=requested.tenant))
    if requested.service:
        predicates.append(Predicate(field="service", op="in", value=requested.service))
    result = ops.log_execute(ctx, ScopeSpec(predicates=predicates, limit=500, sort=Sort(field="date", descending=True)))
    if result.unsupported or (result.total is not None and result.total > len(result.listed)):
        raise BackendError("logs", "shared_error_scope", "shared-error selection was incomplete; narrow the window or client")
    ids = {row.id for row in result.listed}
    if scope.document_ids is not None:
        ids &= set(scope.document_ids)
    eligible = Scope(predicates=scope.predicates, document_ids=sorted(ids), exclude_ids=scope.exclude_ids)
    docs, cursor = [], None
    while ids:
        page = ops.backends[ops.config.storage.catalog].catalog.iter_scope(ctx.generation, eligible, Page(size=500, cursor=cursor))
        docs.extend(page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    words = strip_terms(query, getattr(ops, "question_vocabulary", ()) or ())  # M0: vocabulary words are not literal terms
    for found in find_windows(words, requested.as_of or ops.log_context(ctx).today):
        words = words.replace(found.text, " ")
    for name in [requested.tenant, *(requested.service or [])]:
        if name:
            words = re.sub(re.escape(name), " ", words, flags=re.I)
    terms = set(tokens(words)) - _GENERIC - set("shared common cross across patterns pattern communities community failures failure".split())
    terms = {term for term in terms if not term.isdigit()}
    docs = [doc for doc in docs if not terms or terms <= set(tokens(" ".join(doc.fields.values())))]
    docs.sort(key=lambda doc: (str(doc.metadata.get("date", "")), doc.id), reverse=True)
    limited = len(docs) > 100
    docs = docs[:100]
    signatures = sorted({str(doc.metadata["signature_id"]) for doc in docs})
    days = set((ops.log_manifest(ctx).get("logs") or {}).get("days") or [])
    wanted = [(window.start + timedelta(days=n)).isoformat() for n in range((window.end-window.start).days)]
    report = {"kind": "log_shared_error_scope", "start": window.start.isoformat(), "end_exclusive": window.end.isoformat(),
              "missing_days": [day for day in wanted if day not in days], "selected_documents": len(docs), "limited": limited,
              "basis": "Same classified error signature across clients on a partition day; not a shared incident or cause."}
    return Scope(predicates=scope.predicates, document_ids=[d.id for d in docs], exclude_ids=scope.exclude_ids), " ".join(signatures) or query, report


def answer(ops, ctx, evidence, reports):
    report = next((r for r in reports if r.get("kind") == "log_shared_error_scope"), {})
    docs = [doc for doc in ops.documents_in(ctx, [row["id"] for row in evidence if row.get("source") != "structured"]) if doc.kind == "shared_error"]
    docs.sort(key=lambda doc: (-int(doc.metadata["lines"]), doc.id))
    docs = docs[:20]
    scope = f"Requested window: {report.get('start', 'unknown')} through {report.get('end_exclusive', 'unknown')} (end exclusive)."
    caveat = "Counts describe classified ERROR/FATAL events in the displayed client/service records. Approximate counts come from sampled logs. A shared pattern does not establish a shared outage or cause. Only members allowed by the requested scope are shown; other clients' counts are not included."
    if (ops.log_manifest(ctx).get("logs") or {}).get("collection_scope") == "incident_episode_summaries_v1":
        caveat = "Counts describe exclusive detection-rule matches in source snapshots for the displayed client/service records, not source ERROR/FATAL severity counts. A shared rule does not establish a shared outage or cause. Only members allowed by the requested scope are shown."
    if report.get("missing_days"):
        caveat += " Missing indexed days: " + ", ".join(report["missing_days"]) + "."
    if any(doc.metadata.get("pattern_reviewed") is False for doc in docs):
        caveat += " Some pattern labels are provisional: those patterns have not been human-reviewed. See their supporting expression in the evidence."
    if not docs:
        return Answer(answer="No matching stored shared-error community evidence was found. This does not establish that no shared errors occurred.\n\n" + scope + "\n\n" + caveat, insufficient_evidence=True)
    def cell(value):
        return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    lines = ["Shared-error community evidence", "", scope, "", "| Error pattern | Client | Service | Day | Severity | Error count | Evidence |", "| --- | --- | --- | --- | --- | --- | --- |"]
    claims = []
    for doc in docs:
        m = doc.metadata
        count = f"{m['lines']} ({m['row_exactness']})"
        label = m.get("error_type") or str(m["signature_id"]).removeprefix("sig.").replace("_", " ").replace(".", ": ")
        values = [label, m["tenant"], m["service"], m["date"], m["level"], count]
        lines.append("| " + " | ".join(cell(v) for v in values) + f" | [{doc.id}] |")
        claims.append(Claim(text=f"Stored shared-error evidence associates {m['signature_id']} with client {m['tenant']} and service {m['service']} on {m['date']}.", evidence_ids=[doc.id], kind="narrative"))
    lines += ["", caveat, "", "This is a limited selection of retrieved supporting records, not a complete ranking or total across clients. Unclassified logs are outside these communities."]
    return Answer(answer="\n".join(lines), citations=[doc.id for doc in docs], claims=claims)
