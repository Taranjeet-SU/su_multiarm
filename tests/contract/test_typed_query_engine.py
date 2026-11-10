"""Engine end to end with typed-query planning (typed-query plan M6), synthetic two-tenant corpus.

``tests/typed_support.py`` (two tenants plus a canary, sqlite projection per generation); the
planner and generator are fakes patched over ``JsonModel.call`` (the planner reply is validated
with the capability-bound schema the engine passes, as for a provider without schema enforcement).

* ``typed_query`` reaches the planner only with ``pipeline.decision.typed_query.enabled`` and a
  sealed projection of the pinned generation; otherwise the planner payload has no typed section;
* the arm applies the principal's ACL at execution (recompiled, per-tenant file);
* typed results are packed as evidence with bounded lineage and row citations, counted against
  the packing budget, and back exact count claims through the ``AnswerContract``.
"""

from __future__ import annotations

import json

import pytest

from su_multi_arm import Engine, SearchRequest
from su_multi_arm.core.config import validate_config
from su_multi_arm.decision import LlmPlan
from su_multi_arm.models import Answer, Claim
from su_multi_arm.providers import JsonModel
from su_multi_arm.runtime.acl import Principal

import typed_support as ts

FAKE = {"provider": "openai_compatible", "model": "fake"}
COUNT_Q = "How many open tickets are in module Billing?"
COUNT = {
    "arm": "typed_query",
    "typed": {
        "analysis": "Count ticket rows with status open in module Billing.",
        "answerability": "answerable",
        "query": {"grain": "ticket", "measure": {"kind": "count"}, "where": [
            {"table": "ticket", "predicate": {"field": "status", "op": "eq", "value": "open"}},
            {"table": "ticket", "predicate": {"field": "module", "op": "eq", "value": "Billing"}},
        ]},
    },
}
LOOKUP_Q = "What is the status and priority of tk-1?"
LOOKUP = {
    "arm": "typed_query",
    "typed": {
        "analysis": "Look up one ticket by id and project status and priority.",
        "answerability": "answerable",
        "query": {"grain": "ticket", "limit": 1, "where": [{"table": "ticket", "predicate": {"field": "id", "op": "eq", "value": "tk-1"}}],
                  "select": [{"table": "ticket", "column": "status"}, {"table": "ticket", "column": "priority"}]},
    },
}
LIST_Q = "List the comments of tickets in module Billing"
CHILD_LIST = {
    "arm": "typed_query",
    "typed": {
        "analysis": "List comment rows joined to their ticket, restricted to Billing tickets.",
        "answerability": "answerable",
        "query": {"grain": "comment", "join": "comment_parent", "where": [{"table": "ticket", "predicate": {"field": "module", "op": "eq", "value": "Billing"}}],
                  "select": [{"table": "comment", "column": "visibility"}, {"table": "ticket", "column": "status"}]},
    },
}
ALPHA = Principal(id="alpha-reader", tenant="t_alpha", scopes=["search", "answer"])
BETA = Principal(id="beta-reader", tenant="t_beta", scopes=["search", "answer"])


def engine_for(tmp_path, *, enabled=True, packing=None, **decision):
    data = ts.config_dict(tmp_path / "state")
    data["pipeline"]["decision"] = {"kind": "llm", "typed_query": {"enabled": enabled, **decision}}
    if packing:
        data["pipeline"]["selection"]["packing"] = packing
    data.setdefault("models", {}).setdefault("roles", {}).update({"planner": FAKE, "generator": FAKE})
    engine = Engine(validate_config(data))
    engine.ingest(ts.documents())
    return engine


@pytest.fixture
def fake(monkeypatch):
    """Planner replies ``fake.steps``; the generator states ``fake.count`` from the first typed
    evidence (or cites the first evidence when there is none)."""
    seen: dict = {"planner": [], "generator": [], "steps": [COUNT], "count": None}

    def respond(self, system, payload, schema, budget, stage):
        seen[stage].append(payload)
        if stage == "planner":
            assert issubclass(schema, LlmPlan)
            return schema.model_validate({"steps": seen["steps"]})
        typed = [row for row in payload["evidence"] if row["source"] == "structured"]
        if typed and seen["count"] is not None:
            cited = typed[0]["id"]
            return Answer(answer=f"There is {seen['count']} matching ticket [{cited}].", claims=[Claim(text=f"{seen['count']} matching ticket", kind="count", evidence_ids=[cited])], citations=[cited])
        cited = payload["evidence"][0]["id"]
        return Answer(answer=f"See the evidence [{cited}].", citations=[cited])

    monkeypatch.setattr(JsonModel, "call", respond)
    return seen


def test_typed_count_end_to_end_per_tenant(tmp_path, fake):
    engine = engine_for(tmp_path)
    alpha = engine.search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    assert alpha.trace["decision"]["outcome"] == "decided" and alpha.trace["steps"][0]["arm"] == "typed_query"
    [aggregate] = alpha.aggregates
    assert aggregate["typed_query"]["grain"] == "ticket" and aggregate["value"] == 1 and aggregate["exactness"] == "exact"
    assert aggregate["file_grant"] == "tenant" and aggregate["document_ids"] == ["tk-1"]
    assert "typed_query" in fake["planner"][0]["available_arms"] and "ticket" in fake["planner"][0]["typed_query"]["catalog"]["tables"]
    beta = engine.search(SearchRequest(query=COUNT_Q), principal=BETA)
    assert beta.aggregates[0]["value"] == 1 and beta.aggregates[0]["document_ids"] == ["tk-4"]  # never tk-1 or the canary
    operator = engine.search(SearchRequest(query=COUNT_Q), principal=Principal(id="operator", scopes=["search", "answer"]))
    assert operator.aggregates[0]["file_grant"] == "all" and operator.aggregates[0]["value"] == 3  # access.unrestricted
    denied = engine.search(SearchRequest(query=COUNT_Q), principal=Principal(id="nobody", scopes=["search", "answer"]))
    assert all(a["value"] in (0, None) for a in denied.aggregates)  # no tenant: an empty grant, never all rows


def test_acl_is_applied_at_execution_not_trusted_from_the_decision(tmp_path, fake, monkeypatch):
    engine = engine_for(tmp_path)
    arm = engine.pipelines["default"].arms["typed_query"]
    from su_multi_arm.decision import typed_query as compiler_module

    original = compiler_module.TypedQueryCompiler.compile
    calls = []

    def spy(self, draft, filters, acl, **kwargs):
        calls.append(list(acl))
        return original(self, draft, filters, acl, **kwargs)

    monkeypatch.setattr(compiler_module.TypedQueryCompiler, "compile", spy)
    result = engine.search(SearchRequest(query=COUNT_Q), principal=BETA)
    assert len(calls) == 2 and all(c and c[0].value == "t_beta" for c in calls)  # decision (validation) and arm (authoritative)
    assert result.aggregates[0]["value"] == 1
    arm.inject_acl = False  # test-only hook: the per-tenant file still holds t_beta rows only
    try:
        result = engine.search(SearchRequest(query=COUNT_Q), principal=BETA)
    finally:
        arm.inject_acl = True
    assert result.aggregates[0]["value"] == 1 and result.aggregates[0]["exactness"] == "bounded"


def test_typed_lookup_rows_are_packed_with_citations(tmp_path, fake):
    fake["steps"] = [LOOKUP]
    engine = engine_for(tmp_path)
    result = engine.answer(SearchRequest(query=LOOKUP_Q), principal=ALPHA)
    payload = fake["generator"][0]
    [typed] = [row for row in payload["evidence"] if row["source"] == "structured"]
    packed = json.loads(typed["passage"])
    assert packed["arm"] == "typed_query" and packed["evidence"] == "typed_rows" and packed["exactness"] == "exact"
    [row] = packed["rows"]
    # the access column is always projected (the returned-row tenant check needs it)
    assert row["document_id"] == "tk-1" and row["values"] == {"status": "open", "priority": 1, "tenant": "t_alpha"}
    assert row["cited"] == [["tk-1", "status"], ["tk-1", "priority"], ["tk-1", "tenant"]]
    lineage = packed["contributor_lineage"]
    assert lineage["citation_form"] == "(document_id, field, generation)" and lineage["generation"]["generation"] == result.retrieval.generation
    assert "canonical" not in packed["typed_query"] and packed["typed_query"]["fingerprint"]
    assert result.contract == "passed" and typed["id"] in result.answer.citations
    # the same plan under another tenant reads that tenant's file: no row
    other = engine.search(SearchRequest(query=LOOKUP_Q), principal=BETA)
    assert other.aggregates[0]["rows"] == [] and other.aggregates[0]["total"] == 0


def test_child_rows_cite_the_parent_for_parent_columns(tmp_path, fake):
    fake["steps"] = [CHILD_LIST]
    engine = engine_for(tmp_path)
    result = engine.search(SearchRequest(query=LIST_Q), principal=ALPHA)
    rows = result.aggregates[0]["rows"]
    assert {r["document_id"] for r in rows} == {"cm-1", "cm-2", "cm-3"}
    for row in rows:
        parent = {c["field"]: c["document_id"] for c in row["citations"]}
        assert parent["status"] in {"tk-1", "tk-2"} and parent["visibility"] == row["document_id"]
        assert all(c["generation"] == result.generation for c in row["citations"])


def test_exact_typed_count_backs_a_count_claim(tmp_path, fake):
    fake["count"] = 1
    engine = engine_for(tmp_path)
    result = engine.answer(SearchRequest(query=COUNT_Q), principal=ALPHA)
    assert result.contract == "passed" and not result.answer.insufficient_evidence
    assert [c.kind for c in result.answer.claims] == ["count"]
    [typed] = [row for row in fake["generator"][0]["evidence"] if row["source"] == "structured"]
    assert result.trace["evidence"]["evidence_characters"] >= len(typed["passage"])  # counted in the budget


def test_wrong_number_against_typed_evidence_is_refused(tmp_path, fake):
    fake["count"] = 3  # the cross-tenant total; the principal's exact typed count is 1
    engine = engine_for(tmp_path)
    result = engine.answer(SearchRequest(query=COUNT_Q), principal=ALPHA)
    assert result.contract != "passed" and any(v.rule == "answer_number_mismatch" for v in result.violations)


def test_typed_evidence_counts_against_the_packing_budget(tmp_path, fake):
    fake["count"] = 1
    engine = engine_for(tmp_path, packing={"budget_chars": 500})
    result = engine.answer(SearchRequest(query=COUNT_Q), principal=ALPHA)
    aggregate_id = result.retrieval.aggregates[0]["id"]
    assert aggregate_id in result.trace["evidence"]["omitted_ids"]
    # nothing else was retrieved: the generator is not called and no count is stated
    assert fake["generator"] == [] and result.answer.insufficient_evidence
    assert not any(c.kind == "count" for c in result.answer.claims)


def test_rows_beyond_the_pack_cap_are_bounded(tmp_path, fake, monkeypatch):
    import su_multi_arm.engine as engine_module

    monkeypatch.setattr(engine_module, "TYPED_PACK_ROWS", 1)
    fake["steps"] = [CHILD_LIST]
    engine = engine_for(tmp_path)
    result = engine.answer(SearchRequest(query=LIST_Q), principal=ALPHA)
    aggregate = result.retrieval.aggregates[0]
    assert aggregate["exactness"] == "bounded" and any(u["reason"] == "result_truncated" for u in aggregate["unsupported"])
    packed = json.loads(next(row for row in fake["generator"][0]["evidence"] if row["source"] == "structured")["passage"])
    assert len(packed["rows"]) == 1 and packed["shown"] == "showing 1 of 3"


def test_not_answerable_is_recorded_through_the_engine(tmp_path, fake):
    fake["steps"] = [{"arm": "typed_query", "typed": {"analysis": "No sentiment column.", "answerability": "not_answerable", "gaps": ["no sentiment column"], "query": None}}]
    fake["count"] = 2
    engine = engine_for(tmp_path)
    result = engine.answer(SearchRequest(query="How many tickets mention unhappy customers?"), principal=ALPHA)
    decision = result.trace["decision"]
    assert [d["reason"] for d in decision["dropped_steps"]] == ["planner_not_answerable"] and decision["planner_fallback"] == "retrieval_after_dropped_steps"
    assert result.retrieval.aggregates == []
    assert any(u.reason == "planner_not_answerable" for u in result.unsupported_constraints)
    assert not any(c.kind == "count" for c in result.answer.claims)


def test_typed_query_is_not_offered_when_disabled_or_without_a_projection(tmp_path, fake, monkeypatch):
    fake["steps"] = [{"arm": "lexical", "query": "billing"}]
    disabled = engine_for(tmp_path / "off", enabled=False)
    disabled.search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    payload = fake["planner"][-1]
    assert "typed_query" not in payload and "typed_query" not in payload["available_arms"]
    enabled = engine_for(tmp_path / "on")
    monkeypatch.setattr(type(enabled.pipelines["default"].arms["typed_query"]), "catalog_for", lambda self, ctx: None)
    result = enabled.search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    assert "typed_query" not in fake["planner"][-1]
    assert result.trace["decision"]["outcome"] == "decided"
