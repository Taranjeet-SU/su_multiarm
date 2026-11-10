"""Daily ticket snapshot member (unified merge plan §6.1, §10.6 M3; lane LD).

The support tickets of the unified collection move from the adopted support store to a **daily
snapshot member** (key ``snapshot:<day>``) so new and updated tickets reach the shared arms and the
link builders every day. This module turns one day's ticket export into validated documents and
registers them through the partition core's published functions only (``Engine.build_member`` ->
``ingestion/members.py``; ``replaces`` and ``tombstone`` of the id directory):

* ``TicketRow`` — the neutral row format the private export is mapped to (unknown keys are refused,
  so no unexpected private field reaches the index); timestamps need an offset.
* ``ticket_documents`` — rows -> ``Document`` (id ``ticket:<number>``, kind ``ticket``); duplicate
  numbers are refused.
* ``snapshot_diff`` — added / changed / removed ids against the previous snapshot (removed ids
  are tombstoned in the id directory and their links are retracted by the next link build).
* ``build_snapshot_member`` — build ``<member>/snapshot:<day>`` replacing the previous snapshot
  unit; ids missing from the new snapshot are tombstoned by the new unit.

Superseding the **adopted** support store's ticket portion needs M1b's ``members.supersede`` and
is gated on the QA100 ticket subset (§10.6); it is not done here.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any

from pydantic import Field, field_validator

from ..core.types import Document
from ..models import StrictModel

TICKET_PREFIX = "ticket:"
TEXT_FIELDS = ("description", "resolution", "comments")
METADATA_FIELDS = ("account", "service", "product", "status", "priority", "created_at", "resolved_at", "updated_at")
TIME_FIELDS = ("created_at", "resolved_at", "updated_at")


class TicketRow(StrictModel):
    number: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    subject: str = Field(min_length=1, max_length=1000)
    description: str = ""
    resolution: str = ""
    comments: str = ""
    account: str | None = None
    service: str | None = None
    product: str | None = None
    status: str | None = None
    priority: str | None = None
    created_at: str
    resolved_at: str | None = None
    updated_at: str | None = None
    url: str | None = None

    @field_validator("number", mode="before")
    @classmethod
    def number_text(cls, value: Any) -> Any:
        return str(value) if isinstance(value, int) else value

    @field_validator(*TIME_FIELDS)
    @classmethod
    def with_offset(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"not an ISO timestamp: {value!r}") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"timestamp without an offset: {value!r}")
        return value


def snapshot_key(day: date | str) -> str:
    value = day.isoformat() if isinstance(day, date) else str(day)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"snapshot day must be YYYY-MM-DD, not {value!r}")
    date.fromisoformat(value)
    return f"snapshot:{value}"


def ticket_documents(rows: Iterable[TicketRow | dict[str, Any]], *, dataset: str) -> list[Document]:
    out: list[Document] = []
    seen: set[str] = set()
    for raw in rows:
        row = raw if isinstance(raw, TicketRow) else TicketRow.model_validate(raw)
        doc_id = TICKET_PREFIX + row.number
        if doc_id in seen:
            raise ValueError(f"duplicate ticket {doc_id!r} in one snapshot")
        seen.add(doc_id)
        fields = {k: getattr(row, k) for k in TEXT_FIELDS if getattr(row, k)}
        metadata: dict[str, Any] = {"source": "ticket", **{k: getattr(row, k) for k in METADATA_FIELDS if getattr(row, k) is not None}}
        out.append(Document(id=doc_id, dataset=dataset, source="ticket", kind="ticket", title=row.subject, fields=fields, metadata=metadata, url=row.url))
    return sorted(out, key=lambda d: d.id)


def _digest(d: Document) -> str:
    return hashlib.sha256(d.model_dump_json().encode()).hexdigest()


def snapshot_diff(previous: Sequence[Document], current: Sequence[Document]) -> dict[str, list[str]]:
    before = {d.id: _digest(d) for d in previous}
    after = {d.id: _digest(d) for d in current}
    return {
        "added": sorted(set(after) - set(before)),
        "changed": sorted(i for i in set(after) & set(before) if after[i] != before[i]),
        "removed": sorted(set(before) - set(after)),
    }


def build_snapshot_member(engine: Any, documents: Sequence[Document], day: date | str, *, member: str = "support",
                          previous: tuple[str, Sequence[Document]] | None = None) -> dict[str, Any]:
    """Build ``<member>/snapshot:<day>``; with ``previous = (key, documents)`` the new unit replaces
    the previous snapshot unit (continuing ids superseded) and tombstones the removed ids. Compose
    (and retiring the previous unit) stay with the caller (``Engine.compose``)."""
    key = snapshot_key(day)
    diff = snapshot_diff(previous[1], documents) if previous is not None else {"added": sorted(d.id for d in documents), "changed": [], "removed": []}
    replaces = [f"{member}/{previous[0]}"] if previous is not None else []
    ref = engine.build_member(member, key, list(documents), replaces=replaces)
    if diff["removed"]:
        from ..ingestion.members import MemberRegistry

        MemberRegistry(engine.config).directory.tombstone(diff["removed"], by=ref.unit_id)
    return {"unit_id": ref.unit_id, "replaces": replaces, **{k: len(v) for k, v in diff.items()}, "removed_ids": diff["removed"]}
