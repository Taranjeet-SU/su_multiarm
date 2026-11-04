"""Synthetic 10-day logs gold set through the log tools (addendum A2 §6.2, L5d acceptance).

Expected answers are computed from the generator's plan (``testing/fixtures/logs/gold``), never
from the engine. Evidence level: synthetic, in-process (fake source, sqlite backend, hash embedder).
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

import pytest

from su_multi_arm.testing.fixtures.logs.gold import INCIDENT, day_of, lines_by, plan, pulled_days, select

from tests.logs_gold_support import AS_OF, SPEC, build_gold

EVENTS = plan(SPEC)
PULLED = pulled_days(SPEC)
CAPPED_DAY = day_of(SPEC, SPEC.capped[2])
EXACT_DAYS = [d for d in PULLED if d != CAPPED_DAY]


@pytest.fixture(scope="module")
def gold(tmp_path_factory):
    engine, pipe, report = build_gold(tmp_path_factory.mktemp("gold"))
    return engine, report


def tool(engine, name, **args):
    return engine.call_tool(name, {"as_of": AS_OF.isoformat(), **args} if name != "log_around" and name != "incidents" else args).result.output


def ranked(counter: dict) -> list[tuple]:
    return sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))


# --- log_top --------------------------------------------------------------------------------------


@pytest.mark.parametrize("day", PULLED)
@pytest.mark.parametrize("level", ["ERROR", "WARN"])
def test_top_services_by_level(gold, day, level):
    engine, _ = gold
    out = tool(engine, "log_top", by="service", level=[level.lower()], start=day.isoformat(), end=day.isoformat())
    expected = ranked(lines_by(select(EVENTS, days=[day], level=level), "service"))
    assert [(r["service"], r["lines"]) for r in out.rows] == expected
    assert out.exactness == "exact" and out.status == ("answered" if expected else "empty")
    assert f"index date {day.isoformat()} (index_date, UTC)" in out.rendering


@pytest.mark.parametrize("day", EXACT_DAYS)
@pytest.mark.parametrize("service", ["svc-api", "svc-billing"])
def test_top_error_signatures_for_a_service(gold, day, service):
    engine, _ = gold
    out = tool(engine, "log_top", by="signature", level=["error"], service=[service], start=day.isoformat(), end=day.isoformat())
    expected = ranked(lines_by(select(EVENTS, days=[day], level="ERROR", service=service, status="parsed"), "signature"))
    assert [(r["signature_id"], r["lines"]) for r in out.rows] == expected


def test_row_count_order_differs_from_sum_order(gold):
    """svc-api has fewer error **rows** than its line sum would suggest: ranking is by the sum."""
    engine, _ = gold
    out = tool(engine, "log_top", by="service", level=["error"], window="last 10 days")
    expected = ranked(lines_by(select(EVENTS, days=PULLED, level="ERROR"), "service"))
    assert [(r["service"], r["lines"]) for r in out.rows] == expected
    assert "source_unavailable" in out.rendering and day_of(SPEC, 4).isoformat() in out.source_unavailable


def test_fatal_tenants_ranked(gold):
    engine, _ = gold
    out = tool(engine, "log_top", by="tenant", level=["fatal"], window="yesterday")
    assert [(r["tenant"], r["lines"]) for r in out.rows] == [("t01", 2), ("t03", 2)]
    assert "FATAL-level lines" in out.rendering
    # the parsed rows miss svc-legacy's unparsed FATAL lines: bounded, never exact
    assert out.status == "bounded" and out.exactness == "bounded" and "exact." not in out.rendering


# --- level questions against the source's own counts (real-run fix) ------------------------------


def source_fatal(day) -> dict[tuple[str, str], int]:
    """Source-counted FATAL lines per (service, tenant): every FATAL event, parsed or not."""
    return dict(Counter((e.service, e.tenant) for e in select(EVENTS, days=[day], level="FATAL")))


def unparsed_fatal(day) -> dict[tuple[str, str], int]:
    return dict(Counter((e.service, e.tenant) for e in select(EVENTS, days=[day], level="FATAL", status="unparsed")))


@pytest.mark.parametrize("by", ["service", "tenant", "signature"])
def test_fatal_with_unparsed_source_lines_is_bounded_with_source_counts(gold, by):
    engine, _ = gold
    day = AS_OF - timedelta(days=1)
    out = tool(engine, "log_top", by=by, level=["fatal"], window="yesterday")
    rows = {(r["service"], r["tenant"]): r for r in out.source_counts}
    assert {k: r["lines_source"] for k, r in rows.items()} == source_fatal(day)
    assert {k: r["lines_unparsed"] for k, r in rows.items() if r["lines_unparsed"]} == unparsed_fatal(day)
    assert rows[("svc-legacy", "t02")]["basis"] == "token_match" and rows[("svc-legacy", "t02")]["source_exactness"] == "approximate"
    assert rows[("svc-auth", "t01")]["basis"] == "level_field" and rows[("svc-auth", "t01")]["source_exactness"] == "exact"
    assert out.status == "bounded" and out.exactness == "bounded"
    assert "svc-legacy t02: 2 lines containing 'fatal' (token_match, approximate), 2 unparsed" in out.rendering
    assert "parse coverage below 100%" in out.rendering and not out.rendering.endswith("exact.")


def test_no_parsed_fatal_but_source_counts_is_never_none_exact(gold):
    """D8: every FATAL line of the day is unparsed (svc-legacy t01): the old answer was "No
    FATAL-level lines ... exact"."""
    engine, _ = gold
    day = day_of(SPEC, 8)
    out = tool(engine, "log_top", by="service", level=["fatal"], start=day.isoformat(), end=day.isoformat())
    assert out.rows == [] and out.status == "bounded" and out.exactness == "bounded"
    assert out.rendering.startswith("No FATAL-level lines in the parsed rows for index date 2026-03-08")
    assert "svc-legacy t01: 1 lines containing 'fatal' (token_match, approximate), 1 unparsed" in out.rendering
    assert out.rendering.endswith("bounded.") and "exact" not in out.rendering.replace("approximate", "")
    assert [(r["service"], r["tenant"], r["lines_source"], r["lines_answered"]) for r in out.source_counts] == [("svc-legacy", "t01", 1, 0)]


def test_fully_parsed_levels_stay_exact_with_their_source_counts(gold):
    engine, _ = gold
    day = AS_OF - timedelta(days=1)
    out = tool(engine, "log_top", by="service", level=["error"], window="yesterday")
    assert out.status == "answered" and out.exactness == "exact" and "source counts" not in out.rendering
    expected = Counter((e.service, e.tenant) for e in select(EVENTS, days=[day], level="ERROR"))
    assert {(r["service"], r["tenant"]): r["lines_source"] for r in out.source_counts} == dict(expected)
    assert all(r["lines_answered"] == r["lines_source"] and r["lines_unparsed"] == 0 for r in out.source_counts)
    warn = tool(engine, "log_top", by="service", level=["warn"], window="yesterday")  # no WARN count query
    assert warn.source_counts == [] and any("no single-level source count query for WARN" in n for n in warn.notes)


def test_tenants_from_counts_report_the_unparsed_share(gold):
    engine, _ = gold
    out = tool(engine, "log_tenants", service="svc-legacy", level="fatal", window="yesterday")
    assert sorted((r["tenant"], r["lines"]) for r in out.rows) == [("t02", 2), ("t04", 1)]
    assert [(r["tenant"], r["lines_unparsed"]) for r in out.source_counts] == [("t02", 2), ("t04", 1)]
    assert "2 unparsed" in out.rendering and out.status == "answered"  # the rows are the source counts


def test_trend_and_compare_by_level_are_bounded_too(gold):
    engine, _ = gold
    trend = tool(engine, "log_trend", level=["fatal"], window="last 3 days")
    assert trend.status == "bounded" and sum(r["lines_unparsed"] for r in trend.source_counts) == 4
    compare = tool(engine, "log_compare", periods=[day_of(SPEC, 8).isoformat(), day_of(SPEC, 9).isoformat()], level=["fatal"])
    assert compare.status == "bounded" and "source counts" in compare.rendering


def test_source_counts_equal_a_pinned_aggregate_and_free_scopes_stay_guarded(gold):
    """Same numbers everywhere: the source counts of a level answer equal the pinned ``unit_day``
    aggregate a client may run itself; a kind-less scope over the same field is still refused."""
    engine, _ = gold
    day = (AS_OF - timedelta(days=1)).isoformat()
    out = tool(engine, "log_top", by="service", level=["fatal"], window="yesterday")
    scope = {
        "predicates": [
            {"field": "kind", "op": "eq", "value": "unit_day"},
            {"field": "date", "op": "date_interval", "lower": day, "upper": AS_OF.isoformat(), "granularity": "day"},
            {"field": "count_fatal", "op": "range", "lower": 1},
        ],
        "group_by": ["service", "tenant"],
        "measure": {"kind": "sum", "field": "count_fatal"},
    }
    result = engine.call_tool("aggregate", {"scope": scope}).result.output.result
    assert {(g.key["service"], g.key["tenant"]): int(g.value) for g in result.groups} == {(r["service"], r["tenant"]): r["lines_source"] for r in out.source_counts}
    free = engine.call_tool("aggregate", {"scope": {"group_by": ["service"], "measure": {"kind": "sum", "field": "count_fatal"}}}).result.output.result
    assert free.exactness == "bounded" and free.unsupported[0].reason == "intent_required"


def test_top_exception_classes(gold):
    engine, _ = gold
    out = tool(engine, "log_top", by="exception_class", window="yesterday")
    expected = ranked(Counter(e.values["exception_class"] for e in select(EVENTS, days=[AS_OF - timedelta(days=1)], signature="handler.unhandled_exception")))
    assert [(r["exception_class"], r["lines"]) for r in out.rows] == expected


def test_capped_unit_is_approximate_with_its_method(gold):
    engine, _ = gold
    out = tool(engine, "log_top", by="tenant", level=["info"], service=["svc-worker"], start=CAPPED_DAY.isoformat(), end=CAPPED_DAY.isoformat())
    assert out.exactness == "approximate" and out.methods and "approximate (" in out.rendering
    assert any("quota" in m or "hour" in m for m in out.methods)


def test_endpoint_ranking_without_a_service_is_refused(gold):
    engine, _ = gold
    out = tool(engine, "log_top", by="endpoint", window="yesterday")
    assert out.status == "refused" and "service" in out.refusal and out.rendering.startswith("Not answered")


# --- log_new, log_affected, log_signature ------------------------------------------------------


def first_seen() -> dict[str, object]:
    out: dict[str, object] = {}
    for event in sorted(select(EVENTS, days=PULLED, status="parsed"), key=lambda e: e.day):
        out.setdefault(event.signature, event.day)
    return out


@pytest.mark.parametrize("day", PULLED)
def test_new_signatures_per_day(gold, day):
    engine, _ = gold
    out = tool(engine, "log_new", start=day.isoformat(), end=day.isoformat())
    expected = sorted(sig for sig, first in first_seen().items() if first == day)
    assert sorted(r["signature_id"] for r in out.rows) == expected
    assert all(r["first_seen"] == day.isoformat() for r in out.rows)
    assert "across all composed history" in out.rendering or not expected


@pytest.mark.parametrize("signature", ["feature.flag_conflict", "process.crashed", "cache.evicted"])
def test_affected_tenants(gold, signature):
    engine, _ = gold
    out = tool(engine, "log_affected", signature_id=signature, window="last 10 days")
    expected = ranked(lines_by(select(EVENTS, days=PULLED, signature=signature), "tenant"))
    assert [(r["tenant"], r["lines"]) for r in out.rows] == expected


def test_affected_per_day(gold):
    engine, _ = gold
    out = tool(engine, "log_affected", signature_id="feature.flag_conflict", window="last 10 days", per_day=True)
    expected = sorted(Counter((e.tenant, e.day.isoformat()) for e in select(EVENTS, days=PULLED, signature="feature.flag_conflict")).items(), key=lambda kv: (kv[0][1], kv[0][0]))
    assert sorted(((r["tenant"], r["date"]), r["lines"]) for r in out.rows) == sorted(expected)


def test_signature_explained_with_template_and_tenants(gold):
    engine, _ = gold
    out = tool(engine, "log_signature", signature_id="process.crashed", window="last 10 days")
    assert sum(r["lines"] for r in out.rows) == 4 and {r["tenant"] for r in out.rows} == {"t01", "t03"}
    assert "process crashed: out of memory" in out.rendering and "first seen" in out.rendering
    kinds = {d.kind for d in engine.documents_in(engine._context_for(None), out.document_ids)}
    assert {"signature", "signature_examples", "signature_catalog"} <= kinds


# --- trend and compare ------------------------------------------------------------------------------


@pytest.mark.parametrize("service", ["svc-api", "svc-legacy"])
def test_trend_per_day_lists_missing_days_as_unavailable(gold, service):
    engine, _ = gold
    out = tool(engine, "log_trend", signature_id="dependency.connect_failed", service=service, window="last 10 days")
    expected = {d.isoformat(): len(select(EVENTS, days=[d], signature="dependency.connect_failed", service=service)) for d in PULLED}
    assert {r["date"]: r["lines"] for r in out.rows} == expected
    assert day_of(SPEC, 4).isoformat() not in {r["date"] for r in out.rows}
    assert out.source_unavailable == [day_of(SPEC, 4).isoformat()]


def test_trend_per_hour_on_the_incident_day(gold):
    engine, _ = gold
    day = day_of(SPEC, 5)
    out = tool(engine, "log_trend", signature_id="cache.evicted", granularity="hour", start=day.isoformat(), end=day.isoformat())
    expected = sorted(Counter(f"{e.hour:02d}" for e in select(EVENTS, days=[day], signature="cache.evicted")).items())
    assert [(r["ts_hour"][11:13], r["lines"]) for r in out.rows] == expected == [("13", 1), ("14", 1), ("16", 1)]


@pytest.mark.parametrize("pair", [(9, 8), (10, 9), (8, 7)])
def test_compare_two_days(gold, pair):
    engine, _ = gold
    first, second = (day_of(SPEC, n).isoformat() for n in pair)
    out = tool(engine, "log_compare", periods=[first, second], service="svc-api", level=["error"])
    expected = [len(select(EVENTS, days=[day_of(SPEC, n)], service="svc-api", level="ERROR")) for n in pair]
    assert [r["lines"] for r in out.rows] == expected and [r["period"] for r in out.rows] == [first, second]


# --- tenants (basis) ----------------------------------------------------------------------------------


def test_tenants_with_fatal_use_the_level_field_basis(gold):
    engine, _ = gold
    out = tool(engine, "log_tenants", service="svc-auth", level="fatal", window="yesterday")
    assert [(r["tenant"], r["lines"]) for r in out.rows] == [("t01", 2), ("t03", 2)]
    assert out.basis == ["level_field"] and "FATAL-level lines" in out.rendering


def test_token_basis_is_never_rendered_as_errors(gold):
    engine, _ = gold
    out = tool(engine, "log_tenants", service="svc-legacy", level="error", window="yesterday")
    assert sorted((r["tenant"], r["lines"]) for r in out.rows) == [("t01", 1), ("t02", 1), ("t03", 1), ("t04", 1)]
    assert out.basis == ["token_match"] and "lines containing 'error'" in out.rendering and "ERROR-level" not in out.rendering


# --- around, incidents, endpoints, coverage -----------------------------------------------------------


def test_around_the_incident(gold):
    engine, _ = gold
    at = f"{day_of(SPEC, 5).isoformat()}T14:00:00+00:00"
    out = tool(engine, "log_around", at=at, tenant="t02", minutes=60)
    assert sorted((r["signature_id"], r["lines"]) for r in out.rows) == sorted((s, 2) for s in INCIDENT)
    assert len(out.document_ids) == 1 and "not causality" in out.rendering


def test_incidents_recover_the_planted_one_only(gold):
    engine, report = gold
    out = tool(engine, "incidents")
    assert [(i.tenant, i.signatures) for i in out.incidents] == [("t02", sorted(INCIDENT))]
    incident = out.incidents[0]
    assert incident.cells == 3 and incident.row_exactness == "exact" and incident.lines == 9
    assert incident.peak_hour.startswith(day_of(SPEC, 5).isoformat())
    assert report["co_firing"]["incidents"] == 1


def test_endpoint_latency_regression_and_5xx(gold):
    engine, _ = gold
    day = day_of(SPEC, 8).isoformat()
    worst = tool(engine, "log_endpoints", service="svc-api", metric="latency_max", start=day, end=day)
    assert worst.rows[0]["endpoint"] == "/api/orders/{id}" and worst.rows[0]["latency_ms_max"] == 904
    errors = tool(engine, "log_endpoints", service="svc-api", metric="errors_5xx", start=day, end=day)
    assert [(r["endpoint"], r["lines"]) for r in errors.rows] == [("/api/orders/{id}", 20)]
    p95 = tool(engine, "log_endpoints", service="svc-api", metric="latency_p95", start=day, end=day, top_n=3)
    assert p95.rows and all(r["latency_ms_p95"] >= 800 for r in p95.rows) and "never added" in p95.rendering


def test_sum_of_a_per_row_p95_is_refused(gold):
    engine, _ = gold
    out = engine.call_tool("aggregate", {"scope": {"predicates": [{"field": "kind", "op": "eq", "value": "endpoint_day"}], "measure": {"kind": "sum", "field": "latency_ms_p95"}}}).result.output.result
    assert out.exactness == "bounded" and out.unsupported[0].reason == "measure_unsupported"


def test_coverage_equals_a_recount(gold):
    engine, _ = gold
    day = AS_OF - timedelta(days=1)
    out = tool(engine, "log_coverage", window="yesterday")
    for row in out.rows:
        service_events = select(EVENTS, days=[day], service=row["service"])
        parsed = sum(1 for e in service_events if e.status == "parsed")
        noise = sum(1 for e in service_events if e.status == "noise")
        assert row["lines_parsed"] == parsed and row["lines_noise"] == noise and row["lines_pulled"] == len(service_events)
        assert row["deterministic_coverage"] == round((parsed + noise) / len(service_events), 4)


# --- error rate and the undated anchor (real-run fixes) -------------------------------------------


def test_error_rate_ranks_services_by_failing_share_with_source_counts(gold):
    engine, _ = gold
    day = AS_OF - timedelta(days=1)
    out = tool(engine, "log_top", by="service", metric="error_rate", window="yesterday")
    events = select(EVENTS, days=[day])
    # the parse sees a level for every line except the token-only service's raw (unparsed) FATAL lines
    failing = Counter(e.service for e in events if e.level in {"ERROR", "FATAL"} and not e.values.get("raw"))
    total = Counter(e.service for e in events)
    expected = sorted(((s, n, total[s]) for s, n in failing.items()), key=lambda t: (-round(t[1] / t[2], 4), -t[1], t[0]))
    assert [(r["service"], r["lines"], r["lines_total"]) for r in out.rows] == expected
    assert all(r["error_rate"] == round(r["lines"] / r["lines_total"], 4) for r in out.rows)
    assert out.rendering.startswith("Top ") and "by error rate (ERROR, FATAL lines / all lines)" in out.rendering
    assert {r["level"] for r in out.source_counts} == {"ERROR", "FATAL"} and out.status == "bounded"  # svc-legacy's unparsed FATAL


def test_an_undated_question_reads_the_latest_served_day_and_says_so(gold):
    """Today (the real clock) has no composed data in the gold set: an undated question is answered
    for the latest served day, stated in the rendering, not "no lines today, exact"."""
    engine, _ = gold
    out = engine.call_tool("log_top", {"by": "service", "level": ["error"]}).result.output
    assert out.window["start"] == out.window["end"] == AS_OF.isoformat() == out.window["latest_available_day"]
    assert out.rows and f"latest available day: {AS_OF.isoformat()}" in out.rendering
    named = engine.call_tool("log_top", {"by": "service", "level": ["error"], "window": "yesterday"}).result.output
    assert named.rows == [] and named.status == "bounded" and named.source_unavailable  # not composed yet: never "none, exact"
    assert "source_unavailable" in named.rendering and not named.rendering.endswith("exact.")
