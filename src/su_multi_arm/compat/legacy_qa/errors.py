"""Legacy error shape: HTTP status plus ``{"detail": <string | list | {error, reason}>}``.

Engine failures reuse the package's HTTP mapping (``serving.http.http_error``: the same statuses and
the same generic, non-leaking texts); stream failures map the ``ErrorEvent`` category to a status for
the legacy ``error {status, detail}`` event. A legacy client retries through the non-streaming route
only on status 500, so request-shaped failures never carry 500.

Classification is by failure category (``runtime.events.error_category``), so the same failure gets
the same status on every route (``/api/qa``, ``/api/qa/stream``, ``/api/search``, ``/source``):

* request-shaped (``RequestRejected``, validation errors, adapter refusals): 422 with a detail;
* a model reply that does not parse (``schema_violation``) or any provider failure: 502;
* backend/state failures: 503; budget 429; deadline 504; cancelled 499 (as ``serving.http``);
* anything unclassified (an adapter or engine bug: ``KeyError``, ``TypeError``, ...): **500**,
  logged with its traceback and the request id; the detail names the request id only.
"""

from __future__ import annotations

import logging
from typing import Any

from ...serving.sse import public_error

LOGGER = logging.getLogger("su_multi_arm.compat.legacy_qa")
INTERNAL = "Internal error while answering; see the server log"
UPSTREAM = "Model provider request failed; check server logs"
GENERIC = "Request could not be completed; check configuration and server logs"

STREAM_STATUS = {
    "cancelled": 499,
    "budget_exhausted": 429,
    "deadline_exceeded": 504,
    "session_not_found": 404,
    "access_denied": 403,
    "rejected": 422,
    "invalid_request": 422,
    "schema_violation": 502,
    "unavailable": 503,
    "internal": 500,
}


class LegacyError(Exception):
    """A request the adapter refuses before reaching an engine."""

    def __init__(self, status: int, detail: Any):
        super().__init__(str(detail))
        self.status, self.detail = status, detail


def internal_detail(request_id: str | None) -> str:
    return f"{INTERNAL} (request id {request_id})" if request_id else INTERNAL


def engine_error(exc: BaseException, request_id: str | None = None, route: str | None = None) -> tuple[int, Any]:
    """Status and detail for an exception raised while serving a request (never leaks its text
    except for request-shaped refusals). Unclassified failures are logged with their traceback."""
    from fastapi import HTTPException
    from pydantic import ValidationError

    from ...runtime.events import error_category
    from ...serving.http import http_error

    if isinstance(exc, LegacyError):
        return exc.status, exc.detail
    if isinstance(exc, HTTPException):
        return exc.status_code, exc.detail
    if isinstance(exc, ValidationError):
        mapped = http_error(exc)
        return mapped.status_code, mapped.detail
    category = error_category(exc)
    if category == "internal":
        LOGGER.error("legacy %s request %s failed: %s", route or "adapter", request_id, type(exc).__name__, exc_info=exc)
        return 500, internal_detail(request_id)
    if category == "schema_violation":
        LOGGER.warning("legacy %s request %s: 502 model reply did not parse (%s)", route or "adapter", request_id, getattr(exc, "stage", None) or "-")
        return 502, UPSTREAM
    if category == "invalid_request":  # a bare ValueError from an engine call: request-shaped, logged
        LOGGER.warning("legacy %s request %s: 422 %s (request-shaped engine refusal)", route or "adapter", request_id, type(exc).__name__)
        return 422, GENERIC
    mapped = http_error(exc)
    return mapped.status_code, mapped.detail


def stream_status(category: str) -> int:
    if category.startswith("provider_"):
        return 502
    return STREAM_STATUS.get(category, 500)


def stream_error(event: Any) -> dict[str, Any]:
    """``error {status, detail}`` for a package ``ErrorEvent`` (public message only)."""
    safe = public_error(event)
    return {"status": stream_status(event.category), "detail": {"error": event.category, "reason": safe.message}}


def internal_stream_error(request_id: str | None) -> dict[str, Any]:
    """The terminal ``error`` event for an adapter failure after the stream started."""
    return {"status": 500, "detail": {"error": "internal", "reason": internal_detail(request_id)}}
