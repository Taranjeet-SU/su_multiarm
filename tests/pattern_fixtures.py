"""Synthetic pattern-library fixtures (addendum §3.3 example layout; N18: unrelated to any real system).

Every line, name and value here is generated. ``synthetic_lines`` is deterministic for a seed.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest
import yaml

SERVICES = ["ingest-worker", "query-gateway", "doc-fetcher", "scheduler"]
MASKS = {
    "uuid": "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    "num": r"-?[0-9]+(?:\.[0-9]+)?",
}
FORMAT = {
    "id": "fmt.kv_v1",
    "kind": "format",
    "status": "approved",
    "priority": 100,
    "applies_to": {"field": "message", "service": ["*"]},
    "regex": r'^ts=(?P<ts>[0-9T:.\-]+Z) lvl=(?P<level>[a-z]+) comp=(?P<component>[a-z0-9_.]+) msg="(?P<body>[^"]*)"$',
    "fields": {"ts": {"type": "datetime"}, "level": {"type": "enum", "values": ["debug", "info", "warn", "error", "fatal"]}},
    "entities": [{"group": "component", "type": "component"}],
    "then": {"field_from_group": "body"},
    "examples": {"positive": ['ts=2026-11-04T10:00:00.000Z lvl=error comp=net.client msg="db-client failed to connect redis"']},
    "provenance": {"author": "user"},
}
CONNECT = {
    "id": "msg.dependency.connect_failed",
    "kind": "message",
    "status": "approved",
    "priority": 50,
    "applies_to": {"service": ["*"], "level": ["error", "warn"]},
    "regex": r"(?P<client>[a-z]+-client) (?P<trigger>failed to connect|refused by) (?P<dep>kafka|mongo|postgres|redis)(?: after (?P<ms>[0-9]+) ?ms)?",
    "signature": {"id": "dependency.connect_failed", "category": "connectivity"},
    "fields": {"ms": {"type": "integer"}},
    "entities": [{"group": "client", "type": "component"}, {"group": "dep", "type": "dependency"}],
    "relations": [{"subject": "client", "predicate": "fails_to_connect", "object": "dep", "trigger_group": "trigger"}],
    "attribute_links": [{"subject": "$service", "predicate": "emits", "object": "$signature"}],
    "examples": {
        "positive": ["db-client failed to connect redis after 3000 ms", "queue-client refused by kafka", "http-client failed to connect mongo"],
        "negative": ["db-client connected to redis"],
    },
    "provenance": {"author": "user", "created": "2026-11-04T10:00:00Z"},
}
# Bodies of the synthetic generator: (template, level). ``{uuid}``/``{n}``/``{word}`` vary per line.
BODIES = [
    ("{client} failed to connect {dep} after {n} ms", "error"),
    ("cache miss for key {uuid} in {n} ms", "warn"),
    ("job {word} finished with status {n}", "info"),
    ("retrying request {uuid} attempt {n} of 5", "warn"),
    ("worker pool resized from {n} to {n} slots", "info"),
    ("checkpoint written for shard {n} at offset {n}", "info"),
    ("session {uuid} expired after {n} seconds idle", "warn"),
]
WORDS = ["alpha", "bravo", "delta", "kilo", "lima", "oscar", "tango", "zulu"]


def write_library(root: Path, patterns: list[dict] | None = None, **header) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    data = {"library": "synthetic-core", "format_version": 1, "engine": "re2", "vocabularies": {"service": SERVICES}, "masks": dict(MASKS)}
    data.update(header)
    data["patterns"] = list(patterns or [])
    (root / "library.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    return root


def write_system(root: Path, name: str, rows: list[dict]) -> None:
    (root / name).write_text(yaml.safe_dump(rows, sort_keys=False))


def _uuid(rng: random.Random) -> str:
    h = "".join(rng.choice("0123456789abcdef") for _ in range(32))
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def synthetic_lines(count: int, seed: int = 7) -> list[tuple[str, str, dict[str, str]]]:
    """``(line id, raw line, attributes)`` in the key=value layout of ``FORMAT``."""
    rng = random.Random(seed)
    out = []
    for i in range(count):
        template, level = BODIES[i % len(BODIES)]
        body = template
        while "{" in body:
            body = (
                body.replace("{client}", rng.choice(["db-client", "queue-client", "http-client"]), 1)
                .replace("{dep}", rng.choice(["kafka", "mongo", "postgres", "redis"]), 1)
                .replace("{uuid}", _uuid(rng), 1)
                .replace("{n}", str(rng.randint(1, 99999)), 1)
                .replace("{word}", rng.choice(WORDS), 1)
            )
        minute, second = divmod(i, 60)
        line = f'ts=2026-11-04T10:{minute % 60:02d}:{second:02d}.000Z lvl={level} comp=svc.core msg="{body}"'
        out.append((f"l{i}", line, {"service": SERVICES[i % len(SERVICES)]}))
    return out


def engine_or_skip(kind: str):
    from su_multi_arm.extraction.patterns.engine import PatternEngineUnavailable, select_engine

    try:
        return select_engine(kind)
    except PatternEngineUnavailable as exc:
        pytest.skip(str(exc))
