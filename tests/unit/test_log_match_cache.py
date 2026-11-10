"""Exact repeated heads save matcher work without collapsing any occurrence or scope."""
from datetime import timedelta

import pytest

from su_multi_arm.core.config import PatternLibraryRef
from su_multi_arm.extraction.patterns.extractor import LineResult, PatternExtractor
from su_multi_arm.ingestion import logs_parse
from su_multi_arm.ingestion.accounting import AccountingPolicy
from su_multi_arm.ingestion.logs_parse import match_records, parse_matches, parse_units
from su_multi_arm.extraction.patterns.cache import ExactMatchCache
from tests.logs_support import ENGINE, write_library
from tests.pattern_fixtures import engine_or_skip
from tests.unit.test_logs_parse import artifacts


@pytest.fixture
def library(tmp_path):
    engine_or_skip(ENGINE)
    write_library(tmp_path / "lib")
    return PatternExtractor(PatternLibraryRef(path=tmp_path / "lib", engine=ENGINE), None).load()


def repeats(n=20):
    source = artifacts()
    first = source.records[0]
    source.records = [first.model_copy(update={"id": f"record-{i}", "source_id": f"source-{i}",
        "ts": first.ts + timedelta(hours=i % 2), "stack": f"  at Synthetic.handler{i} (demo.js:1:2)"}) for i in range(n)]
    source.count.total = n
    source.sample.unit_total = n
    source.sample.strata[0].count = source.sample.strata[0].quota = source.sample.strata[0].fetched = n
    return source


def test_reuse_preserves_ids_counts_hours_evidence_and_accounting(library, monkeypatch):
    source = repeats()
    optimized = parse_units([source], library)
    assert optimized.parse_work[source.unit.id]["head_match_evaluations"] == 2
    assert optimized.parse_work[source.unit.id]["exact_match_reuses"] == 18
    assert len({r["id"] for r in optimized.matches}) == 20
    assert sum(r["lines"] for r in optimized.hourly_cells) == 20
    assert len(optimized.hourly_cells) == 2
    monkeypatch.setattr(logs_parse, "ExactMatchCache", lambda: ExactMatchCache(max_entries=0))
    baseline = parse_units([source], library)
    assert optimized.matches == baseline.matches
    assert optimized.hourly_cells == baseline.hourly_cells
    assert optimized.exemplars == baseline.exemplars
    assert optimized.accounting == baseline.accounting
    assert optimized.counts == baseline.counts
    assert optimized.aggregates == baseline.aggregates


def test_same_length_and_prefix_different_values_are_not_duplicates(library):
    a = repeats(1).records[0]
    b = a.model_copy(update={"id": "other", "message": a.message.replace("30 ms", "40 ms")})
    assert len(a.message) == len(b.message)
    stats = {}
    rows = match_records([a, b, a, b, a], library, stats=stats)
    assert stats["head_match_evaluations"] == 4 and stats["exact_match_reuses"] == 1
    assert rows[0]["fields"][0]["value"] == 30
    assert rows[1]["fields"][0]["value"] == 40


def test_all_selectors_level_source_and_truncation_are_part_of_key(library):
    a = repeats(1).records[0]
    records = [a]
    for key in ("tenant", "service", "env", "deployment", "custom_selector"):
        records.append(a.model_copy(update={"attributes": {**a.attributes, key: "other"}}))
    records += [a.model_copy(update={"level": "INFO"}), a.model_copy(update={"level": None}), a.model_copy(update={"truncated": True})]
    stats = {}
    assert match_records(records, library, stats=stats) == match_records(records, library, cache_enabled=False)
    assert stats["head_match_evaluations"] == len(records)
    assert stats["exact_match_reuses"] == 0


def test_mutating_one_output_does_not_change_other_outputs_or_cached_template(library):
    records, cache = repeats(3).records, ExactMatchCache()
    rows = match_records(records, library, cache=cache)
    rows[0]["fields"][0]["value"] = -100
    rows[1]["level_selector"].append("invented")
    again = match_records(records[:1], library, cache=cache)[0]
    assert again["fields"][0]["value"] == rows[2]["fields"][0]["value"] == 30
    assert "invented" not in again["level_selector"]


def test_policy_or_library_change_invalidates_reuse(library, tmp_path):
    records, cache = repeats(2).records, ExactMatchCache()
    match_records(records, library, cache=cache)
    required = AccountingPolicy(required={"connectivity": ["absent"]})
    stats = {}
    result = match_records(records, library, required, cache=cache, stats=stats)
    assert result[0]["reason"] == "field_gap" and stats["head_match_evaluations"] == 2
    fresh_library = PatternExtractor(PatternLibraryRef(path=tmp_path / "lib", engine=ENGINE), None).load()
    match_records(records, fresh_library, required, cache=cache, stats=stats)
    assert stats["head_match_evaluations"] == 2


def test_timeouts_are_retried_per_occurrence_and_exceptions_propagate(library, monkeypatch):
    original, calls = library.match_line, []
    def timeout_once(text, **kwargs):
        calls.append(text)
        return LineResult(text=text, status="timeout") if len(calls) == 1 else original(text, **kwargs)
    monkeypatch.setattr(library, "match_line", timeout_once)
    stats = {}
    rows = match_records(repeats(4).records, library, stats=stats)
    assert [r["status"] for r in rows] == ["timeout", "parsed", "parsed", "parsed"]
    assert len(calls) == 3 and stats["exact_match_reuses"] == 1
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(library, "match_line", fail)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        match_records(repeats(2).records, library)


def test_lru_entry_byte_limits_and_oversized_bypass(library):
    a = repeats(1).records[0]
    b = a.model_copy(update={"message": a.message.replace("30 ms", "40 ms")})
    stats = {}
    cache = ExactMatchCache(max_entries=1, admission_hits=1)
    rows = match_records([a, b, a], library, cache=cache, stats=stats)
    assert stats["head_match_evaluations"] == 3 and stats["cache_evictions"] == 2
    assert len(cache.entries) == 1 and cache.bytes <= cache.max_bytes
    tiny = ExactMatchCache(max_bytes=1, admission_hits=1)
    assert match_records([a, b, a], library, cache=tiny, stats=stats) == rows
    assert stats["oversized_results"] == 3 and not tiny.entries and tiny.bytes == 0


def test_equal_hashes_cannot_merge_different_exact_keys():
    class Collision(str):
        def __hash__(self):
            return 7
    a, b = (Collision("abc"),), (Collision("xyz"),)
    assert hash(a) == hash(b) and a != b
    cache = ExactMatchCache(admission_hits=1)
    cache.put(a, {"status": "parsed", "signature": "one"})
    cache.put(b, {"status": "parsed", "signature": "two"})
    assert cache.get(a)["signature"] == "one" and cache.get(b)["signature"] == "two"


def test_unparsed_and_noise_reuse_still_count_all_occurrences(library):
    source = artifacts()
    records = [source.records[i].model_copy(update={"id": f"{i}-{j}"}) for i in (2, 3, 4) for j in range(5)]
    stats = {}
    assert match_records(records, library, stats=stats) == match_records(records, library, cache_enabled=False)
    assert stats["head_match_evaluations"] == 6 and stats["exact_match_reuses"] == 9


def test_pool_reuses_across_batches_without_changing_rows(library, tmp_path):
    records = repeats(60).records
    stats = {}
    ref = PatternLibraryRef(path=tmp_path / "lib", engine=ENGINE)
    rows = parse_matches(records, library, workers=2, batch_size=5, ref=ref, stats=stats)
    assert rows == match_records(records, library, cache_enabled=False)
    assert 2 <= stats["head_match_evaluations"] <= 4  # two admissions per participating worker
    assert stats["head_match_evaluations"] + stats["exact_match_reuses"] == 60


def test_unique_stream_bypasses_cache_and_later_repeated_region_recovers(library):
    a = repeats(1).records[0]
    unique = [a.model_copy(update={"message": a.message.replace("30 ms", f"{i + 1} ms")}) for i in range(500)]
    records = unique + [a] * 5000
    stats = {}
    assert match_records(records, library, stats=stats) == match_records(records, library, cache_enabled=False)
    assert stats["cache_bypasses"] == 4096
    assert stats["exact_match_reuses"] > 1000
    assert stats["head_match_evaluations"] + stats["exact_match_reuses"] == len(records)


JSON_FORMAT = {
    "id": "fmt.synthetic_json", "kind": "format", "status": "approved", "priority": 200,
    "regex": r'^\{"event":\{"timestamp":"(?P<ts>[0-9T:.Z+-]{20,35})"\},"level":"(?P<level>[A-Z]{4,5})","message":"(?P<body>[^"]{1,500})"\}$',
    "fields": {"ts": {"type": "datetime"}}, "then": {"field_from_group": "body"},
    "examples": {"positive": ['{"event":{"timestamp":"2026-01-05T10:30:00Z"},"level":"ERROR","message":"db-client failed to connect redis after 30 ms"}']},
    "provenance": {"author": "user"},
}


def json_records():
    import json
    from datetime import datetime
    base = repeats(3)
    times = ("2026-01-05T10:30:00Z", "2026-01-05T11:30:00.123Z", "2026-01-05T12:30:00Z")
    base.records = [r.model_copy(update={"level": None, "ts": datetime.fromisoformat(t.replace("Z", "+00:00")),
        "message": json.dumps({"event": {"timestamp": t}, "level": "ERROR", "message": "db-client failed to connect redis after 30 ms"}, separators=(",", ":"))}) for r, t in zip(base.records, times)]
    return base


def test_json_timestamp_body_reuse_preserves_new_timestamps_spans_and_hours(tmp_path):
    engine_or_skip(ENGINE)
    write_library(tmp_path / "json-lib", [JSON_FORMAT])
    library = PatternExtractor(PatternLibraryRef(path=tmp_path / "json-lib", engine=ENGINE), None).load()
    source, stats = json_records(), {}
    rows = match_records(source.records, library, stats=stats)
    assert stats["head_match_evaluations"] == 3 and stats["exact_match_reuses"] == 0
    assert stats["body_match_evaluations"] == 2 and stats["body_match_reuses"] == 1
    assert rows == match_records(source.records, library, cache_enabled=False)
    assert rows[0]["fields"] != rows[1]["fields"]  # current timestamps and changed body offsets
    for record, row in zip(source.records, rows):
        fields = {f["name"]: f for f in row["fields"]}
        assert fields["ts"]["value"] == record.ts.isoformat()
        assert record.message[fields["ms"]["start"]:fields["ms"]["end"]] == "30"
    parsed = parse_units([source], library)
    assert len(parsed.hourly_cells) == 3 and sum(r["lines"] for r in parsed.hourly_cells) == 3


def test_timestamp_dependent_selector_prevents_body_reuse(tmp_path):
    engine_or_skip(ENGINE)
    timed = {"id": "msg.timed", "kind": "message", "status": "approved", "priority": 500,
        "applies_to": {"ts": ["2026-01-05T11:30:00.123Z"]},
        "regex": r"db-client failed to connect redis after [0-9]{1,6} ms",
        "signature": {"id": "timed"}, "examples": {"positive": ["db-client failed to connect redis after 30 ms"]},
        "provenance": {"author": "user"}}
    write_library(tmp_path / "timed-lib", [JSON_FORMAT, timed])
    library = PatternExtractor(PatternLibraryRef(path=tmp_path / "timed-lib", engine=ENGINE), None).load()
    stats = {}
    rows = match_records(json_records().records, library, stats=stats)
    assert [r["signature"] for r in rows] == ["dependency.connect_failed", "timed", "dependency.connect_failed"]
    assert stats["body_match_reuses"] == 0


def test_opaque_json_timestamp_is_not_blindly_removed(library):
    import json
    source, stats = json_records(), {}
    for record in source.records:
        payload = json.loads(record.message)
        payload["unrecognized_schema"] = True
        record.message = json.dumps(payload)
    rows = match_records(source.records, library, stats=stats)
    assert rows == match_records(source.records, library, cache_enabled=False)
    assert stats["body_match_reuses"] == stats["exact_match_reuses"] == 0


def test_body_result_mutation_does_not_corrupt_cached_groups(library):
    a = repeats(1).records[0].message
    b = a.replace("10:30:00.000", "11:30:00.000")
    library.match_line(a)
    first = library.match_line(a)
    first.message.groups.clear()
    second = library.match_line(b)
    assert second.typed_fields()["ms"] == 30


def test_shared_library_has_thread_local_body_caches(library):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    barrier = Barrier(4)
    first = repeats(1).records[0].message
    def run_thread(index):
        barrier.wait(timeout=5)
        for i in range(20):
            text = first.replace("10:30:00.000", f"{index + 1:02}:30:{i:02}.000")
            result = library.match_line(text)
            assert result.typed_fields()["ms"] == 30
            assert result.message.groups["ms"] is not None
        return id(library.body_cache), library.body_cache.counts["reuses"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run_thread, range(4)))
    assert len({identity for identity, _ in results}) == 4
    assert all(reuses == 18 for _, reuses in results)
