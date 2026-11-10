"""Scope guard of the logs profile (addendum A2 rev 3 §11.0, review R2-B4).

The rollups of a log generation are many document kinds over the same lines (``unit_day``,
``service_day``, ``log_day`` …). A typed scope that does not pin **one** kind sums the same lines
several times. Under ``logs.intents.enabled``, a scope that can match a log kind is refused with
``intent_required`` (naming the kinds) when it is:

* **kind-less** (no positive ``kind`` predicate),
* **negated** on ``kind`` (``kind != x`` still spans every other rollup), or
* **multi-kind** (``kind in [...]`` naming more than one kind, at least one a log kind).

A scope compiled from a ``LogIntentCall`` (``decision/log_intents.py``) never passes through the
guard (the log tools execute it directly). ``logs.serving.allow_adhoc_scopes: true`` turns the guard
off for a deployment that knows its kinds. The guard runs in the structured arm (decision steps and
the request aggregate) and in ``Engine.aggregate_in`` (the HTTP and MCP ``aggregate`` tool).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from ..core.types import Predicate, ScopeSpec
from ..models import UnsupportedConstraint

LOG_KINDS: frozenset[str] = frozenset(
    {
        "unit_day",
        "signature_day",
        "service_day",
        "signature_window",
        "signature_service_day",
        "signature_tenant_window",
        "signature_hour",
        "signature",
        "log_day",
        "log_hour",
        "endpoint_day",
        "endpoint_window",
        "exception_day",
        "coverage_day",
        "incident",
        "signature_examples",
        "signature_catalog",
        "graph_link",
        "signature_history",
        "signature_lineage",
    }
)


def guard_active(config: Any) -> bool:
    logs = getattr(config, "logs", None)
    return bool(logs is not None and logs.intents.enabled and not logs.serving.allow_adhoc_scopes)


@dataclass(frozen=True)
class LogGuard:
    """The guard's settings for one engine (``Dependencies.components["log_scope_guard"]``)."""

    log_sources: frozenset[str] | None = None


def log_sources(config: Any) -> frozenset[str] | None:
    """The ``Document.source`` values that can carry log records on a partitioned collection: every
    source of a member that declares a log kind in ``record_kinds``, is produced by a log host or is
    fed every few minutes. ``None`` without a ``collection`` (one store: not separable by source)."""
    collection = getattr(config, "collection", None)
    if collection is None:
        return None
    out: set[str] = set()
    for member in collection.members.values():
        if set(member.record_kinds) & LOG_KINDS or member.producer.kind == "log_host" or member.cadence == "minutes":
            out.update(member.sources)
    return frozenset(out)


def guard_for(config: Any) -> LogGuard | None:
    """The engine's guard component, or ``None`` when the guard is off (``guard_active``)."""
    return LogGuard(log_sources=log_sources(config)) if guard_active(config) else None


def admitted_sources(predicates: Iterable[Predicate]) -> set[str] | None:
    """The sources the positive ``source`` conditions admit (intersected); ``None`` when no
    positive ``eq``/``in`` condition names ``source``."""
    allowed: set[str] | None = None
    for p in predicates:
        if p.field != "source" or p.negate or p.op not in {"eq", "in"}:
            continue
        values = {str(v) for v in p.value} if isinstance(p.value, list) else {str(p.value)}
        allowed = values if allowed is None else allowed & values
    return allowed


def log_scope_guard(spec: ScopeSpec, *, context: Iterable[Predicate] = (), log_sources: Iterable[str] | None = None) -> UnsupportedConstraint | None:
    """``None`` when ``spec`` pins one kind (or only non-log kinds), or when ``log_sources`` is
    known and the positive ``source`` conditions of ``spec`` and of the executed ``context`` (ACL,
    request filters, the knowledge-half scope) admit none of them; else the refusal."""
    if log_sources is not None:
        admitted = admitted_sources([*spec.predicates, *context])
        if admitted is not None and not admitted & set(log_sources):
            return None  # the scope cannot read a log member (N1)
    positive: list[set[str]] = []
    for predicate in [*spec.predicates]:
        if predicate.field != "kind":
            continue
        if predicate.negate:
            return _refuse("a negated kind condition still spans every other log rollup", LOG_KINDS)
        if predicate.op == "eq" and isinstance(predicate.value, str):
            positive.append({predicate.value})
        elif predicate.op == "in" and isinstance(predicate.value, list):
            positive.append({str(v) for v in predicate.value})
        else:
            return _refuse("a kind condition must be eq or in", LOG_KINDS)
    if not positive:
        return _refuse("the scope names no document kind, so it sums every log rollup of the same lines", LOG_KINDS)
    kinds = set.intersection(*positive)
    if len(kinds) > 1 and kinds & LOG_KINDS:
        return _refuse("the scope spans several log kinds", kinds & LOG_KINDS)
    return None


def _refuse(why: str, kinds: set[str] | frozenset[str]) -> UnsupportedConstraint:
    return UnsupportedConstraint(
        constraint_text="kind " + ", ".join(sorted(kinds)),
        reason="intent_required",
        suggestion=f"{why}; ask through a log intent or tool (log_top, log_trend, …) or pin one kind",
    )
