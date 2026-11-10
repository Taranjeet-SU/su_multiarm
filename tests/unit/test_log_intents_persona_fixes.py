"""Persona-eval follow-ups (fix5) in the rules grammar and the compiler: customer words are the
tenant dimension, tenant/service filters reach ``log_trend``/``log_compare``, change words are a
comparison, and periods compare in date order. Evidence: unit (synthetic)."""

from __future__ import annotations

from datetime import date

import pytest

from su_multi_arm.decision.log_intents import CompileContext, LogIntentCall, compile_intent, detect

AS_OF = date(2026, 3, 10)
VALUES = {"service": ["svc-api", "svc-auth", "svc-legacy"], "tenant": ["t01", "t02"], "signature_id": ["process.crashed", "dependency.connect_failed"]}


def call(question: str) -> LogIntentCall | None:
    return detect(question, as_of=AS_OF, values=lambda f: VALUES.get(f, []))


@pytest.mark.parametrize("word", ["customers", "clients", "tenants", "accounts", "customer"])
def test_customer_words_rank_by_tenant(word):
    found = call(f"Which {word} had the most problems yesterday?")
    assert found is not None and found.intent == "log_top" and found.args["by"] == "tenant" and found.args["level"] == ["error", "fatal"]


def test_customer_words_with_a_service_ask_the_tenants_of_that_service():
    found = call("which customers had FATAL in svc-auth yesterday")
    assert found is not None and found.intent == "log_tenants" and found.args["service"] == "svc-auth"


@pytest.mark.parametrize(
    "question,args",
    [
        ("How many errors did t01 have per day?", {"tenant": "t01", "level": ["error", "fatal"]}),
        ("daily errors of svc-api for t02", {"tenant": "t02", "service": "svc-api"}),
    ],
)
def test_trend_keeps_the_tenant_and_service_filters(question, args):
    found = call(question)
    assert found is not None and found.intent == "log_trend"
    for key, value in args.items():
        assert found.args.get(key) == value, found.args


@pytest.mark.parametrize(
    "question",
    [
        "Did errors for t01 increase on 2026-03-09 compared to 2026-03-08?",
        "Did errors for t01 drop on 2026-03-09 vs 2026-03-08?",
        "change in errors for t01 between 2026-03-09 and 2026-03-08",
    ],
)
def test_change_words_are_a_comparison_with_the_tenant(question):
    found = call(question)
    assert found is not None and found.intent == "log_compare", found
    assert found.args["tenant"] == "t01" and sorted(found.args["periods"]) == ["2026-03-08", "2026-03-09"]


def test_worse_means_failures():
    found = call("Did things get worse on 2026-03-09 compared to 2026-03-08?")
    assert found is not None and found.intent == "log_compare" and found.args["level"] == ["error", "fatal"]


def test_compare_periods_compile_in_date_order_with_the_tenant_filter():
    later_first = LogIntentCall(intent="log_compare", args={"periods": ["2026-03-09", "2026-03-08"], "tenant": "t01"})
    spec = compile_intent(later_first, CompileContext(today=AS_OF)).scopes["main"]
    assert [p.label for p in spec.periods] == ["2026-03-08", "2026-03-09"]
    assert any(p.field == "tenant" and p.value == "t01" for p in spec.predicates)
    trend = compile_intent(LogIntentCall(intent="log_trend", args={"tenant": "t02", "window": "last 7 days"}), CompileContext(today=AS_OF))
    assert any(p.field == "tenant" and p.value == "t02" for p in trend.scopes["main"].predicates)
