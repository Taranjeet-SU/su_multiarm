"""Live log member benchmark through the engine (unified merge plan §6.7; M2a wired into M1a, lane LB).

A partitioned collection = the F0 support documents as a snapshot member + the live log member
``logs`` (``cadence: minutes``). Earlier generated log days are closed as archive day members
(``LiveLogs.close_day``: close, register, **one compose** — measured — and release) or kept live as
one full-day slice; the replayed day (``python -m su_multi_arm.testing.fixtures.unified.log_day``)
is sealed refresh by refresh through the member's ledger with the compaction ladder and a dense
worker, and ``Engine.search`` (rules decision, lexical + dense arms, fusion, hit reads) is measured
against the request pin after new slices and after every base compaction (the first dense request
after a compaction: §6.7 first-request target, the M2a 7.1 s outlier). At the end the replayed day is
closed (compose measured again).

Before the close, the live window is measured from fresh engines: one without warm-up (cold caches,
same process: the OS page cache is warm), one after ``Engine.warmup`` (its seconds reported), and
in **fresh processes** (``--probe``: this script re-run with the work directory and the simulated
clock; interpreter start and imports are outside the measured first search), with and without
warm-up.

Simulated clock for retirement/gc; wall clock for every measured duration. Work and output
directories must be outside the repository (refused inside it). Run with ``PYTHONPATH=src
PYTHONDONTWRITEBYTECODE=1``. The embedder is the package's ``hash`` provider (``--dims``, default
1,024 = BGE-M3's width): the retrieval cost depends on the vector width and row count, not the
model; no model or network is used.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from su_multi_arm import Engine, SearchRequest
from su_multi_arm.core.config import validate_config
from su_multi_arm.ingestion.log_slices import UnitReader, collapse_upserts, embed_pending_slices
from su_multi_arm.ingestion.text_vectors import embed_missing
from su_multi_arm.testing.fixtures.unified import load
from su_multi_arm.testing.fixtures.unified.log_day import read_refresh

QUERIES = ["crawler timeout tenant", "synthetic failure pattern 17 indexer", "unexpected response upstream variant 12", "scheduler jobs t-0042",
           "search-api availability outage", "rule-matching log messages gateway", "crawler fetch timeout", "index write failed after deploy"]
LOG_KINDS = ["episode_cell", "incident", "shared_error", "signature", "signature_day", "summary_coverage", "unclassified_example", "unit_day"]


def _refuse_repository(path: Path) -> None:
    for parent in [path, *path.parents]:
        if (parent / "pyproject.toml").is_file() and (parent / "src" / "su_multi_arm").is_dir():
            raise SystemExit(f"refusing to write inside the repository ({parent}); use the external test workspace")


def summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    return {"n": len(values), "p50": round(statistics.median(values), 4), "p95": round(float(np.percentile(values, 95)), 4), "max": round(max(values), 4)}


def config(work: Path, dims: int, embed_fields: dict[str, list[str]]) -> Any:
    return validate_config({
        "schema_version": 3,
        "dataset": "unified",
        "backends": {"local": {"kind": "sqlite", "path": str(work / "state"), "lexical_engine": "bm25"}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": dims}}},
        "pipeline": {"decision": {"kind": "rules"}, "arms": {"lexical": {"backend": "local"}, "dense": {"backend": "local"}}},
        "collection": {"mode": "partitioned", "members": {
            "support": {"kind": "support", "sources": ["ticket", "doc"], "record_kinds": ["ticket", "doc"], "key": "snapshot", "id_namespaces": ["ticket:", "https://docs.example.org/kb/"]},
            "logs": {"kind": "logs", "sources": ["logs"], "record_kinds": LOG_KINDS, "key": "day", "cadence": "minutes", "producer": {"kind": "log_host"},
                     "id_namespaces": ["logs:"], "embed_fields": embed_fields},
        }},
    })


def full_day_documents(day_dir: Path) -> list[Any]:
    manifest = json.loads((day_dir / "manifest.json").read_text())
    final: dict[str, Any] = {}
    for refresh in manifest["refreshes"]:
        for d in read_refresh(day_dir / refresh["file"]):
            final[d.id] = d
    return list(final.values())


class Docs:
    def __init__(self, engine: Engine):
        self.engine = engine

    def encode(self, texts: list[str]) -> np.ndarray:
        return self.engine.embedder.encode(texts)


def embed_members(engine: Engine) -> int:
    """Embed the archive members' texts the store does not hold (the dense worker at close)."""
    registry = engine.view.attach()
    live = registry.live
    from su_multi_arm.ingestion.compose import members_of

    total = 0
    for ref in members_of(registry.generations.current() or {}):
        if ref.member == "logs":
            reader = UnitReader(registry.unit_dir(ref), live.ledger("logs").bm25)
            total += embed_missing(live.vector_store("logs"), reader.embed()[1], Docs(engine), batch_size=256).texts
            reader.close()
    return total


def timed_search(engine: Engine, query: str) -> tuple[float, Any]:
    started = time.perf_counter()
    result = engine.search(SearchRequest(query=query))
    return time.perf_counter() - started, result


def probe(work: Path, dims: int, embed_fields: dict[str, list[str]], now: float, warm: bool) -> dict[str, Any]:
    """Fresh-process measurement: engine construction, optional ``Engine.warmup``, first search and
    the following searches over the live window."""
    t = time.perf_counter()
    engine = Engine(config(work, dims, embed_fields))
    engine.view.attach().live.clock = lambda: now
    out: dict[str, Any] = {"construct_seconds": round(time.perf_counter() - t, 4), "warmup": warm}
    if warm:
        t = time.perf_counter()
        report = engine.warmup()
        out["warmup_seconds"] = round(time.perf_counter() - t, 4)
        out["warmup_units"] = report["backends"]["local"]["units"]
    out["first_search_seconds"] = round(timed_search(engine, QUERIES[0])[0], 4)
    out["next_search_seconds"] = summary([timed_search(engine, q)[0] for q in QUERIES[1:]])
    return out


def fresh_process(work: Path, dims: int, day_dir: Path, now: float, warm: bool) -> dict[str, Any]:
    command = [sys.executable, str(Path(__file__).resolve()), "--probe", "--work", str(work), "--day-dir", str(day_dir), "--out", "-", "--dims", str(dims), "--probe-now", repr(now)]
    if warm:
        command.append("--probe-warm")
    done = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(done.stdout.strip().splitlines()[-1])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--day-dir", type=Path, required=True)
    parser.add_argument("--closed-days", type=Path, nargs="*", default=[], help="generated days closed as archive day members before the replay")
    parser.add_argument("--live-days", type=Path, nargs="*", default=[], help="generated days kept live as one full-day slice each")
    parser.add_argument("--fixture", type=Path, default=Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "unified")
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dims", type=int, default=1024)
    parser.add_argument("--query-every", type=int, default=6)
    parser.add_argument("--limit-refreshes", type=int, default=0)
    parser.add_argument("--no-warm", action="store_true", help="disable the compaction warm hook (the M2a behaviour) for comparison")
    parser.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)  # fresh-process measurement of an existing --work
    parser.add_argument("--probe-now", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--probe-warm", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.probe:
        spec = json.loads((args.day_dir / "manifest.json").read_text())["spec"]
        print(json.dumps(probe(args.work, args.dims, spec["embed_fields"], args.probe_now, args.probe_warm)))
        return
    for path in (args.work, args.out):
        _refuse_repository(path.expanduser().resolve())
    args.work.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((args.day_dir / "manifest.json").read_text())
    spec = manifest["spec"]
    day = date.fromisoformat(spec["day"])
    day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
    sim = {"now": day_start - 4 * 86400}
    engine = Engine(config(args.work, args.dims, spec["embed_fields"]))
    registry = engine.view.attach()
    live = registry.live
    live.clock = lambda: sim["now"]
    if args.no_warm:
        live._registered = lambda *a: None  # noqa: SLF001 - benchmark comparison only
    fx = load(args.fixture)
    support = next(v for k, v in fx.members.items() if k.startswith("support/"))
    engine.build_member("support", "snapshot:2026-11-09", support)
    engine.compose()
    ledger = live.ledger("logs")
    report: dict[str, Any] = {"spec": {k: spec[k] for k in ("day", "target_chunks", "refresh_minutes", "chunk_size", "chunk_overlap")}, "dims": args.dims,
                              "warm_hook": not args.no_warm, "closes": [], "live_days": []}

    for earlier in args.closed_days:
        eday = date.fromisoformat(json.loads((earlier / "manifest.json").read_text())["spec"]["day"])
        sim["now"] = datetime(eday.year, eday.month, eday.day, 23, 59, tzinfo=timezone.utc).timestamp()
        docs = full_day_documents(earlier)
        t = time.perf_counter()
        ledger.seal(eday, "day", docs, as_of=datetime(eday.year, eday.month, eday.day, 23, 59, tzinfo=timezone.utc))
        seal_s = time.perf_counter() - t
        t = time.perf_counter()
        closed = live.close_day("logs", eday, as_of=datetime(eday.year, eday.month, eday.day, 23, 59, tzinfo=timezone.utc))
        close_s = time.perf_counter() - t
        t = time.perf_counter()
        texts = embed_members(engine)
        report["closes"].append({"day": eday.isoformat(), "documents": len(docs), "seal_seconds": round(seal_s, 2), "close_seconds": round(close_s, 2),
                                 "compose_seconds": round(closed["compose_seconds"], 3), "embed_seconds": round(time.perf_counter() - t, 2), "texts": texts})
        print(json.dumps(report["closes"][-1]), flush=True)
    for earlier in args.live_days:
        eday = date.fromisoformat(json.loads((earlier / "manifest.json").read_text())["spec"]["day"])
        docs = full_day_documents(earlier)
        t = time.perf_counter()
        ledger.seal(eday, "day", docs, as_of=datetime(eday.year, eday.month, eday.day, 23, 59, tzinfo=timezone.utc))
        seal_s = time.perf_counter() - t
        embed_pending_slices(ledger, live.vector_store("logs"), Docs(engine), batch_size=256)
        report["live_days"].append({"day": eday.isoformat(), "documents": len(docs), "seal_seconds": round(seal_s, 2)})
        print(json.dumps(report["live_days"][-1]), flush=True)

    refreshes = manifest["refreshes"][: args.limit_refreshes or None]
    refresh_s = spec["refresh_minutes"] * 60
    seal_times, compactions, first_after_slice, warm, first_after_base, first_dense_after_base = [], [], [], [], [], []
    pinned_max = 0
    started_all = time.perf_counter()
    for r, refresh in enumerate(refreshes):
        sim["now"] = day_start + r * refresh_s
        docs = collapse_upserts(read_refresh(args.day_dir / refresh["file"]))
        end = datetime.fromisoformat(refresh["end"].replace("Z", "+00:00"))
        t = time.perf_counter()
        ref = ledger.seal(day, "five_minute", docs, as_of=end)
        seal_times.append(time.perf_counter() - t)
        if ref is not None:
            embed_pending_slices(ledger, live.vector_store("logs"), Docs(engine), batch_size=256)
        bases = []
        for d, level, hour in ledger.due(end):
            t = time.perf_counter()
            cref = ledger.compact(d, level, hour=hour)
            took = time.perf_counter() - t
            if cref is not None:
                compactions.append({"refresh": r, "level": level, "seconds": round(took, 2), "documents": cref.documents})
                if level == "base":
                    bases.append(cref)
                print(json.dumps(compactions[-1]), flush=True)
        if bases:
            # the first request after a base compaction (cold dense matrix in M2a: 7.1 s max)
            seconds, result = timed_search(engine, QUERIES[r % len(QUERIES)])
            first_after_base.append(seconds)
            gen = engine.write_path.pin()[0]
            vector = engine.embedder.encode([QUERIES[(r + 1) % len(QUERIES)]], query=True)[0]
            t = time.perf_counter()
            engine.backends["local"].vector.search(gen, vector, None, 20)
            first_dense_after_base.append(time.perf_counter() - t)
        elif ref is not None and r % args.query_every == 0:
            seconds, result = timed_search(engine, QUERIES[r % len(QUERIES)])
            first_after_slice.append(seconds)
            seconds, result = timed_search(engine, QUERIES[r % len(QUERIES)])
            warm.append(seconds)
            slices = result.trace["collection"].get("slices", [])
            pinned_max = max(pinned_max, len(slices) + len(result.trace["collection"]["members"]))
        ledger.gc()
        if r % 24 == 0:
            print(json.dumps({"refresh": r, "seal_s": round(seal_times[-1], 3), "units": len(ledger.vector().get(day, ())), "elapsed_s": round(time.perf_counter() - started_all, 1)}), flush=True)

    # live-window search over the final pin: a fresh engine (cold caches) and then warm
    fresh = Engine(config(args.work, args.dims, spec["embed_fields"]))
    fresh.view.attach().live.clock = lambda: sim["now"]
    cold_window, warm_window = [], []
    for q in QUERIES:
        cold_window.append(timed_search(fresh, q)[0])
    for _ in range(3):
        for q in QUERIES:
            warm_window.append(timed_search(fresh, q)[0])
    last = timed_search(fresh, QUERIES[0])[1]
    coverage = last.trace["collection"]
    sources = sorted({h.source for h in last.hits})
    warmed = Engine(config(args.work, args.dims, spec["embed_fields"]))
    warmed.view.attach().live.clock = lambda: sim["now"]
    t = time.perf_counter()
    warmed.warmup()
    warmup_s = time.perf_counter() - t
    warmed_window = [timed_search(warmed, q)[0] for q in QUERIES]
    processes = {f"{'warm' if warm else 'cold'}_{i}": fresh_process(args.work, args.dims, args.day_dir, sim["now"], warm) for i in range(2) for warm in (False, True)}

    # close the replayed day: archive member, register, one compose (measured), release
    sim["now"] = day_start + 86400
    t = time.perf_counter()
    closed = live.close_day("logs", day, as_of=datetime(day.year, day.month, day.day, 23, 59, tzinfo=timezone.utc))
    close_s = time.perf_counter() - t
    report.update({
        "refreshes": len(refreshes),
        "wall_seconds": round(time.perf_counter() - started_all, 1),
        "seal_seconds": summary(seal_times),
        "compactions": {lvl: summary([c["seconds"] for c in compactions if c["level"] == lvl]) for lvl in ("hourly", "base")},
        "engine_search_first_after_slice_seconds": summary(first_after_slice),
        "engine_search_warm_seconds": summary(warm),
        "engine_search_first_after_base_seconds": summary(first_after_base),
        "dense_facet_first_after_base_seconds": summary(first_dense_after_base),
        "pinned_units_max": pinned_max,
        "live_window": {"units": len(coverage.get("slices", [])) + len(coverage["members"]), "slices": len(coverage.get("slices", [])),
                        "engine_search_cold_seconds": summary(cold_window), "engine_search_warm_seconds": summary(warm_window), "hit_sources": sources,
                        "dense": coverage.get("dense", "ready"),
                        "engine_warmup_seconds": round(warmup_s, 4), "engine_search_after_warmup_seconds": summary(warmed_window),
                        "fresh_process": processes},
        "close_replayed_day": {"seconds": round(close_s, 2), "compose_seconds": round(closed["compose_seconds"], 3), "retired_slices": len(closed["retired_slices"])},
        "generations": len(registry.generations.generations()),
    })
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1, sort_keys=True))
    print(json.dumps(report, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
