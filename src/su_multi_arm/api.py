"""HTTP app entry point kept at its v1 import path; the implementation is ``serving/http.py``."""

from __future__ import annotations

from .serving.http import create_app

__all__ = ["create_app"]
