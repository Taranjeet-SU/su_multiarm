"""Model providers (plan §3.6).

Chat: ``anthropic``, ``openai``, ``openai_compatible``, ``none`` behind the ``ChatProvider``
protocol; capabilities per ``(provider, model-pattern)`` in ``capabilities.REGISTRY``. Embeddings:
``hash``, ``sentence_transformers``, ``openai``, ``openai_compatible``. The alpha names
(``JsonModel``, ``Embedder``, ``CallBudget``, ``tokens``) stay importable from here.
"""

from .accounting import RunBudgetMeter
from .anthropic import AnthropicProvider
from .base import (
    CallBudget,
    CallContext,
    CallEstimate,
    CallMeter,
    CapabilityMismatch,
    ChatProvider,
    ProviderAuthError,
    ProviderConnectionError,
    ProviderError,
    ProviderIncomplete,
    ProviderRateLimited,
    ProviderRefusal,
    ProviderRequestError,
    ProviderResponseError,
    ProviderServerError,
    ProviderTimeout,
    ProviderUnavailable,
    RoleSpec,
    SchemaViolation,
    Usage,
    StrictSchemaError,
    strict_schema,
    tokens,
)
from ._chat import MissingSecret
from .capabilities import REGISTRY, ModelCapabilities, check_role, lookup, supports_native
from .embeddings import Embedder, EmbeddingProvider, embedding_provider
from .json_model import JsonModel, chat_provider
from .none import NoneProvider
from .openai import OpenAIProvider
from .openai_compatible import OpenAICompatibleProvider

__all__ = [
    "AnthropicProvider",
    "CallBudget",
    "CallContext",
    "CallEstimate",
    "CallMeter",
    "CapabilityMismatch",
    "ChatProvider",
    "Embedder",
    "EmbeddingProvider",
    "JsonModel",
    "MissingSecret",
    "ModelCapabilities",
    "NoneProvider",
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "ProviderAuthError",
    "ProviderConnectionError",
    "ProviderError",
    "ProviderIncomplete",
    "ProviderRateLimited",
    "ProviderRefusal",
    "ProviderRequestError",
    "ProviderResponseError",
    "ProviderServerError",
    "ProviderTimeout",
    "ProviderUnavailable",
    "REGISTRY",
    "RoleSpec",
    "RunBudgetMeter",
    "SchemaViolation",
    "Usage",
    "chat_provider",
    "check_role",
    "embedding_provider",
    "lookup",
    "StrictSchemaError",
    "strict_schema",
    "supports_native",
    "tokens",
]
