"""The partition-core equivalence kit (unified merge plan §6.5, §10.2; M1a).

A partitioned collection must serve exactly what today's monolithic engine serves over the same
documents. Rollback criterion of M1a: any failure here blocks M1b and partitioned mode stays off.

1. **Single member == monolithic** (F0 sources and the shipped example corpus): identical fused
   hits (ids, order, scores, chunk ids, text, provenance), identical per-arm hits, identical answer
   evidence and identical records results.
2. **Multi-member, per-stratum**: a request scoped to one member's sources reads that member only
   and is identical to a monolithic engine over that member's documents alone (the R1/§6.5
   guarantee: support ranking unchanged by log volume) — lexical, dense and records.
3. **Query-time statistics (B10)**: the log stratum over its catalog, month and day members ranks
   exactly as one store built from the same documents (ids and order exact; scores within 1e-9 —
   the epsilon floor's average idf is summed in another order).
4. **Dense exact merge**: the exact top-k over every member equals the monolithic exact top-k.
5. **Records and listing**: per-member rows with one ``scope_result`` == the monolithic result;
   the k-way merged listing pages == the monolithic listing.

Synthetic evidence only (F0 fixture, hash embedder, sqlite bm25); no service, no model call.
"""

from __future__ import annotations

import pytest

from su_multi_arm import SearchRequest
from su_multi_arm.core.types import Page, Predicate, Scope, ScopeSpec
from tests.partition_support import (
    capture_evidence,
    close_hits,
    examples,
    f0,
    f0_members,
    hits,
    arm_hits,
    monolithic,
    partitioned,
    union,
)

QUERIES = [
    "crawler fetch timeout",
    "index write failed after deploy",
    "search 5xx errors for the tenant",
    "scheduler job halted",
    "password reset email not delivered",
    "how do I configure the crawler",
    "zero result searches",
    "render timeout crawler.render_timeout",
]


@pytest.fixture(scope="module")
def fx():
    return f0()


@pytest.fixture(scope="module")
def members(fx):
    return f0_members(fx)


def _same_search(a, b, queries, filters=None):
    for query in queries:
        request = SearchRequest(query=query, filters=filters or {})
        left, right = a.search(request), b.search(request)
        assert hits(left) == hits(right), query
        assert arm_hits(left) == arm_hits(right), query
        assert left.aggregates == right.aggregates, query


# --- 1. single member == monolithic --------------------------------------------------------------


@pytest.mark.parametrize("member", ["support", "tracker", "analytics"])
def test_a_single_member_collection_serves_todays_engine(tmp_path, fx, members, member):
    only = {member: members[member]}
    part = partitioned(tmp_path / "part", fx, only)
    mono = monolithic(tmp_path / "mono", fx, union(only))
    _same_search(part, mono, QUERIES)


def test_single_member_answer_evidence_is_identical(tmp_path, fx, members):
    only = {"support": members["support"]}
    part = partitioned(tmp_path / "part", fx, only)
    mono = monolithic(tmp_path / "mono", fx, union(only))
    left, right = capture_evidence(part), capture_evidence(mono)
    for query in QUERIES[:4]:
        a, b = part.answer(query), mono.answer(query)
        assert hits(a.retrieval) == hits(b.retrieval)
        assert a.answer.model_dump() == b.answer.model_dump()
        assert a.coverage == b.coverage and a.contract == b.contract
    assert left and left == right


def test_the_example_corpus_as_one_member(tmp_path):
    from su_multi_arm import Engine
    from tests.partition_support import config

    docs = examples()
    block = {"mode": "partitioned", "members": {"all": {"kind": "all", "sources": sorted({d.source for d in docs}), "record_kinds": sorted({d.kind for d in docs}), "id_namespaces": sorted({d.id.split(":")[0] + ":" for d in docs})}}}
    schema = {"fields": [{"name": "source", "type": "string", "listable": True, "groupable": True}, {"name": "product", "type": "string", "listable": True, "casefold_match": True}, {"name": "state", "type": "string", "groupable": True}, {"name": "status", "type": "string", "groupable": True}, {"name": "created_at", "type": "datetime", "sortable": True}]}
    part = Engine(config(tmp_path / "part", schema=schema, collection=block, dataset="demo"))
    part.build_member("all", "snapshot:2026-11-09", docs)
    part.compose()
    mono = Engine(config(tmp_path / "mono", schema=schema, dataset="demo"))
    mono.ingest(docs)
    _same_search(part, mono, ["exemption certificate", "rounding error on invoices", "content source CloudTax", "SearchDesk ERR-42"])
    _same_search(part, mono, ["exemption certificate"], filters={"product": "CloudTax"})
    spec = ScopeSpec(group_by=["status"])
    gen_p, gen_m = part.write_path.pin()[0], mono.write_path.pin()[0]
    assert part.records.execute(gen_p, spec, []).model_dump() == mono.records.execute(gen_m, spec, []).model_dump()


# --- 2. multi-member: a scoped stratum == that member alone ----------------------------------------


@pytest.fixture(scope="module")
def full(tmp_path_factory, fx, members):
    return partitioned(tmp_path_factory.mktemp("full"), fx, members)


@pytest.mark.parametrize(("source", "member"), [("ticket", "support"), ("doc", "support"), ("tracker", "tracker"), ("analytics", "analytics")])
def test_a_source_scoped_request_equals_that_member_alone(tmp_path, fx, members, full, source, member):
    mono = monolithic(tmp_path / "mono", fx, union({member: members[member]}))
    _same_search(full, mono, QUERIES, filters={"source": source})


def test_support_lexical_and_dense_are_unchanged_by_log_members(tmp_path, fx, members, full):
    """§6.5 guarantee: for a support-scoped scope the support stratum's lexical and dense
    candidates (ids, order, scores) are identical with and without log members present."""
    alone = partitioned(tmp_path / "alone", fx, {"support": members["support"]})
    backend_full, backend_alone = full.backends["local"], alone.backends["local"]
    gen_full, gen_alone = full.write_path.pin()[0], alone.write_path.pin()[0]
    scope = Scope(predicates=[Predicate(field="source", op="in", value=["ticket", "doc"])])
    from su_multi_arm.core.types import LexicalQuery

    for query in QUERIES:
        q = LexicalQuery(kind="terms", text=query)
        a = backend_full.lexical.search(gen_full, q, scope, 50).items
        b = backend_alone.lexical.search(gen_alone, q, scope, 50).items
        assert a == b, query
        vector = full.embedder.encode([query], query=True)[0]
        assert backend_full.vector.search(gen_full, vector, scope, 50) == backend_alone.vector.search(gen_alone, vector, scope, 50)


# --- 3. query-time statistics over the log stratum (B10) ----------------------------------------------


def test_the_log_stratum_ranks_like_one_store_built_from_the_same_documents(tmp_path, fx, members, full):
    logs = union({"logs": members["logs"]})
    mono = monolithic(tmp_path / "mono", fx, logs, arms=("lexical",))
    from su_multi_arm.core.types import LexicalQuery

    backend, gen = full.backends["local"], full.write_path.pin()[0]
    mono_backend, mono_gen = mono.backends["local"], mono.write_path.pin()[0]
    scope = Scope(predicates=[Predicate(field="source", op="eq", value="logs")])
    units = {u.unit_id for u in backend.units(gen, scope.predicates)}
    assert units == {"logs/catalog", "logs/month:2026-10", "logs/day:2026-11-04", "logs/day:2026-11-05", "logs/day:2026-11-06"}
    compared = 0
    for query in [*QUERIES, "signature day incident tenant", "unit day coverage summary", "episode cell"]:
        q = LexicalQuery(kind="terms", text=query)
        a = [(h.chunk_id, h.score) for h in backend.lexical.search(gen, q, scope, 100).items]
        b = [(h.chunk_id, h.score) for h in mono_backend.lexical.search(mono_gen, q, None, 100).items]
        assert close_hits(a, b), query
        compared += len(a)
    assert compared > 50  # the comparison is not vacuous


def test_the_log_stratum_search_equals_the_monolithic_engine_end_to_end(tmp_path, fx, members, full):
    mono = monolithic(tmp_path / "mono", fx, union({"logs": members["logs"]}))
    for query in QUERIES:
        request = SearchRequest(query=query, filters={"source": "logs"})
        left, right = hits(full.search(request)), hits(mono.search(request))
        assert [h[0] for h in left] == [h[0] for h in right], query
        assert all(abs(x[1] - y[1]) <= 1e-9 for x, y in zip(left, right)), query


# --- 4. dense exact merge ---------------------------------------------------------------------------


def test_dense_exact_top_k_over_every_member_equals_the_monolithic_top_k(tmp_path, fx, members, full):
    mono = monolithic(tmp_path / "mono", fx, union(members))
    backend, gen = full.backends["local"], full.write_path.pin()[0]
    mono_backend, mono_gen = mono.backends["local"], mono.write_path.pin()[0]
    for query in QUERIES:
        vector = full.embedder.encode([query], query=True)[0]
        a = [(h.chunk_id, h.document_id, h.score) for h in backend.vector.search(gen, vector, None, 40)]
        b = [(h.chunk_id, h.document_id, h.score) for h in mono_backend.vector.search(mono_gen, vector, None, 40)]
        assert sorted(a, key=lambda x: (-x[2], x[0])) == sorted(b, key=lambda x: (-x[2], x[0])), query
        assert [x[2] for x in a] == [x[2] for x in b]


# --- 5. records and listing ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        ScopeSpec(group_by=["kind"]),
        ScopeSpec(group_by=["source"]),
        ScopeSpec(predicates=[Predicate(field="source", op="eq", value="logs")], group_by=["kind"]),
        ScopeSpec(predicates=[Predicate(field="kind", op="eq", value="ticket")], group_by=["status"]),
        ScopeSpec(predicates=[Predicate(field="kind", op="eq", value="incident")]),
    ],
)
def test_records_over_members_equal_one_store(tmp_path_factory, fx, members, full, spec):
    mono = _mono_full(tmp_path_factory, fx, members)
    a = full.records.execute(full.write_path.pin()[0], spec, [])
    b = mono.records.execute(mono.write_path.pin()[0], spec, [])
    assert a.model_dump() == b.model_dump()


_MONO: dict = {}


def _mono_full(tmp_path_factory, fx, members):
    if "full" not in _MONO:
        _MONO["full"] = monolithic(tmp_path_factory.mktemp("mono_full"), fx, union(members))
    return _MONO["full"]


@pytest.mark.parametrize("size", [1, 7, 50, 500])
def test_the_merged_listing_pages_equal_the_monolithic_listing(tmp_path_factory, fx, members, full, size):
    mono = _mono_full(tmp_path_factory, fx, members)

    def listing(engine, scope=None):
        gen, catalog, out, cursor = engine.write_path.pin()[0], engine.backends["local"].catalog, [], None
        while True:
            page = catalog.iter_scope(gen, scope, Page(cursor=cursor, size=size))
            out.extend(d.id for d in page.items)
            assert len(page.items) <= size
            if page.next_cursor is None:
                return out
            cursor = page.next_cursor

    assert listing(full) == listing(mono)
    scope = Scope(predicates=[Predicate(field="kind", op="in", value=["ticket", "incident", "search_click"])])
    assert listing(full, scope) == listing(mono, scope)


def test_documents_and_values_are_routed_and_equal(tmp_path_factory, fx, members, full):
    mono = _mono_full(tmp_path_factory, fx, members)
    gen, mono_gen = full.write_path.pin()[0], mono.write_path.pin()[0]
    ids = [d.id for d in union(members)][::3] + ["ticket:999999", "logs:incident:unknown"]
    assert full.backends["local"].catalog.get(gen, ids) == mono.backends["local"].catalog.get(mono_gen, ids)
    for field in ("source", "status", "service", "tenant"):
        assert full.records.values(gen, field, [], 100) == mono.records.values(mono_gen, field, [], 100)
    assert full.backends["local"].catalog.count_documents(gen) == mono.backends["local"].catalog.count_documents(mono_gen)
    assert full.backends["local"].catalog.count_chunks(gen) == mono.backends["local"].catalog.count_chunks(mono_gen)
