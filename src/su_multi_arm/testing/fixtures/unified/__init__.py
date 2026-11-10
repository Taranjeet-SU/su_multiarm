"""Synthetic multi-source fixtures of the unified collection (unified merge plan rev 3, increment F0).

* ``collection`` — the small committed collection under ``tests/fixtures/unified/``: an adopted
  support member (tickets + docs), a tracker snapshot, log archive day/month members under two
  keyword-recipe epochs, provisional live-window slices with a slice ledger, analytics day members,
  an identity registry with every alias status, link candidates of every tier and status, traps and
  a synthetic onboarding file. ``build_files()`` is the single source of the committed files;
  ``load()`` reads them back as validated ``Document`` lists plus the manifest/registry/links.
* ``live_shape`` — a synthetic config with the *shape* of a live support deployment (typed query on
  the SQL surface with a router, attached kuzu graph, segment sidecar, imported communities,
  precomputed embeddings, agentic review pipeline); ``materialize()`` writes it with its side files.
* ``log_day`` — a deterministic generator of a large synthetic log day (default 600k chunks) as
  five-minute refresh deltas, for the M2a benchmark; written on demand outside the repository.

Synthetic only: every tenant, account, company, service, rule, URL and text is invented. Log
document ids are built with the write path's own id function (``ingestion.rollups``), so they carry
no day and repeat across days exactly as the real compose output does.
"""

from __future__ import annotations

from .collection import FIXTURE_NOW, UnifiedFixture, build_files, load, write_files
from .live_shape import materialize as materialize_live_shape
from .log_day import LogDaySpec, generate_log_day

__all__ = ["FIXTURE_NOW", "LogDaySpec", "UnifiedFixture", "build_files", "generate_log_day", "load", "materialize_live_shape", "write_files"]
