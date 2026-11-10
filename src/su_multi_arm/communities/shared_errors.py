"""Daily shared error patterns from actual joint classified hourly cells, never inferred pairs.

``level_hours`` carries severity and joint dimensions; legacy ``hourly_cells`` does not. Each
support document contains only one tenant/environment/service/level/day. It never contains other
tenants' names or global support counts. Communities join these documents by signature and day;
the stable family ID joins the same signature across days without broadening a document's scope.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..core.config_logs import SharedErrorConfig
from ..core.types import Document

SHARED_ERROR_BUILDER_VERSION = 3
TEMPLATE_PREVIEW_CHARS = 240


def _signature_label(signature: str) -> str:
    if re.fullmatch(r"(?:llm|syn|gen)\.[0-9a-f]{6,}", signature):
        return "Classified error pattern"
    return signature.removeprefix("sig.").replace("_", " ").replace(".", " ").replace("-", " ").capitalize()


def approved_error_labels(library: Any) -> dict[str, dict[str, Any]]:
    """Labels from effective approved pattern definitions only. Never read examples or window
    templates: those may contain another tenant's exemplar. Approval is not human review.
    The unexpanded expression preserves mask placeholders instead of copying vocabulary values.
    """
    labels: dict[str, dict[str, Any]] = {}
    for loaded in library.library.approved("message"):
        entry = loaded.entry
        if entry.signature is None or entry.signature.id in labels:
            continue
        name = entry.provenance.get("issue_name")
        label = " ".join(name.split()) if isinstance(name, str) and name.strip() else _signature_label(entry.signature.id)
        labels[entry.signature.id] = {
            "error_type": label[:120], "label_truncated": len(label) > 120,
            "label_source": "pattern_issue_name" if isinstance(name, str) and name.strip() else "signature_id",
            "template": entry.regex, "pattern_id": entry.id,
            "pattern_origin": loaded.owner, "pattern_reviewed": library.library.reviewed(loaded),
            "category": entry.signature.category,
        }
    return labels


def error_labels_identity(labels: Mapping[str, dict[str, Any]]) -> str:
    """Issue names/review decisions are excluded from matching identity but change document text."""
    return hashlib.sha256(json.dumps(labels, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _id(parts: list[str]) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:24]


@dataclass
class SharedErrorReport:
    documents: list[Document] = field(default_factory=list)
    groups: int = 0
    dropped_groups: int = 0
    excluded_cells: int = 0
    ineligible_cells: int = 0
    unavailable_days: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {"groups": self.groups, "documents": len(self.documents), "dropped_groups": self.dropped_groups, "excluded_cells": self.excluded_cells, "ineligible_cells": self.ineligible_cells, "unavailable_days": self.unavailable_days}


def shared_error_documents(dataset: str, cells: Iterable[dict[str, Any]], cfg: SharedErrorConfig, basis: dict[str, str], labels: Mapping[str, dict[str, Any]] | None = None) -> SharedErrorReport:
    """Cells must include ``date`` (partition day), ``attributes`` and the format-2 ``level_hours``
    fields. Missing severity/scope is ineligible, never inferred from the signature's name.
    ``lines`` may be estimated; minimum tenant support uses observed ``lines_pulled`` unless
    the row carries reconciled exact grouped occurrence counts (``method: exact_groups``).
    """
    report = SharedErrorReport()
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    excluded = set(cfg.exclude_signatures)
    for cell in cells:
        signature = cell.get("signature_id")
        attrs = cell.get("attributes") or {}
        if signature in excluded:
            report.excluded_cells += 1
            continue
        if not (
            isinstance(signature, str) and signature and cell.get("status") == "parsed"
            and str(cell.get("level", "")).upper() in {"ERROR", "FATAL"}
            and all(isinstance(attrs.get(key), str) and attrs[key] not in {"", "unknown"} for key in ("tenant", "service"))
            and isinstance(cell.get("date"), str) and isinstance(cell.get("hour"), str)
            and type(cell.get("lines")) is int and cell["lines"] > 0
            and type(cell.get("lines_pulled")) is int and 0 < cell["lines_pulled"] <= cell["lines"]
        ):
            report.ineligible_cells += 1
            continue
        grouped[(cell["date"], signature)].append(cell)
    accepted = []
    for (day, signature), rows in sorted(grouped.items()):
        tenant_support: Counter[str] = Counter()
        for row in rows:
            tenant_support[row["attributes"]["tenant"]] += _support(row)
        tenants = {tenant for tenant, support in tenant_support.items() if support >= cfg.min_lines_per_tenant}
        if len(tenants) >= cfg.min_tenants:
            supported = [row for row in rows if row["attributes"]["tenant"] in tenants]
            accepted.append((day, signature, supported))
    accepted.sort(key=lambda group: (-sum(_support(row) for row in group[2]), group[0], group[1]))
    report.dropped_groups = max(0, len(accepted) - cfg.max_groups)
    for day, signature, rows in accepted[:cfg.max_groups]:
        context = (labels or {}).get(signature, {})
        error_type = str(context.get("error_type") or _signature_label(signature))
        template = str(context.get("template") or "")
        template_truncated = len(template) > TEMPLATE_PREVIEW_CHARS
        family = "shared_error:" + _id([dataset, signature])
        community = "shared_error:" + _id([dataset, signature, day])
        buckets: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            attrs = row["attributes"]
            buckets[(attrs["tenant"], attrs.get("env") or "unknown", attrs["service"], attrs.get("deployment") or f"{attrs['tenant']}-{attrs.get('env') or 'unknown'}", row["level"].upper())].append(row)
        for (tenant, env, service, deployment, level), member in sorted(buckets.items()):
            hours = sorted({row["hour"] for row in member})
            lines = sum(row["lines"] for row in member)
            pulled = sum(row["lines_pulled"] for row in member)
            exactness = "exact" if all(row.get("row_exactness") == "exact" for row in member) else "approximate"
            grouped_counts = any(row.get("method") == "exact_groups" for row in member)
            metadata = {
                **basis, "community_id": community, "community_family_id": family,
                "community_label": f"{error_type} on {day}",
                "date": day, "window_first": day, "window_last": day,
                "tenant": tenant, "env": env, "service": service, "deployment": deployment, "deployment_id": deployment,
                "signature_id": signature, "level": level, "status": "parsed", "lines": lines, "lines_pulled": pulled,
                "first_hour": hours[0], "last_hour": hours[-1], "cells": len(member), "row_exactness": exactness,
                "method": ",".join(sorted({str(row.get("method") or "unknown") for row in member})),
                "extraction_source": "deterministic",
                "evidence_basis": "grouped_occurrences_and_downloaded_examples" if grouped_counts else "downloaded_records",
                "error_type": error_type,
                "label_source": context.get("label_source", "signature_id"),
                "label_truncated": bool(context.get("label_truncated", False)),
                "template_preview_truncated": template_truncated,
                **{key: context[key] for key in ("pattern_id", "pattern_origin", "pattern_reviewed", "category") if key in context},
            }
            header = f"kind: shared_error | tenant: {tenant} | service: {service} | signature_id: {signature} | date: {day} | level: {level} | lines: {lines} ({exactness})"
            observed = f"{pulled} downloaded examples" if grouped_counts else f"{pulled} observed"
            summary = f"{error_type} (pattern {signature}) occurred for client {tenant}, service {service}, environment {env}, deployment {deployment}, on {day}: {lines} {level} events ({exactness}; {observed}). First hour {hours[0]}, last hour {hours[-1]}. Pattern membership does not establish simultaneous failure, a common cause, or an active alert."
            if context.get("pattern_reviewed") is False:
                summary += " Pattern label is provisional; no human review is recorded."
            if template:
                summary += f" Stored pattern expression: {template[:TEMPLATE_PREVIEW_CHARS]}"
                if template_truncated:
                    summary += "… (preview truncated; full expression in the template field)."
            fields = {"header": header, "summary": summary, **({"template": template} if template else {})}
            report.documents.append(Document(id="logs:shared_error:" + _id([community, tenant, env, service, deployment, level]), dataset=dataset, source="logs", kind="shared_error", title=f"{error_type} · {tenant} · {service} · {day}", fields=fields, metadata=metadata))
        report.groups += 1
    report.documents.sort(key=lambda document: document.id)
    return report


def _support(row: dict[str, Any]) -> int:
    return row["lines"] if row.get("method") == "exact_groups" and row.get("row_exactness") == "exact" else row["lines_pulled"]


def shared_error_stats(documents: list[Document]) -> dict[str, Any]:
    """Recompute from exactly the supplied member documents; never copy a first member's counts."""
    if documents and all(d.metadata.get("collection_scope") == "incident_episode_summaries_v1" for d in documents):
        return {
            "kind": "shared_rule", "members": len(documents),
            "tenants_affected": len({d.metadata["tenant"] for d in documents}),
            "services_affected": len({d.metadata["service"] for d in documents}),
            "rule_matched_messages": sum(d.metadata["lines"] for d in documents),
            "window_first": min(d.metadata["date"] for d in documents),
            "window_last": max(d.metadata["date"] for d in documents),
            "first_hour": min(d.metadata["first_hour"] for d in documents),
            "last_hour": max(d.metadata["last_hour"] for d in documents),
            "membership": "same detection rule in source snapshots; not a common cause, outage, or source severity count",
        }
    return {
        "kind": "shared_error", "members": len(documents),
        "tenants_affected": len({document.metadata["tenant"] for document in documents}),
        "services_affected": len({document.metadata["service"] for document in documents}),
        "lines": sum(document.metadata["lines"] for document in documents),
        "lines_pulled": sum(document.metadata["lines_pulled"] for document in documents),
        "window_first": min(document.metadata["date"] for document in documents),
        "window_last": max(document.metadata["date"] for document in documents),
        "first_hour": min(document.metadata["first_hour"] for document in documents),
        "last_hour": max(document.metadata["last_hour"] for document in documents),
        "row_exactness": "exact" if all(document.metadata["row_exactness"] == "exact" for document in documents) else "approximate",
        "membership": "same error signature on the same partition day; not causality or a monitored alert",
    }
