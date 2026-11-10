"""Arm cards (opt-in ``pipeline.decision.arm_cards``): generated per enabled arm, shapes from the wire
schema, deterministic, in the planner payload, the typed router payload and the SQL writer prompt.

Synthetic deployments only (``tests/typed_support.py`` corpus and the ``conftest`` v2 schema); the
models are fakes patched over ``JsonModel.call``. ``test_prompt_sizes`` prints the measured character
size of each prompt (system + serialised payload) with and without cards, and a rough token figure
(characters / 4, an estimate: no tokenizer is used).
"""

from __future__ import annotations

import json
from datetime import date

import pytest

pytest.importorskip("sqlglot")

from su_multi_arm import Engine, SearchRequest  # noqa: E402
from su_multi_arm.core.component import Dependencies  # noqa: E402
from su_multi_arm.core.config import ChatRole, DecisionConfig, validate_config  # noqa: E402
from su_multi_arm.decision import Capabilities, DecisionRequest, LlmAgent, LlmConfig, LlmPlan  # noqa: E402
from su_multi_arm.decision.arm_cards import STEP_FIELDS, shape, step_properties  # noqa: E402
from su_multi_arm.decision.intent import detect_intents  # noqa: E402
from su_multi_arm.decision.llm import LlmStep, capability_plan_schema  # noqa: E402
from su_multi_arm.decision.typed_router import TypedRoute  # noqa: E402
from su_multi_arm.decision.values import ValueResolver  # noqa: E402
from su_multi_arm.models import Answer  # noqa: E402
from su_multi_arm.profiles.prompts import PLANNER_PROMPT  # noqa: E402
from su_multi_arm.providers import JsonModel  # noqa: E402
from su_multi_arm.runtime.acl import Principal  # noqa: E402
from su_multi_arm.sql_surface.planner import SqlDraft  # noqa: E402

import typed_support as ts  # noqa: E402
from conftest import V2_GEN, V2_ID_PATTERN  # noqa: E402

FAKE = {"provider": "openai_compatible", "model": "fake"}
ROLE = ChatRole(provider="openai_compatible", model="synthetic")
QUESTION = "How many records are in project Alpha?"
ALPHA = Principal(id="alpha-reader", tenant="t_alpha", scopes=["search", "answer"])
COUNT_Q = "How many open tickets are in module Billing?"
LIMITS = {"lexical": {"candidates_per_step": 200}, "dense": {"candidates_per_step": 200}, "structured": {}, "graph": {"hops": 1, "direction": "any", "edge_kinds": ["fact"]}, "community": {}}


def _payload(v2_backend, v2_schema, arms, *, limits=LIMITS, graph_templates=None):
    agent = LlmAgent(LlmConfig(role=ROLE), Dependencies(backends={"records": v2_backend.records}))
    capabilities = Capabilities(arms=arms, metadata_schema=v2_schema, id_patterns=[V2_ID_PATTERN], graph_templates=graph_templates or {}, arm_limits=limits)
    intents = detect_intents(QUESTION, [V2_ID_PATTERN], capabilities.structured_targets())
    resolver = ValueResolver(v2_schema, v2_backend.records, V2_GEN, [])
    request = DecisionRequest(query=QUESTION, filters={"kind": "record"}, as_of=date(2024, 4, 2))
    return agent.payload(request, intents, capabilities, resolver)


def test_cards_are_off_by_default(v2_backend, v2_schema):
    payload = _payload(v2_backend, v2_schema, ["lexical", "structured"], limits=None)
    assert "arm_cards" not in payload
    assert DecisionConfig().arm_cards is False


@pytest.mark.parametrize("arms", [["lexical"], ["lexical", "structured"], ["exact_id", "dense", "lexical", "structured", "graph", "community"]])
def test_cards_exist_only_for_enabled_arms_in_wire_order(v2_backend, v2_schema, arms):
    limits = {**LIMITS, "exact_id": {"max_ids_per_step": 20, "case_insensitive": False, "alias_fields": []}}
    section = _payload(v2_backend, v2_schema, arms, limits={a: limits[a] for a in arms})["arm_cards"]
    names = [card["arm"] for card in section["cards"]]
    assert names == [a for a in ("exact_id", "dense", "lexical", "graph", "community", "structured") if a in arms]
    assert "typed_query" not in names and section["max_steps"] == 10
    for card in section["cards"]:
        assert set(card) == {"arm", "purpose", "use_when", "avoid_when", "input", "limits", "returns"}
        assert card["limits"] == limits[card["arm"]]


@pytest.mark.parametrize("templates", [None, {"depends_on": "Follow dependency edges from the seeds."}])
def test_input_shapes_are_the_wire_schema(v2_backend, v2_schema, templates):
    arms = ["dense", "lexical", "structured", "graph", "community"]
    payload = _payload(v2_backend, v2_schema, arms, graph_templates=templates)
    wire = capability_plan_schema(tuple(payload["available_arms"]), (), None).model_json_schema()
    properties, defs = step_properties(wire)
    for card in payload["arm_cards"]["cards"]:
        fields = {k: v for k, v in card["input"].items() if k != "arm"}
        assert card["input"]["arm"] == card["arm"]
        assert set(fields) <= set(STEP_FIELDS[card["arm"]]) and set(fields) <= set(properties)
        for name, value in fields.items():
            assert value == shape(properties[name], defs), (card["arm"], name)
    graph = next(c for c in payload["arm_cards"]["cards"] if c["arm"] == "graph")
    assert ("template" in graph["input"]) == bool(templates)
    if templates:
        assert graph["limits"]["templates"] == ["depends_on"]
    # the card fields are the ones the step accepts for the arm; a field the card omits is refused
    LlmStep.model_validate({"arm": "lexical", "query": "x", "filters": [{"field": "project", "value": "Alpha"}]})
    with pytest.raises(ValueError):
        LlmStep.model_validate({"arm": "lexical", "query": "x", "scope": {}})


def test_cards_are_deterministic(v2_backend, v2_schema):
    arms = ["dense", "lexical", "structured", "graph", "community"]
    first = json.dumps(_payload(v2_backend, v2_schema, arms)["arm_cards"])
    second = json.dumps(_payload(v2_backend, v2_schema, list(arms))["arm_cards"])
    assert first == second and "Alpha" not in first  # no source values in a card


def test_arm_cards_need_the_llm_planner():
    with pytest.raises(ValueError, match="arm_cards are read by the llm planner only"):
        DecisionConfig(kind="rules", arm_cards=True)
    assert DecisionConfig(kind="llm", arm_cards=True).arm_cards


# --- through the engine: planner, router and SQL writer ----------------------------------------


def config_data(tmp_path, *, routing="router", mode="sql_surface", cards=True):
    data = ts.config_dict(tmp_path / "state")
    data["pipeline"]["decision"] = {"kind": "llm", "arm_cards": cards, "typed_query": {"enabled": True, "routing": routing}}
    data["pipeline"]["arms"]["typed_query"]["mode"] = mode
    data.setdefault("models", {}).setdefault("roles", {}).update({"planner": FAKE, "generator": FAKE})
    return data


@pytest.fixture
def captured(monkeypatch):
    seen: dict = {}

    def respond(self, system, payload, schema, budget, stage):
        budget.reserve()
        seen.setdefault(stage, []).append((system, payload))
        try:
            if stage == "typed_router":
                return TypedRoute(route="typed", sub_question="Count the open tickets in module Billing.", reason="count")
            if stage == "sql_surface":
                assert schema is SqlDraft
                return SqlDraft(thought="Count.", answerability="answerable", sql_code="SELECT count(*) FROM ticket WHERE status = 'open' AND module = 'Billing'", response="Counts tickets.")
            if stage == "planner":
                assert issubclass(schema, LlmPlan)
                return schema.model_validate({"steps": [{"arm": "lexical", "query": "Billing tickets"}]})
            return Answer(answer=f"See [{payload['evidence'][0]['id']}].", citations=[payload["evidence"][0]["id"]])
        finally:
            budget.record({"stage": stage, "provider": "fake", "model": "fake", "success": True, "usage": {"reported": True, "input_tokens": 1, "output_tokens": 1}})

    monkeypatch.setattr(JsonModel, "call", respond)
    return seen


def _run(tmp_path, **kwargs):
    engine = Engine(validate_config(config_data(tmp_path, **kwargs)))
    engine.ingest(ts.documents())
    return engine.search(SearchRequest(query=COUNT_Q), principal=ALPHA)


def test_router_mode_cards_in_planner_router_and_sql_writer(tmp_path, captured):
    result = _run(tmp_path)
    assert result.aggregates[0]["value"] == 1
    [(_, planner)] = captured["planner"]
    names = [c["arm"] for c in planner["arm_cards"]["cards"]]
    assert "typed_query" not in names and "typed_query" not in planner["available_arms"] and names
    [(_, router)] = captured["typed_router"]
    assert set(router) == {"question", "request_filters", "typed_catalog", "routes"}
    ticket = router["typed_catalog"]["tables"]["ticket"]
    assert ticket["columns"]["status"] == {"type": "enum", "description": "Ticket workflow state at capture time."}
    assert router["typed_catalog"]["relations"][0]["from"] == "comment.parent_id"
    assert set(router["routes"]) == {"reply", "typed", "retrieval"} and router["routes"]["typed"]["limits"]["mode"] == "sql_surface"
    assert [a["arm"] for a in router["routes"]["retrieval"]["arms"]] == names
    wire = TypedRoute.model_json_schema()
    assert router["routes"]["reply"] == {k: shape(v, {}) for k, v in wire["properties"].items()}
    assert "samples" not in json.dumps(router) and "Billing" not in json.dumps(router["typed_catalog"])
    [(_, sql_prompt)] = captured["sql_surface"]
    assert "System limits" in sql_prompt and "at most 3 joins and 4 subqueries" in sql_prompt and "At most 50 rows" in sql_prompt
    assert "DECLARED JOIN PATHS" in sql_prompt and "Ticket workflow state at capture time." in sql_prompt  # relations and descriptions


def test_planner_mode_cards_include_the_typed_card(tmp_path, captured):
    captured.clear()
    _run(tmp_path, routing="planner")
    [(_, planner)] = captured["planner"]
    typed = next(c for c in planner["arm_cards"]["cards"] if c["arm"] == "typed_query")
    assert typed["limits"]["mode"] == "sql_surface" and typed["limits"]["row_limit"] == 50
    assert set(typed["input"]) == {"arm", "typed"} and "question" in json.dumps(typed["input"]["typed"])
    assert "typed_router" not in captured


def test_cards_off_changes_nothing(tmp_path, captured):
    _run(tmp_path, cards=False)
    [(_, planner)] = captured["planner"]
    [(_, router)] = captured["typed_router"]
    [(_, sql_prompt)] = captured["sql_surface"]
    assert "arm_cards" not in planner and "routes" not in router and "System limits" not in sql_prompt
    assert isinstance(router["typed_catalog"]["tables"]["ticket"], list)


def _size(system: str, payload) -> int:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
    return len(system) + len(text)


def test_prompt_sizes(tmp_path, captured, capsys):
    """Measured on the synthetic deployment (3 tables, 9 fields, lexical/dense/structured + typed)."""
    sizes = {}
    for cards in (False, True):
        captured.clear()
        _run(tmp_path / str(cards), cards=cards)
        for stage in ("typed_router", "sql_surface", "planner"):
            system, payload = captured[stage][0]
            sizes[(stage, cards)] = _size(system, payload)
    lines = [f"{stage:13s} cards={str(cards):5s} chars={chars:6d} ~tokens={chars // 4:5d}" for (stage, cards), chars in sorted(sizes.items())]
    with capsys.disabled():
        print("\nprompt sizes (synthetic deployment; tokens = chars/4 estimate):\n" + "\n".join(lines))
    for stage in ("typed_router", "sql_surface", "planner"):
        assert sizes[(stage, True)] > sizes[(stage, False)]
    assert sizes[("typed_router", False)] < 2500  # compact: names only
    assert sizes[("planner", True)] - sizes[("planner", False)] < 12000
    assert len(PLANNER_PROMPT) < sizes[("planner", False)]
