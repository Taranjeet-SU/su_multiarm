import asyncio
import json
import os
import sys

import pytest


def same_package_env() -> dict[str, str]:
    """The server subprocess imports the package under test (a worktree), not an editable install elsewhere."""
    from pathlib import Path

    import su_multi_arm

    source = str(Path(su_multi_arm.__file__).resolve().parents[1])
    existing = os.environ.get("PYTHONPATH")
    return {**os.environ, "PYTHONPATH": source + (os.pathsep + existing if existing else "")}


def test_mcp_stdio_protocol(engine, tmp_path):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    config_path = tmp_path / "config.json"
    config_path.write_text(engine.config.model_dump_json())

    async def exercise():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "su_multi_arm.cli", "--config", str(config_path), "mcp"],
            env=same_package_env(),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} >= {"search", "answer", "get_document", "list_values", "aggregate", "session_get", "session_delete", "capabilities"}
                result = await session.call_tool(
                    "search",
                    {
                        "query": "exemption certificate",
                        "record_context": {"record_id": "case:certificate"},
                        "aggregate": {"operation": "count"},
                    },
                )
                assert not result.isError
                data = result.structuredContent or json.loads(result.content[0].text)
                assert data["aggregates"][0]["value"] == 5
                assert "case:certificate" not in {h["document_id"] for h in data["hits"]}

    asyncio.run(exercise())
