"""Deterministic Drain-style template miner with bounded memory (addendum §3.6.1 row 1, L4a).

Lines are masked with the library ``masks`` (each mask match becomes a typed placeholder), split on
runs of spaces and routed through a fixed-depth prefix tree per ``(service, level, token count)``:
the first ``depth - 2`` tokens select internal nodes (a token holding a digit routes to the ``<*>``
child, and so does any new token once a node has ``max_children``). A leaf keeps clusters; a line
joins the most similar cluster (share of equal positions, ``>= similarity``; ties go to the oldest)
and the differing positions become ``<*>``; otherwise a new cluster starts.

**Bounds.** At most ``max_clusters`` clusters live at once; creating one more evicts the least
recently updated cluster (counted in ``evictions``). Each cluster keeps at most ``max_examples``
example lines. Lines with more than ``max_tokens`` tokens, or holding the internal placeholder
delimiters, are not mined and are counted (``skipped``). The routing tree is bounded by ``max_children``
per node over ``depth - 2`` levels per ``(service, level, length)`` key; emptied leaves are kept.
The result depends only on the input order.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from ...extraction.patterns.engine import PatternEngine

WILDCARD = "\x01*\x02"
_OPEN, _CLOSE = "\x01", "\x02"


def placeholder(mask: str) -> str:
    return f"{_OPEN}{mask}{_CLOSE}"


def is_placeholder(token: str) -> bool:
    return token.startswith(_OPEN) and token.endswith(_CLOSE) and token.count(_OPEN) == 1


@dataclass(frozen=True)
class MinerConfig:
    depth: int = 3  # tokens [0, depth-2) route; 3 = the first token only (variables in token 2 still merge)
    similarity: float = 0.5
    max_children: int = 100
    max_clusters: int = 1000
    max_examples: int = 5
    max_tokens: int = 64

    def __post_init__(self) -> None:
        if self.depth < 3 or not 0 < self.similarity <= 1 or min(self.max_children, self.max_clusters, self.max_examples, self.max_tokens) < 1:
            raise ValueError("invalid miner bounds")


@dataclass
class Cluster:
    seq: int
    service: str | None
    level: str | None
    tokens: list[str]
    size: int = 0
    examples: list[tuple[str, str]] = field(default_factory=list)  # (line id, original text)
    last_update: int = 0

    @property
    def id(self) -> str:
        key = "\0".join([self.service or "", self.level or "", " ".join(self.tokens)])
        return "c-" + hashlib.sha256(key.encode()).hexdigest()[:12]

    def template(self) -> str:
        """Readable template (``<*>`` for variables, ``<mask>`` for mask placeholders)."""
        return " ".join(t.replace(_OPEN, "<").replace(_CLOSE, ">") for t in self.tokens)


class TemplateMiner:
    def __init__(self, engine: PatternEngine, masks: dict[str, str] | None = None, config: MinerConfig | None = None):
        self.engine = engine
        self.config = config or MinerConfig()
        self.masks = [(name, engine.compile(pattern)) for name, pattern in (masks or {}).items()]
        self.root: dict[Any, Any] = {}
        self.clusters: dict[int, Cluster] = {}
        self._leaf_of: dict[int, list[int]] = {}
        self._seq = 0
        self._clock = 0
        self.evictions = 0
        self.skipped = 0
        self.lines = 0

    # masking ---------------------------------------------------------------------------------
    def mask(self, text: str) -> str:
        """Replace mask matches (earlier masks first, non-overlapping) with placeholders."""
        taken: list[tuple[int, int, str]] = []
        for name, compiled in self.masks:
            position = 0
            while position <= len(text):
                hit = self.engine.search(compiled, text[position:])
                if hit is None:
                    break
                start, end = hit.start + position, hit.end + position
                if end == start:
                    position = start + 1
                    continue
                if not any(start < e and s < end for s, e, _ in taken):
                    taken.append((start, end, name))
                position = end
        out, cursor = [], 0
        for start, end, name in sorted(taken):
            out.append(text[cursor:start])
            out.append(placeholder(name))
            cursor = end
        out.append(text[cursor:])
        return "".join(out)

    def tokens(self, text: str) -> list[str]:
        return [t for t in self.mask(text).split(" ") if t]

    # clustering ------------------------------------------------------------------------------
    def add(self, line_id: str, text: str, service: str | None = None, level: str | None = None) -> Cluster | None:
        self.lines += 1
        self._clock += 1
        if _OPEN in text or _CLOSE in text:
            self.skipped += 1
            return None
        tokens = self.tokens(text)
        if not tokens or len(tokens) > self.config.max_tokens:
            self.skipped += 1
            return None
        leaf = self._leaf(service, level, tokens)
        best, best_score = None, -1.0
        for seq in leaf:
            cluster = self.clusters[seq]
            score = self._similarity(cluster.tokens, tokens)
            if score > best_score:
                best, best_score = cluster, score
        if best is not None and best_score >= self.config.similarity:
            best.tokens = [a if a == b else WILDCARD for a, b in zip(best.tokens, tokens)]
            cluster = best
        else:
            cluster = self._create(service, level, tokens, leaf)
        cluster.size += 1
        cluster.last_update = self._clock
        if len(cluster.examples) < self.config.max_examples:
            cluster.examples.append((line_id, text))
        return cluster

    @staticmethod
    def _similarity(template: list[str], tokens: list[str]) -> float:
        same = sum(1 for a, b in zip(template, tokens) if a == b and a != WILDCARD)
        return same / len(tokens)

    def _leaf(self, service: str | None, level: str | None, tokens: list[str]) -> list[int]:
        node = self.root.setdefault((service, level, len(tokens)), {})
        for token in tokens[: self.config.depth - 2]:
            key = WILDCARD if any(c.isdigit() for c in token) and not is_placeholder(token) else token
            if key not in node and len(node) >= self.config.max_children:
                key = WILDCARD
            node = node.setdefault(key, {})
        return node.setdefault(None, [])  # the leaf list lives under the ``None`` key

    def _create(self, service: str | None, level: str | None, tokens: list[str], leaf: list[int]) -> Cluster:
        if len(self.clusters) >= self.config.max_clusters:
            victim = min(self.clusters.values(), key=lambda c: (c.last_update, c.seq))
            self._leaf_of[victim.seq].remove(victim.seq)
            del self.clusters[victim.seq]
            del self._leaf_of[victim.seq]
            self.evictions += 1
        self._seq += 1
        cluster = Cluster(self._seq, service, level, list(tokens))
        self.clusters[cluster.seq] = cluster
        leaf.append(cluster.seq)
        self._leaf_of[cluster.seq] = leaf
        return cluster

    def result(self) -> list[Cluster]:
        """Live clusters, largest first (ties by creation order)."""
        return sorted(self.clusters.values(), key=lambda c: (-c.size, c.seq))

    def stats(self) -> dict[str, int]:
        return {"lines": self.lines, "clusters": len(self.clusters), "evictions": self.evictions, "skipped": self.skipped}
