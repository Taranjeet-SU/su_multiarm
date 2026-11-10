"""Per-partition recipe decomposition (unified merge plan §6.2; ``ingestion/recipes.py``; M1a).

Contract rows: the partition recipe is the member part of today's index recipe (chunking, privacy,
extractor, embedder, communities, backend kinds — the same fragments ``recipe_of`` hashes) without
the dataset or any store path, so moving the member registry keeps every member fingerprint; the
collection recipe changes with the member list, the embedding space and the dataset, and not with
a member's chunking; the unified fingerprint is order-free over members and changes with any member
fingerprint; non-partitioned configurations keep ``recipe_of == WritePath.index_recipe`` (the S0
golden test in ``test_index_fingerprint_golden.py`` stays the guard).
"""

from __future__ import annotations

import json

import pytest

from su_multi_arm.core.config import validate_config
from su_multi_arm.ingestion.recipes import (
    collection_fingerprint,
    collection_recipe,
    embedding_space_ref,
    partition_recipe,
    recipe_of,
    unified_fingerprint,
)
from tests.partition_support import config, f0, f0_members, member_block


@pytest.fixture(scope="module")
def fx():
    return f0()


@pytest.fixture(scope="module")
def block(fx):
    return member_block(fx, f0_members(fx))


def test_the_partition_recipe_is_the_member_part_of_todays_recipe(tmp_path, fx, block):
    cfg = config(tmp_path / "a", schema=fx.manifest["metadata_schema"], collection=block)
    whole = recipe_of(cfg)
    part = partition_recipe(cfg, "support")
    assert part.chunking == whole["chunking"] and part.privacy == whole["privacy"]
    assert part.content["extractor"] == whole["extractor"] and part.content["communities"] == whole["communities"]
    assert set(part.content["backends"]) == set(whole["backends"])
    assert all("path" not in b for b in part.content["backends"].values())
    assert part.embedding_space == embedding_space_ref(cfg) and part.embedding_space.dimensions == 64
    assert part.analyzer.engine == "bm25" and part.projection is None


def test_member_fingerprints_are_location_free(tmp_path, fx, block):
    a = config(tmp_path / "a", schema=fx.manifest["metadata_schema"], collection=block)
    b = config(tmp_path / "elsewhere" / "b", schema=fx.manifest["metadata_schema"], collection=block)
    assert recipe_of(a) != recipe_of(b)  # today's whole recipe hashes the store path
    assert partition_recipe(a, "logs").fingerprint() == partition_recipe(b, "logs").fingerprint()
    assert collection_fingerprint(a) == collection_fingerprint(b)


def test_epochs_and_chunking_are_member_recipe_changes_only(tmp_path, fx, block):
    cfg = config(tmp_path, schema=fx.manifest["metadata_schema"], collection=block)
    assert partition_recipe(cfg, "logs", keyword_epoch="kw-e1").fingerprint() != partition_recipe(cfg, "logs", keyword_epoch="kw-e2").fingerprint()
    data = json.loads(cfg.model_dump_json())
    data["ingestion"]["chunking"]["size"] = 900
    rechunked = validate_config(data)
    assert partition_recipe(rechunked, "logs").fingerprint() != partition_recipe(cfg, "logs").fingerprint()
    assert collection_fingerprint(rechunked) == collection_fingerprint(cfg)


def test_the_collection_recipe_changes_with_members_space_and_dataset(tmp_path, fx, block):
    cfg = config(tmp_path, schema=fx.manifest["metadata_schema"], collection=block)
    recipe = collection_recipe(cfg)
    assert sorted(recipe["members"]) == ["analytics", "logs", "support", "tracker"]
    assert recipe["members"]["logs"]["catalog_kinds"] == ["signature"]
    fewer = {**block, "members": {k: v for k, v in block["members"].items() if k != "analytics"}}
    assert collection_fingerprint(config(tmp_path, schema=fx.manifest["metadata_schema"], collection=fewer)) != collection_fingerprint(cfg)
    data = json.loads(cfg.model_dump_json())
    data["models"]["roles"]["embedder"]["dimensions"] = 128
    assert collection_fingerprint(validate_config(data)) != collection_fingerprint(cfg)
    data = json.loads(cfg.model_dump_json())
    data["collection"]["embedding_space"] = {"id": "fixture-space-1"}
    declared = validate_config(data)
    assert embedding_space_ref(declared).id == "fixture-space-1" and collection_fingerprint(declared) != collection_fingerprint(cfg)
    data = json.loads(cfg.model_dump_json())
    data["dataset"] = "other"
    assert collection_fingerprint(validate_config(data)) != collection_fingerprint(cfg)


def test_the_unified_fingerprint_is_order_free_and_member_sensitive(tmp_path, fx, block):
    cfg = config(tmp_path, schema=fx.manifest["metadata_schema"], collection=block)
    a, b = partition_recipe(cfg, "logs", keyword_epoch="kw-e1").fingerprint(), partition_recipe(cfg, "logs", keyword_epoch="kw-e2").fingerprint()
    assert unified_fingerprint(cfg, [a, b]) == unified_fingerprint(cfg, [b, a])
    assert unified_fingerprint(cfg, [a, b]) != unified_fingerprint(cfg, [a])
    assert unified_fingerprint(cfg, [a], layers=[{"name": "graph", "digest": "0" * 64}]) != unified_fingerprint(cfg, [a])


def test_a_v2_configuration_has_no_collection_recipe(tmp_path):
    with pytest.raises(ValueError, match="partitioned configuration"):
        collection_recipe(config(tmp_path, arms=("lexical",)))
