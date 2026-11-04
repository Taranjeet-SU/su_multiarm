"""``packing`` (plan §3.9): the evidence character budget with fair per-record allocation.

A record's passage is its fields rendered as ``name: value`` lines in the **configured**
``field_priority`` order, then its remaining fields in their own order — no hardcoded
``details``/``resolution`` preference. ``fair_share`` gives every record ``budget // n`` characters,
returns what short records do not use, and water-fills the truncated ones in order; a record whose
share would fall under ``min_per_record`` is omitted rather than reduced to a stub. ``greedy`` is
today's first-come allocation. Aggregates are appended whole when they fit under the
``aggregate_reserve_max`` share of the budget, never truncated.
Everything omitted or truncated is reported, never hidden.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ..core.config_selection import PackingConfig
from ..models import StrictModel


class PackRecord(StrictModel):
    id: str = Field(min_length=1)
    title: str = ""
    source: str = "document"
    url: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)
    fallback_text: str = ""  # retrieved passage when the record has no fields
    chunk_ids: list[str] = Field(default_factory=list)


class PackExtra(StrictModel):
    """An aggregate or report rendered as one indivisible passage."""

    id: str = Field(min_length=1)
    source: str = "aggregate"
    passage: str


class PackedEvidence(StrictModel):
    id: str
    title: str = ""
    source: str
    url: str | None = None
    passage: str
    retrieved_chunk_ids: list[str] = Field(default_factory=list)
    truncated: bool = False


class PackResult(StrictModel):
    evidence: list[PackedEvidence] = Field(default_factory=list)
    omitted_ids: list[str] = Field(default_factory=list)
    truncated_ids: list[str] = Field(default_factory=list)
    passage_characters: int = 0
    fairness: Literal["fair_share", "greedy"]
    field_priority: list[str] = Field(default_factory=list)
    aggregate_reserve_chars: int = 0  # the cap on aggregate evidence (``aggregate_reserve_max``)


def render(record: PackRecord, field_priority: list[str]) -> str:
    ordered = list(dict.fromkeys([*field_priority, *record.fields]))
    body = "\n".join(f"{name}: {record.fields[name]}" for name in ordered if record.fields.get(name))
    return body or record.fallback_text


def _allocate(lengths: list[int], budget: int, fairness: str, min_per_record: int) -> list[int]:
    n = len(lengths)
    if n == 0:
        return []
    if fairness == "greedy":
        allocations, remaining = [], budget
        for length in lengths:
            take = min(length, remaining)
            allocations.append(take)
            remaining -= take
        return allocations
    kept = n if min_per_record == 0 else min(n, max(0, budget // min_per_record))
    allocations = [0] * n
    if kept == 0:
        return allocations
    share = budget // kept
    for i in range(kept):
        allocations[i] = min(lengths[i], share)
    leftover = budget - sum(allocations)
    for i in range(kept):
        need = lengths[i] - allocations[i]
        if need > 0 and leftover > 0:
            extra = min(need, leftover)
            allocations[i] += extra
            leftover -= extra
    return allocations


def pack(records: list[PackRecord], config: PackingConfig, extras: list[PackExtra] | None = None) -> PackResult:
    result = PackResult(fairness=config.fairness, field_priority=list(config.field_priority))
    # Exact derived evidence (aggregates) has its size reserved before documents are allocated: given
    # only what the fair-share allocation left over, it got nothing on a full budget and a count
    # question reached the generator without its count. Output order stays documents, then extras.
    # The reservation is capped (``aggregate_reserve_max``, review N1): aggregates beyond it are
    # omitted and reported, so documents always keep the rest of the budget.
    used = 0
    reserve = int(config.budget_chars * config.aggregate_reserve_max)
    result.aggregate_reserve_chars = reserve
    kept_extras: list[PackedEvidence] = []
    for extra in extras or []:
        if used + len(extra.passage) <= reserve:
            used += len(extra.passage)
            kept_extras.append(PackedEvidence(id=extra.id, source=extra.source, passage=extra.passage))
        else:
            result.omitted_ids.append(extra.id)
    bodies = [render(r, config.field_priority) for r in records]
    allocations = _allocate([len(b) for b in bodies], config.budget_chars - used, config.fairness, config.min_per_record)
    for record, body, allocation in zip(records, bodies, allocations, strict=True):
        if allocation <= 0:
            result.omitted_ids.append(record.id)
            continue
        truncated = allocation < len(body)
        if truncated:
            result.truncated_ids.append(record.id)
        used += allocation
        result.evidence.append(
            PackedEvidence(
                id=record.id,
                title=record.title,
                source=record.source,
                url=record.url,
                passage=body[:allocation],
                retrieved_chunk_ids=record.chunk_ids,
                truncated=truncated,
            )
        )
    result.evidence.extend(kept_extras)
    result.passage_characters = used
    return result
