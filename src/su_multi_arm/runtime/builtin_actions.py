"""Built-in write actions and the ledger-backed audit log (plan §3.4).

Only the actions named in ``actions.allow`` are registered with the engine's ``ActionRunner``; any
other name is denied (and the denial is audited). Built-ins in this release: ``ingest_delta``
(upsert/delete documents through the single write path, producing a new generation) and
``invalidate_cache`` (drop the active generation's cache entries). ``promote_generation`` and
``rollback_generation`` arrive with the release-ops slice (Phase 2).

``LedgerAuditLog`` stores ``AuditRecord`` versions as append-only entries of one ledger job
(``audit:<dataset>``) on ``storage.ledger``: the ``started`` version is written **before** the action
runs and a final version after it, so a crash leaves a visible ``started`` record. Duplicate
detection folds the entries (latest version per audit id); it reads the whole audit job, which is
acceptable for the rare, operator-driven action volume this release targets.
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING, Any, Callable

from pydantic import Field, model_validator

from ..core.types import Document, LedgerEntry
from ..models import StrictModel
from .actions import ActionRequest, ActionResult, AuditRecord

if TYPE_CHECKING:  # pragma: no cover
    from ..backends.contracts import LedgerStore


class LedgerAuditLog:
    """``AuditLog`` over a ``LedgerStore`` facet (append-only versions of each record)."""

    def __init__(self, ledger: LedgerStore, dataset: str):
        self.ledger = ledger
        self.job_id = f"audit:{dataset}"

    def _append(self, record: AuditRecord) -> None:
        entries = self.ledger.replay(self.job_id)
        sequence = (entries[-1].sequence + 1) if entries else 0
        at = _dt.datetime.fromtimestamp(record.finished or record.started, tz=_dt.timezone.utc).isoformat()
        self.ledger.append(self.job_id, LedgerEntry(sequence=sequence, kind="audit", at=at, payload=record.model_dump(mode="json")))

    def write(self, record: AuditRecord) -> None:
        self._append(record)

    def finalise(self, record: AuditRecord) -> None:
        self._append(record)

    def records(self) -> list[AuditRecord]:
        """Latest version of every audit record, in first-written order."""
        latest: dict[str, AuditRecord] = {}
        for entry in self.ledger.replay(self.job_id):
            if entry.kind == "audit":
                record = AuditRecord.model_validate(entry.payload)
                latest[record.id] = record
        return list(latest.values())

    def history(self) -> list[AuditRecord]:
        """Every version as written (``started`` before the final status)."""
        return [AuditRecord.model_validate(e.payload) for e in self.ledger.replay(self.job_id) if e.kind == "audit"]

    def find_duplicate(self, action: str, idempotency_key: str, since: float) -> AuditRecord | None:
        for record in reversed(self.records()):
            if record.action == action and record.idempotency_key == idempotency_key and record.started >= since:
                if record.status in {"started", "applied"}:
                    return record
        return None


class IngestDeltaArgs(StrictModel):
    documents: list[Document] = Field(default_factory=list, max_length=10000)
    delete_ids: list[str] = Field(default_factory=list, max_length=10000)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.documents and not self.delete_ids:
            raise ValueError("ingest_delta needs documents or delete_ids")
        return self


class NoArgs(StrictModel):
    """Actions without arguments reject any."""


def ingest_delta(engine: Any) -> Callable[[ActionRequest], dict[str, Any]]:
    def run(request: ActionRequest) -> dict[str, Any]:
        args = IngestDeltaArgs.model_validate(request.args)
        manifest = engine.ingest(list(args.documents), delete_ids=list(args.delete_ids))  # the new manifest
        return {"generation": manifest.get("generation"), "status": manifest.get("status"), "documents": manifest.get("documents")}

    return run


def invalidate_cache(engine: Any) -> Callable[[ActionRequest], dict[str, Any]]:
    def run(request: ActionRequest) -> dict[str, Any]:
        NoArgs.model_validate(request.args)
        gen, _ = engine.write_path.current()
        cache = engine.backends[engine.config.storage.cache].cache
        return {"generation": gen.generation, "invalidated": cache.invalidate(gen.prefix())}

    return run


BUILTIN_ACTIONS: dict[str, Callable[[Any], Callable[[ActionRequest], dict[str, Any]]]] = {
    "ingest_delta": ingest_delta,
    "invalidate_cache": invalidate_cache,
}


class ActionCall(StrictModel):
    """``Engine.run_action`` result: the ``ActionResult`` (with its audit id) and the masked trace."""

    action: str
    result: ActionResult
    trace: dict[str, Any]
