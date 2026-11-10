"""M4 guard: measure units (unified merge plan §6.8; synthetic F0 data).

Units are declared per measure column of a setup; after parsing, adding, subtracting, comparing,
``BETWEEN``/``IN`` lists and ``COALESCE``/``IFNULL``/``NULLIF``/``CASE``/``IIF``/multi-argument
``min``/``max`` over different units are refused (``mixed_units``, repairable). Products and ratios
(a click-through rate) carry no unit and are allowed; literals and counts are unitless. A catalog
without units (every projection catalog) is not checked.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

pytest.importorskip("sqlglot")

import sql_setup_support as sss  # noqa: E402
import sql_surface_support as ss  # noqa: E402
from su_multi_arm.decision.values import ValueResolver  # noqa: E402
from su_multi_arm.sql_surface.catalog import build_sql_catalog  # noqa: E402
from su_multi_arm.sql_surface.guard import SqlRefusal, guard  # noqa: E402
from su_multi_arm.sql_surface.setup import SetupValues, SqlSurfaceSetup, execute_setup, setup_catalog  # noqa: E402


@pytest.fixture(scope="module")
def surface(tmp_path_factory: pytest.TempPathFactory) -> SqlSurfaceSetup:
    return sss.build(tmp_path_factory.mktemp("m4_units"))


def run(setup: SqlSurfaceSetup, sql: str):
    with SetupValues(setup, []) as values:
        guarded = guard(sql, setup_catalog(setup, [], values), values, acl=[])
    return guarded, execute_setup(setup, guarded, timeout_ms=5000)


def refused(setup: SqlSurfaceSetup, sql: str) -> SqlRefusal:
    with SetupValues(setup, []) as values, pytest.raises(SqlRefusal) as info:
        guard(sql, setup_catalog(setup, [], values), values, acl=[])
    return info.value


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SUM(occurrences) + SUM(observed_seconds) AS x FROM issue_day",
        "SELECT occurrences - observed_seconds AS x FROM issue_day",
        "SELECT id FROM issue_day WHERE occurrences > observed_seconds",
        "SELECT id FROM issue_day WHERE occurrences BETWEEN 1 AND observed_seconds",
        "SELECT COALESCE(searches, clicks) AS x FROM search_query_day",
        "SELECT IFNULL(searches, clicks) AS x FROM search_query_day",
        "SELECT NULLIF(searches, clicks) AS x FROM search_query_day",
        "SELECT CASE WHEN day = '2026-11-07' THEN searches ELSE clicks END AS x FROM search_query_day",
        "SELECT IIF(day = '2026-11-07', searches, clicks) AS x FROM search_query_day",
        "SELECT MAX(searches, clicks) AS x FROM search_query_day",
        "SELECT SUM(searches + clicks) * 1.0 / COUNT(*) AS x FROM search_query_day",  # mixing inside a ratio operand
        "SELECT id FROM search_query_day WHERE searches IN (clicks, 3)",
        # through a derived table and a CTE: the measures keep their units
        "SELECT s.a - s.b AS x FROM (SELECT SUM(searches) AS a, SUM(clicks) AS b FROM search_query_day) AS s",
        "WITH s AS (SELECT SUM(occurrences) AS e FROM issue_day) SELECT s.e + (SELECT SUM(resolution_hours) FROM ticket) AS x FROM s",
        # across a declared join
        "SELECT t.id FROM ticket t JOIN customer c ON t.customer_id = c.id JOIN issue_day i ON i.customer_id = c.id WHERE t.resolution_hours < i.observed_seconds",
        # a scalar subquery keeps its unit
        "SELECT id FROM issue_day WHERE occurrences > (SELECT AVG(observed_seconds) FROM issue_day)",
    ],
)
def test_mixed_unit_arithmetic_is_refused(surface, sql):
    error = refused(surface, sql)
    assert error.code == "mixed_units" and error.kind == "repairable" and "different units" in error.detail


def test_two_scalar_measures_of_different_sources_do_not_add(surface):
    assert refused(surface, "SELECT (SELECT SUM(occurrences) FROM issue_day) + (SELECT SUM(resolution_hours) FROM ticket) AS x").code == "mixed_units"
    _, out = run(surface, "SELECT (SELECT SUM(searches) FROM search_query_day) - (SELECT SUM(zero_results) FROM search_query_day) AS x")
    assert out["rows"][0][0] == 25


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT SUM(clicks) * 1.0 / SUM(searches) AS ctr FROM search_query_day", None),  # a rate of two measures
        ("SELECT SUM(searches) - SUM(zero_results) AS answered FROM search_query_day", 25),  # same unit
        ("SELECT COUNT(*) AS n FROM issue_day WHERE occurrences > 5", None),  # a literal is unitless (count cross-checked below)
        ("SELECT SUM(occurrences) AS e FROM issue_day WHERE observed_seconds >= 600", None),
        ("SELECT MAX(occurrences) AS m FROM issue_day", None),
        ("SELECT ROUND(AVG(resolution_hours), 1) AS h FROM ticket", None),
        ("SELECT SUM(occurrences) * 60.0 / SUM(observed_seconds) AS per_minute FROM issue_day", None),
        ("SELECT COUNT(*) + SUM(occurrences) AS x FROM issue_day", None),  # a count is unitless
    ],
)
def test_like_units_ratios_and_literals_are_allowed(surface, sql, expected):
    guarded, out = run(surface, sql)
    assert out["rows"]
    if expected is not None:
        assert out["rows"][0][0] == expected


def test_a_filtered_count_equals_a_direct_read_of_the_pinned_rows(surface):
    """Cross-check against the raw file: archive members plus the ledger's current slices."""
    import sqlite3

    from su_multi_arm.testing.fixtures.unified import load

    current = sorted(s for day in load(sss.ROOT).ledger["current"].values() for s in day)
    path = next(a.path for a in surface.attachments if a.alias == "logs")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        archive = db.execute("SELECT COUNT(*) FROM issue_day_archive WHERE occurrences > 5").fetchone()[0]
        live = db.execute(f"SELECT COUNT(*) FROM issue_day_slice WHERE occurrences > 5 AND slice_id IN ({', '.join('?' for _ in current)})", current).fetchone()[0]
    finally:
        db.close()
    _, out = run(surface, "SELECT COUNT(*) AS n FROM issue_day WHERE occurrences > 5")
    assert out["rows"][0][0] == archive + live > 0


def test_a_ratio_of_measures_keeps_its_parts(surface):
    guarded, out = run(surface, "SELECT SUM(clicks) * 1.0 / SUM(searches) AS ctr FROM search_query_day")
    assert guarded.ratios and guarded.ratio_projected == ["ctr_numerator", "ctr_denominator"]
    assert out["rows"][0][1:] == (5, 30)


def test_units_are_declared_on_numeric_measures_only(surface):
    data = surface.model_dump(mode="json")
    data["views"][0]["columns"][4]["unit"] = "hours"  # service: a string
    with pytest.raises(ValidationError, match="numeric measure"):
        SqlSurfaceSetup.model_validate(data)
    data = surface.model_dump(mode="json")
    data["views"][0]["columns"][8]["unit"] = "Hours!"
    with pytest.raises(ValidationError, match="pattern"):
        SqlSurfaceSetup.model_validate(data)


def test_the_catalog_shows_units_to_the_sql_writer(surface):
    text = setup_catalog(surface, []).render()
    assert "occurrences | INTEGER (integer) | events" in text and "observed_seconds | REAL (number) | seconds" in text
    assert "UNITS: a column with a unit holds values in that unit" in text


def test_a_catalog_without_units_is_not_checked():
    world = ss.world()
    arm = ss.make_arm(world)
    ctx = ss.context()
    resolver = ValueResolver(arm.schema, arm._records(), ctx.generation, [])
    sql_cat = build_sql_catalog(arm.catalog_for(ctx), arm.schema, resolver, [])
    assert not sql_cat.has_units()
    table = next(t for t, entry in sql_cat.tables.items() if entry.column("hours") is not None and entry.column("priority") is not None)
    guard(f"SELECT SUM(hours) + SUM(priority) AS x FROM {table}", sql_cat, resolver, acl=[])  # no units declared: not checked
