"""Write leases, fencing and compare-and-set activation (risk R1 of the 2.0.0a1 completeness review).

Registry level: every registry refuses a second writer while a lease is live (typed
``WriteConflict(held)``, never a wait), takes a lease over only after its expiry (registries whose
lease can be abandoned), refuses release/renewal by a non-owner, and activates only from the
expected generation. Write-path level: two concurrent writers (threads) on sqlite and on the memory
backend — exactly one activates, the other fails typed and nothing it was told succeeded is lost; a
writer whose lease was taken over, or whose base generation is no longer ``CURRENT``, never switches
the pointer. Real-server variants live in ``tests/conformance`` (``external``).
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from su_multi_arm.backends.memory import MemoryBackend
from su_multi_arm.backends.sqlite import SqliteBackend
from su_multi_arm.core.component import Dependencies
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.errors import BackendError, WriteConflict
from su_multi_arm.core.types import Document
from su_multi_arm.extraction.base import BuiltinExtractor
from su_multi_arm.ingestion.lease import Lease
from su_multi_arm.ingestion.release import FileGenerationRegistry, MemoryGenerationRegistry
from su_multi_arm.ingestion.write import IngestConflict, IngestError, WritePath
from su_multi_arm.providers import Embedder
from tests.conformance.lease_kit import check_compare_and_set, check_lease

CONFIG = {
    "schema_version": 2,
    "dataset": "demo",
    "backends": {"local": {"kind": "memory"}},
    "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
    "metadata_schema": {"fields": [{"name": "source", "type": "string"}]},
    "pipeline": {"arms": {"dense": {"backend": "local"}, "lexical": {"backend": "local"}, "structured": {"backend": "local"}}},
    "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 32}}},
}


def manifests(n: int = 3) -> list[dict]:
    return [{"dataset": "demo", "generation": c * 32, "index_format": 3, "fingerprint": "f" * 64} for c in "abc"[:n]]


def doc(doc_id: str, body: str = "rounding applies per invoice line") -> Document:
    return Document(id=doc_id, dataset="demo", title=f"Title {doc_id}", fields={"body": body}, metadata={"source": "kb"})


# --- registry level ------------------------------------------------------------------------------


def test_memory_registry_lease_and_compare_and_set():
    clock = {"now": 1000.0}
    shared = MemoryGenerationRegistry(clock=lambda: clock["now"])

    def advance(seconds):
        clock["now"] += seconds

    check_lease(lambda: shared, advance, 120.0)
    check_compare_and_set(MemoryGenerationRegistry(), manifests())


def test_file_registry_flock_lease_and_compare_and_set(tmp_path):
    # flock is released by the kernel with the holder's file: no expiry, no takeover
    check_lease(lambda: FileGenerationRegistry(tmp_path / "state"), None, 120.0)
    check_compare_and_set(FileGenerationRegistry(tmp_path / "state"), manifests())


def test_file_registry_lease_excludes_other_threads(tmp_path):
    registry = FileGenerationRegistry(tmp_path / "state")
    errors: list[BaseException] = []
    with registry.lock():

        def other():
            try:
                with FileGenerationRegistry(tmp_path / "state").lock():
                    pass
            except BaseException as exc:  # noqa: BLE001 - collected for the assertion
                errors.append(exc)

        thread = threading.Thread(target=other)
        thread.start()
        thread.join(5)
    assert len(errors) == 1 and isinstance(errors[0], WriteConflict) and errors[0].reason == "held"


def test_sql_registry_lease_and_compare_and_set_on_sqlite(tmp_path):
    pytest.importorskip("sqlalchemy")
    from su_multi_arm.backends.sql import SqlBackend

    clock = {"now": 100.0}
    backend = SqlBackend(f"sqlite:///{tmp_path / 'sql.db'}", "lease", clock=lambda: clock["now"])
    try:
        check_lease(backend.generation_registry, lambda s: clock.update(now=clock["now"] + s), backend.generation_registry().lock_ttl_seconds)
        check_compare_and_set(backend.generation_registry(), manifests())
        # a lease row written before owner tokens (``acquired_at`` only) expires ttl after it was taken
        meta = backend.rt["meta"]
        with backend.tx("test") as conn:
            conn.execute(meta.insert().values(key="write_lock", value=json.dumps({"acquired_at": clock["now"]})))
        with pytest.raises(WriteConflict):
            with backend.generation_registry().lock():
                pass
        clock["now"] += backend.generation_registry().lock_ttl_seconds + 1
        with backend.generation_registry().lock():
            pass
    finally:
        backend.drop_all()
        backend.close()


class FakeMeta:
    """The few pymongo collection operations the Mongo registry uses, with MongoDB's single-document
    atomicity (enough for owner-conditional writes; ``$or``, ``$lte``, ``$exists``)."""

    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.lock = threading.Lock()

    @staticmethod
    def _match(doc, flt) -> bool:
        for key, want in flt.items():
            if key == "$or":
                if not any(FakeMeta._match(doc, sub) for sub in want):
                    return False
            elif isinstance(want, dict):
                for op, value in want.items():
                    if op == "$lte" and not (key in doc and doc[key] <= value):
                        return False
                    if op == "$exists" and (key in doc) != value:
                        return False
            elif doc.get(key) != want:
                return False
        return True

    def _find(self, flt):
        found = self.docs.get(flt["_id"])
        return found if found is not None and self._match(found, flt) else None

    def insert_one(self, doc):
        from pymongo.errors import DuplicateKeyError

        with self.lock:
            if doc["_id"] in self.docs:
                raise DuplicateKeyError("duplicate key")
            self.docs[doc["_id"]] = dict(doc)

    def find_one(self, flt):
        with self.lock:
            return self._find(flt)

    def _apply(self, doc, update):
        doc.update(update.get("$set", {}))
        for key in update.get("$unset", {}):
            doc.pop(key, None)

    def find_one_and_update(self, flt, update):
        with self.lock:
            doc = self._find(flt)
            if doc is None:
                return None
            before = dict(doc)
            self._apply(doc, update)
            return before

    def update_one(self, flt, update):
        with self.lock:
            doc = self._find(flt)
            if doc is not None:
                self._apply(doc, update)
            return SimpleNamespace(matched_count=int(doc is not None))

    def replace_one(self, flt, doc, upsert=False):
        with self.lock:
            self.docs[flt["_id"]] = dict(doc)

    def delete_one(self, flt):
        with self.lock:
            doc = self._find(flt)
            if doc is not None:
                del self.docs[flt["_id"]]
            return SimpleNamespace(deleted_count=int(doc is not None))


def test_mongo_registry_lease_and_compare_and_set_with_a_fake_collection():
    pytest.importorskip("pymongo")
    from su_multi_arm.backends.mongodb.registry import MongoGenerationRegistry

    clock = {"now": 100.0}
    meta = FakeMeta()
    backend = SimpleNamespace(collection=lambda name: meta, clock=lambda: clock["now"])
    check_lease(lambda: MongoGenerationRegistry(backend), lambda s: clock.update(now=clock["now"] + s), MongoGenerationRegistry(backend).lock_ttl_seconds)
    check_compare_and_set(MongoGenerationRegistry(backend), manifests())
    # a lease document written before owner tokens (``acquired_at`` only)
    meta.docs["write_lock"] = {"_id": "write_lock", "acquired_at": clock["now"]}
    with pytest.raises(WriteConflict):
        with MongoGenerationRegistry(backend).lock():
            pass
    clock["now"] += 121
    with MongoGenerationRegistry(backend).lock() as lease:
        assert meta.docs["write_lock"]["owner"] == lease.token and "acquired_at" not in meta.docs["write_lock"]


def test_postgres_registry_release_by_a_non_owner_session_unlocks_nothing():
    from tests.unit.test_postgres_adapter import FakeConnection, make

    holder = FakeConnection()
    backend = make(FakeConnection(), holder)  # schema transaction, then the lease's own session
    registry = backend.generation_registry()
    with registry.lock() as lease:
        assert registry.lease_release("not-the-owner") is False and not holder.closed
        assert lease.token in registry._sessions
    assert holder.closed and lease.released is True


def test_lease_heartbeat_renews_and_a_failed_renewal_window_loses_the_lease():
    calls = {"renew": 0}

    class Store:
        owner = None
        failing = False

        def lease_acquire(self, token, ttl):
            self.owner = token
            return True

        def lease_renew(self, token, ttl):
            calls["renew"] += 1
            if self.failing:
                raise OSError("store unreachable")
            return token == self.owner

        def lease_release(self, token):
            return token == self.owner

    store = Store()
    lease = Lease(store, "fake", ttl=0.2).acquire()
    try:
        time.sleep(0.25)
        assert calls["renew"] >= 2 and lease.lost is None  # heartbeat every ttl/4
        store.failing = True
        time.sleep(0.3)  # no successful renewal for a whole ttl
        with pytest.raises(WriteConflict) as info:
            lease.check()
        assert info.value.reason == "lost" and "no successful renewal" in str(info.value)
    finally:
        assert lease.release() is True


# --- write-path level ----------------------------------------------------------------------------


class Gate:
    """Pauses the first ``encode`` call of one writer until released (the writer holds its lease)."""

    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()
        self.hook = None

    def wrap(self, embedder):
        original = embedder.encode

        def encode(texts, query=False):
            if not self.entered.is_set():
                self.entered.set()
                if self.hook is not None:
                    self.hook()
                assert self.release.wait(10)
            return original(texts, query)

        embedder.encode = encode
        return embedder


def make_path(cfg, backend, embedder=None):
    extractor = BuiltinExtractor(cfg.extraction, Dependencies(providers={}))
    return WritePath(cfg, {"local": backend}, extractor=extractor, embedder=embedder or Embedder(cfg.models.roles.embedder))


def run(target, results, key):
    def body():
        try:
            results[key] = target()
        except BaseException as exc:  # noqa: BLE001 - collected for the assertions
            results[key] = exc

    thread = threading.Thread(target=body)
    thread.start()
    return thread


@pytest.mark.parametrize("kind", ["sqlite", "memory"])
def test_two_concurrent_writers_exactly_one_activates_the_other_fails_typed(kind, tmp_path):
    cfg = validate_config(CONFIG)
    shared = MemoryBackend() if kind == "memory" else None

    def backend():
        return shared if shared is not None else SqliteBackend(tmp_path / "state")

    seed = make_path(cfg, backend()).ingest([doc("base")])
    gate = Gate()
    writer_a = make_path(cfg, backend(), gate.wrap(Embedder(cfg.models.roles.embedder)))
    writer_b = make_path(cfg, backend())
    results: dict = {}
    thread_a = run(lambda: writer_a.ingest([doc("from-a")]), results, "a")
    assert gate.entered.wait(10)  # A holds the lease and is building
    thread_b = run(lambda: writer_b.ingest([doc("from-b")]), results, "b")
    thread_b.join(10)
    gate.release.set()
    thread_a.join(10)
    assert isinstance(results["b"], WriteConflict) and results["b"].reason == "held"
    assert isinstance(results["a"], dict) and results["a"]["status"] == "complete"
    gen, current = make_path(cfg, backend()).current()
    assert current["generation"] == results["a"]["generation"] != seed["generation"]
    catalog = backend().catalog
    assert {d.id for d in catalog.get(gen, ["base", "from-a", "from-b"])} == {"base", "from-a"}
    # B was told; its retry builds on A's generation and nothing is lost
    retry = writer_b.ingest([doc("from-b")])
    gen, _ = make_path(cfg, backend()).current()
    assert gen.generation == retry["generation"]
    assert {d.id for d in backend().catalog.get(gen, ["base", "from-a", "from-b"])} == {"base", "from-a", "from-b"}


def test_writer_whose_lease_was_taken_over_never_switches_current():
    """A's lease expires mid-build (missed renewals), B takes it over and activates; A then fails
    with ``IngestConflict(lost)``, CURRENT stays B's and B's documents are all there."""
    cfg = validate_config(CONFIG)
    clock = {"now": 1000.0}
    backend = MemoryBackend(clock=lambda: clock["now"])
    seed = make_path(cfg, backend).ingest([doc("base")])
    gate = Gate()
    writer_a = make_path(cfg, backend, gate.wrap(Embedder(cfg.models.roles.embedder)))
    writer_b = make_path(cfg, backend)
    results: dict = {}
    thread_a = run(lambda: writer_a.ingest([doc("from-a")]), results, "a")
    assert gate.entered.wait(10)
    clock["now"] += 10_000  # A missed every renewal: its lease expired
    results["b"] = writer_b.ingest([doc("from-b")])
    gate.release.set()
    thread_a.join(10)
    error = results["a"]
    assert isinstance(error, IngestConflict) and isinstance(error, WriteConflict) and isinstance(error, IngestError)
    assert error.reason == "lost" and error.failure["category"] == "write_conflict:lost" and error.failure["stage"] == "activate"
    gen, current = make_path(cfg, backend).current()
    assert current["generation"] == results["b"]["generation"] != seed["generation"]
    assert {d.id for d in backend.catalog.get(gen, ["base", "from-a", "from-b"])} == {"base", "from-b"}
    assert backend.generation_registry()._lease is None  # B released its own lease; A deleted nothing of B's
    assert error.failure["generation"] in {f["generation"] for f in backend.generation_registry().failures()}


def test_writer_whose_base_generation_moved_fails_compare_and_set(monkeypatch):
    """Even with a valid lease, activation is a compare-and-set on the generation the build started
    from: a pointer moved underneath (an operator, a writer that ignored the lease) is never
    overwritten."""
    cfg = validate_config(CONFIG)
    backend = MemoryBackend()
    seed = make_path(cfg, backend).ingest([doc("base")])
    other = make_path(cfg, MemoryBackend()).ingest([doc("elsewhere")])
    registry = backend.generation_registry()
    gate = Gate()
    gate.release.set()

    def move_current():
        registry.write_manifest({**seed, "generation": other["generation"]})
        registry.activate({**seed, "generation": other["generation"]})  # unconditional, outside any lease

    gate.hook = move_current
    writer = make_path(cfg, backend, gate.wrap(Embedder(cfg.models.roles.embedder)))
    with pytest.raises(IngestConflict) as info:
        writer.ingest([doc("from-writer")])
    assert info.value.reason == "stale_base" and info.value.failure["category"] == "write_conflict:stale_base"
    assert registry.current_generation() == other["generation"]


def test_held_lease_error_is_backward_compatible_backend_error(tmp_path):
    registry = FileGenerationRegistry(tmp_path / "state")
    with registry.lock():
        with pytest.raises(BackendError, match="write lock is held by another writer"):
            with FileGenerationRegistry(tmp_path / "state").lock():
                pass
