"""Intent pre-pass (plan §3.7): deterministic, always runs first, never fails.

Detects aggregate intent (count / how many / list all / total / earliest / latest / absence
grammar), exact identifiers (patterns supplied from the alias map), quoted phrases and follow-up
markers. The grammar is **English-only**; other languages yield no aggregate intent (and therefore
the plain ``retrieve`` intent), never a wrong one. Its output feeds every decision agent and the
``AnswerContract``: an aggregate intent without an exact ``ScopeResult`` can never become a count.

Untargeted list operators (QQ-04, 2.0.0a3): "list all/every", "show (me) all/every" and "enumerate"
followed within three words by a common **record noun** (``COUNT_NOUNS``: ticket, case, issue ...)
that is *not* a declared structured target are recorded in ``Intents.untargeted``, not in
``aggregates``: no structured scope can answer them exactly, but the question still asks for an
exhaustive list, so the ``AnswerContract`` checks the answer text as a list answer and the decision
trace names the undeclared noun instead of claiming the question asks for no list. "List all the
steps" is neither (``steps`` is not a record noun).
"""

from __future__ import annotations

import re
from typing import Literal, Sequence

from pydantic import Field

from ..models import StrictModel, UnsupportedConstraint

AggregateKind = Literal["count", "total", "list", "absence", "earliest", "latest", "rank", "breakdown", "measure", "share", "compare"]
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
    # query-compiler operators (2.0.0a3): ranking, breakdowns, numeric measures, shares, comparisons
    "rank": r"(top \d+|top|(most|least) (common|frequent|popular|active)|(has|have|had|with) the (\w+ )?(most|fewest|least|highest|lowest)|second|third|\d+(st|nd|rd|th) most)",
    "breakdown": r"(by|per|for each|broken down by|break down by|breakdown by|grouped by|distribution of|monthly|yearly|daily)",
    "measure": r"(average|avg|mean|median|minimum|maximum)",
    "share": r"(percentage|percent|share|proportion|fraction|ratio)",
    "compare": r"(vs\.?|versus|compared (to|with)|compare)",
}
# Operators that must sit right next to their target (at most an article between): "by" and "per"
# are ordinary English otherwise ("created by the team").
_TIGHT: frozenset[str] = frozenset({"breakdown"})
# Schema-less fallback (``targets is None``): the permissive operators alone, for callers without a
# schema (tests, tooling). Decision agents always pass the deployment's targets.
_PERMISSIVE: tuple[tuple[AggregateKind, re.Pattern[str]], ...] = (
    ("count", _COUNT),
    # The query-compiler operators ("by", "top", "share" ...) are ordinary English without a target:
    # the schema-less fallback keeps the original operators only.
    *((kind, re.compile(r"\b" + body + r"\b", re.I)) for kind, body in _OPERATORS.items() if kind in {"total", "list", "absence", "earliest", "latest"}),
    ("absence", re.compile(r"\bno .{1,40}? (exist|exists|recorded)\b", re.I)),
)
GENERIC_UNITS: tuple[str, ...] = ("records", "record", "documents", "document", "entries", "entry", "items", "item", "rows", "row")
# Common English record nouns: units of a count/list statement in answer text (``generation/
# statements.py``) and the nouns of an untargeted list operator (QQ-04).
COUNT_NOUNS: tuple[str, ...] = (
    "record", "document", "entry", "entries", "item", "row", "ticket", "case", "issue", "incident",
    "report", "request", "result", "match", "matches", "article", "order", "account", "customer",
    "user", "file", "message", "event", "error", "failure", "complaint", "defect", "bug", "task",
    "submission", "transaction", "invoice", "claim", "instance", "occurrence", "hit",
)  # fmt: skip
_RECORD_NOUN = r"(?:" + "|".join(re.escape(n) for n in sorted(COUNT_NOUNS, key=len, reverse=True)) + r")(?:e?s)?"
_UNTARGETED_LIST = re.compile(
    r"\b(?:list\s+(?:all|every)|show\s+(?:me\s+)?(?:all|every)|enumerate(?:\s+(?:all|every))?)\s+(?:of\s+)?(?:the\s+)?"
    r"(?:[\w'-]+\s+){0,3}?(?P<noun>" + _RECORD_NOUN + r")\b",
    re.I,
)


def _singulars(noun: str) -> set[str]:
    forms = {noun}
    if noun.endswith("ies"):
        forms.add(noun[:-3] + "y")
    if noun.endswith("es"):
        forms.add(noun[:-2])
    if noun.endswith("s"):
        forms.add(noun[:-1])
    return forms


def _targeted_grammar(targets: Sequence[str]) -> tuple[tuple[AggregateKind, re.Pattern[str]], ...]:
    # Targets are whole phrases ("resolved on", "created at"); they are never split into words, so a
    # field name cannot leak a function word ("on") into the grammar.
    words = sorted({t.casefold().strip() for t in targets if t and t.strip()}, key=len, reverse=True)
    if not words:
        return (("count", _COUNT),)
    target = r"(?:" + "|".join(re.escape(w) + r"s?" for w in words) + r")"
    grammar: list[tuple[AggregateKind, re.Pattern[str]]] = [("count", _COUNT)]
    for kind, body in _OPERATORS.items():
        if kind in _TIGHT:
            grammar.append((kind, re.compile(r"\b" + body + r"\s+(?:the\s+|each\s+)?\b" + target + r"\b|\b(?:monthly|yearly|daily)\b(?=.*\b" + target + r"\b)", re.I)))
            continue
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
    # Explicit list operators on a record noun that is not a declared structured target (QQ-04).
    untargeted: list[AggregateIntent] = Field(default_factory=list)
    exact_ids: list[str] = Field(default_factory=list)
    quoted_phrases: list[str] = Field(default_factory=list)
    follow_up: bool = False
    language: Literal["en"] = LANGUAGE

    def aggregate(self) -> bool:
        return bool(self.aggregates)

    def kinds(self) -> list[AggregateKind]:
        return list(dict.fromkeys(a.kind for a in self.aggregates))

    def asks_aggregate(self) -> bool:
        """The question asks for a count/list/total/... answer, targeted or not (QQ-04)."""
        return bool(self.aggregates or self.untargeted)

    def asks_list(self) -> bool:
        return "list" in self.kinds() or any(u.kind == "list" for u in self.untargeted)

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
    untargeted: list[AggregateIntent] = []
    if targets is not None and "list" not in seen_kinds:
        declared = {t.casefold().strip() for t in targets}
        match = _UNTARGETED_LIST.search(query)
        if match is not None and not (_singulars(match.group("noun").casefold()) & declared):
            untargeted.append(AggregateIntent(kind="list", text=match.group(0), start=match.start(), end=match.end()))
    exact_ids: list[str] = []
    for raw in id_patterns:
        exact_ids.extend(m.group(0) for m in re.finditer(raw, query))
    quoted = [a or b for a, b in _QUOTED.findall(query)]
    return Intents(
        aggregates=aggregates,
        untargeted=untargeted,
        exact_ids=list(dict.fromkeys(exact_ids)),
        quoted_phrases=list(dict.fromkeys(q.strip() for q in quoted if q.strip())),
        follow_up=bool(_FOLLOW_UP.search(query.strip())),
    )


def untargeted_constraint(intent: AggregateIntent) -> UnsupportedConstraint:
    """QQ-04: the question asks for a list of a record noun that is not a declared structured
    target; recorded on the decision and the answer instead of the generic "no operator" reason."""
    return UnsupportedConstraint(
        constraint_text=intent.text,
        reason="aggregate_intent_without_scope",
        suggestion=(
            "the question asks for an exhaustive list of a record noun that is not a declared structured target "
            "(canonical unit or field); no typed scope can list it exactly"
        ),
    )
