"""Legacy-QA adapter hardening before a cutover (QA 2026-11-09, architecture review M1-M10).

Contract tests with fakes: synthetic corpus (``tests/compat_support.py``), the loopback
``FakeChatServer`` generator (contract-mocked), FastAPI ``TestClient`` and, where a real socket
matters (client disconnect, threadpool starvation), a loopback uvicorn server. Credentials below are
synthetic strings shaped like secrets; nothing here is real data.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

import su_multi_arm.compat.legacy_qa.app as legacy_app
from su_multi_arm import Document, Engine
from su_multi_arm.compat.legacy_qa import LegacyProfile, create_legacy_app
from su_multi_arm.compat.legacy_qa.profile import HideRule
from su_multi_arm.testing.provider_fakes import FakeChatServer

from compat_support import corpus
from compat_support import engine as make_engine
from serving_support import free_port, memory_config
from tests.contract.test_legacy_qa_compat import citing_answer, engine_overrides, profile_data, sse_events

TOKEN = "Zx9" + "q" * 30  # synthetic, shaped like an opaque bearer token
SECRET_DOC = Document(
    id="r99",
    dataset="demo",
    kind="record",
    title="Billing export failing with 401",
    fields={"summary": f"Billing export calls fail. Request header was Authorization: Bearer {TOKEN} and password=hunter2xyz", "details": "Rotate the key."},
    metadata={"kind": "record", "project": "Alpha", "status": "open", "created_at": "2024-03-01T08:00:00+00:00"},
)
MCP = {"X-QA-Session": "conv-1", "X-QA-Channel": "mcp", "X-QA-Client-Id": "n8n"}


@pytest.fixture
def fake():
    with FakeChatServer() as server:
        server.answer = citing_answer
        yield server


def app_for(engine, tmp_path, **profile):
    return create_legacy_app(profile_data(request_log=str(tmp_path / "r.jsonl"), **profile), engines={"demo": engine})


def rows(tmp_path):
    return [json.loads(line) for line in (tmp_path / "r.jsonl").read_text().splitlines()]


def serve(app):
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    return server, thread, port


# --- QA-01 / B1: an expired session is replaced, never 404 forever ------------------------------------


def _sid(value: str, who: str = "legacy:mcp:n8n") -> str:
    return "lq" + hashlib.sha256(f"demo\0{who}\0{value}".encode()).hexdigest()[:40]


def test_an_expired_session_is_replaced_and_the_conversation_continues(fake, tmp_path):
    engine = make_engine(fake.role(streaming=True), **engine_overrides())
    client = TestClient(app_for(engine, tmp_path))
    assert client.post("/api/qa", json={"question": "billing invoices"}, headers=MCP).status_code == 200
    manager = engine.sessions.manager
    real = manager.clock
    manager.clock = lambda: real() + engine.config.sessions.ttl_seconds + 5  # idle past sessions.ttl_seconds
    again = client.post("/api/qa", json={"question": "billing invoices"}, headers=MCP)
    assert again.status_code == 200, again.text
    block = again.json()["session_cache"]
    assert block["restarted"] is True and block["decision"] == "session_restarted" and block["turns_in_window"] == 0
    third = client.post("/api/qa", json={"question": "rounding"}, headers=MCP).json()
    assert third["session_cache"]["turns_in_window"] == 1 and "restarted" not in third["session_cache"]  # the new session continues
    assert any(r.get("session_restarted") for r in rows(tmp_path))


def test_expired_sessions_are_swept_periodically(fake, tmp_path):
    engine = make_engine(fake.role(streaming=True), **engine_overrides())
    app = app_for(engine, tmp_path, session_sweep_seconds=1)
    client = TestClient(app)
    for value in ("conv-a", "conv-b"):
        client.post("/api/qa", json={"question": "billing"}, headers={**MCP, "X-QA-Session": value})
    store = engine.sessions.manager.store
    assert store.get(_sid("conv-a")) is not None
    manager = engine.sessions.manager
    real = manager.clock
    manager.clock = lambda: real() + engine.config.sessions.ttl_seconds + 5
    app.state.served["demo"].swept_at = float("-inf")  # the sweep interval has passed
    assert client.post("/api/qa", json={"question": "billing"}, headers={**MCP, "X-QA-Session": "conv-c"}).status_code == 200
    assert store.get(_sid("conv-a")) is None and store.get(_sid("conv-b")) is None  # expired rows deleted
    assert store.get(_sid("conv-c")) is not None


# --- QA-03 / M3: unexpected failures are logged 500s, consistent across routes --------------------------


def _boom(**_kwargs):
    raise KeyError("selection")  # stands in for any adapter bug while building the envelope


def test_an_adapter_bug_is_a_logged_500_with_a_request_id(fake, tmp_path, monkeypatch, caplog):
    client = TestClient(app_for(make_engine(fake.role(streaming=True), **engine_overrides()), tmp_path))
    monkeypatch.setattr(legacy_app, "qa_envelope", _boom)
    with caplog.at_level(logging.ERROR):
        response = client.post("/api/qa", json={"question": "billing invoices"})
    assert response.status_code == 500, response.text
    rid = response.headers["X-Request-Id"]
    assert rid in response.json()["detail"] and "selection" not in response.json()["detail"]
    record = next(r for r in caplog.records if r.levelno >= logging.ERROR and rid in r.getMessage())
    assert record.exc_info and record.exc_info[0] is KeyError
    assert rows(tmp_path)[-1]["status"] == 500 and rows(tmp_path)[-1]["request_id"] == rid


def test_the_same_bug_on_search_is_also_a_500(fake, tmp_path, monkeypatch):
    client = TestClient(app_for(make_engine(fake.role(streaming=True), **engine_overrides()), tmp_path))
    monkeypatch.setattr(legacy_app, "search_envelope", lambda *a, **k: _boom())
    response = client.post("/api/search", json={"searchString": "billing invoices"})
    assert response.status_code == 500 and response.headers["X-Request-Id"] in response.json()["detail"]


def test_a_generator_schema_violation_on_search_enrich_is_a_502_like_on_answers(fake, tmp_path):
    fake.answer = citing_answer  # replies in the answer schema, not the result-notes schema
    engine = make_engine(fake.role(streaming=False), **engine_overrides())
    data = profile_data(request_log=str(tmp_path / "r.jsonl")).model_dump()
    data["datasets"][0]["search"]["enrich"] = {"enabled": True}
    client = TestClient(create_legacy_app(LegacyProfile.model_validate(data), engines={"demo": engine}))
    response = client.post("/api/search", json={"searchString": "billing invoices", "enrich": True})
    assert response.status_code == 502, (response.status_code, response.text)


def test_a_failure_after_the_done_event_still_ends_the_stream_with_an_error_event(fake, tmp_path, monkeypatch, caplog):
    client = TestClient(app_for(make_engine(fake.role(streaming=True), **engine_overrides()), tmp_path), raise_server_exceptions=False)
    monkeypatch.setattr(legacy_app, "qa_envelope", _boom)
    with caplog.at_level(logging.ERROR):
        response = client.post("/api/qa/stream", json={"question": "billing invoices"})
    events = sse_events(response.text)
    assert [n for n, _ in events][-1] == "error" and "final" not in [n for n, _ in events]
    error = events[-1][1]
    assert error["status"] == 500 and error["detail"]["error"] == "internal" and response.headers["X-Request-Id"] in error["detail"]["reason"]
    assert any(r.exc_info and r.exc_info[0] is KeyError for r in caplog.records)
    assert rows(tmp_path)[-1]["status"] == 500


# --- QA-04 / M1: disconnect cancels the run; deadline below the gateway timeout -------------------------


def test_abandoned_qa_request_cancels_generation_and_logs_499(tmp_path):
    with FakeChatServer(delay=0.25, chunk_size=4) as slow:  # ~25 s of streamed generator output
        slow.answer = citing_answer
        engine = make_engine(slow.role(streaming=True), **engine_overrides())
        server, thread, port = serve(app_for(engine, tmp_path))
        try:
            with pytest.raises(httpx.ReadTimeout):
                httpx.post(f"http://127.0.0.1:{port}/api/qa", json={"question": "billing invoices"}, timeout=2.0)
            sent_at_abort = slow.chunks_sent
            time.sleep(3.0)
            assert slow.disconnected.is_set() or slow.chunks_sent - sent_at_abort <= 4, (sent_at_abort, slow.chunks_sent)
            deadline = time.time() + 10
            while time.time() < deadline and not any(r["status"] == 499 for r in rows(tmp_path)):
                time.sleep(0.1)
            row = next(r for r in rows(tmp_path) if r["status"] == 499)
            assert row["route"] == "qa" and row["cancelled"] == "client_disconnected" and "usd" in row
        finally:
            server.should_exit = True
            thread.join(timeout=30)


def test_the_answer_deadline_cancels_the_run_with_504(tmp_path):
    with FakeChatServer(delay=0.2, chunk_size=4) as slow:
        slow.answer = citing_answer
        engine = make_engine(slow.role(streaming=True), **engine_overrides())
        client = TestClient(app_for(engine, tmp_path, answer_deadline_seconds=0.5))
        response = client.post("/api/qa", json={"question": "billing invoices"})
        assert response.status_code == 504 and "answer_deadline_seconds" in response.json()["detail"]
        events = sse_events(client.post("/api/qa/stream", json={"question": "billing invoices"}).text)
        assert events[-1][0] == "error" and events[-1][1]["status"] == 504
    assert [r["status"] for r in rows(tmp_path)] == [504, 504]


def test_profile_deadline_defaults_below_a_180_second_gateway():
    profile = profile_data()
    assert profile.answer_deadline_seconds == 170 and profile.max_concurrent_answers == 16


# --- QA-05 / M2: cheap routes never wait behind answers; admission limit -----------------------------------


def test_health_answers_within_3s_while_40_answers_are_in_flight(tmp_path):
    with FakeChatServer(delay=0.1, chunk_size=6) as slow:  # each answer streams for several seconds
        slow.answer = citing_answer
        engine = make_engine(slow.role(streaming=True), **engine_overrides())
        app = app_for(engine, tmp_path, warmup=False, max_concurrent_answers=64)  # every answer admitted
        server, thread, port = serve(app)
        try:

            async def scenario():
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=120, limits=httpx.Limits(max_connections=100)) as c:
                    answers = [asyncio.create_task(c.post("/api/qa", json={"question": f"billing invoices {i}"})) for i in range(42)]
                    await asyncio.sleep(2.0)
                    timings = {}
                    for route in ("/health", "/ready", "/api/datasets", "/api/models"):
                        started = time.perf_counter()
                        response = await c.get(route, timeout=3.0)
                        timings[route] = (response.status_code, time.perf_counter() - started)
                    done = await asyncio.gather(*answers)
                    return timings, [r.status_code for r in done]

            timings, statuses = asyncio.run(scenario())
            assert all(code == 200 and seconds < 3 for code, seconds in timings.values()), timings
            assert statuses.count(200) == 42
        finally:
            server.should_exit = True
            thread.join(timeout=60)


def test_a_full_server_refuses_answers_with_503_and_retry_after(fake, tmp_path):
    app = app_for(make_engine(fake.role(streaming=True), **engine_overrides()), tmp_path, max_concurrent_answers=2)
    client = TestClient(app)
    app.state.admission.active = 2  # two answers in flight
    for route in ("/api/qa", "/api/qa/stream"):
        response = client.post(route, json={"question": "billing"})
        assert response.status_code == 503 and response.headers["Retry-After"] and "limit 2" in response.json()["detail"]
    assert client.get("/health").json()["answers"] == {"in_flight": 2, "max_concurrent": 2}
    app.state.admission.active = 0
    assert client.post("/api/qa", json={"question": "billing"}).status_code == 200
    assert client.post("/api/qa/stream", json={"question": "billing"}).status_code == 200
    assert app.state.admission.active == 0  # every slot was returned
    assert [r.get("refused") for r in rows(tmp_path)][:2] == ["admission", "admission"]


# --- M4: record_context limit ----------------------------------------------------------------------------


def test_record_context_limits_are_utf8_bytes_and_named_in_the_refusal(fake, tmp_path):
    client = TestClient(app_for(make_engine(fake.role(streaming=True), **engine_overrides()), tmp_path))
    accented = client.post("/api/qa", json={"question": "billing", "record_context": {"subject": "é" * 3999, "recordId": "r01"}})
    assert accented.status_code == 200, accented.text  # ~8 KB of UTF-8 (24 KB as ASCII escapes): within the default
    too_long = client.post("/api/qa", json={"question": "billing", "record_context": {"subject": "x" * 4001}})
    assert too_long.status_code == 422 and "context_max_field_chars = 4000" in too_long.json()["detail"]
    small = make_engine(fake.role(streaming=True), **{**engine_overrides(), "request": {**engine_overrides()["request"], "context_max_bytes": 512}})
    refused = TestClient(app_for(small, tmp_path)).post("/api/qa", json={"question": "billing", "record_context": {"subject": "é" * 300}})
    assert refused.status_code == 422 and "context_max_bytes = 512" in refused.json()["detail"]


# --- M5: readiness ------------------------------------------------------------------------------------------


def test_ready_reports_missing_keys_failed_warmup_and_spend_cap(fake, tmp_path, monkeypatch):
    engine = make_engine(fake.role(streaming=True), **engine_overrides())
    client = TestClient(app_for(engine, tmp_path, warmup=False))
    assert client.get("/ready").status_code == 200 and client.get("/health").json()["ready"] is True
    monkeypatch.setattr(engine, "missing_keys", lambda: {f"pipeline:{engine.config.default_pipeline}": {"generator": ["SYNTHETIC_PROVIDER_KEY"]}})
    response = client.get("/ready")
    assert response.status_code == 503 and "SYNTHETIC_PROVIDER_KEY" in json.dumps(response.json())
    health = client.get("/health")
    assert health.status_code == 200 and health.json()["ready"] is False  # liveness unchanged, readiness exposed
    monkeypatch.undo()

    class Spent:
        def usage(self):
            return {"day": {"day": "2026-01-05", "committed_usd": 1.0, "reserved_usd": 0.0, "max_usd": 1.0, "calls": 3, "max_calls": 100}, "totals": {}}

    monkeypatch.setattr(engine, "_answers_spend", lambda: Spent())
    capped = client.get("/ready", params={"dataset": "demo"})
    assert capped.status_code == 503 and "daily answer spend cap reached" in json.dumps(capped.json())
    assert client.get("/ds/demo/health").json()["readiness"]["spend"]["remaining_usd_today"] == 0


def test_ready_fails_while_warmup_failed(fake, tmp_path):
    engine = make_engine(fake.role(streaming=True), **engine_overrides())

    def broken(*_args, **_kwargs):
        raise RuntimeError("synthetic embedder failure")

    engine.embedder.encode = broken
    app = app_for(engine, tmp_path, warmup=True)
    deadline = time.time() + 10
    while app.state.served["demo"].warmup.get("status") in {"not_run", "running"} and time.time() < deadline:
        time.sleep(0.05)
    response = TestClient(app).get("/ready")
    assert response.status_code == 503 and "warm-up failed" in json.dumps(response.json())


def _settled(app, statuses=("not_run", "running")):
    deadline = time.time() + 10
    while app.state.served["demo"].warmup.get("status") in statuses and time.time() < deadline:
        time.sleep(0.02)
    return app.state.served["demo"].warmup


def test_ready_waits_for_the_engine_warmup_and_reports_it(fake, tmp_path):
    engine = make_engine(fake.role(streaming=True), **engine_overrides())
    app = app_for(engine, tmp_path, warmup=True)
    state = _settled(app)
    # the in-memory backend has nothing to pre-open; the pin was taken (sqlite: tests/contract/test_live_log_warmup.py)
    assert state["status"] == "done" and state["engine"]["generation"] and state["engine"]["backends"] == {}
    assert TestClient(app).get("/ready").status_code == 200


def test_an_engine_warmup_failure_is_a_clear_readiness_problem(fake, tmp_path, monkeypatch):
    from su_multi_arm.core.errors import BackendError

    engine = make_engine(fake.role(streaming=True), **engine_overrides())

    def broken():
        raise BackendError("sqlite", "member", "slice at /synthetic/private/path is gone")

    monkeypatch.setattr(engine, "warmup", broken)
    app = app_for(engine, tmp_path, warmup=True)
    assert _settled(app)["status"] == "failed"
    response = TestClient(app).get("/ready")
    problems = response.json()["datasets"]["demo"]["problems"]
    assert response.status_code == 503 and "warm-up failed (BackendError in sqlite.member; see the server log)" in problems
    assert "/synthetic/private" not in response.text


def test_a_warmup_past_its_bound_keeps_the_dataset_not_ready_until_it_ends(fake, tmp_path, monkeypatch):
    engine = make_engine(fake.role(streaming=True), **engine_overrides())
    release = threading.Event()
    monkeypatch.setattr(engine, "warmup", lambda: release.wait(10) and {"slow": True})
    app = app_for(engine, tmp_path, warmup=True, warmup_timeout_seconds=0.05)
    client = TestClient(app)
    time.sleep(0.2)
    response = client.get("/ready")
    assert response.status_code == 503 and "warm-up timed out after" in response.text and "still running" in response.text
    assert client.get("/ds/demo/health").json()["warmup"]["status"] == "timed_out"
    release.set()
    state = _settled(app, ("not_run", "running", "timed_out"))
    assert state["status"] == "done" and state["late"] is True and state["engine"] == {"slow": True}
    assert client.get("/ready").status_code == 200


# --- M6: planner failure is visible ---------------------------------------------------------------------


def test_a_planner_failure_is_a_warning_a_log_entry_and_not_a_grounded_answer(fake, tmp_path):
    dead = {**fake.role(streaming=False), "base_url": "http://127.0.0.1:9/v1"}
    overrides = {**engine_overrides(), "pipeline": {"decision": {"kind": "llm"}}, "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 64}, "generator": fake.role(streaming=True), "planner": dead}}}
    client = TestClient(app_for(make_engine(fake.role(streaming=True), **overrides), tmp_path))
    response = client.post("/api/qa", json={"question": "billing invoices"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["insufficient_evidence"] is True and body["compat"]["planner_failure"]["failure"]
    assert any("planning failed" in w for w in body["warnings"])
    row = rows(tmp_path)[-1]
    assert row["decision_failure"] == body["compat"]["planner_failure"]["failure"] and row["plan_outcome"] == "abstain" and row["insufficient_evidence"] is True


# --- M7 / QA-06: masking and visibility on every surface -----------------------------------------------------


@pytest.fixture
def secret_client(fake, tmp_path):
    engine = Engine(memory_config(fake.role(streaming=True), **engine_overrides()))
    engine.ingest([*corpus(), SECRET_DOC])
    return TestClient(app_for(engine, tmp_path))


QUESTION = "billing export fails with 401 authorization bearer"


def leaks(text: str) -> bool:
    return TOKEN in text or "hunter2xyz" in text


def test_source_view_masks_the_token(secret_client):
    assert not leaks(json.dumps(secret_client.get("/source", params={"dataset": "demo", "id": "r99"}).json()))


def test_answer_citations_panels_and_answer_do_not_reveal_what_the_source_view_masks(secret_client, fake):
    fake.answer = lambda payload: {**citing_answer(payload), "answer": f"The header was Authorization: Bearer {TOKEN} [{payload['evidence'][0]['id']}]."}
    body = secret_client.post("/api/qa", json={"question": QUESTION}).json()
    assert [c for c in body["citations"] if c["record_id"] == "r99"], "r99 not retrieved"
    assert not leaks(json.dumps(body)), "credential in /api/qa"
    assert "[redacted]" in body["answer"]
    stream = secret_client.post("/api/qa/stream", json={"question": QUESTION}).text
    assert not leaks(stream) and "event: final" in stream


def test_search_evidence_does_not_reveal_what_the_source_view_masks(secret_client):
    body = secret_client.post("/api/search", json={"searchString": QUESTION}).json()
    assert [r for r in body["results"] if r["id"] == "r99"], "r99 not retrieved"
    assert not leaks(json.dumps(body)), "credential in /api/search"


def test_redaction_off_returns_text_as_stored(fake, tmp_path):
    engine = Engine(memory_config(fake.role(streaming=True), **engine_overrides()))
    engine.ingest([*corpus(), SECRET_DOC])
    profile = profile_data()
    profile.datasets[0].source.redact = False
    body = TestClient(create_legacy_app(profile, engines={"demo": engine})).post("/api/search", json={"searchString": QUESTION}).json()
    assert leaks(json.dumps(body))  # the switch is honoured (documented as an operator decision)


def test_hidden_records_are_removed_everywhere_and_an_answer_that_read_one_is_withheld(fake, tmp_path):
    engine = make_engine(fake.role(streaming=True), **engine_overrides())
    profile = profile_data(request_log=str(tmp_path / "r.jsonl"))
    profile.datasets[0].source.hide = [HideRule(field="kind", values=["report"])]  # r01, r05, r09
    client = TestClient(create_legacy_app(profile, engines={"demo": engine}))
    hidden = {"r01", "r05", "r09"}
    body = client.post("/api/qa", json={"question": "billing invoices rounding"}).json()
    listed = {c["record_id"] for c in body["citations"]} | {r["record_id"] for p in ("dense", "lexical") for r in (body.get(p) or {}).get("results") or []}
    listed |= {r["record_id"] for r in body["fusion"]["results"]} | set(body["compat"]["evidence_ids"].values())
    assert not listed & hidden
    assert body["compat"]["withheld"]["reason"] == "hidden_evidence" and body["insufficient_evidence"] is True
    assert body["answer"].startswith("This answer was withheld") and any("withheld" in w for w in body["warnings"])
    events = sse_events(client.post("/api/qa/stream", json={"question": "billing invoices rounding"}).text)
    names = [n for n, _ in events]
    assert "token" not in names and names[-1] == "final" and events[-1][1]["answer_rewritten"] is True  # the final notice replaces the (empty) stream
    assert not {c["url"] for c in events[1][1]["citations"]} & {f"/source?dataset=demo&id={h}" for h in hidden}
    search = client.post("/api/search", json={"searchString": "billing invoices"}).json()
    assert not {r["id"] for r in search["results"]} & hidden and not {e["id"] for e in search["evidence"]} & hidden
    assert rows(tmp_path)[0]["withheld"] == "hidden_evidence"


# --- QA-09..QA-12: minor contract gaps ---------------------------------------------------------------------


def test_total_cases_counts_only_records_that_can_be_clustered(fake, tmp_path):
    pages = [
        Document(id=f"doc{i}", dataset="demo", kind="record", title=f"Guide {i}", fields={"summary": f"Guide page {i} about billing."}, metadata={"kind": "record", "status": "open", "created_at": "2024-01-01T08:00:00+00:00"})
        for i in range(5)
    ]  # no "project": not part of any cluster
    engine = Engine(memory_config(fake.role(streaming=True), **engine_overrides()))
    engine.ingest([*corpus(), *pages])
    client = TestClient(app_for(engine, tmp_path, clusters={"dataset": "demo", "field": "project", "representatives": 1}))
    body = client.get("/api/clusters").json()
    assert body["status"] == "ready", body
    assert sum(c["size"] for c in body["clusters"]) == body["total_cases"] == 12
    assert body["compat"]["records_in_scope"] == 17 and body["compat"]["records_without_value"] == 5


def test_an_aggregate_answer_is_not_announced_as_no_match(fake, tmp_path):
    client = TestClient(app_for(make_engine(fake.role(streaming=True), **engine_overrides()), tmp_path))
    body = client.post("/api/search", json={"searchString": "how many records are there per project"}).json()
    if not body["facts"]:
        pytest.skip("the rules planner computed no aggregate for this synthetic query")
    assert body["notice"] is None or "No records matched" not in body["notice"]["text"], (body["notice"], body["facts"])


def test_an_applied_unobserved_filter_is_reported_as_applied(fake, tmp_path):
    client = TestClient(app_for(make_engine(fake.role(streaming=True), **engine_overrides()), tmp_path))
    body = client.post("/api/search", json={"searchString": "billing invoices", "aggregations": [{"type": "project", "filter": ["Gamma"]}]}).json()
    assert body["totalResults"] == 0  # the filter was applied
    text = (body["notice"] or {}).get("text", "")
    assert "not narrowed" not in text and "applied as given" in text and "project eq 'Gamma'" in text and text.startswith("No records matched")


def test_whitespace_only_search_string_is_refused_like_an_empty_question(fake, tmp_path):
    client = TestClient(app_for(make_engine(fake.role(streaming=True), **engine_overrides()), tmp_path))
    response = client.post("/api/search", json={"searchString": "   "})
    assert response.status_code == 422, (response.status_code, response.json().get("totalResults"))


def test_request_log_keeps_the_caller_ip_agent_and_request_id(fake, tmp_path):
    client = TestClient(app_for(make_engine(fake.role(streaming=True), **engine_overrides()), tmp_path))
    response = client.post("/api/qa", json={"question": "billing invoices"}, headers={"X-QA-Channel": "ui", "X-Forwarded-For": "203.0.113.7", "User-Agent": "qa-browser/1.0 " + "x" * 400})
    row = rows(tmp_path)[-1]
    assert row["forwarded_for"] == "203.0.113.7" and row["user_agent"].startswith("qa-browser/1.0") and len(row["user_agent"]) <= 160
    assert row["request_id"] == response.headers["X-Request-Id"] and row["trace_id"] and row["plan_outcome"]
    assert "question" not in row and "answer" not in row  # still no question or answer text
