"""The nine natural-language count probe shapes from the public HybridQA diagnostic (docs/VALIDATION.md:
0/9 correct; planner-invented identifiers gave confident zero counts and an unsupported numeric total
was misrepresented as a record count), reproduced as synthetic requests (plan Phase 0 acceptance).

Under ``fail_closed`` every shape abstains with the failing constraint flagged; under
``raw_all_arms`` every shape yields a raw plan with a non-empty ``unsupported`` list and no scope.
Either way a count claim is refused by the ``AnswerContract`` and no number is produced.
"""

from __future__ import annotations

import pytest

from su_multi_arm.core.component import Dependencies
from su_multi_arm.core.config import ChatRole
from su_multi_arm.core.config_decision import StagePolicies
from su_multi_arm.decision import CascadeAgent, CascadeConfig, DecisionRequest, LlmAgent, LlmConfig, RulesAgent, RulesConfig
from su_multi_arm.generation import Answer, AnswerContract, Claim
from su_multi_arm.providers import JsonModel

# (question shape, historical failure mode, a planner reply that would fabricate the number)
PROBES = [
    ("How many rows are in table T-404?", "invented table identifier", {"arm": "structured", "scope": {"predicates": [{"field": "external_id", "op": "eq", "value": "T-404"}], "measure": {"kind": "count"}}}),
    ("What is the total amount for project Delta?", "value not observed", {"arm": "structured", "scope": {"predicates": [{"field": "project", "op": "eq", "value": "Delta"}], "measure": {"kind": "sum", "field": "amount"}}}),
    ("What is the total of all records?", "numeric total answered with a record count", {"arm": "structured", "scope": {"measure": {"kind": "count"}}}),
    ("How many records are in region north?", "qualifier on a non-filterable field", {"arm": "structured", "scope": {"predicates": [{"field": "region", "op": "eq", "value": "north"}], "measure": {"kind": "count"}}}),
    ("How many records were created in March 2024?", "calendar qualifier without an offset", {"arm": "structured", "scope": {"predicates": [{"field": "created_at", "op": "date_interval", "lower": "2024-03", "granularity": "month"}], "measure": {"kind": "count"}}}),
    ("List all billing records", "exhaustive list over an unbound topic", {"arm": "structured", "scope": {"predicates": [{"field": "status", "op": "eq", "value": "billing"}]}}),
    ("Are there any records for project Omega?", "absence with an unobserved value", {"arm": "structured", "scope": {"predicates": [{"field": "project", "op": "eq", "value": "Omega"}], "measure": {"kind": "count"}}}),
    ("When was the earliest record?", "earliest without a declared time basis", {"arm": "structured", "scope": {"measure": {"kind": "first_seen", "field": "created_at"}}}),
    ("How many open records are there in table 7?", "count scoped to a non-existent table", {"arm": "structured", "scope": {"predicates": [{"field": "status", "op": "eq", "value": "open"}, {"field": "tag", "op": "eq", "value": "7"}], "measure": {"kind": "count"}}}),
]
FAKE_COUNT = Answer(answer="There are 7 matching records.", claims=[Claim(text="7 records", kind="count", evidence_ids=["kb:a"])], citations=["kb:a"])


def has_number(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


def no_fabrication(decision):
    assert decision.scope is None and decision.outcome in {"abstain", "raw_all_arms"}
    assert decision.unsupported, "a flagged constraint is required"
    if decision.outcome == "raw_all_arms":
        assert "structured" not in decision.plan.arms() and decision.plan.arms()
    result = AnswerContract().enforce(FAKE_COUNT, evidence_ids={"kb:a"}, aggregates=[], intents=decision.intents, unsupported=decision.unsupported)
    assert result.contract == "abstained" and not has_number(result.answer.answer) and result.unsupported_constraints


@pytest.mark.parametrize("policy", ["fail_closed", "raw_all_arms"])
@pytest.mark.parametrize(("question", "mode", "_reply"), PROBES, ids=[p[1] for p in PROBES])
def test_rules_agent_never_fabricates_a_count(v2_ctx, v2_backend, v2_caps, policy, question, mode, _reply):
    agent = RulesAgent(RulesConfig(on_failure=policy), Dependencies(backends={"records": v2_backend.records}))
    decision = agent.decide(v2_ctx, DecisionRequest(query=question), None, v2_caps)
    assert decision.intents.aggregate(), mode
    assert decision.outcome == ("abstain" if policy == "fail_closed" else "raw_all_arms")
    no_fabrication(decision)


@pytest.mark.parametrize("policy", ["fail_closed", "raw_all_arms"])
@pytest.mark.parametrize(("question", "mode", "reply"), PROBES, ids=[p[1] for p in PROBES])
def test_cascade_with_fabricating_planner_never_fabricates_a_count(v2_ctx, v2_backend, v2_caps, monkeypatch, policy, question, mode, reply):
    def respond(self, system, payload, schema, budget, stage):
        budget.reserve()
        return schema.model_validate({"steps": [reply]})

    monkeypatch.setattr(JsonModel, "call", respond)
    deps = Dependencies(backends={"records": v2_backend.records})
    stages = {
        "rules": RulesAgent(RulesConfig(), deps),
        "llm": LlmAgent(LlmConfig(role=ChatRole(provider="openai_compatible", model="fake")), deps),
    }
    config = CascadeConfig(stages=["rules", "llm"], on_failure=policy, stage_policies=StagePolicies(llm="fallback_to_next_stage"))
    decision = CascadeAgent(config, Dependencies(components=stages)).decide(v2_ctx, DecisionRequest(query=question), None, v2_caps)
    assert decision.failure == "all_stages_failed" and decision.outcome == ("abstain" if policy == "fail_closed" else "raw_all_arms")
    no_fabrication(decision)
    strict = CascadeAgent(CascadeConfig(stages=["rules", "llm"], on_failure=policy), Dependencies(components=stages))
    decision = strict.decide(v2_ctx, DecisionRequest(query=question), None, v2_caps)
    assert decision.outcome == "abstain" and decision.stage == "llm"  # default llm stage policy: abstain, even under raw_all_arms
    no_fabrication(decision)


@pytest.mark.parametrize(
    "question",
    [
        "Show me all the steps to renew a certificate",
        "What happens every time the service restarts?",
        "Which article explains the latest release notes?",
        "Why does the export fail after every upgrade?",
        "What is the total cost of ownership described in the guide?",
        "Is there any way to reset the connector token?",
        "What was the last recorded message in the log before the crash?",
    ],
)
def test_rules_agent_retrieves_plain_questions_that_merely_contain_aggregate_words(v2_ctx, v2_backend, v2_caps, question):
    """Review B4: the default rules agent must not abstain on ordinary retrieval questions."""
    agent = RulesAgent(RulesConfig(), Dependencies(backends={"records": v2_backend.records}))
    decision = agent.decide(v2_ctx, DecisionRequest(query=question), None, v2_caps)
    assert decision.decided() and decision.scope is None and not decision.intents.aggregate(), question
    assert decision.plan.arms() == ["dense", "lexical", "graph", "community"] and decision.unsupported == []


def test_exactly_resolvable_shapes_still_count(v2_ctx, v2_backend, v2_caps, v2_gen):
    """The guard is about fabrication, not about refusing every count."""
    agent = RulesAgent(RulesConfig(), Dependencies(backends={"records": v2_backend.records}))
    decision = agent.decide(v2_ctx, DecisionRequest(query="How many records have status open?"), None, v2_caps)
    assert decision.decided() and v2_backend.records.execute(v2_gen, decision.scope, []).value == 3
    decision = agent.decide(v2_ctx, DecisionRequest(query="What is the total amount for project Alpha?"), None, v2_caps)
    assert decision.scope.measure.kind == "sum" and v2_backend.records.execute(v2_gen, decision.scope, []).value == 30.5
    decision = agent.decide(v2_ctx, DecisionRequest(query="When was the earliest created record?"), None, v2_caps)
    assert decision.scope.measure.kind == "first_seen" and v2_backend.records.execute(v2_gen, decision.scope, []).value["record_id"] == "r4"
    decision = agent.decide(v2_ctx, DecisionRequest(query="List all open records"), None, v2_caps)
    assert decision.scope.measure is None and v2_backend.records.execute(v2_gen, decision.scope, []).matched_ids == ["r1", "r3", "r4"]
    decision = agent.decide(v2_ctx, DecisionRequest(query="Are there any pending records?"), None, v2_caps)
    assert decision.scope.measure.kind == "count" and v2_backend.records.execute(v2_gen, decision.scope, []).value == 1
    decision = agent.decide(v2_ctx, DecisionRequest(query="REC-1001 details"), None, v2_caps)
    assert decision.decided() and decision.intents.exact_ids == ["REC-1001"] and decision.plan.arms() == ["dense", "lexical", "graph", "community"]
