"""Frozen synthetic question suite for typed-query planning (typed-query plan §7.1 M6, §8.1).

Each case is a question, the typed step a fake planner returns for it, the principal and the
expected outcome, hand-derived from ``tests/typed_support.py`` (two tenants plus a canary). The
suite runs the whole engine search path: capability-bound validation of the reply, decision-time
compile (validation only), the arm's recompile with the principal's ACL and execution on the
per-generation sqlite projection. It freezes which shapes execute (and their values) and which
are refused (and why). It measures the plumbing, not a model: the planner is a fake.
"""

from __future__ import annotations

import json

import pytest

from su_multi_arm import Engine, SearchRequest
from su_multi_arm.core.config import validate_config
from su_multi_arm.providers import JsonModel
from su_multi_arm.runtime.acl import Principal

import typed_support as ts

FAKE = {"provider": "openai_compatible", "model": "fake"}
ALPHA = Principal(id="alpha-reader", tenant="t_alpha", scopes=["search", "answer"])
BETA = Principal(id="beta-reader", tenant="t_beta", scopes=["search", "answer"])
OPERATOR = Principal(id="operator", scopes=["search", "answer"])


def T(grain, *, answerability="answerable", gaps=(), **query):
    query = {"grain": grain, **query}
    return {"arm": "typed_query", "typed": {"analysis": f"Typed query over {grain}.", "answerability": answerability, "gaps": list(gaps), "query": query}}


def W(field, value, table="ticket", op="eq"):
    return {"table": table, "predicate": {"field": field, "op": op, "value": value}}


def C(table, column):
    return {"table": table, "column": column}


COUNT = {"kind": "count"}
NA = {"arm": "typed_query", "typed": {"analysis": "No column holds sentiment.", "answerability": "not_answerable", "gaps": ["no sentiment column"], "query": None}}

# (id, question, step, principal, request filters, expected)
CASES = [
    ("count_all", "How many tickets are there?", T("ticket", measure=COUNT), ALPHA, {}, {"value": 3}),
    ("count_open", "How many open tickets are there?", T("ticket", measure=COUNT, where=[W("status", "open")]), ALPHA, {}, {"value": 2}),
    ("count_open_billing", "How many open tickets are in module Billing?", T("ticket", measure=COUNT, where=[W("status", "open"), W("module", "Billing")]), ALPHA, {}, {"value": 1}),
    ("count_beta", "How many tickets are there?", T("ticket", measure=COUNT), BETA, {}, {"value": 2}),
    ("count_operator", "How many tickets are there?", T("ticket", measure=COUNT), OPERATOR, {}, {"value": 6}),
    ("group_status", "How many tickets per status?", T("ticket", measure=COUNT, group_by=[C("ticket", "status")]), ALPHA, {}, {"groups": {"open": 2, "closed": 1}}),
    ("rank_module", "Which module has the most tickets?", T("ticket", measure=COUNT, group_by=[C("ticket", "module")], order="desc", top_n=1), ALPHA, {}, {"groups": {"Billing": 2}}),
    ("sum_hours", "What is the total of hours over tickets?", T("ticket", measure={"kind": "sum", "field": "hours"}), ALPHA, {}, {"value": 6.5, "missing": 1}),
    ("avg_priority", "What is the average priority of tickets?", T("ticket", measure={"kind": "avg", "field": "priority"}), ALPHA, {}, {"value": 1.5}),
    ("max_priority", "What is the maximum priority of tickets?", T("ticket", measure={"kind": "max", "field": "priority"}), ALPHA, {}, {"value": 2}),
    # no aggregate operator word in the question: a typed aggregate is bounded (qualifier check)
    ("no_operator_bounded", "What is the highest priority number?", T("ticket", measure={"kind": "max", "field": "priority"}), ALPHA, {}, {"value": 2, "exactness": "bounded"}),
    ("min_hours", "What is the minimum hours of tickets?", T("ticket", measure={"kind": "min", "field": "hours"}), ALPHA, {}, {"value": 2.5}),
    ("distinct_modules", "How many distinct modules have tickets?", T("ticket", measure={"kind": "count_distinct", "unit": "module"}), ALPHA, {}, {"value": 2}),
    ("first_opened", "When was the earliest ticket opened?", T("ticket", measure={"kind": "first_seen", "field": "opened_at"}), ALPHA, {}, {"first": "tk-1"}),
    ("last_opened", "What is the latest opened date of tickets?", T("ticket", measure={"kind": "last_seen", "field": "opened_at"}), ALPHA, {}, {"first": "tk-3"}),
    ("lookup_own", "What is the status of tk-1?", T("ticket", limit=1, where=[W("id", "tk-1")], select=[C("ticket", "status")]), ALPHA, {}, {"rows": {"tk-1": "open"}}),
    ("lookup_other_tenant", "What is the status of tk-4?", T("ticket", limit=1, where=[W("id", "tk-4")], select=[C("ticket", "status")]), ALPHA, {}, {"rows": {}}),
    ("lookup_canary", "What is the status of tk-6?", T("ticket", limit=1, where=[W("id", "tk-6")], select=[C("ticket", "status")]), BETA, {}, {"rows": {}}),
    ("list_child_join", "List the comments of Billing tickets", T("comment", join="comment_parent", where=[W("module", "Billing")], select=[C("comment", "visibility")]), ALPHA, {}, {"rows": {"cm-1": "public", "cm-2": "private", "cm-3": "public"}}),
    ("count_child_join", "How many comments are on open tickets?", T("comment", join="comment_parent", measure=COUNT, where=[W("status", "open")]), ALPHA, {}, {"value": 2}),
    ("count_private", "How many private comments are there?", T("comment", measure=COUNT, where=[W("visibility", "private", table="comment")]), ALPHA, {}, {"value": 1}),
    ("parent_with_children", "How many tickets have comments?", T("ticket", join="comment_parent", measure=COUNT), ALPHA, {}, {"value": 2}),
    ("dangling_child_bounded", "How many comments are on open tickets?", T("comment", join="comment_parent", measure=COUNT, where=[W("status", "open")]), BETA, {}, {"value": 1, "exactness": "bounded"}),
    ("request_filter", "How many tickets are there?", T("ticket", measure=COUNT), ALPHA, {"module": "Billing"}, {"value": 2}),
    ("injection_value", "How many tickets carry this title?", T("ticket", measure=COUNT, where=[W("title_text", ts.INJECTION)]), ALPHA, {}, {"value": 1}),
    # refusals
    ("unknown_value", "How many tickets are in module Nowhere?", T("ticket", measure=COUNT, where=[W("module", "Nowhere")]), ALPHA, {}, {"failure": "unresolvable_value"}),
    ("other_tenant_value", "How many tickets are in module Reports?", T("ticket", measure=COUNT, where=[W("module", "Reports")]), ALPHA, {}, {"failure": "unresolvable_value"}),
    ("canary_value", "How many tickets belong to t_canary?", T("ticket", measure=COUNT, where=[W("tenant", ts.CANARY)]), ALPHA, {}, {"failure": "unresolvable_value"}),
    ("join_not_declared", "How many assets have comments?", T("asset", join="comment_parent", measure=COUNT), ALPHA, {}, {"failure": "invalid_step"}),
    ("fanout_sum", "What is the total of hours of tickets with comments?", T("ticket", join="comment_parent", measure={"kind": "sum", "field": "hours"}), ALPHA, {}, {"failure": "unsupported_measure"}),
    ("non_numeric_sum", "What is the sum of statuses?", T("ticket", measure={"kind": "sum", "field": "status"}), ALPHA, {}, {"failure": "unsupported_measure"}),
    ("window_without_timezone", "How many tickets were opened in 2024-03?", T("ticket", measure=COUNT, windows=[{"table": "ticket", "column": "opened_at", "expression": "2024-03"}]), ALPHA, {}, {"failure": "unresolvable_value"}),
    ("group_not_groupable", "How many tickets per opened time?", T("ticket", measure=COUNT, group_by=[C("ticket", "opened_at")]), ALPHA, {}, {"failure": "invalid_step"}),
    ("limit_over_cap", "List every ticket", T("ticket", limit=500), ALPHA, {}, {"failure": "invalid_step"}),
    ("filter_contradiction", "How many closed tickets are there?", T("ticket", measure=COUNT, where=[W("status", "closed")]), ALPHA, {"status": "open"}, {"failure": "invalid_step"}),
    ("not_answerable", "How many tickets mention unhappy customers?", NA, ALPHA, {}, {"dropped": "planner_not_answerable"}),
]


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    data = ts.config_dict(tmp_path_factory.mktemp("suite") / "state")
    data["pipeline"]["decision"] = {"kind": "llm", "typed_query": {"enabled": True}}
    data.setdefault("models", {}).setdefault("roles", {}).update({"planner": FAKE, "generator": FAKE})
    engine = Engine(validate_config(data))
    engine.ingest(ts.documents())
    return engine


def test_the_suite_has_at_least_thirty_frozen_questions():
    assert len(CASES) >= 30 and len({c[0] for c in CASES}) == len(CASES)


@pytest.mark.parametrize(("case", "question", "step", "principal", "filters", "expected"), CASES, ids=[c[0] for c in CASES])
def test_frozen_question(engine, monkeypatch, case, question, step, principal, filters, expected):
    monkeypatch.setattr(JsonModel, "call", lambda self, system, payload, schema, budget, stage: schema.model_validate({"steps": [step]}))
    result = engine.search(SearchRequest(query=question, filters=filters), principal=principal)
    decision = result.trace["decision"]
    if "failure" in expected:
        assert decision["outcome"] == "abstain" and decision["failure"] == expected["failure"], decision
        assert result.aggregates == []
        return
    if "dropped" in expected:
        assert [d["reason"] for d in decision["dropped_steps"]] == [expected["dropped"]] and result.aggregates == []
        return
    assert decision["outcome"] == "decided", decision
    [aggregate] = result.aggregates
    assert aggregate["exactness"] == expected.get("exactness", "exact"), aggregate
    canary = json.dumps({k: aggregate.get(k) for k in ("rows", "groups", "document_ids", "value")})  # results, not the echoed IR
    if principal is not OPERATOR:
        assert ts.CANARY not in canary and "tk-6" not in canary and "cm-6" not in canary
    if "value" in expected:
        assert aggregate["value"] == pytest.approx(expected["value"]), aggregate
    if "missing" in expected:
        assert aggregate["missing_or_invalid"] == expected["missing"]
    if "groups" in expected:
        assert {next(iter(g["key"].values())): g["value"] for g in aggregate["groups"]} == expected["groups"]
    if "first" in expected:
        assert aggregate["value"]["record_id"] == expected["first"]
    if "rows" in expected:
        column = next(iter(step["typed"]["query"]["select"]))["column"]
        assert {r["document_id"]: r["values"][column] for r in aggregate["rows"]} == expected["rows"]
