"""Corpus-B rerun B4 contract defects (2026-11-09), reproduced on synthetic data only.

Shapes (no corpus data): an exact count stated without a typed claim (q028); ranking / ratio results
whose answers restate the question's threshold, the result size or advice parameters (q011, q034,
q042, q043, q045); a status breakdown the intent pre-pass missed (q001); a quoted phrase that is part
of the question, not a count (q037); conflicting counts of one mention claim (q037/q044); a
multi-term mention with a status qualifier bound in SQL (q041); the rank position of an OFFSET
result (q011). Safety rows: an unknown inline citation, a number that no cited exact aggregate
holds, an unrequested OR term, an "and" question over an OR group and an unbound module word all
stay refused or bounded.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from su_multi_arm.core.types import FieldSpec, MetadataSchema, ScopeResult
from su_multi_arm.decision.intent import detect_intents
from su_multi_arm.decision.qualifiers import qualifier_candidates
from su_multi_arm.generation.contract import AggregateEvidence, AnswerContract, result_statements
from su_multi_arm.generation.rendering import render_sql_rows
from su_multi_arm.models import Answer, Claim

TARGETS = ["ticket", "record", "records", "status", "priority", "module", "organization", "created", "month"]


def sql(rows: list[dict], columns: list[str], ident: str = "aggregate:1", exactness: str = "exact", offset: int = 0) -> AggregateEvidence:
    value = rows[0][columns[0]] if len(rows) == 1 and len(columns) == 1 else None
    result = ScopeResult(exactness=exactness, denominator=len(rows), value=value, total=len(rows))
    return AggregateEvidence(
        id=ident,
        result=result,
        rendering=render_sql_rows(columns, rows, exactness=exactness, offset=offset),
        rows=[{"values": r} for r in rows],
        columns=columns,
    )


def count(value: int, ident: str = "aggregate:2", exactness: str = "exact") -> AggregateEvidence:
    result = ScopeResult(exactness=exactness, denominator=100, value=value, total=value)
    return AggregateEvidence(id=ident, result=result, rendering=f"records: {value} ({exactness}; {value} records matched).")


def enforce(answer: Answer, aggregates: list[AggregateEvidence], question: str, mode: str = "strip", evidence: set[str] | None = None):
    ids = {a.id for a in aggregates} | (evidence or set())
    return AnswerContract(mode).enforce(answer, evidence_ids=ids, aggregates=aggregates, intents=detect_intents(question, targets=TARGETS), question=question, targets=TARGETS)


# --- q028: an exact count without a typed claim ---------------------------------------------------


def test_q028_an_exact_count_cited_inline_needs_no_typed_claim():
    question = "How many are pending?"
    text = '**22 tickets are pending** in this snapshot.\n\n- SQL aggregate: "SQL result: pending=22 (exact; 1 row)." [aggregate:1]\n- Count: "records: 22 (exact; 22 records matched)." [aggregate:2]'
    result = enforce(Answer(answer=text), [sql([{"pending": 22}], ["pending"]), count(22)], question)
    assert result.contract == "passed", result.violations
    assert result.answer.citations == ["aggregate:1", "aggregate:2"]  # inline markers are citations


def test_q028_a_number_no_cited_exact_aggregate_holds_still_abstains():
    question = "How many are pending?"
    text = "**23 tickets are pending** in this snapshot [aggregate:1]."
    result = enforce(Answer(answer=text), [sql([{"pending": 22}], ["pending"])], question)
    assert result.contract == "abstained" and [v.rule for v in result.violations] == ["answer_text_without_exact_scope"]


def test_q028_a_bounded_aggregate_never_backs_an_unlabelled_count():
    question = "How many are pending?"
    result = enforce(Answer(answer="22 tickets are pending [aggregate:2]."), [count(22, exactness="bounded")], question)
    assert result.contract == "abstained"


def test_an_unknown_inline_citation_is_unknown_evidence_in_every_mode():
    question = "How many are pending?"
    answer = Answer(answer="22 tickets are pending [aggregate:1] [ticket:999].", citations=["aggregate:1"])
    for mode in ("strip", "annotate", "abstain_with_reason"):
        result = enforce(answer, [sql([{"pending": 22}], ["pending"])], question, mode=mode, evidence={"ticket:1"})
        assert result.contract == "abstained" and result.violations[0].rule == "unknown_evidence", mode


def test_markdown_links_and_prose_brackets_are_not_citations():
    question = "How many are pending?"
    text = "22 tickets are pending [aggregate:1]. See [the guide](https://example.invalid/x) and [note: synthetic]."
    assert enforce(Answer(answer=text), [sql([{"pending": 22}], ["pending"])], question).contract == "passed"


# --- ranking / ratio shapes ---------------------------------------------------------------------


def test_q042_threshold_restated_from_the_question_is_not_a_count():
    question = "Which organization with at least 20 tickets has the highest urgent-ticket ratio?"
    text = "**Org A** has the highest urgent-ticket ratio among organizations with at least 20 tickets [aggregate:1].\n\n- SQL result: organization=Org A, urgent_ratio=0.3 (exact; 1 row). [aggregate:1]\n- The query returned one row."
    result = enforce(Answer(answer=text), [sql([{"organization": "Org A", "urgent_ratio": 0.3}], ["organization", "urgent_ratio"])], question)
    assert result.contract == "passed", result.violations


def test_a_threshold_the_question_does_not_state_is_still_checked():
    question = "Which organization has the highest urgent-ticket ratio?"
    text = "**Org A** has the highest ratio among organizations with at least 25 tickets [aggregate:1]."
    result = enforce(Answer(answer=text), [sql([{"organization": "Org A", "urgent_ratio": 0.3}], ["organization", "urgent_ratio"])], question)
    assert result.contract == "abstained" and result.violations[0].rule == "answer_text_without_exact_scope"


def test_q045_a_symbolic_threshold_in_the_question():
    question = "Which organization has the highest portal-mention rate among orgs with >=20 tickets?"
    text = "**Org A** has the highest portal-mention rate among organizations with at least 20 tickets [aggregate:1]. The aggregate returned 1 result row."
    result = enforce(Answer(answer=text), [sql([{"organization": "Org A", "portal_share": 0.675}], ["organization", "portal_share"])], question)
    assert result.contract == "passed", result.violations


def test_q043_inline_only_citations_and_the_requested_n():
    question = "What are the next two highest urgent ratios among organizations with at least 20 tickets?"
    rows = [{"organization": "Org B", "ratio": 0.238}, {"organization": "Org C", "ratio": 0.2}, {"organization": "Org D", "ratio": 0.2}]
    text = 'The next two organizations after the top one are **Org C** and **Org D**, tied at 0.2 [aggregate:1].\n\nThe ranking aggregate, verbatim: "SQL result: ... (exact; 3 rows)." [aggregate:1]'
    result = enforce(Answer(answer=text, citations=[]), [sql(rows, ["organization", "ratio"])], question)
    assert result.contract == "passed", result.violations
    assert result.answer.citations == ["aggregate:1"]


def test_a_result_size_that_differs_from_the_rows_is_still_a_count():
    question = "Which module has the most records?"
    text = "Billing ranks first [aggregate:1]. The query returned 4 rows."
    result = enforce(Answer(answer=text), [sql([{"module": "Billing", "n": 3}], ["module", "n"])], question)
    assert result.contract == "abstained"


def test_q011_advice_row_parameters_and_the_offset_position():
    question = "Which organization has the second-most tickets?"
    aggregate = sql([{"organization": "Org B", "tickets": 73}], ["organization", "tickets"], offset=1)
    assert "ranked position 2 of the ordering: OFFSET 1 skipped the first 1" in aggregate.rendering
    text = "**Org B** has the second-most tickets, 73 [aggregate:1].\n\nAdvice: rerun the aggregate and return at least two rows to see the leader too."
    result = enforce(Answer(answer=text, claims=[Claim(text="Org B has 73 tickets", kind="count", evidence_ids=["aggregate:1"])]), [aggregate], question)
    assert result.contract == "passed", result.violations
    plain = render_sql_rows(["organization", "tickets"], [{"organization": "Org A", "tickets": 87}], exactness="exact")
    assert "OFFSET" not in plain


def test_q034_q037_question_phrase_and_evidence_counts_are_not_record_counts():
    question = "How many ticket subject/descriptions contain the exact phrase '500 Internal Server Error'?"
    text = "Exactly 1 ticket matches the phrase '500 Internal Server Error' [aggregate:1]. The exact count is given by one aggregate result."
    result = enforce(Answer(answer=text), [sql([{"tickets": 1}], ["tickets"])], question)
    assert result.contract == "passed", result.violations
    quoted = Answer(answer=text)
    kept = result_statements([], quoted, question, [])
    assert kept == []


# --- q001: a breakdown named before its operator --------------------------------------------------


@pytest.mark.parametrize("question", ["Give me the full status breakdown of all tickets.", "What is the priority distribution?", "Show the breakdown by status"])
def test_q001_status_breakdown_is_a_breakdown_intent(question):
    assert "breakdown" in detect_intents(question, targets=TARGETS).kinds()


@pytest.mark.parametrize("question", ["The status of the release notes", "Is there a breakdown of the outage?"])
def test_breakdown_prose_without_a_target_stays_retrieval(question):
    assert "breakdown" not in detect_intents(question, targets=TARGETS).kinds()


def test_quoted_phrases_drop_the_sentence_punctuation_inside_the_quotes():
    intents = detect_intents("How many mention 'intermittent,' 'misconfigured,' or 'failing'?", targets=TARGETS)
    assert intents.quoted_phrases == ["intermittent", "misconfigured", "failing"]


# --- q041: distinctive value words are qualifiers --------------------------------------------------


class Resolver:
    def __init__(self, values: dict[str, list[str]]):
        self.values = values

    def observed(self, field: str):
        return self.values.get(field)

    def known_value(self, field: str, value: str) -> bool:
        return value.casefold() in {v.casefold() for v in self.values.get(field, [])}


SCHEMA = MetadataSchema(
    canonical_unit="ticket",
    fields=[
        FieldSpec(name="status", type="string", listable=True, groupable=True, states=[{"name": "active", "values": ["open", "pending", "hold", "new"]}]),
        FieldSpec(name="module", type="string", listable=True, groupable=True),
    ],
)
RESOLVER = Resolver({"status": ["open", "pending", "hold", "new", "closed"], "module": ["market_place__ledgerly", "search_client_1", "search_analytics_1", "llms"]})
Q041 = "Among active Ledgerly tickets, how many literally mention 'intermittent,' 'misconfigured,' or 'failing'?"


def test_q041_a_distinctive_word_of_one_value_is_a_value_qualifier():
    intents = detect_intents(Q041, targets=TARGETS)
    found = {(q.kind, q.field, q.value, q.token) for q in qualifier_candidates(Q041, SCHEMA, RESOLVER, intents)}
    assert ("value", "module", "market_place__ledgerly", True) in found
    assert ("value", "status", "active", False) in found
    # an ambiguous word ("search" is part of two values) is no candidate
    ambiguous = qualifier_candidates("How many search tickets?", SCHEMA, RESOLVER, detect_intents("How many search tickets?", targets=TARGETS))
    assert not any(q.token for q in ambiguous)


def lineage(*, module: bool, needles: tuple[str, ...] = ("intermittent", "misconfigured", "failing"), group_filter: str = "any_of") -> dict:
    checked = [{"column": "ticket.status", "op": "in", "value": v, "filter": "positive"} for v in ("open", "pending", "hold", "new")]
    if module:
        checked.append({"column": "ticket.module", "op": "eq", "value": "market_place__ledgerly", "filter": "positive"})
    patterns = [{"column": f"ticket.{c}", "op": "like", "pattern": f"%{n}%", "filter": group_filter, "group": 1} for n in needles for c in ("subject", "description")]
    return {"operation": "sql_aggregate", "sql_surface": {"checked_values": checked, "columns_referenced": ["ticket.status", "ticket.subject", "ticket.description"], "text_patterns": patterns}}


def test_q041_multi_term_mention_and_status_bound_in_sql_apply():
    from su_multi_arm.sql_surface import route

    intents = detect_intents(Q041, targets=TARGETS)
    assert route.executed_unapplied(lineage(module=True), Q041, {}, SCHEMA, RESOLVER, intents) == []


def test_q041_without_the_module_filter_the_sql_stays_bounded():
    from su_multi_arm.sql_surface import route

    missing = route.executed_unapplied(lineage(module=False), Q041, {}, SCHEMA, RESOLVER, detect_intents(Q041, targets=TARGETS))
    assert [u.constraint_text for u in missing] == ["Ledgerly"]


def test_an_or_group_never_applies_an_and_question_or_an_unrequested_term():
    from su_multi_arm.sql_surface import route

    both = "Among active Ledgerly tickets, how many mention both 'intermittent' and 'failing'?"
    missing = route.executed_unapplied(lineage(module=True, needles=("intermittent", "failing")), both, {}, SCHEMA, RESOLVER, detect_intents(both, targets=TARGETS))
    assert {u.constraint_text for u in missing} == {"intermittent", "failing"}
    one = "Among active Ledgerly tickets, how many mention 'failing'?"
    wider = route.executed_unapplied(lineage(module=True, needles=("failing", "timeout")), one, {}, SCHEMA, RESOLVER, detect_intents(one, targets=TARGETS))
    assert [u.constraint_text for u in wider] == ["failing"]
    other = route.executed_unapplied(lineage(module=True, group_filter="other"), Q041, {}, SCHEMA, RESOLVER, detect_intents(Q041, targets=TARGETS))
    assert {u.constraint_text for u in other} == {"intermittent", "misconfigured", "failing"}


# --- q037 / q044: conflicting counts of one mention claim ------------------------------------------


def reconcile(question: str, aggregates: list[dict]):
    from su_multi_arm.engine import Engine

    events: list[tuple] = []
    ctx = SimpleNamespace(trace=SimpleNamespace(event=lambda name, **kw: events.append((name, kw))))
    # the engine's ``_Retrieval`` always carries the decision; ``_supersede_counts`` (B5) reads its intents
    state = SimpleNamespace(aggregates=aggregates, request=SimpleNamespace(query=question), decision=SimpleNamespace(intents=detect_intents(question, targets=TARGETS)))
    Engine._reconcile_counts(ctx, state)
    return events


def sql_record(value: int | None, rows: list[dict], *, ident: str = "aggregate:1") -> dict:
    patterns = [{"column": "ticket.subject", "op": "like", "pattern": "%portal%", "filter": "any_of", "group": 1}]
    return {"id": ident, "operation": "sql_aggregate", "sql_derived": True, "exactness": "exact", "unsupported": [], "value": value, "rows": rows, "sql_surface": {"text_patterns": patterns}}


def text_record(value: int | None, *, ident: str = "aggregate:2") -> dict:
    return {"id": ident, "operation": "count", "text_scope": True, "exactness": "exact", "unsupported": [], "value": value}


def test_q037_the_text_column_count_wins_for_a_mention_question():
    aggregates = [sql_record(1, [{"values": {"tickets": 1}}]), text_record(5)]
    events = reconcile("How many ticket subjects contain the exact phrase 'portal'?", aggregates)
    assert aggregates[0]["exactness"] == "exact" and aggregates[1]["exactness"] == "bounded"
    assert aggregates[1]["unsupported"][0]["reason"] == "conflicting_count"
    assert events == [("conflicting_counts", {"demoted": ["aggregate:2"], "mention": True})]
    again = reconcile("How many ticket subjects contain the exact phrase 'portal'?", aggregates)
    assert again == [] and len(aggregates[1]["unsupported"]) == 1  # idempotent


def test_q044_without_a_mention_cue_neither_count_stays_exact():
    aggregates = [sql_record(None, [{"values": {"organization": "Org A", "tickets": 27}}]), text_record(None)]
    reconcile("Which organization has the most 'portal' tickets?", aggregates)
    assert [a["exactness"] for a in aggregates] == ["bounded", "bounded"]


def test_equal_counts_are_no_conflict():
    aggregates = [sql_record(3, [{"values": {"n": 3}}]), text_record(3)]
    assert reconcile("How many tickets mention 'portal'?", aggregates) == []
    assert [a["exactness"] for a in aggregates] == ["exact", "exact"]
