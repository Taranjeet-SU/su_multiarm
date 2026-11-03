"""Dense arm: query embedding -> ``VectorIndex.search`` under the pushed-down scope -> document hits."""

from __future__ import annotations

from typing import ClassVar

import numpy as np

from ..core.component import Diagnostic, Environment, recipe_fingerprint
from ..core.config import DenseArm as DenseArmConfig
from ..runtime.context import RunContext
from .base import ArmInput, ArmResult, BaseArm


class DenseArm(BaseArm):
    name: ClassVar[str] = "dense"
    config_model: ClassVar[type] = DenseArmConfig
    capability: ClassVar[str] = "vector"
    depends_on_stage: ClassVar[str | None] = "embeddings"

    def __init__(self, config, deps):
        super().__init__(config, deps)
        self.embedder = deps.providers.get("embedder")

    def validate(self, env: Environment, probe: bool = False) -> list[Diagnostic]:
        diagnostics = super().validate(env, probe)
        if self.embedder is None:
            diagnostics.append(Diagnostic(severity="error", code="embedder_missing", message="dense arm needs models.roles.embedder", component=self.name))
        return diagnostics

    def recipe_fingerprint(self) -> str | None:
        """The embedder recipe is part of the index identity."""
        return recipe_fingerprint(self.name, self.embedder.config) if self.embedder is not None else None

    def _run(self, ctx: RunContext, inputs: ArmInput) -> ArmResult:
        vector = self.embedder.encode([inputs.query], query=True)[0]
        if not np.any(vector):
            return ArmResult(arm=self.name)  # punctuation-only query: no zero-vector matches
        hits = self._facet().search(ctx.generation, vector, inputs.scope, inputs.candidates)
        return ArmResult(arm=self.name, hits=self.collapse(ctx, [(h.chunk_id, h.score, {}) for h in hits]))
