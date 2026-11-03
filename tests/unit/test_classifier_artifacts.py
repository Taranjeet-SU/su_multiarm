"""Classifier artifact contract (plan §3.7.3): pickle and dotted paths fail, sha256 mismatch fails,
no weights ship, no runtime is loaded from configuration, default off."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from su_multi_arm.core.component import ArtifactRef, ClassifierManifest, Dependencies, Environment
from su_multi_arm.core.config import DecisionConfig
from su_multi_arm.core.errors import ArtifactError
from su_multi_arm.decision import ClassifierAgent, ClassifierConfig, ClassifierOutput

MANIFEST = ClassifierManifest(features="hashed_ngrams", labels=["lexical"], threshold=0.5)
SHA = "a" * 64


@pytest.mark.parametrize("path", ["model.pkl", "weights.joblib", "sklearn/model.pickle", "pkg.module.Classifier", "pkg.module:factory"])
def test_pickle_and_dotted_paths_are_rejected(path):
    with pytest.raises((ArtifactError, ValidationError)):
        ClassifierConfig(artifact=ArtifactRef(model_path=Path(path), sha256=SHA, manifest=MANIFEST))
    with pytest.raises((ArtifactError, ValidationError)):
        DecisionConfig(kind="classifier", artifact={"model_path": path, "sha256": SHA, "manifest": MANIFEST.model_dump()})


def test_sha256_mismatch_fails_validation_and_decision(tmp_path, v2_ctx):
    path = tmp_path / "model.onnx"
    path.write_bytes(b"synthetic")
    ref = ArtifactRef(model_path=path, sha256=SHA, manifest=MANIFEST)
    with pytest.raises(ArtifactError, match="sha256 mismatch"):
        ref.verify(tmp_path)
    agent = ClassifierAgent(ClassifierConfig(artifact=ref), Dependencies(components={"classifier": _Runtime()}))
    assert [d.code for d in agent.validate(Environment(base_path=tmp_path))] == ["artifact_invalid"]
    missing = ArtifactRef(model_path=tmp_path / "absent.onnx", sha256=SHA, manifest=MANIFEST)
    with pytest.raises(ArtifactError, match="not found"):
        missing.verify(tmp_path)


class _Runtime:
    def classify(self, query, intents):
        return ClassifierOutput(label="lexical", confidence=0.9)


def test_verified_artifact_records_sha_and_requires_injected_runtime(tmp_path, v2_ctx):
    from su_multi_arm.decision import Capabilities, DecisionRequest

    path = tmp_path / "model.onnx"
    path.write_bytes(b"synthetic")
    ref = ArtifactRef(model_path=path, sha256=hashlib.sha256(b"synthetic").hexdigest(), manifest=MANIFEST)
    without = ClassifierAgent(ClassifierConfig(artifact=ref), Dependencies())
    assert [d.code for d in without.validate(Environment(base_path=tmp_path))] == ["runtime_unavailable"]
    assert without.decide(v2_ctx, DecisionRequest(query="q"), None, Capabilities(arms=["lexical"])).failure == "runtime_unavailable"
    wrong = ClassifierAgent(ClassifierConfig(artifact=ref), Dependencies(components={"classifier": object()}))
    assert [d.code for d in wrong.validate(Environment(base_path=tmp_path))] == ["runtime_invalid"]
    agent = ClassifierAgent(ClassifierConfig(artifact=ref), Dependencies(components={"classifier": _Runtime()}))
    assert agent.validate(Environment(base_path=tmp_path)) == [] and agent.artifact_sha == ref.sha256
    decision = agent.decide(v2_ctx, DecisionRequest(query="q"), None, Capabilities(arms=["lexical", "dense"]))
    assert decision.decided() and decision.plan.arms() == ["lexical"]
    assert any(e["name"] == "classifier" and e["artifact_sha256"] == ref.sha256 for s in v2_ctx.trace.spans for e in s.events)
    bad_label = ArtifactRef(model_path=path, sha256=ref.sha256, manifest=ClassifierManifest(features="hashed_ngrams", labels=["structured"], threshold=0.5))
    agent = ClassifierAgent(ClassifierConfig(artifact=bad_label), Dependencies(components={"classifier": _Runtime()}))
    assert [d.code for d in agent.validate(Environment(base_path=tmp_path))] == ["label_not_an_arm"]


def test_classifier_needs_exactly_one_source_and_defaults_off():
    with pytest.raises(ValidationError, match="exactly one"):
        ClassifierConfig()
    with pytest.raises(ValidationError, match="exactly one"):
        ClassifierConfig(component="my_plugin", artifact=ArtifactRef(model_path=Path("models/m.onnx"), sha256=SHA, manifest=MANIFEST))
    assert ClassifierConfig(component="my_plugin").effective_threshold() == 0.5
    assert DecisionConfig().kind == "rules"
    with pytest.raises(ValidationError, match="exactly one"):
        DecisionConfig(kind="classifier")
