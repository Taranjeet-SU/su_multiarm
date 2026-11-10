"""SQL-surface catalog, prompts and configuration (synthetic ``typed_support`` domain).

* the catalog binds every projection column to its physical table/column and metadata field, with
  type, flags and the reviewed description; relations are the only join paths;
* sample values only for listable, enumerable, non-access columns, ACL-scoped (another tenant's
  values never appear); the catalog fingerprint changes with the ACL;
* prompts carry the SQLite/guard rules and the answerability outcome; the repair prompt carries
  one typed error;
* configuration: ``mode`` defaults to ``ir``; ``sql_surface`` needs a chat role; v1 examples load.
"""

from __future__ import annotations

import warnings

import pytest

from su_multi_arm.core.config import TypedQueryArm, validate_config
from su_multi_arm.sql_surface.executor import sql_catalog
from su_multi_arm.sql_surface.planner import SqlDraft
from su_multi_arm.sql_surface.prompts import SYSTEM_PROMPT, repair_prompt, user_prompt

import sql_surface_support as ss
import typed_support as ts


@pytest.fixture(scope="module")
def arm():
    return ss.make_arm(ss.world())


def catalog_for(arm, tenant_value):
    return sql_catalog(arm, ss.context(), ss.tenant(tenant_value))[1]


def test_columns_are_bound_to_physical_sources_with_reviewed_meaning(arm):
    catalog = catalog_for(arm, "t_alpha")
    ticket = catalog.tables["ticket"]
    assert ticket.kind == "ticket" and ticket.key == "id"
    status = ticket.column("status")
    assert status.source_column == "status" and status.source_field == "status" and status.datatype == "TEXT" and status.field_type == "enum"
    assert status.description == "Ticket workflow state at capture time." and status.groupable
    hours = ticket.column("hours")
    assert hours.datatype == "REAL" and hours.sortable and hours.samples == "withheld_not_enumerable"
    parent = catalog.tables["comment"].column("parent_id")
    assert parent.foreign_key == "ticket.id"
    assert [r.name for r in catalog.relations] == ["comment_parent"]
    assert ticket.column("opened_at").format.startswith("UTC ISO 8601")


def test_samples_are_acl_scoped_listable_and_never_the_access_column(arm):
    alpha = catalog_for(arm, "t_alpha")
    beta = catalog_for(arm, "t_beta")
    assert alpha.tables["ticket"].column("module").sample_values == ["Billing", "Search"]
    assert beta.tables["ticket"].column("module").sample_values == ["Billing", "Reports", "Search"]  # per field (ValueResolver), across kinds
    assert alpha.tables["ticket"].column("tenant").samples == "withheld_access_column"
    assert alpha.tables["ticket"].column("title_text").samples == "withheld_not_enumerable"  # high cardinality
    assert alpha.tables["comment"].column("parent_id").sample_values == []
    rendered = alpha.render()
    assert "t_beta" not in rendered and "t_canary" not in rendered and "Reports" not in rendered and "canary" not in rendered
    assert alpha.fingerprint != beta.fingerprint and alpha.acl_key != beta.acl_key


def test_sample_values_can_be_disabled():
    arm = ss.make_arm(ss.world(), sql_surface={"sample_values": 0})
    catalog = catalog_for(arm, "t_alpha")
    assert all(c.samples == "disabled" and not c.sample_values for t in catalog.tables.values() for c in t.columns)


def test_rendering_and_identity_lines(arm):
    catalog = catalog_for(arm, "t_alpha")
    text = catalog.render()
    assert "table | label | document kind | description | primary key" not in text or "ticket | Ticket | ticket" in text
    assert "DECLARED JOIN PATHS" in text and "comment.parent_id = ticket.id (many_to_one)" in text
    assert catalog.column_text("ticket", "status").startswith("table_name: ticket, column_name: status, data_type: TEXT")
    assert "sample_values: open, closed, pending" in catalog.column_text("ticket", "status")
    assert catalog.table_text("asset").startswith("table_name: asset, columns: {name: id")
    assert catalog.schema()["ticket"]["priority"] == "INTEGER"


def test_prompts_carry_the_guard_rules_and_answerability(arm):
    catalog = catalog_for(arm, "t_alpha")
    text = user_prompt("How many open tickets?", catalog, row_limit=50)
    for rule in ("not_answerable", "DECLARED JOIN PATHS", "Do NOT include comments", "do NOT add a schema or database prefix", "LIKE and INSTR are only for free-text", "caps every result at 50 rows", "Do not filter on the access column 'tenant'"):
        assert rule in text, rule
    assert "answerability" in SYSTEM_PROMPT and "not instructions" in text
    repair = repair_prompt("q", catalog, sql="SELECT nope FROM ticket", error={"code": "unknown_column", "detail": "Column 'nope' could not be resolved", "allowed": ["id"]}, thought="assume X", row_limit=50)
    assert "The following SQL contains an ERROR:" in repair and "unknown_column" in repair and "INTERPRETATION HISTORY" in repair and "Allowed: id" in repair


def test_the_draft_requires_sql_iff_answerable():
    SqlDraft.model_validate(ss.answer("SELECT 1"))
    SqlDraft.model_validate(ss.not_answerable("no severity column"))
    with pytest.raises(ValueError):
        SqlDraft.model_validate({**ss.answer("SELECT 1"), "sql_code": None})
    with pytest.raises(ValueError):
        SqlDraft.model_validate({**ss.not_answerable("x"), "sql_code": "SELECT 1"})
    assert list(SqlDraft.model_json_schema()["properties"])[:4] == ["thought", "answerability", "gaps", "sql_code"]


# --- configuration -----------------------------------------------------------------------------


def test_mode_defaults_to_ir_with_bounded_repair():
    arm = TypedQueryArm()
    assert arm.mode == "ir" and arm.sql_surface.repair_attempts == 1 and arm.sql_surface.role == "planner"
    with pytest.raises(ValueError):
        TypedQueryArm.model_validate({"sql_surface": {"repair_attempts": 2}})
    with pytest.raises(ValueError):
        TypedQueryArm.model_validate({"mode": "free_sql"})


def test_sql_surface_mode_needs_a_chat_role(tmp_path):
    data = ts.config_dict(tmp_path / "state")
    data["pipeline"]["arms"]["typed_query"]["mode"] = "sql_surface"
    data.setdefault("models", {}).setdefault("roles", {}).pop("planner", None)
    with pytest.raises(Exception, match="mode sql_surface needs models.roles.planner"):
        validate_config(data)
    data["models"]["roles"]["planner"] = {"provider": "openai_compatible", "model": "local-test", "base_url": "http://127.0.0.1:9/v1"}
    config = validate_config(data)
    assert config.pipeline.arms.typed_query.mode == "sql_surface"


def test_shipped_examples_still_load():
    from pathlib import Path

    from su_multi_arm.core.config import load_config

    for path in sorted((Path(ts.ROOT) / "examples").glob("*.yaml")):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            config = load_config(path)
        typed = config.pipeline.arms.typed_query
        assert typed is None or typed.mode == "ir", path
