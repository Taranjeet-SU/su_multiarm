"""``doctor --probe`` and CLI v2 (plan Phase 1 row "doctor --probe + CLI v2"): the engine is built,
every backend is probed, provider capability pairs are reported from the registry and no model is
called; sessions persist in the configured sqlite session store across engine instances; the CLI
reaches sessions, tools and actions through the same Python API."""

from __future__ import annotations

import json
import sys

import pytest
import yaml

from su_multi_arm import Document, Engine
from su_multi_arm.cli import main
from su_multi_arm.core.config import validate_config
from su_multi_arm.pipeline.validate import probe_report
from su_multi_arm.providers import JsonModel

FAKE = {"provider": "openai_compatible", "model": "fake-local", "base_url": "http://127.0.0.1:9/v1"}
DOCS = [
    Document(id="a", dataset="demo", title="Alpha", fields={"summary": "billing invoices and rounding"}, metadata={"project": "Alpha"}),
    Document(id="b", dataset="demo", title="Beta", fields={"summary": "table layouts and rows"}, metadata={"project": "Beta"}),
]


def data(kind: str, state=None):
    backend = {"kind": kind} if kind == "memory" else {"kind": kind, "path": str(state)}
    return {
        "schema_version": 2,
        "dataset": "demo",
        "backends": {"local": backend},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "metadata_schema": {"fields": [{"name": "project", "type": "string", "listable": True}]},
        "pipeline": {"arms": {"lexical": {"backend": "local"}, "dense": {"backend": "local"}}},
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 64}, "generator": FAKE, "planner": FAKE}},
        "request": {"modes": ["llm"]},
        "actions": {"allow": ["invalidate_cache"]},
    }


def forbid_model_calls(monkeypatch):
    def refuse(*args, **kwargs):  # pragma: no cover - reached only on a regression
        raise AssertionError("doctor --probe must not call a model")

    monkeypatch.setattr(JsonModel, "call", refuse)
    monkeypatch.setattr(JsonModel, "call_text", refuse)
    monkeypatch.setattr(JsonModel, "text", refuse)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_probe_reports_backends_and_provider_pairs_without_model_calls(monkeypatch, tmp_path, kind):
    forbid_model_calls(monkeypatch)
    engine = Engine(validate_config(data(kind, tmp_path / "state")))
    report = probe_report(engine)
    backend = report["backends"]["local"]
    assert report["probe"] == "ok" and report["model_calls"] == 0 and backend["status"] == "ok" and backend["kind"] == kind
    assert set(backend["contracts"]) == {"catalog", "lexical", "vector", "records", "graph", "cache", "ledger", "session"}
    if kind == "sqlite":
        assert backend["detail"]["sqlite_version"] and backend["detail"]["generations"] == 0
    else:
        assert backend["detail"]["in_process"] is True
    generator = report["models"]["generator"]
    assert generator["called"] is False and generator["registry"]["role_declared"] is True and generator["declared"]["structured_output"] == "none"
    assert report["models"]["embedder"]["provider"] == "hash"
    assert report["pipelines"]["default"]["modes"] == ["llm", "rules"] and report["actions"] == ["invalidate_cache"]


def test_probe_failure_is_reported_not_hidden(tmp_path):
    engine = Engine(validate_config(data("memory")))

    def broken():
        raise ConnectionError("unreachable")

    engine.backends["local"].probe = broken
    report = probe_report(engine)
    assert report["probe"] == "failed" and report["backends"]["local"]["status"] == "error" and "unreachable" in report["backends"]["local"]["error"]


def run_cli(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["su-multi-arm", *argv])
    main()
    return json.loads(capsys.readouterr().out)


def test_cli_doctor_probe_on_sqlite(monkeypatch, capsys, tmp_path):
    forbid_model_calls(monkeypatch)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data("sqlite", tmp_path / "state")))
    output = run_cli(monkeypatch, capsys, "--config", str(path), "doctor", "--probe")
    assert output["config"] == "valid" and output["pipelines"] == ["default"]
    assert output["probe"]["probe"] == "ok" and output["probe"]["backends"]["local"]["status"] == "ok"
    plain = run_cli(monkeypatch, capsys, "--config", str(path), "doctor")
    assert "probe" not in plain


def test_sessions_persist_in_the_sqlite_store_and_the_cli_matches_the_api(monkeypatch, capsys, tmp_path):
    state = tmp_path / "state"
    path = tmp_path / "config.yaml"
    config = data("sqlite", state)
    config["models"]["roles"].pop("generator")
    config["models"]["roles"].pop("planner")
    config["request"] = {}
    path.write_text(yaml.safe_dump(config))
    engine = Engine(validate_config(config))
    engine.ingest(DOCS)
    session_id = run_cli(monkeypatch, capsys, "--config", str(path), "session", "create")["session_id"]
    first = run_cli(monkeypatch, capsys, "--config", str(path), "search", "billing", "--session", session_id)
    assert first["trace"]["session"]["turn"] == 1
    second = Engine(validate_config(config)).search("rows", session_id=session_id)  # a fresh engine, same store
    assert second.trace["session"]["turn"] == 2
    record = run_cli(monkeypatch, capsys, "--config", str(path), "session", "show", session_id)
    assert len(record["turns"]) == 2 and record["turns"][0]["shown_ids"] == [h["document_id"] for h in first["hits"]]
    tool = run_cli(monkeypatch, capsys, "--config", str(path), "tool", "list_values", "--args", '{"field": "project"}')
    assert tool["result"]["output"]["values"] == ["Alpha", "Beta"]
    action = run_cli(monkeypatch, capsys, "--config", str(path), "action", "invalidate_cache", "--idempotency-key", "k1")
    assert action["result"]["status"] == "applied"
    again = Engine(validate_config(config)).audit.records()
    assert [(r.action, r.status) for r in again] == [("invalidate_cache", "applied")]  # audit persisted in the ledger


@pytest.mark.parametrize(
    ("module", "name"),
    [("su_multi_arm.backends.postgres", "PostgresBackend"), ("su_multi_arm.backends.sql", "SqlBackend"), ("su_multi_arm.backends.neo4j", "Neo4jBackend")],
)
def test_server_adapters_probe_through_their_typed_ping(module, name):
    import importlib
    import types

    try:
        mod = importlib.import_module(module)
    except ModuleNotFoundError as missing:  # the adapter's optional extra is not installed
        pytest.skip(str(missing))
    cls = getattr(mod, name)
    stub = types.SimpleNamespace(ping=lambda: {"version": "x"})
    assert cls.probe(stub) == {"version": "x"}  # probe() is ping(): reachability + version, typed failures
