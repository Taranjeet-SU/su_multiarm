"""Fixed, parameterised Cypher for the neo4j ``GraphStore`` facet. **No query text is ever built from
request data**: every statement below is a module constant (or, for relationship types, which Cypher
cannot parameterise, one entry of a dictionary precomputed at import time from the closed
``EdgeKind`` set). Values (generation key, seeds, predicates, document-id allow/deny lists, limits)
travel only as driver parameters; the hop bound is enforced by the adapter as a validated integer and
expanded one hop per statement, so it never appears in query text either.

Layout (fixed labels; generation scope is the ``g`` property, ``<namespace>|<dataset>|<generation>``,
so one database can hold many namespaces and generations side by side):

* ``(:SuGraphGeneration {g})``: one per loaded generation, written in the same transaction as the
  data it describes, so it exists only for committed loads;
* ``(:SuGraphPointer {pointer})``: ``<namespace>|<dataset>`` -> active generation, replaced by a
  single statement (atomic) and only by ``activate_generation``;
* ``(:SuGraphEntity {g, key})``: one node per canonical entity; edges between entities use one
  relationship type per edge kind (``SU_FACT``, ``SU_CO_OCCURRENCE``, ...) carrying ``predicate``,
  ``edge_kind``, ``support``, the span (document, chunk, field, offsets, quote, match level), the
  provenance and every occurrence (JSON), and a load sequence ``seq``;
* ``(:SuGraphMention {g, mid})``: one node per span-anchored mention (``mid`` = hash of key + span);
* ``(:SuGraphCommunity {g, id})`` with report/document/entity lists, and
  ``(:SuGraphMembership {g, mkey})-[:SU_GRAPH_MEMBER_OF]->(:SuGraphCommunity)``.
"""

from __future__ import annotations

from itertools import combinations
from typing import get_args

from ...core.types import EdgeKind

KIND = "neo4j"
BATCH = 500

REL_TYPES: dict[str, str] = {
    "fact": "SU_FACT",
    "co_occurrence": "SU_CO_OCCURRENCE",
    "mention": "SU_MENTION",
    "native_link": "SU_NATIVE_LINK",
    "derived": "SU_DERIVED",
}
assert set(REL_TYPES) == set(get_args(EdgeKind)), "every EdgeKind needs a fixed relationship type"

SCHEMA: tuple[str, ...] = (
    "CREATE CONSTRAINT su_graph_generation IF NOT EXISTS FOR (n:SuGraphGeneration) REQUIRE n.g IS UNIQUE",
    "CREATE CONSTRAINT su_graph_pointer IF NOT EXISTS FOR (n:SuGraphPointer) REQUIRE n.pointer IS UNIQUE",
    "CREATE CONSTRAINT su_graph_entity IF NOT EXISTS FOR (n:SuGraphEntity) REQUIRE (n.g, n.key) IS UNIQUE",
    "CREATE CONSTRAINT su_graph_mention IF NOT EXISTS FOR (n:SuGraphMention) REQUIRE (n.g, n.mid) IS UNIQUE",
    "CREATE CONSTRAINT su_graph_community IF NOT EXISTS FOR (n:SuGraphCommunity) REQUIRE (n.g, n.id) IS UNIQUE",
    "CREATE CONSTRAINT su_graph_membership IF NOT EXISTS FOR (n:SuGraphMembership) REQUIRE (n.g, n.mkey) IS UNIQUE",
    "CREATE INDEX su_graph_mention_token IF NOT EXISTS FOR (n:SuGraphMention) ON (n.g, n.first_token)",
    "CREATE INDEX su_graph_mention_key IF NOT EXISTS FOR (n:SuGraphMention) ON (n.g, n.key)",
)

# --- generation and pointer -----------------------------------------------------------------------

GENERATION_STATE = "MATCH (n:SuGraphGeneration {g: $g}) RETURN n.state AS state, n.seq AS seq"

GENERATION_OPEN = """
MERGE (n:SuGraphGeneration {g: $g})
ON CREATE SET n.namespace = $namespace, n.dataset = $dataset, n.generation = $generation,
              n.index_format = $index_format, n.fingerprint = $fingerprint, n.seq = 0
RETURN n.seq AS seq
"""

GENERATION_CLOSE = "MATCH (n:SuGraphGeneration {g: $g}) SET n.seq = $seq, n.state = 'loaded', n.loaded_at = $now"

POINTER_READ = "MATCH (p:SuGraphPointer {pointer: $pointer}) RETURN p.generation AS generation, p.g AS g"

# One statement: when the generation node is missing, MATCH yields no row and nothing is written.
POINTER_ACTIVATE = """
MATCH (n:SuGraphGeneration {g: $g}) WHERE n.state = 'loaded'
MERGE (p:SuGraphPointer {pointer: $pointer})
SET p.generation = $generation, p.g = $g, p.activated_at = $now
RETURN p.generation AS generation
"""

POINTER_DEACTIVATE = "MATCH (p:SuGraphPointer {pointer: $pointer}) WHERE p.g = $g DELETE p RETURN count(*) AS removed"

GENERATIONS_LIST = (
    "MATCH (n:SuGraphGeneration) WHERE n.namespace = $namespace RETURN n.dataset AS dataset, n.generation AS generation "
    "ORDER BY dataset, generation"
)

# Delete the generation node first (in its own transaction, after the pointer check), so a half-
# deleted generation is unknown to every reader and can never be activated; then the data.
GENERATION_DROP = "MATCH (n:SuGraphGeneration {g: $g}) DELETE n RETURN count(*) AS removed"
DATA_DROP: tuple[str, ...] = tuple(
    f"MATCH (n:{label} {{g: $g}}) CALL (n) {{ DETACH DELETE n }} IN TRANSACTIONS OF 2000 ROWS"
    for label in ("SuGraphMention", "SuGraphMembership", "SuGraphCommunity", "SuGraphEntity")
)
NAMESPACE_DROP: tuple[str, ...] = tuple(
    f"MATCH (n:{label}) WHERE n.g STARTS WITH $prefix CALL (n) {{ DETACH DELETE n }} IN TRANSACTIONS OF 2000 ROWS"
    for label in ("SuGraphMention", "SuGraphMembership", "SuGraphCommunity", "SuGraphEntity", "SuGraphGeneration")
) + ("MATCH (p:SuGraphPointer) WHERE p.pointer STARTS WITH $prefix DELETE p",)

# --- load -----------------------------------------------------------------------------------------

MENTIONS_MERGE = """
UNWIND $rows AS row
MERGE (e:SuGraphEntity {g: $g, key: row.key})
MERGE (m:SuGraphMention {g: $g, mid: row.mid})
ON CREATE SET m.seq = row.seq
SET m.key = row.key, m.name = row.name, m.type = row.type, m.canonical_id = row.canonical_id,
    m.aliases = row.aliases, m.document_id = row.document_id, m.chunk_id = row.chunk_id,
    m.field = row.field, m.span_start = row.span_start, m.span_end = row.span_end, m.quote = row.quote,
    m.match_level = row.match_level, m.first_token = row.first_token, m.tokens = row.tokens
"""

MISSING_ENDPOINTS = """
UNWIND $keys AS k
OPTIONAL MATCH (e:SuGraphEntity {g: $g, key: k})
WITH k, e WHERE e IS NULL
RETURN k ORDER BY k LIMIT 5
"""

_RELATION_MERGE = """
UNWIND $rows AS row
MATCH (s:SuGraphEntity {g: $g, key: row.subject}), (o:SuGraphEntity {g: $g, key: row.object})
MERGE (s)-[r:%s {rid: row.rid}]->(o)
ON CREATE SET r.seq = row.seq
SET r.g = $g, r.predicate = row.predicate, r.edge_kind = row.edge_kind, r.support = row.support,
    r.status = row.status, r.document_id = row.document_id, r.chunk_id = row.chunk_id, r.field = row.field,
    r.span_start = row.span_start, r.span_end = row.span_end, r.quote = row.quote,
    r.match_level = row.match_level, r.provenance = row.provenance, r.occurrences = row.occurrences
RETURN count(r) AS written
"""
RELATION_MERGE: dict[str, str] = {kind: _RELATION_MERGE % rel for kind, rel in REL_TYPES.items()}

MEMBERSHIPS_MERGE = """
UNWIND $rows AS row
MERGE (c:SuGraphCommunity {g: $g, id: row.community_id})
ON CREATE SET c.stored = false
MERGE (m:SuGraphMembership {g: $g, mkey: row.mkey})
ON CREATE SET m.seq = row.seq
SET m.community_id = row.community_id, m.member_id = row.member_id, m.role = row.role, m.basis = row.basis
MERGE (m)-[:SU_GRAPH_MEMBER_OF]->(c)
"""

COMMUNITIES_MERGE = """
UNWIND $rows AS row
MERGE (c:SuGraphCommunity {g: $g, id: row.id})
SET c.stored = true, c.label = row.label, c.document_ids = row.document_ids, c.entity_ids = row.entity_ids,
    c.report = row.report
"""

# --- reads ----------------------------------------------------------------------------------------

# Eligibility: the document-id allow-list (``$all_docs`` = no allow-list) and deny-list, as parameters.
_ELIGIBLE = "($all_docs OR {x}.document_id IN $allow) AND NOT {x}.document_id IN $exclude"

KNOWN_ENTITIES = "MATCH (e:SuGraphEntity {g: $g}) WHERE e.key IN $keys RETURN e.key AS key"

_HOP = (
    """
MATCH (n:SuGraphEntity {g: $g}) WHERE n.key IN $nodes
MATCH (n)-[r:%s]-(:SuGraphEntity)
WHERE r.g = $g AND (size($predicates) = 0 OR r.predicate IN $predicates) AND """
    + _ELIGIBLE.format(x="r")
    + """
WITH DISTINCT r
RETURN startNode(r).key AS subject, endNode(r).key AS object, r.predicate AS predicate,
       r.edge_kind AS edge_kind, r.support AS support, r.status AS status, r.document_id AS document_id,
       r.chunk_id AS chunk_id, r.field AS field, r.span_start AS span_start, r.span_end AS span_end,
       r.quote AS quote, r.match_level AS match_level, r.provenance AS provenance,
       r.occurrences AS occurrences, r.seq AS seq, r.rid AS rid
ORDER BY seq, rid
"""
)


def _hop_templates() -> dict[frozenset[str], str]:
    kinds = sorted(REL_TYPES)
    table: dict[frozenset[str], str] = {}
    for size in range(1, len(kinds) + 1):
        for subset in combinations(kinds, size):
            table[frozenset(subset)] = _HOP % "|".join(REL_TYPES[k] for k in subset)
    return table


HOP: dict[frozenset[str], str] = _hop_templates()
"""One fixed template per set of requested edge kinds; ``frozenset({'fact'})`` can only match
``SU_FACT`` relationships, so co-occurrence is structurally out of reach of a fact traversal."""

FIND_ENTITIES = (
    """
MATCH (m:SuGraphMention {g: $g})
WHERE m.first_token IN $wanted AND size(m.tokens) > 0 AND all(t IN m.tokens WHERE t IN $wanted) AND """
    + _ELIGIBLE.format(x="m")
    + """
RETURN m ORDER BY m.key, m.seq
"""
)

MENTIONS = (
    """
MATCH (m:SuGraphMention {g: $g})
WHERE m.key IN $keys AND """
    + _ELIGIBLE.format(x="m")
    + """
RETURN m ORDER BY m.seq LIMIT $limit
"""
)

MEMBERSHIPS = (
    "MATCH (m:SuGraphMembership {g: $g}) RETURN m.community_id AS community_id, m.member_id AS member_id, "
    "m.role AS role, m.basis AS basis ORDER BY m.seq, m.mkey"
)

COMMUNITIES = (
    "MATCH (c:SuGraphCommunity {g: $g}) WHERE c.stored = true RETURN c.id AS id, c.label AS label, "
    "c.document_ids AS document_ids, c.entity_ids AS entity_ids, c.report AS report ORDER BY id"
)

ALL_TEMPLATES: tuple[str, ...] = (
    *SCHEMA,
    GENERATION_STATE,
    GENERATION_OPEN,
    GENERATION_CLOSE,
    POINTER_READ,
    POINTER_ACTIVATE,
    POINTER_DEACTIVATE,
    GENERATIONS_LIST,
    GENERATION_DROP,
    *DATA_DROP,
    *NAMESPACE_DROP,
    MENTIONS_MERGE,
    MISSING_ENDPOINTS,
    *RELATION_MERGE.values(),
    MEMBERSHIPS_MERGE,
    COMMUNITIES_MERGE,
    KNOWN_ENTITIES,
    *HOP.values(),
    FIND_ENTITIES,
    MENTIONS,
    MEMBERSHIPS,
    COMMUNITIES,
)
"""Every statement the adapter may send; the unit tests assert nothing else is ever executed."""
