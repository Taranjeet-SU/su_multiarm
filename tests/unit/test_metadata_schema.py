"""MetadataSchema (plan §3.1a): type/nullability/enum validation and filterable/listable gating."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from su_multi_arm.core.types import Document, FieldSpec, MetadataSchema

SCHEMA = MetadataSchema(
    fields=[
        FieldSpec(name="kind", type="enum", enum_values=["note", "record"], listable=True, groupable=True),
        FieldSpec(name="created_at", type="datetime", time_basis="created", sortable=True),
        FieldSpec(name="project", type="string", listable=True, nullable=False),
        FieldSpec(name="external_id", type="id", filterable=True, listable=False, max_catalog_cardinality=0),
        FieldSpec(name="score", type="number"),
        FieldSpec(name="count", type="integer"),
        FieldSpec(name="flag", type="boolean"),
        FieldSpec(name="day", type="date", timezone_policy="assume_utc"),
        FieldSpec(name="note", type="string", filterable=False),
    ],
    equivalence_field="external_id",
)


def categories(metadata):
    return sorted((v.field, v.category) for v in SCHEMA.validate_metadata({"project": "p", **metadata}))


def test_type_nullability_enum_and_timezone_violations_by_category():
    assert categories({"kind": "note", "created_at": "2024-01-02T03:04:05+00:00", "score": 1.5, "count": 2, "flag": False}) == []
    assert categories({"kind": "other"}) == [("kind", "enum")]
    assert categories({"created_at": "2024-01-02T03:04:05"}) == [("created_at", "timezone")]
    assert categories({"created_at": "yesterday"}) == [("created_at", "format")]
    assert categories({"count": 2.5}) == [("count", "type")]
    assert categories({"count": True}) == [("count", "type")]  # booleans are not integers
    assert categories({"score": "1"}) == [("score", "type")]
    assert categories({"flag": "yes"}) == [("flag", "type")]
    assert categories({"day": "2024-13-01"}) == [("day", "format")]
    assert categories({"kind": None}) == []  # nullable by default
    assert [(v.field, v.category) for v in SCHEMA.validate_metadata({})] == [("project", "null")]
    assert [(v.field, v.category) for v in SCHEMA.validate_metadata({"project": None})] == [("project", "null")]


def test_unknown_fields_policy_and_document_validation():
    assert SCHEMA.validate_metadata({"project": "p", "extra": 1}) == []
    strict = SCHEMA.model_copy(update={"unknown_fields": "reject"})
    assert [v.category for v in strict.validate_metadata({"project": "p", "extra": 1})] == ["unknown_field"]
    doc = Document(id="d", metadata={"project": "p", "kind": "bad"})
    assert [v.field for v in SCHEMA.validate_document(doc)] == ["kind"]


def test_gating_lists():
    assert SCHEMA.filterable() == ["kind", "created_at", "project", "external_id", "score", "count", "flag", "day"]
    assert SCHEMA.listable() == ["kind", "project"]
    assert SCHEMA.groupable() == ["kind"]
    assert SCHEMA.sortable() == ["created_at"]
    assert SCHEMA.field("missing") is None and SCHEMA.field("kind").type == "enum"


def test_spec_coherence_rules():
    with pytest.raises(ValidationError, match="enum requires"):
        FieldSpec(name="k", type="enum")
    with pytest.raises(ValidationError, match="unique"):
        FieldSpec(name="k", type="enum", enum_values=["a", "a"])
    with pytest.raises(ValidationError, match="only valid for type enum"):
        FieldSpec(name="k", type="string", enum_values=["a"])
    with pytest.raises(ValidationError, match="time_basis"):
        FieldSpec(name="k", type="string", time_basis="created")
    with pytest.raises(ValidationError, match="casefold_match"):
        FieldSpec(name="k", type="integer", casefold_match=True)
    with pytest.raises(ValidationError):
        FieldSpec(name="Bad-Name", type="string")
    with pytest.raises(ValidationError, match="unique"):
        MetadataSchema(fields=[FieldSpec(name="a", type="string"), FieldSpec(name="a", type="id")])
    with pytest.raises(ValidationError, match="equivalence_field"):
        MetadataSchema(fields=[], equivalence_field="a")
    assert MetadataSchema().filterable() == []  # neutral ships none
