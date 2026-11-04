"""``limits`` (plan §3.9): the per-arm record budget, applied after collapse and before union.

``source_slots`` (optional) reserves representation inside each arm's budget: pinned candidates
(exact identifiers) are kept first and never displaced, then records an arm marked ``leads`` (it
ordered them ahead of its scored list); then slot ``i`` takes the best remaining record of the
source named at position ``i`` (skipped when that source has none left); the budget is then filled
by score. The arm's list keeps that order, so slots also reorder the arm: a slotted record can rank
above a higher-scoring one under a rank-based ordering, even when the budget is not binding. Slots
beyond ``per_arm`` are ignored. Without slots the budget is the top ``per_arm`` records by score.
"""

from __future__ import annotations

from .types import Candidate, PerArm


def _slotted(candidates: list[Candidate], per_arm_n: int, source_slots: list[str]) -> list[Candidate]:
    ranked = sorted(candidates, key=lambda x: (-x.score, x.record_id))
    # Pinned candidates (exact identifiers) are never displaced; records the arm itself put first
    # (``leads``) follow; the slots then mix the rest.
    chosen: list[Candidate] = [c for c in ranked if c.pinned][:per_arm_n]
    chosen += [c for c in ranked if c.leads and not c.pinned][: max(0, per_arm_n - len(chosen))]
    taken: set[str] = {c.record_id for c in chosen}
    for source in source_slots[: max(0, per_arm_n - len(chosen))]:
        pick = next((c for c in ranked if c.source == source and c.record_id not in taken), None)
        if pick is not None:
            chosen.append(pick)
            taken.add(pick.record_id)
    for candidate in ranked:
        if len(chosen) >= per_arm_n:
            break
        if candidate.record_id not in taken:
            chosen.append(candidate)
            taken.add(candidate.record_id)
    return chosen


def limit(per_arm: PerArm, per_arm_n: int, source_slots: list[str] | None = None) -> PerArm:
    if per_arm_n < 1:
        raise ValueError("per_arm must be at least 1")
    if source_slots:
        return {arm: _slotted(c, per_arm_n, source_slots) for arm, c in per_arm.items()}
    return {arm: sorted(c, key=lambda x: (-x.score, x.record_id))[:per_arm_n] for arm, c in per_arm.items()}
