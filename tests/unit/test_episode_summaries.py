"""Synthetic source-response and observed-episode invariants."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from su_multi_arm.ingestion.episode_summaries import (
    SummaryRule, compile_summary, episodes, evidence_request, summary_documents, validate_summary,
)

PHRASES = ["failed", "error", "connection refused"]
RULES = [SummaryRule(id="redis.refused", title="Redis connection refused", category="connectivity",
                     alternatives=[["connection refused", "redis"]]),
         SummaryRule(id="connection.refused", title="Connection refused", category="connectivity",
                     alternatives=[["connection refused"]])]
SCOPE = {"index": "synthetic-2026.01.01", "tenant": "synthetic", "env": "test", "service": "worker"}


def compiled():
    return compile_summary(PHRASES, RULES, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")


def millis(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()*1000)


def response():
    start = millis("2026-01-01T10:00:00")
    return {"timed_out": False, "_shards": {"total": 1, "successful": 1, "failed": 0},
            "hits": {"total": {"value": 10001, "relation": "eq"}}, "aggregations": {
                "source_window": {"bounded": {"doc_count": 20000}},
                "unclassified": {"doc_count": 1},
                "keywords": {"buckets": {"0": {"doc_count": 0}, "1": {"doc_count": 1}, "2": {"doc_count": 10000}}},
                "rules": {"buckets": {
                    "redis.refused": {"doc_count": 10000, "minutes": {"buckets": [
                        {"key": start, "doc_count": 6000, "first": {"value": start}, "last": {"value": start+59000}},
                        {"key": start+60000, "doc_count": 4000, "first": {"value": start+60000}, "last": {"value": start+119000}},
                    ]}}, "connection.refused": {"doc_count": 0, "minutes": {"buckets": []}}}}}}


def test_ten_thousand_repeats_form_one_episode_not_ten_thousand_documents():
    summary = validate_summary(response(), compiled(), RULES, PHRASES)
    result = episodes(summary, SCOPE, RULES)
    assert len(result) == 1 and result[0]["occurrences"] == 10000
    assert result[0]["observed_window_seconds"] == 119
    assert result[0]["recovered_at"] is None and result[0]["resolution_seconds"] is None
    assert result[0]["quiet_at_window_end"] is True
    request = evidence_request(compiled(), result[0], includes=["message", "@timestamp"])
    assert request["size"] == 0
    assert sum(v["top_hits"]["size"] for v in request["aggs"].values()) == 2
    assert "scroll" not in request


def test_more_specific_rule_excludes_later_generic_rule():
    c = compiled()
    assert "must_not" not in c["filters"][RULES[0].id]["bool"]
    assert c["filters"][RULES[1].id]["bool"]["must_not"]
    assert c["request"]["query"] == c["query"]
    assert "native_severity" not in str(c) and "native_http_status" not in str(c)


def test_quiet_gap_reopens_and_tenants_stay_separate():
    summary = validate_summary(response(), compiled(), RULES, PHRASES)
    summary["series"][1]["first"] = "2026-01-01T10:10:00+00:00"
    summary["series"][1]["last"] = "2026-01-01T10:10:59+00:00"
    result = episodes(summary, SCOPE, RULES)
    assert len(result) == 2
    other = episodes(summary, {**SCOPE, "tenant": "other"}, RULES)
    assert {r["id"] for r in result}.isdisjoint(r["id"] for r in other)


@pytest.mark.parametrize("mutation", ["timeout", "shard", "count", "minute", "missing", "keyword", "window"])
def test_incomplete_or_inconsistent_summary_refused(mutation):
    r = response()
    if mutation == "timeout":
        r["timed_out"] = True
    elif mutation == "shard":
        r["_shards"]["failed"] = 1
    elif mutation == "count":
        r["aggregations"]["unclassified"]["doc_count"] = 0
    elif mutation == "missing":
        del r["aggregations"]["rules"]["buckets"][RULES[1].id]
    elif mutation == "keyword":
        r["aggregations"]["keywords"]["buckets"]["2"]["doc_count"] = 0
    else:
        m = r["aggregations"]["rules"]["buckets"][RULES[0].id]["minutes"]["buckets"][0]
        if mutation == "minute":
            m["doc_count"] -= 1
        else:
            m["first"]["value"] = millis("2025-12-31T10:00:00")
    with pytest.raises(ValueError):
        validate_summary(r, compiled(), RULES, PHRASES)


def test_candidate_count_is_not_confirmed_incident_and_documents_preserve_unknown():
    summary = validate_summary(response(), compiled(), RULES, PHRASES)
    snap = {"scope": SCOPE, "summary": summary, "episodes": episodes(summary, SCOPE, RULES),
            "observed_at": "2026-01-02T00:00:00Z", "unclassified_examples": [{"message": '{"error": null}'}]}
    docs = summary_documents("synthetic", [snap], RULES)
    coverage = next(d for d in docs if d.kind == "summary_coverage")
    assert coverage.metadata["keyword_documents"] == 10001
    assert coverage.metadata["unclassified_documents"] == 1
    assert sum(d.metadata["occurrences"] for d in docs if d.kind == "episode_cell") == 10000
    incident = next(d for d in docs if d.kind == "incident")
    assert incident.metadata["recovery_basis"] == "not_established"
    assert "duration" not in incident.metadata
    assert any(d.kind == "unclassified_example" for d in docs)
    with pytest.raises(ValueError, match="duplicate"):
        summary_documents("synthetic", [snap, deepcopy(snap)], RULES)


def test_one_occurrence_needs_one_source_example():
    episode = {"first_failure": "2026-01-01T10:00:00Z", "last_failure": "2026-01-01T10:00:00Z",
               "rule_id": RULES[0].id, "occurrences": 1}
    assert len(evidence_request(compiled(), episode, includes=["message"])["aggs"]) == 1


def test_keyword_counters_survive_secret_redaction_and_shared_rules_form_communities():
    import json
    from su_multi_arm.core.config import PrivacyConfig
    from su_multi_arm.privacy.redactors import safe_redactor
    from su_multi_arm.communities.base import declared_communities

    summary = validate_summary(response(), compiled(), RULES, PHRASES)
    summary["keyword_counts_nonexclusive"].update({"invalid token": 12, "api_key": 0})
    snapshots = []
    for tenant in ("synthetic", "other"):
        scope = {**SCOPE, "tenant": tenant, "index": tenant + "-2026.01.01"}
        snapshots.append({"scope": scope, "summary": summary, "episodes": episodes(summary, scope, RULES),
                          "observed_at": "2026-01-02T00:00:00Z"})
    docs = summary_documents("synthetic-summaries", snapshots, RULES)
    redactor = safe_redactor(PrivacyConfig(engine="safe", redactors=["api_key"]))
    for doc in docs:
        if doc.kind == "summary_coverage":
            counters = dict(json.loads(redactor(doc.fields["keyword_counts"])))
            assert counters["invalid token"] == 12 and counters["api_key"] == 0
    groups = declared_communities(docs, [], 1)
    shared = [g for g in groups if g.id.startswith("shared_rule:")]
    observed = [g for g in groups if g.id.startswith("observed_rule:")]
    assert len(shared) == 1 and len(shared[0].document_ids) == 2
    assert len(observed) == 2 and all(g.stats["episodes"] == 1 for g in observed)


def test_summary_source_fields_are_mapped_and_fingerprint_changes():
    from su_multi_arm.core.config_sources import LogFieldMap

    custom = compile_summary(PHRASES, RULES, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z",
                             field_map=LogFieldMap(message="event.text", timestamp="event.at"))
    default = compiled()
    assert custom["recipe"] != default["recipe"]
    rendered = str(custom["request"])
    assert "event.text" in rendered and "event.at" in rendered
    assert "@timestamp" not in rendered and "'message'" not in rendered
    summary = validate_summary(response(), custom, RULES, PHRASES)
    episode = episodes(summary, SCOPE, RULES)[0]
    request = evidence_request(custom, episode, includes=["event.text", "event.at"])
    assert "event.at" in str(request) and "@timestamp" not in str(request)
    assert request["aggs"]["first"]["top_hits"]["sort"] == [{"event.at": "asc"}]
