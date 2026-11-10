from __future__ import annotations

import re
from typing import Any

from ..core.types import Document


def mapped_document(record: dict, mapping: Any, dataset: str) -> Document:
    """Map exact source field names; no HTML stripping or guessed missing bodies."""

    def value(key, default=None):
        # Exact keys deliberately support export field names containing dots (e.g. "Article.Title").
        return record.get(key, default) if key else default

    identifier = value(mapping.id)
    if identifier is None or not str(identifier).strip():
        raise ValueError(f"source record has no value for ID field {mapping.id}")
    fields = {}
    for canonical, source in mapping.fields.items():
        item = value(source, "")
        if item is not None and not isinstance(item, str):
            raise ValueError(f"text field {source} must be a string or null")
        fields[canonical] = item or ""
    return Document(
        id=str(identifier),
        dataset=dataset,
        title=value(mapping.title, "") or "",
        source=value(mapping.source, mapping.source_value) or mapping.source_value,
        url=value(mapping.url),
        fields=fields,
        metadata={target: value(source) for target, source in mapping.metadata.items()},
    )


def parse_documents(records: list[dict], config: Any) -> list[Document]:
    """``config`` is a v2 ``Config`` (``ingestion.input_mapping``) or a v1 ``Config`` (``input_mapping``)."""
    mapping = config.ingestion.input_mapping if hasattr(config, "ingestion") else config.input_mapping
    if mapping:
        return [mapped_document(r, mapping, config.dataset) for r in records]
    return [Document.model_validate(r) for r in records]


def redact_document(document: Any, config: Any, safe: Any = None) -> tuple[Any, dict[str, int]]:
    """Works for v1 and v2 documents (both carry title/fields/metadata). ``safe`` is the
    ``SafeRedactor`` of ``privacy.engine: safe``; it replaces the legacy ``re`` path. A document
    without any replacement is returned as is (no copy)."""
    if safe is not None:
        return _redact_safe(document, config, safe)
    patterns = dict(config.patterns)
    if config.redact_emails:
        patterns["EMAIL"] = r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"
    if not patterns:
        return document, {}  # nothing to redact: no copy (compose of 10^6 rollup documents, N10)
    document = document.model_copy(deep=True)
    counts = {}

    def redact(text):
        for label, pattern in patterns.items():
            text, count = re.subn(pattern, lambda match: f"[REDACTED_{label}]", text, flags=re.I)
            counts[label] = counts.get(label, 0) + count
        return text

    document.title = redact(document.title)
    document.fields = {key: redact(value) for key, value in document.fields.items()}
    for field in config.metadata_fields:
        if isinstance(document.metadata.get(field), str):
            document.metadata[field] = redact(document.metadata[field])
    return document, counts


def _redact_safe(document: Any, config: Any, safe: Any) -> tuple[Any, dict[str, int]]:
    counts: dict[str, int] = {}

    def redact(text: str) -> str:
        out, made = safe.redact(text)
        for label, count in made.items():
            counts[label] = counts.get(label, 0) + count
        return out

    title = redact(document.title)
    fields = {key: redact(value) for key, value in document.fields.items()}
    metadata = dict(document.metadata)
    for name in config.metadata_fields:
        if isinstance(metadata.get(name), str):
            metadata[name] = redact(metadata[name])
    if not counts:
        return document, counts
    return document.model_copy(update={"title": title, "fields": fields, "metadata": metadata}), counts
