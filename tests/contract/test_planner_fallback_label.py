"""The deterministic fallback plan stays, labelled, and only for planner failures (staging finding,
2026-11-10: questions without a usable plan now get the labelled fallback instead of an abstention).

* a question the planner plans gets exactly the same decision, plan and hits whether
  ``crosssource.planner.fallback`` is ``deterministic_plan`` or ``abstain``;
* a planner failure gets the fallback, labelled in the decision trace, the coverage, the answer's
  limitations and the legacy envelope's ``plan.fallback``; with ``abstain`` it abstains as before;
* the fallback's knowledge steps are source-scoped like any knowledge step (no log record).

Synthetic two-halves fixture (``tests/two_halves_support.py``), fake providers only.
"""

from __future__ import annotations

from typing import Any

import pytest

from su_multi_arm import SearchRequest
from su_multi_arm.engine import Engine
from su_multi_arm.providers import JsonModel
from su_multi_arm.providers.base import SchemaViolation
from tests import two_halves_support as th

SUPPORT_QUESTIONS = ["How do I split a large sitemap?", "What is the sitemap URL limit?", "Where can I find the getting started guide?"]  # no operations half


@pytest.fixture(scope="module")
def pinned_clock():
    mp = pytest.MonkeyPatch()
    mp.setattr(Engine, "_clock", staticmethod(lambda zone: th.NOW.astimezone(zone)))
    yield
    mp.undo()


@pytest.fixture(scope="module")
def with_fallback(tmp_path_factory, pinned_clock):
    tmp = tmp_path_factory.mktemp("fallback_on")
    return th.build(tmp, th.unified_config(tmp, llm=True))


@pytest.fixture(scope="module")
def without_fallback(tmp_path_factory, pinned_clock):
    tmp = tmp_path_factory.mktemp("fallback_off")
    return th.build(tmp, th.unified_config(tmp, llm=True, planner={"fallback": "abstain"}))


def planner(valid: bool):
    generator = th.citing_generator()

    def respond(self, system, payload, schema, budget, stage):
        if stage != "planner":
            return generator(self, system, payload, schema, budget, stage)
        budget.reserve()
        if not valid:
            raise SchemaViolation("bad plan", raw='{"steps": "x"}', stage="planner", error_count=1, paths=["steps (list_type)"])
        return schema.model_validate({"steps": [{"arm": "lexical", "query": payload["question"]}, {"arm": "dense", "query": payload["question"]}]})

    return respond


def _decision(result: Any) -> dict[str, Any]:
    return {k: v for k, v in result.trace["decision"].items() if k != "seconds"}


@pytest.mark.parametrize("question", SUPPORT_QUESTIONS)
def test_a_planned_question_is_unchanged_by_the_fallback_setting(with_fallback, without_fallback, monkeypatch, question):
    monkeypatch.setattr(JsonModel, "call", planner(valid=True))
    a, b = with_fallback.answer(SearchRequest(query=question)), without_fallback.answer(SearchRequest(query=question))
    assert _decision(a) == _decision(b) and a.trace["decision"]["planner_fallback"] is None
    assert [(h.document_id, h.score) for h in a.retrieval.hits] == [(h.document_id, h.score) for h in b.retrieval.hits]
    assert [s["arm"] for s in a.trace["rounds"][0]["steps"]] == [s["arm"] for s in b.trace["rounds"][0]["steps"]] == ["lexical", "dense"]
    assert not any("deterministic plan" in x for x in a.limitations)


@pytest.mark.parametrize("question", SUPPORT_QUESTIONS)
def test_a_planner_failure_gets_the_labelled_fallback_or_abstains_when_configured(with_fallback, without_fallback, monkeypatch, question):
    monkeypatch.setattr(JsonModel, "call", planner(valid=False))
    a = with_fallback.answer(SearchRequest(query=question))
    decision = a.trace["decision"]
    assert decision["planner_fallback"] == "deterministic_plan" and decision["policy_applied"] == "deterministic_fallback_plan" and decision["failure"] == "schema_violation"
    assert a.trace["source_coverage"]["plan"]["fallback"] == "deterministic_plan"
    assert a.limitations[0].startswith("The planner could not form a valid plan (schema_violation)")
    assert a.retrieval.hits and all(h.source in {"ticket", "doc"} for h in a.retrieval.hits)  # scoped knowledge steps
    assert a.trace["source_coverage"]["knowledge_scope"]["sources"] == ["ticket", "doc"]
    b = without_fallback.answer(SearchRequest(query=question))
    assert b.trace["decision"]["outcome"] == "abstain" and b.trace["decision"]["planner_fallback"] is None and not b.retrieval.hits


def test_the_legacy_envelope_labels_the_fallback_plan(with_fallback, monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from su_multi_arm.compat.legacy_qa.app import create_legacy_app
    from su_multi_arm.compat.legacy_qa.profile import LegacyProfile

    profile = LegacyProfile.model_validate({"warmup": False, "datasets": [{"id": "support-qa", "unified": {"enabled": True, "config": str(tmp_path / "unified.yaml")}}]})
    client = TestClient(create_legacy_app(profile, engines={"support-qa": with_fallback}))
    monkeypatch.setattr(JsonModel, "call", planner(valid=False))
    body = client.post("/api/qa", json={"question": SUPPORT_QUESTIONS[0], "dataset": "support-qa"}).json()
    assert body["plan"]["fallback"] == "deterministic_plan" and body["plan"]["policy_applied"] == "deterministic_fallback_plan"
    assert body["coverage"]["plan"]["fallback"] == "deterministic_plan"
    monkeypatch.setattr(JsonModel, "call", planner(valid=True))
    plain = client.post("/api/qa", json={"question": SUPPORT_QUESTIONS[0], "dataset": "support-qa"}).json()
    assert "fallback" not in plain["plan"]
