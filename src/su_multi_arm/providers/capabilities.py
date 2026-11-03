"""Per-(provider, model) capability registry (plan §3.6).

The registry is the single source of truth for what a hosted provider/model pair supports. Config
validation consults it (``check_role``), so ``structured_output: native`` or ``prompt_caching:
controlled`` is accepted only for declared pairs, and the providers consult it again at
construction (defence in depth for objects built outside ``validate_config``). Entries are matched
in order with ``fnmatch`` patterns on the model id; the first match wins, so exclusions precede the
families they carve out of. There is no network probe here (that is ``doctor --probe``).

This module imports nothing from the rest of the package so ``core/config.py`` can import it.

Evidence level: the hosted entries reflect the providers' published API documentation and are
**contract-mocked only** in this repository (no paid call has been made, assumed default #14).
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Literal

StructuredMode = Literal["none", "json_mode", "json_schema", "native"]
CachingMode = Literal["none", "passive", "controlled"]
NativeKind = Literal["tool_use", "json_schema_strict"]

CHAT_PROVIDERS: tuple[str, ...] = ("anthropic", "openai", "openai_compatible")
CACHING_RANK = {"none": 0, "passive": 1, "controlled": 2}


@dataclass(frozen=True)
class ModelCapabilities:
    provider: str
    pattern: str
    native_structured_output: bool = False
    native_kind: NativeKind | None = None  # how "native" is realised on the wire
    # Wire-level request hints the provider accepts (``response_format`` json_object / json_schema).
    request_modes: frozenset[str] = frozenset({"none"})
    prompt_caching: CachingMode = "none"
    max_output_tokens_param: str = "max_tokens"
    reasoning_effort_param: str | None = None
    # ``True`` when the role's own declaration is authoritative (servers differ; plan §3.6).
    role_declared: bool = False
    # Server-sent-event streaming of the reply (``complete_*_stream``). Every HTTP chat provider
    # here streams on the wire; a role still has to declare ``capabilities.streaming: true`` before
    # the engine streams it (``openai_compatible`` servers differ).
    streaming: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "pattern": self.pattern,
            "native_structured_output": self.native_structured_output,
            "native_kind": self.native_kind,
            "request_modes": sorted(self.request_modes),
            "prompt_caching": self.prompt_caching,
            "max_output_tokens_param": self.max_output_tokens_param,
            "reasoning_effort_param": self.reasoning_effort_param,
            "role_declared": self.role_declared,
            "streaming": self.streaming,
        }


_OPENAI_MODES = frozenset({"none", "json_mode", "json_schema"})


def _anthropic(pattern: str, native: bool) -> ModelCapabilities:
    return ModelCapabilities(
        provider="anthropic",
        pattern=pattern,
        native_structured_output=native,
        native_kind="tool_use" if native else None,
        request_modes=frozenset({"none"}),  # Messages API has no response_format / JSON mode
        prompt_caching="controlled",  # cache_control on system and tool blocks
        max_output_tokens_param="max_tokens",
    )


def _openai(pattern: str, native: bool, reasoning: bool = False) -> ModelCapabilities:
    return ModelCapabilities(
        provider="openai",
        pattern=pattern,
        native_structured_output=native,
        native_kind="json_schema_strict" if native else None,
        request_modes=_OPENAI_MODES,
        prompt_caching="passive",  # automatic prefix caching; reported from usage, not controlled
        max_output_tokens_param="max_completion_tokens",
        reasoning_effort_param="reasoning_effort" if reasoning else None,
    )


# Order matters: first match wins.
REGISTRY: tuple[ModelCapabilities, ...] = (
    # Anthropic: native structured output = forced tool use with the schema as ``input_schema``,
    # available on Claude 3 and later families. Older/unknown ids fall back to prompt mode only.
    _anthropic("claude-3*", True),
    _anthropic("claude-sonnet-4*", True),
    _anthropic("claude-opus-4*", True),
    _anthropic("claude-haiku-4*", True),
    _anthropic("claude-sonnet-5*", True),
    _anthropic("claude-opus-5*", True),
    _anthropic("claude-haiku-5*", True),
    _anthropic("*", False),
    # OpenAI: native = ``response_format: json_schema`` with ``strict: true``. Snapshots that
    # predate structured outputs are carved out before their family.
    _openai("gpt-4o-2024-05-13", False),
    _openai("gpt-4o*", True),
    _openai("gpt-4.1*", True),
    _openai("gpt-5*", True, reasoning=True),
    _openai("o1-mini*", False, reasoning=True),
    _openai("o1-preview*", False, reasoning=True),
    _openai("o1*", True, reasoning=True),
    _openai("o3*", True, reasoning=True),
    _openai("o4-mini*", True, reasoning=True),
    _openai("*", False),
    # OpenAI-compatible servers (vLLM, Ollama's /v1, llama.cpp server, LM Studio) differ, so the
    # role's declaration decides which request hint is sent; nothing is claimed as native.
    ModelCapabilities(
        provider="openai_compatible",
        pattern="*",
        request_modes=_OPENAI_MODES,
        prompt_caching="passive",
        max_output_tokens_param="max_completion_tokens",
        role_declared=True,
    ),
)


def lookup(provider: str, model: str, registry: tuple[ModelCapabilities, ...] = REGISTRY) -> ModelCapabilities:
    for entry in registry:
        if entry.provider == provider and fnmatchcase(model, entry.pattern):
            return entry
    raise KeyError(f"no capability entry for provider {provider!r}")


def supports_native(provider: str, model: str) -> bool:
    try:
        return lookup(provider, model).native_structured_output
    except KeyError:
        return False


def check_role(
    provider: str,
    model: str,
    structured_output: str,
    prompt_caching: str,
    reasoning_effort: str | None = None,
    max_tokens_parameter: str | None = None,
) -> list[tuple[str, str]]:
    """Static checks of a chat role against the registry: ``[(key, message), ...]``, empty when valid."""
    try:
        caps = lookup(provider, model)
    except KeyError as error:
        return [("provider", str(error))]
    problems: list[tuple[str, str]] = []
    if structured_output == "native":
        if not caps.native_structured_output:
            problems.append(
                (
                    "capabilities.structured_output",
                    f"structured_output: native is not declared for ({provider}, {model}); "
                    + ("use json_schema or json_mode" if caps.request_modes - {"none"} else "use none (schema in prompt, strict validation)"),
                )
            )
    elif structured_output not in caps.request_modes:
        problems.append(
            (
                "capabilities.structured_output",
                f"structured_output: {structured_output} is not available for provider {provider} "
                f"(available: {', '.join(sorted(caps.request_modes | ({'native'} if caps.native_structured_output else set())))})",
            )
        )
    if CACHING_RANK[prompt_caching] > CACHING_RANK[caps.prompt_caching]:
        problems.append(
            (
                "capabilities.prompt_caching",
                f"prompt_caching: {prompt_caching} is not declared for ({provider}, {model}); the registry declares {caps.prompt_caching}",
            )
        )
    if reasoning_effort is not None and caps.reasoning_effort_param is None and not caps.role_declared:
        problems.append(("reasoning_effort", f"reasoning_effort is not declared for ({provider}, {model})"))
    if (
        provider == "openai"
        and max_tokens_parameter is not None
        and max_tokens_parameter != caps.max_output_tokens_param
    ):
        problems.append(
            ("max_tokens_parameter", f"({provider}, {model}) takes {caps.max_output_tokens_param}, not {max_tokens_parameter}")
        )
    return problems
