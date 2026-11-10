"""Legacy question-answering HTTP contract on package engines (``create_legacy_app``).

A compatibility layer for clients written against an earlier QA service: ``POST /api/qa``,
``POST /api/qa/stream`` (SSE ``plan``/``evidence``/``token``/``final``/``error``), ``POST /api/search``
(typed search envelope), ``GET /source`` (record view), the catalog routes and ``/health``. All
deployment specifics come from a ``LegacyProfile`` (YAML/JSON data); every legacy field the package
cannot fill is named in the response's ``compat.unsupported`` block, never silently dropped.
Run it with ``python -m su_multi_arm.compat.legacy_qa --profile PROFILE.yaml --bind 127.0.0.1:8110``.
"""

from .app import LegacyQ, create_legacy_app
from .profile import DatasetProfile, LegacyProfile, load_profile

__all__ = ["DatasetProfile", "LegacyProfile", "LegacyQ", "create_legacy_app", "load_profile"]
