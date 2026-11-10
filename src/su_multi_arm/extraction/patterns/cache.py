"""Bounded, in-memory exact head-match reuse; never removes source occurrences.

Python's hash table verifies equality of the entire key after hashing. Equal lengths,
prefixes or hash values alone cannot cause two different messages to share a result.
"""
from __future__ import annotations

import sys
from collections import Counter, OrderedDict
from copy import deepcopy
from typing import Any


def _clone(value: Any) -> Any:
    """Copy the small JSON-like result tree without copying immutable strings/scalars."""
    if isinstance(value, dict):
        return {k: _clone(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clone(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_clone(v) for v in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return deepcopy(value)


def _size(value: Any, seen: set[int] | None = None) -> int:
    """Conservative per-entry retained-object charge, including Unicode text and fields."""
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(_size(k, seen) + _size(v, seen) for k, v in value.items())
    elif isinstance(value, (tuple, list)):
        size += sum(_size(v, seen) for v in value)
    return size


class ExactMatchCache:
    """One parser/worker's LRU, scoped to its compiled library and accounting policy.

    Limits bound cached entries and their charged bytes, not the parser's output arrays.
    Stored results exclude occurrence IDs and are copied so callers cannot mutate the cache.
    Timeouts are never cached. Exceptions propagate before any result can be stored.
    """

    def __init__(self, max_entries: int = 4096, max_bytes: int = 16 * 1024 * 1024, *, admission_hits: int = 2):
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.admission_hits = admission_hits
        # Fingerprints are only an admission hint, never a match/reuse decision. A collision can
        # admit an unnecessary result, but full tuple equality is still required to reuse it.
        self.seen: OrderedDict[int, None] = OrderedDict()
        self.entries: OrderedDict[tuple[Any, ...], tuple[dict[str, Any], int]] = OrderedDict()
        self.bytes = 0
        self.counts: Counter[str] = Counter()
        self.library: Any = None
        self.scope: tuple[Any, ...] | None = None
        self.misses = 0
        self.cooldown = 0

    def bind(self, library: Any, policy: Any = None) -> None:
        scope = (getattr(library, "identity", None), getattr(library, "max_line_bytes", None), policy.to_json() if policy is not None else None)
        if self.library is not library or self.scope != scope:
            self.entries.clear()
            self.seen.clear()
            self.bytes = 0
            self.counts.clear()
            self.misses = self.cooldown = 0
            self.library, self.scope = library, scope

    def should_lookup(self) -> bool:
        # Unique-heavy streams should not pay for copying/sizing thousands of unused results.
        # Probe again periodically so a later repetitive region can benefit. This only bypasses
        # caching: every bypass still performs the normal match, with no occurrence skipped.
        if self.cooldown or self.max_entries <= 0 or self.max_bytes <= 0:
            self.cooldown = max(0, self.cooldown - 1)
            self.counts["evaluations"] += 1
            self.counts["bypasses"] += 1
            return False
        return True

    def get(self, key: tuple[Any, ...], *, copy_result: bool = True) -> dict[str, Any] | None:
        found = self.entries.get(key)
        if found is None:
            self.counts["evaluations"] += 1
            self.misses += 1
            if self.misses >= 128:
                self.cooldown, self.misses = 4096, 0
            return None
        self.misses = 0
        self.entries.move_to_end(key)
        self.counts["reuses"] += 1
        # The body matcher can reconstruct fresh PatternHits directly from this private payload.
        return _clone(found[0]) if copy_result else found[0]

    def put(self, key: tuple[Any, ...], row: dict[str, Any]) -> None:
        if row["status"] == "timeout" or self.cooldown or self.max_entries <= 0 or self.max_bytes <= 0:
            return
        if self.admission_hits > 1:
            fingerprint = hash(key)
            if fingerprint not in self.seen:
                if len(self.seen) >= self.max_entries:
                    self.seen.popitem(last=False)
                self.seen[fingerprint] = None
                self.counts["admission_skips"] += 1
                return
            self.seen.move_to_end(fingerprint)
        template = {k: v for k, v in row.items() if k != "id"}
        charge = _size((key, template)) + 256  # dictionary/LRU bookkeeping allowance
        if charge > self.max_bytes:
            self.counts["oversized"] += 1
            return
        previous = self.entries.pop(key, None)
        if previous is not None:
            self.bytes -= previous[1]
        while self.entries and (len(self.entries) >= self.max_entries or self.bytes + charge > self.max_bytes):
            _, (_, size) = self.entries.popitem(last=False)
            self.bytes -= size
            self.counts["evictions"] += 1
        self.entries[key] = (_clone(template), charge)
        self.bytes += charge
