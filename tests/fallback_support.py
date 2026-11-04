"""Synthetic fixtures of the LLM fallback tests (addendum A2 rev 2 §4.2): a hand-built sealed day
partition with controlled residue, and a scripted fake provider on the OpenAI-compatible wire
(``httpx.MockTransport``: no network, no key, no spend). Every line, name and value is generated;
the e-mail shape uses a reserved documentation domain."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import yaml

from su_multi_arm.core.config import validate_config
from su_multi_arm.extraction.patterns.extractor import PatternExtractor
from su_multi_arm.ingestion.accounting import AccountingPolicy
from su_multi_arm.ingestion.logs_parse import parse_units
from su_multi_arm.ingestion.partitions import FilePartitionStore, UnitArtifacts
from su_multi_arm.learning.extraction import FallbackSession
from su_multi_arm.sources.contracts import SamplePlan, SourceUnit, Stratum, UnitCount
from su_multi_arm.sources.logs import LogRecord
from tests.logs_support import ENGINE, FORMATS, config_data

DAY = date(2026, 1, 5)
NOW = datetime(2026, 1, 6, 6, 0, tzinfo=timezone.utc)
HTTP = {
    "id": "msg.http.request",
    "kind": "message",
    "status": "approved",
    "priority": 60,
    "regex": r"^(?P<method>GET|POST) (?P<endpoint>/[a-z/]{1,40}) (?P<status_code>[0-9]{3})(?: in (?P<latency_ms>[0-9]{1,6}) ms)?(?: [a-z0-9]{1,10}){0,2}$",
    "signature": {"id": "http.request", "category": "http"},
    "fields": {"status_code": {"type": "integer"}, "latency_ms": {"type": "integer"}, "endpoint": {"type": "string"}},
    "examples": {"positive": ["GET /api/items 200 in 12 ms", "POST /api/orders 201 in 40 ms"]},
    "provenance": {"author": "user"},
}
FIX_RE = r"^(?P<method>GET|POST) (?P<endpoint>/[a-z/]{1,40}) (?P<status_code>[0-9]{3}) (?:in|took) (?P<latency_ms>[0-9]{1,6}) ?ms$"
WARMUP_RE = r"^cache warmup finished for shard (?P<shard>[0-9]{1,4})$"
FIELDS = {"dependency": "string", "latency_ms": "integer", "status_code": "integer", "endpoint": "string", "shard": "integer", "percent": "integer"}
DEPS = ["redis", "kafka", "mongo", "postgres", "redis", "kafka"]


def bodies(day_offset: int = 0) -> list[tuple[str, str]]:
    """``(level, body)`` of one synthetic day: 6 timeout lines (one template), 4 warm-up lines
    (another), 3 field-gap http lines, 2 parsed http lines, 2 singletons and 1 e-mail line."""
    out = [("ERROR", f"upstream timeout calling {DEPS[i]} after {100 + 7 * i + day_offset} ms") for i in range(6)]
    out += [("WARN", f"cache warmup finished for shard {10 + i + day_offset}") for i in range(4)]
    out += [("ERROR", f"GET /api/items 50{i} took {30 + i}ms") for i in range(3)]
    out += [("ERROR", "GET /api/items 200 in 12 ms"), ("ERROR", "POST /api/orders 201 in 40 ms")]
    out += [("WARN", "disk quota of volume data reached 91 percent"), ("WARN", "scheduler drift is 3 seconds behind")]
    out += [("WARN", "login refused for ops.person@example.org")]
    return out


def write_library(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    data = {"library": "synthetic-fallback", "format_version": 1, "engine": "re2", "vocabularies": {}, "patterns": [FORMATS[0], HTTP]}
    (root / "library.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    return root


def config(tmp: Path, **kwargs: Any):
    return validate_config(fallback_data(tmp, **kwargs))


def fallback_data(tmp: Path, *, extraction: dict | None = None, routes: dict | None = None, roles: dict | None = None, patterns: dict | None = None, budgets: dict | None = None, promotion: str = "manual") -> dict[str, Any]:
    data = config_data(tmp)
    data["extraction"]["pattern_library"] = {"path": str(tmp / "patterns"), "engine": ENGINE, "promotion": promotion, "gate": {"require_re2": ENGINE == "re2"}}
    data["models"]["roles"].update(
        roles
        or {
            "pattern_learner": {"provider": "openai_compatible", "model": "local-a", "base_url": "http://127.0.0.1:9/v1", "max_tokens": 512},
            "log_extractor": {"provider": "openai_compatible", "model": "local-a", "base_url": "http://127.0.0.1:9/v1", "max_tokens": 512},
        }
    )
    if routes is not None:
        data["models"]["routes"] = routes
    data["privacy"] = {"redact_emails": True}
    data["learning"] = {
        "patterns": {"spend_ledger": str(tmp / "spend.sqlite"), "max_calls_per_day": 50, "max_calls_per_run": 50, **(patterns or {})},
        "extraction": {
            "enabled": True,
            "fields": dict(FIELDS),
            "lines": {"enabled": True, "batch_size": 4},
            "egress": {"max_refused_share": 0.2},
            "budget": {"max_usd_per_day": None},
            **(extraction or {}),
        },
    }
    data["logs"]["accounting"] = {"required_fields": {"http": ["status_code", "endpoint", "latency_ms"]}, "validators": {"status_code": {"min": 100, "max": 599}}}
    if budgets:
        data["budgets"] = budgets
    return data


def load(cfg: Any):
    return PatternExtractor(cfg.extraction.pattern_library, None).load()


def record(i: int, day: date, level: str, body: str, service: str = "query-gateway") -> LogRecord:
    ts = datetime(day.year, day.month, day.day, 10, 0, tzinfo=timezone.utc) + timedelta(seconds=i)
    message = f"[{ts.strftime('%Y-%m-%dT%H:%M:%S')}.000Z] [{level}] [{service}] {body}"
    return LogRecord(
        id=f"{day.isoformat()}-h{i:03d}",
        ts=ts,
        partition_key=day.isoformat(),
        attributes={"service": service, "tenant": "t001", "env": "prod"},
        level=level,
        level_basis="level_field",
        message=message,
        source_id=f"src-{day.isoformat()}-{i}",
        index=f"t001-prod-{service}-{day.isoformat()}",
    )


def artifacts(day: date, rows: list[tuple[str, str]], *, docs_pulled: int | None = None, fetched: int | None = None) -> UnitArtifacts:
    records = [record(i, day, level, body) for i, (level, body) in enumerate(rows)]
    n = len(records)
    unit_id = hashlib.sha256(day.isoformat().encode()).hexdigest()[:16]
    unit = SourceUnit(id=unit_id, index=f"t001-prod-query-gateway-{day.isoformat()}", attributes={"service": "query-gateway", "tenant": "t001", "env": "prod"}, query_sha="q")
    return UnitArtifacts(
        unit=unit,
        count=UnitCount(unit_id=unit_id, query_sha="q", total=n, at=NOW),
        sample=SamplePlan(method="none", cap=1000, unit_total=n, strata=[Stratum(key="all", count=n, quota=n, fetched=n if fetched is None else fetched)]),
        records=records,
        multiline={"heads": n, "joined": 0, "fragments": 0, "overflow": 0},
        docs_pulled=n if docs_pulled is None else docs_pulled,
        malformed=0,
    )


def seal_day(cfg: Any, day: date = DAY, rows: list[tuple[str, str]] | None = None, **kwargs: Any) -> FilePartitionStore:
    store = FilePartitionStore(cfg.logs.partitions.path, cfg.logs.source)
    library = load(cfg)
    key = day.isoformat()
    store.begin(key, "q", library.provenance())
    unit = artifacts(day, rows if rows is not None else bodies(), **kwargs)
    store.put_unit(key, unit.unit.id, unit)
    parsed = parse_units(list(store.units(key)), library, policy=AccountingPolicy.from_config(cfg))
    store.seal(key, parsed, provenance=library.provenance())
    return store


# --- the scripted fake provider ----------------------------------------------------------------


def cluster_reply(payload: dict[str, Any]) -> dict[str, Any]:
    """The default script: labels for the timeout template, a regex for the warm-up template, a
    fix for the http gaps, ``none`` otherwise."""
    base = {
        "cluster_id": payload["cluster_id"],
        "action": "new",
        "target_pattern_id": None,
        "issue_name": "synthetic issue",
        "issue_category": "dependency",
        "regex": None,
        "fields": [],
        "entities": [],
        "relations": [],
        "positive_line_ids": [],
        "negative_line_ids": [],
        "rationale": "one template",
        "labels": [],
        "required_fields_present": [],
    }
    ids = [line["id"] for line in payload["lines"]]
    if payload["task"] == "fix":
        return {**base, "action": "fix", "target_pattern_id": payload["target"]["id"], "regex": FIX_RE, "fields": [{"group": "latency_ms", "type": "integer"}], "positive_line_ids": ids[:3]}
    template = payload["template"]
    if template.startswith("upstream timeout"):
        return {**base, "labels": [{"group": "v1", "field": "dependency", "type": "string", "entity_type": "dependency"}, {"group": "v2", "field": "latency_ms", "type": "integer", "entity_type": None}]}
    if template.startswith("cache warmup"):
        return {**base, "issue_category": "lifecycle", "regex": WARMUP_RE, "fields": [{"group": "shard", "type": "integer"}], "positive_line_ids": ids[:3]}
    return {**base, "action": "none"}


def line_reply(payload: dict[str, Any]) -> dict[str, Any]:
    out = []
    for line in payload["lines"]:
        fields = []
        if "quota" in line["text"]:
            fields.append({"name": "percent", "quote": "91"})
        out.append({"line_id": line["id"], "fields": fields, "signature_hint": None})
    return {"lines": out}


class FakeProvider:
    """Answers ``POST …/chat/completions`` from scripts and ``GET …/models`` (health) with 200 unless
    the host is listed in ``down``. Every chat request is kept."""

    def __init__(self, cluster: Callable[[dict], Any] = cluster_reply, lines: Callable[[dict], Any] = line_reply, down: set[str] | None = None):
        self.cluster, self.lines = cluster, lines
        self.down = set(down or ())
        self.requests: list[dict[str, Any]] = []
        self.probes: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.method == "GET":
            self.probes.append(str(request.url))
            return httpx.Response(503 if host in self.down else 200, json={"data": []})
        if host in self.down:
            return httpx.Response(503, json={"error": "down"})
        body = json.loads(request.content)
        payload = json.loads(body["messages"][-1]["content"])
        self.requests.append({"host": host, "model": body["model"], "payload": payload})
        answer = self.lines(payload) if payload.get("task") == "lines" else self.cluster(payload)
        content = answer if isinstance(answer, str) else json.dumps(answer)
        return httpx.Response(
            200,
            json={"model": body["model"], "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}], "usage": {"prompt_tokens": 300, "completion_tokens": 80}},
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def tasks(self) -> list[str]:
        return [r["payload"].get("task") for r in self.requests]

    def sent_text(self) -> str:
        return "\n".join(json.dumps(r) for r in self.requests)


def session(cfg: Any, fake: FakeProvider) -> FallbackSession:
    from su_multi_arm.learning.extraction import HttpProbe

    return FallbackSession.from_config(cfg, transport=fake.transport(), probe=HttpProbe(fake.transport()), clock=lambda: NOW)


def heads_of(store: FilePartitionStore, report: dict[str, Any], key: str = DAY.isoformat()) -> dict[str, Any]:
    from su_multi_arm.ingestion.overlay import OverlayStore

    part = report["partitions"][key]
    overlays = OverlayStore(store)
    manifest = overlays.read(key, part["version"], part["identity"])
    out = {}
    for unit in manifest.units:
        for head in overlays.heads(key, part["version"], part["identity"], unit):
            out[head.head_id] = head
    return out


def body_of(head_id: str, rows: list[tuple[str, str]] | None = None) -> str:
    index = int(head_id.rsplit("h", 1)[1])
    return (rows or bodies())[index][1]
