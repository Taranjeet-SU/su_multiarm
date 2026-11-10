"""One SQL surface over the adopted support member and the live log member (M4 completion;
staging fix 2026-11-10, docs/plans/unified_merge.md §6.8).

* The support member's projection tables stay exactly as today: QA100-shaped statements over them
  return the same columns, rows, values, cited documents and exactness with and without the log
  member (and the same refusals); the projection's values are grounded on its member only.
* The log member contributes ``issue_day`` and ``coverage_day`` (derived per pinned unit, served
  rows only) with declared joins to the support tables through the pinned identity registry
  (``customer_id``; matched aliases only); undeclared joins to them are refused
  (``join_not_declared``); the setup opens read-only, before the authorizer.
* The setup's projection part is pinned by its file; every other part keeps a pin.

Synthetic only: F0 fixtures, the F0 identity registry, hash embedder, no provider call.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytest.importorskip("sqlglot")

import tests.live_log_support as L  # noqa: E402
import tests.staging_support as S  # noqa: E402
from su_multi_arm import Engine  # noqa: E402
from su_multi_arm.arms.base import ArmInput  # noqa: E402
from su_multi_arm.core.types import Predicate  # noqa: E402
from su_multi_arm.crosssource.identity import import_registry, sha256_file  # noqa: E402
from su_multi_arm.runtime.context import new_context  # noqa: E402
from su_multi_arm.sql_surface.executor import run_sql_surface, sql_surface_parts  # noqa: E402

NOW = datetime(2026, 11, 9, 10, 30, tzinfo=timezone.utc)

SUPPORT_SQL = [
    "SELECT COUNT(*) AS n FROM ticket",
    "SELECT status, COUNT(*) AS n FROM ticket GROUP BY status ORDER BY n DESC, status",
    "SELECT account, COUNT(*) AS n FROM ticket GROUP BY account ORDER BY account",
    "SELECT id, account, priority FROM ticket WHERE priority = 'high' ORDER BY id",
    "SELECT service, COUNT(*) AS n FROM ticket WHERE created_at >= '2026-10-01' GROUP BY service ORDER BY service",
    "SELECT a.id, b.id AS other FROM ticket a JOIN ticket b ON a.account = b.account AND a.id < b.id ORDER BY a.id, b.id",
    "SELECT id FROM ticket WHERE service = 'crawler' ORDER BY created_at DESC LIMIT 3",
    "SELECT COUNT(*) AS n FROM ticket WHERE service = 'Scheduler'",
    "SELECT COUNT(*) AS n FROM ticket WHERE account = 'alderbank-prod'",  # a log tenant, not an account: refused as today
]
ROW_KEYS = ("operation", "columns", "rows", "value", "total", "document_ids", "exactness", "bounded_by", "unsupported", "truncated", "refused")


def build(root, fx, *, logs: bool) -> Engine:
    registry = root / "ids.sqlite"
    import_registry(registry, fx.registry, now=lambda: NOW)
    original = S.build_support_store(root, fx)
    data = S.unified_data(root, fx, original, logs=logs, sql=True)
    data["crosssource"] = {"identity": {"registry_path": str(registry), "sha256": sha256_file(registry)}}
    from datetime import date

    from su_multi_arm.core.config import validate_config

    engine = Engine(validate_config(data))
    reg = engine.view.attach()
    reg.adopt("support")
    engine.compose()
    if logs:
        lives = reg.live
        ledger = lives.ledger("logs")
        for day_s in L.ARCHIVE_DAYS:
            day = date.fromisoformat(day_s)
            ledger.seal(day, "five_minute", fx.members[f"logs/day:{day_s}"], as_of=L.at(day, 23, 59))
            lives.close_day("logs", day, as_of=L.at(day, 23, 59))
        L.seal_live_window(ledger, fx)
    return engine


@pytest.fixture(scope="module")
def fx():
    return L.fixture()


@pytest.fixture(scope="module")
def support_only(tmp_path_factory, fx):
    return build(tmp_path_factory.mktemp("sql_support_only"), fx, logs=False)


@pytest.fixture(scope="module")
def unified(tmp_path_factory, fx):
    return build(tmp_path_factory.mktemp("sql_unified"), fx, logs=True)


def arm_of(engine: Engine):
    return engine.pipelines[engine.config.default_pipeline].arms["typed_query"]


def run(engine: Engine, sql: str, acl: list | None = None) -> dict:
    gen, _ = engine.write_path.pin()
    ctx = new_context(gen)
    result = run_sql_surface(arm_of(engine), ctx, ArmInput(query="q", sql=sql, acl=acl or [], question="q"))
    record = result.aggregates[0]
    return record


def comparable(record: dict) -> dict:
    """The record without the request generation id (the pin differs between the two engines,
    as in the M1b equivalence: only the generation stamped in citations)."""
    import copy

    out = copy.deepcopy({k: record.get(k) for k in ROW_KEYS})
    for row in out.get("rows") or []:
        for citation in row.get("citations", []):
            citation.pop("generation", None)
    if record.get("refused"):
        out["code"] = record["sql_surface"]["error"]["code"]
    return out


@pytest.mark.parametrize("sql", SUPPORT_SQL)
def test_support_statements_return_the_same_results_with_and_without_the_log_member(support_only, unified, sql):
    before, after = run(support_only, sql), run(unified, sql)
    assert comparable(after) == comparable(before)
    if not before.get("refused"):
        assert after["sql_surface"]["sql"] == before["sql_surface"]["sql"]  # the same guarded statement, the same file


def test_the_projection_values_are_its_member_s_only(support_only, unified):
    assert run(unified, SUPPORT_SQL[-1])["sql_surface"]["error"]["code"] == "value_not_observed"
    gen0, _ = support_only.write_path.pin()
    gen1, _ = unified.write_path.pin()
    _, _, before, _ = sql_surface_parts(arm_of(support_only), new_context(gen0), [])
    _, _, after, _ = sql_surface_parts(arm_of(unified), new_context(gen1), [])
    for field in ("service", "account", "status", "priority", "source"):
        assert after.observed(field) == before.observed(field)
    assert "logs" not in after.observed("source")  # the collection holds log records; the projection's values do not


def test_support_catalog_tables_are_unchanged_and_log_tables_join_them(support_only, unified):
    gen0, _ = support_only.write_path.pin()
    gen1, _ = unified.write_path.pin()
    _, before, _, none = sql_surface_parts(arm_of(support_only), new_context(gen0), [])
    _, after, _, surface = sql_surface_parts(arm_of(unified), new_context(gen1), [])
    assert none is None and surface is not None and before.join_policy == "shown" and after.join_policy == "cross_source"
    assert after.tables["ticket"] == before.tables["ticket"]  # samples, descriptions, flags: the support member's only
    assert {"issue_day", "coverage_day", "customer_logs_tenant", "customer_support_account"} <= set(after.tables)
    pairs = {(j.left, j.left_column, j.right, j.right_column) for j in after.joins}
    assert ("issue_day", "tenant", "customer_logs_tenant", "id") in pairs and ("ticket", "account", "customer_support_account", "id") in pairs
    assert ("customer_support_account", "customer_id", "customer_logs_tenant", "customer_id") in pairs
    text = after.render()
    assert "- logs: issue_day, coverage_day" in text and "DECLARED JOIN PATHS" in text


def test_log_tables_hold_the_served_rows_of_the_pin(unified):
    record = run(unified, "SELECT COUNT(*) AS n FROM issue_day")
    gen, _ = unified.write_path.pin()
    from su_multi_arm.core.types import ScopeSpec

    served = unified.records.execute(gen, ScopeSpec(predicates=[Predicate(field="kind", op="eq", value="incident")]), [])
    assert record["value"] == served.total and record["value"] > 0
    days = run(unified, "SELECT date, SUM(occurrences) AS occ FROM issue_day GROUP BY date ORDER BY date")
    assert [r["values"]["date"] for r in days["rows"]] == sorted({r["values"]["date"] for r in days["rows"]})
    assert {r["values"]["date"] for r in days["rows"]} & set(L.LIVE)


def test_cross_source_join_through_the_identity_registry(unified):
    sql = ("SELECT t.account, i.tenant, COUNT(*) AS episodes FROM ticket t "
           "JOIN customer_support_account a ON t.account = a.id "
           "JOIN customer_logs_tenant g ON a.customer_id = g.customer_id "
           "JOIN issue_day i ON i.tenant = g.id GROUP BY t.account, i.tenant ORDER BY t.account, i.tenant")
    record = run(unified, sql)
    assert not record.get("refused"), record.get("sql_surface")
    gen, _ = unified.write_path.pin()
    view = unified.identity_view()
    pairs = {(r["values"]["account"], r["values"]["tenant"]) for r in record["rows"]}
    assert pairs and all(view.customer_id("support.account", a) == view.customer_id("logs.tenant", t) is not None for a, t in pairs)


def test_an_undeclared_join_to_a_log_table_is_refused(unified):
    record = run(unified, "SELECT t.id FROM ticket t JOIN issue_day i ON t.service = i.service")
    assert record["refused"] and record["sql_surface"]["error"]["code"] == "join_not_declared"


def test_a_tenant_principal_gets_the_projection_surface_only(unified):
    gen, _ = unified.write_path.pin()
    _, catalog, _, surface = sql_surface_parts(arm_of(unified), new_context(gen), [Predicate(field="account", op="eq", value="Alderbank Ltd")])
    assert surface is None and "issue_day" not in catalog.tables


def test_the_setup_pins_every_log_part_and_reads_read_only(unified):
    gen, _ = unified.write_path.pin()
    _, _, _, surface = sql_surface_parts(arm_of(unified), new_context(gen), [])
    roles = {a.alias: a.role for a in surface.setup.attachments}
    for view in surface.setup.views:
        for part in view.parts:
            assert part.pins or roles[part.attachment] == "projection"
    issue = surface.setup.view("issue_day")
    assert [p.label for part in issue.parts for p in part.pins] == ["pin"]  # one file per request pin, served rows only
    assert len(surface.setup.attachments) <= 10  # bounded whatever the number of pinned units


def test_a_missing_pinned_file_fails_closed_labelled(tmp_path, fx, monkeypatch):
    engine = build(tmp_path, fx, logs=True)
    from su_multi_arm.sql_surface import members

    monkeypatch.setattr(members, "view_file", lambda root, backend, gen, member: (tmp_path / "gone.sqlite", {}))
    record = run(engine, "SELECT COUNT(*) AS n FROM issue_day")
    assert record["refused"] and record["sql_surface"]["error"]["code"] == "projection_unavailable"
    assert not run(engine, "SELECT COUNT(*) AS n FROM ticket").get("refused")  # the projection surface itself still serves
