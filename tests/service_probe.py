"""Reachability gate for the ``external`` search-engine tests (QA-16).

``pytest -q`` (the CONTRIBUTING and CI command) collects the ``external`` tests too. Each one first
asks ``require_search_service(kind)``: when its ``SU_TEST_*_URL`` variable is **unset** and the
default throwaway stack (``tests/services/``) does not answer, or the client extra is not installed,
the test is skipped with the reason. An **explicitly configured** server that does not answer is a
failure, never a skip, the same rule as the PostgreSQL and MongoDB fixtures. The probe runs once
per kind per session.
"""

from __future__ import annotations

import os

import pytest

SERVICES = {
    "opensearch": ("SU_TEST_OPENSEARCH_URL", "http://127.0.0.1:19200"),
    "elasticsearch": ("SU_TEST_ELASTICSEARCH_URL", "http://127.0.0.1:19201"),
}
_probed: dict[str, str | None] = {}  # kind -> skip reason (None = reachable)


def service_url(kind: str) -> str:
    variable, default = SERVICES[kind]
    return os.environ.get(variable) or default


def _probe(kind: str) -> None:
    url = service_url(kind)
    if kind == "opensearch":
        from su_multi_arm.backends.opensearch import OpenSearchBackend as Backend
    else:
        from su_multi_arm.backends.elasticsearch import ElasticsearchBackend as Backend
    backend = Backend(url, timeout_seconds=3)
    try:
        backend.probe()
    finally:
        backend.close()


def require_search_service(kind: str) -> str:
    """The server URL, or ``pytest.skip`` with the reason (``pytest.fail`` when configured)."""
    variable, _ = SERVICES[kind]
    if kind not in _probed:
        try:
            _probe(kind)
            _probed[kind] = None
        except Exception as exc:  # ImportError of the extra, connection refused, wrong server
            if os.environ.get(variable):
                pytest.fail(f"{variable} is set but the {kind} server does not answer: {type(exc).__name__}: {exc}")
            _probed[kind] = f"{variable} unset and the default {kind} test stack is not reachable ({type(exc).__name__})"
    if _probed[kind] is not None:
        pytest.skip(_probed[kind])
    return service_url(kind)
