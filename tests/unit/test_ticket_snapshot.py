"""M3 (unified merge plan §6.1, §10.6): the daily ticket snapshot member.

Rows of a day's ticket export become the same documents as F0's tickets; unknown keys, duplicate
numbers and timestamps without an offset are refused. A day's snapshot is built through the
partition core's published function (``Engine.build_member``), replacing the previous day's unit
(continuing ids superseded) and tombstoning ids that left the snapshot. The adopted store's ticket
portion is not superseded here (M1b ``supersede``; QA100 gate). Synthetic only."""

from __future__ import annotations

import pytest

from su_multi_arm import Engine
from su_multi_arm.ingestion.members import MemberRegistry
from su_multi_arm.sources.ticket_snapshot import TicketRow, build_snapshot_member, snapshot_diff, snapshot_key, ticket_documents
from su_multi_arm.testing.fixtures.unified.collection import TICKETS, ticket_documents as f0_tickets
from tests.partition_support import config, f0, member_block


def rows() -> list[dict]:
    out = []
    for n, account, service, product, created, resolved, status, priority, subject, description, resolution in TICKETS:
        row = {"number": n, "subject": subject, "description": description, "resolution": resolution, "account": account, "service": service,
               "product": product, "created_at": created, "resolved_at": resolved, "status": status, "priority": priority,
               "url": f"https://support.example.org/tickets/{n}"}
        out.append({k: v for k, v in row.items() if v is not None})
    return out


def test_rows_become_the_f0_ticket_documents():
    assert ticket_documents(rows(), dataset="unified") == sorted(f0_tickets(), key=lambda d: d.id)


@pytest.mark.parametrize(("change", "message"), [
    ({"created_at": "2026-11-07T10:00:00"}, "offset"),
    ({"created_at": "yesterday"}, "ISO"),
    ({"internal_notes": "private"}, "Extra inputs"),
    ({"number": "12 34"}, "pattern"),
])
def test_invalid_rows_are_refused(change, message):
    with pytest.raises(ValueError, match=message):
        TicketRow.model_validate({**rows()[0], **change})


def test_duplicates_and_bad_days_are_refused():
    with pytest.raises(ValueError, match="duplicate ticket"):
        ticket_documents([rows()[0], rows()[0]], dataset="unified")
    assert snapshot_key("2026-11-09") == "snapshot:2026-11-09"
    for bad in ("2026-13-01", "20261109", "2026-11-09T00:00"):
        with pytest.raises(ValueError):
            snapshot_key(bad)


def test_snapshot_diff():
    before = ticket_documents(rows()[:3], dataset="unified")
    changed = {**rows()[1], "status": "closed"}
    after = ticket_documents([rows()[0], changed, rows()[3]], dataset="unified")
    assert snapshot_diff(before, after) == {"added": ["ticket:1004"], "changed": ["ticket:1002"], "removed": ["ticket:1003"]}


def test_daily_snapshots_replace_the_previous_unit_and_tombstone_removed_tickets(tmp_path):
    fx = f0()
    day1 = ticket_documents(rows()[:4], dataset="unified")
    day2 = ticket_documents([rows()[0], {**rows()[1], "status": "closed"}, rows()[3], rows()[4]], dataset="unified")
    block = member_block(fx, {"support": [("snapshot:2026-11-08", day1)]})
    engine = Engine(config(tmp_path, schema=fx.manifest["metadata_schema"], collection=block))
    first = build_snapshot_member(engine, day1, "2026-11-08")
    assert first == {"unit_id": "support/snapshot:2026-11-08", "replaces": [], "added": 4, "changed": 0, "removed": 0, "removed_ids": []}
    second = build_snapshot_member(engine, day2, "2026-11-09", previous=("snapshot:2026-11-08", day1))
    assert second["replaces"] == ["support/snapshot:2026-11-08"] and (second["added"], second["changed"], second["removed"]) == (1, 1, 1)
    entries = {e.doc_id: e for e in MemberRegistry(engine.config).directory.entries("support/snapshot:2026-11-08")}
    assert entries["ticket:1003"].deleted_by == "support/snapshot:2026-11-09"
    assert entries["ticket:1001"].superseded_by == "support/snapshot:2026-11-09" and entries["ticket:1002"].superseded_by == "support/snapshot:2026-11-09"
    with pytest.raises(ValueError, match="already registered"):
        build_snapshot_member(engine, day2, "2026-11-09")
