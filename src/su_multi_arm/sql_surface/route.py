"""The SQL route of a planned ``typed_query`` step (arm ``mode: sql_surface``; planner integration).

With ``pipeline.arms.typed_query.mode: sql_surface`` and ``pipeline.decision.typed_query.enabled`` the
``llm`` planner routes a self-contained sub-question (``TypedStep.question``) instead of a typed IR
draft. Before the plan's steps execute, the engine runs each such step here:

1. ``run``: a ``SqlSurfaceSession`` for the **request's** ACL and immutable filters (the catalog and
   samples are built for exactly that ACL; nothing decided earlier is trusted), then
   ``SqlSurfacePlanner.plan`` with the configured ``sql_surface.role``: one structured call plus at most
   ``sql_surface.repair_attempts`` repair round, each metered through ``RunBudgetMeter`` under the stage
   ``sql_surface`` (the run's budget, ledger and ``model_call`` trace, like every request-path call).
   The SQL runs only through the arm: guard, ACL/filter binding, per-tenant file, returned-row check.
2. ``executed_unapplied``: the question qualifiers the **executed** SQL does not express (its grounded
   filter values and referenced columns against ``qualifiers.qualifier_candidates``), plus
   ``NO_OPERATOR`` for an aggregate result of a question that asks none; the engine attaches them to
   the step, so the evidence is ``bounded`` (the IR path checks its draft the same way at decision).
3. ``apply_failures``: every other status is a ``DroppedStep`` (``planner_not_answerable`` /
   ``sql_refused`` / ``sql_unsupported`` / ``sql_error``) with its ``UnsupportedConstraint`` records
   and one ``aggregate_intent_without_scope`` per aggregate intent; then ``on_step_failure`` decides:
   ``abstain`` (default; or a caller-typed aggregate/scope) fails the decision through the configured
   ``on_failure`` (abstain or ``raw_all_arms``); ``partial`` drops the step; ``retrieval_only`` drops every
   structured and typed step. When nothing remains the original query runs on the enabled
   lexical/dense arms (``fallback: retrieval_after_dropped_steps``), else the decision fails.

Model text (the sub-question, the SQL writer's thought, gaps and summary) never reaches a
``DroppedStep.detail`` or a constraint; those carry status, codes and fixed wording only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from ..core.errors import BudgetExhausted, DeadlineExceeded
from ..core.types import JsonScalar, MetadataSchema, Predicate, ScopeSpec, UnsupportedConstraint
from .planner import STAGE, SqlSurfacePlanner, SqlSurfaceResult, SqlSurfaceUnavailable, open_session

ROUTE = "typed query (sql route)"
# a question about mentions (as ``decision/rules.py``), plus "containing"/"contains"
_MENTION = re.compile(r"\b(?:mention(?:s|ed|ing)?|about|regarding|referencing|refer(?:s|red|ring)?\s+to|related\s+to|relating\s+to|involving|contain(?:s|ed|ing)?)\b|-(?:mention(?:s|ed|ing)?|related)\b", re.I)
# Several mention terms joined as alternatives ("'x', 'y' or 'z'", "any of"), never "both"/"and".
_ANY_OF = re.compile(r"^(?!.*\b(?:and|both)\b).*\b(?:or|any\s+of|either)\b", re.S)


def _between(text: str, needles: set[str]) -> str:
    """The question text from the first to the last occurrence of the needles."""
    spans = [(text.find(n), text.find(n) + len(n)) for n in needles if n in text]
    return text[min(a for a, _ in spans) : max(b for _, b in spans)] if spans else ""


DROP_REASON = {"not_answerable": "planner_not_answerable", "refused": "sql_refused", "unsupported": "sql_unsupported", "error": "sql_error"}
DROP_DETAIL = {
    "not_answerable": "the SQL route reported that the projection catalog cannot express the sub-question",
    "refused": "the SQL route's statement was refused by the guard or the read-only database",
    "unsupported": "the SQL route's statement compares a value the catalog does not hold",
    "error": "the SQL route could not write a statement",
}
ERROR_CATEGORY = {"budget_exhausted": "budget_exhausted", "timeout": "timeout", "schema_violation": "schema_violation", "projection_unavailable": "runtime_unavailable", "sqlglot_missing": "runtime_unavailable", "role_missing": "runtime_unavailable"}


@dataclass
class Failure:
    """One SQL-route step that did not execute, with what the decision records for it."""

    step: Any  # the plan's ``TypedStep``
    result: SqlSurfaceResult

    @property
    def code(self) -> str | None:
        return (self.result.error or {}).get("code")

    def dropped(self) -> Any:
        from ..decision.base import DroppedStep

        code = f"; code {self.code}" if self.code else ""
        return DroppedStep(arm="typed_query", reason=DROP_REASON[self.result.status], detail=f"{DROP_DETAIL[self.result.status]} ({self.result.model_calls} model call(s){code})")  # type: ignore[arg-type]

    def constraints(self) -> list[UnsupportedConstraint]:
        status = self.result.status
        if status == "not_answerable":
            return [UnsupportedConstraint(constraint_text=ROUTE, reason="planner_not_answerable", suggestion=DROP_DETAIL[status])]
        if status == "error":
            return [UnsupportedConstraint(constraint_text=ROUTE, reason="sql_route_failed", suggestion=f"{DROP_DETAIL[status]} ({self.code or 'error'})")]
        out = list(self.result.unsupported)
        return out or [UnsupportedConstraint(constraint_text=ROUTE, reason="sql_refused", suggestion=f"{DROP_DETAIL[status]} ({self.code or status})")]

    def category(self) -> str:
        status = self.result.status
        if status == "not_answerable":
            return "no_plan"
        if status == "unsupported":
            return "unresolvable_value"
        if status == "refused":
            return "invalid_step"
        code = self.code or ""
        return ERROR_CATEGORY.get(code, "timeout" if "timeout" in code else "provider_error")


def _error(code: str, detail: str, fingerprint: str = "") -> SqlSurfaceResult:
    return SqlSurfaceResult(status="error", error={"code": code, "detail": detail[:300]}, catalog_fingerprint=fingerprint)


def run(
    engine: Any, ctx: Any, arm: Any, question: str, acl: list[Predicate], filters: dict[str, JsonScalar], *, arm_cards: bool = False, grounding: str | None = None, collection: bool = True
) -> SqlSurfaceResult:
    """The sub-question through the SQL-writing role and the arm, for this request's ACL and filters.
    ``arm_cards``: the SQL writer prompt also states the configured limits and refusal rules.
    ``grounding``: the user's own question, for the guard's cross-field value rule (2026-11-09).
    ``collection: False`` (a support question on the unified collection; staging round 5, N13): the
    SQL writer sees the projection's tables only, as without the log member; the statement is still
    guarded and executed by the arm over the request's whole surface."""
    from ..providers import JsonModel
    from ..providers.accounting import RunBudgetMeter

    try:
        session = open_session(arm, ctx, acl, filters, grounding, collection=collection)
    except SqlSurfaceUnavailable as error:
        return _error("projection_unavailable", str(error))
    name = arm.cfg.sql_surface.role
    role = getattr(engine.config.models.roles, name, None)
    if role is None or role.provider == "none":
        return _error("role_missing", f"models.roles.{name} is not configured", session.catalog.fingerprint)
    meter = RunBudgetMeter(ctx, STAGE, role.price_key())
    try:
        limits = ""
        if arm_cards:
            from ..decision.arm_cards import sql_limits_block

            limits = sql_limits_block(arm.cfg)
        return SqlSurfacePlanner(session, budget=meter, limits=limits).plan(question, session.catalog, JsonModel(role))
    except BudgetExhausted as error:
        return _error("budget_exhausted", str(error), session.catalog.fingerprint)
    except DeadlineExceeded as error:
        return _error("timeout", str(error), session.catalog.fingerprint)


def executed_unapplied(evidence: dict[str, Any], query: str, filters: dict[str, JsonScalar], schema: MetadataSchema, resolver: Any, intents: Any) -> list[UnsupportedConstraint]:
    """Question qualifiers the executed SQL does not express. A qualifier counts as applied only by a
    grounded comparison on its field that is a conjunct of the outermost ``WHERE``/``HAVING``
    (``checked_values[].filter``: ``positive``; ``negated`` under one ``NOT`` or as ``<>``); a field
    named without a value by any reference to the column; a time period by such a comparison on a
    declared datetime/date column. Conservative: a comparison under ``OR``/``CASE``, in a join
    condition, a filter subquery or a derived table beside a join never counts; the one exception
    (2026-11-09 F4) is the condition of a conditional measure of the outermost statement (``filter:
    numerator``, guard ``_numerator_position``) when the question asks a share/ratio. Since B5, a
    derived table or CTE the outermost statement reads as its only source counts as the outermost
    statement (guard ``_effective_roots``)."""
    from ..decision.intent import untargeted_constraint
    from ..decision.qualifiers import NO_OPERATOR, qualifier_candidates, unapplied

    lineage = evidence.get("sql_surface") or {}
    predicates: list[Predicate] = []
    # a share question's qualifier may sit in the ratio numerator (F4): COUNT(CASE WHEN c THEN 1 END)
    applying = {"positive", "negated", *({"numerator", "numerator_negated"} if "share" in intents.kinds() else set())}
    for check in lineage.get("checked_values") or []:
        column = str(check.get("column", "")).split(".", 1)[-1]
        position = check.get("filter")
        if schema.field(column) is None or check.get("value") is None or position not in applying:
            continue  # only a conjunct of the outermost WHERE/HAVING (or a share's numerator) filters the result
        try:
            predicates.append(Predicate(field=column, op="eq", value=check["value"], negate=position in {"negated", "numerator_negated"}))
        except ValidationError:
            continue  # not representable: the qualifier stays unapplied (conservative)
    referenced = {str(c).split(".", 1)[-1] for c in lineage.get("columns_referenced") or []}
    candidates = qualifier_candidates(query, schema, resolver, intents)
    pending = [c for c in candidates if not (c.kind == "field" and c.value is None and not c.compare and c.field in referenced)]
    # F6: a LIKE/instr on a free-text column (the only columns the guard allows it on) that is a
    # conjunct of the outermost WHERE applies a quoted phrase it contains, and, when the question
    # asks about mentions ("mention", "about", "containing" ...), a field value it contains ("tickets
    # that mention billing" is not "module = Billing"). Without a mention cue the value stays unapplied.
    text_patterns = lineage.get("text_patterns") or []
    patterns = [str(p.get("pattern", "")).casefold() for p in text_patterns if p.get("filter") == "positive"]
    # 2026-11-09 (B4 q037/q041/q044): an OR group of text matches that is a conjunct of the
    # outermost WHERE (guard ``filter: any_of``) applies a phrase it contains when every needle of
    # the group is a word of the question (no unrequested term widens the match) and, for several
    # distinct needles, the question joins them with "or" ("mention 'x', 'y' or 'z'").
    groups: dict[Any, list[str]] = {}
    for p in text_patterns:
        if p.get("filter") == "any_of":
            groups.setdefault(p.get("group"), []).append(str(p.get("pattern", "")).casefold())
    folded = query.casefold()
    for members in groups.values():
        needles = {m.strip("%") for m in members}
        if all(n and n in folded for n in needles) and (len(needles) == 1 or _ANY_OF.search(_between(folded, needles))):
            patterns.extend(members)
    mention = bool(_MENTION.search(query))

    def needle(c: Any) -> str:
        return (c.text if c.token else c.value or "").casefold()

    pending = [
        c
        for c in pending
        if not ((c.kind == "quoted" or (mention and c.kind == "value" and not c.negated)) and isinstance(c.value, str) and any(needle(c) in p for p in patterns))
    ]
    missing = unapplied(pending, ScopeSpec(predicates=predicates), filters, schema)
    if not intents.aggregate() and evidence.get("operation") == "sql_aggregate":
        # a row lookup/list answers a lookup question; an aggregate needs an operator in the question
        missing.extend([untargeted_constraint(u) for u in intents.untargeted] or [NO_OPERATOR])
    return missing


def _merge(*groups: list[UnsupportedConstraint]) -> list[UnsupportedConstraint]:
    out: list[UnsupportedConstraint] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for item in group:
            if (item.constraint_text, item.reason) not in seen:
                seen.add((item.constraint_text, item.reason))
                out.append(item)
    return out


def apply_failures(decision: Any, failures: list[Failure], *, policy: str, on_failure: str, request: Any, capabilities: Any, trace: Any) -> Any:
    """The decision after its failed SQL-route steps, per ``on_step_failure`` (module docstring)."""
    from ..decision.base import Decision, DroppedStep, Plan, RetrievalStep, StructuredStep, TypedStep, abstain, raw_all_arms

    dropped = [*decision.dropped_steps, *(f.dropped() for f in failures)]
    flags = [UnsupportedConstraint(constraint_text=a.text, reason="aggregate_intent_without_scope", suggestion="the typed_query SQL route for this aggregate did not execute and was dropped") for a in decision.intents.aggregates]
    unsupported = _merge(decision.unsupported, *(f.constraints() for f in failures), flags)
    tolerant = policy != "abstain" and request.aggregate is None and request.scope is None
    failed = {id(f.step) for f in failures}
    kept = [s for s in decision.plan.steps if id(s) not in failed]
    fallback = decision.fallback
    if tolerant and policy == "retrieval_only":
        for step in kept:
            if isinstance(step, (StructuredStep, TypedStep)):
                dropped.append(DroppedStep(arm=step.arm, reason="step_not_executable", detail="dropped under on_step_failure retrieval_only: a typed_query SQL route step did not execute"))
        kept = [s for s in kept if not isinstance(s, (StructuredStep, TypedStep))]
    if tolerant and not kept:
        arms = [arm for arm in capabilities.arms if arm in {"lexical", "dense"}]
        kept = [RetrievalStep(arm=arm, query=request.query) for arm in arms]  # type: ignore[arg-type]
        fallback = "retrieval_after_dropped_steps" if kept else None
    category = failures[0].category()
    trace.event(
        "sql_route_dropped",
        count=len(failures),
        statuses=sorted({f.result.status for f in failures}),
        codes=sorted({f.code for f in failures if f.code}),
        on_step_failure=policy,
        tolerant=tolerant,
        fallback=fallback if tolerant and kept else None,
        arms=[s.arm for s in kept] if tolerant else [],
    )
    if tolerant and kept:
        scope = decision.scope if decision.scope is not None and any(isinstance(s, StructuredStep) for s in kept) else None
        # Plan/model_copy keep the step objects (the engine keys executed routes by identity)
        updated = decision.model_copy(update={"plan": Plan(steps=kept), "scope": scope, "unsupported": unsupported, "dropped_steps": dropped, "fallback": fallback})
        Decision.model_validate(updated.model_dump())  # the coherence rules still hold
        return updated
    note = f"a typed_query SQL route step did not execute ({', '.join(sorted({f.result.status for f in failures}))}); on_step_failure {policy}"
    if on_failure == "raw_all_arms":
        failed_decision = raw_all_arms(request, decision.intents, capabilities, agent=decision.agent, stage=decision.stage, failure=category, unsupported=unsupported)  # type: ignore[arg-type]
    else:
        failed_decision = abstain(decision.intents, agent=decision.agent, stage=decision.stage, failure=category, unsupported=unsupported, note=note)  # type: ignore[arg-type]
    return failed_decision.model_copy(update={"dropped_steps": dropped})


def refusal_record(ctx: Any, result: SqlSurfaceResult, constraints: list[UnsupportedConstraint]) -> dict[str, Any]:
    """A non-executed route as a refused, ``bounded`` aggregate record (review hops only: a hop has
    no step-failure policy to apply, so the refusal is reported in place, never dropped silently)."""
    return {
        "evidence": "aggregate_record",
        "operation": "sql_refused",
        "sql_derived": True,
        "generation": ctx.generation.generation,
        "sql_surface": {"status": result.status, "error": result.error, "catalog": result.catalog_fingerprint or None},
        "refused": True,
        "exactness": "bounded",
        "value": None,
        "total": None,
        "denominator": 0,
        "unsupported": [u.model_dump(mode="json") for u in constraints],
    }
