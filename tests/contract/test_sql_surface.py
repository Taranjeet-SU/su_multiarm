"""SQL surface of the ``typed_query`` arm (contract; synthetic fixtures and a scripted model only).

* execution: rows cite (document id, field, generation); aggregates are ``sql_derived`` with the
  executed parameterised SQL in lineage; the ACL key is in the fingerprint;
* layer 2: the ACL selects the per-tenant file (absent tenant / empty grant: an empty database;
  other ACL shapes and unsealed generations: ``projection_unavailable``);
* layer 3: a planted foreign row in a tenant file discards a row result;
* defence in depth: adversarial statements sent straight to the read-only connection (guard
  bypassed) are denied by the authorizer or confined to the tenant file;
* planner: answerable -> executed; not_answerable -> nothing runs; one repair with the typed error;
  values and security refusals are never repaired; provider/schema failures are errors;
* tenant canary: 5,000 generated and adversarial SQL strings with compiler ACL injection disabled
  -> zero cross-tenant rows, ids, values or aggregates;
* engine: ``engine_session`` on the sqlite backend with a tenant principal.
"""

from __future__ import annotations

import json
import random
import sqlite3

import pytest

pytest.importorskip("sqlglot")

from su_multi_arm.arms.base import ArmInput  # noqa: E402
from su_multi_arm.backends.memory import MemoryBackend  # noqa: E402
from su_multi_arm.core.errors import BackendError  # noqa: E402
from su_multi_arm.core.types import Predicate  # noqa: E402
from su_multi_arm.projection import tenant_file  # noqa: E402
from su_multi_arm.projection.store import open_readonly  # noqa: E402
from su_multi_arm.providers.base import ProviderTimeout  # noqa: E402
from su_multi_arm.runtime.acl import Principal  # noqa: E402
from su_multi_arm.sql_surface import SQL_FUNCTIONS, SqlSurfacePlanner, SqlSurfaceUnavailable, engine_session, open_session  # noqa: E402

import sql_surface_support as ss  # noqa: E402
import typed_support as ts  # noqa: E402

GEN = ss.GEN


@pytest.fixture(scope="module")
def world():
    return ss.world()


def execute(arm, sql, acl, filters=None, gen=GEN):
    ctx = ss.context(gen)
    result = arm.run(ctx, ArmInput(query="question", sql=sql, acl=acl, filters=filters or {}))
    events = {e["name"]: e for s in ctx.trace.spans for e in s.events}
    return result.aggregates[0], events


# --- execution and evidence --------------------------------------------------------------------


def test_rows_cite_document_field_and_generation(world):
    evidence, events = execute(ss.make_arm(world), "SELECT id, status, priority FROM ticket WHERE module = 'Billing' ORDER BY id", ss.tenant("t_alpha"))
    assert evidence["evidence"] == "typed_rows" and evidence["operation"] == "sql_rows" and evidence["sql_derived"] is True
    assert evidence["exactness"] == "exact" and evidence["document_ids"] == ["tk-1", "tk-2"]
    first = evidence["rows"][0]
    assert first["document_id"] == "tk-1" and first["values"] == {"id": "tk-1", "status": "open", "priority": 1}
    assert {"document_id": "tk-1", "table": "ticket", "field": "status", "generation": GEN.generation} in first["citations"]
    assert "__sid_0" not in first["values"] and evidence["columns"] == ["id", "status", "priority"]
    assert events["sql_surface_executed"]["file_grant"] == "tenant" and events["sql_surface_validated"]["acl_applied"] is True


def test_joined_rows_cite_both_documents(world):
    evidence, _ = execute(ss.make_arm(world), "SELECT c.id, t.status FROM comment c JOIN ticket t ON c.parent_id = t.id WHERE c.visibility = 'private'", ss.tenant("t_alpha"))
    row = evidence["rows"][0]
    assert row["document_id"] == "cm-2" and row["values"] == {"id": "cm-2", "status": "open"}
    assert {"document_id": "tk-1", "table": "ticket", "field": "status", "generation": GEN.generation} in row["citations"]
    both, _ = execute(ss.make_arm(world), "SELECT c.id, t.id FROM comment c JOIN ticket t ON c.parent_id = t.id WHERE c.visibility = 'private'", ss.tenant("t_alpha"))
    assert both["columns"] == ["id", "id_2"] and both["rows"][0]["values"] == {"id": "cm-2", "id_2": "tk-1"}


def test_aggregates_are_sql_derived_with_parameterised_lineage(world):
    evidence, _ = execute(ss.make_arm(world), "SELECT status, count(*) AS n, sum(hours) AS h FROM ticket WHERE module = 'Billing' GROUP BY status ORDER BY status", ss.tenant("t_alpha"))
    assert evidence["evidence"] == "aggregate_record" and evidence["operation"] == "sql_aggregate" and evidence["sql_derived"]
    assert [r["values"] for r in evidence["rows"]] == [{"status": "closed", "n": 1, "h": 4.0}, {"status": "open", "n": 1, "h": 2.5}]
    lineage = evidence["sql_surface"]
    assert ":p_1" in lineage["sql"] and "'Billing'" not in lineage["sql"] and "t_alpha" not in json.dumps(lineage)
    assert lineage["params"]["acl_0_0"] == "<acl>" and "Billing" in lineage["params"].values()
    assert lineage["checked_values"] == [{"column": "ticket.module", "op": "eq", "value": "Billing", "grounded_by": "observed", "filter": "positive"}]
    assert len(lineage["fingerprint"]) == 64 and lineage["acl_key"] and lineage["file"] == "tenant"
    single, _ = execute(ss.make_arm(world), "SELECT count(*) FROM ticket", ss.tenant("t_alpha"))
    assert single["value"] == 3 and single["exactness"] == "exact"


def test_the_fingerprint_changes_with_the_principal(world):
    arm = ss.make_arm(world)
    alpha, _ = execute(arm, "SELECT sum(hours) FROM ticket", ss.tenant("t_alpha"))
    beta, _ = execute(arm, "SELECT sum(hours) FROM ticket", ss.tenant("t_beta"))
    assert (alpha["value"], beta["value"]) == (6.5, 7.5)
    assert alpha["sql_surface"]["fingerprint"] != beta["sql_surface"]["fingerprint"] and alpha["sql_surface"]["acl_key"] != beta["sql_surface"]["acl_key"]


def test_file_layer_selection(world):
    arm = ss.make_arm(world)
    count = "SELECT count(*) FROM ticket"
    assert execute(arm, count, [])[0]["value"] == 6  # unrestricted principal: the all-rows file incl. the canary
    absent = execute(arm, count, ss.tenant("t_unknown"))[0]
    assert absent["value"] == 0 and absent["file_grant"] == "absent" and absent["exactness"] == "exact"
    nothing = execute(arm, "SELECT count(*), total(hours) FROM ticket", [Predicate(field="tenant", op="in", value=[])])[0]
    assert nothing["rows"] == [{"values": {"_col_0": 0, "_col_1": 0.0}}] and nothing["file_grant"] == "none"
    other = execute(arm, count, [Predicate(field="tenant", op="in", value=["t_alpha", "t_beta"])])[0]
    assert other["refused"] and other["unsupported"][0]["reason"] == "projection_unavailable"
    unsealed = execute(arm, count, ss.tenant("t_alpha"), gen=ss.OTHER)[0]
    assert unsealed["refused"] and unsealed["unsupported"][0]["reason"] == "projection_unavailable"


def test_request_filters_narrow_every_table(world):
    evidence, _ = execute(ss.make_arm(world), "SELECT t.id FROM ticket t JOIN comment c ON c.parent_id = t.id", ss.tenant("t_alpha"), filters={"visibility": "private"})
    assert [r["values"]["id"] for r in evidence["rows"]] == ["tk-1"]


def test_refusals_are_typed_unsupported_constraints(world):
    arm = ss.make_arm(world)
    value, events = execute(arm, "SELECT count(*) FROM ticket WHERE module = 'Reports'", ss.tenant("t_alpha"))
    assert value["refused"] and value["unsupported"][0]["reason"] == "value_not_observed" and value["sql_surface"]["error"]["kind"] == "value"
    assert events["sql_surface_refused"]["code"] == "value_not_observed"
    security, _ = execute(arm, "SELECT * FROM ticket; DROP TABLE ticket", ss.tenant("t_alpha"))
    assert security["unsupported"][0]["reason"] == "sql_refused" and security["sql_surface"]["error"]["kind"] == "security"


def test_the_arm_runs_only_its_modes_input(world):
    ir = ss.make_arm(world, mode="ir")
    evidence, _ = execute(ir, "SELECT count(*) FROM ticket", ss.tenant("t_alpha"))
    assert evidence["refused"] and evidence["unsupported"][0]["reason"] == "not_supported_by_backend"
    from su_multi_arm.core.types_typed import TypedQueryDraft

    surface = ss.make_arm(world)
    draft = TypedQueryDraft.model_validate({"grain": "ticket", "measure": {"kind": "count"}})
    refused = surface.run(ss.context(), ArmInput(query="q", typed=draft, acl=ss.tenant("t_alpha"))).aggregates[0]
    assert refused["refused"] and "mode sql_surface" in refused["sql_surface"]["error"]["detail"]
    assert surface.run(ss.context(), ArmInput(query="q", acl=ss.tenant("t_alpha"))).aggregates == []


def test_truncation_and_dangling_joins_are_bounded(world):
    arm = ss.make_arm(world, row_limit=2)
    rows, _ = execute(arm, "SELECT id FROM ticket", [])
    assert len(rows["rows"]) == 2 and rows["truncated"] and rows["exactness"] == "bounded" and rows["unsupported"][-1]["reason"] == "result_truncated"
    limited, _ = execute(arm, "SELECT id FROM ticket ORDER BY id LIMIT 2", [])
    assert limited["truncated"] is False and limited["exactness"] == "exact"
    joined, _ = execute(ss.make_arm(world), "SELECT count(*) FROM comment c JOIN ticket t ON c.parent_id = t.id", ss.tenant("t_beta"))
    assert joined["value"] == 1 and joined["exactness"] == "bounded" and "no resolvable ticket" in joined["bounded_by"][0]


def test_the_returned_row_check_fires_on_a_planted_foreign_row(world):
    backend, schema, files = world
    planted = MemoryBackend()
    for name, data in files.items():
        planted.relational.put_file(GEN, name, data)
    planted.relational.put_file(GEN, tenant_file("t_alpha"), files[tenant_file("t_beta")])  # a corrupted tenant file
    planted.relational.put_file(GEN, "manifest.json", backend.relational.read_file(GEN, "manifest.json"))
    planted.records.upsert(GEN, ts.records(), schema)
    arm = ss.make_arm(world, backend=planted)
    assert execute(arm, "SELECT id FROM asset", ss.tenant("t_alpha"))[0]["rows"] == []  # layer 1 alone filters the rows
    arm.inject_acl = False
    with pytest.raises(BackendError, match="does not belong to the granted tenant"):
        execute(arm, "SELECT id FROM asset", ss.tenant("t_alpha"))


@pytest.mark.parametrize(
    "sql",
    [
        "ATTACH DATABASE ':memory:' AS other",
        "PRAGMA table_info(ticket)",
        "SELECT name FROM sqlite_master",
        "INSERT INTO ticket (id) VALUES ('x')",
        "DELETE FROM ticket",
        "SELECT load_extension('evil')",
        "SELECT _rejected, random() FROM ticket",
        "SELECT value FROM json_each('[1]')",
    ],
)
def test_defence_in_depth_the_authorizer_denies_what_the_guard_refuses(world, sql):
    backend, _, _ = world
    arm = ss.make_arm(world)
    catalog = arm.catalog_for(ss.context())
    conn = open_readonly(backend.relational, GEN, tenant_file("t_alpha"), catalog.grant(), 2000, functions=SQL_FUNCTIONS)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_defence_in_depth_unions_over_the_tenant_file_stay_in_the_tenant(world):
    backend, _, _ = world
    catalog = ss.make_arm(world).catalog_for(ss.context())
    conn = open_readonly(backend.relational, GEN, tenant_file("t_beta"), catalog.grant(), 2000, functions=SQL_FUNCTIONS)
    try:
        rows = conn.execute("SELECT id, tenant FROM ticket UNION ALL SELECT id, tenant FROM comment UNION ALL SELECT id, tenant FROM asset").fetchall()
    finally:
        conn.close()
    assert rows and {tenant for _, tenant in rows} == {"t_beta"}


# --- planner -----------------------------------------------------------------------------------


def session(world, acl=None, **limits):
    return open_session(ss.make_arm(world, **limits), ss.context(), acl if acl is not None else ss.tenant("t_alpha"))


def test_plan_executes_an_answerable_question(world):
    s = session(world)
    role = ss.FakeRole(ss.answer("SELECT count(*) AS open_tickets FROM ticket WHERE status = 'open'"))
    result = SqlSurfacePlanner(s).plan("How many open tickets?", s.catalog, role)
    assert result.status == "executed" and result.model_calls == 1 and not result.repaired
    assert result.evidence["value"] == 2 and result.evidence["exactness"] == "exact"
    assert "How many open tickets?" in role.payloads[0] and "Billing" in role.payloads[0] and "t_beta" not in role.payloads[0]


def test_not_answerable_runs_nothing(world):
    s = session(world)
    result = s.plan("What is the severity of ticket 1?", ss.FakeRole(ss.not_answerable("no severity column")))
    assert result.status == "not_answerable" and result.evidence is None and result.model_calls == 1
    assert result.unsupported[0].reason == "planner_not_answerable" and result.gaps == ["no severity column"]


def test_one_repair_with_the_typed_error(world):
    s = session(world)
    role = ss.FakeRole(ss.answer("SELECT count(*) FROM main.ticket"), ss.answer("SELECT count(*) FROM ticket"))
    result = s.plan("How many tickets?", role)
    assert result.status == "executed" and result.repaired and result.model_calls == 2
    assert result.attempts[0].error["code"] == "qualified_name" and result.attempts[1].error is None
    assert "The following SQL contains an ERROR:" in role.payloads[1] and "qualified_name" in role.payloads[1]
    assert result.evidence["value"] == 3


def test_execution_errors_are_repairable(world):
    s = session(world)
    role = ss.FakeRole(ss.answer("SELECT status, count(*) FROM ticket GROUP BY 9"), ss.answer("SELECT status, count(*) FROM ticket GROUP BY status"))
    result = s.plan("Tickets per status?", role)
    assert result.attempts[0].error["code"] in {"execution_error", "parse_error", "unknown_column"}
    assert result.status == "executed" and result.model_calls == 2


def test_at_most_one_repair(world):
    s = session(world)
    role = ss.FakeRole(ss.answer("SELECT nope FROM ticket"), ss.answer("SELECT still_nope FROM ticket"))
    result = s.plan("q", role)
    assert result.status == "refused" and result.model_calls == 2 and result.error["code"] == "unknown_column"
    assert role.replies == []


def test_repair_can_be_disabled(world):
    s = session(world, sql_surface={"repair_attempts": 0})
    result = s.plan("q", ss.FakeRole(ss.answer("SELECT nope FROM ticket")))
    assert result.status == "refused" and result.model_calls == 1


def test_unknown_values_are_never_repaired_or_replaced(world):
    s = session(world)
    # 2026-11-09 (B5): 'billing' now grounds to the stored 'Billing' by the documented normalization
    # (recorded); a value no rule grounds is still never repaired or replaced
    role = ss.FakeRole(ss.answer("SELECT count(*) FROM ticket WHERE module = 'billing ops'"), ss.answer("SELECT count(*) FROM ticket WHERE module = 'Billing'"))
    result = s.plan("How many billing ops tickets?", role)
    assert result.status == "unsupported" and result.model_calls == 1 and len(role.replies) == 1
    assert result.unsupported[0].reason == "value_not_observed" and result.evidence["refused"]


def test_security_refusals_are_final(world):
    s = session(world)
    role = ss.FakeRole(ss.answer("SELECT count(*) FROM ticket /* x */"), ss.answer("SELECT count(*) FROM ticket"))
    result = s.plan("q", role)
    assert result.status == "refused" and result.model_calls == 1 and result.error["kind"] == "security"


def test_provider_and_schema_failures_are_errors_without_retry(world):
    s = session(world)
    timeout = s.plan("q", ss.FakeRole(ProviderTimeout("slow", stage="sql_surface"), ss.answer("SELECT 1")))
    assert timeout.status == "error" and timeout.error["code"] == "timeout" and timeout.model_calls == 1
    bad = {"thought": "x", "answerability": "answerable", "sql_code": None}
    # 2026-11-08: the one repair round also covers a schema violation; a second one is the error
    invalid = s.plan("q", ss.FakeRole(bad, bad))
    assert invalid.status == "error" and invalid.error["code"] == "schema_violation" and invalid.model_calls == 2
    off = session(world, sql_surface={"repair_attempts": 0})
    once = off.plan("q", ss.FakeRole(bad))
    assert once.status == "error" and once.error["code"] == "schema_violation" and once.model_calls == 1


def test_a_schema_violation_gets_the_one_repair_round(world):
    """2026-11-08 parity fix: a reply that is not the draft schema (e.g. "thought: ..." lines) is
    returned once with the violated field paths (never the reply); the repaired SQL executes."""
    from su_multi_arm.providers.base import SchemaViolation

    s = session(world)
    violation = SchemaViolation("reply is not JSON", raw="thought: count the rows", stage="sql_surface", paths=["<root> (json_invalid)"])
    role = ss.FakeRole(violation, ss.answer("SELECT count(*) FROM ticket WHERE status = 'open'"))
    result = s.plan("How many open tickets?", role)
    assert result.status == "executed" and result.repaired and result.model_calls == 2 and result.evidence["value"] == 2
    assert result.attempts[0].error["code"] == "schema_violation" and result.attempts[0].sql is None
    assert "schema_violation: <root> (json_invalid)" in role.payloads[1] and "thought: count the rows" not in role.payloads[1]
    # the round is shared: a schema violation then a repairable SQL error is not repaired again
    shared = s.plan("q", ss.FakeRole(violation, ss.answer("SELECT nope FROM ticket")))
    assert shared.status == "refused" and shared.model_calls == 2


def test_the_sql_writer_example_is_json():
    from su_multi_arm.sql_surface.prompts import SYSTEM_PROMPT

    start = SYSTEM_PROMPT.index('{"thought"')
    example = json.loads(SYSTEM_PROMPT[start : SYSTEM_PROMPT.rindex("}") + 1])
    assert list(example) == ["thought", "answerability", "gaps", "sql_code", "response"]
    assert "\nthought:\n" not in SYSTEM_PROMPT


# --- contributor lineage of SQL aggregates (2026-11-08) ---------------------------------------------


def test_a_scalar_aggregate_carries_its_contributors(world):
    evidence, _ = execute(ss.make_arm(world), "SELECT count(*) FROM ticket WHERE status = 'open'", ss.tenant("t_alpha"))
    assert evidence["value"] == 2 and evidence["exactness"] == "exact"
    assert evidence["contributors"] == 2 and evidence["lineage"] == "computed"
    assert evidence["document_ids"] == ["tk-1", "tk-3"] and evidence["document_ids_truncated"] is False


def test_contributors_follow_the_acl_and_the_request_filters(world):
    arm = ss.make_arm(world)
    alpha, _ = execute(arm, "SELECT sum(hours) FROM ticket", ss.tenant("t_alpha"))
    beta, _ = execute(arm, "SELECT sum(hours) FROM ticket", ss.tenant("t_beta"))
    assert set(alpha["document_ids"]) <= OWNED["t_alpha"] and set(beta["document_ids"]) <= OWNED["t_beta"]
    assert alpha["contributors"] == len(OWNED_TICKETS["t_alpha"]) and beta["contributors"] == len(OWNED_TICKETS["t_beta"])
    filtered, _ = execute(arm, "SELECT count(*) FROM ticket", ss.tenant("t_alpha"), filters={"status": "closed"})
    assert filtered["value"] == filtered["contributors"] == 1


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM ticket t JOIN comment c ON c.parent_id = t.id",  # joined
        "SELECT count(*) FROM ticket WHERE id IN (SELECT parent_id FROM comment)",  # nested
        "SELECT id, status FROM ticket",  # row-level (rows cite themselves)
    ],
)
def test_no_contributor_lineage_is_claimed_for_other_shapes(world, sql):
    evidence, _ = execute(ss.make_arm(world), sql, ss.tenant("t_alpha"))
    assert evidence["contributors"] is None and evidence["lineage"] in {"not_derivable", "rows"}


def test_a_grouped_count_lists_small_groups_not_a_scalar_lineage(world):
    """B7: a grouped statement carries per-group contributor ids (``lineage: groups``), never the
    scalar ``contributors`` count; with ``group_lineage_max: 0`` nothing is claimed (the old row)."""
    sql = "SELECT status, count(*) FROM ticket GROUP BY status"
    evidence, _ = execute(ss.make_arm(world), sql, ss.tenant("t_alpha"))
    assert evidence["contributors"] is None and evidence["lineage"] == "groups"
    assert {i for r in evidence["rows"] for i in r["contributor_ids"]} <= OWNED["t_alpha"]
    off, _ = execute(ss.make_arm(world, sql_surface={"group_lineage_max": 0}), sql, ss.tenant("t_alpha"))
    assert off["contributors"] is None and off["lineage"] == "not_derivable"


def test_the_catalog_must_be_the_sessions(world):
    alpha = session(world)
    beta = session(world, acl=ss.tenant("t_beta"))
    with pytest.raises(ValueError, match="built per principal"):
        SqlSurfacePlanner(alpha).plan("q", beta.catalog, ss.FakeRole())


def test_sessions_need_the_mode_and_a_sealed_projection(world):
    with pytest.raises(SqlSurfaceUnavailable, match="mode"):
        open_session(ss.make_arm(world, mode="ir"), ss.context(), ss.tenant("t_alpha"))
    with pytest.raises(SqlSurfaceUnavailable, match="sealed"):
        open_session(ss.make_arm(world), ss.context(ss.OTHER), ss.tenant("t_alpha"))


# --- tenant canary -----------------------------------------------------------------------------

OWNED = {t: {d.id for d in ts.documents() if d.metadata.get("tenant") == t} for t in (*ts.TENANTS, ts.CANARY)}
OWNED_TICKETS = {t: {d.id for d in ts.documents() if d.metadata.get("tenant") == t and d.id.startswith("tk-")} for t in (*ts.TENANTS, ts.CANARY)}
FOREIGN_TENANTS = {"t_alpha": {"t_beta", "t_canary"}, "t_beta": {"t_alpha", "t_canary"}}
VALUES = {"status": ["open", "closed", "pending"], "module": ["Billing", "Search", "Reports"], "visibility": ["public", "private"]}
ALL_IDS = {d.id for d in ts.documents()}


def _predicate(rng: random.Random, alias: str, table: str, principal: str) -> str:
    prefix = f"{alias}." if alias else ""
    options = {
        "ticket": [
            lambda: f"{prefix}status = '{rng.choice(VALUES['status'])}'",
            lambda: f"{prefix}module IN ('{rng.choice(VALUES['module'])}', '{rng.choice(VALUES['module'])}')",
            lambda: f"{prefix}priority {rng.choice(['<', '>=', '='])} {rng.randint(1, 4)}",
            lambda: f"{prefix}hours > {rng.choice([0, 1.5, 5])}",
            lambda: f"{prefix}opened_at >= '2024-03-0{rng.randint(1, 9)}'",
            lambda: f"{prefix}tenant {rng.choice(['=', '<>'])} '{principal}'",
            lambda: f"NOT {prefix}tenant = '{principal}'",
            lambda: f"{prefix}title_text LIKE '%{rng.choice(['ticket', 'canary', 'DROP'])}%'",
        ],
        "comment": [
            lambda: f"{prefix}visibility = '{rng.choice(VALUES['visibility'])}'",
            lambda: f"{prefix}hours IS NULL",
            lambda: f"{prefix}tenant <> '{principal}'",
        ],
        "asset": [lambda: f"{prefix}module = '{rng.choice(VALUES['module'])}'", lambda: f"{prefix}tenant <> '{principal}'"],
    }[table]
    return rng.choice(options)()


def random_sql(rng: random.Random, principal: str) -> str:
    """One generated statement: ordinary shapes over every table, ACL-probing predicates and
    adversarial strings (other tenants' values and ids, unions, attach, pragma, system tables,
    comments, multiple statements, qualified names, cross joins, recursion, functions)."""
    other = rng.choice(sorted(FOREIGN_TENANTS[principal]))
    foreign_id = rng.choice(sorted(OWNED[other]))
    table = rng.choice(["ticket", "comment", "asset"])
    family = rng.randint(0, 13)
    where = f" WHERE {_predicate(rng, '', table, principal)}" if rng.random() < 0.7 else ""
    if family == 0:
        return f"SELECT * FROM {table}{where}"
    if family == 1:
        return f"SELECT count(*), sum(hours), max(hours), min(id) FROM {table}{where}"
    if family == 2:
        column = {"ticket": "status", "comment": "visibility", "asset": "module"}[table]
        return f"SELECT {column}, tenant, count(*) AS n, group_concat(id) AS ids FROM {table}{where} GROUP BY {column}, tenant ORDER BY n DESC"
    if family == 3:
        return f"SELECT c.id, c.tenant, t.id, t.tenant, t.title_text FROM comment c {rng.choice(['JOIN', 'LEFT JOIN'])} ticket t ON c.parent_id = t.id WHERE {_predicate(rng, 't', 'ticket', principal)}"
    if family == 4:
        return f"SELECT id, tenant FROM ticket WHERE id IN (SELECT parent_id FROM comment WHERE {_predicate(rng, '', 'comment', principal)}) OR EXISTS (SELECT 1 FROM asset a WHERE a.tenant <> '{principal}')"
    if family == 5:
        return f"WITH x AS (SELECT id, tenant, hours FROM {table}{where}) SELECT tenant, count(*), total(hours) FROM x GROUP BY tenant"
    if family == 6:
        return f"SELECT id, tenant FROM {table} UNION {rng.choice(['', 'ALL '])}SELECT id, tenant FROM {rng.choice(['ticket', 'comment', 'asset'])} WHERE tenant = '{other}'"
    if family == 7:
        return rng.choice([f"ATTACH DATABASE 'x.sqlite' AS {other}", "PRAGMA database_list", "SELECT name, sql FROM sqlite_master", "SELECT * FROM pragma_table_info('ticket')", "DETACH DATABASE main"])
    if family == 8:
        return rng.choice([f"SELECT * FROM {table} WHERE id = '{foreign_id}'", f"SELECT * FROM {table} WHERE tenant = '{other}'", f"SELECT count(*) FROM {table} WHERE tenant IN ('{principal}', '{other}')"])
    if family == 9:
        return rng.choice([f"SELECT * FROM {table} -- WHERE tenant = '{principal}'", f"SELECT * FROM {table} /* */ WHERE 1 = 1", f"SELECT * FROM {table}; SELECT * FROM {table} WHERE tenant = '{other}'", f"SELECT * FROM {table} WHERE id = '{foreign_id}' OR '1' = '1'"])
    if family == 10:
        return rng.choice([f"SELECT * FROM main.{table}", f"SELECT a.id, b.id FROM {table} a, {table} b", f"SELECT a.id FROM {table} a CROSS JOIN ticket b", "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT n FROM r"])
    if family == 11:
        return f"SELECT id, tenant FROM {table} WHERE tenant <> '{principal}' OR {rng.choice(['1 = 1', 'hours IS NULL', 'id IS NOT NULL'])}"
    if family == 12:
        return f"SELECT DISTINCT tenant FROM {table}" if rng.random() < 0.5 else f"SELECT tenant, max(hours) FROM {table} GROUP BY tenant HAVING count(*) >= 1"
    return f"SELECT t.id, (SELECT count(*) FROM comment c WHERE c.parent_id = t.id) AS comments FROM ticket t{where.replace(' WHERE ', ' WHERE t.') if where and table == 'ticket' else ''}"


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def test_tenant_canary_with_compiler_acl_disabled(world):
    """5,000 generated/adversarial statements, layer 1 (ACL binding) off: the per-tenant file and the
    row check alone keep every other tenant's rows, ids, values and aggregates out."""
    arm = ss.make_arm(world)
    arm.inject_acl = False
    rng = random.Random(20261108)
    executed = refused = 0
    families: dict[str, int] = {}
    for _ in range(5000):
        principal = rng.choice(ts.TENANTS)
        sql = random_sql(rng, principal)
        evidence, _ = execute(arm, sql, ss.tenant(principal))
        if evidence.get("refused"):
            refused += 1
            code = evidence["sql_surface"]["error"]["code"]
            families[code] = families.get(code, 0) + 1
            continue
        executed += 1
        assert evidence["sql_surface"]["acl_applied"] is False and "acl_not_injected" in evidence["bounded_by"]
        foreign_ids = ALL_IDS - OWNED[principal]
        cells = list(_strings([r["values"] for r in evidence["rows"]]))
        for cell in cells:
            assert cell not in FOREIGN_TENANTS[principal], (sql, cell)
            assert not (set(cell.split(",")) & foreign_ids), (sql, cell)
            assert "canary" not in cell, (sql, cell)
        assert set(evidence["document_ids"]) <= OWNED[principal], sql
        for row in evidence["rows"]:
            assert all(c["document_id"] in OWNED[principal] for c in row.get("citations", [])), sql
            for value in row["values"].values():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    assert value < 99.0, (sql, value)  # the canary ticket's 99.0 hours never reach a tenant result
    assert executed >= 1500 and refused >= 1500, (executed, refused, families)
    for code in ("set_operation", "not_select", "system_object", "comment", "multiple_statements", "value_not_observed", "qualified_name", "join_not_allowed", "recursive_cte"):
        assert families.get(code, 0) > 0, (code, families)


def test_canary_counts_never_exceed_the_principals_rows(world):
    arm = ss.make_arm(world)
    arm.inject_acl = False
    for principal in ts.TENANTS:
        for table, kind in (("ticket", "ticket"), ("comment", "comment"), ("asset", "asset")):
            evidence, _ = execute(arm, f"SELECT count(*) FROM {table} WHERE tenant <> '{principal}' OR 1 = 1", ss.tenant(principal))
            owned = sum(1 for d in ts.documents() if d.kind == kind and d.metadata.get("tenant") == principal)
            assert evidence["value"] == owned


# --- engine --------------------------------------------------------------------------------------


def test_engine_session_on_the_sqlite_projection(tmp_path):
    from su_multi_arm import Engine

    data = ts.config_dict(tmp_path / "state")
    data["pipeline"]["arms"]["typed_query"]["mode"] = "sql_surface"
    data.setdefault("models", {}).setdefault("roles", {})["planner"] = {"provider": "openai_compatible", "model": "local-test", "base_url": "http://127.0.0.1:9/v1"}
    from su_multi_arm.core.config import validate_config

    engine = Engine(validate_config(data))
    engine.ingest(ts.documents())
    assert engine.pipelines["default"].arms["typed_query"].validate(None) == []
    beta = engine_session(engine, Principal(id="u-beta", tenant="t_beta", scopes=["search", "answer"]))
    assert beta.acl == ss.tenant("t_beta")
    role = ss.FakeRole(ss.answer("SELECT status, count(*) AS n FROM ticket GROUP BY status ORDER BY status"))
    result = beta.plan("Tickets per status?", role)
    assert result.status == "executed" and [r["values"] for r in result.evidence["rows"]] == [{"status": "open", "n": 1}, {"status": "pending", "n": 1}]
    assert result.evidence["file_grant"] == "tenant" and "t_alpha" not in role.payloads[0]
    beta.arm.inject_acl = False
    try:
        again = beta.plan("Tickets?", ss.FakeRole(ss.answer("SELECT count(*) FROM ticket WHERE tenant <> 't_beta' OR 1 = 1")))
    finally:
        beta.arm.inject_acl = True
    assert again.evidence["value"] == 2  # the sqlite tenant file holds t_beta only
