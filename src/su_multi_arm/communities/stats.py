"""Deterministic per-community statistics from declared fields only (plan §3.13, R17).

* ``size`` (documents) and ``entities`` (member entity count);
* ``members_by_kind``: documents per ``Document.kind`` (a core field);
* ``trend`` over a declared ``time_basis`` (a datetime/date metadata field): counts per month or year
  bucket (UTC), the least-squares slope of those counts over the bucket index, and how many members
  had no usable value — never a guessed date;
* ``top_entities`` by lift: ``(members with the entity / size) / (corpus documents with the entity /
  corpus size)``, ties broken by support then id; an entity seen in a single member document of a
  community with more than one document is not reported (support below two is noise).

No severity, domain labels or model calls: every number is recomputable from the inputs.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping

from ..backends._scope import temporal_key, truncate
from ..core.types import Document, MetadataSchema
from .base import Community


def _slope(values: list[int]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean_x, mean_y = (n - 1) / 2, sum(values) / n
    num = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values))
    den = sum((i - mean_x) ** 2 for i in range(n))
    return round(num / den, 6) if den else 0.0


def community_stats(
    community: Community,
    documents: Mapping[str, Document],
    entity_documents: Mapping[str, set[str]],
    corpus_size: int,
    schema: MetadataSchema,
    time_basis: str | None,
    granularity: str,
    top_entities: int,
    names: Mapping[str, str],
) -> dict[str, Any]:
    members = [documents[d] for d in community.document_ids if d in documents]
    size = len(members)
    stats: dict[str, Any] = {
        "size": size,
        "entities": len(community.entity_ids),
        "members_by_kind": dict(sorted(Counter(d.kind for d in members).items())),
    }
    if time_basis is not None:
        spec = schema.field(time_basis)
        buckets: Counter[str] = Counter()
        missing = 0
        for doc in members:
            moment = temporal_key(doc.metadata.get(time_basis), spec)
            if moment is None:
                missing += 1
            else:
                buckets[truncate(moment, granularity).date().isoformat()] += 1
        ordered = dict(sorted(buckets.items()))
        stats["trend"] = {
            "field": time_basis,
            "granularity": granularity,
            "buckets": ordered,
            "slope": _slope(list(ordered.values())),
            "first": next(iter(ordered), None),
            "last": next(reversed(ordered), None) if ordered else None,
            "missing_or_invalid": missing,
        }
    if top_entities and size and corpus_size:
        member_set = set(community.document_ids)
        rows = []
        for entity, docs in entity_documents.items():
            support = len(docs & member_set)
            if support == 0 or (size > 1 and support < 2):
                continue
            lift = (support / size) / (len(docs) / corpus_size)
            rows.append((-round(lift, 6), -support, entity))
        stats["top_entities"] = [
            {"entity_id": entity, "name": names.get(entity, entity), "support": -neg_support, "lift": -neg_lift}
            for neg_lift, neg_support, entity in sorted(rows)[:top_entities]
        ]
    return stats
