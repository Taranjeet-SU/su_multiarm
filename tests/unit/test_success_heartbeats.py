"""Success heartbeats in the keyword collection (O6, 2026-11-09): failure precedence, the 5-minute
heartbeat block, the documents of success rules (counted apart, no evidence, no community), the
failure-only documents unchanged, the recipe checks, the liveness rows and the detection of
liveness questions. Synthetic data only."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone

import pytest

from su_multi_arm.core.errors import ConfigError
from su_multi_arm.decision.log_intents import LivenessInput, LogIntentCall, detect, detect_issue_history, detect_liveness
from su_multi_arm.ingestion.episode_summaries import (
    SUCCESS,
    SummaryRule,
    check_heartbeats,
    check_precedence,
    compile_summary,
    heartbeat_buckets,
    success_phrases,
    summary_documents,
)
from su_multi_arm.ingestion.log_policy import load_keyword_recipe
from su_multi_arm.runtime.liveness import cadence, matches, runs
from tests.heartbeat_support import ALL_PHRASES, ALL_RULES, SUCCESS_FAMILY, SUCCESS_IDS, configure_hb, every, hb_snapshot
from tests.log_policy_support import FAMILY, RULES, day_snapshots

# digest of the documents of three failure-only fixture days, as built before success heartbeats
# existed (computed with the package at 2d4fed0): a failure-only recipe builds exactly these
FAILURE_ONLY_DIGEST = "6206ae35399c9e265c373988a081585aa5512cc5f0062b3a34657217363130a3"


def digest(docs) -> str:
    return hashlib.sha256(json.dumps([d.model_dump(mode="json") for d in docs], sort_keys=True).encode()).hexdigest()


# --- recipe -------------------------------------------------------------------------------------


def test_failure_only_documents_are_byte_identical_to_before():
    snaps = [s for i, d in enumerate(["2026-01-01", "2026-01-02", "2026-01-03"]) for s in day_snapshots(d, i)]
    docs = summary_documents("golden", snaps, RULES, citations=True)
    assert len(docs) == 35 and digest(docs) == FAILURE_ONLY_DIGEST


def test_success_rules_must_follow_every_failure_rule():
    check_precedence(ALL_RULES)
    with pytest.raises(ValueError, match="failure precedence"):
        check_precedence([*ALL_RULES[3:4], *ALL_RULES[:3]])
    with pytest.raises(ValueError, match="failure precedence"):
        compile_summary(ALL_PHRASES, [ALL_RULES[3], *ALL_RULES[:3]], "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00")


def test_failure_filters_are_unchanged_by_appending_success_rules():
    window = ("2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00")
    before = compile_summary(list(FAMILY["phrases"]), RULES, *window)["filters"]
    after = compile_summary(ALL_PHRASES, ALL_RULES, *window)["filters"]
    assert {r.id: after[r.id] for r in RULES} == before  # a line matching a failure rule stays a failure
    assert set(after) - set(before) == SUCCESS_IDS


def test_success_only_phrases():
    rules = [*RULES, SummaryRule(id="success.job_done", title="Job done", category=SUCCESS, alternatives=[["training job failed", "retry ok now"], ["job finished ok"]])]
    assert success_phrases(rules) == {"retry ok now", "job finished ok"}


def test_load_keyword_recipe_checks_precedence_and_the_category_field(tmp_path):
    config = configure_hb(tmp_path / "ok")
    recipe = load_keyword_recipe(config)
    assert [r.id for r in recipe.rules][-4:] == sorted(SUCCESS_IDS, key=[r["id"] for r in SUCCESS_FAMILY["rules"]].index)
    reversed_dir = tmp_path / "reversed"
    config = configure_hb(reversed_dir)
    data = config.model_dump(mode="json")
    data["logs"]["collection"]["keywords"]["files"].reverse()
    from su_multi_arm.core.config import validate_config

    with pytest.raises(ConfigError, match="failure precedence"):
        load_keyword_recipe(validate_config(data))
    with pytest.raises(ConfigError, match="category"):
        load_keyword_recipe(configure_hb(tmp_path / "nocat", category_filterable=False))


# --- heartbeat block ----------------------------------------------------------------------------


def test_heartbeat_buckets_aggregate_minutes_into_five_minute_buckets():
    snap = hb_snapshot("2026-01-02", "tenant-a", "crawler", [("success.crawl_completed", 0, 2), ("success.crawl_completed", 3, 1), ("success.crawl_completed", 7, 4), ("net.refused", 1, 9)])
    buckets = snap["heartbeats"]
    assert [(b["rule_id"], b["count"]) for b in buckets] == [("success.crawl_completed", 3), ("success.crawl_completed", 4)]
    assert buckets[0]["first"].endswith("00:00:00+00:00") and buckets[0]["last"].endswith("00:03:59+00:00")
    assert buckets[1]["bucket_start_ms"] - buckets[0]["bucket_start_ms"] == 300_000
    check_heartbeats(snap, ALL_RULES)
    assert heartbeat_buckets(snap["summary"]["series"], SUCCESS_IDS, 600)[0]["count"] == 7


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda s: s["heartbeats"][0].update(count=99), "reconcile"),
        (lambda s: s["heartbeats"].pop(), "reconcile"),
        (lambda s: s["heartbeats"][0].update(rule_id="net.refused"), "not a success rule"),
        (lambda s: s.update(heartbeat_bucket_seconds=90), "whole minutes"),
        (lambda s: s["heartbeats"][0].pop("first"), "invalid heartbeat bucket"),
    ],
)
def test_an_unreconciled_heartbeat_block_refuses_the_snapshot(mutate, message):
    snap = hb_snapshot("2026-01-02", "tenant-a", "crawler", every("success.crawl_completed", 30))
    mutate(snap)
    with pytest.raises(ValueError, match=message):
        summary_documents("hb", [snap], ALL_RULES)


def test_heartbeats_without_success_rules_are_refused_and_absent_blocks_accepted():
    snap = hb_snapshot("2026-01-02", "tenant-a", "crawler", every("success.crawl_completed", 30))
    with pytest.raises(ValueError, match="no success rule"):
        check_heartbeats(snap, RULES)
    plain = hb_snapshot("2026-01-02", "tenant-a", "crawler", every("success.crawl_completed", 30), heartbeats=False)
    assert summary_documents("hb", [plain], ALL_RULES)  # the block is optional (derivable from the series)


# --- documents ------------------------------------------------------------------------------------


def mixed_day():
    failures = [("ml.model_unavailable", 125, 3), ("net.refused", 700, 2)]
    successes = every("success.crawl_completed", 60) + every("success.scheduler_tick", 1, 0, 30)
    return failures, successes


def test_failure_documents_equal_the_failure_only_recipe_with_success_rows_beside_them():
    failures, successes = mixed_day()
    tenants = ("tenant-a", "tenant-b")
    with_success = summary_documents("hb", [hb_snapshot("2026-01-02", t, "crawler", failures + successes) for t in tenants], ALL_RULES, citations=True)
    without = summary_documents("hb", [hb_snapshot("2026-01-02", t, "crawler", failures, heartbeats=False) for t in tenants], RULES, citations=True)
    def failure_side(docs):
        return {d.id: d.model_dump(mode="json") for d in docs if d.kind not in {"summary_coverage", "unit_day"} and d.metadata.get("category") != SUCCESS and d.metadata.get("signature_id") not in SUCCESS_IDS}
    assert failure_side(with_success) == failure_side(without)
    success = [d for d in with_success if d.metadata.get("signature_id") in SUCCESS_IDS]
    assert {d.kind for d in success} == {"incident", "signature_day", "episode_cell", "signature"}
    assert all(d.metadata["category"] == SUCCESS for d in success)
    assert not [d for d in with_success if d.kind == "shared_error" and d.metadata["signature_id"] in SUCCESS_IDS]  # never a shared-error community
    runs_ = [d for d in success if d.kind == "incident"]
    crawl = [d for d in runs_ if d.metadata["signature_id"] == "success.crawl_completed"]
    assert len(crawl) == 2 * 24 and all(d.metadata["episode_status"] == "success_heartbeat" and d.metadata["evidence_count"] == 0 for d in crawl)
    assert all("community_id" not in d.metadata for d in runs_) and all("exemplars" not in d.fields for d in runs_)
    ticks = [d for d in runs_ if d.metadata["signature_id"] == "success.scheduler_tick"]
    assert len(ticks) == 2 and ticks[0].metadata["occurrences"] == 30  # one run per tenant: ticks a minute apart


def test_coverage_counts_the_failure_side_and_success_apart():
    failures, successes = mixed_day()
    snap = hb_snapshot("2026-01-02", "tenant-a", "crawler", failures + successes)
    [coverage] = [d for d in summary_documents("hb", [snap], ALL_RULES) if d.kind == "summary_coverage"]
    summary = snap["summary"]
    m = coverage.metadata
    assert m["success_documents"] == 24 + 30 and m["rule_documents"] == 5 == summary["rule_documents"] - 54
    assert m["keyword_documents"] == summary["keyword_documents"] - 54
    assert m["episodes"] == 2 and m["success_episodes"] == 24 + 1
    assert m["unclassified_documents"] == summary["unclassified_documents"]
    assert set(dict(json.loads(coverage.fields["success_keyword_counts"]))) == set(SUCCESS_FAMILY["phrases"])
    assert not set(dict(json.loads(coverage.fields["keyword_counts"]))) & set(SUCCESS_FAMILY["phrases"])


# --- liveness arithmetic ------------------------------------------------------------------------------


def piece(first: str, last: str, rule: str = "success.crawl_completed", n: int = 1) -> dict:
    return {"first_failure": first, "last_failure": last, "occurrences": n, "signature_id": rule, "issue": "Crawl completed", "doc_id": first, "tier": "archive"}


def test_runs_merge_pieces_within_the_gap_and_cadence_is_the_median_interval():
    pieces = [piece(f"2026-01-02T{h:02d}:00:00+00:00", f"2026-01-02T{h:02d}:00:59+00:00") for h in (0, 1, 2, 3, 5)]
    pieces.insert(1, piece("2026-01-02T00:04:00+00:00", "2026-01-02T00:04:59+00:00"))  # 3 min later: same run
    merged = runs(pieces, 300)
    assert len(merged) == 5 and merged[0]["occurrences"] == 2
    seconds, basis, used = cadence(merged, set(), 3, 300)
    assert (seconds, basis, used) == (3600.0, "median_interval", 4)
    assert cadence(merged, {"2026-01-02"}, 3, 300)[1] == "insufficient_intervals"  # intervals over an unusable day are dropped
    continuous = runs([piece("2026-01-02T00:00:00+00:00", "2026-01-02T05:00:59+00:00", "success.scheduler_tick", 300)], 300)
    assert cadence(continuous, set(), 3, 300) == (300.0, "continuous", 0)
    assert cadence(merged[:2], set(), 3, 300) == (None, "insufficient_intervals", 1)


def test_event_names_match_rule_ids_or_their_event_part():
    assert matches("success.crawl_completed", "crawl_completed") and matches("success.crawl_completed", "success.crawl_completed")
    assert not matches("success.crawl_completed", "crawl")


def test_liveness_input_needs_an_aware_instant():
    LivenessInput(at=datetime(2026, 1, 2, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="timezone"):
        LivenessInput(at=datetime(2026, 1, 2))
    call = LogIntentCall(intent="log_liveness", args={"tenant": "tenant-a"})
    assert call.args == {"tenant": "tenant-a", "recovery": False, "top_n": 50}


# --- detection --------------------------------------------------------------------------------------


KNOWN = {
    "service": ["crawler", "indexer", "ml-serving"],
    "tenant": ["tenant-a", "tenant-b"],
    "signature_id": ["net.refused", "ml.training_failed", "scheduler.job_halted", *sorted(SUCCESS_IDS)],
}


def values(field):
    return KNOWN.get(field, [])


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Has the crawl for tenant-a halted?", {"tenant": "tenant-a", "event": ["success.crawl_completed"], "recovery": False}),
        ("Is the indexer still running?", {"service": ["indexer"], "event": ["success.index_completed"], "recovery": False}),
        ("When did indexing last complete for tenant-b?", {"tenant": "tenant-b", "event": ["success.index_completed"], "recovery": False}),
        ("Did the crawler stop over the last 14 days?", {"service": ["crawler"], "event": ["success.crawl_completed"], "lookback_days": 14, "recovery": False}),
        ("Was the ml-serving outage fixed automatically?", {"service": ["ml-serving"], "recovery": True}),
        ("Did tenant-b recover on its own after the failures?", {"tenant": "tenant-b", "recovery": True}),
        ("When were reports last delivered?", {"event": ["success.report_delivered"], "recovery": False}),
    ],
)
def test_detect_liveness(question, expected):
    call = detect_liveness(question, as_of=date(2026, 1, 6), values=values)
    assert call is not None and call.intent == "log_liveness"
    got = {k: v for k, v in call.args.items() if k not in {"as_of", "top_n"}}
    assert got == expected
    assert detect_issue_history(question, as_of=date(2026, 1, 6), values=values) == call  # the archive detector routes it


@pytest.mark.parametrize(
    "question",
    [
        "How many ML outages were there in the last 3 months?",
        "How many job halted incidents were there in the last 30 days?",
        "Which tenants had connection refused failures last week?",
        "top errors for crawler yesterday",
    ],
)
def test_failure_questions_are_not_liveness(question):
    assert detect_liveness(question, as_of=date(2026, 1, 6), values=values) is None


def test_a_failure_rule_named_by_an_event_word_is_kept_for_the_runtime_to_drop():
    call = detect_liveness("Has the job for tenant-a halted?", as_of=date(2026, 1, 6), values=values)
    assert call.args["event"] == ["scheduler.job_halted"]  # not a success rule: the runtime ignores it and says so
    assert detect("Has the job for tenant-a halted?", as_of=date(2026, 1, 6), values=values) is None  # generation intents unchanged


def test_history_exclusion_keeps_null_categories_and_refuses_unknown_fields(tmp_path):
    import sqlite3

    from su_multi_arm.ingestion.episode_history import DayTier, HistorySpec, HistoryUnavailable, TieredAggregate, ensure_schema, insert_rows, issue_rows

    path = tmp_path / "p.sqlite"
    db = sqlite3.connect(path)
    ensure_schema(db)
    base = {"day": "2026-01-02", "source": "p", "env": "prod", "service": "crawler", "issue": "x", "occurrences": 2, "evidence_count": 0, "continues_id": None,
            "is_continuation": 0, "boundary_unknown": 0, "partial": 0, "provisional": 1, "as_of": "2026-01-02T12:00:00+00:00",
            "first_failure": "2026-01-02T01:00:00+00:00", "last_failure": "2026-01-02T01:00:59+00:00", "chain_first": "2026-01-02T01:00:00+00:00"}
    pieces = [{**base, "doc_id": "a", "tenant": "t1", "signature_id": "net.refused", "category": "connectivity"},
              {**base, "doc_id": "b", "tenant": "t1", "signature_id": "legacy.rule", "category": None},
              {**base, "doc_id": "c", "tenant": "t1", "signature_id": "success.crawl_completed", "category": SUCCESS, "occurrences": 7}]
    insert_rows(db, pieces, issue_rows("2026-01-02", "p", pieces), [])
    db.commit()
    db.close()
    tiers = {"2026-01-02": DayTier("provisional", source=str(path))}
    result = TieredAggregate(None, tiers).execute(HistorySpec(first="2026-01-02", last="2026-01-02", group_by=["signature_id"], exclude={"category": [SUCCESS]}))
    assert {g["signature_id"] for g in result.groups} == {"net.refused", "legacy.rule"} and result.excluded_occurrences == 7
    assert [p["doc_id"] for p in TieredAggregate(None, tiers).pieces(HistorySpec(first="2026-01-02", last="2026-01-02", filters={"category": [SUCCESS]}))] == ["c"]
    with pytest.raises(HistoryUnavailable, match="more than 1"):
        TieredAggregate(None, tiers).pieces(HistorySpec(first="2026-01-02", last="2026-01-02"), limit=1)
    with pytest.raises(HistoryUnavailable, match="cannot exclude"):
        TieredAggregate(None, tiers).execute(HistorySpec(first="2026-01-02", last="2026-01-02", exclude={"issue": ["x"]}))
