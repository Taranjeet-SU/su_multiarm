"""The day-partition store (addendum A1 §3.5): begin/resume with the source query sha, atomic units,
immutable sealed versions, ``reparse``/``repull`` as ``v+1``, ``expire`` to a content-free tombstone,
and the schedule lock."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from su_multi_arm.core.errors import WriteConflict
from su_multi_arm.ingestion.logs_parse import ParsedPartition
from su_multi_arm.ingestion.partitions import FilePartitionStore, PartitionConflict, PartitionMissing, UnitArtifacts, line_hash, source_id_hash
from su_multi_arm.sources.contracts import SamplePlan, SourceUnit, UnitCount
from su_multi_arm.sources.logs import LogRecord

KEY = "2026-01-05"
TS = datetime(2026, 1, 5, 10, tzinfo=timezone.utc)


def unit(n: int, lines: list[str]) -> UnitArtifacts:
    uid = f"{n:032x}"
    records = [
        LogRecord(id=f"{n:04d}{i:028x}", ts=TS, partition_key=KEY, attributes={"service": "query-gateway"}, message=text, source_id=f"doc-{n}-{i}", index="t001-prod-query-gateway-2026.01.05")
        for i, text in enumerate(lines)
    ]
    return UnitArtifacts(
        unit=SourceUnit(id=uid, index="t001-prod-query-gateway-2026.01.05", query_sha="q"),
        count=UnitCount(unit_id=uid, query_sha="q", total=len(lines), at=TS),
        sample=SamplePlan(method="none", cap=100, unit_total=len(lines)),
        records=records,
    )


def parsed(exemplar: str = "connect failed line") -> ParsedPartition:
    return ParsedPartition(
        matches=[{"id": "r1", "status": "parsed"}],
        aggregates={"units": [], "signatures": []},
        hourly_cells=[],
        exemplars={"sig": [{"id": "r1", "text": exemplar}]},
        unparsed=[{"id": "r2", "status": "unparsed", "text": "odd line"}],
        counts={"records": 2},
    )


@pytest.fixture
def store(tmp_path):
    return FilePartitionStore(tmp_path / "parts", "synthetic")


def test_begin_resume_and_query_conflict(store):
    first = store.begin(KEY, "sha-a", {"library_identity": "L1"})
    assert first.version == 1 and first.status == "open"
    store.put_unit(KEY, f"{1:032x}", unit(1, ["a"]))
    again = store.begin(KEY, "sha-a", {"library_identity": "L1"})
    assert again.version == 1 and again.units == [f"{1:032x}"]  # resumed, unit kept
    with pytest.raises(PartitionConflict):
        store.begin(KEY, "sha-b", {})


def test_put_unit_is_atomic_and_idempotent(store):
    store.begin(KEY, "sha", {})
    artifacts = unit(1, ["a", "b"])
    store.put_unit(KEY, artifacts.unit.id, artifacts)
    store.put_unit(KEY, artifacts.unit.id, artifacts)
    manifest = store.latest(KEY)
    assert manifest.units == [artifacts.unit.id] and manifest.unit_counts[artifacts.unit.id]["total"] == 2
    assert not list((store.root / "synthetic" / KEY).rglob("*.tmp"))
    with pytest.raises(ValueError):
        store.put_unit(KEY, f"{9:032x}", artifacts)  # id mismatch


def test_sealed_versions_are_immutable(store):
    store.begin(KEY, "sha", {"library_identity": "L1"})
    store.put_unit(KEY, f"{1:032x}", unit(1, ["a"]))
    sealed = store.seal(KEY, parsed(), provenance={"library_identity": "L1"})
    assert sealed.status == "sealed" and set(sealed.artifacts) >= {"matches.jsonl.gz", "aggregates.json", "hourly_cells.json", "exemplars.json", "unparsed.jsonl.gz", f"units/{1:032x}.json.gz"}
    with pytest.raises(PartitionConflict):
        store.put_unit(KEY, f"{2:032x}", unit(2, ["b"]))
    with pytest.raises(PartitionConflict):
        store.seal(KEY, parsed())
    assert store.get(KEY).version == 1 and store.read(KEY, "exemplars.json") == {"sig": [{"id": "r1", "text": "connect failed line"}]}
    # the next pull of the same day opens v2 (e.g. after a source query change)
    assert store.begin(KEY, "sha-new", {}).version == 2 and store.get(KEY).version == 1


class FakeLibrary:
    def __init__(self, identity):
        self.identity = identity

    def provenance(self):
        return {"library_identity": self.identity}


def test_reparse_writes_v_plus_one_from_retained_records(store):
    store.begin(KEY, "sha", {"library_identity": "L1"})
    store.put_unit(KEY, f"{1:032x}", unit(1, ["a", "b"]))
    store.seal(KEY, parsed(), provenance={"library_identity": "L1"})
    seen = []

    def parse(units, library):
        seen.append(([r.message for u in units for r in u.records], library.identity))
        return parsed("reparsed")

    child = store.reparse(KEY, FakeLibrary("L2"), parse)
    assert child.version == 2 and child.reason == "reparse" and child.parent_version == 1 and child.library_identity == "L2"
    assert seen == [(["a", "b"], "L2")]
    assert store.read(KEY, "exemplars.json")["sig"][0]["text"] == "reparsed"
    assert store.read(KEY, "exemplars.json", version=1)["sig"][0]["text"] == "connect failed line"


def test_repull_keeps_other_units_and_opens_v_plus_one(store):
    store.begin(KEY, "sha", {})
    for n in (1, 2):
        store.put_unit(KEY, f"{n:032x}", unit(n, [f"line {n}"]))
    store.seal(KEY, parsed())
    child = store.repull(KEY, [f"{2:032x}"])
    assert child.status == "open" and child.units == [f"{1:032x}"] and child.reason == "repull"
    assert store.get(KEY).version == 1  # compose keeps reading v1 until v2 is sealed
    with pytest.raises(ValueError):
        store.repull(KEY, ["f" * 32])


def test_expire_leaves_a_content_free_tombstone(store):
    store.begin(KEY, "sha", {})
    store.put_unit(KEY, f"{1:032x}", unit(1, ["secret-free line one", "shared line"]))
    store.seal(KEY, parsed("connect failed line"))
    tombstone = store.expire(KEY, sample_size=10, keep_hashes={line_hash("shared line")})
    assert store.keys() == [] and store.get(KEY) is None
    assert line_hash("secret-free line one") in tombstone.verify_sample and line_hash("connect failed line") in tombstone.verify_sample
    assert line_hash("shared line") not in tombstone.verify_sample  # also in a live day: not expired content
    assert source_id_hash("doc-1-0") in tombstone.verify_source_ids
    text = (store.root / "synthetic" / KEY / "tombstone.json").read_text()
    assert "line one" not in text and "doc-1-0" not in text
    assert [t.key for t in store.tombstones()] == [KEY]
    assert json.loads(text)["deleted"]["versions"] == 1
    with pytest.raises(PartitionMissing):
        store.read(KEY, "aggregates.json")


def test_schedule_lock_is_exclusive_across_store_instances(store, tmp_path):
    other = FilePartitionStore(tmp_path / "parts", "synthetic")
    with store.schedule_lock():
        with pytest.raises(WriteConflict) as caught:
            with other.schedule_lock():
                pass
        assert caught.value.reason == "held"
    with other.schedule_lock():
        pass


def test_keys_are_validated(store):
    with pytest.raises(ValueError):
        store.begin("2026-1-5", "sha", {})
    with pytest.raises(ValueError):
        FilePartitionStore(store.root, "../escape")
