"""A partition reuses one bounded spawn pool without changing its evidence or counts."""

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from su_multi_arm.core.config import PatternLibraryRef
from su_multi_arm.extraction.patterns.extractor import PatternExtractor
from su_multi_arm.ingestion import logs_parse
from tests.logs_support import ENGINE, write_library
from tests.pattern_fixtures import engine_or_skip
from tests.unit.test_logs_parse import artifacts


@pytest.fixture
def library(tmp_path):
    engine_or_skip(ENGINE)
    write_library(tmp_path / "lib")
    return PatternExtractor(PatternLibraryRef(path=tmp_path / "lib", engine=ENGINE), None).load()


def two_units():
    first, second = artifacts(), artifacts()
    second.unit.id = "2" * 32
    second.count.unit_id = second.unit.id
    second.unit.attributes["tenant"] = "t002"
    for record in second.records:
        record.id = "second-" + record.id
        record.attributes["tenant"] = "t002"
    return [first, second]


def comparable(parsed):
    values = asdict(parsed)
    values.pop("parse_work")
    return values


@pytest.mark.parametrize("reuse_first", [False, True])
def test_one_pool_preserves_partition_and_delta_reuse(library, tmp_path, monkeypatch, reuse_first):
    units = two_units()
    baseline = logs_parse.parse_units(units, library)
    reuse = {row["id"]: row for row in baseline.matches if row["id"].endswith("0000")} if reuse_first else None
    real_pool = logs_parse.ProcessPoolExecutor
    created = []

    def pool(**kwargs):
        created.append(kwargs)
        return real_pool(**kwargs)

    monkeypatch.setattr(logs_parse, "ProcessPoolExecutor", pool)
    parsed = logs_parse.parse_units(units, library, workers=2, batch_size=2,
                                   ref=PatternLibraryRef(path=tmp_path / "lib", engine=ENGINE), reuse=reuse)
    assert len(created) == 1
    assert comparable(parsed) == comparable(baseline)
    assert sum(row["stored_rows_reused"] for row in parsed.parse_work.values()) == (2 if reuse_first else 0)


def test_serialization_and_submissions_are_bounded(monkeypatch):
    serialized = 0
    consumed = 0
    pending = 0
    peak_pending = 0
    shutdown = []

    class Record:
        def model_dump_json(self):
            nonlocal serialized
            serialized += 1
            return str(serialized)

    class Future:
        def __init__(self, batch):
            self.batch = batch

        def result(self):
            nonlocal consumed, pending
            # Four queued batches, two records each; the entire source is never serialized first.
            assert serialized - consumed <= 8
            consumed += len(self.batch)
            pending -= 1
            return "same", [{"id": item} for item in self.batch], {}

    class Pool:
        def __init__(self, **kwargs):
            pass

        def submit(self, fn, batch):
            nonlocal pending, peak_pending
            pending += 1
            peak_pending = max(peak_pending, pending)
            return Future(batch)

        def shutdown(self, **kwargs):
            shutdown.append(kwargs)

    monkeypatch.setattr(logs_parse, "ProcessPoolExecutor", Pool)
    ref = SimpleNamespace(model_dump_json=lambda: "{}")
    matches = logs_parse.parse_matches([Record() for _ in range(31)], SimpleNamespace(identity="same"),
                                      workers=2, batch_size=2, ref=ref)
    assert [row["id"] for row in matches] == [str(i) for i in range(1, 32)]
    assert peak_pending == 4
    assert shutdown == [{"wait": True, "cancel_futures": True}]


def test_partition_pool_closes_when_a_worker_fails(library, monkeypatch):
    shutdown = []

    class Pool:
        def __init__(self, **kwargs):
            pass

        def submit(self, fn, batch):
            def fail():
                raise RuntimeError("synthetic worker failure")
            return SimpleNamespace(result=fail)

        def shutdown(self, **kwargs):
            shutdown.append(kwargs)

    monkeypatch.setattr(logs_parse, "ProcessPoolExecutor", Pool)
    with pytest.raises(RuntimeError, match="synthetic worker failure"):
        logs_parse.parse_units(two_units(), library, workers=2, batch_size=2,
                               ref=SimpleNamespace(model_dump_json=lambda: "{}"))
    assert shutdown == [{"wait": True, "cancel_futures": True}]
