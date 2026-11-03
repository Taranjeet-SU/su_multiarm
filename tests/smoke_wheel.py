"""Run using a clean environment that has the built wheel plus [api,mcp], from any cwd."""

import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def main():
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="su-wheel-smoke-") as directory:
        directory = Path(directory)
        config = directory / "config.json"
        config.write_text(
            json.dumps(
                {
                    "dataset": "demo",
                    "storage_path": str(directory / "state"),
                    "extraction": {
                        "providers": ["tags"],
                        "tags": {"topic": ["exemption certificate", "rounding", "content source"]},
                    },
                }
            )
        )
        command = [sys.executable, "-m", "su_multi_arm.cli", "--config", str(config)]
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)

        def cli(*args):
            return json.loads(subprocess.check_output([*command, *args], cwd=directory, env=env, text=True))

        manifest = cli("ingest", str(root / "examples/documents.jsonl"))
        assert manifest["documents"] == 6
        result = cli("search", "exemption certificate")
        assert "kb:certificate" in {h["document_id"] for h in result["hits"]}
        assert cli("answer", "exemption certificate")["answer"]["insufficient_evidence"]
        aggregate = directory / "aggregate.json"
        aggregate.write_text(
            json.dumps(
                {"query": "count cases", "filters": {"source": "case"}, "aggregate": {"operation": "count"}}
            )
        )
        assert cli("search", "--request", str(aggregate))["aggregates"][0]["value"] == 2
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env["SU_SMOKE_TOKEN"] = "local-smoke-only"
        process = subprocess.Popen(
            [*command, "serve", "--port", str(port), "--token-env", "SU_SMOKE_TOKEN"],
            cwd=directory,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
                for _ in range(100):
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    if process.poll() is not None:
                        raise RuntimeError("API exited during startup")
                    time.sleep(0.05)
                else:
                    raise RuntimeError("API startup timeout")
                assert client.post("/search", json={"query": "certificate"}).status_code == 401
                response = client.post(
                    "/search",
                    headers={"Authorization": "Bearer local-smoke-only"},
                    json={"query": "exemption certificate"},
                )
                response.raise_for_status()
                assert response.json()["hits"]
        finally:
            process.terminate()
            process.wait(timeout=10)

        async def protocol():
            params = StdioServerParameters(
                command=sys.executable, args=command[1:] + ["mcp"], env=env, cwd=str(directory)
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool("search", {"query": "exemption certificate"})
                    assert not result.isError

        asyncio.run(protocol())
        assert cli("delete", "case:certificate")["documents"] == 5
        print(
            json.dumps(
                {
                    "clean_wheel": "passed",
                    "cli": "passed",
                    "http_api": "passed",
                    "mcp_stdio": "passed",
                    "documents": 6,
                }
            )
        )


if __name__ == "__main__":
    main()
