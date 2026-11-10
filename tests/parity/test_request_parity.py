"""Request-model parity (plan §3.15): Python API, HTTP and MCP accept the same ``SearchRequest`` —
typed filter values, ``exclude_ids``, ``record_context`` and the typed aggregate — and return the
same hits. The MCP tool schemas are checked field-for-field against the model."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from su_multi_arm.api import create_app
from su_multi_arm.models import SearchRequest
from su_multi_arm.serving import REQUEST_FIELDS, tool_differences
from su_multi_arm.serving.mcp import TOOL_MODELS

REQUEST = {
    "query": "certificate rounding",
    "filters": {"product": "CloudTax"},
    "exclude_ids": ["tracker:rounding"],
    "record_context": {"subject": "certificate"},
    "aggregate": {"operation": "count"},
}


def ids(payload):
    return [h["document_id"] for h in payload["hits"]], payload["aggregates"][0]["value"], payload["unsupported_constraints"]


def test_python_http_and_mcp_accept_the_same_request_and_agree(engine):
    pytest.importorskip("mcp")
    from su_multi_arm.mcp import create_mcp

    python = ids(engine.search(SearchRequest.model_validate(REQUEST)).model_dump(mode="json"))
    assert len(python[0]) >= 2 and python[1] == 3 and "tracker:rounding" not in python[0]  # 4 CloudTax records minus the excluded one
    client = TestClient(create_app(engine.config))
    for path in ("/search", "/v2/search"):
        response = client.post(path, json=REQUEST)
        assert response.status_code == 200 and ids(response.json()) == python, path
    assert client.post("/search", json={**REQUEST, "filters": {"product": 7}}).status_code == 200  # typed filter values accepted

    async def mcp():
        server = create_mcp(engine.config)
        tools = {tool.name: tool for tool in await server.list_tools()}
        assert set(tools) >= {"search", "answer", "get_document", "list_values", "aggregate", "session_get", "session_delete", "capabilities"}
        for name in ("search", "answer"):
            tool = tools[name]
            assert set(tool.inputSchema["properties"]) == set(REQUEST_FIELDS) and tool_differences(tool.inputSchema) == []
        for name, model in [(n, m) for n, m in TOOL_MODELS.items() if n in tools]:  # the tool mirrors take their models' fields, nothing else
            assert tool_differences(tools[name].inputSchema, model) == [] if model is not None else not tools[name].inputSchema.get("properties")
        result = await server.call_tool("search", REQUEST)
        content = result[0] if isinstance(result, tuple) else result
        return json.loads(content[0].text)

    assert ids(asyncio.run(mcp())) == python


def test_tool_differences_detect_dropped_or_narrowed_fields():
    schema = SearchRequest.model_json_schema()
    assert tool_differences(schema) == []
    narrowed = json.loads(json.dumps(schema))
    narrowed["properties"]["filters"]["additionalProperties"] = {"type": "string"}  # the alpha's MCP wrapper
    del narrowed["properties"]["exclude_ids"]
    drift = tool_differences(narrowed)
    assert any("missing field 'exclude_ids'" in d for d in drift) and any("field 'filters'" in d for d in drift)
