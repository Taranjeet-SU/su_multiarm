"""Typed provenance for deliberately filtered incident seeds and bounded context.

The exact source population belongs to the seed query snapshot. Context windows
have separate snapshots; their union is never extrapolated to that population.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from ..models import StrictModel

INCIDENT_SCOPE = "incident_seed_policy_with_bounded_context"
HASH = r"^[0-9a-f]{64}$"


class IncidentContextWindow(StrictModel):
    query_id: str = Field(pattern=HASH)
    seed_record_ids: list[str] = Field(min_length=1)
    start: datetime
    end: datetime
    status: Literal["complete", "pending"]
    source_total: int | None = Field(default=None, ge=0, strict=True)
    record_ids: list[str] = Field(default_factory=list)
    snapshot: Literal["separate_from_seed_snapshot"] = "separate_from_seed_snapshot"
    reason: str | None = None

    @model_validator(mode="after")
    def validate_window(self):
        if self.start.tzinfo is None or self.end.tzinfo is None or self.start > self.end:
            raise ValueError("incident context requires an aware ordered window")
        if len(self.seed_record_ids) != len(set(self.seed_record_ids)) or not all(self.seed_record_ids):
            raise ValueError("duplicate or empty incident context seed")
        if len(self.record_ids) != len(set(self.record_ids)):
            raise ValueError("duplicate incident context record")
        if self.status == "complete" and (
            self.source_total != len(self.record_ids) or not set(self.seed_record_ids) <= set(self.record_ids)
        ):
            raise ValueError("complete incident context must reconcile and include its seed")
        if self.status == "pending" and self.record_ids:
            raise ValueError("pending context cannot attest retained context records")
        return self


class IncidentAccounting(StrictModel):
    """Compact accounting provenance; per-record membership stays in UnitArtifacts."""

    collection_scope: Literal["incident_seed_policy_with_bounded_context"] = INCIDENT_SCOPE
    policy_id: str = Field(pattern=HASH)
    query_id: str = Field(pattern=HASH)
    window_start: datetime
    window_end: datetime
    source_total: int = Field(ge=0, strict=True)
    selected_seed_total: int = Field(ge=0, strict=True)
    seeds_collected: int = Field(ge=0, strict=True)
    context_documents: int = Field(ge=0, strict=True)
    context_pending_seeds: int = Field(ge=0, strict=True)
    context_windows: int = Field(ge=0, strict=True)


class IncidentSelection(StrictModel):
    contract: Literal["elk_incidents_v1"] = "elk_incidents_v1"
    collection_scope: Literal["incident_seed_policy_with_bounded_context"] = INCIDENT_SCOPE
    policy_id: str = Field(pattern=HASH)
    query_id: str = Field(pattern=HASH)
    import_recipe: str = Field(pattern=HASH)
    window_start: datetime
    window_end: datetime
    source_total: int = Field(ge=0, strict=True)
    selected_seed_total: int = Field(ge=0, strict=True)
    seed_record_ids: list[str]
    context_windows: list[IncidentContextWindow] = Field(default_factory=list)
    source_snapshot: Literal["seed_initial_search"] = "seed_initial_search"

    @model_validator(mode="after")
    def validate_selection(self):
        seed_ids = set(self.seed_record_ids)
        if (
            self.window_start.tzinfo is None
            or self.window_end.tzinfo is None
            or self.window_start >= self.window_end
        ):
            raise ValueError("incident selection requires an aware half-open window")
        if (
            len(seed_ids) != len(self.seed_record_ids)
            or len(self.seed_record_ids) != self.selected_seed_total
        ):
            raise ValueError("incident selected seed IDs do not reconcile")
        if self.selected_seed_total > self.source_total:
            raise ValueError("incident seeds exceed source population")
        if len({w.query_id for w in self.context_windows}) != len(self.context_windows):
            raise ValueError("duplicate incident context query")
        for window in self.context_windows:
            window.validate_window()
            if not set(window.seed_record_ids) <= seed_ids:
                raise ValueError("incident context references an unknown seed")
        return self

    def members(self) -> dict[str, list[str]]:
        roles = {rid: ["seed"] for rid in self.seed_record_ids}
        for window in self.context_windows:
            for rid in window.record_ids:
                role = roles.setdefault(rid, [])
                if "context" not in role:
                    role.append("context")
        return roles

    def counters(self) -> dict[str, int]:
        seeds = set(self.seed_record_ids)
        complete = {rid for w in self.context_windows if w.status == "complete" for rid in w.seed_record_ids}
        pending = seeds - complete | {
            rid for w in self.context_windows if w.status == "pending" for rid in w.seed_record_ids
        }
        return {
            "incident_units": 1,
            "source_window_documents": self.source_total,
            "selected_seed_documents": self.selected_seed_total,
            "seeds_collected": len(seeds),
            "context_documents": len(set(self.members()) - seeds),
            "context_pending_seeds": len(pending),
        }

    def summary(self) -> IncidentAccounting:
        counts = self.counters()
        return IncidentAccounting(
            policy_id=self.policy_id,
            query_id=self.query_id,
            window_start=self.window_start,
            window_end=self.window_end,
            source_total=self.source_total,
            selected_seed_total=self.selected_seed_total,
            seeds_collected=counts["seeds_collected"],
            context_documents=counts["context_documents"],
            context_pending_seeds=counts["context_pending_seeds"],
            context_windows=len(self.context_windows),
        )
