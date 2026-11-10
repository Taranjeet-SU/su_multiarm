"""Read-only adoption of an existing store as a collection member (unified merge M1b, §6.3, §10.3).

The store is built by a v2 configuration (``tests/adopted_support.py``, synthetic F0 support data).
Adoption recomputes its fingerprint backend-free from the original configuration, records digests
in the unified registry and never writes, re-stamps or relocates the store: the tree digests and
mtimes are equal before and after adoption, compose and serving, no ``WritePath`` and no backend
are constructed while adopting, and a changed store is refused.
"""

from __future__ import annotations

import json

import pytest

from su_multi_arm import Engine, SearchRequest
from su_multi_arm.backends.sqlite import SqliteBackend
from su_multi_arm.core.config import load_config, validate_config
from su_multi_arm.core.errors import BackendError, ConfigError
from su_multi_arm.core.types import Document, GenerationId
from su_multi_arm.ingestion.adopt_member import ADOPTION_FILE, AdoptionError, store_digests, store_generation
from su_multi_arm.ingestion.members import MEMBER_FILE, MemberRegistry, MemberUnavailable
from su_multi_arm.ingestion.recipes import fingerprint_of, store_recipe
from tests.adopted_support import QUERIES, adopted_engine, build_original, sha256, store_tree, unified_data, write_yaml


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("adopt")
    return root, build_original(root)


def fresh(tmp_path, built):
    """A private store for a test that changes it. (A copy would not do: the store path is part of
    today's index recipe, so a copied store no longer matches its original configuration.)"""
    root = tmp_path / "own"
    return root, build_original(root)


def registry(root, original, **member):
    return MemberRegistry(validate_config(unified_data(root, original, **member)))


def test_adoption_recomputes_the_original_fingerprint_and_records_digests_in_the_registry(built):
    root, original = built
    reg = registry(root, original)
    ref = reg.adopt("support")
    generation, manifest = store_generation(root / "store")
    cfg = load_config(original)
    assert ref.tier == "adopted" and ref.key.startswith("adopted:") and ref.unit_id == f"support/{ref.key}"
    assert ref.stats["store_fingerprint"] == fingerprint_of(cfg) == manifest["fingerprint"]
    assert ref.stats["generation"] == generation and ref.recipe == store_recipe(cfg)
    assert ref.digests == store_digests(root / "store", generation)
    assert "CURRENT" in ref.digests and f"snapshots/{generation}/catalog.sqlite" in ref.digests
    assert not any(name.startswith("runtime") for name in ref.digests)  # the serving process's file
    assert ref.counts == {"doc": 6, "ticket": 12} and ref.stats["store_backend"]["lexical_engine"] == "bm25"
    folder = reg.unit_dir(ref)
    assert folder.is_relative_to(reg.root) and (folder / MEMBER_FILE).is_file() and (folder / ADOPTION_FILE).is_file()
    assert not any(str(root / "store") in p.read_text() for p in (folder / MEMBER_FILE, folder / ADOPTION_FILE))  # no host path of the store
    assert reg.directory.count(ref.unit_id) == 18 and reg.store_dir(ref) == (root / "store").resolve()
    assert reg.adopt("support") == ref  # idempotent


def test_adoption_never_constructs_a_write_path_or_a_backend(tmp_path, built, monkeypatch):
    root, original = fresh(tmp_path, built)
    reg = registry(root, original)

    def refuse(*args, **kwargs):
        raise AssertionError("adoption constructed a write path or a backend")

    monkeypatch.setattr("su_multi_arm.ingestion.write.WritePath.__init__", refuse)
    monkeypatch.setattr(SqliteBackend, "__init__", refuse)
    before = store_tree(root / "store")
    ref = reg.adopt("support")
    assert ref.counts and store_tree(root / "store") == before


def test_adopt_compose_and_serve_leave_the_store_tree_unchanged(tmp_path, built):
    root, original = fresh(tmp_path, built)
    before = store_tree(root / "store")
    engine = adopted_engine(root, original)
    for query in QUERIES:
        engine.search(SearchRequest(query=query))
        engine.answer(query)
    gen = engine.write_path.pin()[0]
    engine.records.values(gen, "status", [], 100)
    assert store_tree(root / "store") == before  # sha256 and mtime of every file, runtime included
    assert sorted(p.name for p in (root / "store").iterdir()) == sorted(before_names(before))


def before_names(tree):
    return {name.split("/")[0] for name in tree}


def test_a_read_only_backend_refuses_every_write(tmp_path, built):
    root, original = fresh(tmp_path, built)
    generation, manifest = store_generation(root / "store")
    from su_multi_arm.ingestion.release import generation_id

    gen = generation_id(manifest)
    backend = SqliteBackend(root / "store", lexical_engine="bm25", readonly=True)
    assert backend.catalog.count_documents(gen) == 18  # reads serve
    doc = Document(id="ticket:9", dataset="unified", source="ticket", kind="ticket", title="t", fields={"description": "x"})
    with pytest.raises(BackendError, match="read-only"):
        backend.catalog.put_documents(gen, [doc], [])
    with pytest.raises(BackendError, match="read-only"):
        backend.cache.get("k")
    with pytest.raises(BackendError, match="read-only"):
        backend.relational.put_file(gen, "all.sqlite", b"")
    with pytest.raises(BackendError, match="read-only"):
        backend.delete_generation(gen)
    with pytest.raises(BackendError, match="read-only"):
        backend.catalog.put_documents(GenerationId(dataset="unified", generation="b" * 32, index_format=3, fingerprint="f" * 64), [doc], [])
    assert not (root / "store" / "snapshots" / ("b" * 32)).exists()


def test_a_changed_store_is_refused_at_serve_time(tmp_path, built):
    root, original = fresh(tmp_path, built)
    engine = adopted_engine(root, original)
    generation, _ = store_generation(root / "store")
    meta = root / "store" / "snapshots" / generation / "bm25" / "meta.json"
    meta.write_text(meta.read_text() + " ")
    fresh_engine = Engine(validate_config(unified_data(root, original)))  # a new process: digests re-checked
    with pytest.raises(BackendError, match="unavailable"):
        fresh_engine.search(SearchRequest(query=QUERIES[0]))
    with pytest.raises(MemberUnavailable, match="digest"):
        fresh_engine.compose()
    assert engine.write_path.pin()  # the generation itself is unchanged; only the member is unavailable


def test_a_changed_original_config_or_another_store_is_refused(tmp_path, built):
    root, original = fresh(tmp_path, built)
    data = unified_data(root, original)
    data["collection"]["members"]["support"]["adopt"]["original_config_sha256"] = "0" * 64
    with pytest.raises(AdoptionError, match="is not the pinned"):
        MemberRegistry(validate_config(data)).adopt("support")
    other = load_config(original).model_dump(mode="json")
    other["backends"]["local"]["path"] = str(root / "elsewhere")
    path = write_yaml(root / "other.yaml", other)
    data = unified_data(root, path)
    with pytest.raises(AdoptionError, match="not the declared store"):
        MemberRegistry(validate_config(data)).adopt("support")


def test_a_restamped_or_foreign_store_is_refused(tmp_path, built):
    root, original = fresh(tmp_path, built)
    generation, manifest = store_generation(root / "store")
    path = root / "store" / "snapshots" / generation / "manifest.json"
    path.write_text(json.dumps({**manifest, "fingerprint": "e" * 64}))
    reg = registry(root, original)
    with pytest.raises(AdoptionError, match="re-stamped"):
        reg.adopt("support")
    assert not (reg.root / "members").exists() and reg.directory.units() == []


def test_documents_outside_the_member_declaration_register_nothing(tmp_path, built):
    root, original = fresh(tmp_path, built)
    reg = registry(root, original, id_namespaces=["ticket:"])
    with pytest.raises(AdoptionError, match="outside member"):
        reg.adopt("support")
    assert not (reg.root / "members").exists() and reg.directory.units() == []
    reg = registry(root, original, record_kinds=["ticket"])
    with pytest.raises(AdoptionError, match="record_kinds"):
        reg.adopt("support")


def test_a_non_adopted_member_cannot_be_adopted(tmp_path, built):
    root, original = fresh(tmp_path, built)
    data = unified_data(root, original)
    data["collection"]["members"]["tickets"] = {"kind": "ticket", "sources": ["ticket"], "record_kinds": ["ticket"], "id_namespaces": ["case:"]}
    with pytest.raises(ConfigError, match="not an adopted member"):
        MemberRegistry(validate_config(data)).adopt("tickets")


def test_the_cli_adopts_and_prints_layer_digests(tmp_path, built, monkeypatch, capsys):
    from su_multi_arm.cli import main

    root, original = fresh(tmp_path, built)
    before = store_tree(root / "store")
    config = write_yaml(tmp_path / "unified.yaml", unified_data(root, original))

    def run(*argv):
        monkeypatch.setattr("sys.argv", ["su-multi-arm", "--config", str(config), *argv])
        try:
            main()
        except SystemExit as exc:
            assert not exc.code
        return json.loads(capsys.readouterr().out)

    adopted = run("collection", "adopt", "--member", "support")
    assert adopted["adopted"]["unit_id"].startswith("support/adopted:") and adopted["files"] > 5
    layers = run("collection", "layers")["layers"]
    assert {layer["name"]: layer["matches"] for layer in layers} == {"support_communities": True, "support_segments": True}
    assert layers[0]["digest"] == sha256(root / "communities.jsonl")
    composed = run("collection", "compose")
    assert composed["members"] == [adopted["adopted"]["unit_id"]] and composed["documents"] == 18
    assert store_tree(root / "store") == before
