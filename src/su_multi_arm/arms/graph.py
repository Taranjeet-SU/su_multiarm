"""Graph arm. Two modes, both on the frozen graph contract:

* **free text** (no graph intent): seeds are stored entities whose name tokens all occur in the
  query and that have a mention inside the scope; a minimal ``TraversalSpec`` follows ``fact`` edges
  only by default (``edge_kinds``; co-occurrence is never traversed unless configured); evidence is the mention passages of reached entities with
  depth and edge provenance;
* **typed intent** (``step.graph = {template, seeds}``): a declared traversal template
  (``pipeline.arms.graph.templates``) is run through ``arms/traversal.py``; evidence is the documents
  the path edges (or the mentions) are anchored in, each hit carrying the edge quotes with predicate,
  edge kind, support and provenance. An unknown template name is refused, never guessed.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from ..backends import extras
from ..core.component import Diagnostic, Environment
from ..core.config import GraphArm as GraphArmConfig
from ..core.types import Document, TraversalSpec
from ..models import Hit
from ..providers import tokens
from ..runtime.context import RunContext
from .base import ArmInput, ArmResult, BaseArm
from .traversal import TemplateResult, run_template

MAX_QUOTES_PER_DOCUMENT = 4


class GraphArm(BaseArm):
    name: ClassVar[str] = "graph"
    config_model: ClassVar[type] = GraphArmConfig
    capability: ClassVar[str] = "graph"
    depends_on_stage: ClassVar[str | None] = "extraction"

    def validate(self, env: Environment, probe: bool = False) -> list[Diagnostic]:
        diagnostics = super().validate(env, probe)
        if self.backend is not None and not isinstance(getattr(self.backend, "graph", None), extras.GraphExtras):
            diagnostics.append(Diagnostic(severity="error", code="graph_extras_missing", message="graph arm needs mention lookup (backends.extras.GraphExtras)", component=self.name))
        ontology_of = getattr(getattr(self.backend, "graph", None), "ontology", None)
        if callable(ontology_of) and self.cfg.templates:
            ontology = ontology_of()
            for name, template in self.cfg.templates.items():
                for problem in template.problems(ontology):
                    diagnostics.append(Diagnostic(severity="error", code="template_ontology", message=f"template {name!r}: {problem}", path=f"templates.{name}", component=self.name))
        return diagnostics

    def _run(self, ctx: RunContext, inputs: ArmInput) -> ArmResult:
        graph = extras.require(self.backend, extras.GraphExtras, "graph", "find_entities")
        scope = self.graph_scope(ctx, inputs.scope)
        if inputs.graph is not None:
            return self._run_template(ctx, inputs, graph, scope)
        seeds = graph.find_entities(ctx.generation, tokens(inputs.query), scope)
        if not seeds:
            return ArmResult(arm=self.name)
        spec = TraversalSpec(
            seed_entity_ids=[s.id for s in seeds],
            hops=self.cfg.hops,
            predicates=list(self.cfg.predicates),
            direction=self.cfg.direction,
            edge_kinds=list(getattr(self.cfg, "edge_kinds", None) or ["fact"]),
            limit=inputs.candidates,
        )
        paths = graph.traverse(ctx.generation, spec, scope) if self.cfg.hops else []
        depths = {s.id: 0 for s in seeds}
        for path in paths:
            for depth, node in enumerate(path.nodes):
                depths[node] = min(depths.get(node, depth), depth)
        mentions = graph.mentions(ctx.generation, list(depths), scope, inputs.candidates)
        scored = [
            (
                m.span.chunk_id,
                1 / (1 + depths[m.id]),
                {"entity": m.name, "quote": m.span.text, "span": [m.span.start, m.span.end], "match_level": m.span.match_level, "depth": depths[m.id]},
            )
            for m in mentions
        ]
        scored.sort(key=lambda row: (-row[1], row[0]))
        return ArmResult(arm=self.name, hits=self.collapse(ctx, scored), paths=paths)

    # --- typed intents -----------------------------------------------------------------------

    def _run_template(self, ctx: RunContext, inputs: ArmInput, graph: Any, scope: Any) -> ArmResult:
        intent = inputs.graph
        template = self.cfg.templates.get(intent.template)
        if template is None:
            raise ValueError(f"graph intent names undeclared template {intent.template!r}; declared: {sorted(self.cfg.templates)}")
        result = run_template(graph, ctx.generation, intent.template, template, intent, scope)
        hits, outside = self._evidence_hits(ctx, result)
        diagnostics = [Diagnostic(severity="info", code="graph_template", message=json.dumps(result.summary(), sort_keys=True), component=self.name)]
        if result.unresolved:
            diagnostics.append(Diagnostic(severity="info", code="graph_seed_unresolved", message=f"seeds without an eligible entity: {result.unresolved}", component=self.name))
        if result.truncated:
            diagnostics.append(Diagnostic(severity="info", code="graph_template_truncated", message=f"template {intent.template!r} stopped at its limit {template.limit}", component=self.name))
        if outside:
            diagnostics.append(Diagnostic(severity="info", code="graph_evidence_outside_catalog", message=f"{outside} evidence document(s) are not in the catalog generation", component=self.name))
        return ArmResult(arm=self.name, hits=hits[: inputs.candidates], paths=result.paths, diagnostics=diagnostics)

    def _evidence_hits(self, ctx: RunContext, result: TemplateResult) -> tuple[list[Hit], int]:
        """Document hits for the documents the template's evidence is anchored in, ordered by first
        appearance (path order, then edge order); catalog reads bounded by those documents."""
        quotes: dict[str, list[dict[str, Any]]] = {}
        for rank, path in enumerate(result.paths):
            for depth, edge in enumerate(path.edges, start=1):
                quotes.setdefault(edge.span.document_id, []).append(
                    {
                        "kind": "graph_path",
                        "template": result.template,
                        "path_rank": rank,
                        "depth": depth,
                        "subject": edge.subject_id,
                        "predicate": edge.predicate,
                        "object": edge.object_id,
                        "edge_kind": edge.edge_kind,
                        "support": edge.support,
                        "quote": edge.span.text,
                        "chunk_id": edge.span.chunk_id,
                        "field": edge.span.field,
                        "span": [edge.span.start, edge.span.end],
                        "match_level": edge.span.match_level,
                        "extractor": edge.provenance.extractor,
                        "confidence": edge.provenance.confidence,
                    }
                )
        for mention in result.mentions:
            quotes.setdefault(mention.span.document_id, []).append(
                {"kind": "mention", "template": result.template, "entity": mention.id, "quote": mention.span.text, "chunk_id": mention.span.chunk_id, "field": mention.span.field, "match_level": mention.span.match_level}
            )
        if not quotes:
            return [], 0
        documents: dict[str, Document] = {d.id: d for d in self.catalog.get(ctx.generation, list(quotes))}
        hits = []
        for rank, (doc_id, rows) in enumerate(quotes.items()):
            doc = documents.get(doc_id)
            if doc is None:
                continue
            rows = rows[:MAX_QUOTES_PER_DOCUMENT]
            hits.append(
                Hit(
                    document_id=doc.id,
                    title=doc.title or doc.id,
                    source=doc.source,
                    url=doc.url,
                    text="\n".join(f"{r.get('subject', r.get('entity'))} -{r.get('predicate', 'mentioned_in')}-> {r.get('object', doc.id)}: {r['quote']}" for r in rows),
                    chunk_ids=[],
                    score=1.0 / (1 + rank),
                    arms=[self.name],
                    provenance=rows,
                )
            )
        return hits, len(quotes) - len(hits)
