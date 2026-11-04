"""HTTP surface (plan §3.15) through FastAPI's TestClient and raw ASGI: auth through the
``PrincipalResolver`` hook, SSE event order and heartbeat, client disconnect cancelling the run and
its budget, session endpoints, read-only tools, audited allowlisted actions, ``listable`` + ACL value
listing, ``/health`` and ``/ready`` (503 when a backend probe fails). Synthetic data; the generator
is the loopback ``FakeChatServer``."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from su_multi_arm import Engine, SearchRequest
from su_multi_arm.runtime.acl import Principal, StaticPolicy
from su_multi_arm.serving.auth import BearerTokenResolver, LocalResolver, Unauthenticated, default_resolver
from su_multi_arm.serving.http import create_app
from su_multi_arm.testing.provider_fakes import FakeChatServer

from serving_support import DOCS, REQUEST, memory_config, normalise

TOKEN_ENV = "SU_TEST_HTTP_TOKEN"


@pytest.fixture
def fake():
    with FakeChatServer() as server:
        yield server


def make_engine(generator, acl=None, **overrides):
    engine = Engine(memory_config(generator, **overrides), acl=acl)
    engine.ingest(DOCS)
    return engine


def sse_events(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.split("\n\n"):
        lines = [line for line in block.splitlines() if line and not line.startswith(":")]
        if not lines:
            continue
        fields = dict(line.split(": ", 1) for line in lines)
        events.append((fields["event"], json.loads(fields["data"])))
    return events


class HeaderResolver:
    """An out-of-core resolver (what an OAuth/API-key adapter would implement)."""

    def resolve(self, headers):
        user = headers.get("x-test-user")
        if not user:
            raise Unauthenticated("missing user")
        scopes = ["search"] if user == "reader" else ["search", "answer", "tools", "actions"]
        return Principal(id=user, tenant=user.split("-")[0], scopes=scopes)


def test_default_resolver_is_bearer_when_a_token_is_configured(monkeypatch, fake):
    engine = make_engine(fake.role())
    assert isinstance(default_resolver(engine.config), LocalResolver)
    monkeypatch.setenv(TOKEN_ENV, "s3cret")
    client = TestClient(create_app(engine=engine, token_env=TOKEN_ENV))
    assert client.post("/v2/search", json=REQUEST).status_code == 401
    assert client.post("/v2/search", json=REQUEST, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/v2/search", json=REQUEST, headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert client.get("/health").status_code == 200  # liveness never needs credentials
    monkeypatch.delenv(TOKEN_ENV)
    with pytest.raises(ValueError):  # a configured but unset token fails closed
        create_app(engine=engine, token_env=TOKEN_ENV)
    with pytest.raises(ValueError):
        BearerTokenResolver("")


@pytest.mark.parametrize("header", ["Bearer s3cret", "bearer s3cret", "BEARER s3cret", "BeArEr s3cret"])
def test_bearer_scheme_is_case_insensitive(header):
    """QA-12, RFC 9110 §11.1: the scheme is case-insensitive; the token is not."""
    assert BearerTokenResolver("s3cret").resolve({"authorization": header}).id == "bearer"


@pytest.mark.parametrize("header", ["", "Bearer", "Bearer ", "Bearer S3CRET", "Bearer s3cret2", "Basic s3cret", "Bearers3cret", "Token s3cret"])
def test_bearer_resolver_refuses_wrong_scheme_or_token(header):
    with pytest.raises(Unauthenticated):
        BearerTokenResolver("s3cret").resolve({"authorization": header})


def test_custom_resolver_scopes_and_acl(fake):
    engine = make_engine(fake.role(), acl=StaticPolicy(field="project", attribute="tenant"))
    client = TestClient(create_app(engine=engine, principal_resolver=HeaderResolver()))
    assert client.post("/v2/search", json={"query": "billing"}).status_code == 401
    reader = {"x-test-user": "reader"}
    assert client.post("/v2/answer", json={"query": "billing"}, headers=reader).status_code == 403  # no answer scope
    alpha = client.post("/v2/search", json={"query": "billing records"}, headers={"x-test-user": "Alpha-1"}).json()
    assert alpha["hits"] and {h["document_id"] for h in alpha["hits"]} <= {"r1", "r2"}  # ACL from the principal
    values = client.get("/v2/values/project", headers={"x-test-user": "Beta-1"}).json()
    assert values["values"] == ["Beta"] and values["truncated"] is False


def test_search_answer_and_v1_aliases_match_the_python_api(fake):
    engine = make_engine(fake.role())
    client = TestClient(create_app(engine=engine))
    python = engine.answer(SearchRequest.model_validate(REQUEST))
    for path in ("/v2/answer", "/answer"):
        response = client.post(path, json=REQUEST)
        assert response.status_code == 200 and normalise(response.json()) == normalise(python.model_dump(mode="json")), path
    assert client.post("/v2/search", json={**REQUEST, "pipeline": "nope"}).status_code == 422
    assert client.post("/v2/search", json={**REQUEST, "unknown": 1}).status_code == 422  # same strict model as Python


def test_sse_event_order_names_ids_and_final_result(fake):
    engine = make_engine(fake.role())
    client = TestClient(create_app(engine=engine))
    response = client.post("/v2/answer/stream", json=REQUEST)
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    events = sse_events(response.text)
    names = [name for name, _ in events]
    assert names[0] == "decision" and names[-2:] == ["contract", "done"] and "token" in names
    assert names.index("evidence") < names.index("token") and [data["seq"] for _, data in events] == list(range(1, len(events) + 1))
    assert all(data["provisional"] for name, data in events if name == "token")
    final = events[-1][1]["result"]
    assert normalise(final) == normalise(client.post("/v2/answer", json=REQUEST).json())
    assert client.post("/v2/answer/stream", json={**REQUEST, "mode": "llm"}).status_code == 422  # rejected before streaming


def test_sse_heartbeat_while_waiting_and_generic_error_text():
    with FakeChatServer(delay=0.05, chunk_size=200) as slow:
        engine = make_engine(slow.role())
        text = TestClient(create_app(engine=engine, heartbeat_seconds=0.01)).post("/v2/answer/stream", json=REQUEST).text
        assert ": heartbeat" in text and sse_events(text)[-1][0] == "done"
    broken = make_engine({**slow.role(), "base_url": "http://127.0.0.1:9/v1"})
    events = sse_events(TestClient(create_app(engine=broken)).post("/v2/answer/stream", json=REQUEST).text)
    assert events[-1][0] == "error" and events[-1][1]["category"] == "provider_connection"
    assert "127.0.0.1" not in events[-1][1]["message"]  # no provider detail reaches the client


def test_client_disconnect_cancels_the_run_and_its_budget():
    with FakeChatServer(delay=0.05, chunk_size=4) as slow:
        engine = make_engine(slow.role())
        streams = []
        original = engine.answer_stream

        def capture(*args, **kwargs):
            stream = original(*args, **kwargs)
            streams.append(stream)
            return stream

        engine.answer_stream = capture
        app = create_app(engine=engine)
        body = json.dumps(REQUEST).encode()
        got_token = asyncio.Event()
        sent: list[bytes] = []

        async def scenario():
            delivered = False

            async def receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await got_token.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.body":
                    sent.append(message.get("body", b""))
                    if b"event: token" in message.get("body", b""):
                        got_token.set()

            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},  # newer servers: no built-in disconnect listener
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/v2/answer/stream",
                "raw_path": b"/v2/answer/stream",
                "query_string": b"",
                "headers": [(b"content-type", b"application/json"), (b"host", b"testserver")],
                "client": ("127.0.0.1", 1),
                "server": ("testserver", 80),
            }
            await asyncio.wait_for(app(scope, receive, send), timeout=10)

        asyncio.run(scenario())
        stream = streams[0]
        assert stream.cancelled and stream.worker_done.wait(5)
        assert b"event: done" not in b"".join(sent)
        assert slow.disconnected.wait(5)  # the provider stream was closed too


def test_session_endpoints_bind_to_the_principal(fake):
    engine = make_engine(fake.role())
    client = TestClient(create_app(engine=engine, principal_resolver=HeaderResolver()))
    alice, bob = {"x-test-user": "alice"}, {"x-test-user": "bob"}
    session_id = client.post("/v2/sessions", headers=alice).json()["session_id"]
    answered = client.post("/v2/answer", json=REQUEST, headers={**alice, "x-session-id": session_id}).json()
    assert answered["trace"]["session"]["id"] == session_id
    record = client.get(f"/v2/sessions/{session_id}", headers=alice).json()
    assert len(record["turns"]) == 1 and record["turns"][0]["shown_ids"] == [h["document_id"] for h in answered["retrieval"]["hits"]]
    assert client.get(f"/v2/sessions/{session_id}", headers=bob).status_code == 403
    assert client.post("/v2/search", json=REQUEST, headers={**bob, "x-session-id": session_id}).status_code == 403
    assert client.delete(f"/v2/sessions/{session_id}", headers=alice).json() == {"closed": session_id}
    assert client.get(f"/v2/sessions/{session_id}", headers=alice).status_code == 404
    assert client.post("/v2/answer/stream", json=REQUEST, headers={**alice, "x-session-id": session_id}).status_code == 404


def test_tools_actions_and_values(fake):
    engine = make_engine(fake.role(), tools={"enabled": ["get_document", "list_values"]}, actions={"allow": ["invalidate_cache"]}, serving={"actions": {"allow": ["invalidate_cache"]}})
    client = TestClient(create_app(engine=engine, principal_resolver=HeaderResolver()))
    admin, reader = {"x-test-user": "admin"}, {"x-test-user": "reader"}
    assert [t["name"] for t in client.get("/v2/tools", headers=admin).json()["tools"]] == ["get_document", "list_values"]
    found = client.post("/v2/tools/get_document", json={"ids": ["r1", "nope"]}, headers=admin).json()
    assert [d["id"] for d in found["result"]["output"]["documents"]] == ["r1"] and found["result"]["output"]["missing"] == ["nope"]
    assert client.post("/v2/tools/aggregate", json={}, headers=admin).status_code == 404  # not enabled
    assert client.post("/v2/tools/get_document", json={"ids": []}, headers=admin).status_code == 422
    assert client.post("/v2/tools/get_document", json={"ids": ["r1"]}, headers=reader).status_code == 403
    applied = client.post("/v2/actions/invalidate_cache", json={"idempotency_key": "k1", "reason": "test"}, headers=admin)
    assert applied.status_code == 200 and applied.json()["result"]["status"] == "applied" and applied.json()["result"]["audit_id"]
    again = client.post("/v2/actions/invalidate_cache", json={"idempotency_key": "k1"}, headers=admin).json()
    assert again["result"]["status"] == "skipped_duplicate"
    denied = client.post("/v2/actions/invalidate_cache", json={"idempotency_key": "k2"}, headers=reader)
    assert denied.status_code == 403 and denied.json()["result"]["status"] == "denied"
    assert client.post("/v2/actions/ingest_delta", json={"idempotency_key": "k3"}, headers=admin).status_code == 403  # not registered
    assert {r.status for r in engine.audit.records()} >= {"applied", "denied"}  # denials audited too
    values = client.get("/v2/values/status", headers=reader).json()
    assert values["values"] == ["closed", "open", "pending"] and not values["truncated"]
    assert client.get("/v2/values/status?limit=1", headers=reader).json()["truncated"] is True
    assert client.get("/v2/values/external_id", headers=reader).status_code == 422  # not listable
    assert client.get("/v2/values/status?limit=51", headers=reader).status_code == 422  # > page_size_max
    caps = client.get("/v2/capabilities", headers=reader).json()
    assert caps["datasets"][0]["name"] == "demo" and "default" in caps["pipelines"] and "status" in caps["listable_fields"]
    assert caps["remote_actions"] == ["invalidate_cache"] and caps["answer_streaming"]["tokens"] is True


def test_health_and_ready_probe_backends(fake, monkeypatch):
    engine = make_engine(fake.role())
    client = TestClient(create_app(engine=engine))
    assert client.get("/health").json()["status"] == "ok"
    ready = client.get("/ready")
    assert ready.status_code == 200 and ready.json()["status"] == "ready" and ready.json()["generation"]
    assert ready.json()["models"]["generator"]["declared"]["streaming"] is True
    backend = engine.backends["local"]

    def broken():
        raise RuntimeError("connection refused to private-host:5432")

    monkeypatch.setattr(backend, "probe", broken, raising=False)
    failed = client.get("/ready")
    assert failed.status_code == 503 and failed.json()["backends"]["local"]["status"] == "error"
    assert "private-host" not in failed.text  # probe details stay in `doctor --probe`, not the public endpoint
    empty = Engine(memory_config(fake.role()))
    response = TestClient(create_app(engine=empty)).get("/ready")
    assert response.status_code == 503 and response.json()["reason"] == "no active generation"


def test_dependency_failures_map_to_5xx_not_422(fake, monkeypatch):
    """Release review N4: backend, generation and provider failures are server/dependency errors
    (503/502), cancellation is 499; the detail stays generic (no backend or provider text)."""
    from su_multi_arm.core.errors import BackendError, GenerationDisagreement, RunCancelled
    from su_multi_arm.providers.base import ProviderError, ProviderRateLimited

    empty = TestClient(create_app(engine=Engine(memory_config(fake.role()))))
    response = empty.post("/v2/search", json=REQUEST)
    assert response.status_code == 503 and "ingest" not in response.text  # no active index: server state
    engine = make_engine(fake.role())
    client = TestClient(create_app(engine=engine))
    cases = [
        (BackendError("postgres", "search", "connection refused to private-host:5432"), 503),
        (GenerationDisagreement("demo", "g1", {"a": "g0"}), 503),
        (ProviderError("upstream said private-model-name failed", stage="generator"), 502),
        (ProviderRateLimited("slow down private-key", stage="generator"), 502),
        (RunCancelled("cancelled"), 499),
    ]
    for error, status in cases:

        def fail(*args, error=error, **kwargs):
            raise error

        monkeypatch.setattr(engine, "search", fail)
        monkeypatch.setattr(engine, "answer", fail)
        for route in ("/v2/search", "/v2/answer"):
            response = client.post(route, json=REQUEST)
            assert response.status_code == status, (type(error).__name__, route, response.status_code)
            assert "private" not in response.text


# --- conflicts (2.0.0a2 review B5) ---------------------------------------------------------------


def test_write_conflict_on_ingest_delta_is_a_typed_409(fake):
    engine = make_engine(fake.role(), actions={"allow": ["ingest_delta"]}, serving={"actions": {"allow": ["ingest_delta"]}})
    client = TestClient(create_app(engine=engine, principal_resolver=HeaderResolver()))
    admin = {"x-test-user": "admin"}
    registry = engine.write_path.registry
    assert registry.lease_acquire("another-writer", 60)  # a live lease held by another writer
    doc = {**DOCS[0].model_dump(mode="json"), "id": "r9", "title": "new"}
    response = client.post("/v2/actions/ingest_delta", json={"idempotency_key": "w1", "args": {"documents": [doc]}}, headers=admin)
    assert response.status_code == 409
    result = response.json()["result"]
    assert result["status"] == "failed" and result["category"] == "write_conflict" and result["reason"] == "held"
    assert result["error"] == "write_conflict" and "lock" not in response.text and "Conflict:" not in response.text
    audit = [r for r in engine.audit.records() if r.idempotency_key == "w1"]
    assert audit and audit[0].status == "failed" and audit[0].error  # the detail stays in the audit record
    registry.lease_release("another-writer")
    applied = client.post("/v2/actions/ingest_delta", json={"idempotency_key": "w2", "args": {"documents": [doc]}}, headers=admin)
    assert applied.status_code == 200 and applied.json()["result"]["status"] == "applied"
    invalid = client.post("/v2/actions/ingest_delta", json={"idempotency_key": "w3", "args": {}}, headers=admin)
    assert invalid.status_code == 422 and invalid.json()["result"]["category"] == "invalid_request"
    assert "ingest_delta needs" not in invalid.text


@pytest.mark.parametrize("kind", ["write", "session"])
def test_conflicts_on_read_routes_are_409_without_backend_text(fake, monkeypatch, kind):
    from su_multi_arm.core.errors import SessionConflict, WriteConflict

    engine = make_engine(fake.role())
    client = TestClient(create_app(engine=engine, principal_resolver=HeaderResolver()))

    def conflict(*args, **kwargs):
        if kind == "write":
            raise WriteConflict("elasticsearch", "lock", "private-host:9200 lease row", "held")
        raise SessionConflict("session s-1 version 7 != 8 on private-host")

    monkeypatch.setattr(engine, "search", conflict)
    response = client.post("/v2/search", json=REQUEST, headers={"x-test-user": "admin"})
    assert response.status_code == 409 and "private-host" not in response.text
    expected = {"error": "write_conflict", "reason": "held"} if kind == "write" else {"error": "session_conflict", "reason": "cas_attempts_exhausted"}
    assert response.json()["detail"] == expected


def test_mcp_conflict_messages_are_typed():
    from su_multi_arm.core.errors import SessionConflict, WriteConflict
    from su_multi_arm.ingestion.write import IngestConflict
    from su_multi_arm.serving.mcp import safe_message

    conflict = WriteConflict("pg", "lock", "private-host detail", "lost")
    assert safe_message(conflict) == "write_conflict: lost"
    assert safe_message(IngestConflict({"category": "write_conflict:lost", "message": "private-host"}, conflict)) == "write_conflict: lost"
    assert safe_message(SessionConflict("private-host")) == "session_conflict: cas_attempts_exhausted"
