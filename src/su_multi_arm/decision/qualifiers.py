"""Qualifier coverage of an agent-compiled scope (QA-01; plan §3.7, §3.9a).

A structured step a decision agent compiled from the question must account for every qualifier the
question carries; otherwise its count, list or absence answers a wider question. The check is
deterministic and English-scoped like the intent pre-pass. Qualifier candidates are:

* ``value``: an observed value of a filterable, listable field (ACL-scoped, under its catalog cap)
  or a declared enum value that occurs verbatim in the question (word-bounded, case-insensitive;
  longest value first, the canonical-unit words never count). Applied when a predicate (or a
  request filter) on a field holding that value constrains it to that value.
* ``time``: a calendar expression (a year, an ISO date, a month name next to a day or year, a
  quarter, ``yesterday``/``today``, ``last|this|past|previous <unit>``, ``last N <units>``).
  Applied when a predicate (or a request filter) constrains a declared ``datetime``/``date`` field.
* ``quoted``: a quoted phrase. Applied when a predicate or request filter carries it as a value.
* ``field``: a filterable field named in the question (its name, underscores read as spaces) next to
  a value of the field's type: ``priority 3``, ``version 2.1``, ``severity high``, ``priority is 3``,
  ``priority above 2``. Numbers count for ``integer``/``number`` fields, ``true``/``false``/``yes``/
  ``no`` for ``boolean`` fields, a word for ``string``/``id`` fields and an enum value for ``enum``
  fields (function words and unit words never count). A ``boolean`` field named on its own
  ("escalated tickets") is a qualifier too. Applied when a predicate (or a request filter) on that
  field constrains it to that value; a comparison word ("above", ">=", "at least") or a bare
  boolean field is applied by any predicate on the field. This covers non-string qualifiers that
  have no observed-value universe (QA-01, 2.0.0a2 review B1).

Every candidate left unapplied becomes ``UnsupportedConstraint(reason="qualifier_not_applied")`` on
the step and on the decision; the engine reports the step's aggregate as ``bounded`` and the
``AnswerContract`` then refuses count, exhaustive-list and absence claims backed by it. A structured
step on a question without any aggregate operator (the pre-pass found none and the caller typed no
aggregate) is flagged the same way: its topic words are not part of the scope. Scopes the caller
typed (``request.aggregate`` / ``request.scope``) are the caller's and are not checked.

Not covered (documented limitation): free-text topic words of a question that does carry an
aggregate operator ("how many CloudTax tickets mention rounding": ``rounding``) are not candidates
for the ``llm`` agent; the ``rules`` agent refuses residual words on its own.
"""

from __future__ import annotations

import re
from typing import Literal, Sequence

from ..core.types import JsonScalar, MetadataSchema, Predicate, ScopeSpec, UnsupportedConstraint
from ..models import StrictModel
from .intent import GENERIC_UNITS, Intents
from .values import ValueResolver

QualifierKind = Literal["value", "time", "quoted", "field"]
_MONTHS = "january|february|march|april|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
_TIME = re.compile(
    r"\b(?:"
    r"(?:19|20)\d{2}-\d{2}(?:-\d{2})?"  # ISO date or month
    r"|(?:19|20)\d{2}"  # a year
    r"|(?:" + _MONTHS + r"|may)\s+\d{1,2}(?:st|nd|rd|th)?\b"  # month + day
    r"|(?:" + _MONTHS + r"|may)\s+(?:of\s+)?(?:19|20)\d{2}"  # month + year
    r"|(?:in|during|since|before|after|until)\s+(?:" + _MONTHS + r")"  # "in march"
    r"|q[1-4]"
    r"|yesterday|today|tonight"
    r"|(?:last|this|past|previous|next)\s+(?:day|week|weekend|month|quarter|year|\d+\s+(?:days|weeks|months|quarters|years))"
    r")\b",
    re.I,
)


_NUMBER = r"[+-]?\d+(?:\.\d+)?"
_COMPARE = r"(?:>=|<=|>|<|above|below|over|under|at\s+least|at\s+most|greater\s+than|less\s+than|more\s+than|fewer\s+than|higher\s+than|lower\s+than)"
_LINK = r"(?:(?:is|=|==|:|of|level|equal\s+to|equals)\s*)?"
_BOOLEAN_WORDS = {"true": True, "false": False, "yes": True, "no": False}
# Words that follow a field name without being its value ("status of", "priority tickets").
_FUNCTION_WORDS = frozenset(
    "a an and are as at be been by do does for from has have in into is it its not of on or per set "
    "than that the their them these this those to was were what when where which while who why will "
    "with value values field fields changed change changes count counts level levels".split()
)


class Qualifier(StrictModel):
    kind: QualifierKind
    text: str  # the question fragment (exported only as masked constraint_text)
    field: str | None = None
    value: str | None = None
    compare: bool = False  # ``field`` qualifiers: a comparison, applied by any predicate on the field


def _labels(name: str) -> list[str]:
    spaced = name.replace("_", " ")
    return sorted({name, spaced}, key=len, reverse=True)


# "by <field>", "per <field>", "for each <field>", "across <field>": the field is a group-by, not a
# qualifier, so the word after it is never that field's value.
_GROUP_CUE = re.compile(r"(?:\bby|\bper|\beach|\bacross|\bgrouped\s+by|\bbroken\s+down\s+by)\s*$", re.I)


def _field_name_spans(query: str, schema: MetadataSchema) -> list[tuple[int, int]]:
    """Where a declared field's identifier (``lines_matching_level_fatal``) occurs as one word. A
    value or field name found **inside** such a span is part of the field's name, never a qualifier
    (P11: an enum value ``fatal`` inside a numeric field's name)."""
    spans: list[tuple[int, int]] = []
    folded = query.casefold()
    for spec in schema.fields:
        if "_" not in spec.name:
            continue
        for match in re.finditer(r"(?<![\w])" + re.escape(spec.name.casefold()) + r"(?![\w])", folded):
            spans.append((match.start(), match.end()))
    return spans


def _inside(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(a <= span[0] and span[1] <= b and (a, b) != span for a, b in spans)


def _field_qualifiers(query: str, schema: MetadataSchema, units: set[str], taken: list[tuple[int, int]], names: list[tuple[int, int]] | None = None) -> list[tuple[int, Qualifier]]:
    """``field`` candidates: a filterable field's name next to a value of its type (B1). Not a
    candidate: a field name inside a longer field identifier, a field named as a group-by
    (``by service``) followed by a word that is not linked to it, and a value word that is a
    calendar expression (``by service yesterday``: ``yesterday`` is a time qualifier)."""
    names = names if names is not None else _field_name_spans(query, schema)
    times = [(m.start(), m.end()) for m in _TIME.finditer(query)]
    found: list[tuple[int, Qualifier]] = []
    for spec in schema.fields:
        if not spec.filterable or spec.type in {"datetime", "date"}:
            continue  # time qualifiers have their own kind
        if spec.type in {"integer", "number"}:
            token = _NUMBER
        elif spec.type == "boolean":
            token = r"true|false|yes|no"
        else:
            token = r"[^\W_][\w.-]*"
        for label in _labels(spec.name):
            head = _word_pattern(label)
            pattern = re.compile(head + r"\s*" + _LINK + r"(?P<cmp>" + _COMPARE + r"\s*)?(?P<val>" + token + r")(?![\w.])", re.I)
            for match in pattern.finditer(query):
                value = match.group("val")
                vspan = (match.start("val"), match.end("val"))
                if any(a < vspan[1] and vspan[0] < b for a, b in taken):
                    continue  # the value is already a ``value`` candidate
                if _inside((match.start(), match.start() + len(label)), names) or _inside(vspan, names):
                    continue  # part of a longer field identifier
                if spec.type in {"string", "id", "enum"} and any(a < vspan[1] and vspan[0] < b for a, b in times):
                    continue  # a calendar word ("yesterday") is a time qualifier, never a word value
                linked = query[match.start() + len(label) : vspan[0]].strip() != ""
                if not linked and _GROUP_CUE.search(query[: match.start()]):
                    continue  # "by <field>": a group-by, the next word is not its value
                folded = value.casefold()
                if spec.type in {"string", "id"} and (folded in _FUNCTION_WORDS or folded in units):
                    continue
                if spec.type == "enum" and folded not in {v.casefold() for v in spec.enum_values or []}:
                    continue
                taken.append(vspan)
                found.append((match.start(), Qualifier(kind="field", text=query[match.start() : match.end()], field=spec.name, value=value, compare=match.group("cmp") is not None)))
            if spec.type == "boolean":
                valued = {p for p, q in found if q.field == spec.name}
                for match in re.finditer(head, query.casefold()):
                    if _inside((match.start(), match.end()), names) or _GROUP_CUE.search(query[: match.start()]):
                        continue
                    if match.start() not in valued:
                        valued.add(match.start())
                        found.append((match.start(), Qualifier(kind="field", text=query[match.start() : match.end()], field=spec.name, compare=True)))
    return found


def _word_pattern(value: str) -> str:
    return r"(?<![^\W_])" + re.escape(value.casefold()) + r"(?![^\W_])"


def qualifier_candidates(query: str, schema: MetadataSchema, resolver: ValueResolver | None, intents: Intents) -> list[Qualifier]:
    """Deterministic candidates (see module docstring), in question order."""
    text = query.casefold()
    units = {schema.canonical_unit.casefold(), schema.canonical_unit.casefold() + "s", *GENERIC_UNITS}
    found: list[tuple[int, Qualifier]] = []
    taken: list[tuple[int, int]] = []
    universe: list[tuple[str, str]] = []
    for spec in schema.fields:
        if not spec.filterable:
            continue
        values = spec.enum_values if spec.type == "enum" else (resolver.observed(spec.name) if resolver is not None else None)
        universe.extend((spec.name, v) for v in values or [] if isinstance(v, str))
        universe.extend((spec.name, s.name) for s in spec.states)  # declared states ("open")
    names = _field_name_spans(query, schema)
    for field, value in sorted(universe, key=lambda fv: len(fv[1]), reverse=True):
        folded = value.casefold().strip()
        if len(folded) < 2 or folded in units:
            continue
        for match in re.finditer(_word_pattern(folded), text):
            span = (match.start(), match.end())
            if _inside(span, names):
                continue  # "fatal" inside "lines_matching_level_fatal" names the field, not a value
            if any(a < span[1] and span[0] < b for a, b in taken if (a, b) != span):
                continue  # inside a longer value already found
            if span not in taken:
                taken.append(span)
            found.append((match.start(), Qualifier(kind="value", text=query[span[0] : span[1]], field=field, value=value)))
    fields = _field_qualifiers(query, schema, units, taken, names)
    found.extend(fields)
    for match in _TIME.finditer(query):
        if any(f.value is not None and p <= match.start() < p + len(f.text) for p, f in fields):
            continue  # "year 2024" on an integer field is that field's qualifier, not a period
        found.append((match.start(), Qualifier(kind="time", text=match.group(0))))
    for phrase in intents.quoted_phrases:
        found.append((max(0, text.find(phrase.casefold())), Qualifier(kind="quoted", text=phrase, value=phrase)))
    found.sort(key=lambda item: item[0])
    return [q for _, q in found]


def _values(predicate: Predicate) -> list[JsonScalar]:
    if predicate.op == "in" and isinstance(predicate.value, list):
        return list(predicate.value)
    return [predicate.value] if predicate.op == "eq" else []


def _same(a: JsonScalar, b: str) -> bool:
    return isinstance(a, str) and a.casefold() == b.casefold()


def _same_typed(a: JsonScalar, b: str) -> bool:
    """A predicate value equals a question token of a ``field`` qualifier (typed comparison)."""
    folded = b.casefold()
    if isinstance(a, bool):
        return _BOOLEAN_WORDS.get(folded) is a
    if isinstance(a, (int, float)):
        try:
            return float(folded) == float(a)
        except ValueError:
            return False
    if isinstance(a, str):
        if a.casefold() == folded:
            return True
        try:
            return float(a) == float(folded)  # "3" coerced by the resolver
        except ValueError:
            return a.casefold() in _BOOLEAN_WORDS and _BOOLEAN_WORDS[a.casefold()] is _BOOLEAN_WORDS.get(folded, None)
    return False


def _is_state(schema: MetadataSchema, field: str, value: str | None) -> bool:
    """``value`` names a declared state of ``field`` (its predicate is the state's value set)."""
    spec = schema.field(field)
    return value is not None and spec is not None and any(s.name.casefold() == value.casefold() for s in spec.states)


def unapplied(candidates: Sequence[Qualifier], scope: ScopeSpec, request_filters: dict[str, JsonScalar], schema: MetadataSchema) -> list[UnsupportedConstraint]:
    """The candidates the scope (plus the caller's filters) does not express."""
    # Share numerators constrain the question too ("what percentage of X are open"); a text
    # condition carries its phrase as a value (quoted qualifiers).
    predicates = [*scope.predicates, *scope.numerator, *(Predicate(field=k, op="eq", value=v) for k, v in request_filters.items())]
    if scope.text is not None:
        predicates.append(Predicate(field="text", op="eq", value=scope.text.text))
    temporal = {f.name for f in schema.fields if f.type in {"datetime", "date"}}
    by_value: dict[str, set[str]] = {}
    for c in candidates:
        if c.kind == "value" and c.value is not None:
            by_value.setdefault(c.text.casefold(), set()).add(c.field or "")
    missing: list[UnsupportedConstraint] = []
    seen: set[str] = set()
    for c in candidates:
        if c.kind == "value":
            fields = by_value.get(c.text.casefold(), set())
            ok = any(p.field in fields and (any(_same(v, c.value or "") for v in _values(p)) or _is_state(schema, p.field, c.value)) for p in predicates)
            suggestion = f"the question names {c.field!r} = this value but the compiled scope does not constrain it; the aggregate covers a wider population"
        elif c.kind == "field":
            on_field = [p for p in predicates if p.field == c.field]
            if c.compare or c.value is None:
                ok = bool(on_field)
            else:
                ok = any(any(_same_typed(v, c.value) for v in _values(p)) for p in on_field)
            suggestion = f"the question names {c.field!r} with a value but the compiled scope does not constrain that field to it; the aggregate covers a wider population"
        elif c.kind == "time":
            ok = any(p.field in temporal for p in predicates)
            suggestion = "the question names a time period but the compiled scope has no predicate on a declared datetime/date field"
        else:
            ok = any(any(_same(v, c.text) for v in _values(p)) for p in predicates)
            suggestion = "the quoted phrase is not a value of any predicate in the compiled scope"
        key = f"{c.kind}:{c.text.casefold()}"
        if not ok and key not in seen:
            seen.add(key)
            missing.append(UnsupportedConstraint(constraint_text=c.text, reason="qualifier_not_applied", suggestion=suggestion))
    return missing


NO_OPERATOR = UnsupportedConstraint(
    constraint_text="structured scope without an aggregate operator in the question",
    reason="qualifier_not_applied",
    suggestion="the question asks no count, list, total, earliest/latest or absence; the planner's scope ignores its topic words, so its aggregate is not an exact answer to it",
)
