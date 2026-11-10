"""``louvain_hierarchical``: deterministic hierarchical communities (plan §3.13, minimal version).

Substrate: the entity–document bipartite graph (the same substrate as ``louvain``). One seeded
Louvain run yields its dendrogram (``networkx.community.louvain_partitions``: one partition per
aggregation pass, each coarser than the last and nesting the previous one). Level 0 is the coarsest
partition; levels 1 and 2 are the next finer passes. A finer community becomes a child only when its
parent has at least ``min_split`` documents and the parent actually splits (a child identical to its
parent is not repeated); every kept community has ``min_documents`` documents and one entity. Ids are
content hashes of ``(level, members)``, so identical input and seed give identical communities.
Louvain (``networkx``, the ``communities`` extra) stands in for Leiden: plan open decision 7 keeps the
GPL ``leidenalg`` out of the core; the hierarchy contract (levels 0..2, parent ids, seeded) is the
same.
"""

from __future__ import annotations

import hashlib
import json

from .base import Community, louvain_partitions


def _id(level: int, members: frozenset[str]) -> str:
    return hashlib.sha256(json.dumps([level, sorted(members)]).encode()).hexdigest()


def build_hierarchical(
    memberships: dict[str, set[str]],
    names: dict[str, str],
    min_documents: int,
    seed: int,
    levels: int = 3,
    resolution: float = 1.0,
    min_split: int = 4,
) -> list[Community]:
    """``memberships``: entity id -> document ids; ``names``: entity id -> display name. Returns every
    level's communities sorted by ``(level, id)``."""
    passes = louvain_partitions(memberships, seed, resolution)
    if not passes:
        return []
    dendrogram = list(reversed(passes))[:levels]  # coarsest first

    def documents(group: frozenset[str]) -> list[str]:
        return sorted(n[2:] for n in group if n.startswith("d:"))

    def keeps(group: frozenset[str]) -> bool:
        return len(documents(group)) >= min_documents and any(n.startswith("e:") for n in group)

    result: list[Community] = []
    parents: dict[frozenset[str], Community] = {}
    for level, partition in enumerate(dendrogram):
        current: dict[frozenset[str], Community] = {}
        for group in sorted(partition, key=sorted):
            if not keeps(group):
                continue
            parent = None
            if level:
                parent = next((c for g, c in parents.items() if group <= g), None)
                if parent is None or len(parent.document_ids) < min_split or len(parent.document_ids) == len(documents(group)) and set(parent.entity_ids) == {n[2:] for n in group if n.startswith("e:")}:
                    continue
            keys = sorted(n[2:] for n in group if n.startswith("e:"))
            community = Community(
                id=_id(level, group),
                label=", ".join(sorted(names[k] for k in keys)),
                document_ids=documents(group),
                entity_ids=keys,
                level=level,
                parent_id=parent.id if parent else None,
            )
            current[group] = community
            result.append(community)
        if not current:
            break
        parents = current
    return sorted(result, key=lambda c: (c.level, c.id))
