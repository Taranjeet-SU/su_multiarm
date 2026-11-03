"""HTTP transport for the Elasticsearch adapter over the official ``elasticsearch`` client
(optional extra ``elasticsearch``).

Same contract as the OpenSearch transport: every failed round trip is a
``_lucene_common.TransportError`` with the HTTP status (``None`` when no reply arrived), the
engine's error type and reason; the adapter turns those into ``BackendError``s naming the
operation. No retries (``max_retries=0``).
"""

from __future__ import annotations

from typing import Any

from .._lucene_common import TransportError, error_reason

JSON_HEADERS = {"accept": "application/json", "content-type": "application/json"}
NDJSON_HEADERS = {"accept": "application/json", "content-type": "application/x-ndjson"}


class ElasticsearchTransport:
    def __init__(self, hosts: list[str], timeout_seconds: float):
        try:
            import elasticsearch
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise ImportError("the elasticsearch backend needs the 'elasticsearch' extra: pip install 'su-multi-arm[elasticsearch]'") from exc
        self._exc = elasticsearch
        self.hosts = list(hosts)
        self._client = elasticsearch.Elasticsearch(self.hosts, request_timeout=timeout_seconds, max_retries=0, retry_on_timeout=False)

    def request(self, method: str, path: str, params: dict[str, Any] | None = None, body: Any = None) -> Any:
        headers = NDJSON_HEADERS if isinstance(body, str) else JSON_HEADERS
        try:
            response = self._client.perform_request(method, path, params=params, headers=headers, body=body)
        except self._exc.ApiError as exc:
            error_type, reason = error_reason(exc.body if isinstance(exc.body, dict) else None)
            if error_type == "error" and not reason:
                error_type, reason = str(exc.message), str(exc.body)[:500]
            raise TransportError(exc.meta.status, error_type, reason, exc.body) from exc
        except self._exc.TransportError as exc:  # connection refused, timeouts, TLS, product check
            raise TransportError(None, type(exc).__name__, str(exc)) from exc
        return response.body

    def close(self) -> None:
        self._client.close()
