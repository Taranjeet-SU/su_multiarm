"""Deterministic log-intent detection and compilation (addendum A2 §4.1.5): plain-language log
questions map to typed calls; compilation depends on the arguments alone."""

from __future__ import annotations

from datetime import date

import pytest

from su_multi_arm.decision.log_guard import log_scope_guard
from su_multi_arm.decision.log_intents import CompileContext, LogIntentCall, compile_intent, detect

AS_OF = date(2026, 3, 10)
VALUES = {"service": ["svc-api", "svc-auth", "svc-legacy"], "tenant": ["t01", "t02"], "signature_id": ["process.crashed", "feature.flag_conflict", "dependency.connect_failed"]}


def call(question: str) -> LogIntentCall | None:
    return detect(question, as_of=AS_OF, values=lambda f: VALUES.get(f, []))


@pytest.mark.parametrize(
    "question,intent,subset",
    [
        ("which services had FATAL yesterday", "log_top", {"by": "service", "level": ["fatal"], "window": "yesterday"}),
        ("top errors for service svc-api", "log_top", {"by": "signature", "level": ["error", "fatal"], "service": ["svc-api"]}),
        ("top 3 exceptions this week", "log_top", {"by": "exception_class", "top_n": 3}),
        ("what's new today", "log_new", {"window": "today"}),
        ("which tenants are affected by process.crashed", "log_affected", {"signature_id": "process.crashed"}),
        ("trend of dependency.connect_failed over 10 days", "log_trend", {"signature_id": "dependency.connect_failed", "window": "last 10 days"}),
        ("errors for svc-api per hour yesterday", "log_trend", {"granularity": "hour", "service": "svc-api"}),
        ("errors for svc-api yesterday vs the day before", "log_compare", {"periods": ["yesterday", "2026-03-08"]}),
        ("which tenants had FATAL in svc-auth yesterday", "log_tenants", {"service": "svc-auth", "level": "fatal"}),
        ("what else broke around 14:00 for t02 on 2026-03-05", "log_around", {"at": "2026-03-05T14:00:00Z", "tenant": "t02"}),
        ("slowest endpoints of svc-api today", "log_endpoints", {"metric": "latency_max", "service": "svc-api"}),
        ("5xx by endpoint for svc-api", "log_endpoints", {"metric": "errors_5xx"}),
        ("what does process.crashed mean", "log_signature", {"signature_id": "process.crashed"}),
        ("how much of yesterday did we parse", "log_coverage", {"window": "yesterday"}),
    ],
)
def test_question_families(question, intent, subset):
    found = call(question)
    assert found is not None and found.intent == intent, found
    for key, value in subset.items():
        got = found.args.get(key)
        if key == "at":
            got = got.replace("+00:00", "Z")
        assert got == value, (key, found.args)


@pytest.mark.parametrize("question", ["How do I configure the source?", "explain retention", "hello"])
def test_non_log_questions_are_not_intents(question):
    assert call(question) is None


def test_compilation_is_a_function_of_the_arguments():
    one = LogIntentCall(intent="log_top", args={"by": "service", "level": ["ERROR"], "window": "yesterday", "as_of": "2026-03-10"})
    two = LogIntentCall(intent="log_top", args={"window": "yesterday", "as_of": "2026-03-10", "level": ["ERROR"], "by": "service", "top_n": 10})
    assert one == two
    ctx = CompileContext(today=date(2030, 1, 1))
    first, second = compile_intent(one, ctx), compile_intent(two, ctx)
    assert first.scopes == second.scopes and first.window.start == date(2026, 3, 9)
    assert all(log_scope_guard(spec) is None for spec in first.scopes.values())  # every compiled scope pins one kind


def test_invalid_arguments_are_refused():
    with pytest.raises(ValueError):
        LogIntentCall(intent="log_top", args={"by": "planet"})
    with pytest.raises(ValueError):
        LogIntentCall(intent="log_compare", args={"periods": ["today"]})


# --- failure vocabulary and error rate (real-run fix) ---------------------------------------------


@pytest.mark.parametrize(
    "question,subset",
    [
        ("which service is failing the most", {"by": "service", "level": ["error", "fatal"]}),
        ("which services had failures yesterday", {"by": "service", "level": ["error", "fatal"], "window": "yesterday"}),
        ("what are the top problems in svc-api", {"by": "signature", "level": ["error", "fatal"], "service": ["svc-api"]}),
        ("what is broken in svc-auth today", {"level": ["error", "fatal"], "service": ["svc-auth"]}),
        ("what are issues that need my attention", {"by": "signature", "level": ["error", "fatal"]}),
        ("which service had ERROR yesterday", {"by": "service", "level": ["error"]}),  # a named level stays itself
        ("which services had FATAL yesterday", {"by": "service", "level": ["fatal"]}),
        ("which service has the highest error rate", {"by": "service", "metric": "error_rate"}),
        ("which tenants have the worst failure rate this week", {"by": "tenant", "metric": "error_rate"}),
    ],
)
def test_failure_words_map_to_error_and_fatal(question, subset):
    found = call(question)
    assert found is not None and found.intent == "log_top", found
    for key, value in subset.items():
        assert found.args.get(key) == value, (key, found.args)
    if subset.get("metric") == "error_rate":
        assert "level" not in found.args  # the default: ERROR and FATAL


def test_a_level_word_inside_a_named_value_is_not_a_level():
    found = call("How has dependency.connect_failed trended over 10 days?")
    assert found.intent == "log_trend" and "level" not in found.args


COUNTS = {"errors": ["error"], "fatal": ["fatal"]}


def test_error_rate_compiles_source_count_scopes_over_unit_day_and_refuses_signatures():
    """Real-run fix: the rate is the source's own level counts over the source's total lines
    (``unit_day``), never the pulled lines (only the pulled strata, capped)."""
    ctx = CompileContext(today=AS_OF, count_queries=COUNTS)
    compiled = compile_intent(LogIntentCall(intent="log_top", args={"by": "service", "metric": "error_rate", "window": "yesterday"}), ctx)
    assert set(compiled.scopes) == {"main", "count_fatal", "total", "units", "units_complete", "basis_errors", "basis_fatal"}
    assert all(log_scope_guard(s) is None for s in compiled.scopes.values())
    assert all(any(p.field == "kind" and p.value == "unit_day" for p in s.predicates) for s in compiled.scopes.values())
    assert compiled.scopes["main"].measure.field == "count_errors" and compiled.scopes["count_fatal"].measure.field == "count_fatal"
    assert compiled.scopes["total"].measure.field == "lines_total"
    assert any(p.field == "lines_total" for p in compiled.scopes["total"].predicates) and not any(p.field == "lines_total" for p in compiled.scopes["units"].predicates)
    assert not any(p.field in {"level", "extraction_source"} for s in compiled.scopes.values() for p in s.predicates)
    refused = compile_intent(LogIntentCall(intent="log_top", args={"by": "signature", "metric": "error_rate"}), ctx)
    assert refused.refusal and "error_rate" in refused.refusal


def test_error_rate_without_source_counts_for_a_level_is_refused_never_divided_by_pulled_lines():
    only_errors = CompileContext(today=AS_OF, count_queries={"errors": ["error"]})
    refused = compile_intent(LogIntentCall(intent="log_top", args={"by": "service", "metric": "error_rate"}), only_errors)
    assert refused.refusal and "FATAL" in refused.refusal and "not a denominator" in refused.refusal and not refused.scopes
    superset = CompileContext(today=AS_OF, count_queries={"failures": ["error", "fatal", "warn"]})  # would over-count
    assert compile_intent(LogIntentCall(intent="log_top", args={"by": "service", "metric": "error_rate"}), superset).refusal
    combined = CompileContext(today=AS_OF, count_queries={"failures": ["error", "fatal"]})
    compiled = compile_intent(LogIntentCall(intent="log_top", args={"by": "tenant", "metric": "error_rate"}), combined)
    assert compiled.refusal is None and compiled.scopes["main"].measure.field == "count_failures" and compiled.scopes["main"].group_by == ["tenant"]
    errors_only = compile_intent(LogIntentCall(intent="log_top", args={"by": "service", "metric": "error_rate", "level": ["error"]}), only_errors)
    assert errors_only.refusal is None and "count_fatal" not in errors_only.scopes


def test_count_fields_prefers_an_exact_level_set_then_single_levels():
    from su_multi_arm.decision.log_intents import count_fields

    assert count_fields(["fatal", "error"], {"failures": ["error", "fatal"], "errors": ["error"], "fatal": ["fatal"]}) == (["failures"], [])
    assert count_fields(["fatal", "error"], COUNTS) == (["errors", "fatal"], [])
    assert count_fields(["warn"], COUNTS) == ([], ["warn"])


@pytest.mark.parametrize(
    "levels,queries,expected,uncovered",
    [
        (["fatal"], COUNTS, [("FATAL", "fatal", "level")], []),
        (["error"], COUNTS, [("ERROR", "errors", "level")], []),
        (["warn"], COUNTS, [], ["warn"]),
        (["error", "fatal", "warn"], COUNTS, [("ERROR", "errors", "level"), ("FATAL", "fatal", "level")], ["warn"]),
        # the real-run case: no single-level FATAL query, one query over ERROR and FATAL
        (["fatal"], {"failures": ["error", "fatal"]}, [("ERROR+FATAL", "failures", "superset")], []),
        (["error", "fatal"], {"failures": ["error", "fatal"]}, [("ERROR+FATAL", "failures", "levels")], []),
        (["fatal"], {"failures": ["error", "fatal"], "severe": ["critical", "error", "fatal"]}, [("ERROR+FATAL", "failures", "superset")], []),
        (["warn", "fatal"], {"failures": ["error", "fatal"], "warn": ["warn"]}, [("WARN", "warn", "level"), ("ERROR+FATAL", "failures", "superset")], []),
    ],
)
def test_every_level_combination_has_a_source_count_plan(levels, queries, expected, uncovered):
    from su_multi_arm.runtime.log_tools import count_plan

    items, missing = count_plan(levels, queries)
    assert [(i["label"], i["query"], i["coverage"]) for i in items] == expected and missing == uncovered


def test_deployment_is_a_filter_and_a_grouping():
    from su_multi_arm.decision.log_intents import DEPLOYMENT_GROUP

    ctx = CompileContext(today=AS_OF, count_queries=COUNTS)
    top = compile_intent(LogIntentCall(intent="log_top", args={"by": "deployment", "level": ["error"], "deployment_id": ["1658254295"]}), ctx)
    assert top.scopes["main"].group_by == DEPLOYMENT_GROUP
    assert any(p.field == "deployment_id" and p.value == ["1658254295"] for p in top.scopes["main"].predicates)
    tenants = compile_intent(LogIntentCall(intent="log_tenants", args={"by": "deployment", "service": "svc-api", "deployment_id": "1658254295"}), ctx)
    assert tenants.scopes["main"].group_by == DEPLOYMENT_GROUP and any(p.field == "deployment_id" for p in tenants.scopes["main"].predicates)
    rate = compile_intent(LogIntentCall(intent="log_top", args={"by": "deployment", "metric": "error_rate"}), ctx)
    assert rate.scopes["total"].group_by == DEPLOYMENT_GROUP
    assert call("which deployments of svc-api had FATAL yesterday").args["by"] == "deployment"
    rate_call = call("which instance has the highest error rate").args
    assert rate_call["by"] == "deployment" and rate_call["metric"] == "error_rate" and "level" not in rate_call


def test_a_clock_anchor_is_not_pinned_into_the_call():
    """Real-run fix: the engine's clock default is not a day the caller named. Unpinned, the call
    carries no ``as_of``: relative words resolve against the clock at execution and an undated
    question keeps the latest-served-day anchor."""
    values = lambda f: VALUES.get(f, [])  # noqa: E731
    clock = detect("which services had FATAL", as_of=date(2026, 11, 4), values=values, pin=False)
    assert "as_of" not in clock.args and "window" not in clock.args
    relative = detect("which services had FATAL yesterday", as_of=date(2026, 11, 4), values=values, pin=False)
    assert relative.args["window"] == "yesterday" and "as_of" not in relative.args
    named = detect("which services had FATAL yesterday", as_of=date(2026, 11, 4), values=values)
    assert named.args["as_of"] == "2026-11-04"
    around = detect("what else broke around 14:00 for t02", as_of=date(2026, 11, 4), values=values, pin=False)
    assert around.args["at"].startswith("2026-11-04T14:00")  # a time without a day is read on the clock's day


def test_an_undated_question_is_anchored_on_the_latest_served_day_when_today_is_not_composed():
    ctx = CompileContext(today=date(2026, 11, 4), latest_day=date(2026, 3, 10))
    undated = compile_intent(LogIntentCall(intent="log_top", args={"by": "service"}), ctx)
    assert undated.window.start == date(2026, 3, 10) and undated.window.latest_anchor == date(2026, 3, 10)
    dated = compile_intent(LogIntentCall(intent="log_top", args={"by": "service", "window": "yesterday"}), ctx)
    assert dated.window.start == date(2026, 11, 3) and dated.window.latest_anchor is None  # a named window stays relative to today
    pinned = compile_intent(LogIntentCall(intent="log_top", args={"by": "service", "as_of": "2026-03-05"}), ctx)
    assert pinned.window.start == date(2026, 3, 5) and pinned.window.latest_anchor is None
