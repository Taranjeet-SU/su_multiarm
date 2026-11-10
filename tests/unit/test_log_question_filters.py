"""Synthetic scope binding: names and dates in the question must survive planning."""

from datetime import date

import pytest

from su_multi_arm.decision.log_intents import CompileContext, LogIntentCall, bind_question_scope, compile_intent, detect


def values(field):
    return {"service": ["svc-api", "svc-worker"], "tenant": ["t01", "t02"], "signature_id": []}.get(field, [])


def test_missing_planner_filters_are_bound_and_date_overrides_default_period():
    call = LogIntentCall(intent="log_top", args={"by": "signature", "window": "today", "as_of": "2026-03-10"})
    bound = bind_question_scope(call, "What errors affected client t01 in svc-api on 2026-03-07?", as_of=date(2026, 3, 10), values=values)
    assert bound.args["tenant"] == "t01" and bound.args["service"] == ["svc-api"]
    compiled = compile_intent(bound, CompileContext(today=date(2026, 3, 10)))
    assert compiled.window.start == date(2026, 3, 7) and compiled.window.end == date(2026, 3, 8)
    predicates = compiled.scopes["main"].predicates
    assert any(p.field == "tenant" and p.value == "t01" for p in predicates)
    assert any(p.field == "service" and p.value == ["svc-api"] for p in predicates)


def test_explicit_range_is_inclusive_and_relative_dates_use_the_anchor():
    call = LogIntentCall(intent="log_top", args={"as_of": "2026-03-10"})
    bound = bind_question_scope(call, "errors from 2026-03-05 through 2026-03-07 for t01", as_of=date(2026, 3, 10), values=values)
    compiled = compile_intent(bound, CompileContext(today=date(2026, 3, 10)))
    assert compiled.window.start == date(2026, 3, 5) and compiled.window.end == date(2026, 3, 8)
    relative = bind_question_scope(call, "errors yesterday for t01", as_of=date(2026, 3, 10), values=values)
    assert compile_intent(relative, CompileContext(today=date(2026, 3, 10))).window.start == date(2026, 3, 9)


@pytest.mark.parametrize("call,question", [
    (LogIntentCall(intent="log_top", args={"tenant": "t02"}), "errors for client t01"),
    (LogIntentCall(intent="log_new", args={}), "new issues in svc-api"),
    (LogIntentCall(intent="log_coverage", args={}), "coverage for client t01"),
])
def test_contradictory_or_unsupported_filters_are_refused(call, question):
    with pytest.raises(ValueError):
        bind_question_scope(call, question, as_of=date(2026, 3, 10), values=values)


def test_latest_is_not_misread_as_most_frequent_and_named_client_is_not_dropped():
    assert detect("what was the latest error in svc-api for t01", as_of=date(2026, 3, 10), values=values) is None
    call = detect("what errors affected client t01 in svc-api yesterday", as_of=date(2026, 3, 10), values=values)
    assert call.intent == "log_top" and call.args["tenant"] == "t01" and call.args["service"] == ["svc-api"]


def test_month_range_and_multiple_explicit_severities_survive_model_defaults():
    call = LogIntentCall(intent="log_top", args={"level": ["error"], "window": "today"})
    bound = bind_question_scope(call, 'ERROR or FATAL for client "t01" and service "svc-api" on March 2–3, 2026', as_of=date(2026, 3, 10), values=values)
    compiled = compile_intent(bound, CompileContext(today=date(2026, 3, 10)))
    assert bound.args["level"] == ["error", "fatal"]
    assert compiled.window.start == date(2026, 3, 2)
    assert compiled.window.end == date(2026, 3, 4)


@pytest.mark.parametrize("question", [
    'coverage for client "unknown-client"',
    'what errors in service "unknown-service"',
    "what was the latest error in svc-api",
    "errors in svc-api on March 3–2, 2026",
])
def test_unknown_quoted_entities_latest_rollups_and_invalid_ranges_do_not_broaden(question):
    with pytest.raises(ValueError):
        bind_question_scope(LogIntentCall(intent="log_top"), question, as_of=date(2026, 3, 10), values=values)


def test_source_count_comparison_is_not_answered_with_parsed_counts():
    call = LogIntentCall(intent="log_compare", args={"periods": ["2026-03-02", "2026-03-03"]})
    with pytest.raises(ValueError, match="source-count comparison"):
        bind_question_scope(call, "Compare source ERROR counts for svc-api on March 2 versus March 3", as_of=date(2026, 3, 10), values=values)


def test_service_issue_question_cannot_be_reduced_to_service_volume_by_the_model():
    call = LogIntentCall(intent="log_top", args={"by": "service", "level": ["error", "fatal"]})
    bound = bind_question_scope(call, "which services failed in last 3 days and what was the issue", as_of=date(2026, 3, 10), values=values)
    assert bound.args["by"] == "service_signature" and bound.args["top_n"] == 100
    assert compile_intent(bound, CompileContext(today=date(2026, 3, 10))).scopes["main"].group_by == ["service", "signature_id"]


@pytest.mark.parametrize("noun", ["client", "clients", "tenant", "customers", "accounts"])
def test_client_issue_questions_keep_issue_dimension(noun):
    question = f"which {noun} failed in last 3 days and what was the issue"
    call = detect(question, as_of=date(2026, 3, 10), values=values)
    bound = bind_question_scope(call, question, as_of=date(2026, 3, 10), values=values)
    compiled = compile_intent(bound, CompileContext(today=date(2026, 3, 10)))
    assert compiled.scopes["main"].group_by == ["tenant", "signature_id"]
    assert compiled.args["columns"] == ["tenant", "error_type", "error_count"]


def test_planner_can_choose_joint_dimensions_and_column_order():
    args = {"group_by": ["tenant", "service", "signature_id"], "columns": ["service", "tenant", "error_type", "error_count"]}
    call = LogIntentCall(intent="log_top", args=args)
    bound = bind_question_scope(call, "which clients and services failed and what was the issue", as_of=date(2026, 3, 10), values=values)
    compiled = compile_intent(bound, CompileContext(today=date(2026, 3, 10)))
    assert compiled.scopes["main"].group_by == args["group_by"]
    assert compiled.args["columns"] == args["columns"]


@pytest.mark.parametrize("columns", [["service", "error_count"], ["service", "root_cause", "error_count"], ["error_type", "error_count"]])
def test_presentation_cannot_invent_fields_or_hide_required_dimensions(columns):
    with pytest.raises(ValueError):
        compile_intent(LogIntentCall(intent="log_top", args={"group_by": ["service", "signature_id"], "columns": columns}), CompileContext(today=date(2026, 3, 10)))


def test_joint_question_prepass_does_not_drop_client_or_service():
    question = "which clients and services failed in last 3 days and what was the issue"
    call = detect(question, as_of=date(2026, 3, 10), values=values)
    bound = bind_question_scope(call, question, as_of=date(2026, 3, 10), values=values)
    assert compile_intent(bound, CompileContext(today=date(2026, 3, 10))).scopes["main"].group_by == ["tenant", "service", "signature_id"]
