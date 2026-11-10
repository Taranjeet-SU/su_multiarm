"""Unified merge, M2a wired into the partition core (lane LB): the live log member's configuration,
its ledger on LA's real id directory (supersede-before-register for compaction and cross-day
continuation, F0 chains B/C), close registration, request/session slice pins and their release.
Synthetic only (F0 fixtures; ``tests/live_log_support.py``)."""

from __future__ import annotations

from datetime import date

import pytest

from su_multi_arm import Engine
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.core.types import LexicalQuery
from su_multi_arm.ingestion.id_directory import IdConflict
from su_multi_arm.ingestion.log_collection import live_members, member_recipe, request_id
from su_multi_arm.ingestion.log_slices import SliceRefused
from su_multi_arm.testing.fixtures.unified.collection import incident_id
from tests.live_log_support import ARCHIVE_DAYS, LIVE, at, build_support_store, collection, fixture, unified_data
from tests.log_slices_support import Clock, doc


@pytest.fixture(scope="module")
def fx():
    return fixture()


def _data(tmp_path, fx, **logs):
    original = build_support_store(tmp_path, fx)
    data = unified_data(tmp_path, fx, original, logs=True)
    data["collection"]["members"]["logs"].update(logs)
    return data


# --- configuration -----------------------------------------------------------------------------------


def test_the_live_member_is_built_from_the_collection_config(tmp_path, fx):
    engine = Engine(validate_config(_data(tmp_path, fx)))
    registry = engine.view.attach()
    assert live_members(engine.config) == ["logs"] and registry.live.members == ["logs"]
    ledger = registry.live.ledger("logs")
    assert ledger.root == registry.root / "slices" / "logs" and ledger.directory is registry.directory
    assert ledger.embed_fields == {k: tuple(v) for k, v in engine.config.collection.members["logs"].embed_fields.items()}
    assert ledger.settings == engine.config.collection.slices and ledger.metadata_schema == engine.config.metadata_schema
    assert ledger.catalog_kinds == frozenset({"signature"})
    recipe = member_recipe(engine.config, "logs")
    assert ledger.recipe == recipe and recipe.embedding_space.template == "engine_template" and ledger.template == "engine_template"
    assert recipe.content["bm25"] == {"lowercase": True, "stemming": "none", "include_title": False}
    assert ledger.pins.rows is registry.live.slice_pins().rows  # request pins and slice gc share one pin store


@pytest.mark.parametrize("change, path", [
    ({"sources": ["logs", "audit"]}, "collection.members.logs.sources"),
])
def test_unservable_live_members_are_refused(tmp_path, fx, change, path):
    with pytest.raises(ConfigError) as caught:
        Engine(validate_config(_data(tmp_path, fx, **change)))
    assert caught.value.path == path


def test_member_rrf_is_refused_for_the_live_member(tmp_path, fx):
    data = _data(tmp_path, fx)
    data["collection"]["lexical"] = {"logs": {"stats": "member_rrf"}}
    with pytest.raises(ConfigError, match="query-time statistics"):
        Engine(validate_config(data))


def test_the_write_path_never_builds_the_live_member(tmp_path, fx):
    engine = Engine(validate_config(_data(tmp_path, fx)))
    with pytest.raises(ValueError, match="live log member"):
        engine.build_member("logs", "day:2026-11-04", fx.members["logs/day:2026-11-04"])


# --- the id directory: supersede-before-register (F0 chains B/C, compaction) ----------------------------


def test_slices_register_on_the_real_id_directory_with_the_f0_chains(tmp_path, fx):
    engine = collection(tmp_path, fx)
    registry = engine.view.attach()
    directory = registry.directory
    gen = engine.write_path.pin()[0]
    view = engine.backends["local"].view(gen)
    # chain B: archived on 2026-11-06, continued into the live window: the slice registered with
    # replaces=["logs/day:2026-11-06"], so the archive row is superseded only for that id
    chain_b = incident_id("chainB_live")
    entry = directory.resolve(chain_b, view)
    assert entry is not None and entry.unit_id.startswith("slice:2026-11-07:")
    archived = [e for e in directory.entries("logs/day:2026-11-06") if e.doc_id == chain_b]
    assert archived and archived[0].superseded_by == entry.unit_id
    assert directory.unit("logs/day:2026-11-06")["status"] == "live"
    # only the continuing episode's ids (its incident and signature_day) are superseded, each by the slice serving it
    owners = engine.backends["local"].log_stratum("logs").owners(view)
    continued = [e for e in directory.entries("logs/day:2026-11-06") if e.superseded_by is not None]
    assert {e.kind for e in continued} == {"incident", "signature_day"}
    assert all(owners[e.doc_id] == e.superseded_by for e in continued)
    # chain C: superseded across provisional days
    assert directory.resolve(incident_id("chainC_v2"), view).unit_id.startswith("slice:2026-11-09:")
    # the gap merge's tombstoned ids resolve to nothing in this view
    for tomb in fx.manifest["expected"]["gap_merge"]["tombstones"]:
        assert directory.resolve(tomb, view) is None
    # every id the stratum serves resolves in the directory for this view
    assert all(directory.resolve(d, view) is not None for d in owners)


def test_compaction_and_gc_follow_the_directory(tmp_path, fx):
    clock = Clock(2_000_000_000.0)
    engine = collection(tmp_path, fx, clock=clock)
    live = engine.view.attach().live
    directory = engine.view.attach().directory
    ledger = live.ledger("logs")
    day = date(2026, 11, 8)
    before = ledger.vector()[day]
    base = ledger.compact(day, "base")
    assert base is not None and all(directory.unit(s)["status"] == "retired" for s in before)
    assert directory.unit(base.slice_id)["status"] == "live"
    # no request pinned the old slices: after the grace they are deleted and purged from the directory
    clock.now += engine.config.collection.slices.gc_grace_seconds + 1
    deleted = ledger.gc()
    assert set(before) <= set(deleted) and all(directory.unit(s) is None for s in before)


def test_a_conflicting_id_is_still_refused(tmp_path, fx):
    engine = collection(tmp_path, fx, live=False, embed=False)
    ledger = engine.view.attach().live.ledger("logs")
    # an incident id of an archive day that is not a recorded continuation (older than the
    # archived-row window): the directory refuses the duplicate and the ledger rolls back
    held = next(e.doc_id for e in engine.view.attach().directory.entries("logs/day:2026-11-04") if e.kind == "incident")
    with ledger._db() as db:
        db.execute("DELETE FROM archived WHERE doc_id=?", (held,))
    with pytest.raises(IdConflict):
        ledger.seal(date(2026, 11, 9), "five_minute", [doc(held, "a late copy")], as_of=at(date(2026, 11, 9), 1))
    assert date(2026, 11, 9) not in ledger.vector()


# --- close registration --------------------------------------------------------------------------------


def test_close_registers_the_day_and_the_new_catalog_version(tmp_path, fx):
    engine = collection(tmp_path, fx, live=False, embed=False)
    registry = engine.view.attach()
    directory = registry.directory
    manifest = registry.generations.current()
    units = {m["member"] + "/" + m["key"] for m in manifest["members"]}
    assert units == {"support/" + next(m["key"] for m in manifest["members"] if m["member"] == "support"), "logs/catalog", *(f"logs/day:{d}" for d in ARCHIVE_DAYS)}
    assert all(directory.unit(u)["status"] == "live" for u in units)
    catalog = next(m for m in manifest["members"] if m["key"] == "catalog")
    assert directory.count("logs/catalog") == sum(catalog["counts"].values())
    # three closes, three activations (≤ 6/day: the catalog rides in the day's compose)
    assert len(registry.generations.generations()) == 1 + len(ARCHIVE_DAYS)
    # the closed days refuse late refreshes
    with pytest.raises(SliceRefused, match="closed"):
        registry.live.ledger("logs").seal(date(2026, 11, 4), "five_minute", [doc("logs:incident:late", "late")], as_of=at(date(2026, 11, 4), 23))


def test_the_catalog_check_reads_the_active_catalog_member(tmp_path, fx):
    engine = collection(tmp_path, fx, live=False, embed=False)
    live = engine.view.attach().live
    shas = live.catalog_shas("logs")
    signatures = {d.id for d in fx.members["logs/day:2026-11-06"] if d.kind == "signature"}
    assert signatures <= set(shas)
    ledger = live.ledger("logs")
    known = next(d for d in fx.members["logs/day:2026-11-06"] if d.kind == "signature")
    assert ledger.seal(date(2026, 11, 7), "five_minute", [known], as_of=at(date(2026, 11, 7), 1)) is None  # a known catalog doc is not resealed


# --- request and session pins ---------------------------------------------------------------------------


def test_request_pins_hold_slices_until_the_lease_lapses(tmp_path, fx):
    clock = Clock(2_000_000_000.0)
    engine = collection(tmp_path, fx, clock=clock)
    live = engine.view.attach().live
    ledger = live.ledger("logs")
    gen = engine.write_path.pin()[0]
    day = date(2026, 11, 9)
    pinned = ledger.vector()[day]
    assert set(pinned) <= live.held_slices() and all(live.slice_pins().pinned(s) for s in pinned)
    ledger.compact(day, "base")
    live.hold("long-session", gen.generation, ttl=10 * 3600)  # a session keeps reading the pre-compaction view
    clock.now += engine.config.collection.slices.gc_grace_seconds + 1
    assert not set(pinned) & set(ledger.gc())  # past the grace, but pinned: kept
    assert engine.backends["local"].lexical.search(gen, LexicalQuery(kind="terms", text="crawler timeout"), None, 10).items is not None
    live.release("long-session")
    live.release(gen.generation)
    assert not set(pinned) & live.held_slices() and not any(live.slice_pins().pinned(s) for s in pinned)
    assert set(pinned) <= set(ledger.gc())


def test_a_session_holds_its_pin_for_its_lifetime(tmp_path, fx):
    clock = Clock(2_000_000_000.0)
    engine = collection(tmp_path, fx, clock=clock)
    live = engine.view.attach().live
    gen = engine.write_path.pin()[0]
    pinned = {s for days in live.view(gen.generation).slices.values() for ids in days.values() for s in ids}
    live.hold("s-1", gen.generation, ttl=86_400)
    live.release(gen.generation)  # the request ends; the session still holds the slices
    assert pinned <= live.held_slices()
    clock.now += 86_401
    live.release("s-1")
    assert not pinned & live.held_slices()


def test_request_ids_are_stable_per_vector():
    vector = {"logs": {date(2026, 11, 9): ("slice:2026-11-09:m00000001",)}}
    assert request_id("a" * 32, vector) == request_id("a" * 32, dict(vector))
    assert request_id("a" * 32, vector) != request_id("b" * 32, vector)
    assert request_id("a" * 32, vector) != request_id("a" * 32, {"logs": {date(2026, 11, 9): ("slice:2026-11-09:m00000002",)}})


def test_live_window_days_are_one_tier(tmp_path, fx):
    engine = collection(tmp_path, fx)
    view = engine.backends["local"].view(engine.write_path.pin()[0])
    archived = {m.day() for m in view.members if m.member == "logs" and m.day()}
    assert not archived & set(view.slices["logs"]) and {d.isoformat() for d in view.slices["logs"]} == set(LIVE)


# --- the dense cold start after a compaction (M2a follow-up: the 7.1 s first query) -----------------------


def test_a_compacted_unit_is_preloaded_and_loads_one_vector_per_text(tmp_path, fx):
    engine = collection(tmp_path, fx)
    live = engine.view.attach().live
    backend = engine.backends["local"]
    stratum = backend.log_stratum("logs")  # the serving stratum exists: the compaction hook warms it
    ledger = live.ledger("logs")
    day = date(2026, 11, 8)
    base = ledger.compact(day, "base")
    keys = list(stratum.dense._matrices)
    assert any(k.startswith(base.slice_id + "@") for k in keys)  # pre-loaded at registration, before any request
    reader = stratum.units.reader(base.slice_id, ledger.unit_dir(base.slice_id))
    unique, inverse, chunk_ids, _docs, _pos = stratum.dense.matrix(reader)
    rows, texts = reader.embed()
    assert len(unique) == len({r.hash for r in rows}) and len(inverse) == len(rows) == len(chunk_ids)
    store = live.vector_store("logs")
    assert (unique[inverse] == store.get([r.hash for r in rows])).all()
    assert store.get_unique(["0" * 32]) is None  # a missing text: pending, never guessed


def test_get_unique_reads_each_distinct_text_once(tmp_path):
    import numpy as np

    from su_multi_arm.ingestion.text_vectors import TextVectorStore
    from tests.log_slices_support import SPACE

    store = TextVectorStore(tmp_path / "v", SPACE)
    vectors = np.random.default_rng(1).normal(size=(3, SPACE.dimensions)).astype(np.float32)
    store.put(["a" * 32, "b" * 32, "c" * 32], vectors)
    unique, inverse = store.get_unique(["c" * 32, "a" * 32, "c" * 32, "b" * 32])
    assert unique.shape == (3, SPACE.dimensions) and list(inverse) == [0, 1, 0, 2]
    assert np.allclose(unique[inverse], store.get(["c" * 32, "a" * 32, "c" * 32, "b" * 32]))


def test_the_partitioned_view_pin_carries_the_slice_vector(tmp_path, fx):
    engine = collection(tmp_path, fx, embed=False)
    base = engine.view.attach().generations.current_generation()
    view = engine.view.pin(base)
    assert view.generation == base and {d.isoformat() for d in view.slices["logs"]} == set(LIVE)
    live = engine.view.attach().live
    held = {s for days in view.slices.values() for ids in days.values() for s in ids}
    assert held <= live.held_slices()
    request = engine.write_path.pin()[0]
    assert engine.view.pin(request.generation) == view  # a request generation id resolves to its view
    engine.view.release(request.generation)
    assert not held & live.held_slices()


def test_a_record_violating_the_collection_schema_is_refused_at_seal(tmp_path):
    """Field clashes across members (e.g. ``status``: free-text ticket status vs a log enum) are
    resolved in the one collection schema; a slice whose records violate it is refused, never coerced."""
    from su_multi_arm.core.types import MetadataSchema
    from tests.log_slices_support import make_ledger

    ledger = make_ledger(tmp_path / "ledger")
    ledger.metadata_schema = MetadataSchema.model_validate({"fields": [{"name": "status", "type": "enum", "enum_values": ["open", "closed"]}]})
    day = date(2026, 11, 8)
    bad = doc("logs:incident:x", "alpha").model_copy(update={"metadata": {"status": "firing"}})
    with pytest.raises(SliceRefused, match="status"):
        ledger.seal(day, "five_minute", [bad], as_of=at(day, 1))
    assert ledger.vector() == {}
    ok = doc("logs:incident:y", "beta").model_copy(update={"metadata": {"status": "open"}})
    assert ledger.seal(day, "five_minute", [ok], as_of=at(day, 1)) is not None
