"""Read-only attachment of an **existing** Kuzu database to the graph contract (``mode: attach``).

An operator declares, in a mapping file named by the config (``backends.<name>.attach.mapping``),
which node tables are entities, which relationship tables are edges (with their predicate, edge kind,
support, quote/source/verification columns and provenance columns), how a source id maps to a package
document id, and optionally which tables hold communities. The mapping is validated against the
store's own catalog when the backend opens (unknown tables/columns are a typed ``BackendError``); the
read statements are then built **once** from those validated identifiers (backtick-quoted) and are
the only statements the session will run. Request data travels only as parameters. The database is
opened ``read_only``: Kuzu itself refuses every write, and ``upsert``/``put_communities`` are refused
with a typed error before reaching it.

Contract mapping (what an attached store can and cannot claim):

* entity id = ``<NodeTable>:<key>``; entity type = the mapping's ``type``;
* an edge is surfaced only when its quote is non-empty, its source id yields a document id through
  ``document_pattern`` (a Python regular expression searched in it; the document id is
  ``document_template`` expanded from the match, default group 1) and — with
  ``require_verified`` (default) and a ``verified`` column — the store marked the quote verified.
  Everything else is skipped, never repaired. Document ids are derived in Python, not with Kuzu's
  regex functions (``regexp_extract`` returned stale values under filters in Kuzu 0.11.3). The
  edge's ``Span`` anchors the stored quote itself (``field = <REL>.<quote column>``,
  ``chunk_id`` = the external source id, offsets ``0..len(quote)``): it is verbatim store text, not an
  offset into the package catalog;
* eligibility (document allow/deny lists) is applied to that document id on every row; reads are
  paged per node so a scope never hides a node's eligible rows behind ineligible ones; predicates are
  refused (``predicate_scope: false``) and resolved through the catalog by the arms;
* ``find_entities`` keeps nodes whose name tokens (the package tokeniser) are all in the query tokens,
  from an in-memory token index of the ``lookup`` node tables built once on first use (bounded by
  ``max_lookup_rows``; the store is immutable); under a scope a node is returned only when it has an
  eligible mention (its own document or an eligible edge);
* the attached store is one immutable snapshot: operations are accepted for ``dataset`` and the
  declared ``generations`` (or any generation of the dataset with ``any_generation: true``).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path as FsPath
from typing import Any, Callable, Sequence

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from ...communities.base import Community
from ...core.config_graph import GraphOntology, PredicateDecl
from ...core.errors import BackendError
from ...core.types import EdgeKind, Entity, GenerationId, Membership, Path, PathEdge, Provenance, Relation, Scope, Span, Support, TraversalSpec
from ...models import StrictModel
from ...providers import tokens
from .._scope import validated_hops
from .adapter import KuzuSession, import_kuzu
from .graph import expand, scope_params

KIND = "kuzu"
TABLE = r"^[A-Za-z_][A-Za-z0-9_]{0,127}$"
IDENT = r"^[a-z][a-z0-9_]{0,63}$"
HEX32 = re.compile(r"^[0-9a-f]{32}$")
MAX_PATH_NODES = 4  # a path has at most MAX_HOPS + 1 nodes: the most rows of one node a hop can skip


class DocumentRef(StrictModel):
    """How a node maps to a package document id: group 1 of ``pattern`` searched in ``column``."""

    column: str = Field(pattern=TABLE)
    pattern: str = r"^(.+)$"
    template: str = r"\1"  # ``re.Match.expand`` template, e.g. ``\1:\2``


class NodeMap(StrictModel):
    table: str = Field(pattern=TABLE)
    type: str = Field(pattern=IDENT)
    key: str = Field(default="id", pattern=TABLE)
    name: str = Field(default="name", pattern=TABLE)
    lookup: bool = True  # may seed a traversal through find_entities
    document: DocumentRef | None = None  # the node is (part of) a package document
    mention_text: str | None = Field(default=None, pattern=TABLE)  # stored text that is the node's self-mention


class ProvenanceMap(StrictModel):
    extractor: str | None = Field(default=None, pattern=TABLE)
    model: str | None = Field(default=None, pattern=TABLE)
    extracted_at: str | None = Field(default=None, pattern=TABLE)
    confidence: str | None = Field(default=None, pattern=TABLE)
    default_extractor: str = Field(default="external", min_length=1)


class EdgeMap(StrictModel):
    table: str = Field(pattern=TABLE)
    predicate: str = Field(pattern=IDENT)
    edge_kind: EdgeKind = "fact"
    support: Support = "unchecked"
    quote: str = Field(default="quote", pattern=TABLE)
    source: str = Field(default="source_id", pattern=TABLE)
    document_pattern: str = r"^(.+)$"
    document_template: str = r"\1"
    verified: str | None = Field(default=None, pattern=TABLE)
    status: str | None = Field(default=None, pattern=TABLE)
    provenance: ProvenanceMap = Field(default_factory=ProvenanceMap)

    @field_validator("support")
    @classmethod
    def no_entailment(cls, value):
        if value == "entailment_checked":
            raise ValueError("support=entailment_checked is reserved: no producer exists before Phase 5")
        return value


class CommunityMap(StrictModel):
    table: str = Field(pattern=TABLE)
    key: str = Field(default="id", pattern=TABLE)
    label: list[str] = Field(default_factory=lambda: ["name"], min_length=1)
    level: str | None = Field(default=None, pattern=TABLE)
    summary: str | None = Field(default=None, pattern=TABLE)
    quote: str | None = Field(default=None, pattern=TABLE)
    quote_source: str | None = Field(default=None, pattern=TABLE)
    quote_document_pattern: str = r"^(.+)$"
    quote_document_template: str = r"\1"
    member_edge: str = Field(pattern=TABLE)  # (member)-[member_edge]->(community)

    @field_validator("label")
    @classmethod
    def label_columns(cls, value):
        for column in value:
            if not re.fullmatch(TABLE, column):
                raise ValueError(f"label column {column!r} is not an identifier")
        return value


class AttachMapping(StrictModel):
    dataset: str = Field(min_length=1)
    generations: list[str] = Field(default_factory=list)
    any_generation: bool = False
    require_verified: bool = True
    nodes: list[NodeMap] = Field(min_length=1)
    edges: list[EdgeMap] = Field(default_factory=list)
    communities: CommunityMap | None = None
    max_lookup_rows: int = Field(default=500_000, ge=1, le=10_000_000)
    max_rows_per_hop: int = Field(default=5000, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def coherent(self):
        if not self.generations and not self.any_generation:
            raise ValueError("declare the package generations this store answers for, or any_generation: true")
        for generation in self.generations:
            if not HEX32.match(generation):
                raise ValueError(f"generation {generation!r} must be 32 lowercase hex characters")
        tables = [n.table for n in self.nodes]
        if len(set(tables)) != len(tables):
            raise ValueError("each node table is mapped once")
        types = [n.type for n in self.nodes]
        if len(set(types)) != len(types):
            raise ValueError("each entity type maps exactly one node table")
        rules = [(n.document.pattern, n.document.template) for n in self.nodes if n.document] + [(e.document_pattern, e.document_template) for e in self.edges]
        if self.communities is not None:
            rules.append((self.communities.quote_document_pattern, self.communities.quote_document_template))
        for pattern, template in rules:
            DocRule(pattern, template)  # raises ValueError when it cannot produce a document id
        return self

    @classmethod
    def load(cls, path: str | FsPath) -> AttachMapping:
        try:
            data = yaml.safe_load(FsPath(path).read_text())
        except (OSError, yaml.YAMLError) as exc:
            raise BackendError(KIND, "attach", f"cannot read mapping {path}: {exc}") from exc
        try:
            return cls.model_validate(data or {})
        except ValidationError as exc:
            raise BackendError(KIND, "attach", f"invalid mapping {path}: {exc.error_count()} error(s): {exc.errors()[0]['msg']}") from exc


def _q(name: str) -> str:
    return f"`{name}`"


class DocRule:
    """``pattern`` searched in a stored value; the package document id is ``template`` expanded from
    the match (``re.Match.expand``). Operator configuration, compiled once; never request data."""

    def __init__(self, pattern: str, template: str):
        try:
            self.regex = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"pattern {pattern!r} does not compile: {exc}") from exc
        refs = [int(n) for n in re.findall(r"\\(\d+)", template)]
        if not refs or max(refs) > self.regex.groups:
            raise ValueError(f"template {template!r} must reference 1..{self.regex.groups} capturing groups of {pattern!r}")
        self.template = template


def document_of(rule: DocRule | None, value: Any) -> str | None:
    """The package document id a stored value names under ``rule``, or ``None`` (no match)."""
    if rule is None or value is None:
        return None
    match = rule.regex.search(str(value))
    if match is None:
        return None
    try:
        doc = match.expand(rule.template)
    except (IndexError, re.error):  # a referenced optional group did not participate
        return None
    return doc or None


class _Catalog:
    """Tables and columns of the attached store (validated once at open)."""

    def __init__(self, session: KuzuSession):
        self.tables: dict[str, str] = {}
        self.columns: dict[str, set[str]] = {}
        self.connections: dict[str, list[tuple[str, str]]] = {}
        with session.lock:
            for row in _raw(session, "CALL show_tables() RETURN *"):
                self.tables[row["name"]] = row["type"]
            for name in self.tables:
                self.columns[name] = {r["name"] for r in _raw(session, f"CALL table_info('{name}') RETURN *")}
                if self.tables[name] == "REL":
                    self.connections[name] = [(r["source table name"], r["destination table name"]) for r in _raw(session, f"CALL show_connection('{name}') RETURN *")]

    def require(self, table: str, kind: str, columns: Sequence[str | None], where: str) -> None:
        if self.tables.get(table) != kind:
            raise BackendError(KIND, "attach", f"{where}: {kind.lower()} table {table!r} does not exist in the attached store")
        missing = sorted({c for c in columns if c} - self.columns[table])
        if missing:
            raise BackendError(KIND, "attach", f"{where}: table {table!r} has no column(s) {missing}")


def _raw(session: KuzuSession, statement: str) -> list[dict[str, Any]]:
    """Catalog introspection at open (identifiers come from the store itself, never from requests)."""
    from .adapter import mapped

    with mapped("attach"):
        result = session.conn.execute(statement)
        columns = result.get_column_names()
        rows = []
        while result.has_next():
            rows.append(dict(zip(columns, result.get_next())))
        return rows


class _EdgeQuery:
    """Fixed read statements for one (edge mapping, from table, to table) connection, plus the
    compiled document pattern. Rows come back in store order (``rowid``), paged by ``SKIP/LIMIT``."""

    def __init__(self, index: int, edge: EdgeMap, source: NodeMap, target: NodeMap, require_verified: bool):
        self.index, self.edge, self.source, self.target = index, edge, source, target
        self.pattern = DocRule(edge.document_pattern, edge.document_template)
        r = edge
        filters = [f"coalesce(r.{_q(r.quote)}, '') <> ''"]
        if require_verified and r.verified:
            filters.append(f"r.{_q(r.verified)} = true")
        prov = r.provenance
        columns = [
            f"s.{_q(source.key)} AS skey",
            f"o.{_q(target.key)} AS okey",
            f"r.{_q(r.quote)} AS quote",
            f"r.{_q(r.source)} AS source",
            f"{'r.' + _q(prov.extractor) if prov.extractor else 'NULL'} AS extractor",
            f"{'r.' + _q(prov.model) if prov.model else 'NULL'} AS model",
            f"{'r.' + _q(prov.extracted_at) if prov.extracted_at else 'NULL'} AS extracted_at",
            f"{'r.' + _q(prov.confidence) if prov.confidence else 'NULL'} AS confidence",
            f"{'r.' + _q(r.status) if r.status else 'NULL'} AS status",
            "offset(id(r)) AS rowid",
        ]
        head = f"MATCH (s:{_q(source.table)})-[r:{_q(r.table)}]->(o:{_q(target.table)}) WHERE {{side}}.{{key}} IN $keys AND "
        body = " AND ".join(filters) + " RETURN " + ", ".join(columns) + " ORDER BY rowid SKIP $skip LIMIT $page"
        self.out = head.format(side="s", key=_q(source.key)) + body
        self.into = head.format(side="o", key=_q(target.key)) + body
        count = " AND ".join(filters) + " RETURN {side}.{key} AS key, count(*) AS n"
        self.counts = {
            self.out: (head + count).format(side="s", key=_q(source.key)),
            self.into: (head + count).format(side="o", key=_q(target.key)),
        }

    def statements(self) -> list[str]:
        return [self.out, self.into, *self.counts.values()]


class AttachedGraph:
    """``GraphStore`` + ``GraphExtras`` over an attached store (read-only)."""

    def __init__(self, backend: KuzuAttachedBackend):
        self._b = backend
        self._communities: list[Community] | None = None
        self._memberships: list[Membership] | None = None
        self._names: dict[str, list[tuple[str, str, frozenset[str], str | None]]] | None = None

    # --- refused writes ------------------------------------------------------------------------

    def upsert(self, gen: GenerationId, entities: Sequence[Entity], relations: Sequence[Relation], memberships: Sequence[Membership]) -> None:
        raise BackendError(KIND, "upsert", "the kuzu backend is attached read-only (mode: attach); writes are refused")

    def put_communities(self, gen: GenerationId, communities: Sequence[Community]) -> None:
        raise BackendError(KIND, "put_communities", "the kuzu backend is attached read-only (mode: attach); writes are refused")

    # --- helpers -------------------------------------------------------------------------------

    def _edge(self, query: _EdgeQuery, row: dict[str, Any]) -> PathEdge:
        quote = row["quote"]
        confidence = row["confidence"]
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
            confidence = None
        provenance = Provenance(
            extractor=str(row["extractor"] or query.edge.provenance.default_extractor),
            model=str(row["model"]) if row["model"] else None,
            extracted_at=str(row["extracted_at"]) if row["extracted_at"] else None,
            confidence=confidence,
        )
        span = Span(document_id=row["doc"], chunk_id=str(row["source"]), field=f"{query.edge.table}.{query.edge.quote}", start=0, end=len(quote), text=quote, match_level="exact")
        return PathEdge(
            subject_id=f"{query.source.table}:{row['skey']}",
            predicate=query.edge.predicate,
            object_id=f"{query.target.table}:{row['okey']}",
            edge_kind=query.edge.edge_kind,
            support=query.edge.support,
            span=span,
            provenance=provenance,
        )

    def _rows(self, query: _EdgeQuery, statement: str, keys: list[str], eligible: dict[str, Any], per_key: int, operation: str) -> list[dict[str, Any]]:
        """Eligible rows (with ``doc``) of one connection for the given node keys, in store order. A
        caller uses at most ``per_key`` rows of any one node. One batched read when it fits in
        ``max_rows_per_hop`` rows. Otherwise the rows per node are counted first: nodes with at most
        ``per_key`` rows are read whole in batches of at most ``max_rows_per_hop`` rows, and each
        larger node is read in pages until it has ``per_key`` eligible rows or none are left. Either
        way the caller sees the same rows: never a silent cut."""
        cap = self._b.mapping.max_rows_per_hop
        rows = self._b.run(operation, statement, keys=keys, skip=0, page=cap + 1)
        if len(rows) <= cap:
            return self._eligible_rows(query, rows, eligible)
        counts = {str(r["key"]): int(r["n"]) for r in self._b.run(operation, query.counts[statement], keys=keys)}
        out: list[dict[str, Any]] = []
        batch: list[str] = []
        total = 0
        for key in keys:
            n = counts.get(key, 0)
            if n == 0 or n > per_key:
                continue
            if batch and total + n > cap:
                out.extend(self._eligible_rows(query, self._b.run(operation, statement, keys=batch, skip=0, page=total), eligible))
                batch, total = [], 0
            batch.append(key)
            total += n
        if batch:
            out.extend(self._eligible_rows(query, self._b.run(operation, statement, keys=batch, skip=0, page=total), eligible))
        page = max(per_key, 64)
        for key in keys:
            if counts.get(key, 0) <= per_key:
                continue
            taken, skip = 0, 0
            while taken < per_key:
                chunk = self._b.run(operation, statement, keys=[key], skip=skip, page=page)
                kept = self._eligible_rows(query, chunk, eligible)[: per_key - taken]
                out.extend(kept)
                taken += len(kept)
                skip += len(chunk)
                if len(chunk) < page:
                    break
        return sorted(out, key=lambda r: r["rowid"])

    def entity_types(self, entity_ids: Sequence[str]) -> dict[str, str]:
        """Entity type per id from the id itself (``<NodeTable>:<key>``): no store read."""
        out = {}
        for entity_id in entity_ids:
            table, sep, _ = entity_id.partition(":")
            if sep and table in self._b.node_maps:
                out[entity_id] = self._b.node_maps[table].type
        return out

    @staticmethod
    def _eligible_rows(query: _EdgeQuery, rows: list[dict[str, Any]], eligible: dict[str, Any]) -> list[dict[str, Any]]:
        kept = []
        for row in rows:
            doc = document_of(query.pattern, row["source"])
            if doc is None or not AttachedGraph._admits(eligible, doc):
                continue
            kept.append({**row, "doc": doc})
        return kept

    def _by_table(self, entity_ids: Sequence[str]) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for entity_id in entity_ids:
            table, sep, key = entity_id.partition(":")
            if sep and table in self._b.node_maps:
                grouped.setdefault(table, []).append(key)
        return grouped

    # --- GraphStore ----------------------------------------------------------------------------

    def traverse(self, gen: GenerationId, spec: TraversalSpec, scope: Scope | None) -> list[Path]:
        hops = validated_hops(KIND, spec.hops)
        self._b.require_generation(gen, "traverse")
        eligible = scope_params(scope, "traverse")
        if eligible is None:
            return []
        known = self._known(spec.seed_entity_ids)
        frontier = [Path(nodes=[seed]) for seed in spec.seed_entity_ids if seed in known]
        kinds, predicates = set(spec.edge_kinds), set(spec.predicates)
        queries = [qy for qy in self._b.edge_queries if qy.edge.edge_kind in kinds and (not predicates or qy.edge.predicate in predicates)]
        paths: list[Path] = []
        per_key = spec.limit + MAX_PATH_NODES
        for _ in range(hops):
            grouped = self._by_table(sorted({p.nodes[-1] for p in frontier}))
            if not grouped:
                break
            rows: list[dict[str, Any]] = []
            for query in queries:
                for side, statement, table in (("out", query.out, query.source.table), ("in", query.into, query.target.table)):
                    if spec.direction not in {side, "any"} or table not in grouped:
                        continue
                    for row in self._rows(query, statement, grouped[table], eligible, per_key, "traverse"):
                        edge = self._edge(query, row)
                        rows.append({"subject": edge.subject_id, "object": edge.object_id, "edge": edge, "order": (query.index, row["rowid"])})
            unique: dict[tuple, dict[str, Any]] = {}
            for row in rows:
                unique.setdefault(row["order"], row)
            ordered = sorted(unique.values(), key=lambda r: r["order"])
            frontier, done = expand(frontier, ordered, spec.direction, paths, spec.limit, edge=lambda r: r["edge"])
            if done:
                break
        return paths

    def _known(self, entity_ids: Sequence[str]) -> set[str]:
        known: set[str] = set()
        for table, keys in self._by_table(entity_ids).items():
            node = self._b.node_maps[table]
            for row in self._b.run("traverse", self._b.node_by_key[table], keys=keys):
                known.add(f"{node.table}:{row['key']}")
        return known

    # --- GraphExtras ---------------------------------------------------------------------------

    def find_entities(self, gen: GenerationId, tokens_: Sequence[str], scope: Scope | None) -> list[Entity]:
        self._b.require_generation(gen, "find_entities")
        wanted = set(tokens_)
        eligible = scope_params(scope, "find_entities")
        if not wanted or eligible is None:
            return []
        index = self._name_index()
        found: list[Entity] = []
        for node in self._b.mapping.nodes:
            for key, name, words, doc in index.get(node.table, []):
                if words and words <= wanted:
                    entity = self._node_entity(node, key, name, doc)
                    if scope is not None and not self._eligible_entity(gen, entity, scope):
                        continue
                    found.append(entity)
        return found

    def _name_index(self) -> dict[str, list[tuple[str, str, frozenset[str], str | None]]]:
        """``table -> [(key, name, name tokens, document id)]`` of every ``lookup`` node table, in key
        order; read once (the attached store is immutable), bounded by ``max_lookup_rows``."""
        if self._names is not None:
            return self._names
        cap = self._b.mapping.max_lookup_rows
        index: dict[str, list[tuple[str, str, frozenset[str], str | None]]] = {}
        total = 0
        for node in self._b.mapping.nodes:
            if not node.lookup:
                continue
            rows = self._b.run("find_entities", self._b.names[node.table], cap=cap + 1 - total)
            total += len(rows)
            if total > cap:
                raise BackendError(KIND, "find_entities", f"the lookup node tables hold more than {cap} names; set lookup: false on large tables or raise max_lookup_rows")
            pattern = self._b.node_patterns.get(node.table)
            index[node.table] = [
                (str(r["key"]), str(r["name"]), frozenset(tokens(str(r["name"]))), document_of(pattern, r["doc"]))
                for r in rows
                if r["name"]
            ]
        self._names = index
        return index

    def _node_entity(self, node: NodeMap, key: str, name: str, doc: str | None) -> Entity:
        entity_id = f"{node.table}:{key}"
        return Entity(
            id=entity_id,
            name=name,
            type=node.type,
            span=Span(document_id=doc or f"external:{node.table}", chunk_id=entity_id, field=f"{node.table}.{node.name}", start=0, end=len(name), text=name, match_level="exact"),
        )

    def _eligible_entity(self, gen: GenerationId, entity: Entity, scope: Scope) -> bool:
        return bool(self.mentions(gen, [entity.id], scope, 1))

    def mentions(self, gen: GenerationId, entity_ids: Sequence[str], scope: Scope | None, limit: int) -> list[Entity]:
        """Self-mentions of document nodes (their stored ``mention_text``) first, then the quotes of
        eligible incident edges in mapping order and store order, at most ``limit``."""
        self._b.require_generation(gen, "mentions")
        eligible = scope_params(scope, "mentions")
        ids = list(dict.fromkeys(entity_ids))
        if not ids or eligible is None or limit <= 0:
            return []
        grouped = self._by_table(ids)
        out: list[Entity] = []
        names: dict[str, tuple[str, str]] = {}
        for table, keys in grouped.items():
            node = self._b.node_maps[table]
            pattern = self._b.node_patterns.get(table)
            for row in self._b.run("mentions", self._b.node_by_key[table], keys=keys):
                entity_id = f"{table}:{row['key']}"
                names[entity_id] = (str(row["name"] or row["key"]), node.type)
                doc, text = document_of(pattern, row["doc"]), row.get("text")
                if node.mention_text and doc and text and self._admits(eligible, doc):
                    out.append(Entity(id=entity_id, name=names[entity_id][0], type=node.type, span=Span(document_id=doc, chunk_id=entity_id, field=f"{table}.{node.mention_text}", start=0, end=len(text), text=str(text), match_level="exact")))
        for query in self._b.edge_queries:
            if len(out) >= limit:
                break
            for side, statement, table, key_name in (("out", query.out, query.source.table, "skey"), ("in", query.into, query.target.table, "okey")):
                if table not in grouped:
                    continue
                for row in self._rows(query, statement, grouped[table], eligible, limit, "mentions"):
                    entity_id = f"{table}:{row[key_name]}"
                    if entity_id not in names:
                        continue
                    edge = self._edge(query, row)
                    out.append(Entity(id=entity_id, name=names[entity_id][0], type=names[entity_id][1], span=edge.span))
                    if len(out) >= limit:
                        break
        return out[:limit]

    @staticmethod
    def _admits(eligible: dict[str, Any], doc: str) -> bool:
        return (eligible["all_docs"] or doc in eligible["allow"]) and doc not in eligible["exclude"]

    def memberships(self, gen: GenerationId) -> list[Membership]:
        self._b.require_generation(gen, "memberships")
        self._load_communities()
        return list(self._memberships or [])

    def communities(self, gen: GenerationId) -> list[Community]:
        self._b.require_generation(gen, "communities")
        self._load_communities()
        return list(self._communities or [])

    def _load_communities(self) -> None:
        """Communities of an attached store are immutable: read once (bounded by the store's
        community count, not by the corpus) and kept for the life of the backend."""
        mapping = self._b.mapping.communities
        if self._communities is not None:
            return
        if mapping is None:
            self._communities, self._memberships = [], []
            return
        quote_pattern = DocRule(mapping.quote_document_pattern, mapping.quote_document_template)
        heads = {r["key"]: {**r, "quote_doc": document_of(quote_pattern, r["quote_source"])} for r in self._b.run("communities", self._b.community_heads)}
        docs: dict[str, set[str]] = {key: set() for key in heads}
        entities: dict[str, set[str]] = {key: set() for key in heads}
        memberships: list[Membership] = []
        for table, statement in self._b.community_members:
            pattern = self._b.node_patterns.get(table)
            for row in self._b.run("communities", statement):
                community = row["community"]
                if community not in heads:
                    continue
                doc = document_of(pattern, row["doc"])
                if doc:
                    if doc not in docs[community]:
                        memberships.append(Membership(community_id=community, member_id=doc, basis="declared"))
                    docs[community].add(doc)
                else:
                    entities[community].add(f"{table}:{row['member']}")
        communities = []
        for key, head in sorted(heads.items()):
            label = next((str(head[f"label{i}"]) for i in range(len(mapping.label)) if head.get(f"label{i}")), key)
            claims = []
            if head.get("summary") and head.get("quote") and head.get("quote_doc"):
                claims.append({"text": str(head["summary"]), "evidence": [{"document_id": head["quote_doc"], "quote": str(head["quote"])}]})
            report = {"status": "external", "claims": claims, "level": head.get("level"), "summary_unverified": head.get("summary")}
            communities.append(Community(id=key, label=label, document_ids=sorted(docs[key]), entity_ids=sorted(entities[key]), report=report))
        self._communities, self._memberships = communities, memberships

    def ontology(self) -> GraphOntology:
        return self._b.ontology


class KuzuAttachedBackend:
    """An existing Kuzu database, opened read-only and mapped onto the ``graph`` contract."""

    kind = KIND

    def __init__(self, path: str | FsPath, mapping: AttachMapping, timeout_seconds: float = 30.0, buffer_pool_mb: int = 512, clock: Callable[[], float] = time.time):
        self.mapping = mapping
        self.clock = clock
        self.session = KuzuSession(FsPath(path), True, timeout_seconds, buffer_pool_mb, None)
        catalog = _Catalog(self.session)
        self.node_maps: dict[str, NodeMap] = {n.table: n for n in mapping.nodes}
        statements: list[str] = []
        self.node_by_key: dict[str, str] = {}
        self.names: dict[str, str] = {}
        self.node_patterns: dict[str, DocRule] = {n.table: DocRule(n.document.pattern, n.document.template) for n in mapping.nodes if n.document}
        for node in mapping.nodes:
            catalog.require(node.table, "NODE", [node.key, node.name, node.mention_text, node.document.column if node.document else None], f"nodes[{node.table}]")
            doc = f"n.{_q(node.document.column)}" if node.document else "NULL"
            text = f"n.{_q(node.mention_text)}" if node.mention_text else "NULL"
            self.node_by_key[node.table] = (
                f"MATCH (n:{_q(node.table)}) WHERE n.{_q(node.key)} IN $keys RETURN n.{_q(node.key)} AS key, n.{_q(node.name)} AS name, "
                f"{doc} AS doc, {text} AS text ORDER BY key"
            )
            self.names[node.table] = f"MATCH (n:{_q(node.table)}) RETURN n.{_q(node.key)} AS key, n.{_q(node.name)} AS name, {doc} AS doc ORDER BY key LIMIT $cap"
            statements += [self.node_by_key[node.table], self.names[node.table]]
        self.edge_queries: list[_EdgeQuery] = []
        predicates: dict[str, PredicateDecl] = {}
        for index, edge in enumerate(mapping.edges):
            catalog.require(edge.table, "REL", [edge.quote, edge.source, edge.verified, edge.status, *edge.provenance.model_dump(exclude={"default_extractor"}).values()], f"edges[{edge.table}]")
            pairs = [(a, b) for a, b in catalog.connections.get(edge.table, []) if a in self.node_maps and b in self.node_maps]
            if not pairs:
                raise BackendError(KIND, "attach", f"edges[{edge.table}]: no connection between mapped node tables")
            decl = predicates.setdefault(edge.predicate, PredicateDecl())
            for a, b in pairs:
                query = _EdgeQuery(index, edge, self.node_maps[a], self.node_maps[b], mapping.require_verified)
                self.edge_queries.append(query)
                statements += query.statements()
                if self.node_maps[a].type not in decl.subject_types:
                    decl.subject_types.append(self.node_maps[a].type)
                if self.node_maps[b].type not in decl.object_types:
                    decl.object_types.append(self.node_maps[b].type)
        for index, query in enumerate(self.edge_queries):
            query.index = index
        self.ontology = GraphOntology(entity_types=[n.type for n in mapping.nodes], predicates=predicates)
        self.community_heads = ""
        self.community_members: list[tuple[str, str]] = []
        cm = mapping.communities
        if cm is not None:
            catalog.require(cm.table, "NODE", [cm.key, *cm.label, cm.level, cm.summary, cm.quote, cm.quote_source], "communities")
            if catalog.tables.get(cm.member_edge) != "REL":
                raise BackendError(KIND, "attach", f"communities: relationship table {cm.member_edge!r} does not exist")
            labels = ", ".join(f"c.{_q(col)} AS label{i}" for i, col in enumerate(cm.label))
            quote_source = f"c.{_q(cm.quote_source)}" if cm.quote_source else "NULL"
            self.community_heads = (
                f"MATCH (c:{_q(cm.table)}) RETURN c.{_q(cm.key)} AS key, {labels}, "
                f"{'c.' + _q(cm.level) if cm.level else 'NULL'} AS level, {'c.' + _q(cm.summary) if cm.summary else 'NULL'} AS summary, "
                f"{'c.' + _q(cm.quote) if cm.quote else 'NULL'} AS quote, {quote_source} AS quote_source ORDER BY key"
            )
            statements.append(self.community_heads)
            members = {a for a, b in catalog.connections.get(cm.member_edge, []) if b == cm.table}
            for node in mapping.nodes:
                if node.table not in members:
                    continue
                doc = f"m.{_q(node.document.column)}" if node.document else "NULL"
                statement = (
                    f"MATCH (m:{_q(node.table)})-[:{_q(cm.member_edge)}]->(c:{_q(cm.table)}) RETURN c.{_q(cm.key)} AS community, "
                    f"m.{_q(node.key)} AS member, {doc} AS doc"
                )
                self.community_members.append((node.table, statement))
                statements.append(statement)
        self.session.allowed = frozenset(statements)
        self.graph = AttachedGraph(self)

    @classmethod
    def from_config(cls, config: Any, clock: Callable[[], float] = time.time) -> KuzuAttachedBackend:
        return cls(config.path, AttachMapping.load(config.attach.mapping), config.timeout_seconds, config.buffer_pool_mb, clock)

    def run(self, operation: str, statement: str, **params: Any) -> list[dict[str, Any]]:
        return self.session.run(operation, statement, **params)

    def require_generation(self, gen: GenerationId, operation: str) -> None:
        if gen.dataset != self.mapping.dataset or not (self.mapping.any_generation or gen.generation in self.mapping.generations):
            raise BackendError(KIND, operation, f"unknown generation {gen.prefix()} (the attached store answers for dataset {self.mapping.dataset!r} and its declared generations)")

    def generation_identity(self, gen: GenerationId) -> dict[str, Any]:
        return {"path": str(self.session.path), "mode": "attach", "dataset": self.mapping.dataset}

    def delete_generation(self, gen: GenerationId) -> None:
        raise BackendError(KIND, "delete_generation", "the kuzu backend is attached read-only (mode: attach)")

    def probe(self) -> dict[str, Any]:
        return self.ping()

    def ping(self) -> dict[str, Any]:
        kuzu = import_kuzu()
        return {
            "engine": f"kuzu/{kuzu.__version__}",
            "path": str(self.session.path),
            "mode": "attach",
            "nodes": len(self.mapping.nodes),
            "edge_queries": len(self.edge_queries),
            "statements": len(self.session.allowed or ()),
        }

    def mapping_json(self) -> str:
        return json.dumps(self.mapping.model_dump(mode="json"), sort_keys=True)

    def close(self) -> None:
        self.session.close()

