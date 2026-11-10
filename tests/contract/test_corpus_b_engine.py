"""Corpus-B fix F1 through the Engine (synthetic two-tenant corpus, fake roles).

A ranking SQL result has no scalar value; the generator's count must be backed by the row cell it
quotes. Before the fix the generator path built ``AggregateEvidence`` without rows, so a correct
"Billing, 2 records" answer was an ``answer_number_mismatch``.
"""

from __future__ import annotations

import copy
import json

import pytest

pytest.importorskip("sqlglot")

from su_multi_arm import Engine, SearchRequest  # noqa: E402
from su_multi_arm.core.config import validate_config  # noqa: E402
from su_multi_arm.models import Answer, Claim  # noqa: E402
from su_multi_arm.providers import JsonModel  # noqa: E402
from su_multi_arm.runtime.acl import Principal  # noqa: E402

import typed_support as ts  # noqa: E402

FAKE = {"provider": "openai_compatible", "model": "fake"}
ALPHA = Principal(id="alpha-reader", tenant="t_alpha", scopes=["search", "answer"])
QUESTION = "Which module has the most records?"
ROUTE = {"arm": "typed_query", "typed": {"analysis": "Rank modules by record count.", "answerability": "answerable", "gaps": [], "question": "Which module has the most tickets?"}}


def engine_for(tmp_path):
    data = ts.config_dict(tmp_path / "state")
    data["pipeline"]["decision"] = {"kind": "llm", "typed_query": {"enabled": True}}
    data["pipeline"]["arms"]["typed_query"].update({"mode": "sql_surface"})
    data.setdefault("models", {}).setdefault("roles", {}).update({"planner": FAKE, "generator": FAKE})
    engine = Engine(validate_config(data))
    engine.ingest(ts.documents())
    return engine


@pytest.fixture
def fake(monkeypatch):
    seen: dict = {"sql": None, "count": 2, "generator": []}

    def respond(self, system, payload, schema, budget, stage):
        budget.reserve()
        try:
            if stage == "planner":
                return schema.model_validate({"steps": [ROUTE]})
            if stage == "sql_surface":
                return schema.model_validate({"thought": "Rank modules.", "answerability": "answerable", "gaps": [], "sql_code": seen["sql"], "response": "Ranks modules."})
            seen["generator"].append(payload)
            cited = next(row["id"] for row in payload["evidence"] if row["source"] == "structured")
            text = f"Billing has the most records, {seen['count']} records [{cited}]."
            return Answer(answer=text, claims=[Claim(text=f"{seen['count']} records", kind="count", evidence_ids=[cited])], citations=[cited])
        finally:
            budget.record({"stage": stage, "provider": "fake", "model": "fake", "success": True, "usage": {"reported": True, "input_tokens": 10, "output_tokens": 1}})

    monkeypatch.setattr(JsonModel, "call", respond)
    return seen


def test_a_ranked_sql_cell_backs_the_generator_count(tmp_path, fake):
    fake["sql"] = "SELECT module, COUNT(*) AS n FROM ticket GROUP BY module ORDER BY n DESC LIMIT 1"
    result = engine_for(tmp_path).answer(SearchRequest(query=QUESTION), principal=ALPHA)
    assert result.trace["contract"]["status"] == "passed", result.trace["contract"]
    assert "2 records" in result.answer.answer


def test_a_measure_the_sql_did_not_project_is_added_and_backs_the_count(tmp_path, fake):
    fake["sql"] = "SELECT module FROM ticket GROUP BY module ORDER BY COUNT(*) DESC LIMIT 1"
    result = engine_for(tmp_path).answer(SearchRequest(query=QUESTION), principal=ALPHA)
    assert result.trace["contract"]["status"] == "passed", result.trace["contract"]
    packed = json.loads(next(row["passage"] for row in fake["generator"][0]["evidence"] if row["source"] == "structured"))
    # B7: the small group lists its contributing records
    assert packed["rows"] == [{"values": {"module": "Billing", "sort_value": 2}, "contributor_ids": ["tk-1", "tk-2"]}] and packed["sort_projected"] == ["sort_value"]
    assert packed["rendering"].startswith("SQL result: module=Billing, sort_value=2 (records: tk-1, tk-2) (exact")


def test_a_count_no_row_holds_is_replaced_by_the_cited_sql_rendering(tmp_path, fake):
    fake["sql"] = "SELECT module, COUNT(*) AS n FROM ticket GROUP BY module ORDER BY n DESC LIMIT 1"
    fake["count"] = 3
    result = engine_for(tmp_path).answer(SearchRequest(query=QUESTION), principal=ALPHA)
    assert result.trace["contract"]["status"] == "stripped"
    assert "answer_number_mismatch" in {v["rule"] for v in result.trace["contract"]["violations"]}
    assert result.answer.answer.startswith("SQL result: module=Billing, n=2 (records: tk-1, tk-2) (exact")


def test_text_columns_are_built_on_ingest_and_matched_with_like(tmp_path, fake):
    """F6 through the write path: ``text_columns`` sourced from the title and a text field."""
    data = ts.config_dict(tmp_path / "state")
    data["projection"] = copy.deepcopy(data["projection"])  # ts.PROJECTION is shared
    data["projection"]["tables"]["ticket"]["text_columns"] = {
        "subject": {"source": "title", "description": "Ticket subject line."},
        "body": {"source": "fields.body", "description": "Original description; LIKE only."},
    }
    data["pipeline"]["decision"] = {"kind": "llm", "typed_query": {"enabled": True}}
    data["pipeline"]["arms"]["typed_query"].update({"mode": "sql_surface"})
    data.setdefault("models", {}).setdefault("roles", {}).update({"planner": FAKE, "generator": FAKE})
    engine = Engine(validate_config(data))
    engine.ingest(ts.documents())
    fake["sql"] = "SELECT COUNT(*) AS n FROM ticket WHERE body LIKE '%about Billing%'"
    fake["count"] = 2
    result = engine.answer(SearchRequest(query="How many records mention billing in their description?"), principal=ALPHA)
    packed = json.loads(next(row["passage"] for row in fake["generator"][0]["evidence"] if row["source"] == "structured"))
    assert packed["rows"] == [{"values": {"n": 2}}] and packed["exactness"] == "exact"  # tk-1, tk-2 (t_alpha only)
    assert result.trace["contract"]["status"] == "passed", result.trace["contract"]


def test_a_text_column_shadowing_a_metadata_field_is_a_config_error(tmp_path):
    from su_multi_arm.core.errors import ConfigError

    data = ts.config_dict(tmp_path / "state")
    data["projection"] = copy.deepcopy(data["projection"])  # ts.PROJECTION is shared
    data["projection"]["tables"]["asset"]["text_columns"] = {"status": {"source": "title", "description": "x"}}
    with pytest.raises((ConfigError, ValueError), match="shadows"):
        validate_config(data)


def test_a_like_without_a_mention_cue_leaves_the_value_qualifier_unapplied(tmp_path, fake):
    """"Billing records" names the module value: a LIKE on the description is another question."""
    data = ts.config_dict(tmp_path / "state")
    data["projection"] = copy.deepcopy(data["projection"])  # ts.PROJECTION is shared
    data["projection"]["tables"]["ticket"]["text_columns"] = {"body": {"source": "fields.body", "description": "Original description; LIKE only."}}
    data["pipeline"]["decision"] = {"kind": "llm", "typed_query": {"enabled": True}}
    data["pipeline"]["arms"]["typed_query"].update({"mode": "sql_surface"})
    data.setdefault("models", {}).setdefault("roles", {}).update({"planner": FAKE, "generator": FAKE})
    engine = Engine(validate_config(data))
    engine.ingest(ts.documents())
    fake["sql"] = "SELECT COUNT(*) AS n FROM ticket WHERE body LIKE '%Billing%'"
    engine.answer(SearchRequest(query="How many Billing records are there?"), principal=ALPHA)
    packed = json.loads(next(row["passage"] for row in fake["generator"][0]["evidence"] if row["source"] == "structured"))
    assert packed["exactness"] == "bounded" and any(u["reason"] == "qualifier_not_applied" for u in packed["unsupported"])
