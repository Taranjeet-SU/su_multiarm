"""Synthetic guards for an optional externally supplied private PIT transport."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


@pytest.fixture
def transport(tmp_path, monkeypatch):
    location = os.environ.get("SU_GROUPED_WRAPPER_DIR")
    if not location:
        pytest.skip("private grouped transport not supplied")
    spec = importlib.util.spec_from_file_location("synthetic_grouped_transport", Path(location) / "grouped_esro.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "_read_auth_from_stdin", lambda: "Basic synthetic-test-only")
    with_client = mod.ReadOnlyES("synthetic", min_interval=0)
    monkeypatch.setattr(with_client, "_connect", lambda: pytest.fail("unexpected network connection"))
    yield mod, with_client
    with_client.close()


def test_only_owned_pit_search_and_close_allowed(transport):
    mod, client = transport
    with pytest.raises(mod.Forbidden, match="opened by this process"):
        client.request("POST", "/_search", {"size": 0, "pit": {"id": "foreign"}})
    with pytest.raises(mod.Forbidden, match="opened by this process"):
        client.request("DELETE", "/_pit", {"id": "foreign"})
    with pytest.raises(mod.Forbidden):
        client.request("DELETE", "/synthetic-index")


def test_pit_open_disallows_partial_shards_and_closes_failed_session(transport, monkeypatch):
    _, client = transport
    calls = []
    def request(method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if method == "POST":
            client._pits.add("owned")
            return {"id": "owned", "_shards": {"total": 2, "successful": 1, "failed": 1}}
        return {"succeeded": True}
    monkeypatch.setattr(client, "request", request)
    with pytest.raises(RuntimeError, match="every shard"):
        client.open_pit("synthetic-index")
    assert "allow_partial_search_results=false" in calls[0][1]
    assert calls[1] == ("DELETE", "/_pit", {"id": "owned"})
    assert not client._pits


def test_nested_top_hits_cannot_bypass_projection_guard(transport):
    mod, _ = transport
    body = {"size": 0, "aggs": {"groups": {"aggs": {"example": {"top_hits": {"size": 1, "_source": {"includes": ["body"]}}}}}}}
    with pytest.raises(mod.Forbidden, match="forbidden _source"):
        mod.ReadOnlyES._check_search_body("/_search", body)
