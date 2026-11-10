"""Unified merge, M2a wired into the partition core (lane LB): the live log member served by the real
engine over a partitioned collection.

An F0 collection = an adopted-shape support member + the live log member's archive day members and
catalog member (``LiveLogs.close_day``: close, register, one compose, release) + the provisional
slices of the live window (``tests/live_log_support.py``). Covered here:

* searches span support and logs through the real arms (lexical, dense, structured), hit text read
  from log units, ``trace.collection`` lists the pinned slices;
* the §6.5 / B4 gate: support results are byte-identical with and without the log member present
  (facets with a support scope, the support stratum unscoped, engine searches per support source);
* N slices == one compacted day through the real arms (ids and order exact, scores within 1e-9);
* the live log stratum equals a monolithic engine built from the served documents;
* records/routing over served versions (superseded and tombstoned rows never served);
* vectors not stored yet: lexical serves the slice, dense skips it and says ``dense: pending``.
"""

from __future__ import annotations

from datetime import date

import pytest

from su_multi_arm import Engine, SearchRequest
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.types import LexicalQuery, Page, Predicate, Scope, ScopeSpec
from tests.live_log_support import ARCHIVE_DAYS, LIVE, QUERIES, arm_hits, at, collection, fixture, hits

LOG_QUERIES = ["crawler fetch timeout", "index write failed cobalt", "scheduler job halted", "search 5xx tenant", "render timeout", "unexpected response upstream"]


@pytest.fixture(scope="module")
def fx():
    return fixture()


@pytest.fixture(scope="module")
def full(tmp_path_factory, fx):
    return collection(tmp_path_factory.mktemp("full"), fx)


@pytest.fixture(scope="module")
def alone(tmp_path_factory, fx):
    return collection(tmp_path_factory.mktemp("alone"), fx, logs=False)


@pytest.fixture(scope="module")
def compacted(tmp_path_factory, fx):
    return collection(tmp_path_factory.mktemp("compacted"), fx, compact=True)


def _pin(engine):
    return engine.write_path.pin()[0]


def test_the_request_pin_names_the_generation_and_the_live_slices(full):
    gen, manifest = full.write_path.pin()
    assert gen.generation != manifest["generation"]  # a request generation id: generation + slice vector
    view = full.backends["local"].view(gen)
    assert view.generation == manifest["generation"]
    assert {d.isoformat() for d in view.slices["logs"]} == set(LIVE)
    archived = {m.key for m in view.members if m.member == "logs"}
    assert archived == {"catalog", *(f"day:{d}" for d in ARCHIVE_DAYS)}
    # equal vectors -> equal ids (caches shared); every pinned slice is held by a slice pin
    assert full.write_path.pin()[0] == gen
    live = full.view.attach().live
    pinned = {s for days in view.slices.values() for ids in days.values() for s in ids}
    assert pinned <= live.held_slices()
    pins = live.slice_pins()
    assert all(pins.pinned(s) for s in pinned)


def test_searches_span_support_and_logs(full):
    sources = set()
    for query in QUERIES:
        result = full.search(SearchRequest(query=query))
        sources |= {h.source for h in result.hits}
        assert all(h.text for h in result.hits if h.chunk_ids), query  # hit text read from log units and support alike
        coverage = result.trace["collection"]
        assert {s["unit"] for s in coverage["slices"]} and all(s["tier"] == "slice" for s in coverage["slices"])
        assert "dense" not in coverage  # every vector is stored
    assert {"logs", "ticket", "doc"} <= sources
    arms = full.search(SearchRequest(query="scheduler job halted")).arm_hits
    assert any(h.source == "logs" for h in arms["lexical"]) and any(h.source == "logs" for h in arms["dense"])
    assert any(h.source != "logs" for h in arms["lexical"])


@pytest.mark.parametrize("query", QUERIES)
def test_support_results_are_identical_with_and_without_logs(full, alone, query):
    """B4 gate (§6.5): the support stratum's lexical and dense candidates (ids, order, scores) and the
    engine's support-scoped results are identical with and without the log member present."""
    a, b = full.backends["local"], alone.backends["local"]
    ga, gb = _pin(full), _pin(alone)
    scope = Scope(predicates=[Predicate(field="source", op="in", value=["ticket", "doc"])])
    lex = LexicalQuery(kind="terms", text=query)
    assert a.lexical.search(ga, lex, scope, 50, Page(size=50)).items == b.lexical.search(gb, lex, scope, 50, Page(size=50)).items
    vector = full.embedder.encode([query], query=True)[0]
    assert a.vector.search(ga, vector, scope, 50) == b.vector.search(gb, vector, scope, 50)
    from su_multi_arm.backends.sqlite.members import MemberStratumScorer

    view_a, view_b = a.view(ga), b.view(gb)
    left, right = MemberStratumScorer(a, "support").rank(query, 40, view_a), MemberStratumScorer(b, "support").rank(query, 40, view_b)
    assert (left.ids, left.scores) == (right.ids, right.scores) and left.ids
    for source in ("ticket", "doc"):
        request = SearchRequest(query=query, filters={"source": source})
        x, y = full.search(request), alone.search(request)
        assert hits(x) == hits(y) and arm_hits(x) == arm_hits(y) and x.aggregates == y.aggregates
    # the log units are not opened for a support-scoped read
    reads = [k for k in a.stats if k.startswith("collection.unit_reads:slice:")]
    before = {k: a.stats[k] for k in reads}
    a.lexical.search(ga, lex, scope, 50)
    assert {k: a.stats[k] for k in reads} == before


@pytest.mark.parametrize("query", LOG_QUERIES)
def test_n_slices_rank_like_one_compacted_day_through_the_real_arms(full, compacted, query):
    request = SearchRequest(query=query, filters={"source": "logs"})
    a, b = full.search(request), compacted.search(request)
    left, right = hits(a), hits(b)
    assert left and [h[0] for h in left] == [h[0] for h in right]
    assert all(abs(x[1] - y[1]) <= 1e-9 for x, y in zip(left, right))
    for arm in ("lexical", "dense"):
        la, lb = arm_hits(a)[arm], arm_hits(b)[arm]
        assert [h[0] for h in la] == [h[0] for h in lb], arm
        assert all(abs(x[1] - y[1]) <= 1e-9 for x, y in zip(la, lb)), arm
    # facet level: chunk ids and scores of the log stratum
    lex = LexicalQuery(kind="terms", text=query)
    scope = Scope(predicates=[Predicate(field="source", op="eq", value="logs")])
    fa = [(h.chunk_id, h.score) for h in full.backends["local"].lexical.search(_pin(full), lex, scope, 100).items]
    fb = [(h.chunk_id, h.score) for h in compacted.backends["local"].lexical.search(_pin(compacted), lex, scope, 100).items]
    assert [c for c, _ in fa] == [c for c, _ in fb] and all(abs(x - y) <= 1e-9 for (_, x), (_, y) in zip(fa, fb))
    assert len(full.backends["local"].view(_pin(full)).unit_ids()) > len(compacted.backends["local"].view(_pin(compacted)).unit_ids())


def _served(engine):
    """``{doc id: Document}`` the engine's pinned view serves for the log member."""
    backend = engine.backends["local"]
    gen = _pin(engine)
    view = backend.view(gen)
    stratum = backend.log_stratum("logs")
    derived = stratum.derived(view)
    out = {}
    for reader in derived["readers"]:
        mask = derived["masks"][reader.unit_id][1]
        served = {d for d, ok in zip(reader.ids, mask) if ok}
        out.update({d.id: d for d in reader.documents(served)})
    return out


def test_the_live_log_stratum_equals_one_store_of_the_served_documents(tmp_path, fx, full):
    served = _served(full)
    gap = fx.manifest["expected"]["gap_merge"]
    assert not set(gap["tombstones"]) & set(served)  # tombstoned ids are never served
    mono = Engine(validate_config({
        "schema_version": 2, "dataset": "unified",
        "backends": {"local": {"kind": "sqlite", "path": str(tmp_path / "mono"), "lexical_engine": "bm25"}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "metadata_schema": fx.manifest["metadata_schema"],
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 64}}},
        "pipeline": {"decision": {"kind": "rules"}, "arms": {"lexical": {"backend": "local"}}},
    }))
    mono.ingest(list(served.values()))
    backend, gen = full.backends["local"], _pin(full)
    mono_backend, mono_gen = mono.backends["local"], mono.write_path.pin()[0]
    scope = Scope(predicates=[Predicate(field="source", op="eq", value="logs")])
    compared = 0
    for query in [*LOG_QUERIES, *QUERIES]:
        lex = LexicalQuery(kind="terms", text=query)
        before = backend.stats["collection.approximation:average_idf_lag"]
        a = [(h.chunk_id, h.score) for h in backend.lexical.search(gen, lex, scope, 100).items]
        b = [(h.chunk_id, h.score) for h in mono_backend.lexical.search(mono_gen, lex, None, 100).items]
        if backend.stats["collection.approximation:average_idf_lag"] != before:
            assert {c for c, _ in a} == {c for c, _ in b}, query  # declared epsilon-floor lag: same candidates
            continue
        assert [c for c, _ in a] == [c for c, _ in b], query
        assert all(abs(x - y) <= 1e-9 for (_, x), (_, y) in zip(a, b)), query
        compared += len(a)
    assert compared > 20


def test_documents_records_and_listings_read_the_served_versions(full, fx):
    from su_multi_arm.testing.fixtures.unified.collection import incident_id

    backend, gen = full.backends["local"], _pin(full)
    served = _served(full)
    chain_b = incident_id("chainB_live")
    doc = backend.catalog.get(gen, [chain_b])[0]
    assert doc == served[chain_b] and doc.metadata.get("occurrences") == 15  # the live continuation, not the archived piece
    tomb = fx.manifest["expected"]["gap_merge"]["tombstones"]
    assert backend.catalog.get(gen, list(tomb)) == []
    spec = ScopeSpec(predicates=[Predicate(field="kind", op="eq", value="incident")])
    result = backend.records.execute(gen, spec, [])
    incidents = {d for d, v in served.items() if v.kind == "incident"}
    assert result.value == len(incidents) and set(result.matched_ids) <= incidents and result.exactness == "exact"
    listed, cursor = [], None
    while True:
        page = backend.catalog.iter_scope(gen, Scope(predicates=[Predicate(field="source", op="eq", value="logs")]), Page(cursor=cursor, size=7))
        listed.extend(d.id for d in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert sorted(listed) == sorted(served) and len(listed) == len(set(listed))


def test_pending_vectors_are_reported_and_lexical_still_serves(tmp_path, fx):
    engine = collection(tmp_path, fx, embed=False)
    result = engine.search(SearchRequest(query="scheduler job halted"))
    coverage = result.trace["collection"]
    assert coverage.get("dense") == "pending" and any(s["dense"] == "pending" for s in coverage["slices"])
    assert any(h.source == "logs" for h in result.arm_hits["lexical"])
    assert not any(h.source == "logs" for h in result.arm_hits.get("dense", []))  # skipped, never guessed
    backend = engine.backends["local"]
    assert backend.vector.pending(_pin(engine)) and any(k.startswith("collection.dense_pending:") for k in backend.stats)


def test_close_composes_once_then_releases_and_old_pins_keep_reading(tmp_path, fx):
    engine = collection(tmp_path, fx)
    live = engine.view.attach().live
    old = _pin(engine)
    old_view = engine.backends["local"].view(old)
    day = date(2026, 11, 7)
    report = live.close_day("logs", day, as_of=at(day, 23, 59))
    assert report["compose_seconds"] < 10 and report["retired_slices"]
    new = _pin(engine)
    view = engine.backends["local"].view(new)
    assert day not in view.slices["logs"] and any(m.key == "day:2026-11-07" for m in view.members)  # one tier per day: archive wins
    # the old request still reads its pinned slices (pins + grace keep them on disk)
    request = SearchRequest(query="crawler fetch timeout", filters={"source": "logs"})
    assert engine.search(request).hits
    backend = engine.backends["local"]
    assert backend.view(old) == old_view
    assert backend.lexical.search(old, LexicalQuery(kind="terms", text="crawler fetch timeout"), None, 20).items
    assert live.ledger("logs").gc() == []  # pinned and inside the grace period


def test_strata_are_fused_by_rank_under_source_quotas(tmp_path, fx):
    quotas = {"ticket": 0.25, "doc": 0.25, "logs": 0.5}
    engine = collection(tmp_path, fx, quotas=quotas)
    backend, gen = engine.backends["local"], _pin(engine)
    from su_multi_arm.arms.stratified import fuse_strata

    checked = 0
    for query in ["crawler timeout", "index write failed", "scheduler job halted", "search errors tenant"]:
        lex = LexicalQuery(kind="terms", text=query)
        fused = backend.lexical.search(gen, lex, None, 8).items
        logs = backend.lexical.search(gen, lex, Scope(predicates=[Predicate(field="source", op="eq", value="logs")]), 8).items
        support = backend.lexical.search(gen, lex, Scope(predicates=[Predicate(field="source", op="in", value=["ticket", "doc"])]), 8).items
        expected = fuse_strata({"logs": logs, "support": support}, 8, sources={"logs": ["logs"], "support": ["ticket", "doc"]}, quotas=quotas)
        assert [(h.chunk_id, h.score) for h in fused] == [(h.chunk_id, h.score) for h in expected], query
        if len(logs) >= 4 and len(support) >= 4:
            assert sum(1 for h in fused if h.document_id.startswith("logs:")) == 4  # half of k by quota
            checked += 1
    assert checked
