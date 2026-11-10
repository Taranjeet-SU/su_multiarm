"""Member partitions and compose-by-reference (unified merge plan §6.1, §6.2, §6.12; M1a).

Contract rows: members are built by the shared write path into member-scoped stores and checked
against their declaration (sources, record kinds, id namespaces, key scheme); catalog kinds live in
the catalog member only, latest-wins; non-catalog duplicates across members are refused (F0 trap);
compose is O(members) (no document read), CAS-safe, and refuses an unknown member, a foreign
embedding space, a digest mismatch and an id directory inconsistent with the member manifest;
``current()`` compares only the collection recipe; ``ingest`` is refused in partitioned mode;
v2 configurations are unaffected.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from su_multi_arm import Engine
from su_multi_arm.core.errors import ConfigError, IndexNotReady, WriteConflict
from su_multi_arm.core.types import Document
from su_multi_arm.ingestion.compose import compose
from su_multi_arm.ingestion.id_directory import IdConflict
from su_multi_arm.ingestion.members import catalog_documents
from su_multi_arm.ingestion.release import ANY
from su_multi_arm.testing.fixtures.unified.collection import read_documents
from tests.partition_support import F0, config, f0, f0_members, member_block


@pytest.fixture(scope="module")
def fx():
    return f0()


@pytest.fixture(scope="module")
def members(fx):
    return f0_members(fx)


def engine_for(tmp_path: Path, fx, members, **kwargs) -> Engine:
    return Engine(config(tmp_path, schema=fx.manifest["metadata_schema"], collection=member_block(fx, members), **kwargs))


def test_catalog_kinds_are_latest_wins_and_only_in_the_catalog_member(tmp_path, fx, members):
    logs = [fx.members[f"logs/{m['key']}"] for m in fx.manifest["members"] if m["id"] == "logs"]
    catalog = catalog_documents(logs, ())
    assert sorted(d.id for d in catalog) == sorted({d.id for docs in logs for d in docs if d.kind == "signature"})
    by_id = {d.id: d for d in catalog}
    for docs in logs:  # the later member's version wins
        for d in docs:
            if d.kind == "signature" and d.id in by_id and docs is logs[-1]:
                assert by_id[d.id] == d
    engine = engine_for(tmp_path, fx, members)
    day = fx.members["logs/day:2026-11-04"]
    with pytest.raises(ValueError, match="catalog kinds live in the member's catalog member"):
        engine.build_member("logs", "day:2026-11-04", day)
    with pytest.raises(ValueError, match="catalog member holds catalog kinds only"):
        engine.build_member("logs", "catalog", [d for d in day if d.kind != "signature"])
    ref = engine.build_member("logs", "catalog", catalog)
    assert ref.tier == "catalog" and ref.catalog_kinds == ("signature",) and ref.unit_id == "logs/catalog"


def test_member_declarations_are_enforced(tmp_path, fx, members):
    engine = engine_for(tmp_path, fx, members)
    tickets = members["support"][0][1]
    with pytest.raises(ValueError, match="no member"):
        engine.build_member("nope", "snapshot:2026-11-09", tickets)
    with pytest.raises(ValueError, match="cannot build key"):
        engine.build_member("support", "day:2026-11-09", tickets)
    with pytest.raises(ValueError, match="not one of member 'tracker'"):
        engine.build_member("tracker", "snapshot:2026-11-09", tickets)
    foreign = [tickets[0].model_copy(update={"id": "case:1"})]
    with pytest.raises(ValueError, match="outside member 'support'"):
        engine.build_member("support", "snapshot:2026-11-09", foreign)
    assert not (Path(engine.view.attach().root) / "members" / "support").exists() or not list((Path(engine.view.attach().root) / "members" / "support").rglob("member.json"))


def test_a_duplicate_id_across_members_is_refused_and_the_build_is_removed(tmp_path, fx, members):
    engine = engine_for(tmp_path, fx, members)
    engine.build_member("support", "snapshot:2026-11-09", members["support"][0][1])
    trap = next(t for t in fx.traps["traps"] if t["id"] == "duplicate_id_member")
    duplicates = read_documents(F0 / trap["file"])
    ticket_dups = [d for d in duplicates if d.id.startswith("ticket:")]
    assert ticket_dups
    with pytest.raises(IdConflict):
        engine.build_member("support", "snapshot:2026-11-10", ticket_dups)
    registry = engine.view.attach()
    assert [r.key for r in registry.members()] == ["snapshot:2026-11-09"]
    assert registry.directory.unit("support/snapshot:2026-11-10") is None
    assert not any(p.name.startswith("support") for p in (registry.root / "members").rglob("snapshot-2026-11-10"))


def test_compose_is_by_reference_and_cas_safe(tmp_path, fx, members, monkeypatch):
    engine = engine_for(tmp_path, fx, members)
    registry = engine.view.attach()
    tracker = engine.build_member("tracker", *members["tracker"][0])
    analytics = [engine.build_member("analytics", key, docs) for key, docs in members["analytics"]]
    # compose never reads a document: break every catalog read and compose still succeeds
    from su_multi_arm.backends.sqlite.catalog import SqliteCatalog

    monkeypatch.setattr(SqliteCatalog, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("compose read a document")))
    monkeypatch.setattr(SqliteCatalog, "iter_scope", lambda *a, **k: (_ for _ in ()).throw(AssertionError("compose read a document")))
    first = compose(engine.config, registry, [tracker])
    assert [m["key"] for m in first["members"]] == ["snapshot:2026-11-08"] and first["kind"] == "partitioned"
    second = compose(engine.config, registry, [tracker, *analytics], expected=first["generation"])
    assert registry.generations.current_generation() == second["generation"] and second["base"] == first["generation"]
    with pytest.raises(WriteConflict) as info:
        compose(engine.config, registry, [tracker], expected=first["generation"])  # stale base
    assert info.value.reason == "stale_base"
    assert registry.generations.current_generation() == second["generation"]
    monkeypatch.undo()
    gen, manifest = engine.write_path.current()
    assert gen.generation == second["generation"] and manifest["documents"] == sum(r.stats["documents"] for r in [tracker, *analytics])


def test_compose_refuses_inconsistent_members(tmp_path, fx, members):
    engine = engine_for(tmp_path, fx, members)
    registry = engine.view.attach()
    tracker = engine.build_member("tracker", *members["tracker"][0])
    with pytest.raises(ValueError, match="at least one member"):
        compose(engine.config, registry, [])
    foreign = tracker.model_copy(update={"recipe": tracker.recipe.model_copy(update={"embedding_space": tracker.recipe.embedding_space.model_copy(update={"id": "other-space"})})})
    with pytest.raises(ValueError, match="embedding space"):
        compose(engine.config, registry, [foreign])
    undeclared = tracker.model_copy(update={"member": "ghost"})
    with pytest.raises(ValueError, match="not declared"):
        compose(engine.config, registry, [undeclared])
    # a changed member file: digest mismatch, refused (fail closed)
    manifest = next(name for name in tracker.digests if name.endswith("manifest.json"))
    path = registry.unit_dir(tracker) / manifest
    original = path.read_bytes()
    path.write_bytes(original + b" ")
    with pytest.raises(Exception, match="digest"):
        compose(engine.config, registry, [tracker])
    path.write_bytes(original)
    # a directory that disagrees with the member manifest: refused
    registry.directory.retire(tracker.unit_id)
    with pytest.raises(ValueError, match="registered and live"):
        compose(engine.config, registry, [tracker])
    assert registry.generations.current_generation() is None


def test_current_compares_only_the_collection_recipe(tmp_path, fx, members):
    engine = engine_for(tmp_path, fx, members)
    with pytest.raises(IndexNotReady, match="no active collection generation"):
        engine.write_path.current()
    engine.build_member("tracker", *members["tracker"][0])
    engine.compose()
    engine.write_path.current()
    # a member-recipe change alone (another chunk size) does not invalidate the composed generation
    data = json.loads(engine.config.model_dump_json())
    data["ingestion"]["chunking"]["size"] = 900
    other = Engine(type(engine.config).model_validate(data))
    other.write_path.current()
    # a collection-recipe change (another member list) does
    block = member_block(fx, {"tracker": members["tracker"]})
    changed = Engine(config(tmp_path, schema=fx.manifest["metadata_schema"], collection=block))
    with pytest.raises(IndexNotReady, match="collection recipe mismatch"):
        changed.write_path.current()


def test_ingest_is_refused_in_partitioned_mode(tmp_path, fx, members):
    engine = engine_for(tmp_path, fx, members)
    with pytest.raises(ConfigError, match="build each member"):
        engine.ingest([Document(id="ticket:1", dataset="unified", source="ticket", kind="ticket", title="x")])


def test_v2_engines_have_no_member_api(tmp_path):
    engine = Engine(config(tmp_path, arms=("lexical",)))
    with pytest.raises(ConfigError, match="partitioned collection"):
        engine.build_member("x", "snapshot:2026-11-09", [])
    with pytest.raises(ConfigError, match="partitioned collection"):
        engine.compose()


def test_retire_refuses_a_member_of_the_active_generation(tmp_path, fx, members):
    engine = engine_for(tmp_path, fx, members)
    registry = engine.view.attach()
    first = engine.build_member("analytics", *members["analytics"][0])
    second = engine.build_member("analytics", *members["analytics"][1])
    engine.compose([first, second])
    with pytest.raises(ValueError, match="active generation"):
        registry.retire(first)
    engine.compose([second], expected=ANY)
    registry.retire(first)
    assert registry.directory.unit(first.unit_id) is None and not registry.unit_dir(first).exists()
    assert [r.unit_id for r in registry.members()] == [second.unit_id]


def test_partitioned_mode_refuses_what_m1a_does_not_serve(tmp_path, fx, members):
    from su_multi_arm.core.config import validate_config

    base = json.loads(config(tmp_path, schema=fx.manifest["metadata_schema"], collection=member_block(fx, members)).model_dump_json())
    graph = {**base, "pipeline": {**base["pipeline"], "arms": {**base["pipeline"]["arms"], "graph": {"backend": "local"}}}}
    with pytest.raises(ConfigError, match="graph arm") as info:
        Engine(validate_config(graph))
    assert info.value.path == "pipeline.arms.graph"
    fts = json.loads(json.dumps(base))
    fts["backends"]["local"]["lexical_engine"] = "fts5"
    fts["backends"]["local"]["bm25"] = None
    with pytest.raises(ConfigError, match="fts5 is refused"):
        Engine(validate_config(fts))
    assert not (tmp_path / "collection").exists()
