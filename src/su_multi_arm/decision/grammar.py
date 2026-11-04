"""Deterministic query grammar of the ``rules`` agent (plan §3.7; 2.0.0a3 query compiler).

Turns an aggregate question into a ``ScopeDraft`` from the deployment's ``MetadataSchema`` alone: no
domain vocabulary lives here. Field names (underscores read as spaces, plurals accepted), declared
``time_basis`` labels, declared ``states``, enum values and observed values of listable fields are
the only words that bind. English only; every recognised fragment is consumed and whatever content
word is left over is returned as residual (the agent refuses it: never a wider, guessed scope).

Recognised operations (each maps onto ``ScopeDraft``/``ScopeSpec`` fields):

* **windows**: calendar expressions (``decision/windows.py``) on the time basis (a named
  ``time_basis`` label or temporal field, else the only one declared); ``between X and Y`` / ``from X
  to Y`` is one window; two or more windows with ``vs``/``versus``/``compared to``/``compare`` are a
  **period comparison**;
* **group-by / cross-tab**: ``by|per|for each|across|broken down by|grouped by|distribution of``
  a groupable field (a second one after ``and`` makes a cross-tab); ``by month|year|day``,
  ``monthly``, ``yearly``, ``daily`` group the time basis with that granularity;
* **ranking**: ``which <field> has the most|fewest ...``, ``(second|3rd ...) most common <field>``,
  ``top N <fields>``, ``least common``: groups ranked by their measure, ``nth`` / ``top_n`` with ties
  reported by the store;
* **list**: ``list``/``show all`` (records shown with the total), ``top|first N <unit> by <field>``,
  ``N most recent|latest|newest|oldest <unit>``, ``N <unit> with the highest|lowest <field>``;
* **measures**: ``average|mean|min|max|minimum|maximum|highest|lowest|total|sum of <numeric field>``
  (``median`` is refused); records without a value are reported, never imputed;
* **distinct**: ``distinct|unique|different <field>`` and ``how many <field plural>``; ``distinct
  <unit>`` counts the ``equivalence_field`` when one is declared;
* **share**: ``percentage|percent|share|proportion|fraction|ratio``: with a group-by each group's
  share; otherwise the conditions after the share's verb (``are``, ``have``, ``mention`` ...) are the
  numerator and the rest the denominator;
* **values / states / comparisons**: declared state names (``open``), enum and observed values,
  ``not|excluding|except <value>`` (negated), several values of one field (``in``), a numeric field
  next to a number or comparison (``priority 3``, ``amount above 10``), a bare boolean field;
* **text**: a quoted phrase that is not a field value, or ``mentioning|containing|about|regarding
  <words>``: a literal lexical whole-corpus match (``ScopeSpec.text``), labelled with its basis.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..core.types import FieldSpec, JsonScalar, Measure, MetadataSchema, Predicate, Sort, TextMatch, UnsupportedConstraint
from .intent import GENERIC_UNITS
from .scope import ScopeDraft, WindowDraft
from .values import ValueResolver
from .windows import Window, WindowError, find_windows

WORD = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)
_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5, "6th": 6, "7th": 7, "8th": 8, "9th": 9, "10th": 10,
}  # fmt: skip
_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twenty": 20}
_ORD = r"(?:" + "|".join(sorted(_ORDINALS, key=len, reverse=True)) + r")"
_N = r"(?:\d{1,4}|" + "|".join(_NUMBERS) + r")"
_DIRECTION = {"most": "desc", "highest": "desc", "largest": "desc", "biggest": "desc", "greatest": "desc", "fewest": "asc", "least": "asc", "lowest": "asc", "smallest": "asc"}
_MEASURES = {
    "average": "avg", "avg": "avg", "mean": "avg", "minimum": "min", "min": "min", "maximum": "max", "max": "max",
    "highest": "max", "lowest": "min", "largest": "max", "smallest": "min", "total": "sum", "sum of": "sum", "combined": "sum", "median": "median",
}  # fmt: skip
_GRANULARITY = {"day": "day", "days": "day", "daily": "day", "month": "month", "months": "month", "monthly": "month", "year": "year", "years": "year", "yearly": "year", "annually": "year", "annual": "year"}
_NEGATION = re.compile(r"(?:\bnot|\bexcluding|\bexcept(?:\s+for)?|\bother\s+than|\bwithout|\bnon)[\s-]+$")
_COMPARE = {"above": ">", "over": ">", "more than": ">", "greater than": ">", "higher than": ">", ">": ">", "at least": ">=", ">=": ">=",
            "below": "<", "under": "<", "less than": "<", "fewer than": "<", "lower than": "<", "<": "<", "at most": "<=", "<=": "<=", "=": "=", "is": "=", "of": "="}  # fmt: skip
_TOPIC_STOP = frozenset("a an the any word words term terms phrase text topic topics".split())
_TOPIC_END = frozenset(
    "in during since before after by per and or from for with that which where when between within over until this last past created updated".split()
)
# Grammar vocabulary: consumed implicitly (never residual) once the question is an aggregate.
GRAMMAR_WORDS = frozenset(
    """by per each across broken breakdown break down grouped group split distribution top rank ranking ranked most
    least fewest common frequent popular active used reported highest lowest largest smallest biggest greatest
    average avg mean minimum maximum min max sum combined total distinct unique different percentage percent share
    proportion fraction ratio vs versus compared compare comparison against versus between to until through than
    mention mentions mentioned mentioning contain contains contained containing about regarding referencing refer
    refers referred referring related relating involving monthly yearly daily annually annual month months year
    years day days week weeks so far overall altogether currently still now ever one ones has have had got gets saw
    sees not excluding except other without non above over below under greater higher lower fewer more less at
    equal equals value values breakdowns trend trends""".split()
)


# Qualifiers the residual check skips as grammar vocabulary; flagged when no operation consumed them.
_FREE_ORDINAL = re.compile(r"(?<![\w-])(?:" + "|".join(sorted((o for o in _ORDINALS if _ORDINALS[o] > 1), key=len, reverse=True)) + r")(?![\w-])")
_STATE_WORDS = re.compile(r"(?<![\w-])(?:in)?active(?![\w-])")
_FREE_COMPARISON = re.compile(
    r"(?:>=|<=|>|<|=|(?<![\w-])(?:at\s+least|at\s+most|more\s+than|fewer\s+than|less\s+than|greater\s+than|higher\s+than|lower\s+than|above|below|over|under))\s*\d+(?:\.\d+)?(?![\w.])"
)


@dataclass
class GrammarResult:
    draft: ScopeDraft
    residual: list[str] = field(default_factory=list)
    unsupported: list[UnsupportedConstraint] = field(default_factory=list)
    operations: list[str] = field(default_factory=list)  # recognised operations, for the trace


def _flag(text: str, reason: str, suggestion: str) -> UnsupportedConstraint:
    return UnsupportedConstraint(constraint_text=text, reason=reason, suggestion=suggestion)  # type: ignore[arg-type]


def _plural(word: str) -> str:
    if word.endswith("y") and len(word) > 2 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    if word.endswith(("s", "x", "ch", "sh")):
        return word + "es"
    return word + "s"


def _number(text: str) -> int | None:
    text = text.casefold()
    if text.isdigit():
        return int(text)
    return _NUMBERS.get(text)


class _Text:
    """The lower-cased question with consumed spans."""

    def __init__(self, query: str):
        self.raw = query
        self.low = query.casefold()
        self.taken: list[tuple[int, int]] = []

    def free(self, start: int, end: int) -> bool:
        return not any(a < end and start < b for a, b in self.taken)

    def take(self, start: int, end: int) -> None:
        self.taken.append((start, end))

    def finditer(self, pattern: re.Pattern[str]):
        for match in pattern.finditer(self.low):
            if self.free(match.start(), match.end()):
                yield match

    def search(self, pattern: re.Pattern[str]) -> re.Match[str] | None:
        return next(self.finditer(pattern), None)

    def residual_words(self) -> list[tuple[int, str]]:
        words = []
        for match in WORD.finditer(self.low):
            if self.free(match.start(), match.end()):
                words.append((match.start(), match.group(0)))
        return words


class QueryGrammar:
    def __init__(self, schema: MetadataSchema, resolver: ValueResolver, anchor: date | None, skip_fields: set[str]):
        self.schema = schema
        self.resolver = resolver
        self.anchor = anchor
        self.skip = skip_fields
        self.units = {schema.canonical_unit.casefold(), _plural(schema.canonical_unit.casefold()), *GENERIC_UNITS}
        self.labels: dict[str, str] = {}  # label form -> field
        for spec in schema.fields:
            spaced = spec.name.replace("_", " ").casefold()
            for form in {spec.name.casefold(), spaced, _plural(spaced)}:
                self.labels.setdefault(form, spec.name)
        self.plural_labels = {_plural(spec.name.replace("_", " ").casefold()): spec.name for spec in schema.fields}
        self.temporal = [f for f in schema.fields if f.type in {"datetime", "date"} and f.time_basis]

    # --- helpers ---------------------------------------------------------------------------------

    def _alternation(self, forms: list[str]) -> str:
        if not forms:
            return r"(?!x)x"
        return r"(?:" + "|".join(re.escape(f) for f in sorted(set(forms), key=len, reverse=True)) + r")"

    def _field_forms(self, predicate) -> list[str]:
        return [form for form, name in self.labels.items() if predicate(self.schema.field(name))]

    def _group_forms(self) -> list[str]:
        forms = self._field_forms(lambda s: s is not None and s.groupable and s.type not in {"datetime", "date"})
        if self._basis_groupable():
            forms.extend(_GRANULARITY)
        return forms

    def _basis_groupable(self) -> bool:
        return any(f.groupable for f in self.temporal)

    def _unit_forms(self) -> str:
        return self._alternation(sorted(self.units))

    # --- entry point -----------------------------------------------------------------------------

    def parse(self, query: str, kinds: list[str]) -> GrammarResult:
        text = _Text(query)
        draft = ScopeDraft()
        result = GrammarResult(draft=draft)
        numerator_from = self._share_split(text, result)
        self._quoted(text, result, numerator_from)
        self._windows(text, result)
        self._topic(text, result, numerator_from)
        self._list(text, result)
        self._grouping(text, result)
        self._measure(text, result)
        self._distinct(text, result)
        self._values(text, result, numerator_from)
        self._comparisons(text, result, numerator_from)
        share = "share_cue" in result.operations
        if share and (draft.group_by or draft.periods):
            draft.with_share = bool(draft.group_by)
            if draft.text is not None and draft.text.role == "numerator":
                draft.text = draft.text.model_copy(update={"role": "scope"})
            if draft.periods:
                result.unsupported.append(_flag("share", "measure_unsupported", "a share of a period comparison is not supported; ask each period separately"))
        elif share:
            if numerator_from is None or (not draft.numerator and (draft.text is None or draft.text.role != "numerator")):
                result.unsupported.append(_flag("share", "measure_unsupported", "a share needs a condition after its verb (the numerator), e.g. 'what percentage of X are Y'"))
            elif draft.measure is None:
                draft.measure = Measure(kind="share")
                result.operations.append("share")
        units = self.units
        field_words = {w for spec in self.schema.fields for w in WORD.findall(spec.name.replace("_", " ").casefold())}
        field_words |= {_plural(w) for w in field_words}
        label_words = {w for spec in self.temporal for w in WORD.findall((spec.time_basis or "").casefold())}
        from .rules import STOP_WORDS  # the agent's stop list (shared)

        self._dropped_qualifiers(text, result)
        for _, word in text.residual_words():
            if word in STOP_WORDS or word in GRAMMAR_WORDS or word in units or word in field_words or word in label_words:
                continue
            if word.isdigit() or word in _NUMBERS or word in _ORDINALS:
                continue
            result.residual.append(word)
        return result

    # --- qualifiers no operation consumed ------------------------------------------------------------

    def _dropped_qualifiers(self, text: _Text, result: GrammarResult) -> None:
        """Qualifier words that the residual check would otherwise skip as grammar vocabulary but
        that no recognised operation consumed (2.0.0a3 parity fix: the rules path returned an
        ``exact`` count that ignored them). Each is ``qualifier_not_applied``; the agent refuses the
        scope instead of counting a wider population:

        * an ordinal other than ``first`` ("who is second for X") outside a ranking;
        * a state word (``active``, ``inactive``) that is not a declared state or value;
        * a comparison with a number ("with >= 20 records", "at least 5") bound to no field."""
        for match in text.finditer(_FREE_ORDINAL):
            result.unsupported.append(_flag(match.group(0), "qualifier_not_applied", "an ordinal outside a group ranking ('second most common <field>'); the scope would ignore it"))
        for match in text.finditer(_STATE_WORDS):
            result.unsupported.append(
                _flag(match.group(0), "qualifier_not_applied", "a state word that is not a declared state or value of any field; declare it under the field's states")
            )
        for match in text.finditer(_FREE_COMPARISON):
            result.unsupported.append(_flag(match.group(0), "qualifier_not_applied", "a numeric comparison bound to no declared field; the scope would ignore it"))

    # --- share ------------------------------------------------------------------------------------

    def _share_split(self, text: _Text, result: GrammarResult) -> int | None:
        cue = re.search(r"\b(?:percentage|percent|share|proportion|fraction|ratio)\b|%", text.low)
        if cue is None:
            return None
        text.take(cue.start(), cue.end())
        verb = re.compile(r"\b(?:are|is|were|was|have|has|had|that|which|who|got|get|mention\w*|contain\w*|reference\w*|involv\w*)\b")
        after = verb.search(text.low, cue.end())
        result.operations.append("share_cue")
        return after.start() if after else None

    # --- text --------------------------------------------------------------------------------------

    def _is_value(self, phrase: str) -> bool:
        folded = phrase.casefold().strip()
        for spec in self.schema.fields:
            if not spec.filterable:
                continue
            if any(s.name.casefold() == folded for s in spec.states):
                return True
            universe = spec.enum_values if spec.type == "enum" else (self.resolver.observed(spec.name) or [])
            if any(isinstance(v, str) and v.casefold() == folded for v in universe):
                return True
        return False

    def _set_text(self, result: GrammarResult, match: TextMatch, fragment: str) -> None:
        if result.draft.text is not None:
            result.unsupported.append(_flag(fragment, "measure_unsupported", "one text condition per scope; combine the words into one phrase or ask separately"))
            return
        result.draft.text = match
        result.operations.append(f"text_{match.kind}")

    def _quoted(self, text: _Text, result: GrammarResult, numerator_from: int | None) -> None:
        pattern = re.compile(r"\"([^\"]{2,200})\"|“([^”]{2,200})”|(?<![\w])'([^']{2,200})'(?![\w])")
        for match in text.finditer(pattern):
            phrase = next(g for g in match.groups() if g)
            if self._is_value(phrase):
                continue  # a field value: bound by ``_values``
            text.take(match.start(), match.end())
            role = "numerator" if numerator_from is not None and match.start() >= numerator_from else "scope"
            self._set_text(result, TextMatch(kind="phrase", text=phrase.strip(), role=role), phrase)  # type: ignore[arg-type]

    def _topic(self, text: _Text, result: GrammarResult, numerator_from: int | None) -> None:
        # "<word>-mention <unit>" (2.0.0a3 parity fix): the word before the hyphen is the topic; the
        # cue below never starts inside a hyphenated word ("invoice-mention rate" is not "mention rate").
        compound = re.compile(r"(?<![\w-])(?P<w>[^\W_]+)-(?:mention(?:s|ed|ing)?|related|containing)(?![\w-])")
        for match in text.finditer(compound):
            word = match.group("w")
            if word in GRAMMAR_WORDS or word in self.units or self._is_value(word):
                continue
            text.take(match.start(), match.end())
            role = "numerator" if numerator_from is not None and match.start() >= numerator_from - 1 else "scope"
            self._set_text(result, TextMatch(kind="all_terms", text=text.raw[match.start("w") : match.end("w")], role=role), word)  # type: ignore[arg-type]
            return
        cue = re.compile(
            r"(?<![\w-])(?:that\s+|which\s+|who\s+)?(?:mention(?:s|ed|ing)?|contain(?:s|ed|ing)?|about|regarding|referencing|"
            r"refer(?:s|red|ring)?\s+to|related\s+to|relating\s+to|involving)\s+"
        )
        from .rules import STOP_WORDS  # the agent's stop list (shared)

        for match in text.finditer(cue):
            words: list[tuple[int, int, str]] = []
            for word in WORD.finditer(text.low, match.end()):
                between = text.low[(words[-1][1] if words else match.end()) : word.start()]
                if re.search(r"[?.!,;:]", between) or not text.free(word.start(), word.end()) or word.group(0) in _TOPIC_END:
                    break
                words.append((word.start(), word.end(), word.group(0)))
                if len(words) >= 6:
                    break
            content = [w for w in words if w[2] not in _TOPIC_STOP]
            if not content or all(w[2] in GRAMMAR_WORDS or w[2] in STOP_WORDS or w[2] in self.units for w in content):
                continue  # "contain the most active records": grammar words, not a topic
            text.take(match.start(), words[-1][1])
            role = "numerator" if numerator_from is not None and match.start() >= numerator_from - 1 else "scope"
            phrase = " ".join(text.raw[a:b] for a, b, _ in content)
            self._set_text(result, TextMatch(kind="all_terms", text=phrase, role=role), phrase)  # type: ignore[arg-type]
            return

    # --- time -----------------------------------------------------------------------------------

    def _named(self, text: _Text) -> list[FieldSpec]:
        """Temporal ``time_basis`` fields the question names (field name or basis label)."""
        named = []
        for spec in self.temporal:
            forms = {spec.name.casefold(), spec.name.replace("_", " ").casefold(), (spec.time_basis or "").casefold()}
            if any(f and re.search(r"(?<![^\W_])" + re.escape(f) + r"(?![^\W_])", text.low) for f in forms):
                named.append(spec)
        return named

    def _basis(self, text: _Text, result: GrammarResult) -> FieldSpec | None:
        named = self._named(text)
        if len(named) == 1:
            return named[0]
        if not named and len(self.temporal) == 1:
            return self.temporal[0]
        result.unsupported.append(
            _flag("time basis", "measure_unsupported", "name the date the window applies to: " + ", ".join(f"{f.time_basis} ({f.name})" for f in self.temporal) if self.temporal else "declare a datetime/date field with time_basis")
        )
        return None

    def _windows(self, text: _Text, result: GrammarResult) -> None:
        try:
            windows = [w for w in find_windows(text.raw, self.anchor) if text.free(w.begin, w.finish)]
        except WindowError as error:
            result.unsupported.append(_flag("time window", "unparseable_bound", str(error)))
            return
        if not windows:
            return
        basis = self._basis(text, result)
        for window in windows:
            text.take(window.begin, window.finish)
        if basis is None:
            return
        draft = result.draft
        draft.time_basis = basis.name
        compare = re.search(r"\b(?:vs\.?|versus|compared?\s+(?:to|with)|compare|comparison|against)\b", text.low)
        if len(windows) >= 2 and compare:
            draft.periods = [WindowDraft(field=basis.name, expression=w.text, label=w.label) for w in windows]
            text.take(compare.start(), compare.end())
            result.operations.append("compare_periods")
            return
        if len(windows) == 2:
            first, second = windows
            between = text.low[first.finish : second.begin]
            before = text.low[: first.begin]
            if re.fullmatch(r"\s*(?:and|to|until|through|thru|-)\s*", between) and re.search(r"\b(?:between|from)\s*$", before):
                span = Window(start=first.start, end=second.end, label=f"{first.label} to {second.label}", text=f"{first.text} to {second.text}")
                if span.end <= span.start:
                    result.unsupported.append(_flag(span.text, "unparseable_bound", "the window ends before it starts"))
                    return
                draft.predicates.append(self._span_predicate(basis, span))
                result.operations.append("window")
                return
        if len(windows) > 1:
            result.unsupported.append(_flag(" / ".join(w.text for w in windows), "unparseable_bound", "several time windows without 'between ... and ...' or a comparison ('vs')"))
            return
        draft.windows = [WindowDraft(field=basis.name, expression=windows[0].text, label=windows[0].label)]
        result.operations.append("window")

    def _span_predicate(self, basis: FieldSpec, span: Window) -> Predicate:
        """``between X and Y`` as one ``date_interval`` (the resolver re-reads its calendar bounds;
        the upper bound is the last day included)."""
        from ..backends._scope import zone_of
        from .windows import bounds

        last = span.model_copy(update={"start": span.end - timedelta(days=1), "end": span.end})
        lower, _ = bounds(span, basis.type, zone_of(self.schema))
        upper, _ = bounds(last, basis.type, zone_of(self.schema))
        if basis.type == "datetime" and basis.timezone_policy == "require_offset" and self.schema.timezone is None:
            lower, upper = span.start.isoformat(), last.start.isoformat()  # calendar-shaped: the resolver refuses it (timezone_missing)
        return Predicate(field=basis.name, op="date_interval", lower=lower, upper=upper, granularity="day")

    # --- grouping, ranking, list --------------------------------------------------------------------

    def _group_field(self, form: str, result: GrammarResult, text: _Text) -> str | None:
        form = form.casefold()
        if form in _GRANULARITY:
            groupable = [f for f in self.temporal if f.groupable]
            if result.draft.time_basis is not None:
                groupable = [f for f in groupable if f.name == result.draft.time_basis] or groupable
            elif len(groupable) > 1:
                named = {f.name for f in self._named(text)}
                groupable = [f for f in groupable if f.name in named] or groupable
            if len(groupable) != 1:
                result.unsupported.append(_flag(f"by {form}", "measure_unsupported", "a calendar grouping needs exactly one groupable time_basis field (name it)"))
                return None
            granularity = _GRANULARITY[form]
            if result.draft.granularity not in {None, granularity}:
                result.unsupported.append(_flag(f"by {form}", "measure_unsupported", "one calendar granularity per scope"))
                return None
            result.draft.granularity = granularity  # type: ignore[assignment]
            result.draft.time_basis = result.draft.time_basis or groupable[0].name
            return groupable[0].name
        return self.labels.get(form)

    def _add_group(self, name: str | None, result: GrammarResult, fragment: str) -> None:
        if name is None:
            return
        if name in result.draft.group_by:
            return
        if len(result.draft.group_by) >= 2:
            result.unsupported.append(_flag(fragment, "measure_unsupported", "at most two group-by fields (a cross-tab)"))
            return
        result.draft.group_by.append(name)

    def _grouping(self, text: _Text, result: GrammarResult) -> None:
        groups = self._alternation(self._group_forms())
        draft = result.draft
        rank_which = re.compile(
            r"\b(?:which|what)\s+(?P<f>" + groups + r")\s+(?:has|have|had|got|gets|saw|sees|is|was|were|are|with)\s+(?:the\s+)?(?P<ord>" + _ORD + r"\s+)?(?P<dir>most|fewest|least|highest|lowest|largest|smallest|biggest|greatest)\b"
        )
        rank_common = re.compile(
            r"\b(?:the\s+)?(?P<ord>" + _ORD + r"\s+)?(?P<dir>most|least)\s+(?:common|frequent|popular|active|used|reported)\s+(?P<f>" + groups + r")\b"
        )
        rank_top = re.compile(r"\b(?:top|(?P<n2>" + _N + r")\s+most\s+(?:common|frequent|popular|active))\s+(?:(?P<n>" + _N + r")\s+)?(?P<f>" + groups + r")\b")
        for pattern, kind in ((rank_which, "which"), (rank_common, "common"), (rank_top, "top")):
            for match in text.finditer(pattern):
                name = self._group_field(match.group("f"), result, text)
                if name is None:
                    continue
                if kind == "top":
                    n = _number(match.group("n") or match.group("n2") or "")
                    if n is None:
                        draft.nth = 1
                    else:
                        draft.top_n = n
                    draft.order = "desc"
                else:
                    ordinal = _ORDINALS.get((match.group("ord") or "").strip(), 1)
                    draft.nth = ordinal
                    draft.order = _DIRECTION[match.group("dir")]  # type: ignore[assignment]
                    after = text.low[match.end() :]
                    numeric = self._alternation(self._field_forms(lambda s: s is not None and s.type in {"integer", "number"}))
                    if re.match(r"\s+(?:the\s+)?" + numeric + r"\b", after):
                        result.unsupported.append(
                            _flag(match.group(0), "measure_unsupported", "say how the field is aggregated per group (average, total, maximum ...)")
                        )
                self._add_group(name, result, match.group(0))
                text.take(match.start(), match.end())
                result.operations.append("rank")
        by = re.compile(
            r"\b(?:by|per|for\s+each|each|across|broken\s+down\s+by|break(?:ing)?\s+(?:it\s+)?down\s+by|breakdown\s+by|grouped\s+by|split\s+by|distribution\s+(?:of|by|across))\s+"
            r"(?:the\s+|their\s+|its\s+)?(?P<f>" + groups + r")\b(?:\s*(?:,|and|&|/|x)\s*(?:by\s+|per\s+)?(?:the\s+)?(?P<g>" + groups + r")\b)?"
        )
        for match in text.finditer(by):
            for key in ("f", "g"):
                if match.group(key):
                    self._add_group(self._group_field(match.group(key), result, text), result, match.group(0))
            text.take(match.start(), match.end())
            result.operations.append("group_by")
        bare = re.compile(r"\b(?P<f>monthly|yearly|daily|annually|month[- ]by[- ]month|year[- ]by[- ]year|over\s+time|trend(?:s|ing)?)\b")
        if self._basis_groupable():
            for match in text.finditer(bare):
                word = match.group("f").split()[0].split("-")[0]
                form = {"over": "month", "trend": "month", "trends": "month", "trending": "month"}.get(word, word)
                self._add_group(self._group_field(form, result, text), result, match.group(0))
                text.take(match.start(), match.end())
                result.operations.append("group_by")
        if draft.periods and draft.group_by:
            result.unsupported.append(_flag("comparison with grouping", "measure_unsupported", "a period comparison is its own grouping; ask the breakdown per period separately"))

    def _list(self, text: _Text, result: GrammarResult) -> None:
        draft = result.draft
        if draft.group_by or draft.periods:
            return
        unit = self._unit_forms()
        sortable = self._alternation(self._field_forms(lambda s: s is not None and s.sortable))
        top_by = re.compile(r"\b(?:top|first)\s+(?P<n>" + _N + r")\s+(?:" + unit + r"\s+)?(?:by|on|ranked\s+by|sorted\s+by|ordered\s+by)\s+(?:the\s+)?(?:(?P<dir>highest|lowest|most|least|largest|smallest)\s+)?(?P<s>" + sortable + r")\b")
        recent = re.compile(r"\b(?P<n>" + _N + r")\s+(?P<dir>most\s+recent|latest|newest|oldest|earliest)\s+(?:" + unit + r")?|\b(?P<dir2>latest|newest|oldest|earliest|most\s+recent|last|first)\s+(?P<n2>" + _N + r")\s+(?:" + unit + r")")
        with_most = re.compile(r"\b(?P<n>" + _N + r")\s+(?:" + unit + r")\s+with\s+the\s+(?P<dir>highest|lowest|most|fewest|least|largest|smallest)\s+(?P<s>" + sortable + r")\b")
        match = text.search(top_by)
        if match is not None:
            draft.sort = Sort(field=self.labels[match.group("s")], descending=_DIRECTION.get(match.group("dir") or "highest", "desc") == "desc")
            draft.limit = _number(match.group("n"))
            text.take(match.start(), match.end())
            result.operations.append("list_sorted")
            return
        match = text.search(with_most)
        if match is not None:
            draft.sort = Sort(field=self.labels[match.group("s")], descending=_DIRECTION[match.group("dir")] == "desc")
            draft.limit = _number(match.group("n"))
            text.take(match.start(), match.end())
            result.operations.append("list_sorted")
            return
        match = text.search(recent)
        if match is not None:
            direction = (match.group("dir") or match.group("dir2") or "").split()[-1]
            n = _number(match.group("n") or match.group("n2") or "")
            basis = [f for f in self.temporal if f.sortable]
            if draft.time_basis:
                basis = [f for f in basis if f.name == draft.time_basis]
            if len(basis) != 1:
                result.unsupported.append(_flag(match.group(0), "measure_unsupported", "most recent/oldest needs exactly one sortable time_basis field"))
            else:
                draft.sort = Sort(field=basis[0].name, descending=direction in {"recent", "latest", "newest", "last"})
                draft.limit = n
                draft.time_basis = draft.time_basis or basis[0].name
                result.operations.append("list_sorted")
            text.take(match.start(), match.end())

    # --- measures ----------------------------------------------------------------------------------

    def _measure(self, text: _Text, result: GrammarResult) -> None:
        numeric = self._alternation(self._field_forms(lambda s: s is not None and s.type in {"integer", "number"}))
        functions = r"(?:" + "|".join(sorted((re.escape(k) for k in _MEASURES), key=len, reverse=True)) + r")"
        pattern = re.compile(r"\b(?P<fn>" + functions + r")\s+(?:of\s+)?(?:the\s+)?(?:(?:number|count)\s+of\s+)?(?P<f>" + numeric + r")\b")
        for match in text.finditer(pattern):
            kind = _MEASURES[match.group("fn")]
            text.take(match.start(), match.end())
            if kind == "median":
                result.unsupported.append(_flag(match.group(0), "measure_unsupported", "median is not a supported measure; it is never replaced by an average"))
                return
            if result.draft.measure is not None:
                result.unsupported.append(_flag(match.group(0), "measure_unsupported", "one measure per scope"))
                return
            result.draft.measure = Measure(kind=kind, field=self.labels[match.group("f")])  # type: ignore[arg-type]
            result.operations.append(kind)

    def _distinct(self, text: _Text, result: GrammarResult) -> None:
        forms = self._alternation([*self.labels, *self.units])
        pattern = re.compile(r"\b(?:distinct|unique|different)\s+(?:number\s+of\s+)?(?P<f>" + forms + r")\b")
        how_many = re.compile(r"\bhow\s+many\s+(?P<f>" + self._alternation(list(self.plural_labels)) + r")\b(?!\s+(?:" + self._unit_forms() + r")\b)")
        match = text.search(pattern) or text.search(how_many)
        if match is None:
            return
        form = match.group("f")
        if form in self.units:
            unit = self.schema.equivalence_field or self.schema.canonical_unit
        else:
            unit = self.labels.get(form) or self.plural_labels.get(form)  # type: ignore[assignment]
        if unit in self.skip:
            return
        if result.draft.measure is not None:
            result.unsupported.append(_flag(match.group(0), "measure_unsupported", "one measure per scope"))
            return
        result.draft.measure = Measure(kind="count_distinct", unit=unit)
        start = match.start("f") if match.re is how_many else match.start()
        text.take(start, match.end())
        result.operations.append("count_distinct")

    # --- values ------------------------------------------------------------------------------------

    def _bind(self, result: GrammarResult, predicate: Predicate, start: int, numerator_from: int | None) -> None:
        target = result.draft.numerator if numerator_from is not None and start >= numerator_from and not result.draft.group_by else result.draft.predicates
        target.append(predicate)

    def _values(self, text: _Text, result: GrammarResult, numerator_from: int | None) -> None:
        """States, enum and observed values verbatim in the question (word-bounded, longest first).
        Several values of one field are one ``in``; a negation word before a value negates it."""
        found: dict[tuple[str, bool, bool], list[tuple[int, JsonScalar]]] = {}  # (field, negate, numerator) -> values
        candidates: list[tuple[str, str, bool]] = []  # (field, value, is_state)
        for spec in self.schema.fields:
            if not spec.filterable or spec.name in self.skip:
                continue
            candidates.extend((spec.name, s.name, True) for s in spec.states)
            universe = spec.enum_values if spec.type == "enum" else (self.resolver.observed(spec.name) or [])
            candidates.extend((spec.name, v, False) for v in universe if isinstance(v, str))
        for name, value, is_state in sorted(candidates, key=lambda c: len(c[1]), reverse=True):
            folded = value.casefold().strip()
            if len(folded) < 2 or folded in self.units:
                continue
            pattern = re.compile(r"(?<![^\W_])" + re.escape(folded) + r"(?![^\W_])")
            for match in text.finditer(pattern):
                negation = _NEGATION.search(text.low[max(0, match.start() - 24) : match.start()])
                start = match.start() - (len(negation.group(0)) if negation else 0)
                text.take(start, match.end())
                numerator = numerator_from is not None and match.start() >= numerator_from and not result.draft.group_by
                found.setdefault((name, bool(negation), numerator), []).append((match.start(), value))
        for (name, negate, numerator), values in found.items():
            distinct = list(dict.fromkeys(v for _, v in sorted(values, key=lambda item: item[0])))
            spec = self.schema.field(name)
            states = {s.name.casefold() for s in spec.states} if spec is not None else set()
            if len(distinct) == 1:
                predicate = Predicate(field=name, op="eq", value=distinct[0], negate=negate)
            elif any(isinstance(v, str) and v.casefold() in states for v in distinct):
                result.unsupported.append(_flag(" / ".join(str(v) for v in distinct), "measure_unsupported", "a state cannot be combined with other values of the same field"))
                continue
            else:
                predicate = Predicate(field=name, op="in", value=distinct, negate=negate)
            (result.draft.numerator if numerator else result.draft.predicates).append(predicate)

    def _comparisons(self, text: _Text, result: GrammarResult, numerator_from: int | None) -> None:
        """A numeric field next to a number (``priority 3``, ``amount above 10``) and a bare boolean field."""
        numeric = self._field_forms(lambda s: s is not None and s.type in {"integer", "number"} and s.filterable and s.name not in self.skip)
        if numeric:
            ops = r"(?:" + "|".join(sorted((re.escape(k) for k in _COMPARE), key=len, reverse=True)) + r")"
            pattern = re.compile(r"\b(?P<f>" + self._alternation(numeric) + r")\s*(?:is\s+|=\s*|:\s*|of\s+)?(?P<cmp>" + ops + r")?\s*(?P<n>[+-]?\d+(?:\.\d+)?)(?![\w.])")
            for match in text.finditer(pattern):
                name = self.labels[match.group("f")]
                number: float | int = float(match.group("n")) if "." in match.group("n") else int(match.group("n"))
                op = _COMPARE.get((match.group("cmp") or "=").strip(), "=")
                if op == "=":
                    predicate = Predicate(field=name, op="eq", value=number)
                elif op in {">", ">="}:
                    predicate = Predicate(field=name, op="range", lower=number, inclusive=op == ">=")
                else:
                    predicate = Predicate(field=name, op="range", upper=number, inclusive=op == "<=")
                if predicate.op == "range" and not predicate.inclusive and op in {">", "<"}:
                    pass  # exclusive bound: Predicate.inclusive applies to its one bound
                text.take(match.start(), match.end())
                self._bind(result, predicate, match.start(), numerator_from)
                result.operations.append("comparison")
        booleans = self._field_forms(lambda s: s is not None and s.type == "boolean" and s.filterable and s.name not in self.skip)
        if booleans:
            pattern = re.compile(r"(?P<neg>\bnot\s+|\bnon-?)?\b(?P<f>" + self._alternation(booleans) + r")\b")
            for match in text.finditer(pattern):
                text.take(match.start(), match.end())
                self._bind(result, Predicate(field=self.labels[match.group("f")], op="eq", value=not match.group("neg")), match.start(), numerator_from)
