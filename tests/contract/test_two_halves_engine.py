"""The two-halves planner over the unified collection (unified merge M2b-2, §6.11, §10.5).

Synthetic, neutral equivalents of the owner's seven acceptance-question shapes (the private
originals are not reproduced) are answered on one engine whose generation holds the F0 support
member and the episode archive's live window (``tests/two_halves_support.py``): each gets a
knowledge half (today's plan over tickets/docs) and an operations half (typed log intents grounded
by vocabulary, question clock, default live view and the identity registry), with per-source
coverage. Also: refusal instead of widening for a customer without a matched tenant, a disabled
registry, the labelled deterministic fallback when the planner fails after its repair, source
cards only for two-halves questions, labelled links in the legacy envelope behind the dataset
switch, and the regression rule: support questions without an operations intent keep exactly
today's planner payload, plan and evidence. Fake providers only (``JsonModel.call``); no service.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from su_multi_arm import SearchRequest
from su_multi_arm.engine import Engine
from su_multi_arm.providers import JsonModel
from su_multi_arm.providers.base import SchemaViolation
from tests import two_halves_support as th


@pytest.fixture(scope="module")
def pinned_clock():
    mp = pytest.MonkeyPatch()
    mp.setattr(Engine, "_clock", staticmethod(lambda zone: th.NOW.astimezone(zone)))
    yield
    mp.undo()


@pytest.fixture(scope="module")
def unified(tmp_path_factory, pinned_clock):
    tmp = tmp_path_factory.mktemp("two_halves")
    return th.build(tmp, th.unified_config(tmp))


@pytest.fixture(scope="module")
def unified_llm(tmp_path_factory, pinned_clock):
    tmp = tmp_path_factory.mktemp("two_halves_llm")
    return th.build(tmp, th.unified_config(tmp, llm=True))


@pytest.fixture
def calls(monkeypatch):
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(JsonModel, "call", th.citing_generator(seen))
    return seen


def ask(engine, question: str, **kw):
    return engine.answer(SearchRequest(query=question, **({"mode": "rules"} if "mode" not in kw else {}), **kw))


def intents_of(result) -> list[tuple[str, dict]]:
    return [(a["intent"], a["args"]) for a in result.retrieval.aggregates if a.get("intent")]


def halves(result) -> list[str]:
    return result.trace["source_coverage"]["plan"]["halves"]


def knowledge_evidence(result) -> int:
    sources = result.trace["source_coverage"]["sources"]
    return sources["ticket"].get("evidence", 0) + sources["doc"].get("evidence", 0)


# --- the seven question shapes (synthetic equivalents) ---------------------------------------------


def test_shape1_a_halted_scheduled_crawl_reads_heartbeats_failures_and_tickets(unified, calls):
    result = ask(unified, "Has the crawl of the help-centre articles stopped? It is supposed to run every 30 minutes.")
    assert halves(result) == ["knowledge", "operations"] and result.trace["decision"]["policy_applied"] == "two_halves"
    found = dict(intents_of(result))
    assert found["log_liveness"]["service"] == ["crawler"] and found["log_liveness"]["event"] == ["success.crawl_completed"]
    assert found["issue_history"]["window"] == "last 3 days"  # the default live view
    liveness = next(a for a in result.retrieval.aggregates if a.get("intent") == "log_liveness")
    assert "1 halted" in liveness["rendering"] and '"crawl" was read as service crawler' in liveness["rendering"]
    coverage = result.trace["source_coverage"]
    assert coverage["sources"]["logs"]["status"] == "evidence" and knowledge_evidence(result) > 0
    assert {"intent", "status", "window", "tiers"} <= set(coverage["sources"]["logs"]["intents"][0])
    assert result.answer.citations and any(c.startswith("aggregate:") for c in result.answer.citations)


def test_shape2_unauthorized_despite_correct_credentials_for_a_named_customer(unified, calls):
    result = ask(unified, "Brightmoor gets unauthorized errors connecting the MCP connector even though the credentials are correct")
    assert halves(result) == ["knowledge", "operations"]
    [(intent, args)] = intents_of(result)
    assert intent == "issue_history" and args["tenant"] == "brightmoor" and args["service"] == ["mcp-gateway"] and args["window"] == "last 3 days"
    history = result.retrieval.aggregates[-1]
    assert history["rows"] and history["rows"][0]["signature_id"] == "auth.unauthorized"
    assert '"MCP" was read as service mcp-gateway' in history["rendering"]
    identity = result.trace["source_coverage"]["identity"]
    assert identity["customers"] == [{"text": "Brightmoor", "namespace": "support.account", "customer_id": "cust:brightmoor", "tenants": ["brightmoor"]}]
    ids = [h.document_id for h in result.retrieval.hits]
    assert "ticket:2001" in ids or "https://docs.example.org/kb/mcp-authentication" in ids  # earlier fix / doc


def test_shape3_a_report_stuck_for_hours_states_that_hours_are_not_isolated(unified, calls):
    result = ask(unified, "Our usage report has been stuck in the queue for the last 3 hours")
    names = [i for i, _ in intents_of(result)]
    assert names == ["log_liveness", "issue_history"] and halves(result) == ["knowledge", "operations"]
    history = result.retrieval.aggregates[-1]
    assert history["rows"][0]["signature_id"] == "report.queue_stalled"
    assert any(u.constraint_text == "for the last 3 hours" and "not isolated" in u.suggestion for u in result.unsupported_constraints)


def test_shape4_a_search_ranking_question_states_that_analytics_are_not_served(unified, calls):
    result = ask(unified, "We searched for sitemap limits but the page is not showing on the first page")
    coverage = result.trace["source_coverage"]
    assert coverage["sources"]["search_analytics"]["status"] == "not_served"
    assert halves(result) == ["knowledge"] and knowledge_evidence(result) > 0
    assert any("Search analytics" in x for x in result.limitations)


def test_shape5_a_user_who_cannot_log_in_reads_auth_failures_and_tickets(unified, calls):
    result = ask(unified, "A user was added in the admin console but still cannot log in")
    [(intent, args)] = intents_of(result)
    assert intent == "issue_history" and args["service"] == ["auth-service"]
    assert result.retrieval.aggregates[-1]["rows"] and halves(result) == ["knowledge", "operations"]
    assert "ticket:2002" in [h.document_id for h in result.retrieval.hits]


def test_shape6_slow_search_and_an_outage_reads_the_default_live_view(unified, calls):
    result = ask(unified, "search feels slow, is there an outage?")
    [(intent, args)] = intents_of(result)
    assert intent == "issue_history" and args["service"] == ["search-api"] and args["window"] == "last 3 days"
    rendering = result.retrieval.aggregates[-1]["rendering"]
    assert '"slow" describes a symptom' in rendering or "slow" in rendering
    assert any("default live view" in n for n in result.trace["source_coverage"]["sources"]["logs"]["notes"])


def test_shape7_a_failure_at_a_bare_hour_states_both_readings_and_lists_that_days_errors(unified, calls):
    result = ask(unified, "Brightmoor said the crawl failed at 2, what was the error and did it recover on its own?")
    found = dict(intents_of(result))
    assert found["log_liveness"]["recovery"] is True and found["log_liveness"]["tenant"] == "brightmoor"
    assert found["issue_history"]["start"] == "2026-11-08" and found["issue_history"]["end"] == "2026-11-09"
    clock = [u for u in result.unsupported_constraints if u.constraint_text == "at 2"]
    assert clock and "02:00" in clock[0].suggestion and "14:00" in clock[0].suggestion and "Australia/Adelaide" in clock[0].suggestion
    history = next(a for a in result.retrieval.aggregates if a.get("intent") == "issue_history")
    assert history["rows"][0]["signature_id"] == "crawler.fetch_timeout"
    assert halves(result) == ["knowledge", "operations"]


# --- grounding refusals, identity, rollups ------------------------------------------------------


def test_a_customer_without_a_matched_tenant_refuses_the_operations_half_instead_of_widening(unified, calls):
    result = ask(unified, "Cobaltfield Systems says search is slow, is there an outage?")
    assert intents_of(result) == [] and halves(result) == ["knowledge"]
    logs = result.trace["source_coverage"]["sources"]["logs"]
    assert logs["status"] == "refused" and "Cobaltfield Systems" in logs["reason"]
    assert result.trace["source_coverage"]["identity"]["unmatched"] == ["Cobaltfield Systems"]
    assert any(u.constraint_text == "Cobaltfield Systems" for u in result.unsupported_constraints)


def test_a_disabled_registry_is_stated_and_never_resolves_a_customer(unified, calls, monkeypatch):
    from su_multi_arm.crosssource.identity import IdentityView

    monkeypatch.setitem(unified._crosssource_views, "identity", IdentityView(False, "digest_mismatch"))
    result = ask(unified, "Alderbank Ltd says the crawl stopped")
    identity = result.trace["source_coverage"]["identity"]
    assert identity["joins_enabled"] is False and identity["reason"] == "digest_mismatch" and "customers" not in identity
    assert any("identity joins are disabled" in n for n in result.trace["source_coverage"]["sources"]["logs"]["notes"])


def test_a_rollup_only_log_question_has_no_knowledge_half(unified, calls):
    result = ask(unified, "Which services had errors yesterday?")
    assert halves(result) == ["operations"]
    assert result.trace["source_coverage"]["sources"]["ticket"]["status"] == "not_planned"


def test_a_support_question_without_operations_keeps_the_agents_own_plan(unified, calls):
    result = ask(unified, "How do I split a large sitemap?")
    assert result.trace["decision"]["policy_applied"] == "rules" and halves(result) == ["knowledge"]
    assert result.trace["source_coverage"]["sources"]["logs"]["status"] == "not_asked"
    assert not intents_of(result)


# --- llm planner: source cards and the labelled fallback ---------------------------------------------


def planner_reply(mode: str, seen: list[dict[str, Any]]):
    generator = th.citing_generator()

    def respond(self, system, payload, schema, budget, stage):
        seen.append({"stage": stage, "payload": payload})
        if stage != "planner":
            return generator(self, system, payload, schema, budget, stage)
        budget.reserve()
        if mode == "invalid":
            raise SchemaViolation("bad plan", raw='{"steps": "x"}', stage="planner", error_count=1, paths=["steps (list_type)"])
        return schema.model_validate({"steps": [{"arm": "lexical", "query": payload["question"]}, {"arm": "dense", "query": payload["question"]}]})

    return respond


def test_the_llm_planner_gets_source_cards_only_for_two_halves_questions(unified_llm, monkeypatch):
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(JsonModel, "call", planner_reply("valid", seen))
    two = ask(unified_llm, "A user was added in the admin console but still cannot log in", mode=None)
    payload = next(c["payload"] for c in seen if c["stage"] == "planner")
    cards = payload["source_cards"]
    assert [c["source"] for c in cards["sources"]] == ["ticket", "doc", "logs"]
    assert cards["operations_half"]["steps"][0]["intent"] == "issue_history"
    assert "log_intents" not in payload and "log_context" not in payload  # the planner plans the knowledge half
    assert halves(two) == ["knowledge", "operations"] and [s["arm"] for s in two.trace["rounds"][0]["steps"]][:2] == ["lexical", "dense"]
    seen.clear()
    ask(unified_llm, "How do I split a large sitemap?", mode=None)
    plain = next(c["payload"] for c in seen if c["stage"] == "planner")
    assert "source_cards" not in plain and "log_intents" not in plain


def test_a_planner_failure_after_repair_gets_a_labelled_deterministic_plan_of_both_halves(unified_llm, monkeypatch):
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(JsonModel, "call", planner_reply("invalid", seen))
    result = ask(unified_llm, "Brightmoor gets unauthorized errors connecting the MCP connector even though the credentials are correct", mode=None)
    assert [c["stage"] for c in seen].count("planner") == 2  # the planner call and its one repair
    decision = result.trace["decision"]
    assert decision["planner_fallback"] == "deterministic_plan" and decision["failure"] == "schema_violation" and decision["outcome"] == "decided"
    assert decision["policy_applied"] == "deterministic_fallback_plan"
    assert [s["arm"] for s in result.trace["rounds"][0]["steps"]] == ["lexical", "dense", "structured"]
    assert result.trace["source_coverage"]["plan"] == {"halves": ["knowledge", "operations"], "policy": "deterministic_fallback_plan", "fallback": "deterministic_plan", "origin": "unified"}
    assert result.limitations[0].startswith("The planner could not form a valid plan (schema_violation)")
    assert intents_of(result)[0][1]["tenant"] == "brightmoor" and not result.answer.insufficient_evidence


def test_a_planner_failure_on_a_support_question_is_never_left_empty(unified_llm, monkeypatch):
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(JsonModel, "call", planner_reply("invalid", seen))
    result = ask(unified_llm, "How do I split a large sitemap?", mode=None)
    assert result.trace["decision"]["planner_fallback"] == "deterministic_plan"
    assert [s["arm"] for s in result.trace["rounds"][0]["steps"]] == ["lexical", "dense"] and result.retrieval.hits


# --- legacy envelope behind the dataset switch ------------------------------------------------------


def test_the_legacy_route_serves_the_merged_view_with_coverage_and_labelled_links(unified, calls, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from su_multi_arm.compat.legacy_qa.app import create_legacy_app
    from su_multi_arm.compat.legacy_qa.profile import LegacyProfile

    profile = LegacyProfile.model_validate({"warmup": False, "datasets": [{"id": "support-qa", "unified": {"enabled": True, "config": str(tmp_path / "unified.yaml")}}]})
    client = TestClient(create_legacy_app(profile, engines={"support-qa": unified}))
    body = client.post("/api/qa", json={"question": "The MCP connector returns 401 after token rotation", "dataset": "support-qa"}).json()
    assert body["dataset"] == "support-qa" and body["coverage"]["version"] == 1
    assert {"answer", "citations", "plan", "timings", "compat"} <= set(body)  # the legacy keys are unchanged
    by_id = {c["record_id"]: c for c in body["citations"]}
    assert all("source" in c for c in body["citations"])
    linked = {rid: c for rid, c in by_id.items() if c.get("links")}
    assert linked, "a cited ticket has a pinned link"
    statuses = {c["link_status"] for c in linked.values()}
    assert statuses <= {"reviewed", "candidate"}
    entry = linked["ticket:2001"]["links"][0]  # a tier-B candidate: shown labelled
    assert linked["ticket:2001"]["link_status"] == "candidate" and entry["labelled"] is True and entry["label"]
    assert entry["other_id"] == "https://docs.example.org/kb/mcp-authentication" and entry["status"] == "candidate"
    doc = by_id.get("https://docs.example.org/kb/mcp-authentication")
    if doc is not None:  # the same link seen from the doc side
        assert doc["links"][0]["other_id"] == "ticket:2001"
    assert body["coverage"]["links"]["enabled"] is True and body["coverage"]["links"]["shown"] >= 1
    health = client.get("/health", params={"dataset": "support-qa"}).json()
    assert health.get("view") == "unified"


def test_links_are_shown_only_between_readable_records(unified):
    links = unified.links_for(["ticket:1001", "ticket:2001", "ticket:1003"])
    assert set(links) == {"ticket:1001", "ticket:2001"}
    [accepted] = links["ticket:1001"]  # the link to the record outside the collection is not shown
    assert accepted["other_id"] == "https://docs.example.org/kb/crawler-timeouts" and accepted["status"] == "accepted" and accepted["labelled"] is False
    assert links["ticket:2001"][0]["labelled"] is True
    assert unified.link_view().enabled and unified.link_view().link_version


# --- regression: support questions without an operations intent keep today's plan and evidence -------

QA_SHAPED = [
    "How do I split a large sitemap?",
    "What is the sitemap URL limit?",
    "How long does a full reindex take?",
    "Where can I find the getting started guide?",
    "How were the staging crawl timeouts resolved last month?",
    "What causes index write failures?",
    "How do I restart the connector after a token rotation?",
    "What does a halted scheduled job do on the next run?",
]


def _strip(trace: dict[str, Any]) -> dict[str, Any]:
    drop = {"seconds", "trace_id", "source_coverage", "model_calls", "generator_latency"}
    out = {}
    for k, v in trace.items():
        if k in drop:
            continue
        if isinstance(v, dict):
            v = _strip(v)
        elif isinstance(v, list):
            v = [_strip(x) if isinstance(x, dict) else x for x in v]
        out[k] = v
    return out


def test_support_questions_keep_exactly_todays_payload_plan_and_evidence(tmp_path, monkeypatch):
    seen_today: list[dict[str, Any]] = []
    seen_unified: list[dict[str, Any]] = []
    docs = th.support_documents()
    today = Engine(th.support_config(tmp_path / "today", planner=False))
    today.ingest(docs)
    merged = Engine(th.support_config(tmp_path / "unified", planner=True))
    merged.ingest(docs)
    assert today.two_halves is None and merged.two_halves is not None
    for question in QA_SHAPED:
        monkeypatch.setattr(JsonModel, "call", planner_reply("valid", seen_today))
        a = today.answer(SearchRequest(query=question))
        monkeypatch.setattr(JsonModel, "call", planner_reply("valid", seen_unified))
        b = merged.answer(SearchRequest(query=question))
        assert json.dumps(seen_today[-2]["payload"], sort_keys=True, default=str) == json.dumps(seen_unified[-2]["payload"], sort_keys=True, default=str), question
        assert a.trace["decision"]["policy_applied"] == b.trace["decision"]["policy_applied"] != "two_halves"
        assert [(h.document_id, h.score) for h in a.retrieval.hits] == [(h.document_id, h.score) for h in b.retrieval.hits], question
        assert seen_today[-1]["payload"]["evidence"] == seen_unified[-1]["payload"]["evidence"], question
        assert a.answer == b.answer and _strip(a.trace) == _strip(b.trace)
        assert "source_coverage" not in a.trace and b.trace["source_coverage"]["sources"]["logs"]["status"] == "not_served"
