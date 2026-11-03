import json

import httpx
import pytest
from fastapi.testclient import TestClient

from su_multi_arm.api import create_app
from su_multi_arm.config import ModelConfig
from su_multi_arm.models import Answer
from su_multi_arm.providers import CallBudget, JsonModel


def test_api_context_optional_and_auth(engine, monkeypatch):
    monkeypatch.setenv("SU_TEST_API_TOKEN", "test-only")
    client = TestClient(create_app(engine.config, "SU_TEST_API_TOKEN"))
    assert client.get("/health").status_code == 200
    assert client.post("/search", json={"query": "certificate"}).status_code == 401
    headers = {"Authorization": "Bearer test-only"}
    assert client.post("/search", headers=headers, json={"query": "certificate"}).status_code == 200
    context = {"record_id": "case:certificate", "subject": "certificate"}
    headers["X-Record-Context"] = json.dumps(context)
    result = client.post("/search", headers=headers, json={"query": "certificate"})
    assert result.status_code == 200
    assert "case:certificate" not in {h["document_id"] for h in result.json()["hits"]}
    assert (
        client.post(
            "/search",
            headers=headers,
            json={"query": "certificate", "record_context": {"subject": "conflict"}},
        ).status_code
        == 422
    )
    headers["X-Record-Context"] = "bad json"
    assert client.post("/search", headers=headers, json={"query": "certificate"}).status_code == 422
    assert (
        client.post(
            "/answer", headers={"Authorization": "Bearer test-only"}, json={"query": "certificate"}
        ).status_code
        == 200
    )


@pytest.mark.parametrize("provider", ["openai_compatible", "anthropic"])
def test_model_http_contract_usage_and_no_secret_trace(provider, monkeypatch):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        value = '{"answer":"supported","citations":["id"]}'
        if provider == "anthropic":
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": value}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 12, "output_tokens": 5},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": value}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5},
            },
        )

    original = httpx.Client
    monkeypatch.setenv("SU_TEST_KEY", "secret-test-value")
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs)
    )
    model = JsonModel(ModelConfig(provider=provider, model="test", api_key_env="SU_TEST_KEY"))
    budget = CallBudget(1)
    assert model.call("system", {"query": "q"}, Answer, budget, "generator").citations == ["id"]
    assert budget.calls[0]["success"]
    assert "secret-test-value" not in json.dumps(budget.calls)
    assert requests[0]["model"] == "test"


def test_provider_truncation_is_not_success(monkeypatch):
    original = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}
        )
    )
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=transport, **kwargs))
    budget = CallBudget(1)
    with pytest.raises(RuntimeError, match="incomplete"):
        JsonModel(ModelConfig(provider="openai_compatible", model="fake")).call(
            "system", {}, Answer, budget, "generator"
        )
    assert not budget.calls[0]["success"]


def test_mcp_tools_call(engine):
    pytest.importorskip("mcp")
    import asyncio
    from su_multi_arm.mcp import create_mcp

    async def run():
        server = create_mcp(engine.config)
        names = {tool.name for tool in await server.list_tools()}
        assert names >= {"search", "answer", "get_document", "list_values", "aggregate", "session_get", "session_delete", "capabilities"}
        result = await server.call_tool("search", {"query": "exemption certificate"})
        assert result

    asyncio.run(run())
