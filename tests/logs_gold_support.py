"""The synthetic 10-day logs gold set, composed through the real pipeline (fake source -> pull ->
parse -> seal -> compose -> engine) under the ``logs_example`` profile. Synthetic only."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from su_multi_arm.core.config import validate_config
from su_multi_arm.engine import Engine
from su_multi_arm.ingestion.schedule import LogsPipeline
from su_multi_arm.sources import build_source
from su_multi_arm.testing.fixtures.logs import FakeLogCluster
from su_multi_arm.testing.fixtures.logs.gold import LIBRARY, SERVICES, GoldSpec, day_of, indices, plan, pulled_days

from tests.logs_support import ENGINE, ENV, URL_ENV, no_wait, redact
from tests.pattern_fixtures import engine_or_skip

SPEC = GoldSpec()
AS_OF = day_of(SPEC, SPEC.days)  # D10


def gold_config(tmp: Path, **overrides: Any) -> dict[str, Any]:
    # The logs profile composes incidents (communities extra: networkx) and parses with the pattern
    # engine (patterns / patterns-lite extra): a minimal install skips these tests, the ConfigError stays.
    pytest.importorskip("networkx", reason="the logs profile needs the communities extra")
    engine_or_skip(ENGINE)
    data = {
        "schema_version": 2,
        "dataset": "logs",
        "profile": "logs_example",
        "backends": {"local": {"kind": "sqlite", "path": str(tmp / "state")}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 16}}},
        "pipeline": {
            "decision": {"kind": "rules"},
            "arms": {"lexical": {"backend": "local"}, "dense": {"backend": "local"}, "structured": {"backend": "local"}, "graph": {"backend": "local"}},
        },
        "extraction": {"pattern_library": {"path": str(tmp / "patterns"), "engine": ENGINE}},
        "sources": {
            "gold": {
                "kind": "elasticsearch_rest",
                "url_env": URL_ENV,
                "indices": ["*-{date}"],
                "mapping": {"level": "level", "host": "host.name", "container": "kubernetes.container.name", "file": "log.file.path", "offset": "log.offset"},
                "index_name": {"vocabularies": {"service": list(SERVICES)}},
                "counts": {"errors": {"levels": ["ERROR"], "tokens": ["error"]}, "fatal": {"levels": ["FATAL"], "tokens": ["fatal"]}},
                "pull": {"max_docs_per_unit": SPEC.max_docs_per_unit},
            }
        },
        "logs": {
            "source": "gold",
            "partitions": {"path": str(tmp / "partitions")},
            "retention": {"keep_days": 10, "keep_generations": 2, "pin_retry_seconds": 1, "max_pin_wait_seconds": 0},
            "communities": {"co_firing": {"min_support_cells": 3, "min_lift": 2.0}},
        },
    }
    for key, value in overrides.items():
        data[key] = {**data.get(key, {}), **value} if isinstance(value, dict) and isinstance(data.get(key), dict) else value
    return data


def write_gold_library(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "library.yaml").write_text(yaml.safe_dump(LIBRARY, sort_keys=False))


class Clock:
    def __init__(self, day: date):
        self.value = datetime.combine(day, time(6), tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


def build_gold(tmp: Path, *, days: list[date] | None = None, data: dict[str, Any] | None = None, spec: GoldSpec = SPEC) -> tuple[Engine, LogsPipeline, dict[str, Any]]:
    """Pull every gold day except the missing one, compose once (as of D10) and return the engine
    (``spec``: a variant plan, e.g. with an ``error_flood`` plant)."""
    write_gold_library(tmp / "patterns")
    cfg = validate_config(data or gold_config(tmp))
    events = plan(spec)
    found, mappings = indices(spec, events)
    cluster = FakeLogCluster(indices=found, mappings=mappings)
    engine = Engine(cfg)
    source = build_source("gold", cfg.sources["gold"], env=ENV, http_transport=cluster.transport())
    source._rate_limiter = no_wait()
    pipe = LogsPipeline(cfg, source=source, write_path=engine.write_path, redactor=redact, now=Clock(AS_OF))
    library = pipe.library()
    for day in days or pulled_days(spec):
        pipe.pull(day, library)
    composed = pipe.compose(AS_OF, library)
    return engine, pipe, composed.report()


# The ten log intents asked in natural language with the arguments each must resolve to (shared by the
# surface-parity and history-tier tests; kept here so neither needs the api extra to import it).
QUESTIONS = [
    ("Which services had FATAL yesterday?", "log_top", {"by": "service", "level": ["fatal"], "window": "yesterday"}),
    ("Top errors for svc-api yesterday", "log_top", {"by": "signature", "level": ["error", "fatal"], "service": ["svc-api"], "window": "yesterday"}),
    ("What's new today?", "log_new", {"window": "today"}),
    ("Which tenants are affected by feature.flag_conflict?", "log_affected", {"signature_id": "feature.flag_conflict", "window": "last 10 days"}),
    ("How has dependency.connect_failed trended over 10 days?", "log_trend", {"signature_id": "dependency.connect_failed", "window": "last 10 days"}),
    ("Which tenants had FATAL in svc-auth yesterday?", "log_tenants", {"service": "svc-auth", "level": "fatal", "window": "yesterday"}),
    ("Errors for svc-api yesterday vs today", "log_compare", {"periods": ["yesterday", "today"], "service": "svc-api", "level": ["error", "fatal"]}),
    ("What else broke around 14:00 for t02 on 2026-03-05?", "log_around", {"at": "2026-03-05T14:00:00+00:00", "tenant": "t02"}),
    ("Slowest endpoints of svc-api yesterday", "log_endpoints", {"service": "svc-api", "metric": "latency_max", "window": "yesterday"}),
    ("What does process.crashed mean?", "log_signature", {"signature_id": "process.crashed", "window": "last 10 days"}),
    ("How much of yesterday did we parse?", "log_coverage", {"window": "yesterday"}),
]
