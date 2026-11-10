"""QA-20261109-14: the MCP tools refuse unknown arguments, like the HTTP surface (``extra="forbid"``).

A misspelt argument (``filter`` for ``filters``) used to run an unfiltered search over MCP while
``POST /v2/search`` answered 422. Synthetic in-memory engine; no provider call."""

from __future__ import annotations

import asyncio

import pytest

from su_multi_arm.serving.mcp import create_mcp

from compat_support import engine as make_engine


@pytest.fixture(scope="module")
def server():
    return create_mcp(engine=make_engine(None))


@pytest.mark.parametrize("tool", ["search", "answer"])
def test_unknown_arguments_are_rejected_and_named(server, tool):
    with pytest.raises(Exception) as caught:
        asyncio.run(server.call_tool(tool, {"query": "billing invoices", "filter": {"project": "Alpha"}}))
    assert "filter" in str(caught.value) and "Extra inputs are not permitted" in str(caught.value)


def test_known_arguments_still_work(server):
    result = asyncio.run(server.call_tool("search", {"query": "billing invoices", "filters": {"project": "Alpha"}}))
    assert result is not None


def test_advertised_schemas_forbid_additional_properties(server):
    tools = asyncio.run(server.list_tools())
    assert tools and all(t.inputSchema.get("additionalProperties") is False for t in tools)
