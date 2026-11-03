"""Server-sent events for ``POST /v2/answer/stream``.

Each ``AnswerEvent`` is one SSE message: ``id`` = the event's ``seq``, ``event`` = its name
(``decision``, ``arm_result``, ``review_round``, ``evidence``, ``token``, ``contract``, ``done``,
``error``), ``data`` = the event as JSON. A comment line (``: heartbeat``) is sent whenever no event
arrived for ``heartbeat_seconds``. ``error`` messages are replaced by a generic text on this
surface (no raw provider/source/config details reach unaudited clients); the category stays.

Client disconnect cancels the run and its budget: ``SseResponse`` always watches the ASGI
``receive`` channel for ``http.disconnect`` (independent of the server's ASGI spec version) and
cancels the body iterator, whose ``finally`` calls ``AnswerStream.cancel()``.
"""

from __future__ import annotations

import json
from functools import partial
from typing import Any, AsyncIterator

import anyio
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from ..runtime.events import AnswerStream, ErrorEvent

PUBLIC_ERROR = "The answer could not be completed; check configuration and server logs"
SAFE_ERROR_CATEGORIES = frozenset({"cancelled", "budget_exhausted", "deadline_exceeded"})


def public_error(event: ErrorEvent) -> ErrorEvent:
    message = event.message if event.category in SAFE_ERROR_CATEGORIES else PUBLIC_ERROR
    return event.model_copy(update={"message": message})


def format_event(event: Any) -> str:
    if isinstance(event, ErrorEvent):
        event = public_error(event)
    data = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.seq}\nevent: {event.event}\ndata: {data}\n\n"


HEARTBEAT = ": heartbeat\n\n"


async def event_source(stream: AnswerStream, heartbeat_seconds: float) -> AsyncIterator[str]:
    """SSE text for a stream; cancels the run when the consumer goes away (disconnect, error)."""
    try:
        while True:
            event = await anyio.to_thread.run_sync(stream.next_event, heartbeat_seconds, abandon_on_cancel=True)
            if event is None:
                yield HEARTBEAT
                continue
            yield format_event(event)
            if stream.finished:
                return
    finally:
        stream.cancel("client_disconnected")


class SseResponse(StreamingResponse):
    """``text/event-stream`` response that always listens for the client's disconnect."""

    media_type = "text/event-stream"

    def __init__(self, content: AsyncIterator[str], **kwargs: Any):
        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **(kwargs.pop("headers", None) or {})}
        super().__init__(content, media_type=self.media_type, headers=headers, **kwargs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async with anyio.create_task_group() as group:

            async def run(func) -> None:
                await func()
                group.cancel_scope.cancel()

            group.start_soon(run, partial(self.stream_response, send))
            await run(partial(self.listen_for_disconnect, receive))
