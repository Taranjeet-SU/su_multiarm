"""Per-(provider, model) capability registry (plan §3.6).

The registry is the single source of truth for what a hosted provider/model pair supports. Config
validation consults it (``check_role``), so ``structured_output: native`` or ``prompt_caching:
controlled`` is accepted only for declared pairs, and the providers consult it again at
construction (defence in depth for objects built outside ``validate_config``). Entries are matched
in order with ``fnmatch`` patterns on the model id; the first match wins, so exclusions precede the
families they carve out of. There is no network probe here (that is ``doctor --probe``).

This module imports nothing from the rest of the package so ``core/config.py`` can import it.

Evidence level: the hosted entries reflect the providers' published API documentation and are
contract-mocked in this repository. 2.0.0a3: the ``claude-haiku-5*`` and ``claude-sonnet-5*``
entries (``output_config.format``, forced tool use with ``$defs``, thinking by default on Haiku 5.5,
prompt-cache token reporting) were also exercised against the real Anthropic API in one small,
spend-capped smoke run (docs/VALIDATION.md); every other hosted entry is documentation-based only.

Output ceilings (2.0.0a3): a model that **thinks by default** spends part of ``max_tokens`` on
thinking before it writes the reply (Anthropic Haiku 5.5 reports the share in
``usage.output_tokens_details.thinking_tokens``). Such entries declare ``min_max_tokens``: a smaller
role ``max_tokens`` is rejected by ``check_role`` (the ``ChatRole`` default of 2048 is above it).
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Literal

StructuredMode = Literal["none", "json_mode", "json_schema", "native"]
CachingMode = Literal["none", "passive", "controlled"]
NativeKind = Literal["tool_use", "output_format", "json_schema_strict"]

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
    # The model thinks by default; thinking tokens count against ``max_tokens``.
    thinks_by_default: bool = False
    min_max_tokens: int | None = None  # a smaller role ``max_tokens`` is rejected

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
            "thinks_by_default": self.thinks_by_default,
            "min_max_tokens": self.min_max_tokens,
        }


_OPENAI_MODES = frozenset({"none", "json_mode", "json_schema"})


# Output ceiling of a model that thinks by default: below ``THINKING_MIN_MAX_TOKENS`` a short
# reply can be truncated by thinking alone (observed: 128 tokens all spent thinking, 2.0.0a3 smoke).
THINKING_MIN_MAX_TOKENS = 1024


def _anthropic(pattern: str, native: bool, kind: NativeKind = "tool_use", thinks: bool = False) -> ModelCapabilities:
    return ModelCapabilities(
        provider="anthropic",
        pattern=pattern,
        native_structured_output=native,
        native_kind=kind if native else None,
        request_modes=frozenset({"none"}),  # Messages API has no response_format / JSON mode
        prompt_caching="controlled",  # cache_control on system and tool blocks
        max_output_tokens_param="max_tokens",
        thinks_by_default=thinks,
        min_max_tokens=THINKING_MIN_MAX_TOKENS if thinks else None,
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
    # Anthropic: native structured output = forced tool use with the schema as ``input_schema``
    # (Claude 3 and later), or ``output_config.format`` (a JSON-schema-constrained text reply) on
    # the families verified against the real API (Haiku/Sonnet 5.x, 2.0.0a3). Haiku 5.x thinks by
    # default. Older/unknown ids fall back to prompt mode only.
    _anthropic("claude-3*", True),
    _anthropic("claude-sonnet-4*", True),
    _anthropic("claude-opus-4*", True),
    _anthropic("claude-haiku-4*", True),
    _anthropic("claude-sonnet-5*", True, "output_format"),
    _anthropic("claude-opus-5*", True),
    _anthropic("claude-haiku-5*", True, "output_format", thinks=True),
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
    max_tokens: int | None = None,
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
    if max_tokens is not None and caps.min_max_tokens is not None and max_tokens < caps.min_max_tokens:
        problems.append(
            (
                "max_tokens",
                f"({provider}, {model}) thinks by default and thinking tokens count against max_tokens; "
                f"max_tokens {max_tokens} is below the minimum {caps.min_max_tokens}",
            )
        )
    return problems


def default_structured_output(provider: str, model: str) -> str:
    """The ``structured_output`` a chat role gets when it declares none (2.0.0a3): ``native`` for
    Anthropic models the registry declares capable, ``none`` otherwise."""
    if provider == "anthropic" and supports_native(provider, model):
        return "native"
    return "none"



def min_max_tokens(provider: str, model: str) -> int | None:
    """Smallest ``max_tokens`` the registry accepts for ``(provider, model)``; ``None`` when unbounded."""
    try:
        return lookup(provider, model).min_max_tokens
    except KeyError:
        return None
