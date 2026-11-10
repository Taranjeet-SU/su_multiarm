"""Corpus-B rerun B5 losses (2026-11-09), reproduced on synthetic data only.

Shapes (no corpus data):

* q020: a count of the records that "belong to" a value the planner and the SQL writer put on the
  wrong field (``organization = 'Ledgerly'`` where ``Ledgerly`` is the distinctive word of the
  module value ``market_place__ledgerly``): the documented value normalization
  (``decision/value_match.py``) plus the question-grounded cross-field rule.
* q023: a bounded count next to an exact count of the same count question: the contract corrects
  the bounded number (strip and annotate), never keeps it as an annotated alternative.
* q041: a multi-term mention (an OR group of LIKEs) plus a status filter and a value on the wrong
  field compiles, applies and is exact; a bounded zero is never rendered or stated as an exact 0.
* q043: a ratio ranking among groups with at least N records: the structured per-group share with
  ``min_count`` (ties reported) and the SQL numerator inside a derived table recognised as applied.

Safety rows: no fuzzy match beyond the rule table, ambiguity is an explicit refusal, a request
filter is never rewritten, a question that names the field keeps the value on it, the cross-field
rule needs the user's question (never model text), two differing exact counts correct nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from su_multi_arm.core.types import FieldSpec, Measure, MetadataSchema, Predicate, ScopeResult, ScopeSpec, StructuredRecord
from su_multi_arm.decision.intent import detect_intents
from su_multi_arm.decision.scope import ScopeCompiler, ScopeDraft
from su_multi_arm.decision.value_match import field_named, match_value, question_mentions
from su_multi_arm.decision.values import ValueResolver
from su_multi_arm.generation.contract import AggregateEvidence, AnswerContract
from su_multi_arm.generation.rendering import relabel_bounded, render_aggregate
from su_multi_arm.models import Answer, Claim

MODULES = ["market_place__ledgerly", "market_place__chartwise", "llms", "search"]
ORGS = ["Acme Corp", "Globex LLC", "Initech", "Umbrella Group", "Hooli"]
STATUSES = ["new", "open", "pending", "hold", "solved", "closed"]
TARGETS = ["ticket", "record", "records", "status", "priority", "module", "organization"]


class Store:
    """A records store stand-in: observed values per field (ACL ignored) and ``exists``."""

    def __init__(self, values: dict[str, list[str]]):
        self.by_field = values

    def values(self, gen, field, acl, limit):
        return list(self.by_field.get(field, []))[:limit]

    def exists(self, gen, field, value, acl):
        return value in self.by_field.get(field, [])


def schema() -> MetadataSchema:
    def text(name: str, **kw: Any) -> FieldSpec:
        return FieldSpec(name=name, type="string", filterable=True, listable=True, groupable=True, **kw)

    return MetadataSchema(
        canonical_unit="ticket",
        fields=[text("status", casefold_match=True), text("priority", casefold_match=True), text("module", casefold_match=True), text("organization", max_catalog_cardinality=200), text("rca")],
    )


def resolver(question: str | None = None, **extra: list[str]) -> ValueResolver:
    values = {"status": STATUSES, "priority": ["urgent", "normal", "high"], "module": MODULES, "organization": ORGS, "rca": ["config_error", "user_error"], **extra}
    out = ValueResolver(schema(), Store(values), gen=object(), acl=[])  # type: ignore[arg-type]
    out.question = question
    return out


def eq(field: str, value: Any) -> Predicate:
    return Predicate(field=field, op="eq", value=value)


# --- 1. the documented normalization (value_match) ------------------------------------------------


@pytest.mark.parametrize(
    "value, stored, rule",
    [
        ("llms", "llms", "exact"),
        ("LLMS", "llms", "casefold"),
        ("Market-Place  Ledgerly", "market_place__ledgerly", "normalized"),  # separators read as one space
        ("Place Ledgerly", None, None),  # two words of three: not a whole value, not one word
        ("market place__ledgerly", "market_place__ledgerly", "normalized"),
        ("LLM", "llms", "plural"),
        ("Ledgerly", "market_place__ledgerly", "token"),
        ("ledgerlys", "market_place__ledgerly", "token"),  # plural fold, then the distinctive word
    ],
)
def test_the_rule_table_grounds_spellings_of_one_stored_value(value, stored, rule):
    match = match_value(value, MODULES, casefold=True)
    assert (match.value, match.rule) == (stored, rule)


@pytest.mark.parametrize("value", ["Ledgerlyy", "Ledg", "edgerly", "chart", "ledger", "llmss"])
def test_nothing_fuzzy_matches(value):
    match = match_value(value, MODULES, casefold=True)
    assert not match.found and not match.ambiguous


def test_case_only_matches_a_case_sensitive_field_by_the_normalized_rule():
    assert (match_value("Urgent", ["urgent"], casefold=False).rule) == "normalized"


def test_several_matches_are_ambiguous_never_a_pick():
    assert match_value("market place", ["Market Place", "market_place"], casefold=False).ambiguous == ["Market Place", "market_place"]
    assert match_value("market", ["market_place__a", "market_place__b"], casefold=False).ambiguous == ["market_place__a", "market_place__b"]


def test_question_helpers_are_word_bounded():
    assert question_mentions("How many belong to Ledgerly?", "ledgerly")
    assert not question_mentions("How many belong to Ledgerlyx?", "ledgerly")
    assert field_named("tickets from organization Ledgerly", "organization")
    assert field_named("what is the kg outcome", "kg_outcome")
    assert not field_named("How many belong to Ledgerly?", "organization")


# --- 1. ValueResolver: in-field canonicalization and the cross-field rule --------------------------


def test_an_agent_spelling_executes_the_stored_value_and_is_recorded():
    r = resolver()
    resolution = r.resolve(eq("module", "LLMs"))
    assert resolution.predicate == eq("module", "llms")
    assert resolution.canonical == [] and r.canonicalized == []  # casefold field: the casefold rule (unchanged behaviour)
    resolution = r.resolve(Predicate(field="module", op="in", value=["LLM", "llms", "Ledgerly"]))
    assert resolution.predicate.value == ["llms", "market_place__ledgerly"]  # two spellings of one value collapse
    assert [c["rule"] for c in resolution.canonical] == ["plural", "token"]
    assert r.canonicalized == resolution.canonical


def test_a_request_filter_is_never_rewritten():
    r = resolver()
    resolution = r.resolve(eq("organization", "acme corp"), from_request=True)
    assert resolution.predicate.value == "acme corp" and resolution.zero_match and resolution.canonical == []


def test_q020_a_value_on_the_wrong_field_moves_to_the_field_the_question_grounds_it_in():
    r = resolver("How many belong to Ledgerly?")
    resolution = r.resolve(eq("organization", "Ledgerly"))
    assert resolution.ok() and resolution.predicate == eq("module", "market_place__ledgerly")
    assert resolution.canonical == [{"field": "module", "from_field": "organization", "as_written": "Ledgerly", "value": "market_place__ledgerly", "rule": "question_field:token"}]


@pytest.mark.parametrize(
    "question",
    [
        None,  # no user question: the rule is off (never model text)
        "How many tickets from the organization Ledgerly?",  # the question names the field
        "How many belong to that marketplace app?",  # the value is not a word of the question
    ],
)
def test_the_cross_field_rule_needs_the_users_question(question):
    resolution = resolver(question).resolve(eq("organization", "Ledgerly"))
    assert resolution.predicate is None and resolution.unsupported.reason == "value_not_observed"


def test_a_value_two_fields_ground_is_an_explicit_unsupported_constraint():
    r = resolver("How many belong to Ledgerly?", rca=["ledgerly_sync__bug"])
    resolution = r.resolve(eq("organization", "Ledgerly"))
    assert resolution.predicate is None and resolution.unsupported.reason == "value_not_observed"
    assert "several values of other fields" in resolution.unsupported.suggestion


def test_q020_the_planner_scope_compiles_exact_with_the_canonical_value():
    r = resolver("How many belong to Ledgerly?")
    compiled = ScopeCompiler(schema(), r).compile(ScopeDraft(predicates=[eq("organization", "Ledgerly")], measure=Measure(kind="count")))
    assert compiled.executable() and compiled.spec.predicates == [eq("module", "market_place__ledgerly")]


# --- 2. q023: the exact count of a count question corrects a bounded alternative -------------------


def count(value: int, ident: str, exactness: str = "exact", unsupported: list | None = None) -> AggregateEvidence:
    result = ScopeResult(exactness=exactness, denominator=100, value=value, total=value, unsupported=unsupported or [])
    rendering = f"records: {value} ({exactness}; {value} records matched)."
    return AggregateEvidence(id=ident, result=result, rendering=rendering, scalar_count=True)


QUALIFIER = {"constraint_text": "LLMs", "reason": "qualifier_not_applied", "suggestion": "wider"}
Q023 = "How many belong to LLMs?"
WRONG_23 = "Based on the evidence, **24 tickets belong to LLMs** [aggregate:1].\n\nThe count covers subject and description matches [aggregate:1]."


def enforce(answer: Answer, aggregates: list[AggregateEvidence], question: str, mode: str):
    return AnswerContract(mode).enforce(answer, evidence_ids={a.id for a in aggregates}, aggregates=aggregates, intents=detect_intents(question, targets=TARGETS), question=question, targets=TARGETS)


def test_q023_annotate_corrects_the_bounded_number_and_cites_the_exact_count():
    aggregates = [count(24, "aggregate:1", "bounded", [QUALIFIER]), count(35, "aggregate:2")]
    answer = Answer(answer=WRONG_23, claims=[Claim(text="24 tickets belong to LLMs", kind="count", evidence_ids=["aggregate:1"])], citations=["aggregate:1"])
    result = enforce(answer, aggregates, Q023, "annotate")
    assert result.contract == "annotated"
    body = result.answer.answer.split("Limitations:")[0]
    assert "24" not in body and result.answer.claims == []
    assert result.limitations == ["A stated count that differs from the exact count of the question was removed; the exact result is: records: 35 (exact; 35 records matched). [aggregate:2]"]
    assert "aggregate:2" in result.answer.citations
    assert {v.rule: v.action for v in result.violations} == {"answer_number_mismatch": "strip"}


def test_q023_strip_falls_back_to_the_exact_rendering():
    aggregates = [count(24, "aggregate:1", "bounded", [QUALIFIER]), count(35, "aggregate:2")]
    result = enforce(Answer(answer=WRONG_23, citations=["aggregate:1"]), aggregates, Q023, "strip")
    assert result.contract == "stripped" and result.answer.answer.startswith("records: 35 (exact")


def test_two_differing_exact_counts_correct_nothing():
    aggregates = [count(24, "aggregate:1"), count(35, "aggregate:2")]
    text = "24 tickets belong to LLMs [aggregate:1]."
    result = enforce(Answer(answer=text, citations=["aggregate:1"]), aggregates, Q023, "annotate")
    assert result.contract == "passed"


def test_a_non_count_question_is_not_corrected():
    aggregates = [count(24, "aggregate:1", "bounded", [QUALIFIER]), count(35, "aggregate:2")]
    question = "Which tickets belong to LLMs?"
    result = enforce(Answer(answer="Tickets about LLMs are listed in [aggregate:1].", citations=["aggregate:1"]), aggregates, question, "annotate")
    assert all(v.rule != "answer_number_mismatch" for v in result.violations)


# --- 3. q041: a bounded zero is never an exact 0 -------------------------------------------------


def test_a_bounded_zero_is_relabelled_and_rendered_as_no_exact_zero():
    assert relabel_bounded("records: 0 (exact; 0 records matched).", zero=True) == "records: 0 (bounded, not an exact zero for the question; 0 records matched)."
    assert relabel_bounded("SQL result: tickets=24 (exact; 1 row).") == "SQL result: tickets=24 (bounded; 1 row)."
    assert relabel_bounded("records: 3 (bounded; 3 records matched).") == "records: 3 (bounded; 3 records matched)."
    spec = ScopeSpec(predicates=[eq("status", "open")], measure=Measure(kind="count"))
    rendering = render_aggregate(spec, ScopeResult(exactness="bounded", denominator=10, value=0, total=0, unsupported=[QUALIFIER]))
    assert rendering == "records: 0 (bounded, not an exact zero for the question; 0 records matched)."


def test_q041_annotate_removes_a_zero_count_without_an_exact_scope():
    question = "Among active Ledgerly tickets, how many literally mention 'intermittent,' 'misconfigured,' or 'failing'?"
    zero = count(0, "aggregate:1", "bounded", [{"constraint_text": "failing", "reason": "qualifier_not_applied", "suggestion": "x"}])
    text = "Among active Ledgerly tickets, 0 tickets mention the terms [aggregate:1].\n\nTicket [ticket:7] mentions failing [ticket:7]."
    result = AnswerContract("annotate").enforce(
        Answer(answer=text, citations=["aggregate:1", "ticket:7"]), evidence_ids={"aggregate:1", "ticket:7"}, aggregates=[zero], intents=detect_intents(question, targets=TARGETS), question=question, targets=TARGETS
    )
    assert result.contract == "annotated"
    body = result.answer.answer.split("Limitations:")[0]
    assert "0 tickets" not in body and "[ticket:7]" in body
    assert any(n.startswith("A zero count that no exact result backs was removed") for n in result.limitations)


# --- 4. q043: per-group share with a record threshold ----------------------------------------------


def ratio_records() -> list[StructuredRecord]:
    from su_multi_arm.core.types import Provenance

    plan = {"Acme Corp": (6, 3), "Globex LLC": (5, 1), "Initech": (5, 1), "Umbrella Group": (2, 2), "Hooli": (4, 0)}
    out, n = [], 0
    for org, (total, urgent) in plan.items():
        for k in range(total):
            n += 1
            values = {"organization": org, "priority": "urgent" if k < urgent else "normal", "status": "open"}
            out.append(StructuredRecord(schema_id="document", id=f"t-{n:03d}", values=values, provenance=Provenance(extractor="catalog")))
    return out


def run_scope(spec: ScopeSpec) -> ScopeResult:
    from su_multi_arm.backends._scope import scope_result

    records = ratio_records()
    return scope_result(spec, records, len(records), schema(), list(spec.predicates), [])


def test_q043_per_group_share_with_min_count_ranks_ratios_and_reports_ties():
    spec = ScopeSpec(group_by=["organization"], measure=Measure(kind="share"), numerator=[eq("priority", "urgent")], order="desc", top_n=2, min_count=4)
    result = run_scope(spec)
    assert result.exactness == "exact"
    rows = [(g.key["organization"], g.value, g.numerator, g.count, g.rank) for g in result.groups]
    assert rows == [("Acme Corp", 0.5, 3, 6, 1), ("Globex LLC", 0.2, 1, 5, 2)]
    assert [(g.key["organization"], g.rank) for g in result.ties] == [("Initech", 2)]  # the tie at the cut is reported
    rendering = render_aggregate(spec, result)
    assert rendering.startswith("share by organization (groups with at least 4 records): Acme Corp: 0.5 (3 of 6); Globex LLC: 0.2 (1 of 5)")
    assert "1 more groups tie the last one shown" in rendering


def test_q043_nth_rank_two_keeps_every_tied_group_and_the_threshold_drops_small_groups():
    result = run_scope(ScopeSpec(group_by=["organization"], measure=Measure(kind="share"), numerator=[eq("priority", "urgent")], order="desc", nth=2, min_count=4))
    assert sorted(g.key["organization"] for g in result.groups) == ["Globex LLC", "Initech"]
    every = run_scope(ScopeSpec(group_by=["organization"], measure=Measure(kind="share"), numerator=[eq("priority", "urgent")], order="desc"))
    assert every.groups[0].key["organization"] == "Umbrella Group" and every.groups[0].value == 1.0  # without min_count


def test_a_grouped_share_numerator_is_no_longer_ignored():
    result = run_scope(ScopeSpec(group_by=["organization"], measure=Measure(kind="share"), numerator=[eq("priority", "urgent")]))
    by_org = {g.key["organization"]: g.value for g in result.groups}
    assert by_org["Hooli"] == 0.0 and by_org["Acme Corp"] == 0.5  # was count / population before


def test_min_count_needs_group_by_and_the_compiler_passes_it_through():
    with pytest.raises(ValueError, match="min_count"):
        ScopeSpec(measure=Measure(kind="count"), min_count=3)
    r = resolver("Which organizations with at least 4 tickets have the highest urgent ratio?")
    draft = ScopeDraft(group_by=["organization"], measure=Measure(kind="share"), numerator=[eq("priority", "Urgent")], sort={"field": "ratio", "descending": True}, top_n=3, min_count=4)
    compiled = ScopeCompiler(schema(), r).compile(draft)
    assert compiled.executable(), compiled.unsupported
    assert compiled.spec.min_count == 4 and compiled.spec.order == "desc" and compiled.spec.sort is None
    assert compiled.spec.numerator == [eq("priority", "urgent")]
    incoherent = ScopeCompiler(schema(), r).compile(ScopeDraft(measure=Measure(kind="count"), min_count=4))
    assert not incoherent.executable() and incoherent.unsupported[0].reason == "measure_unsupported"


def test_the_contract_accepts_a_group_numerator_and_count():
    spec = ScopeSpec(group_by=["organization"], measure=Measure(kind="share"), numerator=[eq("priority", "urgent")], order="desc", top_n=2, min_count=4)
    result = run_scope(spec)
    aggregate = AggregateEvidence(id="aggregate:1", result=result, rendering=render_aggregate(spec, result))
    question = "Which organization with at least 4 tickets has the highest urgent ratio?"
    text = "Acme Corp has the highest urgent ratio: 3 of its 6 tickets are urgent (0.5) [aggregate:1]."
    checked = AnswerContract("strip").enforce(Answer(answer=text, citations=["aggregate:1"]), evidence_ids={"aggregate:1"}, aggregates=[aggregate], intents=detect_intents(question, targets=TARGETS), question=question, targets=TARGETS)
    assert checked.contract == "passed", checked.violations


# --- the typed IR compiler: in-field spellings ground, a cross-field move never runs ---------------


def test_the_ir_compiler_grounds_spellings_but_refuses_a_cross_field_move():
    import typed_support as ts

    from su_multi_arm.backends.memory import MemoryBackend
    from su_multi_arm.core.config_projection import ProjectionConfig
    from su_multi_arm.core.types import GenerationId
    from su_multi_arm.core.types_typed import TypedQueryDraft
    from su_multi_arm.decision.typed_query import TypedQueryCompiler
    from su_multi_arm.projection import ProjectionBuilder, ProjectionCatalog, ProjectionSpec

    gen = GenerationId(dataset="demo", generation="d" * 32, index_format=3, fingerprint="e" * 64)
    acl = [Predicate(field="tenant", op="eq", value="t_alpha")]
    sch = ts.schema()
    backend = MemoryBackend()
    backend.records.upsert(gen, ts.records(), sch)
    spec = ProjectionSpec.build(ProjectionConfig.model_validate(ts.PROJECTION), sch, "tenant")
    builder = ProjectionBuilder(spec, sch)
    builder.add(ts.records())
    files, manifest = builder.seal(gen)
    ProjectionBuilder.write(backend.relational, gen, files, manifest)
    catalog = ProjectionCatalog.load(backend.relational, gen, spec)
    values = ValueResolver(sch, backend.records, gen, acl)
    values.question = "How many belong to Billing?"  # grounds a move for structured scopes only
    compiler = TypedQueryCompiler(catalog, values, row_limit=50, max_groups=100)

    def draft(field: str, value: str) -> TypedQueryDraft:
        return TypedQueryDraft.model_validate({"grain": "ticket", "measure": {"kind": "count"}, "where": [{"table": "ticket", "predicate": {"field": field, "op": "eq", "value": value}}]})

    spelled = compiler.compile(draft("module", "billing"), {}, acl)
    assert spelled.errors == []
    moved = compiler.compile(draft("status", "Billing"), {}, acl)
    assert [(e.path, e.reason) for e in moved.errors] == [("where[0]", "value_not_observed")]
