"""Fixed, parameterised Cypher for the native kuzu ``GraphStore`` facet. **No query text is ever built
from request data**: every statement is a module constant, or one entry of a table precomputed at
import time from the closed ``EdgeKind`` set (Kuzu cannot parameterise table names). Values travel
only as parameters; the hop bound is enforced by the adapter as a validated integer and expanded one
hop per statement.

Layout (fixed tables in one embedded database; the generation scope is the ``g`` property,
``<namespace>|<dataset>|<generation>``, and every primary key is prefixed with it, so one database
holds many namespaces and generations side by side):

* ``SuGraphGeneration(g)`` — one row per committed load, written in the load's own transaction;
* ``SuGraphPointer(pointer)`` — ``<namespace>|<dataset>`` -> active generation;
* ``SuGraphEntity(id = g|key)`` — one node per canonical entity; one relationship table per edge kind
  (``SU_FACT``, ``SU_CO_OCCURRENCE``, ...) between entities, carrying predicate, kind, support,
  status, the span, the provenance and every occurrence (JSON) and a load sequence ``seq``;
* ``SuGraphMention(id = g|mid)`` — one row per span-anchored mention (``mid`` = hash of key + span);
* ``SuGraphMembership(id = g|mkey)`` and ``SuGraphCommunity(id = g|community id)``.
"""

from __future__ import annotations

from itertools import combinations
from typing import get_args

from ...core.types import EdgeKind

KIND = "kuzu"
BATCH = 500

REL_TABLES: dict[str, str] = {
    "fact": "SU_FACT",
    "co_occurrence": "SU_CO_OCCURRENCE",
    "mention": "SU_MENTION",
    "native_link": "SU_NATIVE_LINK",
    "derived": "SU_DERIVED",
    "co_firing": "SU_CO_FIRING",
}
assert set(REL_TABLES) == set(get_args(EdgeKind)), "every EdgeKind needs a fixed relationship table"

_REL_COLUMNS = (
    "rid STRING, g STRING, subject STRING, object STRING, predicate STRING, edge_kind STRING, support STRING, "
    "status STRING, document_id STRING, chunk_id STRING, field STRING, span_start INT64, span_end INT64, "
    "quote STRING, match_level STRING, provenance STRING, occurrences STRING, seq INT64"
)

SCHEMA: tuple[str, ...] = (
    "CREATE NODE TABLE IF NOT EXISTS SuGraphGeneration(g STRING, namespace STRING, dataset STRING, generation STRING, "
    "index_format INT64, fingerprint STRING, seq INT64, state STRING, loaded_at DOUBLE, PRIMARY KEY(g))",
    "CREATE NODE TABLE IF NOT EXISTS SuGraphPointer(pointer STRING, generation STRING, g STRING, activated_at DOUBLE, PRIMARY KEY(pointer))",
    "CREATE NODE TABLE IF NOT EXISTS SuGraphEntity(id STRING, g STRING, key STRING, PRIMARY KEY(id))",
    "CREATE NODE TABLE IF NOT EXISTS SuGraphMention(id STRING, g STRING, mid STRING, key STRING, name STRING, etype STRING, "
    "canonical_id STRING, aliases STRING[], document_id STRING, chunk_id STRING, field STRING, span_start INT64, "
    "span_end INT64, quote STRING, match_level STRING, first_token STRING, tokens STRING[], seq INT64, PRIMARY KEY(id))",
    "CREATE NODE TABLE IF NOT EXISTS SuGraphMembership(id STRING, g STRING, community_id STRING, member_id STRING, role STRING, "
    "basis STRING, seq INT64, PRIMARY KEY(id))",
    "CREATE NODE TABLE IF NOT EXISTS SuGraphCommunity(id STRING, g STRING, cid STRING, stored BOOLEAN, label STRING, "
    "document_ids STRING[], entity_ids STRING[], report STRING, PRIMARY KEY(id))",
    *(f"CREATE REL TABLE IF NOT EXISTS {table}(FROM SuGraphEntity TO SuGraphEntity, {_REL_COLUMNS})" for table in REL_TABLES.values()),
)

# --- generation and pointer -----------------------------------------------------------------------

GENERATION_STATE = "MATCH (n:SuGraphGeneration) WHERE n.g = $g RETURN n.state AS state, n.seq AS seq"

GENERATION_OPEN = """
MERGE (n:SuGraphGeneration {g: $g})
ON CREATE SET n.namespace = $namespace, n.dataset = $dataset, n.generation = $generation,
              n.index_format = $index_format, n.fingerprint = $fingerprint, n.seq = 0
RETURN n.seq AS seq
"""

GENERATION_CLOSE = "MATCH (n:SuGraphGeneration) WHERE n.g = $g SET n.seq = $seq, n.state = 'loaded', n.loaded_at = $now"

GENERATION_LOADED = "MATCH (n:SuGraphGeneration) WHERE n.g = $g AND n.state = 'loaded' RETURN count(n) AS loaded"

POINTER_READ = "MATCH (p:SuGraphPointer) WHERE p.pointer = $pointer RETURN p.generation AS generation, p.g AS g"

POINTER_WRITE = "MERGE (p:SuGraphPointer {pointer: $pointer}) SET p.generation = $generation, p.g = $g, p.activated_at = $now"

POINTER_DEACTIVATE = "MATCH (p:SuGraphPointer) WHERE p.pointer = $pointer AND p.g = $g DELETE p"

GENERATIONS_LIST = (
    "MATCH (n:SuGraphGeneration) WHERE n.namespace = $namespace RETURN n.dataset AS dataset, n.generation AS generation "
    "ORDER BY dataset, generation"
)

GENERATION_DROP = "MATCH (n:SuGraphGeneration) WHERE n.g = $g DELETE n"
DATA_DROP: tuple[str, ...] = (
    "MATCH (n:SuGraphMention) WHERE n.g = $g DELETE n",
    "MATCH (n:SuGraphMembership) WHERE n.g = $g DELETE n",
    "MATCH (n:SuGraphCommunity) WHERE n.g = $g DELETE n",
    "MATCH (n:SuGraphEntity) WHERE n.g = $g DETACH DELETE n",
)
NAMESPACE_DROP: tuple[str, ...] = (
    "MATCH (n:SuGraphMention) WHERE starts_with(n.g, $prefix) DELETE n",
    "MATCH (n:SuGraphMembership) WHERE starts_with(n.g, $prefix) DELETE n",
    "MATCH (n:SuGraphCommunity) WHERE starts_with(n.g, $prefix) DELETE n",
    "MATCH (n:SuGraphEntity) WHERE starts_with(n.g, $prefix) DETACH DELETE n",
    "MATCH (n:SuGraphGeneration) WHERE starts_with(n.g, $prefix) DELETE n",
    "MATCH (p:SuGraphPointer) WHERE starts_with(p.pointer, $prefix) DELETE p",
)

# --- load -----------------------------------------------------------------------------------------

ENTITIES_MERGE = "UNWIND $rows AS row MERGE (e:SuGraphEntity {id: row.id}) SET e.g = $g, e.key = row.key"

MENTIONS_MERGE = """
UNWIND $rows AS row
MERGE (m:SuGraphMention {id: row.id})
ON CREATE SET m.seq = row.seq
SET m.g = $g, m.mid = row.mid, m.key = row.key, m.name = row.name, m.etype = row.etype,
    m.canonical_id = row.canonical_id, m.aliases = row.aliases, m.document_id = row.document_id,
    m.chunk_id = row.chunk_id, m.field = row.field, m.span_start = row.span_start, m.span_end = row.span_end,
    m.quote = row.quote, m.match_level = row.match_level, m.first_token = row.first_token, m.tokens = row.tokens
"""

KNOWN_IDS = "MATCH (e:SuGraphEntity) WHERE e.id IN $ids RETURN e.key AS key"

_RELATION_MERGE = """
UNWIND $rows AS row
MATCH (s:SuGraphEntity {id: row.sid}), (o:SuGraphEntity {id: row.oid})
MERGE (s)-[r:%s {rid: row.rid}]->(o)
ON CREATE SET r.seq = row.seq
SET r.g = $g, r.subject = row.subject, r.object = row.object, r.predicate = row.predicate,
    r.edge_kind = row.edge_kind, r.support = row.support, r.status = row.status, r.document_id = row.document_id,
    r.chunk_id = row.chunk_id, r.field = row.field, r.span_start = row.span_start, r.span_end = row.span_end,
    r.quote = row.quote, r.match_level = row.match_level, r.provenance = row.provenance, r.occurrences = row.occurrences
RETURN count(r) AS written
"""
RELATION_MERGE: dict[str, str] = {kind: _RELATION_MERGE % table for kind, table in REL_TABLES.items()}

MEMBERSHIPS_MERGE = """
UNWIND $rows AS row
MERGE (m:SuGraphMembership {id: row.id})
ON CREATE SET m.seq = row.seq
SET m.g = $g, m.community_id = row.community_id, m.member_id = row.member_id, m.role = row.role, m.basis = row.basis
"""

COMMUNITIES_MERGE = """
UNWIND $rows AS row
MERGE (c:SuGraphCommunity {id: row.id})
SET c.g = $g, c.cid = row.cid, c.stored = true, c.label = row.label, c.document_ids = row.document_ids,
    c.entity_ids = row.entity_ids, c.report = row.report
"""

# --- reads ----------------------------------------------------------------------------------------

# Eligibility: the document-id allow-list (``$all_docs`` = no allow-list) and deny-list, as parameters.
_ELIGIBLE = "($all_docs OR {x}.document_id IN $allow) AND NOT {x}.document_id IN $exclude"

_EDGE_COLUMNS = (
    "r.subject AS subject, r.object AS object, r.predicate AS predicate, r.edge_kind AS edge_kind, r.support AS support, "
    "r.status AS status, r.document_id AS document_id, r.chunk_id AS chunk_id, r.field AS field, "
    "r.span_start AS span_start, r.span_end AS span_end, r.quote AS quote, r.match_level AS match_level, "
    "r.provenance AS provenance, r.occurrences AS occurrences, r.seq AS seq, r.rid AS rid"
)

_HOP = (
    "MATCH (s:SuGraphEntity)-[r:%s]->(o:SuGraphEntity) WHERE {side}.id IN $nodes AND r.g = $g "
    "AND ($any_predicate OR r.predicate IN $predicates) AND "
    + _ELIGIBLE.format(x="r")
    + " RETURN "
    + _EDGE_COLUMNS
    + " ORDER BY seq, rid"
)


def _hop_templates(side: str) -> dict[frozenset[str], str]:
    kinds = sorted(REL_TABLES)
    table: dict[frozenset[str], str] = {}
    for size in range(1, len(kinds) + 1):
        for subset in combinations(kinds, size):
            table[frozenset(subset)] = _HOP.replace("{side}", side) % "|".join(REL_TABLES[k] for k in subset)
    return table


HOP_OUT: dict[frozenset[str], str] = _hop_templates("s")
"""Edges leaving the frontier; one fixed template per set of requested edge kinds, so a fact
traversal can only read ``SU_FACT`` and co-occurrence is structurally out of reach."""
HOP_IN: dict[frozenset[str], str] = _hop_templates("o")

_MENTION_COLUMNS = (
    "m.key AS key, m.name AS name, m.etype AS etype, m.canonical_id AS canonical_id, m.aliases AS aliases, "
    "m.document_id AS document_id, m.chunk_id AS chunk_id, m.field AS field, m.span_start AS span_start, "
    "m.span_end AS span_end, m.quote AS quote, m.match_level AS match_level, m.tokens AS tokens, m.seq AS seq"
)

FIND_ENTITIES = (
    "MATCH (m:SuGraphMention) WHERE m.g = $g AND m.first_token IN $wanted AND "
    + _ELIGIBLE.format(x="m")
    + " RETURN "
    + _MENTION_COLUMNS
    + " ORDER BY key, seq"
)

MENTIONS = (
    "MATCH (m:SuGraphMention) WHERE m.g = $g AND m.key IN $keys AND "
    + _ELIGIBLE.format(x="m")
    + " RETURN "
    + _MENTION_COLUMNS
    + " ORDER BY seq LIMIT $limit"
)

MEMBERSHIPS = (
    "MATCH (m:SuGraphMembership) WHERE m.g = $g RETURN m.community_id AS community_id, m.member_id AS member_id, "
    "m.role AS role, m.basis AS basis ORDER BY m.seq, m.id"
)

COMMUNITIES = (
    "MATCH (c:SuGraphCommunity) WHERE c.g = $g AND c.stored = true RETURN c.cid AS id, c.label AS label, "
    "c.document_ids AS document_ids, c.entity_ids AS entity_ids, c.report AS report ORDER BY id"
)

ALL_TEMPLATES: tuple[str, ...] = (
    *SCHEMA,
    GENERATION_STATE,
    GENERATION_OPEN,
    GENERATION_CLOSE,
    GENERATION_LOADED,
    POINTER_READ,
    POINTER_WRITE,
    POINTER_DEACTIVATE,
    GENERATIONS_LIST,
    GENERATION_DROP,
    *DATA_DROP,
    *NAMESPACE_DROP,
    ENTITIES_MERGE,
    MENTIONS_MERGE,
    KNOWN_IDS,
    *RELATION_MERGE.values(),
    MEMBERSHIPS_MERGE,
    COMMUNITIES_MERGE,
    *HOP_OUT.values(),
    *HOP_IN.values(),
    FIND_ENTITIES,
    MENTIONS,
    MEMBERSHIPS,
    COMMUNITIES,
)
"""Every statement the native adapter may send; the unit tests assert nothing else is ever executed."""
