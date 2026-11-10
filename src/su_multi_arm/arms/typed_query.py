"""``typed_query`` arm (typed-query plan §5.5-§5.7, MVP M5): compiled typed queries over the projection.

``_run``: (1) load the generation's ``ProjectionCatalog`` (no sealed projection of this spec:
``projection_unavailable``); (2) **recompile** the draft with ``ArmInput.acl`` (authoritative; a
decision-time compile or fingerprint is never trusted) and recompute the fingerprint with
``acl_key(inputs.acl)``; (3) select the file from the ACL (layer 2): no constraint -> the all-rows
file; ``eq(access column, v)`` -> the tenant file of ``v`` (a tenant absent from the sealed manifest
reads zero rows; listed but missing is a ``BackendError``); ``in(field, [])`` -> nothing; any other
shape -> ``projection_unavailable``; (4) resolve ``text`` to an id set like
``StructuredArm.resolve_text`` (at most ``max_text_ids`` ids, else refused); (5) execute the lowered
SQL on a read-only connection (``row_limit``/``max_groups`` + 1, ``timeout_ms``); (6) check every
returned row's access columns against the grant (layer 3): a mismatch discards the result and
raises ``BackendError``; (7) build evidence.

Evidence: aggregates are one ``aggregate_record`` (value, groups, distinct grain total, at most 8
contributor ids, NULL and in-scope rejected counts, truncation) with ``typed_query: {fingerprint,
grain, join, canonical, manifest, file}``; lookups/lists are ``typed_row`` records citing
``(document id, field, generation)`` per value (no spans: projected metadata has none). The
result is ``exact`` only when everything compiled, the ACL was applied, nothing was truncated, no
in-scope referenced value was rejected and no join dropped unresolvable child rows; otherwise
``bounded`` with the reasons. Reached from the ``llm`` planner's typed steps (M6) through
``Engine._run_step``, which passes the request's ACL and immutable filters.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, ClassVar

from pydantic import Field

from ..backends import capabilities as caps
from ..core.component import Diagnostic, Environment
from ..core.config import TypedQueryArm as TypedQueryArmConfig
from ..core.config_projection import KEY, ProjectionConfig
from ..core.errors import BackendError
from ..core.types import Predicate, ScopePredicates, Scope, UnsupportedConstraint
from ..core.types_typed import CompiledTypedQuery, TypedPredicate, TypedQueryDraft
from ..decision.typed_query import TypedQueryCompiler, TypedStepInvalid, describe, operation
from ..decision.typed_sql import Lowered, lower
from ..decision.values import ValueResolver
from ..projection.builder import ALL_ROWS, tenant_file
from ..projection.catalog import ProjectionCatalog
from ..projection.spec import ProjectionSpec
from ..projection.store import open_readonly
from ..runtime.context import RunContext
from .base import ArmInput, ArmResult, BaseArm


class TypedQueryRuntime(TypedQueryArmConfig):
    """The arm's limits plus what the engine binds from the configuration."""

    backend: str
    records: str | None = None  # ``storage.records`` (the ValueResolver's observed values)
    projection: ProjectionConfig = Field(default_factory=ProjectionConfig)
    access_column: str | None = None


class TypedQueryArm(BaseArm):
    name: ClassVar[str] = "typed_query"
    config_model: ClassVar[type] = TypedQueryRuntime
    capability: ClassVar[str] = "relational"
    # Test-only hook (plan §5.7): with False the compiler injects no ACL predicate, so the per-tenant
    # file layer alone must keep other tenants out. Never set by configuration.
    inject_acl: bool = True

    def __init__(self, config: Any, deps: Any):
        super().__init__(config, deps)
        cfg = self.cfg
        self.spec = ProjectionSpec.build(cfg.projection, self.schema, cfg.access_column) if cfg.projection.enabled and self.schema is not None else None

    def validate(self, env: Environment, probe: bool = False) -> list[Diagnostic]:
        diagnostics = super().validate(env, probe)
        if self.spec is None:
            diagnostics.append(Diagnostic(severity="error", code="projection_missing", message="typed_query needs projection.enabled", component=self.name))
        if getattr(self.cfg, "mode", "ir") == "sql_surface":
            from ..sql_surface.guard import available

            if not available():
                diagnostics.append(Diagnostic(severity="error", code="extra_missing", message="typed_query mode sql_surface needs sqlglot: install su-multi-arm[sql-surface]", component=self.name))
        return diagnostics

    # --- helpers ---------------------------------------------------------------------------------

    def catalog_for(self, ctx: RunContext) -> ProjectionCatalog | None:
        if self.spec is None or self.backend is None:
            return None
        return ProjectionCatalog.load(self.backend.relational, ctx.generation, self.spec)

    def _records(self) -> Any:
        backend = self.deps.backends.get(getattr(self.cfg, "records", None) or "")
        return getattr(backend, "records", None)

    def compile(self, ctx: RunContext, catalog: ProjectionCatalog, draft: TypedQueryDraft, filters: dict[str, Any], acl: ScopePredicates) -> CompiledTypedQuery:
        from ..sql_surface.members import projection_scope

        # on a collection the projection's values are its member's (unchanged with other members)
        resolver = ValueResolver(self.schema, self._records(), ctx.generation, [*list(acl), *projection_scope(getattr(self.backend, "relational", None), ctx.generation)])
        compiler = TypedQueryCompiler(catalog, resolver, row_limit=self.cfg.row_limit, max_groups=self.cfg.max_groups)
        return compiler.compile(draft, filters, acl, inject_acl=self.inject_acl)

    @staticmethod
    def file_for(acl: ScopePredicates, catalog: ProjectionCatalog) -> tuple[str, str | None, str | None]:
        """``(grant, file, tenant value)``: ``all`` | ``tenant`` | ``absent`` (zero rows) |
        ``none`` (empty grant) | ``unavailable`` (an ACL shape no file layer expresses)."""
        if not acl:
            return "all", ALL_ROWS, None
        if len(acl) == 1:
            p = acl[0]
            if p.op == "in" and p.value == [] and not p.negate:
                return "none", None, None
            if p.op == "eq" and not p.negate and isinstance(p.value, str) and catalog.access_column is not None and p.field == catalog.access_column:
                name = tenant_file(p.value)
                return ("tenant", name, p.value) if name in catalog.tenant_files else ("absent", None, p.value)
        return "unavailable", None, None

    def _text_ids(self, ctx: RunContext, draft: TypedQueryDraft, inputs: ArmInput) -> tuple[list[str] | None, list[UnsupportedConstraint], list[str]]:
        if draft.text is None:
            return None, [], []
        from .structured import text_basis, text_match_ids, text_query

        name = self.cfg.text_backend
        backend = self.deps.backends.get(name) if name else None
        query = text_query(draft.text)
        kind = getattr(backend, "kind", "")
        problem = None
        if backend is None or not (caps.satisfied(backend, "lexical") and kind in caps.MATRIX and caps.serves(kind, "lexical")):
            problem = "no lexical index serves the typed_query arm (set pipeline.arms.typed_query.text_backend)"
        elif query is None:
            problem = "the text condition has no indexable words"
        elif draft.text.kind == "phrase" and not caps.flags(kind, "lexical").get("phrase"):
            problem = f"the lexical index of {kind} has no phrase semantics"
        if problem is not None:
            return None, [UnsupportedConstraint(constraint_text=f"text {draft.text.kind}", reason="not_supported_by_backend", suggestion=problem)], []
        scope = Scope(predicates=list(inputs.acl))
        ids = text_match_ids(backend.lexical, ctx.generation, query, scope)  # type: ignore[arg-type]
        if len(ids) > self.cfg.max_text_ids:
            raise BackendError("typed_query", "text", f"text condition matches more than {self.cfg.max_text_ids} documents; narrow the query")
        return ids, [], [text_basis(draft.text, caps.flags(kind, "lexical").get("analyzer_id"))]

    # --- run -------------------------------------------------------------------------------------

    def _run(self, ctx: RunContext, inputs: ArmInput) -> ArmResult:
        if inputs.sql is not None or getattr(self.cfg, "mode", "ir") == "sql_surface":
            # model-written SQL behind the guard (``sql_surface/``); the arm runs only its mode's input
            from ..sql_surface.executor import run_sql_surface

            return run_sql_surface(self, ctx, inputs)
        draft = inputs.typed
        if draft is None:
            return ArmResult(arm=self.name)
        catalog = self.catalog_for(ctx)
        if catalog is None:
            flag = UnsupportedConstraint(constraint_text=f"typed query on {draft.grain}", reason="projection_unavailable", suggestion="this generation has no sealed typed projection of the configured spec")
            return ArmResult(arm=self.name, aggregates=[self._refused(draft, [flag], ctx)])
        filters = dict(inputs.filters)
        try:
            compiled = self.compile(ctx, catalog, draft, filters, inputs.acl)
        except TypedStepInvalid as error:
            raise BackendError("typed_query", "compile", str(error)) from error
        ctx.trace.event("typed_query_compiled", fingerprint=compiled.fingerprint, errors=[e.model_dump() for e in compiled.errors], acl_applied=compiled.acl_applied, **describe(compiled.query))
        if compiled.errors:
            flagged = [UnsupportedConstraint(constraint_text=e.path, reason=e.reason, suggestion=e.detail) for e in compiled.errors]
            return ArmResult(arm=self.name, aggregates=[self._refused(compiled.query, flagged, ctx, compiled)])
        grant, file, tenant = self.file_for(inputs.acl, catalog)
        if grant == "unavailable":
            flag = UnsupportedConstraint(constraint_text="access constraints", reason="projection_unavailable", suggestion="the principal's ACL has no per-tenant projection file")
            return ArmResult(arm=self.name, aggregates=[self._refused(compiled.query, [flag], ctx, compiled)])
        ids, flagged, basis = self._text_ids(ctx, compiled.query, inputs)
        extra = [TypedPredicate(table=compiled.query.grain, predicate=Predicate(field=KEY, op="in", value=list(ids)))] if ids is not None else []
        lowered = lower(compiled, catalog, row_limit=self.cfg.row_limit, max_groups=self.cfg.max_groups, extra=extra)
        started = time.perf_counter()
        results = self._execute(ctx, catalog, lowered, file) if file is not None else {}
        evidence = self._evidence(ctx, catalog, compiled, lowered, results, grant, tenant, flagged, basis)
        ctx.trace.event(
            "typed_query_executed",
            fingerprint=compiled.fingerprint,
            file_grant=grant,
            rows=evidence.get("total"),
            elapsed=time.perf_counter() - started,
            truncated=evidence.get("truncated", False),
        )
        return ArmResult(arm=self.name, aggregates=[evidence])

    def _execute(self, ctx: RunContext, catalog: ProjectionCatalog, lowered: Lowered, file: str) -> dict[str, list[tuple]]:
        out: dict[str, list[tuple]] = {}
        conn = open_readonly(self.backend.relational, ctx.generation, file, catalog.grant(), self.cfg.timeout_ms)
        try:
            for name, (sql, params) in lowered.statements.items():
                out[name] = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as error:
            raise BackendError("typed_query", "execute", f"projection query failed: {error}") from error
        finally:
            conn.close()
        return out

    # --- evidence --------------------------------------------------------------------------------

    def _base(self, query: TypedQueryDraft, ctx: RunContext, compiled: CompiledTypedQuery | None, catalog: ProjectionCatalog | None = None, grant: str | None = None) -> dict[str, Any]:
        return {
            "evidence": "typed_rows" if query.measure is None and not query.group_by else "aggregate_record",
            "operation": operation(query),
            "denominator": 0,  # the ``ScopeResult`` population (the matched total once executed)
            "generation": ctx.generation.generation,
            "typed_query": {
                "fingerprint": compiled.fingerprint if compiled else None,
                "grain": query.grain,
                "join": query.join,
                "canonical": query.model_dump(mode="json"),
                "manifest": catalog.fingerprint if catalog else None,
                "file": grant,
                "acl_applied": compiled.acl_applied if compiled else None,
                "fanout": compiled.fanout if compiled else None,
            },
        }

    def _refused(self, query: TypedQueryDraft, flagged: list[UnsupportedConstraint], ctx: RunContext, compiled: CompiledTypedQuery | None = None) -> dict[str, Any]:
        return {**self._base(query, ctx, compiled), "exactness": "bounded", "value": None, "total": None, "unsupported": [u.model_dump(mode="json") for u in flagged], "refused": True}

    @staticmethod
    def _value(column_type: str, value: Any) -> Any:
        return bool(value) if column_type == "boolean" and value is not None else value

    def _evidence(self, ctx, catalog, compiled, lowered, results, grant, tenant, flagged, basis) -> dict[str, Any]:  # noqa: ANN001
        query = compiled.query
        evidence = self._base(query, ctx, compiled, catalog, grant)
        unsupported = [u.model_dump(mode="json") for u in flagged]
        bounded: list[str] = []
        contributors = [r[0] for r in results.get("contributors", [])]
        evidence["document_ids"] = contributors[:8]
        evidence["document_ids_truncated"] = len(contributors) > 8
        if not compiled.acl_applied:
            bounded.append("acl_not_injected")
        if query.join is not None:
            relation = catalog.relations[query.join]
            file_name = {"all": ALL_ROWS, "tenant": tenant_file(tenant) if tenant else None}.get(grant)
            dangling = relation.dangling_by_file.get(file_name, 0) if file_name else 0
            if query.grain == relation.child and dangling:
                bounded.append(f"{dangling} {relation.child} row(s) in the granted file have no resolvable {relation.parent}; the inner join leaves them out")
            evidence["basis"] = [f"inner join {query.join}: only {query.grain} rows with a related row are counted", *basis]
        else:
            evidence["basis"] = list(basis)
        types = {(t, c): catalog.tables[t].columns[c].type for t in catalog.tables for c in catalog.tables[t].columns}
        if lowered.shape == "rows":
            rows = results.get("main", [])
            self._check_rows(rows, lowered, catalog, grant, tenant, query)
            truncated = len(rows) > lowered.limit
            rows = rows[: lowered.limit]
            total, rejected = (results.get("total") or [(0, 0)])[0]
            evidence["rows"] = [self._row(r, lowered, types, ctx, query.grain) for r in rows]
            evidence["total"] = int(total or 0)
            evidence["value"] = len(rows)
            evidence["rejected_in_scope"] = int(rejected or 0)
        elif lowered.shape == "groups":
            rows = results.get("main", [])
            truncated = len(rows) > lowered.limit
            ties = truncated and query.top_n is not None and query.order is not None and rows[lowered.limit][0] == rows[lowered.limit - 1][0]
            kept = rows[: lowered.limit]
            total, rejected = (results.get("total") or [(0, 0)])[0]
            evidence["groups"] = [
                {"key": {f"{t}.{c}" if t != query.grain else c: self._value(types[(t, c)], v) for (t, c), v in zip(lowered.columns, r[4:])}, "value": r[0], "count": int(r[1]), "rank": (i + 1) if query.order is not None else None}
                for i, r in enumerate(kept)
            ]
            evidence["value"] = {json.dumps(g["key"], sort_keys=True): g["value"] for g in evidence["groups"]}
            evidence["total"] = int(total or 0)
            evidence["missing_or_invalid"] = sum(int(r[2] or 0) for r in kept)
            evidence["rejected_in_scope"] = int(rejected or 0)
            if ties:
                evidence["ties_at_cut"] = True
                bounded.append("groups tie the last group shown")
            if truncated and query.top_n is not None:
                truncated = False  # top_n is the asked cut, not a truncation
        else:
            row = (results.get("main") or [(None, 0, 0, 0)])[0]
            value, total, nulls, rejected = row
            if not results and (query.measure is None or query.measure.kind in {"count", "count_distinct"}):
                value = 0  # no file to read for this grant (empty grant or absent tenant): zero rows
            if query.measure is not None and query.measure.kind in {"first_seen", "last_seen"}:
                pick = results.get("pick") or []
                value = {"value": pick[0][0], "record_id": pick[0][1]} if pick else None
            evidence["value"] = value
            evidence["total"] = int(total or 0)
            evidence["missing_or_invalid"] = int(nulls or 0)
            evidence["rejected_in_scope"] = int(rejected or 0)
            truncated = False
        if evidence.get("rejected_in_scope"):
            bounded.append("in-scope rows hold rejected values in referenced columns")
        if truncated:
            unsupported.append(UnsupportedConstraint(constraint_text=f"{lowered.shape} limit", reason="result_truncated", suggestion=f"more than {lowered.limit} {lowered.shape}; narrow the query").model_dump(mode="json"))
        evidence["denominator"] = int(evidence.get("total") or 0)
        evidence["truncated"] = truncated
        evidence["measure"] = query.measure.model_dump(mode="json", exclude_none=True) if query.measure else {"kind": "rows"}
        evidence["unsupported"] = unsupported
        evidence["bounded_by"] = bounded
        evidence["exactness"] = "bounded" if (unsupported or bounded) else "exact"
        evidence["file_grant"] = grant
        return evidence

    def _row(self, row: tuple, lowered: Lowered, types: dict, ctx: RunContext, grain: str) -> dict[str, Any]:
        document_id = row[0]
        cells = row[1 : 1 + len(lowered.columns)]
        rejected_cells = row[1 + len(lowered.columns) :]
        rejected = {t: set(json.loads(v)) if v else set() for t, v in zip(lowered.rejected_tables, rejected_cells)}
        ids = {grain: document_id}
        for (t, c), v in zip(lowered.columns, cells):
            if c == KEY:
                ids[t] = v
        values: dict[str, Any] = {}
        out_types: dict[str, str] = {}
        nulls: list[str] = []
        marks: list[str] = []
        citations: list[dict[str, Any]] = []
        for (t, c), v in zip(lowered.columns, cells):
            label = c if t == grain else f"{t}.{c}"
            values[label] = self._value(types[(t, c)], v)
            out_types[label] = types[(t, c)]
            if v is None:
                (marks if c in rejected.get(t, set()) else nulls).append(label)
            citations.append({"document_id": ids.get(t), "field": c, "generation": ctx.generation.generation})
        return {"evidence": "typed_row", "document_id": document_id, "values": values, "types": out_types, "nulls": nulls, "rejected": marks, "citations": citations}

    @staticmethod
    def _check_rows(rows: list[tuple], lowered: Lowered, catalog: ProjectionCatalog, grant: str, tenant: str | None, query: TypedQueryDraft) -> None:
        """Layer 3: every returned row's access columns equal the tenant grant."""
        access = catalog.access_column
        if grant != "tenant" or access is None:
            return
        positions = [1 + i for i, (_t, c) in enumerate(lowered.columns) if c == access]
        if not positions:
            raise BackendError("typed_query", "row_check", "returned rows carry no access column; result discarded")
        for row in rows:
            if any(row[p] != tenant for p in positions):
                raise BackendError("typed_query", "row_check", "a returned row does not belong to the granted tenant; result discarded")
