"""OpenAI-compatible local servers (vLLM, Ollama's ``/v1``, llama.cpp server, LM Studio).

Same wire shape as ``openai``, but servers differ, so the role's declaration is authoritative
(plan §3.6): ``response_format`` json_schema (``strict: false``) only under ``structured_output:
json_schema``, ``json_object`` only under ``json_mode``, and nothing under ``none`` (the default:
schema in the system prompt, strict validation). ``native`` is never declared for this provider.
The output-limit parameter name is the role's ``max_tokens_parameter``; ``Authorization`` is sent
only when ``api_key_env`` is set. Cached tokens are recorded when the server reports
``prompt_tokens_details.cached_tokens``; a server that reports no usage is recorded as
``usage.reported: false`` and the budget keeps the reservation estimate.

Evidence level: contract-mocked; one optional ``external`` test runs against a local server when
``SU_TEST_LOCAL_LLM_URL`` is set.
"""

from __future__ import annotations

from typing import ClassVar

from .openai import OpenAIProvider


class OpenAICompatibleProvider(OpenAIProvider):
    name: ClassVar[str] = "openai_compatible"
    default_base_url: ClassVar[str] = "https://api.openai.com/v1"  # v1 default; set base_url for a local server

    def _max_tokens_param(self) -> str:
        return self.role.max_tokens_parameter

    def _reasoning_param(self) -> str | None:
        return "reasoning_effort"
