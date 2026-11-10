"""logs.intents.not_values: words never read as a field value when a log question is bound (synthetic)."""

from datetime import date

from su_multi_arm.core.config_logs import LogIntentsConfig
from su_multi_arm.decision.log_intents import bind_question_scope, detect_issue_history
from su_multi_arm.decision.log_question import QuestionSettings

TENANTS = ["acme", "logbox"]  # "logbox" is the log platform's own name and also a collected tenant id


def values_of(settings: QuestionSettings):
    def values(field: str) -> list[str]:
        return settings.keep(field, list(TENANTS)) if field == "tenant" else []
    return values


def test_config_accepts_not_values_and_defaults_empty():
    assert LogIntentsConfig().not_values == {}
    assert LogIntentsConfig(not_values={"tenant": ["LogBox"]}).not_values == {"tenant": ["LogBox"]}


def test_keep_drops_the_configured_words_casefold_for_that_field_only():
    s = QuestionSettings(not_values={"tenant": frozenset({"logbox"})})
    assert s.keep("tenant", ["acme", "LogBox"]) == ["acme"]
    assert s.keep("service", ["logbox"]) == ["logbox"]
    assert QuestionSettings().keep("tenant", ["logbox"]) == ["logbox"]


def test_a_platform_word_no_longer_makes_a_one_tenant_question_name_two_tenants():
    as_of = date(2026, 11, 10)
    call = detect_issue_history("acme outages in last 7 days", as_of=as_of, values=values_of(QuestionSettings()))
    assert call is not None
    q = "acme outages in last 7 days and their logbox logs"
    try:
        bind_question_scope(call, q, as_of=as_of, values=values_of(QuestionSettings()))
        raised = None
    except ValueError as error:
        raised = str(error)
    assert raised and "only one tenant" in raised
    s = QuestionSettings(not_values={"tenant": frozenset({"logbox"})})
    bound = bind_question_scope(call, q, as_of=as_of, values=values_of(s))
    assert bound.args.get("tenant") in ("acme", ["acme"])
