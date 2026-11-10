"""I1 of plan K (unified_su_kg_phase1 rev 2): the typed day tables of the episode archive, the
cross-midnight count rule (with a parity test against the read-time ``stitch``), the streaming
(bounded-memory) close, partial and overdue closes, the row-hash audit and the tiered aggregate's
refusals. Synthetic data only."""

from __future__ import annotations

import json
import random
import sqlite3
import tracemalloc
from datetime import date

import pytest

from su_multi_arm.core.config import validate_config
from su_multi_arm.ingestion.day_spill import DaySpill
from su_multi_arm.ingestion.episode_archive import ARCHIVE_KINDS, ArchiveRefused, EpisodeArchive, trim
from su_multi_arm.ingestion.episode_history import HistorySpec, HistoryUnavailable, TieredAggregate, link_continuations, tier_map
from su_multi_arm.ingestion.episode_summaries import summary_documents
from su_multi_arm.ingestion.history import content_shard_id
from su_multi_arm.runtime.episode_tools import stitch
from tests.history_support import rolling, stage, utc
from tests.log_policy_support import RULES, day_snapshots, days, policy_logs, snapshot, write_family
from tests.logs_gold_support import gold_config


def make_config(tmp, **collection):
    sha = write_family(tmp / "policy" / "ml.yaml")
    data = gold_config(tmp)
    data["logs"].update(policy_logs(tmp, sha, **collection))
    return validate_config(data)


def archive_at(config, now, store=None):
    return EpisodeArchive(config, now=lambda: now, store=store)


def aggregate(archive, first, last, today=None, **spec):
    manifest = {"logs": archive.manifest_section(today or date.fromisoformat(last))}
    tiers = tier_map(manifest, {}, first, last)
    return TieredAggregate(archive.store.path, tiers).execute(HistorySpec(first=first, last=last, **spec))


def total(result):
    return sum(g["episodes"] for g in result.groups), sum(g["occurrences"] for g in result.groups)


# --- config ------------------------------------------------------------------------------------


def test_i1_keys_default_to_the_behaviour_before(tmp_path):
    config = make_config(tmp_path)
    policy = config.logs.collection
    assert policy.schedule.close_after_days == 1 and policy.schedule.grace_days == 2
    assert policy.provisional.enabled is False and policy.serving.default_view_days == 3


@pytest.mark.parametrize(
    "collection,message",
    [
        ({"provisional": {"enabled": True}, "archive": {"enabled": False}}, "needs archive.enabled"),
        ({"serving": {"max_window_days": 2, "default_view_days": 3}}, "default_view_days must not exceed"),
    ],
)
def test_i1_config_refusals(tmp_path, collection, message):
    with pytest.raises(Exception, match=message):
        make_config(tmp_path, **collection)


def test_provisional_path_must_not_overlap_staging(tmp_path):
    with pytest.raises(Exception, match="must not contain each other"):
        make_config(tmp_path, provisional={"enabled": True, "path": str(tmp_path / "staging" / "prov")})


# --- streaming close ---------------------------------------------------------------------------


def test_spilled_builder_equals_summary_documents(tmp_path):
    snaps = [*day_snapshots("2026-01-01", 0), snapshot("2026-01-01", "tenant-c", "ml-serving", [("net.refused", 5, 2), ("net.refused", 9, 1)])]
    expected = {d.id: d.model_dump_json() for d in summary_documents("ds", snaps, RULES, citations=True)}
    with DaySpill(tmp_path / "x.spill") as spill:
        spill.build("ds", RULES, iter(snaps), citations=True)
        got = {d.id: d.model_dump_json() for d in spill.documents()}
    assert got == expected


def test_streamed_close_writes_the_shard_the_list_close_wrote(tmp_path):
    config = make_config(tmp_path, archive={"max_text_chars": 64})
    snaps = day_snapshots("2026-01-01", 0)
    docs = [trim(d, 64) for d in summary_documents(config.dataset, snaps, RULES, citations=True) if d.kind in ARCHIVE_KINDS]
    result = archive_at(config, utc("2026-01-03T02:00:00")).close_day("2026-01-01", iter(snaps))
    assert result["shard"] == content_shard_id("2026-01-01", docs)  # unchanged content address
    assert result["rows"] == len(docs)


def test_close_memory_is_bounded_by_one_snapshot_not_the_day(tmp_path):
    config = make_config(tmp_path)
    day = "2026-01-01"
    folder = tmp_path / "big"
    folder.mkdir()
    big = "x" * 40_000
    for i in range(120):
        snap = snapshot(day, f"tenant-{i:03d}", "ml-serving", [("ml.model_unavailable", 600 + i % 30, 3)])
        for row in snap["episodes"]:
            row["evidence"] = [{"id": f"src-{i}", "ts": row["first_failure"], "message": f"{i} {big}"}]
        snap["unclassified_examples"] = [{"message": f"u{i} {big}"}]
        (folder / f"{i:03d}.json").write_text(json.dumps(snap))
    files = sorted(folder.glob("*.json"))
    total_bytes = sum(p.stat().st_size for p in files)
    archive = archive_at(config, utc("2026-01-03T02:00:00"))
    tracemalloc.start()
    try:
        result = archive.close_day(day, (json.loads(p.read_text()) for p in files))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result["status"] == "archived" and result["scopes"] == 120
    assert total_bytes > 9_000_000
    # Python heap peak (tracemalloc; sqlite's own page cache is not traced): far below the day's input
    assert peak < total_bytes * 0.3, (peak, total_bytes)  # measured ~0.18; holding the day would exceed 1.0


# --- cross-midnight count rule ---------------------------------------------------------------


def chain_days(tmp_path):
    config = make_config(tmp_path)
    archive = archive_at(config, utc("2026-01-10T02:00:00"))
    d1 = [snapshot("2026-01-01", "tenant-a", "ml-serving", [("ml.model_unavailable", m, 1) for m in range(1430, 1440)])]
    d2 = [
        snapshot("2026-01-02", "tenant-a", "ml-serving", [("ml.model_unavailable", m, 2) for m in range(0, 1440, 4)]),
        snapshot("2026-01-02", "tenant-b", "ml-serving", [("ml.model_unavailable", 720, 5)]),
    ]
    d3 = [snapshot("2026-01-03", "tenant-a", "ml-serving", [("ml.model_unavailable", m, 3) for m in range(0, 10)])]
    for day, snaps in (("2026-01-01", d1), ("2026-01-02", d2), ("2026-01-03", d3)):
        archive.close_day(day, snaps)
    return archive


@pytest.mark.parametrize(
    "first,last,episodes",
    [("2026-01-01", "2026-01-03", 2), ("2026-01-01", "2026-01-01", 1), ("2026-01-02", "2026-01-02", 2), ("2026-01-03", "2026-01-03", 1), ("2026-01-02", "2026-01-03", 2)],
)
def test_a_chain_across_two_midnights_is_counted_once_per_range(tmp_path, first, last, episodes):
    archive = chain_days(tmp_path)
    result = aggregate(archive, first, last)
    occ = {"2026-01-01": 10, "2026-01-02": 360 * 2 + 5, "2026-01-03": 30}
    assert total(result) == (episodes, sum(n for d, n in occ.items() if first <= d <= last))  # occurrences window-exact


def test_continuation_columns_and_chain_bounds(tmp_path):
    archive = chain_days(tmp_path)
    db = sqlite3.connect(archive.store.path)
    db.row_factory = sqlite3.Row
    rows = {(r["day"], r["tenant"]): dict(r) for r in db.execute("SELECT * FROM episode_piece")}
    a1, a2, a3 = rows[("2026-01-01", "tenant-a")], rows[("2026-01-02", "tenant-a")], rows[("2026-01-03", "tenant-a")]
    assert a1["is_continuation"] == 0 and a1["boundary_unknown"] == 0  # 23:50 is not near midnight
    assert a2["continues_id"] == a1["doc_id"] and a2["chain_first"] == a1["first_failure"]
    assert a3["continues_id"] == a2["doc_id"] and a3["chain_first"] == a1["first_failure"]
    issue = {r["day"]: dict(r) for r in db.execute("SELECT * FROM issue_day WHERE tenant='tenant-a'")}
    assert (issue["2026-01-02"]["episodes_started"], issue["2026-01-02"]["episodes_continued"]) == (0, 1)
    listed = aggregate(archive, "2026-01-03", "2026-01-03", list_k=5).pieces
    assert listed[0]["continues_before_window"] is True and listed[0]["chain_first"].startswith("2026-01-01T23:50")


def test_a_gap_before_midnight_marks_the_boundary_unknown_and_counts_a_new_episode(tmp_path):
    config = make_config(tmp_path)
    archive = archive_at(config, utc("2026-01-10T02:00:00"))
    archive.close_day("2026-01-01", [snapshot("2026-01-01", "tenant-a", "ml-serving", [("net.refused", 1439, 1)])])
    archive.record_gap("2026-01-02", "synthetic capture outage")
    archive.close_day("2026-01-03", [snapshot("2026-01-03", "tenant-a", "ml-serving", [("net.refused", 0, 1)])])
    result = aggregate(archive, "2026-01-01", "2026-01-03")
    assert total(result) == (2, 2) and result.boundary_unknown == 1
    assert result.tiers["2026-01-02"].tier == "gap"


def test_link_continuations_matches_only_within_the_gap():
    tail = [{"tenant": "t", "env": "e", "service": "s", "signature_id": "r", "doc_id": "p", "last_failure": "2026-01-01T23:58:59+00:00", "chain_first": "2026-01-01T20:00:00+00:00"}]

    def piece(first, key="r"):
        return {"tenant": "t", "env": "e", "service": "s", "signature_id": key, "doc_id": first, "first_failure": first, "is_continuation": 0, "boundary_unknown": 0, "continues_id": None, "chain_first": first}

    near, far, other = piece("2026-01-02T00:03:00+00:00"), piece("2026-01-02T00:10:00+00:00"), piece("2026-01-02T00:01:00+00:00", "q")
    link_continuations("2026-01-02", [near, far, other], tail, 300)
    assert near["is_continuation"] == 1 and near["chain_first"] == "2026-01-01T20:00:00+00:00"
    assert far["is_continuation"] == 0 and other["is_continuation"] == 0
    unknown = piece("2026-01-02T00:02:00+00:00")
    link_continuations("2026-01-02", [unknown], None, 300)
    assert unknown["boundary_unknown"] == 1 and unknown["is_continuation"] == 0


def random_days(seed: int, n: int):
    rng = random.Random(seed)
    out = {}
    for i, day in enumerate(days("2026-02-01", n)):
        snaps = []
        for tenant in ("tenant-a", "tenant-b", "tenant-c"):
            events, used = [], set()
            for rule in ("ml.model_unavailable", "net.refused"):
                for lo, hi in ((0, 6), (600, 640), (1433, 1440)):
                    if rng.random() < 0.45:
                        for m in sorted(rng.sample(range(lo, hi), k=rng.randint(1, 3))):
                            if (rule, m) not in used:
                                used.add((rule, m))
                                events.append((rule, m, rng.randint(1, 4)))
            snaps.append(snapshot(day, tenant, "ml-serving", events))
        out[day] = snaps
    return out


@pytest.mark.parametrize("seed", [3, 11, 29])
def test_count_rule_parity_with_read_time_stitch(tmp_path, seed):
    """The existing ``stitch`` (incident_episodes) and the stored count rule agree on every range:
    stitched rows = counted episodes, and their occurrences add up the same."""
    config = make_config(tmp_path)
    archive = archive_at(config, utc("2026-03-10T02:00:00"))
    data = random_days(seed, 8)
    for day, snaps in data.items():
        archive.close_day(day, snaps)
    shards = {d: r["shard_id"] for d, r in archive.store.days().items()}
    rng = random.Random(seed)
    names = sorted(data)
    for _ in range(12):
        a, b = sorted(rng.sample(range(len(names)), 2)) if rng.random() < 0.8 else (rng.randrange(len(names)),) * 2
        first, last = names[a], names[b]
        rows = []
        for day in names[a : b + 1]:
            for doc in archive.store.day_documents(shards[day], ["incident"]):
                m = doc.metadata
                rows.append({"id": doc.id, "tenant": m["tenant"], "env": m["env"], "service": m["service"], "signature_id": m["signature_id"], "first_failure": m["first_hour"], "last_failure": m["last_hour"], "occurrences": m["occurrences"], "evidence_id": doc.id, "evidence": "", "evidence_count": 0})
        stitched = stitch(rows, 300)
        episodes, occ = total(aggregate(archive, first, last))
        assert (len(stitched), sum(r["occurrences"] for r in stitched)) == (episodes, occ), (first, last)


# --- partial and overdue close ---------------------------------------------------------------


def test_overdue_day_closes_partial_flagged_and_a_later_complete_set_is_refused(tmp_path):
    config = make_config(tmp_path)
    root = tmp_path / "staging"
    day = "2026-01-01"
    events = [("ml.model_unavailable", 600, 4), ("ml.model_unavailable", 1300, 2)]
    a = rolling(day, "tenant-a", "ml-serving", events, until_minute=18 * 60)  # stale: captured until 18:00
    stage(root, day, [a], expected=[a["scope"]["index"], "tenant-z-ml-serving-2026.01.01"])
    # D+1 (close_after_days 1): due, but incomplete -> waits for the grace
    first = archive_at(config, utc("2026-01-02T02:00:00")).import_staging()
    assert first["pending"] == [day] and first["days"] == []
    second = archive_at(config, utc("2026-01-03T02:00:00")).import_staging()
    assert second["pending"] == [day]
    third = archive_at(config, utc("2026-01-04T02:00:00")).import_staging()  # D+1+2: overdue
    [closed] = third["days"]
    assert closed["closure"] == "partial" and closed["missing_scopes"] == ["tenant-z-ml-serving-2026.01.01"] and closed["stale_scopes"] == [a["scope"]["index"]]
    archive = archive_at(config, utc("2026-01-04T02:00:00"))
    row = archive.store.days()[day]
    assert row["closure"] == "partial" and json.loads(row["missing"]) == ["tenant-z-ml-serving-2026.01.01"]
    assert archive.store.log()[-1]["entry"] == "closed_partial" and archive.store.check_chain() == []
    result = aggregate(archive, day, day)
    assert total(result) == (1, 4) and all(g["partial"] for g in result.groups)  # 13:00 not yet captured
    section = archive.manifest_section(date(2026, 1, 4))
    assert section["ledger"][day]["closure"] == "partial" and section["ledger"][day]["missing_scopes"]
    # a later complete set for the partial day: refused, staging kept
    stage(root, day, [snapshot(day, "tenant-a", "ml-serving", events)])
    later = archive_at(config, utc("2026-01-05T02:00:00")).import_staging()
    assert later["refused"] and (root / day).exists()
    with pytest.raises(ArchiveRefused, match="supersedes"):
        archive.close_day(day, [snapshot(day, "tenant-a", "ml-serving", events)])


def test_overdue_day_without_any_capture_becomes_a_gap_not_zero(tmp_path):
    config = make_config(tmp_path)
    stage(tmp_path / "staging", "2026-01-01", [], expected=["tenant-a-ml-serving-2026.01.01"])
    report = archive_at(config, utc("2026-01-04T02:00:00")).import_staging()
    assert report["gaps"] == ["2026-01-01"]
    assert "no summary snapshot" in archive_at(config, utc("2026-01-04T02:00:00")).store.gaps()["2026-01-01"]


def test_complete_close_needs_every_expected_scope(tmp_path):
    config = make_config(tmp_path)
    with pytest.raises(ArchiveRefused, match="lacks expected scopes"):
        archive_at(config, utc("2026-01-03T02:00:00")).close_day("2026-01-01", day_snapshots("2026-01-01", 0), expected=["nope"])


def test_close_after_days_holds_a_day_until_its_close_day(tmp_path):
    config = make_config(tmp_path, schedule={"heavy_window_utc": {"start": "01:00", "end": "07:00"}, "day_close_delay_minutes": 60, "close_after_days": 3, "grace_days": 2})
    for i, day in enumerate(days("2026-01-01", 3)):
        stage(tmp_path / "staging", day, day_snapshots(day, i))
    report = archive_at(config, utc("2026-01-04T02:00:00")).import_staging()  # D+3 of 01-01 only
    assert [d["day"] for d in report["days"]] == ["2026-01-01"] and report["pending"] == ["2026-01-02", "2026-01-03"]


# --- row-hash audit and backfill -----------------------------------------------------------------


def dump_digests(store):
    db = sqlite3.connect(store.path)
    out = {r[0]: json.loads(r[1]) for r in db.execute("SELECT day, digests FROM history_log")}
    tables = {t: db.execute(f"SELECT * FROM {t} ORDER BY day").fetchall() for t in ("episode_piece", "issue_day", "coverage_day")}
    db.close()
    return out, tables


def test_incremental_day_add_leaves_earlier_rows_and_hashes_unchanged(tmp_path):
    config = make_config(tmp_path)
    archive = archive_at(config, utc("2026-01-10T02:00:00"))
    for i, day in enumerate(days("2026-01-01", 3)):
        archive.close_day(day, day_snapshots(day, i))
    digests, tables = dump_digests(archive.store)
    archive.close_day("2026-01-04", day_snapshots("2026-01-04", 3))
    after, after_tables = dump_digests(archive.store)
    assert {d: v for d, v in after.items() if d != "2026-01-04"} == digests
    for name, rows in tables.items():
        assert [r for r in after_tables[name] if r[0] != "2026-01-04"] == rows
    assert archive.store.check_chain() == []
    # a changed earlier row is detected by the audit
    db = sqlite3.connect(archive.store.path)
    db.execute("UPDATE issue_day SET occurrences = occurrences + 1 WHERE day='2026-01-02'")
    db.commit()
    db.close()
    assert any("history rows of day 2026-01-02 changed" in p for p in archive.store.check_chain())


def test_days_archived_before_i1_are_backfilled_identically(tmp_path):
    config = make_config(tmp_path)
    archive = archive_at(config, utc("2026-01-10T02:00:00"))
    archive.close_day("2026-01-01", [snapshot("2026-01-01", "tenant-a", "ml-serving", [("net.refused", 1439, 1)])])
    archive.close_day("2026-01-02", [snapshot("2026-01-02", "tenant-a", "ml-serving", [("net.refused", 1, 1)])])
    before = dump_digests(archive.store)
    db = sqlite3.connect(archive.store.path)  # an archive written before the history tables existed
    for table in ("episode_piece", "issue_day", "coverage_day", "history_log"):
        db.execute(f"DELETE FROM {table}")
    db.commit()
    db.close()
    with pytest.raises(HistoryUnavailable, match="no history tables"):
        aggregate(archive, "2026-01-01", "2026-01-02")
    assert archive.store.backfill_history(300) == ["2026-01-01", "2026-01-02"]
    assert dump_digests(archive.store) == before and archive.store.check_chain() == []
    assert total(aggregate(archive, "2026-01-01", "2026-01-02")) == (1, 2)


# --- tiered aggregate refusals ---------------------------------------------------------------------


def test_non_mergeable_measures_and_unknown_dimensions_are_refused(tmp_path):
    archive = chain_days(tmp_path)
    with pytest.raises(HistoryUnavailable, match="not mergeable"):
        aggregate(archive, "2026-01-01", "2026-01-03", measures=["p95_latency"])
    with pytest.raises(HistoryUnavailable, match="cannot group by"):
        aggregate(archive, "2026-01-01", "2026-01-03", group_by=["hostname"])
    with pytest.raises(HistoryUnavailable, match="cannot filter"):
        aggregate(archive, "2026-01-01", "2026-01-03", filters={"message": ["x"]})


def test_grouping_by_day_partitions_the_range_total(tmp_path):
    archive = chain_days(tmp_path)
    whole = aggregate(archive, "2026-01-01", "2026-01-03", group_by=[])
    by_day = aggregate(archive, "2026-01-01", "2026-01-03", group_by=["day"])
    assert total(by_day) == total(whole)
    assert {g["day"]: g["episodes"] for g in by_day.groups} == {"2026-01-01": 1, "2026-01-02": 1, "2026-01-03": 0}
    month = aggregate(archive, "2026-01-01", "2026-01-03", group_by=["month", "tenant"], measures=["occurrences", "episodes", "active_days", "tenants"])
    assert {(g["month"], g["tenant"]): g["active_days"] for g in month.groups} == {("2026-01", "tenant-a"): 3, ("2026-01", "tenant-b"): 1}


def test_hour_predicates_push_day_bounds_into_the_archive_read(tmp_path):
    from su_multi_arm.core.types import Predicate, ScopeSpec

    archive = chain_days(tmp_path)
    spec = ScopeSpec(predicates=[Predicate(field="kind", op="eq", value="incident"), Predicate(field="last_hour", op="range", lower="2026-01-02T00:00:00+00:00"), Predicate(field="first_hour", op="range", upper="2026-01-02T23:59:59+00:00")])
    sql, params = archive.store._pushdown(spec)
    assert "r.day>=?" in sql and "r.day<=?" in sql and params[-2:] == ["2026-01-02", "2026-01-02"]
    shards = sorted(r["shard_id"] for r in archive.store.days().values())
    result = archive.store.execute(shards, spec, [], archive.config.metadata_schema)
    assert result.total == 2  # tenant-a's and tenant-b's pieces of 2026-01-02 only


def test_stitch_never_joins_pieces_a_day_apart():
    rows = [
        {"id": "a", "tenant": "t", "env": "e", "service": "s", "signature_id": "r", "first_failure": "2026-01-01T23:59:00+00:00", "last_failure": "2026-01-01T23:59:59+00:00", "occurrences": 1, "evidence_id": "a"},
        {"id": "b", "tenant": "t", "env": "e", "service": "s", "signature_id": "r", "first_failure": "2026-01-03T00:00:00+00:00", "last_failure": "2026-01-03T00:00:59+00:00", "occurrences": 1, "evidence_id": "b"},
    ]
    assert len(stitch(rows, 300)) == 2  # a whole day between them: never one episode


# --- schedule, verify, detection -------------------------------------------------------------------


def test_refresh_job_and_cli_command_with_the_provisional_store(tmp_path):
    from su_multi_arm.cli import build_parser
    from su_multi_arm.ingestion.log_policy import emit_systemd, planned_jobs

    config = make_config(tmp_path, provisional={"enabled": True}, schedule={"heavy_window_utc": {"start": "01:00", "end": "07:00"}, "day_close_delay_minutes": 60, "close_after_days": 3, "grace_days": 2})
    jobs = {j["name"]: j for j in planned_jobs(config, "c.yaml")}
    assert jobs["refresh"]["commands"] == ["@COLLECTOR_REFRESH_COMMAND@", "su-multi-arm --config c.yaml logs policy refresh"]
    assert "D+3" in jobs["daily"]["note"] and "2 grace days" in jobs["daily"]["note"]
    assert "ExecStart=su-multi-arm --config c.yaml logs policy refresh" in emit_systemd(config, "c.yaml")
    assert build_parser().parse_args(["--config", "c.yaml", "logs", "policy", "refresh"]).policy_command == "refresh"
    plain = {j["name"]: j for j in planned_jobs(make_config(tmp_path / "plain"), "c.yaml")}
    assert plain["refresh"]["commands"] == ["@COLLECTOR_REFRESH_COMMAND@"]


def test_verify_reports_overdue_days_unbuilt_history_and_lost_provisional_files(tmp_path):
    from pathlib import Path

    from su_multi_arm.ingestion.log_policy import verify_policy
    from su_multi_arm.ingestion.provisional import open_provisional

    class Registry:
        def generations(self):
            return []

        def current(self):
            return None

    config = make_config(tmp_path, provisional={"enabled": True})
    archive = archive_at(config, utc("2026-01-02T02:00:00"))
    archive.close_day("2026-01-01", day_snapshots("2026-01-01", 0))
    stage(tmp_path / "staging", "2026-01-02", [rolling("2026-01-02", "tenant-a", "ml-serving", [("net.refused", 5, 1)], until_minute=60)])
    archive_at(config, utc("2026-01-02T02:00:00"), archive.store).refresh_provisional()
    ok = verify_policy(config, registry=Registry(), now=utc("2026-01-03T03:00:00"), archive=archive.store)
    assert not any("past close_after_days" in p for p in ok["problems"]) and ok["checks"]["provisional"]["unclosed"] == 1
    late = verify_policy(config, registry=Registry(), now=utc("2026-01-06T03:00:00"), archive=archive.store)
    assert any("2026-01-02" in p and "past close_after_days + grace_days" in p for p in late["problems"])
    Path(open_provisional(config).current()["2026-01-02"]["file"]).unlink()
    lost = verify_policy(config, registry=Registry(), now=utc("2026-01-03T03:00:00"), archive=archive.store)
    assert any("name projection files that do not exist" in p for p in lost["problems"])
    db = sqlite3.connect(archive.store.path)
    db.execute("DELETE FROM history_log")
    db.commit()
    db.close()
    unbuilt = verify_policy(config, registry=Registry(), now=utc("2026-01-03T03:00:00"), archive=archive.store)
    assert any("have no history tables" in p for p in unbuilt["problems"])


@pytest.mark.parametrize(
    "question,expected",
    [
        ("How many ML outages were there in the last 3 months and what was the reason?", {"window": "in the last 3 months", "category": ["ml_serving", "ml_training"], "group_by": ["category", "signature_id"]}),
        ("which incidents did tenant-a have between 2026-01-01 and 2026-02-01", {"start": "2026-01-01", "end": "2026-02-01", "tenant": "tenant-a", "list_episodes": 0}),
        ("list ML failures per day in the last 10 days", {"window": "last 10 days", "group_by": ["day", "category", "signature_id"], "list_episodes": 20}),
        # owner test 2026-11-10: a short noun phrase or an "any ...?" question asks for the period's history
        ("outages in last 30 days", {"window": "last 30 days", "group_by": ["category", "signature_id"]}),
        ("any outages in the last 3 days?", {"window": "last 3 days"}),
    ],
)
def test_detect_issue_history(question, expected):
    from su_multi_arm.decision.log_intents import detect_issue_history

    vocab = {"category": ["ml_serving", "ml_training", "connectivity"], "tenant": ["tenant-a", "tenant-b"], "service": ["ml-serving"]}
    call = detect_issue_history(question, as_of=date(2026, 3, 31), values=lambda f: vocab.get(f, []))
    assert call is not None and call.intent == "issue_history"
    assert {k: call.args.get(k) for k in expected} == expected


@pytest.mark.parametrize("question", ["why did the model server fail", "how many outages", "top errors yesterday for tenant-a", "the customer said the outage last week broke their search page and they want a refund"])
def test_detect_issue_history_needs_an_episode_noun_an_ask_and_a_period(question):
    from su_multi_arm.decision.log_intents import detect_issue_history

    assert detect_issue_history(question, as_of=date(2026, 3, 31), values=lambda f: []) is None
