from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from .models import Hit

# The alpha's fusion, kept as a pure function so ``tests/parity`` can prove that ``selection.order``
# reproduces it (``reciprocal_rank`` = ``per_arm``). The engine runs ``selection`` only.


@dataclass(frozen=True)
class FusionLike:
    """What ``fuse`` reads: the v1 ``FusionConfig`` shape."""

    strategy: str = "per_arm"
    rrf_k: int = 60
    source_weights: dict = field(default_factory=dict)
    arm_weights: dict = field(default_factory=dict)
    max_documents: int | None = None


def fuse(arms: dict[str, list[Hit]], cfg) -> list[Hit]:
    result = {}
    for arm, hits in arms.items():
        # One vote per document per arm, including multiple planner queries/hops.
        unique = {}
        for hit in hits:
            if hit.document_id not in unique:
                unique[hit.document_id] = hit
        values = [h.score for h in unique.values()]
        low, high = (min(values), max(values)) if values else (0, 0)
        mean, std = (statistics.mean(values), statistics.pstdev(values)) if values else (0, 0)
        for rank, hit in enumerate(unique.values(), 1):
            if cfg.strategy == "rrf":
                score = 1 / (cfg.rrf_k + rank)
            elif cfg.strategy == "minmax":
                score = (hit.score - low) / (high - low) if high != low else 1.0
            elif cfg.strategy == "zscore":
                # CDF normalization makes all present-arm contributions nonnegative.
                score = (1 + math.erf((hit.score - mean) / (std * math.sqrt(2)))) / 2 if std else 0.5
            else:
                score = 1 / rank
            score *= cfg.arm_weights.get(arm, 1)
            if hit.document_id not in result:
                copy = hit.model_copy(deep=True)
                copy.score = 0
                copy.arms = []
                copy.provenance = []
                result[hit.document_id] = copy
            merged = result[hit.document_id]
            merged.score += score
            merged.arms.append(arm)
            merged.chunk_ids = list(dict.fromkeys([*merged.chunk_ids, *hit.chunk_ids]))
            merged.provenance.extend(hit.provenance)
    for hit in result.values():
        hit.score *= cfg.source_weights.get(hit.source, 1)
    hits = sorted(result.values(), key=lambda h: (-h.score, h.document_id))
    # per_arm preserves the union; its rank score affects display, not inclusion unless explicitly capped.
    return hits[: cfg.max_documents] if cfg.max_documents else hits
