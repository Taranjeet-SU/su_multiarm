"""Shared helpers of the cross-source link tests (unified merge M3, lane LD).

The F0 unified collection as the link builders see it: the archive members in manifest order,
then each live day's pinned slices (latest version per id), and the F0 identity registry imported
into a private SQLite file and pinned. A fake ``ChatProvider`` reviews links. Synthetic only; no
paid call, no service.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from su_multi_arm.core.types import Document
from su_multi_arm.crosssource.identity import IdentityView, import_registry, sha256_file
from su_multi_arm.providers.base import CallEstimate, ProviderServerError, Usage
from su_multi_arm.runtime.budget import PriceTable
from su_multi_arm.testing.fixtures.unified import load

F0 = Path(__file__).resolve().parent / "fixtures" / "unified"
PRICES = PriceTable(version="test", prices={"review-model": {"input_per_million": 3.0, "output_per_million": 15.0}})


def clock() -> datetime:
    return datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def f0():
    return load(F0)


def archive_documents(fx: Any) -> list[Document]:
    return [d for member in fx.manifest["members"] for d in fx.members[f"{member['id']}/{member['key']}"]]


def pinned_documents(fx: Any, days: dict[str, list[str]] | None = None) -> list[Document]:
    """Archive members, then the pinned slices of every live day (``days`` overrides the ledger's
    current slice list per day)."""
    docs = archive_documents(fx)
    current = {**fx.ledger["current"], **(days or {})}
    for day in sorted(current):
        for slice_id in current[day]:
            docs.extend(fx.slices[slice_id])
    return docs


def identity_view(fx: Any, tmp: Path) -> IdentityView:
    path = tmp / "ids.sqlite"
    if not path.exists():
        import_registry(path, fx.registry, now=clock)
    return IdentityView.pinned(path, sha256_file(path))


def ticket(n: int, account: str | None, created: str, description: str, *, service: str | None = None, title: str = "Synthetic ticket", **fields: str) -> Document:
    meta: dict[str, Any] = {"source": "ticket", "created_at": created}
    if account is not None:
        meta["account"] = account
    if service is not None:
        meta["service"] = service
    return Document(id=f"ticket:{n}", dataset="unified", source="ticket", kind="ticket", title=title, fields={"description": description, **fields}, metadata=meta)


def incident(uid: str, tenant: str, rule: str, first: str, last: str, *, service: str = "crawler", title: str = "Crawler fetch timed out") -> Document:
    meta = {"tenant": tenant, "service": service, "signature_id": rule, "first_hour": first, "last_hour": last}
    return Document(id=f"logs:incident:{uid}", dataset="unified", source="logs", kind="incident", title=title, fields={"summary": title}, metadata=meta)


class FakeRole:
    provider, model, max_tokens = "fake", "review-model", 400


class FakeReviewer:
    """A ``ChatProvider`` stand-in that reserves and records through the meter like ``MeteredCall``."""

    name = "fake"
    role = FakeRole()

    def __init__(self, verdicts: dict[str, dict[str, Any]] | None = None, *, fail_on: str | None = None, reported: bool = True):
        self.verdicts, self.fail_on, self.reported = verdicts or {}, fail_on, reported
        self.calls: list[dict[str, Any]] = []

    def complete_json(self, system, payload, schema, budget, ctx):
        budget.reserve(CallEstimate.of(len(system) + len(json.dumps(payload)), self.role.max_tokens))
        subj = ctx.attributes["subject"]
        self.calls.append(payload)
        usage = Usage(input_tokens=1000, output_tokens=100, reported=self.reported)
        error = ProviderServerError("synthetic 500", stage=ctx.stage) if subj == self.fail_on else None
        budget.record({"stage": ctx.stage, "success": error is None, "usage": usage.as_dict()})
        if error is not None:
            raise error
        reply = self.verdicts.get(subj, {"verdict": "uncertain", "rationale": "no evidence either way", "cited_fields": []})
        return schema.model_validate(reply), usage
