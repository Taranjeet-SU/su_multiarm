"""Named pipelines at run time (plan §3.3, Phase 1 "Declarative pipeline").

The configuration's ``pipeline`` block is the pipeline named ``default``; ``pipelines`` adds named
variants (overlays already merged by ``Config``). The engine builds one ``PipelineRuntime`` per
pipeline: its arms, one decision agent per allowed mode, the review stage per mode (it re-plans
through that mode's agent) and its answer contract. ``choose`` resolves a request's ``pipeline`` and
``mode`` against the ``request.pipelines`` / ``request.modes`` allowlists; anything else is a
``RequestRejected`` (a ``ValueError``, so HTTP maps it to 422), never a silent fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.errors import SuError

if TYPE_CHECKING:  # pragma: no cover
    from ..core.config import Config, PipelineConfig

ARM_ORDER: tuple[str, ...] = ("exact_id", "dense", "lexical", "structured", "graph", "community")


class RequestRejected(SuError, ValueError):
    """A request asked for a pipeline or mode the configuration does not allow."""


@dataclass
class PipelineRuntime:
    name: str
    config: PipelineConfig
    arms: dict[str, Any]
    decisions: dict[str, Any]  # mode (decision kind) -> DecisionAgent
    reviews: dict[str, Any] = field(default_factory=dict)  # mode -> ReviewStage | None
    contract: Any = None

    @property
    def default_mode(self) -> str:
        return self.config.decision.kind

    def review_enabled(self) -> bool:
        return self.config.review.enabled

    def id_patterns(self) -> list[str]:
        arm = self.arms.get("exact_id")
        return list(arm.id_patterns()) if arm is not None and hasattr(arm, "id_patterns") else []

    def enabled_arms(self) -> list[str]:
        return [a for a in ARM_ORDER if a in self.arms]


def allowed_modes(config: Config, pipeline: PipelineConfig) -> list[str]:
    return list(dict.fromkeys([pipeline.decision.kind, *config.request.modes]))


def selectable_pipelines(config: Config) -> list[str]:
    return list(dict.fromkeys([config.default_pipeline, *config.request.pipelines]))


def choose(config: Config, pipelines: dict[str, PipelineRuntime], name: str | None, mode: str | None) -> tuple[PipelineRuntime, str]:
    """The pipeline and decision mode a request runs with (allowlists enforced)."""
    chosen = name or config.default_pipeline
    if chosen not in selectable_pipelines(config) or chosen not in pipelines:
        raise RequestRejected(f"pipeline {chosen!r} is not selectable per request; allowed: {selectable_pipelines(config)}")
    runtime = pipelines[chosen]
    effective = mode or runtime.default_mode
    if effective != runtime.default_mode and effective not in config.request.modes:
        raise RequestRejected(f"mode {effective!r} is not allowed per request; allowed: {allowed_modes(config, runtime.config)}")
    if effective not in runtime.decisions:
        raise RequestRejected(f"mode {effective!r} is not available on pipeline {chosen!r}")
    return runtime, effective
