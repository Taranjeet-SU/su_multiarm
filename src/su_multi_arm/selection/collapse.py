"""``collapse`` (plan §3.9): passages → canonical records, one vote per record per arm.

Reproduces today's per-arm merge in ``engine.py``: candidates are sorted by ``(-score, id)``, the
first occurrence of a record wins its score, and chunk IDs / provenance of later occurrences are
merged into it. With ``unit="chunk"`` nothing is merged (chunk granularity is kept).
"""

from __future__ import annotations

from typing import Literal

from .types import Candidate, PerArm

CollapseUnit = Literal["document", "chunk"]


def collapse_arm(candidates: list[Candidate], unit: CollapseUnit = "document") -> list[Candidate]:
    ordered = sorted(candidates, key=lambda c: (-c.score, c.record_id, c.chunk_ids))
    if unit == "chunk":
        return ordered
    seen: dict[str, Candidate] = {}
    for candidate in ordered:
        prior = seen.get(candidate.record_id)
        if prior is None:
            # Fresh containers, not a deep copy: the merge below only rebinds these fields, and a deep
            # copy of every candidate dominated selection time on large candidate pools.
            seen[candidate.record_id] = candidate.model_copy(
                update={
                    "chunk_ids": list(candidate.chunk_ids),
                    "metadata": dict(candidate.metadata),
                    "provenance": [dict(p) for p in candidate.provenance],
                }
            )
            continue
        prior.chunk_ids = list(dict.fromkeys([*prior.chunk_ids, *candidate.chunk_ids]))
        prior.provenance = [*prior.provenance, *candidate.provenance]
        prior.pinned = prior.pinned or candidate.pinned
        prior.leads = prior.leads or candidate.leads
    return list(seen.values())


def collapse(per_arm: PerArm, unit: CollapseUnit = "document") -> PerArm:
    return {arm: collapse_arm(candidates, unit) for arm, candidates in per_arm.items()}
