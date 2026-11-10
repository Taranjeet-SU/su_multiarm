"""Live log path cost benchmark (unified merge plan §6.7, increment M2a) on the synthetic F0 day.

Replays the refreshes of a generated log day (``python -m su_multi_arm.testing.fixtures.unified.log_day
--out <dir>``) through the slice ledger in simulated time: every refresh is sealed as a five-minute
slice, the compaction ladder runs when due, a dense worker embeds the new unique texts, and requests
are measured against the pinned view after each slice. Optionally earlier days (other generated
days) are loaded as one full-day slice each, so the live window holds several days.

Simulated timeline (one writer, one dense worker, as on the log host): refresh ``r`` is available
at ``r * refresh_seconds``; its seal starts when the writer is free (a compaction holds the writer
lock) and the slice is lexically servable when the seal returns; the dense worker starts when it
is free and the slice is sealed; lags are measured from the refresh's availability.

Work and output directories must be outside the repository (refused inside it). Run with
``PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1``. ``--embed bge-m3`` loads ``BAAI/bge-m3`` through
sentence-transformers from the local model cache (no network call is needed when cached); the GPU is
used only when ``--device auto`` finds enough free memory, in small batches, and released at the end.
"""

from __future__ import annotations

import argparse
import gc
import json
import statistics
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from su_multi_arm.backends.sqlite.query_stats import DenseLogScorer, LogUnits, QueryTimeScorer
from su_multi_arm.core.config import EmbedderRole
from su_multi_arm.core.config_collection import SliceSettings
from su_multi_arm.core.types_members import AnalyzerRecipe, EmbeddingSpaceRef, PartitionRecipe, PinnedView
from su_multi_arm.ingestion.handoff import transfer, verify_unit
from su_multi_arm.ingestion.log_members import close_day, member_ref_of
from su_multi_arm.ingestion.log_slices import LogSliceLedger, UnitReader, collapse_upserts
from su_multi_arm.ingestion.text_vectors import TextVectorStore, embed_missing, pick_device
from su_multi_arm.providers.embeddings import Embedder
from su_multi_arm.testing.fixtures.unified.log_day import read_refresh

QUERIES = ["crawler timeout tenant", "synthetic failure pattern 17 indexer", "unexpected response upstream variant 12", "scheduler jobs t-0042",
           "search-api availability outage", "rule-matching log messages gateway"]


def _refuse_repository(path: Path) -> None:
    for parent in [path, *path.parents]:
        if (parent / "pyproject.toml").is_file() and (parent / "src" / "su_multi_arm").is_dir():
            raise SystemExit(f"refusing to write inside the repository ({parent}); use the external test workspace")


def p95(values: list[float]) -> float:
    return float(np.percentile(values, 95)) if values else 0.0


def summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    return {"n": len(values), "p50": round(statistics.median(values), 4), "p95": round(p95(values), 4), "max": round(max(values), 4)}


def recipe_for(spec: dict[str, Any], space: EmbeddingSpaceRef) -> PartitionRecipe:
    return PartitionRecipe(embedding_space=space, analyzer=AnalyzerRecipe(engine="bm25", tokenizer="unicode_word"),
                           chunking={"size": spec["chunk_size"], "overlap": spec["chunk_overlap"], "field_order": []},
                           content={"generator": "log_day", "keyword_epoch": "bench"})


class Encoder:
    """Document encoder of the benchmark: the package's ``Embedder`` with its device/batch config."""

    def __init__(self, embedder: Embedder):
        self.embedder = embedder

    def encode(self, texts: list[str]) -> np.ndarray:
        return self.embedder.encode(texts)


def make_embedder(kind: str, device: str, batch: int) -> tuple[Embedder | None, EmbeddingSpaceRef, str]:
    if kind == "none":
        return None, EmbeddingSpaceRef(id="none", model="none", dimensions=16), "none"
    if kind == "hash":
        role = EmbedderRole(provider="hash", model="hash-demo-v1", dimensions=256, batch_size=batch)
        return Embedder(role), EmbeddingSpaceRef(id="hash-demo", model="hash-demo-v1", dimensions=256), "cpu"
    chosen = pick_device(allow_gpu=device != "cpu", min_free_bytes=6 << 30) if device in {"auto", "cpu"} else device
    role = EmbedderRole(provider="sentence_transformers", model="BAAI/bge-m3", dimensions=1024, device=chosen, batch_size=batch)
    return Embedder(role), EmbeddingSpaceRef(id="bge-m3", model="BAAI/bge-m3", dimensions=1024, template="chunk_text"), chosen


def full_day_documents(day_dir: Path) -> list[Any]:
    manifest = json.loads((day_dir / "manifest.json").read_text())
    final: dict[str, Any] = {}
    for refresh in manifest["refreshes"]:
        for d in read_refresh(day_dir / refresh["file"]):
            final[d.id] = d
    return list(final.values())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--day-dir", type=Path, required=True, help="generated log day (the replayed day)")
    parser.add_argument("--earlier-days", type=Path, nargs="*", default=[], help="generated days loaded as one full-day slice each (live window)")
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--embed", choices=["none", "hash", "bge-m3"], default="none")
    parser.add_argument("--device", default="auto", help="auto | cpu | cuda")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--limit-refreshes", type=int, default=0)
    parser.add_argument("--query-every", type=int, default=1)
    args = parser.parse_args()
    for path in (args.work, args.out):
        _refuse_repository(path.expanduser().resolve())
    args.work.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((args.day_dir / "manifest.json").read_text())
    spec = manifest["spec"]
    day = date.fromisoformat(spec["day"])
    embedder, space, device = make_embedder(args.embed, args.device, args.batch_size)
    settings = SliceSettings()
    day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
    sim = {"now": day_start}  # simulated clock: retirement and gc grace follow the replayed day
    ledger = LogSliceLedger(args.work / "loghost" / "ledger", member="logs", source="logs", recipe=recipe_for(spec, space), settings=settings,
                            embed_fields=spec["embed_fields"], clock=lambda: sim["now"])
    store = TextVectorStore(args.work / "vectors", space) if embedder is not None else None
    units = LogUnits(ledger, args.work / "registry")
    lexical = QueryTimeScorer("logs", units)
    dense = DenseLogScorer("logs", units, store, lambda q: embedder.encode([q], query=True)[0]) if embedder is not None else None
    report: dict[str, Any] = {"spec": {k: spec[k] for k in ("day", "target_chunks", "refresh_minutes", "chunk_size", "chunk_overlap")},
                              "embed": args.embed, "device": device, "batch_size": args.batch_size, "earlier_days": []}

    # earlier days of the live window: one full-day slice each (as after their day-end base compaction)
    for earlier in args.earlier_days:
        em = json.loads((earlier / "manifest.json").read_text())
        eday = date.fromisoformat(em["spec"]["day"])
        docs = full_day_documents(earlier)
        started = time.perf_counter()
        ref = ledger.seal(eday, "day", docs, as_of=datetime(eday.year, eday.month, eday.day, 23, 59, tzinfo=timezone.utc))
        seal_s = time.perf_counter() - started
        embed_s, texts = 0.0, 0
        if embedder is not None:
            reader = UnitReader(ledger.unit_dir(ref.slice_id), ledger.bm25)
            r = embed_missing(store, reader.embed()[1], Encoder(embedder), batch_size=args.batch_size)
            reader.close()
            ledger.mark_dense_ready(ref.slice_id)
            embed_s, texts = r.seconds, r.texts
        report["earlier_days"].append({"day": eday.isoformat(), "documents": len(docs), "seal_seconds": round(seal_s, 2), "embed_seconds": round(embed_s, 2),
                                       "embedded_texts": texts})
        print(json.dumps(report["earlier_days"][-1]), flush=True)

    refresh_s = spec["refresh_minutes"] * 60
    writer_free = 0.0
    dense_free = 0.0
    lexical_lag: list[float] = []
    dense_lag: list[float] = []
    seal_times: list[float] = []
    compactions: list[dict[str, Any]] = []
    first_request: list[float] = []
    warm_request: list[float] = []
    first_delta: list[float] = []
    dense_query: list[float] = []
    embed_texts_total = 0
    embed_seconds_total = 0.0
    units_per_day_max = 0
    collected = 0
    gc_times: list[float] = []
    ledger_bytes_max = 0
    pinned_units_max = 0
    refreshes = manifest["refreshes"][: args.limit_refreshes or None]
    started_all = time.perf_counter()
    for r, refresh in enumerate(refreshes):
        docs = collapse_upserts(read_refresh(args.day_dir / refresh["file"]))
        available = r * refresh_s
        sim["now"] = day_start + available
        end = datetime.fromisoformat(refresh["end"].replace("Z", "+00:00"))
        seal_start = max(available, writer_free)
        t = time.perf_counter()
        ref = ledger.seal(day, "five_minute", docs, as_of=end)
        seal = time.perf_counter() - t
        seal_times.append(seal)
        servable = seal_start + seal
        lexical_lag.append(servable - available)
        writer_free = servable
        # requests right after the new slice (cold for the new unit only), then warm
        if ref is not None and r % args.query_every == 0:
            vector = ledger.vector()
            view = PinnedView(generation="bench", slices={"logs": vector})
            pinned_units_max = max(pinned_units_max, sum(len(v) for v in vector.values()))
            units_per_day_max = max(units_per_day_max, max(len(v) for v in vector.values()))
            q = QUERIES[r % len(QUERIES)]
            t = time.perf_counter()
            lexical.rank(q, 20, view)
            cold = time.perf_counter() - t
            t = time.perf_counter()
            lexical.rank(q, 20, view)
            warm = time.perf_counter() - t
            first_request.append(cold)
            warm_request.append(warm)
            first_delta.append(cold - warm)
        # dense worker
        if embedder is not None and ref is not None:
            dense_start = max(servable, dense_free)
            reports = []
            t = time.perf_counter()
            for slice_id in ledger.pending_dense():
                reader = UnitReader(ledger.unit_dir(slice_id), ledger.bm25)
                rep = embed_missing(store, reader.embed()[1], Encoder(embedder), batch_size=args.batch_size)
                reader.close()
                ledger.mark_dense_ready(slice_id)
                reports.append(rep)
            embed_s = time.perf_counter() - t
            embed_texts_total += sum(x.texts for x in reports)
            embed_seconds_total += sum(x.seconds for x in reports)
            dense_free = dense_start + embed_s
            dense_lag.append(dense_free - available)
            if r % args.query_every == 0:
                view = PinnedView(generation="bench", slices={"logs": ledger.vector()})
                t = time.perf_counter()
                dense.rank(QUERIES[r % len(QUERIES)], 20, view)
                dense_query.append(time.perf_counter() - t)
        # background compaction (holds the writer)
        now = end
        for d, level, hour in ledger.due(now):
            t = time.perf_counter()
            cref = ledger.compact(d, level, hour=hour)
            took = time.perf_counter() - t
            begin = max(servable, writer_free)
            writer_free = begin + took
            if cref is not None:
                compactions.append({"refresh": r, "level": level, "hour": hour, "seconds": round(took, 2), "documents": cref.documents})
                print(json.dumps(compactions[-1]), flush=True)
        # slice gc (unpinned, retired longer than gc_grace_seconds ago) and the live disk footprint
        t = time.perf_counter()
        collected += len(ledger.gc())
        gc_times.append(time.perf_counter() - t)
        if r % 12 == 11:
            ledger_bytes_max = max(ledger_bytes_max, sum(p.stat().st_size for p in (args.work / "loghost" / "ledger").rglob("*") if p.is_file()))
        if r % 24 == 0:
            print(json.dumps({"refresh": r, "seal_s": round(seal, 3), "lexical_lag_s": round(lexical_lag[-1], 2),
                              "dense_lag_s": round(dense_lag[-1], 2) if dense_lag else None, "units": len(ledger.vector().get(day, ())),
                              "elapsed_s": round(time.perf_counter() - started_all, 1)}), flush=True)

    # live-window search over the final pinned view (all days), warm and cold (fresh caches)
    vector = ledger.vector()
    view = PinnedView(generation="bench", slices={"logs": vector})
    pinned = sum(len(v) for v in vector.values())
    fresh = QueryTimeScorer("logs", LogUnits(ledger, args.work / "registry"))
    cold_window, warm_window = [], []
    for q in QUERIES:
        t = time.perf_counter()
        fresh.rank(q, 20, view)
        cold_window.append(time.perf_counter() - t)
    for _ in range(3):
        for q in QUERIES:
            t = time.perf_counter()
            fresh.rank(q, 20, view)
            warm_window.append(time.perf_counter() - t)
    dense_window = []
    if dense is not None:
        for q in QUERIES:
            t = time.perf_counter()
            ranking = dense.rank(q, 20, view)
            dense_window.append(time.perf_counter() - t)
            assert not ranking.approximations, ranking.approximations

    # close the replayed day: archive member + handoff verification (what a compose by reference checks)
    t = time.perf_counter()
    closed = close_day(ledger, day, args.work / "loghost" / "registry")
    close_s = time.perf_counter() - t
    t = time.perf_counter()
    copied = transfer(closed.day_dir, args.work / "serve" / "incoming")
    transfer_s = time.perf_counter() - t
    t = time.perf_counter()
    verify_unit(copied)
    member = member_ref_of(copied)
    verify_s = time.perf_counter() - t
    member_bytes = sum(p.stat().st_size for p in copied.rglob("*") if p.is_file())

    embed_rows = 0
    for slice_id in ledger.vector().get(day, ()):
        reader = UnitReader(ledger.unit_dir(slice_id), ledger.bm25)
        embed_rows += len(reader.embed()[0])
        reader.close()
    report.update({
        "refreshes": len(refreshes),
        "wall_seconds": round(time.perf_counter() - started_all, 1),
        "seal_seconds": summary(seal_times),
        "lexical_refresh_to_servable_seconds": summary(lexical_lag),
        "dense_refresh_to_servable_seconds": summary(dense_lag),
        "compactions": {"count": len(compactions), "by_level": {lvl: summary([c["seconds"] for c in compactions if c["level"] == lvl]) for lvl in ("hourly", "base")}},
        "first_request_after_slice_seconds": summary(first_request),
        "warm_request_seconds": summary(warm_request),
        "first_minus_warm_seconds": summary(first_delta),
        "dense_query_seconds": summary(dense_query),
        "units_per_day_max": units_per_day_max,
        "pinned_units_max": pinned_units_max,
        "live_window": {"pinned_units": pinned, "days": len(vector), "lexical_cold_seconds": summary(cold_window), "lexical_warm_seconds": summary(warm_window),
                        "dense_warm_seconds": summary(dense_window)},
        "close_day": {"seconds": round(close_s, 2), "documents": member.counts, "member_bytes": member_bytes, "transfer_seconds": round(transfer_s, 2),
                      "verify_seconds": round(verify_s, 2), "catalog_member": closed.catalog is not None},
        "activations_per_day_from_logs": 1,
        "embedding": {"texts": embed_texts_total, "seconds": round(embed_seconds_total, 2),
                      "texts_per_second": round(embed_texts_total / embed_seconds_total, 1) if embed_seconds_total else None,
                      "embed_rows_live_day": embed_rows},
        "vector_store": {"vectors": store.count() if store else 0, "bytes": store.disk_bytes() if store else 0,
                         "bytes_without_dedupe_float32": embed_rows * space.dimensions * 4},
        "ledger_bytes_end": sum(p.stat().st_size for p in (args.work / "loghost" / "ledger").rglob("*") if p.is_file()),
        "ledger_bytes_max_sampled_hourly": ledger_bytes_max,
        "slices_collected": collected,
        "gc_seconds": summary(gc_times),
    })
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1, sort_keys=True))
    print(json.dumps(report, indent=1, sort_keys=True))
    dense = embedder = None  # drop the model before releasing the GPU cache
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001 - no torch: nothing to release
        pass


if __name__ == "__main__":
    main()
