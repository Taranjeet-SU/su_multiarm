"""``run_daily`` end to end on the fake cluster (addendum A1 §3.5, §3.7, §3.8.1; slice L3a): it refuses
an unredacted mapping, is idempotent and resumable, composes under the write lease it takes itself,
holds only the schedule lock, reparses on a library change, and settles grown units."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from su_multi_arm.core.errors import WriteConflict
from su_multi_arm.core.types import Page
from su_multi_arm.ingestion.release import generation_id
from su_multi_arm.ingestion.schedule import RedactionRequired
from su_multi_arm.sources.errors import SourceUnavailable
from tests.logs_support import ENGINE, Clock, cluster_for, pipeline, write_library
from tests.pattern_fixtures import engine_or_skip

DAY = date(2026, 1, 5)


@pytest.fixture(autouse=True)
def _engine():
    engine_or_skip(ENGINE)


def run(tmp, cluster=None, **kwargs):
    cluster = cluster or cluster_for([DAY], lines=20)
    clock = kwargs.pop("now", None) or Clock(DAY + timedelta(days=1))
    pipe, cfg, backend = pipeline(tmp, cluster, now=clock, **kwargs)
    return pipe, cfg, backend, cluster


def docs_of(backend, generation):
    manifest = backend.generation_registry().manifest(generation)
    return backend.catalog.iter_scope(generation_id(manifest), None, Page(size=1000)).items


def test_refuses_a_mapping_without_a_redactor_before_any_request(tmp_path):
    pipe, _, backend, cluster = run(tmp_path, redactor=None)
    with pytest.raises(RedactionRequired):
        pipe.run_daily(DAY)
    assert cluster.requests == []
    assert pipe.store.keys() == []
    assert backend.generation_registry().current() is None
    report = json.loads((pipe.reports_dir() / f"run-{DAY.isoformat()}.json").read_text())
    assert not report["ok"] and report["steps"]["retention"]["status"] == "skipped"


def test_partition_holds_only_redacted_text(tmp_path):
    pipe, _, backend, cluster = run(tmp_path)
    pipe.run_daily(DAY)
    raw = "".join(json.dumps(a.model_dump(mode="json")) for a in pipe.store.units(DAY.isoformat()))
    assert "@example.invalid" not in raw and "192.0.2." not in raw
    assert "<email>" in raw or "<ipv4>" in raw
    assert pipe.store.get(DAY.isoformat()).redaction["mode"] == "injected"


def test_run_daily_builds_documents_with_headers_and_counts(tmp_path):
    pipe, cfg, backend, cluster = run(tmp_path)
    report = pipe.run_daily(DAY)
    assert report.ok and report.steps["compose"]["status"] == "built"
    docs = docs_of(backend, report.steps["compose"]["generation"])
    kinds = {d.kind for d in docs}
    assert {"unit_day", "signature_day", "service_day", "signature_service_day", "signature_window", "signature_tenant_window", "signature"} <= kinds
    assert all(d.fields["header"].startswith(f"kind: {d.kind}") for d in docs)
    unit_days = [d for d in docs if d.kind == "unit_day"]
    truth = sum(len(v) for v in cluster.indices.values())
    assert sum(d.metadata["lines_total"] for d in unit_days) == truth  # exact source counts
    assert all(d.metadata["row_exactness"] == "exact" and d.metadata["date_basis"] == "index_date" for d in unit_days)
    assert all("(index_date, UTC)" in d.fields["header"] for d in unit_days)
    signature = next(d for d in docs if d.kind == "signature" and d.metadata["signature_id"] == "dependency.connect_failed")
    exemplars = signature.fields["exemplars"].split("\n\n")
    assert 1 <= len(exemplars) <= 3
    gen = generation_id(backend.generation_registry().manifest(report.steps["compose"]["generation"]))
    chunks = [c for c in pipe.write_path._chunk(signature) if c.field == "exemplars"]
    assert [c.text for c in chunks] == exemplars and {c.recipe for c in chunks} == {"line"}  # one chunk per record
    assert [c.id for c in backend.catalog.get_chunks(gen, [c.id for c in chunks])] == [c.id for c in chunks]
    manifest = backend.generation_registry().manifest(report.steps["compose"]["generation"])
    assert manifest["logs"]["partitions"] == [{"key": DAY.isoformat(), "version": 1, "library_identity": manifest["pattern_provenance"]["library_identity"]}]
    assert manifest["ledger_refs_count"] == len(backend.ledger.refs(manifest["generation"]))
    # projections: aggregates are catalog/records only; the signature's exemplars are never embedded
    lexical_kinds = {"signature", "signature_window"}
    assert manifest["projections"]["lexical"]["rows"] == sum(len(pipe.write_path._chunk(d)) for d in docs if d.kind in lexical_kinds)
    assert manifest["projections"]["vector"]["rows"] == sum(1 for d in docs if d.kind == "signature")
    assert manifest["projections"]["catalog"]["documents"] == len(docs)


def test_signature_counts_match_the_parsed_lines(tmp_path):
    pipe, cfg, backend, cluster = run(tmp_path)
    report = pipe.run_daily(DAY)
    matches = pipe.store.read(DAY.isoformat(), "matches.jsonl.gz")
    expected: dict[str, int] = {}
    for match in matches:
        if match["signature"]:
            expected[match["signature"]] = expected.get(match["signature"], 0) + 1
    docs = docs_of(backend, report.steps["compose"]["generation"])
    windows = {d.metadata["signature_id"]: d.metadata["lines_window"] for d in docs if d.kind == "signature_window"}
    assert windows == expected and expected


def test_second_run_is_idempotent(tmp_path):
    pipe, cfg, backend, cluster = run(tmp_path)
    first = pipe.run_daily(DAY)
    searches = sum(1 for r in cluster.requests if r.path.endswith("/_search"))
    second = pipe.run_daily(DAY)
    assert second.steps["partition"]["status"] == "sealed_already"
    assert second.steps["compose"]["status"] == "unchanged"
    assert second.steps["compose"]["generation"] == first.steps["compose"]["generation"]
    assert sum(1 for r in cluster.requests if r.path.endswith("/_search")) == searches


class FlakySource:
    """Wraps the real source; the n-th unit pull raises (a crash in the middle of the day)."""

    def __init__(self, inner, fail_at: int):
        self.inner, self.fail_at, self.pulls = inner, fail_at, 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def pull(self, *args, **kwargs):
        self.pulls += 1
        if self.pulls == self.fail_at:
            raise SourceUnavailable("simulated outage", "pull")
        return self.inner.pull(*args, **kwargs)


def test_crash_resume_without_duplicates(tmp_path):
    clean_dir, crash_dir = tmp_path / "clean", tmp_path / "crash"
    cluster = cluster_for([DAY], lines=20)
    clean, _, clean_backend, _ = run(clean_dir, cluster)
    clean_report = clean.run_daily(DAY)
    pipe, cfg, backend, _ = run(crash_dir, cluster)
    pipe.source = FlakySource(pipe.source, fail_at=3)
    with pytest.raises(SourceUnavailable):
        pipe.run_daily(DAY)
    open_manifest = pipe.store.latest(DAY.isoformat())
    assert open_manifest.status == "open" and len(open_manifest.units) == 2
    assert backend.generation_registry().current() is None  # nothing composed, nothing expired
    pipe.source = pipe.source.inner
    report = pipe.run_daily(DAY)
    assert report.steps["partition"]["resumed"] == 2 and report.steps["partition"]["pulled"] == 4
    ids = [r.id for a in pipe.store.units(DAY.isoformat()) for r in a.records]
    assert len(ids) == len(set(ids))
    assert pipe.store.get(DAY.isoformat()).counts == clean.store.get(DAY.isoformat()).counts
    a = sorted((d.id, d.metadata.get("lines"), d.metadata.get("lines_total")) for d in docs_of(backend, report.steps["compose"]["generation"]))
    b = sorted((d.id, d.metadata.get("lines"), d.metadata.get("lines_total")) for d in docs_of(clean_backend, clean_report.steps["compose"]["generation"]))
    assert a == b


def test_resume_refuses_another_source_query(tmp_path):
    from su_multi_arm.ingestion.partitions import PartitionConflict

    pipe, cfg, backend, cluster = run(tmp_path)
    pipe.store.begin(DAY.isoformat(), "other-query-sha", {})
    with pytest.raises(PartitionConflict):
        pipe.run_daily(DAY)


def test_schedule_lock_excludes_a_second_run_but_not_compose(tmp_path):
    pipe, cfg, backend, cluster = run(tmp_path)
    other, _, _ = pipeline(tmp_path, cluster, now=pipe.now)
    with pipe.store.schedule_lock():
        with pytest.raises(WriteConflict) as caught:
            other.run_daily(DAY)
        assert caught.value.reason == "held"
        # run-daily's own compose takes the registry write lease while the schedule lock is held
        library = pipe.library()
        pipe.pull(DAY, library)
        result = pipe.compose(DAY, library)
    assert result.status == "built"


def test_salted_extraction_keys_follow_their_generation(tmp_path):
    """L2 salts the pattern extractor's reuse keys with the library; after a library change the
    old generation's salted keys stay referenced only by it and go with it (``ledger_refs``)."""
    pipe, cfg, backend, cluster = run(tmp_path, retention={"keep_generations": 1})
    first = pipe.run_daily(DAY)
    old_keys = {k for k in backend.ledger.refs(first.steps["compose"]["generation"]) if k.startswith("extraction:")}
    assert old_keys and all(len(k.split(":")) == 4 for k in old_keys)  # extraction:<recipe>:<salt>:<chunk>
    extra = {"id": "msg.work.indexed", "kind": "message", "status": "approved", "regex": r"^indexed (?P<n>[0-9]{1,6}) documents$", "signature": {"id": "work.indexed"}, "examples": {"positive": ["indexed 12 documents"]}, "provenance": {"author": "user"}}
    write_library(tmp_path / "patterns", [extra])
    pipe2, _, backend2 = pipeline(tmp_path, cluster, now=pipe.now, retention={"keep_generations": 1})
    second = pipe2.run_daily(DAY)
    new_keys = {k for k in backend2.ledger.refs(second.steps["compose"]["generation"]) if k.startswith("extraction:")}
    assert {k.split(":")[2] for k in new_keys}.isdisjoint({k.split(":")[2] for k in old_keys})  # new salt
    assert first.steps["compose"]["generation"] not in backend2.generation_registry().generations()
    for key in old_keys - new_keys:
        assert backend2.ledger.get_raw(key) is None
    assert all(backend2.ledger.get_raw(k) is not None for k in new_keys)
    assert second.steps["verify"]["ok"], second.steps["verify"]["problems"]


def test_library_change_reparses_before_compose(tmp_path):
    pipe, cfg, backend, cluster = run(tmp_path)
    first = pipe.run_daily(DAY)
    extra = {
        "id": "msg.work.indexed",
        "kind": "message",
        "status": "approved",
        "priority": 20,
        "regex": r"^indexed (?P<n>[0-9]{1,6}) documents$",
        "signature": {"id": "work.indexed", "category": "throughput"},
        "examples": {"positive": ["indexed 12 documents"]},
        "provenance": {"author": "user"},
    }
    write_library(tmp_path / "patterns", [extra])
    pipe2, _, backend2 = pipeline(tmp_path, cluster, now=pipe.now)
    second = pipe2.run_daily(DAY)
    assert second.steps["compose"]["reparsed"] == [DAY.isoformat()]
    assert pipe2.store.get(DAY.isoformat()).version == 2
    assert second.steps["compose"]["generation"] != first.steps["compose"]["generation"]
    sigs = {d.metadata.get("signature_id") for d in docs_of(backend2, second.steps["compose"]["generation"]) if d.kind == "signature_window"}
    assert "work.indexed" in sigs
    manifest = backend2.generation_registry().manifest(second.steps["compose"]["generation"])
    assert manifest["logs"]["library_identity"] == manifest["pattern_provenance"]["library_identity"]
    assert {p["library_identity"] for p in manifest["logs"]["partitions"]} == {manifest["logs"]["library_identity"]}


def test_parse_pool_matches_in_process_parse(tmp_path):
    pipe, cfg, backend, cluster = run(tmp_path / "one")
    pipe.run_daily(DAY)
    pooled, _, _, _ = run(tmp_path / "pool", cluster)
    pooled.logs.parse.workers = 2
    pooled.logs.parse.batch_size = 5  # every unit has more records than one batch: the pool runs
    pooled.run_daily(DAY)
    assert pooled.store.read(DAY.isoformat(), "matches.jsonl.gz") == pipe.store.read(DAY.isoformat(), "matches.jsonl.gz")
    assert pooled.store.read(DAY.isoformat(), "aggregates.json") == pipe.store.read(DAY.isoformat(), "aggregates.json")


def test_settle_repulls_grown_units(tmp_path):
    pipe, cfg, backend, cluster = run(tmp_path)
    pipe.run_daily(DAY)
    assert pipe.settle(DAY) == {"status": "settled", "repulled": []}
    index = sorted(cluster.indices)[0]
    template = cluster.indices[index][-1]
    for i in range(10):  # late-arriving lines in one index
        doc = json.loads(json.dumps(template))
        doc["_id"] = f"{index}-late-{i}"
        cluster.indices[index].append(doc)
    result = pipe.settle(DAY)
    assert result["status"] == "repulled" and len(result["repulled"]) == 1 and result["version"] == 2
    manifest = pipe.store.get(DAY.isoformat())
    assert manifest.reason == "repull" and manifest.counts["records"] > 0
    unit = result["repulled"][0]
    assert manifest.unit_counts[unit]["total"] == len(cluster.indices[index])


def test_manifest_extra_cannot_override_and_failed_builds_drop_their_refs(tmp_path):
    from su_multi_arm.core.types import Document as Doc
    from su_multi_arm.ingestion.write import IngestError

    pipe, cfg, backend, cluster = run(tmp_path)
    document = Doc(id="d1", dataset="logs", kind="note", title="t", fields={"body": "db-client refused by kafka"})
    with pytest.raises(IngestError) as caught:
        pipe.write_path.ingest([document], replace=True, manifest_extra={"documents": 99})
    failed = caught.value.failure["generation"]
    assert backend.ledger.refs(failed) == [] and backend.ledger.referencing_generations() == []
    manifest = pipe.write_path.ingest([document], replace=True, manifest_extra={"logs_note": {"k": 1}})
    assert manifest["logs_note"] == {"k": 1} and backend.ledger.refs(manifest["generation"])
    second = pipe.write_path.ingest([document], replace=True)
    pipe.write_path.delete_generation(manifest["generation"])
    assert backend.ledger.referencing_generations() == [second["generation"]]
