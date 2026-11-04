"""SQLite ``GraphStore`` facet: passage-backed mentions, typed relations, memberships, communities.
Traversal is templated BFS over ``fact`` edges (or the requested kinds) with per-hop SQL bounded by
the frontier, never a whole-graph load."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Sequence

from ...communities.base import Community
from ...core.errors import BackendError
from .._scope import validated_hops
from ...core.types import Entity, GenerationId, Membership, Occurrence, Path, PathEdge, Provenance, Relation, Scope, Span, TraversalSpec
from ._db import KIND, dumps, placeholders, scope_sql
from ...providers import tokens  # casefolded entity-name tokens (the graph arm folds the same way)

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import SqliteBackend


def _entity(row) -> Entity:
    return Entity(
        id=row["key"],
        name=row["name"],
        type=row["kind"],
        span=Span(
            document_id=row["document_id"],
            chunk_id=row["chunk_id"],
            field=row["field"],
            start=row["start"],
            end=row["end"],
            text=row["quote"],
            match_level=row["match_level"],
        ),
        canonical_id=row["canonical_id"],
        aliases=json.loads(row["aliases"] or "[]"),
    )


def _edge(row) -> PathEdge:
    return PathEdge(
        subject_id=row["subject"],
        predicate=row["predicate"],
        object_id=row["object"],
        edge_kind=row["edge_kind"],
        support=row["support"],
        span=Span(
            document_id=row["document_id"],
            chunk_id=row["chunk_id"],
            field=row["field"],
            start=row["start"],
            end=row["end"],
            text=row["quote"],
            match_level=row["match_level"],
        ),
        provenance=Provenance.model_validate_json(row["provenance"]),
        occurrences=[Occurrence.model_validate(o) for o in json.loads(row["occurrences"] or "[]")],
    )


class SqliteGraph:
    def __init__(self, backend: SqliteBackend):
        self._b = backend

    def upsert(
        self, gen: GenerationId, entities: Sequence[Entity], relations: Sequence[Relation], memberships: Sequence[Membership]
    ) -> None:
        with self._b.catalog_db(gen, create=True) as db:
            for entity in entities:
                span = entity.span
                words = tokens(entity.name)
                db.execute(
                    "INSERT INTO entities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        entity.id,
                        entity.name,
                        entity.type,
                        span.document_id,
                        span.chunk_id,
                        span.text,
                        span.start,
                        span.end,
                        span.match_level,
                        span.field,
                        entity.canonical_id,
                        json.dumps(entity.aliases),
                        words[0] if words else "",
                    ),
                )
            for relation in relations:
                for end in (relation.subject_id, relation.object_id):
                    if db.execute("SELECT 1 FROM entities WHERE key=? LIMIT 1", (end,)).fetchone() is None:
                        raise BackendError(KIND, "upsert", "relation endpoints must be stored entities")
                span = relation.span
                db.execute(
                    "INSERT INTO relations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        relation.subject_id,
                        relation.predicate,
                        relation.object_id,
                        span.document_id,
                        span.chunk_id,
                        span.text,
                        span.start,
                        span.end,
                        span.match_level,
                        relation.edge_kind,
                        relation.support,
                        span.field,
                        relation.status,
                        relation.provenance.model_dump_json(),
                        dumps([o.model_dump(mode="json") for o in relation.occurrences]),
                    ),
                )
            db.executemany(
                "INSERT INTO memberships VALUES (?,?,?,?)",
                [(m.community_id, m.member_id, m.role, m.basis) for m in memberships],
            )

    def traverse(self, gen: GenerationId, spec: TraversalSpec, scope: Scope | None) -> list[Path]:
        hops = validated_hops(KIND, spec.hops)
        where, params = scope_sql("r.document_id", scope)
        kinds = list(spec.edge_kinds)
        paths: list[Path] = []
        with self._b.catalog_db(gen) as db:
            known = {
                row["key"]
                for row in db.execute(f"SELECT DISTINCT key FROM entities WHERE key IN ({placeholders(spec.seed_entity_ids)})", spec.seed_entity_ids)
            }
            frontier = [Path(nodes=[seed]) for seed in spec.seed_entity_ids if seed in known]
            for _ in range(hops):
                nodes = sorted({p.nodes[-1] for p in frontier})
                if not nodes:
                    break
                predicate_clause = ""
                extra: list = []
                if spec.predicates:
                    predicate_clause = f" AND r.predicate IN ({placeholders(spec.predicates)})"
                    extra = list(spec.predicates)
                rows = db.execute(
                    f"SELECT r.* FROM relations r WHERE r.edge_kind IN ({placeholders(kinds)}){predicate_clause} "
                    f"AND (r.subject IN ({placeholders(nodes)}) OR r.object IN ({placeholders(nodes)})) AND {where} ORDER BY r.rowid",
                    [*kinds, *extra, *nodes, *nodes, *params],
                ).fetchall()
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

    def memberships(self, gen: GenerationId) -> list[Membership]:
        with self._b.catalog_db(gen) as db:
            return [Membership(**dict(r)) for r in db.execute("SELECT community_id, member_id, role, basis FROM memberships ORDER BY rowid")]

    def find_entities(self, gen: GenerationId, tokens_: Sequence[str], scope: Scope | None) -> list[Entity]:
        wanted = sorted(set(tokens_))
        if not wanted:
            return []
        where, params = scope_sql("e.document_id", scope)
        with self._b.catalog_db(gen) as db:
            rows = db.execute(
                f"SELECT e.* FROM entities e WHERE e.first_token IN ({placeholders(wanted)}) AND {where} ORDER BY e.key, e.rowid",
                [*wanted, *params],
            ).fetchall()
        found: dict[str, Entity] = {}
        wanted_set = set(wanted)
        for row in rows:
            if row["key"] in found:
                continue
            name_tokens = set(tokens(row["name"]))
            if name_tokens and name_tokens <= wanted_set:
                found[row["key"]] = _entity(row)
        return list(found.values())

    def mentions(self, gen: GenerationId, entity_ids: Sequence[str], scope: Scope | None, limit: int) -> list[Entity]:
        ids = list(dict.fromkeys(entity_ids))
        if not ids:
            return []
        where, params = scope_sql("e.document_id", scope)
        with self._b.catalog_db(gen) as db:
            rows = db.execute(
                f"SELECT e.* FROM entities e WHERE e.key IN ({placeholders(ids)}) AND {where} ORDER BY e.rowid LIMIT ?",
                [*ids, *params, max(0, limit)],
            ).fetchall()
        return [_entity(r) for r in rows]

    def put_communities(self, gen: GenerationId, communities: Sequence[Community]) -> None:
        with self._b.catalog_db(gen, create=True) as db:
            for community in communities:
                db.execute(
                    "INSERT OR REPLACE INTO communities VALUES (?,?,?,?,?)",
                    (community.id, community.label, json.dumps(community.document_ids), json.dumps(community.entity_ids), dumps(community.report)),
                )

    def communities(self, gen: GenerationId) -> list[Community]:
        with self._b.catalog_db(gen) as db:
            rows = db.execute("SELECT * FROM communities ORDER BY id").fetchall()
        return [
            Community(id=r["id"], label=r["label"], document_ids=json.loads(r["documents"]), entity_ids=json.loads(r["entities"]), report=json.loads(r["report"]))
            for r in rows
        ]
