"""Authentication hook for the serving surfaces (plan §3.15; execution-log default #6).

A ``PrincipalResolver`` turns request headers into a ``runtime.acl.Principal`` or raises
``Unauthenticated``. The core ships two resolvers and nothing else:

* ``LocalResolver`` — no credentials; for a loopback bind only (the CLI and ``serving.http``
  validation refuse a non-local bind without auth). The principal is ``anonymous`` (the same
  identity the CLI and the Python API use, so sessions created there are usable here) with the
  ``search``, ``answer`` and ``tools`` scopes; no ``actions`` scope.
* ``BearerTokenResolver`` — one shared token read from the environment variable named by
  ``serving.http.token_env`` (or ``serve --token-env``); constant-time comparison. The principal is
  ``bearer`` with ``search``, ``answer``, ``tools`` and ``actions`` scopes (an action still needs
  ``serving.actions.allow``).

OAuth2, OIDC, mTLS or per-client API keys are **outside the core**: implement ``resolve(headers)``
in your own code and pass it as ``create_app(..., principal_resolver=...)`` /
``create_mcp(..., principal_resolver=...)``. Configuration never names a resolver class (no code
loading from YAML).
"""

from __future__ import annotations

import secrets
from typing import Mapping, Protocol, runtime_checkable

from ..runtime.acl import Principal

LOCAL_PRINCIPAL = Principal(id="anonymous", scopes=["search", "answer", "tools"])
BEARER_PRINCIPAL = Principal(id="bearer", scopes=["search", "answer", "tools", "actions"])


class Unauthenticated(Exception):
    """Missing or invalid credentials (HTTP 401). The message never echoes the credential."""


@runtime_checkable
class PrincipalResolver(Protocol):
    def resolve(self, headers: Mapping[str, str]) -> Principal: ...


class LocalResolver:
    def __init__(self, principal: Principal = LOCAL_PRINCIPAL):
        self.principal = principal

    def resolve(self, headers: Mapping[str, str]) -> Principal:
        return self.principal


class BearerTokenResolver:
    def __init__(self, token: str, principal: Principal = BEARER_PRINCIPAL):
        if not token:
            raise ValueError("bearer token must not be empty")
        self._expected = "Bearer " + token
        self.principal = principal

    def resolve(self, headers: Mapping[str, str]) -> Principal:
        supplied = headers.get("authorization") or ""
        if not secrets.compare_digest(supplied.encode(), self._expected.encode()):
            raise Unauthenticated("invalid bearer token")
        return self.principal


def default_resolver(config, token_env: str | None = None) -> PrincipalResolver:
    """``token_env`` (CLI) or ``serving.http.auth: bearer_env`` selects the bearer resolver; a missing
    variable is an error (fail closed), never an open server."""
    from ..core.config import secret

    http = config.serving.http
    name = token_env or (http.token_env if http.auth == "bearer_env" else None)
    if name:
        return BearerTokenResolver(secret(name) or "")
    return LocalResolver()
