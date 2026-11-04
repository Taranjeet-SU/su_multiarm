"""Aggregate statements in answer text (QA-04; plan §3.9a): deterministic, English-scoped.

The ``AnswerContract`` checks the user-facing ``answer`` string, not only the model's typed
``claims``. This module finds the three statement kinds the contract binds:

* ``count``: a number (digits or an English number word up to twenty, ``dozens``, ``hundreds``)
  followed within three content words by a **unit noun** ("12 open CloudTax tickets"), an explicit
  total phrase ("a total of 12", "12 in total"), or (QQ-03, 2.0.0a3) a **total/sum statement** of an
  amount: "total/sum/combined ..." followed within six words by a copula ("is", "was", "comes to",
  "amounts to", "equals") and an amount with an optional currency symbol/code or unit ("The total
  amount for project Delta is 4,200 USD", "the sum of the invoices was $1,250.50"). Such a statement
  is bound whether or not the deployment declares a numeric field; its ``amount`` is compared with
  the cited exact aggregate like any stated count.
* ``exhaustive_list``: "complete/full/exhaustive/entire list|set", "these are all (the) <units>",
  "the only <units>", "no other <units>", "all/every <units> are listed/as follows"; when the question
  carries a ``list`` intent, also "are as follows"/"are the following" and an enumerated block of two
  or more items.
* ``absence``: "there are/were no <units>", "no <units> were found/recorded/exist", "none of the
  <units>", "zero <units>", "there are none".

**Unit nouns** are the deployment's structured targets (canonical unit and the generic record
words), a fixed list of common English record nouns (``COUNT_NOUNS``) and the nouns that follow
"how many"/"number of"/"list all|every" in the question. A number is **not** a count when it is part
of an identifier, version, date, time, decimal or percentage (``case:0005``, ``v2``, ``2.3.1``,
``2025-01-03``, ``10:30``, ``2.5``, ``40%``), when the words between it and the unit include a
function word ("set it to 2 decimal places for tickets"), or when the same number-and-unit phrase is
quoted verbatim from a supplied evidence passage (a quotation, not a corpus count). Other languages
yield no statements (the contract then relies on the typed claims alone).

Precision exclusions (2.0.0a2 release review N1; negative fixtures in
``tests/unit/test_answer_text_contract.py``):

* (QQ-02, 2.0.0a3) a number directly after a **declared field name** is that field's value, not a
  count ("priority 3 tickets", "severity 2 incidents", "tier: 1 accounts");

* a number other than one directly before a **singular** unit is a label, not a count ("a 404
  error", "the 500 error page");
* a number directly after a product-like name (a word with a capital letter that is not a sentence
  quantifier such as "Only"/"About") is a version ("Windows 10 users", "Python 3 files");
* a unit directly followed by a head noun it modifies is not the counted population ("there is no
  error message", "no error codes");
* ``the only <unit>`` is an exhaustive list only with a plural unit ("the only file you need" is
  not; "the only pending tickets are ..." is);
* "complete/full list of <noun>" is an exhaustive list only when ``<noun>`` is a unit ("the complete
  list of supported currencies" is not); a bare "that is the complete list" still is.
"""

from __future__ import annotations

import re
from typing import Literal, Sequence

from pydantic import Field

from ..decision.intent import COUNT_NOUNS
from ..models import StrictModel

LANGUAGE = "en"
StatementKind = Literal["count", "exhaustive_list", "absence"]
__all__ = ["COUNT_NOUNS", "LANGUAGE", "Statement", "StatementKind", "detect_statements", "stated_number", "units_for"]
_NUMBER_WORDS = (
    "zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
    "sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|a dozen|dozens of|hundreds of"
)
# A digit run that is not part of an identifier, version, date, time, decimal or percentage.
_DIGITS = r"(?<![\w.:#/$€£@-])(?:\d{1,3}(?:,\d{3})+|\d+)(?![\w%]|[.:/-]\d|,\d|\s*(?:%|percent\b))"
NUMBER = r"(?:" + _DIGITS + r"|\b(?:" + _NUMBER_WORDS + r")\b)"
_FUNCTION = r"(?:for|to|of|in|on|at|with|by|per|from|and|or|than|into|across|each|every|then|if|when)"
_GAP3 = r"(?:(?!" + _FUNCTION + r"\b)[\w'-]+\s+){0,3}?"
_GAP4 = r"(?:[\w'-]+\s+){0,4}?"
# QQ-03: an amount after a total/sum phrase and a copula; currency symbol/code or unit optional.
_CURRENCY = r"(?:[$€£¥]|(?:USD|EUR|GBP|JPY|CAD|AUD|CHF|INR|CNY)\b)"
_AMOUNT = (
    r"(?P<amount>" + _CURRENCY + r"?\s?(?P<digits>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(?:k|m|bn|thousand|million|billion)\b)?(?:\s?" + _CURRENCY + r"|\s+(?:dollars|euros|pounds|units|hours|days|minutes|items|records)\b)?)"
)
_TOTAL_STATEMENT = re.compile(
    r"\b(?:(?:grand\s+|combined\s+|overall\s+)?total|sum|combined\s+\w+)\b(?:\s+[\w'.-]+){0,6}?\s+"
    r"(?:is|was|are|were|comes\s+to|came\s+to|amounts\s+to|amounted\s+to|equals|equalled|totals|totalled|totaled|of)\s+(?:about\s+|approximately\s+|roughly\s+|exactly\s+)?"
    + _AMOUNT,
    re.I,
)
_QUESTION_UNIT = re.compile(r"\b(?:how many|number of|count of|list (?:all|every)|show (?:me )?(?:all|every)|are there any)\s+((?:[\w'-]+\s*){1,3})", re.I)


class Statement(StrictModel):
    kind: StatementKind
    text: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    amount: float | None = None  # a total/sum statement's stated amount (QQ-03)


def _plural(word: str) -> str:
    word = word.casefold()
    if word.endswith(("s", "x", "ch", "sh")):
        return word + "es"
    if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"


def units_for(targets: Sequence[str], question: str | None) -> list[str]:
    """Unit nouns (singular and plural) for a request: targets, ``COUNT_NOUNS`` and question nouns."""
    words: set[str] = set()
    for item in [*targets, *COUNT_NOUNS]:
        item = item.casefold().strip()
        if item and " " not in item:
            words.update({item, _plural(item)})
    if question:
        for match in _QUESTION_UNIT.finditer(question):
            for word in re.findall(r"[^\W\d_][\w'-]*", match.group(1)):
                if len(word) > 2:
                    words.update({word.casefold(), _plural(word)})
    return sorted(words, key=len, reverse=True)


def _unit_re(units: Sequence[str]) -> str:
    return r"(?P<unit>" + "|".join(re.escape(u) for u in units) + r")\b"


# Words that modify a following head noun ("error message"): the unit is then not the population.
_HEAD_NOUNS = r"\s+(?:message|messages|code|codes|handling|text|dialog|dialogs|page|pages|log|logs|banner|popup|screen|prompt|box|type|types|rate|rates)\b"
# Capitalised sentence words that may precede a count ("Only 12 tickets"); any other capitalised
# word right before a number reads as a product name with a version ("Windows 10 users").
_QUANTIFIERS = frozenset(
    "only about exactly roughly approximately nearly almost over under around just some all the there "
    "these those total and but so we i found saw of with in at least most more fewer than another".split()
)
_WORD_VALUES = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
_WORD_VALUES.update({"thirty": 30, "forty": 40, "fifty": 50, "a dozen": 12})


def _plural_unit(word: str) -> bool:
    word = word.casefold()
    return word.endswith("s") and not word.endswith(("ss", "us", "is"))


def stated_number(text: str) -> int | None:
    """The record count a ``count`` statement states (digits or a number word); ``None`` when vague
    ("dozens of", "hundreds of") or absent."""
    match = re.search(NUMBER, text, re.I)
    if match is None:
        return None
    token = match.group(0).casefold()
    if token in _WORD_VALUES:
        return _WORD_VALUES[token]
    digits = token.replace(",", "")
    return int(digits) if digits.isdigit() else None


def _number_is_label(answer: str, match: re.Match[str], kind: StatementKind, fields: re.Pattern[str] | None = None) -> bool:
    """N1 / QQ-02 precision exclusions for one candidate match (see module docstring)."""
    if kind == "count" and fields is not None and fields.search(answer[: match.start()]):
        return True  # "priority 3 tickets": the value of a declared field, not a count
    unit = match.groupdict().get("unit")
    if unit is not None and re.match(_HEAD_NOUNS, answer[match.end("unit") :], re.I):
        return True
    if kind != "count" or unit is None:
        return False
    value = stated_number(match.group(0))
    number = re.search(NUMBER, match.group(0), re.I)
    if value is not None and value != 1 and not _plural_unit(unit) and number is not None and match.start() + number.end() + 1 == match.start("unit"):
        return True  # "a 404 error": a number directly before a singular noun is a label
    before = re.search(r"(\S+)\s+$", answer[: match.start()])
    if before is not None:
        word = before.group(1)
        if any(ch.isupper() for ch in word) and word.strip(",;:").casefold() not in _QUANTIFIERS and word[-1] not in ",;:":
            return True  # "Windows 10 users": a version after a product-like name
    return False


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def detect_statements(
    answer: str,
    *,
    units: Sequence[str],
    list_intent: bool = False,
    passages: Sequence[str] = (),
    fields: Sequence[str] = (),
) -> list[Statement]:
    """Count, exhaustive-list and absence statements in ``answer`` (see module docstring).
    ``fields`` are the deployment's declared field names (QQ-02 value labels)."""
    if not answer.strip():
        return []
    unit = _unit_re(units) if units else r"(?!x)x"
    names = sorted({f.casefold().strip() for f in fields if f and f.strip()}, key=len, reverse=True)
    field_label = re.compile(r"\b(?:" + "|".join(re.escape(n).replace(r"\ ", r"[\s_]+") for n in names) + r")\s*[:=#]?\s*$", re.I) if names else None
    count_near = re.compile(NUMBER + r"\s+" + _GAP3 + unit, re.I)
    count_far = re.compile(NUMBER + r"\s+" + _GAP3.replace("{0,3}?", "{0,3}") + unit, re.I)
    patterns: list[tuple[StatementKind, re.Pattern[str]]] = [
        ("absence", re.compile(r"\bthere\s+(?:are|were|is|was)\s+(?:no|zero)\s+" + _GAP4 + unit, re.I)),
        ("absence", re.compile(r"\bno\s+" + _GAP3 + unit + r"\s+(?:were|was|are|is|have been|has been|could be)?\s*(?:found|recorded|reported|exist|exists|matched|match)\b", re.I)),
        ("absence", re.compile(r"\b(?:none|zero)\s+(?:of\s+the\s+)?" + _GAP3 + unit, re.I)),
        ("absence", re.compile(r"\bthere\s+(?:are|were|is|was)\s+none\b", re.I)),
        ("exhaustive_list", re.compile(r"\b(?:complete|full|exhaustive|entire|whole)\s+(?:list|set|listing)\b(?!\s+of\b)", re.I)),
        ("exhaustive_list", re.compile(r"\b(?:complete|full|exhaustive|entire|whole)\s+(?:list|set|listing)\s+of\s+(?:the\s+|all\s+)?" + _GAP3 + unit, re.I)),
        ("exhaustive_list", re.compile(r"\b(?:these|those|here|they)\s+are\s+(?:all|every)\s+(?:of\s+)?(?:the\s+)?" + _GAP3 + unit, re.I)),
        ("exhaustive_list", re.compile(r"\bthe\s+only\s+" + _GAP3 + unit, re.I)),
        ("exhaustive_list", re.compile(r"\bno\s+other\s+" + _GAP3 + unit, re.I)),
        ("exhaustive_list", re.compile(r"\b(?:all|every)\s+(?:of\s+the\s+)?" + _GAP3 + unit + r"\s+(?:are|were)\s+(?:listed|as follows|below|shown)\b", re.I)),
        ("count", count_near),
        ("count", re.compile(r"\b(?:a\s+total\s+of|total\s+of|totals?|totalling|totaling|altogether|count\s+(?:is|was|of))\s+" + NUMBER, re.I)),
        ("count", re.compile(NUMBER + r"\s+(?:in\s+total|altogether|overall)\b", re.I)),
    ]
    if list_intent:
        patterns.append(("exhaustive_list", re.compile(r"\b(?:are|were|include|includes)\s+(?:as\s+follows|the\s+following)\b", re.I)))
    quoted = [_normalise(p) for p in passages if p]
    found: list[Statement] = []
    for kind, pattern in patterns:
        for match in pattern.finditer(answer):
            fragment = match.group(0)
            if kind in {"count", "absence"} and any(_normalise(fragment) in passage for passage in quoted):
                continue  # quoted from a supplied passage: not a corpus count
            if any(s.start <= match.start() < s.end for s in found):
                continue  # overlaps a statement already found (absence wins over "zero <units>" as a count)
            if _number_is_label(answer, match, kind, field_label):
                # The nearest unit reads as a label ("12 open" when the question made "open" a unit
                # noun); a farther unit within the gap may still be the counted population.
                longer = count_far.match(answer, match.start()) if pattern is count_near else None
                if (
                    longer is None
                    or longer.end() <= match.end()
                    or _number_is_label(answer, longer, kind, field_label)
                    or any(_normalise(longer.group(0)) in passage for passage in quoted)
                ):
                    continue
                match, fragment = longer, longer.group(0)
            if kind == "exhaustive_list" and re.match(r"the\s+only\b", fragment, re.I) and not _plural_unit(match.group("unit")):
                continue  # "the only file you need": uniqueness of one thing, not a list
            found.append(Statement(kind=kind, text=fragment, start=match.start(), end=match.end()))
    for match in _TOTAL_STATEMENT.finditer(answer):  # QQ-03
        fragment = match.group(0)
        if any(_normalise(fragment) in passage for passage in quoted):
            continue  # quoted from a supplied passage
        found = [s for s in found if not (match.start() <= s.start < match.end())]  # the total subsumes a count inside it
        if any(s.start <= match.start() < s.end for s in found):
            continue
        amount = float(match.group("digits").replace(",", ""))
        found.append(Statement(kind="count", text=fragment, start=match.start(), end=match.end(), amount=amount))
    if list_intent:
        items = list(re.finditer(r"(?m)^\s*(?:\d+[.)]|[-*•])\s+\S", answer)) or list(re.finditer(r"(?:^|\s)\d+[.)]\s+\S", answer))
        if len(items) >= 2:
            found.append(Statement(kind="exhaustive_list", text="enumerated list", start=items[0].start(), end=len(answer)))
    found.sort(key=lambda s: s.start)
    return found
