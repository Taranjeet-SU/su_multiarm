"""Startup warm-up and session holds of the live log member (unified merge §6.7, the fresh-process
first-search gap of the M2a wiring).

* ``Engine.warmup`` pre-opens the pinned live window in a fresh engine: every pinned unit opened,
  the log stratum's readers, served-row masks and dense matrices built, so the first search opens
  and computes nothing more and returns exactly what a cold engine returns;
* pending vectors are reported by the warm-up (never filled); a pinned slice missing on disk fails
  the warm-up as it would fail the request; without an active generation there is nothing to warm;
* a single sqlite store warms its lexical segment and vector matrix;
* a session holds the slices its turns read (union across turns) until it is closed.

Synthetic only (F0 fixtures, ``tests/live_log_support.py``): sqlite + bm25, the hash embedder, rules
decision, no provider call.
"""

from __future__ import annotations

import shutil
from datetime import date

import pytest

from su_multi_arm import Engine, SearchRequest
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.errors import BackendError
from su_multi_arm.ingestion.log_collection import LiveLogs
from tests.live_log_support import QUERIES, build_support_store, collection, fixture, hits, original_data, unified_data
from tests.log_slices_support import Clock


@pytest.fixture(scope="module")
def fx():
    return fixture()


@pytest.fixture(scope="module")
def built(tmp_path_factory, fx):
    return collection(tmp_path_factory.mktemp("warm"), fx)


def _fresh(engine: Engine) -> Engine:
    """A new engine (cold caches) over the same collection state, as a fresh process sees it."""
    return Engine(engine.config)


def _stratum(engine: Engine):
    return engine.backends["local"].log_stratum("logs")


def test_warmup_pre_opens_the_pinned_live_window(built):
    warm = _fresh(built)
    report = warm.warmup()
    local = report["backends"]["local"]
    gen = warm.write_path.pin()[0]
    refs = warm.backends["local"].pinned_refs(gen)
    log_units = [r for r in refs if r.member == "logs"]
    assert report["generation"] == gen.generation and local["units"] == len(refs) and local["log_members"] == ["logs"] and local["dense_pending"] == []
    stratum = _stratum(warm)
    opened, computed, matrices = stratum.units.opened, stratum.served.computed, len(stratum.dense._matrices)
    assert opened == len(log_units) and computed == 1 and matrices == len(log_units)
    loads = warm.backends["local"].stats["collection.unit_loads"]
    cold = _fresh(built)
    for query in QUERIES:
        assert hits(warm.search(SearchRequest(query=query))) == hits(cold.search(SearchRequest(query=query))), query
    # the first searches opened, verified and computed nothing the warm-up had not already
    assert (stratum.units.opened, stratum.served.computed, len(stratum.dense._matrices)) == (opened, computed, matrices)
    assert warm.backends["local"].stats["collection.unit_loads"] == loads
    assert _stratum(cold).units.opened == len(log_units)  # the cold engine paid them on its first search


def test_warmup_reports_pending_vectors_and_never_fills_them(tmp_path, fx):
    engine = collection(tmp_path, fx, embed=False)
    report = engine.warmup()["backends"]["local"]
    assert report["dense_pending"] and all(u.startswith(("slice:", "logs/")) for u in report["dense_pending"])
    assert engine.warmup()["backends"]["local"]["dense_pending"] == report["dense_pending"]  # still pending: nothing was embedded


def test_a_pinned_slice_missing_on_disk_fails_the_warmup(tmp_path, fx):
    engine = collection(tmp_path, fx)
    fresh = _fresh(engine)
    gen = fresh.write_path.pin()[0]
    slice_id = next(iter(fresh.view.attach().live.view(gen.generation).slices["logs"].values()))[0]
    shutil.rmtree(fresh.view.attach().live.ledger("logs").unit_dir(slice_id))
    with pytest.raises(BackendError, match="no longer on disk"):
        fresh.warmup()


def test_without_an_active_generation_there_is_nothing_to_warm(tmp_path, fx):
    original = build_support_store(tmp_path, fx)
    engine = Engine(validate_config(unified_data(tmp_path, fx, original, logs=True)))
    report = engine.warmup()
    assert set(report) == {"skipped", "seconds"} and "no active collection generation" in report["skipped"]


def test_a_single_sqlite_store_warms_its_segment_and_vectors(tmp_path, fx):
    build_support_store(tmp_path, fx)
    engine = Engine(validate_config(original_data(tmp_path, fx, ("lexical", "dense", "structured"))))
    report = engine.warmup()
    gen = engine.write_path.pin()[0]
    backend = engine.backends["local"]
    assert report["backends"]["local"]["vector_rows"] == backend.vector.count(gen) > 0
    assert gen.prefix() in backend.lexical._segments and (gen.prefix(), "default") in backend.vector._loaded


def test_a_session_holds_the_slices_its_turns_read_until_it_is_closed(tmp_path, fx):
    clock = Clock(2_000_000_000.0)
    engine = collection(tmp_path, fx, clock=clock)
    live: LiveLogs = engine.view.attach().live
    sid = engine.create_session()
    first = engine.search(SearchRequest(query="crawler fetch timeout"), session_id=sid)
    gen1 = first.generation
    turn1 = {s for days in live.view(gen1).slices.values() for ids in days.values() for s in ids}
    assert turn1 and set(live._held[f"session:{sid}"]) == turn1
    live.release(gen1)  # the request lease ends; the session still holds what its turn read
    assert turn1 <= live.held_slices()
    ledger = live.ledger("logs")
    ledger.compact(date(2026, 11, 9), "base")  # the next turn pins a new slice vector
    second = engine.search(SearchRequest(query="scheduler job halted"), session_id=sid)
    turn2 = {s for days in live.view(second.generation).slices.values() for ids in days.values() for s in ids}
    assert turn2 != turn1 and set(live._held[f"session:{sid}"]) == turn1 | turn2  # earlier turns stay held
    live.release(second.generation)
    clock.now += engine.config.collection.slices.gc_grace_seconds + 1
    retired = turn1 - turn2
    assert retired and not retired & set(ledger.gc())  # retired by the compaction but held by the session
    engine.close_session(sid)
    assert not live.held_slices() and retired <= set(ledger.gc())
