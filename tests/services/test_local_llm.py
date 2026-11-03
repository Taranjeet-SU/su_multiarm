"""Optional smoke against an already running local OpenAI-compatible server (plan §3.6, §6.1).

Runs only when ``SU_TEST_LOCAL_LLM_URL`` (e.g. ``http://127.0.0.1:11434/v1``) is set; it never
starts, stops or configures a model server. ``SU_TEST_LOCAL_LLM_MODEL`` names the served model
(required when the URL is set); ``SU_TEST_LOCAL_LLM_STRUCTURED`` is the declared
``structured_output`` (``none`` by default, or ``json_mode``/``json_schema`` if the server supports
it); ``SU_TEST_LOCAL_LLM_KEY_ENV`` optionally names the variable holding a key. No paid provider.
"""

from __future__ import annotations

import os

import pytest
from pydantic import BaseModel, Field

from su_multi_arm.core.config import ChatRole
from su_multi_arm.providers import CallBudget, JsonModel

URL = os.environ.get("SU_TEST_LOCAL_LLM_URL")

pytestmark = [
    pytest.mark.external,
    pytest.mark.skipif(not URL, reason="SU_TEST_LOCAL_LLM_URL is not set; no local model server is used"),
]


class Colour(BaseModel):
    colour: str = Field(min_length=1, max_length=40)


def test_local_openai_compatible_structured_call_records_usage():
    model_id = os.environ.get("SU_TEST_LOCAL_LLM_MODEL")
    assert model_id, "SU_TEST_LOCAL_LLM_MODEL must name the served model when SU_TEST_LOCAL_LLM_URL is set"
    role = ChatRole(
        provider="openai_compatible",
        model=model_id,
        base_url=URL,
        api_key_env=os.environ.get("SU_TEST_LOCAL_LLM_KEY_ENV") or None,
        max_tokens=256,
        timeout_seconds=120,
        capabilities={"structured_output": os.environ.get("SU_TEST_LOCAL_LLM_STRUCTURED", "none")},
    )
    budget = CallBudget(1)
    reply = JsonModel(role).call(
        "Answer with the colour named in the user's message.", {"message": "The synthetic widget is blue."}, Colour, budget, "smoke"
    )
    assert reply.colour.strip()
    (record,) = budget.calls
    assert record["success"] and record["stage"] == "smoke" and record["provider"] == "openai_compatible"
