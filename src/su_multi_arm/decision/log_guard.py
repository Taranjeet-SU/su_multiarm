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

from typing import Any

from ..core.types import ScopeSpec
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


def log_scope_guard(spec: ScopeSpec) -> UnsupportedConstraint | None:
    """``None`` when ``spec`` pins one kind (or only non-log kinds); else the refusal."""
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
