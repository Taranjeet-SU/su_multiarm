"""Optional private incident bundle adapter; fixtures are entirely synthetic."""

import importlib.util
import json
import os
from pathlib import Path

import pytest

location = os.environ.get("SU_INCIDENT_IMPORT_FILE")
if not location or not Path(location).is_file():
    pytest.skip("optional incident import helper not supplied", allow_module_level=True)
zstandard = pytest.importorskip("zstandard")
spec = importlib.util.spec_from_file_location("synthetic_incident_import", location)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
SCOPE = {
    "index": "su-synthetic-production-admin-2026.01.01",
    "day": "2026-01-01",
    "tenant": "synthetic",
    "env": "production",
    "service": "admin",
}


def record(identity, ts="2026-01-01T10:00:00Z"):
    return {
        **SCOPE,
        "id": identity,
        "ts": ts,
        "message": "db-client failed to connect redis after 30 ms",
        "level": "ERROR",
        "container": "synthetic-stream",
        "host": "synthetic-host",
    }


def write_manifest(path, value):
    path.write_text(json.dumps(value))


def fixtures(tmp_path):
    seed_dir, context_dir = tmp_path / "seed", tmp_path / "context"
    seed_dir.mkdir(parents=True)
    context_dir.mkdir()
    seed_file = seed_dir / "seed.jsonl.zst"
    seed_file.write_bytes(
        zstandard.ZstdCompressor().compress(
            (
                json.dumps({"contract": m.CONTRACT, "kind": "incident_seed", "record": record("seed-1")})
                + "\n"
            ).encode()
        )
    )
    seed = {
        "contract": m.CONTRACT,
        "phase": "seed_capture",
        "status": "done",
        "scope": SCOPE,
        "collection_scope": "incident_seed_policy",
        "full_capture": False,
        "grouped_capture": False,
        "complete_for_scope": False,
        "complete_for_selected_seeds": True,
        "count_reconciled": True,
        "source_total": 100,
        "selected_total": 1,
        "records_fetched": 1,
        "unique_source_ids": 1,
        "policy_id": "a" * 64,
        "query_id": "b" * 64,
        "recipe": "c" * 64,
        "storage_format": "incident_seed_records_v1",
        "file": seed_file.name,
        "sha256": m.sha(seed_file),
    }
    seed_path = seed_dir / "manifest.json"
    write_manifest(seed_path, seed)
    context_file = context_dir / "context.jsonl"
    context_file.write_text(json.dumps(record("context-1", "2026-01-01T10:00:10Z")) + "\n")
    context = {
        "contract": m.CONTRACT,
        "phase": "context",
        "scope": SCOPE,
        "full_capture": False,
        "context_snapshot": "separate_from_seed_snapshot",
        "seed_manifest": str(seed_path),
        "seed_sha256": seed["sha256"],
        "seed_ids": ["seed-1"],
        "context_seconds": 120,
        "context_status": "complete_for_window",
        "context_complete": True,
        "context_truncated": False,
        "query_id": "d" * 64,
        "context_record_ids": ["seed-1", "context-1"],
        "context_total": 2,
        "records_fetched": 2,
        "records_to_emit": 1,
        "already_present": 1,
        "budget": 1000,
        "window": {"gte": "2026-01-01T09:58:00Z", "lte": "2026-01-01T10:02:00Z"},
        "file": context_file.name,
        "sha256": m.sha(context_file),
    }
    context_path = context_dir / "manifest.json"
    write_manifest(context_path, context)
    return seed_path, context_path


def test_seed_and_context_roles_and_separate_counts(tmp_path):
    seed, context = fixtures(tmp_path)
    bundle = m.load_bundle(seed, [context])
    meta = bundle["manifest"]
    assert (
        meta["source_total"],
        meta["selected_seed_total"],
        meta["context_additional_documents"],
        meta["retained_unique_documents"],
    ) == (100, 1, 1, 2)
    assert meta["context_unique_documents"] == 2 and not meta["context_pending"]
    assert meta["source_completeness_fraction"] is None and not meta["all_source_complete"]
    rows = {row["record"]["id"]: row for row in bundle["records"]}
    assert rows["seed-1"]["roles"] == ["context", "seed"]
    assert rows["context-1"]["context_seed_ids"] == ["seed-1"]
    with pytest.raises(m.CoreIncidentScopeUnsupported, match="accounting"):
        m.to_unit_artifacts(bundle)


def test_missing_context_stays_pending_and_stages_only(tmp_path):
    seed, _ = fixtures(tmp_path)
    report = m.stage_bundle(seed, [], tmp_path / "staging")
    assert report["context_pending"] and report["seeds_without_complete_context"] == 1
    assert report["status"] == "validated_staged_not_imported" and not report["core_import_supported"]
    assert report["import_recipe"] != report["seed_recipe"]
    target = tmp_path / "staging" / report["import_recipe"] / report["artifact_file"]
    assert m.sha(target) == report["artifact_sha256"]
    assert not list((tmp_path / "staging").rglob("CURRENT"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("sha256", "0" * 64),
        ("source_total", 0),
        ("selected_total", 2),
        ("full_capture", True),
        ("collection_scope", "all_source_documents"),
        ("recipe", "missing"),
    ],
)
def test_invalid_seed_metadata_refused(tmp_path, field, value):
    seed, context = fixtures(tmp_path)
    data = json.loads(seed.read_text())
    data[field] = value
    write_manifest(seed, data)
    with pytest.raises(m.IncidentImportError):
        m.load_bundle(seed, [context])


@pytest.mark.parametrize(
    "corruption",
    [
        "sha",
        "scope",
        "seed_link",
        "count",
        "missing_id",
        "window",
        "timestamp",
        "stream",
        "host",
        "duplicate_row",
        "path_escape",
    ],
)
def test_invalid_context_integrity_scope_links_and_dedup_refused(tmp_path, corruption):
    seed, context = fixtures(tmp_path)
    data = json.loads(context.read_text())
    raw = context.parent / data["file"]
    if corruption == "sha":
        data["sha256"] = "0" * 64
    elif corruption == "scope":
        data["scope"] = {**SCOPE, "tenant": "other"}
    elif corruption == "seed_link":
        data["seed_ids"] = ["unknown-seed"]
    elif corruption == "count":
        data["records_fetched"] = 3
    elif corruption == "missing_id":
        data["context_record_ids"] = ["seed-1", "unknown-context"]
    elif corruption == "window":
        data["window"]["lte"] = "2026-01-01T11:00:00Z"
    elif corruption == "path_escape":
        data["file"] = "../seed/seed.jsonl.zst"
    else:
        row = json.loads(raw.read_text())
        if corruption == "timestamp":
            row["ts"] = "2026-01-01T11:00:00Z"
        if corruption == "stream":
            row["container"] = "other-stream"
        if corruption == "host":
            row["host"] = "other-host"
        raw.write_text((json.dumps(row) + "\n") * (2 if corruption == "duplicate_row" else 1))
        data["sha256"] = m.sha(raw)
        if corruption == "duplicate_row":
            data.update(records_to_emit=2, already_present=0)
    write_manifest(context, data)
    with pytest.raises(m.IncidentImportError):
        m.load_bundle(seed, [context])


def test_deferred_context_remains_pending(tmp_path):
    seed, context = fixtures(tmp_path)
    data = json.loads(context.read_text())
    raw = context.parent / data["file"]
    raw.write_text("")
    data.update(context_status="deferred_budget", context_complete=False, sha256=m.sha(raw))
    write_manifest(context, data)
    bundle = m.load_bundle(seed, [context])
    assert bundle["manifest"]["context_pending"] and bundle["manifest"]["pending_context_windows"] == 1
    assert len(bundle["records"]) == 1


def incident_config(tmp_path):
    from su_multi_arm.core.config import validate_config
    from tests.logs_gold_support import gold_config
    from tests.logs_support import write_library

    data = gold_config(tmp_path)
    write_library(tmp_path / "patterns")
    data["dataset"] = "elk-incidents"
    data["sources"]["incident_logs"] = data["sources"].pop("gold")
    data["sources"]["incident_logs"]["index_name"] = {
        "prefix": "su",
        "vocabularies": {"tenant": ["synthetic"], "env": ["production"], "service": ["admin"]},
    }
    data["logs"]["source"] = "incident_logs"
    data["logs"]["partitions"]["path"] = str(tmp_path / "elk-incidents/partitions")
    data["logs"]["parse"] = {"workers": 1}
    data["logs"]["history"] = {"tier": True}
    return validate_config(data)


def test_private_adapter_core_compose_coverage_and_scoped_error_table(tmp_path):
    from datetime import date
    from su_multi_arm.engine import Engine

    seed, context = fixtures(tmp_path)
    cfg = incident_config(tmp_path)
    result = m.import_exports(
        cfg, [{"seed_manifest": seed, "context_manifests": [context]}], compose=True, as_of=date(2026, 1, 1)
    )
    assert result["imports"]["2026-01-01"]["selected_seed_documents"] == 1
    assert result["compose"]["status"] == "built"
    engine = Engine(cfg)
    coverage = engine.call_tool("log_coverage", {"as_of": "2026-01-01", "window": "today"}).result.output
    assert coverage.rows and coverage.rows[0]["selected_seed_documents"] == 1
    assert coverage.rows[0]["context_documents"] == 1 and coverage.rows[0]["completeness"] is None
    table = engine.call_tool(
        "log_top", {"as_of": "2026-01-01", "window": "today", "by": "service_signature"}
    ).result.output
    assert table.rows and table.rows[0]["error_count"] == 1
    assert table.rows[0]["collection_scope"] == "incident_seed_policy_with_bounded_context"
    assert any("selected incident seeds" in note for note in table.notes)
    repeated = m.import_exports(
        cfg, [{"seed_manifest": seed, "context_manifests": [context]}], compose=True, as_of=date(2026, 1, 1)
    )
    assert repeated["imports"]["2026-01-01"]["status"] == "unchanged"
    assert repeated["compose"]["status"] == "unchanged"


def test_context_side_index_binds_exact_seed_manifest_without_mutating_collector_pointer(tmp_path):
    seed, context = fixtures(tmp_path)
    pointers, state = tmp_path / "current", tmp_path / "context-state"
    pointers.mkdir()
    state.mkdir()
    seed_meta = json.loads(seed.read_text())
    pointer = {
        key: seed_meta[key]
        for key in (
            "contract",
            "scope",
            "policy_id",
            "query_id",
            "recipe",
            "sha256",
            "selected_total",
            "source_total",
        )
    }
    pointer["manifest"] = str(seed)
    pointer_path = pointers / "synthetic.json"
    write_manifest(pointer_path, pointer)
    before = pointer_path.read_bytes()
    assert not m.current_descriptors(pointers, state)[0]["context_manifests"]
    fingerprint = m.sha(seed)
    sidecar = state / (fingerprint + ".json")
    write_manifest(sidecar, {"seed_manifest_sha256": fingerprint, "context_manifests": [str(context)]})
    descriptors = m.current_descriptors(pointers, state)
    assert descriptors[0]["context_manifests"] == [str(context)]
    assert pointer_path.read_bytes() == before
    assert not m.load_bundle(seed, descriptors[0]["context_manifests"])["manifest"]["context_pending"]
    write_manifest(sidecar, {"seed_manifest_sha256": "f" * 64, "context_manifests": [str(context)]})
    with pytest.raises(m.IncidentImportError, match="different seed"):
        m.current_descriptors(pointers, state)


@pytest.mark.skipif(not hasattr(m, "mapped_units"), reason="optional parallel mapping adapter not supplied")
class TestParallelMapping:
    def test_cache_reuse_and_context_config_invalidation(self, tmp_path, monkeypatch):
        seed, context = fixtures(tmp_path)
        cfg = incident_config(tmp_path)
        desc = {"seed_manifest": seed, "context_manifests": []}
        cache = tmp_path / "cache"
        first = list(m.mapped_units(cfg, [desc], workers=1, cache=cache))[0]
        original = m.load_bundle
        monkeypatch.setattr(m, "load_bundle", lambda *_: pytest.fail("unchanged cache remapped source"))
        report = {}
        repeated = list(m.mapped_units(cfg, [desc], workers=1, cache=cache, report=report))[0]
        assert repeated == first and report["cache_hits"] == 1 and report["mapped_units"] == 0
        monkeypatch.setattr(m, "load_bundle", original)
        context_desc = {**desc, "context_manifests": [context]}
        with_context = list(m.mapped_units(cfg, [context_desc], workers=1, cache=cache))[0]
        assert len(with_context.records) == 2 and with_context.unit.query_sha != first.unit.query_sha
        changed = cfg.model_copy(deep=True)
        changed.sources[changed.logs.source].max_line_bytes = 512
        remapped = list(m.mapped_units(changed, [context_desc], workers=1, cache=cache))[0]
        assert remapped.unit.query_sha != with_context.unit.query_sha
        before = m.mapping_task(context_desc, cfg, cache)["recipe"]
        monkeypatch.setattr(m, "core_mapping_identity", lambda: "f" * 64)
        assert m.mapping_task(context_desc, cfg, cache)["recipe"] != before

    @pytest.mark.parametrize("corruption", ["source", "context", "cache", "cache_metadata"])
    def test_cache_hit_still_refuses_corruption(self, tmp_path, corruption):
        seed, context = fixtures(tmp_path)
        cfg = incident_config(tmp_path)
        desc = {"seed_manifest": seed, "context_manifests": [context]}
        cache = tmp_path / "cache"
        list(m.mapped_units(cfg, [desc], workers=1, cache=cache))
        task = m.mapping_task(desc, cfg, cache)
        if corruption in {"source", "context"}:
            manifest = seed if corruption == "source" else context
            target = manifest.parent / json.loads(manifest.read_text())["file"]
        else:
            target = Path(task["artifact"])
            if corruption == "cache_metadata":
                target = target.with_suffix(".meta.json")
                data = json.loads(target.read_text())
                data["recipe"] = "0" * 64
                target.write_text(json.dumps(data))
        if corruption != "cache_metadata":
            with target.open("ab") as handle:
                handle.write(b"corruption")
        with pytest.raises(m.IncidentImportError, match="SHA|recipe"):
            list(m.mapped_units(cfg, [desc], workers=1, cache=cache))

    def test_parallel_parity_parent_only_partition_writes_and_admission(self, tmp_path, monkeypatch):
        from su_multi_arm.ingestion.partitions import FilePartitionStore

        descriptors = []
        for tenant in ("synthetic", "second", "third", "fourth"):
            seed, context = fixtures(tmp_path / tenant)
            if tenant != "synthetic":
                data = json.loads(seed.read_text())
                scope = {**SCOPE, "tenant": tenant, "index": SCOPE["index"].replace("synthetic", tenant)}
                raw = seed.parent / data["file"]
                envelope = {"contract": m.CONTRACT, "kind": "incident_seed", "record": {**record("seed-1"), **scope}}
                raw.write_bytes(zstandard.ZstdCompressor().compress((json.dumps(envelope) + "\n").encode()))
                data.update(scope=scope, sha256=m.sha(raw))
                write_manifest(seed, data)
            descriptors.append({"seed_manifest": seed, "context_manifests": []})
        cfg = incident_config(tmp_path)
        serial = list(m.mapped_units(cfg, descriptors, workers=1, cache=tmp_path / "serial"))
        report = {}
        parallel = list(m.mapped_units(cfg, descriptors, workers=4, cache=tmp_path / "parallel", report=report))
        assert sorted(a.model_dump_json() for a in serial) == sorted(a.model_dump_json() for a in parallel)
        assert report["worker_pids"] and os.getpid() not in report["worker_pids"]
        assert not Path(cfg.logs.partitions.path).exists()
        writes = []
        original = FilePartitionStore.put_unit

        def checked_write(self, *args, **kwargs):
            writes.append(os.getpid())
            return original(self, *args, **kwargs)

        monkeypatch.setattr(FilePartitionStore, "put_unit", checked_write)
        result = m.import_exports(cfg, descriptors, mapping_workers=4, mapping_cache=tmp_path / "parallel")
        assert writes == [os.getpid()] * 4
        assert result["imports"]["2026-01-01"]["mapping"]["cache_hits"] == 4
        with pytest.raises(m.IncidentImportError, match="memory admission"):
            list(m.mapped_units(cfg, descriptors, workers=4, cache=tmp_path / "tiny", memory_bytes=1))
        with pytest.raises(m.IncidentImportError, match="cache budget"):
            list(m.mapped_units(cfg, descriptors, workers=4, cache=tmp_path / "tiny", cache_bytes=1))
