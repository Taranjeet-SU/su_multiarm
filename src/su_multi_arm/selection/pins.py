"""``pins`` (plan §3.9): records pinned by the ``exact_id`` arm move to the front, order preserved.

``order_by_source`` is the strict source order (``selection.source_order``): a stable sort of the
union by the position of each record's source in the configured list, unlisted sources after the
listed ones in their existing order. It runs before ``pin``, so exact-identifier hits still lead.
Neither function adds or removes a record."""

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


def order_by_source(ordered: list[Selected], source_order: list[str]) -> list[Selected]:
    position = {source: i for i, source in enumerate(source_order)}
    last = len(source_order)
    ranked = sorted(enumerate(ordered), key=lambda item: (position.get(item[1].source or "", last), item[0]))
    result = []
    for _, record in ranked:
        if record.source in position:
            record.reasons.append(f"source_order:{record.source}")
        result.append(record)
    return result
