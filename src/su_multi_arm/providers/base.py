"""Provider kernel: typed errors, usage, call metering, the ``ChatProvider`` protocol, HTTP helpers.

Strict behaviour (plan §3.6): no automatic retries, no repair calls, no silent downgrade from native
to prompt mode. Every failure is a typed ``ProviderError`` (``RuntimeError``) carrying a
``category``; a reply that does not satisfy the schema is a ``SchemaViolation`` (``ValueError``)
carrying the raw text so callers can keep it for replay. The caller's fail policy decides what
happens next. Provider response bodies are never echoed into error messages (they can contain
source text or secrets). API keys are read only from the environment variable the role names.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, TypeVar, runtime_checkable

import httpx
from pydantic import BaseModel, ValidationError

from .capabilities import ModelCapabilities

T = TypeVar("T", bound=BaseModel)


def tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


# --- errors ------------------------------------------------------------------------------------


class ProviderError(RuntimeError):
    """A provider call failed. ``category`` is stable and machine-readable."""

    category = "provider_error"

    def __init__(self, message: str, *, stage: str | None = None, status: int | None = None):
        self.stage, self.status = stage, status
        super().__init__(f"{stage}: {message}" if stage else message)


class ProviderAuthError(ProviderError):
    category = "auth"


class ProviderRateLimited(ProviderError):
    category = "rate_limited"

    def __init__(self, message: str, *, stage: str | None = None, status: int | None = None, retry_after: str | None = None):
        self.retry_after = retry_after  # recorded only; the package never retries
        super().__init__(message, stage=stage, status=status)


class ProviderServerError(ProviderError):
    category = "server_error"


class ProviderRequestError(ProviderError):
    """4xx other than auth/rate limit, e.g. a schema the server refuses in strict mode."""

    category = "bad_request"


class ProviderSchemaTooComplex(ProviderRequestError):
    """HTTP 400 because the server cannot compile the reply schema into a grammar (Anthropic
    ``output_config.format``: "compiled grammar is too large" / "schema is too complex"). The
    request was refused before generation. The role should use prompt mode for this schema
    (``structured_output: none``); see ``output_format_complexity``."""

    category = "schema_too_complex"


# Fragments of a 400 body that identify a grammar the server could not compile. Matched
# case-insensitively; the body itself is never echoed into the error.
SCHEMA_TOO_COMPLEX_MARKERS: tuple[str, ...] = ("grammar is too large", "schema is too complex", "too complex for compilation")


class ProviderTimeout(ProviderError):
    category = "timeout"


class ProviderConnectionError(ProviderError):
    category = "connection"


class ProviderResponseError(ProviderError):
    """The body is not the documented shape (not JSON, missing choices/content, no tool call)."""

    category = "bad_response"


class ProviderIncomplete(ProviderError):
    """Truncation (``max_tokens``/``length``) or another non-final stop: always a failure."""

    category = "incomplete"


class ProviderLimitIgnored(ProviderResponseError):
    """The provider reported more output tokens than the role's ``max_tokens`` (QQ-01): the server
    ignored the output ceiling (Ollama's ``/v1`` ignores ``max_completion_tokens``). The reply is
    not used; set the role's ``max_tokens_parameter`` to the name the server honours."""

    category = "output_limit_ignored"


class ProviderRefusal(ProviderError):
    category = "refusal"


class ProviderUnavailable(ProviderError):
    """No provider is configured for a stage that needs one (``provider: none``)."""

    category = "unavailable"


class CapabilityMismatch(ProviderError):
    """The role relies on a capability the registry does not declare, or the schema cannot be sent
    in the declared mode. Raised before any network call or budget reservation; never downgraded."""

    category = "capability_mismatch"


class SchemaViolation(ValueError):
    """The reply does not parse as the requested schema. ``raw`` keeps the reply for replay."""

    category = "schema_violation"

    def __init__(self, message: str, *, raw: str, stage: str | None = None, error_count: int = 0, paths: list[str] | None = None, stop_reason: str | None = None):
        self.raw, self.stage, self.error_count = raw, stage, error_count
        # Loggable without content: the violated field paths with the error type (never the input
        # values) and the provider's stop reason (``length``/``max_tokens`` explain a cut reply).
        self.paths = list(paths or [])
        self.stop_reason = stop_reason
        super().__init__(f"{stage}: {message}" if stage else message)


def map_http_error(response: httpx.Response, stage: str) -> ProviderError:
    status = response.status_code
    message = f"provider HTTP {status}"
    if status in (401, 403):
        return ProviderAuthError(message, stage=stage, status=status)
    if status == 429:
        return ProviderRateLimited(message, stage=stage, status=status, retry_after=response.headers.get("retry-after"))
    if status >= 500:
        return ProviderServerError(message, stage=stage, status=status)
    if status == 400 and _body_mentions(response, SCHEMA_TOO_COMPLEX_MARKERS):
        return ProviderSchemaTooComplex(f"{message}: the reply schema is too large for the provider's grammar compiler", stage=stage, status=status)
    return ProviderRequestError(message, stage=stage, status=status)


def _body_mentions(response: httpx.Response, markers: tuple[str, ...]) -> bool:
    """Whether the error body contains one of ``markers`` (case-insensitive). The body is only
    inspected (first 64 KiB), never returned or logged."""
    try:
        response.read()
        text = response.text[:65536].casefold()
    except Exception:  # noqa: BLE001 - an unreadable body is simply not a match
        return False
    return any(marker in text for marker in markers)


# --- usage and metering -----------------------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    """Normalised token usage. ``input_tokens`` is the total prompt size including cached and
    cache-write tokens on every provider (Anthropic reports them separately; they are summed)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    reasoning_tokens: int = 0
    reported: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CallEstimate:
    """What a call may consume, reserved before it is sent. Output is the ``max_tokens`` ceiling;
    input is a chars/4 heuristic (the committed value is the provider-reported usage)."""

    input_tokens: int
    output_tokens: int

    @classmethod
    def of(cls, text_chars: int, max_tokens: int) -> "CallEstimate":
        return cls(input_tokens=max(1, math.ceil(text_chars / 4)), output_tokens=max_tokens)


@dataclass(frozen=True)
class CallContext:
    """Per-call metadata. ``stage`` names the caller (planner, review, generator, extraction ...)."""

    stage: str
    attributes: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class CallMeter(Protocol):
    """What providers need from a budget: a reservation before the call and a record after it.

    ``record`` receives a dict with ``stage``, ``provider``, ``model``, ``returned_model``,
    ``mode``, ``success``, ``error_category``, ``seconds``, ``usage`` (``Usage.as_dict()``),
    ``estimate`` and ``pricing_key``. Implementations: ``CallBudget`` (ingestion) and
    ``providers.accounting.RunBudgetMeter`` (request path: ``Budget`` + ``Trace``).
    """

    def reserve(self, estimate: CallEstimate | None = None) -> None: ...

    def record(self, item: dict[str, Any]) -> None: ...


class CallBudget:
    """Call-count budget used on the write path; records every call with its normalised usage."""

    def __init__(self, maximum: int):
        self.maximum = maximum
        self.calls: list[dict] = []
        self._reserved = 0
        self._lock = threading.Lock()

    def reserve(self, estimate: CallEstimate | None = None):
        with self._lock:
            if self._reserved >= self.maximum:
                raise RuntimeError("LLM call budget exhausted")
            self._reserved += 1

    def record(self, item):
        with self._lock:
            self.calls.append(item)

    def usage(self) -> dict[str, int]:
        """Token totals over the recorded calls (unreported usage counts as zero)."""
        keys = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_creation_input_tokens")
        with self._lock:
            return {k: sum(int((c.get("usage") or {}).get(k) or 0) for c in self.calls) for k in keys}


# --- protocol ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleSpec:
    """Provider-facing view of a chat role (v2 ``ChatRole`` or v1 ``ModelConfig``)."""

    provider: str
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    max_tokens: int = 2048
    timeout_seconds: float = 60
    max_tokens_parameter: str = "max_completion_tokens"
    structured_output: str = "none"
    prompt_caching: str = "none"
    reasoning_effort: str | None = None
    pricing_key: str | None = None
    streaming: bool = False  # the role declares ``capabilities.streaming``
    # ``structured_output`` came from the registry default, not the config: only then may a native
    # call refused as ``ProviderSchemaTooComplex`` be switched once, recorded, to prompt mode.
    structured_output_defaulted: bool = False

    @classmethod
    def of(cls, config: Any) -> "RoleSpec":
        caps = getattr(config, "capabilities", None)
        pricing = getattr(config, "pricing_key", None)
        return cls(
            provider=config.provider,
            model=config.model,
            base_url=config.base_url,
            api_key_env=config.api_key_env,
            max_tokens=config.max_tokens,
            timeout_seconds=config.timeout_seconds,
            max_tokens_parameter=getattr(config, "max_tokens_parameter", "max_completion_tokens"),
            structured_output=getattr(caps, "structured_output", None) or "none",
            prompt_caching=getattr(caps, "prompt_caching", "none"),
            reasoning_effort=getattr(config, "reasoning_effort", None),
            pricing_key=pricing or config.model or None,
            streaming=bool(getattr(caps, "streaming", False)),
            structured_output_defaulted=getattr(config, "structured_output_defaulted", False) is True,
        )


@runtime_checkable
class ChatProvider(Protocol):
    name: str
    role: RoleSpec

    def capabilities(self) -> ModelCapabilities: ...

    def complete_json(self, system: str, payload: Any, schema: type[T], budget: CallMeter, ctx: CallContext) -> tuple[T, Usage]: ...

    def complete_json_raw(self, system: str, payload: Any, schema: type[BaseModel], budget: CallMeter, ctx: CallContext) -> tuple[str, Usage]: ...

    def complete_text(self, system: str, payload: Any, budget: CallMeter, ctx: CallContext) -> tuple[str, Usage]: ...


# --- shared helpers ---------------------------------------------------------------------------

FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def user_content(payload: Any) -> str:
    return payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)


def schema_instruction(schema: type[BaseModel]) -> str:
    return "\nReturn only one JSON object matching this schema:\n" + json.dumps(schema.model_json_schema())


def schema_name(schema: type[BaseModel]) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]", "_", schema.__name__)[:64]
    return name or "result"


def validate_reply(schema: type[T], raw: str, stage: str) -> T:
    try:
        return schema.model_validate_json(raw)
    except ValidationError as error:
        paths = [(".".join(str(part) for part in item.get("loc", ())) or "<root>") + f" ({item.get('type', 'invalid')})" for item in error.errors()[:10]]
        raise SchemaViolation(
            f"reply violates the {schema.__name__} schema ({error.error_count()} errors)", raw=raw, stage=stage, error_count=error.error_count(), paths=paths
        ) from error


# OpenAI strict structured output accepts a subset of JSON Schema. ``STRICT_KEYWORDS`` holds the
# documented structural keywords plus those the 2026-11-03 live probe saw accepted
# (docs/plans/reviews/qa_2.0.0a1.md, QA-15). Everything else (``oneOf``, ``allOf``, ``not``,
# ``discriminator``, ``uniqueItems``, ``if``/``then``/``else``, ``patternProperties``,
# ``prefixItems`` ...) is refused before any call. ``title`` and ``default`` are dropped: the reply is
# validated with the original pydantic model, so a value the model now has to emit is accepted.
STRICT_KEYWORDS: frozenset[str] = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "anyOf",
        "$ref",
        "$defs",
        "description",
        "pattern",
        "format",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
    }
)
STRICT_DROPPED: frozenset[str] = frozenset({"title", "default"})
_ANYOF_SIBLINGS: frozenset[str] = frozenset({"anyOf", "description"})


class StrictSchemaError(CapabilityMismatch):
    """A schema OpenAI strict mode cannot express. ``problems`` lists every offending construct
    with its JSON path so validation reports all of them at once. Raised before any call."""

    def __init__(self, name: str, problems: list[str], hint: str = "declare structured_output: json_schema for this role"):
        self.schema_name = name
        self.problems = list(problems)
        super().__init__(f"{name} is not strict-expressible: " + "; ".join(self.problems) + f" ({hint})")


def strict_schema(schema: dict[str, Any], name: str = "schema") -> dict[str, Any]:
    """The schema in OpenAI strict form (every object closed, every property required).

    Raises ``StrictSchemaError`` listing every construct strict mode refuses: a keyword outside
    ``STRICT_KEYWORDS`` (``oneOf``, ``allOf``, ``discriminator``, ``uniqueItems`` ...), a ``const``
    without a sibling ``type``, an ``anyOf`` at the root, an ``anyOf`` mixed with constraining
    sibling keywords or with a branch that has neither ``type`` nor ``$ref``, and free-form mappings
    (``additionalProperties`` with a schema, or an object without declared properties). Nothing is
    rewritten into a different construct: a schema either converts as it is or is refused."""
    problems: list[str] = []

    def walk(node: Any, path: str) -> Any:
        if isinstance(node, list):
            return [walk(v, f"{path}[{i}]") for i, v in enumerate(node)]
        if not isinstance(node, dict):
            return node
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key in STRICT_DROPPED:
                continue
            if key not in STRICT_KEYWORDS:
                problems.append(f"{path}: keyword {key!r} is not supported in strict mode")
                continue
            if key in {"properties", "$defs"} and isinstance(value, dict):
                result[key] = {sub: walk(child, f"{path}.{key}.{sub}") for sub, child in value.items()}
            elif key in {"enum", "required", "const"}:
                result[key] = value
            else:
                result[key] = walk(value, f"{path}.{key}")
        if "const" in node and "type" not in node:
            problems.append(f"{path}: const without a sibling type")
        if "anyOf" in node:
            mixed = sorted(set(node) - _ANYOF_SIBLINGS - STRICT_DROPPED)
            if mixed:
                problems.append(f"{path}: anyOf mixed with {mixed}")
            if path == "$":
                problems.append(f"{path}: anyOf at the root")
            for i, branch in enumerate(node["anyOf"] if isinstance(node["anyOf"], list) else []):
                if not isinstance(branch, dict) or not ({"type", "$ref"} & set(branch)):
                    problems.append(f"{path}.anyOf[{i}]: branch without a type or $ref")
        if node.get("type") == "object" or "properties" in node:
            if "properties" not in node:
                problems.append(f"{path}: object without declared properties (free-form mapping)")
            elif node.get("additionalProperties") not in (None, False):
                problems.append(f"{path}: additionalProperties with a schema (free-form mapping)")
            else:
                result["additionalProperties"] = False
                result["required"] = list(node["properties"])
        return result

    converted = walk(schema, "$")
    if problems:
        raise StrictSchemaError(name, problems)
    return converted


# Anthropic ``output_config.format`` (2.0.0a3, verified against the real API): numeric bounds and
# ``maxItems`` are refused with HTTP 400, ``minItems`` only takes 0 or 1, and every object must be
# closed. These value constraints are dropped from the wire schema; the reply is still validated
# with the original pydantic model, so a value outside them is a ``SchemaViolation``, never accepted.
OUTPUT_FORMAT_DROPPED: frozenset[str] = frozenset({"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "maxItems"})


def output_format_schema(schema: dict[str, Any], name: str = "schema") -> dict[str, Any]:
    """The schema as Anthropic ``output_config.format`` accepts it. Free-form mappings
    (``additionalProperties`` with a schema, or an object without declared properties) cannot be
    closed and raise ``StrictSchemaError`` before any call; nothing else is rewritten."""
    problems: list[str] = []

    def walk(node: Any, path: str) -> Any:
        if isinstance(node, list):
            return [walk(v, f"{path}[{i}]") for i, v in enumerate(node)]
        if not isinstance(node, dict):
            return node
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key in OUTPUT_FORMAT_DROPPED or (key == "minItems" and isinstance(value, int) and value > 1):
                continue
            if key in {"properties", "$defs"} and isinstance(value, dict):
                result[key] = {sub: walk(child, f"{path}.{key}.{sub}") for sub, child in value.items()}
            elif key in {"enum", "required", "const", "default"}:
                result[key] = value
            else:
                result[key] = walk(value, f"{path}.{key}")
        if node.get("type") == "object" or "properties" in node:
            if "properties" not in node:
                problems.append(f"{path}: object without declared properties (free-form mapping)")
            elif node.get("additionalProperties") not in (None, False):
                problems.append(f"{path}: additionalProperties with a schema (free-form mapping)")
            else:
                result["additionalProperties"] = False
        return result

    converted = walk(schema, "$")
    if problems:
        raise StrictSchemaError(name, problems, hint="declare structured_output: none for this role")
    return converted


# Complexity ceilings of Anthropic structured outputs (the documented schema complexity limits; above
# them the API answers HTTP 400 "compiled grammar is too large"). Counted on the wire schema that
# ``output_format_schema`` produces, each ``$defs`` entry once: properties not listed in their
# object's ``required`` and properties whose value is a union (``anyOf`` or a ``type`` list,
# including ``X | None``). The estimate is deterministic and conservative; a schema over a ceiling is
# never sent natively (2.0.0a3 parity finding: the planner's ``LlmPlan`` is over both).
OUTPUT_FORMAT_LIMITS: dict[str, int] = {"optional_properties": 24, "union_properties": 16}


def output_format_complexity(wire: dict[str, Any]) -> dict[str, int]:
    """``{"properties", "optional_properties", "union_properties"}`` counted over a wire schema."""
    counts = {"properties": 0, "optional_properties": 0, "union_properties": 0}

    def union(node: Any) -> bool:
        return isinstance(node, dict) and ("anyOf" in node or isinstance(node.get("type"), list))

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        if isinstance(properties, dict):
            required = set(node.get("required") or [])
            counts["properties"] += len(properties)
            counts["optional_properties"] += sum(1 for name in properties if name not in required)
            counts["union_properties"] += sum(1 for child in properties.values() if union(child))
            for child in properties.values():
                walk(child)
        for key, value in node.items():
            if key != "properties":
                walk(value)

    walk(wire)
    return counts


def output_format_problems(schema: dict[str, Any], name: str = "schema") -> list[str]:
    """Why ``schema`` cannot be sent as Anthropic ``output_config.format``: a free-form mapping
    (``StrictSchemaError``) or a complexity count over ``OUTPUT_FORMAT_LIMITS``. Empty when it can
    be sent. Deterministic; never calls the network."""
    try:
        wire = output_format_schema(schema, name)
    except StrictSchemaError as error:
        return [str(error)]
    return output_format_limit_problems(wire, name)


def output_format_limit_problems(wire: dict[str, Any], name: str = "schema") -> list[str]:
    """Complexity counts of a wire schema over ``OUTPUT_FORMAT_LIMITS`` (empty when it fits)."""
    counts = output_format_complexity(wire)
    over = [f"{key} {counts[key]} > {limit}" for key, limit in OUTPUT_FORMAT_LIMITS.items() if counts[key] > limit]
    if not over:
        return []
    return [f"{name} is too complex for output_config.format ({', '.join(over)}); the provider refuses it with HTTP 400 (declare structured_output: none for this role)"]


class HttpCall:
    """One HTTP POST with typed error mapping. No retry, no redirect following."""

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.transport = transport

    def post(self, url: str, *, headers: dict[str, str], body: dict[str, Any], timeout: float, stage: str) -> dict[str, Any]:
        try:
            options: dict[str, Any] = {} if self.transport is None else {"transport": self.transport}
            with httpx.Client(timeout=timeout, follow_redirects=False, **options) as client:
                response = client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as error:
            raise ProviderTimeout(f"provider timed out ({type(error).__name__})", stage=stage) from error
        except httpx.TransportError as error:
            raise ProviderConnectionError(f"provider unreachable ({type(error).__name__})", stage=stage) from error
        if response.is_error:
            raise map_http_error(response, stage)
        try:
            data = response.json()
        except ValueError as error:
            raise ProviderResponseError("provider reply is not JSON", stage=stage, status=response.status_code) from error
        if not isinstance(data, dict):
            raise ProviderResponseError("provider reply is not a JSON object", stage=stage, status=response.status_code)
        return data

    def post_stream(self, url: str, *, headers: dict[str, str], body: dict[str, Any], timeout: float, stage: str, consumer: Any) -> dict[str, Any]:
        """POST and read a ``text/event-stream`` reply, feeding every event to ``consumer.feed(event,
        data)``; returns ``consumer.result()`` (the reply in the provider's non-streaming shape). A
        reply that is not an event stream is a ``ProviderResponseError`` (no silent non-streaming
        fallback). Exceptions raised by the consumer (a cancelled run) close the connection."""
        try:
            options: dict[str, Any] = {} if self.transport is None else {"transport": self.transport}
            with httpx.Client(timeout=timeout, follow_redirects=False, **options) as client:
                with client.stream("POST", url, headers={**headers, "Accept": "text/event-stream"}, json=body) as response:
                    if response.is_error:
                        raise map_http_error(response, stage)
                    kind = response.headers.get("content-type", "")
                    if not kind.startswith("text/event-stream"):
                        raise ProviderResponseError("provider reply is not an event stream", stage=stage, status=response.status_code)
                    for event, data in iter_sse(response.iter_lines()):
                        consumer.feed(event, data)
        except httpx.TimeoutException as error:
            raise ProviderTimeout(f"provider timed out ({type(error).__name__})", stage=stage) from error
        except httpx.TransportError as error:
            raise ProviderConnectionError(f"provider unreachable ({type(error).__name__})", stage=stage) from error
        return consumer.result()


def iter_sse(lines: Any) -> Any:
    """Server-sent events from text lines: yields ``(event, data)``; ``event`` defaults to
    ``message``; multi-line ``data`` fields are joined with newlines; comments are skipped."""
    event, data = "", []
    for line in lines:
        line = line.rstrip("\r")
        if not line:
            if data:
                yield event or "message", "\n".join(data)
            event, data = "", []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "event":
            event = value
        elif name == "data":
            data.append(value)
    if data:
        yield event or "message", "\n".join(data)


def as_int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


class MeteredCall:
    """Wraps one provider exchange: reservation, timing, and a record in ``finally`` (also on failure)."""

    def __init__(self, provider: Any, budget: CallMeter, ctx: CallContext, mode: str, estimate: CallEstimate):
        self.provider, self.budget, self.ctx, self.mode, self.estimate = provider, budget, ctx, mode, estimate
        self.usage = Usage()
        self.returned_model: str | None = None
        self.extra: dict[str, Any] = {}

    def __enter__(self) -> "MeteredCall":
        self.budget.reserve(self.estimate)
        self.started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        role: RoleSpec = self.provider.role
        category = None
        if exc is not None:
            category = getattr(exc, "category", type(exc).__name__)
        self.budget.record(
            {
                "stage": self.ctx.stage,
                "provider": role.provider,
                "model": role.model,
                "returned_model": self.returned_model,
                "mode": self.mode,
                "success": exc is None,
                "error_category": category,
                "seconds": time.perf_counter() - self.started,
                "usage": self.usage.as_dict(),
                "estimate": asdict(self.estimate),
                "pricing_key": role.pricing_key,
                "prompt_caching": role.prompt_caching,
                "reasoning_effort": role.reasoning_effort,
                "max_tokens": role.max_tokens,
                **self.extra,
            }
        )


def timeout_for(role: RoleSpec, budget: CallMeter) -> float:
    """The role timeout, capped by the request deadline when the meter knows it."""
    remaining = getattr(budget, "remaining_seconds", None)
    left = remaining() if callable(remaining) else None
    if left is None:
        return role.timeout_seconds
    return max(0.001, min(role.timeout_seconds, left))
