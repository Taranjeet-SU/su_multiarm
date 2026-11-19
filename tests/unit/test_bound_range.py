"""A date bound at the edge of the supported range is refused as unparseable, never a crash. Synthetic data only."""

from __future__ import annotations

import pytest

from su_multi_arm.core.types import FieldSpec
from su_multi_arm.decision.values import BoundError, _parse_date_bound, _parse_datetime_bound

DATETIME = FieldSpec(name="created", type="datetime")
DATE = FieldSpec(name="due", type="date")


@pytest.mark.parametrize("value", ["0001-01-01T00:00:00+05:30", "0001-01-01T00:00:00+01:00", "0000-01-01T00:00:00+00:00"])
def test_a_datetime_bound_that_cannot_be_read_in_utc_is_refused(value):
    with pytest.raises(BoundError) as error:
        _parse_datetime_bound(DATETIME, value)
    assert error.value.reason == "unparseable_bound"


@pytest.mark.parametrize("value", ["0001-01-01T00:00:00+05:30", "9999-12-31T23:59:59-05:00"])
def test_a_date_bound_that_cannot_be_read_in_utc_is_refused(value):
    with pytest.raises(BoundError) as error:
        _parse_date_bound(DATE, value)
    assert error.value.reason == "unparseable_bound"


def test_ordinary_bounds_still_parse():
    assert _parse_datetime_bound(DATETIME, "2024-03-01T00:00:00+05:30").year == 2024
    assert _parse_date_bound(DATE, "2024-03-01").year == 2024
    assert _parse_datetime_bound(DATETIME, "2000-01-01T00:00:00+00:00").year == 2000
