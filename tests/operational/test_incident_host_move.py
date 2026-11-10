"""Optional private host extraction worker; synthetic fixtures only."""
import gzip
import importlib.util
import json
import os
from pathlib import Path

import pytest

location = os.environ.get("SU_INCIDENT_HOST_WORKER")
if not location or not Path(location).is_file():
    pytest.skip("optional private host worker not supplied", allow_module_level=True)
spec = importlib.util.spec_from_file_location("synthetic_host_worker", location)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)

from tests.operational import test_incident_import as helper  # noqa: E402
from su_multi_arm.extraction.patterns.extractor import PatternExtractor  # noqa: E402
from su_multi_arm.ingestion.logs_parse import parse_units  # noqa: E402
from su_multi_arm.ingestion.partitions import FilePartitionStore, UnitArtifacts  # noqa: E402


def summary(parsed):
    value = {k: getattr(parsed, k) for k in (
        "aggregates", "hourly_cells", "exemplars", "unparsed", "counts", "format", "patterns", "parse_work"
    )}
    value["accounting"] = [a.model_dump(mode="json") for a in parsed.accounting]
    return value


def inputs(tmp_path, monkeypatch):
    cfg = helper.incident_config(tmp_path)
    library = PatternExtractor(cfg.extraction.pattern_library, None).load()
    arts = []
    for i in range(3):
        scope = {**helper.SCOPE, "tenant": f"synthetic{i}",
                 "index": f"su-synthetic{i}-production-admin-2026.01.01"}
        monkeypatch.setattr(helper, "SCOPE", scope)
        seed, context = helper.fixtures(tmp_path / str(i))
        arts.append(helper.m.to_unit_artifacts(helper.m.load_bundle(seed, [context]), cfg))
    return cfg, library, sorted(arts, key=lambda a: a.unit.id)


def entries_for(tmp_path, arts, library):
    entries = []
    for art in arts:
        target = tmp_path / art.unit.id
        target.mkdir()
        parsed = parse_units([art], library)
        worker.write_unit(target / "unit.json.gz", art)
        worker.write_rows(target / "matches.jsonl.gz", parsed.matches)
        worker.adapter.atomic_json(target / "summary.json", summary(parsed))
        meta = {"recipe": art.unit.id, "unit": art.unit.model_dump(mode="json"),
                "count": art.count.model_dump(mode="json"), "capped": art.capped,
                "files": {n: worker.adapter.sha(target / n) for n in (
                    "unit.json.gz", "matches.jsonl.gz", "summary.json")}}
        worker.adapter.atomic_json(target / "complete.json", meta)
        entries.append((target, meta))
    return entries


def test_split_indices_preserve_every_match_and_aggregate(tmp_path, monkeypatch):
    cfg, library, arts = inputs(tmp_path, monkeypatch)
    expected = parse_units(arts, library)
    entries = entries_for(tmp_path, arts, library)
    actual = worker.combine(json.loads((p / "summary.json").read_text()) for p, _ in entries)
    assert actual.aggregates == expected.aggregates
    assert actual.hourly_cells == expected.hourly_cells
    assert actual.exemplars == expected.exemplars
    assert actual.unparsed == expected.unparsed
    assert actual.counts == expected.counts
    assert actual.accounting == expected.accounting
    assert list(worker.row_iter([p / "matches.jsonl.gz" for p, _ in entries])) == expected.matches
    # Each unit contains a seed ERROR and a context ERROR; context does not inflate events.
    assert sum(r["lines"] for r in actual.aggregates["levels"]) == 3
    assert actual.counts["records"] == 6


def test_stream_unit_round_trip_and_core_seal(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    cfg, library, arts = inputs(tmp_path, monkeypatch)
    entries = entries_for(tmp_path, arts, library)
    for (target, _), art in zip(entries, arts):
        with gzip.open(target / "unit.json.gz", "rt") as stream:
            assert UnitArtifacts.model_validate_json(stream.read()) == art
    report = worker.seal_day(cfg, "2026-01-01", entries, library)
    assert report["status"] == "sealed"
    store = FilePartitionStore(cfg.logs.partitions.path, cfg.logs.source)
    assert list(store.units("2026-01-01")) == arts
    expected = parse_units(arts, library)
    assert store.read("2026-01-01", "matches.jsonl.gz") == expected.matches
    assert store.read("2026-01-01", "aggregates.json") == expected.aggregates
    assert store.get("2026-01-01").counts == expected.counts
    assert worker.seal_day(cfg, "2026-01-01", entries, library)["status"] == "unchanged"


@pytest.mark.parametrize("name", ["unit.json.gz", "matches.jsonl.gz", "summary.json"])
def test_cache_corruption_refused(tmp_path, monkeypatch, name):
    _, library, arts = inputs(tmp_path, monkeypatch)
    target, meta = entries_for(tmp_path, arts[:1], library)[0]
    assert worker.check_cache(target, meta["recipe"]) == meta
    (target / name).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="artifact mismatch"):
        worker.check_cache(target, meta["recipe"])


def test_duplicate_unit_refused(tmp_path, monkeypatch):
    _, library, arts = inputs(tmp_path, monkeypatch)
    parsed = summary(parse_units(arts[:1], library))
    with pytest.raises(ValueError, match="duplicate parsed unit"):
        worker.combine([parsed, parsed])


def test_partial_cache_is_not_complete(tmp_path):
    (tmp_path / "matches.jsonl.gz").write_bytes(b"partial")
    assert worker.check_cache(tmp_path, "synthetic") is None


@pytest.mark.parametrize("corrupt", [False, True])
def test_handoff_verifies_files_before_partition_visibility(tmp_path, monkeypatch, corrupt):
    import shutil

    location = os.environ.get("SU_INCIDENT_HANDOFF_WORKER")
    if not location:
        pytest.skip("optional handoff helper not supplied")
    spec = importlib.util.spec_from_file_location("synthetic_handoff", location)
    handoff = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(handoff)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    cfg, library, arts = inputs(tmp_path, monkeypatch)
    entries = entries_for(tmp_path, arts, library)
    worker.seal_day(cfg, "2026-01-01", entries, library)
    origin = FilePartitionStore(cfg.logs.partitions.path, cfg.logs.source)
    manifest = origin.get("2026-01-01")
    incoming = tmp_path / "deployment/data/pkg/incidents/incoming/synthetic"
    shutil.copytree(origin._vdir(manifest.key, manifest.version), incoming / manifest.key / "v0001")
    receiver = cfg.model_copy(deep=True)
    receiver.logs.partitions.path = tmp_path / "receiver"
    monkeypatch.setattr(handoff.Path, "home", classmethod(lambda cls: tmp_path))
    if corrupt:
        (incoming / manifest.key / "v0001/aggregates.json").write_text("corrupt")
        with pytest.raises(ValueError, match="SHA mismatch"):
            handoff.accept(receiver, incoming)
        assert FilePartitionStore(receiver.logs.partitions.path, cfg.logs.source).get(manifest.key) is None
    else:
        assert handoff.accept(receiver, incoming)[0]["status"] == "accepted"
        assert handoff.accept(receiver, incoming)[0]["status"] == "unchanged"
        target = FilePartitionStore(receiver.logs.partitions.path, cfg.logs.source)
        assert target.get(manifest.key) == manifest
        assert target.read(manifest.key, "matches.jsonl.gz") == origin.read(manifest.key, "matches.jsonl.gz")
