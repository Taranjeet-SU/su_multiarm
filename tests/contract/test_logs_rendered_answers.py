"""Live-deployment follow-ups (2026-11-04, fix5), on the synthetic gold set composed through the real
pipeline (fake source, sqlite, hash embedder; models scripted, never called over the network).

1. A ranking of services, tenants or deployments at asked levels ranks by the **source counts** when
   the parse is incomplete: a planted service whose ERROR lines are almost all unparsed
   (``GoldSpec.error_flood``) ranks first, with its parsed lines and unparsed share alongside. A
   signature ranking keeps the parsed rows and states the unparsed share and the services with the
   most unparsed lines.
2. A pure ranking/count answer's text is the aggregate's deterministic rendering (with its
   exactness label), never "No matching evidence was retrieved."; a configured generator may add
   one sentence that restates only the rendering's numbers.

Expected numbers come from the generator's plan, never from the engine. Evidence: contract
(synthetic, in-process)."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

import pytest

from su_multi_arm import SearchRequest
from su_multi_arm.core.errors import BudgetExhausted
from su_multi_arm.providers import JsonModel
from su_multi_arm.testing.fixtures.logs.gold import GoldSpec, plan, select

from tests.logs_gold_support import AS_OF, SPEC, build_gold, gold_config

FLOOD = ("t03", "svc-legacy", 10, 300)  # 300 ERROR lines no pattern reads, in a token-only service
FLOOD_SPEC = GoldSpec(error_flood=FLOOD)
EVENTS = plan(FLOOD_SPEC)
DAY = AS_OF.isoformat()
SECRET_ENV = "SU_TEST_RENDERED_KEY"
FAKE = {"provider": "openai_compatible", "model": "fake-local", "base_url": "http://127.0.0.1:9/v1", "api_key_env": SECRET_ENV}


def config(tmp, generator: bool) -> dict:
    data = gold_config(tmp)
    if generator:
        data["models"]["roles"]["generator"] = FAKE
    data["pipeline"]["decision"] = {"kind": "cascade", "stages": ["rules", "llm"]}
    data["models"]["roles"]["planner"] = FAKE
    data["request"] = {"modes": ["rules", "llm", "cascade"]}
    return data


@pytest.fixture(scope="module")
def flood(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("flood")
    mp = pytest.MonkeyPatch()
    mp.setenv(SECRET_ENV, "synthetic-not-a-key")
    engine, _, _ = build_gold(tmp, data=config(tmp, generator=True), spec=FLOOD_SPEC)
    yield engine
    mp.undo()


@pytest.fixture(scope="module")
def plain(tmp_path_factory):
    """The default gold plan (no flood) without a generator."""
    tmp = tmp_path_factory.mktemp("plain")
    mp = pytest.MonkeyPatch()
    mp.setenv(SECRET_ENV, "synthetic-not-a-key")
    engine, _, _ = build_gold(tmp, data=config(tmp, generator=False))
    yield engine
    mp.undo()


def top(engine, **args):
    return engine.call_tool("log_top", {"as_of": DAY, **args}).result.output.model_dump(mode="json")


def source_lines(key: str, levels=("ERROR", "FATAL")) -> dict[str, int]:
    """The source's own lines at ``levels`` per ``key`` on D10, from the plan (the token-only
    service's lines carry the level word, so its token count equals this too)."""
    return dict(Counter(getattr(e, key) for e in select(EVENTS, days=[AS_OF], level=tuple(levels))))


# --- 1. rankings by source counts -------------------------------------------------------------------


def test_a_service_with_mostly_unparsed_errors_ranks_first_by_source_counts(flood):
    out = top(flood, by="service", level=["error", "fatal"])
    expected = source_lines("service")
    assert out["ranked_by"] == "source_counts" and out["status"] == "bounded"
    rows = out["rows"]
    assert rows[0]["service"] == "svc-legacy" and rows[0]["lines"] == expected["svc-legacy"] == 3 + FLOOD[3] + 1
    assert [(r["service"], r["lines"]) for r in rows] == sorted(expected.items(), key=lambda kv: (-kv[1], kv[0]))
    legacy = rows[0]
    assert legacy["lines_parsed"] < 10 and legacy["lines_unparsed"] == legacy["lines"] - legacy["lines_parsed"]
    assert legacy["unparsed_share"] > 0.95 and legacy["source_exactness"] == "approximate"  # token_match
    parsed = Counter(e.service for e in select(EVENTS, days=[AS_OF], level=("ERROR", "FATAL"), status="parsed"))
    for row in rows[1:]:  # complete units: parsed lines equal the plan's, nothing unparsed
        assert row["lines_parsed"] == parsed[row["service"]] and row["lines_unparsed"] == 0 and row["ranked_by"] == "source_counts"
    text = out["rendering"]
    assert text.startswith("Top 3 service by source ERROR+FATAL lines, ranked by source counts (the parse is incomplete)")
    assert f"svc-legacy: {legacy['lines']} source (token_match, approximate), {legacy['lines_parsed']} parsed" in text
    assert "% unparsed" in text and "ranked by source counts (errors, fatal)" in text and text.endswith("bounded.")


def test_a_call_without_by_is_a_service_ranking_and_is_cross_checked(flood):
    """``by`` defaults to service (a planner may omit it): the source counts and the source ranking
    apply exactly as with ``by: service`` (they used to be skipped)."""
    implicit, explicit = top(flood, level=["error", "fatal"]), top(flood, by="service", level=["error", "fatal"])
    assert implicit["ranked_by"] == "source_counts" and implicit["rows"] == explicit["rows"] and implicit["source_counts"] == explicit["source_counts"]


def test_tenants_rank_by_source_counts_too(flood):
    out = top(flood, by="tenant", level=["error"])
    expected = source_lines("tenant", ("ERROR",))
    assert out["ranked_by"] == "source_counts"
    assert out["rows"][0]["tenant"] == FLOOD[0] and out["rows"][0]["lines"] == expected[FLOOD[0]]
    assert {r["tenant"]: r["lines"] for r in out["rows"]} == expected


def test_top_n_cuts_the_source_ranking_not_the_parsed_one(flood):
    out = top(flood, by="service", level=["error", "fatal"], top_n=1)
    assert [r["service"] for r in out["rows"]] == ["svc-legacy"]  # absent from the parsed top 1


def test_a_signature_ranking_keeps_parsed_rows_and_names_the_unparsed_services(flood):
    out = top(flood, by="signature", level=["error", "fatal"])
    assert out["ranked_by"] is None and all("lines_parsed" not in r for r in out["rows"])
    text = out["rendering"]
    assert "unparsed share:" in text and "so no signature ranks them" in text
    assert "services with the most unparsed ERROR lines: svc-legacy" in text


def test_a_complete_parse_keeps_the_parsed_ranking(plain):
    out = top(plain, by="service", level=["error"])
    assert out["ranked_by"] == "parsed_rows" and "ranked by source counts" not in out["rendering"]
    expected = Counter(e.service for e in select(plan(SPEC), days=[AS_OF], level="ERROR"))
    assert {r["service"]: r["lines"] for r in out["rows"]} == dict(expected)


def test_a_filter_the_source_cannot_count_keeps_the_parsed_ranking(flood):
    out = top(flood, by="service", level=["error", "fatal"], owner="reviewed")
    assert out["ranked_by"] == "parsed_rows"
    assert any(n.startswith("not ranked by source counts: the ranking filters on a field") for n in out["notes"])


# --- 2. rendered answers ----------------------------------------------------------------------------


def aggregate_of(result):
    found = [a for a in result.retrieval.aggregates if a.get("intent")]
    assert len(found) == 1
    return found[0]


@pytest.mark.parametrize("mode", ["rules", "cascade"])
def test_a_pure_ranking_answer_is_the_rendering_without_a_generator(plain, mode):
    result = plain.answer(SearchRequest(query=f"which services had ERROR on {DAY}", mode=mode))
    agg = aggregate_of(result)
    answer = result.answer
    assert answer.answer == agg["rendering"] and "No matching evidence" not in answer.answer
    assert answer.answer.endswith(("exact.", "approximate (hourly_quota, seed 0).", "bounded."))
    assert answer.citations == [agg["id"]] and not answer.insufficient_evidence and answer.short_answer is None
    assert result.trace["rendered_answer"] == {"aggregates": [agg["id"]], "narrative": "none: no generator configured"}
    assert result.contract == "passed"


class Narrator:
    """Scripted generator: returns ``text`` as the answer (counts the calls)."""

    def __init__(self, text: str | None = None, raises: Exception | None = None):
        self.text, self.raises, self.calls = text, raises, []

    def __call__(self, model, system, payload, schema, budget, stage):
        if self.raises is not None:
            raise self.raises
        budget.reserve()
        self.calls.append((system, payload))
        ids = [row["id"] for row in payload["evidence"]]
        return schema.model_validate({"answer": self.text, "citations": ids, "claims": [{"text": self.text, "kind": "narrative", "evidence_ids": ids}]})


@pytest.fixture
def narrator(monkeypatch):
    holder: dict = {}

    def install(text: str | None = None, raises: Exception | None = None) -> Narrator:
        holder["n"] = Narrator(text, raises)
        return holder["n"]

    monkeypatch.setattr(JsonModel, "call", lambda self, *a: holder["n"](self, *a))
    monkeypatch.setenv(SECRET_ENV, "synthetic-not-a-key")
    return install


QUESTION = f"which services had ERROR or FATAL on {DAY}"


@pytest.mark.parametrize("mode", ["rules", "cascade"])
def test_a_narrative_sentence_that_restates_rendered_numbers_is_kept(flood, narrator, mode):
    n = source_lines("service")["svc-legacy"]
    scripted = narrator(f"svc-legacy fails the most, with {n} source lines, ranked by source counts.")
    result = flood.answer(SearchRequest(query=QUESTION, mode=mode))
    agg = aggregate_of(result)
    assert len(scripted.calls) == 1
    system, payload = scripted.calls[0]
    assert "one short sentence" in system and payload["evidence"] == [{"id": agg["id"], "source": "structured", "passage": agg["rendering"]}]
    text = result.answer.answer
    assert text.startswith(agg["rendering"]) and text.endswith(scripted.text) and result.answer.short_answer == scripted.text
    assert result.contract == "passed" and result.trace["rendered_answer"]["narrative"] == "kept"
    assert agg["rendering"].startswith("Top 3 service by source ERROR+FATAL lines, ranked by source counts")


def test_a_narrative_with_a_number_not_in_the_rendering_is_dropped(flood, narrator):
    narrator("svc-legacy fails the most, with 9999 errors.")
    result = flood.answer(SearchRequest(query=QUESTION, mode="rules"))
    agg = aggregate_of(result)
    assert result.answer.answer == agg["rendering"] and result.answer.short_answer is None
    assert result.contract == "stripped" and [v.rule for v in result.violations] == ["answer_number_mismatch"]
    assert result.violations[0].action == "strip" and result.trace["rendered_answer"]["narrative"] == "dropped"


def test_a_narrative_of_two_sentences_is_dropped_not_cut(flood, narrator):
    narrator("svc-legacy fails the most. Restart it.")
    result = flood.answer(SearchRequest(query=QUESTION, mode="rules"))
    assert result.answer.answer == aggregate_of(result)["rendering"] and result.contract == "passed"
    assert result.trace["rendered_answer"]["narrative"] == "dropped"


def test_no_budget_for_the_narrative_keeps_the_rendering(flood, narrator):
    narrator(raises=BudgetExhausted("calls", 1, 0))
    result = flood.answer(SearchRequest(query=QUESTION, mode="rules"))
    assert result.answer.answer == aggregate_of(result)["rendering"]
    assert result.trace["rendered_answer"]["narrative"] == "skipped: budget"


def test_an_aggregate_too_large_for_the_evidence_budget_is_still_the_answer(tmp_path, narrator, monkeypatch):
    """The live defect: the log aggregate did not fit the generator's evidence budget, the
    generator saw no evidence and the answer read "No matching evidence was retrieved."."""
    monkeypatch.setenv(SECRET_ENV, "synthetic-not-a-key")
    data = config(tmp_path, generator=True)
    data["pipeline"]["selection"] = {"packing": {"budget_chars": 500}}
    engine, _, _ = build_gold(tmp_path, data=data, spec=FLOOD_SPEC)
    narrator("svc-legacy fails the most.")
    result = engine.answer(SearchRequest(query=QUESTION, mode="rules"))
    agg = aggregate_of(result)
    assert agg["id"] in result.trace["evidence"]["omitted_ids"]
    assert "No matching evidence" not in result.answer.answer and result.answer.answer.startswith(agg["rendering"])


# --- persona-eval follow-ups: kept tenant filters and the comparison direction ----------------------


def test_a_tenant_trend_question_keeps_the_tenant_on_the_ask_path(plain):
    result = plain.answer(SearchRequest(query="How many errors did t02 have per day over the last 3 days?", as_of=DAY, mode="rules"))
    agg = aggregate_of(result)
    assert agg["intent"] == "log_trend" and agg["args"]["tenant"] == "t02"
    days = [AS_OF - timedelta(days=n) for n in (2, 1, 0)]
    # the composed rows: svc-legacy's raw (unparsed) FATAL lines have no parsed level
    expected = [(d.isoformat(), sum(1 for e in select(plan(SPEC), days=[d], tenant="t02", level=("ERROR", "FATAL")) if not e.values.get("raw"))) for d in days]
    assert [(r["date"], r["lines"]) for r in agg["rows"]] == expected
    assert result.answer.answer == agg["rendering"]


def test_an_increase_question_compares_earlier_to_later_with_the_tenant(plain):
    d8, d9 = (AS_OF - timedelta(days=n) for n in (4, 3))  # D6 and D7, named later-first
    result = plain.answer(SearchRequest(query=f"Did errors for t01 increase on {d9} compared to {d8}?", as_of=DAY, mode="rules"))
    agg = aggregate_of(result)
    assert agg["intent"] == "log_compare" and agg["args"]["tenant"] == "t01"
    counts = [sum(1 for e in select(plan(SPEC), days=[d], tenant="t01", level=("ERROR", "FATAL")) if not e.values.get("raw")) for d in (d8, d9)]
    assert [r["lines"] for r in agg["rows"]] == counts and counts[1] > counts[0]  # svc-api's ramp
    assert f"(change from {d8} to {d9}: +{counts[1] - counts[0]})" in agg["rendering"]
