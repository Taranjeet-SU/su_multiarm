"""Caps and stratified sampling (addendum A1 §3.1.3).

When a unit's records-tier count exceeds ``pull.max_docs_per_unit`` the pull is stratified:

* ``hourly_quota`` (default): one ``_count`` per hour of the window (plus one ``outside`` stratum for
  documents of the unit whose timestamp falls outside the window), quota = cap × the stratum's share,
  rounded by largest remainder so the quotas sum to the cap exactly. Within a stratum documents are
  ordered by ``random_score{seed, field: _seq_no}``, so the quota is spread across the hour.
* ``random_score``: the whole unit ordered by ``random_score{seed, field: _seq_no}``, first ``cap``.

The plan records the method, seed, per-stratum counts, quotas and fetched counts; estimates scale per
stratum and are labelled ``approximate (method)``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .contracts import SamplePlan, Stratum


def allocate(cap: int, counts: dict[str, int]) -> dict[str, int]:
    """Largest-remainder quotas: ``sum == min(cap, total)``, ``quota_k <= counts_k``, deterministic
    (ties broken by stratum key)."""
    total = sum(counts.values())
    if total <= cap:
        return dict(counts)
    exact = {key: cap * count / total for key, count in counts.items()}
    quotas = {key: int(value) for key, value in exact.items()}
    left = cap - sum(quotas.values())
    order = sorted(counts, key=lambda key: (-(exact[key] - quotas[key]), key))
    for key in order[:left]:
        quotas[key] += 1
    return quotas


def hour_bounds(start: datetime, end: datetime) -> list[tuple[str, datetime, datetime]]:
    """``("00", 00:00, 01:00), …`` for every hour that starts inside ``[start, end)``."""
    out = []
    cursor, index = start, 0
    while cursor < end:
        upper = min(cursor + timedelta(hours=1), end)
        out.append((f"{index:02d}", cursor, upper))
        cursor, index = upper, index + 1
    return out


def plan(
    method: str,
    *,
    cap: int,
    seed: int,
    unit_total: int,
    hourly: list[tuple[str, datetime | None, datetime | None, int]] | None = None,
) -> SamplePlan:
    """The sample plan of one unit. ``hourly`` is ``(key, start, end, count)`` per stratum and is
    required for ``hourly_quota`` when the unit is over the cap."""
    if unit_total <= cap:
        return SamplePlan(method="none", seed=seed, cap=cap, unit_total=unit_total, strata=[Stratum(key="all", count=unit_total, quota=unit_total)])
    if method == "random_score":
        return SamplePlan(method="random_score", seed=seed, cap=cap, unit_total=unit_total, strata=[Stratum(key="all", count=unit_total, quota=cap)])
    if method != "hourly_quota":
        raise ValueError(f"unknown sample method {method!r}")
    if hourly is None:
        raise ValueError("hourly_quota needs per-hour counts")
    quotas = allocate(cap, {key: count for key, _, _, count in hourly})
    strata = [Stratum(key=key, start=start, end=end, count=count, quota=quotas[key]) for key, start, end, count in hourly]
    return SamplePlan(method="hourly_quota", seed=seed, cap=cap, unit_total=unit_total, strata=strata)


def random_score_query(query: dict[str, Any], seed: int) -> dict[str, Any]:
    """Reproducible per-shard random order; no script."""
    return {"function_score": {"query": query, "random_score": {"seed": seed, "field": "_seq_no"}, "boost_mode": "replace"}}
