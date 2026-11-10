"""Multi-source SQL-surface fixture for the M4 guard tests (unified merge plan §6.8; synthetic only).

``build(root)`` writes five small SQLite files from the F0 unified collection
(``tests/fixtures/unified``) — support (tickets, docs), tracker, logs (archive issue days and live
slices), analytics (search query days, clicks), registry (customers, aliases) and links — and
returns the ``SqlSurfaceSetup`` that attaches them read-only with pinned TEMP views:

* ``ticket`` / ``doc`` pinned to support ``seq <= 1`` (a later, unpinned ticket version exists);
* ``tracker_issue`` pinned to ``snapshot = 'snapshot:2026-11-08'`` (an older snapshot row exists);
* ``issue_day`` = archive days ``UNION ALL`` the ledger's current live slices (``slice_id IN``);
  superseded slices hold rows that must not be visible;
* ``search_query_day`` / ``search_click`` pinned to the analytics member days;
* ``customer`` / ``customer_alias`` pinned to ``registry_version`` (no access column: a registry
  is not tenant data; a tenant principal's SQL over it is refused, ``acl_not_applicable``);
* ``link`` pinned to ``link_version``.

The access column is ``tenant`` (``t_beta`` for rows of the synthetic customer ``cust:brightmoor``,
``t_alpha`` otherwise). Units: ``resolution_hours`` (hours), ``occurrences`` (events),
``observed_seconds`` (seconds), ``searches``/``zero_results`` (searches), ``clicks`` (clicks).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from su_multi_arm.sql_surface.setup import SqlSurfaceSetup
from su_multi_arm.testing.fixtures.unified import load

ROOT = Path(__file__).parent / "fixtures" / "unified"
REGISTRY_VERSION = "fixture-r1"
LINK_VERSION = "fixture-l1"
SNAPSHOT = "snapshot:2026-11-08"
BETA_CUSTOMER = "cust:brightmoor"


def _canonical(value: str | None) -> str | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat(timespec="microseconds")


def _hours(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    a = datetime.fromisoformat(start.replace("Z", "+00:00"))
    b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    return round((b - a).total_seconds() / 3600.0, 2)


def _write(path: Path, tables: dict[str, tuple[list[str], list[tuple]]]) -> str:
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    try:
        for name, (columns, rows) in tables.items():
            db.execute(f'CREATE TABLE "{name}" ({", ".join(columns)})')
            db.executemany(f'INSERT INTO "{name}" VALUES ({", ".join("?" for _ in columns)})', rows)
        db.commit()
    finally:
        db.close()
    return str(path)


def matched(registry: dict[str, Any]) -> dict[tuple[str, str], str]:
    return {(a["namespace"], a["raw"]): a["customer_id"] for a in registry["aliases"] if a["status"] == "matched" and a["raw"] is not None}


def tenant_of(customer_id: str | None) -> str:
    return "t_beta" if customer_id == BETA_CUSTOMER else "t_alpha"


def build(root: Path) -> SqlSurfaceSetup:
    fixture = load(ROOT)
    root.mkdir(parents=True, exist_ok=True)
    ids = matched(fixture.registry)
    support = next(docs for key, docs in fixture.members.items() if key.startswith("support/"))

    tickets, docs = [], []
    for d in support:
        m = d.metadata
        if d.kind == "ticket":
            cid = ids.get(("support.account", m.get("account")))
            tickets.append((d.id, tenant_of(cid), m.get("account"), cid, m.get("service"), m.get("status"), m.get("priority"), _canonical(m.get("created_at")),
                            _hours(m.get("created_at"), m.get("resolved_at")), d.title, "support", 1))
        elif d.kind == "doc":
            docs.append((d.id, "t_alpha", m.get("service"), d.title, "support", 1))
    # a later ticket version beyond the pin: never visible
    tickets.append(("ticket:1001", "t_alpha", "Alderbank Ltd", "cust:alderbank", "crawler", "reopened", "high", _canonical("2026-11-09T01:00:00Z"), None, "Unpinned", "support", 2))
    tickets.append(("ticket:9999", "t_alpha", "Alderbank Ltd", "cust:alderbank", "crawler", "open", "high", _canonical("2026-11-09T01:00:00Z"), None, "Unpinned new", "support", 2))
    ticket_cols = ["doc_id TEXT", "tenant TEXT", "account TEXT", "customer_id TEXT", "service TEXT", "status TEXT", "priority TEXT", "created_at TEXT",
                   "resolution_hours REAL", "title TEXT", "member TEXT", "seq INTEGER"]
    support_file = _write(root / "support.sqlite", {"tickets": (ticket_cols, tickets),
                                                    "docs": (["doc_id TEXT", "tenant TEXT", "service TEXT", "title TEXT", "member TEXT", "seq INTEGER"], docs)})

    tracker = []
    for d in fixture.members[f"tracker/{SNAPSHOT}"]:
        m = d.metadata
        cid = ids.get(("tracker.customer", m.get("customer")))
        tracker.append((d.id, tenant_of(cid), m.get("customer"), cid, m.get("component"), m.get("status"), _canonical(m.get("created_at")), SNAPSHOT))
    tracker.append(("tracker:ENG-101", "t_alpha", "Alderbank Ltd", "cust:alderbank", "crawler", "closed", _canonical("2026-11-01T10:00:00Z"), "snapshot:2026-11-01"))
    tracker_file = _write(root / "tracker.sqlite", {"issues": (["doc_id TEXT", "tenant TEXT", "customer TEXT", "customer_id TEXT", "component TEXT", "status TEXT",
                                                                "created_at TEXT", "snapshot TEXT"], tracker)})

    def incident_row(d: Any, extra: tuple) -> tuple:
        m = d.metadata
        cid = ids.get(("logs.tenant", m.get("tenant")))
        return (d.id, tenant_of(cid), m.get("tenant"), cid, m.get("service"), m.get("signature_id"), m.get("date"), int(m.get("occurrences", 0)),
                float(m.get("observed_window_seconds", 0.0)), *extra)

    archive = [incident_row(d, (key,)) for key, docs in fixture.members.items() if key.startswith("logs/") for d in docs if d.kind == "incident"]
    live = [incident_row(d, (slice_id,)) for slice_id, docs in fixture.slices.items() for d in docs if d.kind == "incident"]
    issue_cols = ["doc_id TEXT", "tenant TEXT", "log_tenant TEXT", "customer_id TEXT", "service TEXT", "signature_id TEXT", "day TEXT", "occurrences INTEGER",
                  "observed_seconds REAL"]
    logs_file = _write(root / "logs.sqlite", {"issue_day_archive": ([*issue_cols, "member TEXT"], archive), "issue_day_slice": ([*issue_cols, "slice_id TEXT"], live)})

    queries, clicks = [], []
    for key, docs in fixture.members.items():
        if not key.startswith("analytics/"):
            continue
        for d in docs:
            m = d.metadata
            cid = ids.get(("analytics.company_id", m.get("company_id")))
            if d.kind == "search_query_day":
                queries.append((d.id, tenant_of(cid), m["company_id"], cid, m["search_client"], m["date"], m["query_id"], m["searches"], m["zero_results"], m["clicks"], key))
            elif d.kind == "search_click":
                parent = f"analytics:search_query_day:{m['date']}:{m['query_id']}:{m['search_client']}:{m['company_id']}"
                clicks.append((d.id, tenant_of(cid), parent, m["doc_id"], m["rank"], key))
    analytics_file = _write(root / "analytics.sqlite", {
        "query_days": (["doc_id TEXT", "tenant TEXT", "company_id TEXT", "customer_id TEXT", "search_client TEXT", "day TEXT", "query_id TEXT", "searches INTEGER",
                        "zero_results INTEGER", "clicks INTEGER", "member TEXT"], queries),
        "clicks": (["doc_id TEXT", "tenant TEXT", "query_day_id TEXT", "target_id TEXT", "rank INTEGER", "member TEXT"], clicks),
    })

    customers = [(c["customer_id"], c["name"], REGISTRY_VERSION) for c in fixture.registry["customers"]]
    customers.append(("cust:retired", "Retired Customer", "fixture-r0"))
    aliases = [(a["id"], a["namespace"], a["raw"], a["customer_id"], a["status"], REGISTRY_VERSION) for a in fixture.registry["aliases"]]
    registry_file = _write(root / "registry.sqlite", {"customers": (["customer_id TEXT", "name TEXT", "registry_version TEXT"], customers),
                                                      "aliases": (["alias_id TEXT", "namespace TEXT", "raw TEXT", "customer_id TEXT", "status TEXT", "registry_version TEXT"], aliases)})

    ticket_tenant = {row[0]: row[1] for row in tickets if row[-1] == 1}
    links = [(c["id"], ticket_tenant.get(c["subject_id"], "t_alpha"), c["subject_id"], c["object_id"], c["pair"], c["tier"], c["status"], LINK_VERSION)
             for c in fixture.links["candidates"]]
    links.append(("L99", "t_alpha", "ticket:1001", "logs:incident:unpinned", "ticket-episode", "A", "accepted", "fixture-l0"))
    links_file = _write(root / "links.sqlite", {"links": (["link_id TEXT", "tenant TEXT", "subject_id TEXT", "object_id TEXT", "pair TEXT", "tier TEXT", "status TEXT",
                                                           "link_version TEXT"], links)})

    current = sorted(s for day in fixture.ledger["current"].values() for s in day)
    return SqlSurfaceSetup.model_validate(setup_dict(support_file, tracker_file, logs_file, analytics_file, registry_file, links_file, current))


def _col(name: str, type_: str, description: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "type": type_, "description": description, **extra}


def setup_dict(support: str, tracker: str, logs: str, analytics: str, registry: str, links: str, current_slices: list[str]) -> dict[str, Any]:
    key = _col("id", "id", "Document id of the row.", physical="doc_id")
    tenant = _col("tenant", "string", "Tenant that owns the row (access column).")
    customer = _col("customer_id", "id", "Registry customer id when the raw customer value is a matched alias; else null.")
    return {
        "generation": "fixture-view-1",
        "access_column": "tenant",
        "pins": {"member_set": "support,tracker,logs,analytics", "history_seq": 1, "registry_version": REGISTRY_VERSION, "link_version": LINK_VERSION},
        "attachments": [
            {"alias": "support", "path": support, "role": "member"},
            {"alias": "tracker", "path": tracker, "role": "member"},
            {"alias": "logs", "path": logs, "role": "slice"},
            {"alias": "analytics", "path": analytics, "role": "member"},
            {"alias": "registry", "path": registry, "role": "registry"},
            {"alias": "links", "path": links, "role": "links"},
        ],
        "views": [
            {"name": "ticket", "source": "support", "kind": "ticket", "description": "Support tickets.",
             "columns": [key, tenant,
                         _col("account", "string", "Raw customer account name of the ticket.", listable=True),
                         customer,
                         _col("service", "string", "Product service.", listable=True),
                         _col("status", "string", "Ticket workflow state.", listable=True, casefold_match=True),
                         _col("priority", "string", "Ticket priority.", listable=True),
                         _col("created_at", "datetime", "When the ticket was opened."),
                         _col("resolution_hours", "number", "Hours from creation to resolution (null while unresolved).", unit="hours"),
                         _col("title", "string", "Ticket subject line.", free_text=True)],
             "parts": [{"attachment": "support", "table": "tickets", "pins": [{"label": "member_set", "column": "member", "op": "eq", "value": "support"},
                                                                               {"label": "history_seq", "column": "seq", "op": "le", "value": 1}]}]},
            {"name": "doc", "source": "support", "kind": "doc", "description": "Knowledge-base documents.",
             "columns": [key, tenant, _col("service", "string", "Product service.", listable=True), _col("title", "string", "Document title.", free_text=True)],
             "parts": [{"attachment": "support", "table": "docs", "pins": [{"label": "history_seq", "column": "seq", "op": "le", "value": 1}]}]},
            {"name": "tracker_issue", "source": "tracker", "kind": "tracker_issue", "description": "Engineering tracker issues.",
             "columns": [key, tenant, _col("customer", "string", "Raw customer field of the issue.", listable=True), customer,
                         _col("component", "string", "Component.", listable=True), _col("status", "string", "Issue state.", listable=True),
                         _col("created_at", "datetime", "When the issue was filed.")],
             "parts": [{"attachment": "tracker", "table": "issues", "pins": [{"label": "member_set", "column": "snapshot", "op": "eq", "value": SNAPSHOT}]}]},
            {"name": "issue_day", "source": "logs", "kind": "incident", "description": "Log issue episodes per day (archive days and live slices).",
             "columns": [key, tenant, _col("log_tenant", "string", "Raw log tenant.", listable=True), customer,
                         _col("service", "string", "Service.", listable=True), _col("signature_id", "string", "Log rule id.", listable=True),
                         _col("day", "date", "UTC day of the episode."),
                         _col("occurrences", "integer", "Matched log events.", unit="events"),
                         _col("observed_seconds", "number", "Observed episode window.", physical="observed_seconds", unit="seconds")],
             "parts": [{"attachment": "logs", "table": "issue_day_archive", "pins": [{"label": "member_set", "column": "member", "op": "in",
                                                                                     "value": ["logs/month:2026-10", "logs/day:2026-11-04", "logs/day:2026-11-05", "logs/day:2026-11-06"]}]},
                       {"attachment": "logs", "table": "issue_day_slice", "pins": [{"label": "slice", "column": "slice_id", "op": "in", "value": current_slices}]}]},
            {"name": "search_query_day", "source": "analytics", "kind": "search_query_day", "description": "Search queries per day and client.",
             "columns": [key, tenant, _col("company_id", "id", "Raw analytics company id."), customer,
                         _col("search_client", "string", "Search client.", listable=True), _col("day", "date", "Day."), _col("query_id", "id", "Query id."),
                         _col("searches", "integer", "Searches run.", unit="searches"), _col("zero_results", "integer", "Searches with no result.", unit="searches"),
                         _col("clicks", "integer", "Result clicks.", unit="clicks")],
             "parts": [{"attachment": "analytics", "table": "query_days", "pins": [{"label": "member_set", "column": "member", "op": "in",
                                                                                    "value": ["analytics/day:2026-11-07", "analytics/day:2026-11-08"]}]}]},
            {"name": "search_click", "source": "analytics", "kind": "search_click", "description": "Clicked results of a search query day.",
             "columns": [key, tenant, _col("query_day_id", "id", "The search_query_day row."), _col("doc_id", "id", "The clicked document.", physical="target_id"),
                         _col("rank", "integer", "Result rank clicked.")],
             "parts": [{"attachment": "analytics", "table": "clicks", "pins": [{"label": "member_set", "column": "member", "op": "in",
                                                                                "value": ["analytics/day:2026-11-07", "analytics/day:2026-11-08"]}]}]},
            {"name": "customer", "source": "identity", "kind": "customer", "description": "Registry customers.",
             "columns": [_col("id", "id", "Registry customer id.", physical="customer_id"), _col("name", "string", "Customer name.", listable=True)],
             "parts": [{"attachment": "registry", "table": "customers", "pins": [{"label": "registry_version", "column": "registry_version", "op": "eq", "value": REGISTRY_VERSION}]}]},
            {"name": "customer_alias", "source": "identity", "kind": "customer_alias", "description": "Raw customer values per namespace and their status.",
             "columns": [_col("id", "id", "Alias id.", physical="alias_id"), _col("namespace", "string", "Namespace.", listable=True),
                         _col("raw", "string", "Verbatim raw value.", listable=True), _col("customer_id", "id", "Customer id when matched."),
                         _col("status", "string", "Alias status.", listable=True)],
             "parts": [{"attachment": "registry", "table": "aliases", "pins": [{"label": "registry_version", "column": "registry_version", "op": "eq", "value": REGISTRY_VERSION}]}]},
            {"name": "link", "source": "links", "kind": "link", "description": "Cross-source link candidates and their review status.",
             "columns": [_col("id", "id", "Link id.", physical="link_id"), tenant, _col("subject_id", "id", "Subject record id."),
                         _col("object_id", "id", "Object record id."), _col("pair", "string", "Pair kind.", listable=True),
                         _col("tier", "string", "Link tier.", listable=True), _col("status", "string", "Link status.", listable=True)],
             "parts": [{"attachment": "links", "table": "links", "pins": [{"label": "link_version", "column": "link_version", "op": "eq", "value": LINK_VERSION}]}]},
        ],
        "joins": [
            {"left": "ticket", "left_column": "customer_id", "right": "customer", "right_column": "id", "description": "Ticket account resolved through the registry."},
            {"left": "tracker_issue", "left_column": "customer_id", "right": "customer", "right_column": "id", "description": "Tracker customer resolved through the registry."},
            {"left": "issue_day", "left_column": "customer_id", "right": "customer", "right_column": "id", "description": "Log tenant resolved through the registry."},
            {"left": "search_query_day", "left_column": "customer_id", "right": "customer", "right_column": "id", "description": "Analytics company resolved through the registry."},
            {"left": "customer_alias", "left_column": "customer_id", "right": "customer", "right_column": "id", "description": "Alias of a customer."},
            {"left": "link", "left_column": "subject_id", "right": "ticket", "right_column": "id", "description": "Link subject ticket."},
            {"left": "link", "left_column": "object_id", "right": "issue_day", "right_column": "id", "description": "Link object episode."},
            {"left": "link", "left_column": "object_id", "right": "doc", "right_column": "id", "description": "Link object document."},
            {"left": "search_click", "left_column": "query_day_id", "right": "search_query_day", "right_column": "id", "description": "Click of a query day."},
            {"left": "search_click", "left_column": "doc_id", "right": "doc", "right_column": "id", "description": "Clicked document."},
        ],
    }
