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


RECORD_SEPARATOR = "\n\n"  # the ``line`` recipe: log records of one field are separated by a blank line


class LineRecordTooLong(ValueError):
    """A record of a ``line``-recipe field exceeds ``max_chunk_chars``: never split (a fact could span chunks)."""


def line_chunks(doc: Any, field: str, text: str, max_chars: int) -> list[Chunk]:
    """The ``line`` chunk recipe (addendum A1 §3.4.2 n1): one chunk per record, whatever the
    chunking size; records are separated by one blank line and keep their exact offsets."""
    result = []
    start = 0
    while start < len(text):
        end = text.find(RECORD_SEPARATOR, start)
        end = len(text) if end < 0 else end
        value = text[start:end]
        if value.strip():
            if len(value) > max_chars:
                raise LineRecordTooLong(f"document {doc.id!r} field {field!r}: a record of {len(value)} characters exceeds line_recipe.max_chunk_chars ({max_chars})")
            key = f"{doc.dataset}\0{doc.id}\0{field}\0{start}\0{end}\0{value}\0line"
            result.append(Chunk(id=hashlib.sha256(key.encode()).hexdigest(), document_id=doc.id, field=field, start=start, end=end, text=value, recipe="line"))
        start = end + len(RECORD_SEPARATOR)
    return result


def chunk_with_recipes(doc: Any, size: int, overlap: int, field_order: list[str], line_recipe: Any = None) -> list[Chunk]:
    """``chunk_document`` with the fields of ``line_recipe`` (when set) cut by ``line_chunks``,
    in the same field sequence."""
    if line_recipe is None:
        return chunk_document(doc, size, overlap, field_order)
    lines = set(line_recipe.fields)
    sized: dict[str, list[Chunk]] = {}
    for chunk in chunk_document(doc, size, overlap, field_order):
        if chunk.field not in lines:
            sized.setdefault(chunk.field, []).append(chunk)
    fields = {"title": doc.title, **doc.fields}
    result: list[Chunk] = []
    for field in field_sequence(doc, field_order):
        if field in lines:
            if fields.get(field, "").strip():
                result.extend(line_chunks(doc, field, fields[field], line_recipe.max_chunk_chars))
        else:
            result.extend(sized.get(field, []))
    return result
