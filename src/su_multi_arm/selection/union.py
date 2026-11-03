"""``union`` (plan §3.9): ``preserve`` keeps every record of every arm; one vote per record per arm.

Also the merge point for review hops: new per-arm pools are appended and re-collapsed, so a record
retrieved twice by the same arm (two planner queries or two hops) still votes once.
"""

from __future__ import annotations

from .collapse import collapse_arm
from .types import Candidate, PerArm, Selected


def merge_pools(existing: PerArm, incoming: PerArm) -> PerArm:
    merged: PerArm = {arm: list(c) for arm, c in existing.items()}
    for arm, candidates in incoming.items():
        merged.setdefault(arm, []).extend(candidates)
    return {arm: collapse_arm(candidates) for arm, candidates in merged.items()}


def union(per_arm: PerArm) -> dict[str, Selected]:
    """Union with per-record provenance; scores are left at zero for ``ordering`` to fill."""
    result: dict[str, Selected] = {}
    for arm, candidates in per_arm.items():
        unique: dict[str, Candidate] = {}
        for candidate in candidates:
            unique.setdefault(candidate.record_id, candidate)
        for rank, candidate in enumerate(unique.values(), 1):
            selected = result.get(candidate.record_id)
            if selected is None:
                selected = Selected(
                    record_id=candidate.record_id,
                    score=0.0,
                    source=candidate.source,
                    kind=candidate.kind,
                    metadata=dict(candidate.metadata),
                )
                result[candidate.record_id] = selected
            selected.arms.append(arm)
            selected.chunk_ids = list(dict.fromkeys([*selected.chunk_ids, *candidate.chunk_ids]))
            selected.provenance = [*selected.provenance, *candidate.provenance]
            selected.pinned = selected.pinned or candidate.pinned
            selected.reasons.append(f"arm:{arm} rank {rank}")
    return result


def membership(per_arm: PerArm) -> set[str]:
    return {c.record_id for candidates in per_arm.values() for c in candidates}
