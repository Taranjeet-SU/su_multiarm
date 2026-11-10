"""Staging round 6 polish (synthetic; fake providers only, no service).

1. The operations section (``Current logs:``) opens with one reading line built from the executed
   operations aggregates: as-of, window, question clock (and the log clock when it differs), the
   services read (bound ``service`` arguments) and the tenant (bound ``tenant``), domain-neutral.
2. N15: with ``pipeline.generation.schema_repair_attempts: 1`` a generator reply that fails the
   answer schema gets one repair call (the previous reply and its validation paths, the repair
   prompt), metered like any call (call cap, trace ``model_call`` with ``attempt``/``retry_reason``)
   and traced as ``generator_schema_repair``; a refused or failed repair keeps the schema-violation
   answer. ``0`` (the default) makes exactly one call, as before.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
import yaml

from su_multi_arm import SearchRequest
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.types import ScopeResult
from su_multi_arm.engine import Engine
from su_multi_arm.generation.contract import AggregateEvidence
from su_multi_arm.generation.operations import OperationsSection, header_line
from su_multi_arm.models import Answer
from su_multi_arm.profiles.prompts import GENERATOR_REPAIR_PROMPT
from su_multi_arm.providers import JsonModel
from su_multi_arm.providers.base import ProviderServerError, SchemaViolation
from su_multi_arm.runtime.budget import load_price_table
from su_multi_arm.runtime.plan_cost import slots_for
from tests import two_halves_support as th

HALTED = "Has the crawl of the help-centre articles stopped? It is supposed to run every 30 minutes."
UNAUTHORIZED = "Brightmoor gets unauthorized errors connecting the MCP connector even though the credentials are correct"
REFUSED_CUSTOMER = "Cobaltfield Systems says search is slow, is there an outage?"


# --- 1. the reading line ------------------------------------------------------------------------------


def coverage(start: str, end: str, provisional: dict[str, str] | None = None) -> dict[str, Any]:
    return {"window": {"start": start, "end": end, "label": "synthetic"}, "tiers": {"days": 3, "provisional_days": provisional or {}}}


HISTORY = {
    "id": "aggregate:2",
    "intent": "issue_history",
    "status": "bounded",
    "args": {"window": "last 3 days", "service": ["svc-a", "svc-b"], "tenant": "tenant-1"},
    "rows": [],
    "operations_coverage": coverage("2026-11-07", "2026-11-09", {"2026-11-09": "2026-11-09T10:30:00+00:00"}),
}
LIVENESS = {
    "id": "aggregate:1",
    "intent": "log_liveness",
    "status": "bounded",
    "args": {"service": "svc-a"},
    "rows": [{"service": "svc-a", "observed_until": "2026-11-09T10:45:00+00:00", "status": "running"}],
    "operations_coverage": coverage("2026-11-03", "2026-11-09"),
}


def test_header_states_as_of_window_clock_services_and_tenant():
    line = header_line([HISTORY], question_timezone="UTC", log_timezone="UTC")
    assert line == "As of 2026-11-09T10:30:00+00:00; window 2026-11-07 to 2026-11-09; clock UTC; services read: svc-a, svc-b; tenant: tenant-1."


def test_header_takes_the_latest_data_time_and_every_distinct_window():
    line = header_line([HISTORY, LIVENESS], question_timezone="UTC", log_timezone="UTC")
    assert line.startswith("As of 2026-11-09T10:45:00+00:00; windows 2026-11-07 to 2026-11-09 and 2026-11-03 to 2026-11-09;")
    assert "services read: svc-a, svc-b;" in line  # a union in order, no duplicates


def test_header_closed_days_window_text_and_single_day():
    closed = {**HISTORY, "operations_coverage": coverage("2026-11-08", "2026-11-08")}
    assert header_line([closed], question_timezone="UTC", log_timezone="UTC").startswith("As of 2026-11-08 (closed days only); window 2026-11-08;")
    bare = {**HISTORY, "operations_coverage": None}
    line = header_line([bare], question_timezone="UTC", log_timezone="UTC")
    assert line.startswith("Window last 3 days; clock UTC;")  # no data time reported: none is invented


def test_header_states_both_clocks_when_they_differ():
    line = header_line([HISTORY], question_timezone="Europe/Berlin", log_timezone="UTC")
    assert "question clock Europe/Berlin, log times in UTC" in line


def test_header_states_an_unfiltered_read_and_omits_an_unbound_tenant():
    unfiltered = {**HISTORY, "args": {"window": "last 3 days"}}
    line = header_line([unfiltered], question_timezone="UTC", log_timezone="UTC")
    assert line.endswith("services read: all (no service filter).") and "tenant" not in line


def test_no_header_when_nothing_was_read():
    refused = {**HISTORY, "status": "refused"}
    assert header_line([refused], question_timezone="UTC", log_timezone="UTC") is None
    assert header_line([], question_timezone="UTC", log_timezone="UTC") is None


def test_the_header_is_the_section_s_first_line_for_the_reader_and_the_generator():
    rendering = "1 issue groups in last 3 days: 2 observed episodes."
    agg = AggregateEvidence(id="aggregate:2", result=ScopeResult(exactness="bounded", denominator=0), rendering=rendering, rows=[], columns=[])
    head = header_line([HISTORY], question_timezone="UTC", log_timezone="UTC")
    section = OperationsSection(aggregates=[agg], notes=[], label="Current logs:", knowledge_label="Past tickets/docs:", header=head)
    for text in (section.text(), section.text(cited=False)):
        assert text.startswith(f"**Current logs:**\n\n{head}\n\n")
    assert "[aggregate:" not in section.text(cited=False)


def test_the_header_wording_is_domain_neutral():
    line = header_line([HISTORY, LIVENESS], question_timezone="UTC", log_timezone="UTC").lower()
    for word in ("crawl", "ticket", "customer", "outage", "halt"):
        assert word not in line, word


# --- engine fixtures ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pinned_clock():
    mp = pytest.MonkeyPatch()
    mp.setattr(Engine, "_clock", staticmethod(lambda zone: th.NOW.astimezone(zone)))
    yield
    mp.undo()


@pytest.fixture(scope="module")
def unified(tmp_path_factory, pinned_clock):
    tmp = tmp_path_factory.mktemp("r6_polish")
    return th.build(tmp, th.unified_config(tmp))


def ask(engine, question: str):
    return engine.answer(SearchRequest(query=question, mode="rules"))


def test_engine_sections_open_with_the_reading_line(unified, monkeypatch):
    monkeypatch.setattr(JsonModel, "call", th.citing_generator([]))
    zone = unified.config.question.timezone
    halted = ask(unified, HALTED).answer.answer
    head = halted.split("\n\n")[1]
    assert halted.startswith("**Current logs:**\n\nAs of 2026-11-09T10:30:00+00:00;")
    assert "windows 2026-11-03 to 2026-11-09 and 2026-11-07 to 2026-11-09" in head  # aggregate order
    assert f"question clock {zone}, log times in UTC" in head and head.endswith("services read: crawler.")
    tenant = ask(unified, UNAUTHORIZED).answer.answer.split("\n\n")[1]
    assert tenant.endswith("services read: mcp-gateway; tenant: brightmoor.")


def test_a_refused_operations_half_has_no_reading_line(unified, monkeypatch):
    monkeypatch.setattr(JsonModel, "call", th.citing_generator([]))
    text = ask(unified, REFUSED_CUSTOMER).answer.answer
    assert text.startswith("**Current logs:**\n\n- ") and "As of" not in text.split("**Past")[0]


# --- 2. N15: the generator's one schema repair -------------------------------------------------------


def violation() -> SchemaViolation:
    return SchemaViolation("reply does not match Answer", raw='{"answer": 3}', stage="generator", error_count=1, paths=["answer:string_type"])


def scripted(replies: list[Any], seen: list[dict[str, Any]]):
    """A generator that meters each call like ``MeteredCall`` (reserve, then record) and returns or
    raises the next scripted reply."""

    def respond(self, system, payload, schema, budget, stage):
        assert stage == "generator", stage
        budget.reserve()
        budget.record({"stage": stage, "provider": "openai_compatible", "model": "fake-generator", "success": True, "usage": {}})
        seen.append({"system": system, "payload": payload})
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if reply == "cite":
            records = [e["id"] for e in payload["evidence"] if e.get("source") != "structured"][:1]
            return Answer(answer=f"An earlier record describes the fix [{records[0]}].", citations=records)
        return reply

    return respond


@pytest.fixture
def repair_on(unified, monkeypatch):
    pipe = unified.pipelines[unified.config.default_pipeline]
    monkeypatch.setattr(pipe.config.generation, "schema_repair_attempts", 1)
    return unified


def generator_calls(result) -> list[dict[str, Any]]:
    return [c for c in result.trace["model_calls"] if c.get("stage") == "generator"]


def test_default_is_one_call_and_today_s_schema_violation_answer(unified, monkeypatch):
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(JsonModel, "call", scripted([violation(), "cite"], seen))
    result = ask(unified, UNAUTHORIZED)
    assert len(seen) == 1 and len(generator_calls(result)) == 1
    assert "No answer was generated (the reply did not match the answer schema: schema_violation)" in result.answer.answer
    assert result.answer.answer.startswith("**Current logs:**")  # the rendered half stands


def test_one_repair_recovers_the_knowledge_half(repair_on, monkeypatch):
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(JsonModel, "call", scripted([violation(), "cite"], seen))
    result = ask(repair_on, UNAUTHORIZED)
    assert len(seen) == 2
    first, repair = seen
    assert "schema_repair" not in first["payload"]
    assert repair["payload"]["schema_repair"] == {"previous_reply": '{"answer": 3}', "validation_paths": ["answer:string_type"]}
    assert repair["system"] == first["system"] + GENERATOR_REPAIR_PROMPT
    assert {k: v for k, v in repair["payload"].items() if k != "schema_repair"} == first["payload"]
    text = result.answer.answer
    assert "No answer was generated" not in text and "An earlier record describes the fix" in text.split("**Past tickets/docs:**")[1]
    calls = generator_calls(result)
    assert len(calls) == 2 and "attempt" not in calls[0]
    assert calls[1]["attempt"] == 2 and calls[1]["retry_reason"] == "schema_violation"


def test_a_second_violation_keeps_today_s_answer(repair_on, monkeypatch):
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(JsonModel, "call", scripted([violation(), violation()], seen))
    result = ask(repair_on, UNAUTHORIZED)
    assert len(seen) == 2 and len(generator_calls(result)) == 2
    assert "No answer was generated (the reply did not match the answer schema: schema_violation)" in result.answer.answer


def test_a_provider_failure_of_the_repair_keeps_today_s_answer(repair_on, monkeypatch):
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(JsonModel, "call", scripted([violation(), ProviderServerError("provider HTTP 503", stage="generator", status=503)], seen))
    result = ask(repair_on, UNAUTHORIZED)
    assert len(seen) == 2
    assert "No answer was generated" in result.answer.answer


def test_the_repair_counts_against_max_llm_calls(repair_on, monkeypatch):
    """With a one-call cap the repair cannot be reserved: no second call, today's answer."""
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(JsonModel, "call", scripted([violation(), "cite"], seen))
    monkeypatch.setattr(repair_on.config.budgets, "max_llm_calls", 1)
    result = ask(repair_on, UNAUTHORIZED)
    assert len(seen) == 1 and len(generator_calls(result)) == 1
    assert "No answer was generated" in result.answer.answer


def test_the_repair_is_traced(repair_on, monkeypatch):
    seen: list[dict[str, Any]] = []
    events: list[tuple[str, dict[str, Any]]] = []
    from su_multi_arm.runtime import trace as trace_module

    original = trace_module.Trace.event

    def record(self, name, **fields):
        events.append((name, fields))
        return original(self, name, **fields)

    monkeypatch.setattr(trace_module.Trace, "event", record)
    monkeypatch.setattr(JsonModel, "call", scripted([violation(), violation()], seen))
    ask(repair_on, UNAUTHORIZED)
    repairs = [f for name, f in events if name == "generator_schema_repair"]
    assert [r["status"] for r in repairs] == ["started", "failed"]
    assert repairs[1]["reason"] == "schema_violation" and repairs[1]["paths"] == ["answer:string_type"]
    assert any(name == "generator_schema_violation" for name, _ in events)


# --- config and the worst-case plan check ------------------------------------------------------------

PRICES = {"version": "synthetic-1", "prices": {"m-small": {"input_per_million": 0.1, "output_per_million": 0.5}}}
BASE = {
    "schema_version": 2,
    "dataset": "demo",
    "backends": {"local": {"kind": "sqlite", "path": "./state"}},
    "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
    "pipeline": {"arms": {"dense": {"backend": "local"}, "lexical": {"backend": "local"}}},
    "models": {"roles": {"generator": {"provider": "anthropic", "model": "m-small", "api_key_env": "SU_TEST_KEY", "max_tokens": 3000}}},
}


def test_config_default_and_bounds():
    assert validate_config(copy.deepcopy(BASE)).pipeline.generation.schema_repair_attempts == 0
    data = copy.deepcopy(BASE)
    data["pipeline"]["generation"] = {"schema_repair_attempts": 2}
    with pytest.raises(Exception, match="schema_repair_attempts"):
        validate_config(data)


def test_plan_check_counts_the_repair_slot(tmp_path):
    prices = tmp_path / "prices.yaml"
    prices.write_text(yaml.safe_dump(PRICES))
    data = copy.deepcopy(BASE)
    data["budgets"] = {"price_table": str(prices), "max_usd": 1.0, "max_llm_calls": 5}
    table = load_price_table(prices)
    off = slots_for(validate_config(data), table)[0]
    data["pipeline"]["generation"] = {"schema_repair_attempts": 1}
    on = slots_for(validate_config(data), table)[0]
    assert [s.stage for s in off] == ["generator"] and [s.stage for s in on] == ["generator", "generator"]
    first, repair = on
    # the repair resends the evidence plus the previous reply (at most max_tokens)
    assert repair.input_tokens >= first.input_tokens + 3000 and repair.output_tokens == 3000
