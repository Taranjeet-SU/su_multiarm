"""Whole-build budget of the write path (``budgets.ingest``; QA-06, review F17).

One ``runtime.budget.Budget`` per build holds the build's LLM-call and USD limits; embedding
provider requests are counted against ``max_embedding_calls`` and their USD shares the same
``max_usd``. Every reservation is taken **before** the call (LLM: one call, the input-token estimate,
the ``max_tokens`` ceiling and, when priced, the USD of that estimate; embedding: one request and the
USD of a chars/4 input estimate) and committed afterwards with the provider-reported usage (the
estimate when the provider reports none). Exhaustion raises ``BudgetExhausted`` (a
``RuntimeError`` whose message names the budget), which the extraction, report and embedding stages
turn into a ``budget``-category stage failure under the extraction policy. Nothing is retried or
downgraded. The per-request ``budgets.max_llm_calls`` is never used here.
"""

from __future__ import annotations

import math
import threading
from typing import Any

from ..core.errors import BudgetExhausted, ConfigError
from ..providers.base import CallEstimate
from ..runtime.budget import Budget, BudgetLimits, PriceTable


class IngestMeter:
    """The build's budget; ``role(pricing_key, stage)`` is the ``CallMeter`` handed to a provider."""

    def __init__(self, limits: Any, price_table: PriceTable | None):
        self.limits = limits
        self.price_table = price_table
        self.budget = Budget(BudgetLimits(max_llm_calls=limits.max_llm_calls, max_usd=limits.max_usd), price_table)
        self.calls: list[dict[str, Any]] = []  # LLM call records (manifest ``llm_calls``)
        self.embedding_calls = 0
        self._lock = threading.Lock()

    def role(self, pricing_key: str | None, stage: str) -> "RoleMeter":
        return RoleMeter(self, pricing_key, stage)

    def _priced(self, pricing_key: str | None) -> bool:
        return self.price_table is not None and pricing_key is not None and pricing_key in self.price_table.prices

    # --- embeddings ----------------------------------------------------------------------------

    def reserve_embedding(self, chars: int, pricing_key: str | None) -> list[Any]:
        """One embedding provider request: counted, and its estimated USD reserved when priced."""
        with self._lock:
            limit = self.limits.max_embedding_calls
            if limit is not None and self.embedding_calls >= limit:
                self.budget.usage.exhausted.append({"kind": "embedding_calls", "requested": 1, "remaining": 0, "stage": "embeddings"})
                raise BudgetExhausted("embedding_calls", 1, 0)
            self.embedding_calls += 1
        if pricing_key is None or (self.limits.max_usd is None and not self._priced(pricing_key)):
            return []  # local embedders (``price_key() is None``) spend nothing
        if self.price_table is None or pricing_key not in self.price_table.prices:
            raise ConfigError(f"budgets.ingest.max_usd needs a price for {pricing_key!r} in the price table", "models.roles.embedder.pricing_key")
        estimate = self.price_table.cost(pricing_key, max(1, math.ceil(chars / 4)), 0)  # type: ignore[union-attr]
        return [self.budget.reserve("usd", estimate, stage="embeddings", pricing_key=pricing_key)]

    def commit_embedding(self, reservations: list[Any], usage: Any) -> None:
        for reservation in reservations:
            if reservation.committed is not None:
                continue
            if usage is not None and getattr(usage, "reported", False):
                actual = self.price_table.cost(reservation.pricing_key, int(usage.input_tokens), 0)  # type: ignore[union-attr]
            else:
                actual = reservation.estimate  # unreported usage stays at its estimate, never zero
            self.budget.commit(reservation, actual)

    def summary(self) -> dict[str, Any]:
        snapshot = self.budget.snapshot()
        return {
            "limits": self.limits.model_dump(mode="json"),
            "llm_calls": len(self.calls),
            "embedding_calls": self.embedding_calls,
            "usd": snapshot["used"]["usd"] if self.price_table is not None else None,
            "exhausted": snapshot["exhausted"],
        }


class RoleMeter:
    """``providers.CallMeter`` for one write-path role (extraction, community reports)."""

    def __init__(self, meter: IngestMeter, pricing_key: str | None, stage: str):
        self.meter, self.pricing_key, self.stage = meter, pricing_key, stage
        self._reservations: list[Any] = []

    def reserve(self, estimate: CallEstimate | None = None) -> None:
        estimate = estimate or CallEstimate(input_tokens=0, output_tokens=0)
        self._reservations = self.meter.budget.reserve_call(
            stage=self.stage, pricing_key=self.pricing_key, input_tokens=estimate.input_tokens, output_tokens=estimate.output_tokens
        )

    def record(self, item: dict[str, Any]) -> None:
        usage = item.get("usage") or {}
        reported = bool(usage.get("reported"))
        budget, table = self.meter.budget, self.meter.price_table
        for reservation in self._reservations:
            if reservation.committed is not None:
                continue
            if reservation.kind == "calls":
                budget.commit(reservation, 1)
            elif not reported:
                budget.commit(reservation, reservation.estimate)
            elif reservation.kind == "input_tokens":
                budget.commit(reservation, int(usage.get("input_tokens") or 0))
            elif reservation.kind == "output_tokens":
                budget.commit(reservation, int(usage.get("output_tokens") or 0))
            elif reservation.kind == "usd":
                cost = table.cost(
                    reservation.pricing_key,
                    int(usage.get("input_tokens") or 0),
                    int(usage.get("output_tokens") or 0),
                    int(usage.get("cached_input_tokens") or 0),
                    int(usage.get("cache_creation_input_tokens") or 0),
                )  # type: ignore[union-attr]
                budget.commit(reservation, cost)
        self._reservations = []
        with self.meter._lock:
            self.meter.calls.append(item)


def check_prices(config: Any, price_table: PriceTable | None, uses_llm_extraction: bool) -> None:
    """With ``budgets.ingest.max_usd`` every write-path role that can spend must be priced, refused at
    construction (``ConfigError``) rather than mid-build."""
    if config.budgets.ingest.max_usd is None:
        return
    roles = config.models.roles
    keys: list[tuple[str, str | None]] = []
    if uses_llm_extraction and roles.extractor is not None:
        keys.append(("models.roles.extractor", roles.extractor.price_key()))
    if roles.community is not None:
        keys.append(("models.roles.community", roles.community.price_key()))
    if config.pipeline.arms.dense is not None:
        key = roles.embedder.price_key()
        if key is not None:
            keys.append(("models.roles.embedder", key))
    for path, key in keys:
        if price_table is None or key not in price_table.prices:
            raise ConfigError(f"budgets.ingest.max_usd needs a price for {key!r} in the price table", f"{path}.pricing_key")


_LOOPBACK = ("localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0")


def hosted_embedder(role: Any) -> bool:
    """An embedder that bills per call: ``openai``, or ``openai_compatible`` on a non-loopback host."""
    from urllib.parse import urlparse

    if role.provider == "openai":
        return True
    if role.provider != "openai_compatible":
        return False
    host = (urlparse(role.base_url).hostname or "").casefold()
    return host not in _LOOPBACK and not host.endswith(".localhost")


def require_ingest_cap(config: Any) -> None:
    """A hosted embedder on the write path needs an explicit build cap (a2 review N5): an unset
    ``budgets.ingest.max_usd`` and ``max_embedding_calls`` would let one ingest embed a whole corpus
    at the provider's price with no ceiling. Refused at construction (``ConfigError``)."""
    if config.pipeline.arms.dense is None or not hosted_embedder(config.models.roles.embedder):
        return
    ingest = config.budgets.ingest
    if ingest.max_usd is None and ingest.max_embedding_calls is None:
        raise ConfigError(
            "a hosted embedder needs an explicit write-path cap: set budgets.ingest.max_usd (with budgets.price_table) or budgets.ingest.max_embedding_calls",
            "budgets.ingest",
        )
