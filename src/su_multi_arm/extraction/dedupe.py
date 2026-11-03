"""Relation dedupe (plan §3.12): identical ``(subject, predicate, object, span text)`` across chunks
that inherit the same header/title text collapse to one edge whose ``occurrences`` keep every source
span and provenance. Nothing is dropped and no kind changes: a ``co_occurrence`` stays one."""

from __future__ import annotations

from ..core.types import Occurrence, Relation


def dedupe_relations(relations: list[Relation]) -> list[Relation]:
    merged: dict[tuple[str, str, str, str, str], Relation] = {}
    order: list[tuple[str, str, str, str, str]] = []
    for relation in relations:
        key = (relation.subject_id, relation.predicate, relation.object_id, relation.edge_kind, relation.span.text)
        occurrence = Occurrence(span=relation.span, provenance=relation.provenance)
        existing = merged.get(key)
        if existing is None:
            merged[key] = relation.model_copy(update={"occurrences": [*relation.occurrences, occurrence] if not relation.occurrences else list(relation.occurrences)})
            order.append(key)
            continue
        if occurrence not in existing.occurrences:
            existing.occurrences.append(occurrence)
    return [merged[key] for key in order]
