"""The long-term episode archive served through the engine: 90 synthetic days archived as day
shards, one small serving generation, long-range episode queries with reasons, incremental
append, and retention that never deletes an archived day. Synthetic data only."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from su_multi_arm.core.config import validate_config
from su_multi_arm.engine import Engine
from su_multi_arm.ingestion.episode_archive import ARCHIVE_KINDS, EpisodeArchive
from su_multi_arm.ingestion.episode_summaries import summary_documents
from su_multi_arm.ingestion.log_policy import verify_policy
from tests.log_policy_support import RULES, day_snapshots, days, policy_logs, snapshot, write_family
from tests.logs_gold_support import gold_config
from tests.logs_support import write_library

DAYS = days("2026-01-01", 90)
LAST = DAYS[-1]
NOW = datetime(2026, 4, 1, 2, 0, tzinfo=timezone.utc)  # 2026-03-31 is complete


def utc(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def configure(tmp_path):
    sha = write_family(tmp_path / "policy" / "ml.yaml")
    write_library(tmp_path / "patterns")
    data = gold_config(tmp_path)
    data["dataset"] = "synthetic-archive"
    data["logs"].update(policy_logs(tmp_path, sha))
    base = validate_config(data).model_dump(mode="json")
    base["tools"]["enabled"] = ["incident_episodes", "log_top", "log_signature", "aggregate", "get_document"]
    sample = summary_documents("synthetic-archive", day_snapshots(DAYS[0], 0), RULES, citations=True)
    fields = base["metadata_schema"]["fields"]
    existing = {f["name"]: f for f in fields}
    if existing.get("kind", {}).get("enum_values") is not None:
        existing["kind"]["enum_values"] = sorted(set(existing["kind"]["enum_values"]) | {d.kind for d in sample})
    for doc in sample:
        for name, value in {**doc.metadata, "archive_text_trimmed": True}.items():
            if name in existing:
                continue
            kind = "datetime" if name in {"window_start", "window_end", "observed_at"} else "boolean" if isinstance(value, bool) else "integer" if isinstance(value, int) else "number" if isinstance(value, float) else "string"
            existing[name] = {"name": name, "type": kind, "nullable": True, "filterable": True, "groupable": True, "sortable": True}
            fields.append(existing[name])
    return validate_config(base)


@pytest.fixture
def served(tmp_path):
    config = configure(tmp_path)
    engine = Engine(config)
    archive = EpisodeArchive(config, now=lambda: NOW)
    for i, day in enumerate(DAYS):
        archive.close_day(day, day_snapshots(day, i))
    published = archive.publish(engine.write_path, NOW.date())
    return engine, archive, published


def call(engine, name, args):
    return engine.call_tool(name, args).result.output


def test_ninety_days_of_ml_episodes_with_reasons(served):
    engine, archive, published = served
    assert published["status"] == "published" and published["days"] == 90
    manifest = engine.write_path.registry.manifest(published["generation"])
    assert manifest["logs"]["history"]["store"] == "episode_archive"
    assert len(manifest["logs"]["history"]["shards"]) == 90
    # the serving generation stays small: the rule catalog plus the live window (10 days)
    live = sum(len(summary_documents("synthetic-archive", day_snapshots(d, i), RULES)) for i, d in enumerate(DAYS) if d >= "2026-03-22")
    assert manifest["documents"] <= len(RULES) + live < 200
    out = call(engine, "incident_episodes", {"start": DAYS[0], "end": LAST, "as_of": LAST, "service": ["ml-serving"], "top_n": 500})
    assert out.source_unavailable == []
    assert len(out.rows) == 30 * 2 + 60  # tenant-b fails on every third day, tenant-a every day
    categories = {r["category"] for r in out.rows}
    assert categories == {"ml_serving", "ml_training", "connectivity"}
    assert all(r["issue"] and r["first_failure"] and r["occurrences"] > 0 and r["evidence_count"] == 1 for r in out.rows)
    assert {r["first_failure"][:10] for r in out.rows} == set(DAYS)
    # counters over the whole range
    assert out.statistics["source_scopes"] == 180
    assert out.statistics["episodes_total"] == 120
    # tenant filter and the episode-cell aggregation over the same 90 days
    a = call(engine, "incident_episodes", {"start": DAYS[0], "end": LAST, "as_of": LAST, "tenant": "tenant-a", "top_n": 500})
    assert len(a.rows) == 90 and {r["tenant"] for r in a.rows} == {"tenant-a"}
    top = call(engine, "log_top", {"start": DAYS[0], "end": LAST, "as_of": LAST, "by": "signature", "level": ["error", "fatal"]})
    assert {r["signature_id"] for r in top.rows} == {r.id for r in RULES}
    total = sum(r["error_count"] for r in top.rows)
    expected = sum((5 + n) * (2 if n % 3 == 0 else 1) for n in range(90))
    assert total == expected


def test_window_bound_is_the_declared_serving_policy(served):
    engine, _, _ = served
    with pytest.raises(Exception, match="at most 400 days"):
        engine.call_tool("incident_episodes", {"start": "2025-01-01", "end": LAST, "as_of": LAST})


def test_appending_one_day_writes_that_day_only_and_republishes(served):
    engine, archive, first = served
    rows_before = archive.store.row_count()
    shards_before = {d: r["shard_id"] for d, r in archive.store.days().items()}
    nxt = "2026-04-01"
    later = EpisodeArchive(engine.config, store=archive.store, now=lambda: utc("2026-04-02T02:00:00"))
    result = later.close_day(nxt, day_snapshots(nxt, 90))
    assert archive.store.row_count() - rows_before == result["rows"]
    assert {d: r["shard_id"] for d, r in archive.store.days().items() if d != nxt} == shards_before
    second = later.publish(engine.write_path, date(2026, 4, 2))
    assert second["status"] == "published" and second["days"] == 91
    out = call(engine, "incident_episodes", {"start": nxt, "end": nxt, "as_of": nxt})
    assert out.rows and all(r["first_failure"].startswith(nxt) for r in out.rows)
    assert later.publish(engine.write_path, date(2026, 4, 2))["status"] == "unchanged"


def test_episode_across_midnight_is_one_row(tmp_path):
    config = configure(tmp_path)
    engine = Engine(config)
    archive = EpisodeArchive(config, now=lambda: NOW)
    late = snapshot("2026-01-01", "tenant-a", "ml-serving", [("ml.model_unavailable", 23 * 60 + 59, 4)])
    early = snapshot("2026-01-02", "tenant-a", "ml-serving", [("ml.model_unavailable", 1, 6)])
    archive.close_day("2026-01-01", [late])
    archive.close_day("2026-01-02", [early])
    archive.publish(engine.write_path, NOW.date())
    out = call(engine, "incident_episodes", {"start": "2026-01-01", "end": "2026-01-02", "as_of": "2026-01-02"})
    assert len(out.rows) == 1
    row = out.rows[0]
    assert row["occurrences"] == 10 and row["crosses_day_boundary"] is True and len(row["joined_evidence_ids"]) == 2
    assert row["first_failure"].startswith("2026-01-01T23:59") and row["last_failure"].startswith("2026-01-02T00:01")


def test_retention_never_deletes_archived_days(served, tmp_path):
    from su_multi_arm.ingestion.partitions import FilePartitionStore
    from su_multi_arm.ingestion.retention import Retention

    engine, archive, _ = served
    config = engine.config
    for k, day in enumerate(("2026-04-01", "2026-04-02", "2026-04-03")):
        closed_at = utc(day).replace(hour=2) + timedelta(days=1)
        step = EpisodeArchive(config, store=archive.store, now=lambda t=closed_at: t)
        step.close_day(day, day_snapshots(day, 90 + k))
        step.publish(engine.write_path, date.fromisoformat(day))
    registry = engine.write_path.registry
    assert len([g for g in registry.generations() if (registry.manifest(g) or {}).get("logs")]) == 4
    rows_before = archive.store.row_count()
    report = Retention(config, FilePartitionStore(config.logs.partitions.path, config.logs.source), engine.write_path).run(date(2026, 4, 3))
    assert len(report["deleted_generations"]) == 2  # beyond keep_generations
    assert archive.store.row_count() == rows_before
    assert len(archive.store.days()) == 93
    result = verify_policy(config, registry=registry, now=utc("2026-04-03T23:00:00"), archive=archive.store)
    assert result["ok"], result["problems"]
    # the archive serves the oldest day after the generations that first published it are gone
    out = call(engine, "incident_episodes", {"start": DAYS[0], "end": DAYS[0], "as_of": DAYS[0]})
    assert out.rows and out.source_unavailable == []
    from su_multi_arm.ingestion.release import generation_id

    active = generation_id(registry.manifest(registry.current()["generation"]))
    assert engine.history_route(active)[2] == ARCHIVE_KINDS


def test_recipe_epochs_and_gaps_are_stated_in_long_range_answers(tmp_path):
    from tests.log_policy_support import FAMILY

    config = configure(tmp_path)
    engine = Engine(config)
    archive = EpisodeArchive(config, now=lambda: NOW)
    archive.close_day("2026-01-01", day_snapshots("2026-01-01", 0))
    archive.record_gap("2026-01-02", "synthetic capture failure")
    changed = dict(FAMILY, phrases=[*FAMILY["phrases"], "inference timeout"])
    config.logs.collection.keywords.files[0].sha256 = write_family(tmp_path / "policy" / "ml.yaml", changed)
    config.logs.collection.keywords.effective_from = date(2026, 1, 3)
    EpisodeArchive(config, store=archive.store, now=lambda: NOW).close_day(
        "2026-01-03", [snapshot("2026-01-03", "tenant-a", "ml-serving", [("net.refused", 9, 2)], phrases=changed["phrases"])])
    EpisodeArchive(config, store=archive.store, now=lambda: NOW).publish(engine.write_path, NOW.date())
    out = call(engine, "incident_episodes", {"start": "2026-01-01", "end": "2026-01-03", "as_of": "2026-01-03"})
    text = " ".join(out.notes)
    assert "keyword recipe epochs" in text and "2026-01-01 to 2026-01-01" in text and "2026-01-03 to 2026-01-03" in text
    assert "collection gaps (not zero incidents): 2026-01-02" in text
    single = call(engine, "incident_episodes", {"start": "2026-01-03", "end": "2026-01-03", "as_of": "2026-01-03"})
    assert "keyword recipe epochs" not in " ".join(single.notes)
