"""The ``serving.answers`` spend scope (addendum A2 rev 4): answer-time calls reserve on a persistent
per-day ledger under their own scope (separate from the learning scopes) and on the provider's
lifetime cap; a refusal comes before the request. Config load refuses a capped paid provider at
answer time without a finite ``serving.answers`` daily cap. Mocked provider, no network."""

from __future__ import annotations

import json

import pytest

from su_multi_arm.core.errors import BudgetExhausted, ConfigError
from su_multi_arm.learning.patterns.spend import DaySpendLedger, TotalSpendLedger
from su_multi_arm.providers import JsonModel, RunBudgetMeter
from su_multi_arm.runtime.answers_spend import SCOPE, AnswerSpend
from su_multi_arm.runtime.budget import BudgetLimits, PriceTable
from su_multi_arm.runtime.context import new_context
from tests.contract.test_providers import GEN, GOOD, KEY, SECRET, Recorder, Reply, chat, openai_role

PRICES = PriceTable(version="test-1", prices={"gpt-4.1-mini": {"input_per_million": 100.0, "output_per_million": 100.0}})


def test_answer_calls_reserve_on_their_own_scope_and_the_provider_total(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY, SECRET)
    ledger = tmp_path / "answers.sqlite"
    day = DaySpendLedger(ledger, max_calls=2, max_usd=None, scope=SCOPE, role="answers")
    total = TotalSpendLedger(ledger, key="provider:openai", max_usd=2.0)
    spend = AnswerSpend(day, {"openai": total}, {"gpt-4.1-mini": "openai"})
    rec = Recorder(chat(json.dumps(GOOD), usage={"prompt_tokens": 100, "completion_tokens": 20}))
    model = JsonModel(openai_role(), transport=rec.transport)
    for _ in range(2):
        ctx = new_context(GEN, limits=BudgetLimits(max_llm_calls=5, max_usd=1.0), price_table=PRICES, spend=spend)
        model.call("S", {}, Reply, RunBudgetMeter(ctx, "generator", "gpt-4.1-mini"), "generator")
    usage = spend.usage()
    assert usage["day"]["scope"] == "serving.answers" and usage["day"]["calls"] == 2
    assert usage["day"]["committed_usd"] == pytest.approx(2 * 120 * 100 / 1_000_000) and usage["day"]["reserved_usd"] == 0
    assert usage["totals"]["openai"]["used_usd"] == pytest.approx(usage["day"]["committed_usd"])
    # the third answer of the day is refused before its request, by the persistent day cap
    ctx = new_context(GEN, limits=BudgetLimits(max_llm_calls=5, max_usd=1.0), price_table=PRICES, spend=spend)
    with pytest.raises(BudgetExhausted, match="day_calls"):
        model.call("S", {}, Reply, RunBudgetMeter(ctx, "generator", "gpt-4.1-mini"), "generator")
    assert len(rec.requests) == 2
    assert ctx.budget.snapshot()["reserved"].get("usd", 0) == 0  # the request budget was released too
    # learning scopes are separate rows of the same kind of ledger
    learning = DaySpendLedger(ledger, max_calls=5, max_usd=None, scope="learning.patterns", role="pattern_learner")
    assert learning.usage()["calls"] == 0


def test_a_capped_paid_provider_needs_a_finite_answers_cap(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY, SECRET)
    from su_multi_arm.core.config import validate_config
    from tests.logs_support import config_data

    prices = tmp_path / "prices.yaml"
    prices.write_text("version: test-1\nprices: {gpt-4.1-mini: {input_per_million: 1.0, output_per_million: 5.0}}\n")
    data = config_data(tmp_path)
    data["models"]["roles"]["generator"] = {"provider": "openai", "model": "gpt-4.1-mini", "api_key_env": KEY, "max_tokens": 256}
    data["budgets"] = {"price_table": str(prices), "providers": {"openai": {"max_usd_total": 2.0}}}
    with pytest.raises(ConfigError, match="serving.answers needs enabled: true and a finite max_usd_per_day"):
        validate_config(data)
    data["serving"] = {"answers": {"enabled": True, "max_usd_per_day": 0.5}}
    assert validate_config(data).serving.answers.max_usd_per_day == 0.5
    # without a provider lifetime cap nothing changes for existing configs
    data.pop("serving")
    data["budgets"] = {"price_table": str(prices)}
    assert validate_config(data).serving.answers.enabled is False
