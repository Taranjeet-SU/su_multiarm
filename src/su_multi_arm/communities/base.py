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


def louvain_partitions(memberships: dict[str, set[str]], seed: int, resolution: float = 1.0) -> list[list[frozenset[str]]]:
    """Seeded Louvain over the entity-document bipartite graph, **reproducible across processes**
    (O3): nodes are integers assigned in sorted label order (``e:<entity>`` and ``d:<document>``)
    and edges are added in sorted order, so neither ``networkx``'s internal sets nor the caller's
    set/dict order depend on ``PYTHONHASHSEED``. Returns every pass (finest first) as label sets;
    ``[]`` when the graph has no edge."""
    import networkx as nx

    labels = sorted({"e:" + key for key in memberships} | {"d:" + doc for docs in memberships.values() for doc in docs})
    number = {label: n for n, label in enumerate(labels)}
    graph = nx.Graph()
    graph.add_nodes_from(range(len(labels)))
    graph.add_edges_from(sorted((number["e:" + key], number["d:" + doc]) for key in memberships for doc in memberships[key]))
    if not graph.number_of_edges():
        return []
    passes = nx.community.louvain_partitions(graph, seed=seed, resolution=resolution)
    return [sorted((frozenset(labels[n] for n in group) for group in partition), key=sorted) for partition in passes]


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
    partitions = louvain_partitions(memberships, seed)
    if not partitions:
        return []
    result = []
    for group in partitions[-1]:
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


DECLARED_FIELD = "community_id"
DECLARED_STATS = ("window_first", "window_last", "first_hour", "peak_hour", "last_hour", "lines", "signatures", "services", "tenant", "row_exactness", "pairs")


def declared_communities(corpus: list[Any], mentions: list[Any], min_documents: int) -> list[Community]:
    """``communities.strategy: declared``: one community per distinct ``community_id`` metadata
    value (the co-firing incidents of the logs profile, ``communities/cooccurrence.py``). Members
    are those documents; entities are the mentions anchored in them; ``stats`` copies the declared
    incident statistics. Deterministic: ids and members are sorted, no clustering runs."""
    groups: dict[str, list[Any]] = {}
    for document in corpus:
        value = document.metadata.get(DECLARED_FIELD)
        if isinstance(value, str) and value:
            groups.setdefault(value, []).append(document)
    entities: dict[str, set[str]] = {}
    for mention in mentions:
        entities.setdefault(mention.span.document_id, set()).add(mention.id)
    out = []
    for key in sorted(groups):
        docs = sorted(groups[key], key=lambda d: d.id)
        if len(docs) < min_documents:
            continue
        first = docs[0]
        stats = {k: first.metadata[k] for k in DECLARED_STATS if k in first.metadata}
        out.append(
            Community(
                id=key,
                label=str(first.metadata.get("community_label") or first.title or key),
                document_ids=[d.id for d in docs],
                entity_ids=sorted(set().union(*(entities.get(d.id, set()) for d in docs))),
                stats=stats,
            )
        )
    return out
