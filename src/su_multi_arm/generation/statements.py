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
* (QA-20261109-07) a singular unit in the possessive owns the following noun and is not a count
  ("this is one ticket's resolution", "a record's owner"); a plural possessive ("12 tickets'
  owners") still states 12 records;
* ``the only <unit>`` is an exhaustive list only with a plural unit ("the only file you need" is
  not; "the only pending tickets are ..." is);
* "complete/full list of <noun>" is an exhaustive list only when ``<noun>`` is a unit ("the complete
  list of supported currencies" is not); a bare "that is the complete list" still is.

Disclaimers (2026-11-08 parity fix; ``tests/unit/test_parity_fixes_contract.py``): a completeness
phrase that the sentence **denies** or only **advises** is not an exhaustive-list statement: a
negation within six words before it in the same sentence ("this is not a complete list", "no
complete list of the 35 records is available", "does not claim they are the full set") or an
advice sentence (one that starts with an imperative such as "Re-run", "Filter", "Export", "Check",
or "To get/confirm/see ...", or a phrase "to get/obtain/confirm/see/export the full list"). Neither
claims that the answer's list is complete. A completeness phrase without such a denial is still an
exhaustive-list statement. Also not statements: a unit used as a field label ("Zero match: false"),
a double negative ("no zero match") and a property of counted records ("6 tickets have no priority
recorded": the records have no value, which is a count, not an absence of records).
"""

from __future__ import annotations

import re
from typing import Literal, Sequence

from pydantic import Field

from ..decision.intent import COUNT_NOUNS
from ..models import StrictModel

LANGUAGE = "en"
StatementKind = Literal["count", "exhaustive_list", "absence"]
__all__ = ["COUNT_NOUNS", "LANGUAGE", "Statement", "StatementKind", "completeness_disclaimed", "detect_statements", "population_frame", "stated_number", "units_for"]
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


# Function words and auxiliaries after "how many" that are not the counted noun ("How many are
# classified as X?": "are" is no unit; 2026-11-08 parity fix).
_QUESTION_FILLER = frozenset(
    "are were was is have has had did does there the with from for and been being they them these those that which who "
    "currently still also already ever exactly".split()
)


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
                if len(word) > 2 and word.casefold() not in _QUESTION_FILLER:
                    words.update({word.casefold(), _plural(word)})
    return sorted(words, key=len, reverse=True)


def _unit_re(units: Sequence[str]) -> str:
    return r"(?P<unit>" + "|".join(re.escape(u) for u in units) + r")\b"


# Words that modify a following head noun ("error message"): the unit is then not the population.
# ``passage``/``excerpt``/``snippet``: the supplied evidence, not the corpus ("none of the ticket
# passages mention X" is about the retrieved text, not an absence of tickets).
_HEAD_NOUNS = (
    r"\s+(?:message|messages|code|codes|handling|text|dialog|dialogs|page|pages|log|logs|banner|popup|screen|prompt|box|type|types|rate|rates|"
    r"passage|passages|excerpt|excerpts|snippet|snippets)\b"
)
# A singular possessive ("ticket's", "record’s"; QA-20261109-07): the unit owns the following noun.
_POSSESSIVE = re.compile(r"['’]s\b", re.I)
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


# A number stated as the population of a ratio, not as a count (2.0.0a3 parity fix): "out of 2048
# records", "a denominator of 2,048", "denominator: 2048", "12 of 36 tickets", "12/36 tickets",
# "40% of 2048 records", "2,048 records as the denominator". The contract does not compare such a
# number with the cited aggregate's count; the count in the same sentence is still compared.
_POPULATION_BEFORE = re.compile(
    r"(?:\bout\s+of|\bdenominator(?:\s+(?:is|was|of|reported\s+is))?\s*[:=]?|\bdenominator(?:\s+[\w'-]+){1,4}?\s*(?:[:=]|\bis\b|\bwas\b)|\b(?:ratio|share|proportion|fraction|percentage)\s+of|"
    r"(?:%|\bpercent|\bper\s*cent)\s+of|\b(?:\d{1,3}(?:,\d{3})+|\d+)\s+(?:out\s+)?of|\bper|/)"
    r"\s*(?:(?:a|an|the|all|its|their)\s+)?(?:(?:total|reported|stated)\s+)?(?:denominator\s+of\s+)?(?:(?:about|roughly|approximately|nearly)\s+)?$",
    re.I,
)
_POPULATION_AFTER = re.compile(r"^\s*(?:[\w'-]+\s+){0,4}?(?:as|in|for)\s+(?:the|its|a|their)\s+(?:[\w'-]+\s+){0,2}?denominator\b", re.I)


def population_frame(answer: str, start: int, end: int) -> bool:
    """The count statement ``answer[start:end]`` names a ratio's population (see ``_POPULATION_BEFORE``)."""
    return bool(_POPULATION_BEFORE.search(answer[max(0, start - 64) : start]) or _POPULATION_AFTER.match(answer[end : end + 96]))


def _number_is_label(answer: str, match: re.Match[str], kind: StatementKind, fields: re.Pattern[str] | None = None) -> bool:
    """N1 / QQ-02 precision exclusions for one candidate match (see module docstring)."""
    if kind == "count" and fields is not None and fields.search(answer[: match.start()]):
        return True  # "priority 3 tickets": the value of a declared field, not a count
    unit = match.groupdict().get("unit")
    if unit is not None and re.match(_HEAD_NOUNS, answer[match.end("unit") :], re.I):
        return True
    if unit is not None and not _plural_unit(unit) and _POSSESSIVE.match(answer[match.end("unit") :]):
        return True  # QA-07: "one ticket's resolution" names a property of one record, not a count
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


# A completeness phrase that the sentence denies (2026-11-08 parity fix): a negation within six
# words before the phrase, in the same sentence.
_DENIAL = re.compile(
    r"\b(?:not(?!\s+only)|no|never|nor|cannot|can't|isn't|aren't|wasn't|weren't|doesn't|don't|didn't|won't|without)\b"
    r"(?:[\s,]+[\w'*-]+){0,6}?[\s,*]*$",
    re.I,
)
# ... or right after it: "the full list is not available", "the complete set cannot be shown".
_DENIAL_AFTER = re.compile(
    r"^(?:\s+[\w'-]+){0,4}?\s+(?:(?:is|are|was|were)\s+(?:not|unavailable\b)\s*(?:available|provided|included|shown|given|listed|supplied|known|possible|part|in)?\b"
    r"|(?:isn't|aren't|wasn't|weren't)\b|cannot\s+be\s+(?:given|shown|listed|provided|stated|confirmed)\b)",
    re.I,
)
_ADVISORY = re.compile(r"\b(?:advice|advisory|suggest(?:s|ed|ion)?|recommend(?:s|ed|ation)?)\b", re.I)
# Advice: an imperative sentence start ("Re-run ...", "To get the full list, ...") or an advisory
# "to <verb> the full list/set" right before the phrase.
_ADVICE_START = re.compile(
    r"^[\s*_#>\-]*(?:\(?\s*advice[^)]*\)?\s*[:.-]?\s*)?(?:re-?run|run|filter|export|query|check|verify|confirm|request|pull|use|"
    r"to\s+(?:get|obtain|confirm|see|view|export|produce|retrieve|build|list|count))\b",
    re.I,
)
_ADVICE_BEFORE = re.compile(r"\b(?:to|and)\s+(?:get|obtain|confirm|see|view|export|produce|retrieve|build|list|request)\s+(?:the\s+|a\s+)?$|\bagainst\s+(?:the\s+)?$", re.I)
_SENTENCE_START = re.compile(r"(?:[.!?](?=\s)|\n)[^.!?\n]*$")
# Hypothetical or advisory completeness (2026-11-09 corpus-B fix F2): "getting the full list would
# require a records export", "the complete list could be pulled with ...": a modal of possibility
# right after the phrase; and an advisory gerund start ("Getting/Obtaining the full list requires
# ...") together with a requirement verb after it. A parenthetical advice label after the phrase
# ("the full list (advice: export ...)") is advice too. A plain "this is the full list" stays a claim.
_HYPOTHETICAL_AFTER = re.compile(
    r"^(?:\s+[\w'-]+){0,4}?\s+(?:would|could|might)\s+(?:only\s+)?(?:require|need|mean|take|involve|be\s+(?:available|possible|obtained|produced|pulled|exported))\b",
    re.I,
)
_GERUND_START = re.compile(r"^[\s*_#>\-]*(?:getting|obtaining|seeing|viewing|producing|retrieving|exporting|pulling|building|confirming|listing)\b", re.I)
_REQUIREMENT_AFTER = re.compile(r"^[^.!?\n]{0,60}?\b(?:requires?|needs?|would|will|calls\s+for|means)\b", re.I)
_ADVICE_LABEL_AFTER = re.compile(r"^[^.!?\n]{0,80}?\(\s*(?:advice|advisory|suggestion|recommendation)\b", re.I)


def completeness_disclaimed(answer: str, start: int, end: int | None = None) -> bool:
    """The exhaustive-list phrase ``answer[start:end]`` is denied or only advised by its sentence
    (see the module docstring): it does not claim that a list in the answer is complete."""
    head = answer[:start]
    boundary = _SENTENCE_START.search(head)
    sentence = head[boundary.start() + 1 :] if boundary else head
    if _DENIAL.search(sentence[-120:]) or _ADVICE_START.match(sentence) or _ADVICE_BEFORE.search(sentence[-60:]) or _ADVISORY.search(sentence):
        return True
    if end is not None:
        tail = re.match(r"[^.!?\n]*", answer[end:])
        rest = tail.group(0) if tail else ""
        if _HYPOTHETICAL_AFTER.match(rest) or _ADVICE_LABEL_AFTER.match(rest):
            return True
        if _GERUND_START.match(sentence) and _REQUIREMENT_AFTER.match(rest):
            return True
        return bool(_DENIAL_AFTER.match(rest))
    return False


def _label_or_property(answer: str, match: re.Match[str], kind: StatementKind, units: str) -> bool:
    """An absence/count fragment that is a field label ("Zero match: false"), a double negative
    ("no zero match") or a property of counted records ("tickets have no priority recorded")."""
    if kind != "absence":
        return False
    if re.match(r"\s*[:=]\s*(?:true|false|yes|no)\b", answer[match.end() :], re.I):
        return True
    before = answer[max(0, match.start() - 80) : match.start()]
    if re.search(r"\bno\s+$", before, re.I) and match.group(0).casefold().startswith("zero"):
        return True
    return bool(match.group(0).casefold().startswith("no") and re.search(units + r"\s+(?:that\s+|which\s+)?(?:have|has|had|with|without)\s+$", before, re.I))


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
            if kind == "exhaustive_list" and completeness_disclaimed(answer, match.start(), match.end()):
                continue  # "this is not a complete list": a disclaimer, not a completeness claim
            if kind == "absence" and _label_or_property(answer, match, kind, unit):
                continue
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
