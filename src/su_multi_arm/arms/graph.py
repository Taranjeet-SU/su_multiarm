"""Graph arm: seeds are stored entities whose name tokens all occur in the query and that have a
mention inside the scope; a minimal ``TraversalSpec`` follows ``fact`` edges only (co-occurrence is
never traversed); evidence is the mention passages of reached entities with depth and edge
provenance. Extensions (requested edge kinds, typed predicates from an ontology) arrive in Phase 3."""

from __future__ import annotations

from typing import ClassVar

from ..backends import extras
from ..core.component import Diagnostic, Environment
from ..core.config import GraphArm as GraphArmConfig
from ..core.types import TraversalSpec
from ..providers import tokens
from ..runtime.context import RunContext
from .base import ArmInput, ArmResult, BaseArm


class GraphArm(BaseArm):
    name: ClassVar[str] = "graph"
    config_model: ClassVar[type] = GraphArmConfig
    capability: ClassVar[str] = "graph"
    depends_on_stage: ClassVar[str | None] = "extraction"

    def validate(self, env: Environment, probe: bool = False) -> list[Diagnostic]:
        diagnostics = super().validate(env, probe)
        if self.backend is not None and not isinstance(getattr(self.backend, "graph", None), extras.GraphExtras):
            diagnostics.append(Diagnostic(severity="error", code="graph_extras_missing", message="graph arm needs mention lookup (backends.extras.GraphExtras)", component=self.name))
        return diagnostics

    def _run(self, ctx: RunContext, inputs: ArmInput) -> ArmResult:
        graph = extras.require(self.backend, extras.GraphExtras, "graph", "find_entities")
        scope = self.graph_scope(ctx, inputs.scope)
        seeds = graph.find_entities(ctx.generation, tokens(inputs.query), scope)
        if not seeds:
            return ArmResult(arm=self.name)
        spec = TraversalSpec(
            seed_entity_ids=[s.id for s in seeds],
            hops=self.cfg.hops,
            predicates=list(self.cfg.predicates),
            direction=self.cfg.direction,
            edge_kinds=["fact"],
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
