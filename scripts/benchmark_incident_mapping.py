"""Bounded offline comparison of an optional file-only incident mapping adapter.

Supply external descriptor JSON, config, helper and output directory. No source or
provider clients are constructed. Cached inputs are still verified by the adapter.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
from pathlib import Path
import sys
import time

from benchmark_pattern_parser import MemoryMonitor
from su_multi_arm.core.config import load_config


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--adapter", type=Path, required=True)
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--descriptors", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-heads", type=int, default=200_000)
    args = ap.parse_args()
    sys.path.insert(0, str(args.adapter.resolve().parent))
    adapter = importlib.import_module(args.adapter.stem)
    if Path(adapter.__file__).resolve() != args.adapter.resolve():
        raise ValueError("adapter module collision")
    cfg = load_config(args.config)
    descriptors = json.loads(args.descriptors.read_text())
    upper_bound = 0
    for descriptor in descriptors:
        seed = json.loads(Path(descriptor["seed_manifest"]).read_text())
        upper_bound += seed["selected_total"]
        for context in descriptor.get("context_manifests", []):
            upper_bound += json.loads(Path(context).read_text()).get("records_to_emit", 0)
    if not descriptors or upper_bound > args.max_heads:
        raise ValueError("benchmark descriptors empty or exceed explicit record budget")
    args.output.mkdir(parents=True, exist_ok=False)
    reports = []
    cache = args.output / "canonical-cache"
    for label in ("serial-direct", "cold-parallel", "warm-cache"):
        report = {}
        with MemoryMonitor() as memory:
            started = time.perf_counter()
            if label == "serial-direct":
                units = [adapter.to_unit_artifacts(adapter.load_bundle(
                    d["seed_manifest"], d.get("context_manifests", [])), cfg) for d in descriptors]
            else:
                units = list(adapter.mapped_units(
                    cfg, descriptors, workers=args.workers, cache=cache, report=report))
            elapsed = time.perf_counter() - started
        digest = hashlib.sha256()
        for unit in sorted(units, key=lambda item: item.unit.index):
            digest.update(unit.model_dump_json().encode())
        report.update(label=label, seconds=elapsed, units=len(units),
                      heads=sum(len(unit.records) for unit in units),
                      peak_tree_rss_mib=memory.peak / 1024**2,
                      semantic_sha256=digest.hexdigest())
        reports.append(report)
        print(json.dumps(report), flush=True)
        del units
    if len({report["semantic_sha256"] for report in reports}) != 1:
        raise ValueError("serial/parallel/cache semantic mismatch")
    (args.output / "results.json").write_text(json.dumps(reports, indent=2) + "\n")


if __name__ == "__main__":
    main()
