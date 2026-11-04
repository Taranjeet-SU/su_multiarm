"""Cross-process pins and the retention ``gc`` around them (addendum A1 §3.8.4): pins on every
registry that has storage for them, TTL expiry, the per-process heartbeat pin, a pin written by
another process blocking ``gc`` until it expires, and compose failure skipping expiry."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from su_multi_arm.ingestion.pins import MemoryPins, PinsUnsupported, PinToken, ProcessPins, SqlitePins, pin_registry
from su_multi_arm.ingestion.release import FileGenerationRegistry, MemoryGenerationRegistry
from tests.logs_support import ENGINE, Clock, cluster_for, days, pipeline
from tests.pattern_fixtures import engine_or_skip

G1, G2 = "a" * 32, "b" * 32


class Tick:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def sql_pins(tmp_path, clock):
    pytest.importorskip("sqlalchemy")
    from su_multi_arm.backends.sql.adapter import SqlBackend
    from su_multi_arm.ingestion.pins import SqlMetaPins

    backend = SqlBackend(f"sqlite:///{tmp_path / 'sql.db'}", "su")
    return SqlMetaPins(backend.generation_registry(), clock)


@pytest.mark.parametrize("kind", ["memory", "sqlite", "sql"])
def test_pin_ttl_release_and_sweep(tmp_path, kind):
    clock = Tick()
    pins = {"memory": lambda: MemoryPins(clock), "sqlite": lambda: SqlitePins(tmp_path / "pins.sqlite", clock), "sql": lambda: sql_pins(tmp_path, clock)}[kind]()
    token = pins.pin(G1, "proc-a", ttl=120)
    pins.pin(G1, "proc-b", ttl=10)
    pins.pin(G2, "proc-a", ttl=120)
    assert [t.holder for t in pins.pinned(G1)] == ["proc-a", "proc-b"]
    clock.t += 11
    assert [t.holder for t in pins.pinned(G1)] == ["proc-a"]  # proc-b expired by TTL
    assert pins.sweep() == 1
    renewed = pins.renew(token, ttl=5)
    assert renewed.expires_at == clock.t + 5
    pins.release(renewed)
    assert pins.pinned(G1) == [] and [t.holder for t in pins.pinned(G2)] == ["proc-a"]


def test_unreadable_pin_counts_as_live(tmp_path):
    pins = SqlitePins(tmp_path / "pins.sqlite", Tick())
    pins._put(f"pin:{G1}:mystery", "not json")
    assert [t.holder for t in pins.pinned(G1)] == ["mystery"]


def test_pin_registry_dispatch(tmp_path):
    assert isinstance(pin_registry(FileGenerationRegistry(tmp_path / "reg")), SqlitePins)
    memory = MemoryGenerationRegistry()
    assert isinstance(pin_registry(memory), MemoryPins) and pin_registry(memory) is pin_registry(memory)

    class Planned:
        pass

    with pytest.raises(PinsUnsupported):
        pin_registry(Planned())


def test_process_pins_write_per_heartbeat_not_per_request():
    clock = Tick()
    pins = MemoryPins(clock)
    current = {"gen": G1}
    process = ProcessPins(pins, ttl=120, heartbeat=30, current=lambda: current["gen"], holder="proc")
    for _ in range(100):  # 100 requests on the active generation
        process.acquire(G1)
        process.release(G1)
    assert process.writes == 1 and [t.holder for t in pins.pinned(G1)] == ["proc"]  # still current: stays pinned
    process.acquire(G1)  # a long request still on G1 when G2 becomes current
    current["gen"] = G2
    process.acquire(G2)
    process.beat()
    assert pins.pinned(G1) and pins.pinned(G2)
    process.release(G1)  # refcount 0 and no longer current: unpinned
    assert pins.pinned(G1) == [] and pins.pinned(G2)
    clock.t += 200
    assert pins.pinned(G2) == []  # a dead process's pin expires
    process.close()
    with pytest.raises(ValueError):
        ProcessPins(pins, ttl=10, heartbeat=30)


def test_process_pins_from_the_logs_config(tmp_path):
    from su_multi_arm.core.config import validate_config
    from tests.logs_support import config_data

    data = config_data(tmp_path)
    data["logs"]["pins"] = {"ttl_seconds": 60, "heartbeat_seconds": 20}
    cfg = validate_config(data)
    registry = FileGenerationRegistry(tmp_path / "reg")
    process = ProcessPins.for_config(cfg, registry, current=lambda: G1, holder="proc")
    assert (process.ttl, process.heartbeat) == (60, 20) and isinstance(process.pins, SqlitePins)
    process.acquire(G1)
    assert [t.holder for t in pin_registry(registry).pinned(G1)] == ["proc"]
    process.close()
    assert pin_registry(registry).pinned(G1) == []


PIN_SCRIPT = """
import sys
from pathlib import Path
from su_multi_arm.ingestion.pins import SqlitePins
SqlitePins(Path(sys.argv[1])).pin(sys.argv[2], "serving-process", float(sys.argv[3]))
"""


def test_a_pin_from_another_process_blocks_gc_until_it_expires(tmp_path):
    engine_or_skip(ENGINE)
    span = days(date(2026, 1, 1), 3)
    clock = Clock(span[0] + timedelta(days=1))
    cluster = cluster_for(span, lines=8)
    pipe, cfg, backend = pipeline(tmp_path, cluster, now=clock, retention={"keep_days": 2, "keep_generations": 1})
    for day in span[:2]:
        clock.set_day(day + timedelta(days=1))
        pipe.run_daily(day)
    pinned_generation = backend.generation_registry().current()["generation"]  # lists day 1
    sidecar = backend.root / "pins.sqlite"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(__file__).resolve().parents[2] / "src"), os.environ.get("PYTHONPATH", "")]), "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run([sys.executable, "-c", PIN_SCRIPT, str(sidecar), pinned_generation, "3600"], check=True, env=env, timeout=60)
    clock.set_day(span[2] + timedelta(days=1))
    report = pipe.run_daily(span[2])
    retention = report.steps["retention"]
    assert retention["status"] == "pinned_expired_generation"
    assert retention["pinned"] == [{"generation": pinned_generation, "holders": ["serving-process"], "status": "pinned_expired_generation"}]
    assert pinned_generation in backend.generation_registry().generations()
    assert "2026-01-01" in pipe.store.keys()  # never deleted under a live pin
    assert not report.ok and not report.steps["verify"]["ok"]
    # the serving process dies; its pin expires by TTL (rewritten here as already expired)
    pins = SqlitePins(sidecar)
    pins.pin(pinned_generation, "serving-process", 0.001)
    import time

    time.sleep(0.01)
    from su_multi_arm.ingestion.retention import Retention

    result = Retention(cfg, pipe.store, pipe.write_path, backends={"local": backend}, now=pipe.now).run(span[2])
    assert [d["generation"] for d in result["deleted_generations"]] == [pinned_generation]
    assert "2026-01-01" not in pipe.store.keys()
    assert Retention(cfg, pipe.store, pipe.write_path, backends={"local": backend}, now=pipe.now).verify(span[2])["ok"]


def test_pinned_retention_waits_up_to_the_bound(tmp_path):
    engine_or_skip(ENGINE)
    span = days(date(2026, 1, 1), 3)
    clock = Clock(span[0] + timedelta(days=1))
    pipe, cfg, backend = pipeline(tmp_path, cluster_for(span, lines=6), now=clock, retention={"keep_days": 2, "keep_generations": 1, "max_pin_wait_seconds": 30, "pin_retry_seconds": 10})
    for day in span[:2]:
        clock.set_day(day + timedelta(days=1))
        pipe.run_daily(day)
    generation = backend.generation_registry().current()["generation"]
    tick = Tick()
    pins = MemoryPins(tick)
    pins.pin(generation, "reader", ttl=25)
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        tick.t += seconds

    from su_multi_arm.ingestion.retention import Retention

    # compose day 3 first so the active generation no longer lists day 1
    clock.set_day(span[2] + timedelta(days=1))
    library = pipe.library()
    pipe.pull(span[2], library)
    pipe.compose(span[2], library)
    result = Retention(cfg, pipe.store, pipe.write_path, backends={"local": backend}, now=pipe.now, sleep=sleep, pins=pins).run(span[2])
    assert slept == [10, 10, 10] and [d["generation"] for d in result["deleted_generations"]] == [generation]


def test_an_incomplete_deletion_keeps_the_day_and_fails_verify(tmp_path):
    engine_or_skip(ENGINE)
    span = days(date(2026, 1, 1), 3)
    clock = Clock(span[0] + timedelta(days=1))
    pipe, cfg, backend = pipeline(tmp_path, cluster_for(span, lines=6), now=clock, retention={"keep_days": 2, "keep_generations": 1})
    for day in span[:2]:
        clock.set_day(day + timedelta(days=1))
        pipe.run_daily(day)
    stuck = backend.generation_registry().current()["generation"]
    original = pipe.write_path.delete_generation

    def partial(generation):
        if generation != stuck:
            return original(generation)
        return {"generation": generation, "deleted": [], "left_for_cleanup": [{"backend": "local", "generation": generation, "error": "simulated refusal"}]}

    pipe.write_path.delete_generation = partial
    clock.set_day(span[2] + timedelta(days=1))
    report = pipe.run_daily(span[2])
    retention = report.steps["retention"]
    assert retention["status"] == "incomplete_deletion" and retention["deleted_generations"] == []
    assert retention["left_for_cleanup"][0]["error"] == "simulated refusal"
    assert "2026-01-01" in pipe.store.keys() and backend.ledger.refs(stuck)  # nothing of it was purged
    assert not report.ok and not report.steps["verify"]["ok"]


def test_compose_failure_skips_expiry(tmp_path):
    engine_or_skip(ENGINE)
    span = days(date(2026, 1, 1), 3)
    clock = Clock(span[0] + timedelta(days=1))
    pipe, cfg, backend = pipeline(tmp_path, cluster_for(span, lines=6), now=clock, retention={"keep_days": 2})
    for day in span[:2]:
        clock.set_day(day + timedelta(days=1))
        pipe.run_daily(day)
    active = backend.generation_registry().current()["generation"]

    def broken(*args, **kwargs):
        raise RuntimeError("simulated compose failure")

    pipe.write_path.ingest = broken
    clock.set_day(span[2] + timedelta(days=1))
    with pytest.raises(RuntimeError):
        pipe.run_daily(span[2])
    assert "2026-01-01" in pipe.store.keys() and pipe.store.tombstone("2026-01-01") is None
    assert backend.generation_registry().current()["generation"] == active
    report = json.loads((pipe.reports_dir() / "run-2026-01-03.json").read_text())
    assert report["ok"] is False and report["steps"]["retention"]["status"] == "skipped"
    # retention refuses to run directly while the active generation still lists the expired day
    from su_multi_arm.core.errors import ConfigError
    from su_multi_arm.ingestion.retention import Retention

    with pytest.raises(ConfigError):
        Retention(cfg, pipe.store, pipe.write_path, backends={"local": backend}, now=pipe.now).run(span[2])


def test_pin_token_model():
    assert PinToken(generation=G1, holder="h", expires_at=1.0).holder == "h"
