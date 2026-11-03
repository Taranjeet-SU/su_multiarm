"""Per-(provider, model) capability registry and the config validation that consults it (plan §3.6)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from su_multi_arm.compat.config_v1 import downgrade, upgrade
from su_multi_arm.compat.config_v1 import Config as ConfigV1
from su_multi_arm.core.config import ChatRole, EmbedderRole, validate_config
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.providers import (
    AnthropicProvider,
    CapabilityMismatch,
    OpenAICompatibleProvider,
    RoleSpec,
    lookup,
    strict_schema,
    supports_native,
)
from su_multi_arm.runtime.budget import PriceTable


def minimal(**extra):
    return {
        "schema_version": 2,
        "dataset": "demo",
        "backends": {"local": {"kind": "sqlite", "path": "./state"}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "pipeline": {"arms": {"dense": {"backend": "local"}, "lexical": {"backend": "local"}}},
        **extra,
    }


def role(**kw):
    base = {"provider": "anthropic", "model": "claude-sonnet-4-5", "api_key_env": "SU_TEST_KEY"}
    return {**base, **kw}


@pytest.mark.parametrize(
    "provider,model,native,kind,caching",
    [
        ("anthropic", "claude-sonnet-4-5", True, "tool_use", "controlled"),
        ("anthropic", "claude-3-5-haiku-latest", True, "tool_use", "controlled"),
        ("anthropic", "claude-2.1", False, None, "controlled"),
        ("openai", "gpt-4o-2024-05-13", False, None, "passive"),  # carved out before gpt-4o*
        ("openai", "gpt-4o-mini", True, "json_schema_strict", "passive"),
        ("openai", "gpt-4.1-mini", True, "json_schema_strict", "passive"),
        ("openai", "o1-mini", False, None, "passive"),
        ("openai", "gpt-3.5-turbo", False, None, "passive"),
        ("openai_compatible", "any-local-model", False, None, "passive"),
    ],
)
def test_registry_lookup_first_match_wins(provider, model, native, kind, caching):
    caps = lookup(provider, model)
    assert caps.native_structured_output is native and caps.native_kind == kind and caps.prompt_caching == caching
    assert supports_native(provider, model) is native


def test_registry_token_and_reasoning_parameters():
    assert lookup("anthropic", "claude-opus-4-1").max_output_tokens_param == "max_tokens"
    assert lookup("openai", "gpt-4o").max_output_tokens_param == "max_completion_tokens"
    assert lookup("openai", "o3-mini").reasoning_effort_param == "reasoning_effort"
    assert lookup("openai", "gpt-4o").reasoning_effort_param is None
    assert lookup("openai_compatible", "m").role_declared
    with pytest.raises(KeyError):
        lookup("nonexistent", "m")


def test_default_structured_output_is_prompt_mode():
    assert ChatRole(provider="openai_compatible", model="m").capabilities.structured_output == "none"


@pytest.mark.parametrize(
    "spec",
    [
        role(capabilities={"structured_output": "native", "prompt_caching": "controlled"}),
        role(provider="openai", model="gpt-4.1-mini", capabilities={"structured_output": "native", "prompt_caching": "passive"}),
        role(provider="openai", model="o3-mini", reasoning_effort="low", capabilities={"structured_output": "native"}),
        role(provider="openai_compatible", model="local", api_key_env=None, capabilities={"structured_output": "json_schema"}),
        role(provider="openai_compatible", model="local", api_key_env=None, capabilities={"structured_output": "json_mode"}),
        role(provider="openai", model="gpt-3.5-turbo", capabilities={"structured_output": "json_mode"}),
    ],
)
def test_declared_pairs_are_accepted(spec):
    config = validate_config(minimal(models={"roles": {"generator": spec}}))
    assert config.models.roles.generator.provider == spec["provider"]


@pytest.mark.parametrize(
    "spec,needle",
    [
        (role(provider="openai_compatible", model="local", capabilities={"structured_output": "native"}), "native is not declared"),
        (role(model="claude-2.1", capabilities={"structured_output": "native"}), "native is not declared"),
        (role(provider="openai", model="gpt-3.5-turbo", capabilities={"structured_output": "native"}), "native is not declared"),
        (role(capabilities={"structured_output": "json_schema"}), "json_schema is not available for provider anthropic"),
        (role(capabilities={"structured_output": "json_mode"}), "json_mode is not available"),
        (role(provider="openai", model="gpt-4o", capabilities={"prompt_caching": "controlled"}), "prompt_caching: controlled is not declared"),
        (role(provider="openai_compatible", model="m", capabilities={"prompt_caching": "controlled"}), "prompt_caching: controlled"),
        (role(provider="openai", model="gpt-4o", reasoning_effort="high"), "reasoning_effort is not declared"),
        (role(reasoning_effort="high"), "reasoning_effort is not declared"),
        (role(provider="openai", model="gpt-4o", max_tokens_parameter="max_tokens"), "takes max_completion_tokens"),
        (role(provider="openai", model="gpt-4o", api_key_env=None), "OpenAI requires api_key_env"),
    ],
)
def test_undeclared_capabilities_fail_validation_with_a_path(spec, needle):
    with pytest.raises(ConfigError) as error:
        validate_config(minimal(models={"roles": {"planner": spec}}))
    assert needle in str(error.value) and "models.roles.planner" in str(error.value)


def test_openai_embeddings_require_a_key_env_and_downgrade_refuses_openai():
    with pytest.raises(ValueError, match="api_key_env"):
        EmbedderRole(provider="openai", model="text-embedding-3-small", dimensions=256)
    v2 = upgrade(ConfigV1(dataset="demo")).config
    v2.models.roles.generator = ChatRole(provider="openai", model="gpt-4o", api_key_env="SU_TEST_KEY")
    with pytest.raises(ConfigError, match="models.roles.generator.provider"):
        downgrade(v2)


def test_provider_construction_rechecks_the_registry():
    """A role object built outside validate_config still cannot claim an undeclared capability."""
    with pytest.raises(CapabilityMismatch, match="native"):
        OpenAICompatibleProvider(RoleSpec(provider="openai_compatible", model="m", structured_output="native"))
    with pytest.raises(CapabilityMismatch, match="json_mode"):
        AnthropicProvider(RoleSpec(provider="anthropic", model="claude-sonnet-4-5", structured_output="json_mode"))
    with pytest.raises(CapabilityMismatch, match="cannot serve"):
        AnthropicProvider(RoleSpec(provider="openai", model="gpt-4o"))


class Inner(BaseModel):
    name: str
    count: int = 0


class Closed(BaseModel):
    title: str  # a field literally named "title" must survive the title-stripping
    items: list[Inner] = []
    note: str | None = None


class Open(BaseModel):
    filters: dict[str, str] = {}


def test_strict_schema_closes_objects_and_requires_every_property():
    strict = strict_schema(Closed.model_json_schema())
    assert strict["additionalProperties"] is False and strict["required"] == ["title", "items", "note"]
    assert "title" in strict["properties"] and "default" not in strict["properties"]["items"]
    inner = strict["$defs"]["Inner"]
    assert inner["additionalProperties"] is False and inner["required"] == ["name", "count"]
    assert strict_schema(Open.model_json_schema()) is None


def test_price_table_accounts_cached_reads_and_cache_writes():
    table = PriceTable(
        version="t1",
        prices={"m": {"input_per_million": 3.0, "output_per_million": 15.0, "cached_input_per_million": 0.3, "cache_write_input_per_million": 3.75}},
    )
    # 1000 total input = 600 uncached + 300 cached + 100 cache-write; 200 output.
    expected = (600 * 3.0 + 300 * 0.3 + 100 * 3.75 + 200 * 15.0) / 1_000_000
    assert table.cost("m", 1000, 200, 300, 100) == pytest.approx(expected)
    plain = PriceTable(version="t2", prices={"m": {"input_per_million": 1.0, "output_per_million": 2.0}})
    assert plain.cost("m", 1000, 0, 300, 100) == pytest.approx(1000 / 1_000_000)
