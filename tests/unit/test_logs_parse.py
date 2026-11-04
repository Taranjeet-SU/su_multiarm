"""Partition-stage parsing (addendum A1 §3.4.2, §3.9.1): statuses, signature row exactness under the
coverage condition, scaled estimates of sampled units, and the parse pool's library check."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from su_multi_arm.core.config import PatternLibraryRef
from su_multi_arm.extraction.patterns.extractor import PatternExtractor
from su_multi_arm.ingestion.logs_parse import LibraryDrift, parse_matches, parse_units, record_text
from su_multi_arm.ingestion.partitions import UnitArtifacts
from su_multi_arm.sources.contracts import SamplePlan, SourceUnit, Stratum, UnitCount
from su_multi_arm.sources.logs import LogRecord
from tests.logs_support import ENGINE, write_library
from tests.pattern_fixtures import engine_or_skip

TS = datetime(2026, 1, 5, 10, 30, tzinfo=timezone.utc)
LINES = [
    ("ERROR", "[2026-01-05T10:30:00.000Z] [ERROR] [query-gateway] db-client failed to connect redis after 30 ms"),
    ("ERROR", "[2026-01-05T10:30:01.000Z] [ERROR] [query-gateway] queue-client refused by kafka"),
    ("INFO", "[2026-01-05T10:30:02.000Z] [INFO] [query-gateway] GET /healthz 200 1ms"),
    ("INFO", "[2026-01-05T10:30:03.000Z] [INFO] [query-gateway] indexed 3 documents"),
    ("INFO", "a line in no known layout"),
]


@pytest.fixture
def library(tmp_path):
    engine_or_skip(ENGINE)
    write_library(tmp_path / "lib")
    return PatternExtractor(PatternLibraryRef(path=tmp_path / "lib", engine=ENGINE), None).load()


def artifacts(method="none", records_query=None, levels=(), basis=None, total=None, stack=None) -> UnitArtifacts:
    uid = "1" * 32
    records = [
        LogRecord(id=f"{i:032x}", ts=TS, partition_key="2026-01-05", attributes={"tenant": "t001", "env": "prod", "service": "query-gateway"}, level=level, level_basis="level_field", message=text, stack=stack if i == 0 else None, source_id=f"s{i}", index="t001-prod-query-gateway-2026.01.05")
        for i, (level, text) in enumerate(LINES)
    ]
    total = total or len(records)
    sample = SamplePlan(method=method, cap=100, unit_total=total, strata=[Stratum(key="all", count=total, quota=len(records), fetched=len(records))])
    return UnitArtifacts(
        unit=SourceUnit(id=uid, index="t001-prod-query-gateway-2026.01.05", attributes={"tenant": "t001", "env": "prod", "service": "query-gateway"}, query_sha="q"),
        count=UnitCount(unit_id=uid, query_sha="q", total=total, by_query={"errors": 2}, basis={"errors": "level_field"}, at=TS),
        sample=sample,
        records=records,
        records_query=records_query,
        records_levels=list(levels),
        records_basis=basis,
    )


def test_statuses_signatures_cells_and_queue(library):
    parsed = parse_units([artifacts(stack="  at Handler.route (/srv/app/x.js:1:2)")], library)
    statuses = [m["status"] for m in parsed.matches]
    assert statuses == ["parsed", "parsed", "noise", "format_only", "unparsed"]
    assert parsed.counts["parsed"] == 2 and parsed.counts["noise"] == 1 and parsed.counts["records"] == 5
    (row,) = parsed.aggregates["signatures"]
    assert row["signature_id"] == "dependency.connect_failed" and row["lines"] == 2 and row["row_exactness"] == "exact"
    assert parsed.hourly_cells == [{"signature_id": "dependency.connect_failed", "tenant": "t001", "env": "prod", "service": "query-gateway", "hour": "2026-01-05T10:00:00+00:00", "lines": 2}]
    assert [u["status"] for u in parsed.unparsed] == ["format_only", "unparsed"]
    exemplar = parsed.exemplars["dependency.connect_failed"][0]["text"]
    assert exemplar.endswith("at Handler.route (/srv/app/x.js:1:2)") and "\n\n" not in exemplar
    assert all("text" not in m for m in parsed.matches)  # matches are content-free


def test_signature_rows_are_exact_only_under_the_coverage_condition(library):
    # level-field tier query covering the pattern's levels (error, warn): exact
    exact = parse_units([artifacts(records_query="errors", levels=["ERROR", "WARN"], basis="level_field")], library).aggregates["signatures"][0]
    assert exact["row_exactness"] == "exact"
    # tier query narrower than the pattern's levels: approximate
    narrow = parse_units([artifacts(records_query="errors", levels=["ERROR"], basis="level_field")], library).aggregates["signatures"][0]
    assert narrow["row_exactness"] == "approximate"
    # token-match tier query: never exact
    token = parse_units([artifacts(records_query="errors", levels=["ERROR", "WARN"], basis="token_match")], library).aggregates["signatures"][0]
    assert token["row_exactness"] == "approximate"


def test_sampled_units_scale_to_an_estimate(library):
    parsed = parse_units([artifacts(method="hourly_quota", total=50)], library)
    row = parsed.aggregates["signatures"][0]
    assert row["row_exactness"] == "approximate" and row["lines_pulled"] == 2 and row["lines"] == 20
    assert row["method"].startswith("approximate (hourly_quota")
    assert parsed.aggregates["units"][0]["row_exactness"] == "approximate"


def test_record_text_removes_blank_lines():
    record = LogRecord(id="x", ts=TS, partition_key="k", message="head\n\nsecond", stack="\n  at a\n\n  at b", source_id="s", index="i")
    assert record_text(record) == "head\nsecond\n  at a\n  at b"


def test_pool_refuses_a_worker_on_another_library(library, tmp_path):
    other = tmp_path / "other"
    write_library(other, [{"id": "msg.extra", "kind": "message", "status": "approved", "regex": r"^extra line here$", "signature": {"id": "extra"}, "examples": {"positive": ["extra line here"]}, "provenance": {"author": "user"}}])
    records = artifacts().records * 3
    with pytest.raises(LibraryDrift):
        parse_matches(records, library, workers=2, batch_size=4, ref=PatternLibraryRef(path=other, engine=ENGINE))
    same = parse_matches(records, library, workers=2, batch_size=4, ref=PatternLibraryRef(path=tmp_path / "lib", engine=ENGINE))
    assert same == parse_matches(records, library)
