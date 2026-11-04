"""Shared typed-scope semantics for adapters: predicate evaluation, comparisons and measures.

Both the in-memory fake and the sqlite adapter use these functions so that ``ScopeResult`` values
(denominator, exactness, grouping, missing-or-invalid counts) are identical for identical data. SQL
adapters push predicates down where they can and compute measures over the matched rows here.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from ..core.types import (
    Document,
    GroupRow,
    ListedRecord,
    MetadataSchema,
    Predicate,
    ScopeResult,
    ScopeSpec,
    StructuredRecord,
    UnsupportedConstraint,
)


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


def truncate(moment: datetime, granularity: str, zone: tzinfo = timezone.utc) -> datetime:
    """Start of the calendar day/month/year containing ``moment`` in ``zone`` (UTC by default)."""
    moment = moment.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
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
    if spec is None and predicate.field in RECORD_KEYS and predicate.op in {"eq", "in"}:
        return None  # record identity (``id``/``source``/``dataset``/``kind``): equality only
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


def zone_of(schema: MetadataSchema) -> tzinfo:
    """The schema's declared calendar zone (``MetadataSchema.timezone``)."""
    name = getattr(schema, "timezone", "UTC") or "UTC"
    return timezone.utc if name == "UTC" else ZoneInfo(name)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def measure_of(scope: ScopeSpec, rows: list[StructuredRecord], population: int, schema: MetadataSchema) -> tuple[Any, int]:
    """``(value, missing_or_invalid)`` of the scope's measure over ``rows``.

    ``count_distinct`` counts distinct **non-null** values of its unit (rows without one are
    reported as missing), except for the record identity (``id`` / the canonical unit) and the
    schema's ``equivalence_field``: a record without an equivalence value is its own canonical unit.
    """
    measure = scope.measure
    if measure is None or measure.kind == "count":
        return len(rows), 0
    if measure.kind == "count_distinct":
        unit = measure.unit or schema.equivalence_field or "id"
        if unit in {"id", schema.canonical_unit} and schema.field(unit) is None:
            return len({r.id for r in rows}), 0
        if unit == schema.equivalence_field:
            return len({("value", r.values[unit]) if r.values.get(unit) is not None else ("record", r.id) for r in rows}), 0
        present = [r.values.get(unit) for r in rows]
        return len({v for v in present if v is not None}), sum(1 for v in present if v is None)
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
        zone = zone_of(schema)
        for r in rows:
            moment = temporal_key(r.values.get(field), spec)
            if moment is None:
                invalid += 1
            else:
                counts[truncate(moment, measure.granularity or "day", zone).isoformat()] += 1
        return dict(sorted(counts.items())), invalid
    numbers = [r.values.get(field) for r in rows]
    valid_numbers = [n for n in numbers if _number(n)]
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


def _flag(text: str, reason: str, suggestion: str) -> UnsupportedConstraint:
    return UnsupportedConstraint(constraint_text=text, reason=reason, suggestion=suggestion)  # type: ignore[arg-type]


def _key_repr(group: GroupRow) -> str:
    return repr(sorted(group.key.items()))


def calendar_label(moment: datetime, granularity: str, zone: tzinfo) -> str:
    """``2024``, ``2024-03`` or ``2024-03-05``: the calendar bucket of ``moment`` in ``zone``."""
    local = moment.astimezone(zone)
    if granularity == "year":
        return f"{local.year:04d}"
    if granularity == "month":
        return f"{local.year:04d}-{local.month:02d}"
    return local.date().isoformat()


def _group_value(row: StructuredRecord, field: str, scope: ScopeSpec, schema: MetadataSchema, zone: tzinfo) -> Any:
    value = row.values.get(field)
    spec = schema.field(field)
    if scope.granularity is not None and spec is not None and spec.type in {"datetime", "date"}:
        moment = temporal_key(value, spec)
        return None if moment is None else calendar_label(moment, scope.granularity, zone)
    return value


def _rank_value(group: GroupRow) -> float | None:
    return float(group.value) if _number(group.value) else None  # type: ignore[arg-type]


def rank_groups(scope: ScopeSpec, groups: list[GroupRow]) -> tuple[list[GroupRow], list[GroupRow]]:
    """``(kept, ties)``. Groups are ranked by their measure value (``order``, default descending;
    the group count for ``count``), ties broken by the group key ascending, with **dense** ranks
    (1, 1, 2: "second most" is the next lower value). ``nth`` keeps every group at that rank;
    ``top_n`` keeps the first N and ``ties`` lists the groups the cut excluded that share the last
    kept group's rank, so a tie at the boundary is always reported. Groups without a numeric value
    are never ranked and come last."""
    descending = (scope.order or "desc") == "desc"
    rankable = [g for g in groups if _rank_value(g) is not None]
    unranked = sorted((g for g in groups if _rank_value(g) is None), key=_key_repr)
    rankable.sort(key=_key_repr)
    rankable.sort(key=lambda g: _rank_value(g), reverse=descending)  # type: ignore[arg-type, return-value]
    ranked: list[GroupRow] = []
    rank, previous = 0, None
    for group in rankable:
        value = _rank_value(group)
        if value != previous:
            rank, previous = rank + 1, value
        ranked.append(group.model_copy(update={"rank": rank}))
    if scope.nth is not None:
        return [g for g in ranked if g.rank == scope.nth], []
    ordered = ranked + unranked
    if scope.top_n is None:
        return ordered, []
    kept = ordered[: scope.top_n]
    boundary = kept[-1].rank if kept else None
    ties = [g for g in ordered[scope.top_n :] if boundary is not None and g.rank == boundary]
    return kept, ties


def order_groups(scope: ScopeSpec, groups: list[GroupRow]) -> tuple[list[GroupRow], list[GroupRow], dict[str, Any]]:
    """``(groups, ties, value)`` of a grouped scope: groups by count (key ascending on ties), then
    the scope's group-field sort or measure ranking (``rank_groups``); ``value`` maps the joined key
    to the group value. Shared by the client path and the native aggregation path."""
    groups = sorted(groups, key=lambda g: (-g.count, _key_repr(g)))
    ties: list[GroupRow] = []
    if scope.sort and scope.sort.field in scope.group_by:
        field = scope.sort.field
        groups.sort(key=lambda g: repr(g.key.get(field)), reverse=scope.sort.descending)
        if scope.top_n:
            groups = groups[: scope.top_n]
    elif scope.order is not None or scope.top_n is not None or scope.nth is not None:
        groups, ties = rank_groups(scope, groups)
    return groups, ties, {"|".join(str(k) for k in g.key.values()): g.value for g in groups}


def _numerator_rows(scope: ScopeSpec, matched: list[StructuredRecord], schema: MetadataSchema, unsupported: list) -> list[StructuredRecord] | None:
    rows = matched
    for predicate in scope.numerator:
        reason = unsupported_reason(schema, predicate)
        if reason is not None:
            unsupported.append(_flag(f"numerator {predicate.field}", reason, "the share numerator cannot be applied; no share is computed"))
            return None
        spec = schema.field(predicate.field)
        rows = [r for r in rows if matches({**r.values, "id": r.id}, predicate, bool(spec and spec.casefold_match), spec)]
    return rows


def _sort_key(row: StructuredRecord, field: str, spec: Any) -> Any:
    value = row.values.get(field)
    if spec is not None and spec.type in {"datetime", "date"}:
        moment = temporal_key(value, spec)
        return None if moment is None else moment.timestamp()
    if _number(value):
        return float(value)
    return value if isinstance(value, str) else None


def _list_records(scope: ScopeSpec, matched: list[StructuredRecord], schema: MetadataSchema) -> list[ListedRecord]:
    """``limit`` records in ``sort`` order (values missing last, ties by id), else by id."""
    if scope.sort is None:
        return [ListedRecord(id=r.id) for r in sorted(matched, key=lambda r: r.id)[: scope.limit]]
    field = scope.sort.field
    spec = schema.field(field)
    present = sorted((r for r in matched if _sort_key(r, field, spec) is not None), key=lambda r: r.id)
    absent = sorted((r for r in matched if _sort_key(r, field, spec) is None), key=lambda r: r.id)
    if len({type(_sort_key(r, field, spec)) for r in present}) == 1:  # mixed text/number never orders: by id
        present.sort(key=lambda r: _sort_key(r, field, spec), reverse=scope.sort.descending)
    return [ListedRecord(id=r.id, sort_value=r.values.get(field)) for r in (present + absent)[: scope.limit]]


def _basis(scope: ScopeSpec, schema: MetadataSchema, applied: list[Predicate], unknown: dict[str, int]) -> list[str]:
    """Human-readable labels of what the result counts and over which window (shown to the generator)."""
    labels: list[str] = []
    measure = scope.measure
    if measure is not None and measure.kind == "count_distinct":
        unit = measure.unit or schema.equivalence_field or "id"
        own = " (a record without one is its own unit)" if unit == schema.equivalence_field else ""
        labels.append(f"count unit: distinct {unit}{own}")
    else:
        labels.append(f"count unit: {schema.canonical_unit} records")
    if scope.time_basis:
        spec = schema.field(scope.time_basis)
        label = spec.time_basis if spec is not None and spec.time_basis else scope.time_basis
        labels.append(f"time basis: {scope.time_basis} ({label}); calendar zone {schema.timezone or 'UTC (undeclared)'}")
    for predicate in applied:
        spec = schema.field(predicate.field)
        if spec is not None and spec.type in {"datetime", "date"} and predicate.op in {"range", "date_interval"}:
            closing = "]" if predicate.op == "range" and predicate.inclusive else ")"
            labels.append(f"window on {predicate.field}: [{predicate.lower}, {predicate.upper}{closing}")
    for field, count in sorted(unknown.items()):
        labels.append(f"{field}: {count} of the matched records have no value (included by the negated condition)")
    if measure is not None and measure.kind in {"sum", "avg", "min", "max"}:
        labels.append(f"{measure.kind} over records with a numeric {measure.field}; the others are excluded and counted in missing_or_invalid")
    return labels


def _numerator_share(scope: ScopeSpec) -> bool:
    return scope.measure is not None and scope.measure.kind == "share" and (bool(scope.numerator) or (scope.text is not None and scope.text.role == "numerator"))


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
    ``bounded`` (an upper bound over the requested population), never claimed as exact.

    Every adapter computes the query-compiler operations here, over the rows it matched, so their
    semantics are identical on every records backend: group ranking (``order``/``top_n``/``nth``,
    boundary ties reported), calendar buckets of temporal group keys (``granularity``, in
    ``MetadataSchema.timezone``), period comparison (``periods``), group shares (``with_share``),
    share numerators (``numerator``), the record list (``limit``; ``total`` always), and the count
    of null values a negated predicate included (``unknown``). A ``text`` condition that reaches a
    record store unresolved is reported as unsupported (the result is ``bounded``), never ignored."""
    unsupported = list(unsupported)
    if scope.text is not None:
        unsupported.append(
            _flag(
                f"text {scope.text.kind} {scope.text.text!r}",
                "not_supported_by_backend",
                "a text condition is resolved through the lexical index by the structured arm; a record store cannot evaluate it",
            )
        )
    zone = zone_of(schema)
    groups: list[GroupRow] = []
    ties: list[GroupRow] = []
    missing = 0
    numerator: int | None = None
    value: Any
    if scope.periods:
        spec = schema.field(scope.time_basis or "")
        by_period: dict[str, Any] = {}
        for period in scope.periods:
            window = Predicate(field=scope.time_basis or "", op="date_interval", lower=period.lower, upper=period.upper, granularity="day")
            reason = unsupported_reason(schema, window) if spec is not None and spec.type in {"datetime", "date"} else "field_not_filterable"
            if reason is not None:
                unsupported.append(_flag(f"period {period.label}", reason, "a comparison period needs valid bounds on a declared datetime/date time_basis field"))
                continue
            rows = [r for r in matched if matches(r.values, window, False, spec)]
            period_value, period_missing = measure_of(scope, rows, population, schema)
            missing += period_missing
            by_period[period.label] = period_value
            groups.append(GroupRow(key={"period": period.label}, value=period_value, count=len(rows)))
        value = by_period
    elif scope.group_by:
        buckets: dict[tuple, list[StructuredRecord]] = {}
        for row in matched:
            buckets.setdefault(tuple(_group_value(row, f, scope, schema, zone) for f in scope.group_by), []).append(row)
        for key, rows in buckets.items():
            group_value, group_missing = measure_of(scope, rows, population, schema)
            missing += group_missing
            share = (len(rows) / len(matched)) if scope.with_share and matched else None
            groups.append(GroupRow(key=dict(zip(scope.group_by, key)), value=group_value, count=len(rows), share=share))
        groups, ties, value = order_groups(scope, groups)
    elif _numerator_share(scope):
        rows = _numerator_rows(scope, matched, schema, unsupported) if scope.numerator else None
        numerator = None if rows is None else len(rows)
        value = (numerator / len(matched)) if numerator is not None and matched else None
    else:
        value, missing = measure_of(scope, matched, population, schema)
    unknown: dict[str, int] = {}
    for predicate in applied:
        spec = schema.field(predicate.field)
        if predicate.negate and predicate.op in {"eq", "in"} and spec is not None and spec.nullable:
            count = sum(1 for r in matched if r.values.get(predicate.field) is None)
            if count:
                unknown[predicate.field] = unknown.get(predicate.field, 0) + count
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
        total=len(matched),
        listed=_list_records(scope, matched, schema) if scope.limit is not None else [],
        ties=ties,
        unknown=unknown,
        numerator=numerator,
        basis=_basis(scope, schema, applied, unknown),
    )


MAX_HOPS = 3


def validated_hops(adapter: str, hops: Any) -> int:
    """``GraphStore.traverse`` hop bound: an integer in ``0..MAX_HOPS`` (``TraversalSpec`` validates it
    too; adapters re-check because a spec built with ``model_construct`` skips validation). Never coerced."""
    from ..core.errors import BackendError

    if isinstance(hops, bool) or not isinstance(hops, int) or not 0 <= hops <= MAX_HOPS:
        raise BackendError(adapter, "traverse", f"hops must be an integer in 0..{MAX_HOPS}, got {hops!r}")
    return hops
