"""One endpoint over provisional and archived days (plan K I1): ``issue_history`` through the
engine, the HTTP tool route and the rules decision. Every unclosed day is served provisional, a day
moves to the archive only with the publish that serves it, gap days are reported as gaps, 400 days
aggregate without loading rows, and random refresh/close/publish interleavings keep every day in
exactly one tier with sums equal to the ground truth. Synthetic data only."""

from __future__ import annotations

import json
import random
import tracemalloc
from datetime import date, datetime, timedelta

import pytest

from su_multi_arm.engine import Engine
from su_multi_arm.ingestion.episode_archive import ARCHIVE_KINDS, EpisodeArchive
from su_multi_arm.ingestion.episode_summaries import summary_documents
from su_multi_arm.ingestion.provisional import open_provisional
from tests.history_support import configure, occurrences, rolling, stage, utc
from tests.log_policy_support import RULES, day_snapshots, days, snapshot


def call(engine, args):
    return engine.call_tool("issue_history", args).result.output


def by_day(engine, first, last, **extra):
    out = call(engine, {"start": first, "end": last, "as_of": last, "group_by": ["day"], **extra})
    return out, {r["day"]: r["occurrences"] for r in out.rows}


def archive_at(config, now, store=None):
    return EpisodeArchive(config, now=lambda: now, store=store)


@pytest.fixture
def three_archived(tmp_path):
    """Days 2026-01-01..03 archived and published (as of 2026-01-04 02:00)."""
    config = configure(tmp_path)
    engine = Engine(config)
    now = utc("2026-01-04T02:00:00")
    archive = archive_at(config, now)
    for i, day in enumerate(days("2026-01-01", 3)):
        archive.close_day(day, day_snapshots(day, i))
    archive.publish(engine.write_path, now.date())
    return engine, archive, tmp_path


def test_six_unclosed_days_are_all_served_provisional_and_flagged(three_archived):
    engine, archive, tmp_path = three_archived
    unclosed = days("2026-01-04", 6)
    truth = {}
    for i, day in enumerate(unclosed):
        snaps = [rolling(day, t, "ml-serving", [("ml.model_unavailable", 30 + i, 2 + i), ("net.refused", 900, 1)], until_minute=20 * 60) for t in ("tenant-a", "tenant-b")]
        stage(tmp_path / "staging", day, snaps)
        truth[day] = occurrences(snaps)
    report = archive_at(engine.config, utc("2026-01-09T20:00:00"), archive.store).refresh_provisional()
    assert sorted(report["days"]) == unclosed and report["put"] == 12
    out, counts = by_day(engine, "2026-01-01", "2026-01-09")
    coverage = out.history["coverage"]
    assert coverage["archive_days"] == 3 and sorted(coverage["provisional_days"]) == unclosed
    assert out.source_unavailable == [] and coverage["missing_days"] == []
    archived_truth = {d: occurrences(day_snapshots(d, i)) for i, d in enumerate(days("2026-01-01", 3))}
    assert counts == {**{d: n for d, n in archived_truth.items() if n}, **truth}
    flags = {r["day"]: (r["provisional"], r["tiers"]) for r in out.rows}
    assert all(flags[d] == (True, ["provisional"]) for d in unclosed)
    assert all(flags[d] == (False, ["archive"]) for d in archived_truth if d in flags)
    assert any("Provisional days" in n for n in out.notes)
    listed = call(engine, {"start": "2026-01-01", "end": "2026-01-09", "as_of": "2026-01-09", "list_episodes": 5}).history["episodes"]
    assert listed and listed[0]["tier"] == "provisional" and listed[0]["provisional"] is True
    assert listed == sorted(listed, key=lambda p: (p["last_failure"], p["doc_id"]), reverse=True)


def test_default_range_is_default_view_days(three_archived):
    engine, archive, tmp_path = three_archived
    for i, day in enumerate(days("2026-01-04", 3)):
        stage(tmp_path / "staging", day, [rolling(day, "tenant-a", "ml-serving", [("net.refused", 10, 1)], until_minute=600)])
    archive_at(engine.config, utc("2026-01-06T12:00:00"), archive.store).refresh_provisional()
    out = call(engine, {"as_of": "2026-01-06"})
    assert out.window["start"] == "2026-01-04" and out.window["end"] == "2026-01-06" and out.window["label"].startswith("last 3 days")
    from fastapi.testclient import TestClient

    from su_multi_arm.serving.http import create_app

    with TestClient(create_app(engine=engine)) as client:
        caps = client.get("/v2/capabilities").json()
        response = client.post("/v2/tools/issue_history", json={"as_of": "2026-01-06"})
    assert caps["logs"]["default_view_days"] == 3 and "issue_history" in caps["tools"]
    assert response.status_code == 200 and response.json()["result"]["output"] == out.model_dump(mode="json")


def test_a_refresh_with_some_scopes_keeps_the_other_scopes(three_archived):
    engine, archive, tmp_path = three_archived
    day = "2026-01-04"
    a1 = rolling(day, "tenant-a", "ml-serving", [("net.refused", 10, 1), ("net.refused", 700, 5)], until_minute=600)
    b1 = rolling(day, "tenant-b", "ml-serving", [("ml.training_failed", 20, 7)], until_minute=600)
    folder = stage(tmp_path / "staging", day, [a1, b1])
    refresh = archive_at(engine.config, utc("2026-01-04T10:00:00"), archive.store).refresh_provisional
    refresh()
    (folder / f"{b1['scope']['index']}.json").unlink()  # this refresh carries tenant-a only
    a2 = rolling(day, "tenant-a", "ml-serving", [("net.refused", 10, 1), ("net.refused", 700, 5)], until_minute=800)
    stage(tmp_path / "staging", day, [a2])
    report = archive_at(engine.config, utc("2026-01-04T14:00:00"), archive.store).refresh_provisional()
    assert report["days"][day]["rebuilt"] and report["days"][day]["scopes"] == 2
    store = open_provisional(engine.config)
    versions = {r["scope"]: r["version"] for r in store.latest(day)}
    assert versions == {a1["scope"]["index"]: 2, b1["scope"]["index"]: 1}
    _, counts = by_day(engine, day, day)
    assert counts == {day: 1 + 5 + 7}
    # an unchanged refresh is not a new version and not a rebuild
    again = archive_at(engine.config, utc("2026-01-04T14:15:00"), archive.store).refresh_provisional()
    assert again["days"][day]["rebuilt"] is False


def test_boundary_transition_never_double_counts(three_archived):
    engine, archive, tmp_path = three_archived
    day = "2026-01-04"
    rolling_snaps = [rolling(day, "tenant-a", "ml-serving", [("ml.model_unavailable", 1439, 3), ("ml.model_unavailable", 30, 2)], until_minute=1200)]
    final = [snapshot(day, "tenant-a", "ml-serving", [("ml.model_unavailable", 1439, 3), ("ml.model_unavailable", 30, 2)])]
    nxt = [rolling("2026-01-05", "tenant-a", "ml-serving", [("ml.model_unavailable", 0, 4)], until_minute=60)]
    stage(tmp_path / "staging", day, rolling_snaps)
    archive_at(engine.config, utc("2026-01-04T20:00:00"), archive.store).refresh_provisional()
    _, before = by_day(engine, "2026-01-01", day)
    assert before[day] == 2  # the rolling capture (until 20:00)
    stage(tmp_path / "staging", day, final)
    stage(tmp_path / "staging", "2026-01-05", nxt)
    archive_at(engine.config, utc("2026-01-05T01:10:00"), archive.store).refresh_provisional()
    out, provisional_counts = by_day(engine, "2026-01-04", "2026-01-05")
    assert provisional_counts == {day: 5, "2026-01-05": 4}
    assert out.history["coverage"]["tiers"] == {day: "provisional", "2026-01-05": "provisional"}
    episodes = sum(r["episodes"] for r in out.rows)
    assert episodes == 2  # 00:30, and 23:59 continued at 2026-01-05 00:00 (provisional against provisional): one chain
    # close D: archived but not yet served by a generation -> still read from the provisional store
    closer = archive_at(engine.config, utc("2026-01-05T02:00:00"), archive.store)
    report = closer.import_staging()
    assert [d["day"] for d in report["days"]] == [day] and report["days"][0]["closure_delta"]["closed_occurrences"] == 5
    out, counts = by_day(engine, "2026-01-01", "2026-01-05")
    assert out.history["coverage"]["tiers"][day] == "provisional" and counts[day] == 5
    # the publish that serves D from the archive also drops its provisional entry
    published = closer.publish(engine.write_path, date(2026, 1, 5))
    assert published["provisional_dropped"] == [day]
    assert day not in open_provisional(engine.config).current()
    out, counts = by_day(engine, "2026-01-01", "2026-01-05")
    assert out.history["coverage"]["tiers"][day] == "archive" and counts[day] == 5 and counts["2026-01-05"] == 4
    whole = call(engine, {"start": "2026-01-04", "end": "2026-01-05", "as_of": "2026-01-05", "group_by": []})
    assert (whole.rows[0]["episodes"], whole.rows[0]["occurrences"], whole.rows[0]["tiers"]) == (2, 9, ["archive", "provisional"])
    # the next refresh re-links 2026-01-05 against the archived boundary: the chain stays one episode
    report = archive_at(engine.config, utc("2026-01-05T03:00:00"), archive.store).refresh_provisional()
    assert report["days"]["2026-01-05"]["rebuilt"] is True  # its boundary moved from provisional to archive
    again = call(engine, {"start": "2026-01-04", "end": "2026-01-05", "as_of": "2026-01-05", "group_by": []})
    assert (again.rows[0]["episodes"], again.rows[0]["occurrences"]) == (2, 9)
    alone = call(engine, {"start": "2026-01-05", "end": "2026-01-05", "as_of": "2026-01-05", "group_by": [], "list_episodes": 3})
    assert alone.rows[0]["episodes"] == 1  # the chain's head before the range counts once inside it
    [piece] = alone.history["episodes"]
    assert piece["is_continuation"] and piece["continues_before_window"] and piece["tier"] == "provisional"


def test_gap_days_are_reported_as_gaps_not_zero(three_archived):
    engine, archive, _ = three_archived
    later = archive_at(engine.config, utc("2026-01-06T02:00:00"), archive.store)
    later.record_gap("2026-01-04", "synthetic source outage")
    later.close_day("2026-01-05", day_snapshots("2026-01-05", 4))
    later.publish(engine.write_path, date(2026, 1, 6))
    out, counts = by_day(engine, "2026-01-01", "2026-01-06")
    assert "2026-01-04" not in counts
    assert out.history["coverage"]["gap_days"] == {"2026-01-04": "synthetic source outage"}
    assert out.history["coverage"]["missing_days"] == ["2026-01-06"]
    assert out.source_unavailable == ["2026-01-04", "2026-01-06"]
    text = " ".join(out.notes)
    assert "collection gaps (not zero incidents): 2026-01-04" in text and "without served data (not zero incidents): 2026-01-06" in text


def test_issue_history_is_operator_only(three_archived):
    from su_multi_arm.runtime.acl import Principal, StaticPolicy

    engine, _, _ = three_archived
    engine.acl = StaticPolicy("tenant")
    principal = Principal(id="reader", tenant="tenant-a", scopes=["search", "answer", "tools"])
    out = engine.call_tool("issue_history", {"start": "2026-01-01", "end": "2026-01-03", "as_of": "2026-01-03"}, principal=principal).result.output
    assert out.status == "refused" and "operator-only" in out.refusal


def test_four_hundred_days_aggregate_without_loading_rows(tmp_path, monkeypatch):
    config = configure(tmp_path, provisional=False)
    engine = Engine(config)
    now = datetime.fromisoformat("2027-02-06T02:00:00+00:00")
    archive = archive_at(config, now)
    all_days = days("2026-01-01", 400)
    expected = 0
    for i, day in enumerate(all_days):
        snaps = day_snapshots(day, i)
        expected += occurrences(snaps)
        archive.close_day(day, snaps)
    archive.publish(engine.write_path, now.date())
    store = engine.history_route(engine.write_path.current()[0])[0]

    def no_row_loading(*_a, **_k):
        raise AssertionError("issue_history must not read EAV rows")

    monkeypatch.setattr(type(store), "execute", no_row_loading)
    monkeypatch.setattr("su_multi_arm.ingestion.history.MAX_MATCHED", 10)  # would refuse any row listing
    tracemalloc.start()
    try:
        out = call(engine, {"start": all_days[0], "end": all_days[-1], "as_of": all_days[-1]})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert out.status == "bounded" and out.history["coverage"]["archive_days"] == 400
    assert sum(r["occurrences"] for r in out.rows) == expected
    assert sum(r["episodes"] for r in out.rows) == 400 + len([n for n in range(400) if n % 3 == 0])
    assert out.history["rows_read"] == len(out.rows) == 3  # three issue groups, nothing else loaded
    assert peak < 5_000_000, peak
    months = call(engine, {"start": all_days[0], "end": all_days[-1], "as_of": all_days[-1], "group_by": ["month"], "top_n": 500})
    assert len(months.rows) == 14 and sum(r["occurrences"] for r in months.rows) == expected
    assert sum(r["active_days"] for r in months.rows) == 400


def test_rules_route_a_three_month_outage_count_to_issue_history(tmp_path):
    from su_multi_arm.models import SearchRequest

    config = configure(tmp_path, provisional=False)
    data = config.model_dump(mode="json")
    for f in data["metadata_schema"]["fields"]:
        if f["name"] in {"category", "tenant", "service"}:
            f["listable"] = True
    from su_multi_arm.core.config import validate_config

    config = validate_config(data)
    engine = Engine(config)
    now = utc("2026-04-01T02:00:00")
    archive = archive_at(config, now)
    for i, day in enumerate(days("2026-01-01", 90)):
        archive.close_day(day, day_snapshots(day, i))
    archive.publish(engine.write_path, now.date())
    result = engine.answer(SearchRequest(query="How many ML outages were there in the last 3 months and what was the reason?", as_of="2026-03-31"))
    [record] = result.retrieval.aggregates
    assert record["intent"] == "issue_history"
    assert record["args"]["category"] == ["ml_serving", "ml_training"]
    categories = {r["category"] for r in record["rows"]}
    assert categories == {"ml_serving", "ml_training"}
    assert "classified signal, not an established cause" in " ".join(record["notes"])


@pytest.mark.parametrize("seed", [1, 7, 23])
def test_random_refresh_close_publish_interleavings_keep_one_tier_per_day(tmp_path, seed):
    """Property (seeded): after every step each day of the range is in exactly one tier, and the
    per-day and range sums equal the ground truth of what that tier was built from."""
    rng = random.Random(seed)
    config = configure(tmp_path)
    engine = Engine(config)
    first = date(2026, 1, 1)
    finals: dict[str, list] = {}
    events_of: dict[str, list] = {}
    for i in range(12):
        day = (first + timedelta(days=i)).isoformat()
        events = []
        for rule, minute in (("ml.model_unavailable", 0), ("ml.model_unavailable", 1439), ("net.refused", 600 + i)):
            if rng.random() < 0.7:
                events.append((rule, minute, rng.randint(1, 5)))
        finals[day] = [snapshot(day, "tenant-a", "ml-serving", events)]
        events_of[day] = events
    seed_archive = archive_at(config, utc("2026-01-02T02:00:00"))
    seed_archive.close_day("2026-01-01", finals["2026-01-01"])
    seed_archive.publish(engine.write_path, date(2026, 1, 2))
    refreshed: dict[str, int] = {}
    seen: set[str] = set()
    for t in range(1, 12):
        today = first + timedelta(days=t)
        now = utc(today.isoformat()).replace(hour=2)
        yesterday, today_s = (today - timedelta(days=1)).isoformat(), today.isoformat()
        stage(tmp_path / "staging", yesterday, finals[yesterday])
        stage(tmp_path / "staging", today_s, [rolling(today_s, "tenant-a", "ml-serving", events_of[today_s], until_minute=120)])
        step = archive_at(config, now, seed_archive.store)
        if rng.random() < 0.7:
            step.refresh_provisional()
            archived = step.store.days()
            for d in (tmp_path / "staging").iterdir():
                if d.name not in archived:
                    refreshed[d.name] = occurrences([json.loads(p.read_text()) for p in d.glob("*.json")])
        if rng.random() < 0.5:
            step.import_staging()
            if rng.random() < 0.7:
                step.publish(engine.write_path, today)
        out, counts = by_day(engine, "2026-01-01", today_s)
        tiers = out.history["coverage"]["tiers"]
        seen.update(tiers.values())
        assert sorted(tiers) == [(first + timedelta(days=k)).isoformat() for k in range(t + 1)]  # every day, one tier each
        manifest = engine.write_path.registry.manifest(engine.write_path.registry.current()["generation"])
        served = set(manifest["logs"]["history"]["shards"])
        provisional = open_provisional(config).current()
        for d, tier in tiers.items():
            if d in served:
                assert tier == "archive" and counts.get(d, 0) == occurrences(finals[d])
            elif d in provisional:
                assert tier == "provisional" and counts.get(d, 0) == refreshed[d]
            else:
                assert tier == "missing" and d not in counts
        whole = call(engine, {"start": "2026-01-01", "end": today_s, "as_of": today_s, "group_by": []})
        assert sum(r["occurrences"] for r in whole.rows) == sum(counts.values())
    assert {"archive", "provisional"} <= seen


def _archive_of(tmp_path, n):
    config = configure(tmp_path, provisional=True)
    engine = Engine(config)
    first = date(2026, 1, 1)
    now = utc((first + timedelta(days=n)).isoformat()).replace(hour=2)
    archive = archive_at(config, now)
    for i in range(n):
        day = (first + timedelta(days=i)).isoformat()
        archive.close_day(day, day_snapshots(day, i))
    archive.publish(engine.write_path, now.date())
    return engine, archive, (first + timedelta(days=n)).isoformat()


def test_publishing_day_n_plus_one_does_one_days_work(tmp_path, monkeypatch):
    """Coordinator requirement (measured: a full search-generation rebuild costs ~249 s per kept
    day): adding day N+1 to the served history writes one day's rows and advances the archive
    pointer; no generation is rebuilt and nothing scales with N. The live search generation build
    stays bounded by keep_days (the same document count for N = 10 and N = 40)."""
    live = {}
    for n in (10, 40):
        engine, archive, nxt = _archive_of(tmp_path / f"n{n}", n)
        ingested: list[int] = []
        original = engine.write_path.ingest

        def counting(docs, _original=original, _seen=ingested, **kw):
            _seen.append(len(docs))
            return _original(docs, **kw)

        monkeypatch.setattr(engine.write_path, "ingest", counting)
        writes = archive.store.writes
        later = archive_at(engine.config, utc(nxt).replace(hour=2) + timedelta(days=1), archive.store)
        result = later.close_day(nxt, day_snapshots(nxt, n))
        one_day = len([d for d in summary_documents("synthetic-archive", day_snapshots(nxt, n), RULES, citations=True) if d.kind in ARCHIVE_KINDS])
        assert archive.store.writes - writes == result["rows"] == one_day
        served = later.serve()
        assert ingested == [] and served["pointer"]["seq"] == n + 1
        out = call(engine, {"start": nxt, "end": nxt, "as_of": nxt})
        assert out.history["coverage"]["tiers"] == {nxt: "archive"} and out.rows
        assert engine.call_tool("incident_episodes", {"start": nxt, "end": nxt, "as_of": nxt}).result.output.rows  # every archive read follows the pointer
        later.publish(engine.write_path, date.fromisoformat(nxt) + timedelta(days=1))
        assert len(ingested) == 1
        live[n] = ingested[0]
    assert live[10] == live[40]  # the live search generation is bounded by keep_days, not by N
