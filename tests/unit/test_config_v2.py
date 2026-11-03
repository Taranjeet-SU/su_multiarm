"""Config v2 (plan §3.3) and the v1 upgrade path (plan §5): validation invariants, deterministic
upgrade of every example, fingerprint round-trip (N6), schema drift, rejection cases."""

from __future__ import annotations

import copy
import json
import warnings
from pathlib import Path

import pytest
import yaml

from su_multi_arm.compat.config_v1 import Config as ConfigV1
from su_multi_arm.compat.config_v1 import downgrade, upgrade, upgrade_mapping, upgrade_text
from su_multi_arm.core import schema
from su_multi_arm.core.config import Config, load_config, validate_config
from su_multi_arm.core.errors import ArtifactError, ConfigError, PluginError

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = sorted((ROOT / "examples").glob("*.yaml"))

MINIMAL = {
    "schema_version": 2,
    "dataset": "demo",
    "backends": {"local": {"kind": "sqlite", "path": "./state"}},
    "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
    "pipeline": {"arms": {"dense": {"backend": "local"}, "lexical": {"backend": "local"}}},
}


def minimal(**overrides):
    data = copy.deepcopy(MINIMAL)
    for key, value in overrides.items():
        data[key] = value
    return data


def test_minimal_v2_config_validates_with_fail_closed_defaults():
    config = validate_config(minimal())
    assert config.schema_version == 2 and config.profile == "neutral"
    assert config.pipeline.decision.kind == "rules" and config.pipeline.decision.on_failure == "fail_closed"
    assert config.pipeline.selection.ordering == "reciprocal_rank" and config.pipeline.selection.rerank is False
    assert config.metadata_schema.filterable() == []
    assert config.budgets.max_llm_calls == 12 and config.privacy.trace_masking == "privacy"
    assert len(config.fingerprint()) == 64 and config.fingerprint() == validate_config(minimal()).fingerprint()


@pytest.mark.parametrize(
    "mutate, path, match",
    [
        (lambda d: d.__setitem__("unknown", 1), "unknown", "unknown key"),
        (lambda d: d["pipeline"].__setitem__("extra", 1), "pipeline.extra", "unknown key"),
        (lambda d: d["storage"].__setitem__("catalog", "nope"), "storage", "unknown backend"),
        (lambda d: d["pipeline"]["arms"].__setitem__("graph", {"backend": "nope"}), "pipeline.arms.graph", "unknown backend"),
        (lambda d: d.__setitem__("backends", {"local": {"kind": "sqlite"}, "graph": {"kind": "neo4j"}}) or d["storage"].__setitem__("catalog", "graph"), "storage.catalog", "does not declare capability 'catalog'"),
        (lambda d: d["backends"].__setitem__("pg", {"kind": "postgres"}) or d["pipeline"]["arms"].__setitem__("graph", {"backend": "pg"}), "pipeline.arms.graph", "status: planned"),
        (lambda d: d["backends"].__setitem__("pg", {"kind": "postgres"}) or d["storage"].__setitem__("communities", "pg"), "storage.communities", "status: planned"),
        (lambda d: d["backends"].__setitem__("rel", {"kind": "sqlalchemy", "url_env": "U"}) or d["pipeline"]["arms"].__setitem__("lexical", {"backend": "rel"}), "pipeline.arms.lexical", "status: planned"),
        (lambda d: d["backends"].__setitem__("rel", {"kind": "sqlalchemy", "url_env": "U"}) or d["pipeline"]["arms"].__setitem__("dense", {"backend": "rel"}), "pipeline.arms.dense", "status: planned"),
        (lambda d: d["backends"].__setitem__("pg", {"kind": "postgres", "text_search_config": "english; drop"}), "backends.pg", "text_search_config"),
        (lambda d: d["backends"].__setitem__("pg", {"kind": "postgres", "vector": {"index": "ivfflat"}}), "backends.pg", "index"),
        (lambda d: d["backends"].__setitem__("os", {"kind": "opensearch", "hosts_env": "OS"}) or d["storage"].__setitem__("ledger", "os"), "storage.ledger", "does not declare capability 'ledger'"),
        (lambda d: d["pipeline"].__setitem__("decision", {"kind": "llm"}), "planner", "planner is required"),
        (lambda d: d["pipeline"].__setitem__("review", {"enabled": True}), "reviewer", "reviewer"),
        (lambda d: d.__setitem__("extraction", {"extractors": ["llm"], "labels": ["topic"]}), "extractor", "extractor is required"),
        (lambda d: d.__setitem__("extraction", {"extractors": ["llm"]}), "extraction", "labels must declare"),
        (lambda d: d["pipeline"].__setitem__("selection", {"rerank": True}), "reranker", "reranker"),
        (lambda d: d.__setitem__("sessions", {"memory": {"semantic": True}}), "sessions.memory", "deferred"),
        (lambda d: d.__setitem__("budgets", {"max_usd": 1.0}), "budgets", "price_table"),
        (lambda d: d.__setitem__("profile", "no_such_profile"), "profile", "not available"),
        (lambda d: d["pipeline"].__setitem__("guards", ["safety_refusal"]), "guards", "neither a built-in guard"),
        (lambda d: d["pipeline"].__setitem__("decision", {"kind": "cascade"}), "decision", "cascade requires"),
        (lambda d: d["pipeline"].__setitem__("decision", {"kind": "classifier"}), "decision", "exactly one of artifact"),
        (lambda d: d.__setitem__("pipeline", {"arms": {"cache": {"backend": "local"}}}), "arms", "at least one retrieval arm"),
        (lambda d: d.__setitem__("serving", {"http": {"bind": "0.0.0.0:8099"}}), "serving", "requires auth"),
        (lambda d: d.__setitem__("schema_version", 3), "schema_version", ""),
    ],
)
def test_validation_errors_carry_a_key_path(mutate, path, match):
    data = minimal()
    mutate(data)
    with pytest.raises(ConfigError, match=match) as info:
        validate_config(data)
    assert path.split(".")[0] in str(info.value) or path in (info.value.path or "")


def test_community_slots_require_a_graph_with_memberships(monkeypatch):
    """The rule formerly exercised through neo4j (which now flags memberships): a graph backend
    without the ``memberships`` flag cannot hold communities or serve the community arm."""
    from su_multi_arm.backends import capabilities as caps

    without = caps.MATRIX["neo4j"].model_copy(deep=True)
    without.capabilities["graph"].flags["memberships"] = False
    monkeypatch.setitem(caps.MATRIX, "neo4j", without)
    for mutate, path in (
        (lambda d: d["pipeline"]["arms"].__setitem__("community", {"backend": "graph"}), "pipeline.arms.community"),
        (lambda d: d["storage"].__setitem__("communities", "graph"), "storage.communities"),
    ):
        data = minimal()
        data["backends"]["graph"] = {"kind": "neo4j"}
        mutate(data)
        with pytest.raises(ConfigError, match="memberships") as info:
            validate_config(data)
        assert path.split(".")[0] in str(info.value) or path in (info.value.path or "")
    monkeypatch.undo()
    data = minimal()
    data["backends"]["graph"] = {"kind": "neo4j"}
    data["storage"]["communities"] = "graph"
    data["pipeline"]["arms"]["community"] = {"backend": "graph"}
    assert validate_config(data).storage.communities == "graph"


def test_legacy_pairs_that_v1_code_serves_are_accepted():
    data = minimal(backends={"local": {"kind": "sqlite"}, "pg": {"kind": "postgres"}, "graph": {"kind": "neo4j"}})
    data["pipeline"]["arms"] = {"dense": {"backend": "pg"}, "lexical": {"backend": "pg"}, "graph": {"backend": "graph"}}
    config = validate_config(data)
    assert config.pipeline.arms.graph.backend == "graph"
    assert config.backends["pg"].vector.index == "exact" and config.backends["pg"].text_search_config == "english"
    assert config.models.roles.embedder.provider == "hash"
    tagged = minimal(models={"roles": {"extractor": {"provider": "openai_compatible", "model": "mistral:latest", "base_url": "http://127.0.0.1:11434/v1"}}})
    assert validate_config(tagged).models.roles.extractor.price_key() == "mistral:latest"
    with_key = validate_config(minimal(models={"roles": {"generator": {"provider": "openai_compatible", "model": "m", "pricing_key": "m-2026"}}}))
    assert with_key.models.roles.generator.price_key() == "m-2026"


def test_postgres_and_sqlalchemy_serve_storage_on_the_contracts():
    data = minimal(backends={"local": {"kind": "sqlite"}, "pg": {"kind": "postgres", "vector": {"index": "hnsw", "min_filter_recall": 0.9}}, "rel": {"kind": "sqlalchemy", "url_env": "SU_SQL_URL"}})
    data["storage"].update({"catalog": "pg", "records": "pg", "ledger": "rel", "sessions": "rel", "cache": "pg"})
    data["pipeline"]["arms"] = {"dense": {"backend": "pg"}, "lexical": {"backend": "pg"}, "structured": {"backend": "rel"}}
    config = validate_config(data)
    assert config.storage.catalog == "pg" and config.backends["pg"].vector.min_filter_recall == 0.9
    assert config.storage.communities == "local"  # postgres graph is planned; communities stay on a graph backend


def test_native_structured_output_is_rejected_when_not_declared():
    data = minimal(models={"roles": {"generator": {"provider": "openai_compatible", "model": "m", "capabilities": {"structured_output": "native"}}}})
    with pytest.raises(ConfigError, match="native is not declared"):
        validate_config(data)
    data["models"]["roles"]["generator"]["capabilities"]["structured_output"] = "json_schema"
    assert validate_config(data).models.roles.generator.capabilities.structured_output == "json_schema"
    with pytest.raises(ConfigError, match="api_key_env"):
        validate_config(minimal(models={"roles": {"generator": {"provider": "anthropic", "model": "m"}}}))


@pytest.mark.parametrize(
    "plugins, match",
    [(["pkg.module:Factory"], "entry-point name"), (["../evil"], "entry-point name"), (["x", "x"], "unique")],
)
def test_plugin_references_must_be_entry_point_names(plugins, match):
    with pytest.raises(ConfigError, match=match):
        validate_config(minimal(plugins=plugins))


def test_unregistered_plugin_is_rejected_at_load(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(minimal(plugins=["not_installed_anywhere"])))
    with pytest.raises(PluginError, match="not registered"):
        load_config(path)
    assert load_config(path, check_plugins=False).plugins == ["not_installed_anywhere"]


@pytest.mark.parametrize(
    "decision",
    [
        {"kind": "classifier", "artifact": {"model_path": "clf.pkl", "sha256": "0" * 64, "manifest": {"features": "hashed_ngrams", "labels": ["a"], "threshold": 0.5}}},
        {"kind": "classifier", "artifact": {"model_path": "sklearn.pipeline.Pipeline", "sha256": "0" * 64, "manifest": {"features": "hashed_ngrams", "labels": ["a"], "threshold": 0.5}}},
        {"kind": "classifier", "component": "pkg.module:Clf"},
    ],
)
def test_classifier_pickle_and_dotted_paths_fail_validation(decision):
    data = minimal()
    data["pipeline"]["decision"] = decision
    with pytest.raises((ConfigError, ArtifactError)):
        validate_config(data)


def test_classifier_plugin_component_must_be_listed_under_plugins():
    data = minimal()
    data["pipeline"]["decision"] = {"kind": "classifier", "component": "my_clf"}
    with pytest.raises(ConfigError, match="listed under plugins"):
        validate_config(data)
    data["plugins"] = ["my_clf"]
    assert validate_config(data).pipeline.decision.component == "my_clf"


def test_code_references_anywhere_in_config_are_rejected_but_prompts_are_exempt():
    data = minimal(ingestion={"input_mapping": {"id": "pkg.loader:factory"}})
    with pytest.raises(ArtifactError, match="dotted import"):
        validate_config(data)
    data = minimal()
    data["pipeline"]["generation"] = {"prompt": "Return module:attr style keys"}
    assert validate_config(data).pipeline.generation.prompt


# --- v1 upgrade ------------------------------------------------------------------------------


@pytest.mark.parametrize("example", EXAMPLES, ids=[p.name for p in EXAMPLES])
def test_every_example_upgrades_deterministically_and_round_trips_its_fingerprint(example):
    data = yaml.safe_load(example.read_text())
    v1 = ConfigV1.model_validate(data)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        first, second = upgrade_mapping(data), upgrade_mapping(data)
        back = downgrade(first.config)
    assert first.data == second.data and first.config == second.config
    assert first.config.schema_version == 2 and first.config.fingerprint() == second.config.fingerprint()
    assert back.index_fingerprint() == v1.index_fingerprint()  # N6
    assert first.config.legacy_fingerprint() == v1.index_fingerprint()
    assert sorted(back.arms.enabled) == sorted(v1.arms.enabled)
    assert back.model_dump(exclude={"arms": {"enabled"}}) == v1.model_dump(exclude={"arms": {"enabled"}})
    # The written v2 file validates again to the same config.
    text, _ = upgrade_text(example.read_text())
    reloaded = validate_config(yaml.safe_load(text))
    assert reloaded == first.config and text.startswith("# Upgraded from schema_version 1")
    assert any("declare real types" in w for w in first.warnings)
    assert [d.code for d in first.diagnostics] == ["untyped_fields"] and not first.lossy


def test_upgrade_mapping_table_rows():
    v1 = ConfigV1.model_validate(yaml.safe_load((ROOT / "examples/anthropic.yaml").read_text()))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        v2 = upgrade(v1).config
    assert v2.backends["local"].kind == "sqlite" and v2.backends["local"].vector_engine == "faiss"
    assert v2.pipeline.decision.kind == "llm" and v2.pipeline.review.enabled is False
    assert v2.models.roles.planner.provider == "anthropic" and v2.models.roles.reviewer.model == "claude-haiku-5-5"
    assert v2.models.roles.generator.evidence_budget.chars == 60000 and v2.models.roles.community.evidence_budget.chars == 30000
    assert v2.extraction.extractors == ["llm"] and v2.models.roles.extractor.provider == "anthropic"
    assert v2.budgets.max_llm_calls == 100 and v2.privacy.redact_emails is True
    assert v2.pipeline.arms.graph.hops == 2 and set(v2.pipeline.arms.enabled()) == {"dense", "lexical", "structured", "graph", "community"}
    assert v2.pipeline.selection.source_weights == {"article": 1.2, "manual": 1.1, "case": 1.0, "tracker": 0.9}
    assert [f.type for f in v2.metadata_schema.fields] == ["string"] * 7 and v2.metadata_schema.filterable()[0] == "source"
    agentic = v1.model_copy(deep=True)
    agentic.routing.mode = "agentic"
    agentic.fusion.strategy = "rrf"
    agentic.extraction.providers = ["tags", "llm"]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        v2a = upgrade(agentic).config
    assert v2a.pipeline.review.enabled is True and v2a.pipeline.review.max_hops == 2
    assert v2a.pipeline.selection.ordering == "rrf" and v2a.extraction.extractors == ["gazetteer", "llm"]
    assert downgrade(v2a).routing.mode == "agentic" and downgrade(v2a).fusion.strategy == "rrf"
    pg = ConfigV1.model_validate(yaml.safe_load((ROOT / "examples/postgres_neo4j.yaml").read_text()))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        v2p = upgrade(pg).config
    assert v2p.backends["pg"].kind == "postgres" and v2p.backends["graph"].kind == "neo4j"
    assert v2p.pipeline.arms.dense.backend == "pg" and v2p.pipeline.arms.lexical.backend == "pg" and v2p.pipeline.arms.graph.backend == "graph"


def test_lossy_v1_settings_are_reported_never_dropped_silently():
    data = yaml.safe_load((ROOT / "examples/local.yaml").read_text())
    data["community"]["evidence_char_budget"] = 1000  # no community model: v1 ignored it, but it is in the fingerprint
    data["extraction"]["model"] = {"max_tokens": 512}  # provider none with a non-default field
    data["generation"] = {"model": {"timeout_seconds": 5}}  # not fingerprint-affecting, still reported
    v1 = ConfigV1.model_validate(data)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = upgrade_mapping(data)
    lossy = {d.path: d.message for d in result.diagnostics if d.code == "lossy_upgrade"}
    assert set(lossy) == {"community.evidence_char_budget", "extraction.model", "generation.model"}
    assert "fingerprint" in lossy["community.evidence_char_budget"] and "fingerprint" in lossy["extraction.model"]
    assert "fingerprint" not in lossy["generation.model"] and result.lossy
    assert all(d.severity == "warning" for d in result.diagnostics)
    assert result.config.legacy_fingerprint() != v1.index_fingerprint()  # the narrowed N6 case, reported above
    with pytest.raises(ConfigError, match="lossy upgrade") as info:
        upgrade_mapping(data, strict=True)
    assert info.value.path == "extraction.model"
    text, _ = upgrade_text(yaml.safe_dump(data))
    assert "# WARNING: community.evidence_char_budget" in text
    with pytest.raises(ConfigError, match="lossy"):
        upgrade_text(yaml.safe_dump(data), strict=True)
    path = ROOT / "examples/local.yaml"
    with pytest.warns(UserWarning, match="schema_version 1") as caught:
        load_config(path)
    assert "lossy" not in str(caught[0].message)


def test_downgrade_refuses_v1_inexpressible_features():
    config = validate_config(minimal())
    with pytest.raises(ConfigError, match="exact_id or cache"):
        downgrade(config.model_copy(update={"pipeline": config.pipeline.model_copy(update={"arms": config.pipeline.arms.model_copy(update={"exact_id": {}})})}))
    data = minimal(backends={"local": {"kind": "memory"}})
    with pytest.raises(ConfigError, match="kind sqlite"):
        downgrade(validate_config(data))
    for pg, key in (({"vector": {"index": "hnsw"}}, "vector"), ({"text_search_config": "simple"}, "text_search_config")):
        data = minimal(backends={"local": {"kind": "sqlite"}, "pg": {"kind": "postgres", **pg}})
        data["pipeline"]["arms"] = {"dense": {"backend": "pg"}}
        with pytest.raises(ConfigError, match=f"v1 has no postgres {key}"):
            downgrade(validate_config(data))
    with pytest.raises(ConfigError, match="v1 can only express"):
        downgrade(validate_config(minimal(backends={"local": {"kind": "sqlite"}, "rel": {"kind": "sqlalchemy", "url_env": "U"}})))


def test_load_config_accepts_v1_with_warning_and_resolves_paths(tmp_path):
    (tmp_path / "prompt.txt").write_text("Specific instructions")
    (tmp_path / "v1.yaml").write_text("storage_path: store\ngeneration:\n  prompt_file: prompt.txt\n")
    with pytest.warns(UserWarning, match="schema_version 1"):
        config = load_config(tmp_path / "v1.yaml")
    assert config.backends["local"].path == tmp_path / "store"
    assert config.pipeline.generation.prompt == "Specific instructions"
    (tmp_path / "v2.yaml").write_text(yaml.safe_dump(minimal()))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        v2 = load_config(tmp_path / "v2.yaml")
    assert v2.backends["local"].path == tmp_path / "state"
    with pytest.raises(ConfigError, match="unsupported schema_version"):
        (tmp_path / "v9.yaml").write_text("schema_version: 9\n")
        load_config(tmp_path / "v9.yaml")
    with pytest.raises(ConfigError, match="only schema_version 1"):
        upgrade_text(yaml.safe_dump(minimal()))


def test_schema_files_match_the_models():
    assert not schema.drift(ROOT / "docs/config.schema.json", Config), "run core.schema.write_all('docs')"
    assert not schema.drift(ROOT / "docs/config_v1.schema.json", ConfigV1), "run core.schema.write_all('docs')"
    generated = json.loads(schema.render(Config))
    assert generated["properties"]["schema_version"]["const"] == 2
    assert generated["$defs"]["DecisionConfig"]["properties"]["on_failure"]["default"] == "fail_closed"
    assert generated["additionalProperties"] is False


def test_decision_stage_policies_selection_ordering_and_packing_surface():
    config = validate_config(minimal())
    policies = config.pipeline.decision.stage_policies
    assert (policies.rules, policies.classifier, policies.llm) == ("fallback_to_next_stage", "fallback_to_next_stage", "abstain")
    assert config.pipeline.selection.packing.fairness == "fair_share" and config.pipeline.selection.packing.field_priority == []
    data = minimal()
    data["pipeline"]["selection"] = {"ordering": "per_arm"}  # deprecated alias stays loadable
    assert validate_config(data).pipeline.selection.ordering == "per_arm"
    data["pipeline"]["selection"] = {"ordering": "best_arm_rank"}
    with pytest.raises(ConfigError, match="no v1 fusion strategy"):
        downgrade(validate_config(data))
    data["pipeline"]["selection"] = {"packing": {"field_priority": ["Bad-Name"]}}
    with pytest.raises(ConfigError, match="field identifier") as error:
        validate_config(data)
    assert error.value.path == "pipeline.selection.packing.field_priority"
    data["pipeline"]["selection"] = {"ordering": "rrf"}
    assert downgrade(validate_config(data)).fusion.strategy == "rrf"
    assert downgrade(validate_config(minimal())).fusion.strategy == "per_arm"
    data["pipeline"]["decision"] = {"kind": "cascade", "stages": ["rules", "llm"], "stage_policies": {"llm": "fallback_to_next_stage"}}
    data["pipeline"]["selection"] = {}
    data["models"] = {"roles": {"planner": {"provider": "openai_compatible", "model": "m"}}}
    assert validate_config(data).pipeline.decision.stage_policies.llm == "fallback_to_next_stage"
    data["pipeline"]["decision"]["stage_policies"] = {"llm": "raw_all_arms"}
    with pytest.raises(ConfigError):
        validate_config(data)


def test_cli_config_subcommands(tmp_path, capsys, monkeypatch):
    from su_multi_arm.cli import main

    output = tmp_path / "local_v2.yaml"
    monkeypatch.setattr("sys.argv", ["su-multi-arm", "--config", str(ROOT / "examples/local.yaml"), "config", "upgrade", "--output", str(output)])
    main()
    assert json.loads(capsys.readouterr().out)["written"] == str(output)
    monkeypatch.setattr("sys.argv", ["su-multi-arm", "--config", str(output), "config", "validate"])
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == 2 and report["warnings"] == [] and report["decision"] == "rules"
    monkeypatch.setattr("sys.argv", ["su-multi-arm", "--config", str(output), "config", "schema"])
    main()
    assert json.loads(capsys.readouterr().out)["properties"]["schema_version"]["const"] == 2
