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
