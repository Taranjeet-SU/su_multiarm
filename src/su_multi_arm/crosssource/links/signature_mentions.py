"""Error-signature mentions in free text (unified merge plan §6.9, tier B; increment M3, lane LD).

A **signature** is a keyword-rule id of the log member (``crawler.fetch_timeout``); its **family**
is the cross-day group of every episode of that rule, addressed as ``family:<rule>``. The catalog
is read from the pinned log documents themselves (``signature`` and ``incident`` rows carry
``signature_id`` and the rule title), never from configuration.

A text mentions a signature when it contains

* the rule id as a token (``rule_id``) — not inside a longer identifier (``crawler.timeouts`` and
  ``x.crawler.timeout`` do not mention ``crawler.timeout``), or
* the rule's title as a whole phrase, case- and whitespace-insensitive (``title``), when the title
  has at least ``MIN_TITLE_TOKENS`` words.

No stemming, no synonyms, no similarity: anything looser is tier C (M9). Keyword-epoch splits
(one rule superseded by two) are not expanded here: a ticket naming the old rule matches only the
episodes of the old rule.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

FAMILY_PREFIX = "family:"
MIN_TITLE_TOKENS = 2
SIGNATURE_KINDS = ("signature", "incident", "signature_day")


def family_id(rule: str) -> str:
    return FAMILY_PREFIX + rule


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


@dataclass(frozen=True)
class Mention:
    rule: str
    how: str  # rule_id | title
    field: str


@dataclass
class SignatureCatalog:
    """Rule id -> title (latest document wins) and the precompiled matchers."""

    titles: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_documents(cls, documents: Iterable[Any]) -> "SignatureCatalog":
        titles: dict[str, str] = {}
        for d in documents:
            if getattr(d, "kind", None) not in SIGNATURE_KINDS:
                continue
            rule = (d.metadata or {}).get("signature_id")
            if isinstance(rule, str) and rule:
                if d.kind == "signature" or rule not in titles:
                    titles[rule] = d.title or titles.get(rule, "")
        return cls(dict(sorted(titles.items())))

    def __post_init__(self) -> None:
        self._compile()

    def _compile(self) -> None:
        self._ids = {rule: re.compile(r"(?<![\w.\-:/])" + re.escape(rule) + r"(?![\w\-]|\.\w)") for rule in self.titles}
        self._titles = {
            rule: re.compile(r"(?<!\w)" + re.escape(_norm(title)) + r"(?!\w)")
            for rule, title in self.titles.items()
            if len(_norm(title).split()) >= MIN_TITLE_TOKENS
        }

    def __contains__(self, rule: str) -> bool:
        return rule in self.titles

    def families(self) -> set[str]:
        return {family_id(r) for r in self.titles}

    def mentions(self, texts: dict[str, str]) -> list[Mention]:
        """Every rule mentioned in ``texts`` (field -> text): a rule id beats a title; the first
        field (in the given order) that mentions the rule is reported."""
        found: dict[str, Mention] = {}
        for name, text in texts.items():
            if not text:
                continue
            for rule, pattern in self._ids.items():
                if rule not in found and pattern.search(text):
                    found[rule] = Mention(rule, "rule_id", name)
        for name, text in texts.items():
            if not text:
                continue
            normalized = _norm(text)
            for rule, pattern in self._titles.items():
                if rule not in found and pattern.search(normalized):
                    found[rule] = Mention(rule, "title", name)
        return [found[r] for r in sorted(found)]
