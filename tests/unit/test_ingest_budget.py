"""Ingestion budget (QA-06, review F17): ``budgets.ingest`` caps a whole build, separately from the
per-request ``budgets.max_llm_calls``; ``max_usd`` is reserved before every LLM and embedding call on
the write path and committed with the reported usage; embedding requests are counted.

Model and embedding replies are served offline through an ``httpx.MockTransport`` patched into the
providers' HTTP call (no network, no key, no spend). Synthetic data only.
"""

from __future__ import annotations

import json

import httpx
import pytest
import yaml

from su_multi_arm import Document, Engine
from su_multi_arm.compat.config_v1 import upgrade_mapping
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.errors import ConfigError

PRICE = 1000.0  # USD per million tokens, input and output: one reported call (100 in, 20 out) = $0.12


@pytest.fixture
def fake_http(monkeypatch):
    """Chat completions answer an empty extraction; ``/embeddings`` answers deterministic vectors."""
    from su_multi_arm.providers import base

    monkeypatch.setenv("SU_TEST_FAKE_KEY", "sk-offline-not-a-key")
    state = {"chat": 0, "embeddings": 0, "dimensions": 16}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            state["embeddings"] += 1
            rows = [{"index": i, "embedding": [float((i + j) % 5 + 1) for j in range(state["dimensions"])]} for i in range(len(body["input"]))]
            return httpx.Response(200, json={"data": rows, "usage": {"prompt_tokens": 10 * len(rows)}})
        state["chat"] += 1
        content = json.dumps({"entities": [], "relations": []})
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )

    original = base.HttpCall.__init__

    def patched(self, transport=None):
        original(self, httpx.MockTransport(handler))

    monkeypatch.setattr(base.HttpCall, "__init__", patched)
    return state


def price_table(tmp_path, *keys: str):
    path = tmp_path / "prices.yaml"
    path.write_text(yaml.safe_dump({"version": "test-1", "prices": {k: {"input_per_million": PRICE, "output_per_million": PRICE} for k in keys}}))
    return str(path)


def chat_role() -> dict:
    return {"provider": "openai", "model": "gpt-4o-mini", "api_key_env": "SU_TEST_FAKE_KEY", "max_tokens": 128, "capabilities": {"structured_output": "native"}}


def build_config(tmp_path, *, budgets: dict, embedder: dict | None = None, llm: bool = True, policy: str = "partial") -> dict:
    data = {
        "schema_version": 2,
        "dataset": "qa",
        "backends": {"local": {"kind": "sqlite", "path": str(tmp_path / "state")}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "metadata_schema": {"fields": [{"name": "product", "type": "string", "filterable": True}]},
        "models": {"roles": {"embedder": embedder or {"provider": "hash", "dimensions": 16}}},
        "pipeline": {"decision": {"kind": "rules"}, "arms": {"dense": {"backend": "local"}, "lexical": {"backend": "local"}, "structured": {"backend": "local"}}},
        "extraction": {"policy": policy},
        "budgets": budgets,
    }
    if llm:
        data["models"]["roles"]["extractor"] = chat_role()
        data["extraction"].update({"extractors": ["llm"], "labels": ["product"]})
    return data


def corpus(n: int) -> list[Document]:
    return [
        Document(id=f"case:{i:03d}", dataset="qa", title=f"Ticket {i}", fields={"description": f"Rounding problem number {i} in CloudTax."}, metadata={"product": "CloudTax"})
        for i in range(n)
    ]


def test_per_request_call_cap_does_not_cap_the_build_but_the_ingest_cap_does(tmp_path, fake_http):
    cfg = validate_config(build_config(tmp_path, budgets={"max_llm_calls": 2}))
    manifest = Engine(cfg).ingest(corpus(8))
    assert manifest["status"] == "complete" and fake_http["chat"] == manifest["chunks"] == len(manifest["llm_calls"]) == 16
    assert manifest["budget"]["limits"]["max_llm_calls"] == 10000
    capped = validate_config(build_config(tmp_path / "b", budgets={"max_llm_calls": 50, "ingest": {"max_llm_calls": 3}}))
    manifest = Engine(capped).ingest(corpus(8), allow_partial=True)
    stage = manifest["coverage"]["stages"]["extraction"]
    assert stage["status"] == "incomplete" and "budget exhausted for calls" in stage["reason"] and len(manifest["llm_calls"]) == 3


def test_ingest_max_usd_is_reserved_before_every_llm_call(tmp_path, fake_http):
    budgets = {"price_table": price_table(tmp_path, "gpt-4o-mini"), "ingest": {"max_usd": 0.75}}
    cfg = validate_config(build_config(tmp_path, budgets=budgets))
    manifest = Engine(cfg).ingest(corpus(20), allow_partial=True)
    stage = manifest["coverage"]["stages"]["extraction"]
    assert stage["status"] == "incomplete" and "budget exhausted for usd" in stage["reason"]
    # every call sent was reserved first: committed spend never exceeds the cap
    assert 0 < fake_http["chat"] < 20 and manifest["budget"]["usd"] <= 0.75
    assert manifest["budget"]["usd"] == pytest.approx(0.12 * fake_http["chat"])
    assert manifest["budget"]["exhausted"][0]["kind"] == "usd"


def test_embedding_requests_are_counted_and_priced(tmp_path, fake_http):
    embedder = {"provider": "openai_compatible", "model": "emb-test", "base_url": "http://embeddings.invalid/v1", "dimensions": 16, "batch_size": 4}
    budgets = {"price_table": price_table(tmp_path, "emb-test"), "ingest": {"max_usd": 1.0, "max_embedding_calls": 10}}
    cfg = validate_config(build_config(tmp_path, budgets=budgets, embedder=embedder, llm=False))
    engine = Engine(cfg)
    manifest = engine.ingest(corpus(6))
    assert manifest["status"] == "complete" and manifest["embedded"] == manifest["chunks"] == 12
    assert fake_http["embeddings"] == manifest["budget"]["embedding_calls"] == 3
    assert manifest["budget"]["usd"] == pytest.approx(12 * 10 * PRICE / 1_000_000)  # reported prompt tokens
    again = engine.ingest(corpus(6), replace=True)  # unchanged inputs: reused, no request, no spend
    assert again["reused_embeddings"] == 12 and again["budget"]["embedding_calls"] == 0 and again["budget"]["usd"] == 0
    tight = validate_config(build_config(tmp_path / "t", budgets={**budgets, "ingest": {"max_usd": 0.0001}}, embedder=embedder, llm=False))
    manifest = Engine(tight).ingest(corpus(6), allow_partial=True)
    stage = manifest["coverage"]["stages"]["embeddings"]
    assert stage["status"] == "incomplete" and "budget exhausted for usd" in stage["reason"] and fake_http["embeddings"] == 3


def test_ingest_max_usd_needs_a_price_table_and_a_price_for_every_spending_role(tmp_path, fake_http):
    with pytest.raises(ConfigError, match="ingest.max_usd requires price_table"):
        validate_config(build_config(tmp_path, budgets={"ingest": {"max_usd": 1.0}}))
    unpriced = validate_config(build_config(tmp_path, budgets={"price_table": price_table(tmp_path, "other-model"), "ingest": {"max_usd": 1.0}}))
    with pytest.raises(ConfigError, match="needs a price for 'gpt-4o-mini'") as info:
        Engine(unpriced)
    assert info.value.path == "models.roles.extractor.pricing_key"
    assert fake_http["chat"] == 0


def test_v1_max_llm_calls_maps_to_both_caps_only_when_set():
    base = {"schema_version": 1, "dataset": "demo", "storage_path": "state"}
    explicit = upgrade_mapping({**base, "max_llm_calls": 40}).config
    assert explicit.budgets.max_llm_calls == 40 and explicit.budgets.ingest.max_llm_calls == 40
    default = upgrade_mapping(base).config
    assert default.budgets.max_llm_calls == 12 and default.budgets.ingest.max_llm_calls == 10000


# --- 2.0.0a2 release review N5: a hosted embedder needs an explicit build cap -----------------------

HOSTED = {"provider": "openai_compatible", "model": "emb-test", "base_url": "https://embeddings.invalid/v1", "dimensions": 16}


@pytest.mark.parametrize(
    "embedder",
    [HOSTED, {**HOSTED, "provider": "openai", "api_key_env": "SU_TEST_FAKE_KEY"}],
)
def test_hosted_embedder_without_a_build_cap_is_refused(tmp_path, embedder):
    cfg = validate_config(build_config(tmp_path, budgets={}, embedder=embedder, llm=False))
    with pytest.raises(ConfigError, match="hosted embedder needs an explicit write-path cap"):
        Engine(cfg)


@pytest.mark.parametrize(
    "embedder,ingest",
    [
        (HOSTED, {"max_embedding_calls": 5}),
        (HOSTED, {"max_usd": 1.0}),
        ({**HOSTED, "base_url": "http://127.0.0.1:11434/v1"}, {}),  # local endpoint: no spend
        ({**HOSTED, "base_url": "http://localhost:8080/v1"}, {}),
        ({"provider": "hash", "dimensions": 16}, {}),
    ],
)
def test_capped_or_local_embedders_are_accepted(tmp_path, embedder, ingest):
    budgets = {"ingest": ingest}
    if "max_usd" in ingest:
        budgets["price_table"] = price_table(tmp_path, "emb-test")
    Engine(validate_config(build_config(tmp_path, budgets=budgets, embedder=embedder, llm=False)))
