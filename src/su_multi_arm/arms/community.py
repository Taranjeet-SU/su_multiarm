"""Community arm: membership evidence (a shared topic, never a causal fact) plus grounded report
claims whose cited records are all eligible under the scope."""

from __future__ import annotations

from typing import ClassVar

from ..backends import extras
from ..core.config import CommunityArm as CommunityArmConfig
from ..core.types import Page, Scope
from ..providers import tokens
from ..runtime.context import RunContext
from .base import ArmInput, ArmResult, BaseArm


class CommunityArm(BaseArm):
    name: ClassVar[str] = "community"
    config_model: ClassVar[type] = CommunityArmConfig
    capability: ClassVar[str] = "graph"
    depends_on_stage: ClassVar[str | None] = "extraction"

    def _eligible(self, ctx: RunContext, scope: Scope, document_ids: list[str]) -> set[str]:
        """Members of the community that the scope admits (contract-only, bounded by community size)."""
        if not document_ids:
            return set()
        narrowed = Scope(predicates=scope.predicates, document_ids=sorted(set(document_ids) - set(scope.exclude_ids)), exclude_ids=scope.exclude_ids)
        if scope.document_ids is not None:
            narrowed.document_ids = sorted(set(narrowed.document_ids) & set(scope.document_ids))
        if not narrowed.document_ids:
            return set()
        found: set[str] = set()
        cursor = None
        while True:
            page = self.catalog.iter_scope(ctx.generation, narrowed, Page(cursor=cursor, size=500))
            found.update(d.id for d in page.items)
            if page.next_cursor is None:
                return found
            cursor = page.next_cursor

    def _run(self, ctx: RunContext, inputs: ArmInput) -> ArmResult:
        graph = extras.require(self.backend, extras.GraphExtras, "graph", "communities")
        query_terms = set(tokens(inputs.query))
        seed_ids = {s.id for s in graph.find_entities(ctx.generation, sorted(query_terms), self.graph_scope(ctx, inputs.scope))}
        scored, reports = [], []
        for community in graph.communities(ctx.generation):
            # Relevance first (entity overlap with the query, or report terms), catalog reads only
            # for candidate communities (principle 2.1.7).
            report = community.report
            if not seed_ids.intersection(community.entity_ids) and not (
                query_terms & set(tokens(" ".join(c["text"] for c in report.get("claims", []))))
            ):
                continue
            docs = self._eligible(ctx, inputs.scope, community.document_ids)
            if not docs:
                continue
            # A report claim is usable only when every cited record is eligible.
            claims = [c for c in report.get("claims", []) if all(ref["document_id"] in docs for ref in c["evidence"])]
            overlap = len(query_terms & set(tokens(" ".join(c["text"] for c in claims))))
            if not seed_ids.intersection(community.entity_ids) and not overlap:
                continue
            reports.append({"community_id": community.id, "label": community.label, "status": community.status(), "claims": claims})
            member_scope = Scope(predicates=[], document_ids=sorted(docs))
            for mention in graph.mentions(ctx.generation, community.entity_ids, member_scope, inputs.candidates):
                scored.append(
                    (
                        mention.span.chunk_id,
                        1.0 + overlap / max(1, len(query_terms)),
                        {
                            "community_id": community.id,
                            "label": community.label,
                            "member_count": len(docs),
                            "quote": mention.span.text,
                            "kind": "membership_not_causality",
                            "report_status": community.status(),
                            "report_claims": claims,
                        },
                    )
                )
        scored.sort(key=lambda row: (-row[1], row[0]))
        return ArmResult(arm=self.name, hits=self.collapse(ctx, scored), reports=reports)
