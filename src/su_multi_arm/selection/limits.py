"""``limits`` (plan §3.9): the per-arm record budget, applied after collapse and before union."""

from __future__ import annotations

from .types import PerArm


def limit(per_arm: PerArm, per_arm_n: int) -> PerArm:
    if per_arm_n < 1:
        raise ValueError("per_arm must be at least 1")
    return {arm: sorted(c, key=lambda x: (-x.score, x.record_id))[:per_arm_n] for arm, c in per_arm.items()}
