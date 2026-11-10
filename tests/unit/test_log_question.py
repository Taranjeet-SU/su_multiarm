"""M0 question binding of log intents (docs/plans/unified_merge.md §6.11, §9, §10.1).

Outage/presence questions without a period read the default view (last N days, not today only);
a bare clock hour is read in the question clock (``question.timezone``) with both 12-hour readings
when am/pm is missing, and stated; vocabulary terms ground to indexed service values only.
Synthetic services, tenants and terms."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from su_multi_arm.core.config_m0 import QuestionConfig, QuestionVocabulary, VocabularyTerm
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.decision.log_intents import CompileContext, LogIntentCall, compile_intent, detect
from su_multi_arm.decision.log_question import QuestionSettings, bind_question, ground_terms, read_clock, strip_terms

NOW = datetime(2026, 11, 9, 9, 30, tzinfo=timezone.utc)  # 15:00 in a +05:30 zone
VOCAB = (
    VocabularyTerm(term="crawl", match="prefix", values=["crawler_svc", "crawler_sched", "unindexed_svc"]),
    VocabularyTerm(term="slow", field="symptom", note="'slow' is a symptom; the logs record failure signals, not latency."),
    VocabularyTerm(term="ghost", values=["never_indexed"]),
)
SERVICES = ["crawler_svc", "crawler_sched", "search_api"]


def settings(tz: str = "UTC", **kw) -> QuestionSettings:
    return QuestionSettings(timezone=tz, logs_timezone="UTC", view_days=3, vocabulary=VOCAB, now=lambda: NOW, **kw)


def values(field: str) -> list[str]:
    return {"service": SERVICES, "tenant": ["t01", "t02"]}.get(field, [])


# --- question clock -------------------------------------------------------------------------------


def test_a_bare_hour_has_two_12_hour_readings_in_the_question_clock():
    reading = read_clock("client reported crawling failed at 2, what was the error", settings("Asia/Kolkata"))
    assert reading is not None and reading.ambiguous and not reading.dated and reading.text == "at 2"
    local = [(d.date().isoformat(), d.hour) for d in reading.local]
    assert local == [("2026-11-09", 2), ("2026-11-09", 14)]  # most recent occurrences before 15:00 local
    assert [d.strftime("%Y-%m-%d %H:%M") for d in reading.utc()] == ["2026-11-08 20:30", "2026-11-09 08:30"]
    text = reading.statement()
    assert "no date and no am/pm" in text and "Asia/Kolkata" in text and "02:00" in text and "14:00" in text and "UTC" in text


@pytest.mark.parametrize(
    "question, hours, ambiguous",
    [
        ("what broke at 2pm", [14], False),
        ("what broke at 2 a.m.", [2], False),
        ("what broke around 14:00", [14], False),
        ("what broke at 02:30", [2], False),
        ("errors at 12", [0, 12], True),
    ],
)
def test_clock_forms(question, hours, ambiguous):
    reading = read_clock(question, settings())
    assert sorted(d.hour for d in reading.local) == hours and reading.ambiguous is ambiguous


@pytest.mark.parametrize("question", ["errors in the last 3 days", "top 10 errors", "at 2026-11-05 what failed", "about 5 errors for t01", "the report queued for 3 hours"])
def test_no_clock_reading_without_a_clock_hour(question):
    assert read_clock(question, settings()) is None


def test_a_named_day_anchors_the_reading():
    reading = read_clock("what failed at 2 on 2026-11-05", settings())
    assert reading.dated and {d.date() for d in reading.local} == {date(2026, 11, 5)}


# --- vocabulary ---------------------------------------------------------------------------------


def test_vocabulary_grounds_to_indexed_values_only():
    grounded = ground_terms("crawling failed and search is slow", VOCAB, SERVICES)
    assert grounded.services == ["crawler_svc", "crawler_sched"]  # unindexed_svc is not applied
    assert grounded.matched == ["crawling", "slow"] and any("symptom" in s for s in grounded.statements)
    assert "crawling" not in strip_terms("crawling failed", VOCAB)


def test_a_term_with_only_unindexed_values_refuses_the_scope():
    call = detect("which services had errors for ghost jobs", as_of=date(2026, 11, 9), values=values, pin=False)
    with pytest.raises(ValueError, match="not in the indexed vocabulary"):
        bind_question(call, "which services had errors for ghost jobs", settings(), as_of=date(2026, 11, 9), values=values)


def test_a_named_service_wins_over_a_vocabulary_term():
    call = LogIntentCall(intent="log_top", args={"by": "signature", "service": ["search_api"]})
    bound = bind_question(call, "top errors of search_api while crawling", settings(), as_of=None, values=values)
    assert bound.calls[0].args["service"] == ["search_api"]


def test_a_named_service_the_term_also_reads_widens_to_the_term_s_services():
    # "crawler_svc failures while crawling": the named service is one of the term's services, so
    # every crawl service is read (owner test 2026-11-10: "crawler failures" read one service only)
    call = LogIntentCall(intent="log_top", args={"by": "signature", "service": ["crawler_svc"]})
    bound = bind_question(call, "crawler_svc failures while crawling", settings(), as_of=None, values=values)
    assert bound.calls[0].args["service"] == ["crawler_svc", "crawler_sched"]


def test_vocabulary_file_is_pinned(tmp_path):
    path = tmp_path / "vocabulary.yaml"
    path.write_text("- term: crawl\n  match: prefix\n  values: [crawler_svc]\n")
    import hashlib

    good = QuestionVocabulary(file=path, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    assert [t.term for t in good.entries()] == ["crawl"]
    with pytest.raises(ConfigError, match="does not match the pinned"):
        QuestionVocabulary(file=path, sha256="0" * 64).entries()
    with pytest.raises(ValueError):
        QuestionVocabulary(file=path)  # a file without a pin
    with pytest.raises(ValueError):
        QuestionConfig(timezone="Not/AZone")


# --- binding --------------------------------------------------------------------------------------


def test_outage_question_defaults_to_the_last_n_days():
    call = LogIntentCall(intent="log_top", args={})
    bound = bind_question(call, "search is slow is there any outage", settings(), as_of=date(2026, 11, 9), values=values)
    assert bound.calls[0].args["window"] == "last 3 days"
    assert any("default view" in n for n in bound.notes) and any("symptom" in n for n in bound.notes)
    window = compile_intent(bound.calls[0], CompileContext(today=date(2026, 11, 9))).window
    assert (window.start, window.end) == (date(2026, 11, 7), date(2026, 11, 10))  # not today only


def test_a_named_period_is_kept():
    call = detect("which services had errors yesterday, any outage?", as_of=date(2026, 11, 9), values=values, pin=False)
    bound = bind_question(call, "which services had errors yesterday, any outage?", settings(), as_of=date(2026, 11, 9), values=values)
    assert bound.calls[0].args["window"] == "yesterday"


def test_issue_breakdown_with_a_bare_hour_and_a_service_term():
    """The acceptance shape: "crawling failed at 2, what was the error" -> the crawler services on
    the days of both readings, bounded, with the clock statement."""
    question = "client reported crawling failed at 2, what was the error, and was it fixed automatically"
    call = detect(question, as_of=date(2026, 11, 9), values=values, pin=False)
    assert call.intent == "log_top"
    bound = bind_question(call, question, settings("Asia/Kolkata"), as_of=date(2026, 11, 9), values=values)
    args = bound.calls[0].args
    assert args["service"] == ["crawler_svc", "crawler_sched"]
    assert (args["start"], args["end"]) == ("2026-11-08", "2026-11-09")  # the UTC days of both readings
    assert len(bound.unapplied) == 1 and bound.unapplied[0].reason == "qualifier_not_applied"
    assert "hour is not isolated" in bound.unapplied[0].suggestion and "02:00" in bound.unapplied[0].suggestion
    assert any("crawler_svc" in n for n in bound.notes)


def test_log_around_runs_once_per_reading():
    call = LogIntentCall(intent="log_around", args={"at": "2026-11-09T02:00:00+00:00", "tenant": "t01"})
    bound = bind_question(call, "what else broke around 2 for t01", settings("Asia/Kolkata"), as_of=date(2026, 11, 9), values=values)
    assert [c.args["at"] for c in bound.calls] == ["2026-11-08T20:30:00Z", "2026-11-09T08:30:00Z"]
    assert all(c.args["tenant"] == "t01" for c in bound.calls) and bound.unapplied == []
    assert "Each reading is shown separately" in bound.notes[0]


def test_utc_24h_reading_keeps_the_existing_log_around_call():
    call = detect("what else broke around 14:00 for t02 on 2026-03-05", as_of=date(2026, 3, 5), values=values)
    bound = bind_question(call, "what else broke around 14:00 for t02 on 2026-03-05", settings(), as_of=date(2026, 3, 5), values=values)
    assert len(bound.calls) == 1 and bound.calls[0].args["at"].startswith("2026-03-05T14:00")


def test_no_settings_effect_without_terms_hours_or_presence():
    call = LogIntentCall(intent="log_top", args={"by": "service"})
    bound = bind_question(call, "which services had FATAL", settings(), as_of=date(2026, 11, 9), values=values)
    assert bound.calls == [call] and bound.notes == [] and bound.unapplied == []
