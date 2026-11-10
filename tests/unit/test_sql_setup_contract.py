"""M4 guard: the multi-source SQL-surface setup contract (unified merge plan §6.8; synthetic F0 data).

* the connection attaches every file read-only and creates every TEMP view **before** ``query_only``
  and the SELECT/READ authorizer; afterwards ATTACH, CREATE, writes, PRAGMA, direct reads of the
  attached tables and of ``sqlite_*`` are denied by the connection itself;
* the model's SQL still cannot ATTACH, CREATE, UNION, qualify a schema or name a system object
  (guard, unchanged);
* pins are injected into every view (superseded slices, later history, other registry/link
  versions and snapshots are invisible); a view part without a pin is not a valid setup;
* setup failures fail closed (missing file, declared column absent);
* ACL at execution is unchanged: bound into every table, ``acl_not_applicable`` on a table without
  the access column, the returned-row check on a tenant's row-level result;
* the catalog lists the per-source tables, units and the allowed joins for the SQL writer, and its
  fingerprint covers pins and the ACL key; a projection catalog's fingerprint is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

pytest.importorskip("sqlglot")

import sql_setup_support as sss  # noqa: E402
import sql_surface_support as ss  # noqa: E402
from su_multi_arm.core.errors import BackendError  # noqa: E402
from su_multi_arm.sql_surface import setup as surface_setup  # noqa: E402
from su_multi_arm.sql_surface.catalog import build_sql_catalog  # noqa: E402
from su_multi_arm.sql_surface.guard import SqlRefusal, guard  # noqa: E402
from su_multi_arm.sql_surface.setup import (  # noqa: E402
    SetupValues,
    SqlSetupError,
    SqlSurfaceSetup,
    execute_setup,
    open_surface,
    setup_catalog,
    tenant_acl,
)


@pytest.fixture(scope="module")
def surface(tmp_path_factory: pytest.TempPathFactory) -> SqlSurfaceSetup:
    return sss.build(tmp_path_factory.mktemp("m4_setup"))


def run(setup: SqlSurfaceSetup, sql: str, acl: list | None = None, **kw):
    acl = acl or []
    with SetupValues(setup, acl) as values:
        catalog = setup_catalog(setup, acl, values)
        guarded = guard(sql, catalog, values, acl=acl, **kw)
    tenant = acl[0].value if acl else None
    return guarded, execute_setup(setup, guarded, timeout_ms=5000, tenant=tenant)


def refused(setup: SqlSurfaceSetup, sql: str, acl: list | None = None, **kw) -> SqlRefusal:
    acl = acl or []
    with SetupValues(setup, acl) as values, pytest.raises(SqlRefusal) as info:
        guard(sql, setup_catalog(setup, acl, values), values, acl=acl, **kw)
    return info.value


def digests(setup: SqlSurfaceSetup) -> dict[str, str]:
    return {a.alias: hashlib.sha256(Path(a.path).read_bytes()).hexdigest() for a in setup.attachments}


# --- order: attach and views before the authorizer --------------------------------------------------


class Recorder:
    """Delegates to a real connection and records the setup steps in order."""

    def __init__(self, conn: sqlite3.Connection, log: list[str]):
        self._conn, self._log = conn, log

    def execute(self, sql: str, *args):
        self._log.append(sql.split()[0].upper() + (" " + sql.split()[1].upper() if sql.upper().startswith(("CREATE", "PRAGMA")) else ""))
        return self._conn.execute(sql, *args)

    def set_authorizer(self, fn):
        self._log.append("AUTHORIZER")
        return self._conn.set_authorizer(fn)

    def set_progress_handler(self, fn, n):
        self._log.append("DEADLINE")
        return self._conn.set_progress_handler(fn, n)

    def close(self):
        return self._conn.close()


def test_attach_and_views_come_before_query_only_and_the_authorizer(surface, monkeypatch):
    log: list[str] = []
    real = sqlite3.connect
    monkeypatch.setattr(surface_setup.sqlite3, "connect", lambda *a, **k: Recorder(real(*a, **k), log))
    conn = open_surface(surface, 2000)
    conn.close()
    authorizer = log.index("AUTHORIZER")
    assert log.count("ATTACH") == len(surface.attachments) and log.count("CREATE TEMP") == len(surface.views)
    last_setup = max(i for i, step in enumerate(log) if step in {"ATTACH", "CREATE TEMP"})
    assert last_setup < log.index("PRAGMA QUERY_ONLY=1") < authorizer < log.index("DEADLINE")
    assert all(step == "SELECT" for step in log[authorizer + 1 :] if step != "DEADLINE")


def test_the_connection_refuses_attach_create_write_pragma_and_direct_reads(surface, tmp_path):
    before = digests(surface)
    conn = open_surface(surface, 2000)
    try:
        assert conn.execute('SELECT COUNT(*) FROM "ticket"').fetchone()[0] == 12  # the views are readable
        other = tmp_path / "other.sqlite"
        sqlite3.connect(other).close()
        for sql in (
            f"ATTACH DATABASE '{other}' AS other",
            'CREATE TEMP VIEW "x" AS SELECT 1',
            'CREATE TEMP TABLE "x" (a)',
            "INSERT INTO support.tickets (doc_id) VALUES ('ticket:x')",
            "DELETE FROM support.tickets",
            'DROP VIEW "ticket"',
            "DETACH DATABASE support",
            "PRAGMA query_only=0",
            "SELECT * FROM support.tickets",  # the attached table, outside a view
            "SELECT member FROM support.tickets",
            "SELECT name FROM sqlite_temp_master",
            "SELECT name FROM support.sqlite_master",
            "SELECT load_extension('x')",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(sql).fetchall()
    finally:
        conn.close()
    assert digests(surface) == before


def test_attachments_are_read_only_even_without_the_authorizer(surface):
    """Defence in depth: the files are attached with ``mode=ro``."""
    db = sqlite3.connect("file::memory:", uri=True)
    try:
        db.execute("ATTACH DATABASE ? AS s", (surface_setup._ro_uri(surface.attachments[0].path),))
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM s.tickets")
    finally:
        db.close()


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("ATTACH DATABASE 'x.sqlite' AS x", "not_select"),
        ("CREATE TEMP VIEW t2 AS SELECT * FROM ticket", "not_select"),
        ("SELECT id FROM ticket UNION SELECT id FROM doc", "set_operation"),
        ("SELECT id FROM ticket UNION ALL SELECT id FROM tracker_issue", "set_operation"),
        ("SELECT t.id FROM ticket t WHERE t.id IN (SELECT id FROM doc UNION SELECT id FROM link)", "set_operation"),
        ("SELECT doc_id FROM support.tickets", "qualified_name"),
        ("SELECT doc_id FROM tickets", "unknown_table"),
        ("SELECT * FROM issue_day_slice", "unknown_table"),
        ("SELECT name FROM sqlite_temp_master", "system_object"),
        ("SELECT * FROM temp.ticket", "qualified_name"),
        ("SELECT member FROM ticket", "unknown_column"),  # the pin column is not a view column
        ("PRAGMA table_info(ticket)", "not_select"),
        ("SELECT 1; ATTACH DATABASE 'x' AS y", "multiple_statements"),
    ],
)
def test_model_sql_cannot_attach_create_union_or_reach_unlisted_objects(surface, sql, code):
    assert refused(surface, sql).code == code


# --- pins ------------------------------------------------------------------------------------------


def test_pins_hide_later_history_superseded_slices_and_other_versions(surface):
    _, out = run(surface, "SELECT COUNT(*) AS n FROM ticket")
    assert out["rows"][0][0] == 12  # the seq-2 ticket version and ticket:9999 are past the pin
    _, out = run(surface, "SELECT status FROM ticket WHERE id = 'ticket:1001'")
    assert [r[0] for r in out["rows"]] == ["solved"]
    with pytest.raises(SqlRefusal) as info:
        run(surface, "SELECT id FROM ticket WHERE id = 'ticket:9999'")
    assert info.value.code == "value_not_observed"  # grounding reads the pinned view too
    assert refused(surface, "SELECT COUNT(*) FROM tracker_issue WHERE status = 'closed'").code == "value_not_observed"  # only the older snapshot has it
    _, out = run(surface, "SELECT COUNT(*) AS n FROM tracker_issue")
    assert out["rows"][0][0] == 4  # the older snapshot row is not visible
    _, out = run(surface, "SELECT COUNT(*) AS n FROM issue_day")
    assert out["rows"][0][0] == 22  # 10 archive episodes + 12 in the current slices; superseded slices hidden
    _, out = run(surface, "SELECT COUNT(*) AS n FROM customer")
    assert out["rows"][0][0] == 6  # the fixture-r0 customer is another registry version
    _, out = run(surface, "SELECT COUNT(*) AS n FROM link")
    assert out["rows"][0][0] == 12  # the fixture-l0 link is another link version
    assert out["pins"] == {"member_set": "support,tracker,logs,analytics", "history_seq": 1, "registry_version": "fixture-r1", "link_version": "fixture-l1"}


def test_every_view_part_is_pinned_and_pins_are_typed(surface):
    data = surface.model_dump(mode="json")
    data["views"][0]["parts"][0]["pins"] = []
    with pytest.raises(ValidationError, match="at least 1"):
        SqlSurfaceSetup.model_validate(data)
    for pin, message in (
        ({"label": "history_seq", "column": "seq", "op": "le", "value": "1"}, "integer"),
        ({"label": "slice", "column": "slice_id", "op": "in", "value": []}, "non-empty"),
        ({"label": "slice", "column": "slice_id", "op": "eq", "value": ["a"]}, "one value"),
        ({"label": "slice", "column": "slice_id", "op": "eq", "value": True}, "valid integer"),  # strict: no bool coercion
        ({"label": "slice", "column": "slice_id", "op": "eq", "value": 1.5}, "valid integer"),
        ({"label": "slice", "column": "slice id", "op": "eq", "value": "a"}, "pattern"),
    ):
        data = surface.model_dump(mode="json")
        data["views"][0]["parts"][0]["pins"] = [pin]
        with pytest.raises(ValidationError, match=message):
            SqlSurfaceSetup.model_validate(data)


def test_pin_values_are_literals_not_sql(surface):
    data = surface.model_dump(mode="json")
    data["views"][0]["parts"][0]["pins"] = [{"label": "member_set", "column": "member", "op": "eq", "value": "support' OR '1'='1"}]
    hostile = SqlSurfaceSetup.model_validate(data)
    assert "'support'' OR ''1''=''1'" in hostile.view_sql(hostile.views[0])
    conn = open_surface(hostile, 2000)
    try:
        assert conn.execute('SELECT COUNT(*) FROM "ticket"').fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["attachments"][0].update(alias="main"), "reserved"),
        (lambda d: d["attachments"][0].update(alias="temp"), "reserved"),
        (lambda d: d["attachments"][0].update(path="relative.sqlite"), "absolute"),
        (lambda d: d["views"][0].update(name="support"), "overlap"),
        (lambda d: d["views"][0].update(name="sqlite_x"), "reserved"),
        (lambda d: d["views"][0]["columns"].pop(0), "key column"),
        (lambda d: d["views"][0]["parts"][0].update(table='tickets"; DROP TABLE x; --'), "pattern"),
        (lambda d: d["views"][0]["parts"][0].update(attachment="nowhere"), "unknown attachment"),
        (lambda d: d["joins"].append({"left": "ticket", "left_column": "nope", "right": "customer", "right_column": "id", "description": "x"}), "not a declared column"),
        (lambda d: d["joins"].append({"left": "ticket", "left_column": "resolution_hours", "right": "customer", "right_column": "id", "description": "x"}), "incompatible"),
        (lambda d: d["joins"].append(dict(d["joins"][0])), "duplicate"),
        (lambda d: d["views"][0]["columns"][2].update(unit="hours"), "numeric measure"),
    ],
)
def test_setup_validation_refuses_unsafe_or_incoherent_declarations(surface, change, message):
    data = json.loads(json.dumps(surface.model_dump(mode="json")))
    change(data)
    with pytest.raises(ValidationError, match=message):
        SqlSurfaceSetup.model_validate(data)


def test_setup_failures_fail_closed(surface, tmp_path):
    data = surface.model_dump(mode="json")
    data["attachments"][0]["path"] = str(tmp_path / "missing.sqlite")
    with pytest.raises(SqlSetupError) as info:
        open_surface(SqlSurfaceSetup.model_validate(data), 2000)
    assert info.value.code == "attachment_missing"
    data = surface.model_dump(mode="json")
    data["views"][0]["columns"].append({"name": "absent", "type": "string", "description": "Declared but not stored."})
    with pytest.raises(SqlSetupError) as info:
        open_surface(SqlSurfaceSetup.model_validate(data), 2000)
    assert info.value.code == "view_mismatch" and "absent" in info.value.detail
    data = surface.model_dump(mode="json")
    data["views"][0]["parts"][0]["pins"][0]["column"] = "no_such_pin_column"
    with pytest.raises(SqlSetupError) as info:
        open_surface(SqlSurfaceSetup.model_validate(data), 2000)
    assert info.value.code == "view_mismatch" and "no_such_pin_column" in info.value.detail
    data = surface.model_dump(mode="json")
    data["views"][0]["parts"][0]["table"] = "no_such_table"
    with pytest.raises(SqlSetupError) as info:
        open_surface(SqlSurfaceSetup.model_validate(data), 2000)
    assert info.value.code == "view_mismatch"


# --- ACL at execution (unchanged) ---------------------------------------------------------------------


def test_a_tenant_sees_only_its_rows_in_every_table(surface):
    beta = tenant_acl("tenant", "t_beta")
    guarded, out = run(surface, "SELECT t.id, l.status FROM link l JOIN ticket t ON l.subject_id = t.id", beta)
    assert guarded.acl_applied and {r[0] for r in out["rows"]} == {"ticket:1003", "ticket:1012"}  # links whose subject is a t_beta ticket
    _, out = run(surface, "SELECT COUNT(*) AS n FROM ticket", beta)
    assert out["rows"][0][0] == 3
    _, out = run(surface, "SELECT COUNT(*) AS n FROM issue_day", tenant_acl("tenant", "t_nobody"))
    assert out["rows"][0][0] == 0


def test_a_tenant_cannot_ground_or_read_another_tenants_values(surface):
    beta = tenant_acl("tenant", "t_beta")
    error = refused(surface, "SELECT COUNT(*) FROM ticket WHERE account = 'Alderbank Ltd'", beta)
    assert error.code == "value_not_observed"
    error = refused(surface, "SELECT COUNT(*) FROM ticket WHERE id = 'ticket:1001'", beta)
    assert error.code == "value_not_observed"


def test_a_table_without_the_access_column_is_refused_for_a_tenant(surface):
    beta = tenant_acl("tenant", "t_beta")
    assert refused(surface, "SELECT name FROM customer", beta).code == "acl_not_applicable"
    assert refused(surface, "SELECT c.name FROM ticket t JOIN customer c ON t.customer_id = c.id", beta).code == "acl_not_applicable"
    _, out = run(surface, "SELECT name FROM customer ORDER BY name")  # all-rows principal (operator)
    assert len(out["rows"]) == 6


def test_the_returned_row_check_discards_a_foreign_row(surface):
    beta = tenant_acl("tenant", "t_beta")
    with SetupValues(surface, []) as values:
        guarded = guard("SELECT id, status FROM ticket", setup_catalog(surface, beta, values), values, acl=beta, inject_acl=False)
    assert not guarded.acl_applied
    with pytest.raises(BackendError, match="granted tenant"):
        execute_setup(surface, guarded, timeout_ms=2000, tenant="t_beta")


def test_request_filters_bind_per_table_and_are_grounded(surface):
    guarded, out = run(surface, "SELECT COUNT(*) AS n FROM ticket", filters={"service": "crawler"})
    assert out["rows"][0][0] == 5 and "f_0" in guarded.params
    assert refused(surface, "SELECT COUNT(*) FROM ticket", filters={"service": "nope"}).code == "value_not_observed"
    assert refused(surface, "SELECT COUNT(*) FROM ticket", filters={"title": "x"}).code == "filter_not_applicable"
    assert refused(surface, "SELECT COUNT(*) FROM link", filters={"service": "crawler"}).code == "filter_not_applicable"
    _, out = run(surface, "SELECT COUNT(*) AS n FROM ticket", filters={"status": "OPEN"})  # casefold_match column
    assert out["rows"][0][0] == 5


def test_grounding_rewrites_and_refuses_as_on_a_projection(surface):
    guarded, out = run(surface, "SELECT COUNT(*) AS n FROM ticket WHERE account = 'brightmoor'")
    assert out["rows"][0][0] == 2 and guarded.checked[0]["grounded_by"] == "observed"  # exact: 'Brightmoor' is another stored value
    assert refused(surface, "SELECT COUNT(*) FROM ticket WHERE account = 'BRIGHTMOOR'").code == "ambiguous_value"  # two stored spellings
    guarded, out = run(surface, "SELECT COUNT(*) AS n FROM ticket WHERE account = 'cobaltfield systems'")
    assert out["rows"][0][0] == 2 and guarded.checked[0]["as_written"] == "cobaltfield systems" and guarded.checked[0]["value"] == "Cobaltfield Systems"
    assert refused(surface, "SELECT COUNT(*) FROM ticket WHERE title = 'Fetch timeouts again'").code == "free_text_equality"
    assert refused(surface, "SELECT COUNT(*) FROM ticket WHERE service LIKE '%crawl%'").code == "text_match_not_allowed"
    _, out = run(surface, "SELECT COUNT(*) AS n FROM ticket WHERE title LIKE '%timeout%'")
    assert out["rows"][0][0] >= 1
    assert refused(surface, "SELECT COUNT(*) FROM ticket WHERE service = 'billing'").code == "value_not_observed"


# --- catalog for the SQL writer ----------------------------------------------------------------------


def test_the_catalog_lists_per_source_tables_units_and_allowed_joins(surface):
    with SetupValues(surface, []) as values:
        catalog = setup_catalog(surface, [], values)
    text = catalog.render()
    assert "TABLES PER SOURCE" in text and "- logs: issue_day" in text and "- identity: customer, customer_alias" in text
    assert "DECLARED JOIN PATHS (enforced" in text and "- link.subject_id = ticket.id (many_to_one): Link subject ticket." in text
    assert "- search_click.query_day_id = search_query_day.id" in text
    assert "resolution_hours | REAL (number) | hours" in text and "UNITS:" in text
    assert catalog.join_policy == "enforced" and catalog.tables["issue_day"].source == "logs"
    assert catalog.tables["ticket"].column("tenant").samples == "withheld_access_column"  # type: ignore[union-attr]
    assert "member" not in text.split("TABLES PER SOURCE")[1].split("DECLARED")[0].replace("member_set", "")  # pin columns are not offered


def test_the_catalog_fingerprint_covers_pins_and_acl(surface):
    base = setup_catalog(surface, [])
    assert base.fingerprint == setup_catalog(surface, []).fingerprint
    assert base.fingerprint != setup_catalog(surface, tenant_acl("tenant", "t_beta")).fingerprint
    data = surface.model_dump(mode="json")
    data["views"][0]["parts"][0]["pins"][1]["value"] = 2
    assert base.fingerprint != setup_catalog(SqlSurfaceSetup.model_validate(data), []).fingerprint
    data = surface.model_dump(mode="json")
    data["pins"]["link_version"] = "fixture-l2"
    assert base.fingerprint != setup_catalog(SqlSurfaceSetup.model_validate(data), []).fingerprint


def test_a_projection_catalog_is_unchanged_by_the_new_fields():
    """The M4 fields leave a projection catalog's digest (and every statement fingerprint derived
    from it) exactly as computed before them."""
    world = ss.world()
    arm = ss.make_arm(world)
    catalog = arm.catalog_for(ss.context())
    sql_cat = build_sql_catalog(catalog, arm.schema, None, [])
    legacy = sql_cat.model_dump(mode="json", exclude={"fingerprint", "joins", "join_policy", "pins"})
    for table in legacy["tables"].values():
        table.pop("source")
        for column in table["columns"]:
            column.pop("unit")
    assert sql_cat.fingerprint == hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
    assert sql_cat.join_policy == "shown" and "TABLES PER SOURCE" not in sql_cat.render() and "UNITS:" not in sql_cat.render()
