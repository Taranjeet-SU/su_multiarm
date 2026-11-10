"""M3 (unified merge plan §6.7 B12, §6.9): links follow tombstones and rebuilds.

F0 gap-filling merge: on 2026-11-07 two alderbank-stg ``search.5xx`` episodes of the hourly slice
are merged by the day slice and one episode id disappears. A ticket linked to both before the merge
keeps the surviving link (with new evidence: its review no longer counts and it is re-queued) and
the vanished one is retracted as ``tombstoned_object`` and never displayed. A ticket that leaves the
snapshot retracts its links as ``tombstoned_subject``; a link that the rules no longer produce (the
identity view no longer vouches) is retracted as ``not_rebuilt``. Synthetic only."""

from __future__ import annotations

import pytest

from su_multi_arm.crosssource.identity import IdentityView, sha256_file
from su_multi_arm.crosssource.links import LinkStore, LinkView, build_links
from su_multi_arm.crosssource.links.review import review_queue
from tests.links_support import clock, f0, identity_view, pinned_documents, ticket

PRE_MERGE = {"2026-11-07": ["slice:2026-11-07:base", "slice:2026-11-07:h13"]}
SURVIVOR = "logs:incident:b0e9fca5748665615d35ec17e0ba81c3"
VANISHED = "logs:incident:5a53e6f6899b3c2cf82033c3c612722d"
TICKET = ticket(2001, "Alderbank Ltd", "2026-11-07T14:00:00Z", "Search requests fail with search.5xx since 13:00.", service="search-api")


@pytest.fixture(scope="module")
def fx():
    return f0()


def build(store: LinkStore, docs, view):
    result = build_links(docs, view)
    return result, store.build(result.links, known_ids=result.known_ids, report=result.report, registry_version=view.registry_version if view.joins_enabled else None)


def episode_links(store: LinkStore, subject: str = TICKET.id):
    return {s.object_id: s for s in store.state()["links"].values() if s.subject_id == subject and s.pair == "ticket-episode"}


def test_the_fixture_merge_removes_one_episode_id(fx):
    before = {d.id for d in pinned_documents(fx, PRE_MERGE)}
    after = {d.id for d in pinned_documents(fx)}
    assert {SURVIVOR, VANISHED} <= before and SURVIVOR in after and VANISHED not in after


def test_a_merged_away_episode_tombstones_its_link(fx, tmp_path):
    view = identity_view(fx, tmp_path)
    store = LinkStore.create(tmp_path / "links.sqlite", now=clock)
    first, _ = build(store, [*pinned_documents(fx, PRE_MERGE), TICKET], view)
    links = episode_links(store)
    assert set(links) == {SURVIVOR, VANISHED} and {s.status for s in links.values()} == {"candidate"}
    survivor = links[SURVIVOR]
    store.record_review(survivor.link_id, survivor.evidence_sha, model="m", verdict="agree", rationale="same outage")
    assert store.link(survivor.link_id).status == "model_accepted"

    result, summary = build(store, [*pinned_documents(fx), TICKET], view)
    assert summary["tombstoned_object"] == 1 and summary["changed"] == 1  # the survivor's window grew with the merge
    links = episode_links(store)
    assert links[VANISHED].served is False and links[VANISHED].retracted == "tombstoned_object"
    assert links[SURVIVOR].served and links[SURVIVOR].status == "candidate" and links[SURVIVOR].evidence["window"][1] == "2026-11-14T13:30:00Z"
    queued = {i.link_id for i in review_queue(store, result.documents)[0]}
    assert survivor.link_id in queued  # the review saw the pre-merge evidence: re-queued
    lv = LinkView.pinned(store.path, sha256_file(store.path), identity=view)
    assert VANISHED not in {e["other_id"] for e in lv.links_for(TICKET.id)} and lv.links_for(VANISHED) == []
    assert store.report()["retracted"] == {"tombstoned_object": 1}


def test_a_ticket_leaving_the_snapshot_tombstones_its_links(fx, tmp_path):
    view = identity_view(fx, tmp_path)
    store = LinkStore.create(tmp_path / "links.sqlite", now=clock)
    build(store, [*pinned_documents(fx), TICKET], view)
    n = len([s for s in store.state()["links"].values() if s.subject_id == TICKET.id])
    assert n >= 2  # episode + family
    _, summary = build(store, pinned_documents(fx), view)
    assert summary["tombstoned_subject"] == n
    assert {s.retracted for s in store.state()["links"].values() if s.subject_id == TICKET.id} == {"tombstoned_subject"}


def test_links_no_longer_produced_are_retracted_not_rebuilt(fx, tmp_path):
    view = identity_view(fx, tmp_path)
    store = LinkStore.create(tmp_path / "links.sqlite", now=clock)
    build(store, pinned_documents(fx), view)
    _, summary = build(store, pinned_documents(fx), IdentityView(False, "digest_mismatch"))
    assert summary["not_rebuilt"] == 5 and summary["tombstoned_object"] == summary["tombstoned_subject"] == 0
    st = store.state()
    assert st["registry_version"] is None
    assert all(not s.served and s.retracted == "not_rebuilt" for s in st["links"].values() if s.customer_derived)
    # rebuilding with the identity view again serves them again
    _, summary = build(store, pinned_documents(fx), view)
    assert summary["unchanged"] == 17 and all(s.served for s in store.state()["links"].values())
