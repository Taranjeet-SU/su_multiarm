from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Document(StrictModel):
    id: str = Field(min_length=1, max_length=512)
    dataset: str = Field(default="default", min_length=1)
    source: str = "article"
    title: str = ""
    fields: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    url: str | None = None

    @field_validator("fields")
    @classmethod
    def distinct_title(cls, value):
        if "title" in value:
            raise ValueError("use Document.title, not fields.title")
        return value

    @field_validator("url")
    @classmethod
    def absolute_url(cls, value):
        if value:
            from urllib.parse import urlsplit

            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
                raise ValueError("url must be an absolute HTTP(S) URL without credentials, or null")
        return value or None


class Chunk(StrictModel):
    id: str
    document_id: str
    field: str
    start: int
    end: int
    text: str


class Entity(StrictModel):
    name: str
    type: str
    quote: str


class Relation(StrictModel):
    subject: str
    predicate: str
    object: str
    quote: str
    subject_type: str | None = None
    object_type: str | None = None


class Extraction(StrictModel):
    """Model wire format: what an LLM extractor returns. Quotes here are unverified model text."""

    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)


class Span(StrictModel):
    """An exact anchor into a chunk: ``text == chunk.text[start:end]`` always holds."""

    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1)
    match_level: Literal["exact", "normalised", "relocated"]

    @model_validator(mode="after")
    def consistent(self):
        if self.end <= self.start or len(self.text) != self.end - self.start:
            raise ValueError("span offsets do not match its text")
        return self

    def contains(self, other: Span) -> bool:
        return self.start <= other.start and other.end <= self.end


class GroundedEntity(StrictModel):
    name: str = Field(min_length=1)
    type: str = Field(min_length=1)
    span: Span


class GroundedRelation(StrictModel):
    subject: str
    subject_type: str
    predicate: str = Field(min_length=1)
    object: str
    object_type: str
    span: Span
    subject_span: Span
    object_span: Span
    # ``fact`` requires predicate support; everything else is an explicit co-occurrence edge.
    edge_kind: Literal["fact", "co_occurrence"]
    support: Literal["trigger_in_span", "unchecked"]

    @model_validator(mode="after")
    def invariants(self):
        if self.edge_kind == "fact" and self.support == "unchecked":
            raise ValueError("a fact relation requires predicate support")
        if not (self.span.contains(self.subject_span) and self.span.contains(self.object_span)):
            raise ValueError("relation endpoints must lie inside the relation span")
        return self


class GroundedExtraction(StrictModel):
    """Extractor output: every quote is a verified source span."""

    entities: list[GroundedEntity] = Field(default_factory=list)
    relations: list[GroundedRelation] = Field(default_factory=list)


class PassageReference(StrictModel):
    document_id: str
    chunk_id: str
    quote: str = Field(min_length=1)


class CommunityClaim(StrictModel):
    text: str = Field(min_length=1)
    evidence: list[PassageReference] = Field(min_length=1)


class CommunityReport(StrictModel):
    claims: list[CommunityClaim] = Field(default_factory=list)


class Aggregate(StrictModel):
    operation: Literal["count", "group_count", "first_seen"] = "count"
    field: str | None = None


class SearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=16000)
    filters: dict[str, str | int | float | bool] = Field(default_factory=dict)
    exclude_ids: list[str] = Field(default_factory=list)
    record_context: dict[str, str] = Field(default_factory=dict)
    aggregate: Aggregate | None = None
    # Per-request overrides, each checked against the ``request.pipelines`` / ``request.modes``
    # allowlists of the configuration (the default pipeline and its decision kind are always allowed).
    pipeline: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    mode: Literal["rules", "classifier", "llm", "cascade"] | None = None

    @field_validator("record_context")
    @classmethod
    def context_limits(cls, value):
        """Wire-level bound only; the accepted keys and tighter limits come from ``request:`` in the
        configuration (a deployment declares its own context keys; nothing is hardcoded here)."""
        import json

        if len(json.dumps(value).encode()) > 1_000_000 or any(len(v) > 100_000 for v in value.values()):
            raise ValueError("record_context exceeds the wire limit")
        return value


class Hit(StrictModel):
    document_id: str
    title: str
    source: str
    url: str | None
    text: str
    chunk_ids: list[str] = Field(default_factory=list)
    score: float
    arms: list[str]
    provenance: list[dict[str, Any]] = Field(default_factory=list)


class SearchResult(StrictModel):
    query: str
    generation: str
    hits: list[Hit]
    arm_hits: dict[str, list[Hit]]
    aggregates: list[dict[str, Any]] = Field(default_factory=list)
    unsupported_constraints: list[UnsupportedConstraint] = Field(default_factory=list)  # from the decision (additive)
    trace: dict[str, Any] = Field(default_factory=dict)


UnsupportedReason = Literal[
    "value_not_observed",
    "type_mismatch",
    "unparseable_bound",
    "timezone_missing",
    "field_not_nullable",
    "field_not_filterable",
    "measure_unsupported",
    "not_supported_by_backend",
    "aggregate_intent_without_scope",
]


class UnsupportedConstraint(StrictModel):
    """A constraint the request asked for that no exact scope expressed (plan §3.7.1/§3.9a)."""

    constraint_text: str  # query fragment: masked in trace exports
    reason: UnsupportedReason
    suggestion: str | None = None  # also masked


ClaimKind = Literal["narrative", "count", "exhaustive_list", "absence"]
ContractRule = Literal["unknown_evidence", "claim_without_exact_scope", "aggregate_intent_without_scope", "factual_without_citations"]


class Claim(StrictModel):
    text: str = Field(min_length=1)
    kind: ClaimKind = "narrative"
    evidence_ids: list[str] = Field(default_factory=list)


class Answer(StrictModel):
    """Generator output (plan §3.9a). No ``unsupported_constraints`` field exists here on purpose:
    the engine populates it from the decision and the scope results, never the model."""

    answer: str
    claims: list[Claim] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    insufficient_evidence: bool = False
    short_answer: str | None = None


class Coverage(StrictModel):
    """What the pinned generation covers, from its manifest under ``quarantine``/``partial``."""

    policy: Literal["strict", "quarantine", "partial"] = "strict"
    documents_total: int = Field(ge=0)
    documents_covered: int = Field(ge=0)
    note: str | None = None
    incomplete_stages: list[str] = Field(default_factory=list)
    rejected_by_category: dict[str, int] = Field(default_factory=dict)


class Violation(StrictModel):
    rule: ContractRule
    detail: str


class AnswerResult(StrictModel):
    """v1 fields kept; ``unsupported_constraints``, ``coverage``, ``violations`` and ``contract`` are
    additive (plan §5)."""

    answer: Answer
    retrieval: SearchResult
    trace: dict[str, Any]
    unsupported_constraints: list[UnsupportedConstraint] = Field(default_factory=list)  # engine-injected only
    coverage: Coverage | None = None
    violations: list[Violation] = Field(default_factory=list)
    contract: Literal["passed", "abstained"] = "passed"
