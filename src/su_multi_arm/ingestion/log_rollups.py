"""Analytics kinds of the logs profile (addendum A2 §4.1.1–4.1.2; slices L5a/L5b).

Built from format-2 partition aggregates (``logs_parse.py``), next to the A1 kinds of
``rollups.py``. Every document has a rendered ``header`` naming its attributes, so the header links
of the graph ontology (``extraction/patterns/log_links.py``) find both endpoint names in one chunk.

============================ ============================================ ==========================
kind                         one per                                      measures
============================ ============================================ ==========================
``log_day``                  day x tenant x env x service x level x       lines (+ pulled), basis
                             status x signature                           (``level_field`` or
                                                                          ``format_group``)
``log_hour``                 the same per UTC hour (``ts_hour``)          lines
``endpoint_day``             day x tenant x env x service x endpoint      lines, latency sum/count/
                             template x method x status class             max, per-row p95
``endpoint_window``          service x endpoint (top ``max_per_service``) lines, latency sum/count/max
``exception_day``            day x tenant x env x service x signature x   lines
                             exception class
``coverage_day``             day x tenant x env x service                 source lines, pulled lines
                                                                          by status, fragments,
                                                                          deterministic coverage
``signature_catalog``        signature (all history)                      first_seen, last_seen,
                                                                          days_seen, lines_history
============================ ============================================ ==========================

**Exactness.** A row is ``exact`` when its unit was pulled whole and every line it counts was in the
pulled tier (``logs_parse._exact``); otherwise its count is scaled from the pulled share and labelled
``approximate`` with the sample ``method``. ``coverage_day`` counts pulled lines, which are exact as
counts of the pulled set; ``lines_total`` is the source's own count (exact); ``coverage`` is a ratio
(``additive: false``). ``latency_ms_p95`` is a per-row value (``additive: false``). Endpoints beyond
``max_per_service`` per (day, tenant, env, service) fold into ``__other__`` (row ``approximate``,
no p95).

**History (owner decision 2026-11-04).** Days kept by ``logs.retention.derived`` are composed from
their derived snapshots like window days (``logs_compose.py``), so ``signature_catalog`` is computed
from every composed ``signature_day`` document: ``first_seen``/``last_seen`` cover the whole kept
history, not the partition window. Day documents are content-addressed (ids name the day and
dimensions, never the partition version).
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from ..core.types import Document
from .rollups import DayInputs, _document, doc_id, worst

OTHER_ENDPOINT = "__other__"


def _unit_dims(row: dict[str, Any]) -> dict[str, Any]:
    attrs = row["attributes"]
    return {"tenant": attrs.get("tenant"), "env": attrs.get("env"), "service": attrs.get("service"), "deployment": attrs.get("deployment")}


def _basis_of(level_source: str | None) -> str | None:
    if not level_source:
        return None
    return ",".join(sorted({"level_field" if s == "field" else "format_group" for s in level_source.split(",") if s}))


def analytics_day_documents(dataset: str, day: DayInputs, kinds: Any, *, max_endpoints_per_service: int = 500) -> list[Document]:
    aggregates = day.aggregates
    if int(aggregates.get("format", 1)) < 2:
        raise ValueError(f"partition {day.key} has format {aggregates.get('format', 1)}; the analytics kinds need format 2 (reparse first)")
    stamp = {"date": day.key, **day.basis}
    docs: list[Document] = []
    if kinds.log_day:
        for row in aggregates["levels"]:
            meta = {
                **stamp,
                **_unit_dims(row),
                "level": row["level"],
                "status": row["status"],
                "signature_id": row["signature_id"],
                "category": row["category"],
                "pattern_owner": row.get("pattern_owner"),
                "lines": row["lines"],
                "lines_pulled": row["lines_pulled"],
                "basis": _basis_of(row.get("level_source")),
                "extraction_source": "deterministic",
                "row_exactness": row["row_exactness"],
                "method": row["method"],
            }
            parts = [day.key, row["unit_id"], row["level"], row["status"], row["signature_id"] or ""]
            docs.append(_document(dataset, "log_day", parts, f"{row['level']} {row['status']} {meta['service']} {day.key}", meta))
    if kinds.log_hour:
        for row in aggregates["level_hours"]:
            meta = {
                **{k: v for k, v in stamp.items()},
                **_unit_dims(row),
                "ts_hour": row["hour"],
                "level": row["level"],
                "status": row["status"],
                "signature_id": row["signature_id"],
                "lines": row["lines"],
                "lines_pulled": row["lines_pulled"],
                "extraction_source": "deterministic",
                "row_exactness": row["row_exactness"],
                "method": row["method"],
            }
            parts = [row["hour"], row["unit_id"], row["level"], row["status"], row["signature_id"] or ""]
            docs.append(_document(dataset, "log_hour", parts, f"{row['level']} {meta['service']} {row['hour']}", meta))
    if kinds.endpoint_day:
        per_unit: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in aggregates["endpoints"]:
            per_unit[row["unit_id"]].append(row)
        for unit_id in sorted(per_unit):
            rows = per_unit[unit_id]
            templates = sorted({r["endpoint"] for r in rows}, key=lambda e: (-sum(r["lines"] for r in rows if r["endpoint"] == e), e))
            keep = set(templates[:max_endpoints_per_service])
            other: dict[tuple[str, str], dict[str, Any]] = {}
            for row in rows:
                if row["endpoint"] in keep:
                    docs.append(_endpoint_doc(dataset, day, stamp, row, row["endpoint"], row["latency_ms_p95"], row["row_exactness"]))
                    continue
                bucket = other.setdefault((row["http_method"], row["status_class"]), {**row, "lines": 0, "lines_pulled": 0, "latency_ms_sum": 0.0, "latency_ms_count": 0, "latency_ms_max": None})
                bucket["lines"] += row["lines"]
                bucket["lines_pulled"] += row["lines_pulled"]
                bucket["latency_ms_sum"] += row["latency_ms_sum"]
                bucket["latency_ms_count"] += row["latency_ms_count"]
                if row["latency_ms_max"] is not None:
                    bucket["latency_ms_max"] = max(bucket["latency_ms_max"] or 0.0, row["latency_ms_max"])
            for (_m, _c), bucket in sorted(other.items()):
                docs.append(_endpoint_doc(dataset, day, stamp, bucket, OTHER_ENDPOINT, None, "approximate"))
    if kinds.exception_day:
        for row in aggregates["exceptions"]:
            meta = {
                **stamp,
                **_unit_dims(row),
                "signature_id": row["signature_id"],
                "exception_class": row["exception_class"],
                "lines": row["lines"],
                "lines_pulled": row["lines_pulled"],
                "row_exactness": row["row_exactness"],
                "method": row["method"],
            }
            parts = [day.key, row["unit_id"], row["signature_id"], row["exception_class"]]
            docs.append(_document(dataset, "exception_day", parts, f"{row['exception_class']} {meta['service']} {day.key}", meta))
    if kinds.coverage_day:
        for unit in aggregates["units"]:
            statuses = unit["statuses"]
            pulled = unit["pulled"]
            meta = {
                **stamp,
                **_unit_dims(unit),
                "lines_total": unit["lines_total"],
                "lines_pulled": pulled,
                "lines_parsed": statuses["parsed"],
                "lines_noise": statuses["noise"],
                "lines_unparsed": statuses["unparsed"],
                "lines_format_only": statuses["format_only"],
                "lines_timeout": statuses["timeout"],
                "fragments": unit["fragments"],
                "coverage": round((statuses["parsed"] + statuses["noise"]) / pulled, 6) if pulled else None,
                "row_exactness": "exact",
                "method": unit["method"],
            }
            docs.append(_document(dataset, "coverage_day", [day.key, unit["unit_id"]], f"coverage {meta['service']} {meta['tenant']} {day.key}", meta))
    return docs


def _endpoint_doc(dataset: str, day: DayInputs, stamp: dict[str, Any], row: dict[str, Any], endpoint: str, p95: Any, exactness: str) -> Document:
    meta = {
        **stamp,
        **_unit_dims(row),
        "endpoint": endpoint,
        "http_method": row["http_method"],
        "status_class": row["status_class"],
        "lines": row["lines"],
        "lines_pulled": row["lines_pulled"],
        "latency_ms_sum": row["latency_ms_sum"],
        "latency_ms_count": row["latency_ms_count"],
        "latency_ms_max": row["latency_ms_max"],
        "latency_ms_p95": p95,
        "row_exactness": exactness,
        "method": row["method"],
    }
    parts = [day.key, row["unit_id"], endpoint, row["http_method"], row["status_class"]]
    return _document(dataset, "endpoint_day", parts, f"{row['http_method']} {endpoint} {meta['service']} {day.key}", meta)


def endpoint_window_documents(dataset: str, endpoint_days: list[Document], max_per_service: int, basis: dict[str, str]) -> list[Document]:
    """``endpoint_window``: service x endpoint over the window (top ``max_per_service`` by lines per
    service), so ``log_endpoints`` without a service stays under ``max_groups`` (review N7)."""
    acc: dict[tuple[str, str], dict[str, Any]] = {}
    for doc in endpoint_days:
        m = doc.metadata
        entry = acc.setdefault((str(m.get("service")), str(m.get("endpoint"))), {"lines": 0, "sum": 0.0, "count": 0, "max": None, "exact": [], "first": m["date"], "last": m["date"]})
        entry["lines"] += int(m.get("lines") or 0)
        entry["sum"] += float(m.get("latency_ms_sum") or 0.0)
        entry["count"] += int(m.get("latency_ms_count") or 0)
        if m.get("latency_ms_max") is not None:
            entry["max"] = max(entry["max"] or 0.0, float(m["latency_ms_max"]))
        entry["exact"].append(str(m.get("row_exactness")))
        entry["first"], entry["last"] = min(entry["first"], m["date"]), max(entry["last"], m["date"])
    per_service: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for (service, endpoint), entry in acc.items():
        per_service[service].append((endpoint, entry))
    docs = []
    for service in sorted(per_service):
        ranked = sorted(per_service[service], key=lambda item: (-item[1]["lines"], item[0]))
        for endpoint, entry in ranked[:max_per_service]:
            meta = {
                "service": service,
                "endpoint": endpoint,
                "first_seen": entry["first"],
                "last_seen": entry["last"],
                "lines_window": entry["lines"],
                "latency_ms_sum": entry["sum"],
                "latency_ms_count": entry["count"],
                "latency_ms_max": entry["max"],
                "row_exactness": worst(entry["exact"]),
                **basis,
            }
            docs.append(_document(dataset, "endpoint_window", [service, endpoint], f"{endpoint} {service} window", meta))
    return docs


def signature_catalog_documents(dataset: str, signature_days: list[Document], basis: dict[str, str]) -> list[Document]:
    """``signature_catalog``: one per signature over **every** ``signature_day`` document (carried
    ones included), so ``first_seen`` is the first day the signature was ever seen in this
    deployment's composed history, not in the partition window."""
    acc: dict[str, dict[str, Any]] = {}
    for doc in signature_days:
        m = doc.metadata
        sig = str(m.get("signature_id"))
        entry = acc.setdefault(sig, {"first": m["date"], "last": m["date"], "days": set(), "lines": 0, "tenants": set(), "services": set(), "exact": [], "category": m.get("category")})
        entry["first"], entry["last"] = min(entry["first"], m["date"]), max(entry["last"], m["date"])
        entry["days"].add(m["date"])
        entry["lines"] += int(m.get("lines") or 0)
        entry["tenants"].add(m.get("tenant"))
        entry["services"].add(m.get("service"))
        entry["exact"].append(str(m.get("row_exactness")))
    docs = []
    for sig in sorted(acc):
        entry = acc[sig]
        meta = {
            "signature_id": sig,
            "category": entry["category"],
            "first_seen": entry["first"],
            "last_seen": entry["last"],
            "days_seen": len(entry["days"]),
            "lines_history": entry["lines"],
            "tenants_affected": len({t for t in entry["tenants"] if t}),
            "services": ",".join(sorted(s for s in entry["services"] if s)),
            "row_exactness": worst(entry["exact"]),
            **basis,
        }
        docs.append(_document(dataset, "signature_catalog", [sig], f"{sig} catalog", meta))
    return docs


# --- the window shard of the history tier (H1/H2): built from shard summaries, never from rows ---


def catalog_from_summaries(dataset: str, summaries: list[dict[str, Any]], basis: dict[str, str]) -> list[Document]:
    """``signature_catalog`` over every referenced day shard's summary (incremental: no row reads)."""
    acc: dict[str, dict[str, Any]] = {}
    for summary in sorted(summaries, key=lambda s: s["day"]):
        for sig, stats in summary.get("signatures", {}).items():
            entry = acc.setdefault(sig, {"first": summary["day"], "last": summary["day"], "days": 0, "lines": 0, "tenants": set(), "services": set(), "exact": True, "category": stats.get("category")})
            entry["first"], entry["last"] = min(entry["first"], summary["day"]), max(entry["last"], summary["day"])
            entry["days"] += 1
            entry["lines"] += int(stats.get("lines") or 0)
            entry["tenants"].update(stats.get("tenants") or [])
            entry["services"].update(stats.get("services") or [])
            entry["exact"] = entry["exact"] and bool(stats.get("exact", True))
    docs = []
    for sig in sorted(acc):
        e = acc[sig]
        meta = {
            "signature_id": sig,
            "category": e["category"],
            "first_seen": e["first"],
            "last_seen": e["last"],
            "days_seen": e["days"],
            "lines_history": e["lines"],
            "tenants_affected": len({t for t in e["tenants"] if t and t != "None"}),
            "services": ",".join(sorted(s for s in e["services"] if s and s != "None")),
            "row_exactness": "exact" if e["exact"] else "approximate",
            **basis,
        }
        docs.append(_document(dataset, "signature_catalog", [sig], f"{sig} catalog", meta))
    return docs


def graph_link_documents(dataset: str, summaries: list[dict[str, Any]], basis: dict[str, str]) -> list[Document]:
    """One ``graph_link`` document per header link seen in any referenced day shard (``emits``,
    ``runs``, ``serves``, ``raises``): its header names both endpoints, so the graph keeps every
    link of the served history while the day rows stay in their shards; per-day weights are read
    from the shards at query time."""
    acc: dict[str, dict[str, Any]] = {}
    for summary in summaries:
        for key, lines in summary.get("links", {}).items():
            entry = acc.setdefault(key, {"first": summary["day"], "last": summary["day"], "lines": 0, "days": 0})
            entry["first"], entry["last"] = min(entry["first"], summary["day"]), max(entry["last"], summary["day"])
            entry["lines"] += int(lines)
            entry["days"] += 1
    docs = []
    for key in sorted(acc):
        predicate, subject_field, subject, object_field, obj = json.loads(key)
        if not subject or not obj:
            continue
        e = acc[key]
        meta = {
            "predicate": predicate,
            "subject_field": subject_field,
            "object_field": object_field,
            subject_field: subject,
            object_field: obj,
            "first_seen": e["first"],
            "last_seen": e["last"],
            "days_seen": e["days"],
            "lines_history": e["lines"],
            **basis,
        }
        header = f"kind: graph_link | {predicate} | {subject_field.replace('_id', '')}: {subject} | {object_field.replace('_id', '')}: {obj} | days: {e['days']} | lines: {e['lines']}"
        docs.append(
            Document(
                id=doc_id("graph_link", predicate, str(subject), str(obj)),
                dataset=dataset,
                source="logs",
                kind="graph_link",
                title=f"{subject} {predicate} {obj}",
                fields={"header": header},
                metadata={k: v for k, v in meta.items() if v is not None},
            )
        )
    return docs


def signature_history_documents(dataset: str, summaries: list[dict[str, Any]], identities: dict[str, str], basis: dict[str, str]) -> list[Document]:
    """``signature_history`` (H2): one row per (signature, library identity) with the days and lines
    it covers, from the shard summaries and each day's library identity (imported days included)."""
    acc: dict[tuple[str, str], dict[str, Any]] = {}
    for summary in summaries:
        identity = identities.get(summary["day"], "unknown")
        for sig, stats in summary.get("signatures", {}).items():
            entry = acc.setdefault((sig, identity), {"first": summary["day"], "last": summary["day"], "days": 0, "lines": 0})
            entry["first"], entry["last"] = min(entry["first"], summary["day"]), max(entry["last"], summary["day"])
            entry["days"] += 1
            entry["lines"] += int(stats.get("lines") or 0)
    docs = []
    for (sig, identity), e in sorted(acc.items()):
        meta = {"signature_id": sig, "library_identity": identity, "first_seen": e["first"], "last_seen": e["last"], "days_seen": e["days"], "lines_history": e["lines"], **basis}
        docs.append(_document(dataset, "signature_history", [sig, identity], f"{sig} under {identity[:12]}", meta))
    return docs


def signature_lineage_documents(dataset: str, library: Any) -> list[Document]:
    """``signature_lineage`` (H2) from the decisions replay: a ``supersede`` decision maps the
    retired entry's signature to its successor's. Content-free (ids, decision time and actor)."""
    if library is None:
        return []
    lib = library.library
    entries = lib.entries
    docs = []
    for decision in lib.applied_decisions:
        if decision.op != "supersede" or decision.target is None:
            continue
        old, new = entries.get(decision.target), entries.get(decision.id)  # ``id`` supersedes ``target``
        old_sig = old.entry.signature.id if old is not None and old.entry.signature else None
        new_sig = new.entry.signature.id if new is not None and new.entry.signature else None
        if not old_sig or not new_sig or old_sig == new_sig:
            continue
        meta = {"signature_id": old_sig, "successor_id": new_sig, "pattern_id": decision.target, "successor_pattern_id": decision.id, "decided_at": decision.at.isoformat(), "decided_by": decision.by}
        docs.append(_document(dataset, "signature_lineage", [old_sig, new_sig], f"{old_sig} -> {new_sig}", meta))
    return docs
