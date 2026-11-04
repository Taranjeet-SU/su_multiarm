"""Component lifecycle contract (plan §3.2) and the artifact rules that keep config free of code.

A component is constructed from a validated config model and injected dependencies, validated
without network access (unless ``probe`` is requested), run with a ``RunContext``, and may declare a
recipe fingerprint that becomes part of the index identity. No component is ever loaded from a
dotted import path or a pickled file named in YAML.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Mapping, Protocol, runtime_checkable

from pydantic import Field

from ..models import StrictModel
from .errors import ArtifactError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..runtime.context import RunContext

ComponentKind = Literal[
    "arm",
    "extractor",
    "decision",
    "reviewer",
    "tool",
    "action",
    "guard",
    "selector",
    "generator",
    "community",
    "provider",
    "backend",
    "source",
]
COMPONENT_KINDS: tuple[str, ...] = (
    "arm",
    "extractor",
    "decision",
    "reviewer",
    "tool",
    "action",
    "guard",
    "selector",
    "generator",
    "community",
    "provider",
    "backend",
    "source",
)
Severity = Literal["error", "warning", "info"]
COMPONENT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class Diagnostic(StrictModel):
    severity: Severity
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    path: str | None = None
    component: str | None = None


@dataclass(frozen=True)
class Environment:
    """What ``validate()`` may inspect without a network: environment variables and the config dir."""

    env: Mapping[str, str] = field(default_factory=dict)
    base_path: Path = Path(".")

    def has(self, name: str | None) -> bool:
        return bool(name) and bool(self.env.get(name))


@dataclass(frozen=True)
class Dependencies:
    """Injected collaborators. Backends and providers are resolved by name from the config."""

    backends: Mapping[str, Any] = field(default_factory=dict)
    providers: Mapping[str, Any] = field(default_factory=dict)
    components: Mapping[str, Any] = field(default_factory=dict)
    metadata_schema: Any = None


@runtime_checkable
class Component(Protocol):
    name: ClassVar[str]
    kind: ClassVar[str]
    config_model: ClassVar[type[StrictModel]]

    def __init__(self, config: StrictModel, deps: Dependencies): ...

    def validate(self, env: Environment, probe: bool = False) -> list[Diagnostic]: ...

    def run(self, ctx: RunContext, inputs: Any) -> Any: ...

    def recipe_fingerprint(self) -> str | None: ...


def is_component_class(obj: Any) -> bool:
    """Structural check used by registries and plugin loading (``isinstance`` needs an instance)."""
    if not isinstance(obj, type):
        return False
    name = getattr(obj, "name", None)
    kind = getattr(obj, "kind", None)
    model = getattr(obj, "config_model", None)
    return (
        isinstance(name, str)
        and bool(COMPONENT_NAME.fullmatch(name))
        and kind in COMPONENT_KINDS
        and isinstance(model, type)
        and issubclass(model, StrictModel)
        and all(callable(getattr(obj, m, None)) for m in ("validate", "run", "recipe_fingerprint"))
    )


def recipe_fingerprint(name: str, config: StrictModel | Mapping[str, Any], version: str = "1") -> str:
    """Deterministic fingerprint helper for components whose config affects index identity."""
    data = config.model_dump(mode="json") if isinstance(config, StrictModel) else dict(config)
    return hashlib.sha256(json.dumps({"name": name, "version": version, "config": data}, sort_keys=True).encode()).hexdigest()


# --- artifact rules (§3.2, §3.7.3) -------------------------------------------------------------

PICKLE_SUFFIXES = (".pkl", ".pickle", ".joblib", ".dill")
# ``package.module:attr`` factory references (a dotted module part is required so that model ids
# with a tag such as ``mistral:latest`` are not mistaken for code); URLs (``scheme://``) never match.
DOTTED_FACTORY = re.compile(r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)+:[A-Za-z_][\w]*$")
DOTTED_IMPORT = re.compile(r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_ARTIFACT_SUFFIXES = (".onnx",)


def check_code_reference(value: str, path: str, *, names_code: bool = False) -> None:
    """Reject strings that name executable artifacts or importable objects.

    Pickle-style files and ``pkg.module:attr`` factories are rejected everywhere. A bare dotted path
    (``pkg.module.Class``) is rejected only where the value names a component or artifact
    (``names_code=True``), because elsewhere it is indistinguishable from a hostname; a bare file
    name with an allowed artifact suffix (``intent.onnx``) is a path, not an import.
    """
    lowered = value.strip().lower()
    if lowered.endswith(PICKLE_SUFFIXES) or any(f"{s}?" in lowered or f"{s}#" in lowered for s in PICKLE_SUFFIXES):
        raise ArtifactError("pickled artifacts are not loadable from configuration", path)
    if "://" not in value and DOTTED_FACTORY.match(value.strip()):
        raise ArtifactError("dotted import paths are not loadable from configuration", path)
    stripped = value.strip()
    if names_code and DOTTED_IMPORT.match(stripped) and not stripped.lower().endswith(ALLOWED_ARTIFACT_SUFFIXES):
        raise ArtifactError("dotted import paths are not loadable from configuration", path)


class ClassifierManifest(StrictModel):
    features: str = Field(pattern=r"^(hashed_ngrams|embedding:[a-z][a-z0-9_]*)$")
    labels: list[str] = Field(min_length=1)
    threshold: float = Field(ge=0, le=1)


class ArtifactRef(StrictModel):
    """Safe classifier artifact: an ONNX file pinned by sha256 with a declared manifest."""

    model_path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest: ClassifierManifest

    @classmethod
    def check_path(cls, value: Path) -> Path:
        check_code_reference(str(value), "model_path", names_code=True)
        if value.suffix.lower() not in ALLOWED_ARTIFACT_SUFFIXES:
            raise ArtifactError(
                f"artifact must be one of {ALLOWED_ARTIFACT_SUFFIXES}, got {value.suffix!r}", "model_path"
            )
        return value

    def model_post_init(self, __context: Any) -> None:
        self.check_path(self.model_path)

    def verify(self, base_path: Path | None = None) -> Path:
        """Check the file exists and its sha256 matches. Returns the resolved path."""
        path = self.model_path if self.model_path.is_absolute() else (base_path or Path(".")) / self.model_path
        if not path.is_file():
            raise ArtifactError(f"artifact file not found: {path}", "model_path")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != self.sha256:
            raise ArtifactError("artifact sha256 mismatch", "sha256")
        return path


def scan_for_code_references(data: Any, path: str = "", skip_keys: frozenset[str] = frozenset()) -> None:
    """Walk a config mapping and apply ``check_code_reference`` to every string value."""
    if isinstance(data, Mapping):
        for key, value in data.items():
            if key in skip_keys:
                continue
            scan_for_code_references(value, f"{path}.{key}" if path else str(key), skip_keys)
    elif isinstance(data, (list, tuple)):
        for index, value in enumerate(data):
            scan_for_code_references(value, f"{path}[{index}]", skip_keys)
    elif isinstance(data, str):
        check_code_reference(data, path)
