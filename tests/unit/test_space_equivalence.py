"""Declared embedding-space id over an adopted member (staging F5; plan §6.2).

A unified configuration that declares its own ``collection.embedding_space.id`` refused compose with
the adopted store (whose recorded space id is its model-derived default). The explicit mapping
``collection.members.<id>.adopt.embedding_space`` plus a passing verify sample (re-embedded online,
mean/min cosine) recorded in the member registry lets compose accept it; nothing else does.
Synthetic F0 support store, hash embedder; no provider call.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

import tests.live_log_support as L
from su_multi_arm import Engine
from su_multi_arm.core.config import validate_config
from su_multi_arm.ingestion.members import MemberRegistry
from su_multi_arm.ingestion.recipes import embedding_space_ref
from su_multi_arm.ingestion.space_equivalence import RECORD, SpaceEquivalenceError, verify


def setup(tmp_path, *, mapping: bool):
    fx = L.fixture()
    original = L.build_support_store(tmp_path, fx)
    data = L.unified_data(tmp_path, fx, original, logs=False)
    data["collection"]["embedding_space"] = {"id": "support-space-v1"}
    if mapping:
        from su_multi_arm.core.config import load_config

        store_space = embedding_space_ref(load_config(original)).id
        data["collection"]["members"]["support"]["adopt"]["embedding_space"] = store_space
    config = validate_config(data)
    registry = MemberRegistry(config)
    registry.adopt("support")
    return config, registry


def test_without_a_mapping_a_declared_space_id_refuses_compose(tmp_path):
    config, registry = setup(tmp_path, mapping=False)
    with pytest.raises(ValueError, match="re-embed it in the collection's space"):
        Engine(config).compose()


def test_a_mapping_without_a_verified_sample_refuses_compose(tmp_path):
    config, _ = setup(tmp_path, mapping=True)
    with pytest.raises(ValueError, match="not verified"):
        Engine(config).compose()


def test_a_verified_mapping_is_accepted_and_recorded(tmp_path):
    config, registry = setup(tmp_path, mapping=True)
    engine = Engine(config)
    record = verify(config, registry, "support", engine.embedder.encode)
    assert record["passed"] and record["checked"] == record["store_chunks"] and record["mean_cosine"] >= 0.995
    ref = next(r for r in registry.live_members() if r.member == "support")
    stored = json.loads((registry.unit_dir(ref) / RECORD).read_text())
    assert stored["passed"] and stored["collection_space"]["id"] == "support-space-v1"
    manifest = engine.compose()
    assert [m["member"] for m in manifest["members"]] == ["support"]


def test_a_failing_sample_is_recorded_and_refuses_compose(tmp_path):
    config, registry = setup(tmp_path, mapping=True)
    rng = np.random.default_rng(0)
    record = verify(config, registry, "support", lambda texts: rng.normal(size=(len(texts), 64)))
    assert not record["passed"] and "mean cosine" in record["reason"]
    with pytest.raises(ValueError, match="check of member 'support' failed"):
        Engine(config).compose()


def test_a_record_for_other_thresholds_is_stale(tmp_path):
    config, registry = setup(tmp_path, mapping=True)
    verify(config, registry, "support", Engine(config).embedder.encode)
    data = config.model_dump(mode="json", by_alias=True)
    data["collection"]["embedding_space"]["equivalence"] = {"sample": 400, "mean_cosine": 0.995, "min_cosine": 0.98}
    with pytest.raises(ValueError, match="another space, store generation or threshold"):
        Engine(validate_config(data)).compose()


def test_the_mapping_needs_a_declared_space_and_a_different_id(tmp_path):
    fx = L.fixture()
    original = L.build_support_store(tmp_path, fx)
    data = L.unified_data(tmp_path, fx, original, logs=False)
    data["collection"]["members"]["support"]["adopt"]["embedding_space"] = "hash"
    with pytest.raises(Exception, match="declare collection.embedding_space"):
        validate_config(data)
    data["collection"]["embedding_space"] = {"id": "hash"}
    with pytest.raises(Exception, match="no mapping is needed"):
        validate_config(data)


def test_verify_refuses_a_member_without_the_mapping(tmp_path):
    config, registry = setup(tmp_path, mapping=False)
    with pytest.raises(SpaceEquivalenceError, match="no adopt.embedding_space mapping"):
        verify(config, registry, "support", lambda texts: np.ones((len(texts), 64)))
