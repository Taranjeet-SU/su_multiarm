"""``ordering`` (plan §3.9): ranks the union without changing its membership.

``reciprocal_rank`` (default; ``per_arm`` is its deprecated alias) reproduces today's
``fusion.fuse(strategy="per_arm")``: each arm contributes ``arm_weight / rank`` for a record, the sum
is multiplied by the record's ``source_weight``, ties break on the record id. ``rrf`` contributes
``arm_weight / (rrf_k + rank)``; ``minmax`` rescales each arm's scores to [0, 1]; ``zscore`` maps each
arm's standardized score through the normal CDF (absence contributes zero, never a negative);
``best_arm_rank`` orders by the best rank any arm gave; ``stable_interleave`` round-robins the arms
in configured order. ``max_documents`` is the only membership cut and is explicit.
"""

from __future__ import annotations

import math
import statistics
from typing import Literal

from pydantic import Field

from ..core.config_selection import ORDERING_KINDS, OrderingKind
from ..models import StrictModel
from .types import PerArm, Selected
from .union import union

ALIASES = {"per_arm": "reciprocal_rank"}


class OrderingPolicy(StrictModel):
    kind: OrderingKind = "reciprocal_rank"
    rrf_k: int = Field(default=60, ge=1)
    arm_weights: dict[str, float] = Field(default_factory=dict)
    source_weights: dict[str, float] = Field(default_factory=dict)
    max_documents: int | None = Field(default=None, ge=1)

    def effective(self) -> str:
        return ALIASES.get(self.kind, self.kind)


class OrderingTrace(StrictModel):
    policy: str
    requested: str
    union_size: int
    returned: int
    cut_by_max_documents: int = 0


def _arm_scores(per_arm: PerArm, policy: OrderingPolicy) -> dict[str, dict[str, float]]:
    """Per arm: record id -> contribution under the effective policy."""
    kind = policy.effective()
    contributions: dict[str, dict[str, float]] = {}
    for arm, candidates in per_arm.items():
        unique = {}
        for candidate in candidates:
            unique.setdefault(candidate.record_id, candidate)
        values = [c.score for c in unique.values()]
        low, high = (min(values), max(values)) if values else (0.0, 0.0)
        mean, std = (statistics.mean(values), statistics.pstdev(values)) if values else (0.0, 0.0)
        weight = policy.arm_weights.get(arm, 1.0)
        scores: dict[str, float] = {}
        for rank, candidate in enumerate(unique.values(), 1):
            if kind == "rrf":
                score = 1 / (policy.rrf_k + rank)
            elif kind == "minmax":
                score = (candidate.score - low) / (high - low) if high != low else 1.0
            elif kind == "zscore":
                score = (1 + math.erf((candidate.score - mean) / (std * math.sqrt(2)))) / 2 if std else 0.5
            elif kind == "best_arm_rank":
                score = float(rank)
            else:  # reciprocal_rank / stable_interleave (rank kept for interleave)
                score = 1 / rank
            scores[candidate.record_id] = score * weight
        contributions[arm] = scores
    return contributions


def order(per_arm: PerArm, policy: OrderingPolicy) -> tuple[list[Selected], OrderingTrace]:
    kind = policy.effective()
    if kind not in ORDERING_KINDS:
        raise ValueError(f"unknown ordering {policy.kind!r}")
    selected = union(per_arm)
    contributions = _arm_scores(per_arm, policy)
    if kind == "best_arm_rank":
        for record in selected.values():
            record.score = min(contributions[arm][record.record_id] for arm in record.arms)
        ordered = sorted(selected.values(), key=lambda s: (s.score, s.record_id))
    elif kind == "stable_interleave":
        ordered, taken = [], set()
        queues = {arm: [c.record_id for c in per_arm[arm]] for arm in per_arm}
        position = 0
        while any(queues.values()):
            for arm in list(queues):
                while queues[arm]:
                    record_id = queues[arm].pop(0)
                    if record_id not in taken:
                        taken.add(record_id)
                        position += 1
                        record = selected[record_id]
                        record.score = 1 / position
                        ordered.append(record)
                        break
    else:
        for record in selected.values():
            record.score = sum(contributions[arm][record.record_id] for arm in record.arms)
            record.score *= policy.source_weights.get(record.source, 1.0)
        ordered = sorted(selected.values(), key=lambda s: (-s.score, s.record_id))
    for record in ordered:
        record.reasons.append(f"ordering:{kind}")
    cut = 0
    if policy.max_documents is not None and len(ordered) > policy.max_documents:
        cut = len(ordered) - policy.max_documents
        ordered = ordered[: policy.max_documents]
    trace = OrderingTrace(policy=kind, requested=policy.kind, union_size=len(selected), returned=len(ordered), cut_by_max_documents=cut)
    return ordered, trace


Ordering = Literal["reciprocal_rank", "per_arm", "best_arm_rank", "stable_interleave", "rrf", "minmax", "zscore"]
