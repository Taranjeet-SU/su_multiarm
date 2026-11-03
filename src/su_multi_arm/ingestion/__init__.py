"""Ingestion: input normalisation, extraction policies, the single write path and generation release.

``normalize.py`` keeps the v1 mapping/redaction helpers (re-exported here for the
CLI); ``policy.py`` implements §3.14.1; ``write.py`` is the write path on the frozen contracts;
``release.py`` owns the generation pointer, manifests and failure records.
"""

from .normalize import mapped_document, parse_documents, redact_document

__all__ = ["mapped_document", "parse_documents", "redact_document"]
