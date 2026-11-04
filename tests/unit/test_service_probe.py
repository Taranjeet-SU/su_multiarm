"""QA-16: ``pytest -q`` skips the ``external`` search-engine tests with a reason when their variable
is unset and the default stack does not answer, and fails when a configured server does not answer.
Uses a closed local port; no service is contacted."""

from __future__ import annotations

import pytest

import service_probe

CLOSED = "http://127.0.0.1:9"  # discard port: nothing listens there in the test environment


@pytest.fixture(autouse=True)
def fresh_probe_cache(monkeypatch):
    monkeypatch.setattr(service_probe, "_probed", {})


@pytest.mark.parametrize("kind, variable", [("opensearch", "SU_TEST_OPENSEARCH_URL"), ("elasticsearch", "SU_TEST_ELASTICSEARCH_URL")])
def test_unset_and_unreachable_default_skips_with_the_reason(kind, variable, monkeypatch):
    pytest.importorskip("opensearchpy" if kind == "opensearch" else "elasticsearch")
    monkeypatch.delenv(variable, raising=False)
    monkeypatch.setitem(service_probe.SERVICES, kind, (variable, CLOSED))
    with pytest.raises(pytest.skip.Exception, match=f"{variable} unset and the default {kind} test stack is not reachable"):
        service_probe.require_search_service(kind)
    with pytest.raises(pytest.skip.Exception):  # cached: the second test skips without a new probe
        service_probe.require_search_service(kind)


def test_a_configured_server_that_does_not_answer_fails(monkeypatch):
    pytest.importorskip("opensearchpy")
    monkeypatch.setenv("SU_TEST_OPENSEARCH_URL", CLOSED)
    with pytest.raises(pytest.fail.Exception, match="SU_TEST_OPENSEARCH_URL is set but the opensearch server does not answer"):
        service_probe.require_search_service("opensearch")


def test_a_missing_client_extra_skips(monkeypatch):
    monkeypatch.delenv("SU_TEST_ELASTICSEARCH_URL", raising=False)

    def no_extra(kind):
        raise ImportError("No module named 'elasticsearch'")

    monkeypatch.setattr(service_probe, "_probe", no_extra)
    with pytest.raises(pytest.skip.Exception, match="ImportError"):
        service_probe.require_search_service("elasticsearch")
