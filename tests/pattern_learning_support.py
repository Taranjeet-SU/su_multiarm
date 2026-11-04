"""Synthetic fixtures for the LLM pattern-learning loop (L4b): a library, log lines, a scripted fake
``pattern_learner`` served through ``httpx.MockTransport`` (no network, no key, no spend) and v2
configs. Every name, address and value is generated; the e-mail and IP shapes use reserved
documentation ranges and domains."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import yaml

from su_multi_arm.core.config import validate_config
from su_multi_arm.learning.patterns import LogLine
from tests.pattern_fixtures import CONNECT, FORMAT, write_library

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
UUIDS = [f"{i:08x}-0000-4000-8000-{i:012x}" for i in range(1, 13)]
MS = [3, 7, 5, 120, 4, 9, 2, 450, 6, 8, 1, 330]  # the 1-digit values let a narrow regex miss some lines

CACHE_RE = r"^cache miss for key (?P<key>[0-9a-f\-]+) in (?P<ms>[0-9]+) ms$"
SESSION_RE = r"^session (?P<sid>[0-9a-f\-]+) expired after (?P<s>[0-9]+) seconds idle$"


def line(i: int, level: str, body: str, *, service: str = "ingest-worker", tenant: str = "tenant-alpha", host: str = "node-alpha-01") -> LogLine:
    text = f'ts=2026-11-04T10:00:{i % 60:02d}.000Z lvl={level} comp=svc.core msg="{body}"'
    return LogLine(f"l{i:03d}", text, {"service": service, "tenant": tenant, "host": host})


def learning_lines() -> list[LogLine]:
    """12 cache-miss lines (warn), 6 session-expiry lines (warn), 4 lines CONNECT parses (error),
    and 3 short 'cache miss for user' lines (a separate cluster, for the over-breadth bound)."""
    out = []
    for i in range(12):
        tenant = "tenant-alpha" if i % 2 else "tenant-beta"
        out.append(line(i, "warn", f"cache miss for key {UUIDS[i]} in {MS[i]} ms", tenant=tenant))
    for i in range(6):
        out.append(line(20 + i, "warn", f"session {UUIDS[i]} expired after {60 + i} seconds idle"))
    for i, dep in enumerate(["redis", "kafka", "mongo", "postgres"]):
        out.append(line(40 + i, "error", f"db-client failed to connect {dep} after {100 + i} ms"))
    for i, user in enumerate(["u-one", "u-two", "u-three"]):
        out.append(line(60 + i, "warn", f"cache miss for user {user}"))
    return out


def library_dir(root: Path, extra: list[dict] | None = None) -> Path:
    return write_library(root, [FORMAT, CONNECT, *(extra or [])])


def body(text: str) -> str:
    return text.split('msg="', 1)[1][:-1]


def reply(payload: dict[str, Any], *, regex: str | None = CACHE_RE, action: str = "new", positives: list[str] | None = None, negatives: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    ids = [row["id"] for row in payload["lines"]]
    data = {
        "cluster_id": payload["cluster_id"],
        "action": action,
        "target_pattern_id": None,
        "issue_name": "cache miss",
        "issue_category": "performance",
        "regex": regex,
        "fields": [{"group": "ms", "type": "integer"}] if regex and "?P<ms>" in regex else [],
        "entities": [],
        "relations": [],
        "positive_line_ids": ids[:3] if positives is None else positives,
        "negative_line_ids": negatives or [],
        "rationale": "the lines share one template",
    }
    data.update(extra)
    return data


def by_template(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The default script: a correct pattern per known cluster template, ``none`` otherwise."""
    template = payload["template"]
    if template.startswith("cache miss for key"):
        return reply(payload)
    if template.startswith("session"):
        return reply(payload, regex=SESSION_RE, issue_name="session expiry", issue_category="lifecycle", fields=[{"group": "s", "type": "integer"}])
    return reply(payload, regex=None, action="none")


class FakeLearner:
    """``httpx.MockTransport`` handler answering chat completions with ``script(payload)``.

    ``script`` returns a reply mapping (serialised as the message content) or a raw string. Every
    request body is kept, so tests can assert what left the process."""

    def __init__(self, script: Callable[[dict[str, Any]], Any] = by_template, usage: tuple[int, int] = (400, 120)):
        self.script, self.usage = script, usage
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        payload = json.loads(body["messages"][-1]["content"])
        answer = self.script(payload)
        content = answer if isinstance(answer, str) else json.dumps(answer)
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": self.usage[0], "completion_tokens": self.usage[1]},
            },
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def sent_text(self) -> str:
        return "\n".join(json.dumps(r) for r in self.requests)


LOCAL_ROLE = {"provider": "openai_compatible", "model": "local-learner", "base_url": "http://127.0.0.1:9/v1", "max_tokens": 512}
HOSTED_ROLE = {"provider": "openai", "model": "gpt-4o-mini", "api_key_env": "SU_TEST_FAKE_KEY", "max_tokens": 512, "capabilities": {"structured_output": "native"}}
PRICE = 1000.0  # USD per million tokens: one call (400 in, 120 out) = $0.52


def config_data(root: Path, library: Path, *, engine: str = "regex", promotion: str = "auto", role: dict | None = None, learning: dict | None = None, privacy: dict | None = None, price_table: bool = False) -> dict[str, Any]:
    gate = {"require_re2": engine == "re2"}
    data: dict[str, Any] = {
        "schema_version": 2,
        "dataset": "logs",
        "backends": {"local": {"kind": "memory"}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "pipeline": {"arms": {"lexical": {"backend": "local"}}},
        "extraction": {"extractors": ["pattern"], "pattern_library": {"path": str(library), "engine": engine, "promotion": promotion, "gate": gate}},
        "models": {"roles": {"pattern_learner": role or LOCAL_ROLE}},
        "privacy": privacy if privacy is not None else {"redact_emails": True, "patterns": {"IPV4": r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b"}},
        "learning": {"patterns": {"spend_ledger": str(root / "spend.sqlite"), **(learning or {})}},
    }
    if price_table:
        path = root / "prices.yaml"
        model = (role or LOCAL_ROLE)["model"]
        path.write_text(yaml.safe_dump({"version": "test-1", "prices": {model: {"input_per_million": PRICE, "output_per_million": PRICE}}}))
        data["budgets"] = {"price_table": str(path)}
    return data


def make_config(root: Path, library: Path, **kwargs: Any):
    return validate_config(config_data(root, library, **kwargs))


def clock() -> datetime:
    return NOW
