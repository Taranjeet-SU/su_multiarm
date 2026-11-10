"""Index fingerprints of the shipped examples are pinned to their 2.0.0a1 values (a2 review B2).

An index built by 2.0.0a1 must open under a later release when the effective settings did not
change. The values below were computed with the published 2.0.0a1 tree (``public-main``) by the
same procedure (backend ``path`` values fixed to ``/golden/state`` so the hash does not depend on
the checkout location). A release that changes them on purpose must bump ``INDEX_FORMAT``, give
``IndexNotReady`` a specific message and say so in the changelog; it updates this table then.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

from su_multi_arm.core.config import load_config
from su_multi_arm.extraction.base import BuiltinExtractor
from su_multi_arm.ingestion.write import INDEX_FORMAT, WritePath

ROOT = Path(__file__).resolve().parents[2]

A1_FINGERPRINTS = {
    "examples/anthropic.yaml": "986c5e0ea71939b5b9a9845a27ebd770ad5a71cbfd52a32783c2fb8e9e39649c",
    "examples/local.yaml": "ff976a6a623771ab4e22072ed31081cc61b3d5d8cc9b3356f0efcf08a86a0f8c",
    "examples/mapping.yaml": "fc1cbfb089d20750095d53bf2e4b4c066b0b500bf09e328e08d266555277366a",
    "examples/postgres_neo4j.yaml": "e5b1e0566fad36e283a59f72730fa6a4fac6dd5025f13d3b2a0a6b40b872d102",
    "examples/semantic.yaml": "fb1591f611fe98c9d855f491bcf23bb004f61d0703c94d726e2f823328ba2e06",
    "examples/v2/hosted_models.yaml": "f01cd798f2df1bfc434c20c811af7ea71bfff3a470bae1bdf1ca09e8b740eb79",
    "examples/v2/local.yaml": "3eb1abab8e99e774eeac334b89491898f7ae9face8fab45036bdd8c0362b56e9",
    "examples/v2/mixed_backends.yaml": "94f647bd919c087759d800ec9000683e4396891cc5750117a4c688447409680c",
}


def example_fingerprint(path: Path) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        config = load_config(path, check_plugins=False, warn=False)
    for name, backend in list(config.backends.items()):
        if hasattr(backend, "path"):
            config.backends[name] = backend.model_copy(update={"path": Path("/golden/state")})
    role = getattr(config.models.roles, "extractor", None)
    extractor = BuiltinExtractor(config.extraction, SimpleNamespace(providers={"extractor": SimpleNamespace(config=role)} if role else {}))
    storage, arms = config.storage, config.pipeline.arms
    slots = {"catalog": storage.catalog, "records": storage.records, "ledger": storage.ledger, "communities": storage.communities}
    for slot, arm in (("lexical", arms.lexical), ("vector", arms.dense), ("graph", arms.graph)):
        if arm:
            slots[slot] = arm.backend
    return WritePath.index_fingerprint(SimpleNamespace(cfg=config, extractor=extractor, slots=slots))


def test_every_shipped_example_is_pinned():
    shipped = {str(p.relative_to(ROOT)) for p in [*ROOT.glob("examples/*.yaml"), *ROOT.glob("examples/v2/*.yaml")]}
    assert shipped == set(A1_FINGERPRINTS), "a new example needs its golden fingerprint recorded here"


@pytest.mark.parametrize("example", sorted(A1_FINGERPRINTS))
def test_index_fingerprint_equals_the_2_0_0a1_value(example):
    assert INDEX_FORMAT == 3, "INDEX_FORMAT changed: update the golden table and the changelog deliberately"
    assert example_fingerprint(ROOT / example) == A1_FINGERPRINTS[example]


# --- S0 (unified merge plan §7, §6.3 R2): schema v3 and the backend-free recipe -----------------------

V2_EXAMPLES = sorted(e for e in A1_FINGERPRINTS if e.startswith("examples/v2/"))


def _pinned(config):
    """``config`` with every backend ``path`` fixed to ``/golden/state`` (as ``example_fingerprint``)."""
    for name, backend in list(config.backends.items()):
        if hasattr(backend, "path"):
            config.backends[name] = backend.model_copy(update={"path": Path("/golden/state")})
    return config


def _load(path: Path, *, schema_version: int | None = None):
    """``load_config`` of ``path``, optionally re-declared with another ``schema_version`` (the
    mapping is validated in memory and its paths resolved against the example's directory)."""
    import yaml

    from su_multi_arm.core.config import _resolve_paths, validate_config

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if schema_version is None:
            return load_config(path, check_plugins=False, warn=False)
        data = yaml.safe_load(path.read_text())
        data["schema_version"] = schema_version
        return _resolve_paths(validate_config(data), path.parent)


def fingerprint_of_pinned(config) -> str:
    from su_multi_arm.ingestion.recipes import fingerprint_of

    return fingerprint_of(_pinned(config))


@pytest.mark.parametrize("example", sorted(A1_FINGERPRINTS))
def test_recipe_of_is_backend_free_and_equals_the_golden_fingerprint(example, monkeypatch):
    """``recipes.recipe_of(config)`` hashes to the 2.0.0a1 value of every shipped example, including
    the ones whose backends (postgres, neo4j) cannot be constructed here: it opens nothing."""

    def refuse(*args, **kwargs):
        raise AssertionError("recipe_of must not construct a backend")

    monkeypatch.setattr("su_multi_arm.backends.sqlite.SqliteBackend.from_config", refuse)
    assert fingerprint_of_pinned(_load(ROOT / example)) == A1_FINGERPRINTS[example]


@pytest.mark.parametrize(
    "example",
    ["examples/anthropic.yaml", "examples/local.yaml", "examples/mapping.yaml", "examples/semantic.yaml", "examples/v2/hosted_models.yaml", "examples/v2/local.yaml"],
)
def test_recipe_of_equals_the_constructed_write_paths_index_recipe(example, tmp_path, monkeypatch):
    """R2 golden: ``recipe_of(cfg) == WritePath(cfg).index_recipe()`` on an engine built over a
    temporary store (no model call: the key variable is a placeholder that is never sent)."""
    from su_multi_arm import Engine
    from su_multi_arm.ingestion.recipes import recipe_of

    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder-never-sent")
    config = _load(ROOT / example)
    for name, backend in list(config.backends.items()):
        if hasattr(backend, "path"):
            config.backends[name] = backend.model_copy(update={"path": tmp_path / name})
    engine = Engine(config)
    assert recipe_of(config) == engine.write_path.index_recipe()
    assert engine.view.describe()["mode"] == "single"


def test_recipe_of_equals_index_recipe_on_the_live_shape_fixture(tmp_path, monkeypatch):
    """The F0 live-shape config (attached kuzu graph excluded from the recipe, projection on,
    precomputed vectors, imported communities, attach tables): ``recipe_of`` equals the recipe of a
    ``WritePath`` constructed over its sqlite backend (the attached graph is never a write slot)."""
    from su_multi_arm.backends.sqlite import SqliteBackend
    from su_multi_arm.ingestion.recipes import recipe_of, write_slots
    from su_multi_arm.providers import JsonModel
    from su_multi_arm.testing.fixtures.unified import materialize_live_shape

    monkeypatch.setenv("SU_FIXTURE_API_KEY", "placeholder-never-sent")
    path = materialize_live_shape(tmp_path / "live")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        config = load_config(path)
    assert "graph" not in write_slots(config) and "projection" in write_slots(config)
    role = config.models.roles.extractor
    extractor = BuiltinExtractor(config.extraction, SimpleNamespace(providers={"extractor": JsonModel(role)} if role else {}))
    write = WritePath(config, {"local": SqliteBackend.from_config(config.backends["local"])}, extractor=extractor, embedder=None)
    assert recipe_of(config) == write.index_recipe()


@pytest.mark.parametrize("example", V2_EXAMPLES)
def test_a_v2_example_redeclared_as_schema_v3_keeps_its_fingerprint(example):
    """``schema_version: 3`` without v3 blocks is the v2 configuration: same recipe, same fingerprint."""
    v3 = _load(ROOT / example, schema_version=3)
    assert v3.schema_version == 3 and v3.collection is None
    assert fingerprint_of_pinned(v3) == A1_FINGERPRINTS[example]


@pytest.mark.parametrize("example", V2_EXAMPLES)
def test_the_trace_fingerprint_omits_unset_v3_blocks(example):
    """``Config.fingerprint()`` (traces/manifests) is the hash of the dump without the unset v3 keys,
    so S0 did not change it for any v2 configuration."""
    import hashlib
    import json

    config = _load(ROOT / example)
    data = config.model_dump(mode="json")
    assert data.pop("collection") is None and data.pop("crosssource") is None and data["communities"].pop("layers") == []
    assert config.fingerprint() == hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
