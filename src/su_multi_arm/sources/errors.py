"""Typed source errors (addendum A1 §3.1). A forbidden or unavailable operation is one of these and
never an empty day. Messages carry the method and the path only: never a header, a credential, a
host or a request body."""

from __future__ import annotations

from ..core.errors import SuError


class SourceError(SuError, RuntimeError):
    """Base class. ``operation`` is ``"<METHOD> <path>"`` or a logical step such as ``discover``."""

    def __init__(self, message: str, operation: str | None = None):
        self.operation = operation
        super().__init__(f"{operation}: {message}" if operation else message)


class SourceRequestRefused(SourceError):
    """The transport refused a request that is not on the allow-list. Raised **before any I/O**."""

    def __init__(self, method: str, path: str, reason: str):
        self.method, self.path, self.reason = method, path, reason
        super().__init__(f"refused by the source allow-list: {reason}", f"{method} {path}")


class SourceAuthError(SourceError):
    """HTTP 401, or a credential variable that is not set."""


class SourceForbidden(SourceError):
    """HTTP 403 on an operation the source needs."""


class SourceRateLimited(SourceError):
    def __init__(self, message: str, operation: str | None = None, retry_after: float | None = None):
        self.retry_after = retry_after
        super().__init__(message, operation)


class SourceUnavailable(SourceError):
    """Connection failure, 5xx, an unexpected response shape, or a redirect (never followed)."""


class SourceTimeout(SourceError):
    pass


class SourceCursorExpired(SourceError):
    """The scroll context is gone (expired keepalive or a restarted node). The unit restarts."""


class SourceSchemaDrift(SourceError):
    """A field the configuration relies on is missing or has an unexpected type in an index."""


class SourceQueryRejected(SourceError):
    """HTTP 400: the cluster refused the query."""
