"""QA-05: session turns and session budgets under concurrency.

Store level: ``SessionStore.touch(..., expected_version=...)`` is a compare-and-set on every store
(memory and sqlite here; the conformance kit runs the same check on the MongoDB, PostgreSQL and
SQLAlchemy stores in the real-service tier). Manager level: ``SessionManager.update`` re-applies a
change to the freshest record, also across managers that share no lock (separate processes).
Engine level: 30 concurrent turns on one session are all recorded, and racing requests never take
more than ``budgets.session`` (the clip and the hold are one compare-and-set). Synthetic data.
"""

from __future__ import annotations

import threading
import time

import pytest

from su_multi_arm import Document, Engine, SearchRequest
from su_multi_arm.backends.memory import MemoryBackend
from su_multi_arm.backends.sqlite import SqliteBackend
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.errors import BudgetExhausted, SessionConflict
from su_multi_arm.core.types import GenerationId, SessionRecord, SessionReservation
from su_multi_arm.models import Answer, Claim
from su_multi_arm.providers import JsonModel
from su_multi_arm.runtime.acl import ANONYMOUS
from su_multi_arm.runtime.budget import BudgetLimits
from su_multi_arm.runtime.session import SessionManager
from su_multi_arm.runtime.turns import SessionTurns

TURNS = 30
GEN = GenerationId(dataset="demo", generation="a" * 32, index_format=3, fingerprint="f" * 64)


def store(kind: str, tmp_path):
    return (MemoryBackend() if kind == "memory" else SqliteBackend(tmp_path / "s")).session


def run_all(target, n: int) -> list[BaseException]:
    errors: list[BaseException] = []
    barrier = threading.Barrier(n)

    def wrapped(i):
        barrier.wait()
        try:
            target(i)
        except BaseException as exc:  # collected and asserted by the caller
            errors.append(exc)

    threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


# --- store-level compare-and-set ----------------------------------------------------------------


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_store_touch_is_a_compare_and_set(kind, tmp_path):
    sessions = store(kind, tmp_path)
    sessions.create(SessionRecord(id="s1", principal_id="p", dataset="demo", created_at=0.0, last_seen_at=0.0))
    first = sessions.get("s1")
    assert first.version == 0
    sessions.touch("s1", 1.0, first.model_copy(update={"usage": {"calls": 1.0}}), expected_version=0)
    stored = sessions.get("s1")
    assert stored.version == 1 and stored.usage == {"calls": 1.0} and stored.last_seen_at == 1.0
    with pytest.raises(SessionConflict):  # a writer that read version 0 lost the race: nothing written
        sessions.touch("s1", 2.0, first.model_copy(update={"usage": {"calls": 9.0}}), expected_version=0)
    assert sessions.get("s1").usage == {"calls": 1.0} and sessions.get("s1").version == 1
    sessions.touch("s1", 3.0)  # an unconditional touch still bumps the version
    assert sessions.get("s1").version == 2 and sessions.get("s1").last_seen_at == 3.0


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_concurrent_compare_and_set_writers_never_both_win(kind, tmp_path):
    sessions = store(kind, tmp_path)
    sessions.create(SessionRecord(id="s1", principal_id="p", dataset="demo", created_at=0.0, last_seen_at=0.0))
    base = sessions.get("s1")
    wins: list[int] = []

    def write(i):
        try:
            sessions.touch("s1", 1.0, base.model_copy(update={"usage": {"calls": float(i)}}), expected_version=0)
            wins.append(i)
        except SessionConflict:
            pass

    assert run_all(write, 12) == []
    assert len(wins) == 1 and sessions.get("s1").usage == {"calls": float(wins[0])}


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_managers_without_a_shared_lock_lose_no_update(kind, tmp_path):
    """Two managers on one store model two processes: only the store CAS protects them."""
    sessions = store(kind, tmp_path)
    managers = [SessionManager(sessions, ttl_seconds=600) for _ in range(2)]
    sid = managers[0].create(ANONYMOUS, "demo").id

    def bump(i):
        manager = managers[i % 2]
        manager.update(sid, lambda r: r.model_copy(update={"usage": {"calls": r.usage.get("calls", 0.0) + 1}}))

    assert run_all(bump, TURNS) == []
    record = sessions.get(sid)
    assert record.usage == {"calls": float(TURNS)} and record.version == TURNS


class _Docs:
    """Minimal single-document Mongo collection: ``_id``/field-equality filters only."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.lock = threading.Lock()

    def _match(self, row, query):
        return row is not None and all(row.get(k) == v for k, v in query.items())

    def insert_one(self, doc):
        self.rows[doc["_id"]] = dict(doc)

    def find_one(self, query):
        row = self.rows.get(query["_id"])
        return dict(row) if self._match(row, query) else None

    def update_one(self, query, update):
        with self.lock:  # MongoDB applies a single-document update atomically
            row = self.rows.get(query["_id"])
            matched = self._match(row, query)
            if matched:
                row.update(update["$set"])
        return type("Result", (), {"matched_count": int(matched)})()

    def create_index(self, *args, **kwargs):
        return None


def test_mongodb_session_touch_is_a_compare_and_set_on_the_raw_document():
    pytest.importorskip("pymongo", reason="optional extra `mongodb` not installed")
    from su_multi_arm.backends.mongodb import MongoBackend

    docs = _Docs()
    client = type("Client", (), {"__getitem__": lambda self, name: type("Db", (), {"__getitem__": lambda s, n: docs})()})()
    sessions = MongoBackend("mongodb://unused", "db", "pfx", client=client).session
    sessions.create(SessionRecord(id="s1", principal_id="p", dataset="demo", created_at=0.0, last_seen_at=0.0))
    base = sessions.get("s1")
    sessions.touch("s1", 1.0, base.model_copy(update={"usage": {"calls": 1.0}}), expected_version=0)
    with pytest.raises(SessionConflict):
        sessions.touch("s1", 2.0, base.model_copy(update={"usage": {"calls": 5.0}}), expected_version=0)
    assert sessions.get("s1").usage == {"calls": 1.0} and sessions.get("s1").version == 1
    manager = SessionManager(sessions, ttl_seconds=600)
    assert run_all(lambda i: manager.update("s1", lambda r: r.model_copy(update={"usage": {"calls": r.usage["calls"] + 1}})), 10) == []
    assert sessions.get("s1").usage == {"calls": 11.0}


# --- engine turns ---------------------------------------------------------------------------------


def config(kind: str, tmp_path, **extra):
    backend = {"kind": "memory"} if kind == "memory" else {"kind": "sqlite", "path": str(tmp_path / "state")}
    data = {
        "schema_version": 2,
        "dataset": "demo",
        "backends": {"local": backend},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 64}, "generator": {"provider": "openai_compatible", "model": "fake"}}},
        "pipeline": {"arms": {"lexical": {"backend": "local"}}},
    }
    data.update(extra)
    return validate_config(data)


def engine_for(kind, tmp_path, **extra) -> Engine:
    engine = Engine(config(kind, tmp_path, **extra))
    engine.ingest([Document(id=f"d{i}", dataset="demo", title=f"Note {i}", fields={"body": f"rounding note number {i}"}) for i in range(6)])
    return engine


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_thirty_concurrent_turns_are_all_recorded(kind, tmp_path):
    engine = engine_for(kind, tmp_path)
    sid = engine.create_session()
    errors = run_all(lambda i: engine.search(SearchRequest(query=f"rounding {i}"), session_id=sid), TURNS)
    assert errors == []
    record = engine.session(sid).record
    assert len(record.turns) == TURNS and len({t.trace_id for t in record.turns}) == TURNS
    assert record.reservations == {}  # no session budget configured: nothing is held


def slow_citing_generator(calls: list[int], delay: float = 0.05):
    lock = threading.Lock()

    def respond(self, system, payload, schema, budget, stage):
        budget.reserve()
        with lock:
            calls.append(1)
        time.sleep(delay)  # keep turns overlapping
        budget.record({"stage": stage, "success": True, "usage": {}})
        target = payload["evidence"][0]["id"]
        return Answer(answer="Grounded answer.", claims=[Claim(text="a fact", evidence_ids=[target])], citations=[target])

    return respond


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_racing_answers_never_exceed_the_session_call_budget(kind, tmp_path, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(JsonModel, "call", slow_citing_generator(calls))
    engine = engine_for(kind, tmp_path, budgets={"max_llm_calls": 1, "session": {"max_llm_calls": 3}})
    sid = engine.create_session()
    errors = run_all(lambda i: engine.answer(SearchRequest(query=f"rounding {i}"), session_id=sid), TURNS)
    assert all(isinstance(e, BudgetExhausted) and e.kind == "session_calls" for e in errors), errors
    record = engine.session(sid).record
    assert len(calls) <= 3 and record.usage["calls"] == len(calls) <= 3
    assert len(record.turns) == TURNS - len(errors) == len(calls)  # every turn that ran is recorded
    assert record.reservations == {}  # every hold was replaced by the actual usage
    with pytest.raises(BudgetExhausted, match="session_calls"):
        engine.answer(SearchRequest(query="rounding"), session_id=sid)


def test_session_hold_counts_until_finished_or_expired():
    now = {"t": 0.0}
    backend = MemoryBackend()
    manager = SessionManager(backend.session, ttl_seconds=10_000, clock=lambda: now["t"])
    budget = type("B", (), {"max_llm_calls": 4, "max_input_tokens": None, "max_output_tokens": None, "max_usd": None})()
    turns = SessionTurns(manager, "demo", session_budget=budget)
    session = turns.create(ANONYMOUS)
    limits = BudgetLimits(max_llm_calls=3, deadline_seconds=60)
    first = turns.reserve(turns.open(ANONYMOUS, session.id), limits)
    assert first.max_llm_calls == 3
    second = turns.reserve(turns.open(ANONYMOUS, session.id), limits)
    assert second.max_llm_calls == 1  # clipped by the first turn's hold
    with pytest.raises(BudgetExhausted):
        turns.reserve(turns.open(ANONYMOUS, session.id), limits)
    now["t"] = 60 + 300 + 1  # past deadline + grace: abandoned holds stop counting
    assert turns.reserve(turns.open(ANONYMOUS, session.id), limits).max_llm_calls == 3
    assert len(backend.session.get(session.id).reservations) == 1  # expired holds were pruned


def test_release_drops_the_hold_without_usage():
    backend = MemoryBackend()
    budget = type("B", (), {"max_llm_calls": 2, "max_input_tokens": None, "max_output_tokens": None, "max_usd": None})()
    turns = SessionTurns(SessionManager(backend.session, ttl_seconds=600), "demo", session_budget=budget)
    session = turns.open(ANONYMOUS, turns.create(ANONYMOUS).id)
    turns.reserve(session, BudgetLimits(max_llm_calls=5, deadline_seconds=60))
    assert isinstance(next(iter(session.record.reservations.values())), SessionReservation)
    turns.release(session)
    stored = backend.session.get(session.id)
    assert stored.reservations == {} and stored.usage == {} and session.reservation is None
