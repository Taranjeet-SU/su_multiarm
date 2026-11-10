"""Unified merge M2a contract: the F0 log member served through the live log path — archive day
members (closed through the slice ledger), the per-source catalog member and the provisional slices
of the live window — read through one pinned view by the query-time lexical scorer and the dense
scorer. Uses the committed synthetic fixture only.

The engine-level live-window contract (``Engine`` + ``runtime/collection.py`` routing) needs the
partition core (LA, M1a) and is wired after it merges; this test pins the LB side of that contract:
one tier per day, one served row per id, the F0 chains and the gap merge, catalog documents served
once, and no duplicate candidates."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from su_multi_arm.backends.sqlite.query_stats import DenseLogScorer, LogUnits, QueryTimeScorer
from su_multi_arm.core.types_members import PinnedView
from su_multi_arm.ingestion.log_members import catalog_shas, close_day
from su_multi_arm.ingestion.log_slices import UnitReader, embed_pending_slices
from su_multi_arm.ingestion.text_vectors import TextVectorStore
from su_multi_arm.testing.fixtures.unified import load
from su_multi_arm.testing.fixtures.unified.collection import P, incident_documents, incident_id
from tests.log_slices_support import SPACE, at, hash_query_encoder, make_ledger

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "unified"
ARCHIVE_DAYS = ["2026-11-04", "2026-11-05", "2026-11-06"]
LIVE = {"2026-11-07": [("base", False), ("h13", False), ("day", True)], "2026-11-08": [("base", False), ("h22", False), ("h23", False)],
        "2026-11-09": [("base", False), ("h09", False), ("m1025", False)]}


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    fx = load(ROOT)
    tmp = tmp_path_factory.mktemp("live_window")
    registry = tmp / "registry"
    ledger = make_ledger(tmp / "ledger")
    state = {"catalog": None}
    ledger.catalog = lambda: catalog_shas(state["catalog"], ledger)
    members = []
    for day_s in ARCHIVE_DAYS:
        day = date.fromisoformat(day_s)
        ledger.seal(day, "five_minute", fx.members[f"logs/day:{day_s}"], as_of=at(day, 23, 59))
        closed = close_day(ledger, day, registry, previous_catalog=state["catalog"])
        members.append(closed.day)
        if closed.catalog is not None:
            members = [m for m in members if m.key != "catalog"] + [closed.catalog]
            state["catalog"] = closed.catalog_dir
        ledger.release_day(day)
    for day_s, slices in LIVE.items():
        day = date.fromisoformat(day_s)
        for name, full in slices:
            ledger.seal(day, "day" if full else "five_minute", fx.slices[f"slice:{day_s}:{name}"], full_day=full, as_of=at(day, 23, 59))
    view = PinnedView(generation="g", members=tuple(members), slices={"logs": ledger.vector()})
    units = LogUnits(ledger, registry)
    scorer = QueryTimeScorer("logs", units)
    readers = units.readers(view)
    masks = scorer.served.masks(readers)
    rows: dict[str, list[tuple[str, int]]] = {}  # served id -> [(unit, occurrences)]
    for reader in readers:
        docs = {d.id: d for d in reader.documents()}
        for doc_id, ok in zip(reader.ids, masks[reader.unit_id][1]):
            if ok:
                rows.setdefault(doc_id, []).append((reader.unit_id, docs[doc_id].metadata.get("occurrences", -1)))
    return {"fx": fx, "ledger": ledger, "view": view, "scorer": scorer, "rows": rows, "units": units, "tmp": tmp}


def test_one_tier_per_day_and_one_served_row_per_id(served):
    view, rows = served["view"], served["rows"]
    archived = {m.day() for m in view.members if m.day()}
    assert archived == {date.fromisoformat(d) for d in ARCHIVE_DAYS}
    assert set(view.slices["logs"]) == {date.fromisoformat(d) for d in LIVE}
    assert all(len(holders) == 1 for holders in rows.values())


def test_f0_chains_and_gap_merge(served):
    rows = served["rows"]
    gap = served["fx"].manifest["expected"]["gap_merge"]
    assert not set(gap["tombstones"]) & set(rows)
    assert rows[gap["survivor"]][0][1] == 17
    # chain B: archived on 2026-11-06 and continued into the live window: the live version is served once
    assert rows[incident_id("chainB_live")] == [(rows[incident_id("chainB_live")][0][0], 15)]
    assert rows[incident_id("chainB_live")][0][0].startswith("slice:2026-11-07:")
    # chain C: superseded across provisional days; chain D: superseded within one day
    assert rows[incident_id("chainC_v2")][0][1] == 7 and rows[incident_id("chainC_v2")][0][0].startswith("slice:2026-11-09:")
    assert rows[incident_id("D_v2")][0][1] == 21
    # chain A: two pieces across archive days stay two ids
    assert rows[incident_id("chainA_1")][0][0] == "logs/day:2026-11-05" and rows[incident_id("chainA_2")][0][0] == "logs/day:2026-11-06"


def test_catalog_documents_are_served_once_from_the_catalog_or_a_slice(served):
    rows, fx = served["rows"], served["fx"]
    signature_ids = fx.manifest["expected"]["collisions"]["signature_catalog_every_day"]["ids"]
    for sid in signature_ids:
        assert len(rows[sid]) == 1
        assert rows[sid][0][0] in {"logs/catalog"} or rows[sid][0][0].startswith("slice:")


def test_lexical_candidates_are_unique_and_exclude_vanished_ids(served):
    scorer, view = served["scorer"], served["view"]
    e2 = {d.id for d in incident_documents(P["E2"])}
    for query in ["search 5xx", "crawler fetch timeout", "index write failed cobalt", "scheduler job halted"]:
        ranked, info = scorer.rank_rows(query, 50, view)
        chunk_ids = [r[0] for r in ranked]
        assert len(chunk_ids) == len(set(chunk_ids)) and ranked
        assert not e2 & {r[1] for r in ranked}
        assert set(info["units"]) == set(view.unit_ids())


def test_dense_over_the_same_view(served):
    ledger, view = served["ledger"], served["view"]
    embedder, encode_query = hash_query_encoder(SPACE.dimensions)
    store = TextVectorStore(served["tmp"] / "vectors", SPACE)

    class Docs:
        def encode(self, texts):
            return embedder.encode(texts)

    embed_pending_slices(ledger, store, Docs())
    for member in view.members:
        reader = UnitReader(served["units"].registry / Path(member.facets["lexical"].location).parent, ledger.bm25)
        from su_multi_arm.ingestion.text_vectors import embed_missing

        embed_missing(store, reader.embed()[1], Docs())
        reader.close()
    dense = DenseLogScorer("logs", served["units"], store, encode_query)
    ranking = dense.rank("search api server errors", 20, view)
    assert ranking.ids and not ranking.approximations
    assert len(set(ranking.ids)) == len(ranking.ids)
