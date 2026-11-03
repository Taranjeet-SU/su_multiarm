"""Grounded community reports: every claim cites supplied passages with verbatim quotes."""

from __future__ import annotations

from typing import Any

from ..core.types import Chunk
from ..models import CommunityReport
from ..profiles.prompts import COMMUNITY_PROMPT
from .base import Community


class ReportFailure(Exception):
    def __init__(self, category: str, community_id: str, message: str):
        self.category, self.community_id = category, community_id
        super().__init__(f"{category} [{community_id}]: {message}")


def disabled_report() -> dict[str, Any]:
    return {"status": "disabled", "claims": []}


def community_report(
    community: Community, chunks: list[Chunk], model: Any, prompt: str | None, evidence_chars: int, budget: Any
) -> dict[str, Any]:
    """Round-robin passages per member document within the character budget, then call the model."""
    remaining = evidence_chars
    by_doc = {doc: [c for c in chunks if c.document_id == doc] for doc in community.document_ids}
    ordered = []
    for i in range(max((len(parts) for parts in by_doc.values()), default=0)):
        ordered.extend(parts[i] for parts in by_doc.values() if i < len(parts))
    included: dict[str, Chunk] = {}
    omitted = []
    for chunk in ordered:
        if len(chunk.text) > remaining:
            omitted.append(chunk.id)
            continue
        remaining -= len(chunk.text)
        included[chunk.id] = chunk
    if not included:
        return {"status": "no_evidence_within_budget", "claims": [], "omitted_chunks": omitted}
    try:
        result = model.call(
            prompt or COMMUNITY_PROMPT,
            {"community_label": community.label, "passages": [c.model_dump() for c in included.values()]},
            CommunityReport,
            budget,
            "community_summary",
        )
    except RuntimeError as exc:
        raise ReportFailure("budget" if "budget" in str(exc) else "provider", community.id, str(exc)) from exc
    except ValueError as exc:
        raise ReportFailure("schema", community.id, str(exc)) from exc
    for claim in result.claims:
        for reference in claim.evidence:
            chunk = included.get(reference.chunk_id)
            if not chunk or chunk.document_id != reference.document_id or not reference.quote.strip() or reference.quote not in chunk.text:
                raise ReportFailure("report_grounding", community.id, "community claim citation is not grounded in supplied passages")
    return {
        **result.model_dump(),
        "status": "partial" if omitted else "complete_input",
        "included_chunks": list(included),
        "omitted_chunks": omitted,
    }
