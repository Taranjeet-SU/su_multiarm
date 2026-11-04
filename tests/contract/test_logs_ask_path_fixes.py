"""Real-deployment smoke fixes on the Ask path (2026-11-04, fix4), on the synthetic gold set composed
through the real pipeline (fake source, sqlite, hash embedder; planner scripted, never called).

1. The day of a log question on ``search``/``answer``/``answer_stream`` (rules, cascade and the llm
   planner): a clock default is not a caller-named ``as_of``. A question without a date anchors on
   the latest served day and says so; a relative word reads the clock and, when that day is not
   served, answers ``source_unavailable`` plus the latest available day (never silently shifted).
2. Level questions against a source whose only failure count query covers ERROR and FATAL together
   (no single-level FATAL query): FATAL alone, ERROR alone, WARN alone and mixes all cross-check the
   unit source counts.
3. ``deployment_id`` (numeric index-name suffix) on the served rows: filter, group and ``by:
   deployment``.

Expected numbers come from the generator's plan, never from the engine. Evidence: contract
(synthetic, in-process)."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from su_multi_arm import SearchRequest
from su_multi_arm.core.config import validate_config
from su_multi_arm.engine import Engine
from su_multi_arm.ingestion.schedule import LogsPipeline
from su_multi_arm.providers import JsonModel
from su_multi_arm.sources import build_source
from su_multi_arm.testing.fixtures.logs import FakeLogCluster
from su_multi_arm.testing.fixtures.logs.gold import SERVICES, day_of, indices, plan, pulled_days, select

from tests.logs_gold_support import AS_OF, SPEC, Clock, build_gold, gold_config, write_gold_library
from tests.logs_support import ENV, no_wait, redact

EVENTS = plan(SPEC)
SECRET_ENV = "SU_TEST_ASK_PATH_KEY"
FAKE = {"provider": "openai_compatible", "model": "fake-local", "base_url": "http://127.0.0.1:9/v1", "api_key_env": SECRET_ENV}
D9, D10, D11, D12 = AS_OF - timedelta(days=1), AS_OF, AS_OF + timedelta(days=1), AS_OF + timedelta(days=2)


def ask_config(tmp: Path) -> dict:
    data = gold_config(tmp)
    data["models"]["roles"]["planner"] = FAKE
    data["pipeline"]["decision"] = {"kind": "cascade", "stages": ["rules", "llm"]}
    data["request"] = {"modes": ["rules", "llm", "cascade"]}
    return data


@pytest.fixture(scope="module")
def gold(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ask")
    mp = pytest.MonkeyPatch()
    mp.setenv(SECRET_ENV, "synthetic-not-a-key")
    engine, _, _ = build_gold(tmp, data=ask_config(tmp))
    yield engine
    mp.undo()


def at(engine: Engine, day: date) -> Engine:
    """Pin the engine's wall clock to 06:00 UTC of ``day``."""
    engine._clock = lambda zone: datetime.combine(day, time(6), tzinfo=zone).astimezone(zone)  # type: ignore[method-assign]
    return engine


def log_aggregate(result):
    retrieval = getattr(result, "retrieval", result)
    found = [a for a in retrieval.aggregates if a.get("intent")]
    assert len(found) == 1, retrieval.aggregates
    return found[0]


class Planner:
    """Scripted llm planner: one log intent with the given arguments (``as_of`` echoes the
    payload's ``time_anchor`` when ``echo`` is set, as a model copying the anchor would)."""

    def __init__(self, intent: str, args: dict, echo: bool = False):
        self.intent, self.args, self.echo, self.payloads = intent, args, echo, []

    def __call__(self, model, system, payload, schema, budget, stage):
        budget.reserve()
        self.payloads.append(payload)
        args = dict(self.args)
        if self.echo and payload.get("time_anchor"):
            args["as_of"] = payload["time_anchor"]
        pairs = [{"field": k, "value": item} for k, v in args.items() for item in (v if isinstance(v, list) else [v])]
        return schema.model_validate({"steps": [{"arm": "structured", "log_intent": self.intent, "log_args": pairs}]})


@pytest.fixture
def planner(monkeypatch):
    holder: dict = {}

    def install(intent: str, args: dict, echo: bool = False) -> Planner:
        holder["p"] = Planner(intent, args, echo)
        return holder["p"]

    monkeypatch.setattr(JsonModel, "call", lambda self, *a: holder["p"](self, *a))
    monkeypatch.setenv(SECRET_ENV, "synthetic-not-a-key")
    return install


# --- 1. the day of an Ask ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["rules", "cascade"])
def test_an_undated_ask_answers_for_the_latest_served_day_not_the_clock_day(gold, mode):
    """The smoke defect: ``/v2/answer`` filled ``as_of`` from the clock, which disabled the anchor,
    so every undated question answered for today with no data."""
    at(gold, D12)
    agg = log_aggregate(gold.answer(SearchRequest(query="which services had FATAL", mode=mode)))
    assert "as_of" not in agg["args"] and "window" not in agg["args"]  # the clock day is not pinned
    assert f"index date {D10.isoformat()}" in agg["rendering"] and f"latest available day: {D10.isoformat()}" in agg["rendering"]
    expected = Counter(e.service for e in select(EVENTS, days=[D10], level="FATAL", status="parsed"))
    assert {r["service"]: r["lines"] for r in agg["rows"]} == dict(expected)


def test_the_answer_stream_anchors_the_same_way(gold):
    at(gold, D12)
    events = list(gold.answer_stream(SearchRequest(query="which services had ERROR", mode="rules")))
    done = events[-1].result
    agg = log_aggregate(done)
    assert agg["args"].get("level") == ["error"] and "as_of" not in agg["args"]
    expected = sorted(Counter(e.service for e in select(EVENTS, days=[D10], level="ERROR")).items(), key=lambda kv: (-kv[1], kv[0]))
    assert [(r["service"], r["lines"]) for r in agg["rows"]] == expected
    assert f"latest available day: {D10.isoformat()}" in agg["rendering"]
    assert agg == log_aggregate(gold.answer(SearchRequest(query="which services had ERROR", mode="rules")))


def test_yesterday_reads_the_clock_and_is_answered_when_served(gold):
    at(gold, D11)  # yesterday = D10, served
    agg = log_aggregate(gold.answer(SearchRequest(query="which services had FATAL yesterday", mode="rules")))
    assert agg["args"]["window"] == "yesterday" and "as_of" not in agg["args"]
    assert f"index date {D10.isoformat()}" in agg["rendering"] and "latest available day" not in agg["rendering"]
    assert agg["source_unavailable"] == []


@pytest.mark.parametrize("question,day", [("which services had FATAL yesterday", D11), ("which services had FATAL today", D12)])
def test_a_relative_day_that_is_not_served_is_source_unavailable_with_the_latest_day(gold, question, day):
    at(gold, D12)
    agg = log_aggregate(gold.answer(SearchRequest(query=question, mode="rules")))
    assert agg["source_unavailable"] == [day.isoformat()] and agg["status"] == "bounded" and agg["rows"] == []
    assert agg["rendering"].startswith(f"No composed data for index date {day.isoformat()}")  # never silently shifted, never "none"
    assert f"latest available day: {D10.isoformat()}" in agg["rendering"] and not agg["rendering"].endswith("exact.")


def test_a_caller_named_as_of_still_disables_the_anchor(gold):
    at(gold, D12)
    agg = log_aggregate(gold.answer(SearchRequest(query="which services had FATAL", as_of=D9.isoformat(), mode="rules")))
    assert agg["args"]["as_of"] == D9.isoformat() and f"index date {D9.isoformat()}" in agg["rendering"]
    assert "latest available day" not in agg["rendering"]


@pytest.mark.parametrize("echo", [False, True])
def test_the_planner_path_anchors_on_the_latest_served_day(gold, planner, echo):
    """The cascade reaches the llm planner (scripted): its call carries no clock ``as_of``, also when
    the planner copies ``time_anchor`` into the arguments."""
    at(gold, D12)
    scripted = planner("log_top", {"by": "service", "level": ["fatal"]}, echo=echo)
    result = gold.answer(SearchRequest(query="anything needing my attention right now", mode="cascade"))
    assert len(scripted.payloads) == 1 and scripted.payloads[0]["time_anchor"] == D12.isoformat()
    assert scripted.payloads[0]["time_anchor_source"] == "clock" and "never copy `time_anchor`" in scripted.payloads[0]["log_intent_rule"]
    agg = log_aggregate(result)
    assert "as_of" not in agg["args"] and f"latest available day: {D10.isoformat()}" in agg["rendering"]
    caller = planner("log_top", {"by": "service", "level": ["fatal"]})
    named = log_aggregate(gold.answer(SearchRequest(query="anything needing my attention right now", as_of=D9.isoformat(), mode="llm")))
    assert caller.payloads[0]["time_anchor_source"] == "request" and named["args"]["as_of"] == D9.isoformat()
    assert f"index date {D9.isoformat()}" in named["rendering"]


# --- 2 and 3: a deployment-like source (combined failure count, numeric suffixes) ---------------------

DEPLOYMENTS = ("1658254295", "1658254296")


def split_events(day: date) -> dict[str, list]:
    """t01/svc-api's lines of ``day`` in index order, alternately in the two deployments."""
    rows = sorted(select(EVENTS, days=[day], tenant="t01", service="svc-api"), key=lambda e: (e.hour, e.minute, e.text))
    return {DEPLOYMENTS[0]: rows[0::2], DEPLOYMENTS[1]: rows[1::2]}


@pytest.fixture(scope="module")
def variant(tmp_path_factory):
    """The gold set under a source whose only failure count query counts ERROR and FATAL together
    (plus one WARN query), with t01/svc-api served by two numbered deployments."""
    tmp = tmp_path_factory.mktemp("variant")
    write_gold_library(tmp / "patterns")
    data = gold_config(tmp)
    source = data["sources"]["gold"]
    source["counts"] = {"failures": {"levels": ["ERROR", "FATAL"], "tokens": ["error", "fatal"]}, "warn": {"levels": ["WARN"], "tokens": ["warn"]}}
    source["index_name"] = {"vocabularies": {"service": list(SERVICES)}, "service_suffix": "numeric"}
    cfg = validate_config(data)
    found, mappings = indices(SPEC, EVENTS)
    for name in [n for n in found if n.startswith("t01-prod-svc-api-")]:
        docs, mapping, stamp = found.pop(name), mappings.pop(name), name.rsplit("-", 1)[1]
        for dep, part in zip(DEPLOYMENTS, (docs[0::2], docs[1::2])):
            found[f"t01-prod-svc-api-{dep}-{stamp}"] = part
            mappings[f"t01-prod-svc-api-{dep}-{stamp}"] = mapping
    cluster = FakeLogCluster(indices=found, mappings=mappings)
    engine = Engine(cfg)
    src = build_source("gold", cfg.sources["gold"], env=ENV, http_transport=cluster.transport())
    src._rate_limiter = no_wait()
    pipe = LogsPipeline(cfg, source=src, write_path=engine.write_path, redactor=redact, now=Clock(AS_OF))
    library = pipe.library()
    for day in pulled_days(SPEC):
        pipe.pull(day, library)
    pipe.compose(AS_OF, library)
    return engine


def vtool(engine, name, **args):
    return engine.call_tool(name, {"as_of": AS_OF.isoformat(), **args}).result.output


def test_fatal_alone_with_only_a_combined_count_query_is_never_none_exact(variant):
    """The smoke defect: no single-level FATAL query, so FATAL questions were not cross-checked and
    a day whose FATAL lines were all unparsed answered "No FATAL-level lines ... exact"."""
    day = day_of(SPEC, 8)
    out = vtool(variant, "log_top", by="service", level=["fatal"], start=day.isoformat(), end=day.isoformat())
    assert out.rows == [] and out.status == "bounded" and out.exactness == "bounded"
    assert out.rendering.startswith(f"No FATAL-level lines in the parsed rows for index date {day.isoformat()}")
    legacy = [r for r in out.source_counts if r["service"] == "svc-legacy" and r["tenant"] == "t01"]
    assert legacy and legacy[0]["coverage"] == "superset" and legacy[0]["query"] == "failures" and legacy[0]["level"] == "ERROR+FATAL"
    errors = len(select(EVENTS, days=[day], service="svc-legacy", tenant="t01", level="ERROR"))
    assert legacy[0]["lines_source"] == errors + 1 and legacy[0]["lines_answered"] == errors and legacy[0]["lines_unparsed"] == 1
    assert "count query 'failures' counts ERROR+FATAL together" in out.rendering and "unparsed" in out.rendering
    assert any("no single-level source count query for FATAL" in n for n in out.notes)


@pytest.mark.parametrize(
    "levels,status",
    [
        (["fatal"], "bounded"),  # svc-legacy's unparsed FATAL lines are in the ERROR+FATAL count
        (["error"], "bounded"),  # the same count: the missing lines could be ERROR, so not exact either
        (["error", "fatal"], "bounded"),  # the query's own level set: compared directly
        (["warn"], "answered"),  # its own query; every WARN line is parsed
        (["warn", "fatal"], "bounded"),
    ],
)
def test_every_level_combination_uses_the_unit_source_counts(variant, levels, status):
    out = vtool(variant, "log_top", by="service", level=levels, window="yesterday")
    # fix5: only a count query over exactly the asked levels ranks by the source counts (here
    # ERROR+FATAL, incomplete parse); FATAL or ERROR alone cannot be separated from it
    ranked = levels == ["error", "fatal"]
    expected = Counter(e.service for e in select(EVENTS, days=[D9], level=tuple(lv.upper() for lv in levels)) if ranked or not e.values.get("raw"))
    assert {r["service"]: r["lines"] for r in out.rows} == dict(expected)
    assert out.ranked_by == ("source_counts" if ranked else "parsed_rows")
    assert out.status == status and out.source_counts
    assert (out.exactness == "exact") == (status == "answered")
    if status == "bounded":
        assert "source counts: " in out.rendering and "parse coverage below 100%" in out.rendering and not out.rendering.endswith("exact.")
        assert sum(r["lines_unparsed"] for r in out.source_counts if r["service"] == "svc-legacy") == 3


def test_error_rate_with_a_combined_count_query(variant):
    out = vtool(variant, "log_top", by="service", metric="error_rate", window="yesterday")
    events = select(EVENTS, days=[D9])
    failing = Counter(e.service for e in events if e.level in {"ERROR", "FATAL"})
    total = Counter(e.service for e in events)
    assert {r["service"]: (r["lines"], r["lines_total"]) for r in out.rows} == {s: (n, total[s]) for s, n in failing.items()}
    assert "(count queries failures)" in out.notes[0]


def test_deployment_id_is_on_the_unit_rows_and_groups_instances(variant):
    day = D10
    split = split_events(day)
    scope = {
        "predicates": [{"field": "kind", "op": "eq", "value": "unit_day"}, {"field": "date", "op": "date_interval", "lower": day.isoformat(), "upper": (day + timedelta(days=1)).isoformat(), "granularity": "day"}, {"field": "service", "op": "eq", "value": "svc-api"}],
        "group_by": ["deployment_id"],
        "measure": {"kind": "sum", "field": "lines_total"},
    }
    result = variant.call_tool("aggregate", {"scope": scope}).result.output.result
    by_deployment = {g.key["deployment_id"]: int(g.value) for g in result.groups}
    assert by_deployment[DEPLOYMENTS[0]] == len(split[DEPLOYMENTS[0]]) and by_deployment[DEPLOYMENTS[1]] == len(split[DEPLOYMENTS[1]])
    others = len(select(EVENTS, days=[day], service="svc-api")) - len(split[DEPLOYMENTS[0]]) - len(split[DEPLOYMENTS[1]])
    assert by_deployment["none"] == others
    out = vtool(variant, "log_top", by="deployment", service=["svc-api"], level=["error"], start=day.isoformat(), end=day.isoformat())
    rows = {(r["service"], r["deployment_id"]): r["lines"] for r in out.rows}
    for dep in DEPLOYMENTS:
        assert rows[("svc-api", dep)] == sum(1 for e in split[dep] if e.level == "ERROR")
    assert f"svc-api (deployment {DEPLOYMENTS[0]}): " in out.rendering and out.rendering.startswith("Top 3 deployment by ERROR-level lines")
    one = vtool(variant, "log_top", by="tenant", deployment_id=[DEPLOYMENTS[1]], level=["error"], start=day.isoformat(), end=day.isoformat())
    assert [(r["tenant"], r["lines"]) for r in one.rows] == [("t01", sum(1 for e in split[DEPLOYMENTS[1]] if e.level == "ERROR"))]
    rate = vtool(variant, "log_top", by="deployment", metric="error_rate", service=["svc-api"], start=day.isoformat(), end=day.isoformat())
    got = {r["deployment_id"]: (r["lines"], r["lines_total"]) for r in rate.rows}
    for dep in DEPLOYMENTS:
        assert got[dep] == (sum(1 for e in split[dep] if e.level in {"ERROR", "FATAL"}), len(split[dep]))
    tenants = vtool(variant, "log_tenants", by="deployment", service="svc-api", level="warn", start=day.isoformat(), end=day.isoformat())
    assert all("deployment_id" in r for r in tenants.rows) and tenants.rendering.split(" ")[1] == "deployments"
