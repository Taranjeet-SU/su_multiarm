"""Embedding providers: ``hash`` (demo), ``sentence_transformers`` (local), ``openai`` and
``openai_compatible`` (``POST /embeddings``). ``Embedder`` is the facade the engine and write path
use; it applies prefixes, checks shape/finiteness and L2-normalises, whatever the provider.

Failures are typed (``ProviderError`` subclasses for HTTP, ``ValueError`` for shape/length); text
longer than a local model's context is refused rather than truncated. No retries.
"""

from __future__ import annotations

import hashlib
import threading
from fnmatch import fnmatchcase
from typing import Any, Protocol, runtime_checkable

import httpx
import numpy as np

from ._chat import secret
from .base import HttpCall, ProviderResponseError, Usage, as_int, tokens

# OpenAI embedding models that accept a ``dimensions`` request parameter.
OPENAI_DIMENSIONS_PARAM: tuple[str, ...] = ("text-embedding-3*",)


@runtime_checkable
class EmbeddingProvider(Protocol):
    def raw(self, texts: list[str]) -> np.ndarray: ...

    def recipe(self) -> dict[str, Any]: ...


class HashEmbeddings:
    def __init__(self, config: Any):
        self.config = config

    def raw(self, texts: list[str]) -> np.ndarray:
        dims = self.config.dimensions
        array = np.zeros((len(texts), dims), dtype="float32")
        for row, text in enumerate(texts):
            for token in tokens(text):
                digest = hashlib.sha256(token.encode()).digest()
                array[row, int.from_bytes(digest[:4], "little") % dims] += 1 if digest[4] & 1 else -1
        return array

    def recipe(self) -> dict[str, Any]:
        return {"provider": "hash", "dimensions": self.config.dimensions}


class SentenceTransformerEmbeddings:
    def __init__(self, config: Any):
        self.config = config
        self._model = None
        self._lock = threading.Lock()

    def raw(self, texts: list[str]) -> np.ndarray:
        cfg = self.config
        with self._lock:
            if self._model is None:
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(cfg.model, revision=cfg.revision, device=cfg.device, trust_remote_code=False)
        lengths = self._model.tokenizer(texts, truncation=False, padding=False)["input_ids"]
        if any(len(row) > self._model.max_seq_length for row in lengths):
            raise ValueError("embedding text exceeds model context; reduce chunk_size/title or query length")
        return self._model.encode(texts, batch_size=cfg.batch_size, convert_to_numpy=True, show_progress_bar=False)

    def recipe(self) -> dict[str, Any]:
        return {"provider": "sentence_transformers", "model": self.config.model, "revision": self.config.revision}


class HttpEmbeddings:
    """``openai`` and ``openai_compatible``: batches of ``batch_size``, index-checked replies."""

    def __init__(self, config: Any, transport: httpx.BaseTransport | None = None):
        self.config = config
        self.http = HttpCall(transport)
        self.last_usage = Usage()

    def _body(self, batch: list[str]) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.config.model, "input": batch}
        if self.config.provider == "openai" and any(fnmatchcase(self.config.model, p) for p in OPENAI_DIMENSIONS_PARAM):
            body["dimensions"] = self.config.dimensions
        return body

    def raw(self, texts: list[str]) -> np.ndarray:
        cfg = self.config
        key = secret(cfg.api_key_env, "embedding")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        rows: list[Any] = []
        prompt_tokens = 0
        reported = False
        for start in range(0, len(texts), cfg.batch_size):
            batch = texts[start : start + cfg.batch_size]
            data = self.http.post(cfg.base_url.rstrip("/") + "/embeddings", headers=headers, body=self._body(batch), timeout=60, stage="embedding")
            items = data.get("data")
            if not isinstance(items, list) or not all(isinstance(r, dict) and "index" in r and "embedding" in r for r in items):
                raise ProviderResponseError("embedding reply has no data list", stage="embedding")
            items = sorted(items, key=lambda r: r["index"])
            if [r["index"] for r in items] != list(range(len(batch))):
                raise ValueError("embedding response has missing/duplicate indexes")
            rows.extend(r["embedding"] for r in items)
            usage = data.get("usage")
            if isinstance(usage, dict):
                reported = True
                prompt_tokens += as_int(usage.get("prompt_tokens"))
        self.last_usage = Usage(input_tokens=prompt_tokens, reported=reported)
        return np.array(rows)

    def recipe(self) -> dict[str, Any]:
        return {"provider": self.config.provider, "model": self.config.model, "dimensions": self.config.dimensions}


def embedding_provider(config: Any, transport: httpx.BaseTransport | None = None) -> EmbeddingProvider:
    if config.provider == "hash":
        return HashEmbeddings(config)
    if config.provider == "sentence_transformers":
        return SentenceTransformerEmbeddings(config)
    if config.provider in {"openai", "openai_compatible"}:
        return HttpEmbeddings(config, transport)
    raise ValueError(f"unknown embedding provider {config.provider!r}")


class Embedder:
    def __init__(self, config: Any, transport: httpx.BaseTransport | None = None):
        self.config = config
        self.provider = embedding_provider(config, transport)

    def recipe(self) -> dict[str, Any]:
        return self.provider.recipe()

    def encode(self, texts: list[str], query: bool = False) -> np.ndarray:
        cfg = self.config
        if not texts:
            return np.empty((0, cfg.dimensions), dtype="float32")
        prefix = cfg.query_prefix if query else cfg.document_prefix
        texts = [prefix + t for t in texts]
        array = np.asarray(self.provider.raw(texts), dtype="float32")
        if array.shape != (len(texts), cfg.dimensions) or not np.isfinite(array).all():
            raise ValueError(f"embedding shape/values invalid; expected ({len(texts)}, {cfg.dimensions})")
        norm = np.linalg.norm(array, axis=1, keepdims=True)
        return array / np.maximum(norm, 1e-12)
