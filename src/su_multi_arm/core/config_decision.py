"""Decision-agent configuration models (plan §3.7.1 fail-policy table).

Kept out of ``core/config.py`` so the kernel file changes by one import and one field. Every default
is the fail-closed one from the table: a cascade stage that fails falls through to the next stage
(``rules``, ``classifier``) or abstains (``llm``); the standalone outcome is ``abstain`` unless the
operator opts into ``raw_all_arms`` on ``pipeline.decision.on_failure``.
"""

from __future__ import annotations

from typing import Literal

from ..models import StrictModel

StageName = Literal["rules", "classifier", "llm"]
StageFailure = Literal["fallback_to_next_stage", "abstain"]
STAGE_NAMES: tuple[str, ...] = ("rules", "classifier", "llm")


class StagePolicies(StrictModel):
    """Outcome of a failed stage inside a ``cascade`` (plan §3.7.1 rows "cascade stage")."""

    rules: StageFailure = "fallback_to_next_stage"
    classifier: StageFailure = "fallback_to_next_stage"
    llm: StageFailure = "abstain"

    def for_stage(self, stage: str) -> StageFailure:
        if stage not in STAGE_NAMES:
            raise ValueError(f"unknown decision stage {stage!r}")
        return getattr(self, stage)
