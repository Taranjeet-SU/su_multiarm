"""Extraction (plan §3.12): chunking, span-anchored grounding, predicate support, dedupe and the
v2 extractor component. ``legacy.py`` keeps the v1 extractor (re-exported; the v1 store that used it is removed)."""

from .legacy import chunk_document  # v1 chunker (public re-export); v2 code uses .chunking
from .legacy import Extractor

__all__ = ["Extractor", "chunk_document"]
