# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Modified by su_multi_arm contributors, 2026-11-08: adapted from NVIDIA/auto-ontology
# c447003f72a822301e927196a4ecb5b597c308d5 (auto_ontology/retrieval/text_to_sql/agents/
# sql_from_semantic.py SQLFromCandidatesAgent.execute flow; agents/sql_reconstruction.py
# SQLReconstructionAgent.execute; agents/sql_unconstructable.py; retrieval/text_to_sql/models.py
# SQLGenerationModel; the check_answerable entity-coverage idea of mcp/auto_ontology_mcp/tools.py).
# Changes: one structured call through our JsonModel (no LangChain/LangGraph, no hidden retries:
# upstream retried a None reply three times), an explicit answerability field instead of a
# separate coverage call, at most one repair round driven by the typed guard/database error (no
# error-classification call, no table discovery, no literal substitution), execution only through
# the typed_query arm (guard + read-only per-tenant file), and a typed result record.
"""``SqlSurfacePlanner``: question -> one SQLite ``SELECT`` over the logical catalog -> executed result.

Flow (per question): one structured call (``SqlDraft``: ``thought``, ``answerability``, ``gaps``,
``sql_code``, ``response``; reasoning fields first, as upstream). ``not_answerable`` stops with
``planner_not_answerable`` (nothing runs). Otherwise the SQL goes to the ``typed_query`` arm, which
re-validates it with the principal's ACL and executes it read-only. A **repairable** refusal (parse
error, unknown name, construct or function outside the allowlist, a database error) is returned to
the model once, with the typed error, when ``repair_attempts`` is 1; the same single round also covers
a reply that violates the ``SqlDraft`` schema (2026-11-08 parity fix: the violated field paths go
back, never the reply). Unknown or ill-typed filter
values are explicit unsupported constraints (never repaired, never replaced); security refusals
(comments, several statements, non-SELECT, system objects, an authorizer denial) are final. A
provider failure is an ``error`` result, never retried; an invalid reply after the repair round (or
with ``repair_attempts: 0``) is an ``error`` result with code ``schema_violation``.

Entry points for an evaluation harness (no planner integration needed): ``engine_session(engine,
principal)`` (or ``open_session(arm, ctx, acl)``) gives a ``SqlSurfaceSession`` with the
principal's ``catalog``; ``SqlSurfacePlanner(session).plan(question, session.catalog, role)`` with
a ``JsonModel`` role (``role_model(engine)``) returns a ``SqlSurfaceResult``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import Field, model_validator

from ..core.types import JsonScalar, Predicate, UnsupportedConstraint
from ..models import StrictModel
from ..providers.base import CallBudget, CallMeter, ProviderError, SchemaViolation
from ..runtime.context import RunContext
from .catalog import SqlCatalog
from .prompts import SYSTEM_PROMPT, repair_prompt, schema_repair_prompt, user_prompt

STAGE = "sql_surface"


class SqlDraft(StrictModel):
    """Upstream ``SQLGenerationModel`` plus the answerability outcome. Field order matters: the
    model fills ``thought`` and ``answerability`` before the SQL."""

    thought: str = Field(
        max_length=2000,
        description=(
            "Briefly explain the SQL approach and state every assumption the question or schema does not "
            'uniquely determine, as "X, because Y": time window, zero/missing values, ties.'
        ),
    )
    answerability: Literal["answerable", "not_answerable"] = Field(
        description="answerable when every entity, attribute, filter value and time window maps to the schema; else not_answerable."
    )
    gaps: list[str] = Field(default_factory=list, max_length=10, description="For not_answerable: each missing schema element or value.")
    sql_code: str | None = Field(default=None, max_length=20000, description="One complete SQLite SELECT, no comments or delimiters; null when not_answerable.")
    response: str = Field(default="", max_length=2000, description="2-4 sentences for the end user describing what is calculated (no SQL).")

    @model_validator(mode="after")
    def sql_iff_answerable(self):
        has_sql = bool(self.sql_code and self.sql_code.strip())
        if has_sql != (self.answerability == "answerable"):
            raise ValueError("sql_code is given iff answerability is answerable")
        return self


class SqlAttempt(StrictModel):
    sql: str | None = None
    thought: str = ""  # untrusted, truncated
    error: dict[str, Any] | None = None


class SqlSurfaceResult(StrictModel):
    status: Literal["executed", "not_answerable", "refused", "unsupported", "error"]
    answerability: Literal["answerable", "not_answerable"] | None = None
    gaps: list[str] = Field(default_factory=list)
    response: str | None = None  # the model's summary: untrusted, never evidence
    attempts: list[SqlAttempt] = Field(default_factory=list)
    model_calls: int = 0
    repaired: bool = False
    evidence: dict[str, Any] | None = None  # the arm's evidence of the last attempt
    unsupported: list[UnsupportedConstraint] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    catalog_fingerprint: str


@dataclass
class SqlSurfaceSession:
    """One principal's view: the ``typed_query`` arm, a pinned ``RunContext``, the ACL and request
    filters, and the catalog built for exactly that ACL."""

    arm: Any
    ctx: RunContext
    acl: list[Predicate]
    catalog: SqlCatalog
    filters: dict[str, JsonScalar] = field(default_factory=dict)
    # the user's own question (not the routed sub-question): the guard's cross-field value rule
    grounding: str | None = None

    def execute(self, sql: str, question: str = "sql_surface") -> dict[str, Any]:
        from ..arms.base import ArmInput

        result = self.arm.run(self.ctx, ArmInput(query=question.strip() or "sql_surface", sql=sql, acl=self.acl, filters=self.filters, question=self.grounding))
        return result.aggregates[0]

    def plan(self, question: str, role: Any, **kwargs: Any) -> SqlSurfaceResult:
        return SqlSurfacePlanner(self, **kwargs).plan(question, self.catalog, role)


class SqlSurfaceUnavailable(RuntimeError):
    """No sealed projection for this generation, or the arm is not in ``mode: sql_surface``."""


def open_session(
    arm: Any, ctx: RunContext, acl: list[Predicate], filters: dict[str, JsonScalar] | None = None, grounding: str | None = None, *, collection: bool = True
) -> SqlSurfaceSession:
    """``collection: False``: the session's catalog is the projection's alone (no log or identity
    views of the unified collection; ``executor.sql_surface_parts``)."""
    from .executor import sql_catalog

    if arm.cfg.mode != "sql_surface":
        raise SqlSurfaceUnavailable("pipeline.arms.typed_query.mode is not sql_surface")
    _, catalog, _ = sql_catalog(arm, ctx, acl, collection=collection)
    if catalog is None:
        raise SqlSurfaceUnavailable("this generation has no sealed typed projection of the configured spec")
    return SqlSurfaceSession(arm=arm, ctx=ctx, acl=list(acl), catalog=catalog, filters=dict(filters or {}), grounding=grounding)


def engine_session(engine: Any, principal: Any = None, *, pipeline: str | None = None, filters: dict[str, JsonScalar] | None = None) -> SqlSurfaceSession:
    """A session on an ``Engine``: the current generation pinned and the principal's ACL from the
    configured access policy (as every request)."""
    name = pipeline or engine.config.default_pipeline
    arm = engine.pipelines[name].arms.get("typed_query")
    if arm is None:
        raise SqlSurfaceUnavailable(f"pipeline {name!r} has no typed_query arm")
    ctx = engine._context_for(principal)
    return open_session(arm, ctx, list(ctx.constraints()), filters)


def role_model(engine: Any, role: str | None = None) -> Any:
    """The ``JsonModel`` of ``models.roles.<role>`` (default: the arm's ``sql_surface.role``)."""
    from ..providers.json_model import JsonModel

    name = role or engine.pipelines[engine.config.default_pipeline].arms["typed_query"].cfg.sql_surface.role
    config = getattr(engine.config.models.roles, name)
    if config is None:
        raise SqlSurfaceUnavailable(f"models.roles.{name} is not configured")
    return JsonModel(config)


class SqlSurfacePlanner:
    def __init__(self, session: SqlSurfaceSession, *, repair_attempts: int | None = None, budget: CallMeter | None = None, limits: str = ""):
        self.session = session
        self.limits = limits  # the opt-in arm-card limits block (``pipeline.decision.arm_cards``)
        settings = session.arm.cfg.sql_surface
        self.repair_attempts = settings.repair_attempts if repair_attempts is None else max(0, min(1, repair_attempts))
        self.budget = budget
        self.row_limit = session.arm.cfg.row_limit

    def _call(self, role: Any, payload: str, budget: CallMeter) -> SqlDraft:
        return role.call(SYSTEM_PROMPT, payload, SqlDraft, budget, STAGE)

    def plan(self, question: str, catalog: SqlCatalog, role: Any) -> SqlSurfaceResult:
        if catalog.fingerprint != self.session.catalog.fingerprint:
            raise ValueError("the catalog is not the session's (catalogs are built per principal)")
        budget = self.budget or CallBudget(1 + self.repair_attempts)
        trace = self.session.ctx.trace
        attempts: list[SqlAttempt] = []
        calls = 0

        def finish(status: str, **kwargs: Any) -> SqlSurfaceResult:
            trace.event("sql_surface_planned", status=status, model_calls=calls, attempts=len(attempts), repaired=len(attempts) > 1)
            return SqlSurfaceResult(status=status, attempts=attempts, model_calls=calls, repaired=len(attempts) > 1, catalog_fingerprint=catalog.fingerprint, **kwargs)  # type: ignore[arg-type]

        payload = user_prompt(question, catalog, row_limit=self.row_limit, limits=self.limits)
        repairs_left = self.repair_attempts
        while True:
            try:
                calls += 1
                draft = self._call(role, payload, budget)
            except SchemaViolation as error:
                violation = {"code": "schema_violation", "kind": "repairable", "detail": str(error)[:300], "paths": error.paths}
                if repairs_left > 0:
                    # one repair round, shared with the SQL repairs: the field paths, never the reply
                    repairs_left -= 1
                    attempts.append(SqlAttempt(error=violation))
                    payload = schema_repair_prompt(question, catalog, paths=error.paths, row_limit=self.row_limit, limits=self.limits)
                    continue
                return finish("error", error={"code": "schema_violation", "detail": str(error)[:300], "paths": error.paths})
            except ProviderError as error:
                return finish("error", error={"code": error.category, "detail": str(error)[:300]})
            if draft.answerability == "not_answerable":
                attempts.append(SqlAttempt(thought=draft.thought[:600]))
                flag = UnsupportedConstraint(constraint_text=question[:200], reason="planner_not_answerable", suggestion="; ".join(draft.gaps)[:500] or None)
                return finish("not_answerable", answerability="not_answerable", gaps=draft.gaps, response=draft.response or None, unsupported=[flag])
            sql = (draft.sql_code or "").strip()
            evidence = self.session.execute(sql, question)
            error = (evidence.get("sql_surface") or {}).get("error") if evidence.get("refused") else None
            attempts.append(SqlAttempt(sql=sql, thought=draft.thought[:600], error=error))
            if error is None:
                return finish("executed", answerability="answerable", response=draft.response or None, evidence=evidence)
            unsupported = [UnsupportedConstraint.model_validate(u) for u in evidence.get("unsupported", [])]
            if error.get("kind") == "repairable" and repairs_left > 0:
                repairs_left -= 1
                payload = repair_prompt(question, catalog, sql=sql, error=error, thought=draft.thought, row_limit=self.row_limit, limits=self.limits)
                continue
            status = {"value": "unsupported", "unavailable": "error"}.get(error.get("kind", ""), "refused")
            return finish(status, answerability="answerable", response=draft.response or None, evidence=evidence, unsupported=unsupported, error=error)
