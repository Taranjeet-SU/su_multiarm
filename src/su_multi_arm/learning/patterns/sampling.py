"""Deterministic prompt diversity and independent validation partitions for pattern learning.

Frequency remains a ranking signal; repeated text does not consume the prompt budget. Selection
balances tenant, partition day and message shape using explicit attributes, never inferred client
names from a message. Holdout selection must run before mining: its messages are not proposal input.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from typing import Sequence

from .bootstrap import LogLine


def _key(line: LogLine) -> str:
    return hashlib.sha256((line.text + "\0" + line.id).encode()).hexdigest()


def _day(line: LogLine) -> str:
    return line.attributes.get("date", line.attributes.get("partition_key", ""))[:10]


def _shape(text: str) -> str:
    # Only a diversity feature, never a regex or extraction result.
    return re.sub(r"\b(?:[0-9a-fA-F]{8,}|\d+)\b", "<value>", text)


@dataclass(frozen=True)
class SampleSelection:
    lines: list[LogLine]
    input_count: int
    unique_texts: int

    def summary(self) -> dict[str, int]:
        return {"input": self.input_count, "unique_texts": self.unique_texts, "selected": len(self.lines),
                "tenants": len({line.attributes["tenant"] for line in self.lines if line.attributes.get("tenant")}),
                "days": len({_day(line) for line in self.lines if _day(line)}),
                "variants": len({_shape(line.text) for line in self.lines})}


def balanced_sample(lines: Sequence[LogLine], limit: int) -> SampleSelection:
    """Select distinct text, favouring underrepresented tenant/day/shape buckets.

    Stable hash tie-breaking avoids taking the first tenant in input order. Duplicate messages can
    belong to different clients; their representative is chosen during balancing, not in advance.
    Missing dimensions are a single unknown bucket and are not invented.
    """
    if limit < 1:
        raise ValueError("sample limit must be positive")
    candidates = sorted(lines, key=_key)
    selected: list[LogLine] = []
    seen: set[str] = set()
    counts: list[Counter[str]] = [Counter(), Counter(), Counter()]
    features = {line.id: (line.attributes.get("tenant", ""), _day(line), _shape(line.text)) for line in candidates}
    while len(selected) < limit:
        remaining = [line for line in candidates if line.text not in seen]
        if not remaining:
            break
        def rank(line: LogLine):
            hits = tuple(counts[n][value] for n, value in enumerate(features[line.id]))
            return sum(hits), max(hits), hits, _key(line)
        chosen = min(remaining, key=rank)
        selected.append(chosen)
        seen.add(chosen.text)
        for n, value in enumerate(features[chosen.id]):
            counts[n][value] += 1
    return SampleSelection(selected, len(lines), len({line.text for line in lines}))


def contrast_sample(cluster: Sequence[LogLine], others: Sequence[LogLine], limit: int) -> list[LogLine]:
    """Nearby nonmember messages for negative examples; their text is never invented.

    Keep the service/severity fixed so an irrelevant other service does not make the task easier.
    Rank token overlap, deduplicate text, then stable hash; zero-overlap lines are not sent.
    """
    if not cluster or limit < 1:
        return []
    scope = {(line.attributes.get("service"), line.attributes.get("level")) for line in cluster}
    texts = {line.text for line in cluster}
    words = set().union(*(set(re.findall(r"[A-Za-z_]+", line.text.lower())) for line in cluster))
    ranked = []
    for line in others:
        if line.text in texts or (line.attributes.get("service"), line.attributes.get("level")) not in scope:
            continue
        terms = set(re.findall(r"[A-Za-z_]+", line.text.lower()))
        overlap = len(words & terms)
        if overlap:
            ranked.append((-overlap / max(1, len(words | terms)), _key(line), line))
    out = []
    for _, _, line in sorted(ranked, key=lambda item: item[:2]):
        if line.text not in texts:
            out.append(line)
            texts.add(line.text)
        if len(out) >= limit:
            break
    return out


@dataclass(frozen=True)
class ValidationSplit:
    training: list[LogLine]
    validation: list[LogLine]
    axis: str | None
    excluded_overlap: int = 0


def split_validation(lines: Sequence[LogLine], *, min_train_texts: int = 3) -> ValidationSplit:
    """Reserve the latest explicit day, otherwise one tenant, before calling a learner.

    An exact message occurring in validation is removed from training, so duplicates cannot leak
    the held-out answer. If no independent split leaves enough distinct training examples, return
    no validation with ``axis=None``; callers must not claim held-out validation succeeded.
    This helper should be called separately per service.
    """
    for axis, value in (("date", _day), ("tenant", lambda line: line.attributes.get("tenant", ""))):
        groups = sorted({value(line) for line in lines if value(line)})
        if len(groups) < 2:
            continue
        for held in reversed(groups):
            validation = [line for line in lines if value(line) == held]
            held_text = {line.text for line in validation}
            training = [line for line in lines if value(line) != held and line.text not in held_text]
            if len({line.text for line in training}) >= min_train_texts:
                excluded = sum(value(line) != held and line.text in held_text for line in lines)
                return ValidationSplit(training, validation, axis, excluded)
            if axis == "date":
                break  # Never switch the claimed time holdout to an older, easier day.
    return ValidationSplit(list(lines), [], None)
