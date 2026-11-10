"""Deterministic renderings of aggregate results (addendum A2 §4.1.5; live-deployment fix).

The numbers of an aggregate answer come from the aggregate, not from the model: every aggregate
record carries ``rendering``, a sentence built here from the ``ScopeResult`` alone. The generator is
asked to quote it verbatim and may only add narrative around it; when the answer text states a
number the cited exact aggregate does not back (``answer_number_mismatch``), the contract replaces
the model's text with these renderings instead of passing the wrong number (``contract.py``).
"""

from __future__ import annotations

import re
from typing import Any, Sequence

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
    if measure.kind == "share" and not measure.field:
        return "share"
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


def _group_cell(group: Any) -> str:
    if isinstance(group.value, dict):
        return str(group.count)
    if group.numerator is not None:  # a per-group share (B5 q043): "0.3 (12 of 40)"
        return f"{number(group.value)} ({group.numerator} of {group.count})"
    return number(group.value)


def render_aggregate(spec: ScopeSpec, result: ScopeResult) -> str:
    """One sentence (plus ``; ``-separated rows) stating every number of the result."""
    label = _label(spec)
    if result.unsupported and result.exactness == "bounded" and not result.groups and result.value is None:
        return f"{label}: not computed ({'; '.join(u.reason for u in result.unsupported)})."
    if result.groups:
        rows = result.groups[:MAX_ROWS]
        body = "; ".join(f"{_key(g)}: {_group_cell(g)}" for g in rows)
        more = f"; and {len(result.groups) - MAX_ROWS} more groups" if len(result.groups) > MAX_ROWS else ""
        by = ", ".join(spec.group_by) if spec.group_by else "period"
        if spec.min_count is not None:
            by += f" (groups with at least {spec.min_count} records)"
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
    qualifier = _qualifier(result)
    if result.exactness == "bounded" and isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0:
        qualifier = qualifier.replace("bounded", "bounded, not an exact zero for the question", 1)  # B5 q041
    return f"{label}: {number(value)} ({qualifier})."


MAX_CELL = 120  # characters of one free-text cell in a SQL rendering (longer cells end in "...")


def _cell(value: Any) -> str:
    text = number(value) if not isinstance(value, str) else value
    text = " ".join(text.split())
    return text if len(text) <= MAX_CELL else text[: MAX_CELL - 3] + "..."


def _ratio_cell(row: dict[str, Any], numerator: str, denominator: str, ratio: str) -> str:
    """A rate with its parts: "12 of 48 (0.25)"."""
    return f"{_cell(row.get(numerator))} of {_cell(row.get(denominator))} ({_cell(row.get(ratio))})"


def _sql_row(row: dict[str, Any], columns: list[str], ratios: Sequence[tuple[str, str, str]], ids: list[str] | None) -> str:
    by_ratio = {r: (n, d) for r, n, d in ratios}
    parts = {c for _, n, d in ratios for c in (n, d)} - set(by_ratio)
    cells = [f"{c}={_ratio_cell(row, *by_ratio[c], c)}" if c in by_ratio else f"{c}={_cell(row.get(c))}" for c in columns if c not in parts]
    text = ", ".join(cells)
    if ids:
        text += f" (records: {', '.join(ids)})"
    return text


def render_sql_rows(
    columns: list[str],
    rows: list[dict[str, Any]],
    *,
    exactness: str,
    truncated: bool = False,
    ties: int = 0,
    tie_before: bool = False,
    offset: int = 0,
    ratios: Sequence[tuple[str, str, str]] = (),
    contributors: Sequence[list[str] | None] | None = None,
) -> str:
    """The deterministic sentence of an executed SQL-surface result (2026-11-09 corpus-B fix F1):
    every returned row as ``column=value`` pairs, the exactness label, the row count and the
    LIMIT-boundary ties (F3). ``rows`` are the result rows' ``values`` dicts. A SQL aggregate now
    has a rendering of its own, so an answer whose numbers the contract strips falls back to the
    cited SQL result, never to an unrelated structured aggregate.

    2026-11-09 (corpus-B B7): ``ratios`` are ``(ratio, numerator, denominator)`` column names; a
    ratio cell is stated as "N of D (rate)" and its part columns are not repeated. ``contributors``
    (aligned with ``rows``) are the record ids of a small group: "(records: a, b)" after the row.
    Under a group lineage, the note states that the ids of each listed group are all of its records."""
    shown = rows[:MAX_ROWS]
    ids = list(contributors or [])
    body = "; ".join(_sql_row(row, columns, ratios, ids[k] if k < len(ids) else None) for k, row in enumerate(shown)) or "no rows"
    more = f"; and {len(rows) - MAX_ROWS} more rows" if len(rows) > MAX_ROWS else ""
    notes = [exactness, f"{len(rows)} row{'s' if len(rows) != 1 else ''}"]
    if ties:
        notes.append(f"the last {ties} row{'s' if ties != 1 else ''} tie the last row within the LIMIT and are included")
    if offset and rows:
        # 2026-11-09 (B4 q011): "1 row" read as the top of the ranking; the position is stated
        last = offset + len(rows)
        notes.append(f"ranked position {offset + 1}" + (f" to {last}" if last > offset + 1 else "") + f" of the ordering: OFFSET {offset} skipped the first {offset}")
    if tie_before:
        notes.append("the first row ties the row before the OFFSET")
    if truncated:
        notes.append("truncated")
    if any(ids):
        notes.append("the records listed for a row are all of its contributing records")
    return f"SQL result: {body}{more} ({'; '.join(notes)})."


_EXACT_LABEL = re.compile(r"\((exact)(?=[;)])")


def relabel_bounded(rendering: str | None, zero: bool = False) -> str | None:
    """A rendering whose aggregate the engine reported ``bounded`` after it was rendered (an
    unapplied qualifier, a conflicting count; 2026-11-09 B5 q023/q041): the last ``(exact`` label
    becomes ``(bounded``, and a zero is marked as no exact zero. The rendered sentence stated
    "(exact; 0 records matched)" for a bounded zero, and the generator repeated it as an exact 0.
    ``zero``: the aggregate's scalar value is 0."""
    if not rendering:
        return rendering
    matches = list(_EXACT_LABEL.finditer(rendering))
    if not matches:
        return rendering
    last = matches[-1]
    label = "bounded, not an exact zero for the question" if zero else "bounded"
    return rendering[: last.start()] + "(" + label + rendering[last.end() :]
