"""HTTP transport for the OpenSearch adapter over ``opensearch-py`` (optional extra ``opensearch``).

Every failed round trip becomes a ``TransportError`` carrying the HTTP status (``None`` when no reply
arrived), the engine's error type and reason; the adapter turns those into ``BackendError`` with the
operation name. No retries: a transient failure surfaces as a typed error instead of a hidden
re-attempt (``max_retries=0``).
"""

from __future__ import annotations

from typing import Any

from .._lucene_common import TransportError, error_reason


class OpenSearchTransport:
    def __init__(self, hosts: list[str], timeout_seconds: float):
        try:
            from opensearchpy import OpenSearch
            from opensearchpy import exceptions as os_exc
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise ImportError("the opensearch backend needs the 'opensearch' extra: pip install 'su-multi-arm[opensearch]'") from exc
        self._exc = os_exc
        self._client = OpenSearch(
            hosts=hosts,
            timeout=timeout_seconds,
            max_retries=0,
            retry_on_timeout=False,
            http_compress=False,
        )

    def request(self, method: str, path: str, params: dict[str, Any] | None = None, body: Any = None) -> Any:
        headers = {"content-type": "application/x-ndjson"} if isinstance(body, str) else None
        try:
            return self._client.transport.perform_request(method, path, params=params, body=body, headers=headers)
        except self._exc.ConnectionError as exc:  # includes timeouts
            raise TransportError(None, type(exc).__name__, str(getattr(exc, "info", None) or getattr(exc, "error", exc))) from exc
        except self._exc.TransportError as exc:
            status = exc.status_code if isinstance(exc.status_code, int) else None
            error_type, reason = error_reason(exc.info if isinstance(exc.info, dict) else None)
            if error_type == "error" and not reason:
                error_type, reason = str(exc.error), str(exc.info)[:500]
            raise TransportError(status, error_type, reason, exc.info) from exc

    def close(self) -> None:
        self._client.close()
