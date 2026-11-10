"""``on_violation: annotate`` and the generator latency summary through the Engine (2026-11-09).

Synthetic two-tenant corpus and fake roles (``typed_support``): a SQL ranking result is exact; the
fake generator states a wrong count next to a correct sentence. ``strip`` replaces the answer with
the SQL rendering; ``annotate`` keeps the cited sentence, removes the wrong number and states the
exact result in a limitation note (``contract: annotated``, ``limitations``). An OR group of text
matches applies the question's quoted phrase end to end.
"""

from __future__ import annotations

import copy

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


def engine_for(tmp_path, on_violation: str, text_columns: bool = False):
    data = ts.config_dict(tmp_path / "state")
    if text_columns:
        data["projection"] = copy.deepcopy(data["projection"])
        data["projection"]["tables"]["ticket"]["text_columns"] = {
            "subject": {"source": "title", "description": "Ticket subject line."},
            "body": {"source": "fields.body", "description": "Original description; LIKE only."},
        }
    data["pipeline"]["decision"] = {"kind": "llm", "typed_query": {"enabled": True}}
    data["pipeline"]["arms"]["typed_query"].update({"mode": "sql_surface"})
    data["pipeline"]["generation"] = {"contract": {"on_violation": on_violation}}
    data.setdefault("models", {}).setdefault("roles", {}).update({"planner": FAKE, "generator": FAKE})
    engine = Engine(validate_config(data))
    engine.ingest(ts.documents())
    return engine


@pytest.fixture
def fake(monkeypatch):
    seen: dict = {"sql": None, "text": None, "generator": []}

    def respond(self, system, payload, schema, budget, stage):
        budget.reserve()
        try:
            if stage == "planner":
                return schema.model_validate({"steps": [ROUTE]})
            if stage == "sql_surface":
                return schema.model_validate({"thought": "Rank.", "answerability": "answerable", "gaps": [], "sql_code": seen["sql"], "response": "Ranks."})
            seen["generator"].append(payload)
            cited = next(row["id"] for row in payload["evidence"] if row["source"] == "structured")
            text = seen["text"].format(cited=cited)
            return Answer(answer=text, claims=[Claim(text="Billing count", kind="count", evidence_ids=[cited])], citations=[cited])
        finally:
            budget.record({"stage": stage, "provider": "fake", "model": "fake", "success": True, "seconds": 0.01, "usage": {"reported": True, "input_tokens": 10, "output_tokens": 1}})

    monkeypatch.setattr(JsonModel, "call", respond)
    return seen


WRONG = "Billing has the most records [{cited}]. It has 5 records [{cited}]."


def test_strip_replaces_a_wrong_count_with_the_rendering(tmp_path, fake):
    fake["sql"] = "SELECT module, COUNT(*) AS n FROM ticket GROUP BY module ORDER BY n DESC LIMIT 1"
    fake["text"] = WRONG
    result = engine_for(tmp_path, "strip").answer(SearchRequest(query=QUESTION), principal=ALPHA)
    assert result.contract == "stripped" and result.limitations == []


def test_annotate_keeps_the_cited_answer_and_corrects_the_number(tmp_path, fake):
    fake["sql"] = "SELECT module, COUNT(*) AS n FROM ticket GROUP BY module ORDER BY n DESC LIMIT 1"
    fake["text"] = WRONG
    result = engine_for(tmp_path, "annotate").answer(SearchRequest(query=QUESTION), principal=ALPHA)
    assert result.contract == "annotated", result.trace["contract"]
    body, _, notes = result.answer.answer.partition("Limitations:")
    assert body.strip().startswith("Billing has the most records [aggregate:") and "5 records" not in body
    assert result.limitations == [f"A stated count that differs from the exact result was removed; the exact result is: SQL result: module=Billing, n=2 (records: tk-1, tk-2) (exact; 1 row; the records listed for a row are all of its contributing records). [{result.answer.citations[0]}]"]
    assert result.trace["contract"]["status"] == "annotated" and result.trace["contract"]["limitations"] == result.limitations
    assert [v["action"] for v in result.trace["contract"]["violations"]] == ["strip"]
    latency = result.trace["generator_latency"]
    assert latency["stream"] is False and latency["first_token_seconds"] is None and latency["seconds"] == 0.01


def test_an_or_group_of_text_matches_applies_the_quoted_phrase(tmp_path, fake):
    fake["sql"] = "SELECT COUNT(*) AS n FROM ticket WHERE subject LIKE '%about Billing%' OR body LIKE '%about Billing%'"
    fake["text"] = "2 records contain the phrase [{cited}]."
    result = engine_for(tmp_path, "strip", text_columns=True).answer(SearchRequest(query="How many records contain the phrase 'about Billing'?"), principal=ALPHA)
    assert result.contract == "passed", result.trace["contract"]
    assert "2 records" in result.answer.answer
