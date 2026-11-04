"""Deterministic renderings of aggregate results (addendum A2 §4.1.5; live-deployment fix).

The numbers of an aggregate answer come from the aggregate, not from the model: every aggregate
record carries ``rendering``, a sentence built here from the ``ScopeResult`` alone. The generator is
asked to quote it verbatim and may only add narrative around it; when the answer text states a
number the cited exact aggregate does not back (``answer_number_mismatch``), the contract replaces
the model's text with these renderings instead of passing the wrong number (``contract.py``).
"""

from __future__ import annotations

from typing import Any

from ..core.types import ScopeResult, ScopeSpec

MAX_ROWS = 25


def number(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def _label(spec: ScopeSpec) -> str:
    measure = spec.measure
    if measure is None or measure.kind == "count":
        return "records"
    if measure.kind == "count_distinct":
        return f"distinct {measure.unit}"
    if measure.kind == "bucket":
        inner = measure.of
        what = "records" if inner is None or inner.kind == "count" else f"{inner.kind} of {inner.field}"
        return f"{what} per {measure.granularity}"
    return f"{measure.kind} of {measure.field}"


def _qualifier(result: ScopeResult) -> str:
    parts = [result.exactness]
    if result.total is not None:
        parts.append(f"{result.total} records matched")
    if result.applied_basis:
        parts.append("basis " + ", ".join(result.applied_basis))
    return "; ".join(parts)


def _key(group: Any) -> str:
    return ", ".join(str(v) for v in group.key.values())


def render_aggregate(spec: ScopeSpec, result: ScopeResult) -> str:
    """One sentence (plus ``; ``-separated rows) stating every number of the result."""
    label = _label(spec)
    if result.unsupported and result.exactness == "bounded" and not result.groups and result.value is None:
        return f"{label}: not computed ({'; '.join(u.reason for u in result.unsupported)})."
    if result.groups:
        rows = result.groups[:MAX_ROWS]
        body = "; ".join(f"{_key(g)}: {number(g.value) if not isinstance(g.value, dict) else g.count}" for g in rows)
        more = f"; and {len(result.groups) - MAX_ROWS} more groups" if len(result.groups) > MAX_ROWS else ""
        by = ", ".join(spec.group_by) if spec.group_by else "period"
        ties = f" ({len(result.ties)} more groups tie the last one shown)" if result.ties else ""
        return f"{label} by {by}: {body}{more}{ties} ({_qualifier(result)})."
    value = result.value
    if isinstance(value, dict) and "value" in value and "record_id" in value:
        return f"{label}: {value['value']} (record {value['record_id']}; {_qualifier(result)})."
    if isinstance(value, dict):
        body = "; ".join(f"{k}: {number(v)}" for k, v in list(value.items())[:MAX_ROWS])
        return f"{label}: {body} ({_qualifier(result)})."
    if isinstance(value, list):
        return f"{label}: {len(value)} ({_qualifier(result)})."
    return f"{label}: {number(value)} ({_qualifier(result)})."
