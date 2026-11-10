"""Legacy QA compatibility layer (``su_multi_arm.compat.legacy_qa``) against package engines.

Contract tests with fakes: synthetic corpus (``tests/compat_support.py``), loopback ``FakeChatServer``
generator (contract-mocked), FastAPI ``TestClient``. The legacy contract is pinned by
``LEGACY_QA_RESPONSE`` / ``LEGACY_SEARCH_RESPONSE`` below (required keys and types of the legacy
envelope as consumed by an earlier web UI and an MCP gateway) and by the synthetic recorded request
bodies in ``tests/fixtures/legacy_qa/requests.json``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from su_multi_arm.compat.legacy_qa import LegacyProfile, create_legacy_app, load_profile
from su_multi_arm.compat.legacy_qa.profile import HideRule
from su_multi_arm.testing.provider_fakes import FakeChatServer

from compat_support import engine as make_engine

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "legacy_qa"

# Required keys -> accepted JSON types (legacy QAResponse / Citation / Plan / Timings).
LEGACY_QA_RESPONSE = {"question": str, "answer": str, "citations": list, "plan": dict, "timings": dict}
LEGACY_CITATION = {"id": int, "title": str, "url": str}
LEGACY_PLAN = {"selected_modes": list, "reason": str, "planner": str, "intent": str}
LEGACY_TIMINGS = {"total_ms": int, "planner_ms": int, "retrieval_ms": int, "rerank_ms": int, "answer_ms": int}
LEGACY_SEARCH_RESPONSE = {
    "searchString": str,
    "page": int,
    "pageSize": int,
    "totalResults": int,
    "sortBy": str,
    "facts": list,
    "evidence": list,
    "results": list,
    "structured": list,
    "aggregations": list,
    "trace": dict,
}
LEGACY_RESULT = {"rank": int, "n": int, "id": str, "type": str, "title": str, "summary": str, "url": str, "viewerUrl": str, "contentSource": str, "score": float, "arm": str, "metadata": dict}


def conforms(value: dict, shape: dict) -> list[str]:
    problems = [f"missing {k}" for k in shape if k not in value]
    problems += [f"{k}: {type(value[k]).__name__} is not {t.__name__}" for k, t in shape.items() if k in value and not isinstance(value[k], t)]
    return problems


def assert_qa_contract(body: dict) -> None:
    assert conforms(body, LEGACY_QA_RESPONSE) == []
    assert conforms(body["plan"], LEGACY_PLAN) == []
    assert conforms(body["timings"], LEGACY_TIMINGS) == []
    for citation in body["citations"]:
        assert conforms(citation, LEGACY_CITATION) == []
    assert isinstance(body["compat"]["unsupported"], dict) and body["compat"]["unsupported"]


def citing_answer(payload):
    """A generator that cites the first three evidence ids as ``[id]`` and ``[id, id]`` markers."""
    ids = [e["id"] for e in payload.get("evidence") or []]
    if not ids:
        return {"answer": "No evidence.", "claims": [], "citations": [], "insufficient_evidence": True}
    a, b, c = (ids + ids + ids)[:3]
    text = f"Billing records cover invoices [{a}] and rounding [{b}, {c}]. See [the guide](https://example.org/g)."
    return {"answer": text, "claims": [{"text": "invoices", "kind": "narrative", "evidence_ids": [a]}, {"text": "rounding", "kind": "narrative", "evidence_ids": [b, c]}], "citations": list(dict.fromkeys([a, b, c])), "insufficient_evidence": False}


def profile_data(**overrides):
    data = {
        "datasets": [
            {
                "id": "demo",
                "label": "Demo",
                "default": True,
                "modes": {"hybrid": {}, "agentic": {"pipeline": "lexical_only"}},
                "content_source": {"article": "Knowledge Article"},
                "record_context": {"keys": {"recordId": "record_id", "subject": "subject"}},
                "source": {
                    "enabled": True,
                    "sections": [{"field": "summary", "heading": "Summary"}, {"field": "details", "heading": "Details"}],
                    "metadata_keys": ["project", "status", "created_at"],
                    "title_fallback_field": "summary",
                },
                "search": {
                    "enabled": True,
                    "date_field": "created_at",
                    "facets": [{"key": "project", "field": "project", "label": "Project"}, {"key": "kind", "field": "kind", "label": "Kind"}],
                },
            },
            {"id": "old_kb", "label": "Old KB", "retired": True},
        ],
        "models": [{"id": "fake-model", "provider": "openai_compatible", "label": "fake-model"}],
    }
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
        server.answer = citing_answer
        yield server


@pytest.fixture
def client(fake, tmp_path):
    engine = make_engine(fake.role(streaming=True), **engine_overrides())
    profile = profile_data(request_log=str(tmp_path / "requests.jsonl"))
    return TestClient(create_legacy_app(profile, engines={"demo": engine}))


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


# --- /api/qa ------------------------------------------------------------------------------------


def test_qa_envelope_matches_the_legacy_contract_and_renumbers_citations(client):
    response = client.post("/api/qa", json={"question": "billing invoices", "dataset": "demo", "model": "fake-model", "fallback_policy": "error"}, headers={"X-QA-Channel": "ui"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert_qa_contract(body)
    numbers = body["compat"]["evidence_ids"]
    assert [c["id"] for c in body["citations"]] == list(range(1, len(body["citations"]) + 1))  # every packed evidence item, numbered
    assert {str(c["id"]): c["record_id"] for c in body["citations"]} == numbers
    first, second, third = (numbers[str(n)] for n in (1, 2, 3))
    assert body["answer"].startswith("Billing records cover invoices [1] and rounding [2][3].")
    assert first not in body["answer"] and second not in body["answer"]
    assert "[the guide](https://example.org/g)" in body["answer"]  # Markdown links untouched
    assert body["compat"]["cited"] == [1, 2, 3]
    by_id = {c["record_id"]: c for c in body["citations"]}
    for rid, citation in by_id.items():  # absolute record URL when present, else the relative source view
        expected = f"https://example.org/records/{rid}" if int(rid[1:]) % 3 != 1 else f"/source?dataset=demo&id={rid}"
        assert citation["url"] == expected
        assert citation["content_source"] == "Knowledge Article"  # Document.source "article" via the profile map
    assert body["mode"] == "hybrid" and body["answer_model"] == "fake-model" and body["dataset"] == "demo"
    assert body["fusion"]["results"] and body["dense"]["results"] and body["lexical"]["results"]
    assert set(body["plan"]["selected_modes"]) >= {"dense", "lexical"}
    assert body["bundle_asset_id"] == body["compat"]["generation"]
    unsupported = body["compat"]["unsupported"]
    for field in ("cases", "kg2", "delivered_evidence", "synthetic", "timings.rerank_ms"):
        assert field in unsupported
    assert "fallback_policy" not in unsupported and "model" not in unsupported


def test_only_cited_citations_keep_their_evidence_numbers(fake):
    engine = make_engine(fake.role(streaming=False), **engine_overrides())
    profile = profile_data()
    profile.datasets[0].citations = "cited"
    body = TestClient(create_legacy_app(profile, engines={"demo": engine})).post("/api/qa", json={"question": "billing invoices"}).json()
    assert [c["id"] for c in body["citations"]] == [1, 2, 3] and len(body["compat"]["evidence_ids"]) > 3


def test_legacy_request_flags_are_reported_not_dropped(client):
    body = client.post(
        "/api/qa",
        json={"question": "billing", "model": "other-model", "mode": "fast", "evaluation_mode": True, "record_context": {"recordId": "r01", "subject": "Billing", "product": "X"}},
    ).json()
    assert "unknown mode 'fast' — used 'hybrid' (valid: hybrid, agentic)" in body["warnings"]
    assert "unknown model 'other-model' — used the default answer model" in body["warnings"]
    unsupported = body["compat"]["unsupported"]
    assert {"model", "fallback_policy", "evaluation_mode", "record_context.product"} <= set(unsupported)
    assert body["contextUsed"] is True
    assert "r01" not in body["compat"]["evidence_ids"].values()  # the record being looked at never retrieves itself


def test_errors_use_the_legacy_shape(client):
    empty = client.post("/api/qa", json={"question": "  "})
    assert empty.status_code == 422 and empty.json() == {"detail": "`question` must be a non-empty string"}
    unknown = client.post("/api/qa", json={"question": "billing", "dataset": "nope"})
    assert unknown.status_code == 422 and unknown.json()["detail"] == "unknown dataset 'nope'; valid: ['demo']"
    retired = client.post("/api/qa", json={"question": "billing", "dataset": "old_kb"})
    assert retired.status_code == 422 and "not available" in retired.json()["detail"]
    bad_as_of = client.post("/api/qa", json={"question": "billing", "as_of": "yesterday"})
    assert bad_as_of.status_code == 422 and isinstance(bad_as_of.json()["detail"], list)
    unmapped_mode = TestClient(create_legacy_app(profile_data(datasets=[{"id": "demo"}]), engines={"demo": client.app.state.served["demo"].engine}))
    agentic = unmapped_mode.post("/api/qa", json={"question": "billing", "agentic": True})
    assert agentic.status_code == 422 and agentic.json()["detail"] == "mode 'agentic' is not available for dataset 'demo'"


def test_agentic_mode_runs_the_mapped_pipeline_and_flags_missing_orchestration(client):
    body = client.post("/api/qa", json={"question": "billing invoices", "agentic": True}).json()
    assert body["mode"] == "agentic" and body["plan"]["selected_modes"] == ["lexical"]
    dag = body["agentic"]["execution_dag"]
    assert dag and dag[0]["arm"] == "lexical" and body["agentic"]["arms_used"] == ["lexical"]
    assert {"agentic.memory", "agentic.skill"} <= set(body["compat"]["unsupported"])


def test_provider_failure_maps_to_a_legacy_error(fake):
    engine = make_engine({**fake.role(streaming=False), "base_url": "http://127.0.0.1:9/v1"}, **engine_overrides())
    client = TestClient(create_legacy_app(profile_data(), engines={"demo": engine}))
    response = client.post("/api/qa", json={"question": "billing"})
    assert response.status_code == 502 and "127.0.0.1" not in response.text


# --- sessions -----------------------------------------------------------------------------------


def test_session_header_binds_a_package_session_per_client(client):
    headers = {"X-QA-Session": "conv-1", "X-QA-Client-Id": "bot-a", "X-QA-Channel": "mcp"}
    first = client.post("/api/qa", json={"question": "billing"}, headers=headers).json()
    second = client.post("/api/qa", json={"question": "rounding"}, headers=headers).json()
    assert first["session_cache"]["turns_in_window"] == 0 and second["session_cache"]["turns_in_window"] == 1
    assert second["session_cache"]["decision"] == "new_search" and "session_cache.answer_from_session" in second["compat"]["unsupported"]
    other = client.post("/api/qa", json={"question": "billing"}, headers={**headers, "X-QA-Client-Id": "bot-b"}).json()
    assert other["session_cache"]["turns_in_window"] == 0  # same conversation id, other client: other session
    bad = client.post("/api/qa", json={"question": "billing"}, headers={"X-QA-Session": "has space"})
    assert bad.status_code == 422
    clash = client.post("/api/qa", json={"question": "billing", "session_id": "conv-2"}, headers={"X-QA-Session": "conv-1"})
    assert clash.status_code == 422
    started = client.post("/api/qa", json={"question": "billing", "start_session": True}).json()
    resumed = client.post("/api/qa", json={"question": "again", "session_id": started["session_id"]}).json()
    assert resumed["session_cache"]["turns_in_window"] == 1
    assert "session_cache" not in client.post("/api/qa", json={"question": "billing"}).json()


# --- /api/qa/stream -------------------------------------------------------------------------------


def test_stream_order_numbering_and_decoded_tokens(client):
    response = client.post("/api/qa/stream", json={"question": "billing invoices", "dataset": "demo"})
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    events = sse_events(response.text)
    names = [n for n, _ in events]
    assert names[0] == "plan" and names[1] == "evidence" and names[-1] == "final"
    assert set(names[2:-1]) <= {"token"} and "token" in names
    plan, evidence, final = events[0][1], events[1][1], events[-1][1]
    assert plan["mode"] == "hybrid" and set(plan["modes"]) >= {"dense", "lexical"}
    assert [(c["id"], c["title"], c["url"]) for c in evidence["citations"]] == [(c["id"], c["title"], c["url"]) for c in final["citations"]]
    streamed = "".join(d["text"] for n, d in events if n == "token")
    assert not streamed.lstrip().startswith("{")  # the decoded answer string, not the raw JSON reply
    assert streamed == final["answer"] and "[1]" in streamed and "[2][3]" in streamed
    assert final["answer_rewritten"] is False and final["stream"]["tokens"] >= 1 and final["stream"]["streamed_chars"] == len(streamed)
    assert_qa_contract(final)


def test_stream_rejections_are_plain_legacy_errors_and_run_errors_are_events(client, fake):
    rejected = client.post("/api/qa/stream", json={"question": ""})
    assert rejected.status_code == 422 and rejected.json() == {"detail": "`question` must be a non-empty string"}
    broken = make_engine({**fake.role(streaming=True), "base_url": "http://127.0.0.1:9/v1"}, **engine_overrides())
    events = sse_events(TestClient(create_legacy_app(profile_data(), engines={"demo": broken})).post("/api/qa/stream", json={"question": "billing"}).text)
    assert events[-1][0] == "error" and "final" not in [n for n, _ in events]
    error = events[-1][1]
    assert error["status"] == 502 and set(error["detail"]) == {"error", "reason"} and "127.0.0.1" not in json.dumps(error)


def test_stream_client_disconnect_cancels_generation():
    with FakeChatServer(delay=0.05, chunk_size=4) as slow:
        slow.answer = citing_answer
        engine = make_engine(slow.role(streaming=True), **engine_overrides())
        streams = []
        original = engine.answer_stream

        def capture(*args, **kwargs):
            stream = original(*args, **kwargs)
            streams.append(stream)
            return stream

        engine.answer_stream = capture
        app = create_legacy_app(profile_data(), engines={"demo": engine})
        body = json.dumps({"question": "billing invoices"}).encode()
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
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/qa/stream",
                "raw_path": b"/api/qa/stream",
                "query_string": b"",
                "headers": [(b"content-type", b"application/json"), (b"host", b"testserver")],
                "client": ("127.0.0.1", 1),
                "server": ("testserver", 80),
            }
            await asyncio.wait_for(app(scope, receive, send), timeout=10)

        asyncio.run(scenario())
        stream = streams[0]
        assert stream.cancelled and stream.worker_done.wait(5)
        assert b"event: final" not in b"".join(sent)
        assert slow.disconnected.wait(5)


# --- /api/search ----------------------------------------------------------------------------------


def test_search_pages_sorts_filters_and_counts_facets(client):
    first = client.post("/api/search", json={"searchString": "billing invoices", "pageSize": 4, "page": 1}).json()
    assert conforms(first, LEGACY_SEARCH_RESPONSE) == []
    for result in first["results"]:
        assert conforms(result, LEGACY_RESULT) == []
    assert first["totalResults"] == 12 and first["totalRelation"] == "candidates" and first["pages"] == 3
    second = client.post("/api/search", json={"searchString": "billing invoices", "pageSize": 4, "page": 2}).json()
    assert [r["rank"] for r in second["results"]] == [5, 6, 7, 8]
    assert not {r["id"] for r in first["results"]} & {r["id"] for r in second["results"]}
    by_date = client.post("/api/search", json={"searchString": "billing invoices", "sortBy": "date", "pageSize": 12}).json()
    dates = [r["metadata"]["created_at"] for r in by_date["results"]]
    assert dates == sorted(dates, reverse=True)
    filtered = client.post("/api/search", json={"searchString": "billing invoices", "pageSize": 12, "aggregations": [{"type": "project", "filter": ["Alpha"]}, {"key": "kind", "values": ["record", "report"]}]}).json()
    assert filtered["totalResults"] == 6 and {r["metadata"]["project"] for r in filtered["results"]} == {"Alpha"}
    facets = {a["key"]: a for a in filtered["aggregations"]}
    assert facets["project"]["values"] == [{"value": "Alpha", "displayName": "Alpha", "count": 6}]
    assert sum(v["count"] for v in facets["kind"]["values"]) == 6 and facets["kind"]["exactness"] == "bounded"
    only = client.post("/api/search", json={"searchString": "billing invoices", "facetsOnly": True}).json()
    assert only["results"] == [] and only["totalResults"] == 12 and only["aggregations"]
    budget = client.post("/api/search", json={"searchString": "billing invoices", "pageSize": 10, "maxChars": 2000, "enrich": True}).json()
    assert budget["budget"]["maxChars"] == 2000 and budget["budget"]["usedChars"] <= 2000
    assert "enrich" in budget["compat"]["unsupported"] and "totalResults" in budget["compat"]["approximations"]


def test_search_refuses_what_it_cannot_express(client, fake):
    unknown = client.post("/api/search", json={"searchString": "billing", "aggregations": [{"type": "region", "filter": ["north"]}]})
    assert unknown.status_code == 422 and "unknown aggregation key" in unknown.json()["detail"]
    too_big = client.post("/api/search", json={"searchString": "billing", "pageSize": 51})
    assert too_big.status_code == 422
    profile = profile_data()
    profile.datasets[0].search.date_field = None
    no_date = TestClient(create_legacy_app(profile, engines={"demo": client.app.state.served["demo"].engine}))
    response = no_date.post("/api/search", json={"searchString": "billing", "sortBy": "post_time"})
    assert response.status_code == 422 and "not available" in response.json()["detail"]


# --- /source ----------------------------------------------------------------------------------------


def test_source_view_sections_visibility_and_redaction(fake):
    from su_multi_arm import Document, Engine

    from compat_support import corpus
    from serving_support import memory_config

    engine = Engine(memory_config(fake.role(), **engine_overrides()))
    secret = Document(id="r99", dataset="demo", kind="record", title="", fields={"summary": "Login fails\nmore", "details": "Set password=hunter2 and token: abc123secret"}, metadata={"kind": "record", "project": "Alpha", "status": "open", "tag": "t1"})
    engine.ingest([*corpus(), secret])
    profile = profile_data()
    profile.datasets[0].source.hide = [HideRule(field="kind", values=["report"])]
    client = TestClient(create_legacy_app(profile, engines={"demo": engine}))
    view = client.get("/source", params={"dataset": "demo", "id": "r02"})
    assert view.status_code == 200
    body = view.json()
    assert body["id"] == "r02" and body["title"] == "Billing r02" and body["external_url"] == "https://example.org/records/r02"
    assert [s["heading"] for s in body["sections"]] == ["Summary", "Details"]
    assert set(body["metadata"]) <= {"project", "status", "created_at"} and body["kind"] == "record"
    assert client.get("/api/source", params={"dataset": "demo", "id": "r02"}).json() == body
    hidden = client.get("/source", params={"dataset": "demo", "id": "r01"})  # kind report: hidden by the profile
    missing = client.get("/source", params={"dataset": "demo", "id": "r404"})
    assert hidden.status_code == missing.status_code == 404 and hidden.json() == missing.json() == {"detail": "source is not available"}
    redacted = client.get("/source", params={"dataset": "demo", "id": "r99"}).json()
    assert redacted["title"] == "Login fails" and redacted["title_derived"] is True
    details = next(s["text"] for s in redacted["sections"] if s["heading"] == "Details")
    assert "hunter2" not in details and "abc123secret" not in details and "password=[redacted]" in details
    for params in ({"dataset": "demo", "id": "../etc"}, {"dataset": "old_kb", "id": "r02"}, {"id": "r02", "dataset": "nope"}):
        assert client.get("/source", params=params).status_code == 404


# --- catalog, health, research, request log --------------------------------------------------------


def test_catalog_models_research_and_health(client, tmp_path):
    datasets = client.get("/api/datasets").json()
    assert datasets["datasets"] == [{"id": "demo", "label": "Demo", "default": True}] and datasets["default"] == "demo"
    assert datasets["compat"]["retired"] == ["old_kb"]
    models = client.get("/api/models").json()
    assert models["models"] == [{"id": "fake-model", "provider": "openai_compatible", "label": "fake-model"}] and models["default"] == "fake-model"
    clusters = client.get("/api/clusters").json()
    assert clusters["clusters"] == [] and clusters["compat"]["status"] == "pending"
    assert client.get("/api/evolution").json()["epochs"] == []
    assert client.get("/api/replay-runs").json()["runs"] == []
    health = client.get("/health")
    assert health.status_code == 200 and health.json()["ok"] is True and health.json()["build_id"]
    client.post("/api/qa", json={"question": "billing invoices secret words"}, headers={"X-QA-Channel": "mcp", "X-QA-Client-Id": "bot"})
    rows = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    assert rows and rows[-1]["route"] == "qa" and rows[-1]["channel"] == "mcp" and rows[-1]["client"] == "bot"
    assert "secret words" not in (tmp_path / "requests.jsonl").read_text() and rows[-1]["top_ids"]


def test_research_files_are_served_read_only(fake, tmp_path):
    (tmp_path / "clusters.json").write_text(json.dumps({"status": "ready", "clusters": [{"cluster_id": "c1", "size": 3, "top_terms": [], "subclusters": []}]}))
    profile = profile_data(research_files={"clusters": str(tmp_path / "clusters.json")})
    client = TestClient(create_legacy_app(profile, engines={"demo": make_engine(fake.role(), **engine_overrides())}))
    assert client.get("/api/clusters").json()["clusters"][0]["cluster_id"] == "c1"


def test_health_is_503_without_an_active_generation(fake):
    from su_multi_arm import Engine

    from serving_support import memory_config

    empty = Engine(memory_config(fake.role(), **engine_overrides()))
    response = TestClient(create_legacy_app(profile_data(), engines={"demo": empty})).get("/health")
    assert response.status_code == 503 and response.json()["ok"] is False


# --- profile validation and startup checks -------------------------------------------------------------


def test_profile_and_startup_validation_fail_closed(fake, tmp_path):
    with pytest.raises(ValueError, match="retired dataset cannot be the default"):
        LegacyProfile.model_validate({"datasets": [{"id": "a"}, {"id": "b", "retired": True, "default": True}]})
    with pytest.raises(ValueError, match="unknown legacy modes"):
        LegacyProfile.model_validate({"datasets": [{"id": "a", "modes": {"hybrid": {}, "fast": {}}}]})
    with pytest.raises(ValueError, match="at least one dataset must be served"):
        LegacyProfile.model_validate({"datasets": [{"id": "a", "retired": True}]})
    with pytest.raises(ValueError, match="Extra inputs"):
        LegacyProfile.model_validate({"datasets": [{"id": "a", "loader": "python:module.attr"}]})
    engine = make_engine(fake.role(), request={"context_keys": ["record_id"]})
    with pytest.raises(ValueError, match="not in request.context_keys"):
        create_legacy_app(profile_data(), engines={"demo": engine})
    no_tool = make_engine(fake.role(), tools={"enabled": []}, **engine_overrides())
    with pytest.raises(ValueError, match="get_document"):
        create_legacy_app(profile_data(), engines={"demo": no_tool})
    bad_facet = profile_data()
    bad_facet.datasets[0].search.facets[0].field = "status"  # declared but not groupable
    with pytest.raises(ValueError, match="not groupable"):
        create_legacy_app(bad_facet, engines={"demo": make_engine(fake.role(), **engine_overrides())})
    with pytest.raises(ValueError, match="neither an injected engine nor a config file"):
        create_legacy_app(profile_data())
    path = tmp_path / "profile.yaml"
    path.write_text("datasets:\n  - id: demo\n    config: cfg/demo.yaml\nrequest_log: logs/q.jsonl\n")
    loaded = load_profile(path)
    assert loaded.datasets[0].config == tmp_path / "cfg" / "demo.yaml" and loaded.request_log == tmp_path / "logs" / "q.jsonl"


# --- recorded request replay (synthetic fixtures in the legacy request shapes) ------------------------------


def test_recorded_legacy_requests_replay_against_the_adapter(client):
    recorded = json.loads((FIXTURES / "requests.json").read_text())
    assert recorded
    for item in recorded:
        response = client.request(item["method"], item["path"], json=item.get("body"), headers=item.get("headers") or {}, params=item.get("params"))
        assert response.status_code == item["expect_status"], (item["name"], response.text)
        if item["path"] in {"/api/qa"} and response.status_code == 200:
            assert_qa_contract(response.json())
        if item["path"] == "/api/qa/stream" and response.status_code == 200:
            names = [n for n, _ in sse_events(response.text)]
            assert names[0] == "plan" and names[-1] == "final", item["name"]
        if item["path"] == "/api/search" and response.status_code == 200:
            assert conforms(response.json(), LEGACY_SEARCH_RESPONSE) == []
        for key in item.get("expect_keys") or []:
            assert key in response.json(), (item["name"], key)
