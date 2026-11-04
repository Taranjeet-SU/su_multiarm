"""Static assets of the su_multi_arm web UI (built from ``ui/`` with Vite).

This package holds files only: ``STATIC_DIR`` (``index.html`` plus hashed ``assets/``) and
``manifest.json`` (version and the sha256 of every file). ``su_multi_arm.serving.ui`` mounts it
at ``/ui`` with a strict Content-Security-Policy; nothing here runs server-side code."""

from __future__ import annotations

from pathlib import Path

__version__ = "2.0.0a3"
STATIC_DIR = Path(__file__).resolve().parent / "static"
MANIFEST = STATIC_DIR / "manifest.json"

__all__ = ["MANIFEST", "STATIC_DIR", "__version__"]
