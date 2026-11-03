"""Selection (plan §3.9): collapse → limits → union → ordering → diversity → pins → packing.

Each stage is a pure function; ``select`` chains the record stages the way the engine will and
returns the ordered union with a trace of the effective policy. Ordering never changes membership.
"""

from __future__ import annotations

from pydantic import Field

from ..core.config_selection import OrderingKind, PackingConfig
from ..models import StrictModel
from .collapse import CollapseUnit, collapse, collapse_arm
from .diversity import DiversityTrace, diversify
from .limits import limit
from .ordering import OrderingPolicy, OrderingTrace, order
from .packing import PackedEvidence, PackExtra, PackRecord, PackResult, pack, render
from .pins import pin
from .types import Candidate, PerArm, Selected
from .union import membership, merge_pools, union


class SelectionPolicy(StrictModel):
    collapse: CollapseUnit = "document"
    per_arm: int = Field(default=20, ge=1, le=1000)
    ordering: OrderingKind = "reciprocal_rank"
    rrf_k: int = Field(default=60, ge=1)
    arm_weights: dict[str, float] = Field(default_factory=dict)
    source_weights: dict[str, float] = Field(default_factory=dict)
    max_documents: int | None = Field(default=None, ge=1)
    diversity_field: str | None = None
    diversity_min_per_value: int = Field(default=1, ge=1)
    pins: bool = True

    def ordering_policy(self) -> OrderingPolicy:
        return OrderingPolicy(
            kind=self.ordering,
            rrf_k=self.rrf_k,
            arm_weights=self.arm_weights,
            source_weights=self.source_weights,
            max_documents=self.max_documents,
        )


class SelectionTrace(StrictModel):
    collapse: CollapseUnit
    per_arm: int
    per_arm_counts: dict[str, int]
    ordering: OrderingTrace
    diversity: DiversityTrace | None = None
    pinned: int = 0


def select(per_arm: PerArm, policy: SelectionPolicy) -> tuple[list[Selected], PerArm, SelectionTrace]:
    """Returns the ordered union, the per-arm pools after limits, and the effective-policy trace."""
    pools = limit(collapse(per_arm, policy.collapse), policy.per_arm)
    ordered, ordering_trace = order(pools, policy.ordering_policy())
    diversity_trace = None
    if policy.diversity_field:
        ordered, diversity_trace = diversify(ordered, policy.diversity_field, policy.diversity_min_per_value)
    if policy.pins:
        ordered = pin(ordered)
    trace = SelectionTrace(
        collapse=policy.collapse,
        per_arm=policy.per_arm,
        per_arm_counts={arm: len(c) for arm, c in pools.items()},
        ordering=ordering_trace,
        diversity=diversity_trace,
        pinned=sum(1 for r in ordered if r.pinned),
    )
    return ordered, pools, trace


__all__ = [
    "Candidate",
    "CollapseUnit",
    "DiversityTrace",
    "OrderingPolicy",
    "OrderingTrace",
    "PackExtra",
    "PackRecord",
    "PackResult",
    "PackedEvidence",
    "PackingConfig",
    "PerArm",
    "Selected",
    "SelectionPolicy",
    "SelectionTrace",
    "collapse",
    "collapse_arm",
    "diversify",
    "limit",
    "membership",
    "merge_pools",
    "order",
    "pack",
    "pin",
    "render",
    "select",
    "union",
]
