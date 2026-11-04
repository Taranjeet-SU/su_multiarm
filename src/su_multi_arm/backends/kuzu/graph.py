"""Native kuzu ``GraphStore`` facet (+ ``backends.extras.GraphExtras``): MERGE-idempotent load of
mentions, typed relations and memberships; templated per-hop traversal; communities as rows.

Semantics mirror ``backends/sqlite/graph.py`` and ``backends/neo4j/graph.py`` so the conformance kit
holds on all three: traversal is a breadth-first expansion of at most ``spec.hops`` (validated
integer, 0..3) hops over the requested edge kinds only (``fact`` by default; one relationship table
per kind, so a fact template cannot reach a co-occurrence edge), paths never revisit a node, edges are
visited in load order, and the result stops at ``spec.limit`` paths. Eligibility is pushed into every
query as a document-id allow-list and deny-list (the edge's or mention's span document). Predicates
are **not** evaluated here: a scope carrying predicates is refused with a typed error and callers
resolve it to document ids through the catalog first (capability flag ``predicate_scope: false``).

As on neo4j, re-loading an identical mention, relation (same endpoints, predicate, kind and span) or
membership updates it in place instead of adding a duplicate row.
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
    from .adapter import KuzuBackend


def digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def scope_params(scope: Scope | None, operation: str) -> dict[str, Any] | None:
    """Parameters of the eligibility clause; ``None`` when the scope admits nothing."""
    if scope is None:
        return {"all_docs": True, "allow": [], "exclude": []}
    if scope.predicates:
        raise BackendError(
            q.KIND,
            operation,
            "the kuzu graph evaluates eligibility as document-id allow/deny lists only; resolve scope predicates "
            "to document_ids through the catalog first",
        )
    if scope.document_ids is not None and not scope.document_ids:
        return None
    return {"all_docs": scope.document_ids is None, "allow": list(scope.document_ids or []), "exclude": list(scope.exclude_ids)}


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


def _entity(row: dict[str, Any]) -> Entity:
    return Entity(id=row["key"], name=row["name"], type=row["etype"], span=_span(row), canonical_id=row["canonical_id"] or None, aliases=list(row["aliases"] or []))


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


def expand(frontier: list[Path], rows: list[dict[str, Any]], direction: str, paths: list[Path], limit: int, edge=_edge) -> tuple[list[Path], bool]:
    """One BFS hop shared by the native and attached facets: extend every frontier path by every
    eligible row in order, never revisiting a node; stop at ``limit`` paths. Returns the next frontier
    and whether the limit was reached."""
    next_frontier: list[Path] = []
    for path in frontier:
        node = path.nodes[-1]
        for row in rows:
            if direction in {"out", "any"} and row["subject"] == node:
                other = row["object"]
            elif direction in {"in", "any"} and row["object"] == node:
                other = row["subject"]
            else:
                continue
            if other in path.nodes:
                continue
            extended = Path(nodes=[*path.nodes, other], edges=[*path.edges, edge(row)])
            next_frontier.append(extended)
            paths.append(extended)
            if len(paths) >= limit:
                return next_frontier, True
    return next_frontier, False


class KuzuGraph:
    def __init__(self, backend: KuzuBackend):
        self._b = backend

    # --- write ---------------------------------------------------------------------------------

    def upsert(
        self, gen: GenerationId, entities: Sequence[Entity], relations: Sequence[Relation], memberships: Sequence[Membership]
    ) -> None:
        """One transaction: either the whole call is committed (and the generation marked loaded) or
        nothing is. Relation endpoints must be stored entities of the generation."""
        b = self._b
        g = b.gen_key(gen)
        with b.transaction("upsert") as run:
            seq = run(
                q.GENERATION_OPEN,
                g=g,
                namespace=b.namespace,
                dataset=gen.dataset,
                generation=gen.generation,
                index_format=gen.index_format,
                fingerprint=gen.fingerprint,
            )[0]["seq"]
            keys = sorted({e.id for e in entities})
            for start in range(0, len(keys), q.BATCH):
                run(q.ENTITIES_MERGE, g=g, rows=[{"id": f"{g}|{k}", "key": k} for k in keys[start : start + q.BATCH]])
            rows = []
            for entity in entities:
                words = tokens(entity.name)
                mid = digest(entity.id, entity.span.document_id, entity.span.chunk_id, entity.span.field, entity.span.start, entity.span.end)
                rows.append(
                    {
                        "id": f"{g}|{mid}",
                        "mid": mid,
                        "seq": seq,
                        "key": entity.id,
                        "name": entity.name,
                        "etype": entity.type,
                        "canonical_id": entity.canonical_id or "",
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
                part = endpoints[start : start + q.BATCH]
                known = {r["key"] for r in run(q.KNOWN_IDS, ids=[f"{g}|{k}" for k in part])}
                missing = [k for k in part if k not in known]
                if missing:
                    raise BackendError(q.KIND, "upsert", f"relation endpoints must be stored entities (missing: {missing[:5]})")
            by_kind: dict[str, list[dict[str, Any]]] = {}
            for relation in relations:
                span = relation.span
                by_kind.setdefault(relation.edge_kind, []).append(
                    {
                        "sid": f"{g}|{relation.subject_id}",
                        "oid": f"{g}|{relation.object_id}",
                        "subject": relation.subject_id,
                        "object": relation.object_id,
                        "rid": digest(relation.subject_id, relation.predicate, relation.object_id, relation.edge_kind, span.document_id, span.chunk_id, span.field, span.start, span.end),
                        "seq": seq,
                        "predicate": relation.predicate,
                        "edge_kind": relation.edge_kind,
                        "support": relation.support,
                        "status": relation.status or "",
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
                mkey = digest(membership.community_id, membership.member_id, membership.role, membership.basis)
                member_rows.append(
                    {
                        "id": f"{g}|{mkey}",
                        "seq": seq,
                        "community_id": membership.community_id,
                        "member_id": membership.member_id,
                        "role": membership.role or "",
                        "basis": membership.basis,
                    }
                )
                seq += 1
            for start in range(0, len(member_rows), q.BATCH):
                run(q.MEMBERSHIPS_MERGE, g=g, rows=member_rows[start : start + q.BATCH])
            run(q.GENERATION_CLOSE, g=g, seq=seq, now=float(b.clock()))

    def put_communities(self, gen: GenerationId, communities: Sequence[Community]) -> None:
        b = self._b
        g = b.gen_key(gen)
        rows = [
            {
                "id": f"{g}|{c.id}",
                "cid": c.id,
                "label": c.label,
                "document_ids": list(c.document_ids),
                "entity_ids": list(c.entity_ids),
                "report": json.dumps(c.storage_report(), ensure_ascii=False, sort_keys=True),
            }
            for c in communities
        ]
        with b.transaction("put_communities") as run:
            seq = run(q.GENERATION_OPEN, g=g, namespace=b.namespace, dataset=gen.dataset, generation=gen.generation, index_format=gen.index_format, fingerprint=gen.fingerprint)[0]["seq"]
            for start in range(0, len(rows), q.BATCH):
                run(q.COMMUNITIES_MERGE, g=g, rows=rows[start : start + q.BATCH])
            run(q.GENERATION_CLOSE, g=g, seq=seq, now=float(b.clock()))

    # --- read ----------------------------------------------------------------------------------

    def _hop_rows(self, g: str, spec: TraversalSpec, nodes: list[str], eligible: dict[str, Any]) -> list[dict[str, Any]]:
        kinds = frozenset(spec.edge_kinds)
        ids = [f"{g}|{n}" for n in nodes]
        templates = ([q.HOP_OUT[kinds]] if spec.direction in {"out", "any"} else []) + ([q.HOP_IN[kinds]] if spec.direction in {"in", "any"} else [])
        seen: dict[str, dict[str, Any]] = {}
        for template in templates:
            for row in self._b.run("traverse", template, g=g, nodes=ids, any_predicate=not spec.predicates, predicates=list(spec.predicates), **eligible):
                seen.setdefault(row["rid"], row)
        return sorted(seen.values(), key=lambda r: (r["seq"], r["rid"]))

    def traverse(self, gen: GenerationId, spec: TraversalSpec, scope: Scope | None) -> list[Path]:
        hops = validated_hops(q.KIND, spec.hops)
        kinds = frozenset(spec.edge_kinds)
        if kinds not in q.HOP_OUT:
            raise BackendError(q.KIND, "traverse", f"no traversal template for edge kinds {sorted(kinds)}")
        eligible = scope_params(scope, "traverse")
        self._b.require_generation(gen, "traverse")
        if eligible is None:
            return []
        g = self._b.gen_key(gen)
        known = {r["key"] for r in self._b.run("traverse", q.KNOWN_IDS, ids=[f"{g}|{s}" for s in spec.seed_entity_ids])}
        frontier = [Path(nodes=[seed]) for seed in spec.seed_entity_ids if seed in known]
        paths: list[Path] = []
        for _ in range(hops):
            nodes = sorted({p.nodes[-1] for p in frontier})
            if not nodes:
                break
            rows = self._hop_rows(g, spec, nodes, eligible)
            frontier, done = expand(frontier, rows, spec.direction, paths, spec.limit)
            if done:
                break
        return paths

    def find_entities(self, gen: GenerationId, tokens_: Sequence[str], scope: Scope | None) -> list[Entity]:
        wanted = sorted(set(tokens_))
        eligible = scope_params(scope, "find_entities")
        self._b.require_generation(gen, "find_entities")
        if not wanted or eligible is None:
            return []
        rows = self._b.run("find_entities", q.FIND_ENTITIES, g=self._b.gen_key(gen), wanted=wanted, **eligible)
        wanted_set = set(wanted)
        found: dict[str, Entity] = {}
        for row in rows:
            if row["key"] in found:
                continue
            words = set(row["tokens"] or [])
            if words and words <= wanted_set:
                found[row["key"]] = _entity(row)
        return list(found.values())

    def mentions(self, gen: GenerationId, entity_ids: Sequence[str], scope: Scope | None, limit: int) -> list[Entity]:
        ids = list(dict.fromkeys(entity_ids))
        eligible = scope_params(scope, "mentions")
        self._b.require_generation(gen, "mentions")
        if not ids or eligible is None or limit <= 0:
            return []
        rows = self._b.run("mentions", q.MENTIONS, g=self._b.gen_key(gen), keys=ids, limit=int(limit), **eligible)
        return [_entity(row) for row in rows]

    def memberships(self, gen: GenerationId) -> list[Membership]:
        self._b.require_generation(gen, "memberships")
        rows = self._b.run("memberships", q.MEMBERSHIPS, g=self._b.gen_key(gen))
        return [Membership(community_id=r["community_id"], member_id=r["member_id"], role=r["role"] or None, basis=r["basis"]) for r in rows]

    def communities(self, gen: GenerationId) -> list[Community]:
        self._b.require_generation(gen, "communities")
        rows = self._b.run("communities", q.COMMUNITIES, g=self._b.gen_key(gen))
        return [
            Community.from_storage(r["id"], r["label"] or "", list(r["document_ids"] or []), list(r["entity_ids"] or []), json.loads(r["report"]))
            for r in rows
        ]
