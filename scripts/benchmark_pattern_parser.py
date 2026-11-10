"""Offline approved-pattern replay over externally supplied UnitArtifacts JSON files.

No sources, providers, stores or deployment changes. Supply a frozen library/config
and the same units to separate processes for each worker count. Results are aggregate
only; input/output directories must be outside the public repository.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import gzip
import hashlib
import json
import os
from pathlib import Path
import resource
import threading
import time

from su_multi_arm.core.config import load_config
from su_multi_arm.extraction.patterns.extractor import PatternExtractor
from su_multi_arm.ingestion import logs_parse as parser
from su_multi_arm.ingestion.partitions import UnitArtifacts
from su_multi_arm.sources.logs import LogRecord


def timed_worker(payload):
    started = time.perf_counter()
    records = [LogRecord.model_validate_json(row) for row in payload]
    decode = time.perf_counter() - started
    library = parser._WORKER["library"]
    stats = {}
    started = time.perf_counter()
    matches = parser.match_records(
        records, library, parser._WORKER.get("policy"),
        cache=parser._WORKER["cache"], cache_enabled=parser._WORKER["cache_enabled"], stats=stats,
    )
    stats["worker_decode_us"] = int(decode * 1e6)
    stats["worker_match_us"] = int((time.perf_counter() - started) * 1e6)
    return library.identity, matches, stats


class MemoryMonitor:
    """Linux process-tree RSS sum; includes shared-page double counting."""

    def __init__(self):
        self.stop = threading.Event()
        self.peak = 0
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.is_set():
            pending = [os.getpid()]
            total = 0
            while pending:
                pid = pending.pop()
                try:
                    pending.extend(int(v) for v in Path(f"/proc/{pid}/task/{pid}/children").read_text().split())
                    total += int(Path(f"/proc/{pid}/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
                except (OSError, IndexError):
                    pass
            self.peak = max(self.peak, total)
            self.stop.wait(.05)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join()


def benchmark(config, unit_dir, library_path, workers):
    cfg = load_config(config)
    ref = cfg.extraction.pattern_library.model_copy(update={"path": library_path})
    library = PatternExtractor(ref, None).load()
    units = []
    started = time.perf_counter()
    for path in sorted(unit_dir.glob("*.json.gz")):
        with gzip.open(path, "rt") as handle:
            units.append(UnitArtifacts.model_validate_json(handle.read()))
    input_seconds = time.perf_counter() - started
    if not units:
        raise ValueError("no unit artifacts supplied")
    started = time.perf_counter()
    encoded = [record.model_dump_json() for unit in units for record in unit.records]
    encode_seconds = time.perf_counter() - started
    byte_count = sum(len(row.encode()) for row in encoded)
    started = time.perf_counter()
    decoded = [LogRecord.model_validate_json(row) for row in encoded]
    decode_seconds = time.perf_counter() - started
    del encoded, decoded
    calls = []
    original_match, original_worker = parser.parse_matches, parser._worker_batch

    def timed(records, *args, **kwargs):
        started = time.perf_counter()
        result = original_match(records, *args, **kwargs)
        calls.append(time.perf_counter() - started)
        return result

    parser.parse_matches, parser._worker_batch = timed, timed_worker
    cpu = time.process_time()
    try:
        with MemoryMonitor() as memory:
            started = time.perf_counter()
            parsed = parser.parse_units(units, library, workers=workers, batch_size=cfg.logs.parse.batch_size, ref=ref)
            wall = time.perf_counter() - started
    finally:
        parser.parse_matches, parser._worker_batch = original_match, original_worker
    parent_cpu = time.process_time() - cpu
    output = asdict(parsed)
    output.pop("parse_work")
    digest = hashlib.sha256(json.dumps(output, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
    stats = Counter()
    for values in parsed.parse_work.values():
        stats.update(values)
    return {
        "scope": "bounded offline approved-pattern replay; no population coverage estimate",
        "workers": workers, "heads": len(parsed.matches), "units": len(units),
        "distinct_heads": len({r.message.split("\n", 1)[0] for u in units for r in u.records}),
        "services": len({u.unit.attributes.get("service") for u in units}),
        "wall_seconds": wall, "parent_cpu_seconds": parent_cpu,
        "matching_wall_seconds": sum(calls), "aggregation_and_sort_seconds": wall - sum(calls),
        "artifact_load_seconds": input_seconds, "serialization_encode_seconds": encode_seconds,
        "serialization_decode_seconds": decode_seconds, "serialization_bytes": byte_count,
        "peak_tree_rss_mib": memory.peak / 1024**2,
        "parent_peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "parse_work": dict(stats), "statuses": dict(Counter(row["status"] for row in parsed.matches)),
        "output_sha256": digest, "library_identity": library.identity,
        "approved_patterns": len(library.library.approved()),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--units", type=Path, required=True)
    ap.add_argument("--library", type=Path, required=True)
    ap.add_argument("--workers", type=int, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    if args.workers < 1:
        ap.error("workers must be positive")
    report = benchmark(args.config, args.units, args.library, args.workers)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
