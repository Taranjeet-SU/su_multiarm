"""Unified merge M2a, P4: the sealed, sha-verified handoff from the log host. The producer seals
slices; the serving host transfers them (resumable) and registers them only after every byte and
the slice lists verify against its own ledger; any failure keeps the previous slices."""

from __future__ import annotations

import json
import shutil

import pytest

from su_multi_arm.ingestion.handoff import PARTIAL_SUFFIX, HandoffRefused, seal_unit, transfer, verify_unit
from su_multi_arm.ingestion.log_slices import SliceRefused
from tests.log_slices_support import DAY, at, doc, make_ledger, served_documents, view_of


def producer_and_replica(tmp_path):
    producer = make_ledger(tmp_path / "loghost" / "ledger")
    replica = make_ledger(tmp_path / "serve" / "ledger")
    return producer, replica


def test_seal_and_verify(tmp_path):
    unit = tmp_path / "u"
    (unit / "sub").mkdir(parents=True)
    (unit / "a.bin").write_bytes(b"abc")
    (unit / "sub" / "b.txt").write_text("x")
    seal = seal_unit(unit, "slice", {"name": "u"})
    body = verify_unit(unit, expected_seal=seal)
    assert body["files"].keys() == {"a.bin", "sub/b.txt"}
    (unit / "a.bin").write_bytes(b"abd")
    with pytest.raises(HandoffRefused, match="a.bin"):
        verify_unit(unit)
    (unit / "a.bin").write_bytes(b"abc")
    (unit / "extra").write_text("?")
    with pytest.raises(HandoffRefused, match="extra"):
        verify_unit(unit)
    (unit / "extra").unlink()
    (unit / "sub" / "b.txt").unlink()
    with pytest.raises(HandoffRefused, match="missing"):
        verify_unit(unit)
    (unit / "sub" / "b.txt").write_text("x")
    body = json.loads((unit / "unit.json").read_text())
    body["ref"]["name"] = "v"
    (unit / "unit.json").write_text(json.dumps(body))
    with pytest.raises(HandoffRefused, match="SEAL"):
        verify_unit(unit)
    with pytest.raises(HandoffRefused, match="expected"):
        seal_unit(unit, "slice", {"name": "u"})
        verify_unit(unit, expected_seal="0" * 64)


def test_replica_receives_the_producers_slices_in_order(tmp_path):
    producer, replica = producer_and_replica(tmp_path)
    incoming = tmp_path / "serve" / "incoming"
    refs = [
        producer.seal(DAY, "five_minute", [doc("logs:incident:a", "alpha"), doc("logs:incident:b", "beta")], as_of=at(DAY, 0, 5)),
        producer.seal(DAY, "five_minute", [doc("logs:incident:a", "alpha 2")], as_of=at(DAY, 0, 10)),
        producer.seal(DAY, "five_minute", [doc("logs:incident:a", "alpha 2")], full_day=True, as_of=at(DAY, 0, 15)),
    ]
    refs.append(producer.compact(DAY, "hourly", hour=0))
    for ref in refs:
        received = replica.receive(transfer(producer.unit_dir(ref.slice_id), incoming))
        assert received.sha256 == ref.sha256
    assert replica.vector() == producer.vector()
    assert served_documents(replica, view_of(replica)) == served_documents(producer, view_of(producer))
    assert replica.live_rows(DAY) == producer.live_rows(DAY)


def test_out_of_order_or_inconsistent_slices_are_refused(tmp_path):
    producer, replica = producer_and_replica(tmp_path)
    incoming = tmp_path / "serve" / "incoming"
    producer.seal(DAY, "five_minute", [doc("logs:incident:a", "alpha")], as_of=at(DAY, 0, 5))
    second = producer.seal(DAY, "five_minute", [doc("logs:incident:a", "alpha 2")], as_of=at(DAY, 0, 10))
    with pytest.raises(SliceRefused, match="sequence"):
        replica.receive(transfer(producer.unit_dir(second.slice_id), incoming))
    assert replica.vector() == {}
    # a replica whose own state disagrees (it holds no 'a') refuses the supersede list
    own = replica.seal(DAY, "five_minute", [doc("logs:incident:z", "zzz")], as_of=at(DAY, 0, 5))
    with pytest.raises(SliceRefused, match="supersedes"):
        replica.receive(transfer(producer.unit_dir(second.slice_id), incoming))
    assert replica.vector() == {DAY: (own.slice_id,)}


def test_tampered_transfer_is_refused_and_keeps_the_previous_slices(tmp_path):
    producer, replica = producer_and_replica(tmp_path)
    incoming = tmp_path / "serve" / "incoming"
    first = producer.seal(DAY, "five_minute", [doc("logs:incident:a", "alpha")], as_of=at(DAY, 0, 5))
    replica.receive(transfer(producer.unit_dir(first.slice_id), incoming))
    second = producer.seal(DAY, "five_minute", [doc("logs:incident:b", "beta")], as_of=at(DAY, 0, 10))
    copied = transfer(producer.unit_dir(second.slice_id), incoming)
    (copied / "rows.json").write_text((copied / "rows.json").read_text().replace("logs:incident:b", "logs:incident:c"))
    before = replica.current_path.read_text()
    with pytest.raises(SliceRefused, match="verification"):
        replica.receive(copied)
    assert replica.current_path.read_text() == before
    assert not replica.unit_dir(second.slice_id).exists()


def test_transfer_resumes_and_refuses_a_changed_source(tmp_path):
    producer = make_ledger(tmp_path / "loghost" / "ledger")
    ref = producer.seal(DAY, "five_minute", [doc("logs:incident:a", "alpha"), doc("logs:incident:b", "beta")], as_of=at(DAY, 0, 5))
    source = producer.unit_dir(ref.slice_id)
    incoming = tmp_path / "incoming"
    partial = incoming / (source.name + PARTIAL_SUFFIX)
    (partial / "lexical").mkdir(parents=True)
    shutil.copyfile(source / "documents.jsonl.gz", partial / "documents.jsonl.gz")  # interrupted after one file
    (partial / "rows.json").write_text("half-written")  # and one corrupt file
    copied: list[str] = []
    real_copy = shutil.copyfile

    def spy(src, dst, *a, **k):
        copied.append(str(src))
        return real_copy(src, dst, *a, **k)

    shutil.copyfile = spy
    try:
        final = transfer(source, incoming)
    finally:
        shutil.copyfile = real_copy
    assert not any(c.endswith("documents.jsonl.gz") for c in copied)  # resumed: not copied again
    assert any(c.endswith("rows.json") for c in copied)
    assert verify_unit(final)["seal"] == ref.sha256 and not partial.exists()
    assert transfer(source, incoming) == final  # idempotent
    other = make_ledger(tmp_path / "other" / "ledger")
    clash = other.seal(DAY, "five_minute", [doc("logs:incident:q", "q")], as_of=at(DAY, 0, 5))
    assert clash.slice_id == ref.slice_id
    with pytest.raises(HandoffRefused, match="expected"):
        transfer(other.unit_dir(clash.slice_id), incoming)
    changed = tmp_path / "changing" / source.name
    shutil.copytree(source, changed)
    (changed / "embed.json").write_text("{}")  # the source changed after it was sealed
    with pytest.raises(HandoffRefused, match="embed.json"):
        transfer(changed, tmp_path / "incoming2")
