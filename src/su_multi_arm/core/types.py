"""Canonical data types (plan §3.1, §3.1a). Every quote is a verbatim source ``Span``.

These are the v2 kernel types. The v1 wire/engine models stay in ``su_multi_arm.models`` until the
arms and the write path are ported; ``Span.from_chunk_span`` bridges a chunk-local v1 span to a
field-anchored v2 span. Nothing here normalises text: that is ``grounding.py``'s job.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import Field, field_validator, model_validator

from ..models import Span as ChunkSpan
from ..models import StrictModel, UnsupportedConstraint, UnsupportedReason  # noqa: F401 (re-exported)

JsonScalar = str | int | float | bool | None
MatchLevel = Literal["exact", "normalised", "relocated"]
EdgeKind = Literal["fact", "co_occurrence", "mention", "native_link", "derived"]
Support = Literal["trigger_in_span", "entailment_checked", "unchecked"]
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}")
HEX32 = re.compile(r"[0-9a-f]{32}")

T = TypeVar("T")


class Span(StrictModel):
    """Exact source anchor: ``text == source_field[start:end]`` always. The only legal quote carrier."""

    document_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    field: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1)
    match_level: MatchLevel

    @model_validator(mode="after")
    def consistent(self):
        if self.end <= self.start or len(self.text) != self.end - self.start:
            raise ValueError("span offsets do not match its text")
        return self

    def contains(self, other: Span) -> bool:
        """Offset containment inside the same chunk of the same document."""
        return (
            self.document_id == other.document_id
            and self.chunk_id == other.chunk_id
            and self.start <= other.start
            and other.end <= self.end
        )

    def verify(self, source_field: str) -> bool:
        return source_field[self.start : self.end] == self.text

    @classmethod
    def from_chunk_span(cls, chunk: Chunk, local: ChunkSpan) -> Span:
        """Anchor a chunk-local span (``models.Span``) to the chunk's field offsets."""
        if local.end > chunk.end - chunk.start or chunk.text[local.start : local.end] != local.text:
            raise ValueError("chunk-local span does not lie inside the chunk text")
        return cls(
            document_id=chunk.document_id,
            chunk_id=chunk.id,
            field=chunk.field,
            start=chunk.start + local.start,
            end=chunk.start + local.end,
            text=local.text,
            match_level=local.match_level,
        )


class Provenance(StrictModel):
    extractor: str = Field(min_length=1)
    model: str | None = None
    prompt_sha: str | None = None
    recipe_sha: str | None = None
    extracted_at: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class Document(StrictModel):
    """As the v1 document plus a neutral ``kind``. Metadata is validated against a ``MetadataSchema``."""

    id: str = Field(min_length=1, max_length=512)
    dataset: str = Field(default="default", min_length=1)
    source: str = "article"
    kind: str = Field(default="document", min_length=1)
    title: str = ""
    fields: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, JsonScalar] = Field(default_factory=dict)
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
    id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    field: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str
    recipe: str = Field(default="default", min_length=1)  # chunking policy id; embedding vs extraction may differ

    @model_validator(mode="after")
    def consistent(self):
        if self.end - self.start != len(self.text):
            raise ValueError("chunk offsets do not match its text")
        return self


class Entity(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    type: str = Field(min_length=1)
    span: Span
    canonical_id: str | None = None
    aliases: list[str] = Field(default_factory=list)


class Occurrence(StrictModel):
    """One source occurrence of a deduplicated relation: where it was seen and by which extractor."""

    span: Span
    provenance: Provenance


class Relation(StrictModel):
    subject_id: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object_id: str = Field(min_length=1)
    span: Span
    edge_kind: EdgeKind
    support: Support
    status: str | None = None  # ontology-defined (e.g. proposed/verified) when a contract is loaded
    provenance: Provenance
    # Filled by ``extraction/dedupe.py``: every occurrence (incl. the one ``span`` anchors) when
    # identical relations from chunks that inherit the same text collapse into one edge.
    occurrences: list[Occurrence] = Field(default_factory=list)

    @model_validator(mode="after")
    def invariants(self):
        if self.support == "entailment_checked":
            raise ValueError("support=entailment_checked is reserved: no producer exists before Phase 5")
        if self.edge_kind == "fact" and self.support == "unchecked":
            raise ValueError("a fact relation requires predicate support")
        return self


class Event(StrictModel):
    type: str = Field(min_length=1)
    participants: list[tuple[str, str]] = Field(default_factory=list)  # (role, entity_id)
    time: str | None = None
    span: Span
    provenance: Provenance


class StructuredRecord(StrictModel):
    schema_id: str = Field(min_length=1)
    id: str = Field(min_length=1)
    values: dict[str, JsonScalar] = Field(default_factory=dict)
    spans: dict[str, Span] = Field(default_factory=dict)
    provenance: Provenance

    @model_validator(mode="after")
    def spans_reference_values(self):
        if set(self.spans) - set(self.values):
            raise ValueError("every span must anchor a value of the record")
        return self


class Membership(StrictModel):
    community_id: str = Field(min_length=1)
    member_id: str = Field(min_length=1)
    role: str | None = None
    basis: Literal["entity", "similarity", "declared"]


class ExtractionOutput(StrictModel):
    """All lists, all span-anchored. Relation endpoints must be entities of the same chunk."""

    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    records: list[StructuredRecord] = Field(default_factory=list)
    memberships: list[Membership] = Field(default_factory=list)

    @model_validator(mode="after")
    def endpoints_contained(self):
        entities = {}
        for entity in self.entities:
            if entity.id in entities:
                raise ValueError(f"duplicate entity id {entity.id}")
            entities[entity.id] = entity
        for relation in self.relations:
            if relation.edge_kind not in {"fact", "co_occurrence"}:
                continue
            ends = [entities.get(relation.subject_id), entities.get(relation.object_id)]
            if any(e is None for e in ends):
                raise ValueError(f"relation {relation.predicate}: endpoints must be typed entities of this output")
            for end in ends:
                if not relation.span.contains(end.span):
                    raise ValueError(
                        f"relation {relation.predicate}: span must contain both endpoint spans in the same chunk"
                    )
        for event in self.events:
            for _, entity_id in event.participants:
                if entity_id not in entities:
                    raise ValueError(f"event {event.type}: participant {entity_id} is not an extracted entity")
        return self


class Evidence(StrictModel):
    """What a generator may cite. Cached answers are never evidence (no such kind exists)."""

    id: str = Field(min_length=1)
    kind: Literal["document", "aggregate", "graph_path", "community_report"]
    document_ids: list[str] = Field(default_factory=list)
    spans: list[Span] = Field(default_factory=list)
    text: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class GenerationId(StrictModel):
    dataset: str = Field(min_length=1)
    generation: str = Field(pattern=r"^[0-9a-f]{32}$")
    index_format: int = Field(ge=1)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    def prefix(self) -> str:
        """Namespace prefix for cache keys and per-generation index/table names."""
        return f"{self.dataset}/{self.generation}/"

    def is_prefix_of(self, key: str) -> bool:
        return key.startswith(self.prefix())


# --- metadata schema (§3.1a) -------------------------------------------------------------------

FieldType = Literal["string", "integer", "number", "boolean", "datetime", "date", "enum", "id"]
ViolationCategory = Literal["unknown_field", "type", "null", "enum", "timezone", "format"]


class NamedState(StrictModel):
    """A declared name for a set of field values (``open`` = not ``done``/``closed``)."""

    name: str = Field(min_length=1, max_length=64)
    values: list[str] = Field(min_length=1, max_length=200)
    negate: bool = False  # the state is "none of ``values``" (unknown/null values included and reported)


class FieldSpec(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    type: FieldType
    nullable: bool = True
    enum_values: list[str] | None = None  # required when type == "enum"; closed set
    filterable: bool = True  # may appear in ScopeSpec.predicates
    listable: bool = False  # may be enumerated via values listing (ACL-scoped)
    groupable: bool = False
    sortable: bool = False
    max_catalog_cardinality: int = Field(default=200, ge=0)  # observed values offered to a planner only under this
    time_basis: str | None = None  # datetime/date only: label such as "created" / "resolved"
    timezone_policy: Literal["require_offset", "assume_utc"] = "require_offset"
    casefold_match: bool = False  # eq/in matching for string/id fields
    # ``id`` fields only: regex an identifier of this field matches inside free text. The exact_id
    # arm and the intent pre-pass use it to route lookups; it never widens a match (no fuzzy ids).
    pattern: str | None = Field(default=None, min_length=1, max_length=512)
    # Named value sets a question may use instead of a value ("open" = not one of the closed
    # values). Declared by the operator, never inferred; ``negate`` sets match records whose value
    # is unknown (null) too, and every result reports how many of those it counted.
    states: list[NamedState] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent(self):
        if self.states:
            if self.type not in {"string", "enum", "id"}:
                raise ValueError(f"field {self.name}: states are only valid for string/enum/id fields")
            names = [s.name.casefold() for s in self.states]
            if len(set(names)) != len(names):
                raise ValueError(f"field {self.name}: state names must be unique")
            if self.type == "enum":
                unknown = sorted({v for s in self.states for v in s.values} - set(self.enum_values or []))
                if unknown:
                    raise ValueError(f"field {self.name}: state values {unknown} are not enum_values")
        if self.pattern is not None:
            if self.type != "id":
                raise ValueError(f"field {self.name}: pattern is only valid for type id")
            try:
                compiled = re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"field {self.name}: pattern does not compile: {exc}") from None
            if compiled.match(""):
                raise ValueError(f"field {self.name}: pattern must not match the empty string")
        if self.type == "enum":
            if not self.enum_values:
                raise ValueError(f"field {self.name}: enum requires a closed, nonempty enum_values list")
            if len(set(self.enum_values)) != len(self.enum_values):
                raise ValueError(f"field {self.name}: enum_values must be unique")
        elif self.enum_values is not None:
            raise ValueError(f"field {self.name}: enum_values is only valid for type enum")
        if self.time_basis is not None and self.type not in {"datetime", "date"}:
            raise ValueError(f"field {self.name}: time_basis is only valid for datetime/date fields")
        if self.casefold_match and self.type not in {"string", "id", "enum"}:
            raise ValueError(f"field {self.name}: casefold_match is only valid for string/id/enum fields")
        return self


class MetadataViolation(StrictModel):
    field: str
    category: ViolationCategory
    message: str


class MetadataSchema(StrictModel):
    fields: list[FieldSpec] = Field(default_factory=list)
    canonical_unit: str = Field(default="document", min_length=1)  # population counted by default
    equivalence_field: str | None = None  # optional cross-record identity for count_distinct
    unknown_fields: Literal["allow", "reject"] = "allow"  # undeclared metadata keys are untyped, never filterable
    # IANA zone in which calendar expressions ("this month", "October", "last 7 days") and calendar
    # buckets are read; relative expressions also need the request anchor (``as_of``, else now).
    # Undeclared (None): buckets and date/assume_utc fields use UTC, but a calendar expression on a
    # ``require_offset`` datetime field is refused (``timezone_missing``), never read in a guessed zone.
    timezone: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def coherent(self):
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names):
            raise ValueError("metadata field names must be unique")
        if self.timezone is not None:
            try:
                from zoneinfo import ZoneInfo

                ZoneInfo(self.timezone)
            except (ValueError, KeyError, OSError):
                raise ValueError(f"timezone {self.timezone!r} is not a known IANA zone") from None
        if self.equivalence_field and self.equivalence_field not in names:
            raise ValueError("equivalence_field must be a declared field")
        return self

    def field(self, name: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.name == name), None)

    def filterable(self) -> list[str]:
        return [f.name for f in self.fields if f.filterable]

    def listable(self) -> list[str]:
        return [f.name for f in self.fields if f.listable]

    def groupable(self) -> list[str]:
        return [f.name for f in self.fields if f.groupable]

    def sortable(self) -> list[str]:
        return [f.name for f in self.fields if f.sortable]

    def validate_value(self, spec: FieldSpec, value: JsonScalar) -> MetadataViolation | None:
        name = spec.name
        if value is None:
            return None if spec.nullable else MetadataViolation(field=name, category="null", message="not nullable")
        if isinstance(value, bool) and spec.type != "boolean":
            return MetadataViolation(field=name, category="type", message=f"expected {spec.type}, got boolean")
        if spec.type in {"string", "id", "datetime", "date", "enum"} and not isinstance(value, str):
            return MetadataViolation(field=name, category="type", message=f"expected {spec.type} text")
        if spec.type == "integer" and not isinstance(value, int):
            return MetadataViolation(field=name, category="type", message="expected integer")
        if spec.type == "number" and not isinstance(value, (int, float)):
            return MetadataViolation(field=name, category="type", message="expected number")
        if spec.type == "boolean" and not isinstance(value, bool):
            return MetadataViolation(field=name, category="type", message="expected boolean")
        if spec.type == "enum" and value not in spec.enum_values:
            return MetadataViolation(field=name, category="enum", message="value is not in enum_values")
        if spec.type == "datetime":
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                return MetadataViolation(field=name, category="format", message="not an ISO 8601 datetime")
            if parsed.tzinfo is None and spec.timezone_policy == "require_offset":
                return MetadataViolation(field=name, category="timezone", message="datetime needs a UTC offset")
        if spec.type == "date":
            try:
                date.fromisoformat(value)
            except ValueError:
                return MetadataViolation(field=name, category="format", message="not an ISO 8601 date")
        return None

    def validate_metadata(self, metadata: dict[str, JsonScalar]) -> list[MetadataViolation]:
        violations = []
        declared = {f.name: f for f in self.fields}
        for key, value in metadata.items():
            spec = declared.get(key)
            if spec is None:
                if self.unknown_fields == "reject":
                    violations.append(MetadataViolation(field=key, category="unknown_field", message="undeclared"))
                continue
            violation = self.validate_value(spec, value)
            if violation:
                violations.append(violation)
        for spec in self.fields:
            if spec.name not in metadata and not spec.nullable:
                violations.append(MetadataViolation(field=spec.name, category="null", message="missing, not nullable"))
        return violations

    def validate_document(self, document: Document) -> list[MetadataViolation]:
        return self.validate_metadata(document.metadata)


# --- typed scope, paging and backend result types (§3.5, §3.7 subset frozen with the contracts) ---

PredicateOp = Literal["eq", "in", "range", "date_interval", "missing"]


class Predicate(StrictModel):
    field: str = Field(min_length=1)
    op: PredicateOp
    value: JsonScalar | list[JsonScalar] = None  # eq / in
    lower: JsonScalar = None  # range / date_interval
    upper: JsonScalar = None
    inclusive: bool = True
    granularity: Literal["day", "month", "year"] | None = None  # date_interval
    negate: bool = False  # the plan's ``not``

    @model_validator(mode="after")
    def coherent(self):
        if self.op == "in" and not isinstance(self.value, list):
            raise ValueError("in requires a list value")
        if self.op == "eq" and isinstance(self.value, list):
            raise ValueError("eq requires a scalar value")
        if self.op in {"range", "date_interval"} and self.lower is None and self.upper is None:
            raise ValueError(f"{self.op} requires lower and/or upper")
        if self.op == "date_interval" and self.granularity is None:
            raise ValueError("date_interval requires a granularity")
        return self


ScopePredicates = list[Predicate]


class Scope(StrictModel):
    """Eligibility pushed down to a backend: predicates (incl. ACL constraints) plus ID allow/deny lists."""

    predicates: list[Predicate] = Field(default_factory=list)
    document_ids: list[str] | None = None
    exclude_ids: list[str] = Field(default_factory=list)


class Sort(StrictModel):
    field: str = Field(min_length=1)
    descending: bool = False


MeasureKind = Literal[
    "count", "count_distinct", "min", "max", "avg", "sum", "first_seen", "last_seen", "share", "bucket"
]


class Measure(StrictModel):
    kind: MeasureKind = "count"
    field: str | None = None
    unit: str | None = None  # count_distinct(unit)
    granularity: Literal["day", "month", "year"] | None = None  # bucket

    @model_validator(mode="after")
    def coherent(self):
        if self.kind in {"min", "max", "avg", "sum", "first_seen", "last_seen", "bucket"} and not self.field:
            raise ValueError(f"measure {self.kind} requires a field")
        if self.kind == "bucket" and not self.granularity:
            raise ValueError("bucket requires a granularity")
        if self.kind == "count_distinct" and self.field is not None:
            # ``count_distinct`` counts distinct values of ``unit``; a planner that names the field as
            # ``field`` meant that unit. Without this the field was ignored and the count fell back to
            # record ids, a silently wrong "exact" number.
            if self.unit is not None and self.unit != self.field:
                raise ValueError("count_distinct takes one key: field and unit differ")
            self.unit = self.field
        return self


class Period(StrictModel):
    """One period of a comparison: the half-open window ``[lower, upper)`` on the scope's
    ``time_basis`` field (ISO bounds; aware datetimes for ``datetime`` fields, dates for ``date``)."""

    label: str = Field(min_length=1, max_length=128)
    lower: str = Field(min_length=1, max_length=64)
    upper: str = Field(min_length=1, max_length=64)


class TextMatch(StrictModel):
    """A literal topic condition counted through the lexical index over the whole eligible corpus
    (never a ranked top-k). ``phrase``: the words contiguous; ``all_terms``: every word in one indexed
    chunk. ``role: numerator`` makes it the numerator of a share instead of a scope condition. The
    structured arm resolves it to record ids; a record store never evaluates text itself."""

    kind: Literal["phrase", "all_terms"] = "all_terms"
    text: str = Field(min_length=1, max_length=512)
    role: Literal["scope", "numerator"] = "scope"


Granularity = Literal["day", "month", "year"]


class ScopeSpec(StrictModel):
    predicates: list[Predicate] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    top_n: int | None = Field(default=None, ge=1)
    sort: Sort | None = None
    measure: Measure | None = None
    time_basis: str | None = None
    population: Literal["canonical_record", "chunk"] = "canonical_record"
    # --- query-compiler operations (2.0.0a3 parity; all optional, additive) -----------------------
    # Ranking of groups by their measure value (the group count for ``count``); ties broken by the
    # group key, ascending; ``nth`` keeps only the groups at that dense rank ("second most").
    order: Literal["desc", "asc"] | None = None
    nth: int | None = Field(default=None, ge=1, le=1000)
    granularity: Granularity | None = None  # calendar bucket of temporal ``group_by`` fields
    periods: list[Period] = Field(default_factory=list, max_length=12)  # period comparison on time_basis
    with_share: bool = False  # each group's share of the matched total
    numerator: list[Predicate] = Field(default_factory=list)  # share: the numerator condition
    text: TextMatch | None = None  # literal topic via the lexical index (resolved by the structured arm)
    limit: int | None = Field(default=None, ge=1, le=10000)  # list: records shown (``total`` is reported)

    @model_validator(mode="after")
    def coherent(self):
        if len(self.group_by) > 2:
            raise ValueError("group_by takes at most two fields (a cross-tab)")
        if len(set(self.group_by)) != len(self.group_by):
            raise ValueError("group_by fields must differ")
        if self.nth is not None and not self.group_by:
            raise ValueError("nth ranks groups: it needs group_by")
        if self.periods and not self.time_basis:
            raise ValueError("periods compare windows of the time_basis field: set time_basis")
        if self.periods and self.group_by:
            raise ValueError("a period comparison is its own grouping: drop group_by")
        labels = [p.label for p in self.periods]
        if len(set(labels)) != len(labels):
            raise ValueError("period labels must be unique")
        if self.numerator and (self.measure is None or self.measure.kind != "share"):
            raise ValueError("numerator is only valid for a share measure")
        if self.text is not None and self.text.role == "numerator" and (self.measure is None or self.measure.kind != "share"):
            raise ValueError("a numerator text match is only valid for a share measure")
        return self


class GroupRow(StrictModel):
    key: dict[str, JsonScalar]
    value: JsonScalar | dict[str, Any]
    count: int = Field(ge=0)
    rank: int | None = Field(default=None, ge=1)  # dense rank when the scope ranks groups
    share: float | None = Field(default=None, ge=0, le=1)  # count / matched total (``with_share``)


class ListedRecord(StrictModel):
    id: str
    sort_value: JsonScalar = None


class ScopeResult(StrictModel):
    exactness: Literal["exact", "approximate", "bounded"]
    denominator: int = Field(ge=0)
    value: JsonScalar | dict[str, Any] | list[Any] = None
    groups: list[GroupRow] = Field(default_factory=list)
    applied: list[Predicate] = Field(default_factory=list)
    unsupported: list[UnsupportedConstraint] = Field(default_factory=list)
    zero_match: bool = False
    matched_ids: list[str] = Field(default_factory=list)
    missing_or_invalid: int = Field(default=0, ge=0)
    # --- query-compiler operations (2.0.0a3) ------------------------------------------------------
    total: int | None = Field(default=None, ge=0)  # records matched by the scope (list: "showing N of total")
    listed: list[ListedRecord] = Field(default_factory=list)  # ``limit`` records in ``sort`` order
    ties: list[GroupRow] = Field(default_factory=list)  # groups cut by top_n/nth that tie a kept group
    unknown: dict[str, int] = Field(default_factory=dict)  # field -> matched records with a null value under a negated predicate
    numerator: int | None = Field(default=None, ge=0)  # share: records matching the numerator
    basis: list[str] = Field(default_factory=list)  # human-readable labels: time basis, windows, text match, count unit
    # Native aggregation (Lucene ``records.aggregation: native``): ``matched_ids`` holds the first
    # ``matched_ids_cap`` matched ids in id order and this flag says more records matched (``total``
    # is always the full count). The client path never truncates (it refuses above ``max_matched``).
    matched_ids_truncated: bool = False


class Page(StrictModel):
    cursor: str | None = None
    size: int = Field(default=50, ge=1, le=10000)  # same cap as pipeline.arms.*.candidates


class PageResult(StrictModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None


class LexicalQuery(StrictModel):
    kind: Literal["terms", "phrase", "boolean"] = "terms"
    text: str = Field(min_length=1)
    fields: list[str] | None = None
    must: list[str] = Field(default_factory=list)  # boolean only
    must_not: list[str] = Field(default_factory=list)


class ScoredChunk(StrictModel):
    chunk_id: str
    document_id: str
    score: float
    highlights: list[str] = Field(default_factory=list)


class TraversalSpec(StrictModel):
    seed_entity_ids: list[str] = Field(min_length=1)
    hops: int = Field(default=1, ge=0, le=3)
    predicates: list[str] = Field(default_factory=list)  # empty = any predicate
    direction: Literal["out", "in", "any"] = "any"
    edge_kinds: list[EdgeKind] = Field(default_factory=lambda: ["fact"])
    limit: int = Field(default=200, ge=1, le=10000)


class PathEdge(StrictModel):
    subject_id: str
    predicate: str
    object_id: str
    edge_kind: EdgeKind
    support: Support
    span: Span
    provenance: Provenance
    occurrences: list[Occurrence] = Field(default_factory=list)  # every source occurrence of a deduplicated edge


class Path(StrictModel):
    nodes: list[str] = Field(min_length=1)
    edges: list[PathEdge] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent(self):
        if len(self.nodes) != len(self.edges) + 1:
            raise ValueError("a path has one more node than edges")
        return self


class CacheEntry(StrictModel):
    key: str
    value: Any
    expires_at: float


JobStatus = Literal["pending", "running", "succeeded", "failed", "quarantined", "partial", "cancelled"]


class LedgerEntry(StrictModel):
    sequence: int = Field(ge=0)
    kind: str = Field(min_length=1)
    at: str
    payload: dict[str, Any] = Field(default_factory=dict)


class RawReply(StrictModel):
    key: str
    raw_reply: str
    valid: bool


class Turn(StrictModel):
    """Episodic memory entry: hashes and IDs, never passages (unless memory.store_text is on)."""

    query_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    shown_ids: list[str] = Field(default_factory=list)
    answer_id: str | None = None
    trace_id: str
    generation: str = Field(pattern=r"^[0-9a-f]{32}$")
    query_text: str | None = None


class SessionReservation(StrictModel):
    """Session budget held by one in-flight turn (``budgets.session``): the turn's clipped request
    limits per kind, taken atomically at turn start and replaced by the actual usage at turn end. A
    reservation whose turn never finished stops counting at ``expires_at``."""

    amounts: dict[str, float] = Field(default_factory=dict)
    expires_at: float


class SessionRecord(StrictModel):
    id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    dataset: str = Field(min_length=1)
    created_at: float
    last_seen_at: float
    generation: str | None = None
    turns: list[Turn] = Field(default_factory=list)
    closed: bool = False
    # Cumulative usage of every turn (``Budget`` kinds: calls, input/output tokens, usd) for the
    # per-session budget (``budgets.session``).
    usage: dict[str, float] = Field(default_factory=dict)
    # In-flight turns' session-budget holds, keyed by a random per-turn key.
    reservations: dict[str, SessionReservation] = Field(default_factory=dict)
    # Optimistic-concurrency version: every store write bumps it; ``SessionStore.touch`` with
    # ``expected_version`` is a compare-and-set (``SessionConflict`` when another writer won).
    version: int = Field(default=0, ge=0)
