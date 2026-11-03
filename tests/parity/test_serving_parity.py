"""Serving parity (plan §3.15, Phase 1 "serving parity"): one ``SearchRequest`` through the Python
API, ``Engine.answer_stream``, HTTP ``/v2/answer``, HTTP SSE ``/v2/answer/stream``, an MCP stdio
server **subprocess** and an MCP streamable-HTTP server (uvicorn on loopback) yields the same final
``AnswerResult`` (timings, trace ids and session info removed), and MCP progress notifications carry
the stream events in order. Synthetic corpus on the sqlite backend; the generator is the loopback
``FakeChatServer`` (contract-mocked model, real HTTP/SSE/MCP transports)."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from su_multi_arm import Engine, SearchRequest
from su_multi_arm.serving.http import create_app
from su_multi_arm.testing.provider_fakes import FakeChatServer

from serving_support import DOCS, REQUEST, free_port, normalise, sqlite_config

pytest.importorskip("mcp")


def same_package_env() -> dict[str, str]:
    import os
    from pathlib import Path

    import su_multi_arm

    source = str(Path(su_multi_arm.__file__).resolve().parents[1])
    existing = os.environ.get("PYTHONPATH")
    return {**os.environ, "PYTHONPATH": source + (os.pathsep + existing if existing else "")}


async def mcp_calls(session, progress: list[dict]):
    async def on_progress(value, total, message):
        progress.append(json.loads(message))

    await session.initialize()
    answered = await session.call_tool("answer", REQUEST, progress_callback=on_progress)
    searched = await session.call_tool("search", REQUEST)
    bound = await session.call_tool("session_get", {})
    caps = await session.call_tool("capabilities", {})
    values = await session.call_tool("list_values", {"field": "status"})
    assert not any(r.isError for r in (answered, searched, bound, caps, values)), [r.content for r in (answered, searched, bound, caps, values)]
    return answered.structuredContent, searched.structuredContent, bound.structuredContent, caps.structuredContent, values.structuredContent


def run_stdio(config_path) -> tuple:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    progress: list[dict] = []

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "su_multi_arm.cli", "--config", str(config_path), "mcp"], env=same_package_env())
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                return await mcp_calls(session, progress)

    return asyncio.run(go()), progress


def run_streamable_http(engine) -> tuple:
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from su_multi_arm.serving.mcp import create_mcp, mcp_http_app

    port = free_port()
    app = mcp_http_app(create_mcp(engine=engine, port=port))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    progress: list[dict] = []

    async def go():
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                return await mcp_calls(session, progress)

    try:
        return asyncio.run(go()), progress
    finally:
        server.should_exit = True
        thread.join(10)


def test_one_request_yields_the_same_answer_on_every_transport(tmp_path):
    with FakeChatServer() as fake:
        config = sqlite_config(tmp_path / "state", fake.role(streaming=True), serving={"mcp": {"transports": ["stdio", "streamable_http"]}})
        engine = Engine(config)
        engine.ingest(DOCS)
        request = SearchRequest.model_validate(REQUEST)
        python = engine.answer(request).model_dump(mode="json")
        expected = normalise(python)
        assert python["answer"]["citations"] and python["contract"] == "passed" and "r2" not in [h["document_id"] for h in python["retrieval"]["hits"]]
        search_expected = normalise(engine.search(request).model_dump(mode="json"))

        results = {"python_stream": engine.answer_stream(request).result().model_dump(mode="json")}
        client = TestClient(create_app(engine=engine))
        results["http"] = client.post("/v2/answer", json=REQUEST).json()
        sse = [block for block in client.post("/v2/answer/stream", json=REQUEST).text.split("\n\n") if "event: done" in block]
        results["http_sse"] = json.loads(sse[0].split("data: ", 1)[1])["result"]

        config_path = tmp_path / "config.json"
        config_path.write_text(engine.config.model_dump_json())
        (stdio_answer, stdio_search, stdio_session, stdio_caps, stdio_values), stdio_progress = run_stdio(config_path)
        (http_answer, http_search, http_session, http_caps, http_values), http_progress = run_streamable_http(engine)
        results["mcp_stdio"], results["mcp_streamable_http"] = stdio_answer, http_answer

        for transport, result in results.items():
            assert normalise(result) == expected, transport
        for transport, result in (("mcp_stdio", stdio_search), ("mcp_streamable_http", http_search)):
            assert normalise(result) == search_expected, transport
        # MCP calls are bound to an engine session of the MCP session: both turns recorded there.
        for answer, session in ((stdio_answer, stdio_session), (http_answer, http_session)):
            assert answer["trace"]["session"]["id"] == session["id"] and len(session["turns"]) == 2
        assert stdio_session["id"] != http_session["id"]
        for progress in (stdio_progress, http_progress):
            names = [p["event"] for p in progress]
            assert names[0] == "decision" and names[-1] == "contract" and "token" in names and "done" not in names
            assert [p["seq"] for p in progress] == sorted(p["seq"] for p in progress)
        assert stdio_caps == http_caps and stdio_values == http_values == engine.list_values("status") | {"generation": stdio_values["generation"]}
