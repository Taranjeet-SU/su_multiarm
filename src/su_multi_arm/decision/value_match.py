"""Documented value normalization for agent-written filter values (2026-11-09, corpus-B B5 q020/q041).

A planner or the SQL writer often names an observed value in another spelling: ``LLMs`` for the
stored ``llms``, ``Market Place`` for ``market_place``, ``tickets`` for ``ticket``, or the
distinctive word ``Ledgerly`` of the stored ``market_place__ledgerly`` (synthetic names). ``match_value`` grounds such a
value against the **enumerable universe** of one field (declared enum values, or the ACL-scoped
observed values of a listable field under its catalog cap) with a fixed rule table, applied in order;
the first rule that finds a candidate decides:

1. ``exact``: the value is a stored value.
2. ``casefold``: the field declares ``casefold_match`` and the casefolded value equals a stored
   value's casefold (unchanged behaviour: the backend compares casefolded).
3. ``normalized``: equal after NFKC, casefold, and every run of whitespace, punctuation or ``_``
   read as one space (``"Market-Place"`` = ``"market_place"``).
4. ``plural``: equal after (3) and a per-word plural fold (``-ies`` -> ``-y``; a final ``-s`` of a
   word longer than three letters that does not end in ``-ss`` is dropped).
5. ``token``: the value is one word of at least four letters that is, after (4), one ``_``/punctuation
   separated part of exactly **one** stored multi-part value and of no other stored value
   (the distinctive word, as ``qualifiers._token_qualifiers``).

Nothing else matches: no edit distance, no prefix, no substring inside a word, no synonym list. A
rule that finds two or more stored values is **ambiguous** and grounds nothing (the caller reports
it as an explicit unsupported constraint, never picks one). A match by rules 2-5 is a canonical
replacement: the stored value is what executes, and the caller records ``(as written, stored,
rule)``.

``question_mentions`` / ``field_named`` support the one cross-field rule (``ValueResolver``
``question`` grounding): an agent value that no rule grounds on the field it was written for may
move to the one other listable field that grounds it, only when the value is a word sequence of the
user's own question and the question does not name the original field.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal, Sequence

MatchRule = Literal["exact", "casefold", "normalized", "plural", "token"]
_SEPARATORS = re.compile(r"[\W_]+", re.UNICODE)


def normalize(text: str) -> str:
    """Rule 3: NFKC, casefold, separators (whitespace, punctuation, ``_``) as one space."""
    return " ".join(part for part in _SEPARATORS.split(unicodedata.normalize("NFKC", text).casefold()) if part)


def singular(word: str) -> str:
    """Rule 4's per-word plural fold (documented; never a dictionary lookup)."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def folded(text: str) -> tuple[str, ...]:
    return tuple(singular(word) for word in normalize(text).split())


@dataclass
class ValueMatch:
    """``value`` is the stored value when ``rule`` is set; ``ambiguous`` lists the stored values a
    rule found when it found more than one (then nothing is grounded)."""

    value: str | None = None
    rule: MatchRule | None = None
    ambiguous: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.value is not None and self.rule is not None


def match_value(value: str, universe: Sequence[object], *, casefold: bool) -> ValueMatch:
    """Ground ``value`` against ``universe`` (module docstring). Non-string members are ignored."""
    stored = [u for u in universe if isinstance(u, str)]
    if value in stored:
        return ValueMatch(value=value, rule="exact")
    if casefold:
        lowered = value.casefold()
        hit = next((u for u in stored if u.casefold() == lowered), None)
        if hit is not None:
            return ValueMatch(value=hit, rule="casefold")
    target = normalize(value)
    if not target:
        return ValueMatch()
    for rule, key in (("normalized", normalize), ("plural", folded)):
        wanted = key(value)
        hits = list(dict.fromkeys(u for u in stored if key(u) == wanted))
        if len(hits) == 1:
            return ValueMatch(value=hits[0], rule=rule)  # type: ignore[arg-type]
        if len(hits) > 1:
            return ValueMatch(ambiguous=hits)
    words = folded(value)
    if len(words) != 1 or len(words[0]) < 4 or not words[0].isalpha():
        return ValueMatch()
    word = words[0]
    owners = list(dict.fromkeys(u for u in stored if word in folded(u)))
    if len(owners) == 1 and len(folded(owners[0])) >= 2:
        return ValueMatch(value=owners[0], rule="token")
    if len(owners) > 1:
        return ValueMatch(ambiguous=owners)
    return ValueMatch()


def question_mentions(question: str, value: str) -> bool:
    """``value`` (rule 4 form) occurs as a whole word sequence of ``question`` (rule 4 form)."""
    words = folded(value)
    text = folded(question)
    if not words or len(words) > len(text):
        return False
    return any(text[i : i + len(words)] == words for i in range(len(text) - len(words) + 1))


def field_named(question: str, field_name: str) -> bool:
    """The question names the field: its identifier read as words (``kg_outcome`` -> ``kg outcome``),
    or any of its words of at least four letters (``organization`` in "organization Ledgerly")."""
    words = folded(field_name)
    if question_mentions(question, field_name):
        return True
    text = set(folded(question))
    return any(len(w) >= 4 and w in text for w in words)
