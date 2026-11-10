"""SQL-surface execution inside the ``typed_query`` arm (``mode: sql_surface``).

``run_sql_surface(arm, ctx, inputs)``: (1) the generation's sealed ``ProjectionCatalog`` (none:
``projection_unavailable``); (2) the per-principal ``SqlCatalog`` and ``ValueResolver`` from
``ArmInput.acl`` (authoritative; nothing decided earlier is trusted); (3) the file from the ACL
exactly as ``TypedQueryArm.file_for`` (all-rows / tenant file / absent or empty grant -> an empty
database of the same schema, so aggregates over zero rows are computed, never invented / other
shapes -> ``projection_unavailable``); (4) ``guard`` (parse, allowlists, value grounding, request
filters and ACL bound into every table reference, parameters, LIMIT); (5) read-only execution
(``open_readonly``: ``mode=ro``, ``query_only``, SELECT/READ authorizer over the granted columns,
the ``SQL_FUNCTIONS`` allowlist, deadline); (6) the returned-row check for row-level results: every
hidden access column equals the granted tenant, else the result is discarded and ``BackendError``
raised; (7) evidence.

Evidence: row-level results are ``typed_row`` records citing ``(document id, field, generation)``
per value; anything else (aggregates, groups, DISTINCT, derived sources) is ``sql_derived`` with
the executed parameterised SQL in ``sql_surface`` lineage. ``exact`` only when the ACL was bound,
nothing was truncated, no referenced column holds rejected values and no join over a relation with
unresolvable child keys in the granted file was used; otherwise ``bounded``.

2026-11-09 (corpus-B B7): ``sql_ratios`` names each rate column with its numerator and denominator
columns (rendered "N of D (rate)"); a grouped result's rows with at most
``sql_surface.group_lineage_max`` contributing records carry ``contributor_ids`` (``group_lineage``).
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from ..core.errors import BackendError
from ..core.types import GenerationId, UnsupportedConstraint
from ..decision.values import ValueResolver
from ..generation.rendering import render_sql_rows
from ..projection.builder import ALL_ROWS, ProjectionBuilder, tenant_file
from ..projection.catalog import ProjectionCatalog
from ..projection.store import MemoryProjectionFiles, open_readonly
from ..runtime.context import RunContext
from .catalog import SqlCatalog, build_sql_catalog
from .guard import LINEAGE_IDS, SQL_FUNCTIONS, GuardedSql, SqlRefusal, guard
from .setup import SqlSetupError

EMPTY = "empty.sqlite"


def sql_catalog(arm: Any, ctx: RunContext, acl: Any, *, collection: bool = True) -> tuple[ProjectionCatalog | None, SqlCatalog | None, ValueResolver]:
    catalog, sql_cat, resolver, _surface = sql_surface_parts(arm, ctx, acl, collection=collection)
    return catalog, sql_cat, resolver


def sql_surface_parts(arm: Any, ctx: RunContext, acl: Any, *, collection: bool = True) -> tuple[ProjectionCatalog | None, SqlCatalog | None, ValueResolver, Any]:
    """The projection catalog, the SQL catalog, the value resolver and (a partitioned collection with
    a live log member, all-rows principal) the cross-source surface (``members.py``). The projection
    part is today's; on a collection its values are read from the projection member only.
    ``collection: False`` (the SQL writer's catalog for a support question, staging round 5 N13):
    the projection part alone, exactly today's catalog."""
    from .members import collection_surface, extend, projection_scope

    facet = getattr(arm.backend, "relational", None)
    resolver = ValueResolver(arm.schema, arm._records(), ctx.generation, [*list(acl), *projection_scope(facet, ctx.generation)])
    catalog = arm.catalog_for(ctx)
    if catalog is None:
        return None, None, resolver, None
    sql_cat = build_sql_catalog(catalog, arm.schema, resolver, list(acl), sample_values=arm.cfg.sql_surface.sample_values, list_values_up_to=arm.cfg.sql_surface.list_values_up_to)
    surface = collection_surface(facet, ctx.generation, sql_cat, acl, arm.file_for(acl, catalog)[1]) if collection else None
    if surface is not None:
        sql_cat = extend(sql_cat, surface)
    return catalog, sql_cat, resolver, surface


def _refused(ctx: RunContext, error: dict[str, Any], reason: str, catalog: SqlCatalog | None = None, grant: str | None = None) -> dict[str, Any]:
    flag = UnsupportedConstraint(constraint_text="sql", reason=reason, suggestion=error.get("detail"))  # type: ignore[arg-type]
    return {
        "evidence": "aggregate_record",
        "operation": "sql_refused",
        "sql_derived": True,
        "generation": ctx.generation.generation,
        "sql_surface": {"error": error, "catalog": catalog.fingerprint if catalog else None, "file": grant},
        "refused": True,
        "exactness": "bounded",
        "value": None,
        "total": None,
        "denominator": 0,
        "unsupported": [flag.model_dump(mode="json")],
        "file_grant": grant,
    }


def refusal_evidence(ctx: RunContext, refusal: SqlRefusal, catalog: SqlCatalog | None = None, grant: str | None = None) -> dict[str, Any]:
    return _refused(ctx, refusal.as_dict(), refusal.reason, catalog, grant)


def _empty_connection(arm: Any, gen: GenerationId, grant: dict[str, set[str]], timeout_ms: int) -> sqlite3.Connection:
    """A read-only connection to an empty database with the projection's schema (zero rows)."""
    files = MemoryProjectionFiles({})
    files.put_file(gen, EMPTY, ProjectionBuilder(arm.spec, arm.schema)._database({}))
    return open_readonly(files, gen, EMPTY, grant, timeout_ms, functions=SQL_FUNCTIONS)


def execute(arm: Any, ctx: RunContext, catalog: ProjectionCatalog, guarded: GuardedSql, file: str | None, lineage: dict[str, Any] | None = None) -> tuple[list[str], list[tuple]]:
    """Run the guarded statement. ``lineage`` (a dict to fill) also runs the statement's contributor
    reads (``GuardedSql.lineage_sql``) on the same read-only connection: ``ids`` and ``count``, or
    ``error`` when a lineage read fails (the result stands; no lineage is claimed)."""
    grant = catalog.grant()
    timeout = arm.cfg.timeout_ms
    conn = open_readonly(arm.backend.relational, ctx.generation, file, grant, timeout, functions=SQL_FUNCTIONS) if file is not None else _empty_connection(arm, ctx.generation, grant, timeout)
    return run_statement(conn, guarded, timeout, lineage)


def run_statement(conn: sqlite3.Connection, guarded: GuardedSql, timeout: int, lineage: dict[str, Any] | None = None) -> tuple[list[str], list[tuple]]:
    """Run a guarded statement (and its lineage reads) on a read-only connection whose authorizer
    is installed, then close it. Shared by the projection file and the multi-source setup
    (``setup.execute_setup``)."""
    try:
        cursor = conn.execute(guarded.sql, guarded.params)
        rows = cursor.fetchall()
        names = [d[0] for d in cursor.description or []]
        if lineage is not None and guarded.lineage_sql and guarded.lineage_count_sql:
            try:
                lineage["ids"] = [str(r[0]) for r in conn.execute(guarded.lineage_sql, guarded.params).fetchall() if r[0] is not None]
                lineage["count"] = int(conn.execute(guarded.lineage_count_sql, guarded.params).fetchone()[0])
            except sqlite3.DatabaseError as error:
                lineage.clear()
                lineage["error"] = str(error)[:200]
        if lineage is not None and guarded.group_lineage_sql:
            try:
                lineage["groups"] = [tuple(r) for r in conn.execute(guarded.group_lineage_sql, guarded.params).fetchall()]
            except sqlite3.DatabaseError as error:
                lineage["group_error"] = str(error)[:200]
    except sqlite3.DatabaseError as error:
        message = str(error)
        if "not authorized" in message or "prohibited" in message:
            raise SqlRefusal("authorizer_denied", f"the read-only authorizer denied the statement: {message}") from None
        if "interrupted" in message:
            raise SqlRefusal("timeout", f"the statement ran past {timeout} ms") from None
        raise SqlRefusal("execution_error", message[:300]) from None
    finally:
        conn.close()
    return names, rows


def run_sql_surface(arm: Any, ctx: RunContext, inputs: Any) -> Any:
    from ..arms.base import ArmResult

    cfg = arm.cfg
    if cfg.mode != "sql_surface":
        error = {"code": "mode", "kind": "final", "detail": "the typed_query arm is in mode ir; SQL is executed only in mode sql_surface", "allowed": []}
        return ArmResult(arm=arm.name, aggregates=[_refused(ctx, error, "not_supported_by_backend")])
    if inputs.sql is None:
        if inputs.typed is None:
            return ArmResult(arm=arm.name)
        error = {"code": "mode", "kind": "final", "detail": "the typed_query arm is in mode sql_surface; typed drafts run in mode ir", "allowed": []}
        return ArmResult(arm=arm.name, aggregates=[_refused(ctx, error, "not_supported_by_backend")])
    catalog, sql_cat, resolver, surface = sql_surface_parts(arm, ctx, inputs.acl)
    if catalog is None or sql_cat is None:
        error = {"code": "projection_unavailable", "kind": "final", "detail": "this generation has no sealed typed projection of the configured spec", "allowed": []}
        return ArmResult(arm=arm.name, aggregates=[_refused(ctx, error, "projection_unavailable")])
    resolver.question = inputs.question  # the user's question: the guard's cross-field value rule
    grant, file, tenant = arm.file_for(inputs.acl, catalog)
    if grant == "unavailable":
        error = {"code": "projection_unavailable", "kind": "final", "detail": "the principal's ACL has no per-tenant projection file", "allowed": []}
        return ArmResult(arm=arm.name, aggregates=[_refused(ctx, error, "projection_unavailable", sql_cat, grant)])
    started = time.perf_counter()
    from . import members

    if members.statement_reads(inputs.sql, surface):
        # a cross-source statement grounds its values over the whole collection (the log member's
        # tenants, services, rules); a projection-only statement keeps the projection member's
        resolver = ValueResolver(arm.schema, arm._records(), ctx.generation, list(inputs.acl))
        resolver.question = inputs.question
    try:
        guarded = guard(
            inputs.sql,
            sql_cat,
            resolver,
            acl=inputs.acl,
            filters=dict(inputs.filters),
            row_limit=cfg.row_limit,
            max_sql_chars=cfg.sql_surface.max_sql_chars,
            max_joins=cfg.sql_surface.max_joins,
            max_subqueries=cfg.sql_surface.max_subqueries,
            inject_acl=arm.inject_acl,
            group_lineage_max=cfg.sql_surface.group_lineage_max,
        )
        ctx.trace.event("sql_surface_validated", fingerprint=guarded.fingerprint, shape=guarded.shape, tables=guarded.tables, checked_values=len(guarded.checked), acl_applied=guarded.acl_applied)
        lineage: dict[str, Any] = {}
        if members.reads_collection(guarded, surface):
            ctx.trace.event("sql_surface_collection", tables=[t for t in guarded.tables if t in surface.tables], pins=surface.setup.pins)
            names, rows = members.execute(surface, guarded, cfg.timeout_ms, lineage)
        else:
            names, rows = execute(arm, ctx, catalog, guarded, file, lineage)
    except SqlRefusal as refusal:
        ctx.trace.event("sql_surface_refused", code=refusal.code, kind=refusal.kind)
        return ArmResult(arm=arm.name, aggregates=[refusal_evidence(ctx, refusal, sql_cat, grant)])
    except SqlSetupError as failure:
        # the cross-source surface could not be opened as pinned (a missing unit file, a view
        # mismatch): fail closed for this statement, labelled (M4: ``projection_unavailable``)
        ctx.trace.event("sql_surface_refused", code="projection_unavailable", kind="final")
        error = {"code": "projection_unavailable", "kind": "final", "detail": f"the collection SQL surface could not be opened: {failure}", "allowed": []}
        return ArmResult(arm=arm.name, aggregates=[_refused(ctx, error, "projection_unavailable", sql_cat, grant)])
    evidence = _evidence(ctx, catalog, sql_cat, guarded, names, rows, grant, tenant, lineage)
    ctx.trace.event("sql_surface_executed", fingerprint=guarded.fingerprint, file_grant=grant, rows=len(evidence["rows"]), elapsed=time.perf_counter() - started, truncated=evidence["truncated"])
    return ArmResult(arm=arm.name, aggregates=[evidence])


def _check_rows(guarded: GuardedSql, names: list[str], rows: list[tuple], grant: str, tenant: str | None) -> None:
    """Layer 3: every source's access column on every returned row equals the tenant grant (a
    LEFT JOIN's unmatched side carries no id and no access value)."""
    if grant != "tenant":
        return
    index = {name: i for i, name in enumerate(names)}
    pairs = []
    for name, (alias, kind) in guarded.hidden.items():
        if kind == "acl":
            sid = next(n for n, (a, k) in guarded.hidden.items() if a == alias and k == "id")
            pairs.append((index[name], index[sid]))
    if not pairs:
        raise BackendError("typed_query", "row_check", "returned rows carry no access column; result discarded")
    for row in rows:
        for acl_at, id_at in pairs:
            if row[id_at] is None and row[acl_at] is None:
                continue
            if row[acl_at] != tenant:
                raise BackendError("typed_query", "row_check", "a returned row does not belong to the granted tenant; result discarded")


def _labels(names: list[str]) -> list[str]:
    """Output names made unique (``SELECT c.id, t.id`` keeps both values: ``id``, ``id_2``)."""
    out: list[str] = []
    for name in names:
        label, k = name, 1
        while label in out:
            k += 1
            label = f"{name}_{k}"
        out.append(label)
    return out


def _evidence(
    ctx: RunContext, catalog: ProjectionCatalog, sql_cat: SqlCatalog, guarded: GuardedSql, names: list[str], rows: list[tuple], grant: str, tenant: str | None, lineage: dict[str, Any] | None = None
) -> dict[str, Any]:
    gen = ctx.generation.generation
    if guarded.shape == "rows":
        _check_rows(guarded, names, rows, grant, tenant)  # every fetched row, before the cut
    fetched = len(rows)
    ties, tie_before, rows, truncated = _ties(guarded, rows)
    visible = len(guarded.columns)
    columns = _labels(guarded.columns)
    out_rows: list[dict[str, Any]] = []
    ids: list[str] = []
    if guarded.shape == "rows":
        position = {name: i for i, name in enumerate(names)}
        alias_id = {alias: position[name] for name, (alias, kind) in guarded.hidden.items() if kind == "id"}
        primary = guarded.primary
        for row in rows:
            values = {columns[i]: row[i] for i in range(visible)}
            citations = []
            for i, cites in enumerate(guarded.citations):
                for alias, table, column in cites:
                    doc = row[alias_id[alias]] if alias in alias_id else None
                    if doc is not None:
                        citations.append({"document_id": doc, "table": table, "field": column, "generation": gen})
            document_id = row[alias_id[primary]] if primary is not None and primary in alias_id else None
            if document_id is not None and document_id not in ids:
                ids.append(document_id)
            out_rows.append({"evidence": "typed_row", "document_id": document_id, "values": values, "nulls": [c for c, v in values.items() if v is None], "citations": citations})
    else:
        out_rows = [{"values": {columns[i]: row[i] for i in range(visible)}} for row in rows]
        for row in out_rows[len(out_rows) - ties :] if ties else []:
            row["tie"] = True  # past the model's LIMIT, kept because it ties the last row within it
    bounded: list[str] = []
    if not guarded.acl_applied:
        bounded.append("acl_not_injected")
    rejected = sorted(f"{t}.{c}" for t, c in guarded.referenced if t in catalog.tables and c in catalog.tables[t].columns and catalog.tables[t].columns[c].rejected > 0)
    if rejected:
        bounded.append(f"referenced columns hold rejected values stored as NULL in this generation: {rejected}")
    file_name = {"all": ALL_ROWS, "tenant": tenant_file(tenant) if tenant else None}.get(grant)
    if guarded.joins and file_name:
        for name, relation in catalog.relations.items():
            if relation.child in guarded.tables and relation.parent in guarded.tables and relation.dangling_by_file.get(file_name, 0):
                bounded.append(f"{relation.dangling_by_file[file_name]} {relation.child} row(s) in the granted file have no resolvable {relation.parent} ({name}); joins leave them out")
    unsupported = []
    if truncated and guarded.tie_columns:
        unsupported.append(UnsupportedConstraint(constraint_text="sql result", reason="result_truncated", suggestion=f"more than {guarded.tie_window} rows tie the last row within the LIMIT; narrow the query").model_dump(mode="json"))
    elif truncated:
        unsupported.append(UnsupportedConstraint(constraint_text="sql result", reason="result_truncated", suggestion=f"more than {guarded.limit} rows; narrow the query").model_dump(mode="json"))
    # a scalar result keeps its value when the only other columns are the ratio parts the guard added
    value = out_rows[0]["values"][columns[0]] if len(out_rows) == 1 and visible - len(guarded.ratio_projected) == 1 else None
    contributors: int | None = None
    cap = guarded.lineage_ids if guarded.shape != "rows" else LINEAGE_IDS
    if guarded.shape != "rows" and lineage and "count" in lineage:
        # a scalar aggregate over one table: the ids of the rows it ranges over (first ``cap`` by id)
        ids = list(dict.fromkeys(lineage["ids"]))
        contributors = lineage["count"]
    groups = _group_contributors(guarded, columns, out_rows, fetched, lineage) if guarded.group_keys and lineage is not None else None
    if groups is not None and groups["status"] == "computed":
        ids = list(dict.fromkeys(i for row in out_rows for i in row.get("contributor_ids", [])))
    lineage_status = "computed" if contributors is not None else ("error" if lineage and "error" in lineage else ("rows" if guarded.shape == "rows" else "not_derivable"))
    if groups is not None and contributors is None:
        lineage_status = "groups" if groups["status"] == "computed" else "error"
    ratios = [{"ratio": columns[r], "numerator": columns[n], "denominator": columns[d], "parts_added": any(columns[i] in guarded.ratio_projected for i in (n, d))} for r, n, d in guarded.ratios]
    exactness = "bounded" if (unsupported or bounded) else "exact"
    tie_report = None
    if guarded.tie_columns:
        tie_report = {"columns": [columns[i] for i in guarded.tie_columns], "limit": guarded.limit, "after_limit": ties, "before_offset": tie_before, "complete": not truncated}
    return {
        "evidence": "typed_rows" if guarded.shape == "rows" else "aggregate_record",
        "operation": "sql_rows" if guarded.shape == "rows" else "sql_aggregate",
        "sql_derived": True,
        "generation": gen,
        "sql_surface": {
            "fingerprint": guarded.fingerprint,
            "sql": guarded.sql,
            "params": guarded.lineage_params,
            "catalog": sql_cat.fingerprint,
            "manifest": catalog.fingerprint,
            "acl_key": sql_cat.acl_key,
            "file": grant,
            "acl_applied": guarded.acl_applied,
            "tables": guarded.tables,
            "columns_referenced": [f"{t}.{c}" for t, c in guarded.referenced],
            "checked_values": guarded.checked,
            "text_patterns": guarded.text_patterns,
            "joins": guarded.joins,
        },
        "columns": columns,
        "rows": out_rows,
        "value": value,
        "total": len(out_rows),
        "denominator": len(out_rows),  # the ``ScopeResult`` population: the returned rows (no matched-set count for model SQL)
        "document_ids": ids[:cap],
        "document_ids_truncated": len(ids) > cap or (contributors is not None and contributors > cap),
        # rows the aggregate ranges over (scalar single-table aggregates only; else None)
        "contributors": contributors,
        "lineage": lineage_status,
        "truncated": truncated,
        "unsupported": unsupported,
        "bounded_by": bounded,
        "exactness": exactness,
        "file_grant": grant,
        # ORDER BY measures the guard projected and the LIMIT-boundary ties (F3); ``sql_ties``, not
        # ``ties``: a ``ScopeResult.ties`` entry is a structured group row
        "sort_projected": list(guarded.sort_projected),
        "sql_ties": tie_report,
        # rate/ratio/share columns with their numerator and denominator columns (B7)
        "sql_ratios": ratios,
        # per-row contributor ids of small groups (B7; rows carry ``contributor_ids``)
        "group_lineage": groups,
        # the deterministic sentence of the result (F1): what the contract renders when it strips
        "rendering": render_sql_rows(
            columns,
            [r["values"] for r in out_rows],
            exactness=exactness,
            truncated=truncated,
            ties=ties,
            tie_before=tie_before,
            offset=guarded.offset,
            ratios=[(r["ratio"], r["numerator"], r["denominator"]) for r in ratios],
            contributors=[r.get("contributor_ids") for r in out_rows] if groups is not None else None,
        ),
    }


def _group_contributors(guarded: GuardedSql, columns: list[str], out_rows: list[dict[str, Any]], fetched: int, lineage: dict[str, Any]) -> dict[str, Any] | None:
    """Attach ``contributor_ids`` to every returned group row with at most ``group_lineage_max``
    contributing rows (the guard's group lineage read, same connection and bound source). The read
    can return at most ``fetched * group_lineage_max`` rows; more, or a failed read, attaches
    nothing and reports ``error`` (the result stands; no lineage is claimed)."""
    small = guarded.group_lineage_max
    if "group_error" in lineage or "groups" not in lineage or len(lineage["groups"]) > fetched * small:
        return {"status": "error", "max_group_size": small, "groups_with_ids": 0}
    width = len(guarded.group_keys)
    by_key: dict[tuple, list[str]] = {}
    for row in lineage["groups"]:
        if row[width] is not None:
            by_key.setdefault(tuple(row[:width]), []).append(str(row[width]))
    found = 0
    for row in out_rows:
        ids = by_key.get(tuple(row["values"][columns[i]] for i in guarded.group_keys))
        if ids:
            row["contributor_ids"] = list(dict.fromkeys(ids))
            found += 1
    return {"status": "computed", "max_group_size": small, "groups_with_ids": found, "scope": "every returned group with at most max_group_size contributing records lists all of them"}


def _ties(guarded: GuardedSql, rows: list[tuple]) -> tuple[int, bool, list[tuple], bool]:
    """``(ties past the LIMIT, tie with the row before the OFFSET, kept rows, truncated)``. A
    ranking statement (``tie_columns``) keeps the rows past the model's LIMIT whose tie columns equal
    the last kept row's; more ties than the window read is ``truncated`` (bounded, never cut
    silently). Any other statement: the first ``limit`` rows, truncated when more were fetched."""
    if not guarded.tie_columns:
        return 0, False, rows[: guarded.limit], len(rows) > guarded.limit

    def key(row: tuple) -> tuple:
        return tuple(row[i] for i in guarded.tie_columns)

    tie_before = False
    if guarded.peek_before and rows:
        before, rows = rows[0], rows[1:]
        tie_before = bool(rows) and key(before) == key(rows[0])
    head, extra = rows[: guarded.limit], rows[guarded.limit :]
    ties = 0
    if head:
        boundary = key(head[-1])
        for row in extra:
            if key(row) != boundary:
                break
            ties += 1
    truncated = ties > guarded.tie_window
    ties = min(ties, guarded.tie_window)
    return ties, tie_before, head + extra[:ties], truncated
