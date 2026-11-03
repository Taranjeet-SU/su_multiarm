"""Phase 1 harness through the engine (plan §3.3, §3.4, §3.8): named pipelines with per-request
``pipeline``/``mode`` overrides, sessions across turns (incl. a generation switch), memory never
cited, read-only tools and allowlisted, audited actions, and the ``exact_id`` arm (pins, unknown
identifiers reported, no fuzzy matching). Synthetic fixtures, in-memory backend, fake models."""

from __future__ import annotations

import pytest

from su_multi_arm import Document, Engine, SearchRequest
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.errors import AccessDenied, BudgetExhausted, SessionNotFound
from su_multi_arm.core.types import FieldSpec, Measure, ScopeSpec
from su_multi_arm.decision import LlmPlan
from su_multi_arm.models import Answer, Claim
from su_multi_arm.pipeline.spec import RequestRejected
from su_multi_arm.providers import JsonModel
from su_multi_arm.runtime.acl import Principal, StaticPolicy

from conftest import v2_schema_model  # tests/conftest.py (rootdir conftest, on sys.path)

FAKE = {"provider": "openai_compatible", "model": "fake"}
ID_PATTERN = r"\bREC-\d{4}\b"
ROWS = [
    ("r1", "Alpha", "open", "REC-1001", "Alpha billing record about invoices and rounding."),
    ("r2", "Alpha", "closed", "REC-1002", "Alpha record closed after the billing dispute."),
    ("r3", "Beta", "open", "REC-1003", "Beta open record on table layouts and rows."),
    ("r4", "Beta", "pending", "REC-1004", "Beta pending record about region reports and billing."),
]


def docs(rows=ROWS):
    return [
        Document(
            id=rid,
            dataset="demo",
            kind="record",
            title=f"Record {rid}",
            fields={"summary": text},
            metadata={"kind": "record", "project": project, "status": status, "external_id": ext},
        )
        for rid, project, status, ext, text in rows
    ]


def schema():
    base = v2_schema_model()
    fields = [f if f.name != "external_id" else FieldSpec(name="external_id", type="id", listable=False, max_catalog_cardinality=0, pattern=ID_PATTERN) for f in base.fields]
    return base.model_copy(update={"fields": fields}).model_dump(mode="json")


def config_data(**overrides):
    data = {
        "schema_version": 2,
        "dataset": "demo",
        "backends": {"local": {"kind": "memory"}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "metadata_schema": schema(),
        "pipeline": {"arms": {"dense": {"backend": "local"}, "lexical": {"backend": "local"}, "structured": {"backend": "local"}}},
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 64}, "generator": FAKE}},
    }
    data.update(overrides)
    return data


def build(data=None, documents=None, acl=None, **overrides):
    engine = Engine(validate_config(data or config_data(**overrides)), acl=acl)
    engine.ingest(documents or docs())
    return engine


def citing(first_only=True, cite=None, seen=None):
    """Fake generator: books one model call on the request budget (as a provider would) and cites
    the first evidence id, or ``cite`` when given."""

    def respond(self, system, payload, schema, budget, stage):
        budget.reserve()
        budget.record({"stage": stage, "success": True, "usage": {}})
        if seen is not None:
            seen.append(payload)
        if stage == "planner":
            return LlmPlan.model_validate({"steps": [{"arm": "lexical", "query": "billing"}]})
        target = cite or payload["evidence"][0]["id"]
        return Answer(answer="Grounded answer.", claims=[Claim(text="a fact", evidence_ids=[target])], citations=[target])

    return respond


# --- named pipelines, per-request pipeline and mode ------------------------------------------------


def named_pipelines(**request):
    return config_data(
        pipelines={"lexical_only": {"arms": {"dense": None, "structured": None}}, "planned": {"decision": {"kind": "llm"}}},
        request={"pipelines": ["lexical_only", "planned"], **request},
        models={"roles": {"embedder": {"provider": "hash", "dimensions": 64}, "generator": FAKE, "planner": FAKE}},
    )


def test_request_selects_an_allowed_pipeline_and_the_default_runs_otherwise(monkeypatch):
    monkeypatch.setattr(JsonModel, "call", citing())
    engine = build(named_pipelines())
    assert sorted(engine.pipelines) == ["default", "lexical_only", "planned"]
    default = engine.search("billing dispute")
    assert default.trace["pipeline"] == "default" and set(default.arm_hits) == {"dense", "lexical"}
    narrow = engine.search(SearchRequest(query="billing dispute", pipeline="lexical_only"))
    assert narrow.trace["pipeline"] == "lexical_only" and set(narrow.arm_hits) == {"lexical"}
    planned = engine.answer(SearchRequest(query="billing dispute", pipeline="planned"))
    assert planned.trace["pipeline"] == "planned" and planned.trace["decision"]["kind"] == "llm"
    assert sorted(c["stage"] for c in planned.trace["model_calls"]) == ["generator", "planner"]


def test_unlisted_pipeline_and_mode_are_rejected_not_ignored(monkeypatch):
    monkeypatch.setattr(JsonModel, "call", citing())
    engine = build(named_pipelines())
    with pytest.raises(RequestRejected, match="not selectable"):
        engine.search(SearchRequest(query="billing", pipeline="nope"))
    with pytest.raises(RequestRejected, match="mode 'llm' is not allowed"):
        engine.search(SearchRequest(query="billing", mode="llm"))
    data = named_pipelines()
    data["request"]["pipelines"] = []
    locked = build(data)
    with pytest.raises(RequestRejected):
        locked.search(SearchRequest(query="billing", pipeline="lexical_only"))
    assert locked.search(SearchRequest(query="billing", mode="rules")).trace["mode"] == "rules"  # the configured kind is always allowed


def test_mode_override_switches_the_decision_agent_per_request(monkeypatch):
    seen = []
    monkeypatch.setattr(JsonModel, "call", citing(seen=seen))
    engine = build(named_pipelines(modes=["llm"]))
    rules = engine.search("billing")
    assert rules.trace["decision"]["kind"] == "rules" and seen == []
    llm = engine.search(SearchRequest(query="billing", mode="llm"))
    assert llm.trace["decision"]["kind"] == "llm" and llm.trace["mode"] == "llm" and len(seen) == 1
    assert [s["arm"] for s in llm.trace["steps"]] == ["lexical"]


def test_default_pipeline_can_be_a_named_one(monkeypatch):
    monkeypatch.setattr(JsonModel, "call", citing())
    data = named_pipelines()
    data["default_pipeline"] = "lexical_only"
    engine = build(data)
    assert engine.search("billing").trace["pipeline"] == "lexical_only"
    assert sorted(engine.arms) == ["lexical"]


# --- sessions ----------------------------------------------------------------------------------------


def test_session_continuity_across_turns_records_hashes_and_ids(monkeypatch):
    monkeypatch.setattr(JsonModel, "call", citing())
    engine = build()
    session_id = engine.create_session()
    first = engine.answer("billing dispute", session_id=session_id)
    second = engine.answer("rounding invoices", session_id=session_id)
    assert first.trace["session"]["turn"] == 1 and second.trace["session"]["turn"] == 2
    assert second.trace["session"]["generation_changed"] is None
    record = engine.session(session_id).record
    assert len(record.turns) == 2 and record.generation == first.retrieval.generation
    assert record.turns[0].shown_ids == [h.document_id for h in first.retrieval.hits]
    assert record.turns[0].query_text is None and len(record.turns[0].query_sha) == 64  # hashes, not text
    assert record.turns[1].answer_id == second.trace["trace_id"]
    search = engine.search("billing", session_id=session_id)
    assert search.trace["session"]["turn"] == 3 and len(engine.session(session_id).record.turns) == 3


def test_session_is_bound_to_its_principal_and_unknown_sessions_fail(monkeypatch):
    monkeypatch.setattr(JsonModel, "call", citing())
    engine = build()
    alice, bob = Principal(id="alice"), Principal(id="bob")
    session_id = engine.create_session(alice)
    assert engine.answer("billing", principal=alice, session_id=session_id).trace["session"]["id"] == session_id
    with pytest.raises(AccessDenied):
        engine.answer("billing", principal=bob, session_id=session_id)
    with pytest.raises(SessionNotFound):
        engine.answer("billing", session_id="does-not-exist")
    engine.close_session(session_id, alice)
    with pytest.raises(SessionNotFound):
        engine.search("billing", principal=alice, session_id=session_id)


def test_generation_switch_mid_session_is_pinned_per_answer_and_reported(monkeypatch):
    monkeypatch.setattr(JsonModel, "call", citing())
    engine = build()
    session_id = engine.create_session()
    first = engine.answer("Alpha billing invoices rounding", session_id=session_id)
    shown = [h.document_id for h in first.retrieval.hits]
    assert "r1" in shown
    engine.ingest(docs(ROWS[1:]), replace=True)  # r1 is gone in the new generation
    second = engine.answer("billing dispute", session_id=session_id)
    old, new = first.retrieval.generation, second.retrieval.generation
    assert old != new
    session = second.trace["session"]
    assert session["generation_changed"] == {"from": old, "to": new} and session["generation"] == new
    assert "r1" in session["dropped_ids"]
    record = engine.session(session_id).record
    assert [t.generation for t in record.turns] == [old, new] and record.generation == new
    assert "r1" not in record.turns[0].shown_ids  # re-validated against the new generation


def test_memory_is_never_evidence_a_prior_turn_id_cannot_be_cited(monkeypatch):
    seen = []
    monkeypatch.setattr(JsonModel, "call", citing(seen=seen))
    lexical = {"arms": {"lexical": {"backend": "local"}}}
    engine = build(sessions={"memory": {"exclude_shown": True}}, pipeline=lexical)
    session_id = engine.create_session()
    first = engine.answer("dispute", session_id=session_id)
    remembered = first.retrieval.hits[0].document_id
    assert [h.document_id for h in first.retrieval.hits] == ["r2"]
    monkeypatch.setattr(JsonModel, "call", citing(cite=remembered, seen=seen))
    second = engine.answer("billing", session_id=session_id)
    assert remembered in second.trace["session"]["exclusions_added"]
    assert remembered not in {h.document_id for h in second.retrieval.hits} and second.retrieval.hits
    assert set(seen[-1]) == {"question", "record_context", "evidence"}  # no memory in the generator payload
    assert remembered not in {e["id"] for e in seen[-1]["evidence"]}
    assert second.contract == "abstained" and [v.rule for v in second.violations] == ["unknown_evidence"]
    assert second.answer.citations == []


def test_session_budget_spans_turns_and_refuses_when_exhausted(monkeypatch):
    monkeypatch.setattr(JsonModel, "call", citing())
    engine = build(budgets={"max_llm_calls": 5, "session": {"max_llm_calls": 2}})
    session_id = engine.create_session()
    engine.answer("billing", session_id=session_id)
    second = engine.answer("billing dispute", session_id=session_id)
    assert second.trace["session"]["usage"]["calls"] == 2
    with pytest.raises(BudgetExhausted, match="session_calls"):
        engine.answer("rounding", session_id=session_id)
    assert engine.session(session_id).record.usage["calls"] == 2
    assert len(engine.session(session_id).record.turns) == 2
    assert engine.answer("rounding").answer.citations  # requests outside the session keep the request budget


# --- tools and actions ---------------------------------------------------------------------------


def test_builtin_tools_run_through_the_engine_with_acl_and_trace(monkeypatch):
    monkeypatch.setattr(JsonModel, "call", citing())
    engine = build(acl=StaticPolicy(field="project"))
    alpha = Principal(id="u1", tenant="Alpha")
    assert engine.tools.names() == ["aggregate", "get_document", "list_values", "search"]
    found = engine.call_tool("search", {"query": "billing"}, principal=alpha)
    assert found.result.evidence_ids and set(found.result.evidence_ids) <= {"r1", "r2"}
    assert any(span["name"] == "tool.search" for span in found.trace["spans"])
    fetched = engine.call_tool("get_document", {"ids": ["r1", "r3", "zz"]}, principal=alpha).result
    assert [d.id for d in fetched.output.documents] == ["r1"] and fetched.output.missing == ["r3", "zz"]
    values = engine.call_tool("list_values", {"field": "status", "limit": 1}, principal=alpha).result.output
    assert values.values == ["closed"] and values.truncated is True
    counted = engine.call_tool("aggregate", {"scope": ScopeSpec(measure=Measure(kind="count")).model_dump(mode="json")}, principal=alpha).result
    assert counted.output.result.value == 2 and counted.output.result.exactness == "exact"
    with pytest.raises(RequestRejected, match="not listable"):
        engine.call_tool("list_values", {"field": "external_id"}, principal=alpha)
    with pytest.raises(RequestRejected, match="not declared"):
        engine.call_tool("aggregate", {"scope": {"group_by": ["secret"]}}, principal=alpha)


def test_tools_are_limited_to_the_enabled_registered_set():
    engine = build(tools={"enabled": ["get_document"]})
    assert engine.tools.names() == ["get_document"]
    with pytest.raises(RequestRejected, match="not enabled"):
        engine.call_tool("search", {"query": "billing"})


def test_actions_need_the_allowlist_and_every_call_is_audited():
    engine = build(actions={"allow": ["invalidate_cache", "ingest_delta"]}, serving={"actions": {"allow": ["invalidate_cache"]}})
    denied_engine = build()
    denied = denied_engine.run_action("invalidate_cache", idempotency_key="k0")
    assert denied.result.status == "denied" and "unknown action" in denied.result.error
    assert [r.status for r in denied_engine.audit.records()] == ["denied"]

    before = engine.manifest()["generation"]
    added = Document(id="r9", dataset="demo", kind="record", title="Record r9", fields={"summary": "New billing note"}, metadata={"kind": "record", "project": "Alpha", "status": "open", "external_id": "REC-1009"})
    applied = engine.run_action("ingest_delta", {"documents": [added.model_dump(mode="json")]}, idempotency_key="delta-1", reason="new record")
    assert applied.result.status == "applied" and applied.result.outputs["generation"] != before
    assert any(span["name"] == "action.ingest_delta" for span in applied.trace["spans"])
    history = [(r.action, r.status) for r in engine.audit.history()]
    assert history == [("ingest_delta", "started"), ("ingest_delta", "applied")]  # written before, finalised after
    record = engine.audit.records()[0]
    assert record.generation_before == before and record.generation_after == applied.result.outputs["generation"]
    duplicate = engine.run_action("ingest_delta", {"documents": [added.model_dump(mode="json")]}, idempotency_key="delta-1")
    assert duplicate.result.status == "skipped_duplicate" and duplicate.result.audit_id == record.id
    failed = engine.run_action("ingest_delta", {}, idempotency_key="delta-2")
    assert failed.result.status == "failed" and "needs documents" in failed.result.error

    remote = engine.run_action("ingest_delta", {"delete_ids": ["r9"]}, idempotency_key="delta-3", surface="http", principal=Principal(id="ops", scopes=["actions"]))
    assert remote.result.status == "denied" and "not allowed on the http surface" in remote.result.error
    no_scope = engine.run_action("invalidate_cache", idempotency_key="c1", surface="mcp", principal=Principal(id="ops"))
    assert no_scope.result.status == "denied" and "actions" in no_scope.result.error
    remote_ok = engine.run_action("invalidate_cache", idempotency_key="c2", surface="mcp", principal=Principal(id="ops", scopes=["actions"]))
    assert remote_ok.result.status == "applied" and remote_ok.result.outputs["invalidated"] == 0
    statuses = [r.status for r in engine.audit.records()]
    assert statuses.count("denied") == 2 and statuses.count("applied") == 2


# --- exact_id ------------------------------------------------------------------------------------


def exact_config(tmp_path, casefold=False, **arms):
    aliases = tmp_path / "aliases.yaml"
    aliases.write_text('aliases:\n  "Project Phoenix": r2\n  PHX: r2\n')
    exact = {"alias_map": str(aliases), "alias_fields": ["external_id"], "casefold": casefold, **arms}
    return config_data(
        pipeline={"arms": {"exact_id": exact, "dense": {"backend": "local"}, "lexical": {"backend": "local"}, "structured": {"backend": "local"}}}
    )


def test_exact_ids_are_routed_by_the_pre_pass_and_pinned_first(monkeypatch, tmp_path):
    monkeypatch.setattr(JsonModel, "call", citing())
    engine = build(exact_config(tmp_path))
    capabilities = engine.capabilities()
    assert ID_PATTERN in capabilities.id_patterns and len(capabilities.id_patterns) == 2 and "exact_id" in capabilities.arms
    result = engine.search("billing rows for REC-1003")
    assert [s["arm"] for s in result.trace["steps"]][0] == "exact_id" and result.trace["steps"][0]["query"] == "REC-1003"
    assert result.hits[0].document_id == "r3" and "exact_id" in result.hits[0].arms
    assert "pin:exact_id" in result.trace["reasons"]["r3"] and result.trace["selection"]["pinned"] == 1
    assert result.trace["exact_id"] == {"matched": {"REC-1003": "r3"}, "unknown": []}
    aliased = engine.search("status of Project Phoenix")
    assert aliased.hits[0].document_id == "r2" and aliased.arm_hits["exact_id"][0].provenance[0]["match"] == "alias_map"
    answered = engine.answer("what about PHX and REC-1004")
    assert {h.document_id for h in answered.retrieval.hits[:2]} == {"r2", "r4"} and answered.unsupported_constraints == []


def test_unknown_ids_are_reported_and_never_fuzzily_matched(monkeypatch, tmp_path):
    monkeypatch.setattr(JsonModel, "call", citing())
    engine = build(exact_config(tmp_path))
    result = engine.answer("REC-9999 and REC-1001")
    assert result.trace["exact_id"] == {"matched": {"REC-1001": "r1"}, "unknown": ["REC-9999"]}
    unknown = [u for u in result.unsupported_constraints if u.constraint_text == "REC-9999"]
    assert len(unknown) == 1 and unknown[0].reason == "value_not_observed" and "no fuzzy match" in unknown[0].suggestion
    assert result.retrieval.arm_hits["exact_id"] == [h for h in result.retrieval.arm_hits["exact_id"] if h.document_id == "r1"]
    near = engine.search("status of Project Phoenyx and phx")  # misspelt alias, wrong case: no identifier, no guess
    assert "exact_id" not in near.arm_hits and "exact_id" not in near.trace
    folded = build(exact_config(tmp_path, casefold=True)).search("status of phx")
    assert folded.hits[0].document_id == "r2"


def test_exact_id_never_widens_the_access_scope(monkeypatch, tmp_path):
    monkeypatch.setattr(JsonModel, "call", citing())
    engine = build(exact_config(tmp_path), acl=StaticPolicy(field="project"))
    beta = Principal(id="u2", tenant="Beta")
    result = engine.search("REC-1003 and PHX", principal=beta)  # PHX -> r2 belongs to Alpha
    assert result.trace["exact_id"] == {"matched": {"REC-1003": "r3"}, "unknown": ["PHX"]}
    assert "r2" not in {h.document_id for h in result.hits}
    excluded = engine.search(SearchRequest(query="REC-1003", exclude_ids=["r3"]), principal=beta)
    assert excluded.trace["exact_id"]["unknown"] == ["REC-1003"] and "r3" not in {h.document_id for h in excluded.hits}


def test_pins_off_keeps_exact_hits_in_the_union_without_moving_them(monkeypatch, tmp_path):
    monkeypatch.setattr(JsonModel, "call", citing())
    data = exact_config(tmp_path)
    data["pipeline"]["selection"] = {"pins": []}
    result = build(data).search("billing invoices rounding REC-1004")
    assert "r4" in {h.document_id for h in result.hits} and result.trace["selection"]["pinned"] == 0
    assert "pin:exact_id" not in result.trace["reasons"]["r4"]
