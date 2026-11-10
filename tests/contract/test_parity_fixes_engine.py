"""Engine fixes from the 2026-11-08 production-workload parity rerun (synthetic corpus, fake models).

* Defect 2: ``models.roles.query_writer`` serves the typed router and the SQL writer
  (``typed_query.router_role`` / ``sql_surface.role``); with a registry-default ``native`` mode it
  stays native, while the planner role is moved to prompt mode by its ``LlmPlan`` schema.
* Defect 4: a failed typed route flags every aggregate intent; once the planner's own structured
  step executed exactly, those per-intent flags are dropped (the route's own constraints stay).
* Defect 7: the router chain runs concurrently with the planner call (proved with a barrier the
  sequential order would never release); the trace's call order and metering are the sequential
  ones; under a call budget that cannot cover both chains, or ``parallel: false``, it is sequential.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("sqlglot")

from su_multi_arm import Engine, SearchRequest  # noqa: E402
from su_multi_arm.core.config import ModelRoles, validate_config  # noqa: E402
from su_multi_arm.core.errors import ConfigError  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
import test_typed_router_engine as router_tests  # noqa: E402
from test_typed_router_engine import ALPHA, COUNT_Q, FAKE, TYPED, config_data, engine_for, sql  # noqa: E402

import typed_support as ts  # noqa: E402

ANTHROPIC = {"provider": "anthropic", "model": "claude-sonnet-5-5", "api_key_env": "SYNTHETIC_KEY"}
STRUCTURED_COUNT = {"arm": "structured", "scope": {"predicates": [{"field": "status", "op": "eq", "value": "open"}, {"field": "module", "op": "eq", "value": "Billing"}], "measure": {"kind": "count"}}}


@pytest.fixture
def fake(monkeypatch):
    """The scripted router / SQL writer / planner / generator of ``test_typed_router_engine``."""
    return router_tests.fake.__pytest_wrapped__.obj(monkeypatch)


# --- defect 2: a separate role for the router and the SQL writer ----------------------------------


def test_the_query_writer_role_keeps_native_structured_output():
    roles = ModelRoles(planner=dict(ANTHROPIC), query_writer=dict(ANTHROPIC))
    assert roles.planner.capabilities.structured_output == "none" and roles.planner.structured_output_note  # LlmPlan too complex
    assert roles.query_writer.capabilities.structured_output == "native" and roles.query_writer.structured_output_note is None
    assert "query_writer" in roles.chat_roles()


def test_router_and_sql_writer_run_on_the_query_writer_role(tmp_path, fake):
    data = config_data(tmp_path, typed={"router_role": "query_writer"}, arm={"sql_surface": {"role": "query_writer", "repair_attempts": 1}})
    data["models"]["roles"]["query_writer"] = {**FAKE, "model": "fake-writer"}
    engine = Engine(validate_config(data))
    engine.ingest(ts.documents())
    result = engine.search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    assert fake["roles"] == {"typed_router": "fake-writer", "sql_surface": "fake-writer", "planner": "fake"}
    assert result.aggregates[0]["value"] == 1 and result.aggregates[0]["exactness"] == "exact"


def test_a_missing_query_writer_role_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="models.roles.query_writer"):
        validate_config(config_data(tmp_path, typed={"router_role": "query_writer"}))
    with pytest.raises(ConfigError, match="models.roles.query_writer"):
        validate_config(config_data(tmp_path, arm={"sql_surface": {"role": "query_writer"}}))


def test_the_v1_downgrade_refuses_a_query_writer_role():
    from su_multi_arm.compat.config_v1 import downgrade
    from su_multi_arm.compat.config_v1 import upgrade_mapping

    import yaml

    data = upgrade_mapping(yaml.safe_load((Path(__file__).parents[2] / "examples/local.yaml").read_text())).data
    data.setdefault("models", {}).setdefault("roles", {})["query_writer"] = dict(FAKE)
    with pytest.raises(ConfigError, match="query_writer"):
        downgrade(validate_config(data))


# --- defect 4: an exact structured step covers the aggregate intent after a failed route -------------


def test_an_exact_planner_count_drops_the_failed_routes_intent_flags(tmp_path, fake):
    fake["sql"] = [sql("SELECT count(*) FROM ticket WHERE module = 'Payroll'")]  # sql_unsupported: no such value
    fake["steps"] = [STRUCTURED_COUNT]
    fake["count"] = 1
    result = engine_for(tmp_path, decision={"on_step_failure": "partial"}).answer(SearchRequest(query=COUNT_Q), principal=ALPHA)
    decision = result.retrieval.trace["decision"]
    assert [d["reason"] for d in decision["dropped_steps"]] == ["sql_unsupported"]
    [aggregate] = result.retrieval.aggregates
    assert aggregate["exactness"] == "exact" and aggregate["value"] == 1
    reasons = {u.reason for u in result.unsupported_constraints}
    assert "aggregate_intent_without_scope" not in reasons and "value_not_observed" in reasons  # the route's own constraint stays
    assert all(u["reason"] != "aggregate_intent_without_scope" for u in decision["unsupported"])
    assert result.contract == "passed" and [c.kind for c in result.answer.claims] == ["count"]


def test_a_bounded_planner_count_keeps_the_intent_flags(tmp_path, fake):
    fake["sql"] = [sql("SELECT count(*) FROM ticket WHERE module = 'Payroll'")]
    fake["steps"] = [{"arm": "structured", "scope": {"predicates": [{"field": "status", "op": "eq", "value": "open"}], "measure": {"kind": "count"}}}]  # drops module
    result = engine_for(tmp_path, decision={"on_step_failure": "partial"}).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    [aggregate] = result.aggregates
    assert aggregate["exactness"] == "bounded"
    assert "aggregate_intent_without_scope" in {u.reason for u in result.unsupported_constraints}


# --- defect 7: router chain and planner run concurrently --------------------------------------------


def _barrier(fake, monkeypatch, timeout: float):
    """The router call waits for the planner call to start: only a concurrent run releases it."""
    from su_multi_arm.providers import JsonModel

    planner_started = threading.Event()
    waited: dict[str, bool] = {}
    respond = JsonModel.call

    def call(self, system, payload, schema, budget, stage):
        if stage == "planner":
            planner_started.set()
        if stage == "typed_router":
            waited["released"] = planner_started.wait(timeout)
        return respond(self, system, payload, schema, budget, stage)

    monkeypatch.setattr(JsonModel, "call", call)
    return waited


def test_the_router_chain_runs_concurrently_with_the_planner(tmp_path, fake, monkeypatch):
    waited = _barrier(fake, monkeypatch, timeout=10)
    engine = engine_for(tmp_path)
    for _ in range(3):
        waited.clear()
        fake["route"], fake["sql"] = [TYPED], [sql("SELECT count(*) AS n FROM ticket WHERE status = 'open' AND module = 'Billing'")]
        result = engine.search(SearchRequest(query=COUNT_Q), principal=ALPHA)
        assert waited["released"] is True  # the planner call started while the router call was in flight
        # deterministic merge: the trace's call order and the plan are those of the sequential run
        assert [c["stage"] for c in result.trace["model_calls"]] == ["typed_router", "sql_surface", "planner"]
        assert [s["arm"] for s in result.trace["steps"]] == ["typed_query", "lexical"] and result.aggregates[0]["value"] == 1


@pytest.mark.parametrize("how", ["parallel_off", "tight_budget"])
def test_the_router_chain_is_sequential_when_off_or_when_the_budget_is_tight(tmp_path, fake, monkeypatch, how):
    waited = _barrier(fake, monkeypatch, timeout=0.3)
    if how == "parallel_off":
        engine = engine_for(tmp_path, typed={"parallel": False})
    else:
        engine = engine_for(tmp_path)
        engine.config.budgets.max_llm_calls = 3  # router + writer + repair = 3: no room for the planner beside them
    result = engine.search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    assert waited["released"] is False  # the planner started only after the router chain
    assert fake["order"][:2] == ["typed_router", "sql_surface"]
    if how == "parallel_off":
        assert fake["order"] == ["typed_router", "sql_surface", "planner"]
    assert result.trace["model_calls"][0]["stage"] == "typed_router"


def test_a_planner_error_still_waits_for_the_router_chain(tmp_path, fake, monkeypatch):
    from su_multi_arm.decision import LlmAgent

    def boom(self, ctx, request, intents, capabilities):
        raise RuntimeError("planner crashed")

    monkeypatch.setattr(LlmAgent, "decide", boom)
    with pytest.raises(RuntimeError, match="planner crashed"):
        engine_for(tmp_path).search(SearchRequest(query=COUNT_Q), principal=ALPHA)
    assert fake["order"][:1] == ["typed_router"]  # the router chain ran (and was awaited) before the error surfaced
