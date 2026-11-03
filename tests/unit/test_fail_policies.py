"""Fail-policy table (plan §3.7.1): one test per row; fail_closed is the default everywhere."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from su_multi_arm.core.component import ArtifactRef, ClassifierManifest, Dependencies, Environment
from su_multi_arm.core.config import DecisionConfig
from su_multi_arm.core.config_decision import StagePolicies
from su_multi_arm.core.types import Measure
from su_multi_arm.decision import (
    Capabilities,
    CascadeAgent,
    CascadeConfig,
    ClassifierAgent,
    ClassifierConfig,
    ClassifierOutput,
    DecisionAgent,
    DecisionFailure,
    DecisionRequest,
    LlmAgent,
    LlmConfig,
    Plan,
    RetrievalStep,
    RulesAgent,
    RulesConfig,
    detect_intents,
)
from su_multi_arm.decision.base import Decision
from su_multi_arm.core.config import ChatRole
from su_multi_arm.providers import JsonModel
from su_multi_arm.providers.base import ProviderError

COUNT = "How many records have status open?"
NO_STRUCTURED = Capabilities(arms=["dense", "lexical"])


class Deciding(DecisionAgent):
    """A stage that always decides (stands in for a later cascade stage)."""

    name = "deciding"

    def attempt(self, ctx, request, intents, capabilities):
        return Decision(plan=Plan(steps=[RetrievalStep(arm="dense", query=request.query)]), outcome="decided", agent=self.name, policy_applied="x", intents=intents)


class Failing(DecisionAgent):
    name = "failing"

    def attempt(self, ctx, request, intents, capabilities):
        raise DecisionFailure("no_plan", "nothing")


class FakeClassifier:
    def __init__(self, label="lexical", confidence=0.9):
        self.label, self.confidence = label, confidence

    def classify(self, query, intents):
        return ClassifierOutput(label=self.label, confidence=self.confidence)


def artifact(tmp_path: Path, threshold=0.6) -> ArtifactRef:
    path = tmp_path / "model.onnx"
    path.write_bytes(b"synthetic-onnx")
    return ArtifactRef(model_path=path, sha256=hashlib.sha256(path.read_bytes()).hexdigest(), manifest=ClassifierManifest(features="hashed_ngrams", labels=["lexical", "dense"], threshold=threshold))


def rules(backend, **cfg):
    return RulesAgent(RulesConfig(**cfg), Dependencies(backends={"records": backend.records}))


def llm(backend, **cfg):
    return LlmAgent(LlmConfig(role=ChatRole(provider="openai_compatible", model="fake"), **cfg), Dependencies(backends={"records": backend.records}))


def decision_span(ctx):
    return next(s for s in ctx.trace.spans if s.name == "decision")


# --- rows ------------------------------------------------------------------------------------------


def test_row_intent_pre_pass_never_fails_and_feeds_decisions(v2_ctx, v2_backend, v2_caps):
    agent = rules(v2_backend)
    decision = agent.decide(v2_ctx, DecisionRequest(query="☃ ???"), None, v2_caps)
    assert decision.intents == detect_intents("☃ ???", v2_caps.id_patterns) and decision.decided()


def test_row_rules_standalone_default_abstain_and_raw_all_arms_opt_in(v2_ctx, v2_backend):
    default = rules(v2_backend).decide(v2_ctx, DecisionRequest(query=COUNT), None, NO_STRUCTURED)
    assert default.outcome == "abstain" and default.failure == "no_plan" and default.plan is None and default.scope is None
    assert default.policy_applied == "fail_closed" and default.unsupported[0].reason == "aggregate_intent_without_scope"
    assert decision_span(v2_ctx).attributes["decision_fallback"] is False
    raw = rules(v2_backend, on_failure="raw_all_arms").decide(v2_ctx, DecisionRequest(query=COUNT), None, NO_STRUCTURED)
    assert raw.outcome == "raw_all_arms" and raw.plan.arms() == ["dense", "lexical"] and raw.scope is None
    assert len(raw.unsupported) == 1 and raw.unsupported[0].reason == "aggregate_intent_without_scope"
    assert raw.failure == "no_plan" and [s for s in v2_ctx.trace.spans if s.name == "decision"][-1].attributes["decision_fallback"] is True
    assert DecisionConfig().on_failure == "fail_closed" and "fail_closed" in str(DecisionConfig.model_json_schema()["properties"]["on_failure"]["default"])


def test_row_rules_cascade_stage_fallback_default_or_abstain(v2_ctx, v2_backend):
    deps = Dependencies(components={"rules": rules(v2_backend), "llm": Deciding(RulesConfig(), Dependencies())})
    assert StagePolicies().rules == "fallback_to_next_stage"
    cascade = CascadeAgent(CascadeConfig(stages=["rules", "llm"]), deps)
    decision = cascade.decide(v2_ctx, DecisionRequest(query=COUNT), None, NO_STRUCTURED)
    assert decision.decided() and decision.stage == "llm" and decision.policy_applied == "cascade:llm"
    assert any(e["name"] == "stage_failed" and e["stage"] == "rules" and e["policy"] == "fallback_to_next_stage" for e in decision_span(v2_ctx).events)
    strict = CascadeAgent(CascadeConfig(stages=["rules", "llm"], stage_policies=StagePolicies(rules="abstain")), deps)
    decision = strict.decide(v2_ctx, DecisionRequest(query=COUNT), None, NO_STRUCTURED)
    assert decision.outcome == "abstain" and decision.stage == "rules" and decision.failure == "no_plan"


def test_row_classifier_standalone_confidence_and_artifact(v2_ctx, tmp_path):
    caps = Capabilities(arms=["dense", "lexical"])
    low = ClassifierAgent(ClassifierConfig(artifact=artifact(tmp_path)), Dependencies(components={"classifier": FakeClassifier(confidence=0.3)}))
    assert low.validate(Environment(base_path=tmp_path)) == []
    decision = low.decide(v2_ctx, DecisionRequest(query="certificate"), None, caps)
    assert decision.outcome == "abstain" and decision.failure == "low_confidence" and decision.confidence == 0.3
    raw = ClassifierAgent(ClassifierConfig(artifact=artifact(tmp_path), on_failure="raw_all_arms"), Dependencies(components={"classifier": FakeClassifier(confidence=0.3)}))
    raw.validate(Environment(base_path=tmp_path))
    decision = raw.decide(v2_ctx, DecisionRequest(query="certificate"), None, caps)
    assert decision.outcome == "raw_all_arms" and decision.confidence == 0.3 and decision.unsupported == []
    good = ClassifierAgent(ClassifierConfig(artifact=artifact(tmp_path)), Dependencies(components={"classifier": FakeClassifier(confidence=0.95)}))
    good.validate(Environment(base_path=tmp_path))
    decision = good.decide(v2_ctx, DecisionRequest(query="certificate"), None, caps)
    assert decision.decided() and decision.plan.arms() == ["lexical"] and decision.confidence == 0.95
    bad = artifact(tmp_path).model_copy(update={"sha256": "0" * 64})
    broken = ClassifierAgent(ClassifierConfig(artifact=bad), Dependencies(components={"classifier": FakeClassifier()}))
    assert [d.code for d in broken.validate(Environment(base_path=tmp_path))] == ["artifact_invalid"]
    decision = broken.decide(v2_ctx, DecisionRequest(query="certificate"), None, caps)
    assert decision.outcome == "abstain" and decision.failure == "artifact_invalid"
    aggregate = good.decide(v2_ctx, DecisionRequest(query=COUNT), None, caps)
    assert aggregate.outcome == "abstain" and aggregate.failure == "no_plan" and aggregate.unsupported[0].reason == "aggregate_intent_without_scope"


def test_row_classifier_cascade_stage(v2_ctx, tmp_path):
    caps = Capabilities(arms=["dense", "lexical"])
    classifier = ClassifierAgent(ClassifierConfig(artifact=artifact(tmp_path)), Dependencies(components={"classifier": FakeClassifier(confidence=0.2)}))
    classifier.validate(Environment(base_path=tmp_path))
    deps = Dependencies(components={"classifier": classifier, "rules": Deciding(RulesConfig(), Dependencies())})
    assert StagePolicies().classifier == "fallback_to_next_stage"
    decision = CascadeAgent(CascadeConfig(stages=["classifier", "rules"]), deps).decide(v2_ctx, DecisionRequest(query="q"), None, caps)
    assert decision.decided() and decision.stage == "rules"
    strict = CascadeAgent(CascadeConfig(stages=["classifier", "rules"], stage_policies=StagePolicies(classifier="abstain")), deps)
    decision = strict.decide(v2_ctx, DecisionRequest(query="q"), None, caps)
    assert decision.outcome == "abstain" and decision.stage == "classifier" and decision.confidence == 0.2


@pytest.mark.parametrize(
    ("reply", "category", "reason"),
    [
        ({"steps": [{"arm": "dense", "scope": {"measure": {"kind": "count"}}}]}, "schema_violation", None),
        ({"steps": [{"arm": "cache", "query": "x"}]}, "schema_violation", None),
        ({"steps": [{"arm": "graph", "query": "x"}]}, "invalid_step", None),  # disabled arm
        ({"steps": [{"arm": "dense", "query": "x", "filters": {"region": "north"}}]}, "invalid_step", "field_not_filterable"),
        ({"steps": [{"arm": "dense", "query": "x", "filters": {"project": "Beta"}}]}, "invalid_step", None),  # overrides request filter
        ({"steps": [{"arm": "dense", "query": "x", "filters": {"status": "archived"}}]}, "unresolvable_value", "value_not_observed"),
        ({"steps": [{"arm": "structured", "scope": {"predicates": [{"field": "external_id", "op": "eq", "value": "REC-9999"}], "measure": {"kind": "count"}}}]}, "unresolvable_value", "value_not_observed"),
        ({"steps": [{"arm": "structured", "scope": {"measure": {"kind": "sum", "field": "project"}}}]}, "unsupported_measure", "measure_unsupported"),
        ({"steps": [{"arm": "dense", "query": "x"}] * 3}, "invalid_step", None),  # over max_steps
    ],
)
def test_row_llm_failure_categories_default_abstain(v2_ctx, v2_backend, v2_schema, monkeypatch, reply, category, reason):
    def respond(self, system, payload, schema, budget, stage):
        budget.reserve()
        return schema.model_validate(reply)

    monkeypatch.setattr(JsonModel, "call", respond)
    caps = Capabilities(arms=["dense", "lexical", "structured"], metadata_schema=v2_schema, max_steps=2)
    decision = llm(v2_backend).decide(v2_ctx, DecisionRequest(query="How many records?", filters={"project": "Alpha"}), None, caps)
    assert decision.outcome == "abstain" and decision.failure == category and decision.plan is None
    if reason:
        assert decision.unsupported and decision.unsupported[0].reason == reason
    assert decision_span(v2_ctx).events[-1]["category"] == category


def test_row_llm_timeout_budget_provider_and_raw_opt_in(v2_ctx, v2_backend, v2_schema, monkeypatch):
    import httpx

    from su_multi_arm.runtime.budget import Budget, BudgetLimits
    from su_multi_arm.runtime.context import RunContext

    caps = Capabilities(arms=["dense", "lexical", "structured"], metadata_schema=v2_schema)

    def timeout(self, system, payload, schema, budget, stage):
        budget.reserve()
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(JsonModel, "call", timeout)
    assert llm(v2_backend).decide(v2_ctx, DecisionRequest(query="q"), None, caps).failure == "timeout"

    def http_error(self, system, payload, schema, budget, stage):
        budget.reserve()
        raise ProviderError("provider HTTP 500", stage="planner", status=500)

    monkeypatch.setattr(JsonModel, "call", http_error)
    assert llm(v2_backend).decide(v2_ctx, DecisionRequest(query="q"), None, caps).failure == "provider_error"

    def ok(self, system, payload, schema, budget, stage):
        budget.reserve()
        return schema.model_validate({"steps": [{"arm": "dense", "query": "q"}]})

    monkeypatch.setattr(JsonModel, "call", ok)
    tight = RunContext(generation=v2_ctx.generation, budget=Budget(BudgetLimits(max_llm_calls=1)))
    tight.budget.reserve("calls", 1)
    assert llm(v2_backend).decide(tight, DecisionRequest(query="q"), None, caps).failure == "budget_exhausted"
    raw = llm(v2_backend, on_failure="raw_all_arms").decide(tight, DecisionRequest(query=COUNT), None, caps)
    assert raw.outcome == "raw_all_arms" and raw.plan.arms() == ["dense", "lexical"] and raw.unsupported[0].reason == "aggregate_intent_without_scope"
    fine = llm(v2_backend).decide(v2_ctx, DecisionRequest(query="q"), None, caps)
    assert fine.decided() and fine.plan.arms() == ["dense"]


def test_row_llm_cascade_stage_default_abstain(v2_ctx, v2_backend, v2_schema, monkeypatch):
    def respond(self, system, payload, schema, budget, stage):
        budget.reserve()
        return schema.model_validate({"steps": [{"arm": "graph", "query": "x"}]})

    monkeypatch.setattr(JsonModel, "call", respond)
    caps = Capabilities(arms=["dense", "lexical", "structured"], metadata_schema=v2_schema)
    deps = Dependencies(components={"llm": llm(v2_backend), "rules": Deciding(RulesConfig(), Dependencies())})
    assert StagePolicies().llm == "abstain"
    decision = CascadeAgent(CascadeConfig(stages=["llm", "rules"]), deps).decide(v2_ctx, DecisionRequest(query="q"), None, caps)
    assert decision.outcome == "abstain" and decision.stage == "llm" and decision.failure == "invalid_step"
    lenient = CascadeAgent(CascadeConfig(stages=["llm", "rules"], stage_policies=StagePolicies(llm="fallback_to_next_stage")), deps)
    assert lenient.decide(v2_ctx, DecisionRequest(query="q"), None, caps).stage == "rules"


def test_row_cascade_every_stage_failed(v2_ctx, v2_backend):
    deps = Dependencies(components={"rules": rules(v2_backend), "classifier": Failing(RulesConfig(), Dependencies())})
    decision = CascadeAgent(CascadeConfig(stages=["rules", "classifier"]), deps).decide(v2_ctx, DecisionRequest(query=COUNT), None, NO_STRUCTURED)
    assert decision.outcome == "abstain" and decision.failure == "all_stages_failed" and decision.stage is None
    raw = CascadeAgent(CascadeConfig(stages=["rules", "classifier"], on_failure="raw_all_arms"), deps)
    decision = raw.decide(v2_ctx, DecisionRequest(query=COUNT), None, NO_STRUCTURED)
    assert decision.outcome == "raw_all_arms" and decision.failure == "all_stages_failed" and decision.unsupported
    with pytest.raises(ValueError, match="no agent"):
        CascadeAgent(CascadeConfig(stages=["llm"]), Dependencies())


def test_request_filters_immutable_and_first_decided_stage_wins(v2_ctx, v2_backend, v2_caps):
    request = DecisionRequest(query="What is the total amount for project Alpha?", filters={"status": "open"})
    decision = rules(v2_backend).decide(v2_ctx, request, None, v2_caps)
    assert decision.decided() and decision.scope.measure == Measure(kind="sum", field="amount")
    assert [(p.field, p.value) for p in decision.scope.predicates] == [("status", "open"), ("project", "Alpha")]
    deps = Dependencies(components={"rules": rules(v2_backend), "llm": Failing(RulesConfig(), Dependencies())})
    first = CascadeAgent(CascadeConfig(stages=["rules", "llm"]), deps).decide(v2_ctx, request, None, v2_caps)
    assert first.stage == "rules" and first.decided()


@pytest.mark.parametrize("on_failure", ["fail_closed", "raw_all_arms"])
def test_llm_agent_lets_cancellation_and_backend_errors_propagate_typed(v2_ctx, v2_backend, v2_schema, monkeypatch, on_failure):
    """Release review N5: RunCancelled/BackendError are RuntimeErrors; the planner's provider-error
    catch must not turn them into a fail-policy outcome (raw_all_arms would then fail open)."""
    from su_multi_arm.core.errors import BackendError, RunCancelled

    caps = Capabilities(arms=["dense", "lexical"], metadata_schema=v2_schema)
    for error in (RunCancelled("client went away"), BackendError("postgres", "values", "down")):

        def fail(self, system, payload, schema, budget, stage, error=error):
            raise error

        monkeypatch.setattr(JsonModel, "call", fail)
        with pytest.raises(type(error)):
            llm(v2_backend, on_failure=on_failure).decide(v2_ctx, DecisionRequest(query="q"), None, caps)
