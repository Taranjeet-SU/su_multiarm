"""Synthetic exact-match reuse benchmark; work-dir must be an external test workspace.

Run with PYTHONPATH=src:. and PYTHONDONTWRITEBYTECODE=1. No network or model calls.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from su_multi_arm.core.config import PatternLibraryRef
from su_multi_arm.extraction.patterns.extractor import PatternExtractor
from su_multi_arm.ingestion.logs_parse import match_records
from tests.logs_support import ENGINE, write_library
from tests.unit.test_logs_parse import artifacts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--records", type=int, default=20000)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    write_library(args.work_dir / "library")
    library = PatternExtractor(PatternLibraryRef(path=args.work_dir / "library", engine=ENGINE), None).load()
    base = artifacts().records[0]
    results = []
    for name, distinct in (("repeated", 100), ("timestamp_only", 1), ("unique", args.records)):
        records = [base.model_copy(update={"id": f"record-{i}", "source_id": f"source-{i}",
            "message": base.message.replace("30 ms", f"{i % distinct + 1} ms").replace("10:30:00.000", f"{i // 3600 % 24:02}:{i // 60 % 60:02}:{i % 60:02}.000") if name == "timestamp_only" else base.message.replace("30 ms", f"{i % distinct + 1} ms")}) for i in range(args.records)]
        baseline = match_records(records, library, cache_enabled=False)
        durations = {False: [], True: []}
        stats = {}
        for repeat in range(args.rounds):
            for enabled in ((False, True) if repeat % 2 == 0 else (True, False)):
                start = time.perf_counter()
                rows = match_records(records, library, cache_enabled=enabled, stats=stats)
                durations[enabled].append(time.perf_counter() - start)
                assert rows == baseline
        # Content-free counters for a final enabled pass.
        match_records(records, library, stats=stats)
        old, new = (statistics.median(durations[enabled]) for enabled in (False, True))
        results.append({"workload": name, "records": len(records), "engine": ENGINE,
            "baseline_seconds": round(old, 4), "cached_seconds": round(new, 4),
            "speedup": round(old / new, 3), "output_identical": True, **stats})
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
