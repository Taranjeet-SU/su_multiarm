"""Extraction policies (plan §3.14.1): ``strict`` (default), ``quarantine``, ``partial``.

* ``strict``: the first failing unit aborts the job; the old generation stays active.
* ``quarantine``: a document whose chunk extraction fails (grounding, endpoint, schema, predicate,
  ontology) stays in the catalog/lexical/dense/structured projections; **all** of its extraction
  outputs are excluded from the graph/community/report stores and its status records
  ``extraction: rejected:<category>``. Coverage is recorded per category.
* ``partial``: as ``quarantine`` plus whole stages stopped by a budget or provider failure are
  marked ``incomplete`` with the first unprocessed unit; activation needs ``allow_partial``.

No policy drops a unit silently or converts a rejected relation into a fact.
"""

from __future__ import annotations

from typing import Any, Literal, Sequence

from pydantic import Field

from ..models import StrictModel

PolicyName = Literal["strict", "quarantine", "partial"]
UNIT_CATEGORIES = frozenset({"grounding", "endpoint_not_in_span", "endpoint_type_ambiguous", "schema", "empty_output", "predicate", "ontology", "report_grounding"})
STAGE_CATEGORIES = frozenset({"provider", "budget"})


class PolicyViolation(Exception):
    """Raised when the policy cannot absorb a failure; carries the category and unit for the manifest."""

    def __init__(self, category: str, unit: str | None, message: str, stage: str | None = None):
        self.category, self.unit, self.stage = category, unit, stage
        super().__init__(message)


class CoverageExtraction(StrictModel):
    accepted: int = Field(default=0, ge=0)
    rejected_by_category: dict[str, int] = Field(default_factory=dict)
    documents_without_graph: list[str] = Field(default_factory=list)


class StageStatus(StrictModel):
    status: Literal["complete", "incomplete"] = "complete"
    processed: int = Field(default=0, ge=0)  # units (chunks, communities) the stage completed
    remaining: int = Field(default=0, ge=0)  # units the stage never reached
    reason: str | None = None
    first_unprocessed: str | None = None
    documents_not_processed: list[str] = Field(default_factory=list)  # documents the stage never reached


class Coverage(StrictModel):
    policy: PolicyName = "strict"
    extraction: CoverageExtraction = Field(default_factory=CoverageExtraction)
    stages: dict[str, StageStatus] = Field(default_factory=dict)

    @property
    def incomplete(self) -> bool:
        return any(s.status == "incomplete" for s in self.stages.values())

    def to_manifest(self) -> dict[str, Any]:
        return {**self.model_dump(mode="json"), "incomplete": self.incomplete}


class ExtractionPolicy:
    def __init__(self, name: PolicyName):
        self.name: PolicyName = name
        self.coverage = Coverage(policy=name)
        self.rejected: dict[str, str] = {}  # document id -> category
        self.not_processed: dict[str, str] = {}  # document id -> stage that stopped before reaching it
        self.rejected_reports: dict[str, str] = {}  # community id -> category

    # --- units --------------------------------------------------------------------------------

    def reject_document(self, document_id: str, category: str, unit: str, message: str) -> None:
        """A chunk of ``document_id`` failed: quarantine the whole document or abort."""
        if self.name == "strict" or category in STAGE_CATEGORIES:
            raise PolicyViolation(category, unit, message, stage="extraction")
        self.rejected[document_id] = category
        counts = self.coverage.extraction.rejected_by_category
        counts[category] = counts.get(category, 0) + 1
        if document_id not in self.coverage.extraction.documents_without_graph:
            self.coverage.extraction.documents_without_graph.append(document_id)

    def accept_document(self, document_id: str) -> None:
        if document_id not in self.rejected:
            self.coverage.extraction.accepted += 1

    def reject_report(self, community_id: str, category: str, message: str) -> None:
        if self.name == "strict":
            raise PolicyViolation(category, community_id, message, stage="community_reports")
        self.rejected_reports[community_id] = category
        counts = self.coverage.extraction.rejected_by_category
        counts[category] = counts.get(category, 0) + 1

    # --- stages -------------------------------------------------------------------------------

    def stage_complete(self, stage: str, processed: int) -> None:
        self.coverage.stages[stage] = StageStatus(status="complete", processed=processed, remaining=0)

    def stage_failed(
        self,
        stage: str,
        category: str,
        message: str,
        processed: int,
        remaining: int,
        first_unprocessed: str | None,
        documents_not_processed: Sequence[str] = (),
    ) -> None:
        """A stage stopped. Documents it never reached are recorded explicitly: they are neither
        accepted nor rejected and every per-document surface says so."""
        if self.name != "partial":
            raise PolicyViolation(category, first_unprocessed, message, stage=stage)
        for document_id in documents_not_processed:
            self.not_processed.setdefault(document_id, stage)
        self.coverage.stages[stage] = StageStatus(
            status="incomplete",
            processed=processed,
            remaining=remaining,
            reason=f"{category}: {message}",
            first_unprocessed=first_unprocessed,
            documents_not_processed=list(documents_not_processed),
        )

    def document_status(self, document_id: str) -> str:
        """``accepted`` | ``rejected:<category>`` | ``not_processed:<stage>``; never a silent default."""
        category = self.rejected.get(document_id)
        if category is not None:
            return f"rejected:{category}"
        stage = self.not_processed.get(document_id)
        if stage is not None:
            return f"not_processed:{stage}"
        return "accepted"

    def manifest_status(self) -> str:
        return "partial" if self.coverage.incomplete else "complete"
