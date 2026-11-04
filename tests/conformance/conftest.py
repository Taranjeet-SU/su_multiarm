"""Conformance kit fixtures: every adapter on the contracts, built fresh per test.

The in-memory fake and the sqlite adapter run on every push. The OpenSearch and Elasticsearch adapters
(marked ``external``) run against the throwaway stack in ``tests/services/``
(``SU_TEST_OPENSEARCH_URL`` default ``http://127.0.0.1:19200``, ``SU_TEST_ELASTICSEARCH_URL`` default
``http://127.0.0.1:19201``); each test uses its own index prefix and removes it afterwards. Under a
plain ``pytest -q`` they skip with the reason when the variable is unset and the default stack does
not answer (``tests/service_probe.py``, QA-16); a configured server that does not answer fails.
"""

from __future__ import annotations

import secrets

import pytest

from service_probe import require_search_service, service_url
from su_multi_arm.backends.memory import MemoryBackend
from su_multi_arm.backends.sqlite import SqliteBackend

OPENSEARCH_URL = service_url("opensearch")
ELASTICSEARCH_URL = service_url("elasticsearch")
ADAPTERS = [
    "memory",
    "sqlite",
    "sqlite_faiss",
    pytest.param("opensearch", marks=pytest.mark.external),
    pytest.param("opensearch_hnsw", marks=pytest.mark.external),
    pytest.param("elasticsearch", marks=pytest.mark.external),
    pytest.param("elasticsearch_hnsw", marks=pytest.mark.external),
]


def opensearch_backend(prefix: str, knn_method: str = "exact", **kwargs):
    from su_multi_arm.backends.opensearch import OpenSearchBackend

    return OpenSearchBackend(OPENSEARCH_URL, index_prefix=prefix, knn_method=knn_method, timeout_seconds=30, **kwargs)


def elasticsearch_backend(prefix: str, knn_method: str = "exact", **kwargs):
    from su_multi_arm.backends.elasticsearch import ElasticsearchBackend

    return ElasticsearchBackend(ELASTICSEARCH_URL, index_prefix=prefix, knn_method=knn_method, timeout_seconds=30, **kwargs)


@pytest.fixture(params=ADAPTERS)
def adapter_factory(request, tmp_path):
    if request.param.startswith(("opensearch", "elasticsearch")):
        require_search_service(request.param.split("_")[0])
    clock = {"now": 100.0}
    counter = {"n": 0}
    prefix = f"su_test_{secrets.token_hex(4)}"
    created = []

    def make():
        counter["n"] += 1
        if request.param == "memory":
            return MemoryBackend(clock=lambda: clock["now"])
        if request.param == "sqlite_faiss":
            pytest.importorskip("faiss")
            return SqliteBackend(tmp_path / f"faiss{counter['n']}", vector_engine="faiss", clock=lambda: clock["now"])
        if request.param.startswith("opensearch"):
            backend = opensearch_backend(f"{prefix}_{counter['n']}", "hnsw" if request.param.endswith("hnsw") else "exact", clock=lambda: clock["now"])
            created.append(backend)
            return backend
        if request.param.startswith("elasticsearch"):
            backend = elasticsearch_backend(f"{prefix}_{counter['n']}", "hnsw" if request.param.endswith("hnsw") else "exact", clock=lambda: clock["now"])
            created.append(backend)
            return backend
        return SqliteBackend(tmp_path / f"sqlite{counter['n']}", clock=lambda: clock["now"])

    make.clock = clock
    make.kind = request.param.split("_")[0]
    yield make
    for backend in created:  # test-side cleanup of this test's own prefix; the adapter itself only deletes generations
        # Listed by name: Elasticsearch refuses wildcard deletes (``action.destructive_requires_name``).
        rows = backend.transport.request("GET", f"/_cat/indices/{backend.prefix}_*", params={"format": "json", "h": "index"})
        names = [row["index"] for row in rows or []]
        if names:
            backend.transport.request("DELETE", "/" + ",".join(names))
        backend.close()


# --- PostgreSQL test server (postgres and sql[postgres] suites) ----------------------------------

DEFAULT_POSTGRES_DSN = "postgresql://su_test:su_test_password@127.0.0.1:15432/su_test"


@pytest.fixture(scope="session")
def postgres_dsn():
    """``SU_TEST_POSTGRES_DSN`` (default: the throwaway stack in ``tests/services/docker-compose.yml``,
    port 15432). Unset and unreachable default: skip with the reason. An explicitly configured server
    that does not answer is a failure, not a skip. Tests use fresh random prefixes and drop only those."""
    import os

    psycopg = pytest.importorskip("psycopg")
    configured = os.environ.get("SU_TEST_POSTGRES_DSN")
    dsn = configured or DEFAULT_POSTGRES_DSN
    try:
        psycopg.connect(dsn, connect_timeout=3).close()
    except psycopg.Error as exc:
        if configured:
            raise
        pytest.skip(f"SU_TEST_POSTGRES_DSN unset and the default test stack is not reachable: {exc}")
    return dsn


def sqlalchemy_postgres_url(dsn: str) -> str:
    """The same server through SQLAlchemy's psycopg (v3) dialect."""
    for scheme in ("postgresql://", "postgres://"):
        if dsn.startswith(scheme):
            return "postgresql+psycopg://" + dsn[len(scheme) :]
    return dsn
