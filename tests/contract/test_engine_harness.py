"""Phase 0 acceptance through ``Engine`` end to end (plan §4 Phase 0, §3.7–§3.9b): the nine NL-count
probe shapes via ``Engine.answer`` under ``fail_closed`` and ``raw_all_arms`` yield abstentions or
flagged unsupported constraints and zero fabricated numbers; an exactly resolvable count still
counts; the review stage re-plans through the configured decision agent; ``ordering:
reciprocal_rank`` reproduces the alpha's ``per_arm`` order on the shipped local example; packing is
fair and honours the configured field priority; coverage flows into ``AnswerResult.coverage``."""

from __future__ import annotations

import pytest

from su_multi_arm import Document, Engine, SearchRequest
from su_multi_arm.core.config import validate_config
from su_multi_arm.decision import LlmPlan
from su_multi_arm.fusion import FusionLike, fuse
from su_multi_arm.models import Aggregate, Answer, Claim
from su_multi_arm.pipeline import Review
from su_multi_arm.providers import JsonModel

from conftest import v2_schema_model  # tests/conftest.py (rootdir conftest, on sys.path)

FAKE = {"provider": "openai_compatible", "model": "fake"}
# (question shape, historical failure mode, a planner reply that would fabricate the number)
PROBES = [
    ("How many rows are in table T-404?", "invented table identifier", {"arm": "structured", "scope": {"predicates": [{"field": "external_id", "op": "eq", "value": "T-404"}], "measure": {"kind": "count"}}}),
    ("What is the total amount for project Delta?", "value not observed", {"arm": "structured", "scope": {"predicates": [{"field": "project", "op": "eq", "value": "Delta"}], "measure": {"kind": "sum", "field": "amount"}}}),
    ("What is the total of all records?", "numeric total answered with a record count", {"arm": "structured", "scope": {"measure": {"kind": "count"}}}),
    ("How many records are in region north?", "qualifier on a non-filterable field", {"arm": "structured", "scope": {"predicates": [{"field": "region", "op": "eq", "value": "north"}], "measure": {"kind": "count"}}}),
    ("How many records were created in March 2024?", "calendar qualifier without an offset", {"arm": "structured", "scope": {"predicates": [{"field": "created_at", "op": "date_interval", "lower": "2024-03", "granularity": "month"}], "measure": {"kind": "count"}}}),
    ("List all records about billing", "exhaustive list over an unbound topic", {"arm": "structured", "scope": {"predicates": [{"field": "status", "op": "eq", "value": "billing"}]}}),
    ("Are there any records for project Omega?", "absence with an unobserved value", {"arm": "structured", "scope": {"predicates": [{"field": "project", "op": "eq", "value": "Omega"}], "measure": {"kind": "count"}}}),
    ("When was the earliest record?", "earliest without a declared time basis", {"arm": "structured", "scope": {"measure": {"kind": "first_seen", "field": "created_at"}}}),
    ("How many open records are there in table 7?", "count scoped to a non-existent table", {"arm": "structured", "scope": {"predicates": [{"field": "status", "op": "eq", "value": "open"}, {"field": "tag", "op": "eq", "value": "7"}], "measure": {"kind": "count"}}}),
]
ROWS = [
    ("r1", "Alpha", "open", "REC-1001", "t1", 10.5, 1, False, "2024-03-02T10:00:00+00:00", "2024-03-05", "north", "Alpha billing record about invoices and rounding."),
    ("r2", "Alpha", "closed", "REC-1002", "t2", 20.0, 2, True, "2024-03-20T10:00:00+00:00", "2024-04-01", "south", "Alpha record closed after the billing dispute."),
    ("r3", "Beta", "open", "REC-1003", "t3", 5.0, 3, False, "2024-04-01T00:00:00+00:00", "2024-04-02", "north", "Beta open record on table layouts and rows."),
    ("r4", "Beta", "pending", "REC-1004", "t1", 7.25, 2, False, "2023-12-31T23:30:00-01:00", "2024-01-15", "south", "Beta pending record about region reports."),
]
DOCS = [
    Document(
        id=rid,
        dataset="demo",
        kind="record",
        title=f"Record {rid}",
        fields={"summary": text, "details": f"Details for {rid}: " + text * 3},
        metadata={"kind": "record", "project": project, "status": status, "external_id": ext, "tag": tag, "amount": amount, "priority": priority, "archived": archived, "created_at": created, "updated_at": created, "resolved_on": resolved, "region": region},
    )
    for rid, project, status, ext, tag, amount, priority, archived, created, resolved, region, text in ROWS
]


def config(**overrides):
    data = {
        "schema_version": 2,
        "dataset": "demo",
        "backends": {"local": {"kind": "memory"}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "metadata_schema": v2_schema_model().model_dump(mode="json"),
        "pipeline": {"arms": {"dense": {"backend": "local"}, "lexical": {"backend": "local"}, "structured": {"backend": "local"}}},
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 64}, "generator": FAKE}},
    }
    for key, value in overrides.items():
        data[key] = {**data[key], **value} if isinstance(value, dict) and isinstance(data.get(key), dict) else value
    return validate_config(data)


def engine_for(kind, policy, **extra):
    decision = {"kind": kind, "on_failure": policy}
    roles = {"embedder": {"provider": "hash", "dimensions": 64}, "generator": FAKE}
    if kind == "llm":
        roles["planner"] = FAKE
    pipeline = {"arms": {"dense": {"backend": "local"}, "lexical": {"backend": "local"}, "structured": {"backend": "local"}}, "decision": decision, **extra}
    engine = Engine(config(pipeline=pipeline, models={"roles": roles}))
    engine.ingest(DOCS)
    return engine


def has_number(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


def fabricating_generator(reply=None):
    """A generator that would state a number: a count claim citing whatever evidence it was given."""

    def respond(self, system, payload, schema, budget, stage):
        if stage == "planner":
            return LlmPlan.model_validate({"steps": [reply]})
        first = payload["evidence"][0]["id"]
        return Answer(answer="There are 7 matching records.", claims=[Claim(text="7 records", kind="count", evidence_ids=[first])], citations=[first])

    return respond


@pytest.mark.parametrize("kind", ["rules", "llm"])
@pytest.mark.parametrize("policy", ["fail_closed", "raw_all_arms"])
@pytest.mark.parametrize(("question", "mode", "reply"), PROBES, ids=[p[1] for p in PROBES])
def test_nine_count_probe_shapes_never_yield_a_number_through_the_engine(monkeypatch, kind, policy, question, mode, reply):
    monkeypatch.setattr(JsonModel, "call", fabricating_generator(reply))
    engine = engine_for(kind, policy)
    result = engine.answer(question)
    decision = result.trace["decision"]
    assert decision["intents"], mode
    assert decision["outcome"] == ("abstain" if policy == "fail_closed" else "raw_all_arms") and decision["decision_fallback"] == (policy == "raw_all_arms")
    assert result.retrieval.aggregates == [] and result.unsupported_constraints and result.answer.insufficient_evidence
    assert not has_number(result.answer.answer) and result.answer.claims == []
    if policy == "raw_all_arms":
        assert set(result.retrieval.arm_hits) == {"dense", "lexical"} and result.contract == "abstained"
        # 2.0.0a2: the answer text ("There are 7 matching records.") is checked too (QA-04)
        assert [v.rule for v in result.violations] == ["claim_without_exact_scope", "aggregate_intent_without_scope", "answer_text_without_exact_scope"]
        assert any(u.reason == "aggregate_intent_without_scope" for u in result.unsupported_constraints)
    else:
        assert result.retrieval.hits == [] and result.contract == "passed" and result.trace["model_calls"] == []
    search = engine.search(question)
    assert search.hits == [] if policy == "fail_closed" else search.hits is not None
    assert search.unsupported_constraints and search.aggregates == []


def test_exactly_resolvable_count_still_counts_and_the_number_is_kept(monkeypatch):
    def respond(self, system, payload, schema, budget, stage):
        aggregate = next(e for e in payload["evidence"] if e["source"] == "structured")
        return Answer(answer="Two records are open.", claims=[Claim(text="2 open records", kind="count", evidence_ids=[aggregate["id"]])], citations=[aggregate["id"]])

    monkeypatch.setattr(JsonModel, "call", respond)
    engine = engine_for("rules", "fail_closed")
    result = engine.answer("How many records have status open?")
    assert result.trace["decision"]["outcome"] == "decided" and result.retrieval.aggregates[0]["value"] == 2
    assert result.retrieval.aggregates[0]["exactness"] == "exact" and result.retrieval.hits == []  # a count runs the structured arm only
    assert result.contract == "passed" and result.unsupported_constraints == [] and result.answer.claims[0].kind == "count"
    explicit = engine.search(SearchRequest(query="open records", aggregate=Aggregate(operation="group_count", field="status")))
    assert explicit.aggregates[0]["value"] == {"closed": 1, "open": 2, "pending": 1} and explicit.hits  # explicit typed scope next to retrieval
    assert explicit.trace["decision"]["outcome"] == "decided" and explicit.trace["steps"][-1]["arm"] == "structured"


class ScriptedReviewer:
    def __init__(self, *reviews):
        self.reviews, self.calls = list(reviews), []

    def review(self, ctx, question, evidence):
        self.calls.append([e["id"] for e in evidence])
        return self.reviews.pop(0) if len(self.reviews) > 1 else self.reviews[0]


def test_review_replans_through_the_rules_agent_and_never_widens(monkeypatch):
    roles = {"embedder": {"provider": "hash", "dimensions": 64}, "generator": FAKE, "reviewer": FAKE}
    pipeline = {"arms": {"lexical": {"backend": "local"}, "structured": {"backend": "local"}}, "review": {"enabled": True, "max_hops": 3}}
    reviewer = ScriptedReviewer(Review(sufficient=False, missing="table layouts", followup_query="table layouts rows"), Review(sufficient=True))
    engine = Engine(config(pipeline=pipeline, models={"roles": roles}), components=[reviewer])
    engine.ingest(DOCS)
    monkeypatch.setattr(JsonModel, "call", lambda self, system, payload, schema, budget, stage: Answer(answer="Rows are described [r3].", citations=["r3"]))
    result = engine.answer(SearchRequest(query="billing invoices", filters={"project": "Beta"}))
    assert result.trace["stop_reason"] == "sufficient" and result.trace["review"]["hops_executed"] == 1
    assert reviewer.calls[0] == [] or "r3" not in reviewer.calls[0]
    assert "r3" in reviewer.calls[1] and all(h.document_id in {"r3", "r4"} for h in result.retrieval.hits)  # filters stay immutable across hops
    assert result.trace["rounds"][1]["steps"][0]["query"] == "table layouts rows" and result.answer.citations == ["r3"]
    # a follow-up that is an unbound count re-plans to an abstention and stops the loop; nothing widens
    reviewer = ScriptedReviewer(Review(sufficient=False, missing="a count", followup_query="how many rows are in table 9"))
    engine = Engine(config(pipeline=pipeline, models={"roles": roles}), components=[reviewer])
    engine.ingest(DOCS)
    result = engine.answer("billing invoices")
    assert result.trace["stop_reason"] == "replan_abstained" and len(result.trace["rounds"]) == 1


def test_reciprocal_rank_reproduces_the_alpha_per_arm_order_on_the_shipped_example(engine):
    """``examples/local.yaml`` (fusion.strategy per_arm, source weights) through the v2 engine."""
    assert engine.config.pipeline.selection.ordering == "reciprocal_rank"
    weights = dict(engine.config.pipeline.selection.source_weights)
    for query in ("exemption certificate", "rounding", "content source", "California tax", "certificate"):
        result = engine.search(query)
        expected = fuse(result.arm_hits, FusionLike(strategy="per_arm", source_weights=weights))
        assert [h.document_id for h in result.hits] == [h.document_id for h in expected], query
        assert [h.score for h in result.hits] == pytest.approx([h.score for h in expected])
        assert [h.arms for h in result.hits] == [h.arms for h in expected]
        assert result.trace["selection"]["ordering"]["policy"] == "reciprocal_rank"


def test_packing_is_fair_and_the_configured_priority_beats_evidence_fields():
    cfg = config(pipeline={"arms": {"lexical": {"backend": "local"}}, "selection": {"packing": {"budget_chars": 600, "min_per_record": 100}}})
    engine = Engine(cfg)
    engine.ingest(DOCS)
    ctx = engine._context_for(None)
    result = engine.search("record")
    assert len(result.hits) == 4
    evidence, trace = engine._evidence(ctx, result)
    assert trace["fairness"] == "fair_share" and [e["id"] for e in evidence] == [h.document_id for h in result.hits]
    assert len(trace["truncated_ids"]) == 4 and trace["omitted_ids"] == [] and trace["passage_characters"] <= 600
    assert min(len(e["passage"]) for e in evidence) >= 100
    cfg.pipeline.selection.packing.fairness = "greedy"
    evidence, trace = engine._evidence(ctx, result)
    assert trace["fairness"] == "greedy" and len(evidence) < 4 and trace["omitted_ids"]  # first-come allocation starves the tail
    cfg.pipeline.selection.packing.fairness = "fair_share"
    cfg.pipeline.generation.evidence_fields = ["details"]
    assert engine._evidence(ctx, result)[0][0]["passage"].startswith("details:")
    cfg.pipeline.selection.packing.field_priority = ["summary"]
    evidence, trace = engine._evidence(ctx, result)
    assert evidence[0]["passage"].startswith("summary:") and trace["field_priority"][:2] == ["summary", "details"]


def test_coverage_is_none_for_a_strict_complete_generation():
    engine = Engine(config(models={"roles": {"embedder": {"provider": "hash", "dimensions": 64}}}))
    engine.ingest(DOCS)
    strict = engine.answer("billing")
    assert strict.coverage is None and "coverage" not in strict.trace and strict.answer.insufficient_evidence  # no generator
