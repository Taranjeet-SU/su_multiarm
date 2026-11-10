"""Co-firing communities and incident documents (addendum A2 §4.1.4, A1 §3.10 / N6).

The input is only the partitions' ``hourly_cells`` (signature x tenant x env x service x hour line
counts). For one tenant at a time (an incident never mixes tenants):

1. A **cell** is ``(tenant, env, hour)`` (``tenant_env_hour``, the default), ``(tenant, env,
   service, hour)`` (``service_hour``) or ``(deployment, hour)`` (``deployment_hour``; needs format
   2 cells, review N8). A signature *fires* in a cell when it has a line there.
2. For every pair of signatures that fired together in at least ``min_support_cells`` cells, the
   **lift** is ``joint * N / (cells_a * cells_b)`` over the tenant's ``N`` cells with any line. Pairs
   below ``min_lift`` are dropped: two signatures that fire everywhere have lift about 1.
3. Signatures linked by kept pairs form connected components (sorted union-find). A component of
   more than ``split_above`` signatures is split with ``communities.base.louvain_partitions`` (the
   deterministic constructor: integer node ids in sorted label order, sorted edges, a fixed seed;
   review B9) over the signature-cell memberships, coarsest pass. Communities with one signature
   are not emitted.
4. ``IncidentStats`` per community: member signatures, services, the co-fire cells' first / peak /
   last hour, lines, the worst exactness of those cells, the top pairs by lift and the hourly profile.

Above ``max_signatures`` per tenant (top by lines) the tail is dropped and reported, never silent.
Membership is statistical co-occurrence, never causality; no LLM summary is written here.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import combinations
from typing import Any, Iterable

from ..core.types import Document

NOT_CAUSAL = "statistical co-occurrence, not causality"


@dataclass
class IncidentStats:
    community_id: str
    tenant: str
    window: tuple[str, str]
    signatures: list[str]
    services: list[str]
    first_hour: str
    peak_hour: str
    last_hour: str
    lines: int
    cells: int
    row_exactness: str
    top_pairs: list[tuple[str, str, float, int]]  # (a, b, lift, joint cells), at most 10
    hour_profile: list[tuple[str, int]]  # co-fire cells: (hour, member lines)

    @property
    def tenants_affected(self) -> int:
        return 1


@dataclass
class CoFiringReport:
    incidents: list[IncidentStats] = field(default_factory=list)
    dropped_signatures: dict[str, int] = field(default_factory=dict)  # tenant -> signatures beyond max_signatures
    pairs_kept: int = 0

    def summary(self) -> dict[str, Any]:
        return {"incidents": len(self.incidents), "pairs_kept": self.pairs_kept, "dropped_signatures": dict(sorted(self.dropped_signatures.items()))}


def _cell_key(cell: dict[str, Any], unit: str) -> tuple[str, ...]:
    if unit == "service_hour":
        return (cell.get("env") or "unknown", cell.get("service") or "unknown", cell["hour"])
    if unit == "deployment_hour":
        if "deployment" not in cell:
            raise ValueError("unit deployment_hour needs format-2 hourly cells (with deployment); reparse first")
        return (cell["deployment"], cell["hour"])
    return (cell.get("env") or "unknown", cell["hour"])  # tenant_env_hour (the tenant is the partition key)


def co_firing(cells: Iterable[dict[str, Any]], cfg: Any) -> CoFiringReport:
    """See the module docstring. ``cfg`` is a ``CoFiringConfig``. Deterministic for a given input
    and seed (no set or dict order reaches the clustering)."""
    from .base import louvain_partitions

    by_tenant: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cell in cells:
        by_tenant[cell.get("tenant") or "unknown"].append(cell)
    report = CoFiringReport()
    hours_all = sorted({c["hour"] for rows in by_tenant.values() for c in rows})
    window = (hours_all[0][:10], hours_all[-1][:10]) if hours_all else ("", "")
    for tenant in sorted(by_tenant):
        rows = by_tenant[tenant]
        totals: Counter[str] = Counter()
        for cell in rows:
            totals[cell["signature_id"]] += int(cell["lines"])
        ranked = sorted(totals, key=lambda s: (-totals[s], s))
        kept = set(ranked[: cfg.max_signatures])
        if len(ranked) > cfg.max_signatures:
            report.dropped_signatures[tenant] = len(ranked) - cfg.max_signatures
        grid: dict[tuple[str, ...], dict[str, int]] = defaultdict(dict)
        meta: dict[tuple[str, ...], dict[str, Any]] = defaultdict(lambda: {"services": set(), "exact": True, "hour": ""})
        for cell in rows:
            key = _cell_key(cell, cfg.unit)
            m = meta[key]
            m["hour"] = cell["hour"]
            m["exact"] = m["exact"] and cell.get("row_exactness", "exact") == "exact"
            if cell["signature_id"] in kept:
                lines = grid[key].get(cell["signature_id"], 0) + int(cell["lines"])
                grid[key][cell["signature_id"]] = lines
                m["services"].add((cell["signature_id"], cell.get("service") or "unknown"))
        n_cells = len(grid)
        if n_cells == 0:
            continue
        fired: Counter[str] = Counter()
        joint: Counter[tuple[str, str]] = Counter()
        for key in sorted(grid):
            names = sorted(grid[key])
            fired.update(names)
            joint.update(combinations(names, 2))
        lifts: dict[tuple[str, str], float] = {}
        for pair in sorted(joint):
            together = joint[pair]
            if together < cfg.min_support_cells:
                continue
            lift = together * n_cells / (fired[pair[0]] * fired[pair[1]])
            if lift >= cfg.min_lift:
                lifts[pair] = lift
        report.pairs_kept += len(lifts)
        if not lifts:
            continue
        partners: dict[str, set[str]] = defaultdict(set)
        for a, b in lifts:
            partners[a].add(b)
            partners[b].add(a)
        memberships: dict[str, set[str]] = {}
        for key in sorted(grid):
            present = grid[key]
            label = "|".join(key)
            for sig in sorted(present):
                if partners.get(sig, set()) & set(present):
                    memberships.setdefault(sig, set()).add(label)
        for members in _groups(lifts, memberships, cfg, louvain_partitions):
            member_set = set(members)
            co_cells = [key for key in sorted(grid) if len(member_set & set(grid[key])) >= 2]
            if not co_cells:
                continue
            profile: dict[str, int] = defaultdict(int)
            services: set[str] = set()
            exact = True
            for key in co_cells:
                hour = meta[key]["hour"]
                profile[hour] += sum(v for s, v in grid[key].items() if s in member_set)
                services.update(svc for s, svc in meta[key]["services"] if s in member_set)
                exact = exact and meta[key]["exact"]
            hours = sorted(profile)
            peak = sorted(hours, key=lambda h: (-profile[h], h))[0]
            pairs = sorted(((a, b, round(lifts[(a, b)], 3), joint[(a, b)]) for a, b in lifts if a in member_set and b in member_set), key=lambda p: (-p[2], p[0], p[1]))
            identity = json.dumps({"tenant": tenant, "signatures": members, "first_hour": hours[0]}, sort_keys=True)
            report.incidents.append(
                IncidentStats(
                    community_id="cofire:" + hashlib.sha256(identity.encode()).hexdigest()[:24],
                    tenant=tenant,
                    window=window,
                    signatures=members,
                    services=sorted(services),
                    first_hour=hours[0],
                    peak_hour=peak,
                    last_hour=hours[-1],
                    lines=sum(profile.values()),
                    cells=len(co_cells),
                    row_exactness="exact" if exact else "approximate",
                    top_pairs=pairs[:10],
                    hour_profile=[(h, profile[h]) for h in hours],
                )
            )
    report.incidents.sort(key=lambda i: (i.tenant, i.first_hour, i.community_id))
    return report


def _groups(lifts: dict[tuple[str, str], float], memberships: dict[str, set[str]], cfg: Any, louvain: Any) -> list[list[str]]:
    """Signature groups: the connected components of the kept-pair graph (sorted union-find, so
    deterministic); a component larger than ``split_above`` is split by the deterministic Louvain
    constructor over its signature-cell memberships, keeping groups of two or more signatures (the
    signatures Louvain leaves alone stay together as the component's remainder). A small, dense
    component is never split: Louvain's modularity is flat on a complete bipartite block, which
    would scatter a planted incident one signature per community."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in sorted(lifts):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    components: dict[str, list[str]] = defaultdict(list)
    for node in sorted(parent):
        components[find(node)].append(node)
    out: list[list[str]] = []
    for root in sorted(components):
        members = sorted(components[root])
        if len(members) <= cfg.split_above:
            out.append(members)
            continue
        passes = louvain({m: memberships.get(m, set()) for m in members}, cfg.seed, cfg.resolution)
        placed: set[str] = set()
        for group in (passes[-1] if passes else []):
            names = sorted(label[2:] for label in group if label.startswith("e:"))
            if len(names) >= 2:
                out.append(names)
                placed.update(names)
        rest = [m for m in members if m not in placed]
        if len(rest) >= 2:
            out.append(rest)
    return out


def incident_header(stats: IncidentStats) -> str:
    """Deterministic header: every co-fired pair as ``a ~ b`` so a ``co_fires_with`` edge's span
    covers both signature names."""
    pairs = "; ".join(f"{a} ~ {b} (lift {lift:g}, cells {cells})" for a, b, lift, cells in stats.top_pairs)
    return (
        f"kind: incident | tenant: {stats.tenant} | signatures: {', '.join(stats.signatures)} | services: {', '.join(stats.services)} | "
        f"first: {stats.first_hour} | peak: {stats.peak_hour} | last: {stats.last_hour} | lines: {stats.lines} ({stats.row_exactness}) | co_fires: {pairs}"
    )


def incident_summary(stats: IncidentStats) -> str:
    return (
        f"Signatures {', '.join(stats.signatures)} fired together in {stats.cells} hourly cells for tenant {stats.tenant} "
        f"between {stats.first_hour} and {stats.last_hour} (peak {stats.peak_hour}, {stats.lines} lines, {stats.row_exactness}); "
        f"{NOT_CAUSAL}."
    )


def incident_documents(dataset: str, report: CoFiringReport, basis: dict[str, str]) -> list[Document]:
    """One ``incident`` document per co-firing community (``community_id`` metadata, read by
    ``communities.strategy: declared``)."""
    out = []
    for stats in report.incidents:
        metadata: dict[str, Any] = {
            "community_id": stats.community_id,
            "community_label": f"incident {', '.join(stats.signatures)}",
            "tenant": stats.tenant,
            "date": stats.first_hour[:10],
            "window_first": stats.window[0],
            "window_last": stats.window[1],
            "first_hour": stats.first_hour,
            "peak_hour": stats.peak_hour,
            "last_hour": stats.last_hour,
            "lines": stats.lines,
            "cells": stats.cells,
            "tenants_affected": 1,
            "signatures": ",".join(stats.signatures),
            "services": ",".join(stats.services),
            "pairs": ";".join(f"{a}|{b}|{lift:g}|{cells}" for a, b, lift, cells in stats.top_pairs),
            "hour_profile": ";".join(f"{h}={n}" for h, n in stats.hour_profile),
            "row_exactness": stats.row_exactness,
            **basis,
        }
        out.append(
            Document(
                id="logs:incident:" + stats.community_id.split(":", 1)[1],
                dataset=dataset,
                source="logs",
                kind="incident",
                title=f"incident {stats.tenant} {stats.first_hour}",
                fields={"header": incident_header(stats), "summary": incident_summary(stats)},
                metadata=metadata,
            )
        )
    return out


def parse_hour(value: str) -> datetime:
    return datetime.fromisoformat(value)


def last_day(document: Document) -> date | None:
    value = document.metadata.get("last_hour")
    return parse_hour(value).date() if isinstance(value, str) else None
