"""Query-compiler operations (2.0.0a3 parity slice): calendar windows against a declared anchor,
the rules grammar (ranking, nth, ties, calendar groups, cross-tab, period comparison, shares,
distinct counts, numeric measures, lists with totals, states, negation, comparisons, literal text
topics), the LLM planner schema, the structured arm's whole-corpus text match, the answer contract's
number check on the new results, the packing reservation cap (review N1) and count_distinct with
missing/null values (review B1). Synthetic data only."""

from __future__ import annotations

from datetime import date

import pytest

from su_multi_arm import Document, Engine, SearchRequest
from su_multi_arm.backends._scope import measure_of
from su_multi_arm.backends.memory import MemoryBackend
from su_multi_arm.core.component import Dependencies
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.config_selection import PackingConfig
from su_multi_arm.core.types import (
    FieldSpec,
    GroupRow,
    Measure,
    MetadataSchema,
    NamedState,
    Predicate,
    Provenance,
    ScopeResult,
    ScopeSpec,
    StructuredRecord,
    TextMatch,
)
from su_multi_arm.decision import DecisionRequest, LlmAgent, LlmConfig, LlmPlan, RulesAgent, RulesConfig
from su_multi_arm.decision.base import Capabilities
from su_multi_arm.decision.intent import detect_intents
from su_multi_arm.decision.values import ValueResolver
from su_multi_arm.decision.windows import WindowError, anchor_date, bounds, find_windows, parse_window
from su_multi_arm.generation import Answer, AnswerContract, Claim
from su_multi_arm.generation.contract import AggregateEvidence
from su_multi_arm.providers.base import strict_schema
from su_multi_arm.runtime.context import new_context
from su_multi_arm.selection.packing import PackExtra, PackRecord, pack

from conftest import V2_GEN, v2_records, v2_schema_model  # tests/conftest.py

ANCHOR = date(2024, 4, 15)  # a Monday
PROV = Provenance(extractor="test")


# --- windows -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "start", "end"),
    [
        ("this month", "2024-04-01", "2024-04-16"),
        ("last month", "2024-03-01", "2024-04-01"),
        ("last 7 days", "2024-04-09", "2024-04-16"),
        ("past month", "2024-03-16", "2024-04-16"),
        ("this week", "2024-04-15", "2024-04-16"),
        ("last week", "2024-04-08", "2024-04-15"),
        ("last quarter", "2024-01-01", "2024-04-01"),
        ("this year", "2024-01-01", "2024-04-16"),
        ("yesterday", "2024-04-14", "2024-04-15"),
        ("today", "2024-04-15", "2024-04-16"),
        ("ytd", "2024-01-01", "2024-04-16"),
        ("since 2023", "2023-01-01", "2024-04-16"),
        ("October", "2023-10-01", "2023-11-01"),  # most recent October that has begun
        ("in March", "2024-03-01", "2024-04-01"),
        ("March 2023", "2023-03-01", "2023-04-01"),
        ("March 5, 2023", "2023-03-05", "2023-03-06"),
        ("2022", "2022-01-01", "2023-01-01"),
        ("2024-02", "2024-02-01", "2024-03-01"),
        ("2024-02-29", "2024-02-29", "2024-03-01"),
    ],
)
def test_calendar_expressions_resolve_against_the_anchor(expression, start, end):
    window = parse_window(expression, ANCHOR)
    assert (window.start.isoformat(), window.end.isoformat()) == (start, end), expression


def test_relative_expressions_need_an_anchor_and_ambiguous_words_are_not_windows():
    with pytest.raises(WindowError):
        parse_window("this month", None)
    assert parse_window("March 2023", None).start == date(2023, 3, 1)  # absolute: no anchor needed
    assert find_windows("records that may fail", ANCHOR) == []
    assert [w.text for w in find_windows("records from in May", ANCHOR)] == ["in May"]
    with pytest.raises(WindowError):
        parse_window("sometime soon", ANCHOR)


def test_bounds_are_in_the_declared_zone():
    from zoneinfo import ZoneInfo

    window = parse_window("2024-03", None)
    assert bounds(window, "datetime", ZoneInfo("America/New_York")) == ("2024-03-01T00:00:00-05:00", "2024-04-01T00:00:00-04:00")
    assert bounds(window, "date", ZoneInfo("UTC")) == ("2024-03-01", "2024-04-01")
    assert anchor_date(__import__("datetime").datetime(2024, 4, 15, 2, 0, tzinfo=ZoneInfo("UTC")), ZoneInfo("America/New_York")) == date(2024, 4, 14)


def test_resolver_window_refuses_an_undeclared_zone_on_require_offset_fields():
    schema = v2_schema_model()
    refused = ValueResolver(schema, anchor=ANCHOR).window("created_at", "last month")
    assert refused.unsupported.reason == "timezone_missing"
    assert ValueResolver(schema, anchor=ANCHOR).window("resolved_on", "last month").predicate.lower == "2024-03-01"  # date field
    declared = schema.model_copy(update={"timezone": "UTC"})
    ok = ValueResolver(declared, anchor=ANCHOR).window("created_at", "last month")
    assert (ok.predicate.op, ok.predicate.lower, ok.predicate.upper) == ("date_interval", "2024-03-01T00:00:00+00:00", "2024-04-01T00:00:00+00:00")
    assert ValueResolver(declared).window("created_at", "last month").unsupported.reason == "unparseable_bound"  # no anchor
    assert ValueResolver(declared, anchor=ANCHOR).window("region", "last month").unsupported.reason == "field_not_filterable"


def test_unknown_timezone_is_rejected():
    with pytest.raises(ValueError, match="IANA"):
        MetadataSchema(timezone="Mars/Olympus")


def test_declared_states_resolve_for_agents_but_request_filters_stay_literal():
    schema = MetadataSchema(fields=[FieldSpec(name="state", type="string", listable=True, states=[NamedState(name="open", values=["done"], negate=True)])])
    resolver = ValueResolver(schema)
    agent = resolver.resolve(Predicate(field="state", op="eq", value="Open"))
    assert agent.predicate == Predicate(field="state", op="in", value=["done"], negate=True)
    flipped = resolver.resolve(Predicate(field="state", op="eq", value="open", negate=True))
    assert flipped.predicate.negate is False
    caller = resolver.resolve(Predicate(field="state", op="eq", value="open"), from_request=True)
    assert caller.predicate.op == "eq"
    with pytest.raises(ValueError):
        FieldSpec(name="n", type="integer", states=[NamedState(name="x", values=["1"])])


# --- shared measure semantics (review B1) -------------------------------------------------------


def test_count_distinct_excludes_missing_and_null_values():
    values = [{"org": "A"}, {"org": "B"}, {"org": "A"}, {}, {}, {}, {"org": None}, {"org": None}]
    rows = [StructuredRecord(schema_id="document", id=f"r{i}", values=v, provenance=PROV) for i, v in enumerate(values)]
    schema = MetadataSchema(fields=[FieldSpec(name="org", type="string")])
    assert measure_of(ScopeSpec(measure=Measure(kind="count_distinct", field="org")), rows, 8, schema) == (2, 5)
    canonical = MetadataSchema(fields=[FieldSpec(name="org", type="string")], equivalence_field="org")
    # the equivalence field keeps its semantics: a record without one is its own canonical unit
    assert measure_of(ScopeSpec(measure=Measure(kind="count_distinct", unit="org")), rows, 8, canonical) == (7, 0)
    assert measure_of(ScopeSpec(measure=Measure(kind="count_distinct", unit="document")), rows, 8, schema) == (8, 0)


# --- rules grammar ---------------------------------------------------------------------------------


@pytest.fixture
def grammar_setup():
    schema = v2_schema_model()
    fields = []
    for spec in schema.fields:
        update = {}
        if spec.name in {"status", "resolved_on", "project"}:
            update["groupable"] = True
        if spec.name == "status":
            update["states"] = [NamedState(name="unresolved", values=["closed"], negate=True)]
        if spec.name == "resolved_on":
            update["sortable"] = True
        if spec.name == "created_at":
            update["sortable"] = False
        fields.append(spec.model_copy(update=update))
    schema = schema.model_copy(update={"fields": fields, "timezone": "UTC"})
    backend = MemoryBackend()
    backend.records.upsert(V2_GEN, v2_records(), schema)
    caps = Capabilities(arms=["dense", "lexical", "structured"], metadata_schema=schema)
    agent = RulesAgent(RulesConfig(), Dependencies(backends={"records": backend.records}))
    ctx = new_context(V2_GEN)

    def decide(question: str, **extra):
        intents = detect_intents(question, [], caps.structured_targets())
        decision = agent.decide(ctx, DecisionRequest(query=question, as_of=ANCHOR, **extra), intents, caps)
        result = backend.records.execute(V2_GEN, decision.scope, []) if decision.scope is not None else None
        return decision, result

    return decide


def keys(result):
    return [(tuple(g.key.values()), g.value) for g in result.groups]


def test_grammar_group_rank_nth_and_ties(grammar_setup):
    decision, result = grammar_setup("How many records per project?")
    assert decision.scope.group_by == ["project"] and result.value == {"Alpha": 2, "Beta": 2, "Gamma": 1}
    decision, result = grammar_setup("Which project has the most records?")
    assert (decision.scope.nth, decision.scope.order) == (1, "desc") and keys(result) == [(("Alpha",), 2), (("Beta",), 2)]
    _, result = grammar_setup("What is the second most common project?")
    assert keys(result) == [(("Gamma",), 1)] and result.groups[0].rank == 2
    decision, result = grammar_setup("Top 1 projects")
    assert decision.scope.top_n == 1 and keys(result) == [(("Alpha",), 2)] and [g.key["project"] for g in result.ties] == ["Beta"]
    _, result = grammar_setup("Which project has the fewest records?")
    assert keys(result) == [(("Gamma",), 1)]


def test_grammar_measures_cross_tab_and_calendar_groups(grammar_setup):
    decision, result = grammar_setup("What is the average amount by project?")
    assert decision.scope.measure == Measure(kind="avg", field="amount") and result.value == {"Alpha": 15.25, "Beta": 5.0, "Gamma": 7.25}
    _, result = grammar_setup("Which project has the highest average amount?")
    assert keys(result) == [(("Alpha",), 15.25)]
    decision, result = grammar_setup("How many records by project and status?")
    assert decision.scope.group_by == ["project", "status"] and len(result.groups) == 4
    decision, result = grammar_setup("How many records were resolved by month?")
    assert (decision.scope.group_by, decision.scope.granularity) == (["resolved_on"], "month")
    assert result.value == {"2024-03": 2, "2024-01": 1, "2024-04": 1, "None": 1}
    _, result = grammar_setup("Which month had the most resolved records?")
    assert keys(result) == [(("2024-03",), 2)]
    _, result = grammar_setup("What is the maximum amount?")
    assert result.value == 20.0 and result.missing_or_invalid == 1


def test_grammar_windows_comparison_and_between(grammar_setup):
    decision, result = grammar_setup("How many records were resolved in March 2024?")
    assert decision.scope.time_basis == "resolved_on" and result.value == 2
    _, result = grammar_setup("How many open records were resolved last month?")
    assert result.value == 1
    decision, result = grammar_setup("Compare records resolved in March 2024 vs April 2024")
    assert [p.label for p in decision.scope.periods] == ["2024-03", "2024-04"] and result.value == {"2024-03": 2, "2024-04": 1}
    _, result = grammar_setup("How many records were resolved between January 2024 and March 2024?")
    assert result.value == 3
    decision, result = grammar_setup("How many records were created last month?")
    assert result.value == 3 and any("window on created_at" in b for b in result.basis)


def test_grammar_share_distinct_states_negation_and_lists(grammar_setup):
    decision, result = grammar_setup("What percentage of records are open?")
    assert decision.scope.measure.kind == "share" and (result.value, result.numerator, result.total) == (0.6, 3, 5)
    decision, result = grammar_setup("What share of records per project?")
    assert decision.scope.with_share and [g.share for g in result.groups] == [0.4, 0.4, 0.2]
    _, result = grammar_setup("How many distinct projects are there?")
    assert result.value == 3
    decision, result = grammar_setup("How many unresolved records?")
    assert decision.scope.predicates[0] == Predicate(field="status", op="in", value=["closed"], negate=True) and result.value == 4
    _, result = grammar_setup("How many records are not open?")
    assert result.value == 2
    _, result = grammar_setup("How many records are open or pending?")
    assert result.value == 4
    _, result = grammar_setup("How many records with amount above 6?")
    assert result.value == 3
    _, result = grammar_setup("How many archived records?")
    assert result.value == 1
    decision, result = grammar_setup("List the 2 most recent records")
    assert decision.scope.sort.field == "resolved_on" and [r.id for r in result.listed] == ["r2", "r5"] and result.total == 5
    _, result = grammar_setup("Top 1 records by amount")
    assert [r.id for r in result.listed] == ["r2"]
    decision, result = grammar_setup("List all open records")
    assert decision.scope.limit == 50 and result.total == 3


def test_grammar_text_topics(grammar_setup):
    decision, _ = grammar_setup("How many records mention invoice rounding?")
    assert decision.scope.text == TextMatch(kind="all_terms", text="invoice rounding")
    decision, _ = grammar_setup('How many records contain "billing dispute"?')
    assert decision.scope.text == TextMatch(kind="phrase", text="billing dispute") and decision.unsupported == []
    decision, _ = grammar_setup("What percentage of records mention billing?")
    assert decision.scope.text.role == "numerator" and decision.scope.measure.kind == "share"
    decision, _ = grammar_setup('How many records have status "open"?')
    assert decision.scope.text is None and decision.scope.predicates[0].value == "open"  # a quoted value stays a value


@pytest.mark.parametrize(
    ("question", "reason"),
    [
        ("What is the median amount?", "measure_unsupported"),
        ("Which project has the highest amount?", "measure_unsupported"),
        ("How many records were resolved in March 2024 and April 2024?", "unparseable_bound"),
        ("How many records in the north warehouse?", "field_not_filterable"),
        ("What percentage of records?", "measure_unsupported"),
    ],
)
def test_grammar_refuses_what_it_cannot_express(grammar_setup, question, reason):
    decision, _ = grammar_setup(question)
    assert decision.outcome == "abstain" and decision.scope is None
    assert reason in {u.reason for u in decision.unsupported}, decision.unsupported


@pytest.mark.parametrize(
    ("question", "dropped"),
    [
        # 2.0.0a3 parity fix: each of these returned an ``exact`` number that ignored a qualifier.
        ("Who is second for project Alpha?", "second"),  # an ordinal outside a ranking
        ("How many records are currently active?", "active"),  # a state word no field declares
        ("How many invoice-mention records are active?", "active"),
        ("Which projects contain the most active records?", "active"),  # not a text topic of grammar words
        ("Which project has the most records among projects with >= 2 records?", ">= 2"),  # a threshold bound to no field
        ("How many records have at least 3 tags?", "at least 3"),
    ],
)
def test_grammar_refuses_a_dropped_qualifier_instead_of_an_exact_count(grammar_setup, question, dropped):
    decision, _ = grammar_setup(question)
    assert decision.outcome == "abstain" and decision.scope is None, decision
    flagged = {u.constraint_text for u in decision.unsupported if u.reason == "qualifier_not_applied"}
    assert dropped in flagged, decision.unsupported


def test_grammar_hyphenated_mention_is_a_topic_and_the_cue_never_starts_inside_a_word(grammar_setup):
    decision, _ = grammar_setup("How many invoice-mention records are open?")
    assert decision.outcome == "decided" and decision.scope.text == TextMatch(kind="all_terms", text="invoice")
    assert decision.scope.predicates[0].field == "status"
    decision, _ = grammar_setup("Which project has the highest invoice-mention rate?")
    assert decision.outcome == "abstain" and "rate" in " ".join(u.constraint_text for u in decision.unsupported)  # never "mention rate"
    # what still works: the ordinal inside a ranking, a bound state, a bound comparison
    _, result = grammar_setup("What is the second most common project?")
    assert keys(result) == [(("Gamma",), 1)]
    decision, _ = grammar_setup("Which project is the most active?")
    assert decision.outcome == "abstain"  # "most active" without a group noun after it is not a ranking
    _, result = grammar_setup("How many records with amount above 6?")
    assert result.value == 3


def test_planner_sort_by_count_ranks_groups(monkeypatch):
    schema = v2_schema_model()
    schema = schema.model_copy(update={"fields": [s.model_copy(update={"groupable": True}) if s.name == "project" else s for s in schema.fields]})
    backend = MemoryBackend()
    backend.records.upsert(V2_GEN, v2_records(), schema)
    caps = Capabilities(arms=["dense", "structured"], metadata_schema=schema)
    agent = LlmAgent(LlmConfig(role={"provider": "openai_compatible", "model": "fake"}), Dependencies(backends={"records": backend.records}))
    plan: dict = {}
    monkeypatch.setattr(LlmAgent, "call_model", lambda self, ctx, payload: LlmPlan.model_validate(plan))

    def decide(scope: dict, question: str = "Which projects have the most records?"):
        plan["steps"] = [{"arm": "structured", "scope": scope}]
        return agent.decide(new_context(V2_GEN), DecisionRequest(query=question, as_of=ANCHOR), detect_intents(question, [], caps.structured_targets()), caps)

    decision = decide({"group_by": ["project"], "sort": {"field": "count", "descending": True}, "top_n": 1})
    assert decision.outcome == "decided" and (decision.scope.sort, decision.scope.order, decision.scope.top_n) == (None, "desc", 1)
    result = backend.records.execute(V2_GEN, decision.scope, [])
    assert [(g.key["project"], g.count) for g in result.groups] == [("Alpha", 2)] and [g.key["project"] for g in result.ties] == ["Beta"]
    ascending = decide({"group_by": ["project"], "sort": {"field": "count"}, "nth": 1, "measure": {"kind": "count"}})
    assert ascending.scope.order == "asc" and [g.key["project"] for g in backend.records.execute(V2_GEN, ascending.scope, []).groups] == ["Gamma"]
    by_measure = decide({"group_by": ["project"], "measure": {"kind": "avg", "field": "amount"}, "sort": {"field": "amount", "descending": True}, "top_n": 1}, "Which project has the highest average amount?")
    assert by_measure.scope.order == "desc" and by_measure.scope.sort is None  # the measure's field ranks the groups
    assert [(g.key["project"], g.value) for g in backend.records.execute(V2_GEN, by_measure.scope, []).groups] == [("Alpha", 15.25)]
    # not well-defined: records have no count; a direction contradicting order
    no_groups = decide({"sort": {"field": "count", "descending": True}, "limit": 2}, "List the records with the most records")
    assert no_groups.outcome == "abstain" and no_groups.unsupported[0].reason == "measure_unsupported"
    contradiction = decide({"group_by": ["project"], "sort": {"field": "count", "descending": True}, "order": "asc", "nth": 1})
    assert contradiction.outcome == "abstain" and "contradicts" in contradiction.unsupported[0].suggestion
    # an undeclared sort field is still refused as before
    unknown = decide({"group_by": ["project"], "sort": {"field": "region", "descending": True}, "top_n": 1})
    assert unknown.outcome == "abstain" and unknown.unsupported[0].reason == "field_not_filterable"


def test_new_intent_operators_need_a_structured_target():
    targets = ["record", "records", "project", "status", "month"]
    assert "rank" in detect_intents("Which project has the most records?", [], targets).kinds()
    assert "breakdown" in detect_intents("records by project", [], targets).kinds()
    assert detect_intents("Which guide explains the top features of sharing by default?", [], targets).kinds() == []
    assert detect_intents("Show the second step of the export by hand", [], None).kinds() == []


# --- LLM planner -------------------------------------------------------------------------------------


def test_planner_schema_is_strict_and_carries_the_new_operations(monkeypatch):
    strict_schema(LlmPlan.model_json_schema(), "LlmPlan")
    schema = v2_schema_model().model_copy(update={"timezone": "UTC"})
    fields = [s.model_copy(update={"groupable": True}) if s.name == "project" else s for s in schema.fields]
    schema = schema.model_copy(update={"fields": fields})
    backend = MemoryBackend()
    backend.records.upsert(V2_GEN, v2_records(), schema)
    caps = Capabilities(arms=["dense", "structured"], metadata_schema=schema)
    agent = LlmAgent(LlmConfig(role={"provider": "openai_compatible", "model": "fake"}), Dependencies(backends={"records": backend.records}))
    plan = {
        "steps": [
            {"arm": "structured", "scope": {"group_by": ["project"], "order": "desc", "nth": 2, "windows": [{"field": "created_at", "expression": "this year"}]}},
        ]
    }
    seen = {}

    def call_model(self, ctx, payload):
        seen.update(payload)
        return LlmPlan.model_validate(plan)

    monkeypatch.setattr(LlmAgent, "call_model", call_model)
    question = "Which project had the second most records this year?"
    decision = agent.decide(new_context(V2_GEN), DecisionRequest(query=question, as_of=ANCHOR), detect_intents(question, [], caps.structured_targets()), caps)
    assert seen["time_anchor"] == "2024-04-15" and "window" in next(f for f in seen["filterable_fields"] if f["name"] == "created_at")["operations"]
    assert decision.outcome == "decided", decision.unsupported
    window = decision.scope.predicates[0]
    assert (window.op, window.lower, window.upper) == ("date_interval", "2024-01-01T00:00:00+00:00", "2024-04-16T00:00:00+00:00")
    result = backend.records.execute(V2_GEN, decision.scope, [])
    assert [(g.key["project"], g.count) for g in result.groups] == [("Gamma", 1)]
    plan["steps"][0]["scope"] = {"periods": [{"expression": "nonsense period"}, {"expression": "2024-03"}], "time_basis": "created_at"}
    refused = agent.decide(new_context(V2_GEN), DecisionRequest(query="Compare March and nonsense", as_of=ANCHOR), detect_intents("compare", [], None), caps)
    assert refused.outcome == "abstain" and refused.unsupported[0].reason == "unparseable_bound"


# --- engine: structured arm with a whole-corpus text match ---------------------------------------


def engine_with_text():
    schema = v2_schema_model().model_dump(mode="json")
    schema["timezone"] = "UTC"
    data = {
        "schema_version": 2,
        "dataset": "demo",
        "backends": {"local": {"kind": "memory"}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "metadata_schema": schema,
        "pipeline": {"arms": {"dense": {"backend": "local"}, "lexical": {"backend": "local"}, "structured": {"backend": "local"}}},
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 64}}},
    }
    engine = Engine(validate_config(data))
    texts = {"r1": "billing invoice rounding", "r2": "billing dispute closed", "r3": "table layout", "r4": "region report"}
    docs = []
    for record in v2_records()[:4]:
        values = {k: v for k, v in record.values.items() if k in {"kind", "project", "status", "amount", "created_at"}}
        docs.append(Document(id=record.id, dataset="demo", kind=str(record.values["kind"]), title=f"Record {record.id}", fields={"body": texts[record.id]}, metadata=values))
    engine.ingest(docs)
    return engine


def test_engine_counts_a_text_topic_over_the_whole_corpus_with_its_basis():
    engine = engine_with_text()
    result = engine.search(SearchRequest(query="How many records mention billing?", mode="rules", as_of="2024-04-15"))
    [aggregate] = result.aggregates
    assert aggregate["value"] == 2 and aggregate["exactness"] == "exact" and aggregate["document_ids"] == ["r1", "r2"]
    assert any(b.startswith("text scope: documents with an indexed chunk containing every one of the words 'billing'") for b in aggregate["basis"])
    share = engine.search(SearchRequest(query="What percentage of records mention billing?", mode="rules"))
    assert share.aggregates[0]["value"] == 0.5 and share.aggregates[0]["numerator"] == 2
    scoped = engine.search(SearchRequest(query="How many records mention billing?", mode="rules", filters={"status": "open"}))
    assert scoped.aggregates[0]["value"] == 1  # the text match honours request filters
    listed = engine.search(SearchRequest(query='List all records containing "billing dispute"', mode="rules"))
    assert listed.aggregates[0]["listed"] == [{"id": "r2"}] and listed.aggregates[0]["shown"] == "showing 1 of 1"


def test_as_of_anchors_relative_windows():
    with pytest.raises(ValueError):
        SearchRequest(query="q", as_of="next tuesday")
    engine = engine_with_text()
    ask = "How many records were created last month?"
    assert engine.search(SearchRequest(query=ask, mode="rules", as_of="2024-04-15T08:00:00+00:00")).aggregates[0]["value"] == 2
    assert engine.search(SearchRequest(query=ask, mode="rules", as_of="2024-05-02")).aggregates[0]["value"] == 1


# --- answer contract and packing ------------------------------------------------------------------


def test_answer_contract_accepts_numbers_of_the_new_exact_results():
    result = ScopeResult(
        exactness="exact",
        denominator=40,
        value={"2024-03": 12, "2024-04": 7},
        groups=[GroupRow(key={"period": "2024-03"}, value=12, count=12), GroupRow(key={"period": "2024-04"}, value=7, count=7)],
        total=19,
    )
    aggregate = AggregateEvidence(id="aggregate:1", result=result)
    contract = AnswerContract()
    for text, ok in (("There were 5 fewer records in April.", True), ("19 records matched in total.", True), ("There were 9 fewer records in April.", False)):
        answer = Answer(answer=text, claims=[Claim(text=text, kind="count", evidence_ids=["aggregate:1"])], citations=["aggregate:1"])
        checked = contract.enforce(answer, evidence_ids={"aggregate:1"}, aggregates=[aggregate], intents=detect_intents("how many", [], None))
        assert (checked.contract == "passed") == ok, (text, checked.violations)


def test_packing_caps_the_aggregate_reservation():
    records = [PackRecord(id="a", fields={"body": "a" * 900})]
    extras = [PackExtra(id="aggregate:1", passage="x" * 400), PackExtra(id="aggregate:2", passage="y" * 400)]
    capped = pack(records, PackingConfig(budget_chars=1000), extras)
    assert [e.id for e in capped.evidence] == ["a", "aggregate:1"] and capped.omitted_ids == ["aggregate:2"]
    assert capped.aggregate_reserve_chars == 500 and len(capped.evidence[0].passage) == 600
    full = pack(records, PackingConfig(budget_chars=1000, aggregate_reserve_max=1.0), extras)
    assert [e.id for e in full.evidence] == ["a", "aggregate:1", "aggregate:2"] and len(full.evidence[0].passage) == 200


def test_a_text_topic_word_the_lexical_analyzer_drops_is_bounded_not_exact(tmp_path):
    """Release review a3 B3: with sqlite ``bm25`` and ``tokenizer: ascii_alnum`` a non-ASCII topic word
    has no indexed token; the count is reported as bounded with the reason, never as exact."""
    schema = v2_schema_model().model_dump(mode="json")
    schema["timezone"] = "UTC"
    data = {
        "schema_version": 2,
        "dataset": "demo",
        "backends": {"local": {"kind": "sqlite", "path": str(tmp_path / "state"), "lexical_engine": "bm25", "bm25": {"tokenizer": "ascii_alnum"}}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "metadata_schema": schema,
        "pipeline": {"arms": {"dense": {"backend": "local"}, "lexical": {"backend": "local"}, "structured": {"backend": "local"}}},
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 64}}},
    }
    engine = Engine(validate_config(data))
    texts = {"r1": "billing 東京 office", "r2": "billing berlin office", "r3": "table layout", "r4": "region report"}
    docs = []
    for record in v2_records()[:4]:
        values = {k: v for k, v in record.values.items() if k in {"kind", "project", "status", "amount", "created_at"}}
        docs.append(Document(id=record.id, dataset="demo", kind=str(record.values["kind"]), title=f"Record {record.id}", fields={"body": texts[record.id]}, metadata=values))
    engine.ingest(docs)
    plain = engine.search(SearchRequest(query="How many records mention billing?", mode="rules"))
    assert plain.aggregates[0]["value"] == 2 and plain.aggregates[0]["exactness"] == "exact"
    assert any("bm25_okapi:ascii_alnum" in b for b in plain.aggregates[0]["basis"])  # the generation's analyzer
    dropped = engine.search(SearchRequest(query="How many records mention 東京?", mode="rules"))
    [aggregate] = dropped.aggregates
    assert aggregate["exactness"] == "bounded"
    assert any(u["reason"] == "not_supported_by_backend" and "indexes no token" in (u["suggestion"] or "") for u in aggregate["unsupported"])
