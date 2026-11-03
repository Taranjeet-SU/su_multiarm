"""``ValueResolver`` (plan §3.7.2): typed predicate value resolution against the metadata schema.

Resolution table (one row per predicate/field-type combination):

* ``eq``/``in`` on ``string``/``enum``/``id``: ``enum`` values must be in ``enum_values``; otherwise,
  when the field's observed cardinality is under ``max_catalog_cardinality`` (and the field is
  listable) the ACL-scoped observed values are the universe and the value must match one
  (``casefold_match`` per field); above the cap the candidate is checked with
  ``RecordStore.exists(gen, field, value, acl)``. A value the caller wrote in ``request.filters`` is
  applied as given (explicit user filter, not a model-invented identifier) and a zero match is
  reported as ``zero_match``. Failure: ``value_not_observed``.
* ``eq``/``in`` on ``integer``/``number``/``boolean``: type parse only. Failure: ``type_mismatch``.
* ``range`` on ``integer``/``number``/``datetime``/``date``: typed bounds; a datetime bound needs an
  explicit offset under ``require_offset``. Failure: ``unparseable_bound`` | ``timezone_missing``.
* ``date_interval`` on ``datetime``/``date``: as ``range`` plus granularity; half-open ``[start,
  end)`` computed under the field's timezone policy.
* ``missing``: the field must be ``nullable``. Failure: ``field_not_nullable``.
* any predicate on an unknown or non-``filterable`` field: ``field_not_filterable``.

Fields with ``max_catalog_cardinality: 0`` (IDs, free text) are never enumerated to a model. No
value is ever guessed, corrected or dropped silently.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Sequence

from pydantic import Field

from ..core.types import (
    FieldSpec,
    GenerationId,
    JsonScalar,
    MetadataSchema,
    Predicate,
    ScopePredicates,
    UnsupportedConstraint,
    UnsupportedReason,
)
from ..models import StrictModel

Granularity = Literal["day", "month", "year"]
_INT = re.compile(r"^[+-]?\d+$")
_YEAR = re.compile(r"^\d{4}$")
_MONTH = re.compile(r"^\d{4}-\d{2}$")


class Resolution(StrictModel):
    """Outcome for one predicate: a typed predicate, or an unsupported constraint, never both."""

    predicate: Predicate | None = None
    unsupported: UnsupportedConstraint | None = None
    zero_match: bool = False  # caller-supplied value with no observed record (applied as given)
    origin: Literal["request", "agent"] = "agent"

    def ok(self) -> bool:
        return self.predicate is not None and self.unsupported is None


class BoundError(ValueError):
    def __init__(self, reason: UnsupportedReason, message: str):
        self.reason = reason
        super().__init__(message)


def _constraint(predicate: Predicate) -> str:
    if predicate.op in {"eq", "in"}:
        return f"{predicate.field} {predicate.op} {predicate.value!r}"
    if predicate.op == "missing":
        return f"{predicate.field} missing"
    return f"{predicate.field} {predicate.op} [{predicate.lower!r}, {predicate.upper!r}]"


def _parse_number(spec: FieldSpec, value: Any) -> int | float:
    if isinstance(value, bool):
        raise BoundError("type_mismatch", f"{spec.name}: boolean is not a {spec.type}")
    if spec.type == "integer":
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and _INT.match(value.strip()):
            return int(value.strip())
        raise BoundError("type_mismatch", f"{spec.name}: {value!r} is not an integer")
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            pass
    raise BoundError("type_mismatch", f"{spec.name}: {value!r} is not a number")


def _parse_boolean(spec: FieldSpec, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().casefold() in {"true", "false"}:
        return value.strip().casefold() == "true"
    raise BoundError("type_mismatch", f"{spec.name}: {value!r} is not a boolean")


def _parse_datetime_bound(spec: FieldSpec, value: Any) -> datetime:
    if not isinstance(value, str):
        raise BoundError("unparseable_bound", f"{spec.name}: bound {value!r} is not an ISO 8601 datetime")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        raise BoundError("unparseable_bound", f"{spec.name}: bound {value!r} is not an ISO 8601 datetime") from None
    if parsed.tzinfo is None:
        if spec.timezone_policy == "require_offset":
            raise BoundError("timezone_missing", f"{spec.name}: bound {value!r} needs an explicit UTC offset")
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_date_bound(spec: FieldSpec, value: Any) -> date:
    """A ``date`` field accepts an ISO date, or a datetime that is truncated to its UTC date (a naive
    datetime is read as UTC); the store compares calendar dates the same way."""
    if not isinstance(value, str):
        raise BoundError("unparseable_bound", f"{spec.name}: bound {value!r} is not an ISO 8601 date")
    text = value.strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00") if text.endswith("Z") else text)
    except ValueError:
        raise BoundError("unparseable_bound", f"{spec.name}: bound {value!r} is not an ISO 8601 date") from None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).date()


def _calendar_bound(spec: FieldSpec, value: Any, granularity: Granularity) -> datetime:
    """A ``date_interval`` bound: ``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD`` or a full datetime."""
    if not isinstance(value, str):
        raise BoundError("unparseable_bound", f"{spec.name}: bound {value!r} is not a calendar bound")
    text = value.strip()
    naive: datetime | None = None
    try:
        if _YEAR.match(text):
            naive = datetime(int(text), 1, 1)
        elif _MONTH.match(text):
            naive = datetime(int(text[:4]), int(text[5:7]), 1)
        elif len(text) == 10:
            naive = datetime.combine(date.fromisoformat(text), datetime.min.time())
    except ValueError:
        raise BoundError("unparseable_bound", f"{spec.name}: bound {value!r} is not a calendar bound") from None
    if naive is not None:
        # A calendar-shaped bound carries no offset: allowed for date fields and under assume_utc.
        if spec.type == "datetime" and spec.timezone_policy == "require_offset":
            raise BoundError("timezone_missing", f"{spec.name}: calendar bound {value!r} needs an explicit UTC offset")
        return naive.replace(tzinfo=timezone.utc)
    if spec.type == "date":
        raise BoundError("unparseable_bound", f"{spec.name}: bound {value!r} is not an ISO 8601 date")
    return _parse_datetime_bound(spec, value)


def truncate(moment: datetime, granularity: Granularity) -> datetime:
    moment = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "month":
        return moment.replace(day=1)
    if granularity == "year":
        return moment.replace(month=1, day=1)
    return moment


def next_unit(moment: datetime, granularity: Granularity) -> datetime:
    if granularity == "day":
        return moment + timedelta(days=1)
    if granularity == "month":
        year, month = (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
        return moment.replace(year=year, month=month)
    return moment.replace(year=moment.year + 1)


class ValueResolver:
    """Resolves predicate values against the schema, observed values and ``RecordStore.exists``."""

    def __init__(
        self,
        schema: MetadataSchema,
        records: Any | None = None,
        gen: GenerationId | None = None,
        acl: ScopePredicates | Sequence[Predicate] = (),
    ):
        self.schema = schema
        self.records = records
        self.gen = gen
        self.acl = list(acl)
        self._observed: dict[str, list[str] | None] = {}

    # --- observed values -------------------------------------------------------------------------

    def observed(self, field: str) -> list[str] | None:
        """ACL-scoped observed values when the field is listable and under the cap; else ``None``."""
        if field in self._observed:
            return self._observed[field]
        spec = self.schema.field(field)
        result: list[str] | None = None
        if spec is not None and spec.listable and spec.max_catalog_cardinality > 0 and self._store_ready():
            values = self.records.values(self.gen, field, self.acl, spec.max_catalog_cardinality + 1)
            if len(values) <= spec.max_catalog_cardinality:
                result = list(values)
        self._observed[field] = result
        return result

    def planner_values(self) -> dict[str, list[str]]:
        """Observed values a planner may see: listable, filterable fields under their cap only."""
        catalog = {}
        for spec in self.schema.fields:
            if spec.filterable and spec.listable and spec.max_catalog_cardinality > 0:
                values = self.observed(spec.name)
                if values is not None:
                    catalog[spec.name] = values
        return catalog

    def _store_ready(self) -> bool:
        return self.records is not None and self.gen is not None

    def _exists(self, field: str, value: Any) -> bool | None:
        if not self._store_ready():
            return None
        return bool(self.records.exists(self.gen, field, value, self.acl))

    # --- resolution ------------------------------------------------------------------------------

    def resolve(self, predicate: Predicate, *, from_request: bool = False) -> Resolution:
        origin: Literal["request", "agent"] = "request" if from_request else "agent"
        spec = self.schema.field(predicate.field)
        if spec is None or not spec.filterable:
            return Resolution(
                unsupported=UnsupportedConstraint(
                    constraint_text=_constraint(predicate),
                    reason="field_not_filterable",
                    suggestion=f"declare {predicate.field!r} as a filterable field in metadata_schema",
                ),
                origin=origin,
            )
        try:
            if predicate.op == "missing":
                return self._resolve_missing(spec, predicate, origin)
            if predicate.op in {"eq", "in"}:
                return self._resolve_equality(spec, predicate, origin)
            if predicate.op == "range":
                return self._resolve_range(spec, predicate, origin)
            return self._resolve_interval(spec, predicate, origin)
        except BoundError as error:
            return Resolution(
                unsupported=UnsupportedConstraint(constraint_text=_constraint(predicate), reason=error.reason, suggestion=str(error)),
                origin=origin,
            )

    def _resolve_missing(self, spec: FieldSpec, predicate: Predicate, origin: str) -> Resolution:
        if not spec.nullable:
            return Resolution(
                unsupported=UnsupportedConstraint(
                    constraint_text=_constraint(predicate), reason="field_not_nullable", suggestion=f"{spec.name} is never missing"
                ),
                origin=origin,
            )
        return Resolution(predicate=predicate, origin=origin)

    def _resolve_equality(self, spec: FieldSpec, predicate: Predicate, origin: str) -> Resolution:
        raw = predicate.value if predicate.op == "in" else [predicate.value]
        resolved: list[JsonScalar] = []
        zero_match = False
        for value in raw:
            if spec.type in {"integer", "number"}:
                resolved.append(_parse_number(spec, value))
            elif spec.type == "boolean":
                resolved.append(_parse_boolean(spec, value))
            elif spec.type == "datetime":
                resolved.append(_parse_datetime_bound(spec, value).isoformat())
            elif spec.type == "date":
                resolved.append(_parse_date_bound(spec, value).isoformat())
            else:
                canonical, missing = self._resolve_text(spec, value, origin == "request")
                resolved.append(canonical)
                zero_match = zero_match or missing
        typed = predicate.model_copy(update={"value": resolved if predicate.op == "in" else resolved[0]})
        return Resolution(predicate=typed, zero_match=zero_match, origin=origin)

    def _resolve_text(self, spec: FieldSpec, value: Any, from_request: bool) -> tuple[str, bool]:
        """Returns ``(canonical value, zero_match)``; raises ``BoundError(value_not_observed)``."""
        if not isinstance(value, str):
            raise BoundError("type_mismatch", f"{spec.name}: {value!r} is not text")
        if spec.type == "enum":
            match = self._match(value, spec.enum_values or [], spec.casefold_match)
            if match is None:
                raise BoundError("value_not_observed", f"{spec.name}: {value!r} is not one of the declared enum values")
            return match, False
        if from_request:
            exists = self._exists(spec.name, value)
            return value, exists is False  # applied as given; zero match reported, not rewritten
        observed = self.observed(spec.name)
        if observed is not None:
            match = self._match(value, observed, spec.casefold_match)
            if match is None:
                raise BoundError("value_not_observed", f"{spec.name}: {value!r} is not an observed value")
            return match, False
        exists = self._exists(spec.name, value)
        if exists is None:
            raise BoundError("value_not_observed", f"{spec.name}: no record store is available to verify {value!r}")
        if not exists:
            raise BoundError("value_not_observed", f"{spec.name}: {value!r} does not exist in this generation")
        return value, False

    @staticmethod
    def _match(value: str, universe: Sequence[str], casefold: bool) -> str | None:
        if value in universe:
            return value
        if casefold:
            folded = value.casefold()
            return next((u for u in universe if u.casefold() == folded), None)
        return None

    def _resolve_range(self, spec: FieldSpec, predicate: Predicate, origin: str) -> Resolution:
        if spec.type not in {"integer", "number", "datetime", "date"}:
            raise BoundError("type_mismatch", f"{spec.name}: range needs a numeric, datetime or date field")
        bounds: dict[str, JsonScalar] = {}
        for side in ("lower", "upper"):
            value = getattr(predicate, side)
            if value is None:
                continue
            if spec.type in {"integer", "number"}:
                bounds[side] = _parse_number(spec, value)
            elif spec.type == "datetime":
                bounds[side] = _parse_datetime_bound(spec, value).isoformat()
            else:
                bounds[side] = _parse_date_bound(spec, value).isoformat()
        if "lower" in bounds and "upper" in bounds and bounds["lower"] > bounds["upper"]:  # type: ignore[operator]
            raise BoundError("unparseable_bound", f"{spec.name}: lower bound is above the upper bound")
        return Resolution(predicate=predicate.model_copy(update=bounds), origin=origin)

    def _resolve_interval(self, spec: FieldSpec, predicate: Predicate, origin: str) -> Resolution:
        if spec.type not in {"datetime", "date"}:
            raise BoundError("type_mismatch", f"{spec.name}: date_interval needs a datetime or date field")
        granularity: Granularity = predicate.granularity or "day"
        lower = _calendar_bound(spec, predicate.lower, granularity) if predicate.lower is not None else None
        upper = _calendar_bound(spec, predicate.upper, granularity) if predicate.upper is not None else None
        start = truncate(lower if lower is not None else upper, granularity)  # type: ignore[arg-type]
        end = next_unit(truncate(upper if upper is not None else lower, granularity), granularity)  # type: ignore[arg-type]
        if end <= start:
            raise BoundError("unparseable_bound", f"{spec.name}: interval end is not after its start")
        if spec.type == "date":
            bounds = {"lower": start.date().isoformat(), "upper": end.date().isoformat()}
        else:
            bounds = {"lower": start.isoformat(), "upper": end.isoformat()}
        return Resolution(predicate=predicate.model_copy(update={**bounds, "inclusive": True}), origin=origin)


class ResolvedFilters(StrictModel):
    predicates: list[Predicate] = Field(default_factory=list)
    unsupported: list[UnsupportedConstraint] = Field(default_factory=list)
    zero_match_fields: list[str] = Field(default_factory=list)


def resolve_request_filters(resolver: ValueResolver, filters: dict[str, JsonScalar]) -> ResolvedFilters:
    """Caller filters: typed as ``eq`` predicates, applied as given, zero matches reported."""
    result = ResolvedFilters()
    for field, value in filters.items():
        resolution = resolver.resolve(Predicate(field=field, op="eq", value=value), from_request=True)
        if resolution.unsupported is not None:
            result.unsupported.append(resolution.unsupported)
            continue
        result.predicates.append(resolution.predicate)  # type: ignore[arg-type]
        if resolution.zero_match:
            result.zero_match_fields.append(field)
    return result
