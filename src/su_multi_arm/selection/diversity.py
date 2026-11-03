"""``diversity`` (plan §3.9, §6.4 ``source_hierarchy_minority_loss``): opt-in groups by any field.

Guarantees ``min_per_value`` leading slots for every group value present in the union, so a strict
source priority cannot push a minority group out of the head of the list. Membership never changes;
only order does. Groups with fewer records than ``min_per_value`` contribute what they have
(documented fallback) and are listed in the trace as ``short_groups``. ``field`` may be ``source``,
``kind`` or any metadata key; records without the field form the ``<missing>`` group.
"""

from __future__ import annotations

from pydantic import Field

from ..models import StrictModel
from .types import Selected


class DiversityTrace(StrictModel):
    field: str
    groups: dict[str, int] = Field(default_factory=dict)
    short_groups: list[str] = Field(default_factory=list)
    promoted: int = 0


def group_value(record: Selected, field: str) -> str:
    if field == "source":
        return record.source
    if field == "kind":
        return record.kind
    value = record.metadata.get(field)
    return "<missing>" if value is None else str(value)


def diversify(ordered: list[Selected], field: str, min_per_value: int = 1) -> tuple[list[Selected], DiversityTrace]:
    if min_per_value < 1:
        raise ValueError("min_per_value must be at least 1")
    counts: dict[str, int] = {}
    for record in ordered:
        counts[group_value(record, field)] = counts.get(group_value(record, field), 0) + 1
    head: list[Selected] = []
    taken: dict[str, int] = {g: 0 for g in counts}
    remaining: list[Selected] = []
    for record in ordered:
        group = group_value(record, field)
        if taken[group] < min_per_value:
            taken[group] += 1
            head.append(record)
        else:
            remaining.append(record)
    promoted = 0
    for index, record in enumerate(head):
        if ordered[index] is not record:
            record.reasons.append(f"diversity:{field}={group_value(record, field)}")
            promoted += 1
    short = sorted(g for g, n in counts.items() if n < min_per_value)
    return [*head, *remaining], DiversityTrace(field=field, groups=counts, short_groups=short, promoted=promoted)
