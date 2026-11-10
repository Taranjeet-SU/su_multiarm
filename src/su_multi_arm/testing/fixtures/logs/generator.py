"""Deterministic synthetic log generator (addendum A1 §4 L0). Synthetic only: the tenants, services,
hosts, classes, paths and messages are invented, with layouts unrelated to any observed system (N18).

One day gives one index per ``(tenant, env, service)``, named ``<tenant>-<env>-<service>-<YYYY.MM.DD>``.
Each service writes one line style:

* ``node`` — bracketed Node-style: ``[2026-01-05T10:00:00.123Z] [ERROR] [query-gateway] …``, with a
  structured ``level`` field in the document (so counts use the ``level_field`` basis);
* ``python_ansi`` — bracketed Python-style with ANSI colours: ``\\x1b[31m[2026-01-05 10:00:00,123]
  [ERROR] [worker.tasks]\\x1b[0m …``; level only in the text (``token_match`` basis);
* ``log4j`` — ``2026-01-05 10:00:00,123 ERROR [main-3] org.example.fetch.Fetcher - …``; level only in
  the text.

Every index also gets multiline errors split into separate continuation documents (Java/Node stack
frames, ``Caused by:``, ``... N more``, Python ``Traceback``/``File`` lines), health-check noise, and
one head-less continuation fragment at the start of the day. With ``pii_shapes`` some messages carry
synthetic e-mail addresses (``example.invalid``) and documentation-range IPv4 addresses
(RFC 5737), for the redaction tests of slice B11.

The output is a pure function of the spec: the same spec gives byte-identical documents.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

SERVICES = ("ingest-worker", "query-gateway", "doc-fetcher", "scheduler", "gateway")
STYLE = {"ingest-worker": "python_ansi", "query-gateway": "node", "doc-fetcher": "log4j", "scheduler": "node", "gateway": "log4j"}
DEPENDENCIES = ("kafka", "mongo", "postgres", "redis")
CLIENTS = ("db-client", "queue-client", "http-client", "cache-client")
ANSI_COLOUR = {"DEBUG": "\x1b[36m", "INFO": "\x1b[32m", "WARN": "\x1b[33m", "ERROR": "\x1b[31m", "FATAL": "\x1b[35m"}
LEVEL_WEIGHTS = (("INFO", 60), ("DEBUG", 6), ("WARN", 12), ("ERROR", 12), ("noise", 10))
MULTILINE_SHARE = 0.4  # of ERROR lines


@dataclass(frozen=True)
class SyntheticLogSpec:
    day: date
    seed: int = 7
    tenants: tuple[str, ...] = ("t001", "t002")
    envs: tuple[str, ...] = ("prod",)
    services: tuple[str, ...] = SERVICES
    lines_per_index: int = 120
    pii_shapes: bool = True


@dataclass
class IndexTruth:
    docs: int = 0
    by_level: dict[str, int] = field(default_factory=dict)
    noise: int = 0
    multiline_heads: int = 0
    continuation_docs: int = 0
    fragments: int = 0
    pii_lines: int = 0


@dataclass
class SyntheticDay:
    indices: dict[str, list[dict[str, Any]]]
    mappings: dict[str, dict[str, Any]]
    truth: dict[str, IndexTruth]

    def digest(self) -> str:
        """sha256 over every index name and document, for determinism checks."""
        h = hashlib.sha256()
        for name in sorted(self.indices):
            h.update(name.encode())
            for doc in self.indices[name]:
                h.update(json.dumps(doc, sort_keys=True).encode())
        return h.hexdigest()


def index_name(tenant: str, env: str, service: str, day: date) -> str:
    return f"{tenant}-{env}-{service}-{day:%Y.%m.%d}"


def mapping_for(service: str) -> dict[str, Any]:
    props: dict[str, Any] = {
        "@timestamp": {"type": "date"},
        "message": {"type": "text"},
        "host": {"properties": {"name": {"type": "keyword"}}},
        "kubernetes": {"properties": {"container": {"properties": {"name": {"type": "keyword"}}}}},
        "log": {"properties": {"file": {"properties": {"path": {"type": "keyword"}}}, "offset": {"type": "long"}}},
        "trace_id": {"type": "keyword"},
    }
    if STYLE[service] == "node":
        props["level"] = {"type": "text", "fields": {"keyword": {"type": "keyword", "ignore_above": 32}}}
    return {"mappings": {"properties": props}}


def _message(rng: random.Random, level: str, pii: bool) -> tuple[str, bool]:
    ms = rng.randint(1, 9000)
    dep, client = rng.choice(DEPENDENCIES), rng.choice(CLIENTS)
    uid = "%08x-%04x-4%03x-a%03x-%012x" % (rng.getrandbits(32), rng.getrandbits(16), rng.getrandbits(12), rng.getrandbits(12), rng.getrandbits(48))
    if level == "ERROR":
        options = [f"{client} failed to connect {dep} after {ms} ms", f"{client} refused by {dep}", f"job {uid} aborted: quota exceeded", f"unhandled error in handler for request {uid}"]
    elif level == "WARN":
        options = [f"slow response from {dep} took {ms} ms", f"retry budget low for {client}", f"cache miss storm for key k{rng.randint(100, 999)}"]
    elif level == "DEBUG":
        options = [f"poll cycle {rng.randint(1, 500)} finished", f"loaded {rng.randint(1, 90)} rules"]
    else:
        options = [f"request {uid} completed in {ms} ms", f"indexed {rng.randint(1, 900)} documents", f"session refreshed for account {rng.randint(10000, 99999)}"]
    text = rng.choice(options)
    if pii and rng.random() < 0.08:
        user = f"user{rng.randint(1, 99)}@example.invalid"
        addr = f"192.0.2.{rng.randint(1, 254)}"
        return f"{text} (notify {user} from {addr})", True
    return text, False


def _line(style: str, ts: datetime, level: str, service: str, rng: random.Random, text: str) -> str:
    if style == "node":
        return f"[{ts.isoformat(timespec='milliseconds').replace('+00:00', 'Z')}] [{level}] [{service}] {text}"
    stamp = ts.strftime("%Y-%m-%d %H:%M:%S,") + f"{ts.microsecond // 1000:03d}"
    if style == "python_ansi":
        logger = rng.choice(("worker.tasks", "worker.queue", "worker.io"))
        return f"{ANSI_COLOUR[level]}[{stamp}] [{level}] [{logger}]\x1b[0m {text}"
    thread = f"main-{rng.randint(1, 8)}"
    cls = rng.choice(("org.example.fetch.Fetcher", "org.example.fetch.Pool", "org.example.http.Router"))
    return f"{stamp} {level:<5} [{thread}] {cls} - {text}"


def _continuations(style: str, rng: random.Random) -> list[str]:
    if style == "python_ansi":
        frames = [f'  File "/srv/app/worker/{m}.py", line {rng.randint(10, 400)}, in {f}' for m, f in (("tasks", "run"), ("io", "fetch"))[: rng.randint(1, 2)]]
        return ["Traceback (most recent call last):", *frames]
    if style == "node":
        return [f"    at Handler.{rng.choice(('route', 'call', 'next'))} (/srv/app/gateway/handler.js:{rng.randint(1, 300)}:{rng.randint(1, 40)})" for _ in range(rng.randint(2, 3))]
    lines = [f"\tat org.example.fetch.Fetcher.run(Fetcher.java:{rng.randint(10, 400)})" for _ in range(rng.randint(1, 3))]
    lines.append("Caused by: org.example.net.ConnectFailure: connection refused")
    lines.append(f"\t... {rng.randint(3, 30)} more")
    return lines


def _health(style: str, ts: datetime, service: str, rng: random.Random) -> str:
    return _line(style, ts, "INFO", service, rng, f"GET /healthz 200 {rng.randint(0, 3)}ms")


def generate_day(spec: SyntheticLogSpec) -> SyntheticDay:
    indices: dict[str, list[dict[str, Any]]] = {}
    mappings: dict[str, dict[str, Any]] = {}
    truth: dict[str, IndexTruth] = {}
    midnight = datetime(spec.day.year, spec.day.month, spec.day.day, tzinfo=timezone.utc)
    for tenant in spec.tenants:
        for env in spec.envs:
            for service in spec.services:
                name = index_name(tenant, env, service, spec.day)
                rng = random.Random(f"{spec.seed}|{name}")
                style = STYLE[service]
                docs: list[dict[str, Any]] = []
                t = IndexTruth()
                host = f"node-{rng.randint(1, 9):02d}"
                path = f"/var/log/app/{service}.log"
                offset = 0
                seconds = sorted(rng.uniform(1, 86_390) for _ in range(spec.lines_per_index))

                def emit(ts: datetime, message: str, level: str | None) -> None:
                    nonlocal offset
                    source: dict[str, Any] = {
                        "@timestamp": ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                        "message": message,
                        "host": {"name": host},
                        "kubernetes": {"container": {"name": service}},
                        "log": {"file": {"path": path}, "offset": offset},
                    }
                    if level is not None and style == "node":
                        source["level"] = level
                    offset += len(message) + 1
                    docs.append({"_id": f"{name}-{len(docs):06d}", "_source": source})
                    t.docs += 1

                # one head-less fragment at the start of the day
                emit(midnight + timedelta(milliseconds=200), _continuations(style, rng)[-1], None)
                t.fragments += 1
                for second in seconds:
                    ts = midnight + timedelta(seconds=second)
                    roll = rng.choices([lv for lv, _ in LEVEL_WEIGHTS], weights=[w for _, w in LEVEL_WEIGHTS])[0]
                    if roll == "noise":
                        emit(ts, _health(style, ts, service, rng), "INFO")
                        t.noise += 1
                        t.by_level["INFO"] = t.by_level.get("INFO", 0) + 1
                        continue
                    text, pii = _message(rng, roll, spec.pii_shapes)
                    t.pii_lines += int(pii)
                    emit(ts, _line(style, ts, roll, service, rng, text), roll)
                    t.by_level[roll] = t.by_level.get(roll, 0) + 1
                    if roll == "ERROR" and rng.random() < MULTILINE_SHARE:
                        t.multiline_heads += 1
                        for step, cont in enumerate(_continuations(style, rng), start=1):
                            emit(ts + timedelta(milliseconds=step), cont, None)
                            t.continuation_docs += 1
                indices[name] = docs
                mappings[name] = mapping_for(service)
                truth[name] = t
    return SyntheticDay(indices=indices, mappings=mappings, truth=truth)
