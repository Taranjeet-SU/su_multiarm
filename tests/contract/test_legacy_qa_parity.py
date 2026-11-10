"""Legacy QA compatibility layer, UI/API parity slice: record links an earlier UI can render, search
envelope extras (facts / guidance / notice / enrich), session follow-ups from prior evidence,
per-dataset health and ``/ds/<id>`` prefixes, package-data clusters, the agentic envelope from the
trace, adapter stage timings and ``Engine.answer_from_evidence``.

Synthetic corpus (``tests/compat_support.py``), loopback ``FakeChatServer`` generator
(contract-mocked; it also answers the result-notes call), FastAPI ``TestClient``. No paid provider.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from su_multi_arm.compat.legacy_qa import LegacyProfile, create_legacy_app
from su_multi_arm.compat.legacy_qa.envelope import agentic_block
from su_multi_arm.compat.legacy_qa.profile import HideRule
from su_multi_arm.models import Answer, AnswerResult, SearchRequest, SearchResult
from su_multi_arm.pipeline.spec import RequestRejected
from su_multi_arm.runtime.acl import Principal
from su_multi_arm.testing.provider_fakes import FakeChatServer

from compat_support import engine as make_engine

UI = {"X-QA-Channel": "ui"}
UI_LINK = {"dataset": "old_kb", "channels": ["ui"], "id_pattern": r"^case:[A-Za-z0-9_-]+$", "rules": [{"package_prefix": "r", "alias_prefix": "case:rec-"}]}


def reply(payload):
    """Generator replies: result notes for a notes payload; else an answer citing the first two
    evidence ids, or an insufficient-evidence answer when the question says DECLINE."""
    if "results" in payload:
        notes = [{"id": r["id"], "note": f"Covers {r['title']}."} for r in payload["results"][:2]]
        return {"notes": [*notes, {"id": "not-on-page", "note": "invented"}]}
    ids = [e["id"] for e in payload.get("evidence") or []]
    if not ids or "DECLINE" in payload.get("question", ""):
        return {"answer": "The evidence does not say.", "claims": [], "citations": [], "insufficient_evidence": True}
    a, b = (ids + ids)[:2]
    return {"answer": f"Invoices are covered [{a}] and rounding [{b}].", "claims": [{"text": "invoices", "kind": "narrative", "evidence_ids": [a]}], "citations": list(dict.fromkeys([a, b])), "insufficient_evidence": False}


def profile_data(**overrides):
    dataset = {
        "id": "demo",
        "label": "Demo",
        "default": True,
        "modes": {"hybrid": {}, "agentic": {"pipeline": "lexical_only"}},
        "citations": "evidence",
        "record_context": {"keys": {"recordId": "record_id", "subject": "subject"}},
        "source": {
            "enabled": True,
            "id_pattern": r"^r[0-9]{2}$",
            "sections": [{"field": "summary", "heading": "Summary"}],
            "ui_link": UI_LINK,
        },
        "search": {
            "enabled": True,
            "date_field": "created_at",
            "facets": [{"key": "project", "field": "project", "label": "Project"}],
            "enrich": {"enabled": True, "max_results": 3},
        },
        "session_cache": {"enabled": True, "record_id_patterns": [r"\br[0-9]{2}\b"]},
    }
    dataset.update(overrides.pop("dataset", {}))
    data = {"datasets": [dataset, {"id": "old_kb", "label": "Old KB", "retired": True, "retired_message": "dataset 'old_kb' is retired"}], "warmup": False}
    data.update(overrides)
    return LegacyProfile.model_validate(data)


def engine_overrides():
    return {
        "pipelines": {"lexical_only": {"arms": {"dense": None, "structured": None}}},
        "request": {"pipelines": ["lexical_only"], "context_keys": ["record_id", "subject"]},
    }


@pytest.fixture
def fake():
    with FakeChatServer() as server:
        server.answer = reply
        yield server


@pytest.fixture
def engine(fake):
    return make_engine(fake.role(streaming=True), **engine_overrides())


@pytest.fixture
def client(engine):
    return TestClient(create_legacy_app(profile_data(), engines={"demo": engine}))


def sse_events(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.split("\n\n"):
        name, data = None, []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if name and data:
            out.append((name, json.loads("\n".join(data))))
    return out


# --- 1. record links the earlier UI renders -------------------------------------------------------------


def test_ui_channel_citations_link_records_without_urls_through_the_ui_alias(client):
    body = client.post("/api/qa", json={"question": "billing invoices"}, headers=UI).json()
    by_id = {c["record_id"]: c for c in body["citations"]}
    without_url = [rid for rid in by_id if int(rid[1:]) % 3 == 1]  # corpus: every third record has no URL
    assert without_url
    for rid in without_url:
        assert by_id[rid]["url"] == f"/source?dataset=old_kb&id=case%3Arec-{rid[1:]}"
    for rid in set(by_id) - set(without_url):
        assert by_id[rid]["url"] == f"https://example.org/records/{rid}"  # absolute URLs win (external_first)
    mcp = client.post("/api/qa", json={"question": "billing invoices"}, headers={"X-QA-Channel": "mcp"}).json()
    assert {c["url"] for c in mcp["citations"] if c["record_id"] in without_url} == {f"/source?dataset=demo&id={rid}" for rid in without_url}
    stream = sse_events(client.post("/api/qa/stream", json={"question": "billing invoices"}, headers=UI).text)
    evidence = next(d for n, d in stream if n == "evidence")
    assert {c["url"] for c in evidence["citations"]} >= {by_id[rid]["url"] for rid in without_url}


def test_source_resolves_ui_aliases_only_through_their_token(engine):
    profile = profile_data()
    profile.datasets[0].source.hide = [HideRule(field="kind", values=["report"])]
    client = TestClient(create_legacy_app(profile, engines={"demo": engine}))
    view = client.get("/api/source", params={"dataset": "old_kb", "id": "case:rec-04"})
    assert view.status_code == 200 and view.json()["id"] == "r04" and view.json()["sections"][0]["heading"] == "Summary"
    assert client.get("/source", params={"dataset": "old_kb", "id": "case:rec-04"}).json() == view.json()
    for params in (
        {"dataset": "old_kb", "id": "r04"},  # a package id under the token: not an alias
        {"dataset": "old_kb", "id": "case:rec-99"},  # alias of an unknown record
        {"dataset": "old_kb", "id": "case:rec-01"},  # alias of a hidden record (kind report)
        {"dataset": "old_kb", "id": "case:other-04"},  # no rule
        {"dataset": "old_kb", "id": "case:rec-0/4"},  # outside the alias charset
    ):
        response = client.get("/api/source", params=params)
        assert response.status_code == 404 and response.json() == {"detail": "source is not available"}, params
    assert client.post("/api/qa", json={"question": "billing", "dataset": "old_kb"}).status_code == 422  # the token is not a dataset


def test_ui_link_profile_validation_fails_closed():
    with pytest.raises(ValueError, match="must not prefix each other"):
        profile_data(dataset={"source": {"enabled": True, "ui_link": {**UI_LINK, "rules": [{"package_prefix": "r", "alias_prefix": "case:a"}, {"package_prefix": "s", "alias_prefix": "case:ab"}]}}})
    with pytest.raises(ValueError, match="must not be another served dataset's id"):
        profile_data(dataset={"source": {"enabled": True, "ui_link": {**UI_LINK, "dataset": "demo"}}})


# --- 2. search envelope extras ---------------------------------------------------------------------------


def test_search_guidance_and_notice_are_deterministic(client):
    plain = client.post("/api/search", json={"searchString": "billing invoices", "pageSize": 5}).json()
    assert plain["notice"] is None and "counts the candidates" in plain["guidance"] and plain["guidance"].startswith("Answer only from")
    assert plain["facts"] == [] and "facts" in plain["compat"]["unsupported"] and "notice.refusal" in plain["compat"]["unsupported"]
    filtered = client.post("/api/search", json={"searchString": "billing invoices", "pageSize": 5, "maxChars": 2000, "aggregations": [{"type": "project", "filter": ["Alpha"]}]}).json()
    assert "Filters applied to retrieval: project in [Alpha]." in filtered["guidance"]
    assert ("were cut to fit maxChars=2000" in filtered["guidance"]) == bool(filtered["budget"]["truncatedIds"])
    unknown = client.post("/api/search", json={"searchString": "billing invoices", "aggregations": [{"type": "project", "filter": ["Gamma"]}]}).json()
    assert unknown["notice"]["kind"] == "limitation" and "Gamma" in unknown["notice"]["text"]


def test_search_facts_cite_their_structured_block(client):
    body = client.post("/api/search", json={"searchString": "how many records are open"}).json()
    assert body["structured"], body["trace"]
    block = body["structured"][0]
    assert block["id"] == "S1" and block["n"] == len(body["results"]) + 1 and block["citations"] == [block["n"]]
    assert body["evidence"][-1]["n"] == block["n"] and body["evidence"][-1]["type"] == "sql"
    fact = body["facts"][0]
    assert fact["kind"] == "aggregate" and fact["cites"] == [block["n"]] and fact["exactness"] and fact["text"]
    assert "facts" not in body["compat"]["unsupported"]
    second = client.post("/api/search", json={"searchString": "how many records are open", "page": 2, "pageSize": 2}).json()
    assert second["facts"] == [] and second["structured"] == []  # facts and blocks ride on page 1 only


def test_search_enrich_is_metered_attributed_and_opt_in(client, fake, engine):
    body = client.post("/api/search", json={"searchString": "billing invoices", "pageSize": 5, "enrich": True}).json()
    annotated = [r for r in body["results"] if "enrichedSummary" in r]
    assert [r["id"] for r in annotated] == [r["id"] for r in body["results"][:2]]
    assert annotated[0]["enrichedSummary"] == f"Covers {annotated[0]['title']}." and annotated[0]["summary"] != annotated[0]["enrichedSummary"]
    enrich = body["compat"]["enrich"]
    assert enrich["dropped_ids"] == ["not-on-page"] and enrich["notes"] == 2 and enrich["usage"]["model_calls"] == 1
    assert body["compat"]["usage"]["model_calls"] >= 1  # search-path calls + the notes call
    assert body["trace"]["enrich"] is True and body["trace"]["enrichModel"] == "fake-model"
    sent = json.loads(fake.requests[-1]["messages"][-1]["content"])
    assert len(sent["results"]) == 3 and all(len(r["text"]) <= 500 for r in sent["results"])  # max_results, snippet cap
    calls = len(fake.requests)
    off = client.post("/api/search", json={"searchString": "billing invoices", "pageSize": 5}).json()
    assert len(fake.requests) == calls and "enrich" not in off["compat"] and not any("enrichedSummary" in r for r in off["results"])
    disabled = TestClient(create_legacy_app(profile_data(dataset={"search": {"enabled": True, "facets": []}}), engines={"demo": engine}))
    flagged = disabled.post("/api/search", json={"searchString": "billing invoices", "enrich": True}).json()
    assert len(fake.requests) == calls and "enrich" in flagged["compat"]["unsupported"]


# --- 3. session follow-ups from prior evidence -------------------------------------------------------------


def test_follow_up_is_answered_from_the_anchor_evidence_with_provenance(client, fake):
    headers = {"X-QA-Session": "conv-7", "X-QA-Client-Id": "bot-a", "X-QA-Channel": "mcp"}
    first = client.post("/api/qa", json={"question": "billing invoices"}, headers=headers).json()
    assert first["session_cache"]["decision"] == "deep_no_session_evidence" and first["plan"]["selected_modes"] != ["session_cache"]
    calls = len(fake.requests)
    follow = client.post("/api/qa", json={"question": "Summarize that in two bullets"}, headers=headers).json()
    assert follow["session_cache"]["decision"] == "cache_hit" and follow["session_cache"]["anchor_turn"] == 1
    assert follow["session_cache"]["turns_in_window"] == 1
    assert follow["plan"]["selected_modes"] == ["session_cache"] and len(fake.requests) == calls + 1  # one generator call, no planner/arms
    provenance = follow["compat"]["session_evidence"]
    assert provenance["kind"] == "session_followup" and provenance["requested_ids"] == list(first["compat"]["evidence_ids"].values())
    assert provenance["missing_ids"] == [] and provenance["reused_aggregates"] is False
    assert follow["compat"]["evidence_ids"] == first["compat"]["evidence_ids"]  # numbering kept
    assert follow["answer"].startswith("Invoices are covered [1] and rounding [2]")
    sent = json.loads(fake.requests[-1]["messages"][-1]["content"])
    assert sent["question"].startswith("Summarize that in two bullets") and "Earlier question in this conversation" in sent["question"]
    assert "billing invoices" in sent["question"] and first["answer"] not in json.dumps(sent)  # the earlier answer is never sent


def test_follow_up_gate_is_deterministic_and_isolated(client):
    headers = {"X-QA-Session": "conv-8", "X-QA-Client-Id": "bot-a"}
    client.post("/api/qa", json={"question": "billing invoices"}, headers=headers)
    other_client = client.post("/api/qa", json={"question": "summarize that"}, headers={**headers, "X-QA-Client-Id": "bot-b"}).json()
    assert other_client["session_cache"]["decision"] == "deep_no_session_evidence"  # same conversation id, other caller
    other_session = client.post("/api/qa", json={"question": "summarize that"}, headers={**headers, "X-QA-Session": "conv-9"}).json()
    assert other_session["session_cache"]["decision"] == "deep_no_session_evidence"
    not_followup = client.post("/api/qa", json={"question": "region totals for project Beta"}, headers=headers).json()
    assert not_followup["session_cache"]["decision"] == "new_search" and not_followup["plan"]["selected_modes"] != ["session_cache"]
    new_id = client.post("/api/qa", json={"question": "summarize that and r99"}, headers=headers).json()
    assert new_id["session_cache"]["decision"] == "deep_new_record_id"
    changed = client.post("/api/qa", json={"question": "summarize that", "record_context": {"subject": "Other"}}, headers=headers).json()
    assert changed["session_cache"]["decision"] == "deep_record_context_changed"


def test_follow_up_declined_by_the_generator_runs_a_full_search(client):
    headers = {"X-QA-Session": "conv-10"}
    client.post("/api/qa", json={"question": "billing invoices"}, headers=headers)
    declined = client.post("/api/qa", json={"question": "summarize that DECLINE"}, headers=headers).json()
    assert declined["session_cache"]["decision"] == "cache_declined" and "declined_usage_usd" in declined["session_cache"]
    assert declined["plan"]["selected_modes"] != ["session_cache"] and "session_evidence" not in declined["compat"]


def test_follow_up_on_the_stream_route_and_session_cache_off(client, engine):
    headers = {"X-QA-Session": "conv-11"}
    client.post("/api/qa/stream", json={"question": "billing invoices"}, headers=headers)
    events = sse_events(client.post("/api/qa/stream", json={"question": "tell me more about that"}, headers=headers).text)
    assert [n for n, _ in events] == ["plan", "evidence", "token", "final"]
    plan, evidence, token, final = (d for _, d in events)
    assert plan["modes"] == ["session_cache"] and final["session_cache"]["decision"] == "cache_hit"
    assert token["text"] == final["answer"] and [c["id"] for c in evidence["citations"]] == [c["id"] for c in final["citations"]]
    off = TestClient(create_legacy_app(profile_data(dataset={"session_cache": {"enabled": False}}), engines={"demo": engine}))
    off.post("/api/qa", json={"question": "billing invoices"}, headers=headers)
    second = off.post("/api/qa", json={"question": "summarize that"}, headers=headers).json()
    assert second["session_cache"]["enabled"] is False and second["plan"]["selected_modes"] != ["session_cache"]
    assert "session_cache.answer_from_session" in second["compat"]["unsupported"]


def test_answer_from_evidence_rereads_by_id_within_scope(engine):
    result = engine.answer_from_evidence(SearchRequest(query="billing invoices"), ["r03", "r404", "r02"], provenance={"kind": "test"})
    trace = result.trace["evidence_answer"]
    assert trace["requested_ids"] == ["r03", "r404", "r02"] and trace["missing_ids"] == ["r404"] and trace["packed_ids"] == ["r03", "r02"]
    assert result.trace["decision"]["policy_applied"] == "evidence_answer" and result.trace["rounds"] == []
    assert [h.document_id for h in result.retrieval.hits] == ["r03", "r02"] and result.contract in {"passed", "stripped"}
    assert result.answer.citations == ["r03", "r02"]
    with pytest.raises(RequestRejected):
        engine.answer_from_evidence("billing", [])
    with pytest.raises(RequestRejected, match="provenance"):
        engine.answer_from_evidence("billing", ["r02"], provenance={"answer": "cached text"})
    nothing = engine.answer_from_evidence("billing", ["r404"])
    assert nothing.answer.insufficient_evidence and nothing.trace["evidence_answer"]["packed_ids"] == []


def test_answer_from_evidence_respects_the_access_policy(fake):
    from su_multi_arm import Engine

    from compat_support import corpus
    from serving_support import memory_config

    engine = Engine(memory_config(fake.role(streaming=False), access={"policy": "tenant", "field": "project"}))
    engine.ingest(corpus())
    beta = Principal(id="u-beta", tenant="Beta", scopes=["search", "answer"])
    result = engine.answer_from_evidence("billing invoices", ["r01", "r02", "r03"], beta)
    assert result.trace["evidence_answer"]["packed_ids"] == ["r02"] and set(result.trace["evidence_answer"]["missing_ids"]) == {"r01", "r03"}


# --- 4. health per dataset, /ds prefixes, clusters ---------------------------------------------------------


def test_health_per_dataset_and_prefixed_routes(client):
    combined = client.get("/health").json()
    assert combined["datasets"]["old_kb"] == {"status": "retired"} and combined["datasets"]["demo"]["status"] == "up"
    up = client.get("/ds/demo/health")
    assert up.status_code == 200 and up.json()["status"] == "up" and up.json()["build_id"]
    assert client.get("/health", params={"dataset": "demo"}).json()["status"] == "up"
    retired = client.get("/ds/old_kb/health")
    assert retired.status_code == 410 and retired.json()["status"] == "retired" and retired.json()["ok"] is False
    assert client.get("/ds/nope/health").status_code == 404
    assert client.get("/ds/demo/health", params={"dataset": "old_kb"}).status_code == 422
    answer = client.post("/ds/demo/api/qa", json={"question": "billing invoices"})
    assert answer.status_code == 200 and answer.json()["dataset"] == "demo"
    assert client.post("/ds/old_kb/api/qa", json={"question": "billing"}).json() == {"detail": "dataset 'old_kb' is retired"}
    clash = client.post("/ds/demo/api/qa", json={"question": "billing", "dataset": "old_kb"})
    assert clash.status_code == 422 and "disagrees" in clash.json()["detail"]
    assert client.post("/ds/demo/api/search", json={"searchString": "billing"}).status_code == 200


def test_clusters_from_package_metadata_groups(engine):
    profile = profile_data(clusters={"dataset": "demo", "field": "project", "representatives": 2, "representative_sort": "created_at"})
    client = TestClient(create_legacy_app(profile, engines={"demo": engine}))
    body = client.get("/api/clusters").json()
    assert body["status"] == "ready" and body["n_clusters"] == 2 and body["total_cases"] == 12
    assert {c["label"]: c["size"] for c in body["clusters"]} == {"Alpha": 6, "Beta": 6}
    first = body["clusters"][0]
    assert len(first["representatives"]) == 2 and all(r["subject"].startswith("Billing r") for r in first["representatives"])
    assert first["top_terms"] == [] and {"clusters", "top_terms", "subclusters"} <= set(body["compat"]["unsupported"])
    assert client.get("/api/clusters").json() == body  # cached per generation
    with pytest.raises(ValueError, match="not groupable"):
        create_legacy_app(profile_data(clusters={"dataset": "demo", "field": "status"}), engines={"demo": engine})
    with pytest.raises(ValueError, match="not a served dataset"):
        profile_data(clusters={"dataset": "old_kb", "field": "project"})


def test_warmup_loads_the_embedder_and_is_reported(engine):
    client = TestClient(create_legacy_app(profile_data(warmup=True), engines={"demo": engine}))
    deadline = time.time() + 10
    while client.get("/ds/demo/health").json()["warmup"]["status"] != "done" and time.time() < deadline:
        time.sleep(0.05)
    assert client.get("/ds/demo/health").json()["warmup"]["status"] == "done"


# --- 5. agentic envelope from the trace; 6. stage timings ----------------------------------------------------


def _result(trace: dict, insufficient: bool = False) -> AnswerResult:
    retrieval = SearchResult(query="q", generation="0" * 32, hits=[], arm_hits={})
    return AnswerResult(answer=Answer(answer="a", insufficient_evidence=insufficient), retrieval=retrieval, trace=trace)


def test_agentic_block_maps_rounds_review_and_hops():
    trace = {
        "rounds": [
            {"query": "q", "steps": [{"arm": "dense", "query": "q", "documents": 5, "seconds": 0.1}, {"arm": "structured", "query": "q", "documents": 0, "aggregate": True}]},
            {"query": "follow-up q", "steps": [{"arm": "lexical", "query": "follow-up q", "documents": 3, "incomplete": True}]},
        ],
        "review": {"rounds": [{"hop": 1, "sufficient": False, "gap_present": True}, {"hop": 2, "sufficient": True}], "hops_executed": 1, "stop_reason": "sufficient"},
    }
    block = agentic_block(_result(trace), "q")
    dag = block["execution_dag"]
    assert [(s["step"], s["arm"], s["parallel_group"], s["depends_on"]) for s in dag] == [("r1.s1", "dense", 1, []), ("r1.s2", "sql", 1, [])]
    assert dag[0]["n_results"] == 5 and dag[1]["n_results"] == 1 and dag[0]["enriched_query"] is None
    grading = block["grading"]
    assert grading["sufficient"] is True and "sufficient" in grading["reason"] and "1 follow-up search hop executed" in grading["reason"]
    corrective = grading["corrective"]
    assert corrective["reformulated_query"] == "follow-up q" and corrective["hops"] == 1
    assert [(s["step"], s["depends_on"], s["input_from"], s["error"]) for s in corrective["trace"]] == [("r2.s1", ["r1.s1", "r1.s2"], ["review"], "incomplete")]
    assert block["arms_used"] == ["dense", "sql", "lexical"]
    no_review = agentic_block(_result({"rounds": trace["rounds"][:1]}, insufficient=True), "q")
    assert no_review["grading"]["sufficient"] is False and "no review stage" in no_review["grading"]["reason"] and no_review["grading"]["corrective"] is None
    limited = agentic_block(_result({**trace, "review": {"rounds": [], "hops_executed": 1, "stop_reason": "hop_limit"}}), "q")
    assert limited["grading"]["sufficient"] is False and "hop limit" in limited["grading"]["reason"]


def test_adapter_stage_timings_are_reported(client):
    body = client.post("/api/qa", json={"question": "billing invoices"}).json()
    stages = body["timings"]["stages"]
    for key in ("adapter:prepare_ms", "adapter:engine_start_ms", "adapter:to_evidence_ms", "adapter:to_done_ms", "adapter:envelope_ms", "adapter:overhead_ms"):
        assert isinstance(stages[key], int) and stages[key] >= 0, key
    assert stages["adapter:to_evidence_ms"] <= stages["adapter:to_done_ms"] <= body["timings"]["total_ms"]
