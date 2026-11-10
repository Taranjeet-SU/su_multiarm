"""Unified merge M2a, B12: tombstones for documents that vanish from a full-day build (gap-filling
merges of episode/incident groups). Uses the F0 gap-merge fixture (``tests/fixtures/unified``): the
2026-11-07 h13 slice holds two groups E1/E2; late lines merge them, so the full-day rebuild keeps
E1's id and E2's ids vanish."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from su_multi_arm.backends.sqlite.query_stats import LogUnits, QueryTimeScorer
from su_multi_arm.ingestion.log_slices import UnitReader
from su_multi_arm.testing.fixtures.unified import load
from su_multi_arm.testing.fixtures.unified.collection import P, incident_documents
from tests.log_slices_support import FakeDirectory, at, doc, make_ledger, served_documents, view_of

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "unified"
D07 = date(2026, 11, 7)


@pytest.fixture(scope="module")
def fx():
    return load(ROOT)


def seal_gap_merge(ledger, fx):
    base = ledger.seal(D07, "five_minute", fx.slices["slice:2026-11-07:base"], as_of=at(D07, 12))
    h13 = ledger.seal(D07, "five_minute", fx.slices["slice:2026-11-07:h13"], as_of=at(D07, 14))
    rebuild = ledger.seal(D07, "day", fx.slices["slice:2026-11-07:day"], full_day=True, as_of=at(D07, 23, 59))
    return base, h13, rebuild


def test_full_day_rebuild_tombstones_the_merged_away_ids(tmp_path, fx):
    directory = FakeDirectory()
    ledger = make_ledger(tmp_path / "ledger", directory=directory)
    _base, _h13, rebuild = seal_gap_merge(ledger, fx)
    expected = fx.manifest["expected"]["gap_merge"]
    assert sorted(rebuild.tombstones) == expected["tombstones"]
    assert sorted(rebuild.tombstones) == fx.ledger["days"]["2026-11-07"][2]["retires"]
    survivor = expected["survivor"]
    assert survivor in rebuild.supersedes  # E1's id now holds the merged group
    served = served_documents(ledger, view_of(ledger))
    assert not set(expected["tombstones"]) & set(served)
    assert served[survivor].metadata["occurrences"] == 17
    assert set(served) == {d.id for d in fx.slices["slice:2026-11-07:day"]}  # signatures ride along as catalog-provisional
    assert ledger.live_ids(D07) == frozenset(served)
    for tombstoned in expected["tombstones"]:
        assert directory.resolve(tombstoned, view_of(ledger)) is None
    # stats exclude the tombstoned rows (and the superseded ones)
    assert rebuild.excluded_stats["documents"] == len(rebuild.supersedes) + len(rebuild.tombstones)


def test_a_tombstoned_document_is_never_a_candidate(tmp_path, fx):
    ledger = make_ledger(tmp_path / "ledger")
    seal_gap_merge(ledger, fx)
    scorer = QueryTimeScorer("logs", LogUnits(ledger))
    e2 = {d.id for d in incident_documents(P["E2"])}
    ranked, info = scorer.rank_rows("search 5xx alderbank stg", 200, view_of(ledger))
    assert ranked and not e2 & {r[1] for r in ranked}
    served = served_documents(ledger, view_of(ledger))
    assert info["N"] == sum(1 for _ in _chunks(ledger, served.values()))


def _chunks(ledger, docs):
    from su_multi_arm.extraction.chunking import chunk_document

    for d in docs:
        yield from chunk_document(d, ledger.chunking.size, ledger.chunking.overlap, [])


def test_compaction_carries_a_tombstone_only_while_an_older_row_needs_it(tmp_path):
    ledger = make_ledger(tmp_path / "ledger")
    day = date(2026, 11, 8)
    ledger.seal(day, "five_minute", [doc("logs:incident:e1", "group one"), doc("logs:incident:e2", "group two")], as_of=at(day, 13, 5))
    ledger.compact(day, "hourly", hour=13)  # e1/e2 now live in an hourly slice
    gone = ledger.seal(day, "five_minute", [doc("logs:incident:e1", "merged group")], full_day=True, as_of=at(day, 14, 5))
    assert gone.tombstones == ("logs:incident:e2",)
    # the hour-14 compaction must keep the tombstone: the hour-13 slice still holds e2
    hourly = ledger.compact(day, "hourly", hour=14)
    assert hourly.tombstones == ("logs:incident:e2",)
    assert "logs:incident:e2" not in served_documents(ledger, view_of(ledger))
    # the base holds everything: the tombstone has nothing left to hide and is dropped
    base = ledger.compact(day, "base")
    assert base.tombstones == () and "logs:incident:e2" not in UnitReader(ledger.unit_dir(base.slice_id), ledger.bm25).ids
    assert set(served_documents(ledger, view_of(ledger))) == {"logs:incident:e1"}


def test_a_tombstoned_id_can_come_back(tmp_path):
    ledger = make_ledger(tmp_path / "ledger")
    day = date(2026, 11, 8)
    ledger.seal(day, "five_minute", [doc("logs:incident:x", "first"), doc("logs:incident:y", "other")], as_of=at(day, 1, 5))
    ledger.seal(day, "five_minute", [doc("logs:incident:y", "other")], full_day=True, as_of=at(day, 1, 10))
    assert "logs:incident:x" not in served_documents(ledger, view_of(ledger))
    back = ledger.seal(day, "five_minute", [doc("logs:incident:x", "first")], as_of=at(day, 1, 15))
    assert back is not None and back.supersedes == ()  # not live, so nothing is superseded
    assert served_documents(ledger, view_of(ledger))["logs:incident:x"].fields["summary"] == "first"


def test_tombstones_only_concern_the_rebuilt_day(tmp_path):
    ledger = make_ledger(tmp_path / "ledger")
    d1, d2 = date(2026, 11, 8), date(2026, 11, 9)
    ledger.seal(d1, "five_minute", [doc("logs:incident:a", "a")], as_of=at(d1, 1, 5))
    ledger.seal(d2, "five_minute", [doc("logs:incident:b", "b")], as_of=at(d2, 1, 5))
    ref = ledger.seal(d2, "five_minute", [doc("logs:incident:c", "c")], full_day=True, as_of=at(d2, 1, 10))
    assert ref.tombstones == ("logs:incident:b",)
    assert set(served_documents(ledger, view_of(ledger))) == {"logs:incident:a", "logs:incident:c"}
