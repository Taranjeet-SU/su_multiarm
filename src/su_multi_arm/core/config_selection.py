"""Selection configuration models (plan §3.9): ordering kinds and evidence packing.

``reciprocal_rank`` is the default and reproduces the alpha's ``per_arm`` fusion order (sum of
``1/rank`` per arm, weighted); ``per_arm`` is accepted as a deprecated alias of it. ``rrf`` is the
distinct ``1/(k+rank)`` formula. Packing has no hardcoded field order: ``field_priority`` is the
operator's list and everything else follows in document field order.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

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
    metadata_fields: list[str] = Field(default_factory=list)  # explicit canonical metadata allowlist
    # ``prefix`` (default) truncates a long record to its opening. ``question_windows`` is opt-in:
    # a truncated record may instead carry up to three lexical question-matched literal windows.
    passage_selection: Literal["prefix", "question_windows"] = "prefix"
    fairness: Literal["fair_share", "greedy"] = "fair_share"
    min_per_record: int = Field(default=200, ge=0)  # floor of the fair share before a record is omitted
    # Largest fraction of ``budget_chars`` reserved for aggregate evidence before documents are
    # allocated; an aggregate that does not fit under it is omitted (and reported), so aggregates
    # can never starve every document. 1.0 restores the uncapped reservation.
    aggregate_reserve_max: float = Field(default=0.5, gt=0, le=1)
    # Largest share of a record's character allocation its allowlisted metadata may take. Keys are
    # dropped from the end of ``metadata_fields`` until the rest fits (traced per record); a record
    # is never omitted because of its metadata. 0.25 (the V3.2 setting, restored after M7: the
    # interim 1.0 default let metadata take most of the evidence budget and cut the passage share).
    metadata_max_share: float = Field(default=0.25, gt=0, le=1)
    # Allowlisted keys the share cap never drops (e.g. capture time, environment, identity). They
    # still count toward the record's allocation; when they alone exceed it, they are kept and the
    # passage text is trimmed (traced as ``metadata_protected_overflow``). Default: none.
    metadata_protected: list[str] = Field(default_factory=list)
    # Allowlisted keys packed as ``null`` when a record has no value (2026-11-09 corpus-B fix F10):
    # the generator then sees that the field is empty (e.g. a ``resolution`` field with no value)
    # instead of not seeing the field at all. Default: none (absent keys are not packed).
    metadata_show_null: list[str] = Field(default_factory=list)

    @field_validator("field_priority", "metadata_fields", "metadata_protected", "metadata_show_null")
    @classmethod
    def unique_identifiers(cls, value):
        import re

        for name in value:
            if not re.fullmatch(FIELD_NAME, name):
                raise ValueError(f"field_priority entry {name!r} must be a field identifier")
        if len(set(value)) != len(value):
            raise ValueError("field_priority must be unique")
        return value

    @model_validator(mode="after")
    def protected_are_allowlisted(self):
        unknown = [key for key in self.metadata_protected if key not in self.metadata_fields]
        if unknown:
            raise ValueError(f"metadata_protected keys must be listed in metadata_fields: {unknown}")
        unknown = [key for key in self.metadata_show_null if key not in self.metadata_fields]
        if unknown:
            raise ValueError(f"metadata_show_null keys must be listed in metadata_fields: {unknown}")
        return self


class RerankOptions(StrictModel):
    """``pipeline.selection.reranking`` (read only with ``rerank: true``; ``selection/rerank.py``).

    ``top_k_in``: unpinned records scored (the rest keep fusion order after them; None = all).
    ``top_k_out``: explicit cut after reranking (None = no cut). ``timeout_ms``: latency budget of
    the scoring call (None = unbounded). ``on_error``: ``fail`` raises; ``fusion_order`` keeps the
    fusion order and records ``status: failed_open`` with the reason in the selection trace.
    ``passage_chars``: characters of title + retrieved passage sent per record."""

    top_k_in: int | None = Field(default=None, ge=1, le=1000)
    top_k_out: int | None = Field(default=None, ge=1, le=1000)
    timeout_ms: int | None = Field(default=None, ge=10, le=600000)
    on_error: Literal["fail", "fusion_order"] = "fail"
    passage_chars: int = Field(default=1900, ge=100, le=20000)



SOURCE_NAME = r"^[a-z][a-z0-9_]{0,63}$"


def check_source_quotas(value: dict[str, float]) -> dict[str, float]:
    """``collection.selection.source_quotas`` (unified merge plan §6.5, §7; schema v3).

    Per-source shares of the candidate pools and the evidence budget when strata are fused. Keys
    are values of the ``source`` field declared by the collection's members (checked by the
    collection block); every share is in ``(0, 1]`` and the shares sum to at most 1 (the rest is
    unreserved and goes by fused rank). Validated by S0; read by the stratified selection (M1a)."""
    import re

    if not value:
        raise ValueError("source_quotas must name at least one source (omit the key for none)")
    for name, share in value.items():
        if not re.fullmatch(SOURCE_NAME, name):
            raise ValueError(f"source_quotas key {name!r} must be a source identifier")
        if not 0 < share <= 1:
            raise ValueError(f"source_quotas.{name} must be in (0, 1]")
    if sum(value.values()) > 1 + 1e-9:
        raise ValueError(f"source_quotas shares sum to {sum(value.values()):.6g}; at most 1")
    return value
