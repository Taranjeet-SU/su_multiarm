"""Caps and stratified sampling (addendum A1 §3.1.3): quotas sum to the cap, never exceed a stratum,
the plan records method, seed and per-stratum counts; pulls respect the quota and the partition cap
(marked ``capped``, never silently truncated). Fake cluster only."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from su_multi_arm.core.config_sources import PullConfig
from su_multi_arm.sources import PartitionBudget
from su_multi_arm.sources.probe import day_window
from su_multi_arm.sources.sampling import allocate, hour_bounds, plan
from tests.sources_support import wired

WINDOW = day_window("2026-01-05", "UTC")


@pytest.mark.parametrize(
    "cap, counts",
    [(10, {"00": 5, "01": 5, "02": 5}), (7, {"a": 1, "b": 100, "c": 3}), (100, {"x": 50, "y": 20}), (1, {"a": 3, "b": 3}), (13, {f"{h:02d}": h for h in range(24)})],
)
def test_allocate_sums_to_the_cap_and_respects_each_stratum(cap, counts):
    quotas = allocate(cap, counts)
    assert sum(quotas.values()) == min(cap, sum(counts.values()))
    assert all(0 <= quotas[k] <= counts[k] for k in counts)
    assert quotas == allocate(cap, dict(reversed(list(counts.items()))))


def test_hour_bounds_cover_the_window():
    bounds = hour_bounds(WINDOW.start, WINDOW.end)
    assert len(bounds) == 24 and bounds[0][1] == WINDOW.start and bounds[-1][2] == WINDOW.end


def test_plan_is_exact_under_the_cap_and_labelled_when_sampled():
    exact = plan("hourly_quota", cap=100, seed=3, unit_total=40)
    assert exact.method == "none" and not exact.approximate and exact.label() == "exact"
    sampled = plan("random_score", cap=10, seed=3, unit_total=40)
    assert sampled.approximate and sampled.label() == "approximate (random_score, seed 3)"
    with pytest.raises(ValueError):
        plan("hourly_quota", cap=10, seed=3, unit_total=40)


def test_pull_config_bounds():
    with pytest.raises(ValidationError, match="keepalive_max"):
        PullConfig(keepalive_max="11m")
    with pytest.raises(ValidationError, match="keepalive must not exceed"):
        PullConfig(keepalive="3m", keepalive_max="2m")
    with pytest.raises(ValidationError, match="tiebreaker"):
        PullConfig(cursor="search_after")
    with pytest.raises(ValidationError):
        PullConfig(keepalive="1h")


def first_unit(source, service="query-gateway"):
    units = source.discover(WINDOW)
    return next(u for u in units if u.attributes["service"] == service and u.attributes["tenant"] == "t001")


def test_hourly_quota_records_strata_and_fetches_exactly_the_cap():
    source, cluster, day = wired(lines=400, pull={"records_query": "errors", "max_docs_per_unit": 12, "page_size": 5, "seed": 11})
    unit = first_unit(source)
    sample = source.plan_sample(unit)
    errors = day.truth[unit.index].by_level["ERROR"]
    assert sample.method == "hourly_quota" and sample.unit_total == errors > 12
    assert sum(s.count for s in sample.strata) == errors and sum(s.quota for s in sample.strata) == 12
    assert sample.strata[-1].key == "outside"
    docs = [d for batch in source.pull(unit, sample=sample) for d in batch.docs]
    assert len(docs) == 12 == sum(s.fetched for s in sample.strata)
    assert all(s.fetched == s.quota for s in sample.strata)
    assert all(d.fields.get("level") == "ERROR" for d in docs)
    assert len({d.source_id for d in docs}) == 12
    # deterministic: the same seed selects the same documents
    again, _, _ = wired(lines=400, pull={"records_query": "errors", "max_docs_per_unit": 12, "page_size": 5, "seed": 11})
    unit2 = first_unit(again)
    assert [d.source_id for b in again.pull(unit2) for d in b.docs] == [d.source_id for d in docs]
    assert not source.cursors.open_cursors() and not source.transport.own_ids


def test_random_score_method_caps_the_unit():
    source, _, _ = wired(lines=300, pull={"max_docs_per_unit": 25, "sample_method": "random_score", "seed": 5})
    unit = first_unit(source)
    sample = source.plan_sample(unit)
    assert sample.method == "random_score" and [s.key for s in sample.strata] == ["all"]
    docs = [d for b in source.pull(unit, sample=sample) for d in b.docs]
    assert len(docs) == 25 and sample.strata[0].fetched == 25


def test_partition_budget_stops_the_pull_and_marks_it_capped():
    source, cluster, day = wired(lines=60, pull={"page_size": 30, "max_docs_per_unit": 100, "max_docs_per_partition": 100})
    units = source.discover(WINDOW)
    budget = source.partition_budget()
    assert isinstance(budget, PartitionBudget) and budget.limit == 100
    batches = []
    for unit in units:
        batches.extend(source.pull(unit, budget=budget))
        if budget.capped:
            break
    assert len(day.indices[units[0].index]) < 100  # the first unit completes, the second is cut
    assert budget.fetched == 100 and budget.capped and budget.capped_units == [units[1].id]
    assert batches[-1].capped and batches[-1].done
    assert sum(len(b.docs) for b in batches) == 100
    assert not cluster.scrolls, "every scroll context was closed"


def test_window_dates_and_slices():
    source, _, _ = wired(indices=["t001-prod-gateway-*"])
    units = source.discover(WINDOW)
    assert units and all(u.slice == (WINDOW.start, WINDOW.end) for u in units)  # undated: event-time slice
    dated, _, _ = wired()
    assert all(u.slice is None for u in dated.discover(WINDOW))
    assert WINDOW.start == datetime(2026, 1, 5, tzinfo=timezone.utc)
