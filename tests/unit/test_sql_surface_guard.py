"""SQL-surface guard contract (``sql_surface/guard.py``): one test per refusal class and binding rule.

Synthetic ``typed_support`` domain; the guard runs against the principal ``t_alpha``'s catalog and
``ValueResolver`` (observed values are ACL-scoped). No statement here is executed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("sqlglot")

from su_multi_arm.sql_surface import guard as guard_module  # noqa: E402
from su_multi_arm.sql_surface.executor import sql_catalog  # noqa: E402
from su_multi_arm.sql_surface.guard import SqlRefusal, guard  # noqa: E402

import sql_surface_support as ss  # noqa: E402

ALPHA = ss.tenant("t_alpha")


@pytest.fixture(scope="module")
def setting():
    world = ss.world()
    arm = ss.make_arm(world)
    _, catalog, resolver = sql_catalog(arm, ss.context(), ALPHA)
    return catalog, resolver


def run(setting, sql, **kwargs):
    catalog, resolver = setting
    return guard(sql, catalog, resolver, acl=kwargs.pop("acl", ALPHA), **kwargs)


def refused(setting, sql, **kwargs) -> SqlRefusal:
    with pytest.raises(SqlRefusal) as info:
        run(setting, sql, **kwargs)
    return info.value


# --- text and statement -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql, code",
    [
        ("SELECT id FROM ticket -- note", "comment"),
        ("SELECT /* hidden */ id FROM ticket", "comment"),
        ("SELECT /*+ hint */ id FROM ticket", "comment"),
        ("SELECT id FROM ticket; SELECT id FROM comment", "multiple_statements"),
        ("SELECT id FROM ticket; DROP TABLE ticket", "multiple_statements"),
        ("ATTACH DATABASE 'other.sqlite' AS other", "not_select"),
        ("PRAGMA table_info(ticket)", "not_select"),
        ("INSERT INTO ticket (id) VALUES ('x')", "not_select"),
        ("DELETE FROM ticket", "not_select"),
        ("UPDATE ticket SET status = 'open'", "not_select"),
        ("DROP TABLE ticket", "not_select"),
        ("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT n FROM r", "recursive_cte"),
        ("SELECT name FROM sqlite_master", "system_object"),
        ("SELECT * FROM pragma_table_info('ticket')", "system_object"),
        ("SELECT * FROM json_each('[1]')", "system_object"),
        ("SELECT load_extension('evil')", "system_object"),
        ("SELECT id FROM ticket\x00", "nul_byte"),
    ],
)
def test_security_refusals_are_final(setting, sql, code):
    error = refused(setting, sql)
    assert error.code == code and error.kind == "security" and error.reason == "sql_refused"


@pytest.mark.parametrize(
    "sql, code",
    [
        ("SELECT id FROM ticket UNION SELECT id FROM comment", "set_operation"),
        ("SELECT id FROM ticket UNION ALL SELECT id FROM ticket WHERE tenant = 't_alpha'", "set_operation"),
        ("SELECT id FROM ticket INTERSECT SELECT parent_id FROM comment", "set_operation"),
        ("SELECT id FROM main.ticket", "qualified_name"),
        ("SELECT id FROM db.main.ticket", "qualified_name"),
        ("SELECT id FROM tickets", "unknown_table"),
        ("SELECT nope FROM ticket", "unknown_column"),
        ("SELECT _rejected FROM ticket", "unknown_column"),
        ("SELECT rowid FROM ticket", "unknown_column"),
        ('SELECT id FROM ticket WHERE status = "open"', "unknown_column"),
        ("SELECT id FROM ticket WHERE status = ?", "placeholder"),
        ("SELECT id FROM ticket WHERE status = :s", "placeholder"),
        ("SELECT row_number() OVER (ORDER BY id) FROM ticket", "construct_not_allowed"),
        ("SELECT x'41' FROM ticket", "construct_not_allowed"),
        ("SELECT random() FROM ticket", "function_not_allowed"),
        ("SELECT printf('%d', priority) FROM ticket", "function_not_allowed"),
        ("SELECT t.id FROM ticket t, comment c", "join_not_allowed"),
        ("SELECT t.id FROM ticket t CROSS JOIN comment c", "join_not_allowed"),
        ("SELECT t.id FROM ticket t NATURAL JOIN comment c", "join_not_allowed"),
        ("SELECT t.id FROM ticket t RIGHT JOIN comment c ON c.parent_id = t.id", "join_not_allowed"),
        ("SELECT id FROM ticket LIMIT 1 + 1", "limit_invalid"),
        ("SELECT 't_beta' AS __sacl_0, id FROM ticket", "construct_not_allowed"),  # hidden row-check names are reserved
        ("SELECT __x.id FROM ticket __x", "construct_not_allowed"),
        ("WITH ticket AS (SELECT id FROM comment) SELECT id FROM ticket", "cte_shadows_table"),
        ("SELEC id FROM ticket", "parse_error"),
        ("SELECT id FROM ticket WHERE (", "parse_error"),
    ],
)
def test_repairable_refusals(setting, sql, code):
    error = refused(setting, sql)
    assert error.code == code and error.kind == "repairable" and error.reason == "sql_refused"


def test_size_join_and_subquery_limits(setting):
    assert refused(setting, "SELECT id FROM ticket WHERE " + " OR ".join(["priority = 1"] * 400), max_sql_chars=1000).code == "too_long"
    joins = "SELECT c.id FROM comment c JOIN ticket t1 ON c.parent_id = t1.id JOIN ticket t2 ON c.parent_id = t2.id"
    assert refused(setting, joins, max_joins=1).code == "too_complex"
    nested = "SELECT id FROM ticket WHERE id IN (SELECT parent_id FROM comment WHERE id IN (SELECT id FROM comment))"
    assert refused(setting, nested, max_subqueries=1).code == "too_complex"


def test_unknown_tables_list_the_catalog(setting):
    error = refused(setting, "SELECT id FROM tickets")
    assert error.allowed == ["asset", "comment", "ticket"]


def test_sqlglot_missing_is_a_clean_refusal(setting, monkeypatch):
    monkeypatch.setattr(guard_module, "sqlglot", None)
    error = refused(setting, "SELECT id FROM ticket")
    assert error.code == "sqlglot_missing" and error.kind == "unavailable" and "su-multi-arm[sql-surface]" in error.detail
    assert guard_module.available() is False
    arm = ss.make_arm(ss.world())
    codes = {d.code for d in arm.validate(None)}
    assert "extra_missing" in codes  # engine construction aborts with a clear message
    assert "extra_missing" not in {d.code for d in ss.make_arm(ss.world(), mode="ir").validate(None)}


# --- values ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM ticket WHERE status = 'open'",
        "SELECT id FROM ticket WHERE module IN ('Billing', 'Search')",
        "SELECT id FROM ticket WHERE lower(module) = 'billing'",
        "SELECT id FROM ticket WHERE id = 'tk-1'",
        "SELECT id FROM comment WHERE parent_id = 'tk-2'",
        "SELECT id FROM ticket WHERE priority >= 2 AND hours < 5.5",
        "SELECT id FROM ticket WHERE priority = '2'",
        "SELECT id FROM ticket WHERE opened_at >= '2024-03' AND opened_at < '2024-03-06'",
        "SELECT id FROM ticket WHERE opened_at = '2024-03-01T10:00:00.000000+00:00'",
        "SELECT id FROM ticket WHERE date(opened_at) = '2024-03-01'",
        "SELECT id FROM ticket WHERE strftime('%Y-%m', opened_at) = '2024-03'",
        "SELECT id FROM ticket WHERE title_text LIKE '%ticket%'",
        "SELECT id FROM ticket WHERE instr(title_text, 'ticket') > 0",
        "SELECT CASE status WHEN 'open' THEN 'active' ELSE 'other' END AS state FROM ticket",
        "SELECT id FROM ticket WHERE tenant <> 't_alpha'",
        "WITH x AS (SELECT lower(module) AS m FROM ticket) SELECT m FROM x WHERE m = 'search'",
        "SELECT id FROM ticket WHERE id IN (SELECT parent_id FROM comment WHERE visibility = 'private')",
        "SELECT id FROM ticket WHERE priority IS NULL",
    ],
)
def test_grounded_values_pass(setting, sql):
    guarded = run(setting, sql)
    assert guarded.checked or guarded.text_patterns or "IS NULL" in sql


@pytest.mark.parametrize(
    "sql, code",
    [
        ("SELECT id FROM ticket WHERE status = 'archived'", "value_not_observed"),
        ("SELECT id FROM ticket WHERE module = 'billing ops'", "value_not_observed"),  # no rule of value_match grounds it
        ("SELECT id FROM ticket WHERE module = 'Reports'", "value_not_observed"),  # t_beta's value, not observed for t_alpha
        ("SELECT id FROM ticket WHERE tenant = 't_canary'", "value_not_observed"),
        ("SELECT id FROM ticket WHERE id = 'tk-6'", "value_not_observed"),  # the canary's record
        ("SELECT id FROM comment WHERE parent_id = 'tk-404'", "value_not_observed"),
        ("SELECT id FROM ticket WHERE priority = 'high'", "type_mismatch"),
        ("SELECT id FROM ticket WHERE status = 1", "type_mismatch"),
        ("SELECT id FROM ticket WHERE status > 'a'", "type_mismatch"),
        ("SELECT id FROM ticket WHERE opened_at >= '03/01/2024'", "unparseable_bound"),
        ("SELECT id FROM ticket WHERE opened_at = '2024-03-01'", "unparseable_bound"),
        ("SELECT id FROM ticket WHERE opened_at > '2024-03-01 10:00'", "unparseable_bound"),
        ("SELECT id FROM ticket WHERE coalesce(status, 'none') = 'open'", "ungroundable_value"),
        ("SELECT id FROM ticket WHERE status || '' = 'open'", "ungroundable_value"),
        ("SELECT id FROM ticket WHERE 'open' = 'open'", "ungroundable_value"),
        ("SELECT id FROM ticket WHERE status IN (SELECT 'archived')", "ungroundable_value"),
        ("SELECT id FROM ticket WHERE lower(title_text) = 'ticket 1'", "ungroundable_value"),
    ],
)
def test_unknown_or_ill_typed_values_are_explicit_unsupported(setting, sql, code):
    error = refused(setting, sql)
    assert error.code == code and error.kind == "value"
    assert error.reason in {"value_not_observed", "type_mismatch", "unparseable_bound"}


def test_a_case_variant_is_rewritten_to_the_stored_value_and_recorded(setting):
    # 2026-11-09 (B5): replaced the "near miss is refused" row; the documented normalization
    # (``decision/value_match.py``) executes the stored value and records what was written
    guarded = run(setting, "SELECT id FROM ticket WHERE module = 'billing'")
    assert "Billing" in guarded.params.values() and "billing" not in guarded.params.values()
    assert guarded.checked[0] == {"column": "ticket.module", "op": "eq", "value": "Billing", "grounded_by": "observed", "as_written": "billing", "rule": "casefold", "filter": "positive"}
    error = refused(setting, "SELECT id FROM ticket WHERE module = 'billing ops'")
    assert error.allowed == ["Billing", "Search"]


def test_like_is_only_offered_on_free_text_columns(setting):
    error = refused(setting, "SELECT id FROM ticket WHERE module LIKE '%bill%'")
    assert error.code == "text_match_not_allowed" and error.kind == "repairable"
    assert refused(setting, "SELECT id FROM ticket WHERE instr(module, 'Bill') > 0").code == "text_match_not_allowed"
    guarded = run(setting, "SELECT id FROM ticket WHERE title_text LIKE '%canary%'")
    assert guarded.text_patterns == [{"column": "ticket.title_text", "op": "like", "pattern": "%canary%", "filter": "positive"}]


# --- binding ------------------------------------------------------------------------------------


def test_every_literal_is_a_parameter_and_the_acl_is_bound_into_every_table(setting):
    guarded = run(setting, "SELECT t.id, c.visibility FROM comment c JOIN ticket t ON c.parent_id = t.id WHERE c.visibility = 'private' AND t.status = 'open'")
    assert "'private'" not in guarded.sql and "'open'" not in guarded.sql and "t_alpha" not in guarded.sql
    assert guarded.sql.count('"tenant" IN (:acl_0_0)') == 2
    assert guarded.params["acl_0_0"] == "t_alpha" and guarded.lineage_params["acl_0_0"] == "<acl>"
    assert {"private", "open"} <= set(guarded.params.values())
    assert guarded.shape == "rows" and guarded.primary == "c" and set(guarded.hidden.values()) == {("c", "id"), ("c", "acl"), ("t", "id"), ("t", "acl")}
    assert guarded.acl_applied and len(guarded.fingerprint) == 64


@pytest.mark.parametrize("sql", ["SELECT ID FROM TICKET", 'SELECT "ID" FROM "TICKET"', "select id from Ticket T where T.STATUS = 'open'", 'WITH X AS (SELECT id FROM "Comment") SELECT id FROM x'])
def test_identifier_case_variants_are_bound_like_any_other(setting, sql):
    guarded = run(setting, sql)
    assert guarded.sql.count('"tenant" IN (:acl_0_0)') == 1 and len(guarded.tables) == 1


def test_the_acl_is_bound_inside_subqueries_and_ctes(setting):
    guarded = run(setting, "WITH x AS (SELECT id FROM comment) SELECT id FROM ticket WHERE id IN (SELECT parent_id FROM comment) OR EXISTS (SELECT 1 FROM x)")
    assert guarded.sql.count('"tenant" IN (:acl_0_0)') == 3


def test_test_hook_disables_layer_one_only(setting):
    guarded = run(setting, "SELECT id FROM ticket", inject_acl=False)
    assert "acl_0_0" not in guarded.sql and guarded.acl_applied is False
    assert guarded.params == {"limit": 201}


def test_request_filters_are_bound_to_every_table_that_has_the_column(setting):
    guarded = run(setting, "SELECT t.id FROM comment c JOIN ticket t ON c.parent_id = t.id", filters={"visibility": "private"})
    assert guarded.sql.count('"visibility" = :f_0') == 1 and guarded.params["f_0"] == "private"
    casefold = run(setting, "SELECT id FROM ticket", filters={"module": "billing"})
    assert 'LOWER("module") = LOWER(:f_0)' in casefold.sql
    error = refused(setting, "SELECT id FROM asset", filters={"visibility": "private"})
    assert error.code == "filter_not_applicable" and error.reason == "filter_not_applicable"


def test_limits_are_enforced(setting):
    assert run(setting, "SELECT id FROM ticket").params["limit"] == 201  # the guard's default row_limit is 200
    assert run(setting, "SELECT id FROM ticket", row_limit=10).params["limit"] == 11
    small = run(setting, "SELECT id FROM ticket LIMIT 3", row_limit=10)
    assert small.params["limit"] == 3 and small.limit == 3
    big = run(setting, "SELECT id FROM ticket LIMIT 500", row_limit=10)
    assert big.params["limit"] == 11 and big.limit == 10
    assert big.sql.endswith("LIMIT :limit")


def test_aggregates_and_distinct_are_derived_shapes(setting):
    for sql in ("SELECT count(*) FROM ticket", "SELECT status, count(*) FROM ticket GROUP BY status", "SELECT DISTINCT status FROM ticket", "SELECT total(hours) FROM ticket", "WITH x AS (SELECT id FROM ticket) SELECT id FROM x"):
        guarded = run(setting, sql)
        assert guarded.shape == "derived" and guarded.hidden == {}
    rows = run(setting, "SELECT id, status FROM ticket")
    assert rows.shape == "rows" and rows.citations == [[("ticket", "ticket", "id")], [("ticket", "ticket", "status")]]


def test_the_fingerprint_depends_on_the_acl(setting):
    catalog, resolver = setting
    first = guard("SELECT id FROM ticket", catalog, resolver, acl=ALPHA)
    second = guard("SELECT id FROM ticket", catalog.model_copy(update={"acl_key": "other", "fingerprint": "f" * 64}), resolver, acl=ss.tenant("t_beta"))
    assert first.fingerprint != second.fingerprint


def test_rendered_sql_is_one_comment_free_select(setting):
    guarded = run(setting, "SELECT id, 'a -- not a comment' AS label FROM ticket")
    assert "--" not in guarded.sql and guarded.params["p_1"] == "a -- not a comment"


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT count(*) FROM ticket WHERE module = 'Billing' AND status = 'open'", {"Billing": "positive", "open": "positive"}),
        ("SELECT count(*) FROM ticket WHERE (module = 'Billing')", {"Billing": "positive"}),
        ("SELECT count(*) FROM ticket WHERE module <> 'Billing'", {"Billing": "negated"}),
        ("SELECT count(*) FROM ticket WHERE module NOT IN ('Billing', 'Search')", {"Billing": "negated", "Search": "negated"}),
        ("SELECT count(*) FROM ticket WHERE NOT (module <> 'Billing')", {"Billing": "positive"}),
        ("SELECT count(*) FROM ticket WHERE module = 'Billing' OR 1 = 1", {"Billing": "other"}),
        # a conditional measure's condition is a share numerator (2026-11-09 F4; route.py accepts it
        # only for a share question), a multi-branch CASE stays "other"
        ("SELECT sum(CASE WHEN module = 'Billing' THEN 1 ELSE 0 END) FROM ticket", {"Billing": "numerator"}),
        ("SELECT sum(CASE WHEN module = 'Billing' THEN 1 WHEN module = 'Search' THEN 2 ELSE 0 END) FROM ticket", {"Billing": "other", "Search": "other"}),
        # 2026-11-09 (B5): a derived table the outer statement reads as its only source filters every
        # row it reads (``_effective_roots``); beside a join it stays "other"
        ("SELECT count(*) FROM (SELECT * FROM ticket WHERE module = 'Billing') AS t", {"Billing": "positive"}),
        ("SELECT count(*) FROM (SELECT * FROM ticket WHERE module = 'Billing') AS t JOIN asset AS a ON a.module = t.module", {"Billing": "other"}),
        ("SELECT count(*) FROM ticket WHERE id IN (SELECT parent_id FROM comment WHERE visibility = 'private')", {"private": "other"}),
        ("SELECT module, count(*) FROM ticket GROUP BY module HAVING module = 'Billing'", {"Billing": "positive"}),
    ],
)
def test_checked_values_record_their_filter_position(setting, sql, expected):
    """The executed-SQL qualifier check (``route.py``) counts only conjuncts of the outermost
    WHERE/HAVING; anything under OR/CASE/derived tables/subqueries is ``other``."""
    guarded = run(setting, sql)
    assert {c["value"]: c["filter"] for c in guarded.checked} == expected
