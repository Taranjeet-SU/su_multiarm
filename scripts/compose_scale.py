#!/usr/bin/env python3
"""Compose scale benchmark (addendum A1 §3.7 N10): synthetic aggregate rows composed on SQLite.

Writes ``--days`` sealed synthetic partitions straight into a partition store (``units x signatures``
signature rows each; no source, no parsing), then runs one ``compose`` through the write path on a
local sqlite backend and reports wall time, peak RSS and document counts as JSON. Everything goes
under ``--workdir`` (use the external test workspace, never the repository).

Usage::

    python scripts/compose_scale.py --workdir ~/su_mutliarm_tests/<agent>/scale --days 10 --units 100 --signatures 1000
"""

from __future__ import annotations

import argparse
import json
import resource
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml


def library(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    data = {
        "library": "scale",
        "format_version": 1,
        "engine": "re2",
        "patterns": [
            {
                "id": "msg.scale.sig",
                "kind": "message",
                "status": "approved",
                "regex": r"^scale signature [0-9]{1,6} fired$",
                "signature": {"id": "scale.sig", "category": "scale"},
                "examples": {"positive": ["scale signature 12 fired"]},
                "provenance": {"author": "user"},
            }
        ],
    }
    (root / "library.yaml").write_text(yaml.safe_dump(data))


def config(workdir: Path, engine: str, days: int):
    from su_multi_arm.core.config import validate_config

    kinds = ["unit_day", "signature_day", "service_day", "signature_service_day", "signature_window", "signature_tenant_window", "signature_hour"]
    return validate_config(
        {
            "schema_version": 2,
            "dataset": "scale",
            "backends": {"local": {"kind": "sqlite", "path": str(workdir / "state")}},
            "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
            "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 16}}},
            "pipeline": {"arms": {"lexical": {"backend": "local"}, "dense": {"backend": "local"}, "graph": {"backend": "local"}}},
            "extraction": {"extractors": ["pattern"], "pattern_library": {"path": str(workdir / "patterns"), "engine": engine}},
            "ingestion": {
                "line_recipe": {"fields": ["exemplars"]},
                "projections": {"exclude_kinds": {"lexical": kinds, "vector": kinds, "graph": kinds}, "embed_fields": {"signature": ["template"]}},
            },
            "sources": {"scale": {"kind": "elasticsearch_rest", "url_env": "SU_SCALE_UNUSED_URL", "indices": ["scale-{date}"]}},
            "logs": {"source": "scale", "partitions": {"path": str(workdir / "partitions")}, "retention": {"keep_days": days}},
        }
    )


def seed(store, day: date, units: int, signatures: int, tenants: int, services: int, identity: str) -> int:
    from su_multi_arm.ingestion.logs_parse import ParsedPartition

    key = day.isoformat()
    store.begin(key, "scale", {"library_identity": identity})
    unit_rows, rows = [], []
    for u in range(units):
        attrs = {"tenant": f"t{u % tenants:03d}", "env": "prod", "service": f"svc-{u % services:02d}"}
        unit_rows.append({"unit_id": f"{u:032x}", "index": f"scale-{u}", "attributes": attrs, "lines_total": 1000, "by_query": {"errors": 10}, "basis": {"errors": "level_field"}, "pulled": 1000, "statuses": {"parsed": 900, "format_only": 50, "unparsed": 50, "noise": 0, "timeout": 0}, "fragments": 0, "method": "exact", "capped": False, "row_exactness": "exact"})
        for s in range(signatures):
            rows.append({"unit_id": f"{u:032x}", "attributes": attrs, "signature_id": f"scale.sig.{s:05d}", "pattern_id": "msg.scale.sig", "category": "scale", "lines": 1 + (u * s) % 7, "lines_pulled": 1, "row_exactness": "exact", "method": "exact"})
    parsed = ParsedPartition(aggregates={"units": unit_rows, "signatures": rows}, counts={"records": units * 1000})
    store.seal(key, parsed, provenance={"library_identity": identity})
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--units", type=int, default=100)
    parser.add_argument("--signatures", type=int, default=1000)
    parser.add_argument("--tenants", type=int, default=10)
    parser.add_argument("--services", type=int, default=10)
    parser.add_argument("--engine", default="regex", choices=["re2", "regex"])
    args = parser.parse_args()
    from su_multi_arm.core.component import Dependencies
    from su_multi_arm.backends.sqlite import SqliteBackend
    from su_multi_arm.extraction.base import BuiltinExtractor
    from su_multi_arm.extraction.patterns.extractor import PatternExtractor
    from su_multi_arm.ingestion.logs_compose import compose
    from su_multi_arm.ingestion.partitions import FilePartitionStore
    from su_multi_arm.ingestion.write import WritePath
    from su_multi_arm.providers import Embedder

    workdir = args.workdir.expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    library(workdir / "patterns")
    cfg = config(workdir, args.engine, args.days)
    compiled = PatternExtractor(cfg.extraction.pattern_library, None).load()
    store = FilePartitionStore(cfg.logs.partitions.path, "scale")
    first = date(2026, 1, 1)
    started = time.perf_counter()
    rows = sum(seed(store, first + timedelta(days=d), args.units, args.signatures, args.tenants, args.services, compiled.identity) for d in range(args.days))
    seeded = time.perf_counter() - started
    backend = SqliteBackend(workdir / "state")
    write_path = WritePath(cfg, {"local": backend}, extractor=BuiltinExtractor(cfg.extraction, Dependencies(providers={})), embedder=Embedder(cfg.models.roles.embedder))
    started = time.perf_counter()
    result = compose(cfg, store, write_path, compiled, first + timedelta(days=args.days - 1), reparse=None, date_basis={"date_basis": "index_date", "date_timezone": "UTC"})
    seconds = time.perf_counter() - started
    peak_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(
        json.dumps(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "aggregate_rows": rows,
                "documents": result.documents,
                "seed_seconds": round(seeded, 1),
                "compose_seconds": round(seconds, 1),
                "peak_rss_gib": round(peak_kib / 1024 / 1024, 2),
                "status": result.status,
                "params": vars(args) | {"workdir": "<workdir>"},
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
