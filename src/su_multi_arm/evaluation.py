from __future__ import annotations

import statistics
from .models import SearchRequest


def evaluate(engine, cases: list[dict], cutoffs=(5, 10, 20, 100)):
    """Gold document recall, MRR and coverage; never grade a model using itself."""
    rows = []
    latencies = []
    for case in cases:
        expected = set(case.get("expected_ids", []))
        result = engine.search(SearchRequest.model_validate(case["request"]))
        ids = [h.document_id for h in result.hits]
        latencies.append(result.trace["seconds"])
        rows.append(
            {
                "id": case["id"],
                "labelled": bool(expected),
                "expected_count": len(expected),
                "recall": {
                    str(k): len(expected & set(ids[:k])) / len(expected) if expected else None
                    for k in cutoffs
                },
                "matches": {str(k): len(expected & set(ids[:k])) for k in cutoffs},
                "pool_recall": len(expected & set(ids)) / len(expected) if expected else None,
                "reciprocal_rank": next((1 / (i + 1) for i, doc in enumerate(ids) if doc in expected), 0)
                if expected
                else None,
                "arm_recall": {
                    arm: len(expected & {h.document_id for h in hits}) / len(expected) if expected else None
                    for arm, hits in result.arm_hits.items()
                },
                "seconds": result.trace["seconds"],
            }
        )
    labelled = [r for r in rows if r["labelled"]]
    total = sum(r["expected_count"] for r in labelled)
    return {
        "queries": len(rows),
        "labelled_queries": len(labelled),
        "expected_documents": total,
        "macro_recall": {
            str(k): statistics.mean(r["recall"][str(k)] for r in labelled) if labelled else None
            for k in cutoffs
        },
        "micro_recall": {
            str(k): sum(r["matches"][str(k)] for r in labelled) / total if total else None for k in cutoffs
        },
        "mrr": statistics.mean(r["reciprocal_rank"] for r in labelled) if labelled else None,
        "median_seconds": statistics.median(latencies) if latencies else None,
        "rows": rows,
    }
