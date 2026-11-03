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


class Community(StrictModel):
    id: str = Field(min_length=1)
    label: str = ""
    document_ids: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    report: dict[str, Any] = Field(default_factory=lambda: {"status": "disabled", "claims": []})

    def status(self) -> str:
        return str(self.report.get("status", "disabled"))


def build_communities(
    strategy: str, memberships: dict[str, set[str]], names: dict[str, str], min_documents: int, seed: int
) -> list[Community]:
    """``memberships``: entity id -> document ids; ``names``: entity id -> display name."""
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
