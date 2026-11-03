"""Intent pre-pass (plan §3.7): deterministic, always runs first, never fails.

Detects aggregate intent (count / how many / list all / total / earliest / latest / absence
grammar), exact identifiers (patterns supplied from the alias map), quoted phrases and follow-up
markers. The grammar is **English-only**; other languages yield no aggregate intent (and therefore
the plain ``retrieve`` intent), never a wrong one. Its output feeds every decision agent and the
``AnswerContract``: an aggregate intent without an exact ``ScopeResult`` can never become a count.
"""

from __future__ import annotations

import re
from typing import Literal, Sequence

from pydantic import Field

from ..models import StrictModel

AggregateKind = Literal["count", "total", "list", "absence", "earliest", "latest"]
LANGUAGE = "en"

# Order matters: ``count`` is checked before ``total`` so "total number of" is a count, not a sum.
# ``count`` phrases are explicit operators and fire on their own. Every other kind needs a
# **structured target** (the canonical unit, a declared field name or a ``time_basis`` label) within
# a few words of the operator: "list all open records", "total amount", "latest created record".
# Without a target the words are ordinary English ("all the steps", "the latest release notes",
# "every time the service restarts") and the question is a retrieval question.
_COUNT = re.compile(r"\b(how many|number of|total number of|count of|count the|a count)\b", re.I)
_GAP = r"(?:[\w'-]+\s+){0,3}"  # up to three words between operator and target
_OPERATORS: dict[AggregateKind, str] = {
    "total": r"(total(?!\s+number\b)|sum of|summed|altogether|add up)",
    "list": r"(list (all|every)|show (me )?(all|every)|all (of )?the|every|enumerate|exhaustive)",
    "absence": r"((is|are) there (any|no)|(does|do) any|none of|any at all)",
    "earliest": r"(earliest|first (recorded|seen|reported|known)|oldest|first time)",
    "latest": r"(latest|most recent|last (recorded|seen|reported)|newest|last time)",
}
# Schema-less fallback (``targets is None``): the permissive operators alone, for callers without a
# schema (tests, tooling). Decision agents always pass the deployment's targets.
_PERMISSIVE: tuple[tuple[AggregateKind, re.Pattern[str]], ...] = (
    ("count", _COUNT),
    *((kind, re.compile(r"\b" + body + r"\b", re.I)) for kind, body in _OPERATORS.items()),
    ("absence", re.compile(r"\bno .{1,40}? (exist|exists|recorded)\b", re.I)),
)
GENERIC_UNITS: tuple[str, ...] = ("records", "record", "documents", "document", "entries", "entry", "items", "item", "rows", "row")


def _targeted_grammar(targets: Sequence[str]) -> tuple[tuple[AggregateKind, re.Pattern[str]], ...]:
    # Targets are whole phrases ("resolved on", "created at"); they are never split into words, so a
    # field name cannot leak a function word ("on") into the grammar.
    words = sorted({t.casefold().strip() for t in targets if t and t.strip()}, key=len, reverse=True)
    if not words:
        return (("count", _COUNT),)
    target = r"(?:" + "|".join(re.escape(w) + r"s?" for w in words) + r")"
    grammar: list[tuple[AggregateKind, re.Pattern[str]]] = [("count", _COUNT)]
    for kind, body in _OPERATORS.items():
        after = r"\b" + body + r"\s+" + _GAP + r"\b" + target + r"\b"
        before = r"\b" + target + r"\b\s+" + _GAP + body + r"\b"
        grammar.append((kind, re.compile("(?:" + after + ")|(?:" + before + ")", re.I)))
    grammar.append(("absence", re.compile(r"\bno\s+" + _GAP + r"\b" + target + r"\b\s+" + _GAP + r"(exist|exists|recorded)\b", re.I)))
    return tuple(grammar)
_FOLLOW_UP = re.compile(
    r"^(and|also|then|what about|how about|same)\b|\b(the same|that one|those ones|this one|as before|the previous)\b",
    re.I,
)
_QUOTED = re.compile(r"\"([^\"]{2,200})\"|'([^']{2,200})'")


class AggregateIntent(StrictModel):
    kind: AggregateKind
    text: str = Field(min_length=1)  # the matched fragment; exported through constraint_text (masked)
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class Intents(StrictModel):
    aggregates: list[AggregateIntent] = Field(default_factory=list)
    exact_ids: list[str] = Field(default_factory=list)
    quoted_phrases: list[str] = Field(default_factory=list)
    follow_up: bool = False
    language: Literal["en"] = LANGUAGE

    def aggregate(self) -> bool:
        return bool(self.aggregates)

    def kinds(self) -> list[AggregateKind]:
        return list(dict.fromkeys(a.kind for a in self.aggregates))

    def primary(self) -> Literal["aggregate", "lookup", "retrieve"]:
        if self.aggregates:
            return "aggregate"
        if self.exact_ids:
            return "lookup"
        return "retrieve"


def detect_intents(query: str, id_patterns: Sequence[str] = (), targets: Sequence[str] | None = None) -> Intents:
    """Deterministic pre-pass. ``id_patterns`` are regexes from the alias map; ``targets`` are the
    deployment's structured targets (canonical unit, declared field names, ``time_basis`` labels).
    Nothing is inferred: an aggregate kind other than ``count`` fires only next to a target."""
    aggregates: list[AggregateIntent] = []
    grammar = _PERMISSIVE if targets is None else _targeted_grammar(targets)
    seen_kinds: set[str] = set()
    for kind, pattern in grammar:
        match = pattern.search(query)
        if match and kind not in seen_kinds:
            seen_kinds.add(kind)
            aggregates.append(AggregateIntent(kind=kind, text=match.group(0), start=match.start(), end=match.end()))
    aggregates.sort(key=lambda a: a.start)
    exact_ids: list[str] = []
    for raw in id_patterns:
        exact_ids.extend(m.group(0) for m in re.finditer(raw, query))
    quoted = [a or b for a, b in _QUOTED.findall(query)]
    return Intents(
        aggregates=aggregates,
        exact_ids=list(dict.fromkeys(exact_ids)),
        quoted_phrases=list(dict.fromkeys(q.strip() for q in quoted if q.strip())),
        follow_up=bool(_FOLLOW_UP.search(query.strip())),
    )
