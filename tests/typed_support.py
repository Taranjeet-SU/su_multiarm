"""Synthetic typed-projection domain (typed-query plan §8.1) shared by the M3-M5 tests.

Four kinds (ticket, comment, asset, note), two tenants plus a canary tenant, a child table
(comment -> ticket) with public/private visibility, NULLs, one dangling parent key, same-named
columns across tables and injection-shaped strings as values. No real data.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import yaml

from su_multi_arm.compat.config_v1 import upgrade_mapping
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.types import Document, FieldSpec, MetadataSchema, Provenance, StructuredRecord

ROOT = Path(__file__).parent.parent
TENANTS = ("t_alpha", "t_beta")
CANARY = "t_canary"
INJECTION = "x'); DROP TABLE ticket; --"

FIELDS: list[dict[str, Any]] = [
    {"name": "tenant", "type": "string", "listable": True, "groupable": True, "description": "Owning tenant key (access column)."},
    {"name": "status", "type": "enum", "enum_values": ["open", "closed", "pending"], "listable": True, "groupable": True, "description": "Ticket workflow state at capture time."},
    {"name": "module", "type": "string", "listable": True, "groupable": True, "casefold_match": True, "description": "Product module the record concerns."},
    {"name": "priority", "type": "integer", "sortable": True, "groupable": True, "description": "Priority 1 (highest) to 4; not severity."},
    {"name": "hours", "type": "number", "sortable": True, "description": "Logged effort in hours (additive)."},
    {"name": "opened_at", "type": "datetime", "time_basis": "opened", "sortable": True, "description": "When the record was opened (UTC offset required)."},
    {"name": "visibility", "type": "enum", "enum_values": ["public", "private"], "listable": True, "groupable": True, "description": "Comment visibility; private comments are internal."},
    {"name": "parent_id", "type": "id", "listable": False, "max_catalog_cardinality": 0, "description": "Document id of the parent ticket of a comment."},
    {"name": "title_text", "type": "string", "listable": False, "max_catalog_cardinality": 0, "description": "Free title copy (high cardinality)."},
]

PROJECTION: dict[str, Any] = {
    "enabled": True,
    "backend": "local",
    "tables": {
        "ticket": {"kind": "ticket", "columns": ["tenant", "status", "module", "priority", "hours", "opened_at", "title_text"]},
        "comment": {"kind": "comment", "columns": ["tenant", "visibility", "parent_id", "opened_at", "hours"]},
        "asset": {"kind": "asset", "columns": ["tenant", "module", "hours"]},
    },
    "relations": {"comment_parent": {"from": "comment.parent_id", "to": "ticket.id", "cardinality": "many_to_one", "description": "A comment belongs to one ticket."}},
}


def schema() -> MetadataSchema:
    return MetadataSchema(canonical_unit="record", fields=[FieldSpec.model_validate(f) for f in FIELDS])


def _ticket(i: int, tenant: str, status: str, module: str, priority: int | None, hours: float | None, day: int, title: str = "") -> Document:
    metadata = {"tenant": tenant, "status": status, "module": module, "opened_at": f"2024-03-{day:02d}T10:00:00+00:00", "title_text": title or f"ticket {i}"}
    if priority is not None:
        metadata["priority"] = priority
    if hours is not None:
        metadata["hours"] = hours
    return Document(id=f"tk-{i}", dataset="demo", source="tracker", kind="ticket", title=f"Ticket {i}", fields={"body": f"synthetic ticket {i} about {module}"}, metadata=metadata)


def _comment(i: int, tenant: str, parent: str, visibility: str, hours: float | None = None) -> Document:
    metadata: dict[str, Any] = {"tenant": tenant, "visibility": visibility, "parent_id": parent, "opened_at": f"2024-03-{(i % 27) + 1:02d}T12:00:00+00:00"}
    if hours is not None:
        metadata["hours"] = hours
    return Document(id=f"cm-{i}", dataset="demo", source="tracker", kind="comment", title=f"Comment {i}", fields={"body": f"synthetic comment {i}"}, metadata=metadata)


def documents() -> list[Document]:
    docs = [
        _ticket(1, "t_alpha", "open", "Billing", 1, 2.5, 1),
        _ticket(2, "t_alpha", "closed", "Billing", 2, 4.0, 5),
        _ticket(3, "t_alpha", "open", "Search", None, None, 9, INJECTION),
        _ticket(4, "t_beta", "open", "Billing", 3, 1.0, 2),
        _ticket(5, "t_beta", "pending", "Reports", 2, 6.5, 12),
        _ticket(6, CANARY, "open", "Billing", 1, 99.0, 3, "canary"),
        _comment(1, "t_alpha", "tk-1", "public", 0.5),
        _comment(2, "t_alpha", "tk-1", "private", 1.0),
        _comment(3, "t_alpha", "tk-2", "public"),
        _comment(4, "t_beta", "tk-4", "public", 2.0),
        _comment(5, "t_beta", "tk-missing", "private"),  # dangling parent key
        _comment(6, CANARY, "tk-6", "private", 9.0),
        Document(id="as-1", dataset="demo", source="inventory", kind="asset", title="Asset 1", fields={"body": "asset"}, metadata={"tenant": "t_alpha", "module": "Billing", "hours": 3.0}),
        Document(id="as-2", dataset="demo", source="inventory", kind="asset", title="Asset 2", fields={"body": "asset"}, metadata={"tenant": "t_beta", "module": "Search"}),
        Document(id="nt-1", dataset="demo", source="wiki", kind="note", title="Note", fields={"body": "a note without a projection table"}, metadata={"tenant": "t_alpha"}),
        Document(id="nt-2", dataset="demo", source="wiki", kind="note", title="Orphan", fields={"body": "a note without a tenant"}, metadata={}),
    ]
    return docs


def records(docs: list[Document] | None = None) -> list[StructuredRecord]:
    from su_multi_arm.backends._scope import document_values

    return [StructuredRecord(schema_id="document", id=d.id, values=document_values(d), provenance=Provenance(extractor="catalog")) for d in (docs or documents())]


def config_dict(state_dir: Path, *, projection: dict[str, Any] | None = None, access: str = "tenant", typed_arm: bool = True) -> dict[str, Any]:
    data = yaml.safe_load((ROOT / "examples/local.yaml").read_text())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        v2 = upgrade_mapping(data).data
    v2["profile"] = "neutral"
    v2["backends"]["local"]["path"] = str(state_dir)
    v2["metadata_schema"] = {"canonical_unit": "record", "fields": [dict(f) for f in FIELDS]}
    v2["projection"] = PROJECTION if projection is None else projection
    if access == "tenant":
        v2["access"] = {"policy": "tenant", "field": "tenant", "unrestricted": ["operator"]}
    if typed_arm:
        v2["pipeline"]["arms"]["typed_query"] = {"row_limit": 50, "max_groups": 100}
    return v2


def config(state_dir: Path, **kwargs: Any):
    return validate_config(config_dict(state_dir, **kwargs))
