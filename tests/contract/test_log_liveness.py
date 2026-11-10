"""Success heartbeats through the engine (O6): an archive whose first days were captured under a
failure-only keyword recipe and whose later days (``keywords.effective_from``) carry the success
family, plus one provisional day. ``log_liveness`` (last success, cadence, halted/running,
recovery), the failure views without success rows (``issue_history``, ``incident_episodes``, the
summary ``log_top``), the old epoch's shards unchanged, and the rules route. Synthetic data only."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from su_multi_arm.engine import Engine
from su_multi_arm.ingestion.episode_archive import ArchiveRefused, EpisodeArchive
from su_multi_arm.ingestion.episode_summaries import SUCCESS
from tests.heartbeat_support import SUCCESS_IDS, configure_hb, every, hb_snapshot
from tests.history_support import stage, utc
from tests.log_policy_support import day_snapshots

AT = "2026-01-06T12:00:00+00:00"


def archive_at(config, now, store=None):
    return EpisodeArchive(config, now=lambda: now, store=store)


def new_day(day: str) -> list[dict]:
    """tenant-a's hourly crawl stops after 10:00 on 2026-01-05 and a failure follows at 11:40;
    tenant-b crawls hourly and has a failure at 02:05 on 2026-01-04."""
    a = every("success.crawl_completed", 60, 0, 601 if day == "2026-01-05" else 1440)
    b = every("success.crawl_completed", 60)
    if day == "2026-01-05":
        a.append(("ml.model_unavailable", 700, 3))
    if day == "2026-01-04":
        b.append(("net.refused", 125, 2))
    extra = [hb_snapshot(day, "tenant-a", "ml-serving", [])] if day == "2026-01-04" else []  # captured, no heartbeat rule fires
    return [hb_snapshot(day, "tenant-a", "crawler", a), hb_snapshot(day, "tenant-b", "crawler", b), *extra]


@pytest.fixture
def two_epochs(tmp_path):
    old = configure_hb(tmp_path, success=False)
    engine = Engine(old)
    first = archive_at(old, utc("2026-01-03T02:00:00"))
    for i, day in enumerate(("2026-01-01", "2026-01-02")):
        first.close_day(day, day_snapshots(day, i))
    first.publish(engine.write_path, date(2026, 1, 3))
    old_shards = {d: row["shard_id"] for d, row in first.store.days().items()}
    # the success family pinned without a new epoch: never mixed into the archived recipe
    mixed = archive_at(configure_hb(tmp_path), utc("2026-01-04T02:00:00"), first.store)
    with pytest.raises(ArchiveRefused, match="effective_from"):
        mixed.close_day("2026-01-03", new_day("2026-01-03"))
    config = configure_hb(tmp_path, effective_from="2026-01-03")
    engine = Engine(config)
    archive = archive_at(config, utc("2026-01-06T02:00:00"), first.store)
    for day in ("2026-01-03", "2026-01-04", "2026-01-05"):
        archive.close_day(day, new_day(day))
    archive.publish(engine.write_path, date(2026, 1, 6))
    rolling = [hb_snapshot("2026-01-06", "tenant-a", "crawler", [], until_minute=720),
               hb_snapshot("2026-01-06", "tenant-b", "crawler", every("success.crawl_completed", 60), until_minute=720)]
    stage(tmp_path / "staging", "2026-01-06", rolling)
    archive_at(config, utc("2026-01-06T12:00:00"), archive.store).refresh_provisional()
    return engine, archive, old_shards, old


def call(engine, name, args, **kw):
    return engine.call_tool(name, args, **kw).result.output


def test_old_epoch_days_keep_their_shards_and_recipe(two_epochs):
    engine, archive, old_shards, _ = two_epochs
    days = archive.store.days()
    assert {d: days[d]["shard_id"] for d in old_shards} == old_shards
    recipes = {d: row["recipe"] for d, row in days.items()}
    assert recipes["2026-01-01"] == recipes["2026-01-02"] != recipes["2026-01-03"] == recipes["2026-01-05"]


def test_halted_and_running_from_success_heartbeats(two_epochs):
    engine, *_ = two_epochs
    out = call(engine, "log_liveness", {"at": AT})
    assert out.status == "bounded" and out.window["start"] == "2025-12-31" and out.window["end"] == "2026-01-06"
    rows = {r["tenant"]: r for r in out.rows}
    a, b = rows["tenant-a"], rows["tenant-b"]
    assert (a["status"], a["event"], a["last_success"]) == ("halted", "success.crawl_completed", "2026-01-05T10:00:59+00:00")
    assert a["observed_until"] == AT and a["quiet_seconds"] == 26 * 3600 - 59
    assert (a["cadence_seconds"], a["cadence_basis"], a["threshold_seconds"]) == (3600.0, "median_interval", 3 * 3600.0)
    assert (b["status"], b["last_success"], b["quiet_seconds"]) == ("running", "2026-01-06T11:00:59+00:00", 3541.0)
    assert b["provisional"] is True and a["provisional"] is False
    assert out.rows[0]["tenant"] == "tenant-a"  # halted first
    coverage = out.history["coverage"]
    assert coverage["pre_heartbeat_days"] == ["2025-12-31", "2026-01-01", "2026-01-02"]
    assert coverage["heartbeat_days"] == ["2026-01-03", "2026-01-04", "2026-01-05", "2026-01-06"]
    text = " ".join(out.notes)
    assert "keywords.effective_from 2026-01-03" in text and "not evidence of a halt" in text and "not proof" in text
    assert out.document_ids and "1 halted, 1 running" in out.rendering


def test_halt_factor_and_lookback_are_configurable(two_epochs):
    engine, *_ = two_epochs
    data = engine.config.model_dump(mode="json")
    data["logs"]["collection"]["liveness"] = {"halt_factor": 30.0, "cadence_days": 2, "min_intervals": 3}
    from su_multi_arm.core.config import validate_config

    relaxed = Engine(validate_config(data))
    out = call(relaxed, "log_liveness", {"at": AT, "tenant": "tenant-a"})
    [row] = out.rows
    assert row["status"] == "running" and row["threshold_seconds"] == 30 * 3600.0
    assert out.window["start"] == "2026-01-05"


def test_recovery_is_the_first_success_after_a_failure_episode(two_epochs):
    engine, *_ = two_epochs
    out = call(engine, "log_liveness", {"at": AT, "recovery": True})
    recovery = {(r["tenant"], r["signature_id"], r["first_failure"][:10]): r for r in out.history["recovery"]}
    b = recovery[("tenant-b", "net.refused", "2026-01-04")]
    assert (b["status"], b["recovered_at"], b["recovered_by"]) == ("recovered", "2026-01-04T03:00:00+00:00", "success.crawl_completed")
    assert (b["seconds_after_last_failure"], b["time_to_recovery_seconds"], b["success_during_episode"]) == (3241.0, 3300.0, False)
    a = recovery[("tenant-a", "ml.model_unavailable", "2026-01-05")]
    assert a["status"] == "not_recovered" and a["recovered_at"] is None and a["observed_until"] == AT
    old = recovery[("tenant-a", "ml.model_unavailable", "2026-01-01")]
    assert old["status"] == "predates_heartbeats"  # captured before the success family existed
    assert all(r["category"] != SUCCESS for r in out.history["recovery"])
    assert "temporal sequence, not proof" in " ".join(out.notes) and "1 followed by a success heartbeat" in out.rendering


def test_named_scope_without_heartbeats_and_event_filters(two_epochs):
    engine, *_ = two_epochs
    out = call(engine, "log_liveness", {"at": AT, "service": ["ml-serving"]})
    assert {(r["tenant"], r["status"]) for r in out.rows} == {("tenant-a", "no_heartbeats")}
    assert any("1 named scopes were captured only before heartbeat recording" in n and "tenant-b/prod/ml-serving" in n for n in out.notes)
    only = call(engine, "log_liveness", {"at": AT, "event": ["crawl_completed", "net.refused"], "tenant": "tenant-b"})
    assert [r["event"] for r in only.rows] == ["success.crawl_completed"]
    assert any("Not success heartbeat rules, ignored: net.refused" in n for n in only.notes)
    refused = call(engine, "log_liveness", {"at": AT, "event": ["net.refused"]})
    assert refused.status == "refused" and "success.crawl_completed" in refused.refusal


def test_a_lookback_before_heartbeat_recording_says_so(two_epochs):
    engine, *_ = two_epochs
    out = call(engine, "log_liveness", {"at": "2026-01-02T12:00:00+00:00"})
    assert out.status == "empty" and "predates heartbeat recording" in out.rendering and not out.rows


def test_liveness_needs_success_rules_and_is_operator_only(two_epochs, tmp_path):
    engine, _, _, old = two_epochs
    out = call(Engine(old), "log_liveness", {"at": AT})
    assert out.status == "refused" and "no success heartbeats" in out.refusal
    from su_multi_arm.runtime.acl import Principal, StaticPolicy

    engine.acl = StaticPolicy("tenant")
    principal = Principal(id="reader", tenant="tenant-a", scopes=["search", "answer", "tools"])
    out = call(engine, "log_liveness", {"at": AT}, principal=principal)
    assert out.status == "refused" and "operator-only" in out.refusal


def test_failure_views_exclude_success_heartbeats(two_epochs):
    engine, *_ = two_epochs
    history = call(engine, "issue_history", {"start": "2026-01-01", "end": "2026-01-06", "as_of": "2026-01-06", "group_by": ["category", "signature_id"]})
    assert history.status == "bounded" and {r["category"] for r in history.rows} == {"ml_serving", "ml_training", "connectivity"}
    failures = sum(r["occurrences"] for r in history.rows)
    assert failures == 5 + 5 + 6 + 3 + 2  # day 1 (two tenants), day 2 (tenant-a), day 4, day 5
    assert any("success-heartbeat messages (category success)" in n for n in history.notes)
    success = call(engine, "issue_history", {"start": "2026-01-03", "end": "2026-01-06", "as_of": "2026-01-06", "category": [SUCCESS]})
    assert {r["category"] for r in success.rows} == {SUCCESS} and not any("success-heartbeat messages" in n for n in success.notes)
    assert sum(r["occurrences"] for r in success.rows) == 24 * 3 + 12 + 24 * 2 + 11  # tenant-b: 3 days + day 6 to noon; tenant-a: 2 days + day 5 to 10:00
    episodes = call(engine, "incident_episodes", {"start": "2026-01-04", "end": "2026-01-05", "as_of": "2026-01-05"})
    assert episodes.rows and all(r["category"] != SUCCESS for r in episodes.rows)
    assert episodes.statistics["rule_documents"] == 5 and episodes.statistics["episodes_total"] == 2
    assert not set(episodes.keywords) & {"crawl run finished ok"}
    top = call(engine, "log_top", {"start": "2026-01-03", "end": "2026-01-05", "as_of": "2026-01-05", "by": "signature"})
    assert top.rows and not {r.get("signature_id") for r in top.rows} & SUCCESS_IDS
    named = call(engine, "log_top", {"start": "2026-01-03", "end": "2026-01-05", "as_of": "2026-01-05", "by": "signature", "top_n": 50})
    assert sum(r["lines"] for r in named.rows) == 5


def test_rules_route_liveness_questions(two_epochs):
    from su_multi_arm.models import SearchRequest

    engine, *_ = two_epochs
    result = engine.answer(SearchRequest(query="Has the crawl for tenant-a halted?", as_of="2026-01-06"))
    [record] = result.retrieval.aggregates
    assert record["intent"] == "log_liveness" and record["args"]["tenant"] == "tenant-a"
    assert record["args"]["event"] == ["success.crawl_completed"]
    assert [(r["service"], r["status"]) for r in record["rows"]] == [("crawler", "halted"), ("ml-serving", "no_heartbeats")]
    recovered = engine.answer(SearchRequest(query="Did tenant-b recover on its own?", as_of="2026-01-06"))
    [record] = recovered.retrieval.aggregates
    assert record["intent"] == "log_liveness" and record["args"]["recovery"] is True
    history = engine.answer(SearchRequest(query="How many ML outages were there in the last 5 days?", as_of="2026-01-06"))
    [record] = history.retrieval.aggregates
    assert record["intent"] == "issue_history"


def test_the_engine_clock_is_now_and_a_past_as_of_ends_its_day(two_epochs):
    engine, *_ = two_epochs
    out = call(engine, "log_liveness", {"as_of": "2026-01-05", "tenant": "tenant-b"})
    assert out.history["now"] == "2026-01-06T00:00:00+00:00" and out.window["end"] == "2026-01-05"
    assert datetime.fromisoformat(out.rows[0]["observed_until"]) <= datetime.fromisoformat(out.history["now"])



def test_recovery_question_shows_the_episodes_and_the_caveat(two_epochs):
    engine, *_ = two_epochs
    out = call(engine, "log_liveness", {"at": AT, "recovery": True, "tenant": "tenant-b"})
    assert out.columns[-1] == "time_to_recovery" and out.rows == out.history["recovery"]
    assert out.rows[0]["time_to_recovery"] == "55m" and out.history["series"][0]["status"] == "running"
    assert "not proof that the failed operation recovered" in out.rendering


def test_outage_investigation_selects_failure_incidents_only(two_epochs):
    from su_multi_arm.core.types import Scope
    from su_multi_arm.decision.log_intents import LogEvidenceScope
    from su_multi_arm.runtime.log_investigation import prepare

    engine, *_ = two_epochs
    seen = []

    class Ops:
        def __getattr__(self, name):
            return getattr(engine, name)

        def log_execute(self, ctx, spec):
            seen.append(spec)
            return engine.log_execute(ctx, spec)

    scope, _, report = prepare(Ops(), engine._context_for(None), LogEvidenceScope(as_of=date(2026, 1, 5), window="last 3 days"), Scope(), "outage")
    docs = engine.documents_in(engine._context_for(None), scope.document_ids)
    assert docs and all(d.metadata.get("category") != SUCCESS for d in docs)
    assert any(p.field == "category" and p.negate for p in seen[0].predicates)
