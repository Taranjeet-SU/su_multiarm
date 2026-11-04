"""Selection split (plan §3.9): membership preserved under ordering changes, packing fairness,
configured field priority beats ``details``, diversity keeps minority groups, pins to front."""

from __future__ import annotations

import pytest

from su_multi_arm.core.config_selection import ORDERING_KINDS, PackingConfig
from su_multi_arm.selection import (
    Candidate,
    OrderingPolicy,
    PackExtra,
    PackRecord,
    SelectionPolicy,
    collapse,
    diversify,
    limit,
    membership,
    merge_pools,
    order,
    pack,
    pin,
    render,
    select,
    union,
)


def c(record_id, arm, score, source="article", **kw):
    return Candidate(record_id=record_id, arm=arm, score=score, source=source, **kw)


POOLS = {
    "dense": [c("a", "dense", 0.9), c("b", "dense", 0.8), c("c", "dense", 0.7), c("d", "dense", 0.6, source="case")],
    "lexical": [c("c", "lexical", 12.0), c("a", "lexical", 9.0), c("e", "lexical", 3.0, source="case")],
    "graph": [c("f", "graph", 1.0), c("b", "graph", 1.0)],
}


@pytest.mark.parametrize("kind", ORDERING_KINDS)
def test_ordering_changes_never_change_membership(kind):
    ordered, trace = order(POOLS, OrderingPolicy(kind=kind, arm_weights={"lexical": 2.0}, source_weights={"case": 1.5}))
    assert {r.record_id for r in ordered} == membership(POOLS) == {"a", "b", "c", "d", "e", "f"}
    assert len(ordered) == len({r.record_id for r in ordered}) and trace.union_size == 6 and trace.returned == 6
    assert all(f"ordering:{trace.policy}" in r.reasons for r in ordered)
    assert all(r.arms and all(reason.startswith(("arm:", "ordering:")) for reason in r.reasons) for r in ordered)


def test_reciprocal_rank_is_default_per_arm_alias_and_explicit_cap():
    base, _ = order(POOLS, OrderingPolicy())
    alias, trace = order(POOLS, OrderingPolicy(kind="per_arm"))
    assert [r.record_id for r in base] == [r.record_id for r in alias] and trace.policy == "reciprocal_rank" and trace.requested == "per_arm"
    assert [r.record_id for r in base][:3] == ["a", "c", "b"]  # a: 1 + 1/2, c: 1/3 + 1, b: 1/2 + 1/2
    capped, trace = order(POOLS, OrderingPolicy(max_documents=2))
    assert [r.record_id for r in capped] == ["a", "c"] and trace.cut_by_max_documents == 4
    best, _ = order(POOLS, OrderingPolicy(kind="best_arm_rank"))
    assert [r.record_id for r in best][:3] == ["a", "c", "f"]  # every arm's first hit ranks first, ties on id
    interleaved, _ = order(POOLS, OrderingPolicy(kind="stable_interleave"))
    assert [r.record_id for r in interleaved] == ["a", "c", "f", "b", "e", "d"]


def test_collapse_one_vote_per_record_per_arm_and_limits():
    pools = {"lexical": [c("x", "lexical", 1.0, chunk_ids=["x:1"]), c("x", "lexical", 5.0, chunk_ids=["x:2"], provenance=[{"q": 2}]), c("y", "lexical", 2.0)]}
    collapsed = collapse(pools)
    assert [(r.record_id, r.score, r.chunk_ids) for r in collapsed["lexical"]] == [("x", 5.0, ["x:2", "x:1"]), ("y", 2.0, [])]
    assert collapsed["lexical"][0].provenance == [{"q": 2}]
    assert [r.record_id for r in collapse(pools, "chunk")["lexical"]] == ["x", "y", "x"]
    assert [r.record_id for r in limit(collapsed, 1)["lexical"]] == ["x"]
    merged = merge_pools(collapsed, {"lexical": [c("x", "lexical", 9.0)], "dense": [c("z", "dense", 0.1)]})
    assert len(merged["lexical"]) == 2 and merged["lexical"][0].score == 9.0 and merged["dense"][0].record_id == "z"
    assert union(merged)["x"].arms == ["lexical"] and union(merged)["x"].reasons == ["arm:lexical rank 1"]
    with pytest.raises(ValueError):
        limit(pools, 0)


def test_diversity_keeps_minority_group_without_changing_membership():
    ordered, _ = order(POOLS, OrderingPolicy())
    before = [r.record_id for r in ordered]
    diverse, trace = diversify(ordered, "source", 1)
    assert [r.record_id for r in diverse] != before and {r.record_id for r in diverse} == set(before)
    assert diverse[0].source == "article" and diverse[1].source == "case" and trace.promoted == 1
    assert trace.groups == {"article": 4, "case": 2} and trace.short_groups == []
    _, trace = diversify(ordered, "kind", 7)
    assert trace.short_groups == ["document"]  # documented fallback: a short group contributes what it has
    ordered, _ = order(POOLS, OrderingPolicy())
    assert [r.record_id for r in diversify(ordered, "colour")[0]] == before  # missing field: one group, no change


def test_pins_move_to_front_and_select_chains_stages():
    ordered, _ = order(POOLS, OrderingPolicy())
    pinned = pin(ordered, {"e"})
    assert pinned[0].record_id == "e" and pinned[0].pinned and "pin:exact_id" in pinned[0].reasons
    pools = {**POOLS, "exact_id": [c("f", "exact_id", 1.0, pinned=True)]}
    selected, limited, trace = select(pools, SelectionPolicy(per_arm=2, diversity_field="source"))
    assert selected[0].record_id == "f" and trace.pinned == 1 and trace.per_arm_counts == {"dense": 2, "lexical": 2, "graph": 2, "exact_id": 1}
    assert {r.record_id for r in selected} == membership(limited) and trace.ordering.policy == "reciprocal_rank"
    assert trace.diversity is not None and trace.diversity.field == "source"


def test_packing_configured_field_priority_beats_details():
    record = PackRecord(id="kb:1", fields={"details": "D" * 10, "resolution": "R" * 10, "issue": "I" * 5})
    assert render(record, []).startswith("details:")  # document order, no hardcoded preference
    assert render(record, ["resolution"]).startswith("resolution: RRRRRRRRRR\ndetails:")
    assert render(record, ["issue", "resolution"]).split("\n")[-1].startswith("details:")
    assert render(PackRecord(id="kb:2", fallback_text="passage"), ["details"]) == "passage"
    packed = pack([record], PackingConfig(field_priority=["issue"]))
    assert packed.evidence[0].passage.startswith("issue: IIIII") and packed.field_priority == ["issue"]
    with pytest.raises(ValueError):
        PackingConfig(field_priority=["Bad-Name"])


def test_packing_fair_share_versus_greedy_and_min_per_record():
    records = [PackRecord(id="a", fields={"body": "a" * 1000}), PackRecord(id="b", fields={"body": "b" * 100}), PackRecord(id="c", fields={"body": "c" * 1000})]
    fair = pack(records, PackingConfig(budget_chars=1200, fairness="fair_share"))
    lengths = {e.id: len(e.passage) for e in fair.evidence}
    assert lengths == {"a": 700, "b": 106, "c": 394} or lengths["b"] == 106 and lengths["a"] + lengths["c"] == 1200 - 106
    assert fair.truncated_ids == ["a", "c"] and fair.omitted_ids == [] and fair.passage_characters == 1200
    assert all(e.truncated == (e.id != "b") for e in fair.evidence)
    greedy = pack(records, PackingConfig(budget_chars=1200, fairness="greedy"))
    assert {e.id: len(e.passage) for e in greedy.evidence} == {"a": 1006, "b": 106, "c": 88}
    many = [PackRecord(id=f"r{i}", fields={"body": "x" * 500}) for i in range(10)]
    floored = pack(many, PackingConfig(budget_chars=1000, min_per_record=200))
    assert [e.id for e in floored.evidence] == ["r0", "r1", "r2", "r3", "r4"] and floored.omitted_ids == [f"r{i}" for i in range(5, 10)]
    assert all(len(e.passage) == 200 for e in floored.evidence)
    extras = [PackExtra(id="aggregate:1", passage='{"count": 3}'), PackExtra(id="aggregate:2", passage="z" * 600)]
    with_extras = pack(records[:1], PackingConfig(budget_chars=1200, fairness="greedy", aggregate_reserve_max=1.0), extras)
    # aggregates are reserved first (they are exact derived evidence); the document gets the rest
    assert [e.id for e in with_extras.evidence] == ["a", "aggregate:1", "aggregate:2"] and with_extras.omitted_ids == []
    assert len(with_extras.evidence[0].passage) == 1200 - 12 - 600
    assert with_extras.evidence[1].source == "aggregate" and with_extras.evidence[1].passage == '{"count": 3}'
    assert pack([], PackingConfig()).evidence == []
