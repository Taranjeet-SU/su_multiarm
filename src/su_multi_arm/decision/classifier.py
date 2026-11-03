"""``classifier`` decision agent (plan §3.7, §3.7.3): interface and artifact contract only.

A classifier maps a question to one enabled non-structured arm with a confidence. It never
produces a typed scope, so an aggregate question fails (``no_plan``) and, in a cascade, falls to
the next stage. The built-in ``onnx_classifier`` accepts only an ONNX file pinned by sha256 with a
manifest (features, labels, threshold); pickled artifacts and dotted import paths are validation
errors (``core/component.py``). No weights ship with the package and no inference runtime is a
dependency: a ``ClassifierModel`` must be injected through the Python API
(``Dependencies.components["classifier"]``), otherwise ``validate()`` reports an error. Default off.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, runtime_checkable

from pydantic import Field, model_validator

from ..core.component import ArtifactRef, Dependencies, Diagnostic, Environment
from ..core.errors import ArtifactError
from ..core.types import UnsupportedConstraint
from ..models import StrictModel
from .base import RETRIEVAL_ARMS, Capabilities, Decision, DecisionAgent, DecisionFailure, DecisionRequest, Plan, RetrievalStep
from .intent import Intents

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime.context import RunContext


class ClassifierOutput(StrictModel):
    label: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


@runtime_checkable
class ClassifierModel(Protocol):
    """Inference runtime injected through the Python API; never loaded from configuration."""

    def classify(self, query: str, intents: Intents) -> ClassifierOutput: ...


class ClassifierConfig(StrictModel):
    on_failure: Literal["fail_closed", "raw_all_arms"] = "fail_closed"
    artifact: ArtifactRef | None = None  # built-in onnx_classifier
    component: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")  # plugin component name
    threshold: float | None = Field(default=None, ge=0, le=1)  # overrides manifest threshold when set

    @model_validator(mode="after")
    def exactly_one_source(self):
        if (self.artifact is None) == (self.component is None):
            raise ValueError("a classifier needs exactly one of artifact (onnx) or component (plugin)")
        return self

    def labels(self) -> list[str] | None:
        return self.artifact.manifest.labels if self.artifact else None

    def effective_threshold(self) -> float:
        if self.threshold is not None:
            return self.threshold
        return self.artifact.manifest.threshold if self.artifact else 0.5


class ClassifierAgent(DecisionAgent):
    name: ClassVar[str] = "classifier"
    config_model: ClassVar[type[StrictModel]] = ClassifierConfig

    def __init__(self, config: StrictModel, deps: Dependencies):
        super().__init__(config, deps)
        self.model: ClassifierModel | None = deps.components.get("classifier")
        self.artifact_sha: str | None = None
        self._artifact_error: str | None = None

    def validate(self, env: Environment, probe: bool = False) -> list[Diagnostic]:
        cfg: ClassifierConfig = self.config  # type: ignore[assignment]
        diagnostics: list[Diagnostic] = []
        if cfg.artifact is not None:
            try:
                cfg.artifact.verify(env.base_path)
                self.artifact_sha = cfg.artifact.sha256
            except ArtifactError as error:
                self._artifact_error = str(error)
                diagnostics.append(Diagnostic(severity="error", code="artifact_invalid", message=str(error), path="artifact", component=self.name))
            bad = [label for label in cfg.artifact.manifest.labels if label not in RETRIEVAL_ARMS]
            if bad:
                diagnostics.append(
                    Diagnostic(
                        severity="error",
                        code="label_not_an_arm",
                        message=f"labels must be non-structured arm names, got {bad}",
                        path="artifact.manifest.labels",
                        component=self.name,
                    )
                )
        if self.model is None:
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    code="runtime_unavailable",
                    message="no classifier runtime: inject a ClassifierModel through the Python API; nothing is loaded from configuration",
                    component=self.name,
                )
            )
        elif not isinstance(self.model, ClassifierModel):
            diagnostics.append(Diagnostic(severity="error", code="runtime_invalid", message="injected classifier lacks classify()", component=self.name))
        return diagnostics

    def attempt(self, ctx: RunContext, request: DecisionRequest, intents: Intents, capabilities: Capabilities) -> Decision:
        cfg: ClassifierConfig = self.config  # type: ignore[assignment]
        if cfg.artifact is not None and self.artifact_sha is None:
            raise DecisionFailure("artifact_invalid", self._artifact_error or "artifact was not verified by validate()")
        if self.model is None:
            raise DecisionFailure("runtime_unavailable", "no classifier runtime is injected")
        if intents.aggregate():
            flagged = [UnsupportedConstraint(constraint_text=a.text, reason="aggregate_intent_without_scope") for a in intents.aggregates]
            raise DecisionFailure("no_plan", "a classifier cannot produce a typed scope for an aggregate question", flagged)
        output = self.model.classify(request.query, intents)
        ctx.trace.event("classifier", label=output.label, confidence=output.confidence, artifact_sha256=self.artifact_sha)
        if output.confidence < cfg.effective_threshold():
            raise DecisionFailure(
                "low_confidence",
                f"confidence {output.confidence:.3f} below threshold {cfg.effective_threshold():.3f}",
                confidence=output.confidence,
            )
        labels = cfg.labels()
        if labels is not None and output.label not in labels:
            raise DecisionFailure("invalid_step", f"label {output.label!r} is not in the manifest")
        if output.label not in capabilities.retrieval_arms():
            raise DecisionFailure("invalid_step", f"label {output.label!r} is not an enabled non-structured arm")
        return Decision(
            plan=Plan(steps=[RetrievalStep(arm=output.label, query=request.query)]),  # type: ignore[arg-type]
            outcome="decided",
            confidence=output.confidence,
            agent=self.name,
            policy_applied="classifier",
            intents=intents,
        )
