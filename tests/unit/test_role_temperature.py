"""Sampling temperature on a chat role: sent only when set (so unset requests are unchanged), range-checked, and refused where it cannot
apply (the anthropic provider, or together with reasoning_effort). Synthetic data only."""

from __future__ import annotations

import pytest

from su_multi_arm.core.config import ChatRole
from su_multi_arm.providers.openai_compatible import OpenAICompatibleProvider


def body_of(**role):
    provider = OpenAICompatibleProvider(ChatRole(provider="openai_compatible", model="synthetic", **role))
    return provider._request("system", "user", None, "none")[2]


def test_temperature_is_sent_when_set_including_zero():
    assert body_of(temperature=0)["temperature"] == 0
    assert body_of(temperature=0.7)["temperature"] == 0.7


def test_an_unset_temperature_changes_nothing_in_the_request():
    assert "temperature" not in body_of()


@pytest.mark.parametrize("bad", [-0.1, 2.1])
def test_temperature_is_range_checked(bad):
    with pytest.raises(ValueError):
        ChatRole(provider="openai_compatible", model="synthetic", temperature=bad)


def test_temperature_is_refused_for_the_anthropic_provider():
    with pytest.raises(ValueError, match="temperature is not supported"):
        ChatRole(provider="anthropic", model="claude-haiku-4-5", api_key_env="K", temperature=0)


def test_temperature_is_refused_together_with_reasoning_effort():
    with pytest.raises(ValueError, match="reasoning_effort"):
        ChatRole(provider="openai", model="gpt-5-mini", api_key_env="K", reasoning_effort="low", temperature=0)
