"""ReviewStage (plan §3.9b) against fakes: bounded hops, every stop reason, re-plan through the
decision agent (never raw-all-arms), one vote per record per arm, masked follow-up queries.

The two v1 agentic tests (``test_agentic_no_false_missing_none_retry`` and
``test_agentic_hops_are_bounded_and_generation_cites_evidence``) are reproduced here with the same
intent; the originals in ``tests/test_core.py`` keep running against the v1 engine until it is ported.
"""

from __future__ import annotations

import json

import pytest

from su_multi_arm.core.component import Dependencies, Environment
from su_multi_arm.core.config import ChatRole
from su_multi_arm.core.errors import BudgetExhausted
from su_multi_arm.decision import Capabilities, DecisionRequest, RulesAgent, RulesConfig
from su_multi_arm.pipeline import JsonModelReviewer, Review, ReviewInput, ReviewStage, ReviewStageConfig
from su_multi_arm.providers import JsonModel
from su_multi_arm.selection import Candidate

CAPS = Capabilities(arms=["lexical", "dense"])


class ScriptedReviewer:
    def __init__(self, *reviews):
        self.reviews, self.calls = list(reviews), 0

    def review(self, ctx, question, evidence):
        self.calls += 1
        if isinstance(self.reviews[0], Exception):
            raise self.reviews[0]
        return self.reviews.pop(0) if len(self.reviews) > 1 else self.reviews[0]


class Harness:
    def __init__(self, found="kb:rounding", arm="lexical"):
        self.executed, self.found, self.arm = [], found, arm

    def execute(self, decision):
        self.executed.append(decision)
        return {self.arm: [Candidate(record_id=self.found, arm=self.arm, score=1.0)]}

    @staticmethod
    def repack(pools):
        return [{"id": c.record_id, "passage": "p"} for cs in pools.values() for c in cs]


def stage(reviewer, decision, max_hops=2):
    return ReviewStage(ReviewStageConfig(enabled=True, max_hops=max_hops), Dependencies(components={"reviewer": reviewer, "decision": decision}))


def inputs(query="certificate", **kw):
    return ReviewInput(
        request=DecisionRequest(query=query, filters={"project": "Alpha"}),
        capabilities=kw.pop("capabilities", CAPS),
        pools={"lexical": [Candidate(record_id="kb:certificate", arm="lexical", score=2.0)]},
        evidence=[{"id": "kb:certificate", "passage": "p"}],
        **kw,
    )


def rules(**cfg):
    return RulesAgent(RulesConfig(**cfg), Dependencies())


def test_no_false_missing_none_retry(v2_ctx):
    reviewer, harness = ScriptedReviewer(Review(sufficient=False, missing="none", followup_query="repeat")), Harness()
    outcome = stage(reviewer, rules()).run(v2_ctx, inputs(execute=harness.execute, repack=harness.repack))
    assert outcome.stop_reason == "no_actionable_gap" and reviewer.calls == 1 and harness.executed == []
    assert outcome.rounds[0].gap_present is False and outcome.hops_executed == 0 and outcome.queries == []


def test_hops_are_bounded_and_new_evidence_merges(v2_ctx):
    reviewer, harness = ScriptedReviewer(Review(sufficient=False, missing="Need rounding details", followup_query="rounding")), Harness()
    outcome = stage(reviewer, rules(), max_hops=2).run(v2_ctx, inputs(execute=harness.execute, repack=harness.repack))
    assert outcome.stop_reason == "hop_limit" and outcome.hops_executed == 1 and reviewer.calls == 1 and len(harness.executed) == 1
    assert harness.executed[0].plan.arms() == ["lexical", "dense"] and harness.executed[0].decided()
    assert [e["id"] for e in outcome.evidence] == ["kb:certificate", "kb:rounding"] and outcome.rounds[0].new_records == 1
    replan = next(s for s in v2_ctx.trace.spans if s.name == "decision")
    assert replan.attributes["agent"] == "rules" and replan.attributes["outcome"] == "decided"
    reviewer, harness = ScriptedReviewer(Review(sufficient=False, missing="more", followup_query="rounding"), Review(sufficient=False, missing="more", followup_query="precision")), Harness()
    outcome = stage(reviewer, rules(), max_hops=3).run(v2_ctx, inputs(execute=harness.execute, repack=harness.repack))
    assert outcome.stop_reason == "hop_limit" and outcome.hops_executed == 2 and [r.hop for r in outcome.rounds] == [1, 2]


def test_stop_reasons_sufficient_repeated_and_budget(v2_ctx):
    harness = Harness()
    done = stage(ScriptedReviewer(Review(sufficient=True)), rules()).run(v2_ctx, inputs(execute=harness.execute, repack=harness.repack))
    assert done.stop_reason == "sufficient" and done.rounds[0].sufficient is True
    repeated = stage(ScriptedReviewer(Review(sufficient=False, missing="x", followup_query="Certificate")), rules())
    assert repeated.run(v2_ctx, inputs(execute=harness.execute, repack=harness.repack)).stop_reason == "repeated_query"
    broke = stage(ScriptedReviewer(BudgetExhausted("calls", 1, 0)), rules())
    outcome = broke.run(v2_ctx, inputs(execute=harness.execute, repack=harness.repack))
    assert outcome.stop_reason == "budget_exhausted" and harness.executed == []
    assert outcome.evidence == [{"id": "kb:certificate", "passage": "p"}]


def test_replan_goes_through_decision_agent_and_never_raw_all_arms(v2_ctx):
    harness = Harness()
    reviewer = ScriptedReviewer(Review(sufficient=False, missing="a count", followup_query="how many rounding records exist"))
    outcome = stage(reviewer, rules()).run(v2_ctx, inputs(execute=harness.execute, repack=harness.repack))
    assert outcome.stop_reason == "replan_abstained" and outcome.rounds[0].decision_outcome == "abstain" and harness.executed == []
    reviewer = ScriptedReviewer(Review(sufficient=False, missing="a count", followup_query="how many rounding records exist"))
    outcome = stage(reviewer, rules(on_failure="raw_all_arms")).run(v2_ctx, inputs(execute=harness.execute, repack=harness.repack))
    assert outcome.stop_reason == "replan_abstained" and outcome.rounds[0].decision_outcome == "raw_all_arms" and harness.executed == []
    harness = Harness()
    reviewer = ScriptedReviewer(Review(sufficient=False, missing="rounding", followup_query="rounding"))
    outcome = stage(reviewer, rules()).run(v2_ctx, inputs(execute=harness.execute, repack=harness.repack))
    assert harness.executed[0].plan.steps[0].filters == {}  # request filters live on the request, never rewritten
    assert outcome.pools["lexical"][0].record_id == "kb:rounding" or outcome.pools["lexical"][1].record_id == "kb:rounding"


def test_one_vote_per_record_per_arm_across_hops_and_masked_followups(v2_ctx, tmp_path):
    harness = Harness(found="kb:certificate")
    reviewer = ScriptedReviewer(Review(sufficient=False, missing="dup", followup_query="certificate again"))
    outcome = stage(reviewer, rules(), max_hops=3).run(v2_ctx, inputs(execute=harness.execute, repack=harness.repack))
    assert outcome.rounds[0].new_records == 0 and len(outcome.pools["lexical"]) == 1 and outcome.pools["lexical"][0].score == 2.0
    assert outcome.stop_reason == "repeated_query" and outcome.queries == ["certificate again"]
    records = v2_ctx.trace.records()
    review = next(r for r in records if r["name"] == "review")
    followup = next(e for e in review["events"] if e["name"] == "followup")
    assert followup["followup_query"]["masked"] and "certificate again" not in json.dumps(records)
    assert review["attributes"]["stop_reason"] == "repeated_query"


def test_validate_and_default_reviewer_use_review_schema(v2_ctx, monkeypatch):
    unwired = ReviewStage(ReviewStageConfig(enabled=True), Dependencies())
    assert [d.code for d in unwired.validate(Environment())] == ["missing_reviewer", "missing_decision"]
    assert ReviewStage(ReviewStageConfig(), Dependencies()).validate(Environment()) == []
    with pytest.raises(ValueError, match="not wired"):
        unwired.run(v2_ctx, inputs(execute=lambda d: {}, repack=lambda p: []))
    seen = []

    def respond(self, system, payload, schema, budget, stage_name):
        budget.reserve()
        seen.append((schema, stage_name, payload))
        budget.record({"stage": stage_name, "provider": "openai_compatible", "model": "fake", "success": True, "seconds": 0.0, "usage": {}})
        return Review(sufficient=True)

    monkeypatch.setattr(JsonModel, "call", respond)
    reviewer = JsonModelReviewer(ChatRole(provider="openai_compatible", model="fake"))
    assert reviewer.review(v2_ctx, "q", [{"id": "x"}]).sufficient is True
    assert seen[0][0] is Review and seen[0][1] == "review" and seen[0][2] == {"question": "q", "evidence": [{"id": "x"}]}
    assert v2_ctx.budget.usage.used["calls"] == 1


# --- release review B2: a re-planned hop may start with a structured step ---------------------------


def structured_first_replan(monkeypatch):
    from su_multi_arm.models import Answer

    from tests.contract.test_engine_support_example import plan

    calls = {"planner": 0}

    def respond(self, system, payload, schema, budget, stage):
        if stage == "planner":
            calls["planner"] += 1
            if calls["planner"] == 1:
                return plan({"arm": "lexical", "query": "certificate"})
            return plan({"arm": "structured", "scope": {"measure": {"kind": "count"}}}, {"arm": "lexical", "query": "rounding"})
        if stage == "review":
            return Review(sufficient=False, missing="Need count", followup_query="how many")
        cited = payload["evidence"][0]["id"]
        return Answer(answer=f"See [{cited}].", citations=[cited])

    monkeypatch.setattr(JsonModel, "call", respond)
    return calls


@pytest.fixture
def agentic(config, documents):
    from tests.contract.test_engine_support_example import llm_engine

    return llm_engine(config, documents, mode="agentic", generator=True)


def test_structured_first_replan_runs_through_engine_answer(agentic, monkeypatch):
    structured_first_replan(monkeypatch)
    result = agentic.answer("certificate")
    assert len(result.trace["rounds"]) == 2
    second = result.trace["rounds"][1]
    assert second["query"] == "rounding"  # the first retrieval step's query labels the round
    assert [s["arm"] for s in second["steps"]] == ["structured", "lexical"]
    assert result.retrieval.aggregates and result.retrieval.aggregates[0]["value"] >= 1
    assert result.contract == "passed"


def test_structured_first_replan_over_http_and_stream_is_not_an_internal_error(agentic, monkeypatch):
    TestClient = pytest.importorskip("fastapi.testclient").TestClient

    from su_multi_arm.runtime.events import DoneEvent, ErrorEvent
    from su_multi_arm.serving.http import create_app

    structured_first_replan(monkeypatch)
    client = TestClient(create_app(engine=agentic))
    response = client.post("/v2/answer", json={"query": "certificate"})
    assert response.status_code == 200 and len(response.json()["trace"]["rounds"]) == 2
    streamed = client.post("/v2/answer/stream", json={"query": "certificate"})
    assert streamed.status_code == 200 and "event: error" not in streamed.text and "event: done" in streamed.text
    events = list(agentic.answer_stream("certificate"))
    assert not any(isinstance(e, ErrorEvent) for e in events) and isinstance(events[-1], DoneEvent)
