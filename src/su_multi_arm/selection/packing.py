"""``packing`` (plan §3.9): the evidence character budget with fair per-record allocation.

A record's passage is its fields rendered as ``name: value`` lines in the **configured**
``field_priority`` order, then its remaining fields in their own order — no hardcoded
``details``/``resolution`` preference. ``fair_share`` gives every record ``budget // n`` characters,
returns what short records do not use, and water-fills the truncated ones in order; a record whose
share would fall under ``min_per_record`` is omitted rather than reduced to a stub. ``greedy`` is
today's first-come allocation. Aggregates are appended whole when they fit under the
``aggregate_reserve_max`` share of the budget, never truncated.
With ``passage_selection: question_windows`` (opt-in) and a question, bounded literal source windows
replace the opening of a record that would otherwise be truncated. The default ``prefix`` recipe, or
no question, keeps prefix rendering and allocation.
Allowlisted ``metadata_fields`` (V3.2) may take at most ``metadata_max_share`` of a record's
allocation whenever the passage and metadata together do not fit it: keys are dropped from the end
of the allowlist order until the compact wrapper fits, the dropped keys are reported per record, and
the record is never omitted because of its metadata (its passage text keeps the rest). Keys in
``metadata_protected`` are never dropped by the cap; they still count toward the allocation, and
when they alone exceed it they are kept and the passage text is trimmed to nothing (reported in
``metadata_protected_overflow`` with the characters over the allocation). Keys in
``metadata_show_null`` are packed as ``null`` when a record has no value (the field is shown empty).
Everything omitted or truncated is reported, never hidden; selected field offsets remain explicit.
"""

from __future__ import annotations

import heapq
import json
import re
from typing import Literal

from pydantic import Field

from ..core.config_selection import PackingConfig
from ..core.types import JsonScalar
from ..models import StrictModel


class PackRecord(StrictModel):
    id: str = Field(min_length=1)
    title: str = ""
    source: str = "document"
    url: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)
    fallback_text: str = ""  # retrieved passage when the record has no fields
    chunk_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, JsonScalar] = Field(default_factory=dict)


class PackExtra(StrictModel):
    """An aggregate or report rendered as one indivisible passage."""

    id: str = Field(min_length=1)
    source: str = "aggregate"
    passage: str


class PackedSpan(StrictModel):
    """Literal source slice and its position inside the rendered passage, in characters."""

    field: str
    start: int
    end: int
    text: str
    passage_start: int
    passage_end: int


class PackedEvidence(StrictModel):
    id: str
    title: str = ""
    source: str
    url: str | None = None
    passage: str
    retrieved_chunk_ids: list[str] = Field(default_factory=list)
    truncated: bool = False
    spans: list[PackedSpan] = Field(default_factory=list)
    metadata: dict[str, JsonScalar] = Field(default_factory=dict)

    def context_dict(self) -> dict:
        """Compact generator evidence; quote text is reconstructible from passage offsets."""
        excluded: dict = {"truncated": True, "spans": {"__all__": {"text"}}}
        if not self.metadata:
            excluded["metadata"] = True
        return self.model_dump(mode="json", exclude=excluded)


class PackResult(StrictModel):
    evidence: list[PackedEvidence] = Field(default_factory=list)
    omitted_ids: list[str] = Field(default_factory=list)
    truncated_ids: list[str] = Field(default_factory=list)
    passage_characters: int = 0
    fairness: Literal["fair_share", "greedy"]
    field_priority: list[str] = Field(default_factory=list)
    aggregate_reserve_chars: int = 0  # the cap on aggregate evidence (``aggregate_reserve_max``)
    passage_recipe: str = "prefix_v1"  # configured recipe, recorded even when no window was needed
    windowed_ids: list[str] = Field(default_factory=list)  # records whose passage came from windows
    # Records that lost at least one allowlisted metadata key to ``metadata_max_share`` (V3.2: the
    # record itself is kept; before V3.2 these records were omitted). Keys per record, in drop order.
    metadata_omitted_ids: list[str] = Field(default_factory=list)
    metadata_dropped_keys: dict[str, list[str]] = Field(default_factory=dict)
    # Records whose kept (protected) metadata is larger than the share cap, and records whose
    # protected metadata alone exceeds the record allocation -> characters over it (passage trimmed
    # to nothing; the evidence total exceeds ``budget_chars`` by the sum of these values).
    metadata_protected_over_cap_ids: list[str] = Field(default_factory=list)
    metadata_protected_overflow: dict[str, int] = Field(default_factory=dict)
    metadata_characters: int = 0
    evidence_characters: int = 0  # passage plus serialized allowlisted metadata wrapper
    leading_ids: list[str] = Field(default_factory=list)  # ``leading`` extras kept, placed first


def render(record: PackRecord, field_priority: list[str]) -> str:
    ordered = list(dict.fromkeys([*field_priority, *record.fields]))
    body = "\n".join(f"{name}: {record.fields[name]}" for name in ordered if record.fields.get(name))
    return body or record.fallback_text


_WORDS = re.compile(r"\w+", re.UNICODE)
_STOP = frozenset("a an and are as at be been by did do does for from had has have how i in is it its me my of on or our please that the their them there these they this to was were what when which who why will with would you your".split())
_RESOLUTION = re.compile(r"\b(?:fix(?:ed|es)?|resolv(?:e|ed)|resolution|recover(?:ed|y)?|workaround|mitigat(?:e|ed|ion)|restor(?:e|ed)|solution)\b", re.I)
_TIME_WORDS = frozenset("when latest recent last date time timestamp updated created version".split())
_DATE = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}(?::\d{2})?)\b")
_MAX_CANDIDATES = 64


def _prefix_spans(record: PackRecord, priority: list[str], limit: int) -> list[PackedSpan]:
    spans, cursor = [], 0
    for name in dict.fromkeys([*priority, *record.fields]):
        value = record.fields.get(name)
        if not value:
            continue
        start = cursor + len(name) + 2
        size = max(0, min(len(value), limit - start))
        if size:
            spans.append(PackedSpan(field=name, start=0, end=size, text=value[:size], passage_start=start, passage_end=start + size))
        cursor = start + len(value) + 1
    return spans


def _question_windows(record: PackRecord, priority: list[str], allocation: int, query: str, min_per_record: int) -> tuple[str, list[PackedSpan]] | None:
    """Linear source scan, at most 64 candidates and three nonoverlapping literal windows.

    Lexical/intent cues rank evidence only; they do not establish a fix, chronology or cause.
    Field priority breaks ties. No source fetch, model call or metadata inference is involved.
    """
    terms = set(dict.fromkeys(t.casefold() for t in _WORDS.findall(query) if t.casefold() not in _STOP))
    terms = set(sorted(terms)[:48])
    resolution = bool(_RESOLUTION.search(query))
    temporal = bool(set(_WORDS.findall(query.casefold())) & _TIME_WORDS)
    if not terms and not resolution and not temporal:
        return None
    slots = 1 if allocation < 600 else 2 if allocation < 1600 else 3
    width = max(80, min(800, allocation // slots - 40))
    candidates: list[tuple[int, int, int, str, int]] = []
    matched = False
    fields = list(dict.fromkeys([*priority, *record.fields]))
    for order, name in enumerate(fields):
        value = record.fields.get(name, "")
        if not value:
            continue
        for start in range(0, len(value), max(1, width // 2)):
            end = min(len(value), start + width)
            text = value[start:end]
            words = set(_WORDS.findall(text.casefold()))
            score = 3 * len(terms & words)
            score += 6 * bool(resolution and _RESOLUTION.search(name + " " + text))
            score += 2 * bool(temporal and _DATE.search(text))
            if score:
                matched = True
                # Prefer an equally relevant complete tail over cutting its final qualification.
                score = score * 2 + int(end == len(value))
            if score or start == 0:
                candidate = (score, -order, -start, name, end)
                if len(candidates) < _MAX_CANDIDATES:
                    heapq.heappush(candidates, candidate)
                else:
                    heapq.heappushpop(candidates, candidate)
            if end == len(value):
                break
    if not matched:
        return None
    chosen: list[tuple[int, int, str, int]] = []
    for _, neg_order, neg_start, name, end in sorted(candidates, reverse=True):
        start = -neg_start
        if any(name == other_name and start < other_end and other_start < end for _, other_start, other_name, other_end in chosen):
            continue
        chosen.append((-neg_order, start, name, end))
        available = sum(end - start + len(name) + 2 for _, start, name, end in chosen) + 7 * (len(chosen) - 1)
        if len(chosen) >= slots and available >= min(allocation, min_per_record) or len(chosen) == 3:
            break
    if not chosen:
        return None
    passage, spans = "", []
    for _, start, name, end in sorted(chosen):
        separator = "\n[...]\n" if passage else ""
        share = (allocation - len(passage) - len(separator)) // (len(chosen) - len(spans))
        # Labels are presentation; source and packed quote offsets are carried separately.
        label = f"{name}: "
        end = min(end, start + max(0, share - len(label)))
        if end <= start or len(separator) + len(label) >= allocation - len(passage):
            continue
        quote = record.fields[name][start:end]
        passage += separator + label
        packed_start = len(passage)
        passage += quote
        spans.append(PackedSpan(field=name, start=start, end=end, text=quote, passage_start=packed_start, passage_end=len(passage)))
    return (passage, spans) if passage else None


def _metadata_size(metadata: dict[str, JsonScalar]) -> int:
    """Characters of the compact ``{"metadata": ...}`` wrapper the evidence carries (0 when empty)."""
    return len(json.dumps({"metadata": metadata}, ensure_ascii=False, separators=(",", ":"))) if metadata else 0


def _cap_metadata(metadata: dict[str, JsonScalar], cap: int, protected: frozenset[str] = frozenset()) -> tuple[dict[str, JsonScalar], list[str]]:
    """Drop unprotected keys from the end of the allowlist order until the wrapper fits ``cap`` (or
    only protected keys remain); never reorders or truncates a value. Returns the kept metadata and
    the dropped keys in drop order."""
    kept = dict(metadata)
    dropped: list[str] = []
    droppable = [key for key in kept if key not in protected]
    while droppable and _metadata_size(kept) > cap:
        key = droppable.pop()
        dropped.append(key)
        del kept[key]
    return kept, dropped


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


def pack(records: list[PackRecord], config: PackingConfig, extras: list[PackExtra] | None = None, *, query: str | None = None, leading: list[PackExtra] | None = None) -> PackResult:
    """``leading`` extras (the typed router's evidence) are reserved before the other extras, under
    the same ``aggregate_reserve_max`` cap, and placed ahead of every record in the output."""
    result = PackResult(fairness=config.fairness, field_priority=list(config.field_priority))
    windows = config.passage_selection == "question_windows" and bool(query and query.strip())
    if windows:
        result.passage_recipe = "question_windows_v1"
    # Exact derived evidence (aggregates) has its size reserved before documents are allocated: given
    # only what the fair-share allocation left over, it got nothing on a full budget and a count
    # question reached the generator without its count. Output order stays documents, then extras.
    # The reservation is capped (``aggregate_reserve_max``, review N1): aggregates beyond it are
    # omitted and reported, so documents always keep the rest of the budget.
    used = 0
    reserve = int(config.budget_chars * config.aggregate_reserve_max)
    result.aggregate_reserve_chars = reserve
    kept_extras: list[PackedEvidence] = []
    lead: list[PackedEvidence] = []
    for first, extra in [*((True, e) for e in leading or []), *((False, e) for e in extras or [])]:
        if used + len(extra.passage) <= reserve:
            used += len(extra.passage)
            (lead if first else kept_extras).append(PackedEvidence(id=extra.id, source=extra.source, passage=extra.passage))
        else:
            result.omitted_ids.append(extra.id)
    result.evidence.extend(lead)
    result.leading_ids = [e.id for e in lead]
    bodies = [render(r, config.field_priority) for r in records]
    shown_null = frozenset(config.metadata_show_null)
    metadata = [{k: record.metadata.get(k) for k in config.metadata_fields if k in record.metadata or k in shown_null} for record in records]
    metadata_sizes = [_metadata_size(m) for m in metadata]
    protected = frozenset(config.metadata_protected)
    allocations = _allocate([len(b) + size for b, size in zip(bodies, metadata_sizes, strict=True)], config.budget_chars - used, config.fairness, config.min_per_record)
    for record, body, allocation, meta, meta_size in zip(records, bodies, allocations, metadata, metadata_sizes, strict=True):
        if allocation <= 0:
            result.omitted_ids.append(record.id)
            continue
        # The cap binds when metadata competes with passage text: a record whose passage and
        # metadata both fit its allocation keeps every allowlisted key.
        cap = int(allocation * config.metadata_max_share)
        if meta_size > cap and len(body) + meta_size > allocation:
            meta, dropped = _cap_metadata(meta, cap, protected)
            meta_size = _metadata_size(meta)
            if dropped:
                result.metadata_omitted_ids.append(record.id)
                result.metadata_dropped_keys[record.id] = dropped
            if meta_size > cap:
                result.metadata_protected_over_cap_ids.append(record.id)
        text_allocation = allocation - meta_size
        if text_allocation < 0:
            # protected keys alone exceed the allocation: kept, the passage is trimmed to nothing
            result.metadata_protected_overflow[record.id] = -text_allocation
            text_allocation = 0
        truncated = text_allocation < len(body)
        if truncated:
            result.truncated_ids.append(record.id)
        passage, spans = body[:text_allocation], _prefix_spans(record, config.field_priority, text_allocation)
        if truncated and windows and text_allocation:
            selected = _question_windows(record, config.field_priority, text_allocation, query or "", max(0, config.min_per_record - meta_size))
            if selected is not None:
                passage, spans = selected
                result.windowed_ids.append(record.id)
        used += len(passage) + meta_size
        result.metadata_characters += meta_size
        result.evidence.append(
            PackedEvidence(
                id=record.id,
                title=record.title,
                source=record.source,
                url=record.url,
                passage=passage,
                retrieved_chunk_ids=record.chunk_ids,
                truncated=truncated,
                spans=spans,
                metadata=meta,
            )
        )
    result.evidence.extend(kept_extras)
    result.passage_characters = used - result.metadata_characters
    result.evidence_characters = used
    return result
