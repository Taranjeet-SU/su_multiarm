"""One log question, one plan, the same numbers on every surface (addendum A2 review B5, M1).

The decision path in ``rules``, ``llm`` (scripted provider) and ``cascade`` modes, ``Engine.call_tool``,
``POST /v2/tools/{name}``, MCP in process, MCP over stdio (subprocess) and MCP streamable HTTP
mounted on the HTTP app all return the same rows and the same rendering. Also: the scope guard
(R2-B4) on every entry point and the single-tenant refusal. Synthetic gold set; scripted planner
(no model is called); evidence level: contract.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from su_multi_arm import SearchRequest
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.providers import JsonModel
from su_multi_arm.runtime.acl import TenantPolicy
from su_multi_arm.serving.http import create_app
from su_multi_arm.serving.mcp import create_mcp

from tests.logs_gold_support import AS_OF, QUESTIONS, build_gold, gold_config

pytest.importorskip("mcp")
FAKE = {"provider": "openai_compatible", "model": "fake-local", "base_url": "http://127.0.0.1:9/v1"}
D = AS_OF.isoformat()


def parity_config(tmp: Path) -> dict:
    data = gold_config(tmp)
    data["models"]["roles"]["planner"] = FAKE
    data["pipeline"]["decision"] = {"kind": "cascade", "stages": ["rules", "llm"]}
    data["request"] = {"modes": ["rules", "llm", "cascade"]}
    data["serving"] = {"mcp": {"transports": ["stdio", "streamable_http"], "mount_on_http": True}}
    return data


@pytest.fixture(scope="module")
def gold(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("parity")
    data = parity_config(tmp)
    engine, _, _ = build_gold(tmp, data=data)
    (tmp / "config.yaml").write_text(yaml.safe_dump(data))
    return engine, tmp


def comparable(output: dict) -> dict:
    return {k: output[k] for k in ("intent", "args", "rows", "rendering", "exactness", "status", "source_unavailable", "source_counts")}


def no_model(monkeypatch):
    def refuse(*args, **kwargs):  # pragma: no cover - a recognised log question never reaches the planner
        raise AssertionError("the planner must not be called for a recognised log question")

    monkeypatch.setattr(JsonModel, "call", refuse)


@pytest.mark.parametrize("question,intent,args", QUESTIONS, ids=[q[1] + str(i) for i, q in enumerate(QUESTIONS)])
def test_decision_modes_and_tool_call_agree(gold, monkeypatch, question, intent, args):
    engine, _ = gold
    no_model(monkeypatch)
    tool_args = args if intent == "log_around" else {**args, "as_of": D}
    tool = comparable(engine.call_tool(intent, tool_args).result.output.model_dump(mode="json"))
    for mode in ("rules", "llm", "cascade"):
        result = engine.search(SearchRequest(query=question, as_of=D, mode=mode))
        assert len(result.aggregates) == 1, (mode, result.aggregates)
        aggregate = result.aggregates[0]
        assert aggregate["intent"] == intent and comparable({**aggregate, "exactness": aggregate["exactness"]}) == tool, mode


def test_llm_selected_intent_is_compiled_like_the_tool(gold, monkeypatch):
    """A phrasing the rules do not recognise: the scripted planner selects the intent; the engine
    compiles the scope from the arguments (identical to the tool call)."""
    engine, _ = gold

    def respond(self, system, payload, schema, budget, stage):
        budget.reserve()
        assert "log_intents" in payload
        return schema.model_validate(
            {"steps": [{"arm": "structured", "log_intent": "log_top", "log_args": [{"field": "by", "value": "tenant"}, {"field": "level", "value": "fatal"}, {"field": "window", "value": "yesterday"}]}]}
        )

    monkeypatch.setattr(JsonModel, "call", respond)
    result = engine.search(SearchRequest(query="crash report numbers by customer, please", as_of=D, mode="llm"))
    tool = comparable(engine.call_tool("log_top", {"by": "tenant", "level": ["fatal"], "window": "yesterday", "as_of": D}).result.output.model_dump(mode="json"))
    assert comparable(result.aggregates[0]) == tool and tool["rows"] == [{"tenant": "t01", "lines": 2}, {"tenant": "t03", "lines": 2}]


def test_llm_free_scope_over_log_kinds_is_refused(gold, monkeypatch):
    engine, _ = gold

    def respond(self, system, payload, schema, budget, stage):
        budget.reserve()
        return schema.model_validate({"steps": [{"arm": "structured", "scope": {"group_by": ["service"], "measure": {"kind": "sum", "field": "lines"}}}]})

    monkeypatch.setattr(JsonModel, "call", respond)
    result = engine.search(SearchRequest(query="crash report numbers by customer, please", as_of=D, mode="llm"))
    assert not result.aggregates and any(u.reason == "intent_required" for u in result.unsupported_constraints)


# --- the scope guard on every entry point (R2-B4) ----------------------------------------------------


def test_guard_on_the_request_aggregate(gold, monkeypatch):
    engine, _ = gold
    no_model(monkeypatch)
    result = engine.search(SearchRequest(query="count by service", as_of=D, mode="rules", aggregate={"operation": "group_count", "field": "service"}))
    aggregate = result.aggregates[0]
    assert aggregate["exactness"] == "bounded" and aggregate["unsupported"][0]["reason"] == "intent_required"


@pytest.mark.parametrize(
    "scope,refused",
    [
        ({"group_by": ["service"], "measure": {"kind": "sum", "field": "lines"}}, True),  # kind-less
        ({"predicates": [{"field": "kind", "op": "eq", "value": "log_day", "negate": True}]}, True),  # negated
        ({"predicates": [{"field": "kind", "op": "in", "value": ["log_day", "signature_day"]}]}, True),  # multi-kind
        ({"predicates": [{"field": "kind", "op": "eq", "value": "log_day"}], "measure": {"kind": "sum", "field": "lines"}}, False),
    ],
)
def test_guard_on_the_python_and_http_aggregate_tool(gold, scope, refused):
    engine, _ = gold
    python = engine.call_tool("aggregate", {"scope": scope}).result.output.result
    http = TestClient(create_app(engine=engine), base_url="http://127.0.0.1").post("/v2/tools/aggregate", json={"scope": scope}).json()["result"]["output"]["result"]
    for result in (python.model_dump(mode="json"), http):
        assert (result["exactness"] == "bounded" and result["unsupported"][0]["reason"] == "intent_required") is refused


def test_guard_on_the_mcp_aggregate_tool(gold):
    engine, _ = gold
    server = create_mcp(engine=engine)
    content, structured = asyncio.run(server.call_tool("aggregate", {"scope": {"group_by": ["service"]}}))
    assert structured["result"]["output"]["result"]["unsupported"][0]["reason"] == "intent_required"


def test_adhoc_scopes_allowed_by_configuration(tmp_path):
    from su_multi_arm.core.config import validate_config
    from su_multi_arm.decision.log_guard import guard_active

    data = gold_config(tmp_path)
    data["logs"]["serving"] = {"allow_adhoc_scopes": True}
    (tmp_path / "patterns").mkdir()
    assert guard_active(validate_config(data)) is False


# --- HTTP and MCP surfaces --------------------------------------------------------------------------


def test_http_tool_route_matches_the_tool_call(gold):
    engine, _ = gold
    client = TestClient(create_app(engine=engine), base_url="http://127.0.0.1")
    names = {t["name"] for t in client.get("/v2/tools").json()["tools"]}
    assert {"log_top", "log_coverage", "incidents", "graph_neighbourhood"} <= names
    for _, intent, args in QUESTIONS:
        tool_args = args if intent == "log_around" else {**args, "as_of": D}
        http = client.post(f"/v2/tools/{intent}", json=tool_args).json()["result"]["output"]
        assert http == engine.call_tool(intent, tool_args).result.output.model_dump(mode="json")


def test_mcp_lists_read_only_log_tools_and_matches_byte_for_byte(gold):
    engine, _ = gold
    server = create_mcp(engine=engine)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    for name in ("log_top", "log_new", "log_affected", "log_trend", "log_compare", "log_tenants", "log_around", "log_endpoints", "log_signature", "log_coverage", "incidents", "graph_neighbourhood"):
        assert tools[name].annotations.readOnlyHint is True and tools[name].annotations.destructiveHint is False
    assert {"search", "answer", "aggregate", "capabilities"} <= set(tools)  # the existing surface stays
    for _, intent, args in QUESTIONS:
        tool_args = args if intent == "log_around" else {**args, "as_of": D}
        _, structured = asyncio.run(server.call_tool(intent, tool_args))
        python = engine.call_tool(intent, tool_args).result.output.model_dump(mode="json")
        assert json.dumps(structured, sort_keys=True) == json.dumps(python, sort_keys=True)


def _env() -> dict[str, str]:
    import su_multi_arm

    source = str(Path(su_multi_arm.__file__).resolve().parents[1])
    root = str(Path(__file__).resolve().parents[2])
    # Keep the parent's PYTHONPATH (e.g. an isolated google-re2 install) so both sides use one engine.
    inherited = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    return {**os.environ, "PYTHONPATH": os.pathsep.join([source, root, *inherited])}


def test_mcp_stdio_subprocess_and_mounted_streamable_http_agree(gold):
    import uvicorn
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client

    from serving_support import free_port

    engine, tmp = gold
    args = {"by": "service", "level": ["error"], "window": "yesterday", "as_of": D}
    expected = engine.call_tool("log_top", args).result.output.model_dump(mode="json")

    async def call(session):
        await session.initialize()
        listed = await session.list_tools()
        result = await session.call_tool("log_top", args)
        assert not result.isError, result.content
        return {t.name for t in listed.tools}, result.structuredContent

    async def stdio():
        params = StdioServerParameters(command=sys.executable, args=["-m", "su_multi_arm.cli", "--config", str(tmp / "config.yaml"), "mcp"], env=_env())
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                return await call(session)

    stdio_tools, stdio_result = asyncio.run(stdio())
    port = free_port()
    app = create_app(engine=engine)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)

    async def http():
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                return await call(session)

    try:
        http_tools, http_result = asyncio.run(http())
        import httpx

        rebinding = httpx.post(f"http://127.0.0.1:{port}/mcp", headers={"Host": "attacker.example"}, json={})
        assert rebinding.status_code in {400, 403, 421}
    finally:
        server.should_exit = True
        thread.join(10)
    assert stdio_tools == http_tools and "log_top" in http_tools
    assert stdio_result == http_result == expected


# --- single tenant (B4) ------------------------------------------------------------------------------


def test_tenant_scoped_policy_is_refused_under_the_logs_profile(gold, tmp_path):
    engine, _ = gold
    from su_multi_arm import Engine
    from su_multi_arm.core.config import validate_config

    with pytest.raises(ConfigError, match="single tenant"):
        Engine(engine.config, acl=TenantPolicy("tenant"))
    data = gold_config(tmp_path)
    data["access"] = {"policy": "tenant"}
    with pytest.raises(ConfigError, match="single tenant"):
        validate_config(data)
    engine.acl = TenantPolicy("tenant")
    try:
        with pytest.raises(ConfigError, match="single tenant"):
            create_app(engine=engine)
        with pytest.raises(ConfigError, match="single tenant"):
            create_mcp(engine=engine)
    finally:
        from su_multi_arm.runtime.acl import NoConstraints

        engine.acl = NoConstraints()
