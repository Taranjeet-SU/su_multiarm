"""Neo4j ``GraphStore`` facet (+ ``backends.extras.GraphExtras``): MERGE-idempotent load of mentions,
typed relations and memberships; templated per-hop traversal; communities as nodes.

Semantics mirror ``backends/sqlite/graph.py`` so the conformance kit holds on both: traversal is a
breadth-first expansion of at most ``spec.hops`` (validated integer, 0..3) hops over the requested
edge kinds only (``fact`` by default; one relationship type per kind, so a fact template cannot reach
a co-occurrence edge), paths never revisit a node, edges are visited in load order, and the result
stops at ``spec.limit`` paths. Eligibility is pushed into every query as a document-id allow-list and
deny-list (the edge's or mention's span document). Predicates are **not** evaluated here: a scope
carrying predicates is refused with a typed error, and callers resolve it to document ids through the
catalog first (``arms/base.py::BaseArm.graph_scope``; capability flag ``predicate_scope: false``).

Differences from sqlite, by design: re-loading an identical mention, relation (same endpoints,
predicate, kind and span) or membership updates it in place instead of adding a duplicate row.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Sequence

from ...communities.base import Community
from ...core.errors import BackendError
from ...core.types import Entity, GenerationId, Membership, Occurrence, Path, PathEdge, Provenance, Relation, Scope, Span, TraversalSpec
from ...providers import tokens
from .._scope import validated_hops
from . import _cypher as q

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import Neo4jBackend


def _digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _span_props(span: Span) -> dict[str, Any]:
    return {
        "document_id": span.document_id,
        "chunk_id": span.chunk_id,
        "field": span.field,
        "span_start": span.start,
        "span_end": span.end,
        "quote": span.text,
        "match_level": span.match_level,
    }


def _span(row: dict[str, Any]) -> Span:
    return Span(
        document_id=row["document_id"],
        chunk_id=row["chunk_id"],
        field=row["field"],
        start=row["span_start"],
        end=row["span_end"],
        text=row["quote"],
        match_level=row["match_level"],
    )


def _entity(node: dict[str, Any]) -> Entity:
    return Entity(id=node["key"], name=node["name"], type=node["type"], span=_span(node), canonical_id=node.get("canonical_id"), aliases=list(node.get("aliases") or []))


def _edge(row: dict[str, Any]) -> PathEdge:
    return PathEdge(
        subject_id=row["subject"],
        predicate=row["predicate"],
        object_id=row["object"],
        edge_kind=row["edge_kind"],
        support=row["support"],
        span=_span(row),
        provenance=Provenance.model_validate_json(row["provenance"]),
        occurrences=[Occurrence.model_validate(o) for o in json.loads(row["occurrences"] or "[]")],
    )


def scope_params(scope: Scope | None, operation: str) -> dict[str, Any] | None:
    """Driver parameters for the eligibility clause; ``None`` when the scope admits nothing."""
    if scope is None:
        return {"all_docs": True, "allow": [], "exclude": []}
    if scope.predicates:
        raise BackendError(
            q.KIND,
            operation,
            "the neo4j graph evaluates eligibility as document-id allow/deny lists only; resolve scope predicates "
            "to document_ids through the catalog first",
        )
    if scope.document_ids is not None and not scope.document_ids:
        return None
    return {"all_docs": scope.document_ids is None, "allow": list(scope.document_ids or []), "exclude": list(scope.exclude_ids)}


class Neo4jGraph:
    def __init__(self, backend: Neo4jBackend):
        self._b = backend

    # --- write ---------------------------------------------------------------------------------

    def upsert(
        self, gen: GenerationId, entities: Sequence[Entity], relations: Sequence[Relation], memberships: Sequence[Membership]
    ) -> None:
        """One transaction: either the whole call is committed (and the generation is marked loaded)
        or nothing is. Relation endpoints must be stored entities of the generation."""
        b = self._b
        g = b.gen_key(gen)
        with b.transaction("upsert", write=True) as run:
            seq = run(
                q.GENERATION_OPEN,
                g=g,
                namespace=b.namespace,
                dataset=gen.dataset,
                generation=gen.generation,
                index_format=gen.index_format,
                fingerprint=gen.fingerprint,
            )[0]["seq"]
            rows = []
            for entity in entities:
                words = tokens(entity.name)
                rows.append(
                    {
                        "key": entity.id,
                        "mid": _digest(entity.id, entity.span.document_id, entity.span.chunk_id, entity.span.field, entity.span.start, entity.span.end),
                        "seq": seq,
                        "name": entity.name,
                        "type": entity.type,
                        "canonical_id": entity.canonical_id,
                        "aliases": list(entity.aliases),
                        "first_token": words[0] if words else "",
                        "tokens": words,
                        **_span_props(entity.span),
                    }
                )
                seq += 1
            for start in range(0, len(rows), q.BATCH):
                run(q.MENTIONS_MERGE, g=g, rows=rows[start : start + q.BATCH])
            endpoints = sorted({r.subject_id for r in relations} | {r.object_id for r in relations})
            for start in range(0, len(endpoints), q.BATCH):
                missing = run(q.MISSING_ENDPOINTS, g=g, keys=endpoints[start : start + q.BATCH])
                if missing:
                    raise BackendError(q.KIND, "upsert", f"relation endpoints must be stored entities (missing: {[m['k'] for m in missing]})")
            by_kind: dict[str, list[dict[str, Any]]] = {}
            for relation in relations:
                span = relation.span
                by_kind.setdefault(relation.edge_kind, []).append(
                    {
                        "subject": relation.subject_id,
                        "object": relation.object_id,
                        "rid": _digest(relation.subject_id, relation.predicate, relation.object_id, relation.edge_kind, span.document_id, span.chunk_id, span.field, span.start, span.end),
                        "seq": seq,
                        "predicate": relation.predicate,
                        "edge_kind": relation.edge_kind,
                        "support": relation.support,
                        "status": relation.status,
                        "provenance": relation.provenance.model_dump_json(),
                        "occurrences": json.dumps([o.model_dump(mode="json") for o in relation.occurrences], ensure_ascii=False, sort_keys=True),
                        **_span_props(span),
                    }
                )
                seq += 1
            for kind in sorted(by_kind):
                batch = by_kind[kind]
                for start in range(0, len(batch), q.BATCH):
                    part = batch[start : start + q.BATCH]
                    written = run(q.RELATION_MERGE[kind], g=g, rows=part)[0]["written"]
                    if written != len(part):
                        raise BackendError(q.KIND, "upsert", f"{len(part) - written} relation(s) were not written")
            member_rows = []
            for membership in memberships:
                member_rows.append(
                    {
                        "mkey": _digest(membership.community_id, membership.member_id, membership.role, membership.basis),
                        "seq": seq,
                        **membership.model_dump(),
                    }
                )
                seq += 1
            for start in range(0, len(member_rows), q.BATCH):
                run(q.MEMBERSHIPS_MERGE, g=g, rows=member_rows[start : start + q.BATCH])
            run(q.GENERATION_CLOSE, g=g, seq=seq, now=b.clock())

    def put_communities(self, gen: GenerationId, communities: Sequence[Community]) -> None:
        b = self._b
        g = b.gen_key(gen)
        rows = [
            {"id": c.id, "label": c.label, "document_ids": list(c.document_ids), "entity_ids": list(c.entity_ids), "report": json.dumps(c.report, ensure_ascii=False, sort_keys=True)}
            for c in communities
        ]
        with b.transaction("put_communities", write=True) as run:
            seq = run(q.GENERATION_OPEN, g=g, namespace=b.namespace, dataset=gen.dataset, generation=gen.generation, index_format=gen.index_format, fingerprint=gen.fingerprint)[0]["seq"]
            for start in range(0, len(rows), q.BATCH):
                run(q.COMMUNITIES_MERGE, g=g, rows=rows[start : start + q.BATCH])
            run(q.GENERATION_CLOSE, g=g, seq=seq, now=b.clock())

    # --- read ----------------------------------------------------------------------------------

    def traverse(self, gen: GenerationId, spec: TraversalSpec, scope: Scope | None) -> list[Path]:
        hops = validated_hops(q.KIND, spec.hops)
        kinds = frozenset(spec.edge_kinds)
        template = q.HOP.get(kinds)
        if template is None:
            raise BackendError(q.KIND, "traverse", f"no traversal template for edge kinds {sorted(kinds)}")
        eligible = scope_params(scope, "traverse")
        g = self._b.gen_key(gen)
        paths: list[Path] = []
        with self._b.transaction("traverse", write=False) as run:
            self._b.require_generation(run, gen)
            if eligible is None:
                return []
            known = {r["key"] for r in run(q.KNOWN_ENTITIES, g=g, keys=list(spec.seed_entity_ids))}
            frontier = [Path(nodes=[seed]) for seed in spec.seed_entity_ids if seed in known]
            for _ in range(hops):
                nodes = sorted({p.nodes[-1] for p in frontier})
                if not nodes:
                    break
                rows = run(template, g=g, nodes=nodes, predicates=list(spec.predicates), **eligible)
                next_frontier = []
                for path in frontier:
                    node = path.nodes[-1]
                    for row in rows:
                        if spec.direction in {"out", "any"} and row["subject"] == node:
                            other = row["object"]
                        elif spec.direction in {"in", "any"} and row["object"] == node:
                            other = row["subject"]
                        else:
                            continue
                        if other in path.nodes:
                            continue
                        extended = Path(nodes=[*path.nodes, other], edges=[*path.edges, _edge(row)])
                        next_frontier.append(extended)
                        paths.append(extended)
                        if len(paths) >= spec.limit:
                            return paths
                frontier = next_frontier
        return paths

    def find_entities(self, gen: GenerationId, tokens_: Sequence[str], scope: Scope | None) -> list[Entity]:
        wanted = sorted(set(tokens_))
        eligible = scope_params(scope, "find_entities")
        with self._b.transaction("find_entities", write=False) as run:
            self._b.require_generation(run, gen)
            if not wanted or eligible is None:
                return []
            rows = run(q.FIND_ENTITIES, g=self._b.gen_key(gen), wanted=wanted, **eligible)
        found: dict[str, Entity] = {}
        for row in rows:
            node = row["m"]
            if node["key"] not in found:
                found[node["key"]] = _entity(node)
        return list(found.values())

    def mentions(self, gen: GenerationId, entity_ids: Sequence[str], scope: Scope | None, limit: int) -> list[Entity]:
        ids = list(dict.fromkeys(entity_ids))
        eligible = scope_params(scope, "mentions")
        with self._b.transaction("mentions", write=False) as run:
            self._b.require_generation(run, gen)
            if not ids or eligible is None or limit <= 0:
                return []
            rows = run(q.MENTIONS, g=self._b.gen_key(gen), keys=ids, limit=int(limit), **eligible)
        return [_entity(row["m"]) for row in rows]

    def memberships(self, gen: GenerationId) -> list[Membership]:
        with self._b.transaction("memberships", write=False) as run:
            self._b.require_generation(run, gen)
            rows = run(q.MEMBERSHIPS, g=self._b.gen_key(gen))
        return [Membership(**row) for row in rows]

    def communities(self, gen: GenerationId) -> list[Community]:
        with self._b.transaction("communities", write=False) as run:
            self._b.require_generation(run, gen)
            rows = run(q.COMMUNITIES, g=self._b.gen_key(gen))
        return [
            Community(id=r["id"], label=r["label"] or "", document_ids=list(r["document_ids"] or []), entity_ids=list(r["entity_ids"] or []), report=json.loads(r["report"]))
            for r in rows
        ]
