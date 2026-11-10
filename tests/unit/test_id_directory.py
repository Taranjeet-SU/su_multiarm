"""The persistent id directory (unified merge plan §6.1 B9, §6.7 B12; ``ingestion/id_directory.py``).

Contract rows: implements the frozen ``IdDirectory`` protocol; registration is incremental (cost
of the new unit only) and transactional (a refused registration changes nothing); non-catalog ids
are unique across live units except a same-member same-day slice supersede and declared
``replaces``; ``catalog_provisional`` rows are exempt across days and handed to the catalog member;
resolution is one indexed lookup filtered to the pinned units, and respects supersede/tombstone
**relative to the view**; ``live_ids`` is the tombstone diff base; retire/purge. Includes F0's real
id semantics (catalog signatures repeated on every day, the archive-vs-live episode id of chain B).
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from su_multi_arm.core.errors import BackendError
from su_multi_arm.core.types_members import IdDirectory, IdDirectoryEntry, MemberRef, PartitionRecipe, PinnedView
from su_multi_arm.ingestion.id_directory import IdConflict, SqliteIdDirectory

D7, D8 = date(2026, 11, 7), date(2026, 11, 8)
SHA = "a" * 64


def entries(unit: str, ids, kind="incident", unit_kind="member", day=None, provisional=False):
    return [IdDirectoryEntry(doc_id=i, kind=kind, unit_id=unit, unit_kind=unit_kind, day=day, catalog_provisional=provisional) for i in ids]


def member(member_id: str, key: str, tier: str = "snapshot") -> MemberRef:
    extra = {"catalog_kinds": ("signature",)} if tier == "catalog" else {}
    return MemberRef(member=member_id, key=key, partition_id=SHA, recipe=PartitionRecipe(), tier=tier, as_of="2026-11-09T00:00:00Z", sources=("logs",), **extra)


def view(*members: MemberRef, slices: dict | None = None) -> PinnedView:
    return PinnedView(generation="f" * 32, members=members, slices=slices or {})


@pytest.fixture
def directory(tmp_path):
    return SqliteIdDirectory(tmp_path / "ids.sqlite")


def test_it_implements_the_frozen_protocol(directory):
    assert isinstance(directory, IdDirectory)


def test_register_and_resolve_within_the_pinned_units(directory):
    directory.register("tickets/snapshot:2026-11-08", "member", entries("tickets/snapshot:2026-11-08", ["ticket:1", "ticket:2"], kind="ticket"))
    pinned = view(member("tickets", "snapshot:2026-11-08"))
    found = directory.resolve("ticket:1", pinned)
    assert found is not None and found.unit_id == "tickets/snapshot:2026-11-08" and found.live
    assert directory.resolve("ticket:3", pinned) is None
    assert directory.resolve("ticket:1", view(member("tickets", "snapshot:2026-11-09"))) is None  # not pinned: not found
    before = directory.lookups
    assert set(directory.resolve_many(["ticket:1", "ticket:2", "ticket:9"], pinned)) == {"ticket:1", "ticket:2"}
    assert directory.lookups - before == 1


def test_a_duplicate_non_catalog_id_is_refused_and_nothing_is_written(directory):
    directory.register("a/snapshot:2026-11-08", "member", entries("a/snapshot:2026-11-08", ["x:1", "x:2"]))
    with pytest.raises(IdConflict, match="x:2"):
        directory.register("b/snapshot:2026-11-08", "member", entries("b/snapshot:2026-11-08", ["x:3", "x:2"]))
    assert directory.unit("b/snapshot:2026-11-08") is None and directory.count("b/snapshot:2026-11-08") == 0
    with pytest.raises(ValueError, match="already registered"):
        directory.register("a/snapshot:2026-11-08", "member", entries("a/snapshot:2026-11-08", ["x:9"]))


def test_a_same_day_slice_supersedes_the_earlier_version(directory):
    directory.register("slice:2026-11-07:base", "slice", entries("slice:2026-11-07:base", ["logs:i:1", "logs:i:2"], unit_kind="slice", day=D7), member="logs")
    directory.register("slice:2026-11-07:h13", "slice", entries("slice:2026-11-07:h13", ["logs:i:2", "logs:i:3"], unit_kind="slice", day=D7), member="logs")
    both = view(slices={"logs": {D7: ("slice:2026-11-07:base", "slice:2026-11-07:h13")}})
    assert directory.resolve("logs:i:2", both).unit_id == "slice:2026-11-07:h13"
    older = view(slices={"logs": {D7: ("slice:2026-11-07:base",)}})
    assert directory.resolve("logs:i:2", older).unit_id == "slice:2026-11-07:base"  # pinned before: still served
    assert directory.shadowed("slice:2026-11-07:base", both) == frozenset({"logs:i:2"})
    assert directory.live_ids("logs", D7) == frozenset({"logs:i:1", "logs:i:2", "logs:i:3"})
    # another day is not a supersede: refused
    with pytest.raises(IdConflict):
        directory.register("slice:2026-11-08:base", "slice", entries("slice:2026-11-08:base", ["logs:i:3"], unit_kind="slice", day=D8), member="logs")


def test_tombstones_remove_a_vanished_id_from_views_that_pin_the_tombstoning_unit(directory):
    """B12, F0 gap-fill merge: the day rebuild keeps E1 and tombstones E2."""
    directory.register("slice:2026-11-07:h13", "slice", entries("slice:2026-11-07:h13", ["logs:incident:e1", "logs:incident:e2"], unit_kind="slice", day=D7), member="logs")
    directory.register("slice:2026-11-07:day", "slice", entries("slice:2026-11-07:day", ["logs:incident:e1"], unit_kind="slice", day=D7), member="logs")
    directory.tombstone(["logs:incident:e2"], by="slice:2026-11-07:day")
    rebuilt = view(slices={"logs": {D7: ("slice:2026-11-07:h13", "slice:2026-11-07:day")}})
    assert directory.resolve("logs:incident:e2", rebuilt) is None
    assert directory.resolve("logs:incident:e1", rebuilt).unit_id == "slice:2026-11-07:day"
    assert directory.resolve("logs:incident:e2", view(slices={"logs": {D7: ("slice:2026-11-07:h13",)}})).unit_id == "slice:2026-11-07:h13"
    assert "logs:incident:e2" not in directory.live_ids("logs", D7)
    directory.tombstone(["logs:incident:e2"], by="slice:2026-11-07:day")  # idempotent
    with pytest.raises(ValueError, match="no live row"):
        directory.tombstone(["logs:incident:never"], by="slice:2026-11-07:day")


def test_replaces_hands_live_rows_to_a_compacted_or_closing_unit(directory):
    directory.register("slice:2026-11-06:base", "slice", entries("slice:2026-11-06:base", ["logs:i:a", "logs:i:b"], unit_kind="slice", day=date(2026, 11, 6)), member="logs")
    with pytest.raises(IdConflict):
        directory.register("logs/day:2026-11-06", "member", entries("logs/day:2026-11-06", ["logs:i:a", "logs:i:b"], day=date(2026, 11, 6)))
    directory.register("logs/day:2026-11-06", "member", entries("logs/day:2026-11-06", ["logs:i:a", "logs:i:b"], day=date(2026, 11, 6)), replaces=["slice:2026-11-06:base"])
    archived = view(member("logs", "day:2026-11-06", tier="archive"))
    assert directory.resolve("logs:i:a", archived).unit_id == "logs/day:2026-11-06"
    with pytest.raises(ValueError, match="not registered"):
        directory.register("logs/day:2026-11-05", "member", [], replaces=["slice:2026-11-05:nope"])


def test_catalog_provisional_rows_are_exempt_across_days_and_taken_over_by_the_catalog_member(directory):
    sig = "logs:signature:09fe5aa44230d7877fc05c59d181a295"  # F0: the same rule on every day
    directory.register("slice:2026-11-07:base", "slice", entries("slice:2026-11-07:base", [sig], kind="signature", unit_kind="slice", day=D7, provisional=True), member="logs")
    directory.register("slice:2026-11-08:base", "slice", entries("slice:2026-11-08:base", [sig], kind="signature", unit_kind="slice", day=D8, provisional=True), member="logs")
    live = view(slices={"logs": {D7: ("slice:2026-11-07:base",), D8: ("slice:2026-11-08:base",)}})
    assert directory.resolve(sig, live).day == D8  # the latest day's carrier
    directory.register("logs/catalog", "member", entries("logs/catalog", [sig], kind="signature"))
    closed = view(member("logs", "catalog", tier="catalog"), slices={"logs": {D7: ("slice:2026-11-07:base",), D8: ("slice:2026-11-08:base",)}})
    assert directory.resolve(sig, closed).unit_id == "logs/catalog"


def test_the_archive_vs_live_episode_id_of_f0_chain_b_needs_an_explicit_replaces(directory):
    """M1a ruling on F0 ``expected.collisions.archive_and_live``: an episode crossing midnight keeps
    its id in the closed day and the live slices. The directory refuses the duplicate unless the
    sealing slice declares the archive unit in ``replaces`` (latest version wins for that id only)."""
    episode = "logs:incident:252611907407520b065c5a06cf50cda7"
    directory.register("logs/day:2026-11-06", "member", entries("logs/day:2026-11-06", [episode, "logs:incident:other"], day=date(2026, 11, 6)))
    rows = entries("slice:2026-11-07:base", [episode], unit_kind="slice", day=D7)
    with pytest.raises(IdConflict, match="logs/day:2026-11-06"):
        directory.register("slice:2026-11-07:base", "slice", rows, member="logs")
    directory.register("slice:2026-11-07:base", "slice", rows, member="logs", replaces=["logs/day:2026-11-06"])
    both = view(member("logs", "day:2026-11-06", tier="archive"), slices={"logs": {D7: ("slice:2026-11-07:base",)}})
    assert directory.resolve(episode, both).unit_id == "slice:2026-11-07:base"
    assert directory.resolve("logs:incident:other", both).unit_id == "logs/day:2026-11-06"


def test_retire_and_purge(directory):
    directory.register("a/snapshot:2026-11-08", "member", entries("a/snapshot:2026-11-08", ["x:1"]))
    with pytest.raises(ValueError, match="retire it before purging"):
        directory.purge("a/snapshot:2026-11-08")
    directory.retire("a/snapshot:2026-11-08")
    directory.register("b/snapshot:2026-11-08", "member", entries("b/snapshot:2026-11-08", ["x:1"]))  # retired units do not conflict
    assert directory.purge("a/snapshot:2026-11-08") == 1
    assert directory.unit("a/snapshot:2026-11-08") is None
    with pytest.raises(ValueError, match="not registered"):
        directory.retire("a/snapshot:2026-11-08")


def test_registration_is_incremental(directory, tmp_path):
    """O(documents of the new unit): the statements of a registration do not grow with the
    collection already registered."""
    def statements_for(unit: str, ids: list[str]) -> int:
        count = 0
        original = sqlite3.connect

        def traced(*args, **kwargs):
            db = original(*args, **kwargs)
            db.set_trace_callback(lambda _sql: _bump())
            return db

        def _bump():
            nonlocal count
            count += 1

        sqlite3.connect = traced
        try:
            directory.register(unit, "member", entries(unit, ids))
        finally:
            sqlite3.connect = original
        return count

    first = statements_for("m/snapshot:2026-11-01", [f"m:{i}" for i in range(50)])
    for n in range(2, 12):
        directory.register(f"m/snapshot:2026-11-{n:02d}", "member", entries(f"m/snapshot:2026-11-{n:02d}", [f"m:{n}:{i}" for i in range(2000)]))
    later = statements_for("m/snapshot:2026-11-20", [f"m:z:{i}" for i in range(50)])
    assert later == first


def test_an_unavailable_directory_fails_closed(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    with pytest.raises(BackendError, match="id directory unavailable"):
        SqliteIdDirectory(blocker / "ids.sqlite")


def test_entries_are_coherent_with_their_unit(directory):
    with pytest.raises(ValueError, match="names unit"):
        directory.register("a/snapshot:2026-11-08", "member", entries("b/snapshot:2026-11-08", ["x:1"]))
    with pytest.raises(ValueError, match="needs its member"):
        directory.register("slice:2026-11-07:base", "slice", entries("slice:2026-11-07:base", ["x:1"], unit_kind="slice", day=D7))
    with pytest.raises(ValueError, match="duplicate"):
        directory.register("a/snapshot:2026-11-08", "member", entries("a/snapshot:2026-11-08", ["x:1", "x:1"]))
