"""Partition clock and timezone mapping (addendum A1 §3.8.2): index-date partitions keep the source's
day; event-time partitions cut the reporting day in ``logs.timezone`` (23/25 hours across DST) out
of the UTC index days it overlaps, and count exactly the lines inside that window."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from su_multi_arm.ingestion.logs_compose import cutoff, day_window, window_keys
from tests.logs_support import ENGINE, Clock, cluster_for, pipeline
from tests.pattern_fixtures import engine_or_skip

UTC = timezone.utc


def test_index_date_clock_is_the_source_day():
    window = day_window(date(2026, 1, 5), "index_date", "America/New_York", "UTC")
    assert (window.start, window.end) == (datetime(2026, 1, 5, tzinfo=UTC), datetime(2026, 1, 6, tzinfo=UTC))
    assert window.index_dates == [date(2026, 1, 5)] and window.date_basis == "index_date" and window.timezone == "UTC"


def test_event_time_clock_reads_two_utc_index_days_for_a_western_zone():
    window = day_window(date(2026, 1, 5), "event_time", "America/New_York", "UTC")
    assert window.start == datetime(2026, 1, 5, 5, tzinfo=UTC) and window.end == datetime(2026, 1, 6, 5, tzinfo=UTC)
    assert window.index_dates == [date(2026, 1, 5), date(2026, 1, 6)]
    assert window.date_basis == "event_time" and window.timezone == "America/New_York"


def test_event_time_clock_for_an_eastern_zone():
    window = day_window(date(2026, 1, 5), "event_time", "Asia/Kolkata", "UTC")
    assert window.start == datetime(2026, 1, 4, 18, 30, tzinfo=UTC) and window.index_dates == [date(2026, 1, 4), date(2026, 1, 5)]


def test_event_time_utc_is_one_index_day():
    window = day_window(date(2026, 1, 5), "event_time", "UTC", "UTC")
    assert window.index_dates == [date(2026, 1, 5)] and window.hours == 24


@pytest.mark.parametrize(("day", "hours"), [(date(2026, 3, 8), 23), (date(2026, 11, 1), 25), (date(2026, 6, 1), 24)])
def test_dst_days_are_23_or_25_hours(day, hours):
    assert day_window(day, "event_time", "America/New_York", "UTC").hours == hours


def test_window_keys_and_cutoff():
    assert window_keys(date(2026, 1, 10), 3) == ["2026-01-08", "2026-01-09", "2026-01-10"]
    assert cutoff(date(2026, 1, 11), 10) == "2026-01-02"


def test_keep_days_is_the_n_most_recent_complete_days():
    """``keep_days: 10`` keeps exactly 10 index dates, ``today - 10`` .. ``today - 1``; today (not
    complete) is not counted. Day 1 is expired by the run on day 12, and the window retention keeps
    is the window the daily run for yesterday composes."""
    from su_multi_arm.ingestion.logs_compose import expiry_cutoff

    today = date(2026, 1, 12)
    kept = [k for k in (date(2026, 1, d).isoformat() for d in range(1, 13)) if expiry_cutoff(today, 10) <= k < today.isoformat()]
    assert kept == [date(2026, 1, d).isoformat() for d in range(2, 12)] and len(kept) == 10
    assert expiry_cutoff(date(2026, 1, 11), 10) == "2026-01-01"  # day 1 still kept on day 11
    assert window_keys(today - (today - date(2026, 1, 11)), 10) == kept
    assert expiry_cutoff(today, 1) == "2026-01-11"  # keep_days: 1 keeps yesterday only


def test_event_time_partition_counts_exactly_its_local_day(tmp_path):
    engine_or_skip(ENGINE)
    days = [date(2026, 1, 4), date(2026, 1, 5), date(2026, 1, 6)]
    cluster = cluster_for(days, lines=24)
    pipe, cfg, backend = pipeline(tmp_path, cluster, now=Clock(date(2026, 1, 7)), timezone="America/New_York", partitions={"clock": "event_time"})
    report = pipe.run_daily(date(2026, 1, 5))
    window = pipe.window(date(2026, 1, 5))
    expected = sum(
        1
        for docs in cluster.indices.values()
        for doc in docs
        if window.start <= datetime.fromisoformat(doc["_source"]["@timestamp"].replace("Z", "+00:00")) < window.end
    )
    manifest = pipe.store.get("2026-01-05")
    assert manifest.clock == "event_time" and manifest.timezone == "America/New_York"
    assert sum(c["total"] for c in manifest.unit_counts.values()) == expected
    indices = {c["index"] for c in manifest.unit_counts.values()}
    assert any(i.endswith("2026.01.05") for i in indices) and any(i.endswith("2026.01.06") for i in indices)
    assert not any(i.endswith("2026.01.04") for i in indices)
    from su_multi_arm.core.types import Page
    from su_multi_arm.ingestion.release import generation_id

    gen = generation_id(backend.generation_registry().manifest(report.steps["compose"]["generation"]))
    unit_days = [d for d in backend.catalog.iter_scope(gen, None, Page(size=500)).items if d.kind == "unit_day"]
    assert sum(d.metadata["lines_total"] for d in unit_days) == expected
    assert {d.metadata["date"] for d in unit_days} == {"2026-01-05"}
    assert {(d.metadata["date_basis"], d.metadata["date_timezone"]) for d in unit_days} == {("event_time", "America/New_York")}
    assert all("(event_time, America/New_York)" in d.fields["header"] for d in unit_days)
    # every pulled record lies inside the local day
    for artifacts in pipe.store.units("2026-01-05"):
        for record in artifacts.records:
            assert window.start <= record.ts < window.end


def test_run_daily_default_day_is_yesterday_in_the_reporting_zone(tmp_path):
    engine_or_skip(ENGINE)
    cluster = cluster_for([date(2026, 1, 5)], lines=5)
    clock = Clock(date(2026, 1, 6))
    clock.value = datetime(2026, 1, 6, 3, tzinfo=UTC)  # still 2026-01-05 in New York
    pipe, _, _ = pipeline(tmp_path, cluster, now=clock, timezone="America/New_York")
    assert pipe.today() == date(2026, 1, 5)
