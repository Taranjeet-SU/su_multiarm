"""Parity: ``selection.ordering`` reproduces ``fusion.fuse`` for every v1 strategy on fixed inputs.

``reciprocal_rank`` (v2 default) must equal v1 ``per_arm``; ``rrf``, ``minmax`` and ``zscore`` keep
their formulas. Both receive the same per-arm order (the engine sorts per arm before fusing).
"""

from __future__ import annotations

import pytest

from su_multi_arm.compat.config_v1 import FusionConfig
from su_multi_arm.fusion import fuse
from su_multi_arm.models import Hit
from su_multi_arm.selection import Candidate, OrderingPolicy, order

ARMS = {
    "dense": [("kb:a", "article", 0.91), ("kb:b", "article", 0.90), ("case:c", "case", 0.42), ("kb:d", "article", 0.42)],
    "lexical": [("case:c", "case", 14.2), ("kb:a", "article", 9.5), ("case:e", "case", 1.0), ("kb:a", "article", 0.3)],
    "graph": [("kb:f", "article", 1.0), ("kb:b", "article", 1.0), ("case:e", "case", 1.0)],
    "community": [("kb:d", "article", 2.0)],
}
WEIGHTS = {"arm_weights": {"lexical": 1.7, "graph": 0.5}, "source_weights": {"case": 1.25}}


def hits():
    return {
        arm: [Hit(document_id=i, title=i, source=s, url=None, text="", chunk_ids=[f"{i}:{n}"], score=sc, arms=[arm]) for n, (i, s, sc) in enumerate(rows)]
        for arm, rows in ARMS.items()
    }


def candidates():
    return {arm: [Candidate(record_id=i, arm=arm, score=sc, source=s, chunk_ids=[f"{i}:{n}"]) for n, (i, s, sc) in enumerate(rows)] for arm, rows in ARMS.items()}


@pytest.mark.parametrize(("v1", "v2"), [("per_arm", "reciprocal_rank"), ("per_arm", "per_arm"), ("rrf", "rrf"), ("minmax", "minmax"), ("zscore", "zscore")])
@pytest.mark.parametrize("weighted", [False, True])
@pytest.mark.parametrize("cap", [None, 3])
def test_ordering_matches_fuse(v1, v2, weighted, cap):
    extra = WEIGHTS if weighted else {}
    expected = fuse(hits(), FusionConfig(strategy=v1, rrf_k=60, max_documents=cap, **extra))
    actual, trace = order(candidates(), OrderingPolicy(kind=v2, rrf_k=60, max_documents=cap, **extra))
    assert [h.document_id for h in expected] == [r.record_id for r in actual]
    assert [h.score for h in expected] == pytest.approx([r.score for r in actual])
    assert [h.arms for h in expected] == [r.arms for r in actual]
    assert [h.chunk_ids for h in expected] == [r.chunk_ids for r in actual]
    assert trace.union_size == 6 and trace.returned == (cap or 6)
