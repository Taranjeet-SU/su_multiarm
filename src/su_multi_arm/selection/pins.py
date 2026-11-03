"""``pins`` (plan §3.9): records pinned by the ``exact_id`` arm move to the front, order preserved."""

from __future__ import annotations

from .types import Selected


def pin(ordered: list[Selected], pinned_ids: set[str] | None = None) -> list[Selected]:
    pinned_ids = pinned_ids or set()
    front = [r for r in ordered if r.pinned or r.record_id in pinned_ids]
    rest = [r for r in ordered if not (r.pinned or r.record_id in pinned_ids)]
    for record in front:
        record.pinned = True
        record.reasons.append("pin:exact_id")
    return [*front, *rest]
