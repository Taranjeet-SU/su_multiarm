"""2.0.0a2 decision/contract fixes through ``Engine`` (QA-01, QA-02, QA-03, QA-09); synthetic data.

* QA-01: a planner-compiled scope must account for every question qualifier (observed field values,
  time expressions, quoted terms); an unapplied one makes the aggregate ``bounded`` with a
  ``qualifier_not_applied`` constraint and the contract refuses the count.
* QA-02: request filters are validated against ``MetadataSchema`` before execution.
* QA-03: the intent pre-pass gets the deployment's structured targets on every surface.
* QA-09: aggregates expose the predicates actually counted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from su_multi_arm import Document, Engine, SearchRequest
from su_multi_arm.core.config import load_config, validate_config
from su_multi_arm.decision import LlmPlan
from su_multi_arm.models import Aggregate, Answer, Claim
from su_multi_arm.providers import JsonModel

ROOT = Path(__file__).resolve().parents[2]
PRODUCTS = ["CloudTax", "SearchDesk", "LedgerOne"]
STATUSES = ["open", "resolved", "pending"]
FAKE = {"provider": "openai_compatible", "model": "fake"}


def corpus(n: int = 36) -> list[Document]:
    return [
        Document.model_validate(
            {
                "id": f"case:{i:04d}",
                "dataset": "qa",
                "title": f"{PRODUCTS[i % 3]} rounding ticket {i}",
                "fields": {"description": f"Rounding problem in {PRODUCTS[i % 3]}. Status {STATUSES[(i // 3) % 3]}."},
                "metadata": {
                    "product": PRODUCTS[i % 3],
                    "status": STATUSES[(i // 3) % 3],
                    "priority": 1 + i % 3,
                    "created_at": f"{2024 + i % 2}-01-{1 + i % 28:02d}T00:00:00+00:00",
                },
            }
        )
        for i in range(n)
    ]


def engine_for(tmp_path, decision: str = "llm", unknown_fields: str = "allow", generator: bool = True, listable_priority: bool = False) -> Engine:
    roles = {"embedder": {"provider": "hash", "dimensions": 64}, "planner": FAKE}
    if generator:
        roles["generator"] = FAKE
    data = {
        "schema_version": 2,
        "dataset": "qa",
        "backends": {"local": {"kind": "sqlite", "path": str(tmp_path / "state")}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "metadata_schema": {
            "unknown_fields": unknown_fields,
            "fields": [
                {"name": "product", "type": "string", "listable": True, "groupable": True},
                {"name": "status", "type": "string", "listable": True, "groupable": True},
                {"name": "priority", "type": "integer", "groupable": True, "listable": listable_priority},
                {"name": "created_at", "type": "datetime", "sortable": True, "time_basis": "created"},
            ],
        },
        "models": {"roles": roles},
        "pipeline": {"decision": {"kind": decision}, "arms": {"lexical": {"backend": "local"}, "structured": {"backend": "local"}}},
    }
    engine = Engine(validate_config(data))
    engine.ingest(corpus())
    return engine


def scripted(plan_steps, answer_text="See the aggregate.", kind="count"):
    def respond(self, system, payload, schema, budget, stage):
        if stage == "planner":
            return LlmPlan.model_validate({"steps": plan_steps})
        cite = [e["id"] for e in payload["evidence"] if e["id"].startswith("aggregate:")] or [payload["evidence"][0]["id"]]
        return Answer(answer=answer_text, claims=[Claim(text=answer_text, kind=kind, evidence_ids=cite)], citations=cite)

    return respond


def count_step(*predicates):
    return {"arm": "structured", "scope": {"predicates": list(predicates), "measure": {"kind": "count"}}}


STATUS_OPEN = {"field": "status", "op": "eq", "value": "open"}
CLOUDTAX = {"field": "product", "op": "eq", "value": "CloudTax"}


# --- QA-01 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question,steps,flagged",
    [
        # the recorded failing shape: the planner dropped "CloudTax"
        ("How many CloudTax tickets have status open?", [count_step(STATUS_OPEN)], "CloudTax"),
        # the planner bound a different value for the qualifier's field
        ("How many CloudTax tickets have status open?", [count_step(STATUS_OPEN, {**CLOUDTAX, "value": "SearchDesk"})], "CloudTax"),
        # a dropped time expression
        ("How many open tickets were created in 2025?", [count_step(STATUS_OPEN)], "2025"),
        # a dropped quoted term
        ('How many open tickets mention "nexus threshold"?', [count_step(STATUS_OPEN)], "nexus threshold"),
        # a structured step on a question without any aggregate operator (QA-01 second example)
        ("What is the CloudTax rounding policy?", [count_step(CLOUDTAX)], "structured scope without an aggregate operator in the question"),
    ],
)
def test_unapplied_qualifier_bounds_the_aggregate_and_the_count_is_refused(tmp_path, monkeypatch, question, steps, flagged):
    monkeypatch.setattr(JsonModel, "call", scripted(steps, "There are 12 open CloudTax tickets."))
    engine = engine_for(tmp_path)
    result = engine.answer(question)
    aggregate = result.retrieval.aggregates[0]
    assert aggregate["exactness"] == "bounded" and any(u["reason"] == "qualifier_not_applied" for u in aggregate["unsupported"])
    assert any(u.reason == "qualifier_not_applied" and u.constraint_text == flagged for u in result.unsupported_constraints)
    assert result.contract == "abstained" and result.answer.insufficient_evidence
    search = engine.search(question)
    assert any(u.reason == "qualifier_not_applied" for u in search.unsupported_constraints)


@pytest.mark.parametrize(
    "question,steps,value",
    [
        ("How many CloudTax tickets have status open?", [count_step(STATUS_OPEN, CLOUDTAX)], 4),
        ("How many tickets have status open?", [count_step(STATUS_OPEN)], 12),
        ("How many open tickets were created in 2025?", [count_step(STATUS_OPEN, {"field": "created_at", "op": "date_interval", "lower": "2025-01-01T00:00:00+00:00", "granularity": "year"})], 6),
    ],
)
def test_a_scope_that_applies_every_qualifier_stays_exact(tmp_path, monkeypatch, question, steps, value):
    monkeypatch.setattr(JsonModel, "call", scripted(steps, f"There are {value} matching records."))
    engine = engine_for(tmp_path)
    result = engine.answer(question)
    aggregate = result.retrieval.aggregates[0]
    assert aggregate["value"] == value and aggregate["exactness"] == "exact" and aggregate["unsupported"] == []
    assert result.contract == "passed" and result.unsupported_constraints == []


PRIORITY_3 = {"field": "priority", "op": "eq", "value": 3}


@pytest.mark.parametrize("listable", [False, True])
@pytest.mark.parametrize(
    "question,flagged",
    [
        # 2.0.0a2 review B1: the planner dropped the integer qualifier and counted 12 instead of 4
        ("How many priority 3 tickets are open?", "priority 3"),
        ("How many open tickets have priority 3?", "priority 3"),
        ("How many open tickets with priority is 3?", "priority is 3"),
        ("How many open tickets have priority above 2?", "priority above 2"),
        ("Count open tickets of priority: 3", "priority: 3"),
    ],
)
def test_unapplied_numeric_qualifier_bounds_the_count(tmp_path, monkeypatch, question, flagged, listable):
    monkeypatch.setattr(JsonModel, "call", scripted([count_step(STATUS_OPEN)], "There are 12 open priority 3 tickets."))
    engine = engine_for(tmp_path, listable_priority=listable)
    result = engine.answer(question)
    aggregate = result.retrieval.aggregates[0]
    assert aggregate["value"] == 12 and aggregate["exactness"] == "bounded"
    assert any(u.reason == "qualifier_not_applied" and u.constraint_text == flagged for u in result.unsupported_constraints)
    assert result.contract == "abstained"


@pytest.mark.parametrize(
    "question,steps,filters,value",
    [
        ("How many priority 3 tickets are open?", [count_step(STATUS_OPEN, PRIORITY_3)], {}, 4),
        ("How many priority 3 tickets are open?", [count_step(STATUS_OPEN, {**PRIORITY_3, "value": "3"})], {}, 4),
        ("How many priority 3 tickets are open?", [count_step(STATUS_OPEN)], {"priority": 3}, 4),
        ("How many open tickets have priority above 2?", [count_step(STATUS_OPEN, {"field": "priority", "op": "range", "lower": 3})], {}, 4),
    ],
)
def test_numeric_qualifier_applied_by_the_scope_or_a_filter_stays_exact(tmp_path, monkeypatch, question, steps, filters, value):
    monkeypatch.setattr(JsonModel, "call", scripted(steps, f"There are {value} matching records."))
    result = engine_for(tmp_path).answer(SearchRequest(query=question, filters=filters))
    aggregate = result.retrieval.aggregates[0]
    assert aggregate["value"] == value and aggregate["exactness"] == "exact" and result.unsupported_constraints == []
    assert result.contract == "passed"


def test_wrong_numeric_value_is_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(JsonModel, "call", scripted([count_step(STATUS_OPEN, {**PRIORITY_3, "value": 2})], "There are 4 tickets."))
    result = engine_for(tmp_path).answer("How many priority 3 tickets are open?")
    assert result.retrieval.aggregates[0]["exactness"] == "bounded" and result.contract == "abstained"


def test_a_request_filter_applies_a_qualifier_and_caller_typed_aggregates_are_not_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(JsonModel, "call", scripted([count_step(STATUS_OPEN)], "There are 4 matching records."))
    engine = engine_for(tmp_path)
    filtered = engine.answer(SearchRequest(query="How many CloudTax tickets have status open?", filters={"product": "CloudTax"}))
    assert filtered.retrieval.aggregates[0]["exactness"] == "exact" and filtered.retrieval.aggregates[0]["value"] == 4
    rules = engine_for(tmp_path / "rules", decision="rules")
    typed = rules.search(SearchRequest(query="How many CloudTax tickets?", aggregate=Aggregate(operation="count")))
    assert typed.aggregates[0]["exactness"] == "exact" and typed.aggregates[0]["value"] == 36  # the caller typed the scope


def test_plain_retrieval_questions_are_unaffected(tmp_path, monkeypatch):
    monkeypatch.setattr(JsonModel, "call", scripted([{"arm": "lexical", "query": "rounding CloudTax"}], "Enable per-line rounding.", "narrative"))
    result = engine_for(tmp_path).answer("How do I fix a CloudTax rounding problem?")
    assert result.contract == "passed" and result.unsupported_constraints == [] and result.retrieval.hits


# --- QA-09 ---------------------------------------------------------------------------------------


def test_aggregate_and_trace_record_the_compiled_predicates(tmp_path, monkeypatch):
    monkeypatch.setattr(JsonModel, "call", scripted([count_step(STATUS_OPEN, CLOUDTAX)]))
    engine = engine_for(tmp_path, generator=False)
    result = engine.search("How many CloudTax tickets have status open?")
    aggregate = result.aggregates[0]
    assert {"field": "status", "op": "eq", "value": "open"} in aggregate["predicates"] and {"field": "product", "op": "eq", "value": "CloudTax"} in aggregate["predicates"]
    step = next(s for s in result.trace["steps"] if s["arm"] == "structured")
    assert step["predicates"] == aggregate["predicates"]


# --- QA-02 ---------------------------------------------------------------------------------------


def count_request(filters):
    return SearchRequest(query="x", filters=filters, aggregate=Aggregate(operation="count"))


def test_unambiguous_coercion_is_applied_and_traced(tmp_path):
    engine = engine_for(tmp_path, decision="rules", generator=False)
    result = engine.search(count_request({"priority": "3"}))
    assert result.aggregates[0]["value"] == 12 and result.aggregates[0]["exactness"] == "exact"
    assert result.trace["request_filters"]["coercions"] == [{"field": "priority", "type": "str->integer"}]
    assert result.aggregates[0]["filters"] == {"priority": 3}


@pytest.mark.parametrize(
    "filters,reason",
    [
        ({"priority": "high"}, "type_mismatch"),
        ({"priority": True}, "type_mismatch"),
        ({"created_at": "yesterday"}, "unparseable_bound"),
        ({"created_at": "2025-01-01T00:00:00"}, "timezone_missing"),
        ({"prodcut": "CloudTax"}, "field_not_filterable"),
    ],
)
def test_invalid_request_filters_never_yield_an_exact_zero(tmp_path, filters, reason):
    engine = engine_for(tmp_path, decision="rules", generator=False)
    retrieval = engine.search(SearchRequest(query="rounding", filters=filters))
    assert any(u.reason == reason for u in retrieval.unsupported_constraints)
    assert retrieval.trace["request_filters"]["invalid"] == {next(iter(filters)): reason}
    counted = engine.search(count_request(filters))
    assert any(u.reason == reason for u in counted.unsupported_constraints)
    assert all(a["exactness"] != "exact" for a in counted.aggregates)  # abstained (no aggregate) or bounded


def test_undeclared_filter_is_untyped_under_allow_and_refused_under_reject(tmp_path):
    allow = engine_for(tmp_path / "a", decision="rules", generator=False).search(SearchRequest(query="rounding", filters={"prodcut": "CloudTax"}))
    assert allow.trace["request_filters"]["untyped"] == ["prodcut"] and "untyped string equality" in allow.unsupported_constraints[0].suggestion
    reject = engine_for(tmp_path / "r", decision="rules", unknown_fields="reject", generator=False).search(SearchRequest(query="rounding", filters={"prodcut": "CloudTax"}))
    assert reject.trace["request_filters"]["untyped"] == [] and reject.unsupported_constraints[0].reason == "field_not_filterable"


def test_aggregate_tool_validates_filters_the_same_way(tmp_path):
    from su_multi_arm.core.types import Measure, ScopeSpec
    from su_multi_arm.runtime.acl import ANONYMOUS

    engine = engine_for(tmp_path, decision="rules", generator=False)
    ctx = engine._context_for(ANONYMOUS)
    spec = ScopeSpec(measure=Measure(kind="count"))
    assert engine.aggregate_in(ctx, spec, {"priority": "3"}).value == 12
    bad = engine.aggregate_in(ctx, spec, {"priority": "high"})
    assert bad.exactness == "bounded" and bad.unsupported[0].reason == "type_mismatch"


def test_false_absence_from_a_filter_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(JsonModel, "call", scripted([], "There are no priority 3 tickets."))
    engine = engine_for(tmp_path, decision="rules")
    result = engine.answer(SearchRequest(query="Are there any priority 3 tickets?", filters={"priority": "3"}, aggregate=Aggregate(operation="count")))
    assert result.retrieval.aggregates[0]["value"] == 12 and result.contract == "abstained"


# --- QA-03 ---------------------------------------------------------------------------------------

ORDINARY = ["the latest release notes", "every time the service restarts", "What are all the steps to add an exemption certificate?"]


def example_engine(tmp_path, path: str) -> Engine:
    source = ROOT / path
    data = yaml.safe_load(source.read_text())
    if data.get("schema_version") == 2:
        data["backends"]["local"]["path"] = str(tmp_path / "state")
        data.setdefault("tools", {})["enabled"] = ["search"]
        config = validate_config(data)
    else:
        copy = tmp_path / "v1.yaml"
        data["storage_path"] = str(tmp_path / "state")
        copy.write_text(yaml.safe_dump(data))
        with pytest.warns(UserWarning):
            config = load_config(copy)
    engine = Engine(config)
    records = [json.loads(line) for line in (ROOT / "examples/documents.jsonl").read_text().splitlines() if line.strip()]
    engine.ingest([Document.model_validate(r) for r in records])
    return engine


@pytest.mark.parametrize("path", ["examples/v2/local.yaml", "examples/local.yaml"])
def test_documented_ordinary_questions_are_retrieval_on_every_surface(tmp_path, path):
    TestClient = pytest.importorskip("fastapi.testclient").TestClient

    from su_multi_arm.api import create_app

    engine = example_engine(tmp_path, path)
    client = TestClient(create_app(engine=engine))
    for query in ORDINARY:
        search = engine.search(SearchRequest(query=query))
        assert search.trace["decision"]["outcome"] == "decided" and search.trace["decision"]["intents"] == [], (path, query)
        answer = engine.answer(SearchRequest(query=query))
        assert answer.trace["decision"]["outcome"] == "decided", (path, query)
        done = [e for e in engine.answer_stream(SearchRequest(query=query)) if e.event == "done"]
        assert done and done[0].result.trace["decision"]["outcome"] == "decided"
        http = client.post("/v2/search", json={"query": query})
        assert http.status_code == 200 and http.json()["trace"]["decision"]["outcome"] == "decided"
        if "search" in engine._tools:
            tool = engine.call_tool("search", {"query": query})
            assert not tool.result.output.unsupported_constraints


# --- 2.0.0a2 release review N4: unobserved request-filter value ---------------------------------


@pytest.mark.parametrize("value", ["CloudTaxx", "cloudtax"])
def test_unobserved_filter_value_on_a_listable_field_is_reported_and_not_exact(tmp_path, monkeypatch, value):
    engine = engine_for(tmp_path, decision="rules", generator=False)
    counted = engine.search(count_request({"product": value}))
    assert counted.trace["request_filters"]["invalid"] == {"product": "value_not_observed"}
    assert any(u.reason == "value_not_observed" for u in counted.unsupported_constraints)
    assert all(a["exactness"] != "exact" for a in counted.aggregates)
    monkeypatch.setattr(JsonModel, "call", scripted([], "There are no CloudTax tickets.", kind="absence"))
    answered = engine_for(tmp_path / "g", decision="rules").answer(SearchRequest(query="Are there any CloudTax tickets?", filters={"product": value}, aggregate=Aggregate(operation="count")))
    assert answered.contract == "abstained"


def test_observed_filter_value_stays_exact(tmp_path):
    engine = engine_for(tmp_path, decision="rules", generator=False)
    counted = engine.search(count_request({"product": "CloudTax"}))
    assert counted.aggregates[0]["exactness"] == "exact" and counted.aggregates[0]["value"] == 12
