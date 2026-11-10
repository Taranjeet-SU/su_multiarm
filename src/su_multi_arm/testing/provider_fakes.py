"""Provider fakes for contract and parity tests (plan §6.1). Synthetic only; no real model.

* ``FakeChatServer`` — a loopback OpenAI-compatible ``/v1/chat/completions`` server on a thread
  (stdlib ``http.server``). It answers the generator deterministically: an ``Answer`` citing the first
  evidence id it was given (``insufficient_evidence`` when none), as one JSON reply or, for
  ``stream: true``, as server-sent ``data:`` chunks with a usage chunk and ``[DONE]``. Because it is a
  real HTTP endpoint, every serving transport (including an MCP stdio subprocess) can use the same
  fake model through ``base_url``.
* ``openai_sse`` / ``anthropic_sse`` — event-stream bodies in the providers' documented shapes for
  ``httpx.MockTransport`` tests.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def synthetic_answer(payload: dict[str, Any]) -> dict[str, Any]:
    evidence = payload.get("evidence") or []
    if not evidence:
        return {"answer": "No evidence was supplied.", "claims": [], "citations": [], "insufficient_evidence": True}
    first = evidence[0]["id"]
    return {
        "answer": f"Synthetic answer grounded in {first}.",
        "claims": [{"text": f"Grounded in {first}", "kind": "narrative", "evidence_ids": [first]}],
        "citations": [first],
        "insufficient_evidence": False,
    }


def chunks(text: str, size: int = 12) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def openai_sse(text: str, *, finish: str | None = "stop", usage: dict[str, Any] | None = None, done: bool = True, model: str = "fake-model", size: int = 12) -> bytes:
    lines = [f"data: {json.dumps({'model': model, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}}]})}\n\n"]
    for part in chunks(text, size):
        lines.append(f"data: {json.dumps({'model': model, 'choices': [{'index': 0, 'delta': {'content': part}, 'finish_reason': None}]})}\n\n")
    if finish is not None:
        lines.append(f"data: {json.dumps({'model': model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]})}\n\n")
    if usage is not None:
        lines.append(f"data: {json.dumps({'model': model, 'choices': [], 'usage': usage})}\n\n")
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def anthropic_sse(
    text: str | None = None,
    *,
    tool: tuple[str, dict[str, Any]] | None = None,
    stop: str | None = "end_turn",
    usage_in: dict[str, Any] | None = None,
    output_tokens: int = 5,
    message_stop: bool = True,
    size: int = 12,
) -> bytes:
    def event(name: str, data: dict[str, Any]) -> str:
        return f"event: {name}\ndata: {json.dumps(data)}\n\n"

    out = [event("message_start", {"type": "message_start", "message": {"model": "claude-sonnet-4-5-20250929", "content": [], "usage": usage_in or {"input_tokens": 10, "output_tokens": 1}}})]
    out.append(event("ping", {"type": "ping"}))
    if text is not None:
        out.append(event("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}))
        for part in chunks(text, size):
            out.append(event("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": part}}))
        out.append(event("content_block_stop", {"type": "content_block_stop", "index": 0}))
    if tool is not None:
        name, payload = tool
        out.append(event("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_1", "name": name, "input": {}}}))
        for part in chunks(json.dumps(payload), size):
            out.append(event("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": part}}))
        out.append(event("content_block_stop", {"type": "content_block_stop", "index": 0}))
    out.append(event("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": output_tokens}}))
    if message_stop:
        out.append(event("message_stop", {"type": "message_stop"}))
    return "".join(out).encode()


class FakeChatServer:
    """Loopback OpenAI-compatible chat server. ``delay`` sleeps between streamed chunks;
    ``chunks_sent`` counts streamed content chunks (cancellation tests)."""

    def __init__(self, delay: float = 0.0, chunk_size: int = 12):
        self.delay, self.chunk_size = delay, chunk_size
        self.requests: list[dict[str, Any]] = []
        self.chunks_sent = 0
        self.disconnected = threading.Event()
        self.answer: Any = synthetic_answer  # payload -> reply object (tests may return a schema-violating one)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # quiet
                return

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                fake.requests.append(body)
                payload = json.loads(body["messages"][-1]["content"])
                text = json.dumps(fake.answer(payload))
                usage = {"prompt_tokens": 40, "completion_tokens": len(text) // 4}
                if not body.get("stream"):
                    reply = json.dumps({"model": "fake-model", "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}], "usage": usage}).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(reply)))
                    self.end_headers()
                    self.wfile.write(reply)
                    return
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("connection", "close")
                self.end_headers()
                try:
                    for part in chunks(text, fake.chunk_size):
                        if fake.delay:
                            time.sleep(fake.delay)
                        self.wfile.write(f"data: {json.dumps({'model': 'fake-model', 'choices': [{'index': 0, 'delta': {'content': part}, 'finish_reason': None}]})}\n\n".encode())
                        self.wfile.flush()
                        fake.chunks_sent += 1
                    tail = openai_sse("", usage=usage).decode().split("\n\n", 1)[1]
                    self.wfile.write(tail.encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    fake.disconnected.set()
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="fake-chat", daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def role(self, streaming: bool = True) -> dict[str, Any]:
        return {"provider": "openai_compatible", "model": "fake-model", "base_url": self.base_url, "max_tokens": 512, "capabilities": {"streaming": streaming}}

    def __enter__(self) -> "FakeChatServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
