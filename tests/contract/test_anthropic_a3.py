"""2.0.0a3 Anthropic contracts (mocked HTTP): native structured output by default for registry-capable
models, ``output_config.format`` request/reply shape, thinking-by-default models and their minimum
``max_tokens``, thinking-token usage, truncation detail. The reply shapes match what the real API
returned in the 2.0.0a3 smoke (docs/VALIDATION.md); no network here.
"""

from __future__ import annotations

import json
import warnings

import httpx
import pytest
import yaml
from pydantic import BaseModel, Field, ValidationError

from su_multi_arm.compat.config_v1 import upgrade_mapping
from su_multi_arm.core.config import ChatRole, validate_config
from su_multi_arm.providers import (
    AnthropicProvider,
    CallBudget,
    CallContext,
    ProviderIncomplete,
    SchemaViolation,
    StrictSchemaError,
    output_format_schema,
)
from su_multi_arm.providers.capabilities import check_role, lookup
from su_multi_arm.pipeline.validate import strict_schema_problems

KEY = "SU_TEST_PROVIDER_KEY"


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv(KEY, "sk-test-not-a-real-key")


class Step(BaseModel):
    arm: str
    weight: int = Field(default=1, ge=0, le=9)


class Plan(BaseModel):
    steps: list[Step] = Field(default_factory=list, max_length=3)
    note: str | None = None


def role(model: str = "claude-haiku-5-5", **kw) -> ChatRole:
    return ChatRole(provider="anthropic", model=model, api_key_env=KEY, **kw)


class Recorder:
    def __init__(self, reply: dict):
        self.reply, self.bodies = reply, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(200, json=self.reply)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def message(content: list, stop: str = "end_turn", thinking_tokens: int = 0, output_tokens: int = 30) -> dict:
    usage = {"input_tokens": 300, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": output_tokens, "output_tokens_details": {"thinking_tokens": thinking_tokens}}
    return {"model": "claude-haiku-5-5", "type": "message", "role": "assistant", "content": content, "stop_reason": stop, "stop_details": None, "usage": usage}


# --- defaults --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model,mode,kind",
    [("claude-haiku-5-5", "native", "output_format"), ("claude-sonnet-5-5", "native", "output_format"), ("claude-sonnet-4-5", "native", "tool_use"), ("claude-2.1", "none", None)],
)
def test_anthropic_roles_default_to_native_where_the_registry_declares_it(model, mode, kind):
    spec = role(model, max_tokens=2048)
    assert spec.capabilities.structured_output == mode
    assert AnthropicProvider(spec).structured_mode() == (f"native:{kind}" if kind else "prompt")


def test_explicit_none_is_kept_and_round_trips_through_a_dump():
    spec = role(capabilities={"structured_output": "none"})
    assert spec.capabilities.structured_output == "none"
    again = ChatRole.model_validate(spec.model_dump(mode="json", exclude_defaults=True))
    assert again.capabilities.structured_output == "none"
    default = ChatRole.model_validate(role().model_dump(mode="json", exclude_defaults=True))
    assert default.capabilities.structured_output == "native"


def test_other_providers_keep_none_by_default():
    assert ChatRole(provider="openai", model="gpt-4.1-mini", api_key_env=KEY).capabilities.structured_output == "none"
    assert ChatRole(provider="openai_compatible", model="local").capabilities.structured_output == "none"


# --- thinking-by-default models -------------------------------------------------------------------


def test_too_small_max_tokens_for_a_thinking_model_is_rejected():
    assert lookup("anthropic", "claude-haiku-5-5").thinks_by_default and not lookup("anthropic", "claude-sonnet-5-5").thinks_by_default
    with pytest.raises(ValidationError, match="thinks by default.*below the minimum 1024"):
        role(max_tokens=512)
    assert role(max_tokens=1024).max_tokens == 1024 and role().max_tokens == 2048
    assert ChatRole(provider="anthropic", model="claude-sonnet-5-5", api_key_env=KEY, max_tokens=512).max_tokens == 512
    assert check_role("anthropic", "claude-haiku-5-5", "native", "none", max_tokens=256)[0][0] == "max_tokens"


def test_a_v1_config_below_the_minimum_is_raised_with_a_warning():
    data = yaml.safe_load(
        """
dataset: demo
storage_path: ./s
generation:
  model: {provider: anthropic, model: claude-haiku-5-5, api_key_env: ANTHROPIC_API_KEY, max_tokens: 512}
"""
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = upgrade_mapping(data)
    assert result.config.models.roles.generator.max_tokens == 1024
    assert [d.code for d in result.diagnostics if d.code == "max_tokens_raised"] == ["max_tokens_raised"]
    with pytest.raises(Exception, match="below the minimum 1024"):
        upgrade_mapping(data, strict=True)


def test_thinking_tokens_are_reported_and_named_on_truncation():
    rec = Recorder(message([{"type": "thinking", "thinking": ""}], stop="max_tokens", thinking_tokens=1024, output_tokens=1024))
    budget = CallBudget(1)
    with pytest.raises(ProviderIncomplete, match="truncated at max_tokens 1024 \\(1024 of 1024 output tokens were thinking\\)"):
        AnthropicProvider(role(max_tokens=1024), transport=rec.transport).complete_text("S", "q", budget, CallContext("generator"))
    assert budget.calls[0]["usage"]["reasoning_tokens"] == 1024 and budget.calls[0]["stop_reason"] == "max_tokens"


# --- output_config.format -------------------------------------------------------------------------


def test_output_format_request_and_reply():
    reply = message([{"type": "thinking", "thinking": ""}, {"type": "text", "text": json.dumps({"steps": [{"arm": "dense", "weight": 2}]})}], thinking_tokens=12)
    rec = Recorder(reply)
    plan, usage = AnthropicProvider(role(), transport=rec.transport).complete_json("SYS", {"q": "x"}, Plan, CallBudget(1), CallContext("planner"))
    assert plan == Plan(steps=[Step(arm="dense", weight=2)]) and usage.reasoning_tokens == 12
    body = rec.bodies[0]
    assert "tools" not in body and "tool_choice" not in body and body["system"] == "SYS"  # no schema in the prompt
    schema = body["output_config"]["format"]["schema"]
    assert body["output_config"]["format"]["type"] == "json_schema"
    text = json.dumps(schema)
    assert "minimum" not in text and "maximum" not in text and "maxItems" not in text  # refused by the API
    assert schema["additionalProperties"] is False and schema["$defs"]["Step"]["additionalProperties"] is False


def test_output_format_reply_is_validated_with_the_full_model():
    rec = Recorder(message([{"type": "text", "text": json.dumps({"steps": [{"arm": "dense", "weight": 42}]})}]))
    with pytest.raises(SchemaViolation):
        AnthropicProvider(role(), transport=rec.transport).complete_json("S", {}, Plan, CallBudget(1), CallContext("planner"))


def test_output_format_non_final_stop_is_incomplete():
    rec = Recorder(message([{"type": "text", "text": "{}"}], stop="pause_turn"))
    with pytest.raises(ProviderIncomplete):
        AnthropicProvider(role(), transport=rec.transport).complete_json("S", {}, Plan, CallBudget(1), CallContext("planner"))


def test_output_format_refuses_free_form_mappings_before_any_call():
    class Loose(BaseModel):
        filters: dict[str, str] = Field(default_factory=dict)

    with pytest.raises(StrictSchemaError, match="free-form mapping.*structured_output: none"):
        output_format_schema(Loose.model_json_schema(), "Loose")
    rec = Recorder(message([]))
    with pytest.raises(StrictSchemaError):
        AnthropicProvider(role(), transport=rec.transport).complete_json("S", {}, Loose, CallBudget(1), CallContext("planner"))
    assert rec.bodies == []


def test_every_package_schema_converts_for_output_format():
    for name in ("planner", "generator", "reviewer", "extractor", "community"):
        assert strict_schema_problems("anthropic", "claude-haiku-5-5", "native", name) == []


def test_stop_details_are_recorded():
    reply = message([{"type": "text", "text": "ok"}])
    reply["stop_details"] = {"type": "end_turn"}
    budget = CallBudget(1)
    AnthropicProvider(role(), transport=Recorder(reply).transport).complete_text("S", "q", budget, CallContext("generator"))
    assert budget.calls[0]["stop_details"] == {"type": "end_turn"}


def test_hosted_example_still_validates():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    config = validate_config(yaml.safe_load((root / "examples/v2/hosted_models.yaml").read_text()))
    assert config.models.roles.reviewer.capabilities.structured_output == "native"
