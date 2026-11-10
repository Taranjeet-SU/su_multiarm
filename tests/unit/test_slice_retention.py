"""Unified merge M2a, R3: slice retention. A replaced or retired slice is deleted only when no slice
pin holds it and it was retired more than ``collection.slices.gc_grace_seconds`` ago; current
slices are never deleted; slice pins share the generation pins' row store."""

from __future__ import annotations

from datetime import timedelta

import pytest

from su_multi_arm.core.config_collection import SliceSettings
from su_multi_arm.ingestion.log_members import close_day
from su_multi_arm.ingestion.pins import MemoryPins, ProcessPins, SlicePins, SqlitePins, slice_pins
from su_multi_arm.ingestion.release import MemoryGenerationRegistry
from tests.log_slices_support import DAY, Clock, at, doc, make_ledger


def compacted_ledger(tmp_path, clock, pins=None, grace=3600):
    ledger = make_ledger(tmp_path / "ledger", clock=clock, pins=pins, settings=SliceSettings(gc_grace_seconds=grace))
    a = ledger.seal(DAY, "five_minute", [doc("logs:incident:a", "alpha")], as_of=at(DAY, 0, 5))
    b = ledger.seal(DAY, "five_minute", [doc("logs:incident:b", "beta")], as_of=at(DAY, 0, 10))
    hourly = ledger.compact(DAY, "hourly", hour=0)
    return ledger, [a.slice_id, b.slice_id], hourly


def test_replaced_slices_wait_for_the_grace(tmp_path):
    clock = Clock()
    ledger, replaced, hourly = compacted_ledger(tmp_path, clock)
    assert ledger.gc() == []
    clock.now += 3599
    assert ledger.gc() == [] and all(ledger.unit_dir(s).exists() for s in replaced)
    clock.now += 2
    assert sorted(ledger.gc()) == sorted(replaced)
    assert not any(ledger.unit_dir(s).exists() for s in replaced)
    assert ledger.unit_dir(hourly.slice_id).exists()  # current: never collected
    assert {s["slice_id"]: s["status"] for s in ledger.slices()} == {**dict.fromkeys(replaced, "deleted"), hourly.slice_id: "current"}


def test_a_pinned_slice_is_kept_until_its_pin_goes(tmp_path):
    clock = Clock()
    pins = SlicePins(MemoryPins(clock))
    ledger = make_ledger(tmp_path / "ledger", clock=clock, pins=pins)
    a = ledger.seal(DAY, "five_minute", [doc("logs:incident:a", "alpha")], as_of=at(DAY, 0, 5))
    tokens = pins.pin_vector(ledger.vector(), "reader-1", ttl=7200)  # the request pin of a long session
    ledger.compact(DAY, "hourly", hour=0)
    clock.now += 4000
    assert ledger.retire(a.slice_id) is False and ledger.unit_dir(a.slice_id).exists()
    for token in tokens:
        pins.release(token)
    assert ledger.retire(a.slice_id) is True and not ledger.unit_dir(a.slice_id).exists()


def test_an_expired_pin_does_not_block_and_an_unreadable_one_does(tmp_path):
    clock = Clock()
    rows = MemoryPins(clock)
    pins = SlicePins(rows)
    ledger = make_ledger(tmp_path / "ledger", clock=clock, pins=pins)
    a = ledger.seal(DAY, "five_minute", [doc("logs:incident:a", "alpha")], as_of=at(DAY, 0, 5))
    pins.pin(a.slice_id, "crashed-reader", ttl=60)
    ledger.compact(DAY, "hourly", hour=0)
    rows._put(f"slicepin:{a.slice_id}#broken", "not json")
    clock.now += 4000
    assert ledger.gc() == []  # the unreadable pin counts as live
    rows._delete(f"slicepin:{a.slice_id}#broken")
    assert ledger.gc() == [a.slice_id]


def test_process_pins_hold_a_request_vector(tmp_path):
    clock = Clock()
    registry = MemoryGenerationRegistry()
    pins = slice_pins(registry, clock)
    assert pins.rows is slice_pins(registry, clock).rows  # one row store per registry, shared with generation pins
    ledger = make_ledger(tmp_path / "ledger", clock=clock, pins=pins)
    a = ledger.seal(DAY, "five_minute", [doc("logs:incident:a", "alpha")], as_of=at(DAY, 0, 5))
    process = ProcessPins(pins, ttl=120, heartbeat=30, holder="serve-1")
    vector = ledger.vector()
    for ids in vector.values():
        for slice_id in ids:
            process.acquire(slice_id)
    ledger.compact(DAY, "hourly", hour=0)
    clock.now += 4000
    process.beat()  # renewed: still held
    assert ledger.gc() == []
    process.release(a.slice_id)  # refcount 0 and not "current": the registry pin goes
    assert ledger.gc() == [a.slice_id]


def test_retired_day_slices_are_collected_after_release_and_grace(tmp_path):
    clock = Clock()
    ledger = make_ledger(tmp_path / "ledger", clock=clock)
    a = ledger.seal(DAY, "five_minute", [doc("logs:incident:a", "alpha")], as_of=at(DAY, 0, 5))
    close_day(ledger, DAY, tmp_path / "registry")
    clock.now += 10_000
    assert ledger.gc() == []  # not released yet: the day's slices are still current
    ledger.release_day(DAY)
    assert ledger.gc() == []
    clock.now += 3601
    assert ledger.gc() == [a.slice_id]


def test_slice_pins_live_beside_generation_pins_in_sqlite(tmp_path):
    clock = Clock()
    rows = SqlitePins(tmp_path / "pins.sqlite", clock)
    pins = SlicePins(rows)
    rows.pin("gen1", "h", ttl=60)
    token = pins.pin("slice:2026-11-08:m00000001", "serve:1:abc", ttl=60)
    assert [t.holder for t in pins.pinned("slice:2026-11-08:m00000001")] == ["serve:1:abc"]
    assert [t.holder for t in rows.pinned("gen1")] == ["h"]
    assert pins.pinned("slice:2026-11-08:m0000000") == []  # prefix match is exact on the slice id
    clock.now += 61
    assert pins.sweep() == 1 and rows.sweep() == 1
    with pytest.raises(ValueError):
        pins.pin("slice:a#b", "h", ttl=1)
    pins.release(token)


def test_grace_must_cover_the_request_deadline():
    with pytest.raises(ValueError):
        SliceSettings(gc_grace_seconds=10)
    assert SliceSettings().gc_grace_seconds == 3600 and timedelta(seconds=SliceSettings().gc_grace_seconds) == timedelta(hours=1)
