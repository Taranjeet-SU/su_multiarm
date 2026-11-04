"""Executor of declared traversal templates (``core/config_graph.py``) over the graph contract.

Everything here is expressed with ``GraphStore.traverse`` (one ``TraversalSpec`` per hop group) and
the ``GraphExtras`` lookups (``find_entities``, ``mentions``), so a template means the same thing on
every backend that passes the graph conformance kit; ``tests/conformance/test_traversal_templates.py``
runs one fixture through the memory fake, sqlite, kuzu (and, external, neo4j) and asserts identical
results. No query text is produced: a template only fills typed specs.
"""

from __future__ import annotations

from typing import Any, Sequence

from pydantic import Field

from ..core.config_graph import GraphIntent, TraversalHop, TraversalTemplate
from ..core.errors import BackendError
from ..core.types import Entity, GenerationId, Path, Scope, TraversalSpec
from ..models import StrictModel
from ..providers import tokens

SPEC_LIMIT = 10000  # TraversalSpec.limit upper bound
SEED_CANDIDATES = 50  # entities one seed string may resolve to


class TemplateResult(StrictModel):
    template: str
    kind: str
    seeds: list[Entity] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)  # seed strings that matched no eligible entity
    paths: list[Path] = Field(default_factory=list)
    mentions: list[Entity] = Field(default_factory=list)
    truncated: bool = False  # the template's ``limit`` cut the result (reported, never hidden)

    def summary(self) -> dict[str, Any]:
        return {
            "template": self.template,
            "kind": self.kind,
            "seeds": [s.id for s in self.seeds],
            "unresolved": self.unresolved,
            "paths": len(self.paths),
            "mentions": len(self.mentions),
            "truncated": self.truncated,
        }


def resolve_seed(graph: Any, gen: GenerationId, template: TraversalTemplate, seed: str, scope: Scope | None) -> list[Entity]:
    """Entities one seed string names: a stored entity id resolves to itself; otherwise
    ``find_entities`` candidates filtered by ``seed_match`` (``exact``: equal name tokens; ``tokens``:
    every name token is in the seed). Then ``seed_types``. Ordered by entity id."""
    by_id = graph.mentions(gen, [seed], scope, 1)
    if by_id:
        found = by_id
    else:
        words = tokens(seed)
        candidates = graph.find_entities(gen, words, scope) if words else []
        if template.seed_match == "exact":
            found = [e for e in candidates if tokens(e.name) == words]
        else:
            found = list(candidates)
    if template.seed_types:
        found = [e for e in found if e.type in template.seed_types]
    unique: dict[str, Entity] = {}
    for entity in sorted(found, key=lambda e: e.id):
        unique.setdefault(entity.id, entity)
    return list(unique.values())[:SEED_CANDIDATES]


def _spec(seeds: Sequence[str], hop: TraversalHop, hops: int, limit: int) -> TraversalSpec:
    return TraversalSpec(seed_entity_ids=list(seeds), hops=hops, predicates=list(hop.predicates), direction=hop.direction, edge_kinds=list(hop.edge_kinds), limit=limit)


def _types(graph: Any, gen: GenerationId, ids: Sequence[str], scope: Scope | None) -> dict[str, str]:
    """Entity type per id (one eligible mention each), for ``target_types``. A facet that knows types
    without a read (an attached store encodes the table in the id) may offer ``entity_types``; the
    ids passed here are path ends, already reached through eligible edges."""
    known = getattr(graph, "entity_types", None)
    if callable(known):
        return known(list(dict.fromkeys(ids)))
    out: dict[str, str] = {}
    for entity_id in dict.fromkeys(ids):
        found = graph.mentions(gen, [entity_id], scope, 1)
        if found:
            out[entity_id] = found[0].type
    return out


def _chain(graph: Any, gen: GenerationId, seeds: list[str], steps: list[TraversalHop], scope: Scope | None) -> list[Path]:
    """Ordered hops, each from the previous hop's end nodes; paths never revisit a node."""
    frontier = [Path(nodes=[s]) for s in seeds]
    for step in steps:
        ends = list(dict.fromkeys(p.nodes[-1] for p in frontier))
        if not ends:
            return []
        hop_paths = graph.traverse(gen, _spec(ends, step, 1, SPEC_LIMIT), scope)
        by_start: dict[str, list[Path]] = {}
        for path in hop_paths:
            by_start.setdefault(path.nodes[0], []).append(path)
        extended = []
        for path in frontier:
            for hop in by_start.get(path.nodes[-1], []):
                if hop.nodes[1] in path.nodes:
                    continue
                extended.append(Path(nodes=[*path.nodes, hop.nodes[1]], edges=[*path.edges, *hop.edges]))
        frontier = extended
    return frontier


def run_template(graph: Any, gen: GenerationId, name: str, template: TraversalTemplate, intent: GraphIntent, scope: Scope | None) -> TemplateResult:
    result = TemplateResult(template=name, kind=template.kind)
    resolved: list[list[Entity]] = []
    for seed in intent.seeds:
        entities = resolve_seed(graph, gen, template, seed, scope)
        if not entities:
            result.unresolved.append(seed)
        resolved.append(entities)
    result.seeds = list({e.id: e for group in resolved for e in group}.values())
    if template.kind == "mentioned_in":
        ids = [e.id for e in result.seeds]
        mentions = graph.mentions(gen, ids, scope, template.limit + 1) if ids else []
        result.truncated = len(mentions) > template.limit
        result.mentions = mentions[: template.limit]
        return result
    if template.kind == "connect":
        if len(intent.seeds) != 2:
            raise BackendError("traversal", name, "a connect template takes exactly two seeds (from, to)")
        sources, targets = [e.id for e in resolved[0]], {e.id for e in resolved[1]}
        paths = graph.traverse(gen, _spec(sources, template.hop, template.hops, SPEC_LIMIT), scope) if sources and targets else []
        paths = [p for p in paths if p.nodes[-1] in targets]
    elif template.kind == "neighbors":
        ids = [e.id for e in result.seeds]
        paths = graph.traverse(gen, _spec(ids, template.hop, template.hops, SPEC_LIMIT), scope) if ids else []
    else:  # chain
        ids = [e.id for e in result.seeds]
        paths = _chain(graph, gen, ids, template.steps, scope) if ids else []
        if template.steps:
            paths = [p for p in paths if len(p.edges) == len(template.steps)]
    if template.target_types and paths:
        types = _types(graph, gen, [p.nodes[-1] for p in paths], scope)
        paths = [p for p in paths if types.get(p.nodes[-1]) in template.target_types]
    result.truncated = len(paths) > template.limit
    result.paths = paths[: template.limit]
    return result
