"""Typed query IR (typed-query plan §5.1; MVP M3).

A ``TypedQueryDraft`` is an unverified proposal over the declared projection catalog: a ``grain``
table counted/listed/looked up, at most one declared ``many_to_one`` relation (``join``), typed
predicates and calendar windows per table, an optional literal text match (resolved to a document
id set like ``StructuredArm.resolve_text``), one measure, at most two group-by columns, a group
ranking and projected columns. It is never SQL and never executed as given: ``TypedQueryCompiler``
(``decision/typed_query.py``) resolves every name and value against the catalog and the
``ValueResolver`` and returns a ``CompiledTypedQuery`` or typed ``CompileError`` records; the arm
recompiles with the principal's ACL before ``typed_sql.lower`` turns it into parameterised SQL.

Absent on purpose (plan §5.1): exists/not-exists, more than one join, arithmetic, functions,
``LIKE``/regex, unions, CTEs, window functions, self/cross joins and ``HAVING``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from ..models import StrictModel, UnsupportedReason
from .types import Measure, Predicate, TextMatch

NAME = r"^[a-z][a-z0-9_]{0,40}$"


class ColumnRef(StrictModel):
    table: str = Field(pattern=NAME)
    column: str = Field(pattern=NAME)


class TypedPredicate(StrictModel):
    """A predicate on one table's column (``predicate.field`` is the column name)."""

    table: str = Field(pattern=NAME)
    predicate: Predicate


class TableWindow(StrictModel):
    """A calendar expression on a temporal column, resolved by ``ValueResolver.window``."""

    table: str = Field(pattern=NAME)
    column: str = Field(pattern=NAME)
    expression: str = Field(min_length=1, max_length=128)


class TypedQueryDraft(StrictModel):
    """``measure.field``/``unit`` name a column of ``grain`` or ``<table>.<column>`` of the joined
    table. No measure, no group: a lookup/list of ``select`` columns (all projectable grain columns
    when empty), ordered by the grain key, at most ``limit`` (<= the arm's ``row_limit``) rows."""

    grain: str = Field(pattern=NAME)
    join: str | None = Field(default=None, pattern=NAME)  # MVP: at most one declared many_to_one relation
    where: list[TypedPredicate] = Field(default_factory=list, max_length=20)
    windows: list[TableWindow] = Field(default_factory=list, max_length=4)
    text: TextMatch | None = None
    measure: Measure | None = None
    group_by: list[ColumnRef] = Field(default_factory=list, max_length=2)
    order: Literal["desc", "asc"] | None = None
    top_n: int | None = Field(default=None, ge=1, le=1000)
    select: list[ColumnRef] = Field(default_factory=list, max_length=50)
    limit: int | None = Field(default=None, ge=1, le=10000)

    @model_validator(mode="after")
    def coherent(self):
        if len({(c.table, c.column) for c in self.group_by}) != len(self.group_by):
            raise ValueError("group_by columns must differ")
        if (self.order is not None or self.top_n is not None) and not self.group_by:
            raise ValueError("order/top_n rank groups: they need group_by")
        if self.select and (self.measure is not None or self.group_by):
            raise ValueError("select projects rows of a lookup/list; an aggregate has no select")
        if self.limit is not None and (self.measure is not None or self.group_by):
            raise ValueError("limit bounds a lookup/list; groups are bounded by top_n and max_groups")
        if self.text is not None and self.text.role != "scope":
            raise ValueError("a typed query text match is a scope condition (no share numerator)")
        return self


class PlannedTypedStep(StrictModel):
    """Planner wire shape (M6); property order is significant: ``analysis`` comes first, is
    untrusted, is traced masked and is never compiled."""

    analysis: str = Field(max_length=600)
    answerability: Literal["answerable", "partial", "not_answerable"]
    gaps: list[str] = Field(default_factory=list, max_length=10)  # untrusted; traced masked
    query: TypedQueryDraft | None = None

    @model_validator(mode="after")
    def query_iff_answerable(self):
        if (self.query is None) != (self.answerability == "not_answerable"):
            raise ValueError("query is None iff answerability is not_answerable")
        if self.answerability != "answerable" and not any(g.strip() for g in self.gaps):
            raise ValueError("a partial or not_answerable step names its gaps (the reason)")
        if any(len(g) > 300 for g in self.gaps):
            raise ValueError("a gap is at most 300 characters")
        return self


class PlannedSqlRoute(StrictModel):
    """Planner wire shape of a ``typed_query`` step when the arm is in ``mode: sql_surface``: no IR,
    a self-contained sub-question for the SQL route instead. Property order is significant
    (``analysis`` first). ``analysis``, ``gaps`` and ``question`` are untrusted model text: traced
    masked; ``question`` is handed to the SQL-writing role at execution, whose SQL the guard
    validates and binds before anything runs."""

    analysis: str = Field(max_length=600)
    answerability: Literal["answerable", "partial", "not_answerable"]
    gaps: list[str] = Field(default_factory=list, max_length=10)  # untrusted; traced masked
    question: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def question_iff_answerable(self):
        has_question = bool(self.question and self.question.strip())
        if has_question == (self.answerability == "not_answerable"):
            raise ValueError("question is given iff answerability is not not_answerable")
        if self.answerability != "answerable" and not any(g.strip() for g in self.gaps):
            raise ValueError("a partial or not_answerable step names its gaps (the reason)")
        if any(len(g) > 300 for g in self.gaps):
            raise ValueError("a gap is at most 300 characters")
        return self


class CompileError(StrictModel):
    """Typed compile feedback and trace record; ``path`` locates the offending IR element."""

    path: str = Field(min_length=1)
    reason: UnsupportedReason
    allowed: list[str] = Field(default_factory=list)
    detail: str | None = None


class CompiledTypedQuery(StrictModel):
    """``fingerprint = sha256(IR, catalog fingerprint, generation, acl_key)``; a decision-time
    fingerprint is never trusted for execution (the arm recompiles with ``ArmInput.acl``)."""

    query: TypedQueryDraft  # values resolved; request filters and ACL are in ``filters_applied``
    acl_key: str
    acl_applied: bool = True
    generation: str
    catalog_fingerprint: str
    filters_applied: list[TypedPredicate] = Field(default_factory=list)
    fanout: Literal["none", "distinct_guarded"] = "none"
    errors: list[CompileError] = Field(default_factory=list)
    fingerprint: str = ""

    def executable(self) -> bool:
        return not self.errors
