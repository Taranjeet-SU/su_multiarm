"""Client vs native records aggregation on a Lucene engine (``records.aggregation``; informational).

Writes ``--docs`` synthetic log-like records (service, level, status, latency, timestamp, tenant,
user; deterministic, seeded) into one records index of a throwaway server, then runs a fixed list of
``ScopeSpec`` questions under the ACL through both modes on the same index and reports the median
latency of each, the speed-up, and whether the results agree (``agree``: float sums within a relative
1e-9; ``identical``: every field bit-equal). The client path's ``max_matched`` is
raised above the corpus size so it can answer at all (its default refuses above 10000 records).
Results agree when every ``ScopeResult`` field is equal except the id list, which native caps at
``matched_ids_cap`` (``matched_ids_truncated``); the capped list must be the client list's prefix.

Point it only at the throwaway stack (``tests/services/``): it creates and deletes its own random
index prefix. Output (JSON) goes to ``--output``, outside the repository.

    python scripts/bench_native_aggregation.py --url http://127.0.0.1:19200 --engine opensearch \
        --docs 200000 --output ~/su_mutliarm_tests/<agent>/aggs/os2.json
"""

from __future__ import annotations

import argparse
import json
import random
import secrets
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from su_multi_arm.core.types import FieldSpec, GenerationId, Measure, MetadataSchema, Period, Predicate, Provenance, ScopeSpec, StructuredRecord

GEN = GenerationId(dataset="aggbench", generation="e" * 32, index_format=3, fingerprint="f" * 64)
SCHEMA = MetadataSchema(
    canonical_unit="line",
    timezone="UTC",
    fields=[
        FieldSpec(name="service", type="string", groupable=True),
        FieldSpec(name="level", type="string", groupable=True),
        FieldSpec(name="status", type="integer", groupable=True),
        FieldSpec(name="latency_ms", type="number"),
        FieldSpec(name="ts", type="datetime", groupable=True, time_basis="logged"),
        FieldSpec(name="tenant", type="string"),
        FieldSpec(name="user", type="id"),
    ],
)
LEVELS = ["debug", "info", "info", "info", "warn", "error"]
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
ACL = [Predicate(field="tenant", op="in", value=[f"tenant{i:02d}" for i in range(10)])]


def rows(n: int, seed: int = 7) -> list[StructuredRecord]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        values: dict[str, Any] = {
            "service": f"svc{rng.randrange(50):02d}",
            "level": rng.choice(LEVELS),
            "status": rng.choice([200, 200, 200, 201, 204, 301, 400, 404, 500, 503]),
            "latency_ms": round(rng.lognormvariate(3, 1), 3),
            "ts": (START + timedelta(seconds=rng.randrange(30 * 86400))).isoformat(),
            "tenant": f"tenant{rng.randrange(20):02d}",
            "user": f"u{rng.randrange(20000):05d}",
        }
        if rng.random() < 0.02:
            values["latency_ms"] = None
        out.append(StructuredRecord(schema_id="line", id=f"l{i:08d}", values=values, provenance=Provenance(extractor="bench")))
    return out


def questions() -> list[tuple[str, ScopeSpec]]:
    periods = [Period(label="week1", lower="2026-01-01T00:00:00+00:00", upper="2026-01-08T00:00:00+00:00"), Period(label="week2", lower="2026-01-08T00:00:00+00:00", upper="2026-01-15T00:00:00+00:00")]
    return [
        ("count", ScopeSpec(measure=Measure(kind="count"))),
        ("count errors", ScopeSpec(predicates=[Predicate(field="level", op="eq", value="error")])),
        ("group by service", ScopeSpec(group_by=["service"])),
        ("top 5 services by errors", ScopeSpec(predicates=[Predicate(field="level", op="eq", value="error")], group_by=["service"], top_n=5)),
        ("cross-tab service x level", ScopeSpec(group_by=["service", "level"])),
        ("errors per day", ScopeSpec(predicates=[Predicate(field="level", op="eq", value="error")], group_by=["ts"], granularity="day")),
        ("avg latency by service", ScopeSpec(group_by=["service"], measure=Measure(kind="avg", field="latency_ms"))),
        ("distinct users", ScopeSpec(measure=Measure(kind="count_distinct", unit="user"))),
        ("week over week", ScopeSpec(time_basis="ts", periods=periods)),
        ("error share", ScopeSpec(measure=Measure(kind="share"), numerator=[Predicate(field="level", op="eq", value="error")])),
        ("sum latency", ScopeSpec(measure=Measure(kind="sum", field="latency_ms"))),
        ("last seen 5xx", ScopeSpec(predicates=[Predicate(field="status", op="range", lower=500)], measure=Measure(kind="last_seen", field="ts"))),
    ]


def comparable(result: Any) -> dict[str, Any]:
    dump = result.model_dump(mode="json")
    dump.pop("matched_ids")
    dump.pop("matched_ids_truncated")
    return dump


def close(left: Any, right: Any) -> bool:
    """Equal, with float sums allowed a relative 1e-9 (engine compensated sum vs Python sum)."""
    if isinstance(left, float) or isinstance(right, float):
        return isinstance(left, (int, float)) and isinstance(right, (int, float)) and abs(left - right) <= 1e-9 * max(1.0, abs(left), abs(right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(close(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(close(a, b) for a, b in zip(left, right))
    return left == right


def timed(fn: Any, reps: int) -> tuple[float, Any]:
    samples, result = [], None
    for _ in range(reps):
        began = time.perf_counter()
        result = fn()
        samples.append(time.perf_counter() - began)
    return statistics.median(samples), result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--engine", choices=["opensearch", "elasticsearch"], required=True)
    parser.add_argument("--docs", type=int, default=200_000)
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.engine == "opensearch":
        from su_multi_arm.backends.opensearch import OpenSearchBackend as Backend
    else:
        from su_multi_arm.backends.elasticsearch import ElasticsearchBackend as Backend  # type: ignore[assignment]
    backend = Backend(args.url, index_prefix=f"su_bench_{secrets.token_hex(4)}", timeout_seconds=300, max_matched=args.docs + 1)
    server = backend.probe()
    report: dict[str, Any] = {"engine": args.engine, "server": server.get("version"), "docs": args.docs, "reps": args.reps, "questions": []}
    try:
        data = rows(args.docs)
        began = time.perf_counter()
        backend.catalog.put_documents(GEN, [], [])
        for start in range(0, len(data), 5000):
            backend.records.upsert(GEN, data[start : start + 5000], SCHEMA)
        report["ingest_seconds"] = round(time.perf_counter() - began, 1)
        for label, spec in questions():
            backend.aggregation = "client"
            client_s, client = timed(lambda: backend.records.execute(GEN, spec, list(ACL)), args.reps)
            backend.aggregation = "native"
            native_s, native = timed(lambda: backend.records.execute(GEN, spec, list(ACL)), args.reps)
            backend.matched_ids_cap = 100
            native_small_s, _ = timed(lambda: backend.records.execute(GEN, spec, list(ACL)), args.reps)
            backend.matched_ids_cap = 10_000
            agree = close(comparable(client), comparable(native)) and native.matched_ids == client.matched_ids[: len(native.matched_ids)]
            row = {
                "question": label,
                "matched": client.total,
                "client_s": round(client_s, 4),
                "native_s": round(native_s, 4),
                "native_ids100_s": round(native_small_s, 4),
                "speedup": round(client_s / native_s, 1) if native_s else None,
                "agree": agree,
                "identical": comparable(client) == comparable(native) and repr(comparable(client)) == repr(comparable(native)),
                "exactness": native.exactness,
                "ids_truncated": native.matched_ids_truncated,
            }
            report["questions"].append(row)
            print(json.dumps(row), flush=True)
    finally:
        names = [r["index"] for r in backend.transport.request("GET", f"/_cat/indices/{backend.prefix}_*", params={"format": "json", "h": "index"}) or []]
        if names:
            backend.transport.request("DELETE", "/" + ",".join(names))
        backend.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
