"""Worst-case answer spend check (``budgets.plan_check``; docs/plans/unified_merge.md §6.13, M0).

Synthetic configs and a synthetic price table only (illustrative rates, not a vendor price list)."""

from __future__ import annotations

import copy
import logging

import pytest
import yaml

from su_multi_arm.core.config import validate_config
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.runtime.budget import load_price_table
from tests import typed_support
from su_multi_arm.runtime.plan_cost import CHARS_PER_TOKEN, enforce, plan_check, slots_for, worst_case

STRONG, SMALL = "claude-sonnet-5-5", "claude-haiku-5-5"
PRICES = {"version": "synthetic-1", "prices": {STRONG: {"input_per_million": 2.0, "output_per_million": 10.0}, SMALL: {"input_per_million": 0.1, "output_per_million": 0.5}}}
BASE = {
    "schema_version": 2,
    "dataset": "demo",
    "backends": {"local": {"kind": "sqlite", "path": "./state"}},
    "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
    "pipeline": {"arms": {"dense": {"backend": "local"}, "lexical": {"backend": "local"}}, "decision": {"kind": "llm"}},
}


def role(model: str, max_tokens: int, **extra) -> dict:
    return {"provider": "anthropic", "model": model, "api_key_env": "SU_TEST_KEY", "max_tokens": max_tokens, **extra}


@pytest.fixture
def prices(tmp_path):
    path = tmp_path / "prices.yaml"
    path.write_text(yaml.safe_dump(PRICES))
    return path


def config(prices, *, budgets=None, roles=None, pipeline=None, **top):
    data = copy.deepcopy(BASE)
    data["budgets"] = {"price_table": str(prices), "max_usd": 0.25, "max_llm_calls": 5, **(budgets or {})}
    data["models"] = {"roles": roles if roles is not None else {"planner": role(STRONG, 2048), "generator": role(SMALL, 6000, on_incomplete="retry_shorter")}}
    for key, value in (pipeline or {}).items():
        data["pipeline"][key] = value
    data.update(top)
    return validate_config(data)


def test_default_mode_is_warn_and_existing_configs_start(prices, caplog):
    cfg = config(prices, budgets={"max_usd": 0.001})
    assert cfg.budgets.plan_check == "warn"
    with caplog.at_level(logging.WARNING):
        report = enforce(plan_check(cfg, load_price_table(prices)))
    assert report.status == "exceeds" and "exceeds budgets.max_usd" in caplog.text


def test_refuse_mode_raises_only_when_set(prices):
    cfg = config(prices, budgets={"max_usd": 0.001, "plan_check": "refuse"})
    with pytest.raises(ConfigError, match="budgets.plan_check"):
        enforce(plan_check(cfg, load_price_table(prices)))
    ok = config(prices, budgets={"max_usd": 5.0, "plan_check": "refuse"})
    assert enforce(plan_check(ok, load_price_table(prices))).status == "ok"


def test_off_mode_computes_nothing(prices):
    report = plan_check(config(prices, budgets={"plan_check": "off", "max_usd": 0.001}), load_price_table(prices))
    assert report.status == "off" and report.slots == []


def test_formula_planner_repair_generator_and_retry(prices):
    cfg = config(prices, pipeline={"decision": {"kind": "llm", "schema_repair_attempts": 1}})
    table = load_price_table(prices)
    slots, unpriced, _ = slots_for(cfg, table)
    assert unpriced == []
    stages = [(s.stage, s.retry) for s in slots]
    assert stages == [("planner", None), ("planner", None), ("generator", None), ("generator", "incomplete")]
    planner, generator = slots[0], slots[2]
    assert planner.output_tokens == 2048 and generator.output_tokens == 6000
    # I = (prompt + evidence + catalog) chars / 3: the generator's evidence is the packing budget
    assert generator.input_tokens >= cfg.pipeline.selection.packing.budget_chars // CHARS_PER_TOKEN
    assert planner.usd == pytest.approx(table.cost(STRONG, planner.input_tokens, 2048))
    report = plan_check(cfg, table)
    assert report.worst_case_usd == pytest.approx(sum(s.usd for s in slots), abs=1e-6)  # 4 slots <= max_llm_calls 5


def test_retry_larger_slot_uses_the_larger_ceiling(prices):
    cfg = config(prices, roles={"planner": role(STRONG, 2048), "generator": role(SMALL, 4096, on_incomplete="retry_larger", incomplete_retry_max_tokens=8192)})
    slots, _, _ = slots_for(cfg, load_price_table(prices))
    assert [(s.retry, s.output_tokens) for s in slots if s.stage == "generator"] == [(None, 4096), ("incomplete", 8192)]


def typed(prices, tmp_path, *, decision, roles, budgets=None, arm=None, request=None, review=None):
    data = typed_support.config_dict(tmp_path / "state")
    data["budgets"] = {"price_table": str(prices), "max_usd": 0.25, "max_llm_calls": 7, **(budgets or {})}
    data["models"] = {"roles": roles}
    data["pipeline"]["decision"] = decision
    if arm:
        data["pipeline"]["arms"]["typed_query"].update(arm)
    if review:
        data["pipeline"]["review"] = review
    if request:
        data["request"] = request
    return validate_config(data)


def test_typed_query_replan_slots_are_counted(prices, tmp_path):
    """Coordinator re-review: ``decision.typed_query.repair_attempts`` re-plans are planner calls."""
    roles = {"planner": role(STRONG, 2048), "generator": role(SMALL, 4096)}
    cfg = typed(prices, tmp_path, roles=roles, decision={"kind": "llm", "schema_repair_attempts": 1, "typed_query": {"enabled": True, "repair_attempts": 1}})
    slots, _, unmeasured = slots_for(cfg, load_price_table(prices))
    assert [s.stage for s in slots].count("planner") == 3
    assert "typed catalog in the planner payload" in unmeasured


def test_community_answer_role_is_counted(prices):
    """Coordinator re-review: ``community`` is an answer-time role of the serving.answers ledger."""
    roles = {"planner": role(STRONG, 2048), "generator": role(SMALL, 4096), "community": role(SMALL, 1024, evidence_budget={"chars": 9000})}
    slots, _, _ = slots_for(config(prices, roles=roles), load_price_table(prices))
    community = [s for s in slots if s.stage == "community"]
    assert len(community) == 1 and community[0].input_tokens >= 9000 // CHARS_PER_TOKEN and community[0].output_tokens == 1024


def test_transient_slots_are_reported_beside_the_formula(prices):
    roles = {"planner": role(STRONG, 2048, retry_transient={"attempts": 1}), "generator": role(SMALL, 4096)}
    report = plan_check(config(prices, roles=roles, budgets={"max_llm_calls": 5}), load_price_table(prices))
    assert report.worst_case_with_transient_usd > report.worst_case_usd
    assert len([s for s in report.slots if s.retry == "transient"]) == 1


def test_w_takes_the_max_llm_calls_most_expensive_slots(prices):
    cfg = config(prices, budgets={"max_llm_calls": 2})
    slots, _, _ = slots_for(cfg, load_price_table(prices))
    assert plan_check(cfg, load_price_table(prices)).worst_case_usd == pytest.approx(sum(sorted((s.usd for s in slots), reverse=True)[:2]), abs=1e-6)
    assert worst_case(slots, 1, transient=False) == pytest.approx(max(s.usd for s in slots), abs=1e-6)


def test_evidence_uses_the_larger_of_the_budget_and_evidence_chars_max(prices):
    small = slots_for(config(prices), load_price_table(prices))[0]
    large = slots_for(config(prices, request={"evidence_chars_max": 120000}), load_price_table(prices))[0]
    gen = lambda slots: next(s for s in slots if s.stage == "generator")  # noqa: E731
    assert gen(large).input_tokens - gen(small).input_tokens == pytest.approx((120000 - 60000) / CHARS_PER_TOKEN, abs=1)


def test_unpriced_role_is_reported_and_refused_only_in_refuse_mode(prices):
    roles = {"planner": role("claude-opus-4-1", 2048), "generator": role(SMALL, 4096)}
    warn = plan_check(config(prices, roles=roles), load_price_table(prices))
    assert warn.status == "unpriced" and warn.unpriced == ["claude-opus-4-1"]
    enforce(warn)  # warn: starts
    with pytest.raises(ConfigError):
        enforce(plan_check(config(prices, roles=roles, budgets={"plan_check": "refuse"}), load_price_table(prices)))


def test_live_support_shape_warns_and_starts(prices, tmp_path, caplog):
    """§6.13 test: a synthetic copy of the large support config shape (llm planner with repair, typed
    router + SQL writer with repair, two review hops, a 120k-character evidence ceiling, strong-model
    roles) exceeds a 0.25 cap under the formula; the default ``warn`` logs and starts."""
    roles = {"planner": role(STRONG, 3000), "reviewer": role(STRONG, 3000), "query_writer": role(STRONG, 3000), "generator": role("claude-haiku-4-5", 4096)}
    prices_all = copy.deepcopy(PRICES)
    prices_all["prices"]["claude-haiku-4-5"] = {"input_per_million": 1.0, "output_per_million": 5.0}
    prices.write_text(yaml.safe_dump(prices_all))
    cfg = typed(
        prices, tmp_path, roles=roles, request={"evidence_chars_max": 120000}, review={"enabled": True, "max_hops": 2},
        decision={"kind": "llm", "schema_repair_attempts": 1, "typed_query": {"enabled": True, "routing": "router", "router_role": "query_writer"}},
        arm={"mode": "sql_surface", "sql_surface": {"role": "query_writer"}},
    )
    with caplog.at_level(logging.WARNING):
        report = enforce(plan_check(cfg, load_price_table(prices)))
    assert report.status == "exceeds" and report.worst_case_usd > 0.25 and "exceeds budgets.max_usd" in caplog.text
    assert {"planner", "typed_router", "sql_writer", "review", "generator"} <= {s.stage for s in report.slots}
    assert "SQL catalog of the SQL writer" in report.unmeasured


def test_m0_log_shape_fits_the_proposed_cap(prices):
    """§6.13 M0 numbers: strong planner (2,048 out) + one repair + small generator (6,000 out, one
    retry_shorter) with ``max_usd 0.25`` and 5 calls stays under the cap with the built-in prompts.
    The plan's own estimate assumes a ~30k-token planner input (a large logs context pack); at that
    size W is about US$0.17, which this test reproduces by declaring the planner context."""
    cfg = config(prices, pipeline={"decision": {"kind": "llm", "schema_repair_attempts": 1}})
    report = plan_check(cfg, load_price_table(prices))
    assert report.status == "ok" and report.worst_case_usd <= 0.25
    big = config(prices, pipeline={"decision": {"kind": "llm", "schema_repair_attempts": 1, "prompt": "x" * 90_000}})
    heavy = plan_check(big, load_price_table(prices))
    assert 0.15 < heavy.worst_case_usd < 0.25


def test_the_engine_runs_the_check_at_startup(prices, tmp_path, monkeypatch):
    """Where the check runs: ``Engine.__init__`` (every server, CLI and Python API engine)."""
    from su_multi_arm import Engine

    monkeypatch.setenv("SU_TEST_KEY", "sk-test-not-a-real-key")

    data = copy.deepcopy(BASE)
    data["backends"]["local"]["path"] = str(tmp_path / "state")
    data["budgets"] = {"price_table": str(prices), "max_usd": 0.001, "max_llm_calls": 5}
    data["models"] = {"roles": {"planner": role(STRONG, 2048), "generator": role(SMALL, 4096)}}
    engine = Engine(validate_config(data))
    assert engine.plan_check.status == "exceeds" and engine.plan_check.note()["mode"] == "warn"
    data["budgets"]["plan_check"] = "refuse"
    with pytest.raises(ConfigError, match="budgets.plan_check"):
        Engine(validate_config(data))


def test_a_vocabulary_pin_mismatch_refuses_to_load(tmp_path):
    from su_multi_arm.core.config import load_config

    (tmp_path / "vocabulary.yaml").write_text("- {term: crawl, values: [crawler]}\n")
    data = copy.deepcopy(BASE)
    data["pipeline"]["decision"] = {"kind": "rules"}
    data["question"] = {"vocabulary": {"file": "vocabulary.yaml", "sha256": "0" * 64}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="does not match the pinned"):
        load_config(tmp_path / "config.yaml")
