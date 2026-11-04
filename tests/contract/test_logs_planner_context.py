"""The logs planner context pack (``decision/log_context.py``): what the ``llm`` planner sees under
the logs profile (alone or as the cascade's fallback stage), bounded in size and free of secrets,
with the intents as typed tools and configurable few-shot examples. The planner is scripted (no model
is called); the synthetic gold set is composed through the real pipeline. Evidence: contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from su_multi_arm import SearchRequest
from su_multi_arm.core.config import load_config
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.decision.log_intents import INPUT_MODELS
from su_multi_arm.providers import JsonModel

from tests.logs_gold_support import AS_OF, build_gold, gold_config

SECRET_ENV = "SU_TEST_PLANNER_CONTEXT_KEY"
SECRET = "sk-SYNTHETIC-planner-context-secret-000"  # gitleaks:allow (synthetic, never sent)
FAKE = {"provider": "openai_compatible", "model": "fake-local", "base_url": "http://127.0.0.1:9/v1", "api_key_env": SECRET_ENV}
D = AS_OF.isoformat()


def planner_config(tmp: Path, **intents) -> dict:
    data = gold_config(tmp)
    data["models"]["roles"]["planner"] = FAKE
    data["pipeline"]["decision"] = {"kind": "cascade", "stages": ["rules", "llm"]}
    data["request"] = {"modes": ["rules", "llm", "cascade"]}
    if intents:
        data["logs"]["intents"] = {"enabled": True, **intents}
    return data


@pytest.fixture(scope="module")
def gold(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("planner")
    mp = pytest.MonkeyPatch()
    mp.setenv(SECRET_ENV, SECRET)
    engine, _, _ = build_gold(tmp, data=planner_config(tmp))
    yield engine
    mp.undo()


class Scripted:
    """A planner that records its payload and answers with the few-shot call whose question shares
    the most words with the asked one (a stand-in for a model reading the examples)."""

    def __init__(self):
        self.payloads: list[dict] = []
        self.systems: list[str] = []

    def __call__(self, model, system, payload, schema, budget, stage):
        budget.reserve()
        self.payloads.append(payload)
        self.systems.append(system)
        words = set(payload["question"].casefold().split())
        best = max(payload["log_context"]["few_shot"], key=lambda e: len(words & set(e["question"].casefold().split())))
        pairs = []
        for key, value in best["args"].items():
            for item in value if isinstance(value, list) else [value]:
                pairs.append({"field": key, "value": item})
        return schema.model_validate({"steps": [{"arm": "structured", "log_intent": best["intent"], "log_args": pairs}]})


@pytest.fixture
def scripted(monkeypatch):
    planner = Scripted()
    monkeypatch.setattr(JsonModel, "call", lambda self, *a: planner(self, *a))
    monkeypatch.setenv(SECRET_ENV, SECRET)
    return planner


def test_issues_that_need_attention_plan_to_a_log_intent_in_cascade_and_llm_modes(gold, scripted):
    question = "what are issues that need my attention"
    for mode in ("cascade", "llm"):  # every agent looks for a log intent first
        found = gold.search(SearchRequest(query=question, as_of=D, mode=mode))
        assert found.aggregates[0]["intent"] in {"log_top", "log_new"} and found.aggregates[0]["args"]["level"] == ["error", "fatal"]
    assert not scripted.payloads  # the rules recognise it: no planner call
    # a phrasing without a failure word reaches the planner, which picks from the few-shot examples
    planned = gold.search(SearchRequest(query="anything needing my attention right now", as_of=D, mode="cascade"))
    assert len(scripted.payloads) == 1 and planned.aggregates[0]["intent"] in {"log_top", "log_new"}
    assert planned.aggregates[0]["rendering"]  # compiled from the intent, executed like the tool


def test_the_cascade_reaches_the_planner_with_the_pack_when_rules_find_no_intent(gold, scripted):
    result = gold.search(SearchRequest(query="is the parser keeping up", as_of=D, mode="cascade"))
    assert len(scripted.payloads) == 1
    assert result.aggregates[0]["intent"] == "log_coverage"
    pack = scripted.payloads[0]["log_context"]
    assert set(pack) >= {"arms", "structured_kinds", "graph_edge_kinds", "pattern_library", "data_dates", "vocabularies", "intents", "few_shot"}
    assert set(pack["intents"]) == set(INPUT_MODELS) and all("input_schema" in v for v in pack["intents"].values())
    assert {"dense", "lexical", "structured", "graph"} <= set(pack["arms"])
    library = pack["pattern_library"]
    assert library["available"] and "process.crashed" in {e["signature"] for e in library["entries"]}
    assert pack["data_dates"]["served_days"]["last"] == D and "2026-03-04" not in json.dumps(pack["data_dates"]["served_days"])
    assert "svc-legacy" in pack["vocabularies"]["service"]["values"] and "t02" in pack["vocabularies"]["tenant"]["values"]
    assert any(e["question"].startswith("what are the issues") for e in pack["few_shot"])


def test_the_pack_is_bounded_and_carries_no_secret_or_line_text(gold, scripted):
    gold.search(SearchRequest(query="is the parser keeping up", as_of=D, mode="llm"))
    text = json.dumps(scripted.payloads[0], default=str)
    pack = json.dumps(scripted.payloads[0]["log_context"], default=str)
    assert len(pack) <= gold.config.logs.intents.planner_context_max_chars
    assert SECRET not in text and SECRET_ENV not in pack and "127.0.0.1:9" not in text
    assert "out of memory" not in pack  # signature templates and example lines are not in the pack


def test_a_tight_bound_cuts_lists_in_order_and_names_the_cuts():
    from su_multi_arm.decision.log_context import bounded

    pack = {
        "pattern_library": {"entries": [{"signature": f"sig.{i:03d}", "fields": ["a", "b"]} for i in range(200)]},
        "vocabularies": {"service": {"values": [f"svc-{i}" for i in range(50)], "truncated": False}},
        "intents": {"log_top": {"description": "x", "input_schema": {"properties": {"by": {"type": "string"}}, "type": "object"}}},
        "few_shot": [{"question": f"q{i}", "intent": "log_top", "args": {}} for i in range(10)],
    }
    out = bounded(pack, 2_000)
    assert len(json.dumps(out)) <= 2_000 and out["truncated"][0] == "pattern_library" and len(out["few_shot"]) >= 3
    assert len(out["pattern_library"]["entries"]) < 200 and out["intents"]["log_top"]["input_schema"]  # cut only as far as needed


def test_the_scope_guard_still_refuses_a_free_scope_from_the_planner(gold, monkeypatch):
    def free(self, system, payload, schema, budget, stage):
        budget.reserve()
        assert "log_context" in payload
        return schema.model_validate({"steps": [{"arm": "structured", "scope": {"group_by": ["service"], "measure": {"kind": "sum", "field": "lines"}}}]})

    monkeypatch.setattr(JsonModel, "call", free)
    monkeypatch.setenv(SECRET_ENV, SECRET)
    result = gold.search(SearchRequest(query="numbers by component please", as_of=D, mode="llm"))
    assert not result.aggregates and any(u.reason == "intent_required" for u in result.unsupported_constraints)


def test_a_deployment_few_shot_file_replaces_the_profile_set(tmp_path):
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / "shots.yaml").write_text(yaml.safe_dump([{"question": "where do batches stall", "intent": "log_top", "args": {"by": "signature", "level": ["warn"]}}]))
    data = planner_config(tmp_path, few_shot_file="shots.yaml")
    path = tmp_path / "conf" / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    pytest.importorskip("networkx")
    config = load_config(path)
    assert [e.question for e in config.logs.intents.examples()] == ["where do batches stall"]
    (tmp_path / "conf" / "shots.yaml").write_text(yaml.safe_dump([{"question": "bad", "intent": "log_top", "args": {"by": "planet"}}]))
    with pytest.raises(ConfigError, match="few-shot"):
        load_config(path)
    profile = planner_config(tmp_path)
    (tmp_path / "conf" / "plain.yaml").write_text(yaml.safe_dump(profile))
    defaults = load_config(tmp_path / "conf" / "plain.yaml").logs.intents.examples()
    assert len(defaults) >= 5 and all(e.intent in INPUT_MODELS for e in defaults)
