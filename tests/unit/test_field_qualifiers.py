"""Non-string qualifiers (2.0.0a2 review B1): a filterable field named next to a value of its type.

Synthetic schema; no engine. Positive rows must yield a ``field`` candidate the given scope does not
apply; the negative list (plain questions, grouping words, unit words) must yield none.
"""

from __future__ import annotations

import pytest

from su_multi_arm.core.types import MetadataSchema, Predicate, ScopeSpec
from su_multi_arm.decision.intent import detect_intents
from su_multi_arm.decision.qualifiers import qualifier_candidates, unapplied

SCHEMA = MetadataSchema.model_validate(
    {
        "canonical_unit": "ticket",
        "fields": [
            {"name": "priority", "type": "integer"},
            {"name": "version", "type": "number"},
            {"name": "severity", "type": "string"},
            {"name": "tier", "type": "enum", "enum_values": ["gold", "silver"]},
            {"name": "escalated", "type": "boolean"},
            {"name": "ticket_ref", "type": "id"},
            {"name": "status", "type": "string"},
            {"name": "build_number", "type": "integer", "filterable": False},
        ],
    }
)


def flagged(question: str, predicates: list[dict] | None = None, filters: dict | None = None) -> list[str]:
    candidates = qualifier_candidates(question, SCHEMA, None, detect_intents(question))
    scope = ScopeSpec.model_validate({"predicates": predicates or [], "measure": {"kind": "count"}})
    return [u.constraint_text for u in unapplied(candidates, scope, filters or {}, SCHEMA)]


@pytest.mark.parametrize(
    "question,text",
    [
        ("How many priority 3 tickets are open?", "priority 3"),
        ("How many tickets on version 2.1?", "version 2.1"),
        ("How many severity high tickets?", "severity high"),
        ("How many tickets have tier gold?", "gold"),  # enum values are ``value`` candidates already
        ("How many escalated tickets are there?", "escalated"),
        ("How many tickets with escalated false?", "escalated false"),
        ("Count tickets with ticket ref TR-9 attached", "ticket ref TR-9"),
        ("How many tickets have priority at least 2?", "priority at least 2"),
        ("How many tickets have priority >= 2?", "priority >= 2"),
    ],
)
def test_field_named_with_a_value_is_a_qualifier(question, text):
    assert text in flagged(question)


@pytest.mark.parametrize(
    "question,predicates,filters",
    [
        ("How many priority 3 tickets are open?", [{"field": "priority", "op": "eq", "value": 3}], None),
        ("How many priority 3 tickets are open?", [{"field": "priority", "op": "in", "value": [2, 3]}], None),
        ("How many priority 3 tickets are open?", [], {"priority": "3"}),
        ("How many tickets on version 2.1?", [{"field": "version", "op": "eq", "value": 2.1}], None),
        ("How many severity high tickets?", [{"field": "severity", "op": "eq", "value": "High"}], None),
        ("How many escalated tickets are there?", [{"field": "escalated", "op": "eq", "value": True}], None),
        ("How many tickets with escalated false?", [{"field": "escalated", "op": "eq", "value": False}], None),
        ("How many tickets have priority at least 2?", [{"field": "priority", "op": "range", "lower": 2}], None),
    ],
)
def test_applied_field_qualifiers_are_not_flagged(question, predicates, filters):
    assert flagged(question, predicates, filters) == []


def test_wrong_typed_value_is_flagged():
    assert flagged("How many priority 3 tickets?", [{"field": "priority", "op": "eq", "value": 2}]) == ["priority 3"]
    assert flagged("How many tickets with escalated false?", [{"field": "escalated", "op": "eq", "value": True}]) == ["escalated false"]


NEGATIVE = [
    "How many tickets are open?",
    "How many tickets by priority?",
    "Count tickets per severity and status",
    "What priority are the open tickets?",
    "How many tickets have a status of",
    "How many tickets changed severity in the last release?",
    "List the top 3 tickets",
    "How many tickets were filed in 2024 with 3 attachments?",
    "How many tickets have build number 7?",  # not filterable: never a qualifier
    "What is the severity level of this issue?",
    "How do I raise the priority of a ticket?",
]


@pytest.mark.parametrize("question", NEGATIVE)
def test_plain_questions_yield_no_field_qualifier(question):
    candidates = qualifier_candidates(question, SCHEMA, None, detect_intents(question))
    assert [c for c in candidates if c.kind == "field"] == []


def test_a_year_on_an_integer_field_is_not_also_a_time_qualifier():
    schema = MetadataSchema.model_validate({"fields": [{"name": "year", "type": "integer"}]})
    candidates = qualifier_candidates("How many records for year 2024?", schema, None, detect_intents("How many records for year 2024?"))
    assert [c.kind for c in candidates] == ["field"]
    scope = ScopeSpec.model_validate({"predicates": [Predicate(field="year", op="eq", value=2024).model_dump()], "measure": {"kind": "count"}})
    assert unapplied(candidates, scope, {}, schema) == []
