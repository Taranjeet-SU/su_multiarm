"""Pipeline validation at load (plan §3.3; Phase 1 "pipeline validation", "storage selectors"):
named pipelines and their overlays, per-request allowlists, capability/registry/budget checks with
every problem reported together, and the exact_id identifier sources."""

from __future__ import annotations

import warnings

import pytest

from su_multi_arm.core.config import Config, validate_config
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.core.types import FieldSpec, MetadataSchema
from su_multi_arm.pipeline.spec import RequestRejected, choose
from su_multi_arm.pipeline.validate import decision_for_mode

FAKE = {"provider": "openai_compatible", "model": "fake"}


def minimal(**overrides):
    data = {
        "schema_version": 2,
        "backends": {"local": {"kind": "sqlite"}, "mem": {"kind": "memory"}, "search": {"kind": "opensearch", "hosts_env": "X"}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "pipeline": {"arms": {"dense": {"backend": "local"}, "lexical": {"backend": "local"}}},
    }
    data.update(overrides)
    return data


def errors_of(data) -> ConfigError:
    with pytest.raises(ConfigError) as info:
        validate_config(data)
    return info.value


def test_named_pipelines_overlay_the_default_and_round_trip():
    data = minimal(pipelines={"narrow": {"arms": {"dense": None}, "selection": {"per_arm": 5}}})
    config = validate_config(data)
    assert list(config.all_pipelines()) == ["default", "narrow"]
    narrow = config.pipelines["narrow"]
    assert narrow.arms.dense is None and narrow.arms.lexical.backend == "local" and narrow.selection.per_arm == 5
    assert config.pipeline.arms.dense is not None  # the default pipeline is untouched
    again = Config.model_validate(config.model_dump(mode="json"))
    assert again.pipelines == config.pipelines and again.fingerprint() == config.fingerprint()


def test_profiles_overlay_pipelines(monkeypatch):
    import su_multi_arm.profiles as profiles

    overlay = {"pipelines": {"from_profile": {"selection": {"per_arm": 3}}}, "request": {"pipelines": ["from_profile"]}}
    monkeypatch.setattr(profiles, "load_profile", lambda name: overlay)
    config = validate_config(minimal())
    assert config.pipelines["from_profile"].selection.per_arm == 3 and config.request.pipelines == ["from_profile"]
    explicit = validate_config(minimal(pipelines={"from_profile": {"selection": {"per_arm": 7}}}))
    assert explicit.pipelines["from_profile"].selection.per_arm == 7  # explicit config wins over the overlay


def test_validation_aggregates_every_problem_with_paths():
    data = minimal(
        pipeline={"arms": {"dense": {"backend": "nope"}, "lexical": {"backend": "mem"}}, "guards": ["safety_refusal"], "review": {"enabled": True}},
        pipelines={"other": {"arms": {"graph": {"backend": "search"}}}},
        tools={"enabled": ["search", "shell"]},
        actions={"allow": ["drop_tables"]},
        default_pipeline="missing",
    )
    error = errors_of(data)
    paths = [d.path for d in error.diagnostics]
    assert len(error.diagnostics) >= 7 and str(error).startswith(f"{paths[0]}: ") and "configuration errors" in str(error)
    for expected in (
        "pipeline.arms.dense.backend",
        "pipeline.review",
        "pipeline.guards",
        "pipelines.other.arms.graph.backend",
        "pipelines.other.arms.graph",
        "tools.enabled",
        "actions.allow",
        "default_pipeline",
    ):
        assert expected in paths, expected
    messages = " | ".join(d.message for d in error.diagnostics)
    assert "unknown backend 'nope'" in messages and "does not declare capability 'graph'" in messages and "'shell' is not a built-in tool" in messages


def test_a_single_problem_keeps_the_v1_style_message_and_path():
    error = errors_of(minimal(pipeline={"arms": {"lexical": {"backend": "nope"}}}))
    assert error.path == "pipeline.arms.lexical.backend" and str(error).startswith("pipeline.arms.lexical.backend: unknown backend")
    assert len(error.diagnostics) == 1


def test_named_pipelines_only_use_projections_the_write_path_builds():
    mismatch = errors_of(minimal(pipelines={"remote": {"arms": {"lexical": {"backend": "mem"}}}}))
    assert mismatch.diagnostics[0].code == "projection_mismatch" and mismatch.path == "pipelines.remote.arms.lexical.backend"
    missing = errors_of(minimal(pipeline={"arms": {"lexical": {"backend": "local"}}}, pipelines={"dense": {"arms": {"dense": {"backend": "local"}}}}))
    assert missing.diagnostics[0].code == "projection_missing"
    with pytest.warns(UserWarning, match="no filterable fields"):  # records are not a projection
        validate_config(minimal(pipelines={"structured": {"arms": {"structured": {"backend": "mem"}}}}))


def test_request_allowlists_name_existing_pipelines_and_buildable_modes():
    assert errors_of(minimal(request={"pipelines": ["ghost"]})).diagnostics[0].code == "unknown_pipeline"
    no_planner = errors_of(minimal(request={"modes": ["llm"]}))
    assert no_planner.path == "request.modes" and "planner" in str(no_planner)
    assert errors_of(minimal(request={"modes": ["cascade"]})).diagnostics[0].code == "mode_unbuildable"
    assert errors_of(minimal(request={"modes": ["classifier"]})).diagnostics[0].code == "mode_unbuildable"
    ok = validate_config(minimal(request={"modes": ["llm", "rules"]}, models={"roles": {"planner": FAKE}}))
    assert ok.request.modes == ["llm", "rules"]


def test_decision_for_mode_strips_what_the_mode_does_not_use():
    from su_multi_arm.core.config import DecisionConfig

    cascade = DecisionConfig(kind="cascade", stages=["rules", "llm"])
    assert decision_for_mode(cascade, "cascade") is cascade
    rules = decision_for_mode(cascade, "rules")
    assert rules.kind == "rules" and rules.stages == [] and DecisionConfig.model_validate(rules.model_dump()) == rules
    with pytest.raises(ValueError, match="cascade"):
        decision_for_mode(DecisionConfig(kind="rules"), "cascade")


def test_choose_enforces_the_allowlists():
    config = validate_config(minimal(pipelines={"a": {}, "b": {}}, request={"pipelines": ["a"]}))

    class Runtime:
        def __init__(self, kind):
            self.default_mode, self.decisions, self.config = kind, {kind: object()}, config.pipeline

    runtimes = {name: Runtime("rules") for name in ("default", "a", "b")}
    assert choose(config, runtimes, None, None)[0] is runtimes["default"]
    assert choose(config, runtimes, "a", "rules")[1] == "rules"
    with pytest.raises(RequestRejected):
        choose(config, runtimes, "b", None)
    with pytest.raises(RequestRejected):
        choose(config, runtimes, "a", "llm")


def test_budgets_must_cover_one_pass_and_review_bounds_warn():
    roles = {"roles": {"planner": FAKE, "generator": FAKE, "reviewer": FAKE}}
    tight = errors_of(minimal(models=roles, pipeline={"arms": {"lexical": {"backend": "local"}}, "decision": {"kind": "llm"}}, budgets={"max_llm_calls": 1}))
    assert tight.path == "budgets.max_llm_calls" and tight.diagnostics[0].code == "budget_incoherent"
    session = errors_of(minimal(models=roles, budgets={"session": {"max_llm_calls": 0 + 1}}, pipeline={"arms": {"lexical": {"backend": "local"}}, "decision": {"kind": "llm"}}))
    assert session.path == "budgets.session.max_llm_calls"
    with pytest.warns(UserWarning, match="budget_exhausted"):
        validate_config(minimal(models=roles, pipeline={"arms": {"lexical": {"backend": "local"}}, "decision": {"kind": "llm"}, "review": {"enabled": True, "max_hops": 5}}, budgets={"max_llm_calls": 6}))
    with pytest.raises(ConfigError, match="price_table"):
        validate_config(minimal(budgets={"session": {"max_usd": 1.0}}))


def test_actions_and_tools_reference_registered_components_only():
    assert errors_of(minimal(serving={"actions": {"allow": ["invalidate_cache"]}})).path == "serving.actions.allow"
    config = validate_config(minimal(actions={"allow": ["invalidate_cache"]}, serving={"actions": {"allow": ["invalidate_cache"]}}))
    assert config.actions.allow == ["invalidate_cache"] and config.tools.enabled == ["search", "get_document", "list_values", "aggregate"]
    with pytest.raises(ConfigError, match="identifier"):
        validate_config(minimal(tools={"enabled": ["pkg.module:factory"]}))
    with pytest.raises(ConfigError, match="reserved"):
        validate_config(minimal(pipelines={"default": {}}))


def test_exact_id_needs_an_identifier_source_and_valid_alias_fields(tmp_path):
    no_source = errors_of(minimal(pipeline={"arms": {"exact_id": {}}}))
    assert no_source.path == "pipeline.arms.exact_id" and no_source.diagnostics[0].code == "no_identifier_source"
    schema = {"fields": [{"name": "ref", "type": "id", "pattern": r"\bR-\d+\b"}, {"name": "note", "type": "string", "filterable": False}]}
    ok = validate_config(minimal(metadata_schema=schema, pipeline={"arms": {"exact_id": {"alias_fields": ["ref"]}}}))
    assert ok.pipeline.arms.exact_id.alias_fields == ["ref"]
    bad = errors_of(minimal(metadata_schema=schema, pipeline={"arms": {"exact_id": {"alias_fields": ["note", "ghost"]}}}))
    assert [d.code for d in bad.diagnostics] == ["alias_field_invalid", "alias_field_invalid"]


def test_id_field_patterns_are_validated():
    assert FieldSpec(name="ref", type="id", pattern=r"\bR-\d+\b").pattern
    for kwargs, match in (
        ({"type": "string", "pattern": "x"}, "only valid for type id"),
        ({"type": "id", "pattern": "("}, "does not compile"),
        ({"type": "id", "pattern": "a*"}, "empty string"),
    ):
        with pytest.raises(ValueError, match=match):
            FieldSpec(name="ref", **kwargs)
    assert MetadataSchema(fields=[FieldSpec(name="ref", type="id")]).fields[0].pattern is None


def test_structured_without_filterable_fields_still_only_warns():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        validate_config(minimal(pipeline={"arms": {"structured": {"backend": "local"}}}))
    assert any("no filterable fields" in str(w.message) for w in caught)


@pytest.mark.parametrize("where", ["pipeline", "pipelines"])
def test_configured_guards_are_rejected_until_the_guard_runtime_exists(where):
    """Release review B3: a guard listed as a plugin would validate and then never run. Until the
    Phase 2 guard runtime ships, any non-empty guards list is a ConfigError naming the path."""
    if where == "pipeline":
        data = minimal(plugins=["acme_guard"], pipeline={"arms": {"lexical": {"backend": "local"}}, "guards": ["acme_guard"]})
        path = "pipeline.guards"
    else:
        data = minimal(plugins=["acme_guard"], pipelines={"guarded": {"guards": ["acme_guard"]}})
        path = "pipelines.guarded.guards"
    error = errors_of(data)
    hits = [d for d in error.diagnostics if d.path == path and d.code == "not_executed"]
    assert hits and "guards are Phase 2" in hits[0].message and "not executed" in hits[0].message
    assert validate_config(minimal(pipeline={"arms": {"lexical": {"backend": "local"}}, "guards": []}))  # empty stays valid


def test_opensearch_cannot_be_the_authoritative_catalog_at_validation():
    """Release review N3: the refusal the engine makes at construction (no generation registry) is
    made by validation, with the path, consistently with every other storage capability check."""
    data = minimal(storage={**minimal()["storage"], "catalog": "search"})
    error = errors_of(data)
    hit = next(d for d in error.diagnostics if d.path == "storage.catalog")
    assert "registry" in hit.message and "authoritative catalog" in hit.message
    from su_multi_arm.backends import capabilities as caps

    for kind in ("memory", "sqlite", "postgres", "sqlalchemy", "elasticsearch", "mongodb"):
        assert caps.serves(kind, "catalog", "registry"), kind
    assert not caps.serves("opensearch", "catalog", "registry")
