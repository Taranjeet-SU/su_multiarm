"""Typed-query contracts (typed-query plan M3): IR models, reasons, arm name and config surface.

Synthetic; no projection is built here (``test_typed_projection.py``).
"""

from __future__ import annotations

import typing

import pytest
from pydantic import ValidationError

from su_multi_arm.core.config import Config, validate_config
from su_multi_arm.core.config_projection import ProjectionConfig
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.core.types import FieldSpec, Measure
from su_multi_arm.core.types_typed import CompileError, PlannedTypedStep, TypedQueryDraft
from su_multi_arm.decision import ALL_ARMS, Capabilities
from su_multi_arm.models import UnsupportedReason

import typed_support as ts


def test_new_unsupported_reasons_are_additive():
    reasons = set(typing.get_args(UnsupportedReason))
    assert {"join_not_declared", "fanout_unsafe", "filter_not_applicable", "column_not_projectable", "projection_unavailable", "result_truncated", "planner_not_answerable"} <= reasons
    assert {"value_not_observed", "qualifier_not_applied", "intent_required"} <= reasons
    assert CompileError(path="grain", reason="column_not_projectable", allowed=["ticket"]).allowed == ["ticket"]


def test_typed_query_is_a_known_arm_but_not_a_retrieval_arm():
    assert "typed_query" in ALL_ARMS
    caps = Capabilities(arms=["lexical", "typed_query"])
    assert caps.retrieval_arms() == ["lexical"] and not caps.has_structured()


def test_ir_coherence():
    assert TypedQueryDraft(grain="ticket").select == []
    with pytest.raises(ValidationError, match="order/top_n"):
        TypedQueryDraft(grain="ticket", order="desc")
    with pytest.raises(ValidationError, match="select"):
        TypedQueryDraft(grain="ticket", measure=Measure(kind="count"), select=[{"table": "ticket", "column": "status"}])
    with pytest.raises(ValidationError, match="limit"):
        TypedQueryDraft(grain="ticket", measure=Measure(kind="count"), limit=3)
    with pytest.raises(ValidationError):
        TypedQueryDraft(grain="Ticket; DROP")  # names are identifiers, never free text
    with pytest.raises(ValidationError):
        TypedQueryDraft(grain="ticket", group_by=[{"table": "ticket", "column": "a"}] * 2)
    with pytest.raises(ValidationError, match="iff"):
        PlannedTypedStep(analysis="x", answerability="not_answerable", query=TypedQueryDraft(grain="ticket"))
    with pytest.raises(ValidationError, match="iff"):
        PlannedTypedStep(analysis="x", answerability="answerable")
    assert list(PlannedTypedStep.model_fields)[:2] == ["analysis", "answerability"]  # reason first


def test_field_description_is_optional_and_bounded():
    assert FieldSpec(name="a", type="string").description is None
    with pytest.raises(ValidationError):
        FieldSpec(name="a", type="string", description="")
    with pytest.raises(ValidationError):
        FieldSpec(name="a", type="string", description="x" * 1001)


def test_defaults_reproduce_today(tmp_path):
    from conftest import support_config

    config = support_config(tmp_path / "state")
    assert config.projection == ProjectionConfig() and not config.projection.enabled
    assert config.pipeline.arms.typed_query is None
    assert "projection" in Config.model_json_schema()["properties"]


def test_projection_model_rules():
    with pytest.raises(ValidationError, match="backend"):
        ProjectionConfig(enabled=True, tables={"t": {"kind": "k"}})
    with pytest.raises(ValidationError, match="one projection table"):
        ProjectionConfig(enabled=True, backend="b", tables={"a": {"kind": "k"}, "b": {"kind": "k"}})
    with pytest.raises(ValidationError, match="undeclared table"):
        ProjectionConfig(enabled=True, backend="b", tables={"a": {"kind": "k", "columns": ["p"]}}, relations={"r": {"from": "a.p", "to": "z.id", "cardinality": "many_to_one", "description": "d"}})
    with pytest.raises(ValidationError, match="self joins"):
        ProjectionConfig(enabled=True, backend="b", tables={"a": {"kind": "k", "columns": ["p"]}}, relations={"r": {"from": "a.p", "to": "a.id", "cardinality": "many_to_one", "description": "d"}})
    with pytest.raises(ValidationError):  # cardinality is declared, and only many_to_one is offered
        ProjectionConfig(enabled=True, backend="b", tables={"a": {"kind": "k", "columns": ["p"]}, "b": {"kind": "j"}}, relations={"r": {"from": "a.p", "to": "b.id", "cardinality": "one_to_many", "description": "d"}})
    with pytest.raises(ValidationError, match="identifier"):
        ProjectionConfig(tables={"a": {"kind": "k", "columns": ["_rejected"]}})
    with pytest.raises(ValidationError, match="not in the table"):
        ProjectionConfig(tables={"a": {"kind": "k", "columns": ["p"], "descriptions": {"q": "x"}}})
    dumped = ProjectionConfig.model_validate(ts.PROJECTION).recipe()
    assert dumped["relations"]["comment_parent"]["from"] == "comment.parent_id"


def test_typed_arm_needs_an_enabled_projection(tmp_path):
    data = ts.config_dict(tmp_path / "state", projection={"enabled": False})
    with pytest.raises(ConfigError, match="needs projection.enabled"):
        validate_config(data)


def test_projection_backend_must_exist(tmp_path):
    data = ts.config_dict(tmp_path / "state", projection={**ts.PROJECTION, "backend": "nowhere"})
    with pytest.raises(ConfigError, match="unknown backend"):
        validate_config(data)


def test_every_column_needs_a_reviewed_description(tmp_path):
    data = ts.config_dict(tmp_path / "state")
    for field in data["metadata_schema"]["fields"]:
        if field["name"] == "module":
            field.pop("description")
    with pytest.raises(ConfigError, match="reviewed description"):
        validate_config(data)
    data["projection"] = {**ts.PROJECTION, "tables": {**ts.PROJECTION["tables"], "ticket": {**ts.PROJECTION["tables"]["ticket"], "descriptions": {"module": "Reviewed module meaning."}}, "asset": {**ts.PROJECTION["tables"]["asset"], "descriptions": {"module": "Asset module."}}}}
    try:  # a projection-level description satisfies the rule
        validate_config(data)
    except ConfigError as error:
        assert "reviewed description" not in str(error)


def test_columns_must_be_declared_and_tenant_tables_need_the_access_column(tmp_path):
    projection = {**ts.PROJECTION, "tables": {**ts.PROJECTION["tables"], "asset": {"kind": "asset", "columns": ["module", "undeclared"]}}}
    with pytest.raises(ConfigError) as caught:
        validate_config(ts.config_dict(tmp_path / "state", projection=projection))
    assert "'undeclared' is not a declared metadata_schema field" in str(caught.value)
    assert "needs the access column 'tenant'" in str(caught.value)
