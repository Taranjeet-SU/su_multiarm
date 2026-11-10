"""Independent synthetic equivalence checks for exact grouped observations.

The raw oracle expands known synthetic occurrences. Grouped input retains one real
example and an exact multiplicity; it must not manufacture additional evidence.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from su_multi_arm.core.config import PatternLibraryRef
from su_multi_arm.extraction.patterns.extractor import PatternExtractor
from su_multi_arm.ingestion.logs_parse import parse_units
from su_multi_arm.ingestion.partitions import FilePartitionStore, GroupedMultiplicity, UnitArtifacts
from su_multi_arm.runtime.coverage_metrics import coverage_counters, coverage_metrics
from su_multi_arm.sources.contracts import SamplePlan, SourceUnit, Stratum, UnitCount
from su_multi_arm.sources.logs import LogRecord
from tests.logs_support import ENGINE, write_library
from tests.pattern_fixtures import engine_or_skip

DAY = "2026-01-05"
HOUR = datetime(2026, 1, 5, 10, tzinfo=timezone.utc)


@pytest.fixture
def library(tmp_path):
    engine_or_skip(ENGINE)
    path = write_library(tmp_path / "patterns")
    return PatternExtractor(PatternLibraryRef(path=path, engine=ENGINE), None).load()


def source(*, compressed=True, tenant="alpha", service="query-gateway", number=1):
    """Two hours, two severities, noise and two distinct raw-only stack traces."""
    uid = f"{number:032x}"
    index = f"{tenant}-prod-{service}-2026.01.05"
    attrs = {"tenant": tenant, "env": "prod", "service": service, "deployment": f"{tenant}-prod"}
    specs = [
        ("db-client failed to connect redis after 30 ms", "ERROR", 0, 7, None),
        ("db-client failed to connect redis after 40 ms", "WARN", 0, 3, None),
        ("queue-client refused by kafka", "ERROR", 1, 5, None),
        ("GET /healthz 200 2ms", "INFO", 0, 11, None),
        ("db-client failed to connect redis after 30 ms", "ERROR", 0, 1, "at Synthetic.one (demo.py:1)"),
        ("db-client failed to connect redis after 30 ms", "ERROR", 0, 1, "at Synthetic.two (demo.py:2)"),
    ]
    records, groups = [], {}
    for n, (message, level, hour, count, stack) in enumerate(specs):
        start = HOUR + timedelta(hours=hour)
        for occurrence in range(1 if compressed else count):
            record_id = f"{uid}-{n}-{occurrence}"
            record = LogRecord(
                id=record_id, ts=start + timedelta(seconds=occurrence), partition_key=DAY,
                attributes=attrs, level=level, level_basis="level_field", message=message,
                stack=stack, source_id=f"document-{record_id}", index=index,
            )
            records.append(record)
            if compressed and count > 1:
                groups[record_id] = GroupedMultiplicity(
                    group_id=f"group-{uid}-{n}", occurrence_count=count,
                    hour_start=start, first_seen=start, last_seen=start + timedelta(seconds=count - 1),
                )
    total = sum(spec[3] for spec in specs)
    physical = len(records)
    return UnitArtifacts(
        unit=SourceUnit(id=uid, index=index, attributes=attrs, query_sha="synthetic-grouped"),
        count=UnitCount(unit_id=uid, query_sha="synthetic-grouped", total=total, at=HOUR),
        sample=SamplePlan(method="none", cap=total, unit_total=total,
                          strata=[Stratum(key="all", count=total, quota=physical, fetched=physical)]),
        records=records, grouped=groups, docs_pulled=physical, malformed=0,
        multiline={"heads": physical, "joined": 0, "fragments": 0, "overflow": 0},
    )


def analytic_counts(parsed, kind):
    rows = Counter()
    dimensions = {
        "signatures": ("signature_id",),
        "levels": ("level", "status", "signature_id"),
        "level_hours": ("hour", "level", "status", "signature_id"),
        "exceptions": ("signature_id", "exception_class"),
    }[kind]
    for row in parsed.aggregates[kind]:
        scope = tuple(sorted(row["attributes"].items()))
        rows[(scope, *(row.get(key) for key in dimensions))] += row["lines"]
    return rows


def test_exact_group_counts_match_expanded_occurrences_without_scope_cross_products(library):
    scopes = [("alpha", "query-gateway"), ("beta", "doc-fetcher"), ("alpha", "scheduler")]
    grouped = [source(tenant=t, service=s, number=n) for n, (t, s) in enumerate(scopes, 1)]
    expanded = [source(compressed=False, tenant=t, service=s, number=n) for n, (t, s) in enumerate(scopes, 1)]
    actual, oracle = parse_units(grouped, library), parse_units(expanded, library)
    for kind in ("signatures", "levels", "level_hours", "exceptions"):
        assert analytic_counts(actual, kind) == analytic_counts(oracle, kind)
        assert all(row["row_exactness"] == "exact" for row in actual.aggregates[kind])
    assert actual.hourly_cells == oracle.hourly_cells
    assert len(actual.matches) == 18
    assert len(oracle.matches) == 84
    assert sum(row["lines_pulled"] for row in actual.aggregates["levels"]) == 18
    assert sum(row["lines"] for row in actual.aggregates["levels"]) == 84


def test_examples_and_accounting_remain_physical_observations(library):
    unit = source()
    result = parse_units([unit], library, max_exemplars=50)
    original_ids = {record.id for record in unit.records}
    assert {row["id"] for row in result.matches} == original_ids
    exemplars = [row for rows in result.exemplars.values() for row in rows]
    assert {row["id"] for row in exemplars} <= original_ids
    assert len(exemplars) == 5
    assert sum("Synthetic.one" in row["text"] for row in exemplars) == 1
    assert sum("Synthetic.two" in row["text"] for row in exemplars) == 1
    assert all(account.balanced() for account in result.accounting)
    assert result.accounting[0].docs_pulled == 6
    assert result.accounting[0].heads == 6
    row = result.aggregates["units"][0]
    assert row["documents_collected"] == row["parser_heads_scanned"] == row["pulled"] == 6
    assert row["documents_represented"] == 28
    assert sum(row["statuses"].values()) == 6


def test_coverage_does_not_present_represented_occurrences_as_downloaded_records(library):
    row = parse_units([source()], library).aggregates["units"][0]
    metrics = coverage_metrics({**coverage_counters(row), "lines_total": row["lines_total"]}, 1)
    assert metrics["documents_matching"] == metrics["documents_represented"] == 28
    assert metrics["documents_collected"] == 6
    assert metrics["completeness"] == round(6 / 28, 4)
    assert metrics["scanned"] == 1
    assert metrics["extracted"] == round(5 / 6, 4)
    assert "group" in metrics["collection_note"].lower()


def test_same_library_delta_reparse_preserves_counts_and_evidence(library):
    unit = source()
    first = parse_units([unit], library, max_exemplars=50)
    again = parse_units([unit], library, max_exemplars=50, reuse={row["id"]: row for row in first.matches})
    assert again.matches == first.matches
    assert again.aggregates == first.aggregates
    assert again.hourly_cells == first.hourly_cells
    assert again.exemplars == first.exemplars


def test_group_metadata_round_trips_through_sealed_unit(library, tmp_path):
    unit = source()
    store = FilePartitionStore(tmp_path / "partitions", "synthetic")
    store.begin(DAY, "synthetic-grouped", {})
    store.put_unit(DAY, unit.unit.id, unit)
    parsed = parse_units([unit], library)
    store.seal(DAY, parsed)
    (restored,) = list(store.units(DAY, store.get(DAY).version))
    assert restored.grouped == unit.grouped
    assert parse_units([restored], library).aggregates == parsed.aggregates


def test_endpoint_statistics_use_exact_multiplicity_without_expanding_evidence(tmp_path):
    engine_or_skip(ENGINE)
    path = write_library(tmp_path / "endpoint-patterns", [{
        "id": "msg.synthetic.request", "kind": "message", "status": "approved", "priority": 100,
        "regex": r"^(?P<http_method>GET) (?P<endpoint>/orders/[0-9]+) (?P<status_code>503) (?P<latency_ms>[0-9]+)ms$",
        "signature": {"id": "synthetic.request"},
        "fields": {"http_method": {"type": "string"}, "endpoint": {"type": "string"},
                   "status_code": {"type": "integer"}, "latency_ms": {"type": "integer"}},
        "examples": {"positive": ["GET /orders/123 503 1ms"]}, "provenance": {"author": "user"},
    }])
    library = PatternExtractor(PatternLibraryRef(path=path, engine=ENGINE), None).load()
    unit = source()
    unit.records[0].message = "GET /orders/123 503 1ms"
    unit.records[1].message = "GET /orders/456 503 100ms"
    unit.grouped[unit.records[0].id].occurrence_count = 100
    unit.count.total += 93
    result = parse_units([unit], library)
    (row,) = result.aggregates["endpoints"]
    assert row["endpoint"] == "/orders/{id}"
    assert row["lines"] == row["latency_ms_count"] == 103
    assert row["lines_pulled"] == 2
    assert row["latency_ms_sum"] == 400
    assert row["latency_ms_max"] == 100
    assert row["latency_ms_p95"] == 1
    assert len(result.matches) == 6


def test_exception_counts_use_group_multiplicity(tmp_path):
    engine_or_skip(ENGINE)
    path = write_library(tmp_path / "exception-patterns", [{
        "id": "msg.synthetic.exception", "kind": "message", "status": "approved", "priority": 100,
        "regex": r"^Exception=(?P<exception_class>[A-Za-z]+)$",
        "signature": {"id": "synthetic.exception"},
        "fields": {"exception_class": {"type": "string"}},
        "examples": {"positive": ["Exception=ConnectionError"]}, "provenance": {"author": "user"},
    }])
    library = PatternExtractor(PatternLibraryRef(path=path, engine=ENGINE), None).load()
    unit = source()
    unit.records[0].message = unit.records[2].message = "Exception=ConnectionError"
    result = parse_units([unit], library)
    (row,) = result.aggregates["exceptions"]
    assert row["exception_class"] == "ConnectionError"
    assert row["lines"] == 12 and row["lines_pulled"] == 2
    assert row["row_exactness"] == "exact"


def test_model_fallback_and_overlay_refuse_grouped_partitions_before_provider_work(library, tmp_path):
    from su_multi_arm.ingestion.log_rollups import overlay_aggregates
    from su_multi_arm.ingestion.overlay import OverlayStore
    from su_multi_arm.learning.extraction.run import FallbackSession

    unit = source()
    store = FilePartitionStore(tmp_path / "guard-partitions", "synthetic")
    store.begin(DAY, "synthetic-grouped", {})
    store.put_unit(DAY, unit.unit.id, unit)
    manifest = store.seal(DAY, parse_units([unit], library))

    def forbidden(*args, **kwargs):
        pytest.fail("Grouped fallback reached bootstrap/provider work")

    session = SimpleNamespace(clock=forbidden, bootstrap=forbidden)
    with pytest.raises(ValueError, match="unsupported.*grouped"):
        FallbackSession._run(session, store, [DAY], library, allow_calls=True, now=None, progress=None)
    with pytest.raises(ValueError, match="unsupported.*grouped"):
        OverlayStore(store).write_unit(DAY, manifest.version, "f" * 32, unit.unit.id, [])
    with pytest.raises(ValueError, match="unsupported.*grouped"):
        overlay_aggregates(store, manifest, [SimpleNamespace(head_id=unit.records[0].id)])


@pytest.mark.parametrize("change", ["duplicate_group", "duplicate_record", "unknown_representative", "missing_key"])
def test_group_identity_and_equality_key_are_validated(library, change):
    unit = source()
    first, second = unit.records[:2]
    if change == "duplicate_group":
        unit.grouped[second.id].group_id = unit.grouped[first.id].group_id
    elif change == "duplicate_record":
        unit.records.append(first)
    elif change == "unknown_representative":
        unit.grouped["missing"] = unit.grouped.pop(first.id)
    elif change == "missing_key":
        unit.grouped[first.id].key_fields.remove("level")
    with pytest.raises(ValueError):
        parse_units([unit], library)


@pytest.mark.parametrize("count", [0, -1, True, 1.5, "7"])
def test_group_count_is_a_positive_strict_integer(count):
    with pytest.raises(ValueError):
        GroupedMultiplicity(group_id="invalid", occurrence_count=count,
                            hour_start=HOUR, first_seen=HOUR, last_seen=HOUR)


@pytest.mark.parametrize("field,value", [
    ("stack", "at Different.secret (synthetic.py:3)"),
    ("host", "different-host"),
    ("container", "different-container"),
    ("file", "/synthetic/other.log"),
    ("offset", 5),
    ("extra", {"trace_id": "trace-only-in-example"}),
    ("truncated", True),
    ("lines", 2),
    ("message", "first line\nsecond line"),
    ("attributes", {"tenant": "other", "env": "prod", "service": "query-gateway"}),
    ("index", "other-prod-query-gateway-2026.01.05"),
])
def test_unsafe_representative_facts_cannot_be_promoted_to_group(library, field, value):
    unit = source()
    unit.records[0] = unit.records[0].model_copy(update={field: value})
    with pytest.raises(ValueError):
        parse_units([unit], library)


@pytest.mark.parametrize("change", ["total", "missing_record", "sampled", "capped", "selected_tier"])
def test_unreconciled_or_sampled_groups_cannot_claim_exact_counts(library, change):
    unit = source()
    if change == "total":
        unit.count.total += 1
    elif change == "missing_record":
        unit.records.pop(0)
    elif change == "sampled":
        unit.sample.method = "random_score"
    elif change == "capped":
        unit.capped = True
    elif change == "selected_tier":
        unit.records_query = "errors"
    with pytest.raises(ValueError):
        parse_units([unit], library)


@pytest.mark.parametrize("field,value", [
    ("hour_start", HOUR + timedelta(minutes=1)),
    ("first_seen", HOUR - timedelta(microseconds=1)),
    ("last_seen", HOUR + timedelta(hours=1)),
    ("hour_start", HOUR.replace(tzinfo=None)),
    ("first_seen", HOUR.replace(tzinfo=None)),
    ("last_seen", HOUR.replace(tzinfo=None)),
])
def test_group_times_need_an_unambiguous_single_utc_hour(library, field, value):
    with pytest.raises(ValueError):
        unit = source()
        record_id = unit.records[0].id
        data = unit.grouped[record_id].model_dump()
        data[field] = value
        unit.grouped[record_id] = GroupedMultiplicity.model_validate(data)
        parse_units([unit], library)
