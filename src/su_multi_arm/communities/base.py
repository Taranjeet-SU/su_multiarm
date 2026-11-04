"""Community objects and the two Phase 0 strategies (``entity``, ``louvain``).

A community is a set of documents that share entities. Membership is a shared topic, never a causal
fact. Reports are objects with a status; a report that is disabled, rejected or has no evidence never
counts as coverage. Louvain needs the optional ``networkx`` dependency (``communities`` extra).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import Field

from ..models import StrictModel

ReportStatus = str  # disabled | no_evidence_within_budget | partial | complete_input | rejected


# Key under which a stored report carries the hierarchy and statistics (backends persist five
# columns: id, label, documents, entities, report; rows written before 2.0.0a3 simply lack it).
STORAGE_KEY = "_community"


class Community(StrictModel):
    id: str = Field(min_length=1)
    label: str = ""
    document_ids: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    report: dict[str, Any] = Field(default_factory=lambda: {"status": "disabled", "claims": []})
    level: int = Field(default=0, ge=0, le=2)  # 0 = coarsest (``louvain_hierarchical``)
    parent_id: str | None = None
    stats: dict[str, Any] = Field(default_factory=dict)  # ``communities/stats.py``; empty when not configured

    def status(self) -> str:
        return str(self.report.get("status", "disabled"))

    def storage_report(self) -> dict[str, Any]:
        """The report as backends persist it: hierarchy and statistics ride along under
        ``STORAGE_KEY`` when they differ from the defaults (so existing rows are unchanged)."""
        meta = {k: v for k, v in (("level", self.level), ("parent_id", self.parent_id), ("stats", self.stats)) if v}
        return {**self.report, STORAGE_KEY: meta} if meta else dict(self.report)

    @classmethod
    def from_storage(cls, id: str, label: str, document_ids: list[str], entity_ids: list[str], report: dict[str, Any]) -> Community:
        report = dict(report)
        meta = report.pop(STORAGE_KEY, None) or {}
        return cls(id=id, label=label, document_ids=document_ids, entity_ids=entity_ids, report=report, **meta)


def build_communities(
    strategy: str, memberships: dict[str, set[str]], names: dict[str, str], min_documents: int, seed: int, hierarchy: Any = None
) -> list[Community]:
    """``memberships``: entity id -> document ids; ``names``: entity id -> display name. ``hierarchy``
    (``CommunityHierarchy``) tunes ``louvain_hierarchical``; defaults apply when it is ``None``."""
    if strategy == "louvain_hierarchical":
        from .hierarchy import build_hierarchical

        options = {} if hierarchy is None else {"levels": hierarchy.levels, "resolution": hierarchy.resolution, "min_split": hierarchy.min_split}
        return build_hierarchical(memberships, names, min_documents, seed, **options)
    if strategy == "entity":
        return [
            Community(id=key, label=names[key], document_ids=sorted(docs), entity_ids=[key])
            for key, docs in sorted(memberships.items())
            if len(docs) >= min_documents
        ]
    if strategy != "louvain":
        raise ValueError(f"unknown community strategy {strategy!r}")
    import networkx as nx

    graph = nx.Graph()
    for key, docs in memberships.items():
        graph.add_node("e:" + key)
        for doc in docs:
            graph.add_edge("e:" + key, "d:" + doc)
    if not graph.number_of_edges():
        return []
    result = []
    for group in nx.community.louvain_communities(graph, seed=seed):
        keys = {n[2:] for n in group if n.startswith("e:")}
        docs = {n[2:] for n in group if n.startswith("d:")}
        if len(docs) >= min_documents and keys:
            key = hashlib.sha256(json.dumps(sorted(group)).encode()).hexdigest()
            result.append(
                Community(
                    id=key,
                    label=", ".join(sorted(names[k] for k in keys)),
                    document_ids=sorted(docs),
                    entity_ids=sorted(keys),
                )
            )
    return sorted(result, key=lambda c: c.id)
