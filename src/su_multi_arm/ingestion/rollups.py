"""Compose-time documents and rollups (addendum A1 §3.7, §3.9.2; slice L3a).

Every document is built from partition aggregates only (counts, ids, attributes); the only kind
with log-line text is ``signature`` (``exemplars``, the ``line`` chunk recipe). Each kind has a
rendered ``header`` field naming its attributes, so header-scoped attribute links find both
endpoint names in one header chunk (§3.4.2).

=========================== ======================= =============================================
kind                        one per                 metadata
=========================== ======================= =============================================
``unit_day``                day x unit              date, tenant, env, service, lines_total,
                                                    count_<query>, basis_<query>, lines_*,
                                                    row_exactness, basis, method
``signature_day``           day x unit x signature  date, tenant, env, service, signature_id,
                                                    category, lines, row_exactness, method
``service_day``             day x service           date, service, lines_total, count_<query>,
                                                    tenants_affected, row_exactness
``signature_service_day``   day x service x sig.    date, service, signature_id, lines, row_exactness
``signature_hour`` (opt-in) hour x service x sig.   ts_hour, service, signature_id, lines
``signature_window``        signature               first_seen, last_seen, lines_window,
                                                    tenants_affected, services, row_exactness
``signature_tenant_window`` signature x tenant      signature_id, tenant, lines_window,
                                                    first_seen, last_seen, row_exactness
``signature``               signature               signature_id, pattern_id, category, author,
                                                    reviewed; fields header, template, exemplars
=========================== ======================= =============================================

Every dated document also records ``date_basis`` (``index_date`` or ``event_time``) and
``date_timezone``, so an exactness claim can state its time basis and timezone (N19).
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any, Iterable

from ..core.types import Document

HEADER_FIELD = "header"
WORST = ("exact", "approximate")


def worst(values: Iterable[str]) -> str:
    seen = set(values)
    return "approximate" if "approximate" in seen or not seen else "exact"


def doc_id(kind: str, *parts: str) -> str:
    return f"logs:{kind}:" + hashlib.sha256("\0".join(parts).encode()).hexdigest()[:32]


def render_header(kind: str, values: dict[str, Any]) -> str:
    """``kind: signature_day | date: 2026-01-05 (index_date, UTC) | service: query-gateway | ...``:
    deterministic, attribute names first, values separated by `` | `` so a value is never glued to
    its neighbour (attribute-link endpoints are matched on value boundaries)."""
    parts = [f"kind: {kind}"]
    if "date" in values:
        parts.append(f"date: {values['date']} ({values.get('date_basis', 'index_date')}, {values.get('date_timezone', 'UTC')})")
    for key in ("ts_hour", "tenant", "env", "service", "signature_id", "category", "pattern_id", "first_seen", "last_seen", "services"):
        if values.get(key) not in (None, ""):
            parts.append(f"{key.replace('_id', '')}: {values[key]}")
    for key in ("lines", "lines_total", "lines_window", "tenants_affected"):
        if key in values:
            parts.append(f"{key}: {values[key]} ({values.get('row_exactness', 'exact')})")
    return " | ".join(parts)


def _document(dataset: str, kind: str, key_parts: list[str], title: str, metadata: dict[str, Any], fields: dict[str, str] | None = None) -> Document:
    metadata = {k: v for k, v in metadata.items() if v is not None}
    body = {HEADER_FIELD: render_header(kind, metadata), **(fields or {})}
    return Document(id=doc_id(kind, *key_parts), dataset=dataset, source="logs", kind=kind, title=title, fields=body, metadata=metadata)


class DayInputs:
    """The aggregates of one sealed partition, as compose reads them."""

    def __init__(self, key: str, aggregates: dict[str, Any], hourly_cells: list[dict[str, Any]], exemplars: dict[str, list[dict[str, Any]]], basis: dict[str, str]):
        self.key, self.aggregates, self.hourly_cells, self.exemplars, self.basis = key, aggregates, hourly_cells, exemplars, basis


def day_documents(dataset: str, day: DayInputs, *, signature_hour: bool = False) -> list[Document]:
    """Per-day kinds: ``unit_day``, ``signature_day``, ``service_day``, ``signature_service_day``
    and (opt-in) ``signature_hour``."""
    stamp = {"date": day.key, **day.basis}
    docs: list[Document] = []
    services: dict[str, dict[str, Any]] = defaultdict(lambda: {"lines_total": 0, "counts": defaultdict(int), "tenants": set(), "exactness": []})
    for unit in day.aggregates["units"]:
        attrs = unit["attributes"]
        meta: dict[str, Any] = {
            **stamp,
            "tenant": attrs.get("tenant"),
            "env": attrs.get("env"),
            "service": attrs.get("service"),
            "lines_total": unit["lines_total"],
            "lines_pulled": unit["pulled"],
            "lines_parsed": unit["statuses"]["parsed"],
            "lines_noise": unit["statuses"]["noise"],
            "lines_unparsed": unit["statuses"]["unparsed"] + unit["statuses"]["format_only"],
            "fragments": unit["fragments"],
            "row_exactness": unit["row_exactness"],
            "method": unit["method"],
            "basis": ",".join(sorted(set(unit["basis"].values()))) or None,
        }
        for name, count in sorted(unit["by_query"].items()):
            meta[f"count_{name}"] = count
            meta[f"basis_{name}"] = unit["basis"].get(name)
        docs.append(_document(dataset, "unit_day", [day.key, unit["unit_id"]], f"{attrs.get('service')} {attrs.get('tenant')} {day.key}", meta))
        service = services[attrs.get("service") or "unknown"]
        service["lines_total"] += unit["lines_total"]
        for name, count in unit["by_query"].items():
            service["counts"][name] += count
            service.setdefault("basis", {})[name] = unit["basis"].get(name)
        service["exactness"].append(unit["row_exactness"])
    per_service_signature: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"lines": 0, "exactness": []})
    for row in day.aggregates["signatures"]:
        attrs = row["attributes"]
        meta = {
            **stamp,
            "tenant": attrs.get("tenant"),
            "env": attrs.get("env"),
            "service": attrs.get("service"),
            "signature_id": row["signature_id"],
            "category": row["category"],
            "lines": row["lines"],
            "row_exactness": row["row_exactness"],
            "method": row["method"],
        }
        docs.append(_document(dataset, "signature_day", [day.key, row["unit_id"], row["signature_id"]], f"{row['signature_id']} {attrs.get('service')} {day.key}", meta))
        service_name = attrs.get("service") or "unknown"
        services[service_name]["tenants"].add(attrs.get("tenant"))
        services[service_name]["exactness"].append(row["row_exactness"])
        bucket = per_service_signature[(service_name, row["signature_id"])]
        bucket["lines"] += row["lines"]
        bucket["exactness"].append(row["row_exactness"])
    for name, service in sorted(services.items()):
        meta = {**stamp, "service": name, "lines_total": service["lines_total"], "tenants_affected": len(service["tenants"]), "row_exactness": worst(service["exactness"])}
        for query, count in sorted(service["counts"].items()):
            meta[f"count_{query}"] = count
            meta[f"basis_{query}"] = service.get("basis", {}).get(query)
        docs.append(_document(dataset, "service_day", [day.key, name], f"{name} {day.key}", meta))
    for (service_name, signature), bucket in sorted(per_service_signature.items()):
        meta = {**stamp, "service": service_name, "signature_id": signature, "lines": bucket["lines"], "row_exactness": worst(bucket["exactness"])}
        docs.append(_document(dataset, "signature_service_day", [day.key, service_name, signature], f"{signature} {service_name} {day.key}", meta))
    if signature_hour:
        hours: dict[tuple[str, str, str], int] = defaultdict(int)
        for cell in day.hourly_cells:
            hours[(cell["hour"], cell["service"], cell["signature_id"])] += cell["lines"]
        for (hour, service_name, signature), lines in sorted(hours.items()):
            meta = {"ts_hour": hour, "service": service_name, "signature_id": signature, "lines": lines, "row_exactness": "exact", **day.basis}
            docs.append(_document(dataset, "signature_hour", [hour, service_name, signature, day.key], f"{signature} {service_name} {hour}", meta))
    return docs


def window_documents(dataset: str, days: list[DayInputs], library: Any, *, max_exemplars: int = 3) -> list[Document]:
    """Window rollups (``signature_window``, ``signature_tenant_window``) and the ``signature``
    documents. Exemplars come from the newest days first; templates are rendered from the library
    entry (no line text)."""
    windows: dict[str, dict[str, Any]] = {}
    tenants: dict[tuple[str, str], dict[str, Any]] = {}
    for day in sorted(days, key=lambda d: d.key):
        for row in day.aggregates["signatures"]:
            attrs = row["attributes"]
            sig = row["signature_id"]
            entry = windows.setdefault(sig, {"first": day.key, "last": day.key, "lines": 0, "tenants": set(), "services": set(), "exactness": [], "category": row["category"], "pattern_id": row["pattern_id"]})
            entry["last"] = day.key
            entry["lines"] += row["lines"]
            entry["tenants"].add(attrs.get("tenant"))
            entry["services"].add(attrs.get("service"))
            entry["exactness"].append(row["row_exactness"])
            per = tenants.setdefault((sig, attrs.get("tenant") or "unknown"), {"first": day.key, "last": day.key, "lines": 0, "exactness": []})
            per["last"] = day.key
            per["lines"] += row["lines"]
            per["exactness"].append(row["row_exactness"])
    basis = days[-1].basis if days else {}
    docs: list[Document] = []
    entries = _entries_by_signature(library)
    for sig, entry in sorted(windows.items()):
        meta = {
            "signature_id": sig,
            "category": entry["category"],
            "first_seen": entry["first"],
            "last_seen": entry["last"],
            "lines_window": entry["lines"],
            "tenants_affected": len(entry["tenants"]),
            "services": ",".join(sorted(s for s in entry["services"] if s)),
            "row_exactness": worst(entry["exactness"]),
            **basis,
        }
        docs.append(_document(dataset, "signature_window", [sig], f"{sig} window", meta))
        loaded = entries.get(sig)
        author = str((loaded.entry.provenance or {}).get("author", "unknown")) if loaded else "unknown"
        exemplars: list[str] = []
        for day in sorted(days, key=lambda d: d.key, reverse=True):
            for row in day.exemplars.get(sig, []):
                if len(exemplars) < max_exemplars:
                    exemplars.append(row["text"])
        template = f"{sig} ({entry['category']}) pattern {entry['pattern_id']}: {loaded.regex}" if loaded else f"{sig} ({entry['category']}) pattern {entry['pattern_id']}"
        fields = {"template": template}
        if exemplars:
            fields["exemplars"] = "\n\n".join(exemplars)
        signature_meta = {"signature_id": sig, "pattern_id": entry["pattern_id"], "category": entry["category"], "author": author, "reviewed": loaded is not None and loaded.owner == "user"}
        docs.append(_document(dataset, "signature", [sig], sig, signature_meta, fields))
    for (sig, tenant), per in sorted(tenants.items()):
        meta = {"signature_id": sig, "tenant": tenant, "lines_window": per["lines"], "first_seen": per["first"], "last_seen": per["last"], "row_exactness": worst(per["exactness"]), **basis}
        docs.append(_document(dataset, "signature_tenant_window", [sig, tenant], f"{sig} {tenant} window", meta))
    return docs


def _entries_by_signature(library: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if library is None:
        return out
    for loaded in library.library.approved("message"):
        signature = loaded.entry.signature
        if signature is not None:
            out.setdefault(signature.id, loaded)
    return out
