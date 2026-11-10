"""Shared fixtures of the L3a/L3b tests: a synthetic multi-day cluster, a pattern library for the
synthetic generator's three line styles, a test redactor and a fully wired ``LogsPipeline`` on one
sqlite backend. Synthetic only (no real system's layout, names or data)."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from su_multi_arm.core.component import Dependencies
from su_multi_arm.core.config import validate_config
from su_multi_arm.extraction.base import BuiltinExtractor
from su_multi_arm.extraction.patterns.engine import re2_available
from su_multi_arm.ingestion.schedule import LogsPipeline
from su_multi_arm.ingestion.write import WritePath
from su_multi_arm.providers import Embedder
from su_multi_arm.sources import build_source
from su_multi_arm.sources.transport import RateLimiter
from su_multi_arm.testing.fixtures.logs import FakeLogCluster, SyntheticLogSpec, generate_day

ENGINE = "re2" if re2_available() else "regex"
URL_ENV = "SU_TEST_LOGS_URL"
ENV = {URL_ENV: "http://source.invalid:9200"}
SERVICES = ("ingest-worker", "query-gateway", "doc-fetcher", "scheduler", "gateway")
TS = r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9:.,]{8,12}Z?"

FORMATS = [
    {
        "id": "fmt.bracket_node",
        "kind": "format",
        "status": "approved",
        "priority": 100,
        "applies_to": {"field": "message"},
        "regex": r"^\[(?P<ts>" + TS + r")\] \[(?P<level>[A-Z]{4,5})\] \[(?P<component>[a-z][a-z0-9_.-]{1,40})\] (?P<body>[^\n]{1,1000})$",
        "entities": [{"group": "component", "type": "component"}],
        "then": {"field_from_group": "body"},
        "examples": {"positive": ["[2026-01-05T10:00:00.123Z] [ERROR] [query-gateway] db-client refused by kafka"]},
        "provenance": {"author": "user"},
    },
    {
        "id": "fmt.log4j",
        "kind": "format",
        "status": "approved",
        "priority": 90,
        "applies_to": {"field": "message"},
        "regex": r"^(?P<ts>" + TS + r") (?P<level>[A-Z]{4,5}) {0,2}\[(?P<thread>[a-z0-9-]{1,20})\] (?P<component>[A-Za-z0-9_.]{1,80}) - (?P<body>[^\n]{1,1000})$",
        "entities": [{"group": "component", "type": "component"}],
        "then": {"field_from_group": "body"},
        "examples": {"positive": ["2026-01-05 10:00:00,123 ERROR [main-3] org.example.fetch.Fetcher - db-client refused by kafka"]},
        "provenance": {"author": "user"},
    },
]
MESSAGES = [
    {
        "id": "msg.dependency.connect_failed",
        "kind": "message",
        "status": "approved",
        "priority": 50,
        "applies_to": {"level": ["error", "warn"]},
        "regex": r"(?P<client>[a-z]{2,10}-client) (?P<trigger>failed to connect|refused by) (?P<dep>kafka|mongo|postgres|redis)(?: after (?P<ms>[0-9]{1,6}) ms)?",
        "signature": {"id": "dependency.connect_failed", "category": "connectivity"},
        "fields": {"ms": {"type": "integer"}},
        "entities": [{"group": "client", "type": "component"}, {"group": "dep", "type": "dependency"}],
        "relations": [{"subject": "client", "predicate": "fails_to_connect", "object": "dep", "trigger_group": "trigger"}],
        "attribute_links": [{"subject": "$service", "predicate": "emits", "object": "$signature"}],
        "examples": {"positive": ["db-client failed to connect redis after 3000 ms", "queue-client refused by kafka", "http-client failed to connect mongo"], "negative": ["db-client connected to redis"]},
        "provenance": {"author": "user"},
    },
    {
        "id": "msg.job.quota_exceeded",
        "kind": "message",
        "status": "approved",
        "priority": 40,
        "regex": r"job (?P<job>[0-9a-f-]{36}) aborted: quota exceeded",
        "signature": {"id": "job.quota_exceeded", "category": "capacity"},
        "examples": {"positive": ["job 01234567-89ab-4cde-a012-0123456789ab aborted: quota exceeded"]},
        "provenance": {"author": "user"},
    },
    {
        "id": "msg.dependency.slow",
        "kind": "message",
        "status": "approved",
        "priority": 30,
        "regex": r"slow response from (?P<dep>kafka|mongo|postgres|redis) took (?P<ms>[0-9]{1,6}) ms",
        "signature": {"id": "dependency.slow", "category": "latency"},
        "fields": {"ms": {"type": "integer"}},
        "entities": [{"group": "dep", "type": "dependency"}],
        "examples": {"positive": ["slow response from redis took 120 ms"]},
        "provenance": {"author": "user"},
    },
    {
        "id": "noise.healthz",
        "kind": "noise",
        "status": "approved",
        "priority": 10,
        "regex": r"^GET /healthz 200 [0-9]{1,3}ms$",
        "examples": {"positive": ["GET /healthz 200 2ms"]},
        "provenance": {"author": "user"},
    },
]

EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24}")
IPV4 = re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")


def redact(text: str) -> str:
    """Test redactor (the safe redaction contract is slice B11): synthetic e-mail and IPv4 shapes."""
    return IPV4.sub("<ipv4>", EMAIL.sub("<email>", text))


def write_library(root: Path, extra: list[dict] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    data = {"library": "synthetic-logs", "format_version": 1, "engine": "re2", "vocabularies": {"service": list(SERVICES)}, "patterns": [*FORMATS, *MESSAGES, *(extra or [])]}
    (root / "library.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    return root


def cluster_for(days: list[date], *, lines: int = 30, tenants: tuple[str, ...] = ("t001", "t002"), services: tuple[str, ...] = ("query-gateway", "doc-fetcher", "ingest-worker"), seed: int = 7) -> FakeLogCluster:
    indices: dict[str, list] = {}
    mappings: dict[str, dict] = {}
    for day in days:
        generated = generate_day(SyntheticLogSpec(day=day, seed=seed, tenants=tenants, services=services, lines_per_index=lines))
        indices.update(generated.indices)
        mappings.update(generated.mappings)
    return FakeLogCluster(indices=indices, mappings=mappings)


METADATA_FIELDS = [
    {"name": "date", "type": "date", "groupable": True, "sortable": True, "time_basis": "index_date"},
    {"name": "tenant", "type": "string", "groupable": True, "listable": True},
    {"name": "env", "type": "string", "groupable": True},
    {"name": "service", "type": "string", "groupable": True, "listable": True},
    {"name": "signature_id", "type": "string", "groupable": True, "listable": True},
    {"name": "category", "type": "string", "groupable": True},
    {"name": "lines", "type": "integer", "sortable": True},
    {"name": "lines_total", "type": "integer", "sortable": True},
    {"name": "lines_window", "type": "integer", "sortable": True},
    {"name": "tenants_affected", "type": "integer", "sortable": True},
    {"name": "row_exactness", "type": "enum", "enum_values": ["exact", "approximate"]},
    {"name": "first_seen", "type": "date", "sortable": True},
    {"name": "last_seen", "type": "date", "sortable": True},
]


def config_data(tmp: Path, **logs_overrides: Any) -> dict[str, Any]:
    logs = {"source": "synthetic", "partitions": {"path": str(tmp / "partitions")}, "retention": {"keep_days": 10, "keep_generations": 2, "pin_retry_seconds": 1, "max_pin_wait_seconds": 0}}
    for key, value in logs_overrides.items():
        logs[key] = {**logs[key], **value} if isinstance(value, dict) and isinstance(logs.get(key), dict) else value
    return {
        "schema_version": 2,
        "dataset": "logs",
        "backends": {"local": {"kind": "sqlite", "path": str(tmp / "state")}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 16}}},
        "metadata_schema": {"fields": METADATA_FIELDS},
        "pipeline": {"decision": {"kind": "rules"}, "arms": {"lexical": {"backend": "local"}, "dense": {"backend": "local"}, "structured": {"backend": "local"}, "graph": {"backend": "local"}}},
        "extraction": {"extractors": ["pattern"], "pattern_library": {"path": str(tmp / "patterns"), "engine": ENGINE}},
        "ingestion": {
            "line_recipe": {"fields": ["exemplars"]},
            "projections": {
                "exclude_kinds": {
                    "lexical": ["unit_day", "signature_day", "service_day", "signature_service_day", "signature_tenant_window", "signature_hour"],
                    "vector": ["unit_day", "signature_day", "service_day", "signature_service_day", "signature_window", "signature_tenant_window", "signature_hour"],
                    "graph": ["unit_day", "signature_day", "service_day", "signature_service_day", "signature_window", "signature_tenant_window", "signature_hour"],
                },
                "embed_fields": {"signature": ["template"]},
            },
        },
        "sources": {
            "synthetic": {
                "kind": "elasticsearch_rest",
                "url_env": URL_ENV,
                "indices": ["*-{date}"],
                "mapping": {"level": "level", "host": "host.name", "container": "kubernetes.container.name", "file": "log.file.path", "offset": "log.offset"},
                "index_name": {"vocabularies": {"service": list(SERVICES)}},
                "counts": {"errors": {"levels": ["ERROR"], "tokens": ["error"]}},
            }
        },
        "logs": logs,
    }


def no_wait() -> RateLimiter:
    return RateLimiter(1e9, clock=lambda: 0.0, sleep=lambda _s: None)


class Clock:
    """A settable 'now' (UTC) for run_daily's window checks."""

    def __init__(self, day: date):
        self.value = datetime.combine(day, time(6), tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def set_day(self, day: date) -> None:
        self.value = datetime.combine(day, time(6), tzinfo=timezone.utc)


def pipeline(tmp: Path, cluster: FakeLogCluster, *, redactor=redact, data: dict | None = None, now: Clock | None = None, source=None, **logs_overrides):
    """``(pipeline, config, backend)`` with a fresh write path (as a fresh process would build)."""
    from su_multi_arm.backends.sqlite import SqliteBackend

    if not (tmp / "patterns" / "library.yaml").exists():
        write_library(tmp / "patterns")
    cfg = validate_config(data or config_data(tmp, **logs_overrides))
    backend = SqliteBackend(tmp / "state")
    extractor = BuiltinExtractor(cfg.extraction, Dependencies(providers={}))
    write_path = WritePath(cfg, {"local": backend}, extractor=extractor, embedder=Embedder(cfg.models.roles.embedder))
    if source is None:
        source = build_source("synthetic", cfg.sources["synthetic"], env=ENV, http_transport=cluster.transport())
        source._rate_limiter = no_wait()
    clock = now or Clock(date(2026, 1, 20))
    return LogsPipeline(cfg, source=source, write_path=write_path, redactor=redactor, backends={"local": backend}, now=clock), cfg, backend


def days(first: date, count: int) -> list[date]:
    return [first + timedelta(days=i) for i in range(count)]
