"""Actions: the only write path, audited before and after execution (plan §3.4).

``ActionRequest`` -> ``ActionResult{status: applied|skipped_duplicate|denied|failed}``. An
``AuditRecord`` is written **before** execution and finalised after. A duplicate
``idempotency_key`` within the audit retention window yields ``skipped_duplicate``. Invocation
surfaces: CLI and Python API always; HTTP/MCP only when ``serving.actions.allow`` lists the action
and the principal has the ``actions`` scope (default denied). Built-ins (``ingest_delta``,
``invalidate_cache``, ``promote_generation``, ``rollback_generation``) arrive with the write path.
Learning ``apply`` is not an action and has no serving surface.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Callable, Literal, Protocol, runtime_checkable

from pydantic import Field

from ..core.errors import AccessDenied
from ..models import StrictModel
from .acl import Principal

ActionStatus = Literal["applied", "skipped_duplicate", "denied", "failed"]
Surface = Literal["cli", "python", "http", "mcp"]


class ActionRequest(StrictModel):
    action: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    args: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=256)
    principal: Principal
    reason: str = ""


class ActionResult(StrictModel):
    status: ActionStatus
    outputs: dict[str, Any] = Field(default_factory=dict)
    audit_id: str | None = None
    error: str | None = None


class AuditRecord(StrictModel):
    id: str
    action: str
    principal: str
    idempotency_key: str
    args_hash: str
    started: float
    finished: float | None = None
    status: Literal["started", "applied", "failed", "denied", "skipped_duplicate"] = "started"
    generation_before: str | None = None
    generation_after: str | None = None
    error: str | None = None


@runtime_checkable
class AuditLog(Protocol):
    def write(self, record: AuditRecord) -> None: ...

    def finalise(self, record: AuditRecord) -> None: ...

    def find_duplicate(self, action: str, idempotency_key: str, since: float) -> AuditRecord | None: ...


class MemoryAuditLog:
    def __init__(self):
        self.records: list[AuditRecord] = []

    def write(self, record: AuditRecord) -> None:
        self.records.append(record)

    def finalise(self, record: AuditRecord) -> None:
        self.records = [record if r.id == record.id else r for r in self.records]

    def find_duplicate(self, action: str, idempotency_key: str, since: float) -> AuditRecord | None:
        for record in reversed(self.records):
            if record.action == action and record.idempotency_key == idempotency_key and record.started >= since:
                if record.status in {"started", "applied"}:
                    return record
        return None


def args_hash(args: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()


ActionFn = Callable[[ActionRequest], dict[str, Any]]


class ActionRunner:
    """Registers action callables and runs them with audit, idempotency and surface rules."""

    def __init__(
        self,
        audit: AuditLog,
        allow_remote: list[str] | None = None,
        retention_seconds: float = 7 * 24 * 3600,
        clock: Callable[[], float] = time.time,
        current_generation: Callable[[], str | None] = lambda: None,
    ):
        self.audit = audit
        self.allow_remote = set(allow_remote or [])
        self.retention_seconds = retention_seconds
        self.clock = clock
        self.current_generation = current_generation
        self._actions: dict[str, ActionFn] = {}

    def register(self, name: str, fn: ActionFn) -> None:
        if name in self._actions:
            raise ValueError(f"action {name!r} already registered")
        self._actions[name] = fn

    def names(self) -> list[str]:
        return sorted(self._actions)

    def authorise(self, request: ActionRequest, surface: Surface) -> str | None:
        """Reason for denial, or None when permitted."""
        if request.action not in self._actions:
            return f"unknown action {request.action!r}"
        if surface in {"http", "mcp"}:
            if request.action not in self.allow_remote:
                return f"action {request.action!r} is not allowed on the {surface} surface"
            if not request.principal.has_scope("actions"):
                return "principal lacks the 'actions' scope"
        return None

    def invoke(self, request: ActionRequest, surface: Surface = "python") -> ActionResult:
        denial = self.authorise(request, surface)
        now = self.clock()
        if denial:
            record = AuditRecord(
                id=uuid.uuid4().hex,
                action=request.action,
                principal=request.principal.key(),
                idempotency_key=request.idempotency_key,
                args_hash=args_hash(request.args),
                started=now,
                finished=now,
                status="denied",
                error=denial,
            )
            self.audit.write(record)
            return ActionResult(status="denied", audit_id=record.id, error=denial)
        duplicate = self.audit.find_duplicate(request.action, request.idempotency_key, now - self.retention_seconds)
        if duplicate is not None:
            return ActionResult(status="skipped_duplicate", audit_id=duplicate.id)
        record = AuditRecord(
            id=uuid.uuid4().hex,
            action=request.action,
            principal=request.principal.key(),
            idempotency_key=request.idempotency_key,
            args_hash=args_hash(request.args),
            started=now,
            generation_before=self.current_generation(),
        )
        self.audit.write(record)  # before execution
        try:
            outputs = self._actions[request.action](request)
        except AccessDenied as exc:
            self.audit.finalise(record.model_copy(update={"finished": self.clock(), "status": "denied", "error": str(exc)}))
            return ActionResult(status="denied", audit_id=record.id, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - every failure is recorded, never hidden
            self.audit.finalise(record.model_copy(update={"finished": self.clock(), "status": "failed", "error": str(exc)}))
            return ActionResult(status="failed", audit_id=record.id, error=f"{type(exc).__name__}: {exc}")
        self.audit.finalise(
            record.model_copy(
                update={"finished": self.clock(), "status": "applied", "generation_after": self.current_generation()}
            )
        )
        return ActionResult(status="applied", outputs=outputs or {}, audit_id=record.id)
