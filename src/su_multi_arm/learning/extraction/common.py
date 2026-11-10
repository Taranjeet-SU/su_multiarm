"""Shared pieces of the LLM log fallback: residue heads, per-head outcomes, the cache-first caller
and quote grounding."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ...core.errors import SuError
from ...extraction.grounding import Located, _find_all, _on_group_boundaries, locate, normalise
from ...ingestion.accounting import FieldValue, Validators, parse_value

LEVEL_WEIGHT = {"fatal": 3, "critical": 3, "error": 3, "warn": 2, "warning": 2}


@dataclass
class Head:
    """One residue or field-gap head record of a sealed partition."""

    id: str
    unit_id: str
    partition: str
    text: str  # the redacted head line as the parser matched it (truncated to max_line_bytes)
    body: tuple[int, int]  # message-stage span of ``text``
    attributes: dict[str, str]
    stage_status: str
    reason: str | None  # the sealed reason (format_only, fallback_pending, field_gap, ...)
    gaps: list[str] = field(default_factory=list)
    pattern_id: str | None = None
    signature_id: str | None = None
    pattern_owner: str = "none"
    pattern_values: list[FieldValue] = field(default_factory=list)

    @property
    def body_text(self) -> str:
        return self.text[self.body[0] : self.body[1]]

    @property
    def service(self) -> str | None:
        return self.attributes.get("service")

    @property
    def level(self) -> str | None:
        return self.attributes.get("level")

    def rank(self) -> tuple[int, str, str]:
        return (-LEVEL_WEIGHT.get((self.level or "").casefold(), 1), self.partition, self.id)


@dataclass
class Outcome:
    final_status: str
    reason: str | None = None
    fields: list[FieldValue] = field(default_factory=list)
    signature_id: str | None = None
    pattern_owner: str = "none"


class CachePending(SuError):
    """No cached reply and no call: ``reason`` is ``fallback_pending`` (calls not allowed, overlay
    rebuild), ``capacity_deferred`` (no route available), ``budget_deferred`` (a cap was reached) or
    ``provider_error`` (the routes failed)."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass
class CallStats:
    calls: int = 0
    cache_hits: int = 0
    pending: int = 0
    errors: int = 0
    budget_refusals: int = 0
    capacity_deferred: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass
class Route:
    """One provider route at run time: its role, structured-output model, spend meter and probe
    state (``None`` = not probed yet, ``ready``, ``unavailable: …``, ``down: …``,
    ``paid_fallback_not_allowed``)."""

    name: str
    role: Any
    model: Any  # providers.JsonModel
    meter: Any  # LearnerMeter (USD 0 on a local route)
    paid: bool
    health_url: str | None = None
    capacity_url: str | None = None
    timeout: float = 3.0
    status: str | None = None
    calls: int = 0
    seconds: float = 0.0  # wall time spent in calls on this route (throughput, per tier)
    tokens: int = 0
    tier_name: str | None = None  # local | lan | paid (from the config); None: from ``paid``
    windows: list[str] = field(default_factory=list)  # UTC HH:MM-HH:MM; empty = always
    max_invalid_replies: int | None = None
    invalid: int = 0

    def in_window(self, at: datetime) -> bool:
        if not self.windows:
            return True
        minute = at.astimezone(timezone.utc).hour * 60 + at.astimezone(timezone.utc).minute
        for window in self.windows:
            start, end = (int(p[:2]) * 60 + int(p[3:]) for p in window.split("-"))
            if (start <= minute < end) if start <= end else (minute >= start or minute < end):
                return True
        return False

    @property
    def provider(self) -> str:
        return self.role.provider

    @property
    def model_name(self) -> str:
        return self.role.model

    def structured_mode(self) -> str:
        """What the provider uses for the reply schema (recorded in the run report)."""
        provider = getattr(self.model, "provider", None)
        mode = getattr(provider, "structured_mode", None)
        if callable(mode):
            return str(mode())
        return str(getattr(getattr(self.role, "capabilities", None), "structured_output", None) or "none")

    @property
    def tier(self) -> str:
        return self.tier_name or ("paid" if self.paid else "local")

    def throughput(self) -> dict[str, Any]:
        if not self.calls or self.seconds <= 0:
            return {"calls_per_min": None, "tokens_per_s": None}
        return {"calls_per_min": round(60 * self.calls / self.seconds, 2), "tokens_per_s": round(self.tokens / self.seconds, 2)}

    def report(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tier": self.tier,
            "provider": self.provider,
            "model": self.model_name,
            "paid": self.paid,
            "status": self.status or "not_probed",
            "calls": self.calls,
            "invalid_replies": self.invalid,
            "tokens": self.tokens,
            "seconds": round(self.seconds, 4),
            **self.throughput(),
            "structured_output": self.structured_mode(),
        }


def default_health_url(route: Route) -> str | None:
    if route.health_url:
        return route.health_url
    base = getattr(route.role, "base_url", None)
    if route.provider == "openai_compatible" and base:
        return base.rstrip("/") + "/models"
    return None


class HttpProbe:
    """Readiness and capacity probe over HTTP (GET; ``transport`` for tests only)."""

    def __init__(self, transport: Any = None):
        self.transport = transport

    def __call__(self, route: Route) -> tuple[bool, str]:
        import httpx

        health = default_health_url(route)
        if health is None and route.capacity_url is None:
            return True, "not probed (no health URL)"
        try:
            with httpx.Client(transport=self.transport, timeout=route.timeout) as client:
                if health is not None:
                    response = client.get(health)
                    if response.status_code // 100 != 2:
                        return False, f"health {response.status_code}"
                if route.capacity_url is not None:
                    response = client.get(route.capacity_url)
                    if response.status_code // 100 != 2:
                        return False, f"capacity {response.status_code}"
                    if response.content:
                        try:
                            body = response.json()
                        except ValueError:
                            return False, "capacity reply is not JSON"
                        if isinstance(body, dict) and body.get("available") is False:
                            return False, "capacity: not available"
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}"
        return True, "ready"


class Caller:
    """Cache first, then one reserved structured call on the first available route.

    - **Routes** are tried in order. A route is probed (health, then capacity) before its first
      call of the run; an unavailable route is skipped. A paid route after a local one is used only
      with ``allow_paid_fallback``. When no route is available the item is ``capacity_deferred``
      (never dropped; the next run retries it).
    - A provider error marks the route ``down`` for the rest of the run: the item is
      ``provider_error`` (never retried) and later items go to the next route.
    - A spend cap (``BudgetExhausted``: run, day, provider total or scope total) stops all calls of
      this caller: there is no fallback to another provider when a cap is hit.
    - ``allow_calls=False`` (overlay rebuild) turns every miss into ``fallback_pending``.

    Cache lookups try every route's key, so a reply paid on one route is reused on any."""

    def __init__(
        self,
        *,
        routes: list[Route],
        cache: Any,
        stage: str,
        now: datetime,
        allow_calls: bool = True,
        allow_paid_fallback: bool = False,
        probe: Any = None,
        stop: Any = None,
        self_hosted_only: bool = False,
    ):
        self.routes, self.cache, self.stage = routes, cache, stage
        self.now, self.allow_calls, self.allow_paid_fallback = now, allow_calls, allow_paid_fallback
        self.probe = probe or HttpProbe()
        self.stop = stop  # () -> bool: a requested clean stop
        self.self_hosted_only = self_hosted_only  # learning.bootstrap: paid routes are skipped
        self.stopped: str | None = None
        self.stats = CallStats()

    def keys(self, keyf: Any) -> list[tuple[Route, str]]:
        return [(route, keyf(route.provider, route.model_name)) for route in self.routes]

    def cached(self, keyf: Any, schema: type, partitions: list[str]) -> tuple[Any, str, str] | None:
        """``(reply, reply sha, model)`` of the first route whose key is cached."""
        for route, key in self.keys(keyf):
            hit = self.cache.get(key)
            if hit is not None:
                self.stats.cache_hits += 1
                for partition in partitions:
                    self.cache.tag(key, partition)
                return schema.model_validate_json(hit[0]), hit[1], route.model_name
        return None

    def call(self, keyf: Any, kind: str, system: str, payload: dict[str, Any], schema: type, partitions: list[str]) -> tuple[Any, str, str, str]:
        """``(reply, reply sha, "cache" | "call", model)``. Raises ``CachePending`` (with its
        reason), ``BudgetExhausted``, ``ProviderError`` or ``SchemaViolation``."""
        hit = self.cached(keyf, schema, partitions)
        if hit is not None:
            return hit[0], hit[1], "cache", hit[2]
        reply, route = self.fresh(system, payload, schema)
        digest = self.store(keyf(route.provider, route.model_name), kind, reply, partitions, route)
        return reply, digest, "call", route.model_name

    def available(self) -> Route:
        if not self.allow_calls:
            self.stats.pending += 1
            raise CachePending("fallback_pending", "reply not cached and model calls are not allowed in this run")
        if self.stopped is None and self.stop is not None and self.stop():
            self.stopped = "paused"  # a clean stop between calls (stop file or SIGTERM)
        if self.stopped is not None:
            self.stats.pending += 1
            if self.stopped == "paused":
                raise CachePending("capacity_deferred", "paused: stop requested")
            raise CachePending("budget_deferred" if self.stopped.startswith("budget") else "provider_error", self.stopped)
        self_hosted_seen = False
        for route in self.routes:
            if route.paid and self.self_hosted_only:
                route.status = route.status or "bootstrap_self_hosted_only"
                continue
            if route.paid and self_hosted_seen and not self.allow_paid_fallback:
                route.status = route.status or "paid_fallback_not_allowed"
                continue
            self_hosted_seen = self_hosted_seen or not route.paid
            if not route.in_window(self.now):
                route.status = "outside_window"
                continue
            if route.status is None or route.status == "outside_window":
                ok, detail = self.probe(route)
                route.status = "ready" if ok else f"unavailable: {detail}"
            if route.status == "ready":
                return route
        self.stats.capacity_deferred += 1
        if any((r.status or "").startswith("down") for r in self.routes):
            raise CachePending("provider_error", "every route failed or is unavailable")
        raise CachePending("capacity_deferred", "no provider route is available")

    def fresh(self, system: str, payload: dict[str, Any], schema: type) -> tuple[Any, Route]:
        from ...core.errors import BudgetExhausted
        from ...providers.base import ProviderError, SchemaViolation

        route = self.available()
        started = time.perf_counter()
        before = len(getattr(route.meter, "calls", []) or [])
        try:
            reply = route.model.call(system, payload, schema, route.meter, self.stage)
        except BudgetExhausted as exc:
            self.stats.budget_refusals += 1
            self.stopped = f"budget_exhausted:{exc.kind}:{route.name}"
            raise
        except SchemaViolation:
            self.stats.errors += 1
            self.stats.calls += 1
            route.calls += 1
            route.invalid += 1
            if route.max_invalid_replies is not None and route.invalid >= route.max_invalid_replies:
                route.status = f"down: {route.invalid} invalid replies"
            raise
        except ProviderError as exc:
            self.stats.errors += 1
            route.status = f"down: {exc.category}"
            raise
        self.stats.calls += 1
        route.calls += 1
        route.seconds += time.perf_counter() - started
        for item in (getattr(route.meter, "calls", []) or [])[before:]:
            usage = item.get("usage") or {}
            route.tokens += int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        return reply, route

    def store(self, key: str, kind: str, reply: Any, partitions: list[str], route: Route) -> str:
        digest = self.cache.put(key, kind, route.model_name, reply.model_dump_json(), self.now.isoformat())
        for partition in partitions:
            self.cache.tag(key, partition)
        return digest

    def state(self) -> str:
        """``ok`` | ``paused`` | ``paid_exhausted`` (a cap stopped the calls) | ``capacity_limited``
        (items waited for a route). Caps are never raised automatically."""
        if self.stopped == "paused":
            return "paused"
        if self.stopped and self.stopped.startswith("budget"):
            return "paid_exhausted" if any(r.paid and self.stopped.endswith(f":{r.name}") for r in self.routes) else "budget_exhausted"
        if self.stats.capacity_deferred:
            return "capacity_limited"
        return "ok"

    def report(self) -> dict[str, Any]:
        return {**self.stats.as_dict(), "state": self.state(), "stopped": self.stopped, "routes": [r.report() for r in self.routes], "spend": [r.meter.summary() for r in self.routes if r.meter is not None and r.calls]}


# --- quote grounding -----------------------------------------------------------------------------

STEPS = ["nfkc", "casefold", "whitespace"]


def ground_quote(quote: str, line: str, levels: list[str]) -> Located | None:
    """Locate a model quote in its own line (``exact``, then ``normalised`` when allowed). An
    ambiguous quote is rejected unless the source text is identical at every occurrence (then the
    first occurrence is the span). Never fuzzy, never relocated to another field."""
    found = locate(quote, line, STEPS if "normalised" in levels else [], ambiguous="first")
    if found is None:
        return None
    if found.match_level == "exact":
        return found if "exact" in levels else None
    if found.occurrences > 1:
        norm_source, starts, ends = normalise(line, STEPS)
        norm_quote = normalise(quote.strip(), STEPS)[0].strip()
        texts = {line[starts[i] : ends[i + len(norm_quote) - 1]] for i in _find_all(norm_source, norm_quote) if _on_group_boundaries(i, i + len(norm_quote), starts)}
        if len(texts) != 1:
            return None
    return found


def quoted_value(name: str, kind: str, quote: str, line: str, *, levels: list[str], validators: Validators, source: str, reply_sha: str, model: str) -> tuple[FieldValue | None, str | None]:
    """``(value, None)`` or ``(None, reason)`` with reason ``grounding_failed`` (quote not in the
    line), ``type_invalid`` (located, not parseable) or ``range_invalid`` (validator)."""
    located = ground_quote(quote, line, levels)
    if located is None:
        return None, "grounding_failed"
    try:
        value = parse_value(line[located.start : located.end], kind)
    except ValueError:
        return None, "type_invalid"
    if not validators.ok(name, value):
        return None, "range_invalid"
    return FieldValue(name=name, value=value, type=kind, start=located.start, end=located.end, source=source, reply_sha=reply_sha, model=model), None  # type: ignore[arg-type]


def worst(reasons: list[str]) -> str:
    """The reason reported for a line whose every value failed (most specific first)."""
    for reason in ("range_invalid", "type_invalid", "grounding_failed"):
        if reason in reasons:
            return reason
    return "model_declined"
