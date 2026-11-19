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
* ``eq`` (agent-compiled) whose value names a declared ``FieldSpec.states`` entry: the state's
  value set (``in``, negated for a ``negate`` state, which includes and reports null values).
* a calendar expression (``ValueResolver.window``; ``decision/windows.py``): a half-open
  ``date_interval`` in ``MetadataSchema.timezone``; a relative expression needs the request
  ``anchor``. Failure: ``unparseable_bound``.

* an agent-written ``eq``/``in`` text value on a field with an enumerable universe (2026-11-09, B5):
  grounded by the rule table of ``decision/value_match.py`` (exact, casefold, normalized, plural,
  distinctive token); a rule-2..5 match executes the stored value and is recorded on the
  ``Resolution`` (``canonical``) and ``ValueResolver.canonicalized``; a rule that finds several
  stored values is ``value_not_observed`` ("ambiguous"). An ``eq`` value no rule grounds on its
  field moves to the one other field that grounds it only when ``ValueResolver.question`` (the
  user's question) contains it and does not name the original field (``other_field``).

Fields with ``max_catalog_cardinality: 0`` (IDs, free text) are never enumerated to a model. No
value is ever guessed or dropped silently; every replacement is a documented rule and is recorded.
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
from ..backends._scope import RECORD_KEYS
from ..models import StrictModel
from .value_match import MatchRule, ValueMatch, field_named, match_value, question_mentions

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
    # 2026-11-09 (B5): agent values grounded by a normalization rule (``decision/value_match.py``):
    # one ``{"field", "as_written", "value", "rule"}`` per replaced value (plus ``"from_field"`` when
    # the question moved it to another field). Exact matches are not listed.
    canonical: list[dict[str, Any]] = Field(default_factory=list)

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
    try:
        parsed.astimezone(timezone.utc)  # the stores compare instants in UTC: an edge-of-range bound cannot be read
    except OverflowError:
        raise BoundError("unparseable_bound", f"{spec.name}: bound {value!r} is outside the supported date range") from None
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
    try:
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).date()
    except OverflowError:
        raise BoundError("unparseable_bound", f"{spec.name}: bound {value!r} is outside the supported date range") from None


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
        anchor: date | None = None,
        *,
        value_scoped: bool = False,
    ):
        self.schema = schema
        # staging round 4 (N10): the ACL carries a value scope (the two-halves knowledge half's
        # sources): declared enum values count as question qualifiers only when present in scope
        self.value_scoped = value_scoped
        self.records = records
        self.gen = gen
        self.acl = list(acl)
        # The request's anchor day in ``schema.timezone`` for relative calendar expressions
        # ("this month", "last 7 days"); ``None`` refuses them (never guessed).
        self.anchor = anchor
        self._observed: dict[str, list[str] | None] = {}
        # The user's own question (never model text) for the cross-field rule of agent values
        # (``value_match`` module docstring); ``None`` disables it.
        self.question: str | None = None
        # every canonical replacement this resolver made (for the trace; values are masked there)
        self.canonicalized: list[dict[str, Any]] = []
        self._last_rule: MatchRule | None = None

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

    def known_value(self, field: str, value: str) -> bool:
        """``value`` is an observed value of ``field`` (case-insensitive, listable fields under their
        cap) or, without a listable universe, ``RecordStore.exists`` confirms it. No store: False."""
        observed = self.observed(field)
        if observed is not None:
            folded = value.casefold()
            return any(isinstance(v, str) and v.casefold() == folded for v in observed)
        return bool(self._exists(field, value))

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
        if spec is None and predicate.field in RECORD_KEYS and predicate.op in {"eq", "in"}:
            values = predicate.value if predicate.op == "in" and isinstance(predicate.value, list) else [predicate.value]
            if all(isinstance(v, str) for v in values):
                # record identity, always filterable without a schema entry (string equality)
                return Resolution(predicate=predicate, origin=origin)
            return Resolution(
                unsupported=UnsupportedConstraint(constraint_text=_constraint(predicate), reason="type_mismatch", suggestion=f"{predicate.field!r} is a record key; compare it with strings"),
                origin=origin,
            )
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
            # Declared states apply to agent-compiled predicates only; a caller's request filter is
            # a literal value and is applied as given.
            state = self.state(spec, predicate) if origin == "agent" else None
            if state is not None:
                return Resolution(predicate=state, origin=origin)
            if predicate.op == "missing":
                return self._resolve_missing(spec, predicate, origin)
            if predicate.op in {"eq", "in"}:
                return self._resolve_equality(spec, predicate, origin)
            if predicate.op == "range":
                return self._resolve_range(spec, predicate, origin)
            return self._resolve_interval(spec, predicate, origin)
        except BoundError as error:
            moved = self._retarget(spec, predicate, origin, error)
            if moved is not None:
                return moved
            return Resolution(
                unsupported=UnsupportedConstraint(constraint_text=_constraint(predicate), reason=error.reason, suggestion=str(error)),
                origin=origin,
            )

    def ground_text(self, spec: FieldSpec, value: str) -> ValueMatch | None:
        """``value_match.match_value`` over the field's enumerable universe (declared enum values, or
        observed values of a listable field under its cap); ``None`` when it has none."""
        universe = list(spec.enum_values or []) if spec.type == "enum" else self.observed(spec.name)
        if universe is None:
            return None
        return match_value(value, universe, casefold=spec.casefold_match)

    def other_field(self, field: str, value: str) -> tuple[FieldSpec, ValueMatch] | BoundError | None:
        """The cross-field rule (``value_match`` module docstring): the one other filterable string/
        enum field with an enumerable universe that grounds ``value``, when ``value`` is a word
        sequence of ``self.question`` and the question does not name ``field``. ``None``: the rule
        does not apply or nothing grounds it; ``BoundError``: several fields (or values) ground it."""
        question = self.question
        if not question or not question_mentions(question, value) or field_named(question, field):
            return None
        found: list[tuple[FieldSpec, ValueMatch]] = []
        ambiguous: list[str] = []
        for other in self.schema.fields:
            if other.name == field or not other.filterable or other.type not in {"string", "enum"}:
                continue
            match = self.ground_text(other, value)
            if match is None:
                continue
            if match.found:
                found.append((other, match))
            elif match.ambiguous:
                ambiguous.extend(f"{other.name}={v!r}" for v in match.ambiguous)
        if len(found) == 1 and not ambiguous:
            return found[0]
        if found or ambiguous:
            names = [f"{s.name}={m.value!r}" for s, m in found] + ambiguous
            return BoundError("value_not_observed", f"{field}: {value!r} is not an observed value; it matches several values of other fields ({', '.join(names[:6])}); name the field")
        return None

    def _retarget(self, spec: FieldSpec, predicate: Predicate, origin: str, error: BoundError) -> Resolution | None:
        """An agent ``eq`` text value that no rule grounds on its field, moved to the one other field
        that grounds it (``other_field``); ambiguity stays an explicit unsupported constraint."""
        if origin != "agent" or error.reason != "value_not_observed" or predicate.op != "eq" or not isinstance(predicate.value, str):
            return None
        if spec.type not in {"string", "enum", "id"} or "ambiguous" in str(error):
            return None
        moved = self.other_field(spec.name, predicate.value)
        if moved is None:
            return None
        if isinstance(moved, BoundError):
            return Resolution(unsupported=UnsupportedConstraint(constraint_text=_constraint(predicate), reason=moved.reason, suggestion=str(moved)), origin=origin)  # type: ignore[arg-type]
        other, match = moved
        record = {"field": other.name, "from_field": spec.name, "as_written": predicate.value, "value": match.value, "rule": f"question_field:{match.rule}"}
        self.canonicalized.append(record)
        typed = Predicate(field=other.name, op="eq", value=match.value, negate=predicate.negate)
        return Resolution(predicate=typed, origin=origin, canonical=[record])  # type: ignore[arg-type]

    @staticmethod
    def state(spec: FieldSpec, predicate: Predicate) -> Predicate | None:
        """An ``eq`` on a declared state name (``FieldSpec.states``, case-insensitive) becomes the
        state's value set: ``in values``, negated per the state (and inverted again by a negated
        predicate). A declared state takes precedence over an observed value of the same text."""
        if predicate.op != "eq" or not isinstance(predicate.value, str) or not spec.states:
            return None
        folded = predicate.value.strip().casefold()
        named = next((s for s in spec.states if s.name.casefold() == folded), None)
        if named is None:
            return None
        return Predicate(field=spec.name, op="in", value=list(named.values), negate=named.negate != predicate.negate)

    def window(self, field: str, expression: str) -> Resolution:
        """A calendar expression on a temporal field → a half-open ``date_interval`` predicate
        (``decision/windows.py``; relative expressions need ``anchor``)."""
        from ..backends._scope import zone_of
        from .windows import WindowError, bounds, parse_window

        spec = self.schema.field(field)
        text = f"{field} within {expression!r}"
        if spec is None or not spec.filterable or spec.type not in {"datetime", "date"}:
            return Resolution(
                unsupported=UnsupportedConstraint(constraint_text=text, reason="field_not_filterable", suggestion="a time window needs a declared filterable datetime/date field")
            )
        if spec.type == "datetime" and spec.timezone_policy == "require_offset" and self.schema.timezone is None:
            return Resolution(
                unsupported=UnsupportedConstraint(
                    constraint_text=text,
                    reason="timezone_missing",
                    suggestion=f"{field} requires offsets and metadata_schema.timezone is not declared; declare the zone calendar expressions are read in",
                )
            )
        try:
            window = parse_window(expression, self.anchor)
        except WindowError as error:
            return Resolution(unsupported=UnsupportedConstraint(constraint_text=text, reason="unparseable_bound", suggestion=str(error)))
        lower, upper = bounds(window, spec.type, zone_of(self.schema))
        return Resolution(predicate=Predicate(field=field, op="date_interval", lower=lower, upper=upper, granularity="day"))

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
        canonical: list[dict[str, Any]] = []
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
                stored, missing = self._resolve_text(spec, value, origin == "request")
                resolved.append(stored)
                zero_match = zero_match or missing
                if stored != value and isinstance(value, str) and origin == "agent" and self._last_rule not in {None, "exact", "casefold"}:
                    record = {"field": spec.name, "as_written": value, "value": stored, "rule": self._last_rule}
                    canonical.append(record)
                    self.canonicalized.append(record)
        if predicate.op == "in" and len(set(map(str, resolved))) != len(resolved):
            resolved = list(dict.fromkeys(resolved))  # two spellings of one stored value
        typed = predicate.model_copy(update={"value": resolved if predicate.op == "in" else resolved[0]})
        return Resolution(predicate=typed, zero_match=zero_match, origin=origin, canonical=canonical)

    def _resolve_text(self, spec: FieldSpec, value: Any, from_request: bool) -> tuple[str, bool]:
        """Returns ``(canonical value, zero_match)``; raises ``BoundError(value_not_observed)``."""
        self._last_rule = None
        if not isinstance(value, str):
            raise BoundError("type_mismatch", f"{spec.name}: {value!r} is not text")
        if spec.type == "enum":
            if from_request:
                match = self._match(value, spec.enum_values or [], spec.casefold_match)
                if match is None:
                    raise BoundError("value_not_observed", f"{spec.name}: {value!r} is not one of the declared enum values")
                return match, False
            grounded = match_value(value, spec.enum_values or [], casefold=spec.casefold_match)
            if grounded.ambiguous:
                raise BoundError("value_not_observed", f"{spec.name}: {value!r} is ambiguous: it matches {grounded.ambiguous[:6]} under the documented normalization")
            if not grounded.found:
                raise BoundError("value_not_observed", f"{spec.name}: {value!r} is not one of the declared enum values")
            self._last_rule = grounded.rule
            return grounded.value, False  # type: ignore[return-value]
        if from_request:
            exists = self._exists(spec.name, value)
            return value, exists is False  # applied as given; zero match reported, not rewritten
        observed = self.observed(spec.name)
        if observed is not None:
            grounded = match_value(value, observed, casefold=spec.casefold_match)
            if grounded.ambiguous:
                raise BoundError("value_not_observed", f"{spec.name}: {value!r} is ambiguous: it matches {grounded.ambiguous[:6]} under the documented normalization")
            if not grounded.found:
                raise BoundError("value_not_observed", f"{spec.name}: {value!r} is not an observed value")
            self._last_rule = grounded.rule
            return grounded.value, False  # type: ignore[return-value]
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


class FilterCheck(StrictModel):
    """Caller filters validated against ``MetadataSchema`` before anything executes (QA-02).

    ``applied`` is what is pushed down: the typed value of a valid filter (after an unambiguous
    coercion), the caller's value **as given** for an invalid or undeclared one. A filter is never
    dropped, so a scope never silently widens. Every invalid or undeclared filter is an
    ``UnsupportedConstraint``, and every aggregate computed under one is ``bounded``: an invalid
    filter never yields an exact zero. ``coercions`` and ``untyped`` are recorded in the trace."""

    applied: dict[str, JsonScalar] = Field(default_factory=dict)
    unsupported: list[UnsupportedConstraint] = Field(default_factory=list)
    invalid: dict[str, str] = Field(default_factory=dict)  # field -> unsupported reason
    coercions: list[dict[str, Any]] = Field(default_factory=list)  # {field, from, to, type}
    untyped: list[str] = Field(default_factory=list)  # undeclared fields applied as untyped string equality

    def trace(self) -> dict[str, Any]:
        """Field names, coercion kinds and reasons only (values stay out of the trace)."""
        return {
            "fields": sorted(self.applied),
            "coercions": [{"field": c["field"], "type": c["type"]} for c in self.coercions],
            "untyped": list(self.untyped),
            "invalid": dict(self.invalid),
        }


UNTYPED_SUGGESTION = (
    "undeclared field: applied as untyped string equality (metadata_schema.unknown_fields: allow); "
    "declare it as a filterable field for typed matching and exact aggregates"
)


VALUE_NOT_OBSERVED = (
    "the filter value is not among the field's observed values in this generation; it was applied as "
    "given, so the aggregate is bounded and an absence or zero-count claim on it is refused"
)


def _coerce(spec: FieldSpec, value: JsonScalar) -> JsonScalar:
    """Unambiguous coercions only: ``"3"``→3 (integer), ``"2.5"``→2.5 (number), ``"true"``→True
    (boolean), an integer → its decimal text (``string``/``id``). Anything else is returned unchanged
    and validated (and refused) by the resolver."""
    if value is None or (isinstance(value, bool) and spec.type != "boolean"):
        return value
    try:
        if spec.type in {"integer", "number"}:
            return _parse_number(spec, value)
        if spec.type == "boolean":
            return _parse_boolean(spec, value)
    except BoundError:
        return value
    if spec.type in {"string", "id"} and isinstance(value, int):
        return str(value)
    return value


def check_request_filters(resolver: ValueResolver, filters: dict[str, JsonScalar]) -> FilterCheck:
    """Validate caller filters against the schema (see ``FilterCheck``).

    * declared field: unambiguous coercion, then the ``ValueResolver`` row for ``eq``
      (``type_mismatch``, ``unparseable_bound``, ``timezone_missing``, enum ``value_not_observed``,
      ``field_not_filterable``). Datetime/date values are parsed but pushed down as given, so the
      backend's stored-value comparison is unchanged. A string/id value on a listable field under
      its catalog cap that no record holds is applied but reported as ``value_not_observed``.
    * the record keys ``id``, ``source``, ``dataset`` and ``kind`` (``RECORD_KEYS``), when the
      schema does not declare them: string equality, always available (a non-string value is
      ``type_mismatch``).
    * undeclared field under ``unknown_fields: reject``: ``field_not_filterable``.
    * undeclared field under ``unknown_fields: allow``: applied as untyped string equality,
      labelled in ``untyped`` and reported (``field_not_filterable``) because its aggregate cannot be
      exact."""
    schema = resolver.schema
    check = FilterCheck()
    for field, value in filters.items():
        spec = schema.field(field)
        if spec is None and field in RECORD_KEYS:
            # Record identity (``id``/``source``/``dataset``/``kind``) is filterable on every
            # document without a schema entry: string equality, applied as given.
            if not isinstance(value, str):
                check.applied[field] = value
                check.invalid[field] = "type_mismatch"
                check.unsupported.append(UnsupportedConstraint(constraint_text=f"{field} eq {value!r}", reason="type_mismatch", suggestion=f"{field!r} is a record key; filter it with a string"))
                continue
            check.applied[field] = value
            continue
        if spec is None:
            check.applied[field] = value
            check.invalid[field] = "field_not_filterable"
            suggestion = UNTYPED_SUGGESTION if schema.unknown_fields == "allow" else f"declare {field!r} as a filterable field in metadata_schema"
            if schema.unknown_fields == "allow":
                check.untyped.append(field)
            check.unsupported.append(UnsupportedConstraint(constraint_text=f"{field} eq {value!r}", reason="field_not_filterable", suggestion=suggestion))
            continue
        coerced = _coerce(spec, value)
        resolution = resolver.resolve(Predicate(field=field, op="eq", value=coerced), from_request=True)
        if resolution.unsupported is not None:
            check.applied[field] = value  # as given: never dropped, never widened
            check.invalid[field] = resolution.unsupported.reason
            check.unsupported.append(resolution.unsupported)
            continue
        if spec.type in {"datetime", "date"}:
            check.applied[field] = value
            continue
        check.applied[field] = coerced
        if resolution.zero_match and resolver.observed(field) is not None:
            # A listable field whose value universe is known, and the caller's value is not in it
            # (a typo, or the wrong case on a case-sensitive field): applied as given, but its
            # aggregate is not exact, so "0" or an absence claim never passes on it (N4).
            check.invalid[field] = "value_not_observed"
            check.unsupported.append(UnsupportedConstraint(constraint_text=f"{field} eq {value!r}", reason="value_not_observed", suggestion=VALUE_NOT_OBSERVED))
        if type(coerced) is not type(value) or coerced != value:
            check.coercions.append({"field": field, "from": value, "to": coerced, "type": f"{type(value).__name__}->{spec.type}"})
    return check


# --- registry-grounded customer text (unified merge M2b-2, §6.9, §6.11; lane LF) -------------------


class CustomerMention(StrictModel):
    """One customer named in a question: the raw alias as written in the registry, its namespace,
    the registry customer and the log tenants (``tenant_namespace`` aliases) that join to it."""

    text: str
    namespace: str
    customer_id: str
    tenants: list[str] = []


class CustomerGrounding(StrictModel):
    """Customers named in a question, grounded through the pinned identity registry (matched aliases
    only, ``IdentityView``). ``joins_enabled`` false: no name can be resolved (``reason``), which is
    stated, never replaced by an unfiltered answer. ``registry_version`` pins the reading."""

    joins_enabled: bool
    reason: str | None = None
    registry_version: str | None = None
    customers: list[CustomerMention] = []

    def tenants(self) -> list[str]:
        return list(dict.fromkeys(t for c in self.customers for t in c.tenants))

    def ungrounded(self) -> list[CustomerMention]:
        return [c for c in self.customers if not c.tenants]

    def statements(self) -> list[str]:
        out = []
        for c in self.customers:
            if c.tenants:
                out.append(f'"{c.text}" was read as customer {c.customer_id}, log tenant {", ".join(c.tenants)} (identity registry {self.registry_version}, matched aliases only).')
            else:
                out.append(f'"{c.text}" was read as customer {c.customer_id}, which has no matched log tenant in identity registry {self.registry_version}; operations evidence for it is not shown.')
        return out


def _mentions(question: str, raws: list[str]) -> list[str]:
    """Registry raw values written in ``question`` (case-insensitive, whole words, longest first,
    non-overlapping)."""
    found: list[str] = []
    taken: list[tuple[int, int]] = []
    for raw in sorted({r for r in raws if r and len(r) >= 2}, key=lambda r: (-len(r), r)):
        for m in re.finditer(rf"(?<![\w.\-]){re.escape(raw)}(?![\w\-]|\.\w)", question, re.I):
            if not any(a < m.end() and m.start() < b for a, b in taken):
                taken.append((m.start(), m.end()))
                found.append(raw)
                break
    return found


def ground_customers(question: str, identity: Any, customer_namespaces: list[str], tenant_namespace: str) -> CustomerGrounding:
    """Customers named in ``question`` through ``identity`` (``crosssource.identity.IdentityView``):
    a raw alias of a ``customer_namespaces`` namespace written in the question names its customer;
    its log tenants are the ``tenant_namespace`` aliases matched to the same customer. Only matched
    aliases join (candidate, contested, ambiguous and unmatched aliases never do); a tenant value
    written directly in the question is the log intents' own known-value binding, not this one."""
    if identity is None:
        return CustomerGrounding(joins_enabled=False, reason="no identity registry configured (crosssource.identity)")
    if not identity.joins_enabled:
        return CustomerGrounding(joins_enabled=False, reason=identity.reason, registry_version=identity.registry_version)
    by_namespace: dict[str, dict[str, str]] = {}
    for (namespace, raw), customer in identity.matched.items():
        by_namespace.setdefault(namespace, {})[raw] = customer
    tenants_of: dict[str, list[str]] = {}
    for raw, customer in sorted(by_namespace.get(tenant_namespace, {}).items()):
        tenants_of.setdefault(customer, []).append(raw)
    customers: list[CustomerMention] = []
    seen: set[str] = set()
    for namespace in customer_namespaces:
        aliases = by_namespace.get(namespace, {})
        for raw in _mentions(question, list(aliases)):
            customer = aliases[raw]
            if customer in seen:
                continue
            seen.add(customer)
            customers.append(CustomerMention(text=raw, namespace=namespace, customer_id=customer, tenants=tenants_of.get(customer, [])))
    return CustomerGrounding(joins_enabled=True, registry_version=identity.registry_version, customers=customers)
