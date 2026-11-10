"""Model review of candidate links (unified merge plan §6.9, §6.13; increment M3, lane LD).

The queue is every **served** rule link (tier B/C) whose status is ``candidate`` and that has no
review of its current evidence and no human decision. Each call reserves its worst case on the
**shared** review ledger (``review_ledger.py``: one cumulative ceiling with identity review,
recorded with its approval reference at first use) and on the run cap before the request is sent;
the run stops at the first refusal, provider error or invalid reply (no retry, no repair).

The reviewer sees the link (pair, tier, builder, evidence) and an excerpt of both records taken
from the same pinned documents the links were built from. Text fields are cut at
``FIELD_CHARS`` characters and the cut is stated in the payload (``truncated``). A record that is
not among the supplied documents is not reviewed (``document_unavailable``), never guessed. A reply
citing anything outside the supplied fields is not recorded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field

from ...core.errors import BudgetExhausted
from ...models import StrictModel
from ..review_ledger import ReviewLedger, ReviewMeter, RunCap, worst_case_usd
from .signature_mentions import FAMILY_PREFIX, SignatureCatalog

REVIEW_SCOPE = "links"
FIELD_CHARS = 1200
METADATA_KEYS = ("account", "tenant", "customer", "company_id", "service", "services", "product", "component", "status", "priority",
                 "created_at", "resolved_at", "updated_at", "signature_id", "category", "first_hour", "last_hour", "occurrences", "date")
REVIEW_SYSTEM = (
    "You review one proposed link between two records of a support and operations collection (for example a support ticket "
    "and a log incident episode, a ticket and an error-signature family, or a ticket and a documentation page). The link was "
    "proposed by a deterministic rule whose evidence is supplied. Judge only from the supplied records and evidence whether "
    "the two records are about the same problem. Reply 'agree' when they plausibly describe the same problem, 'refutes' when "
    "the records contradict it (different problem, different customer, unrelated time), and 'uncertain' otherwise. Cite only "
    "the field names you used, from the allowed citations. Do not use outside knowledge and do not invent evidence."
)


class LinkReviewReply(StrictModel):
    verdict: Literal["agree", "refutes", "uncertain"]
    rationale: str = Field(min_length=1, max_length=1000)
    cited_fields: list[str] = Field(default_factory=list, max_length=20)


def review_queue_states(state: dict[str, Any]) -> list[Any]:
    """Served ``candidate`` rule links without a review of their current evidence or a decision."""
    return [s for s in sorted(state["links"].values(), key=lambda x: x.link_id)
            if s.served and s.tier != "A" and s.status == "candidate" and s.review is None and s.decision is None]


def excerpt(record_id: str, documents: dict[str, Any], catalog: SignatureCatalog) -> dict[str, Any] | None:
    if record_id.startswith(FAMILY_PREFIX):
        rule = record_id[len(FAMILY_PREFIX):]
        if rule not in catalog:
            return None
        return {"id": record_id, "kind": "issue_family", "rule": rule, "title": catalog.titles[rule]}
    d = documents.get(record_id)
    if d is None:
        return None
    fields: dict[str, str] = {}
    truncated: list[str] = []
    for name, value in sorted((d.fields or {}).items()):
        if not isinstance(value, str) or not value:
            continue
        if len(value) > FIELD_CHARS:
            truncated.append(name)
            value = value[:FIELD_CHARS]
        fields[name] = value
    metadata = {k: d.metadata[k] for k in METADATA_KEYS if k in (d.metadata or {})}
    out = {"id": d.id, "kind": d.kind, "source": d.source, "title": d.title, "fields": fields, "metadata": metadata}
    if truncated:
        out["truncated"] = {"fields": truncated, "max_chars": FIELD_CHARS}
    return out


def _citations(prefix: str, ex: dict[str, Any]) -> set[str]:
    names = {f"{prefix}.{k}" for k in ex if k not in ("fields", "metadata", "truncated")}
    names |= {f"{prefix}.fields.{k}" for k in ex.get("fields", {})}
    names |= {f"{prefix}.metadata.{k}" for k in ex.get("metadata", {})}
    return names


@dataclass(frozen=True)
class LinkReviewItem:
    link_id: str
    evidence_sha: str
    pair: str
    tier: str
    builder: str
    evidence: dict[str, Any]
    subject: dict[str, Any]
    object: dict[str, Any]

    def payload(self) -> dict[str, Any]:
        allowed = {f"link.evidence.{k}" for k in self.evidence} | {"link.pair", "link.builder", "link.tier"}
        allowed |= _citations("subject", self.subject) | _citations("object", self.object)
        return {"link": {"pair": self.pair, "tier": self.tier, "builder": self.builder, "evidence": self.evidence},
                "subject": self.subject, "object": self.object, "allowed_citations": sorted(allowed)}

    def prompt_chars(self) -> int:
        return len(REVIEW_SYSTEM) + len(json.dumps(self.payload(), ensure_ascii=False)) + len(json.dumps(LinkReviewReply.model_json_schema()))


def review_queue(store: Any, documents: dict[str, Any], catalog: SignatureCatalog | None = None) -> tuple[list[LinkReviewItem], list[dict[str, str]]]:
    """(reviewable items, unavailable links) for the store's queue over ``documents``."""
    catalog = catalog or SignatureCatalog.from_documents(documents.values())
    items: list[LinkReviewItem] = []
    unavailable: list[dict[str, str]] = []
    for s in review_queue_states(store.state()):
        subject, obj = excerpt(s.subject_id, documents, catalog), excerpt(s.object_id, documents, catalog)
        if subject is None or obj is None:
            unavailable.append({"link_id": s.link_id, "reason": "document_unavailable", "missing": s.subject_id if subject is None else s.object_id})
            continue
        items.append(LinkReviewItem(s.link_id, s.evidence_sha, s.pair, s.tier, s.builder, s.evidence, subject, obj))
    return items, unavailable


def review_plan(store: Any, documents: dict[str, Any], *, price_table: Any, pricing_key: str | None, max_tokens: int, limit: int | None = None) -> dict[str, Any]:
    """``links review --dry-run``: the queue and its worst-case cost; no model call."""
    queue, unavailable = review_queue(store, documents)
    chosen = queue if limit is None else queue[:limit]
    priced = price_table is not None and pricing_key is not None and pricing_key in price_table.prices
    items = [{"link_id": i.link_id, "pair": i.pair, "builder": i.builder, "prompt_chars": i.prompt_chars(),
              "worst_case_usd": round(worst_case_usd(price_table, pricing_key, i.prompt_chars(), max_tokens), 6) if priced else None} for i in chosen]
    return {"queue": len(queue), "planned": len(items), "unavailable": unavailable, "items": items,
            "worst_case_total_usd": round(sum(i["worst_case_usd"] for i in items), 6) if priced else None, "priced": priced}


def run_review(store: Any, documents: dict[str, Any], provider: Any, ledger: ReviewLedger, price_table: Any, *, pricing_key: str, max_tokens: int,
               max_usd: float, limit: int | None = None, actor: str | None = None) -> dict[str, Any]:
    """Review the queue with ``provider`` (a ``ChatProvider``); see the module docstring."""
    from ...providers.base import CallContext, ProviderError, SchemaViolation

    cap = RunCap(max_usd)
    model = getattr(getattr(provider, "role", None), "model", None) or getattr(provider, "name", "reviewer")
    results: list[dict[str, Any]] = []
    stopped: dict[str, Any] | None = None
    queue, unavailable = review_queue(store, documents)
    for item in queue if limit is None else queue[:limit]:
        meter = ReviewMeter(ledger, price_table, pricing_key, scope=REVIEW_SCOPE, subject=item.link_id, model=model,
                            prompt_chars=item.prompt_chars(), max_tokens=max_tokens, run_cap=cap)
        payload = item.payload()
        try:
            reply, _usage = provider.complete_json(REVIEW_SYSTEM, payload, LinkReviewReply, meter, CallContext("link_review", {"subject": item.link_id}))
        except BudgetExhausted as exc:
            stopped = {"link_id": item.link_id, "reason": "budget_exhausted", "kind": exc.kind, "requested_usd": exc.requested, "remaining_usd": exc.remaining}
            break
        except (ProviderError, SchemaViolation) as exc:
            stopped = {"link_id": item.link_id, "reason": "provider_failed", "error": type(exc).__name__, "usd": meter.committed_usd}
            break
        invalid = sorted(set(reply.cited_fields) - set(payload["allowed_citations"]))
        if invalid:
            results.append({"link_id": item.link_id, "outcome": "invalid_citation", "cited": invalid, "usd": meter.committed_usd})
            stopped = {"link_id": item.link_id, "reason": "invalid_citation"}
            break
        recorded = store.record_review(item.link_id, item.evidence_sha, model=str(model), verdict=reply.verdict, rationale=reply.rationale,
                                       cited_fields=reply.cited_fields, usd=meter.committed_usd, actor=actor)
        results.append({"link_id": item.link_id, "outcome": reply.verdict, "review": recorded["review"], "status": recorded["status"], "usd": meter.committed_usd})
    return {"reviewed": sum(1 for r in results if r["outcome"] in ("agree", "refutes", "uncertain")), "results": results, "stopped": stopped,
            "unavailable": unavailable, "run_usd": round(sum(r["usd"] for r in results) + (stopped or {}).get("usd", 0.0), 6), "ledger": ledger.usage()}
