"""Staging round 4 (N10): a declared enum field of another source is not a qualifier of a question
scoped to the knowledge sources unless its values occur there (synthetic)."""

from __future__ import annotations

from su_multi_arm.core.types import MetadataSchema
from su_multi_arm.decision.intent import detect_intents
from su_multi_arm.decision.qualifiers import qualifier_candidates
from su_multi_arm.decision.values import ValueResolver

QUESTION = "How many tickets contain the exact phrase '500 Internal Server Error'?"
SCHEMA = MetadataSchema.model_validate(
    {
        "fields": [
            {"name": "status", "type": "string", "filterable": True, "listable": True},
            {"name": "row_exactness", "type": "enum", "enum_values": ["exact", "approximate"], "filterable": True},
        ]
    }
)


class _Known(ValueResolver):
    def __init__(self, known: set[tuple[str, str]], **kw):
        super().__init__(SCHEMA, **kw)
        self.known = known

    def known_value(self, field: str, value: str) -> bool:
        return (field, value) in self.known


def _fields(resolver):
    return [(c.field, c.value) for c in qualifier_candidates(QUESTION, SCHEMA, resolver, detect_intents(QUESTION)) if c.field]


def test_unscoped_resolvers_keep_declared_enum_values():
    assert ("row_exactness", "exact") in _fields(_Known(set()))
    assert ("row_exactness", "exact") in _fields(None)


def test_a_scoped_resolver_drops_enum_values_absent_from_the_scope():
    assert ("row_exactness", "exact") not in _fields(_Known(set(), value_scoped=True))


def test_a_scoped_resolver_keeps_enum_values_present_in_the_scope():
    assert ("row_exactness", "exact") in _fields(_Known({("row_exactness", "exact")}, value_scoped=True))
