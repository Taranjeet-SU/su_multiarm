"""Optional bounded context continuation with synthetic seed files and source responses."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

location = os.environ.get("SU_INCIDENT_CONTEXT_WORKER_DIR")
collector = os.environ.get("SU_INCIDENT_BATCH_DIR")
if not location or not collector:
    pytest.skip("optional private context worker not supplied", allow_module_level=True)
sys.path.insert(0, str(Path(collector).resolve()))
sys.path.insert(0, str(Path(location).resolve()))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(location) / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = load("tested_context_worker", "context_worker.py")
context = load("tested_merged_context", "incident_context.py")
import incident_batch as batch  # noqa: E402

SCOPE = dict(index="demo-api-2026.01.04", day="2026-01-04", tenant="demo", env="test", service="api")
PROPS = {"@timestamp": {"type": "date"}, "container": {"properties": {"name": {"type": "keyword"}}},
         "host": {"properties": {"name": {"type": "keyword"}}}}


def record(identity, second):
    return {**SCOPE, "id": identity, "ts": f"2026-01-04T12:{second // 60:02d}:{second % 60:02d}Z",
            "container": "worker", "host": "node", "message": "synthetic masked log"}


def test_merged_plan_uses_exact_minmax_seed_bounds_and_one_stream():
    first, second = record("s1", 0), record("s2", 30)
    plan = context.context_plan(first, SCOPE, PROPS, seeds=[second])
    assert plan["window"] == {"gte": "2026-01-04T11:58:00+00:00", "lte": "2026-01-04T12:02:30+00:00"}
    assert plan["seed_ids"] == ["s1", "s2"]
    with pytest.raises(ValueError, match="stream"):
        context.context_plan(first, SCOPE, PROPS, seeds=[{**second, "container": "other"}])


def test_pending_windows_merge_overlap_with_bounded_span_and_no_cross_stream():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE seeds(id TEXT,ts REAL,stream TEXT,record TEXT,done INTEGER)")
    for identity, timestamp, stream in [("a", 1000, "one"), ("b", 900, "one"), ("c", 500, "one"), ("d", 950, "two")]:
        db.execute("INSERT INTO seeds VALUES(?,?,?,?,0)", (identity, timestamp, stream, json.dumps({"id": identity})))
    assert [row["id"] for row in worker.next_group(db)] == ["a", "b"]
    db.close()


def test_fair_service_rotation_with_newest_index_priority():
    def pointer(service, day):
        return {"scope": {**SCOPE, "service": service, "day": day}, "manifest": service + day}
    pointers = [pointer("a", "2026-01-03"), pointer("a", "2026-01-04"), pointer("b", "2026-01-04")]
    output = worker.fair_candidates(pointers, {}, "a")
    assert [item["scope"]["service"] for item in output] == ["b", "a"]
    assert output[1]["scope"]["day"] == "2026-01-04"


def test_bounded_context_state_is_independent_and_idempotent(tmp_path):
    import zstandard
    seeds = [record("s1", 0), record("s2", 30), record("s3", 60)]
    root = tmp_path / "incidents"
    directory = root / "exports/run"
    directory.mkdir(parents=True)
    seed_file = directory / "seeds.jsonl.zst"
    seed_file.write_bytes(zstandard.ZstdCompressor().compress(b"".join(
        (json.dumps({"contract": "elk_incidents_v1", "kind": "incident_seed", "record": row}) + "\n").encode() for row in seeds)))
    digest = hashlib.sha256(seed_file.read_bytes()).hexdigest()
    metadata = {"status": "done", "complete_for_selected_seeds": True, "count_reconciled": True,
                "scope": SCOPE, "sha256": digest, "file": seed_file.name, "selected_total": 3, "records_fetched": 3}
    seed_manifest = directory / "manifest.json"
    seed_manifest.write_text(json.dumps(metadata))
    descriptor = {"scope": SCOPE, "manifest": str(seed_manifest), "sha256": digest}
    pointer = root / "state/current/demo.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps(descriptor))
    before = pointer.read_bytes()
    info = {**SCOPE, "in_scope": True}
    adapter = SimpleNamespace(zstd=zstandard, parse=lambda name: info, safe_redactor=lambda privacy: object(),
        full_pull=SimpleNamespace(record=lambda hit, *args: {**hit["_source"], "id": hit["_id"]}))
    rows = seeds + [record("context-only", 100)]
    class Source:
        def __init__(self):
            self.calls = []

        def request(self, method, path, body=None, **kwargs):
            self.calls.append((method, path, body))
            if method == "GET":
                return {SCOPE["index"]: {"mappings": {"properties": PROPS}}}
            return {"timed_out": False, "_shards": {"total": 1, "successful": 1, "failed": 0},
                    "hits": {"total": {"value": 4, "relation": "eq"}, "hits": [
                        {"_index": SCOPE["index"], "_id": row["id"], "_source": row} for row in rows]}}
    es = Source()
    attempted, state = worker.run_one(es, adapter, SimpleNamespace(privacy=None), root, descriptor, batch, context)
    assert attempted and len(es.calls) == 2
    assert not state["context_pending"] and len(state["context_manifests"]) == 1
    manifest = json.loads(Path(state["context_manifests"][0]).read_text())
    assert set(manifest["seed_ids"]) == {"s1", "s2", "s3"}
    assert manifest["records_to_emit"] == 1 and manifest["already_present"] == 3
    assert manifest["context_total"] == 4 and manifest["seed_sha256"] == digest
    assert pointer.read_bytes() == before
    assert state["seed_manifest_sha256"] == hashlib.sha256(seed_manifest.read_bytes()).hexdigest()
    attempted, again = worker.run_one(es, adapter, SimpleNamespace(privacy=None), root, descriptor, batch, context)
    assert not attempted and len(es.calls) == 2 and again["queue_exhausted"]
    assert pointer.read_bytes() == before


def test_busy_collector_lock_defers_without_loading_adapter_or_source(tmp_path, monkeypatch, capsys):
    import fcntl
    state = tmp_path / "state"
    state.mkdir()
    with (state / "coordinator.lock").open("a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(sys, "argv", ["context_worker", "--root", str(tmp_path), "--collector-dir", collector,
                                         "--adapter-dir", "/nonexistent", "--config", "/nonexistent"])
        worker.main()
    assert json.loads(capsys.readouterr().out)["status"] == "deferred_collector_busy"
    assert not (state / "context-worker.pid.json").exists()


def test_global_window_budget_stops_at20_without_mutating_seed_pointers(tmp_path, monkeypatch):
    state = tmp_path / "state/current"
    state.mkdir(parents=True)
    for i in range(25):
        (state / f"index{i}.json").write_text(json.dumps({"scope": {**SCOPE, "service": f"service{i:02d}"}, "manifest": f"seed{i}"}))
    calls = []
    def one(*args, **kwargs):
        calls.append(args[4]["manifest"])
        return True, {}
    monkeypatch.setattr(worker, "run_one", one)
    fake_es = SimpleNamespace(request=lambda *args, **kwargs: {}, close=lambda: None)
    adapter = SimpleNamespace(load_config=lambda path: None, esro=SimpleNamespace(ReadOnlyES=lambda *args, **kwargs: fake_es))
    monkeypatch.setitem(sys.modules, "grouped_pull", adapter)
    monkeypatch.setattr(sys, "argv", ["context_worker", "--root", str(tmp_path), "--collector-dir", collector,
                                     "--adapter-dir", "/nonexistent", "--config", "/nonexistent"])
    worker.main()
    assert len(calls) == len(set(calls)) == 20
    assert len(list(state.glob("*.json"))) == 25


def test_retained_unqueryable_host_defers_instead_of_broadening_stream():
    properties = {key: value for key, value in PROPS.items() if key != "host"}
    plan = context.context_plan(record("seed", 0), SCOPE, properties)
    assert plan["context_status"] == "deferred_stream_mapping"
    assert "request" not in plan
