"""The two-halves planner over the unified collection (docs/plans/unified_merge.md §6.11; M2b-2).

``crosssource.planner.enabled`` wraps the pipeline's decision agent (rules, classifier, llm or
cascade) in ``TwoHalvesAgent``. Per request:

1. **Operations half, deterministic.** Only a question that asks for operations evidence has one:
   it names a ``crosssource.planner.operations_terms`` word (whole word) or a failure symptom
   (``log_intents.operations_evidence``; staging round 2, N2). Then today's log detectors run first (``DecisionAgent.log_step``:
   the typed intents, ``issue_history``, ``log_liveness``, the outage investigation), then the
   unified detector (``log_intents.detect_operations``) for support-shaped questions with a failure
   symptom about a grounded subject. Grounding: the M0 question binding (vocabulary, question clock
   with both 12-hour readings stated, the default live view of ``default_view_days`` for undated
   outage/presence questions); customers named in the question through the pinned identity
   registry (``values.ground_customers``: matched aliases only) filter the operations half to their
   log tenants, or the operations half is refused for them (stated, never widened). A clock hour
   that a liveness reading cannot isolate is stated; with "what was the error" the failure episodes
   of the readings' days are added. A search-ranking question names search analytics, which the
   collection may not serve (stated in coverage).
2. **Knowledge half = today's plan.** The wrapped agent decides with the log capabilities removed,
   so a question without an operations half gets exactly the decision (and, for the llm planner,
   exactly the payload) it gets today. With an operations half the llm planner also receives the
   source cards (``decision/source_cards.py``) and the operations half already planned.
3. **Merge.** Both halves' steps in one plan (``policy_applied: two_halves``); a deliberate refusal
   of the knowledge half keeps the operations half and states the refusal; a planner failure is
   replaced by the labelled deterministic plan (``decision/fallback_plan.py``) when
   ``crosssource.planner.fallback`` is ``deterministic_plan``.

The halves, the customer grounding and the refusals are recorded on the decision (``note``,
``unsupported``) and returned by ``TwoHalvesAgent.last`` for the engine's per-source coverage.

**Source-scoped knowledge half** (staging finding F2, 2026-11-10): when the collection holds sources
outside the knowledge sources (a log member), the engine limits every knowledge-half retrieval and
structured step to ``source in knowledge_sources`` at execution (``knowledge_predicate``; never
written into the plan), so the operations half alone reads logs and a support-only question reads
exactly the units it reads without the log member. ``exact_id`` is not scoped (a named identifier
resolves where it lives); an arm served only by layers of knowledge sources needs no scope; a
request filter on ``source`` is the caller's and wins. ``crosssource.planner.scope_knowledge_steps:
false`` turns this off. Coverage labels each step's half by the sources it reads (``step_half``)
and derives the log entry from the pinned log members and the log records actually in the hits.

**Support-only questions keep today's path** (staging round 2, 2026-11-10): the knowledge half's
value listings are scoped like its steps (``Capabilities.value_scope``), so planner values, value
grounding and qualifier checks see the knowledge sources' values only; the log scope guard passes a
knowledge step whose executed source scope admits no log source (``log_guard.log_sources``, N1); and
when the fallback replaced a planner abstention on a question without operations steps, an executed
typed route that answers the aggregate is kept alone (``typed_route_only``), as today (N5, engine
``_apply_typed_route``). The logs planner context (``logs.intents.planner_context``) is never sent by
the two-halves planner: the knowledge half plans without it and the operations half is
deterministic; ``runtime/plan_cost`` counts the source cards instead (N6).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from ..core.types import Predicate
from ..models import UnsupportedConstraint
from .base import Capabilities, Decision, DecisionFailure, DecisionRequest, Plan, RetrievalStep, StructuredStep, log_decision
from .fallback_plan import deterministic_plan, eligible
from .intent import Intents
from .log_intents import INPUT_MODELS, LogIntentCall

POLICY = "two_halves"
POLICY_OPERATIONS_ONLY = "two_halves_operations_only"
INVESTIGATIONS = frozenset({"log_outage_investigation", "log_shared_errors"})
# Arms a knowledge-half step may be source-scoped on. ``exact_id`` is not: an identifier named in the
# question resolves wherever it lives (the hit is still counted under its source in coverage).
SCOPED_ARMS = ("lexical", "dense", "graph", "community", "segment", "structured")
# Arms that read only their declared layers when layers serve them (no member read).
LAYER_ARMS = frozenset({"graph", "community", "segment"})
# The log source of a single-store (non-partitioned) collection with a ``logs`` block.
LOG_SOURCE = "logs"


def operation_sources(config: Any, knowledge: list[str]) -> list[str]:
    """``Document.source`` values of the operations half: the sources of members produced by a log
    host (or fed every few minutes) that are not knowledge sources (partitioned); ``logs`` for one
    store with a ``logs`` block; none otherwise."""
    collection = getattr(config, "collection", None)
    if collection is None:
        return [LOG_SOURCE] if getattr(config, "logs", None) is not None and LOG_SOURCE not in knowledge else []
    out: list[str] = []
    for name in sorted(collection.members):
        member = collection.members[name]
        if member.producer.kind == "log_host" or member.cadence == "minutes":
            out.extend(s for s in member.sources if s not in knowledge and s not in out)
    return out


def scoped_arms(config: Any, knowledge: list[str], enabled: bool = True) -> frozenset[str]:
    """Arms whose knowledge-half steps need the ``source in knowledge`` scope: those that can reach
    a source outside ``knowledge``. Empty without such a source (today's steps, unchanged)."""
    if not enabled or not knowledge:
        return frozenset()
    known = set(knowledge)
    collection = getattr(config, "collection", None)
    if collection is None:
        return frozenset(SCOPED_ARMS) if getattr(config, "logs", None) is not None else frozenset()
    every = {s for member in collection.members.values() for s in member.sources}
    if not every - known:
        return frozenset()
    out = set()
    for arm in SCOPED_ARMS:
        layers = [layer for layer in collection.layers if arm in layer.arms]
        reach = {s for layer in layers for s in layer.sources} if arm in LAYER_ARMS and layers else every
        if reach - known:
            out.add(arm)
    return frozenset(out)


def _named_values(predicates: list[Any], field_name: str) -> set[str] | None:
    allowed: set[str] | None = None
    for p in predicates:
        if p.field != field_name or p.negate or p.op not in {"eq", "in"}:
            continue
        values = {str(p.value)} if p.op == "eq" else {str(v) for v in (p.value or [])}
        allowed = values if allowed is None else allowed & values
    return allowed


def step_half(step: Any, knowledge: list[str], kind_sources: dict[str, set[str]] | None = None) -> str:
    """The half a plan step belongs to, by the sources it reads: a typed log intent or a log
    investigation scope reads logs (``operations``); a structured or retrieval step whose own scope
    names only non-knowledge sources (or record kinds held only by them) is ``operations``; every
    other step (a ticket count, a SQL route, a question-text search) is ``knowledge``."""
    if isinstance(step, StructuredStep) and step.intent is not None:
        return "operations"
    if getattr(step, "log_scope", None) is not None:
        return "operations"
    if isinstance(step, StructuredStep):
        predicates = list(step.scope.predicates)
    elif isinstance(step, RetrievalStep):
        predicates = [Predicate(field=k, op="eq", value=v) for k, v in step.filters.items()]
    else:
        return "knowledge"
    known = set(knowledge)
    sources = _named_values(predicates, "source")
    if sources and not sources & known:
        return "operations"
    kinds = _named_values(predicates, "kind")
    if kinds and kind_sources:
        reach = {s for k in kinds for s in kind_sources.get(k, set())}
        if reach and not reach & known:
            return "operations"
    return "knowledge"


@dataclass
class OperationsHalf:
    """The deterministic operations half of one request."""

    steps: list[Any] = field(default_factory=list)  # StructuredStep (typed log intents) / RetrievalStep (log_scope)
    notes: list[str] = field(default_factory=list)
    unsupported: list[UnsupportedConstraint] = field(default_factory=list)
    origin: str | None = None  # "log_step" (today's detectors) | "unified" (detect_operations)
    refused: str | None = None  # why the operations half was not planned although asked
    analytics: bool = False  # the question asks search analytics
    customers: Any = None  # values.CustomerGrounding
    knowledge: bool = True  # whether a knowledge half is planned beside it

    def calls(self) -> list[dict[str, Any]]:
        out = []
        for step in self.steps:
            if isinstance(step, StructuredStep) and step.intent is not None:
                out.append({"intent": step.intent.intent, "args": dict(step.intent.args)})
            elif getattr(step, "log_scope", None) is not None:
                out.append({"arm": step.arm, "log_scope": step.log_scope.model_dump(mode="json", exclude_none=True)})
        return out

    def asked(self) -> bool:
        return bool(self.steps or self.refused or self.analytics)


@dataclass
class TwoHalvesRecord:
    """What the engine reports per request (``trace.source_coverage``)."""

    operations: OperationsHalf | None
    knowledge_outcome: str | None = None
    knowledge_failure: str | None = None
    fallback: bool = False
    # the knowledge decision the labelled fallback replaced (the engine keeps an executed typed
    # route instead, as today, when the question has no operations half: staging round 2, N5)
    replaced: Decision | None = None


class TwoHalvesAgent:
    """A decision agent wrapping the pipeline's agent with the two-halves planner (see module)."""

    def __init__(self, inner: Any, planner: "TwoHalvesPlanner"):
        self.inner = inner
        self.planner = planner
        self.name = inner.name
        self.config = inner.config
        self.deps = inner.deps
        self._local = threading.local()

    @property
    def last(self) -> TwoHalvesRecord | None:
        return getattr(self._local, "record", None)

    def __getattr__(self, name: str) -> Any:  # validate, recipe_fingerprint, intents, ...
        return getattr(self.inner, name)

    def prepare(self, ctx: Any, request: DecisionRequest, intents: Intents | None, capabilities: Capabilities) -> OperationsHalf | None:
        """Plan the deterministic operations half ahead of ``decide`` (staging round 5: the engine
        needs to know before the router and the planner run whether the question has one); the
        next ``decide`` of the same request uses it."""
        intents = intents if intents is not None else self.inner.intents(request, capabilities)
        operations = self.planner.operations(ctx, request, intents, capabilities, self.inner)
        self._local.prepared = (request.query, operations)
        return operations

    def decide(self, ctx: Any, request: DecisionRequest, intents: Intents | None, capabilities: Capabilities) -> Decision:
        intents = intents if intents is not None else self.inner.intents(request, capabilities)
        prepared = getattr(self._local, "prepared", None)
        self._local.prepared = None
        if prepared is not None and prepared[0] == request.query:
            operations = prepared[1]
        else:
            operations = self.planner.operations(ctx, request, intents, capabilities, self.inner)
        knowledge_caps = self.planner.knowledge_capabilities(capabilities, operations, getattr(ctx, "generation", None))
        knowledge = self.inner.decide(ctx, request, intents, knowledge_caps)
        decision, record = self.planner.merge(ctx, request, intents, capabilities, knowledge, operations, self.inner)
        self._local.record = record
        ctx.trace.event(
            "two_halves",
            operations=len(operations.steps) if operations is not None else 0,
            origin=operations.origin if operations is not None else None,
            refused=bool(operations is not None and operations.refused),
            knowledge=knowledge.outcome,
            policy=decision.policy_applied,
            fallback=decision.fallback,
        )
        return decision


class TwoHalvesPlanner:
    """Per-engine settings of the two-halves planner (``crosssource.planner``)."""

    def __init__(self, engine: Any, settings: Any):
        from .source_cards import knowledge_sources

        self.engine = engine
        self.settings = settings
        self.knowledge_sources = knowledge_sources(engine.config)
        self.operation_sources = operation_sources(engine.config, self.knowledge_sources)
        self.scoped_arms = scoped_arms(engine.config, self.knowledge_sources, getattr(settings, "scope_knowledge_steps", True))
        from .log_guard import log_sources

        self.log_sources = log_sources(engine.config)
        collection = getattr(engine.config, "collection", None)
        self.kind_sources: dict[str, set[str]] = {}
        for member in collection.members.values() if collection is not None else []:
            for kind in member.record_kinds:
                self.kind_sources.setdefault(kind, set()).update(member.sources)
        self._static_cards: dict[str, dict[str, Any]] = {}
        self._fields: dict[str, frozenset[str] | None] = {}
        self._lock = threading.Lock()

    # --- support-only questions keep today's schema (staging round 5, N12/N13) -------------------

    def asks_operations(self, question: str, *, log_intents: bool, caller_typed: bool = False) -> bool:
        """Whether ``question`` can have an operations half: log intents enabled, no caller-typed
        operation, and an operations word or failure symptom (the N2 gate of ``operations``). A
        question without one is support-only: its planner payload, its SQL writer catalog and its
        answer contract are today's (``Engine._support_only``)."""
        if caller_typed or not log_intents:
            return False
        from ..core.config_crosssource import OPERATIONS_TERMS
        from . import log_intents as li

        return bool(li.operations_evidence(question, list(getattr(self.settings, "operations_terms", OPERATIONS_TERMS))))

    def knowledge_fields(self, gen: Any) -> frozenset[str] | None:
        """The field names the knowledge members' own sealed schemas declare (cached per generation:
        members are immutable), or ``None`` (unknown, or no non-knowledge source: nothing to drop)."""
        if gen is None or not self.scoped_arms:
            return None
        key = str(getattr(gen, "generation", gen))
        with self._lock:
            if key in self._fields:
                return self._fields[key]
        declared = getattr(getattr(self.engine, "records", None), "declared_fields", None)
        found = declared(gen, self.knowledge_sources) if callable(declared) else None
        with self._lock:
            if len(self._fields) > 64:
                self._fields.clear()
            self._fields[key] = found
        return found

    def knowledge_schema(self, gen: Any, schema: Any) -> Any:
        """``schema`` without the fields no knowledge member declares (the log member's own fields):
        the knowledge half lists, plans and counts over exactly the fields it has without the log
        member (staging round 5, N13: the planner payload carried every log field). Field order and
        declarations stay the collection's; unknown member schemas keep ``schema`` unchanged."""
        names = self.knowledge_fields(gen)
        if names is None:
            return schema
        keep = set(names) | ({schema.equivalence_field} if schema.equivalence_field else set())
        fields = [f for f in schema.fields if f.name in keep]
        if len(fields) == len(schema.fields):
            return schema
        return schema.model_copy(update={"fields": fields})

    # --- source scope of the knowledge half (staging F2) --------------------------------------

    def knowledge_predicate(self, step: Any, request_filters: dict[str, Any]) -> Predicate | None:
        """The ``source in knowledge_sources`` predicate a knowledge-half step executes under, or
        ``None``: an operations step, an unscoped arm, a caller's own ``source`` filter (applied as
        given), or a collection without a non-knowledge source (today's steps, unchanged)."""
        if not self.scoped_arms or getattr(step, "arm", None) not in self.scoped_arms:
            return None
        if "source" in request_filters:
            return None
        if step_half(step, self.knowledge_sources, self.kind_sources) != "knowledge":
            return None
        return Predicate(field="source", op="in", value=list(self.knowledge_sources))

    # --- operations half ---------------------------------------------------------------------

    def _values(self, inner: Any, ctx: Any, capabilities: Capabilities):
        records = (getattr(inner.deps, "backends", None) or {}).get("records")
        schema = capabilities.metadata_schema

        def values(field_name: str) -> list[str]:
            spec = schema.field(field_name)
            if records is None or spec is None or not spec.listable:
                return []
            return [str(v) for v in records.values(ctx.generation, field_name, list(ctx.constraints()), 5000)]

        return values

    def operations(self, ctx: Any, request: DecisionRequest, intents: Intents, capabilities: Capabilities, inner: Any) -> OperationsHalf | None:
        """The operations half of ``request`` (``None``: the question has none)."""
        from . import log_intents
        from .values import ground_customers

        if request.scope is not None or request.aggregate is not None:
            return None  # a caller-typed operation is answered as asked
        question = request.query
        out = OperationsHalf(analytics=log_intents.asks_search_analytics(question))
        if not capabilities.log_intents or "structured" not in capabilities.arms:
            return out if out.asked() else None
        from ..core.config_crosssource import OPERATIONS_TERMS

        evidence = log_intents.operations_evidence(question, list(getattr(self.settings, "operations_terms", OPERATIONS_TERMS)))
        if not evidence:
            # staging round 2 (N2): no operations word or failure symptom in the question; generic
            # words ("new", "mentions", "increase") never start the log detectors
            ctx.trace.event("operations_gate", passed=False)
            return out if out.asked() else None
        values = self._values(inner, ctx, capabilities)
        decision: Decision | None = None
        failure: DecisionFailure | None = None
        try:
            decision = inner.log_step(ctx, request, intents, capabilities)
        except DecisionFailure as error:
            failure = error
        if decision is not None and decision.policy_applied in INVESTIGATIONS and capabilities.log_history and decision.policy_applied != "log_shared_errors":
            decision = None  # on an episode archive an outage question reads the observed episodes (below)
        customers = ground_customers(question, self.engine.identity_view(), list(self.settings.customer_namespaces), self.settings.tenant_namespace)
        out.customers = customers
        if decision is not None:
            out.origin = "log_step"
            out.steps = list(decision.plan.steps) if decision.plan is not None else []
            out.notes = [decision.note] if decision.note and decision.policy_applied in INVESTIGATIONS else []
            out.unsupported = list(decision.unsupported)
        else:
            settings = capabilities.log_question
            vocabulary = list(settings.vocabulary) if settings is not None else []
            from .log_question import ground_terms

            grounded = ground_terms(question, vocabulary, values("service")) if vocabulary else None
            call = log_intents.detect_operations(
                question,
                as_of=request.as_of,
                values=values,
                vocabulary_services=list(grounded.services) if grounded is not None else [],
                customers=[*customers.tenants(), *(c.customer_id for c in customers.ungrounded())],
                history=capabilities.log_history,
                view_days=settings.view_days if settings is not None else 3,
                pin=request.as_of_source == "request",
            )
            if call is None:
                if failure is not None:
                    out.refused = str(failure)
                    out.unsupported = list(failure.unsupported) or [UnsupportedConstraint(constraint_text=question, reason="qualifier_not_applied", suggestion=f"operations evidence was not read: {failure}")]
                return out if out.asked() else None
            if "window" in call.args and log_intents._window_text(question, request.as_of) is None:
                out.notes.append(f"No period was named, so the default live view is shown: {call.args['window']} (logs.collection.serving.default_view_days).")
            try:
                bound = inner.bind_log_question(ctx, request, capabilities, inner.bind_log_scope(ctx, request, capabilities, call))
            except DecisionFailure as error:
                out.refused = str(error)
                out.unsupported.extend(error.unsupported)
                return out
            built = log_decision(bound, intents, agent=inner.name)
            out.origin = "unified"
            out.steps = list(built.plan.steps)  # type: ignore[union-attr]
            out.unsupported.extend(built.unsupported)
        self._ground_customers(out, question)
        if out.steps:
            self._clock(out, ctx, request, capabilities, inner, values)
            self._liveness_failures(out, request, capabilities)
            span = log_intents.hour_span(question)
            if span is not None:
                out.unsupported.append(UnsupportedConstraint(constraint_text=span, reason="qualifier_not_applied", suggestion=f'"{span}" is shorter than a day: whole days are read; the hours are not isolated.'))
        names = [s.intent.intent for s in out.steps if isinstance(s, StructuredStep) and s.intent is not None]
        out.knowledge = log_intents.asks_knowledge(question, names) if out.steps else True
        return out

    def _ground_customers(self, out: OperationsHalf, question: str) -> None:
        """Filter the operations steps to the log tenants of the customers named in the question,
        or refuse them for a customer without a matched tenant (never an unfiltered answer)."""
        grounding = out.customers
        if grounding is None:
            return
        if not grounding.joins_enabled:
            if out.steps:
                out.notes.append(f"Customer names in the question were not resolved: identity joins are disabled ({grounding.reason}).")
            return
        if not grounding.customers:
            return
        out.notes.extend(grounding.statements())
        if grounding.ungrounded():
            names = ", ".join(c.text for c in grounding.ungrounded())
            out.unsupported.append(UnsupportedConstraint(constraint_text=names, reason="qualifier_not_applied", suggestion=f"{names}: no matched log tenant in the identity registry ({grounding.registry_version}); the operations half is not shown rather than widened to every tenant"))
            out.refused = f"customer {names} has no matched log tenant"
            out.steps = []
            return
        tenants = grounding.tenants()[: self.settings.max_operations_calls]
        steps: list[Any] = []
        for step in out.steps:
            if isinstance(step, StructuredStep) and step.intent is not None:
                fields = INPUT_MODELS[step.intent.intent].model_fields
                if step.intent.args.get("tenant") is not None:
                    steps.append(step)
                    continue
                if "tenant" not in fields:
                    out.unsupported.append(UnsupportedConstraint(constraint_text=grounding.customers[0].text, reason="qualifier_not_applied", suggestion=f"{step.intent.intent} cannot filter by tenant; the operations half is not shown for the named customer"))
                    out.refused = f"{step.intent.intent} cannot filter by the named customer"
                    continue
                calls = [LogIntentCall(intent=step.intent.intent, args={**step.intent.args, "tenant": t}) for t in tenants]
                from .log_question import BoundQuestion

                rebuilt = log_decision(BoundQuestion(calls=calls, notes=list(step.notes), unapplied=list(step.unapplied)), Intents(), agent="two_halves")
                steps.extend(rebuilt.plan.steps)  # type: ignore[union-attr]
            elif getattr(step, "log_scope", None) is not None and step.log_scope.tenant is None:
                for t in tenants:
                    steps.append(step.model_copy(update={"log_scope": step.log_scope.model_copy(update={"tenant": t})}))
            else:
                steps.append(step)
        out.steps = steps

    def _clock(self, out: OperationsHalf, ctx: Any, request: DecisionRequest, capabilities: Capabilities, inner: Any, values: Any) -> None:
        """A clock hour the planned intents cannot isolate (liveness reads a lookback) is stated
        with both 12-hour readings; "what was the error" adds the failure episodes of the readings'
        days (episode archive only)."""
        from datetime import datetime, time
        from zoneinfo import ZoneInfo

        from . import log_intents
        from .log_question import read_clock

        settings = capabilities.log_question
        if settings is None:
            return
        pinned = request.pinned_as_of()
        if pinned is not None:
            from dataclasses import replace

            end = datetime.combine(pinned, time(23, 59, 59), ZoneInfo(settings.timezone))
            settings = replace(settings, now=lambda: end)
        reading = read_clock(request.query, settings, request.as_of)
        if reading is None:
            return
        liveness = [s for s in out.steps if isinstance(s, StructuredStep) and s.intent is not None and s.intent.intent == "log_liveness"]
        if not liveness:
            return
        statement = reading.statement() + " Liveness and recovery are read over the lookback; the hour is not isolated."
        if not any(u.constraint_text == reading.text for u in out.unsupported):
            out.unsupported.append(UnsupportedConstraint(constraint_text=reading.text, reason="qualifier_not_applied", suggestion=statement))
        if not (capabilities.log_history and log_intents.asks_failure_detail(request.query)):
            return
        partition = ZoneInfo(settings.logs_timezone)
        days = sorted({m.astimezone(partition).date() for m in reading.local})
        base = liveness[0].intent.args
        args: dict[str, Any] = {"start": days[0].isoformat(), "end": days[-1].isoformat(), "group_by": ["category", "signature_id"], "list_episodes": 20}
        for key in ("service", "tenant", "as_of"):
            if base.get(key) is not None:
                args[key] = base[key]
        from .log_question import BoundQuestion

        note = reading.statement() + " The failure episodes of the reading's day(s) are listed; day rollups do not isolate the hour."
        call = LogIntentCall(intent="issue_history", args=args)
        rebuilt = log_decision(BoundQuestion(calls=[call], notes=[note]), Intents(), agent="two_halves")
        out.steps.extend(rebuilt.plan.steps)  # type: ignore[union-attr]

    def _liveness_failures(self, out: OperationsHalf, request: DecisionRequest, capabilities: Capabilities) -> None:
        """A liveness question that also describes a failure ("stuck in the queue", "stopped")
        gets the failure episodes of the same scope over the default view (or the named period)
        beside the heartbeat reading (episode archive only; once)."""
        from . import log_intents
        from .log_question import BoundQuestion

        if not capabilities.log_history or not log_intents.has_symptom(request.query):
            return
        steps = [s for s in out.steps if isinstance(s, StructuredStep) and s.intent is not None]
        liveness = [s for s in steps if s.intent.intent == "log_liveness"]
        if not liveness or any(s.intent.intent == "issue_history" for s in steps):
            return
        settings = capabilities.log_question
        view_days = settings.view_days if settings is not None else 3
        window = log_intents._window_text(request.query, request.as_of) or f"last {view_days} days"
        calls = []
        for step in liveness[: self.settings.max_operations_calls]:
            base = step.intent.args
            args: dict[str, Any] = {"window": window, "group_by": ["category", "signature_id"], "list_episodes": 20}
            for key in ("service", "tenant", "as_of"):
                if base.get(key) is not None:
                    args[key] = base[key]
            calls.append(LogIntentCall(intent="issue_history", args=args))
        note = f"The failure episodes of the same scope are listed for {window} beside the heartbeat reading."
        rebuilt = log_decision(BoundQuestion(calls=calls, notes=[note]), Intents(), agent="two_halves")
        out.steps.extend(rebuilt.plan.steps)  # type: ignore[union-attr]

    # --- knowledge half and merge --------------------------------------------------------------

    def static_cards(self, capabilities: Capabilities) -> dict[str, Any]:
        from .source_cards import static_cards

        key = ",".join(capabilities.arms)
        if key not in self._static_cards:
            self._static_cards[key] = static_cards(self.engine.config, list(capabilities.arms))
        return self._static_cards[key]

    def knowledge_capabilities(self, capabilities: Capabilities, operations: OperationsHalf | None, gen: Any = None) -> Capabilities:
        """Today's capabilities without the log ones (the knowledge half is planned as a support
        question): the knowledge members' fields only (``knowledge_schema``, with ``gen``); the
        source cards only when the question has an operations half."""
        update: dict[str, Any] = {"log_intents": False, "log_context": None, "log_cascade_to_planner": False, "log_history": False, "log_question": None}
        schema = self.knowledge_schema(gen, capabilities.metadata_schema)
        if schema is not capabilities.metadata_schema:
            update["metadata_schema"] = schema
        if self.scoped_arms:
            # staging round 2: the knowledge half lists the knowledge sources' values only (planner
            # values, grounding, qualifier checks), as it does without a log member
            update["value_scope"] = [Predicate(field="source", op="in", value=list(self.knowledge_sources))]
        if operations is not None and operations.steps:
            from .source_cards import request_cards

            update["source_cards"] = request_cards(self.static_cards(capabilities), operations.calls(), operations.notes)
        return capabilities.model_copy(update=update)

    def merge(self, ctx: Any, request: DecisionRequest, intents: Intents, capabilities: Capabilities, knowledge: Decision, operations: OperationsHalf | None, inner: Any) -> tuple[Decision, TwoHalvesRecord]:
        record = TwoHalvesRecord(operations=operations, knowledge_outcome=knowledge.outcome, knowledge_failure=knowledge.failure)
        knowledge = self._guard(ctx, request, capabilities, knowledge, inner)
        ops_steps = list(operations.steps) if operations is not None else []
        extra = list(operations.unsupported) if operations is not None else []
        notes = list(operations.notes) if operations is not None else []
        if self.settings.fallback == "deterministic_plan" and eligible(knowledge, request):
            fallback = deterministic_plan(request, intents, capabilities, failed=knowledge, operations=ops_steps, agent=knowledge.agent)
            if fallback is not None:
                record.fallback = True
                record.replaced = knowledge
                return self._with(fallback, extra, notes), record
        if not ops_steps:
            if not extra and not notes:
                return knowledge, record  # no operations half: exactly today's decision
            return self._with(knowledge, extra, notes, policy=None), record
        if knowledge.outcome == "decided" and knowledge.plan is not None:
            if operations is not None and not operations.knowledge:
                keep: list[Any] = []
            else:
                keep = list(knowledge.plan.steps)
            room = max(0, 20 - len(ops_steps))
            steps = [*keep[:room], *ops_steps]
            first_ops = next((s.scope for s in ops_steps if isinstance(s, StructuredStep)), None)
            scope = knowledge.scope if knowledge.scope is not None and keep else first_ops
            merged = knowledge.model_copy(update={"plan": Plan(steps=steps), "scope": scope, "policy_applied": POLICY})
            if not keep:
                merged = merged.model_copy(update={"dropped_steps": [], "fallback": None})
            return self._with(merged, extra, notes), record
        if knowledge.outcome == "raw_all_arms":
            # the operator's fail-open policy without the two-halves fallback: kept as today; the
            # operations half is not run beside a raw plan (stated)
            skipped = UnsupportedConstraint(constraint_text=request.query, reason="qualifier_not_applied", suggestion="the planner failed and the raw_all_arms policy ran; operations evidence was not read")
            return self._with(knowledge, [skipped], notes, policy=None), record
        # the knowledge half refused: the operations half is served with the refusal stated
        refusal = UnsupportedConstraint(constraint_text=request.query, reason="qualifier_not_applied", suggestion=f"the knowledge half was not planned ({knowledge.failure or knowledge.outcome}); only operations evidence is shown")
        scope = next((s.scope for s in ops_steps if isinstance(s, StructuredStep)), None)
        decided = Decision(
            plan=Plan(steps=ops_steps),
            scope=scope,
            unsupported=[*knowledge.unsupported, refusal],
            outcome="decided",
            agent=knowledge.agent,
            stage=knowledge.stage,
            policy_applied=POLICY_OPERATIONS_ONLY,
            intents=intents,
            failure=knowledge.failure,
            note=knowledge.note,
        )
        return self._with(decided, extra, notes), record

    @staticmethod
    def _with(decision: Decision, unsupported: list[UnsupportedConstraint], notes: list[str], policy: str | None = POLICY) -> Decision:
        known = {(u.constraint_text, u.reason) for u in decision.unsupported}
        added = [u for u in unsupported if (u.constraint_text, u.reason) not in known]
        note = " ".join(x for x in [decision.note, *notes] if x) or None
        update: dict[str, Any] = {"unsupported": [*decision.unsupported, *added], "note": note}
        if policy is not None and decision.policy_applied not in {POLICY_OPERATIONS_ONLY, "deterministic_fallback_plan"}:
            update["policy_applied"] = policy
        return decision.model_copy(update=update)

    def _guard(self, ctx: Any, request: DecisionRequest, capabilities: Capabilities, knowledge: Decision, inner: Any) -> Decision:
        """The log scope guard on the knowledge half: a free structured scope that can read the log
        rollups is refused as it is today under the logs profile (``DecisionAgent.require_intent``).
        A step that executes under source conditions admitting no log source (the knowledge scope
        ``knowledge_predicate``, a request ``source`` filter, the ACL) cannot read the log member and
        is not guarded (staging round 2, N1): a support aggregate keeps today's plan."""
        if knowledge.outcome != "decided" or not capabilities.log_intents or knowledge.plan is None:
            return knowledge
        if request.scope is not None or request.aggregate is not None:
            return knowledge
        from .log_guard import log_scope_guard

        acl = list(ctx.constraints())
        filters = [Predicate(field="source", op="in" if isinstance(v, list) else "eq", value=v) for k, v in request.filters.items() if k == "source"]
        for step in knowledge.plan.structured_steps():
            if step.intent is not None:
                continue
            limit = self.knowledge_predicate(step, request.filters)
            context = [*acl, *filters, *([limit] if limit is not None else [])]
            refused = log_scope_guard(step.scope, context=context, log_sources=self.log_sources)
            if refused is None:
                continue
            failure = DecisionFailure("invalid_step", "a free structured scope over the log rollups needs a log intent", [refused])
            ctx.trace.event("two_halves_knowledge_refused", category=failure.category)
            from .base import abstain

            return abstain(knowledge.intents, agent=knowledge.agent, stage=knowledge.stage, failure=failure.category, unsupported=failure.unsupported, note=str(failure))
        return knowledge


# --- per-source coverage (trace ``source_coverage``; the legacy envelope's ``coverage``) ----------

COVERAGE_VERSION = 1
ANALYTICS_UNSERVED = "Search analytics (queries, results, clicks) are not part of this collection: how the query ranked was not checked."
TIER_KEYS = ("days", "archive_days", "partial_days", "provisional_days", "gap_days", "missing_days", "heartbeat_days", "pre_heartbeat_days", "unobserved_days")


def _tier_summary(tiers: dict[str, Any] | None) -> dict[str, Any] | None:
    if not tiers:
        return None
    out = {k: tiers[k] for k in TIER_KEYS if k in tiers and tiers[k] not in (None, [], {})}
    return out or None


def _intent_coverage(aggregate: dict[str, Any]) -> dict[str, Any]:
    meta = aggregate.get("operations_coverage") or {}
    item: dict[str, Any] = {
        "id": aggregate.get("id"),
        "intent": aggregate.get("intent"),
        "args": aggregate.get("args"),
        "status": aggregate.get("status"),
        "exactness": aggregate.get("exactness"),
        "rows": len(aggregate.get("rows") or []),
        "window": meta.get("window"),
    }
    if aggregate.get("source_unavailable"):
        item["days_without_data"] = list(aggregate["source_unavailable"])
    summary = _tier_summary(meta.get("tiers"))
    if summary:
        item["tiers"] = summary
    return item


def _logs_status(operations: OperationsHalf | None, intents: list[dict[str, Any]]) -> tuple[str, str | None]:
    if operations is None or not (operations.steps or operations.refused):
        return "not_asked", None
    if operations.refused and not intents:
        return "refused", operations.refused
    if any(i["status"] in {"answered", "bounded"} and i["rows"] for i in intents):
        return "evidence", None
    if intents and all(i["status"] == "refused" for i in intents):
        return "refused", None
    return ("no_match" if intents else "not_run"), None


def source_coverage(engine: Any, ctx: Any, state: Any, evidence: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Per-source coverage of one request on the unified collection: knowledge sources
    (candidates, packed evidence, members), the operations half (each typed log intent's status,
    window and day tiers; a refusal), search analytics, identity and links, the plan's halves and
    every gap as a stated limitation."""
    from collections import Counter

    planner = engine.two_halves
    record: TwoHalvesRecord | None = state.two_halves
    operations = record.operations if record is not None else None
    decision = state.decision
    limitations: list[str] = []
    collection = engine.backends[engine.config.storage.catalog].coverage(ctx.generation) if engine.view.partitioned else None
    candidates = Counter((hit.source or "unknown") for hit in state.hits_by_id.values())
    packed = Counter(row.get("source") or "unknown" for row in (evidence or []) if row.get("source") != "structured")
    steps = list(decision.plan.steps) if decision.plan is not None else []
    # each step's half by the sources it reads (staging F1: a ticket count is not the operations half)
    step_halves = [step_half(step, planner.knowledge_sources, planner.kind_sources) for step in steps]
    knowledge_planned = "knowledge" in step_halves
    operations_planned = "operations" in step_halves
    sources: dict[str, dict[str, Any]] = {}
    for source in planner.knowledge_sources:
        entry: dict[str, Any] = {"half": "knowledge", "candidates": candidates.get(source, 0)}
        if evidence is not None:
            entry["evidence"] = packed.get(source, 0)
        if not knowledge_planned:
            entry["status"] = "not_planned"
        elif entry.get("evidence"):
            entry["status"] = "evidence"
        else:
            entry["status"] = "candidates" if entry["candidates"] else "no_match"
        if collection is not None:
            entry["members"] = [m for m in collection.get("members", []) if source in m.get("sources", [])]
            entry["layers"] = [layer for layer in collection.get("layers", []) if source in layer.get("sources", [])]
        sources[source] = entry
    log_sources = set(planner.operation_sources) or {LOG_SOURCE}
    for source, count in sorted(candidates.items()):
        if source not in sources and source not in log_sources:
            sources[source] = {"half": "other", "candidates": count}
            if evidence is not None:
                sources[source]["evidence"] = packed.get(source, 0)
    intents = [_intent_coverage(a) for a in state.aggregates if a.get("intent")]
    for item in intents:
        if item.get("days_without_data"):
            limitations.append(f"Log days without data for {item['intent']}: {', '.join(item['days_without_data'])}.")
    # the log entry is derived from what was pinned and what reached the hits (staging F2): never
    # ``not_served`` while log records are in the hits or log members are pinned
    log_members = [m for m in collection.get("members", []) if log_sources & set(m.get("sources", []))] if collection is not None else []
    log_member_names = {m.get("member") for m in log_members}
    log_slices = [s for s in collection.get("slices", []) if s.get("member") in log_member_names] if collection is not None else []
    log_candidates = sum(candidates.get(s, 0) for s in log_sources)
    log_packed = sum(packed.get(s, 0) for s in log_sources)
    logs_config = getattr(engine.config, "logs", None)
    if logs_config is not None and logs_config.intents.enabled:
        status, reason = _logs_status(operations, intents)
    elif log_candidates:
        status, reason = "records_only", None
        limitations.append("Log records are among the retrieved records, but typed log intents are not enabled (logs.intents): no operations half was planned.")
    elif log_members or log_slices or (collection is None and logs_config is not None):
        status, reason = "intents_not_served", "typed log intents are not enabled for this collection (logs.intents); log members are pinned but were not read"
    else:
        status, reason = "not_served", None  # no log member (staged cutover step 1)
    logs: dict[str, Any] = {"half": "operations", "status": status, "intents": intents, "candidates": log_candidates}
    if evidence is not None:
        logs["evidence"] = log_packed
    if reason:
        logs["reason"] = reason
        limitations.append(f"Operations evidence (logs) was not shown: {reason}.")
    if operations is not None and operations.notes:
        logs["notes"] = list(operations.notes)
    if collection is not None:
        logs["members"] = log_members
        if log_slices:
            logs["slices"] = log_slices
    sources["logs"] = logs
    if operations is not None and operations.analytics:
        members = getattr(getattr(engine.config, "collection", None), "members", None) or {}
        served = any(m.kind == "analytics" for m in members.values())
        sources["search_analytics"] = {"half": "operations", "status": "not_queried" if served else "not_served"}
        limitations.append("Search analytics are a member of this collection but no intent reads them yet: how the query ranked was not checked." if served else ANALYTICS_UNSERVED)
    identity_view = engine.identity_view()
    identity: dict[str, Any] = {"configured": identity_view is not None}
    if identity_view is not None:
        identity.update(joins_enabled=identity_view.joins_enabled, registry_version=identity_view.registry_version)
        if identity_view.reason:
            identity["reason"] = identity_view.reason
    customers = operations.customers if operations is not None else None
    if customers is not None and customers.customers:
        identity["customers"] = [c.model_dump() for c in customers.customers]
        unmatched = [c.text for c in customers.ungrounded()]
        if unmatched:
            identity["unmatched"] = unmatched
    link_view = engine.link_view()
    links: dict[str, Any] = {"configured": link_view is not None}
    if link_view is not None:
        links.update(enabled=link_view.enabled, link_version=link_view.link_version, withheld=link_view.withheld)
        if link_view.reason:
            links["reason"] = link_view.reason
            limitations.append(f"Cross-source links are not shown: {link_view.reason}.")
        limitations.extend(f"Cross-source links: {text}." for text in link_view.limitations)
    if decision.fallback == "deterministic_plan" and decision.note:
        limitations.insert(0, decision.note)
    halves = (["knowledge"] if knowledge_planned else []) + (["operations"] if operations_planned else [])
    out: dict[str, Any] = {
        "version": COVERAGE_VERSION,
        "plan": {"halves": halves, "policy": decision.policy_applied, "fallback": decision.fallback, "origin": operations.origin if operations is not None else None},
        "sources": sources,
        "identity": identity,
        "links": links,
        "limitations": limitations,
    }
    if planner.scoped_arms and knowledge_planned:
        # the knowledge half read the knowledge sources only (``knowledge_predicate``)
        out["knowledge_scope"] = {"sources": list(planner.knowledge_sources), "arms": sorted(planner.scoped_arms)}
    if collection is not None:
        out["collection"] = collection
    return out


__all__ = ["OperationsHalf", "TwoHalvesAgent", "TwoHalvesPlanner", "TwoHalvesRecord", "operation_sources", "scoped_arms", "source_coverage", "step_half"]
