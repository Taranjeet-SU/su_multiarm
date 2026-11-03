"""Shared typed-scope semantics for adapters: predicate evaluation, comparisons and measures.

Both the in-memory fake and the sqlite adapter use these functions so that ``ScopeResult`` values
(denominator, exactness, grouping, missing-or-invalid counts) are identical for identical data. SQL
adapters push predicates down where they can and compute measures over the matched rows here.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
from typing import Any

from ..core.types import Document, GroupRow, MetadataSchema, Predicate, ScopeResult, ScopeSpec, StructuredRecord


def document_values(document: Document) -> dict[str, Any]:
    """Flat values a document exposes to typed scope: metadata plus id/source/dataset/kind (never overridable by metadata)."""
    return {**document.metadata, "id": document.id, "source": document.source, "dataset": document.dataset, "kind": document.kind}


def parse_datetime(value: Any) -> datetime | None:
    """Timezone-aware ISO 8601 only; naive stamps are never guessed."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00") if value.endswith("Z") else value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def instant(value: Any) -> datetime | None:
    """A stored temporal value as a UTC instant.

    Aware stamps convert to UTC; a **naive** stamp or a date-only string is read as UTC. That is the
    storage-level rule for both adapters: a naive value can only reach storage through an
    ``assume_utc`` field (``require_offset`` rejects it at ingestion), and a date is its midnight UTC.
    """
    if not isinstance(value, str) or not value or value[0].isalpha():
        return None
    text = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(text), datetime.min.time())
        except ValueError:
            return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def temporal_key(value: Any, spec: Any = None) -> datetime | None:
    """Comparison key of a temporal value under a field spec: instants for ``datetime`` fields and
    for document scope (no spec); **calendar dates** (midnight UTC) for ``date`` fields, so a
    datetime bound against a date field is truncated to its UTC date."""
    moment = instant(value)
    if moment is None:
        return None
    if spec is not None and getattr(spec, "type", None) == "date":
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return moment


def compare(value: Any, bound: Any, spec: Any = None) -> int | None:
    """Three-way comparison of scalars of compatible types; None when incomparable. Temporal strings
    compare as instants (``temporal_key``), never as text."""
    if value is None or bound is None or isinstance(value, bool) or isinstance(bound, bool):
        return None
    if isinstance(value, (int, float)) and isinstance(bound, (int, float)):
        return (value > bound) - (value < bound)
    if isinstance(value, str) and isinstance(bound, str):
        left, right = temporal_key(value, spec), temporal_key(bound, spec)
        if left is not None and right is not None:
            return (left > right) - (left < right)
        if left is not None or right is not None:
            return None  # a temporal value against a non-temporal bound is incomparable
        return (value > bound) - (value < bound)
    return None


def truncate(moment: datetime, granularity: str) -> datetime:
    moment = moment.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "month":
        return moment.replace(day=1)
    if granularity == "year":
        return moment.replace(month=1, day=1)
    return moment


def _temporal_equal(value: Any, candidate: Any, spec: Any) -> bool | None:
    """Instant equality for temporal fields (None when the pair is not temporal)."""
    if spec is None or getattr(spec, "type", None) not in {"datetime", "date"}:
        return None
    left, right = temporal_key(value, spec), temporal_key(candidate, spec)
    if left is None or right is None:
        return None
    return left == right


def matches(values: dict[str, Any], predicate: Predicate, casefold: bool = False, spec: Any = None) -> bool:
    """Evaluate one typed predicate against a flat value mapping (``spec``: the field's ``FieldSpec``
    when known; temporal fields then compare as instants, date fields as calendar dates)."""
    present = predicate.field in values
    value = values.get(predicate.field)
    if predicate.op == "missing":
        result = (not present) or value is None
    elif predicate.op == "eq":
        left, right = value, predicate.value
        temporal = _temporal_equal(left, right, spec)
        if temporal is not None:
            result = present and temporal
        else:
            if casefold and isinstance(left, str) and isinstance(right, str):
                left, right = left.casefold(), right.casefold()
            result = present and left == right
    elif predicate.op == "in":
        candidates = list(predicate.value or [])
        if any(_temporal_equal(value, c, spec) for c in candidates):
            result = present
        elif casefold and isinstance(value, str):
            result = present and value.casefold() in {c.casefold() for c in candidates if isinstance(c, str)}
        else:
            result = present and value in candidates
    else:  # range / date_interval
        result = value is not None
        if result and predicate.lower is not None:
            cmp = compare(value, predicate.lower, spec)
            result = cmp is not None and (cmp >= 0 if predicate.inclusive else cmp > 0)
        if result and predicate.upper is not None:
            cmp = compare(value, predicate.upper, spec)
            inclusive_upper = predicate.inclusive and predicate.op == "range"  # date_interval is half-open
            result = cmp is not None and (cmp <= 0 if inclusive_upper else cmp < 0)
    return (not result) if predicate.negate else result


# Record values that identify the canonical record rather than metadata (``source`` is engine-injected
# for every document); they bypass the schema while it does not declare them.
RECORD_KEYS = frozenset({"id", "dataset", "kind", "source"})


def unsupported_reason(schema: MetadataSchema, predicate: Predicate) -> str | None:
    """Why a scope predicate cannot be applied; schema gating happens here, not in the caller.

    Undeclared or non-filterable fields, ``missing`` on a non-nullable field and unparseable or
    naive datetime bounds are reported as ``UnsupportedConstraint`` reasons and never applied.
    """
    spec = schema.field(predicate.field)
    if spec is None or not spec.filterable:
        return "field_not_filterable"
    if predicate.op == "missing" and not spec.nullable:
        return "field_not_nullable"
    if predicate.op in {"range", "date_interval"} and spec.type in {"datetime", "date"}:
        for bound in (predicate.lower, predicate.upper):
            if bound is None:
                continue
            if not isinstance(bound, str) or instant(bound) is None:
                return "unparseable_bound"
            try:
                parsed = datetime.fromisoformat(bound.replace("Z", "+00:00") if bound.endswith("Z") else bound)
            except ValueError:
                parsed = None  # a date-only bound: fine for date fields and under assume_utc
            naive = parsed is None or parsed.tzinfo is None
            if spec.type == "datetime" and naive and spec.timezone_policy == "require_offset":
                return "timezone_missing"
    return None


def unsupported_interval(predicate: Predicate) -> bool:
    """A ``date_interval`` with a bound that is not a timezone-aware stamp cannot be applied."""
    bounds = [b for b in (predicate.lower, predicate.upper) if b is not None]
    return predicate.op == "date_interval" and any(parse_datetime(b) is None for b in bounds)


def record_violations(schema: MetadataSchema, record: StructuredRecord) -> list:
    """Schema violations of a record's metadata values. Record identity keys (``id``, ``dataset``,
    ``kind``) bypass the schema only while it does not declare them; a declared ``kind`` field is
    validated like any other (an operator who declares it means it)."""
    values = {k: v for k, v in record.values.items() if k not in RECORD_KEYS or schema.field(k) is not None}
    return schema.validate_metadata(values)


def measure_of(scope: ScopeSpec, rows: list[StructuredRecord], population: int, schema: MetadataSchema) -> tuple[Any, int]:
    """``(value, missing_or_invalid)`` of the scope's measure over ``rows``."""
    measure = scope.measure
    if measure is None or measure.kind == "count":
        return len(rows), 0
    if measure.kind == "count_distinct":
        unit = measure.unit or schema.equivalence_field or "id"
        return len({r.values.get(unit, r.id) for r in rows}), 0
    if measure.kind == "share":
        return (len(rows) / population) if population else 0.0, 0
    field = measure.field or ""
    spec = schema.field(field)
    if measure.kind in {"first_seen", "last_seen"}:
        stamped = [(temporal_key(r.values.get(field), spec), r.id) for r in rows]
        valid = [(t, i) for t, i in stamped if t is not None]
        if not valid:
            return None, len(stamped)
        chosen = min(valid) if measure.kind == "first_seen" else max(valid)
        return {"value": chosen[0].isoformat(), "record_id": chosen[1]}, len(stamped) - len(valid)
    if measure.kind == "bucket":
        counts: Counter[str] = Counter()
        invalid = 0
        for r in rows:
            moment = temporal_key(r.values.get(field), spec)
            if moment is None:
                invalid += 1
            else:
                counts[truncate(moment, measure.granularity or "day").isoformat()] += 1
        return dict(sorted(counts.items())), invalid
    numbers = [r.values.get(field) for r in rows]
    valid_numbers = [n for n in numbers if isinstance(n, (int, float)) and not isinstance(n, bool)]
    invalid = len(numbers) - len(valid_numbers)
    if not valid_numbers:
        return None, invalid
    if measure.kind == "min":
        return min(valid_numbers), invalid
    if measure.kind == "max":
        return max(valid_numbers), invalid
    if measure.kind == "sum":
        return sum(valid_numbers), invalid
    return sum(valid_numbers) / len(valid_numbers), invalid


# Upper bound on the records one typed scope may match on the request path (plan §2.1 principle 7).
# The Lucene adapters use the same default (``_lucene_common.MAX_MATCHED_DEFAULT``). Adapters fetch at
# most ``cap + 1`` rows and refuse above the cap with a typed ``BackendError``; never a silent cut.
MAX_MATCHED_DEFAULT = 10_000


def matched_cap(backend: Any) -> int:
    """The adapter's ``max_matched`` (an attribute tests or embedders may lower), else the default."""
    return int(getattr(backend, "max_matched", MAX_MATCHED_DEFAULT))


def check_matched(adapter: str, rows: list, cap: int) -> None:
    """Refuse a scope that matched more than ``cap`` records (``rows`` holds at most ``cap + 1``)."""
    from ..core.errors import BackendError

    if len(rows) > cap:
        raise BackendError(adapter, "execute", f"scope matches more than {cap} records, above max_matched {cap}; narrow the scope")


def scope_result(
    scope: ScopeSpec,
    matched: list[StructuredRecord],
    population: int,
    schema: MetadataSchema,
    applied: list[Predicate],
    unsupported: list,
) -> ScopeResult:
    """Assemble the ``ScopeResult`` for the matched rows of a scope. It is ``exact`` only when every
    predicate was applied: a dropped (``unsupported``) predicate widens the scope, so the result is
    ``bounded`` (an upper bound over the requested population), never claimed as exact."""
    groups: list[GroupRow] = []
    missing = 0
    if scope.group_by:
        buckets: dict[tuple, list[StructuredRecord]] = {}
        for row in matched:
            buckets.setdefault(tuple(row.values.get(f) for f in scope.group_by), []).append(row)
        for key, rows in buckets.items():
            group_value, group_missing = measure_of(scope, rows, population, schema)
            missing += group_missing
            groups.append(GroupRow(key=dict(zip(scope.group_by, key)), value=group_value, count=len(rows)))
        groups.sort(key=lambda g: (-g.count, repr(sorted(g.key.items()))))
        if scope.sort and scope.sort.field in scope.group_by:
            groups.sort(key=lambda g: repr(g.key.get(scope.sort.field)), reverse=scope.sort.descending)
        if scope.top_n:
            groups = groups[: scope.top_n]
        value: Any = {"|".join(str(k) for k in g.key.values()): g.value for g in groups}
    else:
        value, missing = measure_of(scope, matched, population, schema)
    return ScopeResult(
        exactness="bounded" if unsupported else "exact",
        denominator=population,
        value=value,
        groups=groups,
        applied=applied,
        unsupported=unsupported,
        zero_match=bool(scope.predicates) and not matched,
        matched_ids=sorted(r.id for r in matched),
        missing_or_invalid=missing,
    )


MAX_HOPS = 3


def validated_hops(adapter: str, hops: Any) -> int:
    """``GraphStore.traverse`` hop bound: an integer in ``0..MAX_HOPS`` (``TraversalSpec`` validates it
    too; adapters re-check because a spec built with ``model_construct`` skips validation). Never coerced."""
    from ..core.errors import BackendError

    if isinstance(hops, bool) or not isinstance(hops, int) or not 0 <= hops <= MAX_HOPS:
        raise BackendError(adapter, "traverse", f"hops must be an integer in 0..{MAX_HOPS}, got {hops!r}")
    return hops
