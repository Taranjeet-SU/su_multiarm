"""Unified merge M2a: archive day / catalog / month members of the live log source
(``ingestion/log_members.py``) and the one-tier handover from slices to the archive."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from su_multi_arm.backends.sqlite.query_stats import LogUnits, QueryTimeScorer
from su_multi_arm.core.types import Document
from su_multi_arm.core.types_members import PinnedView
from su_multi_arm.ingestion.handoff import HandoffRefused
from su_multi_arm.ingestion.log_members import build_month_member, catalog_shas, close_day, member_ref_of
from su_multi_arm.ingestion.log_slices import ENGINE_TEMPLATE, SliceRefused, UnitReader
from tests.log_slices_support import DAY, at, doc, make_ledger


def signature(rule: str, title: str = "Rule title"):
    return doc(f"logs:signature:{rule}", title, kind="signature", title=title)


def test_close_day_builds_the_archive_and_catalog_members(tmp_path):
    ledger = make_ledger(tmp_path / "ledger")
    ledger.seal(DAY, "five_minute", [signature("r1"), doc("logs:incident:a", "alpha"), doc("logs:incident:b", "beta")], as_of=at(DAY, 1, 5))
    ledger.seal(DAY, "five_minute", [doc("logs:incident:a", "alpha two")], as_of=at(DAY, 1, 10))
    closed = close_day(ledger, DAY, tmp_path / "registry")
    ref = closed.day
    assert ref.key == "day:2026-11-08" and ref.tier == "archive" and ref.counts == {"incident": 2}
    assert ref.unit_id == "logs/day:2026-11-08" and ref.covers(DAY)
    assert set(ref.digests) >= {"documents.jsonl.gz", "rows.json", "SEAL"}
    assert closed.catalog is not None and closed.catalog.tier == "catalog" and closed.catalog.counts == {"signature": 1}
    assert member_ref_of(closed.day_dir) == ref
    reader = UnitReader(closed.day_dir, ledger.bm25)
    assert {d.id: d.fields["summary"] for d in reader.documents()} == {"logs:incident:a": "alpha two", "logs:incident:b": "beta"}
    reader.close()
    with pytest.raises(SliceRefused, match="immutable"):
        close_day(ledger, DAY, tmp_path / "registry")


def test_one_tier_per_day_across_the_handover(tmp_path):
    ledger = make_ledger(tmp_path / "ledger")
    registry = tmp_path / "registry"
    ledger.seal(DAY, "five_minute", [doc("logs:incident:a", "alpha")], as_of=at(DAY, 1, 5))
    nxt = DAY + timedelta(days=1)
    ledger.seal(nxt, "five_minute", [doc("logs:incident:b", "beta")], as_of=at(nxt, 1, 5))
    closed = close_day(ledger, DAY, registry)
    # the new generation serves the archive member while CURRENT still lists the day's slices
    with pytest.raises(ValidationError, match="exactly one tier"):
        PinnedView(generation="g2", members=(closed.day,), slices={"logs": ledger.vector()})
    view = PinnedView(generation="g2", members=(closed.day,), slices={"logs": ledger.vector_for(closed.day.covers)})
    scorer = QueryTimeScorer("logs", LogUnits(ledger, registry))
    assert {r[1] for r in scorer.rank_rows("alpha beta", 10, view)[0]} == {"logs:incident:a", "logs:incident:b"}
    # an old reader (old generation) still reads the slices: they stay until released and past grace
    old = PinnedView(generation="g1", slices={"logs": ledger.vector()})
    assert {r[1] for r in scorer.rank_rows("alpha", 10, old)[0]} == {"logs:incident:a"}
    retired = ledger.release_day(DAY)
    assert retired and set(ledger.vector()) == {nxt}
    assert {r[1] for r in scorer.rank_rows("alpha", 10, old)[0]} == {"logs:incident:a"}  # files kept for pinned readers


def test_quiet_close_adds_no_catalog_version_and_parked_changes_enter_it(tmp_path):
    registry = tmp_path / "registry"
    ledger = make_ledger(tmp_path / "ledger")
    ledger.seal(DAY, "five_minute", [signature("r1"), doc("logs:incident:a", "alpha")], as_of=at(DAY, 1, 5))
    first = close_day(ledger, DAY, registry)
    ledger.release_day(DAY)
    catalog_dir = first.catalog_dir
    ledger.catalog = lambda: catalog_shas(catalog_dir, ledger)
    d2 = DAY + timedelta(days=1)
    ledger.seal(d2, "five_minute", [signature("r1"), doc("logs:incident:b", "beta")], as_of=at(d2, 1, 5))
    quiet = close_day(ledger, d2, registry, previous_catalog=catalog_dir)
    assert quiet.catalog is None  # nothing changed: no new catalog member, no extra activation
    ledger.release_day(d2)
    d3 = d2 + timedelta(days=1)
    ledger.seal(d3, "five_minute", [signature("r1", "Renamed rule"), signature("r2"), doc("logs:incident:c", "gamma")], as_of=at(d3, 1, 5))
    changed = close_day(ledger, d3, registry, previous_catalog=catalog_dir)
    assert changed.catalog is not None and changed.catalog.counts == {"signature": 2}
    reader = UnitReader(changed.catalog_dir, ledger.bm25)
    assert {d.id: d.title for d in reader.documents()} == {"logs:signature:r1": "Renamed rule", "logs:signature:r2": "Rule title"}
    reader.close()


def unclassified(n: int, text: str) -> Document:
    return Document(id=f"logs:unclassified_example:{n}", dataset="unified", source="logs", kind="unclassified_example",
                    title="Unclassified candidate", fields={"exemplars": text})


def test_month_member_keeps_one_row_per_id_and_dedupes_texts(tmp_path):
    registry = tmp_path / "registry"
    ledger = make_ledger(tmp_path / "ledger")
    days = [date(2026, 10, 30), date(2026, 10, 31)]
    shared = "unexpected response <num> from upstream <host>"
    ledger.seal(days[0], "five_minute", [doc("logs:incident:y", "first day only"), unclassified(1, shared)], as_of=at(days[0], 23, 50))
    ledger.seal(days[0], "five_minute", [doc("logs:incident:x", "first piece")], as_of=at(days[0], 23, 55))
    dirs = [close_day(ledger, days[0], registry).day_dir]
    ledger.release_day(days[0])
    # the episode continues past midnight out of the archived day; the same masked text again
    ledger.seal(days[1], "five_minute", [doc("logs:incident:x", "continued piece"), unclassified(2, shared)], as_of=at(days[1], 0, 10))
    dirs.append(close_day(ledger, days[1], registry).day_dir)
    ledger.release_day(days[1])
    month, folder = build_month_member(ledger, dirs, "2026-10", registry)
    assert month.key == "month:2026-10" and month.tier == "archive" and month.covers(days[0]) and month.covers(days[1])
    reader = UnitReader(folder, ledger.bm25)
    docs = {d.id: d for d in reader.documents()}
    assert docs["logs:incident:x"].fields["summary"] == "continued piece"  # one row per id: the latest piece
    assert sorted(docs) == ["logs:incident:x", "logs:incident:y", "logs:unclassified_example:1", "logs:unclassified_example:2"]
    rows, texts = reader.embed()
    assert sum(1 for r in rows if r.doc_id.startswith("logs:unclassified")) == 2
    # the fixture space's template is the write path's (title + field + text): both examples compose
    # to one text, so they point at one text hash and are embedded and stored once
    assert list(texts.values()).count(ENGINE_TEMPLATE.format(title="Unclassified candidate", field="exemplars", text=shared)) == 1
    assert month.stats["unique_embed_texts"] == len(texts)
    reader.close()
    with pytest.raises(SliceRefused, match="not a day of"):
        build_month_member(ledger, dirs, "2026-11", registry)


def test_a_tampered_member_is_refused(tmp_path):
    ledger = make_ledger(tmp_path / "ledger")
    ledger.seal(DAY, "five_minute", [doc("logs:incident:a", "alpha")], as_of=at(DAY, 1, 5))
    closed = close_day(ledger, DAY, tmp_path / "registry")
    (closed.day_dir / "rows.json").write_text("{}")
    with pytest.raises(HandoffRefused, match="rows.json"):
        member_ref_of(closed.day_dir)
