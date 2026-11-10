"""Source-aware arm cards (unified merge M2b-2, §6.11): built from configuration only, deterministic,
one card per knowledge source and one for the logs; the llm planner payload carries them only for a
two-halves question (otherwise it is unchanged). Synthetic configurations only."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from su_multi_arm.core.config import validate_config
from su_multi_arm.decision.source_cards import CARDS_VERSION, knowledge_sources, request_cards, static_cards

SHA = "a" * 64


def v3(tmp_path: Path, **crosssource) -> dict:
    data = {
        "schema_version": 3,
        "dataset": "cards",
        "backends": {"local": {"kind": "sqlite", "path": str(tmp_path / "state"), "lexical_engine": "bm25"}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 16}}},
        "pipeline": {"arms": {"lexical": {"backend": "local"}, "dense": {"backend": "local"}}},
        "collection": {
            "mode": "partitioned",
            "members": {
                "support": {"kind": "support", "sources": ["ticket", "doc"], "record_kinds": ["ticket", "doc"], "id_namespaces": ["ticket:", "https:"],
                            "identity_namespace": "support.account", "time_roles": {"ticket": {"event_time": "created_at"}}},
                "tracker": {"kind": "tracker", "sources": ["tracker_issue"], "record_kinds": ["tracker_issue"], "id_namespaces": ["tracker:"], "cadence": "weekly"},
                "logs": {"kind": "logs", "sources": ["logs"], "record_kinds": ["incident"], "key": "day", "cadence": "minutes", "id_namespaces": ["logs:"],
                         "producer": {"kind": "log_host"}},
            },
        },
    }
    if crosssource:
        data["crosssource"] = crosssource
    return data


def test_knowledge_sources_are_the_members_not_produced_by_a_log_host(tmp_path):
    config = validate_config(v3(tmp_path))
    assert knowledge_sources(config) == ["ticket", "doc", "tracker_issue"]
    declared = validate_config(v3(tmp_path, planner={"enabled": True, "knowledge_sources": ["ticket"]}))
    assert knowledge_sources(declared) == ["ticket"]


def test_cards_name_sources_members_and_identity_without_values(tmp_path):
    config = validate_config(v3(tmp_path, identity={"registry_path": "ids.sqlite", "sha256": SHA}, planner={"enabled": True}))
    cards = static_cards(config, ["lexical", "dense", "structured"])
    assert cards["version"] == CARDS_VERSION and "operations_half" in cards["rule"]
    by_source = {c["source"]: c for c in cards["sources"]}
    assert set(by_source) == {"ticket", "doc", "tracker_issue"}  # no logs card without log intents
    ticket = by_source["ticket"]
    assert ticket["half"] == "knowledge" and ticket["arms"] == ["lexical", "dense"]
    assert ticket["members"][0]["member"] == "support" and ticket["members"][0]["time_roles"] == {"ticket": {"event_time": "created_at"}}
    assert ticket["identity"] == {"namespaces": ["support.account"], "joined_field": "customer_id", "rule": "matched aliases of the pinned registry only"}
    assert by_source["tracker_issue"]["members"][0]["cadence"] == "weekly"
    assert static_cards(config, ["lexical", "dense", "structured"]) == cards  # deterministic
    text = json.dumps(cards)
    assert "Alderbank" not in text and "brightmoor" not in text  # configuration only, no record values


def test_the_logs_card_states_the_live_window_history_and_clocks():
    logs = SimpleNamespace(intents=SimpleNamespace(enabled=True), timezone="UTC",
                           collection=SimpleNamespace(serving=SimpleNamespace(default_view_days=3), archive=SimpleNamespace(enabled=True)))
    planner = SimpleNamespace(knowledge_sources=["ticket"], customer_namespaces=["support.account"], tenant_namespace="logs.tenant")
    config = SimpleNamespace(crosssource=SimpleNamespace(planner=planner, identity=object()), collection=None, logs=logs, question=SimpleNamespace(timezone="Australia/Adelaide"))
    cards = static_cards(config, ["lexical", "structured"])
    card = cards["sources"][-1]
    assert card["source"] == "logs" and card["half"] == "operations"
    assert card["time"]["live_window_days"] == 3 and card["time"]["history"].startswith("episode archive")
    assert card["time"]["question_timezone"] == "Australia/Adelaide" and card["time"]["partition_timezone"] == "UTC"
    assert card["identity"]["namespaces"] == ["logs.tenant"]
    request = request_cards(cards, [{"intent": "log_liveness", "args": {"service": ["crawler"]}}], ["a note"])
    assert request["operations_half"] == {"steps": [{"intent": "log_liveness", "args": {"service": ["crawler"]}}], "notes": ["a note"]}
    assert "operations_half" not in cards  # the static cards are not mutated


def test_the_planner_payload_carries_cards_only_when_set():
    from su_multi_arm.core.types import MetadataSchema
    from su_multi_arm.decision.base import Capabilities, DecisionRequest
    from su_multi_arm.decision.intent import Intents
    from su_multi_arm.decision.llm import LlmAgent, LlmConfig
    from su_multi_arm.core.component import Dependencies
    from su_multi_arm.core.config import ChatRole

    agent = LlmAgent(LlmConfig(role=ChatRole(provider="openai_compatible", model="fake")), Dependencies())
    caps = Capabilities(arms=["lexical", "dense"], metadata_schema=MetadataSchema())
    request = DecisionRequest(query="how do I split a sitemap")
    resolver = SimpleNamespace(planner_values=lambda: {})
    plain = agent.payload(request, Intents(), caps, resolver)
    assert "source_cards" not in plain
    cards = {"version": CARDS_VERSION, "sources": []}
    carded = agent.payload(request, Intents(), caps.model_copy(update={"source_cards": cards}), resolver)
    assert carded.pop("source_cards") == cards and carded == plain
