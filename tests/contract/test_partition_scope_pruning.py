"""Partition scope pruning, exclusion of superseded/tombstoned rows and stratum fusion (unified
merge plan §6.1, §6.5; M1a).

* A request scoped to some sources never opens a unit of another source (counted per unit).
* ``get_document``-style reads and ``document_ids`` scopes are routed through the id directory: only
  the owning units are read; an unknown id is not found (never a scan of all members).
* Rows superseded or tombstoned by another pinned unit are excluded from candidates, records,
  counts and the query-time statistics: the view ranks exactly like one store holding the latest
  versions only.
* Several strata in one request are fused by rank (RRF), under ``source_quotas`` when declared;
  raw scores of different strata are never compared.

Synthetic evidence only (F0 fixture); no service, no model call.
"""

from __future__ import annotations

import pytest

from su_multi_arm import SearchRequest
from su_multi_arm.core.types import Document, LexicalQuery, Page, Predicate, Scope, ScopeSpec
from tests.partition_support import close_hits, f0, f0_members, hits, monolithic, partitioned, union

QUERIES = ["crawler fetch timeout", "search 5xx errors for the tenant", "scheduler job halted", "index write failed", "tracker issue crawler"]


@pytest.fixture(scope="module")
def fx():
    return f0()


@pytest.fixture(scope="module")
def members(fx):
    return f0_members(fx)


def _reads(backend) -> dict[str, int]:
    return {k.split(":", 1)[1]: v for k, v in backend.stats.items() if k.startswith("collection.unit_reads:")}


def test_out_of_scope_members_are_never_opened(tmp_path, fx, members):
    engine = partitioned(tmp_path, fx, members)
    backend = engine.backends["local"]
    backend._pool.clear()  # a fresh process: nothing opened yet
    backend.stats.clear()
    engine.search(SearchRequest(query="crawler fetch timeout", filters={"source": "tracker"}))
    assert set(_reads(backend)) == {"tracker/snapshot:2026-11-08"}
    assert backend.stats["collection.unit_loads"] == 1 and len(backend._pool) == 1
    engine.search(SearchRequest(query="crawler fetch timeout", filters={"source": "logs"}))
    assert {u for u in _reads(backend) if u.startswith("logs/")} == {"logs/catalog", "logs/month:2026-10", "logs/day:2026-11-04", "logs/day:2026-11-05", "logs/day:2026-11-06"}
    assert not any(u.startswith(("support/", "analytics/")) for u in _reads(backend))


def test_kind_predicates_prune_members_that_cannot_hold_the_kind(tmp_path, fx, members):
    engine = partitioned(tmp_path, fx, members)
    backend, gen = engine.backends["local"], engine.write_path.pin()[0]
    backend.stats.clear()
    backend.catalog.iter_scope(gen, Scope(predicates=[Predicate(field="kind", op="eq", value="tracker_issue")]), Page(size=10))
    assert set(_reads(backend)) == {"tracker/snapshot:2026-11-08"}


def test_document_reads_are_routed_through_the_id_directory(tmp_path, fx, members):
    engine = partitioned(tmp_path, fx, members)
    backend, gen = engine.backends["local"], engine.write_path.pin()[0]
    directory = backend.registry.directory
    ticket = members["support"][0][1][0].id
    backend.stats.clear()
    before = directory.lookups
    found = backend.catalog.get(gen, [ticket, "ticket:does-not-exist"])
    assert [d.id for d in found] == [ticket]
    assert directory.lookups - before == 1  # one indexed batch lookup
    assert set(_reads(backend)) == {"support/snapshot:2026-11-09"}
    backend.stats.clear()
    assert backend.catalog.get(gen, ["logs:incident:unroutable"]) == [] and _reads(backend) == {}
    page = backend.catalog.iter_scope(gen, Scope(document_ids=[ticket]), Page(size=5))
    assert [d.id for d in page.items] == [ticket] and set(_reads(backend)) == {"support/snapshot:2026-11-09"}
    ctx_docs = engine.documents_in(engine._context_for(None), [ticket])
    assert [d.id for d in ctx_docs] == [ticket]


# --- superseded and tombstoned rows --------------------------------------------------------------


def _revised(docs: list[Document]) -> tuple[list[Document], list[str], str]:
    """A newer tracker snapshot: two issues rewritten, one new issue; one old issue to tombstone."""
    changed = [d.model_copy(update={"title": d.title + " crawler fetch timeout regression", "fields": {**d.fields, "description": "crawler fetch timeout again after the index write failed"}}) for d in docs[:2]]
    new = docs[0].model_copy(update={"id": "tracker:ENG-999", "title": "scheduler job halted twice", "url": "https://tracker.example.org/browse/ENG-999"})
    return [*changed, new], [d.id for d in docs[:2]], docs[2].id


def test_superseded_and_tombstoned_rows_are_excluded_everywhere(tmp_path, fx, members):
    old_key, old_docs = members["tracker"][0]
    new_docs, superseded, gone = _revised(old_docs)
    only = {"tracker": [(old_key, old_docs)]}
    engine = partitioned(tmp_path / "part", fx, only)
    engine.build_member("tracker", "snapshot:2026-11-09", new_docs, replaces=[f"tracker/{old_key}"])
    registry = engine.view.attach()
    registry.directory.tombstone([gone], by="tracker/snapshot:2026-11-09")
    engine.compose()
    latest = {d.id: d for d in old_docs if d.id != gone} | {d.id: d for d in new_docs}
    mono = monolithic(tmp_path / "mono", fx, [latest[k] for k in sorted(latest)])
    backend, gen = engine.backends["local"], engine.write_path.pin()[0]
    mono_backend, mono_gen = mono.backends["local"], mono.write_path.pin()[0]
    old_unit = next(u for u in backend.units(gen) if u.unit_id == f"tracker/{old_key}")
    assert backend.shadowed(gen, old_unit) == frozenset([*superseded, gone])
    for query in QUERIES:
        q = LexicalQuery(kind="terms", text=query)
        a = [(h.chunk_id, h.score) for h in backend.lexical.search(gen, q, None, 50).items]
        b = [(h.chunk_id, h.score) for h in mono_backend.lexical.search(mono_gen, q, None, 50).items]
        assert close_hits(a, b), query
        left, right = hits(engine.search(SearchRequest(query=query))), hits(mono.search(SearchRequest(query=query)))
        assert [h[0] for h in left] == [h[0] for h in right], query
    for spec in (ScopeSpec(group_by=["status"]), ScopeSpec()):
        assert engine.records.execute(gen, spec, []).model_dump() == mono.records.execute(mono_gen, spec, []).model_dump()
    assert backend.catalog.count_documents(gen) == mono_backend.catalog.count_documents(mono_gen)
    assert backend.catalog.count_chunks(gen) == mono_backend.catalog.count_chunks(mono_gen)
    assert [d.title for d in backend.catalog.get(gen, superseded)] == [latest[i].title for i in superseded]
    assert backend.catalog.get(gen, [gone]) == []
    listing = backend.catalog.iter_scope(gen, None, Page(size=100)).items
    assert [d.id for d in listing] == sorted(latest)


# --- stratum fusion ------------------------------------------------------------------------------


def test_several_strata_are_fused_by_rank_under_source_quotas(tmp_path, fx, members):
    engine = partitioned(tmp_path / "q", fx, members, quotas={"ticket": 0.4, "doc": 0.1, "logs": 0.3, "tracker": 0.1, "analytics": 0.1}, arms=("lexical",))
    backend, gen = engine.backends["local"], engine.write_path.pin()[0]
    q = LexicalQuery(kind="terms", text="crawler fetch timeout search errors")
    page = backend.lexical.search(gen, q, None, 20).items
    assert len(page) == 20
    assert [h.score for h in page] == sorted((h.score for h in page), reverse=True)
    assert all(h.score <= 1 / 61 + 1e-12 for h in page)  # RRF values, never raw BM25 scores
    by_member = {}
    for h in page:
        by_member.setdefault(h.document_id.split(":")[0] if not h.document_id.startswith("https://") else "support", 0)
        by_member[h.document_id.split(":")[0] if not h.document_id.startswith("https://") else "support"] += 1
    assert by_member.get("logs", 0) >= 6  # 0.3 × 20 slots reserved for the log stratum
    # each stratum's own order is preserved inside the fused list
    support_alone = backend.lexical.search(gen, q, Scope(predicates=[Predicate(field="source", op="in", value=["ticket", "doc"])]), 20).items
    fused_support = [h.chunk_id for h in page if h.chunk_id in {s.chunk_id for s in support_alone}]
    assert fused_support == [s.chunk_id for s in support_alone][: len(fused_support)]


def test_fusion_without_quotas_interleaves_by_rank(tmp_path, fx, members):
    engine = partitioned(tmp_path / "r", fx, {"tracker": members["tracker"], "analytics": members["analytics"]}, arms=("lexical",))
    backend, gen = engine.backends["local"], engine.write_path.pin()[0]
    page = backend.lexical.search(gen, LexicalQuery(kind="terms", text="search crawler timeout error"), None, 10).items
    tracker = [h for h in page if h.document_id.startswith("tracker:")]
    analytics = [h for h in page if h.document_id.startswith("analytics:")]
    assert tracker and analytics
    assert page[0].score == pytest.approx(1 / 61)


def test_a_full_collection_answers_end_to_end(tmp_path, fx, members):
    engine = partitioned(tmp_path / "full", fx, members)
    result = engine.search(SearchRequest(query="crawler fetch timeout"))
    assert {h.source for h in result.hits} >= {"ticket", "logs"}
    # the monolithic smoke union (F0 baseline) has the same documents available
    assert {d.id for d in union(members)} >= {h.document_id for h in result.hits}


def test_the_member_stratum_scorer_implements_the_frozen_protocol(tmp_path, fx, members):
    from su_multi_arm.backends.sqlite.members import MemberStratumScorer
    from su_multi_arm.core.types_members import StratumScorer

    engine = partitioned(tmp_path / "s", fx, members, arms=("lexical",))
    backend, gen = engine.backends["local"], engine.write_path.pin()[0]
    scorer = MemberStratumScorer(backend, "logs")
    assert isinstance(scorer, StratumScorer)
    view = engine.view.pin(gen.generation)
    ranked = scorer.rank("crawler fetch timeout", 25, view)
    scope = Scope(predicates=[Predicate(field="source", op="eq", value="logs")])
    direct = backend.lexical.search(gen, LexicalQuery(kind="terms", text="crawler fetch timeout"), scope, 25).items
    assert ranked.stratum == "logs" and list(ranked.ids) == [h.chunk_id for h in direct]
    assert list(ranked.scores) == [h.score for h in direct] and len(ranked.units) == 5
    with pytest.raises(ValueError, match="no member"):
        MemberStratumScorer(backend, "ghost")
