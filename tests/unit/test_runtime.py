"""Runtime (plan §3.4): budget reservation/exhaustion incl. USD via price table, deadline, trace
masking, principal/session ownership, pinned generation, memory revalidation, audited actions."""

from __future__ import annotations

import json

import pytest

from su_multi_arm.backends.memory import MemoryBackend
from su_multi_arm.core.errors import AccessDenied, BudgetExhausted, ConfigError, DeadlineExceeded, SessionNotFound
from su_multi_arm.core.types import GenerationId, UnsupportedConstraint
from su_multi_arm.runtime.acl import ANONYMOUS, NoConstraints, Principal, StaticPolicy, acl_key
from su_multi_arm.runtime.actions import ActionRequest, ActionRunner, MemoryAuditLog
from su_multi_arm.runtime.budget import Budget, BudgetLimits, ModelPrice, PriceTable, load_price_table
from su_multi_arm.runtime.context import RunContext, new_context
from su_multi_arm.runtime.memory import EpisodicMemory, Memory
from su_multi_arm.runtime.session import SessionManager
from su_multi_arm.runtime.tools import ToolRegistry, ToolResult, invoke_tool
from su_multi_arm.runtime.trace import Sensitive, Trace, mask

GEN = GenerationId(dataset="demo", generation="a" * 32, index_format=3, fingerprint="f" * 64)
GEN2 = GEN.model_copy(update={"generation": "b" * 32})
PRICES = PriceTable(version="2026-01", prices={"m1": ModelPrice(input_per_million=1.0, output_per_million=10.0, cached_input_per_million=0.1)})


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


def test_budget_calls_tokens_exhaustion_and_commit():
    budget = Budget(BudgetLimits(max_llm_calls=2, max_input_tokens=100))
    first = budget.reserve("calls", 1, stage="planner")
    budget.reserve("calls", 1)
    with pytest.raises(BudgetExhausted, match="calls"):
        budget.reserve("calls", 1)
    assert budget.usage.exhausted[0]["kind"] == "calls" and budget.remaining("calls") == 0
    tokens = budget.reserve("input_tokens", 80)
    with pytest.raises(BudgetExhausted):
        budget.reserve("input_tokens", 30)
    budget.commit(tokens, 40)  # actual lower than estimate frees budget
    assert budget.remaining("input_tokens") == 60 and budget.usage.used["input_tokens"] == 40
    budget.commit(first, 1)
    with pytest.raises(ValueError, match="already committed"):
        budget.commit(first, 1)
    with pytest.raises(ValueError, match="unknown budget kind"):
        budget.reserve("gpu", 1)
    assert budget.remaining("usd") is None  # unlimited kinds report None


def test_usd_budget_requires_price_table_and_pricing_key(tmp_path):
    with pytest.raises(ConfigError, match="price table"):
        Budget(BudgetLimits(max_usd=1.0))
    budget = Budget(BudgetLimits(max_usd=0.0005, max_llm_calls=10), PRICES)
    reservations = budget.reserve_call(stage="generator", pricing_key="m1", input_tokens=100, output_tokens=10)
    assert [r.kind for r in reservations] == ["calls", "input_tokens", "output_tokens", "usd"]
    assert reservations[-1].estimate == pytest.approx((100 * 1.0 + 10 * 10.0) / 1e6)
    with pytest.raises(BudgetExhausted, match="usd"):
        budget.reserve_call(stage="generator", pricing_key="m1", input_tokens=100, output_tokens=100)
    assert budget.usage.reserved["calls"] == 1  # the failed call released its other reservations
    with pytest.raises(ConfigError, match="pricing_key"):
        budget.reserve_call(stage="x", pricing_key=None)
    with pytest.raises(ConfigError, match="no price for"):
        budget.reserve_call(stage="x", pricing_key="unknown-model", input_tokens=1)
    assert PRICES.cost("m1", 1_000_000, 0, cached_tokens=1_000_000) == pytest.approx(0.1)
    (tmp_path / "prices.yaml").write_text("version: v1\nprices:\n  m1: {input_per_million: 2, output_per_million: 4}\n")
    assert load_price_table(tmp_path / "prices.yaml").prices["m1"].output_per_million == 4
    with pytest.raises(ConfigError, match="USD reservations require"):
        Budget(BudgetLimits()).reserve("usd", 0.1)


def test_deadline_is_enforced_and_recorded():
    clock = Clock()
    budget = Budget(BudgetLimits(deadline_seconds=10), clock=clock)
    budget.check_deadline()
    assert budget.remaining("wall_clock") == 10
    clock.now = 10
    with pytest.raises(DeadlineExceeded):
        budget.reserve_call(stage="planner")
    assert budget.usage.exhausted[-1]["kind"] == "wall_clock"
    ctx_clock = Clock()
    ctx = RunContext(generation=GEN, budget=Budget(BudgetLimits(deadline_seconds=5), clock=ctx_clock))
    ctx.check_deadline()
    ctx_clock.now = 6
    with pytest.raises(DeadlineExceeded):
        ctx.check_deadline()
    assert ctx.remaining_seconds() == 0 and [e["name"] for e in ctx.trace.root.events] == ["deadline_exceeded"]


def test_trace_masks_query_passages_and_unsupported_constraint_text(tmp_path):
    trace = Trace()
    with trace.span("decision", query="how many cases in Texas", arms=["dense"]) as span:
        span.set(unsupported=[UnsupportedConstraint(constraint_text="in Texas", reason="field_not_filterable", suggestion="declare state")])
        trace.event("review", followup_query="Texas cases 2024", hops=1)
        with trace.span("arm.dense", hits=3, passages=["secret text"], note=Sensitive("hidden"), highlights=["frag"], title="Doc title"):
            pass
    records = trace.records()
    decision = next(r for r in records if r["name"] == "decision")
    assert decision["attributes"]["query"]["masked"] and "Texas" not in json.dumps(decision)
    assert decision["attributes"]["arms"] == ["dense"]
    constraint = decision["attributes"]["unsupported"][0]
    assert constraint["constraint_text"]["masked"] and constraint["suggestion"]["masked"] and constraint["reason"] == "field_not_filterable"
    assert decision["events"][0]["followup_query"]["masked"] and decision["events"][0]["hops"] == 1
    dense = next(r for r in records if r["name"] == "arm.dense")
    assert dense["attributes"]["passages"]["masked"] and dense["attributes"]["note"]["masked"] and dense["attributes"]["hits"] == 3
    assert dense["attributes"]["highlights"]["masked"] and dense["attributes"]["title"]["masked"]
    assert dense["parent_id"] == decision["span_id"] and decision["parent_id"] == trace.root.id
    raw = trace.records(masking="raw")
    assert next(r for r in raw if r["name"] == "decision")["attributes"]["query"] == "how many cases in Texas"
    assert next(r for r in raw if r["name"] == "arm.dense")["attributes"]["note"] == "hidden"
    out = tmp_path / "trace.jsonl"
    assert trace.export(out) == 3 and "secret text" not in out.read_text() and len(out.read_text().splitlines()) == 3
    assert mask({"query": None}, "privacy") == {"query": None}


def test_trace_marks_errors_on_spans():
    trace = Trace()
    with pytest.raises(RuntimeError):
        with trace.span("failing"):
            raise RuntimeError("boom")
    failing = next(r for r in trace.records() if r["name"] == "failing")
    assert failing["status"] == "error" and failing["events"][0]["type"] == "RuntimeError" and failing["ended"] is not None


def test_sessions_are_principal_scoped_and_pin_generation_changes():
    backend = MemoryBackend(clock=lambda: 0.0)
    manager = SessionManager(backend.session, ttl_seconds=60, clock=lambda: 0.0)
    alice, bob = Principal(id="alice", scopes=["search"]), Principal(id="bob", scopes=["search"])
    session = manager.create(alice, "demo")
    assert manager.get(alice, "demo", session.id).id == session.id
    with pytest.raises(AccessDenied):
        manager.get(bob, "demo", session.id)
    with pytest.raises(AccessDenied):
        manager.get(alice, "other", session.id)
    with pytest.raises(SessionNotFound):
        manager.get(alice, "demo", "missing")
    assert issubclass(SessionNotFound, KeyError)
    ctx = new_context(GEN, principal=alice, session=session)
    turn = ctx.memory.episodic.record("first question", ["d1", "d2"], GEN.generation, ctx.trace.id)
    assert turn.query_text is None and turn.shown_ids == ["d1", "d2"]  # hashes and IDs, not text
    manager.touch(session, GEN, turn)
    reloaded = manager.get(alice, "demo", session.id)
    assert reloaded.record.generation == GEN.generation and reloaded.memory.exclusions() == ["d1", "d2"]
    ctx2 = new_context(GEN2, principal=alice, session=reloaded)
    assert ctx2.trace.root.attributes["generation_changed"] == {"from": GEN.generation, "to": GEN2.generation}
    assert ctx2.generation == GEN2 and ctx2.memory is reloaded.memory
    dropped = ctx2.memory.episodic.revalidate(GEN2.generation, lambda ids: {"d2"})
    assert dropped == {"generation": GEN2.generation, "dropped": ["d1"]} and ctx2.memory.exclusions() == ["d2"]
    manager.close(session)
    with pytest.raises(SessionNotFound):
        manager.get(alice, "demo", session.id)
    text_manager = SessionManager(backend.session, ttl_seconds=60, store_text=True, clock=lambda: 0.0)
    assert text_manager.create(alice, "demo").memory.episodic.record("q", [], GEN.generation, "t").query_text == "q"


def test_run_context_is_immutable_and_records_principal_and_generation():
    ctx = RunContext(generation=GEN, profile="neutral", config_fingerprint="c" * 64)
    with pytest.raises(Exception):
        ctx.generation = GEN2  # type: ignore[misc]
    root = ctx.trace.root.attributes
    assert root["generation"] == GEN.generation and root["principal"] == ANONYMOUS.key() and root["config_fingerprint"] == "c" * 64
    assert ctx.constraints() == [] and isinstance(ctx.acl, NoConstraints) and ctx.deadline is None
    tenant_ctx = new_context(GEN, principal=Principal(id="u", tenant="t1"), acl=StaticPolicy("tenant"))
    assert [p.model_dump(exclude_defaults=True) for p in tenant_ctx.constraints()] == [{"field": "tenant", "op": "eq", "value": "t1"}]
    denied = StaticPolicy("tenant").constraints(Principal(id="u"))
    assert denied[0].op == "in" and denied[0].value == []  # missing tenant matches nothing
    assert acl_key(denied) != acl_key([]) and len(acl_key([])) == 16


def test_memory_is_not_evidence_and_working_memory_is_per_request():
    memory = Memory(episodic=EpisodicMemory())
    memory.working.set("rewritten", "x")
    assert memory.working.get("rewritten") == "x" and memory.working.get("absent", 0) == 0
    from su_multi_arm.core.types import Evidence

    # Memory carries hashes and IDs only; nothing in it is an Evidence instance and no Evidence kind names memory or cache.
    assert not isinstance(memory.episodic.turns, Evidence)
    assert set(Evidence.model_fields["kind"].annotation.__args__) == {"document", "aggregate", "graph_path", "community_report"}
    assert memory.episodic.revalidate(GEN.generation, lambda ids: set()) == {"generation": GEN.generation, "dropped": []}


def test_actions_are_audited_idempotent_and_surface_gated():
    clock = Clock(1000.0)
    audit = MemoryAuditLog()
    generation = {"current": "g1"}
    runner = ActionRunner(audit, allow_remote=["invalidate_cache"], clock=clock, current_generation=lambda: generation["current"])

    def promote(request):
        generation["current"] = request.args["to"]
        return {"promoted": request.args["to"]}

    def explode(request):
        raise RuntimeError("disk full")

    runner.register("promote_generation", promote)
    runner.register("invalidate_cache", lambda r: {"invalidated": 3})
    runner.register("explode", explode)
    with pytest.raises(ValueError, match="already registered"):
        runner.register("explode", explode)
    operator = Principal(id="op", scopes=["actions"])
    request = ActionRequest(action="promote_generation", args={"to": "g2"}, idempotency_key="k1", principal=operator, reason="release")
    result = runner.invoke(request, surface="cli")
    assert result.status == "applied" and result.outputs == {"promoted": "g2"}
    record = audit.records[0]
    assert (record.status, record.generation_before, record.generation_after, record.principal) == ("applied", "g1", "g2", operator.key())
    assert runner.invoke(request, surface="cli").status == "skipped_duplicate" and generation["current"] == "g2"
    assert runner.invoke(request.model_copy(update={"idempotency_key": "k2", "args": {"to": "g3"}})).status == "applied"
    assert runner.invoke(request.model_copy(update={"idempotency_key": "k3"}), surface="http").status == "denied"
    remote = ActionRequest(action="invalidate_cache", idempotency_key="k4", principal=operator)
    assert runner.invoke(remote, surface="mcp").status == "applied"
    no_scope = remote.model_copy(update={"principal": Principal(id="reader", scopes=["search"]), "idempotency_key": "k5"})
    denied = runner.invoke(no_scope, surface="http")
    assert denied.status == "denied" and "scope" in denied.error and audit.records[-1].status == "denied"
    assert runner.invoke(ActionRequest(action="unknown", idempotency_key="k6", principal=operator)).status == "denied"
    failed = runner.invoke(ActionRequest(action="explode", idempotency_key="k7", principal=operator))
    assert failed.status == "failed" and "disk full" in failed.error and audit.records[-1].status == "failed"
    assert all(r.finished is not None for r in audit.records)


def test_tool_registry_requires_models_and_invoke_runs_in_a_span():
    from typing import ClassVar

    from su_multi_arm.models import StrictModel

    class Args(StrictModel):
        query: str

    class Out(StrictModel):
        ids: list[str]

    class Lookup:
        name: ClassVar[str] = "lookup_record"
        kind: ClassVar[str] = "tool"
        config_model: ClassVar[type[StrictModel]] = StrictModel
        input_model: ClassVar[type[StrictModel]] = Args
        output_model: ClassVar[type[StrictModel]] = Out
        description: ClassVar[str] = "look up"

        def __init__(self, config=None, deps=None):
            pass

        def validate(self, env, probe=False):
            return []

        def run(self, ctx, inputs):
            return self.invoke(ctx, inputs)

        def recipe_fingerprint(self):
            return None

        def invoke(self, ctx, args):
            return ToolResult(output=Out(ids=["d1"]), evidence_ids=["d1"])

    class Broken(Lookup):
        name: ClassVar[str] = "broken"
        input_model: ClassVar[type] = dict

    registry = ToolRegistry()
    registry.register(Lookup)
    with pytest.raises(Exception, match="input_model"):
        registry.register(Broken)
    assert registry.schemas()["lookup_record"]["input"]["properties"]["query"]["type"] == "string"
    ctx = RunContext(generation=GEN)
    result = invoke_tool(ctx, Lookup(), Args(query="x"))
    assert result.evidence_ids == ["d1"] and result.trace_span
    assert next(r for r in ctx.trace.records() if r["name"] == "tool.lookup_record")["attributes"]["evidence_ids"] == 1
