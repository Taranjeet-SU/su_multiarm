"""Relative paths of ``ingestion.attach[].path``, ``communities.imported.path`` and
``pipeline(s).arms.segment.path`` resolve against the YAML file's directory (F0 follow-up, fixed in
S0), like every other path of the configuration. Before S0 they resolved against the working
directory. The index fingerprint is unchanged: the recipe keeps hashing ``communities.imported.path``
as written (the file itself is pinned by ``sha256``); the other two never entered the recipe.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import yaml

from su_multi_arm.core.config import load_config, rebase_paths, validate_config
from su_multi_arm.ingestion.recipes import fingerprint_of, recipe_of

SHA = "b" * 64


def mapping(imported_path: str = "data/communities.jsonl") -> dict:
    return {
        "schema_version": 2,
        "dataset": "paths",
        "backends": {"local": {"kind": "sqlite", "path": "state"}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "metadata_schema": {"fields": [{"name": "region", "type": "string"}]},
        "pipeline": {"arms": {"lexical": {"backend": "local"}, "segment": {"path": "segments"}}},
        "pipelines": {"wide": {"arms": {"segment": {"path": "other/segments"}}}},
        "ingestion": {"attach": [{"path": "data/attrs.sqlite", "sha256": SHA, "table": "attrs", "key_column": "id", "document_id": "doc:{key}", "columns": {"region": "region"}}]},
        "communities": {"strategy": "imported", "imported": {"path": imported_path, "sha256": SHA}},
    }


def load(directory: Path, data: dict, monkeypatch, cwd: Path):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.yaml").write_text(yaml.safe_dump(data))
    cwd.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(cwd)  # a working directory that is not the config's
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return load_config(directory / "config.yaml", check_plugins=False)


def test_the_three_paths_resolve_against_the_config_directory(tmp_path, monkeypatch):
    config = load(tmp_path / "deploy", mapping(), monkeypatch, tmp_path / "elsewhere")
    base = tmp_path / "deploy"
    assert config.ingestion.attach[0].path == base / "data" / "attrs.sqlite"
    assert config.communities.imported.path == base / "data" / "communities.jsonl"
    assert config.pipeline.arms.segment.path == base / "segments"
    assert config.pipelines["wide"].arms.segment.path == base / "other" / "segments"


def test_the_index_fingerprint_is_unchanged_by_the_resolution(tmp_path, monkeypatch):
    """The recipe hashes the written ``communities.imported.path``, so the fingerprint equals the
    pre-S0 value (validation without path resolution, backend paths pinned alike)."""
    data = mapping()
    config = load(tmp_path / "deploy", data, monkeypatch, tmp_path / "elsewhere")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        unresolved = validate_config(data)
    unresolved.backends["local"].path = config.backends["local"].path
    assert recipe_of(config)["communities"]["imported"]["path"] == "data/communities.jsonl"
    assert fingerprint_of(config) == fingerprint_of(unresolved)
    moved = load(tmp_path / "moved", data, monkeypatch, tmp_path / "elsewhere")  # another checkout, same files
    assert recipe_of(moved)["communities"] == recipe_of(config)["communities"]


def test_absolute_and_home_paths_are_kept_as_written(tmp_path, monkeypatch):
    absolute = str(tmp_path / "abs" / "communities.jsonl")
    config = load(tmp_path / "deploy", mapping(absolute), monkeypatch, tmp_path / "elsewhere")
    assert config.communities.imported.path == Path(absolute)
    assert recipe_of(config)["communities"]["imported"]["path"] == absolute


def test_a_copied_config_keeps_the_declared_recipe_path(tmp_path, monkeypatch):
    config = load(tmp_path / "deploy", mapping(), monkeypatch, tmp_path / "elsewhere")
    copy = config.model_copy(deep=True)
    assert recipe_of(copy) == recipe_of(config)


def test_rebase_paths_moves_the_three_paths_with_the_config(tmp_path):
    data = mapping()
    changed = rebase_paths(data, tmp_path / "examples", tmp_path)
    assert {"ingestion.attach.0.path", "communities.imported.path", "pipeline.arms.segment.path", "pipelines.wide.arms.segment.path"} <= set(changed)
    assert data["ingestion"]["attach"][0]["path"] == "examples/data/attrs.sqlite"
    assert data["communities"]["imported"]["path"] == "examples/data/communities.jsonl"
    assert data["pipeline"]["arms"]["segment"]["path"] == "examples/segments"
