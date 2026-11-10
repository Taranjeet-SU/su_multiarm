"""M4 guard: the enforced join allowlist (unified merge plan §6.8; synthetic F0 data).

On a setup catalog (``join_policy: enforced``) every way two tables meet on values — an ``ON``
conjunct, a correlated equality of a subquery, an ``IN (SELECT x ...)`` / ``= (SELECT x ...)`` —
must be an equality of bare columns along a declared path (identity/link tables included), else
``join_not_declared`` (repairable: the model is told the allowed paths). A projection catalog keeps
``join_policy: shown`` and the guard behaves exactly as before (joins on other columns accepted).
"""

from __future__ import annotations

import pytest

pytest.importorskip("sqlglot")

import sql_setup_support as sss  # noqa: E402
import sql_surface_support as ss  # noqa: E402
from su_multi_arm.decision.values import ValueResolver  # noqa: E402
from su_multi_arm.sql_surface.catalog import build_sql_catalog  # noqa: E402
from su_multi_arm.sql_surface.guard import SqlRefusal, guard  # noqa: E402
from su_multi_arm.sql_surface.setup import SetupValues, SqlSurfaceSetup, execute_setup, setup_catalog  # noqa: E402


@pytest.fixture(scope="module")
def surface(tmp_path_factory: pytest.TempPathFactory) -> SqlSurfaceSetup:
    return sss.build(tmp_path_factory.mktemp("m4_joins"))


def run(setup: SqlSurfaceSetup, sql: str, **kw):
    with SetupValues(setup, []) as values:
        guarded = guard(sql, setup_catalog(setup, [], values), values, acl=[], **kw)
    return guarded, execute_setup(setup, guarded, timeout_ms=5000)


def refused(setup: SqlSurfaceSetup, sql: str, **kw) -> SqlRefusal:
    with SetupValues(setup, []) as values, pytest.raises(SqlRefusal) as info:
        guard(sql, setup_catalog(setup, [], values), values, acl=[], **kw)
    return info.value


@pytest.mark.parametrize(
    ("sql", "paths"),
    [
        ("SELECT c.name, COUNT(*) AS n FROM ticket t JOIN customer c ON t.customer_id = c.id GROUP BY c.name", ["ticket.customer_id = customer.id"]),
        ("SELECT c.name FROM customer c JOIN ticket t ON c.id = t.customer_id", ["ticket.customer_id = customer.id"]),  # either orientation
        ("SELECT t.id, i.id FROM link l JOIN ticket t ON l.subject_id = t.id JOIN issue_day i ON l.object_id = i.id",
         ["link.subject_id = ticket.id", "link.object_id = issue_day.id"]),
        ("SELECT d.id, COUNT(*) AS n FROM search_click k JOIN doc d ON k.doc_id = d.id GROUP BY d.id", ["search_click.doc_id = doc.id"]),
        ("SELECT q.query_id, k.rank FROM search_click k LEFT JOIN search_query_day q ON k.query_day_id = q.id", ["search_click.query_day_id = search_query_day.id"]),
        ("SELECT a.raw, c.name FROM customer_alias a JOIN customer c ON a.customer_id = c.id WHERE a.status = 'matched'", ["customer_alias.customer_id = customer.id"]),
        ("SELECT c.name, COUNT(*) AS n FROM issue_day i JOIN customer c ON i.customer_id = c.id GROUP BY c.name", ["issue_day.customer_id = customer.id"]),
        # identity/link hop: tickets and log episodes of one customer
        ("SELECT t.id, i.id FROM ticket t JOIN customer c ON t.customer_id = c.id JOIN issue_day i ON i.customer_id = c.id",
         ["ticket.customer_id = customer.id", "issue_day.customer_id = customer.id"]),
        # a derived table along a declared path (traced through its projection)
        ("SELECT s.cid, c.name FROM (SELECT customer_id AS cid FROM ticket WHERE status = 'open') AS s JOIN customer c ON s.cid = c.id", ["ticket.customer_id = customer.id"]),
        # correlated equality / semi-joins along declared paths
        ("SELECT t.id FROM ticket t WHERE EXISTS (SELECT 1 FROM link l WHERE l.subject_id = t.id AND l.status = 'accepted')", ["link.subject_id = ticket.id"]),
        ("SELECT t.id, (SELECT COUNT(*) FROM link l WHERE l.subject_id = t.id) AS links FROM ticket t", ["link.subject_id = ticket.id"]),
        ("SELECT t.id FROM ticket t WHERE t.id IN (SELECT l.subject_id FROM link l WHERE l.tier = 'A')", ["link.subject_id = ticket.id"]),
        # not joins: a self identity join, a value compared with a computed measure, a same-column filter
        ("SELECT a.id FROM ticket a JOIN ticket b ON a.id = b.id", []),
        ("SELECT id FROM issue_day WHERE occurrences = (SELECT MAX(occurrences) FROM issue_day)", []),
        ("SELECT id FROM ticket WHERE id IN (SELECT id FROM ticket WHERE status = 'open')", []),
    ],
)
def test_declared_paths_are_accepted_and_reported(surface, sql, paths):
    guarded, out = run(surface, sql)
    assert guarded.join_paths == paths
    assert out["join_paths"] == paths


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT t.id FROM ticket t JOIN doc d ON t.service = d.service",  # shared column name, no path
        "SELECT t.id FROM ticket t JOIN customer c ON t.account = c.name",  # raw name = customer name: identity must go through the registry
        "SELECT t.id FROM ticket t JOIN tracker_issue r ON t.customer_id = r.customer_id",  # both lead to customer, but no direct path
        "SELECT i.id FROM issue_day i JOIN ticket t ON i.log_tenant = t.account",  # tenant = organization by text
        "SELECT t.id FROM ticket t JOIN customer c ON t.customer_id = c.id AND c.name = 'Brightmoor'",  # a filter in ON
        "SELECT t.id FROM ticket t JOIN customer c ON t.customer_id = c.id OR t.account = c.name",
        "SELECT t.id FROM ticket t JOIN customer c ON lower(t.customer_id) = lower(c.id)",
        "SELECT t.id FROM ticket t JOIN customer c ON t.customer_id > c.id",
        "SELECT t.id FROM ticket t JOIN link l ON l.object_id = t.id",  # link.object_id is not a ticket path
        "SELECT a.id FROM ticket a JOIN ticket b ON a.service = b.service",  # self-join on a non-key column
        "SELECT t.id FROM ticket t WHERE EXISTS (SELECT 1 FROM doc d WHERE d.service = t.service)",  # correlated, undeclared
        "SELECT t.id FROM ticket t WHERE EXISTS (SELECT 1 FROM link l WHERE l.subject_id <> t.id)",  # correlated, not an equality
        "SELECT t.id, (SELECT MAX(d.title) FROM doc d WHERE d.service = t.service) AS x FROM ticket t",
        "SELECT t.id FROM ticket t WHERE t.account IN (SELECT c.name FROM customer c)",  # semi-join on text
        "SELECT t.id FROM ticket t WHERE t.customer_id = (SELECT r.customer_id FROM tracker_issue r WHERE r.status = 'open' LIMIT 1)",
        "SELECT s.svc FROM (SELECT service AS svc FROM ticket) AS s JOIN doc d ON s.svc = d.service",  # derived table, undeclared
    ],
)
def test_undeclared_joins_are_refused(surface, sql):
    error = refused(surface, sql)
    assert error.code == "join_not_declared" and error.kind == "repairable"
    assert "ticket.customer_id = customer.id" in error.allowed  # the model is told the allowed paths


def test_a_declared_join_executes_exact_counts(surface):
    _, out = run(surface, "SELECT c.name, COUNT(*) AS n FROM ticket t JOIN customer c ON t.customer_id = c.id GROUP BY c.name ORDER BY n DESC, c.name")
    assert out["rows"] == [("Alderbank Ltd", 4), ("Brightmoor", 3), ("Cobaltfield Systems", 2)]
    _, out = run(surface, "SELECT l.id, t.status FROM link l JOIN ticket t ON l.subject_id = t.id WHERE l.status = 'accepted' ORDER BY l.id")
    assert [r[0] for r in out["rows"]] == ["L01", "L06"]


def test_using_is_checked_as_its_on_equality(surface):
    """``USING`` expands to ``ON a.c = b.c``: accepted only along a declared path."""
    assert refused(surface, "SELECT t.id FROM ticket t JOIN doc d USING (service)").code == "join_not_declared"
    assert refused(surface, "SELECT t.id FROM ticket t JOIN tracker_issue r USING (customer_id)").code == "join_not_declared"


def test_cross_and_comma_joins_stay_refused_first(surface):
    assert refused(surface, "SELECT t.id FROM ticket t, customer c").code == "join_not_allowed"
    assert refused(surface, "SELECT t.id FROM ticket t CROSS JOIN customer c").code == "join_not_allowed"


def test_a_setup_without_join_paths_refuses_every_join(surface):
    data = surface.model_dump(mode="json")
    data["joins"] = []
    bare = SqlSurfaceSetup.model_validate(data)
    assert refused(bare, "SELECT c.name FROM ticket t JOIN customer c ON t.customer_id = c.id").code == "join_not_declared"
    assert "- none: every question reads one table" in setup_catalog(bare, []).render()


def test_a_projection_catalog_keeps_accepting_undeclared_joins():
    """``join_policy: shown`` (every projection catalog): the existing behaviour, unchanged."""
    world = ss.world()
    arm = ss.make_arm(world)
    ctx = ss.context()
    catalog = arm.catalog_for(ctx)
    resolver = ValueResolver(arm.schema, arm._records(), ctx.generation, [])
    sql_cat = build_sql_catalog(catalog, arm.schema, resolver, [])
    assert sql_cat.join_policy == "shown"
    guarded = guard("SELECT t.id FROM ticket t JOIN asset a ON a.module = t.module", sql_cat, resolver, acl=[])
    assert guarded.joins == 1 and guarded.join_paths == []
    guarded = guard("SELECT c.id FROM comment c JOIN ticket t ON c.parent_id = t.id", sql_cat, resolver, acl=[])
    assert guarded.join_paths == []  # not computed under ``shown``
