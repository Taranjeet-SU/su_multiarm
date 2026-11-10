"""Worst-case spend check of an answer plan (docs/plans/unified_merge.md §6.13; M0).

For every pipeline the answer's model-call **slots** are listed from the config:

* planner: ``1 + pipeline.decision.schema_repair_attempts + decision.typed_query.repair_attempts``
  when the pipeline uses the llm planner (the typed re-plan is a planner call too);
* typed router: one ``router_role`` call with ``typed_query.routing: router``, then the typed IR
  call (mode ``ir``) or the SQL writer;
* SQL writer: ``1 + sql_surface.repair_attempts`` (``sql_surface.role``) when the typed_query arm
  runs in ``mode: sql_surface``;
* reviewer: ``pipeline.review.max_hops`` when review is enabled;
* generator: ``1``, plus one truncation retry when ``on_incomplete`` is not ``fail``, plus
  ``pipeline.generation.schema_repair_attempts`` schema repairs (staging round 6, N15: the generator
  prompt with ``GENERATOR_REPAIR_PROMPT``, the evidence and the previous reply, at most the role's
  ``max_tokens``);
* community: one call when ``models.roles.community`` is configured (an answer-time role of the
  ``serving.answers`` ledger, ``runtime/answers_spend.ANSWER_ROLES``);
* every slot of a role with ``retry_transient`` is followed by ``attempts`` transient-retry slots.

For a slot of role ``r``: ``I = (prompt_chars(r) + evidence_chars(r) + catalog_chars(r)) / 3``
tokens (characters / 3, conservative) and ``O = r.max_tokens`` (the retry ceiling for a
``retry_larger`` slot); ``cost = price_table.cost(r, I, O)``. ``prompt_chars`` is measured from the
rendered system prompt plus the reply schema JSON; ``evidence_chars`` is the packing budget
(the generator role's ``evidence_budget`` or ``selection.packing.budget_chars``, raised to
``request.evidence_chars_max`` when a request may ask for more; the plan's formula says ``min``, which
would understate a request that raises its own budget, so the larger value is used); the planner's
catalog is the metadata schema plus the logs planner context bound
(``logs.intents.planner_context_max_chars``). Under the two-halves planner
(``crosssource.planner.enabled``) the logs planner context is never sent (the knowledge half plans
without it; the operations half is deterministic), so the planner's catalog is the metadata schema
plus the source cards (measured from config) and an allowance for the operations half they carry
(``OPERATIONS_HALF_CHARS`` per operations call; staging round 2, N6). What cannot be measured from config alone (arm
cards, the typed/SQL catalog, the question and record context) is listed in ``unmeasured``.

``W`` = the sum of the ``budgets.max_llm_calls`` most expensive slots, transient-retry slots
excluded (the plan's formula); ``W_transient`` includes them and is reported beside it.
``budgets.plan_check``: ``off`` | ``warn`` (default: a startup warning and a ``/ready`` note) |
``refuse`` (``ConfigError`` when ``W > budgets.max_usd`` or a slot cannot be priced). Deterministic;
reads no network and no data.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Any

from ..core.errors import ConfigError

LOGGER = logging.getLogger(__name__)
CHARS_PER_TOKEN = 3
COMMUNITY_EVIDENCE_DEFAULT = 30000  # ``ingestion/write.py``: the community role's default budget
# Allowance per planned operations call in the source cards' ``operations_half`` (the call's
# arguments and its stated notes); a request plans at most ``2 * max_operations_calls + 2`` calls
# (customer tenants, the liveness failure episodes and the clock reading's episodes).
OPERATIONS_HALF_CHARS = 1500


@dataclass(frozen=True)
class Slot:
    pipeline: str
    stage: str
    role: str
    pricing_key: str
    input_tokens: int
    output_tokens: int
    usd: float | None
    retry: str | None = None  # ``incomplete`` | ``transient``


@dataclass
class PlanCheckReport:
    mode: str
    status: str  # ok | exceeds | unpriced | no_usd_cap | no_price_table | no_model_calls | off
    max_usd: float | None = None
    max_llm_calls: int | None = None
    worst_case_usd: float | None = None
    worst_case_with_transient_usd: float | None = None
    pipeline: str | None = None
    slots: list[Slot] = field(default_factory=list)
    unpriced: list[str] = field(default_factory=list)
    unmeasured: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["slots"] = [asdict(s) for s in self.slots]
        return out

    def note(self) -> dict[str, Any]:
        """The compact ``/ready`` note."""
        return {k: v for k, v in self.as_dict().items() if k in {"mode", "status", "max_usd", "max_llm_calls", "worst_case_usd", "worst_case_with_transient_usd", "pipeline", "unpriced"}}

    def message(self) -> str:
        if self.status == "exceeds":
            return (
                f"budgets.plan_check: worst-case answer spend W = US${self.worst_case_usd:.4f} (pipeline {self.pipeline}, "
                f"{self.max_llm_calls} most expensive call slots) exceeds budgets.max_usd US${self.max_usd:.4f}; requests that reach "
                "the worst case will be refused with 429 budget exhausted"
            )
        if self.status == "unpriced":
            return f"budgets.plan_check: no price for {', '.join(self.unpriced)} in budgets.price_table; the worst case cannot be computed"
        if self.status == "ok" and self.worst_case_usd is not None and self.max_usd is not None:
            return f"budgets.plan_check: ok (W = US${self.worst_case_usd:.4f} <= max_usd US${self.max_usd:.4f})"
        return f"budgets.plan_check: {self.status}" + (f" (W = US${self.worst_case_usd:.4f})" if self.worst_case_usd is not None else "")


def _tokens(chars: int) -> int:
    return max(1, math.ceil(chars / CHARS_PER_TOKEN))


def _schema_chars(schema: Any) -> int:
    try:
        return len(json.dumps(schema.model_json_schema()))
    except Exception:  # noqa: BLE001 - a schema that cannot be rendered is measured as zero and listed
        return 0


def _evidence_chars(config: Any, pipe: Any, role: Any) -> int:
    budget = role.evidence_budget.chars if role is not None and role.evidence_budget else pipe.selection.packing.budget_chars
    ceiling = config.request.evidence_chars_max
    return max(budget, ceiling) if ceiling is not None else budget


def _prompts() -> dict[str, Any]:
    """Built-in prompts and reply schemas per stage (imported lazily: decision/runtime modules)."""
    from ..decision.llm import LlmPlan, REPAIR_PROMPT
    from ..decision.typed_plan import TYPED_REPAIR_PROMPT
    from ..decision.typed_router import IR_PROMPT, ROUTER_PROMPT, TypedRoute
    from ..models import Answer
    from ..pipeline.review import Review
    from ..profiles.prompts import COMMUNITY_PROMPT, GENERATOR_PROMPT, GENERATOR_REPAIR_PROMPT, PLANNER_PROMPT, REVIEW_PROMPT
    from ..sql_surface.planner import SqlDraft
    from ..sql_surface.prompts import SYSTEM_PROMPT as SQL_PROMPT, USER_PROMPT as SQL_USER_PROMPT

    return {
        "planner": (PLANNER_PROMPT, LlmPlan),
        "repair_suffix": (REPAIR_PROMPT, None),
        "typed_repair_suffix": (TYPED_REPAIR_PROMPT + REPAIR_PROMPT, None),
        "typed_router": (ROUTER_PROMPT, TypedRoute),
        "typed_ir": (IR_PROMPT, None),
        "sql_writer": (SQL_PROMPT + SQL_USER_PROMPT, SqlDraft),
        "review": (REVIEW_PROMPT, Review),
        "generator": (GENERATOR_PROMPT, Answer),
        "generator_repair_suffix": (GENERATOR_REPAIR_PROMPT, None),
        "community": (COMMUNITY_PROMPT, None),
    }


def slots_for(config: Any, price_table: Any) -> tuple[list[Slot], list[str], list[str]]:
    """Every pipeline's call slots, the unpriced pricing keys and the unmeasured inputs."""
    prompts = _prompts()
    roles = config.models.roles
    slots: list[Slot] = []
    unpriced: set[str] = set()
    unmeasured: set[str] = {"question and record_context text"}
    logs = getattr(config, "logs", None)
    planner_context = logs.intents.planner_context_max_chars if logs is not None and logs.intents.enabled and logs.intents.planner_context else 0
    two_halves = getattr(getattr(config, "crosssource", None), "planner", None)
    if two_halves is not None and two_halves.enabled:
        planner_context = _source_cards_chars(config, two_halves)
    schema_chars = len(json.dumps(config.metadata_schema.model_dump(mode="json")))

    def add(pipe_name: str, stage: str, role_name: str, *, prompt_key: str, prompt_override: str | None = None, evidence: int = 0, catalog: int = 0, count: int = 1) -> None:
        role = getattr(roles, role_name, None)
        if role is None:
            return
        text, schema = prompts[prompt_key]
        if prompt_override:
            text = prompt_override
        prompt_chars = len(text) + (_schema_chars(schema) if schema is not None else 0)
        input_tokens = _tokens(prompt_chars + evidence + catalog)
        key = role.price_key()

        def priced(output_tokens: int) -> float | None:
            if price_table is None or key not in price_table.prices:
                unpriced.add(key)
                return None
            return price_table.cost(key, input_tokens, output_tokens)

        transient = role.retry_transient.attempts if role.retry_transient is not None else 0
        for _ in range(count):
            slots.append(Slot(pipe_name, stage, role_name, key, input_tokens, role.max_tokens, priced(role.max_tokens)))
            slots.extend(Slot(pipe_name, stage, role_name, key, input_tokens, role.max_tokens, priced(role.max_tokens), "transient") for _ in range(transient))
        if stage == "generator" and role.on_incomplete != "fail":
            ceiling = role.retry_output_tokens() or role.max_tokens
            slots.append(Slot(pipe_name, stage, role_name, key, input_tokens, ceiling, priced(ceiling), "incomplete"))
            slots.extend(Slot(pipe_name, stage, role_name, key, input_tokens, ceiling, priced(ceiling), "transient") for _ in range(transient))

    for name, pipe in config.all_pipelines().items():
        decision = pipe.decision
        typed = decision.typed_query
        arm = getattr(pipe.arms, "typed_query", None)
        if decision.uses_llm():
            catalog = schema_chars + planner_context
            if decision.arm_cards:
                unmeasured.add("arm cards (pipeline.decision.arm_cards)")
            if typed.enabled and typed.routing == "planner":
                unmeasured.add("typed catalog in the planner payload")
            planner_prompt = decision.prompt or prompts["planner"][0]  # repairs resend it with a suffix (decision/llm.py)
            add(name, "planner", "planner", prompt_key="planner", prompt_override=planner_prompt, catalog=catalog)
            add(name, "planner", "planner", prompt_key="planner", prompt_override=planner_prompt + prompts["repair_suffix"][0], catalog=catalog, count=decision.schema_repair_attempts)
            add(name, "planner", "planner", prompt_key="planner", prompt_override=planner_prompt + prompts["typed_repair_suffix"][0], catalog=catalog, count=typed.repair_attempts)
        if typed.enabled and typed.routing == "router":
            unmeasured.add("typed catalog summary of the router")
            add(name, "typed_router", typed.router_role, prompt_key="typed_router")
            if arm is None or arm.mode == "ir":
                add(name, "typed_ir", typed.router_role, prompt_key="typed_ir")
        if arm is not None and arm.mode == "sql_surface":
            unmeasured.add("SQL catalog of the SQL writer")
            add(name, "sql_writer", arm.sql_surface.role, prompt_key="sql_writer", count=1 + arm.sql_surface.repair_attempts)
        if pipe.review.enabled:
            add(name, "review", "reviewer", prompt_key="review", prompt_override=pipe.review.prompt, evidence=_evidence_chars(config, pipe, roles.generator), count=pipe.review.max_hops)
        add(name, "generator", "generator", prompt_key="generator", prompt_override=pipe.generation.prompt, evidence=_evidence_chars(config, pipe, roles.generator))
        if pipe.generation.schema_repair_attempts and roles.generator is not None:
            # the repair resends the generator prompt with a suffix, the evidence and the previous reply
            add(
                name, "generator", "generator", prompt_key="generator",
                prompt_override=(pipe.generation.prompt or prompts["generator"][0]) + prompts["generator_repair_suffix"][0],
                evidence=_evidence_chars(config, pipe, roles.generator), catalog=roles.generator.max_tokens * CHARS_PER_TOKEN,
                count=pipe.generation.schema_repair_attempts,
            )
        community = roles.community
        add(name, "community", "community", prompt_key="community", prompt_override=config.communities.prompt,
            evidence=community.evidence_budget.chars if community is not None and community.evidence_budget else COMMUNITY_EVIDENCE_DEFAULT)
    return slots, sorted(unpriced), sorted(unmeasured)


def _source_cards_chars(config: Any, planner: Any) -> int:
    """The two-halves planner's extra catalog: the source cards (every knowledge arm) and the
    operations half allowance; ``0`` without log intents (no operations half, no cards)."""
    logs = getattr(config, "logs", None)
    if logs is None or not logs.intents.enabled:
        return 0
    from ..decision.source_cards import KNOWLEDGE_ARMS, static_cards

    cards = len(json.dumps(static_cards(config, list(KNOWLEDGE_ARMS)), default=str))
    return cards + OPERATIONS_HALF_CHARS * (2 * planner.max_operations_calls + 2)


def worst_case(slots: list[Slot], max_calls: int, *, transient: bool) -> float:
    costs = sorted((s.usd or 0.0 for s in slots if transient or s.retry != "transient"), reverse=True)
    return round(sum(costs[:max_calls]), 6)


def plan_check(config: Any, price_table: Any) -> PlanCheckReport:
    budgets = config.budgets
    mode = budgets.plan_check
    report = PlanCheckReport(mode=mode, status="off", max_usd=budgets.max_usd, max_llm_calls=budgets.max_llm_calls)
    if mode == "off":
        return report
    slots, unpriced, unmeasured = slots_for(config, price_table)
    report.slots, report.unpriced, report.unmeasured = slots, unpriced, unmeasured
    if price_table is None:
        report.status = "no_price_table" if slots else "no_model_calls"
        return report
    if unpriced:
        report.status = "unpriced"
        return report
    best: tuple[float, float, str | None] = (0.0, 0.0, None)
    for name in config.all_pipelines():
        mine = [s for s in slots if s.pipeline == name]
        w = worst_case(mine, budgets.max_llm_calls, transient=False)
        if best[2] is None or w > best[0]:
            best = (w, worst_case(mine, budgets.max_llm_calls, transient=True), name)
    report.worst_case_usd, report.worst_case_with_transient_usd, report.pipeline = best
    if budgets.max_usd is None:
        report.status = "no_usd_cap"
    else:
        report.status = "exceeds" if report.worst_case_usd > budgets.max_usd else "ok"
    return report


def enforce(report: PlanCheckReport) -> PlanCheckReport:
    """``warn``: log a warning for ``exceeds``/``unpriced``. ``refuse``: ``ConfigError`` for those
    (only when ``budgets.max_usd`` is set; there is nothing to exceed otherwise). Called by
    ``Engine.__init__`` (startup of every server, CLI and Python API engine)."""
    problem = report.status == "exceeds" or (report.status == "unpriced" and report.max_usd is not None)
    if not problem:
        if report.status not in {"off"}:
            LOGGER.info("%s", report.message())
        return report
    if report.mode == "refuse":
        raise ConfigError(report.message() + " (budgets.plan_check: refuse)", "budgets.plan_check")
    LOGGER.warning("%s", report.message())
    return report
