"""The logs profile's header ontology (addendum A2 §4.1.3; ``extraction.pattern_library.log_links``).

Header-scoped edges between the attribute values a compose document names in its rendered
``header``. Both endpoint names must occur in the same header chunk on value boundaries; the edge's
span is the smallest span covering both, so its quote shows both names (span provenance). These
are fixed by the profile (no code or predicates are loaded from configuration):

================== ======================== ========== ===============================================
document kind      edge                     predicate  edge kind / support
================== ======================== ========== ===============================================
``signature_day``  service -> signature     emits      ``native_link`` / ``unchecked``
``unit_day``       tenant -> service        runs       ``native_link`` / ``unchecked``
``endpoint_day``   service -> endpoint      serves     ``native_link`` / ``unchecked``
``exception_day``  signature -> exception   raises     ``native_link`` / ``unchecked``
``incident``       signature <-> signature  co_fires_  ``co_firing`` / ``unchecked``: statistical
                                            with       co-occurrence (lift and cells in the quote),
                                                       never causality; not traversed by default
================== ======================== ========== ===============================================

Every day document of a kind is one occurrence of its edge, so a deduplicated edge's occurrences
name the day documents it was seen in; the per-day weight is that document's ``lines``
(``runtime/log_tools.graph_neighbourhood`` reads it from there).
"""

from __future__ import annotations

import re
from typing import Any

from ...core.types import Chunk, Document, Entity, Provenance, Relation

_EDGE = r"[A-Za-z0-9_.\-/{}]"
# kind -> [(subject field, subject type, predicate, object field, object type)]
HEADER_LINKS: dict[str, list[tuple[str, str, str, str, str]]] = {
    "signature_day": [("service", "service", "emits", "signature_id", "signature")],
    "unit_day": [("tenant", "tenant", "runs", "service", "service")],
    "endpoint_day": [("service", "service", "serves", "endpoint", "endpoint")],
    "exception_day": [("signature_id", "signature", "raises", "exception_class", "exception_class")],
}
CO_FIRES = "co_fires_with"


def _find(text: str, value: str, start: int = 0, end: int | None = None) -> re.Match[str] | None:
    pattern = re.compile(rf"(?<!{_EDGE}){re.escape(value)}(?!{_EDGE})")
    return pattern.search(text, start, len(text) if end is None else end)


def header_links(extractor: Any, chunk: Chunk, document: Document) -> tuple[list[Entity], list[Relation]]:
    mentions: dict[str, Entity] = {}
    relations: list[Relation] = []
    if document.metadata.get("extraction_source") == "model_assisted":
        return [], []  # B7: model-extracted rows produce no graph edges
    provenance = Provenance(extractor=f"log_links:{document.kind}")
    for subject_field, subject_type, predicate, object_field, object_type in HEADER_LINKS.get(document.kind, []):
        ends = []
        for field, kind in ((subject_field, subject_type), (object_field, object_type)):
            value = document.metadata.get(field)
            found = _find(chunk.text, value) if isinstance(value, str) and value else None
            mention = extractor._mention(chunk, kind, found.start(), found.end()) if found else None
            if mention is None:
                break
            ends.append(mentions.setdefault(mention.id, mention))
        if len(ends) != 2:
            continue
        low = min(e.span.start for e in ends) - chunk.start
        high = max(e.span.end for e in ends) - chunk.start
        relations.append(
            Relation(
                subject_id=ends[0].id,
                predicate=predicate,
                object_id=ends[1].id,
                span=extractor._span(chunk, low, high),
                edge_kind="native_link",
                support="unchecked",
                provenance=provenance,
            )
        )
    if document.kind == "graph_link":  # H1: one link of the served history, endpoints from metadata
        subject_field, object_field = str(document.metadata.get("subject_field")), str(document.metadata.get("object_field"))
        types = {"service": "service", "signature_id": "signature", "tenant": "tenant", "endpoint": "endpoint", "exception_class": "exception_class"}
        ends = []
        for field in (subject_field, object_field):
            value = document.metadata.get(field)
            found = _find(chunk.text, value) if isinstance(value, str) and value else None
            mention = extractor._mention(chunk, types.get(field, field), found.start(), found.end()) if found else None
            if mention is None:
                break
            ends.append(mentions.setdefault(mention.id, mention))
        if len(ends) == 2:
            low = min(e.span.start for e in ends) - chunk.start
            high = max(e.span.end for e in ends) - chunk.start
            relations.append(
                Relation(
                    subject_id=ends[0].id,
                    predicate=str(document.metadata.get("predicate")),
                    object_id=ends[1].id,
                    span=extractor._span(chunk, low, high),
                    edge_kind="native_link",
                    support="unchecked",
                    provenance=provenance,
                )
            )
    if document.kind == "incident":
        pairs = str(document.metadata.get("pairs") or "")
        for item in [p for p in pairs.split(";") if p]:
            a, b, *_ = item.split("|")
            segment = _find(chunk.text, f"{a} ~ {b}")
            if segment is None:
                continue
            close = chunk.text.find(")", segment.end())
            seg_end = close + 1 if close != -1 else segment.end()
            left = extractor._mention(chunk, "signature", segment.start(), segment.start() + len(a))
            right = extractor._mention(chunk, "signature", segment.end() - len(b), segment.end())
            if left is None or right is None:
                continue
            left, right = mentions.setdefault(left.id, left), mentions.setdefault(right.id, right)
            relations.append(
                Relation(
                    subject_id=left.id,
                    predicate=CO_FIRES,
                    object_id=right.id,
                    span=extractor._span(chunk, segment.start(), seg_end),
                    edge_kind="co_firing",
                    support="unchecked",
                    provenance=Provenance(extractor="co_firing"),
                )
            )
    return list(mentions.values()), relations
