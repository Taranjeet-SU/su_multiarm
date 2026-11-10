"""Incident-seeded graph/community route, using the synthetic gold pipeline only."""
from datetime import timedelta

import pytest

from su_multi_arm import SearchRequest
from su_multi_arm.providers import JsonModel
from tests.logs_gold_support import AS_OF, build_gold, gold_config

ANCHOR = AS_OF - timedelta(days=4)


@pytest.fixture(scope="module")
def gold(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("outages")
    config = gold_config(tmp)
    config["pipeline"]["arms"]["community"] = {"backend": "local"}
    engine, _, _ = build_gold(tmp, data=config)
    return engine


def test_exact_outage_query_uses_community_graph_without_any_model(gold, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("outage table must not call a model")
    monkeypatch.setattr(JsonModel, "call", forbidden)
    result = gold.answer(SearchRequest(query="last 3 day outage", as_of=ANCHOR.isoformat(), mode="rules"))
    assert set(result.retrieval.arm_hits) == {"community", "graph"}
    assert result.trace["model_calls"] == []
    assert result.retrieval.hits
    assert next(s for s in result.retrieval.trace["steps"] if s["arm"] == "graph")["paths"] > 0
    assert "| Client | Services | Shared issue patterns |" in result.answer.answer
    assert "not confirmed outages" in result.answer.answer
    assert result.contract == "passed"
    reports = [r for r in result.retrieval.trace["reports"] if r.get("kind") == "log_investigation_scope"]
    assert all(r["start"] == (ANCHOR-timedelta(days=2)).isoformat() for r in reports)
    assert all(r["end_exclusive"] == (ANCHOR+timedelta(days=1)).isoformat() for r in reports)


def test_unrelated_named_outage_subject_does_not_return_every_incident(gold):
    result = gold.answer(SearchRequest(query="last 3 day outage for nonexistent-provider", as_of=ANCHOR.isoformat(), mode="rules"))
    assert not result.retrieval.hits
    assert result.answer.insufficient_evidence
    assert "does not establish" in result.answer.answer


def test_client_and_service_constraints_are_applied_to_incident_members(gold):
    for query in ["outages for client t01 in last 3 days", "outages in svc-legacy in last 3 days"]:
        result = gold.answer(SearchRequest(query=query, as_of=ANCHOR.isoformat(), mode="rules"))
        # Gold's co-firing incident belongs to t02 and specific services, neither requested scope.
        assert not result.retrieval.hits
        assert result.answer.insufficient_evidence


def test_matching_client_and_member_service_retain_incident_evidence(gold):
    result = gold.answer(SearchRequest(query="last 3 day outage for client t02 in svc-worker", as_of=ANCHOR.isoformat(), mode="rules"))
    assert result.retrieval.hits and all("t02" in hit.title for hit in result.retrieval.hits)
    assert result.contract == "passed" and "svc-worker" in result.answer.answer
