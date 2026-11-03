"""Shared fixtures for the serving/streaming tests: a synthetic v2 config on the memory or sqlite
backend whose generator is the loopback ``FakeChatServer``, and a normaliser that removes the only
fields allowed to differ between two runs of the same request (timings, trace ids, session info)."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

from su_multi_arm import Document
from su_multi_arm.core.config import validate_config

from conftest import v2_schema_model  # tests/conftest.py (rootdir conftest, on sys.path)

ROWS = [
    ("r1", "Alpha", "open", "REC-1001", "Alpha billing record about invoices and rounding."),
    ("r2", "Alpha", "closed", "REC-1002", "Alpha record closed after the billing dispute."),
    ("r3", "Beta", "open", "REC-1003", "Beta open record on table layouts and rows."),
    ("r4", "Beta", "pending", "REC-1004", "Beta pending record about region reports and billing."),
]
DOCS = [
    Document(
        id=rid,
        dataset="demo",
        kind="record",
        title=f"Record {rid}",
        fields={"summary": text, "details": f"Details for {rid}: {text}"},
        metadata={"kind": "record", "project": project, "status": status, "external_id": ext, "tag": "t1", "created_at": "2024-03-02T10:00:00+00:00"},
    )
    for rid, project, status, ext, text in ROWS
]
REQUEST = {"query": "billing records", "filters": {"project": "Alpha"}, "exclude_ids": ["r2"]}
VOLATILE = frozenset({"seconds", "trace_id", "session", "at", "elapsed_seconds"})


def config_data(backend: dict[str, Any], generator: dict[str, Any] | None, **overrides: Any) -> dict[str, Any]:
    roles: dict[str, Any] = {"embedder": {"provider": "hash", "dimensions": 64}}
    if generator is not None:
        roles["generator"] = generator
    data: dict[str, Any] = {
        "schema_version": 2,
        "dataset": "demo",
        "backends": {"local": backend},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "metadata_schema": v2_schema_model().model_dump(mode="json"),
        "pipeline": {"arms": {"dense": {"backend": "local"}, "lexical": {"backend": "local"}, "structured": {"backend": "local"}}},
        "models": {"roles": roles},
    }
    for key, value in overrides.items():
        data[key] = {**data[key], **value} if isinstance(value, dict) and isinstance(data.get(key), dict) else value
    return data


def memory_config(generator: dict[str, Any] | None, **overrides: Any):
    return validate_config(config_data({"kind": "memory"}, generator, **overrides))


def sqlite_config(state: Path, generator: dict[str, Any] | None, **overrides: Any):
    return validate_config(config_data({"kind": "sqlite", "path": str(state)}, generator, **overrides))


def normalise(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: normalise(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [normalise(v) for v in value]
    return value


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
