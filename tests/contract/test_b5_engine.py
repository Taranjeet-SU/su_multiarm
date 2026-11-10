"""Corpus-B rerun B5 shapes through the Engine (2026-11-09; synthetic ``typed_support`` corpus, fake roles).

* q023: the planner's exact structured count and a bounded SQL count of the same count question:
  the bounded count is marked ``superseded_by`` and never reaches the generator; a generator that
  still states another number is corrected (strip: the rendering; annotate: a correction note).
* q020: a value the planner and the SQL writer put on the wrong field (``status = 'Billing'``) runs on
  the field the user's question grounds it in (``module``), recorded in the trace and lineage.
"""

from __future__ import annotations

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


def engine_for(tmp_path, on_violation: str = "strip"):
    data = ts.config_dict(tmp_path / "state")
    data["pipeline"]["decision"] = {"kind": "llm", "typed_query": {"enabled": True}, "on_step_failure": "partial"}
    data["pipeline"]["arms"]["typed_query"].update({"mode": "sql_surface"})
    data["pipeline"]["generation"] = {"contract": {"on_violation": on_violation}}
    data.setdefault("models", {}).setdefault("roles", {}).update({"planner": FAKE, "generator": FAKE})
    engine = Engine(validate_config(data))
    engine.ingest(ts.documents())
    return engine


@pytest.fixture
def fake(monkeypatch):
    seen: dict = {"steps": None, "sql": None, "text": None, "generator": []}

    def respond(self, system, payload, schema, budget, stage):
        budget.reserve()
        try:
            if stage == "planner":
                return schema.model_validate({"steps": seen["steps"]})
            if stage == "sql_surface":
                return schema.model_validate({"thought": "Count.", "answerability": "answerable", "gaps": [], "sql_code": seen["sql"], "response": "Counts."})
            seen["generator"].append(payload)
            cited = [row["id"] for row in payload["evidence"] if row["source"] == "structured"]
            text = seen["text"].format(cited=cited[0])
            return Answer(answer=text, claims=[Claim(text=text.split("[")[0].strip(), kind="count", evidence_ids=[cited[0]])], citations=[cited[0]])
        finally:
            budget.record({"stage": stage, "provider": "fake", "model": "fake", "success": True, "seconds": 0.01, "usage": {"reported": True, "input_tokens": 10, "output_tokens": 1}})

    monkeypatch.setattr(JsonModel, "call", respond)
    return seen


def route(question: str) -> dict:
    return {"arm": "typed_query", "typed": {"analysis": "Count.", "answerability": "answerable", "gaps": [], "question": question}}


def count_step(field: str, value: str) -> dict:
    return {"arm": "structured", "scope": {"predicates": [{"field": field, "op": "eq", "value": value}], "measure": {"kind": "count"}}}


Q023 = "How many Billing records are there?"


@pytest.mark.parametrize("mode, outcome", [("strip", "stripped"), ("annotate", "annotated")])
def test_q023_a_bounded_count_is_superseded_and_a_wrong_number_corrected(tmp_path, fake, mode, outcome):
    fake["steps"] = [route("How many open tickets are there?"), count_step("module", "Billing")]
    fake["sql"] = "SELECT COUNT(*) AS n FROM ticket WHERE status = 'open'"  # misses the Billing qualifier: bounded 2
    fake["text"] = "There are 2 Billing records [{cited}]. Billing is the module of these records [{cited}]."
    result = engine_for(tmp_path, mode).answer(SearchRequest(query=Q023), principal=ALPHA)
    aggregates = {a["id"]: a for a in result.retrieval.aggregates}
    superseded = [a for a in aggregates.values() if a.get("superseded_by")]
    assert len(superseded) == 1 and superseded[0]["exactness"] == "bounded" and superseded[0]["value"] == 2
    exact = aggregates[superseded[0]["superseded_by"]]
    assert exact["exactness"] == "exact" and exact["value"] == 3
    shown = {row["id"] for payload in fake["generator"] for row in payload["evidence"]}
    assert superseded[0]["id"] not in shown and exact["id"] in shown  # never an alternative for the generator
    assert "SQL result: n=2 (bounded; 1 row)." == superseded[0]["rendering"]  # relabelled when bounded
    assert result.contract == outcome
    assert "2 Billing records" not in result.answer.answer and "records: 3 (exact" in result.answer.answer


def test_q020_a_value_on_the_wrong_field_runs_on_the_field_the_question_grounds(tmp_path, fake):
    question = "How many belong to Billing?"
    fake["steps"] = [route("How many tickets have status Billing?"), count_step("status", "Billing")]
    fake["sql"] = "SELECT COUNT(*) AS n FROM ticket WHERE status = 'Billing'"
    fake["text"] = "There are 3 records [{cited}]."
    result = engine_for(tmp_path).answer(SearchRequest(query=question), principal=ALPHA)
    sql = next(a for a in result.retrieval.aggregates if a.get("sql_surface"))
    structured = next(a for a in result.retrieval.aggregates if not a.get("sql_surface"))
    assert sql["exactness"] == "exact" and sql["value"] == 2  # two Billing tickets for t_alpha
    assert sql["sql_surface"]["checked_values"][0]["column_as_written"] == "ticket.status"
    assert structured["exactness"] == "exact" and structured["value"] == 3  # tickets and the asset
    assert {"field": "module", "op": "eq", "value": "Billing"} in [{k: p[k] for k in ("field", "op", "value")} for p in structured["predicates"]]
    assert all(u["reason"] != "value_not_observed" for u in result.retrieval.trace["decision"]["unsupported"])
