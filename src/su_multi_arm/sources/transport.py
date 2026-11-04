"""Allow-list transport for read-only sources (addendum A1 §3.1.1).

Every request is checked against an exact table of ``(method, anchored path)`` rows **before any
I/O**: the path (percent-decoded once; a remaining ``%`` is refused), the query parameters (a per-row
allow-list with value checks) and the JSON body (closed key sets; scripts refused anywhere). Anything
else raises ``SourceRequestRefused``. ``{idx}`` must be an index name returned by discovery; only
discovery and ``_count`` accept wildcard expressions. ``DELETE`` exists only to close a scroll or a
PIT that **this transport opened** and the caller's cursor lists, and never ``_all``. Redirects are
not followed. There is no retry: an HTTP failure becomes a typed ``SourceError``.

Credentials come from environment variables named in the config and live only in the HTTP client's
default headers; they are never part of an error message, a request record or ``repr``.

| # | Method | Path |
|---|---|---|
| 1 | GET | ``/_resolve/index/{expr}`` |
| 2 | GET | ``/{expr}/_alias`` |
| 3 | GET | ``/{idx}/_mapping`` |
| 4 | GET, POST | ``/{expr}/_count`` |
| 5 | GET, POST | ``/{idx}/_search`` |
| 6 | POST | ``/_search/scroll`` |
| 7 | DELETE | ``/_search/scroll`` |
| 8 | POST | ``/{idx}/_pit``, OpenSearch ``/{idx}/_search/point_in_time`` |
| 9 | DELETE | ``/_pit``, OpenSearch ``/_search/point_in_time`` |
| 10 | GET | ``/`` |
| 11 | GET, POST | ``/_security/user/_has_privileges``; OpenSearch GET ``/_plugins/_security/api/account`` |
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal, Mapping
from urllib.parse import unquote, urlsplit

import httpx

from ..core.config_sources import INDEX_ITEM, duration_seconds
from .errors import (
    SourceAuthError,
    SourceCursorExpired,
    SourceError,
    SourceForbidden,
    SourceQueryRejected,
    SourceRateLimited,
    SourceRequestRefused,
    SourceSchemaDrift,
    SourceTimeout,
    SourceUnavailable,
)

IDX = r"[a-z0-9][a-z0-9._-]{0,254}"
EXPR = rf"{INDEX_ITEM}(?:,{INDEX_ITEM})*"
SAFE_PATH = re.compile(r"^/[A-Za-z0-9_.,*/-]*$")
FIELD_LIST = re.compile(r"^[A-Za-z0-9@_.*-]+(,[A-Za-z0-9@_.*-]+)*$")
FILTER_PATH = re.compile(r"^-?[A-Za-z0-9_.*]+(,-?[A-Za-z0-9_.*]+)*$")
MAX_PAGE = 10_000
BodyKind = Literal[
    "none", "search", "count", "scroll_continue", "scroll_close", "pit_close_es", "pit_close_os", "has_privileges", "optional_none"
]
SEARCH_KEYS = frozenset({"query", "sort", "size", "search_after", "slice", "_source", "track_total_hits"})
COUNT_KEYS = frozenset({"query"})
# Refused anywhere in a search or count body, at any depth.
FORBIDDEN_KEYS = frozenset({"runtime_mappings", "_update", "_delete", "stored_fields", "docvalue_fields", "aggs", "aggregations"})


@dataclass(frozen=True)
class Route:
    row: int
    methods: frozenset[str]
    pattern: re.Pattern[str]
    params: frozenset[str]
    body: BodyKind
    purpose: str


def _route(row, methods, pattern, params, body, purpose) -> Route:
    return Route(row, frozenset(methods), re.compile(rf"^{pattern}$"), frozenset(params), body, purpose)


ROUTES: tuple[Route, ...] = (
    _route(1, {"GET"}, rf"/_resolve/index/(?P<expr>{EXPR})", {"expand_wildcards"}, "none", "discovery"),
    _route(2, {"GET"}, rf"/(?P<expr>{EXPR})/_alias", {"expand_wildcards"}, "none", "discovery fallback"),
    _route(3, {"GET"}, rf"/(?P<idx>{IDX})/_mapping", set(), "none", "mapping"),
    _route(4, {"GET", "POST"}, rf"/(?P<expr>{EXPR})/_count", {"request_cache"}, "count", "count"),
    _route(
        5, {"GET", "POST"}, rf"/(?P<idx>{IDX})/_search",
        {"scroll", "size", "filter_path", "_source_includes", "request_cache"}, "search", "search",
    ),
    _route(6, {"POST"}, r"/_search/scroll", {"filter_path"}, "scroll_continue", "scroll continue"),
    _route(7, {"DELETE"}, r"/_search/scroll", set(), "scroll_close", "close scroll"),
    _route(8, {"POST"}, rf"/(?P<idx>{IDX})/_pit", {"keep_alive"}, "optional_none", "open PIT"),
    _route(8, {"POST"}, rf"/(?P<idx>{IDX})/_search/point_in_time", {"keep_alive"}, "optional_none", "open PIT"),
    _route(9, {"DELETE"}, r"/_pit", set(), "pit_close_es", "close PIT"),
    _route(9, {"DELETE"}, r"/_search/point_in_time", set(), "pit_close_os", "close PIT"),
    _route(10, {"GET"}, r"/", set(), "none", "version"),
    _route(11, {"GET", "POST"}, r"/_security/user/_has_privileges", set(), "has_privileges", "self-check"),
    _route(11, {"GET"}, r"/_plugins/_security/api/account", set(), "none", "self-check"),
)


# Content-free path templates per row, for request logs (no index name, id or host).
ROW_TEMPLATES = {
    1: "/_resolve/index/{expr}",
    2: "/{expr}/_alias",
    3: "/{idx}/_mapping",
    4: "/{expr}/_count",
    5: "/{idx}/_search",
    6: "/_search/scroll",
    7: "/_search/scroll",
    8: "/{idx}/_pit",
    9: "/_pit",
    10: "/",
    11: "/_security/user/_has_privileges",
}


@dataclass(frozen=True)
class Checked:
    route: Route
    path: str
    params: dict[str, str]
    body: Any
    close_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RequestRecord:
    """Method, path, status and timing of one sent request. Never a body, header or host."""

    method: str
    path: str
    row: int
    status: int | None
    ms: float
    bytes: int


class AllowList:
    """The pure check. ``known_indices`` and ``own_ids`` are owned by the transport."""

    def __init__(self, keepalive_max_seconds: float, disallowed_rows: Iterable[int] = ()):
        self.keepalive_max_seconds = keepalive_max_seconds
        # Rows a deployment forbids on top of the table (``sources.<n>.disallow``): refused before I/O.
        self.disallowed_rows = frozenset(disallowed_rows)

    def check(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None,
        body: Any,
        *,
        known_indices: Iterable[str],
        own_ids: Iterable[str],
        close_ids: Iterable[str] | None = None,
    ) -> Checked:
        method = (method or "").upper()
        canonical = self._canonical_path(method, path)
        for route in ROUTES:
            match = route.pattern.fullmatch(canonical)
            if match is None or method not in route.methods:
                continue
            if route.row in self.disallowed_rows:
                raise SourceRequestRefused(method, canonical, f"row {route.row} ({route.purpose}) is disallowed by this source's configuration")
            groups = match.groupdict()
            if "idx" in groups and groups["idx"] not in set(known_indices):
                raise SourceRequestRefused(method, canonical, f"index {groups['idx']!r} was not returned by discovery")
            if "expr" in groups:
                self._check_expression(method, canonical, groups["expr"])
            clean_params = self._check_params(method, canonical, route, params or {})
            ids = self._check_body(method, canonical, route, body, set(own_ids), close_ids)
            return Checked(route, canonical, clean_params, body, ids)
        if any(route.pattern.fullmatch(canonical) for route in ROUTES):
            raise SourceRequestRefused(method, canonical, f"method {method} is not allowed on this path")
        raise SourceRequestRefused(method, canonical, "path is not on the allow-list")

    # --- path ----------------------------------------------------------------------------------

    @staticmethod
    def _canonical_path(method: str, path: str) -> str:
        if not isinstance(path, str) or not path.startswith("/"):
            raise SourceRequestRefused(method, str(path), "path must be absolute")
        if any(ch in path for ch in "?#\\") or any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in path):
            raise SourceRequestRefused(method, path, "query strings, fragments, whitespace and control characters are not allowed in the path")
        decoded = unquote(path)
        if "%" in decoded:
            raise SourceRequestRefused(method, path, "double percent-encoding is refused")
        if "//" in decoded or any(part in {".", ".."} for part in decoded.split("/")):
            raise SourceRequestRefused(method, decoded, "empty, '.' or '..' path segments are refused")
        if not SAFE_PATH.fullmatch(decoded):
            raise SourceRequestRefused(method, decoded, "path contains characters outside the allowed set")
        return decoded

    @staticmethod
    def _check_expression(method: str, path: str, expr: str) -> None:
        for item in expr.split(","):
            if item.startswith(("_", "-", ".")) or "{" in item or "}" in item:
                raise SourceRequestRefused(method, path, f"index expression item {item!r} is not allowed")

    # --- params --------------------------------------------------------------------------------

    def _check_params(self, method: str, path: str, route: Route, params: Mapping[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, raw in params.items():
            if key not in route.params:
                raise SourceRequestRefused(method, path, f"query parameter {key!r} is not allowed on row {route.row}")
            value = str(raw).lower() if isinstance(raw, bool) else str(raw)
            if key in {"scroll", "keep_alive"}:
                self._check_keepalive(method, path, value)
            elif key == "size":
                if not value.isdigit() or int(value) > MAX_PAGE:
                    raise SourceRequestRefused(method, path, f"size must be an integer 0..{MAX_PAGE}")
            elif key == "filter_path":
                if not FILTER_PATH.fullmatch(value):
                    raise SourceRequestRefused(method, path, "filter_path value is not a field list")
            elif key == "_source_includes":
                if not FIELD_LIST.fullmatch(value):
                    raise SourceRequestRefused(method, path, "_source_includes value is not a field list")
            elif key == "request_cache":
                if value not in {"true", "false"}:
                    raise SourceRequestRefused(method, path, "request_cache must be true or false")
            elif key == "expand_wildcards":
                if value != "open":
                    raise SourceRequestRefused(method, path, "expand_wildcards may only be 'open'")
            out[key] = value
        if route.row == 8 and "keep_alive" not in out:
            raise SourceRequestRefused(method, path, "opening a PIT needs a bounded keep_alive")
        return out

    def _check_keepalive(self, method: str, path: str, value: Any) -> None:
        try:
            seconds = duration_seconds(str(value))
        except ValueError:
            raise SourceRequestRefused(method, path, f"keepalive {value!r} is not a bounded duration") from None
        if seconds > self.keepalive_max_seconds:
            raise SourceRequestRefused(method, path, f"keepalive {value} exceeds keepalive_max ({self.keepalive_max_seconds:g}s)")

    # --- bodies --------------------------------------------------------------------------------

    def _check_body(self, method, path, route: Route, body, own: set[str], close_ids) -> tuple[str, ...]:
        kind = route.body
        if kind in {"none", "optional_none"}:
            if body not in (None, {}):
                raise SourceRequestRefused(method, path, "this request takes no body")
            return ()
        if not isinstance(body, dict):
            if kind == "has_privileges" and body is None:
                return ()
            if kind in {"search", "count"} and body is None:
                return ()
            raise SourceRequestRefused(method, path, "the body must be a JSON object")
        if kind in {"search", "count"}:
            allowed = SEARCH_KEYS if kind == "search" else COUNT_KEYS
            extra = sorted(set(body) - allowed)
            if extra:
                raise SourceRequestRefused(method, path, f"body keys {extra} are not allowed in a {kind} request")
            self._no_scripts(method, path, body)
            if kind == "search":
                size = body.get("size", 0)
                if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= MAX_PAGE:
                    raise SourceRequestRefused(method, path, f"size must be an integer 0..{MAX_PAGE}")
                self._check_source_filter(method, path, body.get("_source"))
            return ()
        if kind == "scroll_continue":
            if set(body) - {"scroll", "scroll_id"} or not isinstance(body.get("scroll_id"), str):
                raise SourceRequestRefused(method, path, "scroll continue takes exactly scroll and scroll_id")
            if body["scroll_id"] not in own:
                raise SourceRequestRefused(method, path, "scroll_id was not opened by this source")
            if "scroll" in body:
                self._check_keepalive(method, path, body["scroll"])
            return ()
        if kind == "has_privileges":
            if set(body) - {"cluster", "index"}:
                raise SourceRequestRefused(method, path, "_has_privileges takes only cluster and index")
            for entry in body.get("index", []):
                if not isinstance(entry, dict) or set(entry) - {"names", "privileges"}:
                    raise SourceRequestRefused(method, path, "_has_privileges index entries take names and privileges")
            return ()
        # close requests: ids must be ours and listed by the caller's cursor
        key, as_list = {"scroll_close": ("scroll_id", None), "pit_close_es": ("id", False), "pit_close_os": ("pit_id", True)}[kind]
        if set(body) != {key}:
            raise SourceRequestRefused(method, path, f"close takes exactly {key!r}")
        value = body[key]
        ids = [value] if isinstance(value, str) else value
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
            raise SourceRequestRefused(method, path, f"{key} must be a non-empty id or list of ids")
        if as_list is False and not isinstance(value, str):
            raise SourceRequestRefused(method, path, f"{key} must be a single id")
        if as_list is True and not isinstance(value, list):
            raise SourceRequestRefused(method, path, f"{key} must be a list of ids")
        if any(i == "_all" for i in ids):
            raise SourceRequestRefused(method, path, "closing _all is never allowed")
        listed = set(close_ids or ())
        if not set(ids) <= listed:
            raise SourceRequestRefused(method, path, "close ids are not listed in the cursor's opened_ids")
        if not set(ids) <= own:
            raise SourceRequestRefused(method, path, "close ids were not opened by this source")
        return tuple(ids)

    def _no_scripts(self, method: str, path: str, node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                lowered = str(key).lower()
                if "script" in lowered or lowered in FORBIDDEN_KEYS:
                    raise SourceRequestRefused(method, path, f"body key {key!r} is refused (scripts and write or aggregation operations)")
                self._no_scripts(method, path, value)
        elif isinstance(node, list):
            for item in node:
                self._no_scripts(method, path, item)

    @staticmethod
    def _check_source_filter(method: str, path: str, value: Any) -> None:
        if value is None or value is False:
            return
        items = value.get("includes") if isinstance(value, dict) else value
        if isinstance(value, dict) and set(value) - {"includes"}:
            raise SourceRequestRefused(method, path, "_source accepts only includes")
        if not isinstance(items, list) or not all(isinstance(i, str) and FIELD_LIST.fullmatch(i) for i in items):
            raise SourceRequestRefused(method, path, "_source must list field paths")


class RateLimiter:
    """Minimum interval between requests (``limits.requests_per_second``). A wait, never a retry."""

    def __init__(self, per_second: float, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.interval = 1.0 / per_second
        self.clock, self.sleep = clock, sleep
        self._next = 0.0

    def wait(self) -> None:
        now = self.clock()
        if now < self._next:
            self.sleep(self._next - now)
            now = self._next
        self._next = now + self.interval


class SourceTransport:
    """httpx client behind the allow-list. One instance per source; not shared across sources."""

    def __init__(
        self,
        base_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        verify: bool | str = True,
        max_response_bytes: int = 64 * 1024 * 1024,
        keepalive_max_seconds: float = 120.0,
        rate_limiter: RateLimiter | None = None,
        http_transport: httpx.BaseTransport | None = None,
        record_limit: int = 2000,
        disallowed_rows: Iterable[int] = (),
    ):
        parts = urlsplit(base_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise SourceUnavailable("the source URL must be an absolute http(s) URL", "connect")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise SourceAuthError("the source URL must not carry credentials, a query or a fragment; use the *_env credential variables", "connect")
        self.allow = AllowList(keepalive_max_seconds, disallowed_rows)
        self.known_indices: set[str] = set()
        self.own_ids: set[str] = set()
        self.records: deque[RequestRecord] = deque(maxlen=record_limit)
        self.sent = 0
        self.max_response_bytes = max_response_bytes
        self.rate_limiter = rate_limiter
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=dict(headers or {}),
            timeout=timeout,
            verify=verify,
            follow_redirects=False,
            transport=http_transport,
            trust_env=False,
        )

    def __repr__(self) -> str:  # never the URL or headers
        return f"SourceTransport(sent={self.sent}, open_ids={len(self.own_ids)})"

    def close(self) -> None:
        self._client.close()

    def request_log(self) -> list[dict[str, Any]]:
        """The sent requests as content-free rows: method, allow-list row, path template, status,
        milliseconds and response bytes (never an index name, body, header or host)."""
        return [
            {"method": r.method, "row": r.row, "path": ROW_TEMPLATES.get(r.row, "?"), "status": r.status, "ms": round(r.ms, 1), "bytes": r.bytes}
            for r in self.records
        ]

    def register_indices(self, names: Iterable[str]) -> None:
        self.known_indices.update(names)

    def check(self, method: str, path: str, params=None, body=None, close_ids=None) -> Checked:
        return self.allow.check(method, path, params, body, known_indices=self.known_indices, own_ids=self.own_ids, close_ids=close_ids)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
        close_ids: Iterable[str] | None = None,
        tolerate: tuple[int, ...] = (),
    ) -> tuple[int, Any]:
        """Check, then send. Returns ``(status, json)``; statuses in ``tolerate`` return instead of
        raising. Everything else outside 2xx raises a typed ``SourceError``."""
        checked = self.check(method, path, params, body, close_ids)
        operation = f"{method.upper()} {checked.path}"
        if self.rate_limiter is not None:
            self.rate_limiter.wait()
        started = time.perf_counter()
        status: int | None = None
        size = 0
        try:
            content = None if checked.body in (None,) else json.dumps(checked.body).encode()
            request_headers = {"content-type": "application/json"} if content is not None else None
            self.sent += 1
            with self._client.stream(method.upper(), checked.path, params=checked.params or None, content=content, headers=request_headers) as response:
                status = response.status_code
                chunks: list[bytes] = []
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > self.max_response_bytes:
                        raise SourceUnavailable(f"response exceeds max_response_bytes ({self.max_response_bytes})", operation)
                    chunks.append(chunk)
                raw = b"".join(chunks)
                retry_after = response.headers.get("retry-after")
        except httpx.TimeoutException:
            raise SourceTimeout("request timed out", operation) from None
        except httpx.TransportError as error:
            raise SourceUnavailable(f"transport error ({type(error).__name__})", operation) from None
        finally:
            self.records.append(RequestRecord(method.upper(), checked.path, checked.route.row, status, (time.perf_counter() - started) * 1000, size))
        payload = self._decode(raw, operation, status)
        if 300 <= status < 400:
            raise SourceUnavailable(f"HTTP {status} redirect is never followed", operation)
        if status in tolerate:
            return status, payload
        if not 200 <= status < 300:
            raise self._error(status, payload, operation, retry_after)
        self._track_ids(checked, payload)
        return status, payload

    @staticmethod
    def _decode(raw: bytes, operation: str, status: int) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            if 200 <= status < 300:
                raise SourceUnavailable("response is not JSON", operation) from None
            return None

    @staticmethod
    def _error(status: int, payload: Any, operation: str, retry_after: str | None) -> SourceError:
        error = payload.get("error") if isinstance(payload, dict) else None
        etype = error.get("type") if isinstance(error, dict) else None
        root = (error.get("root_cause") or [{}])[0].get("type") if isinstance(error, dict) and error.get("root_cause") else None
        kinds = {etype, root}
        label = f"HTTP {status}" + (f" ({etype})" if etype else "")
        if "search_context_missing_exception" in kinds or (status == 404 and "/_search/scroll" in operation):
            return SourceCursorExpired(f"{label}: cursor context is gone", operation)
        if status == 401:
            return SourceAuthError(label, operation)
        if status == 403:
            return SourceForbidden(label, operation)
        if status == 429:
            try:
                seconds = float(retry_after) if retry_after else None
            except ValueError:
                seconds = None
            return SourceRateLimited(label, operation, retry_after=seconds)
        if "index_not_found_exception" in kinds:
            return SourceSchemaDrift(f"{label}: index not found", operation)
        if status == 400:
            return SourceQueryRejected(label, operation)
        return SourceUnavailable(label, operation)

    def _track_ids(self, checked: Checked, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        if checked.route.row in {5, 6} and isinstance(payload.get("_scroll_id"), str):
            self.own_ids.add(payload["_scroll_id"])
        if checked.route.row == 8:
            pit = payload.get("id") or payload.get("pit_id")
            if isinstance(pit, str):
                self.own_ids.add(pit)
        if checked.route.row in {7, 9}:
            self.own_ids.difference_update(checked.close_ids)
