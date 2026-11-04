"""Log mapping (addendum A1 §3.2.1–3.2.2): index-name parsing by service vocabulary (longest suffix),
per-index count queries from the mapping, ``LogRecord`` normalisation (ANSI, control characters,
``max_line_bytes`` cut) and multiline assembly. Synthetic data only."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from su_multi_arm.core.config_sources import CountQueryConfig, IndexNameConfig, LogFieldMap, MultilineConfig
from su_multi_arm.sources import LogMapping, SourceDoc, SourceUnit
from su_multi_arm.sources.errors import SourceSchemaDrift
from su_multi_arm.sources.logs import IndexNameParser, LogRecord, count_query, flatten_mapping
from su_multi_arm.sources.multiline import assemble
from tests.sources_support import source_config

VOCAB = ["gateway", "query-gateway", "doc-fetcher", "ingest-worker", "scheduler"]


def parser(**overrides) -> IndexNameParser:
    return IndexNameParser(IndexNameConfig.model_validate({"vocabularies": {"service": VOCAB}, **overrides}))


@pytest.mark.parametrize(
    "name, expected",
    [
        ("t001-prod-query-gateway-2026.01.05", {"tenant": "t001", "env": "prod", "service": "query-gateway"}),
        ("t001-prod-gateway-2026.01.05", {"tenant": "t001", "env": "prod", "service": "gateway"}),
        ("t2-stage-doc-fetcher-2026.01.05", {"tenant": "t2", "env": "stage", "service": "doc-fetcher"}),
        ("t9-dev-ingest-worker-2026.12.31", {"tenant": "t9", "env": "dev", "service": "ingest-worker"}),
    ],
)
def test_index_names_parse_with_the_longest_service_suffix(name, expected):
    parsed = parser().parse(name)
    assert parsed.ok, parsed.problem
    assert {k: parsed.attributes[k] for k in expected} == expected
    assert parsed.index_date is not None


@pytest.mark.parametrize(
    "name, problem",
    [
        ("t001-prod-unknown-svc-2026.01.05", "no service vocabulary entry"),
        ("t001-prod-gateway", "does not end with"),
        ("t001-prod-gateway-2026.13.40", "invalid date"),
        ("gateway-2026.01.05", "expected prefix fields"),
        ("prod-query-gateway-2026.01.05", "expected prefix fields"),
    ],
)
def test_unparsed_index_names_are_unknown_with_a_problem(name, problem):
    parsed = parser().parse(name)
    assert not parsed.ok and problem in parsed.problem
    assert parsed.attributes["tenant"] == "unknown"


def test_index_names_without_a_vocabulary_take_one_token_per_segment():
    # Replaces the old "first prefix field keeps extra separators" rule: a multi-token segment needs a
    # vocabulary, otherwise the name is unparsed (test_index_name_grammar.py covers the grammar).
    parsed = IndexNameParser(IndexNameConfig(date_format="%Y%m%d", separator="_")).parse("t1_prod_svc_20260105")
    assert parsed.attributes == {"tenant": "t1", "env": "prod", "service": "svc", "index_date": "2026-01-05"}
    parsed = IndexNameParser(IndexNameConfig(date_format="%Y%m%d", separator="_")).parse("t1_prod_my_service_20260105")
    assert parsed.status == "unparsed" and parsed.reason == "segments" and parsed.attributes["service"] == "unknown"


def test_vocabulary_entries_are_literals_and_date_format_is_closed():
    with pytest.raises(ValidationError):
        IndexNameConfig.model_validate({"vocabularies": {"service": ["(a+)+"]}})
    with pytest.raises(ValidationError):
        IndexNameConfig(date_format="%Y.%m.%d.%H")


MAPPING = {"logs-a": {"mappings": {"properties": {
    "@timestamp": {"type": "date"},
    "message": {"type": "text"},
    "level": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
    "host": {"properties": {"name": {"type": "keyword"}}},
}}}}


def test_flatten_mapping_includes_multi_fields_and_objects():
    fields = flatten_mapping(MAPPING, "logs-a")
    assert fields == {"@timestamp": "date", "message": "text", "level": "text", "level.keyword": "keyword", "host.name": "keyword"}
    with pytest.raises(SourceSchemaDrift):
        flatten_mapping({}, "logs-a")


def test_count_queries_are_built_per_index_from_the_mapping():
    spec = CountQueryConfig(levels=["ERROR"], tokens=["error"])
    mapping = LogFieldMap(level="level")
    with_level = flatten_mapping(MAPPING, "logs-a")
    assert count_query("errors", spec, mapping, with_level) == ({"terms": {"level.keyword": ["ERROR"]}}, "level_field")
    without = {"@timestamp": "date", "message": "text"}
    assert count_query("errors", spec, mapping, without) == ({"match": {"message": {"query": "error", "operator": "or"}}}, "token_match")
    with pytest.raises(SourceSchemaDrift, match="no tokens"):
        count_query("errors", CountQueryConfig(levels=["ERROR"]), mapping, without)
    with pytest.raises(SourceSchemaDrift, match="not a text field"):
        count_query("errors", CountQueryConfig(tokens=["error"]), mapping, {"message": "keyword"})


UNIT = SourceUnit(id="u1", index="t001-prod-gateway-2026.01.05", attributes={"service": "gateway"}, query_sha="q")


def doc(message, ts="2026-01-05T10:00:00.000Z", **extra):
    fields = {"@timestamp": ts, "message": message, **extra}
    return SourceDoc(source_id=f"id-{abs(hash((message, ts))) % 10**8}", index=UNIT.index, fields=fields)


def test_log_records_strip_ansi_and_controls_and_cut_at_max_line_bytes():
    cfg = source_config(max_line_bytes=256, mapping={"level": "level", "extra": ["trace_id"]})
    mapping = LogMapping(cfg)
    [record] = mapping.map([doc("\x1b[31m[2026-01-05 10:00:00,000] [ERROR]\x1b[0m boom\x07", level="ERROR", trace_id="abc")], UNIT, "2026-01-05")
    assert record.message == "[2026-01-05 10:00:00,000] [ERROR] boom"
    assert (record.level, record.level_basis, record.extra, record.attributes) == ("ERROR", "level_field", {"trace_id": "abc"}, {"service": "gateway"})
    [long] = mapping.map([doc("é" * 300)], UNIT, "2026-01-05")
    assert long.truncated and len(long.message.encode()) <= 256 and long.message == "é" * 128
    assert long.level is None and long.level_basis == "none"
    assert mapping.stats.as_dict()["ansi_stripped"] == 1 and mapping.stats.truncated == 1
    assert mapping.redaction == "none"


def test_malformed_documents_are_counted_not_dropped_silently():
    mapping = LogMapping(source_config())
    out = mapping.map([doc("ok"), SourceDoc(source_id="x", index=UNIT.index, fields={"message": "no ts"}), SourceDoc(source_id="y", index=UNIT.index, fields={"@timestamp": 1767607200000, "message": 5})], UNIT, "p")
    assert len(out) == 1
    assert mapping.stats.malformed == 2 and sum(mapping.stats.problems.values()) == 2
    with pytest.raises(SourceSchemaDrift):
        mapping.map([SourceDoc(source_id="x", index=UNIT.index, fields={"message": "no ts"})], UNIT, "p", strict=True)


def test_injected_redactor_runs_before_the_cut():
    mapping = LogMapping(source_config(), redactor=lambda text: text.replace("secret", "[r]"))
    [record] = mapping.map([doc("token secret here")], UNIT, "p")
    assert record.message == "token [r] here" and mapping.redaction == "injected"


def rec(i, message, ms, container="c1", file="f1"):
    t0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
    return LogRecord(id=f"r{i:03d}", ts=t0 + timedelta(milliseconds=ms), partition_key="p", message=message, container=container, file=file, offset=i, source_id=f"s{i}", index="i")


def test_multiline_joins_continuations_to_the_head_of_the_same_stream():
    records = [
        rec(0, "    at orphan.frame(x.js:1:1)", 0),  # head-less fragment
        rec(1, "[ERROR] request failed", 1000),
        rec(2, "\tat org.example.A.run(A.java:1)", 1001),
        rec(3, "Caused by: org.example.Boom", 1002),
        rec(4, "\t... 3 more", 1003),
        rec(5, "\tat other.stream(B.java:2)", 1004, container="c2"),  # different container: fragment
        rec(6, "[INFO] next", 2000),
        rec(7, "\tat late.frame(A.java:9)", 9000),  # beyond max_gap_ms
    ]
    out, stats = assemble(records, MultilineConfig(max_gap_ms=2000))
    heads = {r.id: r for r in out}
    assert set(heads) == {"r001", "r006"}
    assert heads["r001"].stack.splitlines() == ["\tat org.example.A.run(A.java:1)", "Caused by: org.example.Boom", "\t... 3 more"]
    assert heads["r001"].lines == 4
    assert stats.as_dict() == {"heads": 2, "joined": 3, "fragments": 3, "overflow": 0, "fragment_samples": 0}


def test_multiline_bounds_mark_the_head_truncated():
    records = [rec(0, "[ERROR] x", 0)] + [rec(i, f"\tat f{i}(A.java:{i})", i) for i in range(1, 6)]
    out, stats = assemble(records, MultilineConfig(max_lines=3))
    assert out[0].lines == 3 and out[0].truncated and stats.overflow == 3


def test_fragment_sampling_is_deterministic():
    records = [rec(i, "\tat x.y(Z.java:1)", i * 10_000, container=f"c{i}") for i in range(400)]
    _, a = assemble(records, MultilineConfig(fragment_sample_rate=0.1))
    _, b = assemble(list(reversed(records)), MultilineConfig(fragment_sample_rate=0.1))
    assert a.fragments == 400 and 10 < len(a.fragment_samples) < 80
    assert [r.id for r in a.fragment_samples] == [r.id for r in b.fragment_samples]


def test_parsed_dates_are_dates():
    assert parser().parse("t001-prod-gateway-2026.01.05").index_date == date(2026, 1, 5)
