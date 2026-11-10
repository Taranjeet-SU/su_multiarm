"""Unified merge M2a: the slice ledger (``ingestion/log_slices.py``) — sealing, supersede, content
dedupe, catalog kinds (N3), compaction ladder and bounds, id directory mirroring and the refusals
that keep the previous slices (fail closed). Synthetic documents only."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from su_multi_arm.core.config_collection import SliceCompaction, SliceSettings
from su_multi_arm.core.types_members import SliceLedger, SliceRef
from su_multi_arm.ingestion.handoff import verify_unit
from su_multi_arm.ingestion.log_slices import LogSliceLedger, SliceRefused, UnitReader, doc_sha
from tests.log_slices_support import DAY, EMBED_FIELDS, FakeDirectory, at, doc, make_ledger, recipe, served_documents, view_of


@pytest.fixture
def ledger(tmp_path):
    return make_ledger(tmp_path / "ledger")


def test_ledger_implements_the_s0_protocol(ledger):
    assert isinstance(ledger, SliceLedger)


def test_seal_registers_a_sealed_slice_without_activation(ledger):
    ref = ledger.seal(DAY, "five_minute", [doc("logs:incident:a", "crawler timeout"), doc("logs:incident:b", "index failed")], as_of=at(DAY, 0, 5))
    assert isinstance(ref, SliceRef)
    assert ref.slice_id == "slice:2026-11-08:m00000001" and ref.sequence == 1 and ref.level == "five_minute"
    assert ref.documents == 2 and ref.counts == {"incident": 2} and ref.dense_pending
    body = verify_unit(ledger.unit_dir(ref.slice_id), expected_seal=ref.sha256)
    assert set(body["files"]) >= {"documents.jsonl.gz", "rows.json", "tombstones.json", "embed.json", "lexical/meta.json", "lexical/terms.sqlite"}
    assert ledger.vector() == {DAY: (ref.slice_id,)}
    assert json.loads(ledger.current_path.read_text())["sequence"] == 1
    assert ledger.get(ref.slice_id) == ref


def test_latest_version_wins_and_supersede_lists_the_previous_live_row(ledger):
    first = ledger.seal(DAY, "five_minute", [doc("logs:incident:a", "one two"), doc("logs:incident:b", "three")], as_of=at(DAY, 0, 5))
    second = ledger.seal(DAY, "five_minute", [doc("logs:incident:a", "one two four")], as_of=at(DAY, 0, 10))
    assert first.supersedes == () and second.supersedes == ("logs:incident:a",)
    assert second.excluded_stats["documents"] == 1 and second.excluded_stats["chunks"] == 2 and second.excluded_stats["tokens"] > 0
    served = served_documents(ledger, view_of(ledger))
    assert served["logs:incident:a"].fields["summary"] == "one two four"
    assert {r[0]: r[1] for r in ledger.live_rows(DAY)} == {"logs:incident:a": second.slice_id, "logs:incident:b": first.slice_id}


def test_unchanged_documents_are_not_sealed_again(ledger):
    docs = [doc("logs:incident:a", "same text")]
    assert ledger.seal(DAY, "five_minute", docs, as_of=at(DAY, 0, 5)) is not None
    assert ledger.seal(DAY, "five_minute", docs, as_of=at(DAY, 0, 10)) is None
    assert len(ledger.vector()[DAY]) == 1


def test_refusals_keep_the_previous_state(ledger):
    ledger.seal(DAY, "five_minute", [doc("logs:incident:a", "x")], as_of=at(DAY, 0, 5))
    before = ledger.current_path.read_text()
    with pytest.raises(SliceRefused, match="once"):
        ledger.seal(DAY, "five_minute", [doc("logs:incident:b", "x"), doc("logs:incident:b", "y")])
    with pytest.raises(SliceRefused, match="source"):
        ledger.seal(DAY, "five_minute", [doc("ticket:1", "x", source="ticket", kind="ticket")])
    with pytest.raises(SliceRefused, match="compact"):
        ledger.seal(DAY, "hourly", [doc("logs:incident:b", "x")])
    with pytest.raises(SliceRefused, match="replaces"):
        ledger.seal(DAY, "five_minute", [doc("logs:incident:b", "x")], replaces=["slice:2026-11-08:m00000001"])
    assert ledger.current_path.read_text() == before
    assert not list((ledger.root / ".staging").iterdir())


def test_ledger_refuses_another_recipe(tmp_path):
    make_ledger(tmp_path / "ledger")
    with pytest.raises(SliceRefused, match="recipe"):
        LogSliceLedger(tmp_path / "ledger", member="logs", source="logs", recipe=recipe(k1=1.2), embed_fields=EMBED_FIELDS)


def test_line_text_is_never_embedded(tmp_path):
    from su_multi_arm.ingestion.text_vectors import RetentionViolation

    with pytest.raises(RetentionViolation):
        LogSliceLedger(tmp_path / "x", member="logs", source="logs", recipe=recipe(), embed_fields={"signature": ["exemplars"]})


# --- catalog kinds (N3) ------------------------------------------------------------------------------------


def signature(rule: str, title: str = "Rule title") -> object:
    return doc(f"logs:signature:{rule}", title, kind="signature", title=title)


def test_first_seen_catalog_document_is_carried_as_catalog_provisional(tmp_path):
    known = signature("known")
    ledger = make_ledger(tmp_path / "ledger", catalog=lambda: {known.id: doc_sha(known)})
    ref = ledger.seal(DAY, "five_minute", [known, signature("new"), doc("logs:incident:a", "x")], as_of=at(DAY, 0, 5))
    assert ref.catalog_provisional == ("logs:signature:new",)
    assert ref.counts == {"incident": 1, "signature": 1}
    assert known.id not in served_documents(ledger, view_of(ledger))
    # the same first-seen rule emitted again (also on another day) is not sealed again
    assert ledger.seal(DAY + timedelta(days=1), "five_minute", [signature("new")], as_of=at(DAY + timedelta(days=1), 0, 5)) is None
    # a changed rule the catalog member already holds is parked for the next close, never served from a slice
    changed = signature("known", "Changed rule title")
    assert ledger.seal(DAY, "five_minute", [changed], as_of=at(DAY, 0, 10)) is None
    assert [d.id for d in ledger.catalog_pending()] == [known.id]
    rows = {r[0]: r[4] for r in ledger.live_rows(DAY)}
    assert rows == {"logs:signature:new": True, "logs:incident:a": False}


def test_compaction_keeps_catalog_provisional_rows(tmp_path):
    directory = FakeDirectory()
    ledger = make_ledger(tmp_path / "ledger", directory=directory)
    ledger.seal(DAY, "five_minute", [signature("new"), doc("logs:incident:a", "x")], as_of=at(DAY, 0, 5))
    hourly = ledger.compact(DAY, "hourly", hour=0)
    assert hourly.catalog_provisional == ("logs:signature:new",)
    assert {r[0]: r[4] for r in ledger.live_rows(DAY)} == {"logs:signature:new": True, "logs:incident:a": False}
    entries = {d: e for (d, u), e in directory.rows.items() if u == hourly.slice_id}
    assert entries["logs:signature:new"].catalog_provisional and not entries["logs:incident:a"].catalog_provisional


def test_catalog_provisional_documents_are_not_tombstoned_by_a_day_build(tmp_path):
    ledger = make_ledger(tmp_path / "ledger")
    ledger.seal(DAY, "five_minute", [signature("new"), doc("logs:incident:a", "x")], as_of=at(DAY, 0, 5))
    ref = ledger.seal(DAY, "five_minute", [doc("logs:incident:a", "x")], full_day=True, as_of=at(DAY, 0, 10))
    assert ref is None  # nothing changed and nothing vanished: the rule is catalog, not day-scoped


# --- compaction ladder and bounds -------------------------------------------------------------------------


def fill_hour(ledger, hour: int, start: int = 0, n: int = 12):
    refs = []
    for i in range(n):
        minute = (i + 1) * 5
        stamp = at(DAY, hour) + timedelta(minutes=minute)
        refs.append(ledger.seal(DAY, "five_minute", [doc(f"logs:incident:{hour}-{start + i}", f"text {hour} {i}"), doc("logs:incident:shared", f"v {hour} {i}")], as_of=stamp))
    return refs


def test_hourly_compaction_replaces_the_hour_and_keeps_versions(ledger):
    refs = fill_hour(ledger, 0)
    before = served_documents(ledger, view_of(ledger))
    hourly = ledger.compact(DAY, "hourly", hour=0)
    assert hourly.level == "hourly" and set(hourly.replaces) == {r.slice_id for r in refs}
    assert ledger.vector() == {DAY: (hourly.slice_id,)}
    assert served_documents(ledger, view_of(ledger)) == before
    reader = UnitReader(ledger.unit_dir(hourly.slice_id), ledger.bm25)
    assert reader.version_of()["logs:incident:shared"] == refs[-1].sequence  # the version of the content, not of the compaction
    reader.close()
    assert {s["status"] for s in ledger.slices() if s["slice_id"] in hourly.replaces} == {"replaced"}


def test_due_follows_the_ladder_and_bounds_hold(ledger):
    for hour in range(7):
        fill_hour(ledger, hour)
        for d, level, h in ledger.due(at(DAY, hour + 1)):
            ledger.compact(d, level, hour=h)
        assert len(ledger.vector()[DAY]) <= ledger.settings.max_units_per_day
    levels = [s["level"] for s in ledger.slices("current")]
    assert levels.count("base") == 1 and levels.count("hourly") == 1  # hours 0-5 in the base at 06:00, hour 6 hourly
    assert ledger.due(at(DAY, 7, 30)) == []


def test_a_seal_beyond_the_unit_bound_is_refused(tmp_path):
    settings = SliceSettings(compaction=SliceCompaction(refresh_seconds=300, hourly_seconds=600, day_base_hours=1), max_units_per_day=9)
    ledger = make_ledger(tmp_path / "ledger", settings=settings)
    for i in range(9):
        ledger.seal(DAY, "five_minute", [doc(f"logs:incident:{i}", "x")], as_of=at(DAY, 0, i + 1))
    with pytest.raises(SliceRefused, match="compaction is behind"):
        ledger.seal(DAY, "five_minute", [doc("logs:incident:late", "x")], as_of=at(DAY, 0, 30))


def test_base_compaction_after_day_end(ledger):
    fill_hour(ledger, 22, n=3)
    fill_hour(ledger, 23, n=3)
    assert ledger.due(at(DAY, 23, 59)) == [(DAY, "hourly", 22)]
    assert ledger.due(at(DAY + timedelta(days=1), 0, 1)) == [(DAY, "base", None)]
    base = ledger.run_due(at(DAY + timedelta(days=1), 0, 1))
    assert [b.level for b in base] == ["base"] and ledger.vector() == {DAY: (base[0].slice_id,)}
    assert ledger.average_idf() is not None


def test_closed_day_refuses_refreshes_and_compaction(ledger):
    ledger.seal(DAY, "five_minute", [doc("logs:incident:a", "x")], as_of=at(DAY, 0, 5))
    retired = ledger.release_day(DAY)
    assert retired and ledger.vector() == {}
    with pytest.raises(SliceRefused, match="closed"):
        ledger.seal(DAY, "five_minute", [doc("logs:incident:b", "x")])
    with pytest.raises(SliceRefused, match="closed"):
        ledger.compact(DAY, "base")


# --- id directory (LA) mirroring ----------------------------------------------------------------------------


def test_registration_is_mirrored_into_the_id_directory(tmp_path):
    directory = FakeDirectory()
    ledger = make_ledger(tmp_path / "ledger", directory=directory)
    first = ledger.seal(DAY, "five_minute", [doc("logs:incident:a", "x"), doc("logs:incident:b", "y")], as_of=at(DAY, 0, 5))
    second = ledger.seal(DAY, "five_minute", [doc("logs:incident:a", "x2")], as_of=at(DAY, 0, 10))
    assert directory.live() == {"logs:incident:a": second.slice_id, "logs:incident:b": first.slice_id}
    gone = ledger.seal(DAY, "five_minute", [doc("logs:incident:a", "x2")], full_day=True, as_of=at(DAY, 0, 15))
    assert gone.tombstones == ("logs:incident:b",)
    assert directory.live() == {"logs:incident:a": second.slice_id}
    hourly = ledger.compact(DAY, "hourly", hour=0)
    assert directory.live() == {"logs:incident:a": hourly.slice_id}


def test_an_unavailable_id_directory_refuses_registration(tmp_path):
    directory = FakeDirectory()
    ledger = make_ledger(tmp_path / "ledger", directory=directory)
    ledger.seal(DAY, "five_minute", [doc("logs:incident:a", "x")], as_of=at(DAY, 0, 5))
    before = (ledger.current_path.read_text(), ledger.live_rows(DAY))
    directory.fail_on = "register"
    with pytest.raises(RuntimeError, match="unavailable"):
        ledger.seal(DAY, "five_minute", [doc("logs:incident:b", "y")], as_of=at(DAY, 0, 10))
    assert (ledger.current_path.read_text(), ledger.live_rows(DAY)) == before
    assert not ledger.unit_dir("slice:2026-11-08:m00000002").exists()
    directory.fail_on = None
    ref = ledger.seal(DAY, "five_minute", [doc("logs:incident:b", "y")], as_of=at(DAY, 0, 10))
    assert ref.sequence == 2


def test_cross_day_supersede_keeps_one_served_row(ledger):
    """An episode continuing across midnight inside the live window (F0 chain C): the next day's
    slice supersedes the earlier day's row; a later rebuild of the earlier day does not revive it."""
    nxt = DAY + timedelta(days=1)
    ledger.seal(DAY, "five_minute", [doc("logs:incident:c", "four lines")], as_of=at(DAY, 23, 59))
    later = ledger.seal(nxt, "five_minute", [doc("logs:incident:c", "seven lines")], as_of=at(nxt, 0, 10))
    assert later.supersedes == ("logs:incident:c",)
    assert ledger.seal(DAY, "five_minute", [doc("logs:incident:c", "four lines")], full_day=True, as_of=at(DAY, 23, 59)) is None
    served = served_documents(ledger, view_of(ledger))
    assert served["logs:incident:c"].fields["summary"] == "seven lines"
    assert ledger.live_ids(DAY) == frozenset() and ledger.live_ids(nxt) == {"logs:incident:c"}
