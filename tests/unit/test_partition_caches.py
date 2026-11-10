"""Caches keyed per unit (unified merge plan §6.1 caches row, §10.2 "a compose warms nothing
unchanged"; M1a).

The partitioned backend keeps one sqlite backend per ``partition_id``: its bm25 segment, vector
matrix, row maps and faiss index are keyed by the member's own generation. Composing a new unified
generation that adds a member opens only the new member; the unchanged members keep their loaded
caches (same objects) and serve identical results. Derived per-view caches (shadowed ids, stratum
vocabularies) follow the id directory's changes.
"""

from __future__ import annotations

from su_multi_arm import SearchRequest
from su_multi_arm.core.types import LexicalQuery
from tests.partition_support import config, f0, f0_members, hits, member_block


def test_a_compose_warms_nothing_that_did_not_change(tmp_path):
    from su_multi_arm import Engine

    fx = f0()
    members = f0_members(fx)
    subset = {"tracker": members["tracker"], "analytics": members["analytics"]}
    engine = Engine(config(tmp_path, schema=fx.manifest["metadata_schema"], collection=member_block(fx, subset)))
    tracker = engine.build_member("tracker", *members["tracker"][0])
    day7 = engine.build_member("analytics", *members["analytics"][0])
    engine.compose([tracker, day7])
    backend = engine.backends["local"]
    request = SearchRequest(query="search crawler timeout error")
    first = engine.search(request)
    loads = backend.stats["collection.unit_loads"]
    assert loads == 2
    tracker_unit = backend._pool[tracker.partition_id]
    segment = tracker_unit.backend.lexical.segment(tracker_unit.gen)
    matrix = tracker_unit.backend.vector._load(tracker_unit.gen, "default")[1]

    day8 = engine.build_member("analytics", *members["analytics"][1])
    engine.compose([tracker, day7, day8])
    second = engine.search(request)
    assert backend.stats["collection.unit_loads"] == loads + 1  # only the new member was opened
    assert backend._pool[tracker.partition_id] is tracker_unit
    assert tracker_unit.backend.lexical.segment(tracker_unit.gen) is segment
    assert tracker_unit.backend.vector._load(tracker_unit.gen, "default")[1] is matrix
    assert second.generation != first.generation
    # the tracker stratum alone is unchanged by the composition
    scoped = SearchRequest(query="search crawler timeout error", filters={"source": "tracker"})
    engine.compose([tracker, day7])
    before = hits(engine.search(scoped))
    engine.compose([tracker, day7, day8])
    assert hits(engine.search(scoped)) == before


def test_derived_view_caches_follow_the_id_directory(tmp_path):
    from su_multi_arm import Engine

    fx = f0()
    members = f0_members(fx)
    only = {"tracker": members["tracker"]}
    engine = Engine(config(tmp_path, schema=fx.manifest["metadata_schema"], collection=member_block(fx, only)))
    key, docs = members["tracker"][0]
    old = engine.build_member("tracker", key, docs)
    new = engine.build_member("tracker", "snapshot:2026-11-09", [docs[0].model_copy(update={"title": "rewritten"})], replaces=[old.unit_id])
    engine.compose([old, new])
    backend, gen = engine.backends["local"], engine.write_path.pin()[0]
    old_unit = next(u for u in backend.units(gen) if u.unit_id == old.unit_id)
    assert backend.shadowed(gen, old_unit) == frozenset({docs[0].id})
    q = LexicalQuery(kind="terms", text="crawler")
    before = {h.document_id for h in backend.lexical.search(gen, q, None, 50).items}
    engine.view.attach().directory.tombstone([docs[1].id], by=new.unit_id)
    assert backend.shadowed(gen, old_unit) == frozenset({docs[0].id, docs[1].id})
    after = {h.document_id for h in backend.lexical.search(gen, q, None, 50).items}
    assert docs[1].id not in after and after <= before | {docs[0].id}
