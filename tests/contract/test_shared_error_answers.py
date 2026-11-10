"""Shared-pattern questions use scoped communities and a cited table without generation."""
import pytest

from su_multi_arm import SearchRequest
from su_multi_arm.providers import JsonModel
from tests.logs_gold_support import AS_OF, build_gold, gold_config


@pytest.fixture(scope="module")
def shared(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("shared-answer")
    config = gold_config(tmp)
    config["logs"]["compose"] = {"kinds": {"shared_error": True}}
    config["pipeline"]["arms"]["community"] = {"backend": "local"}
    return build_gold(tmp, data=config)[0]


def test_shared_error_table_is_grounded_and_model_free(shared, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("shared error table must not call a model")
    monkeypatch.setattr(JsonModel, "call", forbidden)
    result = shared.answer(SearchRequest(query="show shared errors across clients in last 10 days", as_of=AS_OF.isoformat(), mode="rules"))
    assert set(result.retrieval.arm_hits) == {"community", "graph"}
    assert result.retrieval.hits
    assert "| Error pattern | Client | Service | Day | Severity | Error count |" in result.answer.answer
    assert result.contract == "passed"
    assert result.trace["model_calls"] == []
    assert "Missing indexed days" in result.answer.answer
    assert "not a complete ranking" in result.answer.answer


def test_shared_error_members_are_tenant_and_service_scoped(shared):
    result = shared.answer(SearchRequest(query="shared errors for client t01 in svc-api in last 10 days", as_of=AS_OF.isoformat(), mode="rules"))
    assert result.retrieval.hits
    for hit in result.retrieval.hits:
        assert " · t01 · svc-api · " in hit.title
    assert "t02" not in result.answer.answer


def test_unknown_topic_is_not_an_unfiltered_shared_error_table(shared):
    result = shared.answer(SearchRequest(query="shared errors about nonexistent-provider in last 10 days", as_of=AS_OF.isoformat(), mode="rules"))
    assert not result.retrieval.hits
    assert result.answer.insufficient_evidence
