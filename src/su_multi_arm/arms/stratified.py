"""Stratified candidate pools of a partitioned collection (docs/plans/unified_merge.md §6.5; M1a, LA).

A **stratum** owns one statistics set and one candidate list: in this release a stratum is one
member id (all of that member's pinned units — e.g. the log member's catalog, day and month units —
form one stratum scored with query-time statistics). Scores are comparable **within** a stratum
only; across strata the lists are fused **by rank** (reciprocal rank fusion), under
``collection.selection.source_quotas`` when declared:

* one stratum in scope (support-only steps, any single-member collection): its list is returned
  unchanged — ids, order and scores are those of today's engine over the same store;
* several strata without quotas: RRF over every stratum's list (``1 / (RRF_K + rank)``, ties by
  stratum order then rank), top ``k``;
* with quotas: each stratum receives ``round(share × k)`` slots by the largest-remainder method over
  the strata present (a stratum's share is the sum of its sources' shares, renormalised over the
  strata in scope; a stratum without a declared share gets none of the reserved slots); its best
  candidates fill them; any slot left unfilled goes to the best remaining candidates by RRF. The
  final list is ordered by RRF score.

The fused score is the RRF value (declared in the stratified result), never a raw cross-stratum
score comparison.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..core.types import ScoredChunk
from ..core.types_members import StratumRanking

RRF_K = 60


def rrf(rank: int) -> float:
    """Reciprocal-rank value of a 0-based rank."""
    return 1.0 / (RRF_K + rank + 1)


def stratum_shares(strata: Sequence[str], sources: Mapping[str, Sequence[str]], quotas: Mapping[str, float] | None) -> dict[str, float] | None:
    """Per-stratum share of the candidate budget, renormalised over ``strata`` (``None``: no quotas)."""
    if not quotas:
        return None
    raw = {s: sum(float(quotas.get(src, 0.0)) for src in sources.get(s, ())) for s in strata}
    total = sum(raw.values())
    if total <= 0:
        return None
    return {s: v / total for s, v in raw.items()}


def _slots(shares: Mapping[str, float], k: int, order: Sequence[str]) -> dict[str, int]:
    """Largest-remainder allocation of ``k`` slots (ties by stratum order)."""
    exact = {s: shares[s] * k for s in order}
    slots = {s: int(exact[s]) for s in order}
    left = k - sum(slots.values())
    by_remainder = sorted(order, key=lambda s: (-(exact[s] - slots[s]), order.index(s)))
    for s in by_remainder[:left]:
        slots[s] += 1
    return slots


def fuse_strata(
    rankings: Mapping[str, Sequence[ScoredChunk]],
    k: int,
    *,
    sources: Mapping[str, Sequence[str]] | None = None,
    quotas: Mapping[str, float] | None = None,
) -> list[ScoredChunk]:
    """Fuse per-stratum ranked lists (each best first) into one list of at most ``k`` (see module)."""
    order = [s for s in rankings if rankings[s]]
    if not order or k <= 0:
        return []
    if len(order) == 1:
        return list(rankings[order[0]])[:k]
    candidates: list[tuple[float, int, int, ScoredChunk]] = []
    for position, stratum in enumerate(order):
        for rank, hit in enumerate(rankings[stratum]):
            candidates.append((rrf(rank), position, rank, hit))
    candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
    shares = stratum_shares(order, sources or {}, quotas)
    if shares is None:
        chosen = candidates[:k]
    else:
        slots = _slots(shares, k, order)
        chosen, taken = [], set()
        for position, stratum in enumerate(order):
            mine = [c for c in candidates if c[1] == position][: slots[stratum]]
            chosen.extend(mine)
            taken.update(id(c) for c in mine)
        for candidate in candidates:
            if len(chosen) >= k:
                break
            if id(candidate) not in taken:
                chosen.append(candidate)
                taken.add(id(candidate))
        chosen.sort(key=lambda c: (-c[0], c[1], c[2]))
    seen: set[str] = set()
    out: list[ScoredChunk] = []
    for score, _, _, hit in chosen:
        if hit.chunk_id in seen:
            continue
        seen.add(hit.chunk_id)
        out.append(ScoredChunk(chunk_id=hit.chunk_id, document_id=hit.document_id, score=score))
    return out[:k]


def ranking(stratum: str, hits: Sequence[ScoredChunk], units: Sequence[str], approximations: Sequence[str] = ()) -> StratumRanking:
    """A ``StratumRanking`` (the frozen exchange type of ``StratumScorer``) from scored chunks."""
    return StratumRanking(
        stratum=stratum,
        ids=tuple(h.chunk_id for h in hits),
        scores=tuple(float(h.score) for h in hits),
        units=tuple(units),
        approximations=tuple(approximations),
    )


def describe(strata: Mapping[str, Any]) -> dict[str, Any]:
    """Trace-friendly summary of a stratified search: per stratum the units read and hits."""
    return {name: {"units": list(info.get("units", ())), "hits": int(info.get("hits", 0))} for name, info in strata.items()}
