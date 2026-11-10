"""R1 support-stratum equality (unified merge M1b, §6.5, §10.3): the adopted support member is one
statistics/candidate unit served by today's code path over today's files, so the unified engine and
today's monolithic engine over the same store return identical candidate lists and scores.

Synthetic (``tests/adopted_support.py``): the store is built by a v2 configuration with lexical
(bm25), dense (hash embedder), structured, community (imported, claim hits) and segment arms; the
unified configuration adopts it read-only with the community file and the segment sidecar as
attached layers. Compared per query: every arm's candidate list (ids, order, scores, chunk ids,
provenance), the fused hits, aggregates, the answer and the evidence packed for the generator.
"""

from __future__ import annotations

import json

import pytest

from su_multi_arm import SearchRequest
from su_multi_arm.arms import ArmInput
from su_multi_arm.core.types import LexicalQuery, Page, Predicate, Scope, ScopeSpec
from tests.adopted_support import QUERIES, adopted_engine, build_original, todays_engine
from tests.partition_support import arm_hits, capture_evidence, hits


@pytest.fixture(scope="module")
def engines(tmp_path_factory):
    root = tmp_path_factory.mktemp("stratum")
    original = build_original(root)
    return adopted_engine(root, original), todays_engine(original)


def _arm_rows(result):
    return [(h.document_id, h.score, tuple(h.chunk_ids), h.text, repr(h.provenance)) for h in result.hits]


@pytest.mark.parametrize("query", QUERIES)
def test_every_arm_returns_todays_candidates_and_scores(engines, query):
    unified, today = engines
    a_ctx, b_ctx = unified._context_for(None), today._context_for(None)
    for name in ("lexical", "dense", "structured", "community", "segment"):
        inputs = ArmInput(query=query, candidates=50)
        left = unified.arms[name].run(a_ctx, inputs)
        right = today.arms[name].run(b_ctx, inputs)
        assert _arm_rows(left) == _arm_rows(right), (name, query)
        assert left.reports == right.reports, (name, query)


@pytest.mark.parametrize("query", QUERIES)
def test_the_lexical_and_dense_facets_rank_identically(engines, query):
    unified, today = engines
    a, b = unified.backends["local"], today.backends["local"]
    ga, gb = unified.write_path.pin()[0], today.write_path.pin()[0]
    lex = LexicalQuery(kind="terms", text=query)
    left = a.lexical.search(ga, lex, None, 40, Page(size=40)).items
    right = b.lexical.search(gb, lex, None, 40, Page(size=40)).items
    assert [(h.chunk_id, h.document_id, h.score) for h in left] == [(h.chunk_id, h.document_id, h.score) for h in right]
    vector = today.embedder.encode([query], query=True)[0]
    assert [(h.chunk_id, h.score) for h in a.vector.search(ga, vector, None, 40)] == [(h.chunk_id, h.score) for h in b.vector.search(gb, vector, None, 40)]
    scope = Scope(predicates=[Predicate(field="source", op="eq", value="doc")])
    left = a.lexical.search(ga, lex, scope, 40, Page(size=40)).items
    right = b.lexical.search(gb, lex, scope, 40, Page(size=40)).items
    assert [(h.chunk_id, h.score) for h in left] == [(h.chunk_id, h.score) for h in right]


def test_search_hits_aggregates_and_packed_evidence_are_identical(engines):
    unified, today = engines
    left_packed, right_packed = capture_evidence(unified), capture_evidence(today)
    for query in [*QUERIES, "how many tickets per status", "count tickets by service"]:
        request = SearchRequest(query=query)
        a, b = unified.search(request), today.search(request)
        assert hits(a) == hits(b), query
        assert arm_hits(a) == arm_hits(b), query
        # the aggregate lineage stamps the generation the request pinned (the unified one vs the store's)
        assert [{k: v for k, v in x.items() if k != "generation"} for x in a.aggregates] == [{k: v for k, v in x.items() if k != "generation"} for x in b.aggregates], query
        answer_a, answer_b = unified.answer(query), today.answer(query)
        assert hits(answer_a.retrieval) == hits(answer_b.retrieval)
        assert answer_a.answer.model_dump() == answer_b.answer.model_dump()
    # identical but for the generation id and fingerprint an aggregate's lineage stamps (the pinned
    # unified generation vs the store's own)
    ours, theirs = unified.write_path.pin()[0], today.write_path.pin()[0]
    packed = json.dumps(left_packed, sort_keys=True)
    aggregates = sum(1 for rows in left_packed for row in rows if row["id"].startswith("aggregate:"))
    assert aggregates and packed.count(ours.generation) == aggregates
    packed = packed.replace(ours.generation, theirs.generation).replace(ours.fingerprint, theirs.fingerprint)
    assert packed == json.dumps(right_packed, sort_keys=True)


def test_the_trace_labels_the_adopted_member_and_its_layers(engines):
    unified, today = engines
    result = unified.search(SearchRequest(query=QUERIES[1]))
    coverage = result.trace["collection"]
    assert [(m["member"], m["tier"], m["status"]) for m in coverage["members"]] == [("support", "adopted", "available")]
    assert {layer["layer"]: layer["status"] for layer in coverage["layers"]} == {"support_communities": "available", "support_segments": "available"}
    assert "collection" not in today.search(SearchRequest(query=QUERIES[1])).trace


@pytest.mark.parametrize(
    "spec",
    [
        ScopeSpec(group_by=["status"]),
        ScopeSpec(group_by=["source"]),
        ScopeSpec(predicates=[Predicate(field="kind", op="eq", value="ticket")], group_by=["service"]),
    ],
)
def test_records_are_todays(engines, spec):
    unified, today = engines
    a = unified.records.execute(unified.write_path.pin()[0], spec, [])
    b = today.records.execute(today.write_path.pin()[0], spec, [])
    assert a.model_dump() == b.model_dump()


def test_the_imported_community_layer_reads_todays_communities(engines):
    from su_multi_arm.communities.base import CommunityLayerReader

    unified, today = engines
    readers = unified.backends["local"].graph.layers()
    assert [r.name for r in readers] == ["support"] and all(isinstance(r, CommunityLayerReader) for r in readers)
    ours = readers[0].communities(unified.write_path.pin()[0])
    theirs = today.backends["local"].graph.communities(today.write_path.pin()[0])
    assert ours == theirs and ours
