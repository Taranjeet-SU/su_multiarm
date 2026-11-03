"""Selection data types (plan §3.9): arm candidates in, ordered selected records out."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ..core.types import JsonScalar
from ..models import StrictModel


class Candidate(StrictModel):
    """One arm's hit, at passage or record granularity, before collapse."""

    record_id: str = Field(min_length=1)
    arm: str = Field(min_length=1)
    score: float
    chunk_ids: list[str] = Field(default_factory=list)
    source: str = "document"  # key into selection.source_weights
    kind: str = "document"
    metadata: dict[str, JsonScalar] = Field(default_factory=dict)
    provenance: list[dict[str, Any]] = Field(default_factory=list)
    pinned: bool = False  # set by the exact_id arm; honoured by selection.pins


class Selected(StrictModel):
    """A canonical record in the final union with its ordering score and selection reasons."""

    record_id: str = Field(min_length=1)
    score: float
    arms: list[str] = Field(default_factory=list)
    chunk_ids: list[str] = Field(default_factory=list)
    source: str = "document"
    kind: str = "document"
    metadata: dict[str, JsonScalar] = Field(default_factory=dict)
    provenance: list[dict[str, Any]] = Field(default_factory=list)
    pinned: bool = False
    reasons: list[str] = Field(default_factory=list)  # e.g. "arm:lexical rank 2", "pin:exact_id"


PerArm = dict[str, list[Candidate]]
