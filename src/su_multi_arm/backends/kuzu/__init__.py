"""Embedded Kuzu adapter (plan §3.5, Phase 5 "later backends" brought forward for parity): the
``graph`` capability on the frozen contracts.

* ``KuzuBackend`` (``mode: native``) — the package's own generation-scoped store: MERGE-idempotent
  load, fact vs co-occurrence edge kinds, occurrences and provenance on edges, document-id
  eligibility, memberships and communities, an atomic per-dataset generation pointer;
* ``KuzuAttachedBackend`` (``mode: attach``) — an existing Kuzu database opened read-only and mapped
  onto the contract through a declared mapping file (``attach.py``)."""

from __future__ import annotations

from typing import Any

from .adapter import KuzuBackend
from .attach import AttachMapping, KuzuAttachedBackend


def from_config(config: Any) -> Any:
    """The backend for a ``backends.<name>: {kind: kuzu}`` block (native or attached)."""
    if config.mode == "attach":
        return KuzuAttachedBackend.from_config(config)
    return KuzuBackend.from_config(config)


__all__ = ["AttachMapping", "KuzuAttachedBackend", "KuzuBackend", "from_config"]
