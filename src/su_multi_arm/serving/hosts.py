"""Host allow-list and Origin check for the HTTP app (addendum A2 §4.4.2, review finding B10).

A browser UI on a loopback port is reachable by any web page through DNS rebinding (an attacker's
name resolving to 127.0.0.1): the page's requests then carry ``Host: attacker.example``. When the
UI is enabled the ``HostGuard`` therefore admits only these Host names, exact match, port ignored:
``127.0.0.1``, ``localhost``, ``[::1]``, the configured bind host (when it is not a wildcard) and
``serving.allowed_hosts``. Any other Host gets **421 Misdirected Request**.

Cross-origin writes are refused as well: a request with an ``Origin`` header that is not the
server's own ``scheme://host[:port]`` gets **403** on every non-GET/HEAD route and on
``/v2/answer/stream``. Requests without ``Origin`` (curl, server-side clients) are unaffected. An
``Origin`` on a host named in ``serving.allowed_hosts`` (a LAN forwarder) is accepted on any port,
because a forwarder may rewrite the Host header. The Host match ignores the port.

The guard also adds ``X-Content-Type-Options: nosniff`` to every ``/v2/*`` response (N12); that
header is applied whether or not the Host allow-list is active.
"""

from __future__ import annotations

from typing import Iterable
from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})
WILDCARD_BINDS = frozenset({"0.0.0.0", "::", "[::]", ""})
ORIGIN_CHECKED_GET = frozenset({"/v2/answer/stream"})


def host_of(value: str) -> str:
    """The host part of a ``Host`` header (port removed, lower case, IPv6 kept in brackets)."""
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[: end + 1] if end > 0 else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def bind_host(bind: str) -> str:
    host = bind.rsplit(":", 1)[0] if ":" in bind else bind
    return host.lower()


def allowed_hosts(bind: str, extra: Iterable[str] = ()) -> frozenset[str]:
    hosts = set(LOOPBACK_HOSTS) | {h.lower() for h in extra}
    host = bind_host(bind)
    if host not in WILDCARD_BINDS:
        hosts.add(host)
        if ":" in host and not host.startswith("["):
            hosts.add(f"[{host}]")
    return frozenset(hosts)


def guard_hosts(bind: str, extra: Iterable[str] = (), *, browser: bool = False, mcp_on_http: bool = False) -> frozenset[str] | None:
    """The Host allow-list to enforce, or ``None`` (guard off). The guard is on when a browser surface
    is served (``browser``), when MCP is mounted on the HTTP app (``mcp_on_http``), or when
    ``serving.allowed_hosts`` names hosts (an API-only process behind a LAN forwarder)."""
    extra = list(extra)
    if browser or mcp_on_http or extra:
        return allowed_hosts(bind, extra)
    return None


class HostGuard:
    """Pure ASGI middleware (streaming-safe: it never buffers a body)."""

    def __init__(self, app, hosts: frozenset[str] | None, origin_hosts: Iterable[str] = ()):
        self.app = app
        self.hosts = hosts  # None: Host allow-list and Origin check off (nosniff still applied)
        # Origins on these hosts (``serving.allowed_hosts``: a LAN forwarder's name) are accepted even
        # when the forwarder rewrites the Host header; the loopback defaults are not in this set.
        self.origin_hosts = frozenset(h.lower() for h in origin_hosts)

    def _trusted_origin(self, origin: str) -> bool:
        try:
            parts = urlsplit(origin)
        except ValueError:
            return False
        host = (parts.hostname or "").lower()
        return parts.scheme in {"http", "https"} and bool(host) and (host in self.origin_hosts or f"[{host}]" in self.origin_hosts)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if self.hosts is not None:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            host_header = headers.get("host", "")
            if host_of(host_header) not in self.hosts:
                await _reject(send, 421, "host not allowed")
                return
            origin = headers.get("origin")
            method = scope.get("method", "GET").upper()
            if origin is not None and (method not in {"GET", "HEAD", "OPTIONS"} or path in ORIGIN_CHECKED_GET):
                if not _same_origin(origin, scope.get("scheme", "http"), host_header) and not self._trusted_origin(origin):
                    await _reject(send, 403, "cross-origin request refused")
                    return
        if not path.startswith("/v2/"):
            await self.app(scope, receive, send)
            return

        async def with_nosniff(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-content-type-options"]
                message = {**message, "headers": [*headers, (b"x-content-type-options", b"nosniff")]}
            await send(message)

        await self.app(scope, receive, with_nosniff)


def _same_origin(origin: str, scheme: str, host_header: str) -> bool:
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return False
    default = {"http": "80", "https": "443"}
    own = urlsplit(f"{scheme}://{host_header}")
    try:
        origin_port, own_port = parts.port, own.port
    except ValueError:
        return False
    return (
        parts.scheme == scheme
        and (parts.hostname or "").lower() == (own.hostname or "").lower()
        and str(origin_port or default[parts.scheme]) == str(own_port or default.get(scheme, ""))
    )


async def _reject(send, status: int, detail: str) -> None:
    body = ('{"detail":"%s"}' % detail).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"x-content-type-options", b"nosniff")],
        }
    )
    await send({"type": "http.response.body", "body": body})
