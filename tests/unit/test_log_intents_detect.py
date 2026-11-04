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
        ("top errors for service svc-api", "log_top", {"by": "signature", "level": ["error"], "service": ["svc-api"]}),
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
