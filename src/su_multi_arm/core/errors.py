"""Typed errors for the kernel. Every failure is explicit; nothing here downgrades or retries."""

from __future__ import annotations


class SuError(Exception):
    """Base class for package errors."""


class ConfigError(SuError, ValueError):
    """Configuration is invalid. ``path`` is the dotted key of the offending value when known.

    ``diagnostics`` carries every aggregated problem when validation collected more than one
    (``Diagnostic``-like objects with ``path``/``message``); ``path`` is then the first one's.
    """

    def __init__(self, message: str, path: str | None = None, diagnostics: list | None = None):
        self.path = path
        self.diagnostics = list(diagnostics or [])
        super().__init__(f"{path}: {message}" if path else message)


class RegistryError(SuError, ValueError):
    """A component could not be registered or looked up."""


class PluginError(ConfigError):
    """A ``plugins:`` entry is not a registered entry point or does not satisfy the component contract."""


class ArtifactError(ConfigError):
    """An artifact reference is unsafe (pickle, dotted import path) or fails its integrity check."""


class CapabilityError(ConfigError):
    """A backend, provider or component does not declare a capability the configuration relies on."""


class BackendError(SuError, RuntimeError):
    """A backend operation failed. Carries the adapter kind and the operation name."""

    def __init__(self, adapter: str, operation: str, message: str):
        self.adapter, self.operation = adapter, operation
        super().__init__(f"{adapter}.{operation}: {message}")


class WriteConflict(BackendError):
    """Another writer owns the dataset's write path, or this writer lost it (plan §3.14, risk R1).

    ``reason`` is one of ``held`` (a live lease belongs to another writer: refused, never a wait),
    ``lost`` (this writer's lease expired or was taken over while it built), ``stale_base`` (the
    pointer no longer names the generation this writer built on: compare-and-set refused) or
    ``not_owner`` (a release/renewal by a token that does not own the lease). A writer that sees it
    never switches ``CURRENT``."""

    REASONS = ("held", "lost", "stale_base", "not_owner")

    def __init__(self, adapter: str, operation: str, message: str, reason: str):
        if reason not in self.REASONS:
            raise ValueError(f"unknown write-conflict reason {reason!r}")
        self.reason = reason
        super().__init__(adapter, operation, message)


class GenerationDisagreement(SuError, RuntimeError):
    """The backends holding one dataset's projections name different active generations. The request
    is refused: a reader never mixes generations. ``pointers`` maps backend name -> generation it
    serves (``None`` = no pointer); ``authoritative`` is the catalog registry's ``CURRENT``."""

    def __init__(self, dataset: str, authoritative: str, pointers: dict[str, str | None]):
        self.dataset, self.authoritative, self.pointers = dataset, authoritative, dict(pointers)
        disagreeing = {k: v for k, v in sorted(pointers.items()) if v != authoritative}
        super().__init__(f"dataset {dataset!r}: CURRENT is {authoritative} but backends serve {disagreeing}; refusing a mixed-generation read")


class IndexNotReady(SuError, ValueError):
    """No usable active generation: nothing ingested yet, a v1-built generation, or a recipe
    fingerprint that does not match this configuration. A server-state problem (HTTP 503), not a
    malformed request; a ``ValueError`` for compatibility with callers that caught that."""


class GenerationInUse(SuError, RuntimeError):
    """A destructive operation named a generation that is active (on any backend) or protected."""


class BudgetExhausted(SuError, RuntimeError):
    """A reservation would exceed a configured budget. Recorded in the trace by the caller."""

    def __init__(self, kind: str, requested: float, remaining: float):
        self.kind, self.requested, self.remaining = kind, requested, remaining
        super().__init__(f"budget exhausted for {kind}: requested {requested}, remaining {remaining}")


class DeadlineExceeded(SuError, RuntimeError):
    """The request deadline has passed."""


class AccessDenied(SuError, PermissionError):
    """403-class failure: the principal may not use the session, action or scope."""


class SessionNotFound(SuError, KeyError):
    """404-class failure: the session is unknown, expired or closed (for this principal's view)."""


class SessionConflict(SuError, RuntimeError):
    """A session compare-and-set lost: the stored record changed since it was read
    (``SessionStore.touch(..., expected_version=...)``). ``SessionManager.update`` re-reads and
    re-applies the change; it surfaces only when contention outlasts its bounded attempts."""


class RunCancelled(SuError, RuntimeError):
    """The caller cancelled the run (a streaming client disconnected or ``AnswerStream.cancel()``).

    Raised at the next stage boundary, streamed token or budget reservation; the run's budget refuses
    every further reservation. ``category`` matches the provider error categories for trace records."""

    category = "cancelled"


# --- client-facing failure categories ----------------------------------------------------------

FailureCategory = str  # one of FAILURE_STATUS's keys


# HTTP status per category; MCP and action results carry the category and reason only.
FAILURE_STATUS: dict[str, int] = {
    "write_conflict": 409,
    "session_conflict": 409,
    "access_denied": 403,
    "session_not_found": 404,
    "budget_exhausted": 429,
    "cancelled": 499,
    "provider_failed": 502,
    "backend_unavailable": 503,
    "index_not_ready": 503,
    "generation_disagreement": 503,
    "deadline_exceeded": 504,
    "invalid_request": 422,
    "failed": 500,
}


def failure_category(exc: BaseException) -> tuple[str, str | None]:
    """``(category, reason)`` of a failure, with no exception text (a2 review B5, N9).

    The order matters: ``IngestConflict`` is both an ``IngestError`` and a ``WriteConflict``, and
    ``WriteConflict`` is a ``BackendError``; ``MissingSecret`` is a ``ProviderError`` and a
    ``ValueError``."""
    from ..providers.base import ProviderError

    if isinstance(exc, WriteConflict):
        return "write_conflict", getattr(exc, "reason", None)
    if isinstance(exc, SessionConflict):
        return "session_conflict", "cas_attempts_exhausted"
    if isinstance(exc, AccessDenied):
        return "access_denied", None
    if isinstance(exc, SessionNotFound):
        return "session_not_found", None
    if isinstance(exc, BudgetExhausted):
        return "budget_exhausted", getattr(exc, "kind", None)
    if isinstance(exc, RunCancelled):
        return "cancelled", None
    if isinstance(exc, DeadlineExceeded):
        return "deadline_exceeded", None
    if isinstance(exc, ProviderError):
        return "provider_failed", getattr(exc, "category", None)
    if isinstance(exc, IndexNotReady):
        return "index_not_ready", None
    if isinstance(exc, GenerationDisagreement):
        return "generation_disagreement", None
    if isinstance(exc, BackendError):
        return "backend_unavailable", getattr(exc, "adapter", None)
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return "invalid_request", None
    return "failed", None
