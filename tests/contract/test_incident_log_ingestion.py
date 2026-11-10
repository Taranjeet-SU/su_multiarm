"""Incident subsets retain counts/roles without extrapolating or counting context twice."""

from collections import Counter
from datetime import timedelta

import pytest

from su_multi_arm.ingestion.incident import IncidentContextWindow, IncidentSelection
from su_multi_arm.ingestion.logs_parse import parse_units
from su_multi_arm.ingestion.partitions import FilePartitionStore, PartitionConflict, UnitArtifacts
from su_multi_arm.runtime.coverage_metrics import coverage_counters, coverage_metrics
from su_multi_arm.sources.contracts import SamplePlan, Stratum
from tests.contract.test_grouped_log_ingestion import DAY, HOUR, source, library  # noqa: F401


def incident_unit(*, pending=False):
    raw = source(compressed=False)
    records = [raw.records[0], raw.records[1], next(r for r in raw.records if r.level == "INFO")]
    if pending:
        records = records[:1]
    seed = records[0].id
    start = HOUR.replace(hour=0)
    selection = IncidentSelection(
        policy_id="a" * 64,
        query_id="b" * 64,
        import_recipe="c" * 64,
        window_start=start,
        window_end=start + timedelta(days=1),
        source_total=1000,
        selected_seed_total=1,
        seed_record_ids=[seed],
        context_windows=[]
        if pending
        else [
            IncidentContextWindow(
                query_id="d" * 64,
                seed_record_ids=[seed],
                start=HOUR - timedelta(minutes=2),
                end=HOUR + timedelta(minutes=2),
                status="complete",
                source_total=len(records),
                record_ids=[r.id for r in records],
            )
        ],
    )
    n = len(records)
    return UnitArtifacts(
        unit=raw.unit,
        count=raw.count.model_copy(update={"total": 1000, "by_query": {"incident_union": n}}),
        sample=SamplePlan(
            method="none",
            cap=max(n, 1),
            unit_total=n,
            strata=[Stratum(key="all", count=n, quota=n, fetched=n)],
        ),
        records=records,
        docs_pulled=n,
        malformed=0,
        records_query="incident_union",
        incident=selection,
        multiline={"heads": n, "joined": 0, "fragments": 0, "overflow": 0},
    )


def test_context_error_parsed_but_does_not_inflate_seed_statistics(library):  # noqa: F811
    artifact = incident_unit()
    parsed = parse_units([artifact], library)
    assert len(parsed.matches) == 3
    assert Counter(m["status"] for m in parsed.matches) == {"parsed": 2, "noise": 1}
    assert sum(r["lines"] for r in parsed.aggregates["signatures"]) == 1
    assert sum(r["lines"] for r in parsed.aggregates["levels"]) == 1
    assert sum(r["lines"] for r in parsed.aggregates["level_hours"]) == 1
    assert parsed.accounting[0].incident.selected_seed_total == 1
    assert parsed.accounting[0].balanced()
    row = parsed.aggregates["units"][0]
    metrics = coverage_metrics({**coverage_counters(row), "lines_total": row["lines_total"]}, 1)
    assert metrics["completeness"] is None and metrics["completeness_status"] == "scope_filtered"
    assert metrics["seed_completeness"] == metrics["scanned"] == 1
    assert metrics["extracted"] == pytest.approx(2 / 3, abs=0.0001)
    assert metrics["context_documents"] == 2 and metrics["source_window_documents"] == 1000
    assert metrics["selected_seed_documents"] == 1
    assert metrics["context_status"] == "complete_for_requested_windows"


def test_missing_context_explicit_and_not_fake_complete(library):  # noqa: F811
    parsed = parse_units([incident_unit(pending=True)], library)
    row = parsed.aggregates["units"][0]
    metrics = coverage_metrics({**coverage_counters(row), "lines_total": row["lines_total"]}, 1)
    assert metrics["context_pending_seeds"] == 1 and metrics["context_status"] == "pending"
    assert metrics["seed_completeness"] == 1 and metrics["completeness"] is None


def test_sealed_roundtrip_and_daily_replacement_do_not_sum_old_counts(tmp_path, library):  # noqa: F811
    store = FilePartitionStore(tmp_path / "parts", "incidents")
    artifact = incident_unit()
    for attempt in ("first", "late-arrival-replacement"):
        store.begin(DAY, attempt, library.provenance())
        store.put_unit(DAY, artifact.unit.id, artifact)
        store.seal(DAY, parse_units([artifact], library))
    assert store.get(DAY).version == 2
    (restored,) = store.units(DAY, 2)
    assert restored.incident == artifact.incident
    assert len(restored.records) == 3
    assert store.get(DAY).accounting["coverage"]["source_accounting"] == "scope_filtered"
    assert store.get(DAY).accounting["coverage"]["heads"] == 3


def test_incident_cannot_replace_full_source_under_same_partition_key(tmp_path, library):  # noqa: F811
    store = FilePartitionStore(tmp_path / "parts", "ordinary")
    raw = source(compressed=False)
    store.begin(DAY, "ordinary", library.provenance())
    store.put_unit(DAY, raw.unit.id, raw)
    store.seal(DAY, parse_units([raw], library))
    store.begin(DAY, "narrow", library.provenance())
    artifact = incident_unit()
    with pytest.raises(PartitionConflict, match="separate partition"):
        store.put_unit(DAY, artifact.unit.id, artifact)
    assert store.get(DAY).version == 1


@pytest.mark.parametrize(
    "change", ["duplicate", "unknown_member", "grouped", "source_total", "window", "assembled", "extrapolate"]
)
def test_incident_provenance_mutation_fails_closed(change):
    artifact = incident_unit()
    if change == "duplicate":
        artifact.records.append(artifact.records[0])
    if change == "unknown_member":
        artifact.incident.context_windows[0].record_ids[-1] = "absent"
    if change == "grouped":
        artifact.grouped = source().grouped
    if change == "source_total":
        artifact.count.total = 999
    if change == "window":
        artifact.incident.window_end = HOUR
    if change == "assembled":
        artifact.multiline["joined"] = 1
    if change == "extrapolate":
        artifact.sample.method = "random_score"
    with pytest.raises((ValueError, KeyError)):
        artifact.validate_grouped()


def test_two_seeds_share_one_context_query_without_duplicate_counts(library):  # noqa: F811
    artifact = incident_unit()
    ids = [r.id for r in artifact.records[:2]]
    artifact.incident.seed_record_ids = ids
    artifact.incident.selected_seed_total = 2
    artifact.incident.context_windows[0].seed_record_ids = ids
    artifact.validate_grouped()
    parsed = parse_units([artifact], library)
    assert sum(r["lines"] for r in parsed.aggregates["signatures"]) == 2
    assert len(artifact.incident.context_windows) == 1
    assert artifact.incident.counters()["context_documents"] == 1
    assert artifact.incident.counters()["context_pending_seeds"] == 0
