"""Optional file importer checks using synthetic records, without source/provider calls.

Set SU_PARALLEL_IMPORT_DIR to a directory containing parallel_import.py and its
adapter dependencies. SU_PARALLEL_IMPORT_CONFIG optionally selects an external
configuration; otherwise these checks build a fully synthetic configuration.
All partitions, libraries and fixture files stay under pytest's temporary root.
"""

import importlib
import json
import sys
import os
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from su_multi_arm.core.config import validate_config
from tests.logs_support import ENGINE, config_data, write_library

location = os.environ.get("SU_PARALLEL_IMPORT_DIR")
if not location or not (Path(location) / "parallel_import.py").is_file():
    pytest.skip("optional file importer not supplied", allow_module_level=True)
sys.path.insert(0, str(Path(location).resolve()))
p = importlib.import_module("parallel_import")
if Path(p.__file__).resolve() != (Path(location) / "parallel_import.py").resolve():
    raise RuntimeError("parallel_import was loaded from a different helper directory")
zstandard = pytest.importorskip("zstandard")

DAY = "2026-11-02"
TENANTS = ["synthetica", "syntheticb", "syntheticc", "syntheticd"]


def cfg_at(path):
    write_library(path / "patterns")
    config_path = os.environ.get("SU_PARALLEL_IMPORT_CONFIG")
    if config_path:
        cfg = p.load_config(Path(config_path)).model_copy(deep=True)
    else:
        data = config_data(path)
        data["privacy"] = {"engine": "safe", "regex_engine": ENGINE, "redactors": ["email"]}
        data["communities"] = {"strategy": "declared"}
        data["logs"]["compose"] = {"kinds": {"shared_error": True}}
        source = data["sources"]["synthetic"]
        source["index_name"] = {
            "prefix": "su",
            "vocabularies": {"tenant": TENANTS, "env": ["production"], "service": ["admin"]},
        }
        source["mapping"]["container"] = "container.name"
        source["mapping"]["file"] = "file"
        cfg = validate_config(data)
    # Even an explicitly selected external config uses isolated synthetic state/library.
    cfg.logs.partitions.path = path / "parts"
    cfg.logs.parse.workers = 1
    cfg.extraction.pattern_library.path = path / "patterns"
    cfg.extraction.pattern_library.engine = ENGINE
    cfg.sources[cfg.logs.source].index_name.vocabularies["tenant"] = list(TENANTS)
    return cfg


def fixture(path, tenant="synthetica", count=40, grouped=False, broken=False):
    path.mkdir(parents=True, exist_ok=True)
    index = f"su-{tenant}-production-admin-2026.11.02"
    records = []
    for i in range(count):
        message = "2026-11-02 10:00:00 INFO Synthetic request completed user=person@example.com"
        if i % 11 == 1:
            message = "    at synthetic.module(Fixture.java:11)"
        records.append(
            {
                "id": f"{tenant}-{i}",
                "index": index,
                "stratum": "A",
                "ts": f"2026-11-02T10:00:{i % 60:02d}Z",
                "message": message,
                "env": "production",
                "container": "synthetic-container",
                "file": "/synthetic/app.log",
                "offset": i,
            }
        )
    if broken:
        records[-1]["id"] = records[0]["id"]
    target = path / (tenant + ".jsonl.zst")
    target.write_bytes(
        zstandard.ZstdCompressor().compress("".join(json.dumps(r) + "\n" for r in records).encode())
    )
    info = {
        "finished": "2026-11-05T00:00:00Z",
        "capped": False,
        "tenant": tenant,
        "env": "production",
        "service": "admin",
        "sha256": p.ip.sha256(target),
        "file": target.name,
    }
    if grouped:
        info.update(
            contract=p.ip.gi.CONTRACT,
            storage_format="full_records_v1",
            snapshot_consistent=True,
            source_total=count,
            grouped_occurrences=0,
            raw_occurrences=count,
            groups=0,
            downloaded_examples=0,
        )
    else:
        info.update(es_counts={"F": 0, "E": 0, "W": 0, "I": count, "total": count}, pulled={"A": count})
    return {
        "day": DAY,
        "index": index,
        "info": info,
        "path": target,
        "origin": "synthetic",
        "grouped_capture": grouped,
        "full_capture": not grouped,
    }


def pipeline(cfg, descriptors, work, parallel=True, limits=None):
    cls = p.LazyFileSource if parallel else p.ip.FileSource
    source = cls(cfg.sources[cfg.logs.source], {DAY: descriptors}, {}, source_name=cfg.logs.source, env={})
    kwargs = dict(
        source=source, write_path=SimpleNamespace(backends={}), redactor=p.safe_redactor(cfg.privacy)
    )
    if parallel:
        return p.ParallelFileLogsPipeline(
            cfg, work=work, limits=limits or p.ImportLimits(disk_reserve_bytes=0), **kwargs
        )
    return p.ip.FileLogsPipeline(cfg, **kwargs)


def canonical(pipe):
    artifacts = []
    for a in pipe.store.units(DAY):
        d = a.model_dump(mode="json")
        d.pop("mapping")
        d["count"].pop("at", None)
        artifacts.append(d)
    return sorted(artifacts, key=lambda a: a["unit"]["id"])


def test_serial_parallel_parity_parent_writes_and_limits(tmp_path):
    ds = [fixture(tmp_path / "raw", t, 70) for t in ["synthetica", "syntheticb", "syntheticc", "syntheticd"]]
    serial = pipeline(cfg_at(tmp_path / "serial"), ds, tmp_path / "unused", False)
    sm, _ = serial.pull(date.fromisoformat(DAY), serial.library())
    limits = p.ImportLimits(
        disk_reserve_bytes=0,
        large_unit_bytes=1000000,
        process_base_bytes=2 * 1024**2,
        memory_cap_bytes=32 * 1024**2,
    )
    par = pipeline(cfg_at(tmp_path / "parallel"), ds, tmp_path / "cache", limits=limits)
    parent = os.getpid()
    calls = []
    original = par.store.put_unit

    def put(*a, **kw):
        assert os.getpid() == parent
        calls.append(a[1])
        return original(*a, **kw)

    par.store.put_unit = put
    pm, report = par.pull(date.fromisoformat(DAY), par.library())
    assert canonical(serial) == canonical(par)
    assert sm.counts == pm.counts
    assert len(calls) == 4 and report["mapped_units"] == 4
    assert report["peak_large_units"] == 2
    assert report["peak_estimated_reserved_bytes"] <= limits.memory_cap_bytes
    assert all(int(pid) != parent for pid in report["worker_pid_counts"])
    assert par.mapping.stats.as_dict() == serial.mapping.stats.as_dict()
    assert par.mapping.redaction_report() == serial.mapping.redaction_report()


def test_cache_union_new_unit_and_subset_refusal(tmp_path):
    ds = [fixture(tmp_path / "raw", t) for t in ["synthetica", "syntheticb"]]
    cfg = cfg_at(tmp_path / "store")
    work = tmp_path / "cache"
    par = pipeline(cfg, ds, work)
    par.pull(date.fromisoformat(DAY), par.library())
    again = pipeline(cfg, ds, work)
    _, r = again.pull(date.fromisoformat(DAY), again.library())
    assert r["status"] == "sealed_already"
    ds.append(fixture(tmp_path / "raw", "syntheticc"))
    newer = pipeline(cfg, ds, work)
    m, r = newer.pull(date.fromisoformat(DAY), newer.library())
    assert r["cache_reused_units"] == 2 and r["mapped_units"] == 1 and len(m.units) == 3
    subset = pipeline(cfg, ds[-1:], work)
    with pytest.raises(p.PartitionConflict, match="subset"):
        subset.pull(date.fromisoformat(DAY), subset.library())
    assert subset.store.get(DAY).version == m.version


def test_bad_checksum_and_duplicate_do_not_seal(tmp_path):
    for kind in ["checksum", "duplicate"]:
        ds = [fixture(tmp_path / kind / "raw", broken=kind == "duplicate")]
        if kind == "checksum":
            ds[0]["info"]["sha256"] = "0" * 64
        par = pipeline(cfg_at(tmp_path / kind), ds, tmp_path / kind / "cache")
        with pytest.raises(ValueError):
            par.pull(date.fromisoformat(DAY), par.library())
        assert par.store.get(DAY) is None
        assert par.store.latest(DAY).status == "open"


def test_memory_admission_before_partition_begin(tmp_path):
    ds = [fixture(tmp_path / "raw", count=100)]
    limits = p.ImportLimits(process_base_bytes=1, memory_cap_bytes=100, disk_reserve_bytes=0)
    par = pipeline(cfg_at(tmp_path), ds, tmp_path / "cache", limits=limits)
    with pytest.raises(p.ImportAdmissionError, match="memory"):
        par.pull(date.fromisoformat(DAY), par.library())
    assert par.store.latest(DAY) is None


def test_partial_service_completed_indices_and_bad_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(p.ip, "ROOT", tmp_path / "exports")
    service = p.ip.ROOT / "grouped/partitions" / DAY / "admin"
    d = fixture(service / "raw", grouped=True)
    man = {
        "contract": p.ip.gi.CONTRACT,
        "grouped_capture": True,
        "summary": {"sealed": False},
        "indices": {d["index"]: d["info"], "unfinished": {"started": "yes"}},
    }
    (service / "manifest.json").write_text(json.dumps(man))
    selected, skipped = p.select_units([DAY], completed_indices=False)
    assert not selected[DAY]
    selected, skipped = p.select_units([DAY], completed_indices=True)
    assert len(selected[DAY]) == 1 and selected[DAY][0]["service_manifest_sealed"] is False
    assert any(x["reason"] == "index export not complete" for x in skipped)
    par = pipeline(cfg_at(tmp_path), selected[DAY], tmp_path / "cache")
    m, r = par.pull(date.fromisoformat(DAY), par.library())
    assert m.status == "sealed" and r["service_completeness_claim"] is False
    man["indices"][d["index"]]["sha256"] = "0" * 64
    (service / "manifest.json").write_text(json.dumps(man))
    with pytest.raises(ValueError, match="checksum"):
        p.select_units([DAY], completed_indices=True)


def test_source_config_change_invalidates_cache(tmp_path):
    ds = [fixture(tmp_path / "raw")]
    cfg = cfg_at(tmp_path)
    work = tmp_path / "cache"
    first = pipeline(cfg, ds, work)
    first.pull(date.fromisoformat(DAY), first.library())
    changed = cfg.model_copy(deep=True)
    changed.sources[changed.logs.source].multiline.enabled = False
    second = pipeline(changed, ds, work)
    m, r = second.pull(date.fromisoformat(DAY), second.library())
    assert r["mapped_units"] == 1 and r["cache_reused_units"] == 0 and m.version == 2


def test_file_source_transport_forbidden(tmp_path):
    par = pipeline(cfg_at(tmp_path), [], tmp_path / "cache")
    with pytest.raises(p.ImportAdmissionError, match="network"):
        par.source.transport


def test_failure_preserves_prior_sealed_and_resumes_completed_units(tmp_path):
    ds = [fixture(tmp_path / "raw", "synthetica")]
    cfg = cfg_at(tmp_path)
    work = tmp_path / "cache"
    first = pipeline(cfg, ds, work)
    old, _ = first.pull(date.fromisoformat(DAY), first.library())
    second_desc = fixture(tmp_path / "raw", "syntheticb")
    third_desc = fixture(tmp_path / "raw", "syntheticc")
    original = third_desc["path"].read_bytes()
    third_desc["path"].write_bytes(b"corrupt")
    next_ds = ds + [second_desc, third_desc]
    limits = p.ImportLimits(workers=1, disk_reserve_bytes=0)
    failed = pipeline(cfg, next_ds, work, limits=limits)
    with pytest.raises(ValueError):
        failed.pull(date.fromisoformat(DAY), failed.library())
    assert failed.store.get(DAY).version == old.version
    assert len(failed.store.latest(DAY).units) == 2
    third_desc["path"].write_bytes(original)
    resumed = pipeline(cfg, next_ds, work, limits=limits)
    m, r = resumed.pull(date.fromisoformat(DAY), resumed.library())
    assert r["resumed"] == 2 and r["mapped_units"] == 1 and len(m.units) == 3 and m.status == "sealed"


def test_complete_index_wrong_scope_and_count_fail_closed(tmp_path):
    for kind in ["scope", "count"]:
        desc = fixture(tmp_path / kind / "raw", grouped=True)
        if kind == "scope":
            desc["info"]["tenant"] = "syntheticb"
        else:
            desc["info"]["source_total"] += 1
            desc["info"]["raw_occurrences"] += 1
        par = pipeline(cfg_at(tmp_path / kind), [desc], tmp_path / kind / "cache")
        with pytest.raises(ValueError):
            par.pull(date.fromisoformat(DAY), par.library())
        assert par.store.get(DAY) is None


def test_cache_cap_refuses_without_evicting(tmp_path):
    desc = fixture(tmp_path / "raw")
    limits = p.ImportLimits(cache_cap_bytes=1, disk_reserve_bytes=0)
    par = pipeline(cfg_at(tmp_path), [desc], tmp_path / "cache", limits=limits)
    with pytest.raises(p.ImportAdmissionError, match="cap"):
        par.pull(date.fromisoformat(DAY), par.library())
    assert par.store.get(DAY) is None


def deployment_fixture(path, grouped):
    """Exporter retains the suffix while the package parser extracts deployment_id."""
    desc = fixture(path, grouped=grouped)
    index = desc["index"].replace("-admin-", "-admin-1234567890-")
    rows = [
        json.loads(line)
        for line in zstandard.ZstdDecompressor().decompress(desc["path"].read_bytes()).splitlines()
    ]
    for row in rows:
        row["index"] = index
    desc["path"].write_bytes(
        zstandard.ZstdCompressor().compress("".join(json.dumps(row) + "\n" for row in rows).encode())
    )
    desc["index"] = index
    desc["info"]["service"] = "admin-1234567890"
    desc["info"]["sha256"] = p.ip.sha256(desc["path"])
    return desc


@pytest.mark.parametrize("grouped", [False, True], ids=["full", "grouped"])
def test_numeric_deployment_suffix_preserves_canonical_attributes(tmp_path, grouped):
    desc = deployment_fixture(tmp_path / "raw", grouped)
    cfg = cfg_at(tmp_path)
    cfg.sources[cfg.logs.source].index_name.service_suffix = "numeric"
    pipe = pipeline(cfg, [desc], tmp_path / "cache")
    manifest, report = pipe.pull(date.fromisoformat(DAY), pipe.library())
    assert manifest.status == "sealed" and report["mapped_units"] == 1
    (artifact,) = list(pipe.store.units(DAY))
    assert artifact.unit.attributes["service"] == "admin"
    assert artifact.unit.attributes["deployment_id"] == "1234567890"
    assert artifact.unit.attributes["tenant"] == "synthetica"
    assert artifact.unit.attributes["index_date"] == DAY


@pytest.mark.parametrize("grouped", [False, True], ids=["full", "grouped"])
@pytest.mark.parametrize("corruption", ["tenant", "env", "service", "date", "partition_day", "index_date"])
def test_numeric_deployment_export_metadata_mismatch_refused_before_begin(tmp_path, grouped, corruption):
    desc = deployment_fixture(tmp_path / "raw", grouped)
    if corruption == "partition_day":
        desc["day"] = "2026-11-03"
    elif corruption == "index_date":
        desc["index"] = desc["index"].replace("2026.11.02", "2026.11.03")
    else:
        desc["info"][corruption] = {
            "tenant": "syntheticb",
            "env": "staging",
            "service": "other-service-1234567890",
            "date": "2026-11-03",
        }[corruption]
    cfg = cfg_at(tmp_path)
    cfg.sources[cfg.logs.source].index_name.service_suffix = "numeric"
    pipe = pipeline(cfg, [desc], tmp_path / "cache")
    with pytest.raises(ValueError, match="dimensions|date"):
        pipe.pull(date.fromisoformat(DAY), pipe.library())
    assert pipe.store.latest(DAY) is None
