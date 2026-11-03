"""Predicate support: is a relation's predicate actually evidenced inside its grounded span?

A verbatim quote proves that the subject and object co-occur; it does not prove the predicate. A
title such as "Austria at the 1988 Winter Olympics" contains both endpoints of ``hosted`` without
supporting it. A relation is ``trigger_in_span`` only when every content stem of the predicate, or of
a declared trigger phrase, occurs in the span and no negation marker lies between the trigger and the
endpoints. Anything else is ``unchecked`` and is stored as a separate ``co_occurrence`` edge, never as
a fact. This is a conservative structural check, not entailment.
"""

from __future__ import annotations

import re
from typing import Literal

import snowballstemmer

Support = Literal["trigger_in_span", "unchecked"]

_TOKEN = re.compile(r"\w+", re.UNICODE)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# Function words that carry no predicate content ("resolved_by", "is located in").
_FUNCTION_WORDS = {"a", "an", "the", "is", "are", "was", "were", "be", "been", "by", "of", "in", "at", "to", "for", "on", "with", "from", "as"}
_NEGATION = {"not", "no", "never", "none", "neither", "nor", "without", "cannot"}


def languages() -> list[str]:
    return sorted(snowballstemmer.algorithms())


class PredicateSupport:
    def __init__(self, language: str = "english", triggers: dict[str, list[str]] | None = None):
        self._stem = snowballstemmer.stemmer(language).stemWord
        self._triggers = {
            predicate.casefold(): [self.stems(phrase) for phrase in phrases if phrase.strip()]
            for predicate, phrases in (triggers or {}).items()
        }

    def tokens(self, text: str) -> list[tuple[str, int]]:
        """``(stem, char_offset)`` for each word; ``n't`` is split out so negation is visible."""
        text = re.sub(r"n't\b", " not", text)
        return [(self._stem(m.group().casefold()), m.start()) for m in _TOKEN.finditer(text)]

    def stems(self, text: str) -> list[str]:
        return [stem for stem, _ in self.tokens(text)]

    def candidates(self, predicate: str) -> list[list[str]]:
        words = _CAMEL.sub(" ", predicate).replace("_", " ").replace("-", " ")
        content = [stem for stem in self.stems(words) if stem not in _FUNCTION_WORDS]
        own = content or self.stems(words)
        return [own, *self._triggers.get(predicate.casefold(), [])] if own else list(self._triggers.get(predicate.casefold(), []))

    def check(
        self,
        predicate: str,
        span_text: str,
        subject: tuple[int, int],
        object_: tuple[int, int],
    ) -> Support:
        """``subject``/``object_`` are character offsets of the endpoints relative to ``span_text``."""
        tokens = self.tokens(span_text)
        if not tokens:
            return "unchecked"
        positions: dict[str, list[int]] = {}
        for stem, offset in tokens:
            positions.setdefault(stem, []).append(offset)
        for sequence in self.candidates(predicate):
            if not sequence or not all(stem in positions for stem in sequence):
                continue
            trigger_start = min(positions[stem][0] for stem in sequence)
            trigger_end = max(positions[stem][-1] for stem in sequence)
            low = min(subject[0], object_[0], trigger_start)
            high = max(subject[1], object_[1], trigger_end)
            negated = any(stem in _NEGATION and low <= offset <= high for stem, offset in tokens)
            if not negated:
                return "trigger_in_span"
        return "unchecked"
