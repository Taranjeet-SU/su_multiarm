"""QA-20261109-13: a SQLite store moved or copied to another directory is adopted explicitly.

The index fingerprint covers the backend ``path``, so a copied state directory is refused
("fingerprint mismatch"). ``index adopt`` re-stamps it after checking that the old configuration
built it and that the two recipes differ only in location fields (``ingestion/relocate.py``).
Synthetic corpus on the SQLite backend with the hash embedder; no provider call."""

from __future__ import annotations

import json
import shutil
import sys

import pytest
import yaml

from su_multi_arm import Engine
from su_multi_arm.core.errors import IndexNotReady
from su_multi_arm.ingestion.relocate import RelocationRefused, adopt, location_changes, sha256_file

from compat_support import corpus
from serving_support import sqlite_config


@pytest.fixture
def moved(tmp_path):
    first = Engine(sqlite_config(tmp_path / "a", None))
    first.ingest(corpus())
    generation = first._current_generation()
    shutil.copytree(tmp_path / "a", tmp_path / "b")
    return tmp_path, generation


def test_a_copied_state_directory_is_refused_until_adopted_then_served(moved):
    tmp_path, generation = moved
    old, new = sqlite_config(tmp_path / "a", None), sqlite_config(tmp_path / "b", None)
    with pytest.raises(IndexNotReady, match="fingerprint mismatch"):
        Engine(new).write_path.current()
    checked = adopt(new, old, dry_run=True)
    assert checked["status"] == "verified" and [c["field"] for c in checked["changes"]] == ["backends.catalog.path", "backends.communities.path", "backends.ledger.path", "backends.lexical.path", "backends.records.path", "backends.vector.path"]
    with pytest.raises(IndexNotReady):
        Engine(new).write_path.current()  # a dry run changes nothing
    report = adopt(new, old)
    assert report["status"] == "adopted" and report["generation"] == generation
    moved_engine = Engine(new)
    assert moved_engine._current_generation() == generation
    assert moved_engine.search("billing invoices").hits
    manifest = moved_engine.manifest()
    assert manifest["relocations"][-1]["from_fingerprint"] == report["from_fingerprint"] and manifest["fingerprint"] == report["to_fingerprint"]
    assert adopt(new, old)["status"] == "already_adopted"
    assert Engine(old)._current_generation() == generation  # the original is untouched


def test_adopt_refuses_recipes_that_differ_beyond_location(moved):
    tmp_path, _ = moved
    old = sqlite_config(tmp_path / "a", None)
    rechunked = sqlite_config(tmp_path / "b", None, ingestion={"chunking": {"size": 900, "overlap": 90}})
    with pytest.raises(RelocationRefused, match=r"more than location fields: chunking.overlap: changed; chunking.size: changed"):
        adopt(rechunked, old)  # the new recipe differs, so the generation is not "the same index"
    wrong_builder = sqlite_config(tmp_path / "x", None, ingestion={"chunking": {"size": 900, "overlap": 90}})
    with pytest.raises(RelocationRefused, match="matches neither"):
        adopt(sqlite_config(tmp_path / "b", None), wrong_builder)  # OLD did not build this generation
    with pytest.raises(RelocationRefused, match="dataset differs"):
        adopt(sqlite_config(tmp_path / "b", None), sqlite_config(tmp_path / "a", None, dataset="other"))


def test_location_changes_verify_imported_files_by_their_pinned_digest(tmp_path):
    data = tmp_path / "communities.jsonl"
    data.write_text('{"id": "c1"}\n')
    digest = sha256_file(data)
    old = {"communities": {"imported": {"path": "/old/place/communities.jsonl", "sha256": digest}}, "backends": {"vector": {"kind": "sqlite", "path": "/old/state"}}}
    new = {"communities": {"imported": {"path": str(data), "sha256": digest}}, "backends": {"vector": {"kind": "sqlite", "path": str(tmp_path / "state")}}}
    allowed, refused = location_changes(old, new)
    assert refused == [] and {c["kind"] for c in allowed} == {"backend_path", "imported_file"}
    data.write_text('{"id": "c2"}\n')  # the file at the new path is not the pinned one
    _, refused = location_changes(old, new)
    assert refused and "sha256" in refused[0]
    _, refused = location_changes({"chunking": {"size": 1600}}, {"chunking": {"size": 900}})
    assert refused == ["chunking.size: changed"]
    _, refused = location_changes({"backends": {"vector": {"kind": "sqlite"}}}, {"backends": {"vector": {"kind": "faiss"}}})
    assert refused == ["backends.vector.kind: changed (not a location field)"]


def test_cli_index_adopt(moved, monkeypatch, capsys):
    from su_multi_arm import cli

    tmp_path, generation = moved
    paths = {}
    for name in ("a", "b"):
        paths[name] = tmp_path / f"{name}.yaml"
        paths[name].write_text(yaml.safe_dump(sqlite_config(tmp_path / name, None).model_dump(mode="json")))
    monkeypatch.setattr(sys, "argv", ["su-multi-arm", "--config", str(paths["b"]), "index", "adopt", "--from-config", str(paths["a"])])
    cli.main()
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "adopted" and report["generation"] == generation
    bad = tmp_path / "c.yaml"
    bad.write_text(yaml.safe_dump(sqlite_config(tmp_path / "b", None, ingestion={"chunking": {"size": 900, "overlap": 90}}).model_dump(mode="json")))
    monkeypatch.setattr(sys, "argv", ["su-multi-arm", "--config", str(bad), "index", "adopt", "--from-config", str(paths["a"])])
    with pytest.raises(SystemExit) as exit_:
        cli.main()
    assert exit_.value.code != 0 and "RelocationRefused" in capsys.readouterr().err
