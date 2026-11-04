"""Retention over 11 synthetic days with ``keep_days: 10`` and ``keep_generations: 2`` (addendum A1
§3.8.3, L3b acceptance): after day 11 the first day is absent from every store, including the
generation built the day before (rule a), the ledger, the cache, sessions and run reports; the
content-based verifier passes, and catches an injected leak."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

import pytest

from su_multi_arm.backends.extras import PurgeSpec
from su_multi_arm.core.types import Page, SessionRecord
from su_multi_arm.ingestion.partitions import line_hash, record_lines
from su_multi_arm.ingestion.release import generation_id
from su_multi_arm.ingestion.retention import Retention, partition_tag
from tests.logs_support import Clock, cluster_for, days, pipeline
from tests.pattern_fixtures import engine_or_skip

FIRST = date(2026, 1, 1)


@pytest.fixture(scope="module")
def eleven_days(tmp_path_factory):
    from tests.logs_support import ENGINE

    engine_or_skip(ENGINE)
    tmp = tmp_path_factory.mktemp("retention11")
    span = days(FIRST, 11)
    cluster = cluster_for(span, lines=16)
    clock = Clock(FIRST + timedelta(days=1))
    pipe, cfg, backend = pipeline(tmp, cluster, now=clock)
    day1_lines: set[str] = set()
    day1_source_ids: set[str] = set()
    generations: dict[str, str] = {}
    cache_keys: dict[str, str] = {}
    reports = []
    for day in span:
        clock.set_day(day + timedelta(days=1))
        report = pipe.run_daily(day)
        reports.append(report)
        generation = report.steps["compose"]["generation"]
        generations[day.isoformat()] = generation
        manifest = backend.generation_registry().manifest(generation)
        gen = generation_id(manifest)
        backend.cache.put(f"{gen.prefix()}answer", {"text": f"cached for {day}"}, 86_400)
        cache_keys[day.isoformat()] = f"{gen.prefix()}answer"
        if day == FIRST:
            for artifacts in pipe.store.units(day.isoformat()):
                for record in artifacts.records:
                    day1_lines.update(line_hash(t) for t in record_lines(record))
                    day1_source_ids.add(record.source_id)
            # a learning-style raw reply carrying day-1 text, tagged with its partition
            sample = next(iter(next(iter(pipe.store.units(day.isoformat()))).records))
            backend.ledger.put_raw("raw:learn:day1", json.dumps({"lines": [sample.message]}), True, tags=[partition_tag("synthetic", day.isoformat())])
            backend.session.create(SessionRecord(id="s-day1", principal_id="p", dataset="logs", created_at=0.0, last_seen_at=clock().timestamp()))
    return {"pipe": pipe, "cfg": cfg, "backend": backend, "reports": reports, "generations": generations, "cache_keys": cache_keys, "day1_lines": day1_lines, "day1_source_ids": day1_source_ids, "tmp": tmp}


def test_every_run_succeeds_and_verifies(eleven_days):
    for report in eleven_days["reports"]:
        assert report.ok, report.error
        assert report.steps["verify"]["ok"], report.steps["verify"]["problems"]


def test_day_one_partition_is_a_tombstone(eleven_days):
    store = eleven_days["pipe"].store
    assert "2026-01-01" not in store.keys()
    tombstone = store.tombstone("2026-01-01")
    assert tombstone is not None and tombstone.verify_sample and tombstone.verify_source_ids
    assert not list((store.root / "synthetic" / "2026-01-01").glob("v*"))
    assert store.keys() == [d.isoformat() for d in days(FIRST + timedelta(days=1), 10)]


def test_rule_a_deletes_the_previous_generation_whatever_keep_generations_says(eleven_days):
    backend, generations = eleven_days["backend"], eleven_days["generations"]
    registry = backend.generation_registry()
    remaining = registry.generations()
    # day 10's generation lists day 1 and is gone although keep_generations is 2
    assert generations["2026-01-10"] not in remaining
    assert remaining == [generations["2026-01-11"]]
    assert registry.current()["generation"] == generations["2026-01-11"]
    assert not (backend.root / "snapshots" / generations["2026-01-10"]).exists()
    deleted = eleven_days["reports"][-1].steps["retention"]["deleted_generations"]
    # the two rollback generations kept by day 10's run (days 9 and 10) both list day 1
    assert sorted((d["generation"], d["reason"]) for d in deleted) == sorted(
        [(generations["2026-01-10"], "lists_expired_partition"), (generations["2026-01-09"], "lists_expired_partition")]
    )


def test_keep_generations_bounds_in_window_rollback(eleven_days):
    # day 9's run deleted day 7's generation (beyond keep_generations), day 10's run kept day 9's
    # each run before day 11 keeps the active generation and one previous one for rollback
    generations = eleven_days["generations"]
    reasons = {d["generation"]: (r.day, d["reason"]) for r in eleven_days["reports"] for d in r.steps["retention"]["deleted_generations"]}
    assert reasons[generations["2026-01-08"]] == ("2026-01-10", "beyond_keep_generations")
    assert reasons[generations["2026-01-09"]] == ("2026-01-11", "lists_expired_partition")
    for report in eleven_days["reports"][1:-1]:
        assert report.steps["verify"]["checks"]["generations"]["remaining"].__len__() == 2


def test_day_one_text_is_absent_from_every_remaining_document(eleven_days):
    backend = eleven_days["backend"]
    registry = backend.generation_registry()
    expired = eleven_days["day1_lines"] - set().union(*(eleven_days["pipe"].store.line_hashes(k) for k in eleven_days["pipe"].store.keys()))
    assert expired, "the synthetic day must have lines of its own"
    for generation in registry.generations():
        gen = generation_id(registry.manifest(generation))
        assert all(p["key"] != "2026-01-01" for p in registry.manifest(generation)["logs"]["partitions"])
        page = backend.catalog.iter_scope(gen, None, Page(size=1000))
        for document in page.items:
            for value in [document.title, *document.fields.values()]:
                assert not {line_hash(line) for line in value.split("\n") if line.strip()} & expired
            assert document.metadata.get("date", "2026-01-02") != "2026-01-01"


def test_day_one_is_absent_from_the_ledger(eleven_days):
    backend = eleven_days["backend"]
    ledger = backend.ledger
    assert ledger.get_raw("raw:learn:day1") is None
    assert ledger.tagged(partition_tag("synthetic", "2026-01-01")) == []
    remaining = set(backend.generation_registry().generations())
    assert set(ledger.referencing_generations()) == remaining
    referenced = {k for g in remaining for k in ledger.refs(g)}
    for prefix in ("extraction:", "embedding:", "pattern_library:"):
        keys = {r.key for r in ledger.raw_entries(prefix, None, 100_000)}
        assert keys <= referenced, f"unreferenced {prefix} keys: {sorted(keys - referenced)[:3]}"
    for generation in eleven_days["generations"].values():
        if generation not in remaining:
            assert ledger.replay(generation) == []
    with sqlite3.connect(backend.root / "runtime.sqlite") as db:
        jobs = {r[0] for r in db.execute("SELECT DISTINCT job_id FROM ledger")}
    assert jobs == remaining


def test_day_one_is_absent_from_cache_and_sessions(eleven_days):
    backend = eleven_days["backend"]
    assert backend.cache.get(eleven_days["cache_keys"]["2026-01-01"]) is None
    assert backend.cache.get(eleven_days["cache_keys"]["2026-01-10"]) is None
    assert backend.cache.get(eleven_days["cache_keys"]["2026-01-11"]) is not None
    assert backend.session.get("s-day1") is None  # ttl <= window


def test_run_reports_rotate_and_audit_records_are_content_free(eleven_days):
    tmp, pipe = eleven_days["tmp"], eleven_days["pipe"]
    reports = sorted(p.name for p in pipe.reports_dir().glob("*.json"))
    assert reports == [f"run-{d.isoformat()}.json" for d in days(FIRST + timedelta(days=1), 10)]
    texts = [p.read_text() for p in pipe.reports_dir().glob("*.json")]
    audit = tmp / "partitions" / "audit" / "synthetic"
    texts += [p.read_text() for p in audit.glob("*.json")]
    assert len(list(audit.glob("*.json"))) == 10
    for generation in eleven_days["backend"].generation_registry().generations():
        texts.append(json.dumps(eleven_days["backend"].generation_registry().manifest(generation)))
    texts.append((pipe.store.root / "synthetic" / "2026-01-01" / "tombstone.json").read_text())
    for text in texts:
        for line in text.split("\n"):
            assert line_hash(line) not in eleven_days["day1_lines"]
        for source_id in eleven_days["day1_source_ids"]:
            assert source_id not in text


def test_verify_detects_an_injected_leak(eleven_days):
    pipe, backend = eleven_days["pipe"], eleven_days["backend"]
    tombstone = pipe.store.tombstone("2026-01-01")
    retention = Retention(eleven_days["cfg"], pipe.store, pipe.write_path, backends={"local": backend}, now=pipe.now)
    assert retention.verify(date(2026, 1, 12))["ok"]
    # a raw reply holding a day-1 source id that nothing references and nothing tagged
    sample_id = sorted(eleven_days["day1_source_ids"])[0]
    backend.ledger.put_raw("raw:leak", json.dumps({"note": f"seen {sample_id}"}), True)
    try:
        result = retention.verify(date(2026, 1, 12))
        assert not result["ok"]
        assert any("expired content" in p for p in result["problems"]) and any("referenced by no remaining generation" in p for p in result["problems"])
    finally:
        backend.ledger.purge(PurgeSpec(unreferenced_prefixes=["raw:leak"]))
    assert retention.verify(date(2026, 1, 12))["ok"]
    assert tombstone is not None


def test_retention_report_lists_what_is_kept(eleven_days):
    pipe, backend = eleven_days["pipe"], eleven_days["backend"]
    report = Retention(eleven_days["cfg"], pipe.store, pipe.write_path, backends={"local": backend}, now=pipe.now).report(date(2026, 1, 12))
    assert report["cutoff"] == "2026-01-02" and report["tombstones"] == ["2026-01-01"]
    assert [p["key"] for p in report["partitions"]] == [d.isoformat() for d in days(FIRST + timedelta(days=1), 10)]
    assert report["generations"][0]["active"] and report["generations"][0]["days"][0] == "2026-01-02"
    assert report["retained_beyond_window"]["decision"] == "O7" and report["retained_beyond_window"]["approved_examples"]
    assert report["next_run"] == {"expire": [], "expire_derived": [], "expire_raw": [], "delete_generations": []}
