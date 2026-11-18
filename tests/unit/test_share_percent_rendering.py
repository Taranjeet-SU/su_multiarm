"""A scalar share is rendered with its percentage next to the fraction, so the writer copies it."""

from __future__ import annotations

from su_multi_arm.core.types import Measure, ScopeResult, ScopeSpec
from su_multi_arm.generation.rendering import render_aggregate


def _share(value: float, total: int) -> str:
    spec = ScopeSpec(measure=Measure(kind="share"))
    return render_aggregate(spec, ScopeResult(exactness="exact", denominator=total, value=value, total=total))


def test_scalar_share_shows_the_percentage():
    assert _share(0.01951, 205) == "share: 0.01951, that is 1.951% (exact; 205 records matched)."


def test_scalar_share_of_a_large_fraction():
    assert _share(0.5, 10).startswith("share: 0.5, that is 50% (")


def test_count_measure_is_unchanged():
    spec = ScopeSpec(measure=Measure(kind="count"))
    out = render_aggregate(spec, ScopeResult(exactness="exact", denominator=5, value=5, total=5))
    assert out == "records: 5 (exact; 5 records matched)."
