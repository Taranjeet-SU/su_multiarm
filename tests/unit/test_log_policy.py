"""The declared collection policy (logs.collection): validation, keyword recipe pins, schedule
rules, the append-only episode archive and ``verify_policy``. Synthetic data only."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from su_multi_arm.core.config import validate_config
from su_multi_arm.core.config_logs import LogsConfig
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.ingestion.episode_archive import ArchiveRefused, EpisodeArchive, open_archive
from su_multi_arm.ingestion.log_policy import (
    day_complete,
    emit_systemd,
    heavy_window_open,
    load_keyword_recipe,
    planned_jobs,
    policy_report,
    verify_policy,
)
from tests.log_policy_support import FAMILY, day_snapshots, days, policy_logs, snapshot, write_family
from tests.logs_gold_support import gold_config


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def make_config(tmp, **collection):
    sha = write_family(tmp / "policy" / "ml.yaml")
    data = gold_config(tmp)
    data["logs"].update(policy_logs(tmp, sha, **collection))
    return validate_config(data)


class FakeRegistry:
    def __init__(self, manifests=None, current=None):
        self.manifests = manifests or {}
        self._current = current

    def generations(self):
        return sorted(self.manifests)

    def manifest(self, generation):
        return self.manifests.get(generation)

    def current(self):
        return None if self._current is None else {"generation": self._current}


# --- config ------------------------------------------------------------------------------------


def logs_mapping(**collection):
    return {"source": "gold", "partitions": {"path": "parts"}, "collection": collection}


def test_defaults_reproduce_the_behaviour_before_the_policy():
    logs = LogsConfig.model_validate({"source": "gold", "partitions": {"path": "parts"}})
    policy = logs.collection
    assert policy.schedule.heavy_window_utc is None and policy.schedule.day_close_delay_minutes == 0
    assert policy.keywords.files == [] and policy.archive.enabled is False
    assert policy.serving.max_window_days == 31  # the incident_episodes bound before the archive
    assert heavy_window_open(policy, utc("2026-03-01T13:00:00"))  # unrestricted
    assert day_complete(policy, date(2026, 3, 1), utc("2026-03-02T00:00:00"))


@pytest.mark.parametrize(
    "collection,message",
    [
        ({"schedule": {"heavy_window_utc": {"start": "07:00", "end": "01:00"}}}, "same UTC day"),
        ({"schedule": {"heavy_window_utc": {"start": "01:00", "end": "01:00"}}}, "same UTC day"),
        ({"schedule": {"heavy_window_utc": {"start": "1:00", "end": "07:00"}}}, "pattern"),
        ({"schedule": {"heavy_window_utc": {"start": "01:00", "end": "02:00"}, "day_close_delay_minutes": 120}}, "no day can ever be closed"),
        ({"schedule": {"summary_refresh_minutes": 0}}, "greater than or equal"),
        ({"keywords": {"files": [{"path": "k.yaml", "sha256": "abc"}]}}, "pattern"),
        ({"keywords": {"files": [{"path": "k.yaml", "sha256": "a" * 64}, {"path": "k.yaml", "sha256": "b" * 64}]}}, "twice"),
        ({"keywords": {"effective_from": "2026-01-01"}}, "needs keywords.files"),
        ({"archive": {"enabled": True}}, "needs keywords.files"),
        ({"archive": {"enabled": True}, "keywords": {"files": [{"path": "k.yaml", "sha256": "a" * 64}]}}, "derived: forever"),
        ({"serving": {"max_window_days": 90}}, "needs the archive"),
        ({"bogus": 1}, "Extra inputs"),
    ],
)
def test_policy_validation_errors(collection, message):
    with pytest.raises(ValueError, match=message):
        LogsConfig.model_validate(logs_mapping(**collection))


def test_archive_must_not_live_under_staging():
    data = logs_mapping(archive={"enabled": True, "path": "stage/a.sqlite"}, staging={"path": "stage"}, keywords={"files": [{"path": "k.yaml", "sha256": "a" * 64}]})
    data["retention"] = {"derived": "forever"}
    with pytest.raises(ValueError, match="outside logs.collection.staging"):
        LogsConfig.model_validate(data)


@pytest.mark.parametrize(
    "extra,message",
    [
        ({"collection": {"archive": {"enabled": True, "path": "parts/history/x.sqlite"}}}, "outside <partitions>/history"),
        ({"history": {"tier": True}}, "separate collection modes"),
    ],
)
def test_archive_is_never_the_compose_history(extra, message):
    data = logs_mapping(archive={"enabled": True}, keywords={"files": [{"path": "k.yaml", "sha256": "a" * 64}]})
    data["retention"] = {"derived": "forever"}
    if "collection" in extra:
        data["collection"]["archive"] = extra["collection"]["archive"]
    else:
        data.update(extra)
    with pytest.raises(ValueError, match=message):
        LogsConfig.model_validate(data)


def test_relative_policy_paths_resolve_against_the_config_file(tmp_path):
    from su_multi_arm.core.config import load_config
    import yaml

    sha = write_family(tmp_path / "policy" / "ml.yaml")
    data = gold_config(tmp_path)
    data["logs"].update(policy_logs(tmp_path, sha))
    data["logs"]["collection"]["keywords"]["files"][0]["path"] = "policy/ml.yaml"
    data["logs"]["collection"]["staging"]["path"] = "staging"
    (tmp_path / "c.yaml").write_text(yaml.safe_dump(data))
    config = load_config(tmp_path / "c.yaml")
    assert config.logs.collection.keywords.files[0].path == (tmp_path / "policy" / "ml.yaml").resolve()
    assert config.logs.collection.staging.path == (tmp_path / "staging").resolve()


# --- keyword recipe ----------------------------------------------------------------------------


def test_recipe_is_pinned_by_file_hash(tmp_path):
    config = make_config(tmp_path)
    recipe = load_keyword_recipe(config)
    assert recipe.families == ["synthetic_ml"] and [r.id for r in recipe.rules] == ["ml.model_unavailable", "ml.training_failed", "net.refused"]
    changed = dict(FAMILY, phrases=[*FAMILY["phrases"], "inference timeout"])
    write_family(tmp_path / "policy" / "ml.yaml", changed)
    with pytest.raises(ConfigError, match="pins"):
        load_keyword_recipe(config)  # the old pin never covers a changed list
    report = policy_report(config, now=utc("2026-03-02T02:00:00"))
    assert report["keyword_files"][0]["ok"] is False and "recipe" not in report


def test_invalid_family_file_is_refused(tmp_path):
    config = make_config(tmp_path)
    bad = {"family": "synthetic_ml", "phrases": ["x"], "rules": [{"id": "ok.rule", "title": "t", "category": "c", "alternatives": [["short"]]}]}
    sha = write_family(tmp_path / "policy" / "ml.yaml", bad)
    config.logs.collection.keywords.files[0].sha256 = sha
    with pytest.raises(ConfigError, match="invalid keyword file"):
        load_keyword_recipe(config)


# --- schedule ----------------------------------------------------------------------------------


def test_heavy_window_and_day_close_rule(tmp_path):
    policy = make_config(tmp_path).logs.collection
    assert not heavy_window_open(policy, utc("2026-03-02T00:59:00"))
    assert heavy_window_open(policy, utc("2026-03-02T01:00:00"))
    assert heavy_window_open(policy, utc("2026-03-02T06:59:59"))
    assert not heavy_window_open(policy, utc("2026-03-02T07:00:00"))
    assert not day_complete(policy, date(2026, 3, 1), utc("2026-03-02T00:59:00"))  # 60 min close delay
    assert day_complete(policy, date(2026, 3, 1), utc("2026-03-02T01:00:00"))


def test_emitted_schedule_respects_the_window_and_day_boundary(tmp_path):
    config = make_config(tmp_path)
    jobs = {j["name"]: j for j in planned_jobs(config, "/etc/synthetic/config.yaml")}
    daily = jobs["daily"]
    assert daily["start_utc"] == "01:00" and daily["end_utc"] == "07:00" and daily["max_runtime_seconds"] == 6 * 3600
    assert daily["commands"][0].endswith("logs policy import")
    assert jobs["refresh"]["every_minutes"] == 15
    text = emit_systemd(config, "/etc/synthetic/config.yaml")
    assert "OnCalendar=*-*-* 01:00:00 UTC" in text and "RuntimeMaxSec=21600" in text and "OnUnitActiveSec=15min" in text
    # a close delay later than the window start moves the heavy pass, and it still ends in the window
    config.logs.collection.schedule.day_close_delay_minutes = 150
    daily = {j["name"]: j for j in planned_jobs(config)}["daily"]
    assert daily["start_utc"] == "02:30" and daily["max_runtime_seconds"] == (7 * 60 - 150) * 60
    # without a window the heavy pass is bounded by midnight UTC: never across a day boundary
    config.logs.collection.schedule.heavy_window_utc = None
    config.logs.collection.schedule.day_close_delay_minutes = 30
    daily = {j["name"]: j for j in planned_jobs(config)}["daily"]
    assert daily["start_utc"] == "00:30" and daily["end_utc"] == "24:00" and daily["max_runtime_seconds"] == (24 * 60 - 30) * 60


# --- archive -----------------------------------------------------------------------------------


def archive_at(config, now):
    return EpisodeArchive(config, now=lambda: now)


def test_appending_day_n_plus_one_leaves_earlier_days_byte_identical(tmp_path):
    config = make_config(tmp_path)
    first = days("2026-01-01", 3)
    archive = archive_at(config, utc("2026-01-05T02:00:00"))
    for i, day in enumerate(first):
        archive.close_day(day, day_snapshots(day, i))
    store = archive.store

    def dump():
        import sqlite3

        db = sqlite3.connect(store.path)
        rows = db.execute("SELECT shard_id, id, data FROM rows ORDER BY shard_id, id").fetchall()
        fields = db.execute("SELECT * FROM history_fields ORDER BY shard_id, record_id, field").fetchall()
        db.close()
        return rows, fields

    before_rows, before_fields = dump()
    written_before = store.writes
    nxt = "2026-01-04"
    result = archive.close_day(nxt, day_snapshots(nxt, 3))
    after_rows, after_fields = dump()
    new_shard = result["shard"]
    assert [r for r in after_rows if r[0] != new_shard] == before_rows  # days <= N untouched
    assert [f for f in after_fields if f[0] != new_shard] == before_fields
    assert store.writes - written_before == result["rows"]  # rows of day N+1 only
    # idempotent close; other content for an archived day is refused (append-only)
    assert archive.close_day(nxt, day_snapshots(nxt, 3))["status"] == "archived_already"
    with pytest.raises(ArchiveRefused, match="append-only"):
        archive.close_day(nxt, day_snapshots(nxt, 4))
    with pytest.raises(ArchiveRefused, match="append-only"):
        store.gc(set())
    assert store.check_chain() == []


def test_a_rollup_pass_never_crosses_a_day_boundary(tmp_path):
    config = make_config(tmp_path)
    archive = archive_at(config, utc("2026-01-05T02:00:00"))
    crossing = snapshot("2026-01-01", "tenant-a", "ml-serving", [], window=("2026-01-01T12:00:00+00:00", "2026-01-02T12:00:00+00:00"))
    with pytest.raises(ArchiveRefused, match="day boundary"):
        archive.close_day("2026-01-01", [crossing])
    with pytest.raises(ArchiveRefused, match="day boundary"):
        archive.close_day("2026-01-02", day_snapshots("2026-01-01", 0))  # another day's snapshots
    with pytest.raises(ArchiveRefused, match="not complete"):
        archive_at(config, utc("2026-01-02T00:30:00")).close_day("2026-01-01", day_snapshots("2026-01-01", 0))
    with pytest.raises(ArchiveRefused, match="gap"):
        archive.close_day("2026-01-01", [])


def test_recipe_change_refuses_mixing_until_an_explicit_epoch(tmp_path):
    config = make_config(tmp_path)
    archive = archive_at(config, utc("2026-01-10T02:00:00"))
    archive.close_day("2026-01-01", day_snapshots("2026-01-01", 0))
    # a new keyword list, re-pinned without an epoch start
    changed = dict(FAMILY, phrases=[*FAMILY["phrases"], "inference timeout"])
    config.logs.collection.keywords.files[0].sha256 = write_family(tmp_path / "policy" / "ml.yaml", changed)
    phrases = changed["phrases"]
    new = [snapshot("2026-01-02", "tenant-a", "ml-serving", [("net.refused", 5, 3)], phrases=phrases)]
    with pytest.raises(ArchiveRefused, match="differs from archived days"):
        archive_at(config, utc("2026-01-10T02:00:00")).close_day("2026-01-02", new)
    # snapshots captured under the old recipe are refused by the new pin
    with pytest.raises(ArchiveRefused, match="another keyword recipe"):
        archive_at(config, utc("2026-01-10T02:00:00")).close_day("2026-01-02", day_snapshots("2026-01-02", 1))
    # an explicit epoch after the last archived day
    config.logs.collection.keywords.effective_from = date(2026, 1, 2)
    result = archive_at(config, utc("2026-01-10T02:00:00")).close_day("2026-01-02", new)
    assert result["status"] == "archived"
    recipes = {row["recipe"] for row in archive.store.days().values()}
    assert len(recipes) == 2  # two epochs, each day labelled with its own recipe
    with pytest.raises(ArchiveRefused, match="precedes keywords.effective_from"):
        older = [snapshot("2025-12-31", "tenant-a", "ml-serving", [], phrases=phrases)]
        archive_at(config, utc("2026-01-10T02:00:00")).close_day("2025-12-31", older)


def test_trimmed_archive_text_keeps_aggregates_and_citations(tmp_path):
    config = make_config(tmp_path, archive={"max_text_chars": 64})
    archive = archive_at(config, utc("2026-01-05T02:00:00"))
    archive.close_day("2026-01-01", day_snapshots("2026-01-01", 0))
    shard = archive.store.days()["2026-01-01"]["shard_id"]
    from su_multi_arm.core.types import Predicate, ScopeSpec

    result = archive.store.execute([shard], ScopeSpec(predicates=[Predicate(field="kind", op="eq", value="incident")]), [], config.metadata_schema)
    docs = archive.store.documents([shard], result.matched_ids)
    assert docs
    for doc in docs:
        assert doc.metadata["occurrences"] > 0 and doc.metadata["category"] and doc.metadata["first_hour"]
        assert doc.fields["evidence_refs"].startswith("src-")  # citation kept
        assert all(len(v) <= 64 + len(" [trimmed]") for k, v in doc.fields.items() if k not in {"header", "evidence_refs"})
    assert any(d.metadata.get("archive_text_trimmed") for d in docs)


def test_staging_import_closes_complete_days_and_deletes_their_copies(tmp_path):
    config = make_config(tmp_path)
    for i, day in enumerate(days("2026-01-01", 3)):
        folder = tmp_path / "staging" / day
        folder.mkdir(parents=True)
        for j, snap in enumerate(day_snapshots(day, i)):
            (folder / f"{j}.json").write_text(json.dumps(snap))
    # outside the heavy window nothing happens
    assert archive_at(config, utc("2026-01-03T12:00:00")).import_staging()["status"] == "outside_heavy_window"
    report = archive_at(config, utc("2026-01-03T01:30:00")).import_staging()
    assert [d["day"] for d in report["days"]] == ["2026-01-01", "2026-01-02"]
    assert report["pending"] == ["2026-01-03"]  # today is not complete
    assert sorted(p.name for p in (tmp_path / "staging").iterdir()) == ["2026-01-03"]


# --- verify ------------------------------------------------------------------------------------


def generation_manifest(source, shards):
    return {"logs": {"source": source, "history": {"store": "episode_archive", "shards": shards}}}


def test_verify_detects_a_missing_day_an_extra_generation_and_a_stale_raw_day(tmp_path):
    config = make_config(tmp_path)
    now = utc("2026-01-06T02:00:00")
    archive = archive_at(config, now)
    for i, day in enumerate(days("2026-01-01", 5)):
        if day != "2026-01-03":
            archive.close_day(day, day_snapshots(day, i))
    shards = {d: row["shard_id"] for d, row in archive.store.days().items()}
    registry = FakeRegistry({"g1": generation_manifest("gold", shards)}, current="g1")
    result = verify_policy(config, registry=registry, now=now, archive=archive.store)
    assert not result["ok"]
    assert any("without a rollup or a gap record: ['2026-01-03']" in p for p in result["problems"])
    archive.record_gap("2026-01-03", "synthetic source outage: capture failed")
    assert verify_policy(config, registry=registry, now=now, archive=archive.store)["ok"]
    # one generation too many
    extra = FakeRegistry({g: generation_manifest("gold", shards) for g in ("g1", "g2", "g3")}, current="g3")
    assert any("exceed retention.keep_generations" in p for p in verify_policy(config, registry=extra, now=now, archive=archive.store)["problems"])

    # a raw day older than raw_days
    class Raw:
        def keys(self):
            return ["2026-01-01", "2026-01-05"]

    result = verify_policy(config, registry=registry, now=now, archive=archive.store, raw=Raw())
    assert any("raw copies ['2026-01-01']" in p for p in result["problems"])


def test_verify_detects_a_deleted_archived_day_and_a_stale_serving_generation(tmp_path):
    import sqlite3

    config = make_config(tmp_path)
    now = utc("2026-01-04T02:00:00")
    archive = archive_at(config, now)
    for i, day in enumerate(days("2026-01-01", 3)):
        archive.close_day(day, day_snapshots(day, i))
    shards = {d: row["shard_id"] for d, row in archive.store.days().items()}
    stale = FakeRegistry({"g1": generation_manifest("gold", {k: v for k, v in shards.items() if k != "2026-01-03"})}, current="g1")
    assert any("does not serve archived days ['2026-01-03']" in p for p in verify_policy(config, registry=stale, now=now, archive=archive.store)["problems"])
    db = sqlite3.connect(archive.store.path)
    db.execute("DELETE FROM archive_days WHERE day='2026-01-02'")
    db.execute("DELETE FROM rows WHERE shard_id=?", (shards["2026-01-02"],))
    db.commit()
    db.close()
    problems = verify_policy(config, registry=FakeRegistry(), now=now, archive=archive.store)["problems"]
    assert any("archived day 2026-01-02 was deleted" in p for p in problems)
    assert any("without a rollup or a gap record: ['2026-01-02']" in p for p in problems)


def test_verify_detects_undeleted_staging_and_a_changed_keyword_file(tmp_path):
    config = make_config(tmp_path)
    now = utc("2026-01-02T02:00:00")
    archive = archive_at(config, now)
    archive.close_day("2026-01-01", day_snapshots("2026-01-01", 0))
    (tmp_path / "staging" / "2026-01-01").mkdir(parents=True)
    shards = {d: row["shard_id"] for d, row in archive.store.days().items()}
    registry = FakeRegistry({"g1": generation_manifest("gold", shards)}, current="g1")
    problems = verify_policy(config, registry=registry, now=now, archive=archive.store)["problems"]
    assert problems == ["staging copies of archived days ['2026-01-01'] were not deleted"]
    write_family(tmp_path / "policy" / "ml.yaml", dict(FAMILY, phrases=[*FAMILY["phrases"], "extra phrase"]))
    problems = verify_policy(config, registry=registry, now=now, archive=archive.store)["problems"]
    assert any(p.startswith("keyword recipe:") for p in problems)


def test_report_shows_policy_recipe_retention_and_archive(tmp_path):
    config = make_config(tmp_path)
    archive_at(config, utc("2026-01-02T02:00:00")).close_day("2026-01-01", day_snapshots("2026-01-01", 0))
    report = policy_report(config, now=utc("2026-01-02T02:00:00"))
    assert report["recipe"]["families"] == ["synthetic_ml"] and report["keyword_files"][0]["ok"]
    assert report["retention"]["keyword_episode_aggregates"]["scope"] == "forever"
    assert report["retention"]["raw"]["keep_days"] == 3 and report["retention"]["masked_partitions"]["keep_days"] == 10
    assert report["archive"]["days"] == 1 and report["schedule"]["heavy_window_open"] is True
    assert report["source_rate_limit"]["requests_per_second"] == 2.0
    text = json.dumps(report)
    assert str(tmp_path) not in text  # paths are not echoed (file names only)


def test_cli_parses_policy_commands():
    from su_multi_arm.cli import build_parser

    args = build_parser().parse_args(["--config", "c.yaml", "logs", "policy", "report", "--emit", "systemd"])
    assert args.logs_command == "policy" and args.policy_command == "report" and args.emit == "systemd"
    args = build_parser().parse_args(["--config", "c.yaml", "logs", "policy", "record-gap", "--date", "2026-01-01", "--reason", "x"])
    assert args.policy_command == "record-gap"
    assert build_parser().parse_args(["--config", "c.yaml", "logs", "compose", "--date", "2026-01-01"]).policy_command is None


def test_open_archive_requires_the_archive(tmp_path):
    data = gold_config(tmp_path)
    with pytest.raises(ConfigError, match="not enabled"):
        open_archive(validate_config(data))


def test_cli_report_and_emit_run_without_an_engine(tmp_path, capsys, monkeypatch):
    import sys

    import yaml

    from su_multi_arm import cli

    sha = write_family(tmp_path / "policy" / "ml.yaml")
    data = gold_config(tmp_path)
    data["logs"].update(policy_logs(tmp_path, sha))
    (tmp_path / "c.yaml").write_text(yaml.safe_dump(data))
    monkeypatch.setattr(sys, "argv", ["su-multi-arm", "--config", str(tmp_path / "c.yaml"), "logs", "policy", "report", "--emit", "systemd"])
    cli._main()
    out = capsys.readouterr().out
    assert "[Timer]" in out and "logs policy import" in out
    monkeypatch.setattr(sys, "argv", ["su-multi-arm", "--config", str(tmp_path / "c.yaml"), "logs", "policy", "report"])
    cli._main()
    assert json.loads(capsys.readouterr().out)["recipe"]["rules"]


def test_documented_example_policy_validates_and_its_pin_matches(tmp_path):
    from pathlib import Path

    import yaml

    folder = Path(__file__).resolve().parents[2] / "docs" / "log_policy"
    example = yaml.safe_load((folder / "collection_policy.example.yaml").read_text())["logs"]
    data = gold_config(tmp_path)
    example["source"] = "gold"
    example["partitions"] = {"path": str(tmp_path / "partitions")}
    collection = example["collection"]
    collection["keywords"]["files"][0]["path"] = str(folder / collection["keywords"]["files"][0]["path"])
    collection["archive"]["path"] = str(tmp_path / "archive" / "a.sqlite")
    collection["staging"]["path"] = str(tmp_path / "staging")
    data["logs"].update(example)
    config = validate_config(data)
    recipe = load_keyword_recipe(config)
    assert recipe.families == ["example_ml"] and len(recipe.rules) == 3
    assert {j["name"] for j in planned_jobs(config)} == {"refresh", "daily"}
