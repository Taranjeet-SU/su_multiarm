"""Selection configuration models (plan §3.9): ordering kinds and evidence packing.

``reciprocal_rank`` is the default and reproduces the alpha's ``per_arm`` fusion order (sum of
``1/rank`` per arm, weighted); ``per_arm`` is accepted as a deprecated alias of it. ``rrf`` is the
distinct ``1/(k+rank)`` formula. Packing has no hardcoded field order: ``field_priority`` is the
operator's list and everything else follows in document field order.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from ..models import StrictModel

OrderingKind = Literal[
    "reciprocal_rank",
    "per_arm",  # deprecated alias of reciprocal_rank (v1 fusion.strategy name)
    "best_arm_rank",
    "stable_interleave",
    "rrf",
    "minmax",
    "zscore",
]
ORDERING_KINDS: tuple[str, ...] = (
    "reciprocal_rank",
    "per_arm",
    "best_arm_rank",
    "stable_interleave",
    "rrf",
    "minmax",
    "zscore",
)
FIELD_NAME = r"^[a-z][a-z0-9_]{0,63}$"


class PackingConfig(StrictModel):
    """Evidence budget with fair per-record allocation and a configured field priority."""

    budget_chars: int = Field(default=60000, ge=500, le=500000)
    field_priority: list[str] = Field(default_factory=list)  # configured order beats any field name
    fairness: Literal["fair_share", "greedy"] = "fair_share"
    min_per_record: int = Field(default=200, ge=0)  # floor of the fair share before a record is omitted
    # Largest fraction of ``budget_chars`` reserved for aggregate evidence before documents are
    # allocated; an aggregate that does not fit under it is omitted (and reported), so aggregates
    # can never starve every document. 1.0 restores the uncapped reservation.
    aggregate_reserve_max: float = Field(default=0.5, gt=0, le=1)

    @field_validator("field_priority")
    @classmethod
    def unique_identifiers(cls, value):
        import re

        for name in value:
            if not re.fullmatch(FIELD_NAME, name):
                raise ValueError(f"field_priority entry {name!r} must be a field identifier")
        if len(set(value)) != len(value):
            raise ValueError("field_priority must be unique")
        return value
