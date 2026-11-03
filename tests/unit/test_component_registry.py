"""Component + registry (plan §3.2): registration, diagnostics, plugin and artifact rejection."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import ValidationError

from su_multi_arm.core.component import (
    ArtifactRef,
    Component,
    Dependencies,
    Diagnostic,
    Environment,
    check_code_reference,
    is_component_class,
    recipe_fingerprint,
    scan_for_code_references,
)
from su_multi_arm.core.errors import ArtifactError, PluginError, RegistryError
from su_multi_arm.core.registry import Registries, Registry, check_plugins_registered
from su_multi_arm.models import StrictModel


class EchoConfig(StrictModel):
    prefix: str = ""


class EchoArm:
    name: ClassVar[str] = "echo"
    kind: ClassVar[str] = "arm"
    config_model: ClassVar[type[StrictModel]] = EchoConfig

    def __init__(self, config: EchoConfig, deps: Dependencies):
        self.config, self.deps = config, deps

    def validate(self, env: Environment, probe: bool = False) -> list[Diagnostic]:
        if probe:
            return [Diagnostic(severity="info", code="probe", message="would call the network")]
        return [] if env.has("ECHO_TOKEN") else [Diagnostic(severity="error", code="missing_env", message="ECHO_TOKEN unset")]

    def run(self, ctx, inputs):
        return self.config.prefix + str(inputs)

    def recipe_fingerprint(self) -> str | None:
        return recipe_fingerprint(self.name, self.config)


class NotAComponent:
    name = "nope"


def test_registration_and_lookup():
    registry: Registry = Registry("arm")
    registry.register(EchoArm)
    assert registry.get("echo") is EchoArm and registry.names() == ["echo"] and "echo" in registry
    registry.register(EchoArm)  # idempotent for the same class
    with pytest.raises(RegistryError, match="no arm component"):
        registry.get("missing")
    with pytest.raises(RegistryError, match="Component contract"):
        registry.register(NotAComponent)  # type: ignore[arg-type]
    with pytest.raises(RegistryError, match="kind"):
        Registry("tool").register(EchoArm)
    with pytest.raises(RegistryError, match="unknown component kind"):
        Registry("widget")

    class EchoArm2(EchoArm):
        pass

    with pytest.raises(RegistryError, match="already registered"):
        registry.register(EchoArm2)
    instance = EchoArm(EchoConfig(prefix="x"), Dependencies())
    assert isinstance(instance, Component) and is_component_class(EchoArm) and not is_component_class(NotAComponent)


def test_validate_diagnostics_and_recipe_fingerprint_are_deterministic():
    arm = EchoArm(EchoConfig(prefix="a"), Dependencies())
    assert [d.severity for d in arm.validate(Environment(env={}))] == ["error"]
    assert arm.validate(Environment(env={"ECHO_TOKEN": "t"})) == []
    assert arm.validate(Environment(env={}), probe=True)[0].code == "probe"
    assert arm.recipe_fingerprint() == EchoArm(EchoConfig(prefix="a"), Dependencies()).recipe_fingerprint()
    assert arm.recipe_fingerprint() != EchoArm(EchoConfig(prefix="b"), Dependencies()).recipe_fingerprint()


def test_unknown_plugin_and_bad_plugin_names_are_rejected():
    registries = Registries()
    with pytest.raises(PluginError, match="not registered"):
        registries.load_plugins(["definitely_not_installed_plugin"])
    with pytest.raises(PluginError, match="identifier"):
        registries.load_plugins(["pkg.module:Factory"])
    with pytest.raises(PluginError, match="not registered"):
        check_plugins_registered(["definitely_not_installed_plugin"])
    assert registries.load_plugins([]) == []


def test_entry_point_plugin_must_be_a_conforming_component(monkeypatch):
    class FakeEntryPoint:
        def __init__(self, name, obj):
            self.name, self.obj = name, obj

        def load(self):
            return self.obj

    from su_multi_arm.core import registry as registry_module

    monkeypatch.setattr(registry_module, "available_plugins", lambda group=None: [FakeEntryPoint("echo", EchoArm), FakeEntryPoint("bad", NotAComponent)])
    registries = Registries()
    assert registries.load_plugins(["echo"]) == [EchoArm] and registries["arm"].get("echo") is EchoArm
    with pytest.raises(PluginError, match="Component class"):
        registries.load_plugins(["bad"])
    # Only listed names are loaded: nothing else from the group leaks into the registries.
    assert registries["arm"].names() == ["echo"] and len(registries["tool"]) == 0


@pytest.mark.parametrize(
    "value",
    ["model.pkl", "weights.joblib", "/abs/path/clf.pickle", "store.dill", "pkg.module:factory", "pkg.module:Class", "a.b.c:Klass"],
)
def test_pickle_and_dotted_import_references_are_rejected(value):
    with pytest.raises(ArtifactError):
        check_code_reference(value, "x")
    with pytest.raises(ArtifactError):
        scan_for_code_references({"nested": [{"path": value}]})


@pytest.mark.parametrize(
    "value",
    ["bolt://127.0.0.1:7687", "https://api.example.com/v1", "127.0.0.1:8099", "localhost:8099", "sentence-transformers/all-MiniLM-L6-v2", "api.example.com", "urchade/gliner_small-v2.1", "mistral:latest", "llama3:instruct", "phi3:mini"],
)
def test_hostnames_urls_and_model_ids_pass(value):
    check_code_reference(value, "x")
    scan_for_code_references({"models": {"roles": {"extractor": {"model": value}}}})


@pytest.mark.parametrize("value", ["intent.onnx", "models/intent.onnx", "./intent.onnx"])
def test_bare_artifact_file_names_are_paths_not_imports(value):
    check_code_reference(value, "model_path", names_code=True)
    assert ArtifactRef.check_path(Path(value)) == Path(value)


def test_bare_dotted_path_rejected_only_where_code_is_named():
    check_code_reference("pkg.module.Class", "hostname-like")
    with pytest.raises(ArtifactError):
        check_code_reference("pkg.module.Class", "component", names_code=True)


def test_artifact_ref_rules_and_sha256_verification(tmp_path: Path):
    manifest = {"features": "hashed_ngrams", "labels": ["a", "b"], "threshold": 0.5}
    good = tmp_path / "clf.onnx"
    good.write_bytes(b"onnx-bytes")
    digest = hashlib.sha256(b"onnx-bytes").hexdigest()
    ref = ArtifactRef(model_path=good, sha256=digest, manifest=manifest)
    assert ref.verify() == good
    with pytest.raises(ArtifactError, match="mismatch"):
        ArtifactRef(model_path=good, sha256="0" * 64, manifest=manifest).verify()
    with pytest.raises(ArtifactError, match="not found"):
        ArtifactRef(model_path=tmp_path / "missing.onnx", sha256=digest, manifest=manifest).verify()
    # Inside a model, pydantic wraps the ArtifactError; the message and rule are preserved.
    with pytest.raises(ValidationError, match="pickled artifacts"):
        ArtifactRef(model_path=tmp_path / "clf.pkl", sha256=digest, manifest=manifest)
    with pytest.raises(ValidationError, match="dotted import"):
        ArtifactRef(model_path=Path("sklearn.linear_model.LogisticRegression"), sha256=digest, manifest=manifest)
    with pytest.raises(ValidationError, match="must be one of"):
        ArtifactRef(model_path=tmp_path / "clf.bin", sha256=digest, manifest=manifest)
    with pytest.raises(ValidationError):
        ArtifactRef(model_path=good, sha256="nothex", manifest=manifest)
    with pytest.raises(ArtifactError, match="pickled"):
        ArtifactRef.check_path(Path("clf.joblib"))
