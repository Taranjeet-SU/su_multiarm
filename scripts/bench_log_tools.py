"""Read-latency and append benchmark of the log tools over N synthetic days (addendum A2 rev 4:
log tools p95 <= 200 ms and graph neighbourhood p95 <= 500 ms at 180 days, within 2x of the
10-day baseline; H1: appending day N+1 writes 0 rows for days 1..N in <= 10 min and <= 4 GB).
Synthetic gold generator only; the work directory must be outside the repository.

    PYTHONPATH=src:. python scripts/bench_log_tools.py --days 180 --append --workdir ~/su_mutliarm_tests/<you>/bench

Prints one JSON report: build time, documents, history rows, per-tool p50/p95 over ``--repeat``
calls and, with ``--append``, the append of day N+1 (rows written, seconds, peak RSS).
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import time
from dataclasses import replace
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--repeat", type=int, default=20)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--no-tier", action="store_true", help="compose without the history tier")
    parser.add_argument("--append", action="store_true", help="then pull and compose day N+1")
    parser.add_argument("--full-range", action="store_true", help="windows of the whole history instead of the last 10 days")
    parser.add_argument("--opensearch", default=None, help="history rows on this OpenSearch URL (a random index prefix, deleted at the end)")
    args = parser.parse_args()

    import tests.logs_gold_support as support
    from su_multi_arm.testing.fixtures.logs.gold import GoldSpec, day_of

    spec = replace(GoldSpec(), days=args.days + (1 if args.append else 0))
    support.SPEC = spec
    support.AS_OF = day_of(spec, args.days)
    workdir = args.workdir.expanduser()
    workdir.mkdir(parents=True, exist_ok=True)
    data = support.gold_config(workdir)
    data["logs"]["retention"]["keep_days"] = args.days
    data["logs"]["history"] = {"tier": not args.no_tier}
    prefix = None
    if args.opensearch:
        import os
        import secrets

        os.environ["SU_BENCH_OS_HOSTS"] = args.opensearch
        prefix = f"su_bench_h1_{secrets.token_hex(3)}"
        data["backends"]["os"] = {"kind": "opensearch", "hosts_env": "SU_BENCH_OS_HOSTS", "index_prefix": prefix}
        data["logs"]["history"]["backend"] = "os"
    days = [d for d in support.pulled_days(spec) if d <= support.AS_OF]
    started = time.perf_counter()
    engine, pipe, report = support.build_gold(workdir, data=data, days=days)
    build = time.perf_counter() - started
    as_of = support.AS_OF.isoformat()
    # the same questions (fixed windows) at every history length: the history tier must keep their
    # cost independent of how much history is kept; ``--full-range`` also times whole-history ranges
    w = f"last {args.days} days" if args.full_range else "last 10 days"
    calls = {
        "log_top": {"by": "service", "level": ["error"], "window": w},
        "log_new": {"window": w},
        "log_affected": {"signature_id": "feature.flag_conflict", "window": w},
        "log_trend": {"signature_id": "dependency.connect_failed", "window": w},
        "log_compare": {"periods": ["yesterday", "today"], "service": "svc-api"},
        "log_tenants": {"service": "svc-auth", "level": "fatal", "window": w},
        "log_around": {"at": f"{day_of(spec, 5).isoformat()}T14:00:00+00:00", "tenant": "t02"},
        "log_endpoints": {"service": "svc-api", "metric": "latency_max", "window": w},
        "log_signature": {"signature_id": "process.crashed", "window": w},
        "log_coverage": {"window": "last 7 days"},
        "graph_neighbourhood": {"entity": "svc-api"},
    }
    timings: dict[str, dict[str, float]] = {}
    for name, call in calls.items():
        payload = call if name in {"log_around", "graph_neighbourhood"} else {**call, "as_of": as_of}
        samples = []
        for _ in range(args.repeat):
            t = time.perf_counter()
            engine.call_tool(name, payload)
            samples.append((time.perf_counter() - t) * 1000)
        samples.sort()
        timings[name] = {"p50_ms": round(statistics.median(samples), 1), "p95_ms": round(samples[max(0, int(0.95 * len(samples)) - 1)], 1)}
    out = {
        "days": args.days,
        "tier": not args.no_tier,
        "windows": w,
        "build_seconds": round(build, 1),
        "generation_documents": report["documents"],
        "history_rows": engine.history.row_count() if engine.history is not None else 0,
        "timings": timings,
    }
    if args.append and engine.history is not None:
        before = {s["shard_id"]: s["rows"] for s in engine.history.shards()}
        next_day = day_of(spec, args.days + 1)
        t = time.perf_counter()
        library = pipe.library()
        pipe.pull(next_day, library)
        pull = time.perf_counter() - t
        result = pipe.compose(next_day, library)
        total = time.perf_counter() - t
        after = {s["shard_id"]: s["rows"] for s in engine.history.shards()}
        out["append"] = {
            "day": next_day.isoformat(),
            "pull_seconds": round(pull, 1),
            "seconds": round(total, 1),
            "rows_written": result.history_rows,
            "days_appended": result.history_appended,
            "old_shards_unchanged": all(after.get(k) == v for k, v in before.items()),
            "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        }
    out["history_backend"] = "opensearch" if prefix else "sqlite"
    if prefix:
        engine.backends["os"].transport.request("DELETE", f"/{prefix}_*", params={"ignore_unavailable": "true"})
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
