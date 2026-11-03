"""Field-aware chunking with exact offsets (``Chunk.text == field[start:end]``).

The chunk id hashes dataset, document, field, offsets and text, so an unchanged chunk keeps its id
across generations (extraction reuse keys on it). Fields are visited in the configured
``field_order`` first, then the document's remaining fields in their own order; ``field_order`` is
empty under the neutral profile, so no field name is privileged unless the operator says so.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..core.types import Chunk, Document


def field_sequence(document: Any, field_order: list[str]) -> list[str]:
    """``title`` first, then configured order, then the document's own field order."""
    return list(dict.fromkeys(["title", *field_order, *document.fields]))


def chunk_document(doc: Any, size: int, overlap: int, field_order: list[str], recipe: str = "default") -> list[Chunk]:
    if overlap >= size:
        raise ValueError("overlap must be smaller than size")
    result = []
    fields = {"title": doc.title, **doc.fields}
    for field in field_sequence(doc, field_order):
        text = fields.get(field, "")
        if not text.strip():
            continue
        start = 0
        while start < len(text):
            end = min(len(text), start + size)
            value = text[start:end]
            key = f"{doc.dataset}\0{doc.id}\0{field}\0{start}\0{end}\0{value}"
            result.append(
                Chunk(
                    id=hashlib.sha256(key.encode()).hexdigest(),
                    document_id=doc.id,
                    field=field,
                    start=start,
                    end=end,
                    text=value,
                    recipe=recipe,
                )
            )
            if end == len(text):
                break
            start = end - overlap
    return result


def to_v2_document(document: Any) -> Document:
    """Accept a v1 ``models.Document`` (or any object with the same fields) as a v2 ``Document``."""
    if isinstance(document, Document):
        return document
    return Document.model_validate(document.model_dump())
