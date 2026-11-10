"""``Engine``: the Python entry point on configuration v2 and the component harness (plan §2.2).

A request runs through one ``RunContext`` (pinned generation, principal, budget, trace) and the
harness stages in order: intent pre-pass → decision agent (``rules`` | ``classifier`` | ``llm`` |
``cascade``, fail policy from config) → arms as components (eligibility pushed down, never widened)
→ ``selection`` (collapse, limits, union, ordering, diversity, pins) → evidence packing with the
configured field priority → optional ``ReviewStage`` (bounded, re-plans through the decision agent)
→ generation → ``AnswerContract`` with engine-populated ``unsupported_constraints`` and
``coverage``. ``search``/``answer``/``ingest`` keep their names and wire models. A v1 ``Config`` is
accepted and upgraded in memory. Python objects are injected only through
``Engine(config, components=[...])``.

Phase 1 harness: one ``PipelineRuntime`` per configured pipeline (``pipeline`` = ``default`` plus the
named ``pipelines``), chosen per request through ``SearchRequest.pipeline``/``mode`` against the
``request`` allowlists (``pipeline/spec.py``); ``session_id`` binds a request to a stored session
(``runtime/turns.py``: per-session budget, generation-switch re-validation, turn recording); the
``exact_id`` arm pins exact identifier matches and reports unknown identifiers as
``unsupported_constraints``; read-only tools (``call_tool``) and allowlisted, audited write actions
(``run_action``) run through the same context and trace.

Streaming: ``answer_stream`` runs the same answer path on a worker thread and yields typed events
(``runtime/events.py``): decision, arm results, review rounds, packed evidence, provisional generator
token deltas (only when the generator role declares ``capabilities.streaming``), the contract
outcome, then ``done`` with the same ``AnswerResult`` ``answer`` returns, or ``error``.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
import os
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .arms import ArmInput, ArmResult, IncompleteStageError, builtin_registry
from .backends.memory import MemoryBackend
from .backends.opensearch import OpenSearchBackend
from .backends.sqlite import SqliteBackend
from .compat.config_v1 import Config as ConfigV1
from .compat.config_v1 import upgrade
from .core.component import ArtifactRef, Dependencies, Diagnostic, Environment, is_component_class
from .core.config import Config, KuzuBackend as KuzuBackendConfig, LuceneBackend, MemoryBackendConfig, MongoBackend as MongoBackendConfig, Neo4jBackend as Neo4jBackendConfig
from .core.config import PostgresBackend as PostgresBackendConfig
from .core.config import SqlAlchemyBackend as SqlAlchemyBackendConfig
from .core.config import SqliteBackend as SqliteBackendConfig
from .core.errors import BackendError, BudgetExhausted, ConfigError
from .core.types import Document, GroupRow, Measure, Page, Predicate, Scope, ScopeResult, ScopeSpec
from .decision.values import FilterCheck, ValueResolver, check_request_filters
from .decision import (
    Capabilities,
    CascadeAgent,
    CascadeConfig,
    ClassifierAgent,
    ClassifierConfig,
    Decision,
    DecisionAgent,
    DecisionRequest,
    Intents,
    LlmAgent,
    LlmConfig,
    Plan,
    RetrievalStep,
    RulesAgent,
    RulesConfig,
    StructuredStep,
    TypedStep,
    detect_intents,
)
from .extraction.base import BuiltinExtractor
from .generation import AggregateEvidence, AnswerContract
from .generation.contract import CLAIM_KINDS, answer_schema, labelled
from .generation.rendering import relabel_bounded
from .ingestion.layers import LayerUnavailableError
from .ingestion.release import ANY as ANY_BASE
from .ingestion.write import WritePath
from .models import Aggregate, Answer, AnswerResult, Coverage, Hit, SearchRequest, SearchResult, UnsupportedConstraint
from .decision.typed_query import describe as describe_typed
from .pipeline import JsonModelReviewer, ReviewInput, ReviewStage, ReviewStageConfig
from .pipeline.spec import ARM_ORDER, PipelineRuntime, RequestRejected, allowed_modes, choose
from .pipeline.validate import decision_for_mode, raise_for
from .profiles.prompts import AGGREGATE_NARRATIVE_PROMPT, GENERATOR_PROMPT, OPERATIONS_RULE, voice_instruction
from .providers import Embedder, JsonModel, RunBudgetMeter
from .providers.accounting import RetryBudgetMeter
from .providers._chat import MissingSecret
from .providers.base import SchemaViolation
from .decision.log_guard import guard_active, guard_for, log_scope_guard, log_sources
from .runtime.acl import ANONYMOUS, AccessPolicy, Principal, policy_from_config, require_single_tenant
from .runtime.actions import ActionRequest, ActionRunner
from .runtime.budget import BudgetLimits, load_price_table
from .runtime.collection import CollectionView
from .runtime.builtin_actions import BUILTIN_ACTIONS, ActionCall, LedgerAuditLog
from .runtime.builtin_tools import ToolCall, builtin_tool_registry
from .runtime.context import RunContext, new_context
from .runtime.events import AnswerStream, ArmResultEvent, ContractEvent, DecisionEvent, EvidenceEvent, ReviewRoundEvent
from .runtime.session import Session, SessionManager
from .runtime.tools import invoke_tool
from .runtime.trace import Sensitive, Trace
from .runtime.turns import SessionTurns
from .selection import Candidate, PackExtra, PackRecord, PerArm, Selected, SelectionPolicy, pack, select

ARM_NAMES = ARM_ORDER
DERIVED_ARMS = ("graph", "community")
# Typed-query evidence in the generator payload (M6): at most this many rows of a lookup/list and
# groups of an aggregate; a result with more is reported ``bounded`` (``result_truncated``) so no
# complete-list or group claim rests on evidence the generator did not see.
TYPED_PACK_ROWS = 50
TYPED_PACK_GROUPS = 50
LOGGER = logging.getLogger("su_multi_arm.engine")
UNKNOWN_ID_SUGGESTION = "identifier not found in this generation within the request scope; no fuzzy match is attempted"
UNRESOLVED_CONTEXT_SUGGESTION = "record_context record id not found in this generation within the caller's access scope; it was not used"


def scope_spec_for(aggregate: Aggregate) -> ScopeSpec:
    """Today's typed aggregates expressed as a ``ScopeSpec`` (never text-derived)."""
    if aggregate.operation == "count":
        return ScopeSpec(measure=Measure(kind="count"))
    if aggregate.operation == "group_count":
        if not aggregate.field:
            raise ValueError("group_count requires a field")
        return ScopeSpec(group_by=[aggregate.field])
    if not aggregate.field:
        raise ValueError("first_seen requires a field")
    return ScopeSpec(measure=Measure(kind="first_seen", field=aggregate.field))


def scope_result_of(aggregate: dict[str, Any]) -> ScopeResult:
    """The ``ScopeResult`` behind an aggregate record returned by the structured arm."""
    return ScopeResult(
        exactness=aggregate["exactness"],
        denominator=aggregate["denominator"],
        value=aggregate.get("value"),
        groups=[GroupRow.model_validate(g) for g in aggregate.get("groups", [])],
        unsupported=aggregate.get("unsupported", []),
        zero_match=aggregate.get("zero_match", False),
        matched_ids=aggregate.get("document_ids", []),
        matched_ids_truncated=aggregate.get("document_ids_truncated", False),
        missing_or_invalid=aggregate.get("missing_or_invalid", 0),
        total=aggregate.get("total"),
        listed=aggregate.get("listed", []),
        ties=[GroupRow.model_validate(g) for g in aggregate.get("ties", [])],
        unknown=aggregate.get("unknown", {}),
        numerator=aggregate.get("numerator"),
        basis=aggregate.get("basis", []),
        applied_basis=aggregate.get("applied_basis", []),
        applied_extraction=aggregate.get("applied_extraction", []),
    )


@dataclass
class _Retrieval:
    """Per-request state shared by the first pass, the review hops and packing."""

    request: SearchRequest
    decision_request: DecisionRequest
    capabilities: Capabilities
    decision: Decision
    pipe: PipelineRuntime | None = None
    mode: str = ""
    exact_matched: dict[str, str] = field(default_factory=dict)  # exact_id: identifier -> record id
    exact_unknown: list[str] = field(default_factory=list)  # exact_id: identifiers that did not resolve
    hits_by_id: dict[str, Hit] = field(default_factory=dict)
    raw_pools: PerArm = field(default_factory=dict)  # every candidate every step produced (pre-collapse)
    pools: PerArm = field(default_factory=dict)  # per-arm pools after collapse + limits
    ordered: list[Selected] = field(default_factory=list)
    aggregates: list[dict[str, Any]] = field(default_factory=list)
    reports: list[dict[str, Any]] = field(default_factory=list)
    rounds: list[dict[str, Any]] = field(default_factory=list)
    selection_trace: dict[str, Any] = field(default_factory=dict)
    incomplete: bool = False
    pack_trace: dict[str, Any] = field(default_factory=dict)
    unresolved_record_id: str | None = None  # record_context id unknown or outside the principal's scope
    filter_check: FilterCheck = field(default_factory=FilterCheck)  # request filters validated against the schema
    sql_routes: dict[int, ArmResult] = field(default_factory=dict)  # executed SQL-route steps (keyed by step identity)
    routed_steps: set[int] = field(default_factory=set)  # typed-router steps (their evidence is packed first)
    lead_ids: list[str] = field(default_factory=list)  # aggregate ids of the typed-router steps
    two_halves: Any = None  # M2b-2: the two-halves record of this request (``decision/two_halves.py``)


@dataclass
class _Routed:
    """The typed router's outcome for one request (``routing: router``): ``retrieval`` (nothing to
    add), ``typed`` with an executed ``step`` and its ``result``, or with a ``dropped`` step and its
    ``constraints``; ``error``: the router call failed (``dropped`` only)."""

    route: str
    step: Any = None
    result: Any = None
    dropped: Any = None
    constraints: list[UnsupportedConstraint] = field(default_factory=list)


class _RecordsRouter:
    """What the structured arm executes against when the history tier or the episode archive is on
    (``Engine.records_execute``)."""

    def __init__(self, engine: Any):
        self.engine = engine

    def execute(self, gen: Any, spec: ScopeSpec, constraints: list[Any]) -> ScopeResult:
        return self.engine.records_execute(gen, spec, constraints)


class Engine:
    def __init__(self, config: Config | ConfigV1, components: Iterable[Any] = (), acl: AccessPolicy | None = None):
        if isinstance(config, ConfigV1):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                config = upgrade(config).config
        if not isinstance(config, Config):
            raise ConfigError("Engine needs a su_multi_arm.core.config.Config (v2) or a v1 Config")
        self.config = config
        # Unified-merge seam (S0, R6): the single-member view, or the partitioned view (M1a).
        self.view = CollectionView.for_config(config)
        self.acl = acl or policy_from_config(getattr(config, "access", None))
        require_single_tenant(config, self.acl)
        self.backends = self._build_backends()
        if self.view.partitioned:
            # M1a: the configured sqlite backend is served through the partitioned facets (members by
            # reference, id-directory routing, per-stratum lexical, merged exact dense); the
            # runtime facets (cache, ledger, sessions) stay the configured backend's.
            from .backends.sqlite.members import PartitionedSqliteBackend

            name = config.storage.catalog
            partitioned = PartitionedSqliteBackend(self.backends[name], self.view.attach(), config)
            self.backends[name] = partitioned
            # M1b: a backend declared as an attached layer (graph_attach) answers for the store
            # generation of the adopted member it is bound to, verified against its pinned digest
            from .ingestion.layers import LayerBackend

            for layer in config.collection.layers:
                if layer.backend is not None:
                    self.backends[layer.backend] = LayerBackend(self.backends[layer.backend], layer, partitioned)
        roles = config.models.roles
        self.embedder = Embedder(roles.embedder)
        self.models = {name: JsonModel(role) for name, role in roles.chat_roles().items()}
        catalog = getattr(self.backends[config.storage.catalog], "catalog")
        self.records = getattr(self.backends[config.storage.records], "records")
        injected = self._sort_components(components)
        deps = Dependencies(
            backends={**self.backends, "records": self.records},
            providers={"embedder": self.embedder, **self.models},
            components={
                "catalog": catalog,
                **injected["models"],
                **({"log_scope_guard": guard_for(config)} if guard_active(config) else {}),
                **({"records_router": _RecordsRouter(self)} if config.logs is not None and (config.logs.history.tier or config.logs.collection.archive.enabled) else {}),
            },
            metadata_schema=config.metadata_schema,
        )
        self.extractor = BuiltinExtractor(config.extraction, deps)
        self.registry = builtin_registry()
        injected_arms = self._register_arms(injected["arms"])
        self.pipelines: dict[str, PipelineRuntime] = self._build_pipelines(deps, injected_arms, injected["decision"], injected["reviewer"])
        tool_deps = Dependencies(backends=deps.backends, components={"engine": self}, metadata_schema=config.metadata_schema)
        self.tools = builtin_tool_registry(config.tools.enabled)
        self._tools = {name: self.tools.get(name)(self.tools.get(name).config_model(), tool_deps) for name in self.tools.names()}
        self._validate_components()
        if self.view.partitioned:
            from .ingestion.compose import PartitionedWritePath

            self.write_path = PartitionedWritePath(config, self.view.attach())
        else:
            self.write_path = WritePath(
                config,
                self.backends,
                extractor=self.extractor,
                embedder=self.embedder,
                community_model=self.models.get("community"),
                profile=config.profile,
                config_fingerprint=config.fingerprint(),
            )
        # Cross-process generation pins (B8): with a ``logs`` section (``logs.pins``; the logs config
        # only accepts registries that store pins) every request pins the generation it reads, so a
        # retention run in another process waits instead of deleting it mid-request.
        self.process_pins = None
        if getattr(config, "logs", None) is not None:
            from .ingestion.pins import ProcessPins

            self.process_pins = ProcessPins.for_config(config, self.write_path.registry, current=self._current_generation, start=True)
        self.audit = LedgerAuditLog(getattr(self.backends[config.storage.ledger], "ledger"), config.dataset)
        self.actions = ActionRunner(
            self.audit,
            allow_remote=list(config.serving.actions.allow),
            retention_seconds=config.actions.audit_retention_seconds,
            current_generation=self._current_generation,
        )
        for name in config.actions.allow:
            self.actions.register(name, BUILTIN_ACTIONS[name](self))
        sessions = config.sessions
        session_store = getattr(self.backends[sessions.store or config.storage.sessions], "session")
        self.sessions = SessionTurns(
            SessionManager(session_store, sessions.ttl_seconds, store_text=sessions.memory.store_text),
            config.dataset,
            session_budget=config.budgets.session,
            exclude_shown=sessions.memory.exclude_shown,
            episodic=sessions.memory.episodic,
        )
        self.voice = voice_instruction(config.pipeline.generation.voice)
        self.price_table = load_price_table(config.budgets.price_table) if config.budgets.price_table else None
        # M0 (§6.13): the worst-case answer spend against budgets.max_usd, checked once at startup
        # (warn by default; ``budgets.plan_check: refuse`` raises ConfigError); ``/ready`` reports it
        from .runtime.plan_cost import enforce, plan_check

        self.plan_check = enforce(plan_check(config, self.price_table))
        self.question_vocabulary = config.question.vocabulary.entries()  # pin mismatch: refuse to start
        self._reranker = None
        # M2b-2 (§6.11): the two-halves planner over the unified collection, the pinned identity
        # view (customer grounding) and the pinned link view (labelled display); off by default
        self._crosssource_views: dict[str, Any] = {}
        self._halves = threading.local()
        self.two_halves = self._two_halves_planner()

    # --- cross-source views and the two-halves planner (unified merge M2b-2) --------------------

    def _two_halves_planner(self) -> Any:
        planner = getattr(getattr(self.config, "crosssource", None), "planner", None)
        if planner is None or not planner.enabled:
            return None
        # without log intents (staged cutover before the log member joins) there is no operations
        # half: every request keeps today's plan and gains per-source coverage (logs: not_served)
        from .decision.two_halves import TwoHalvesPlanner

        built = TwoHalvesPlanner(self, planner)
        if not built.knowledge_sources:
            raise ConfigError("two-halves planning needs the knowledge sources (crosssource.planner.knowledge_sources, or collection members not produced by a log host)", "crosssource.planner.knowledge_sources")
        return built

    def _knowledge_value_scope(self) -> list[Predicate]:
        """Staging round 3 (N9): the typed SQL route checks qualifiers against the knowledge sources'
        values only, as the knowledge half does (``Capabilities.value_scope``); a log member's field
        values must not mark a support question's qualifier as unapplied. Empty without scoping."""
        planner = self.two_halves
        if planner is None or not planner.scoped_arms:
            return []
        return [Predicate(field="source", op="in", value=list(planner.knowledge_sources))]

    def _knowledge_capabilities(self, ctx: RunContext, capabilities: Capabilities) -> Capabilities:
        """``capabilities`` with the knowledge members' schema (``TwoHalvesPlanner.knowledge_schema``)."""
        schema = self.two_halves.knowledge_schema(ctx.generation, capabilities.metadata_schema)
        if schema is capabilities.metadata_schema:
            return capabilities
        ctx.trace.event("knowledge_schema", fields=len(schema.fields), dropped=len(capabilities.metadata_schema.fields) - len(schema.fields))
        return capabilities.model_copy(update={"metadata_schema": schema})

    def _state_support_only(self, state: Any) -> bool:
        """After the decision: the request has no operations step (``TwoHalvesRecord``)."""
        if self.two_halves is None:
            return False
        record = state.two_halves
        return record is None or record.operations is None or not record.operations.steps

    def _support_only(self, request: SearchRequest) -> bool:
        """Staging round 5 (N12/N13): on the unified collection, a question that cannot have an
        operations half (no operations word or failure symptom, no log intents, or a caller-typed
        operation) is a support question. It gets today's planner payload and answer contract
        (the knowledge members' fields only: ``TwoHalvesPlanner.knowledge_schema``) and today's SQL
        writer catalog (the projection's tables, without the log and identity views). ``False``
        without the two-halves planner (single store: unchanged)."""
        planner = self.two_halves
        if planner is None:
            return False
        logs = self.config.logs
        enabled = bool(logs is not None and logs.intents.enabled)
        return not planner.asks_operations(request.query, log_intents=enabled, caller_typed=request.aggregate is not None)

    def identity_view(self) -> Any:
        """The pinned identity view (``crosssource.identity``; read once per process), or ``None``
        without a registry. A missing file or digest mismatch disables joins (fail closed)."""
        identity = getattr(getattr(self.config, "crosssource", None), "identity", None)
        if identity is None:
            return None
        if "identity" not in self._crosssource_views:
            from .crosssource.identity import IdentityView

            self._crosssource_views["identity"] = IdentityView.from_config(identity)
        return self._crosssource_views["identity"]

    def link_view(self) -> Any:
        """The pinned link view (``crosssource.links.{store_path, sha256}``), or ``None`` without a
        pinned store. Customer-derived links need the identity view (withheld otherwise, labelled)."""
        links = getattr(getattr(self.config, "crosssource", None), "links", None)
        if links is None or links.store_path is None:
            return None
        if "links" not in self._crosssource_views:
            from .crosssource.links import LinkView

            self._crosssource_views["links"] = LinkView.pinned(links.store_path, links.sha256, identity=self.identity_view(), show_candidates=links.show_candidates, max_per_record=links.max_per_record)
        return self._crosssource_views["links"]

    def links_for(self, record_ids: list[str], principal: Principal | None = None) -> dict[str, list[dict[str, Any]]]:
        """Display links of ``record_ids`` (visible statuses, candidates labelled). Links never
        grant access: a link whose other record the caller cannot read is not shown."""
        view = self.link_view()
        if view is None or not view.enabled or not record_ids:
            return {}
        found = {rid: view.links_for(rid) for rid in dict.fromkeys(record_ids)}
        others = sorted({e["other_id"] for entries in found.values() for e in entries if e.get("other_id")})
        if not others:
            return {}
        ctx = self._context_for(principal)
        readable = {d.id for d in self.documents_in(ctx, others)}
        out = {rid: [e for e in entries if e.get("other_id") in readable] for rid, entries in found.items()}
        return {rid: entries for rid, entries in out.items() if entries}

    def _source_coverage(self, ctx: RunContext, state: Any, evidence: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        """Per-source coverage of one two-halves request (``trace.source_coverage``; the legacy
        envelope's ``coverage``): each knowledge source's candidates and packed evidence, the
        operations half (intents, windows, status, refusals), search analytics, identity and links."""
        if self.two_halves is None:
            return None
        from .decision.two_halves import source_coverage

        return source_coverage(self, ctx, state, evidence)

    # --- the default pipeline (v1-era attributes kept for the Python API) ----------------------

    @property
    def default(self) -> PipelineRuntime:
        return self.pipelines[self.config.default_pipeline]

    @property
    def arms(self) -> dict[str, Any]:
        return self.default.arms

    @arms.setter
    def arms(self, value: dict[str, Any]) -> None:
        self.default.arms = value

    @property
    def decision(self) -> DecisionAgent:
        return self.default.decisions[self.default.default_mode]

    @property
    def review(self) -> ReviewStage | None:
        return self.default.reviews.get(self.default.default_mode)

    @property
    def contract(self) -> AnswerContract:
        return self.default.contract

    # --- construction ------------------------------------------------------------------------

    def _build_backends(self) -> dict[str, Any]:
        built: dict[str, Any] = {}
        for name, spec in self.config.backends.items():
            if isinstance(spec, SqliteBackendConfig):
                built[name] = SqliteBackend.from_config(spec)
            elif isinstance(spec, MemoryBackendConfig):
                built[name] = MemoryBackend()
            elif isinstance(spec, MongoBackendConfig):
                from .backends.mongodb import MongoBackend

                built[name] = MongoBackend.from_config(spec)
            elif isinstance(spec, PostgresBackendConfig):
                from .backends.postgres import PostgresBackend

                built[name] = PostgresBackend.from_config(spec)
            elif isinstance(spec, SqlAlchemyBackendConfig):
                from .backends.sql import SqlBackend

                built[name] = SqlBackend.from_config(spec)
            elif isinstance(spec, Neo4jBackendConfig):
                from .backends.neo4j import Neo4jBackend

                built[name] = Neo4jBackend.from_config(spec)
            elif isinstance(spec, KuzuBackendConfig):
                from .backends import kuzu as kuzu_backend

                built[name] = kuzu_backend.from_config(spec)
            elif isinstance(spec, LuceneBackend) and spec.kind == "opensearch":
                try:
                    built[name] = OpenSearchBackend.from_config(spec)
                except BackendError as exc:
                    raise ConfigError(str(exc), f"backends.{name}") from exc
            elif isinstance(spec, LuceneBackend) and spec.kind == "elasticsearch":
                from .backends.elasticsearch import ElasticsearchBackend

                try:
                    built[name] = ElasticsearchBackend.from_config(spec, dataset=self.config.dataset)
                except BackendError as exc:
                    raise ConfigError(str(exc), f"backends.{name}") from exc
            else:
                raise ConfigError(
                    f"kind {spec.kind!r} has no v2 adapter in this release",
                    f"backends.{name}",
                )
        return built

    @staticmethod
    def _sort_components(components: Iterable[Any]) -> dict[str, Any]:
        """Python-side injection: arm classes/instances, a decision agent, a reviewer, a classifier runtime."""
        sorted_: dict[str, Any] = {"arms": [], "decision": None, "reviewer": None, "models": {}}
        for component in components:
            cls = component if isinstance(component, type) else type(component)
            if is_component_class(cls):
                if cls.kind == "arm":
                    sorted_["arms"].append(component)
                elif cls.kind == "decision" and not isinstance(component, type):
                    sorted_["decision"] = component
                else:
                    raise ConfigError(f"only arm components and decision-agent instances can be injected in this release (got kind {cls.kind!r})")
            elif callable(getattr(component, "review", None)):
                sorted_["reviewer"] = component
            elif callable(getattr(component, "classify", None)):
                sorted_["models"]["classifier"] = component
            else:
                raise ConfigError(f"injected object {component!r} does not satisfy the Component contract (nor is it a reviewer/classifier runtime)")
        return sorted_

    def _register_arms(self, components: Iterable[Any]) -> dict[str, Any]:
        """Injected arm classes join the registry (overriding a built-in); instances are returned by name."""
        instances: dict[str, Any] = {}
        for component in components:
            cls = component if isinstance(component, type) else type(component)
            if isinstance(component, type):
                self.registry.register(cls) if cls.name not in self.registry else None
                if self.registry.get(cls.name) is not cls:
                    self.registry._items[cls.name] = cls  # explicit Python-side override of a built-in
            else:
                instances[cls.name] = component
        return instances

    def _build_pipelines(self, deps: Dependencies, injected_arms: dict[str, Any], decision: Any, reviewer: Any) -> dict[str, PipelineRuntime]:
        """One runtime per configured pipeline; every unknown component is reported together."""
        problems: list[Diagnostic] = []
        built: dict[str, PipelineRuntime] = {}
        for name, cfg in self.config.all_pipelines().items():
            prefix = "pipeline" if name == "default" else f"pipelines.{name}"
            arms: dict[str, Any] = {}
            for arm, spec in cfg.arms.enabled().items():
                if arm in injected_arms:
                    arms[arm] = injected_arms[arm]
                elif arm == "typed_query" and arm in self.registry:
                    # bound to the projection backend and spec; offered to the llm planner per request
                    # (``_typed_planning``) only under ``pipeline.decision.typed_query.enabled``
                    from .arms.typed_query import TypedQueryRuntime

                    access = self.config.access.field if self.config.access.policy == "tenant" else None
                    runtime_spec = TypedQueryRuntime(**spec.model_dump(), backend=self.config.projection.backend or "", records=self.config.storage.records, projection=self.config.projection, access_column=access)
                    arms[arm] = self.registry.get(arm)(runtime_spec, deps)
                elif arm in self.registry and arm in ARM_NAMES:
                    arms[arm] = self.registry.get(arm)(spec, deps)
                else:
                    problems.append(Diagnostic(severity="error", code="unknown_component", message=f"arm {arm!r} is not available in this release", path=f"{prefix}.arms.{arm}"))
            decisions: dict[str, DecisionAgent] = {}
            reviews: dict[str, ReviewStage | None] = {}
            for mode in allowed_modes(self.config, cfg):
                try:
                    decision_cfg = decision_for_mode(cfg.decision, mode)
                except ValueError:
                    continue  # config validation reports modes unbuildable on selectable pipelines
                if decision is not None and name == self.config.default_pipeline and mode == cfg.decision.kind:
                    decisions[mode] = decision
                else:
                    decisions[mode] = self._build_decision(deps, decision_cfg, prefix)
                reviews[mode] = self._build_review(cfg, decisions[mode], reviewer)
            contract = AnswerContract(cfg.generation.contract.on_violation)
            built[name] = PipelineRuntime(name=name, config=cfg, arms=arms, decisions=decisions, reviews=reviews, contract=contract)
        raise_for(problems)
        return built

    def _build_decision(self, deps: Dependencies, cfg: Any, prefix: str = "pipeline") -> DecisionAgent:
        roles = self.config.models.roles

        def stage(kind: str) -> DecisionAgent:
            if kind == "rules":
                return RulesAgent(RulesConfig(on_failure=cfg.on_failure), deps)
            if kind == "llm":
                if roles.planner is None:
                    raise ConfigError("decision kind llm requires models.roles.planner", "models.roles.planner")
                return LlmAgent(LlmConfig(on_failure=cfg.on_failure, role=roles.planner, max_steps=cfg.max_steps, schema_repair_attempts=cfg.schema_repair_attempts, prompt=cfg.prompt, on_step_failure=cfg.on_step_failure, typed_repair_attempts=cfg.typed_query.repair_attempts), deps)
            artifact = None
            if cfg.artifact is not None:
                artifact = ArtifactRef.model_validate(cfg.artifact.model_dump(mode="json"))
            return ClassifierAgent(ClassifierConfig(on_failure=cfg.on_failure, artifact=artifact, component=cfg.component), deps)

        if cfg.kind == "cascade":
            stages = {name: stage(name) for name in cfg.stages}
            cascade = CascadeConfig(on_failure=cfg.on_failure, stages=list(cfg.stages), stage_policies=cfg.stage_policies)
            return CascadeAgent(cascade, Dependencies(backends=deps.backends, providers=deps.providers, components=stages, metadata_schema=deps.metadata_schema))
        return stage(cfg.kind)

    def _build_review(self, cfg: Any, decision: DecisionAgent, reviewer: Any | None) -> ReviewStage | None:
        review = cfg.review
        if not review.enabled:
            return None
        if reviewer is None:
            role = self.config.models.roles.reviewer
            if role is None:
                raise ConfigError("pipeline.review.enabled requires models.roles.reviewer", "models.roles.reviewer")
            reviewer = JsonModelReviewer(role, review.prompt)
        components = {"reviewer": reviewer, "decision": decision}
        return ReviewStage(ReviewStageConfig(enabled=True, max_hops=review.max_hops), Dependencies(components=components))

    def _validate_components(self) -> None:
        """``validate()`` of every component of every pipeline and every tool; all errors at once.

        A missing API key (``missing_secret``) of a decision or review component that only a
        non-default pipeline or a non-default request mode uses does not fail construction (QA-14):
        ingest and requests on other pipelines do not need it. It is re-checked when a request first
        selects that ``(pipeline, mode)`` (``_require_keys``) and reported by ``doctor``."""
        env = Environment(env=dict(os.environ), base_path=Path.cwd())
        problems: list[Diagnostic] = []
        # (path, component, lazy scope or None for "needed by every request / by ingest")
        stages: list[tuple[str, Any, tuple[str, str] | None]] = [("extraction", self.extractor, None)]
        for name, runtime in self.pipelines.items():
            prefix = "pipeline" if name == "default" else f"pipelines.{name}"
            stages.extend((f"{prefix}.arms.{arm}", component, None) for arm, component in runtime.arms.items())
            for mode in runtime.decisions:
                lazy = None if (name == self.config.default_pipeline and mode == runtime.default_mode) else (name, mode)
                stages.append((f"{prefix}.decision", runtime.decisions[mode], lazy))
                if runtime.reviews.get(mode) is not None:
                    stages.append((f"{prefix}.review", runtime.reviews[mode], lazy))
        stages.extend((f"tools.{name}", tool, None) for name, tool in self._tools.items())
        seen: set[int] = set()
        self._deferred_keys: dict[tuple[str, str], list[tuple[str, Any]]] = {}
        for path, component, lazy in stages:
            if id(component) in seen:
                continue
            seen.add(id(component))
            for diagnostic in component.validate(env):
                if diagnostic.severity != "error":
                    continue
                if lazy is not None and diagnostic.code == "missing_secret":
                    self._deferred_keys.setdefault(lazy, []).append((path, component))
                    continue
                problems.append(diagnostic.model_copy(update={"path": diagnostic.path or path, "message": f"{diagnostic.component or component.name}: {diagnostic.message}"}))
        if problems:
            raise ConfigError("; ".join(d.message for d in problems), problems[0].path if len(problems) == 1 else None, diagnostics=problems)

    def _choose(self, request: SearchRequest) -> tuple[PipelineRuntime, str]:
        """``choose`` plus the deferred key check of the selected ``(pipeline, mode)``."""
        pipe, mode = choose(self.config, self.pipelines, request.pipeline, request.mode)
        self._require_keys(pipe.name, mode)
        return pipe, mode

    def _require_keys(self, pipeline: str, mode: str) -> None:
        """Fail closed (``MissingSecret``, before any work) when a key deferred at construction is
        still unset; once every key is present the check is not repeated."""
        pending = self._deferred_keys.get((pipeline, mode))
        if not pending:
            return
        env = Environment(env=dict(os.environ), base_path=Path.cwd())
        missing = [f"{path}: {d.message}" for path, component in pending for d in component.validate(env) if d.severity == "error"]
        if missing:
            raise MissingSecret(f"pipeline {pipeline!r} (mode {mode!r}) cannot run: {'; '.join(missing)}", stage="decision")
        self._deferred_keys.pop((pipeline, mode), None)

    def missing_keys(self) -> dict[str, dict[str, list[str]]]:
        """Unset API-key variables per pipeline and role, plus ``ingest`` for write-path roles
        (``doctor``). Only roles a pipeline or the write path actually uses are listed."""
        roles = self.config.models.roles

        def unset(role_names: list[str]) -> dict[str, list[str]]:
            out: dict[str, list[str]] = {}
            for role_name in role_names:
                role = getattr(roles, role_name)
                if role is not None and role.api_key_env and not os.environ.get(role.api_key_env):
                    out[role_name] = [role.api_key_env]
            return out

        report: dict[str, dict[str, list[str]]] = {}
        write_roles = (["extractor"] if "llm" in self.config.extraction.extractors else []) + ["community"]
        if found := unset(write_roles):
            report["ingest"] = found
        for name, runtime in self.pipelines.items():
            used = ["generator"]
            if any(decision_for_mode(runtime.config.decision, m).uses_llm() for m in runtime.decisions):
                used.append("planner")
            if runtime.review_enabled():
                used.append("reviewer")
            if found := unset(used):
                report[f"pipeline:{name}"] = found
        return report

    # --- lifecycle ---------------------------------------------------------------------------

    def ingest(self, documents, **kwargs):
        return self.write_path.ingest(documents, **kwargs)

    # --- partitioned collection (unified merge M1a) ----------------------------------------------

    def _members(self) -> Any:
        if not self.view.partitioned:
            raise ConfigError("member partitions need a partitioned collection (schema_version: 3, collection.mode: partitioned)", "collection")
        return self.view.attach()

    def build_member(self, member: str, key: str, documents, **kwargs: Any) -> Any:
        """Build one member partition with the shared write path (``ingestion/members.py``) and
        register its ids; returns its ``MemberRef``. Not served until ``compose``."""
        return self._members().build(member, key, documents, embedder=self.embedder, extractor=self.extractor, community_model=self.models.get("community"), **kwargs)

    def compose(self, members: Any = None, *, expected: Any = ANY_BASE) -> dict[str, Any]:
        """Activate a unified generation over ``members`` (default: every live registered member,
        the latest build per unit) by reference; compare-and-set on ``expected`` when given."""
        from .ingestion.compose import compose

        registry = self._members()
        if members is None:
            latest: dict[str, Any] = {}
            for ref in registry.live_members():
                latest[ref.unit_id] = ref
            members = list(latest.values())
        return compose(self.config, registry, list(members), expected=expected)

    def manifest(self) -> dict[str, Any]:
        return self.write_path.current()[1]

    def status(self) -> dict[str, Any]:
        return self.write_path.status()

    def warmup(self) -> dict[str, Any]:
        """Pre-open what the first request of a fresh process would load (startup warm-up; the
        serving front ends run it before ``/ready`` reports ready, ``runtime/warmup.py``). The pin a
        request takes is taken (with a live log member: the slice vector, held for its lease), then
        every distinct backend the catalog and the pipelines' lexical/dense arms read that has a
        ``warm`` facet warms that pin: partitioned collections open every pinned unit and pre-open
        the live log strata (readers, served rows, unit catalogs, dense matrices); a single sqlite
        store loads its lexical segment and vector matrix. No model call and no ranking is made.
        Without an active generation there is nothing to warm (``skipped``, with the reason).
        Any other error propagates (a unit that cannot be opened would fail the first request too)."""
        from .core.errors import IndexNotReady

        started = time.perf_counter()
        try:
            gen, _manifest = self.write_path.pin()
        except IndexNotReady as exc:
            return {"skipped": str(exc)[:300], "seconds": round(time.perf_counter() - started, 3)}
        from .ingestion.layers import LayerBackend

        names = [self.config.storage.catalog]
        for cfg in self.config.all_pipelines().values():
            for arm in ("lexical", "dense"):
                spec = cfg.arms.enabled().get(arm)
                backend = getattr(spec, "backend", None)
                if backend:
                    names.append(backend)
        warmed: dict[str, Any] = {}
        seen: set[int] = set()
        for name in dict.fromkeys(names):
            backend = self.backends.get(name)
            if backend is None or id(backend) in seen or isinstance(backend, LayerBackend):
                continue
            seen.add(id(backend))
            warm = getattr(type(backend), "warm", None)
            if warm is not None:
                warmed[name] = backend.warm(gen)
        self._answers_spend()  # the answer spend scope (built once; its imports are on the first request otherwise)
        return {"generation": gen.generation, "backends": warmed, "seconds": round(time.perf_counter() - started, 3)}

    def _current_generation(self) -> str | None:
        try:
            return self.write_path.current()[0].generation
        except ValueError:
            return None

    def _log_question_settings(self) -> Any:
        """M0 question binding of log intents (``decision/log_question.py``): the question clock,
        the partition clock, the default view and the reviewed vocabulary; None without log intents."""
        logs = self.config.logs
        if logs is None or not logs.intents.enabled:
            return None
        from .decision.log_question import QuestionSettings

        return QuestionSettings(
            timezone=self.config.question.timezone,
            logs_timezone=logs.timezone,
            view_days=logs.collection.serving.default_view_days,
            vocabulary=tuple(self.question_vocabulary),
            now=lambda: self._clock(timezone.utc),
            not_values={f: frozenset(w.casefold() for w in words) for f, words in logs.intents.not_values.items()},
        )

    def capabilities(self, pipeline: str | None = None) -> Capabilities:
        """What the decision agent may plan with: enabled arms, schema, step cap and the exact_id
        identifier patterns (id-field patterns + alias map) of the chosen pipeline."""
        runtime = self.pipelines[pipeline or self.config.default_pipeline]
        return Capabilities(
            arms=runtime.enabled_arms(),
            metadata_schema=self.config.metadata_schema,
            max_steps=runtime.config.decision.max_steps,
            id_patterns=runtime.id_patterns(),
            graph_templates={name: t.description for name, t in graph.templates.items()} if (graph := runtime.config.arms.graph) else {},
            log_intents=bool(self.config.logs is not None and self.config.logs.intents.enabled),
            log_cascade_to_planner=bool(self.config.logs is not None and self.config.logs.intents.enabled and self.config.logs.intents.cascade_to_planner),
            # a partitioned collection's live log member is the archive (staging fix B1)
            log_history=bool(self.config.logs is not None and self.config.logs.intents.enabled and (self.config.logs.collection.archive.enabled or self.view.partitioned)),
            log_question=self._log_question_settings(),
        )

    def _limits(self) -> BudgetLimits:
        budgets = self.config.budgets
        return BudgetLimits(
            max_llm_calls=budgets.max_llm_calls,
            max_input_tokens=budgets.max_input_tokens,
            max_output_tokens=budgets.max_output_tokens,
            max_usd=budgets.max_usd,
            deadline_seconds=budgets.deadline_seconds,
        )

    def _context_for(self, principal: Principal | None, pinned=None, session: Session | None = None, limits: BudgetLimits | None = None) -> RunContext:
        gen, manifest = pinned or self.write_path.pin()
        ctx = new_context(
            gen,
            principal=principal or Principal(id="anonymous", scopes=["search", "answer"]),
            session=session,
            limits=limits or self._limits(),
            price_table=self.price_table,
            acl=self.acl,
            profile=self.config.profile,
            config_fingerprint=self.config.fingerprint(),
            spend=self._answers_spend(),
        )
        ctx.trace.root.set(incomplete_stages=self._incomplete_stages(manifest))
        return ctx

    def _answers_spend(self):
        """The ``serving.answers`` spend scope, built once per engine (``None`` when disabled)."""
        if not hasattr(self, "_answers_spend_cache"):
            from .runtime.answers_spend import answers_spend_from_config

            self._answers_spend_cache = answers_spend_from_config(self.config)
        return self._answers_spend_cache

    # --- sessions ------------------------------------------------------------------------------

    def create_session(self, principal: Principal | None = None, session_id: str | None = None) -> str:
        """A new session in the configured session store, bound to ``(principal, dataset)``."""
        return self.sessions.create(principal or ANONYMOUS, session_id).id

    def close_session(self, session_id: str, principal: Principal | None = None) -> None:
        self.sessions.close(principal or ANONYMOUS, session_id)
        self.view.release_session(session_id)  # live log slices its turns held (partitioned)

    def session(self, session_id: str, principal: Principal | None = None) -> Session:
        """The stored session (turns hold hashes and IDs, never passages unless ``store_text``)."""
        return self.sessions.open(principal or ANONYMOUS, session_id)

    def _begin(self, principal: Principal | None, session_id: str | None, request: SearchRequest | None = None):
        """Pin the generation, open the session (budget clipped, memory re-validated) and build the
        context. Returns ``(ctx, pinned, session, session_trace, request)``."""
        pinned = self.write_path.pin()
        generation = pinned[0].generation
        self._hold(generation)
        try:
            return self._begin_pinned(principal, session_id, request, pinned)
        except BaseException:
            self._unhold(generation)
            raise

    def _hold(self, generation: str) -> None:
        """Cross-process pin of the generation a request reads (B8, ``logs.pins``): retention cannot
        delete it until the request ends (``_finish``) and, while it is current, between requests."""
        if self.process_pins is not None:
            self.process_pins.acquire(generation)

    def _unhold(self, generation: str) -> None:
        if self.process_pins is not None:
            self.process_pins.release(generation)

    def _begin_pinned(self, principal: Principal | None, session_id: str | None, request: SearchRequest | None, pinned: Any):
        if session_id is None:
            return self._context_for(principal, pinned), pinned, None, None, request
        session = self.sessions.open(principal or ANONYMOUS, session_id)
        # A session holds the live log slices its turns read for its idle lifetime (plus one answer
        # deadline: ``last_seen_at`` moves at the end of the turn), released on close (§6.7).
        self.view.hold_session(session.id, pinned[0].generation, float(self.config.sessions.ttl_seconds) + float(self.config.budgets.deadline_seconds))
        # Clip to the session remainder and hold it for this turn in one compare-and-set (QA-05).
        limits = self.sessions.reserve(session, self._limits())
        try:
            ctx = self._context_for(principal, pinned, session=session, limits=limits)
            catalog = self.backends[self.config.storage.catalog].catalog

            def exists(ids: list[str]) -> set[str]:
                return {d.id for d in catalog.get(ctx.generation, ids)}

            info = self.sessions.prepare(ctx, session, exists)
            if request is not None:
                added = self.sessions.exclusions(ctx, session, list(request.exclude_ids))
                if added:
                    request = request.model_copy(update={"exclude_ids": [*request.exclude_ids, *added]})
                    info["exclusions_added"] = added
        except BaseException:
            self.sessions.release(session)
            raise
        return ctx, pinned, session, info, request

    def _finish(self, ctx: RunContext, session: Session | None, info: dict[str, Any] | None, query: str | None, shown: list[str], answer_id: str | None = None) -> None:
        try:
            if session is None or info is None:
                return
            info["usage"] = self.sessions.finish(ctx, session, query, shown, answer_id)
        finally:
            self._unhold(ctx.generation.generation)

    @staticmethod
    def _incomplete_stages(manifest: dict[str, Any]) -> list[str]:
        stages = manifest.get("coverage", {}).get("stages", {})
        return sorted(name for name, stage in stages.items() if stage.get("status") == "incomplete")

    # --- requests ----------------------------------------------------------------------------

    def _check_request(self, request: SearchRequest) -> None:
        rc = self.config.request
        unknown = sorted(set(request.record_context) - set(rc.context_keys))
        if unknown:
            raise ValueError(f"unknown context fields: {unknown} (declare them under request.context_keys)")
        size = len(json.dumps(request.record_context, ensure_ascii=False).encode("utf-8"))  # UTF-8 bytes of the JSON
        if size > rc.context_max_bytes:
            raise RequestRejected(f"record_context is {size} bytes of UTF-8 JSON; the limit is request.context_max_bytes = {rc.context_max_bytes}")
        long = sorted(k for k, v in request.record_context.items() if len(v) > rc.context_max_field_chars)
        if long:
            raise RequestRejected(f"record_context fields {long} exceed request.context_max_field_chars = {rc.context_max_field_chars} characters")
        if request.evidence_chars is not None:  # per-request evidence budget: opt-in, never widened silently
            if rc.evidence_chars_max is None:
                raise RequestRejected("per-request evidence budget is not enabled (request.evidence_chars_max)")
            if request.evidence_chars > rc.evidence_chars_max:
                raise RequestRejected(f"evidence_chars exceeds request.evidence_chars_max ({rc.evidence_chars_max})")

    def _scope(self, ctx: RunContext, request: SearchRequest, extra_filters: dict[str, Any]) -> tuple[Scope, list[Predicate]]:
        acl = ctx.constraints()
        filters = {**request.filters, **extra_filters}
        excluded = list(dict.fromkeys(request.exclude_ids))
        record_id = request.record_context.get(self.config.request.record_id_key)
        if record_id and record_id not in excluded:
            excluded.append(record_id)  # the record being looked at never retrieves itself
        predicates = [*acl, *(Predicate(field=k, op="eq", value=v) for k, v in filters.items())]
        return Scope(predicates=predicates, exclude_ids=excluded), acl

    def _check_filters(self, ctx: RunContext, request: SearchRequest) -> tuple[SearchRequest, FilterCheck]:
        """Validate the caller's filters against ``MetadataSchema`` before anything executes (QA-02):
        unambiguous coercions are applied (and traced), invalid or undeclared filters are applied as
        given and reported, so no aggregate under them is ever exact."""
        if not request.filters:
            return request, FilterCheck()
        resolver = ValueResolver(self.config.metadata_schema, self.records, ctx.generation, ctx.constraints())
        check = check_request_filters(resolver, dict(request.filters))
        ctx.trace.event("request_filters", **check.trace())
        if check.applied != request.filters or any(type(check.applied[k]) is not type(v) for k, v in request.filters.items()):
            request = request.model_copy(update={"filters": dict(check.applied)})
        return request, check

    @staticmethod
    def _unsupported(*groups: list[UnsupportedConstraint]) -> list[UnsupportedConstraint]:
        """Concatenate constraint lists, dropping exact duplicates (same text and reason)."""
        seen: set[tuple[str, str]] = set()
        out: list[UnsupportedConstraint] = []
        for group in groups:
            for item in group:
                key = (item.constraint_text, item.reason)
                if key not in seen:
                    seen.add(key)
                    out.append(item)
        return out

    @staticmethod
    def _bound_aggregate(aggregate: dict[str, Any], unsupported: list[UnsupportedConstraint]) -> None:
        """An aggregate computed under an unapplied qualifier or an invalid request filter answers a
        different question: it is reported ``bounded`` with the reasons attached (never ``exact``)."""
        if not unsupported:
            return
        if aggregate.get("exactness") == "exact":
            aggregate["exactness"] = "bounded"
            # the deterministic sentence states the exactness too: never "(exact; ...)" on a
            # bounded record (2026-11-09, B5 q023/q041: the generator quoted a stale "exact 0")
            if aggregate.get("rendering"):
                value = aggregate.get("value")
                aggregate["rendering"] = relabel_bounded(aggregate["rendering"], zero=isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0)
        listed = list(aggregate.get("unsupported") or [])
        for item in unsupported:
            dumped = item.model_dump(mode="json")
            if dumped not in listed:
                listed.append(dumped)
        aggregate["unsupported"] = listed

    def _canonical_record(self, ctx: RunContext, request: SearchRequest) -> tuple[dict[str, str] | None, str | None]:
        """The catalog record the caller context names, read through the principal's ACL scope.

        Returns ``(canonical, unresolved_id)``. The lookup takes the same access-scoped path as
        ``documents_in``: a record id that is unknown in this generation or outside the
        principal's scope resolves to nothing (the two cases are indistinguishable to the caller)
        and is returned as ``unresolved_id`` so it can be reported, never sent to a model.
        """
        rc = self.config.request
        record_id = request.record_context.get(rc.record_id_key)
        if not record_id:
            return None, None
        found = self.documents_in(ctx, [record_id])
        if not found or found[0].id != record_id:
            return None, record_id
        doc = found[0]
        canonical: dict[str, str] = {rc.record_id_key: record_id, "title": doc.title}
        for name in rc.canonical_fields:
            if doc.fields.get(name):
                canonical[name] = doc.fields[name][: rc.context_max_field_chars]
            elif doc.metadata.get(name) is not None:
                canonical[name] = str(doc.metadata[name])
        return canonical, None

    def _caller_context(self, state: _Retrieval) -> dict[str, str]:
        """The caller's record context minus a record id that did not resolve within scope."""
        context = dict(state.request.record_context)
        if state.unresolved_record_id is not None:
            context.pop(self.config.request.record_id_key, None)
        return context

    def _record_context(self, state: _Retrieval) -> dict[str, Any]:
        context = {"caller_context_unverified": self._caller_context(state)}
        if state.decision_request.canonical_record is not None:
            context = {"canonical_record": state.decision_request.canonical_record, **context}
        return context

    # --- decision --------------------------------------------------------------------------------

    def _decide(
        self, ctx: RunContext, request: SearchRequest, pipe: PipelineRuntime, mode: str
    ) -> tuple[DecisionRequest, Capabilities, Decision, Intents, str | None, Any]:
        capabilities = self.capabilities(pipe.name)
        support_only = self._support_only(request)
        if support_only:
            # today's schema for a support question (intents, planner, review, answer contract)
            capabilities = self._knowledge_capabilities(ctx, capabilities)
        logs = self.config.logs
        if capabilities.log_intents and logs is not None and logs.intents.planner_context:
            from .decision import log_context

            built: dict[str, Any] = {}

            def pack() -> dict[str, Any]:  # built once, only when the llm planner runs
                if not built:
                    built.update(log_context.build(self, ctx, arms=capabilities.arms, max_chars=logs.intents.planner_context_max_chars))
                    ctx.trace.event("planner_log_context", chars=len(json.dumps(built, default=str)), truncated=built.get("truncated", []))
                return built

            capabilities = capabilities.model_copy(update={"log_context": pack})
        typed = self._typed_planning(ctx, pipe)
        decision_cfg = pipe.config.decision
        router = typed is not None and decision_cfg.typed_query.routing == "router"
        if typed is not None and not router:
            capabilities = capabilities.model_copy(update={"typed": typed})
        if decision_cfg.arm_cards:
            from .decision.arm_cards import arm_limits

            # typed limits are offered to the planner's cards only when the planner plans typed steps
            capabilities = capabilities.model_copy(update={"arm_limits": arm_limits(pipe.config.arms, decision_cfg, None if router else typed)})
        explicit = scope_spec_for(request.aggregate) if request.aggregate else None
        canonical, unresolved = self._canonical_record(ctx, request)
        record_context = dict(request.record_context)
        if unresolved is not None:
            record_context.pop(self.config.request.record_id_key, None)  # never shown to the planner
            ctx.trace.root.set(record_context_unresolved=True)
        decision_request = DecisionRequest(
            query=request.query,
            filters=dict(request.filters),
            exclude_ids=list(request.exclude_ids),
            record_context=record_context,
            aggregate=explicit.measure if explicit is not None else None,
            scope=explicit,
            canonical_record=canonical,
            as_of=self._anchor(ctx, request),
            as_of_source="request" if request.as_of else "clock",
        )
        # The deployment's structured targets bound the aggregate grammar (QA-03): without them the
        # schema-less permissive grammar turns ordinary questions ("the latest release notes") into
        # aggregate intents. Every surface (search, answer, stream, tools, HTTP, MCP) comes through here.
        intents = detect_intents(request.query, capabilities.id_patterns, capabilities.structured_targets())
        routed = None
        agent = pipe.decisions[mode]
        if self.two_halves is not None:
            from .decision.two_halves import TwoHalvesAgent

            agent = TwoHalvesAgent(agent, self.two_halves)  # M2b-2: knowledge half + operations half
            if not support_only:
                # staging round 5 (N12/N13): the operations half is deterministic, so it is planned
                # before the router and the planner; a question that gets none (no step) is a
                # support question and keeps today's schema, intents and SQL writer catalog
                operations = agent.prepare(ctx, decision_request, intents, capabilities)
                if operations is None or not operations.steps:
                    support_only = True
                    narrowed = self._knowledge_capabilities(ctx, capabilities)
                    if narrowed is not capabilities:
                        capabilities = narrowed
                        intents = detect_intents(request.query, capabilities.id_patterns, capabilities.structured_targets())
        self._halves.agent = agent if self.two_halves is not None else None  # read back by _retrieve (same thread)
        if router and decision_for_mode(decision_cfg, mode).uses_llm():
            # a separate metered call decides typed vs retrieval (``routing: router``); the planner
            # request does not depend on it, so the two run concurrently when allowed
            if self._router_parallel(ctx, pipe):
                routed, decision = self._route_and_decide(ctx, request, decision_request, pipe, typed, capabilities, agent, intents, support_only=support_only)
            else:
                routed = self._typed_route(ctx, request, decision_request, pipe, typed, capabilities, support_only=support_only)
                decision = agent.decide(ctx, decision_request, intents, capabilities)
            return decision_request, capabilities, decision, intents, unresolved, routed
        decision = agent.decide(ctx, decision_request, intents, capabilities)
        return decision_request, capabilities, decision, intents, unresolved, routed

    def _router_parallel(self, ctx: RunContext, pipe: PipelineRuntime) -> bool:
        """Whether the typed-router chain (router call, then the SQL writer and its repair, or the
        typed IR call) runs concurrently with the planner call (``typed_query.parallel``, default on;
        2026-11-08 latency fix). Sequential when the run's call budget cannot cover both chains: then
        which call a ``budget_exhausted`` hits stays the deterministic sequential order (router first)."""
        cfg = pipe.config.decision
        if not cfg.typed_query.parallel:
            return False
        remaining = ctx.budget.remaining("calls")
        if remaining is None:
            return True
        arm = pipe.config.arms.typed_query
        chain = 1 + ((1 + arm.sql_surface.repair_attempts) if arm is not None and arm.mode == "sql_surface" else 1)
        planner = 1 + cfg.schema_repair_attempts
        return remaining >= chain + planner

    def _route_and_decide(
        self,
        ctx: RunContext,
        request: SearchRequest,
        decision_request: DecisionRequest,
        pipe: PipelineRuntime,
        typed: Any,
        capabilities: Capabilities,
        agent: Any,
        intents: Intents,
        support_only: bool = False,
    ) -> tuple[_Routed, Decision]:
        """The typed-router chain in a worker thread while the planner runs here. Both meter through
        the run's budget (thread-safe); the worker's events and spans are recorded under this
        thread's current span, exactly where the sequential run records them, so the trace's
        ``model_calls`` order (router chain, then planner) is unchanged. Neither result is used
        before both finish; the merge in ``_apply_typed_route`` is the same as the sequential one."""
        parent = ctx.trace.current()

        def route() -> _Routed:
            with ctx.trace.attached(parent):
                return self._typed_route(ctx, request, decision_request, pipe, typed, capabilities, support_only=support_only)

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="typed-router") as pool:
            future = pool.submit(route)
            try:
                decision = agent.decide(ctx, decision_request, intents, capabilities)
            except BaseException:
                future.exception()  # wait for the router chain; the planner's error wins
                raise
            routed = future.result()
        ctx.trace.event("typed_router_parallel", planner_outcome=decision.outcome, route=routed.route)
        return routed, decision

    def _typed_planning(self, ctx: RunContext, pipe: PipelineRuntime) -> Any:
        """``TypedPlanning`` for the llm planner (M6): only under ``pipeline.decision.typed_query.enabled``
        with the ``typed_query`` arm bound and a sealed projection of the configured spec for the
        pinned generation; otherwise ``None`` (the planner payload and schema stay unchanged)."""
        if not pipe.config.decision.typed_query.enabled:
            return None
        arm = pipe.arms.get("typed_query")
        catalog = arm.catalog_for(ctx) if arm is not None and hasattr(arm, "catalog_for") else None
        ctx.trace.event("typed_query_offered", offered=catalog is not None, reason=None if catalog is not None else "projection_unavailable")
        if catalog is None:
            return None
        from .decision.typed_plan import TypedPlanning

        mode = getattr(arm.cfg, "mode", "ir")
        return TypedPlanning(catalog=catalog, row_limit=arm.cfg.row_limit, max_groups=arm.cfg.max_groups, text=arm.cfg.text_backend is not None and mode == "ir", mode=mode)

    def _typed_route(
        self, ctx: RunContext, request: SearchRequest, decision_request: DecisionRequest, pipe: PipelineRuntime, typed: Any, capabilities: Capabilities, support_only: bool = False
    ) -> _Routed:
        """``routing: router`` (``decision/typed_router.py``): one metered router call, then for a typed
        route the typed execution with this request's ACL and filters (mode ir: one typed-plan call,
        compiled and run by the arm; mode sql_surface: the SQL route). Runs before the planner; the
        result is attached to the planner's decision by ``_apply_typed_route``. ``support_only``
        (staging round 5, N13): no operations half, so the router text and the SQL writer catalog
        are today's."""
        from .decision import typed_router
        from .decision.base import DroppedStep

        cfg = pipe.config.decision
        arm = pipe.arms["typed_query"]
        role = getattr(self.config.models.roles, cfg.typed_query.router_role)
        routes = None
        if cfg.arm_cards:
            from .decision.arm_cards import arm_limits, router_routes
            from .decision.llm import LLM_ARMS

            limits = arm_limits(pipe.config.arms, cfg, typed)
            # a support question's router payload is today's (N13): the log-intent arm text only
            # where an operations half can be planned
            routes = router_routes([a for a in LLM_ARMS if a in capabilities.arms], limits["typed_query"], typed.mode, log_intents=capabilities.log_intents and not support_only)
        summary = typed_router.catalog_summary(typed.catalog, detailed=cfg.arm_cards)
        payload = typed_router.router_payload(request.query, dict(request.filters), summary, routes)
        try:
            reply = typed_router.call(ctx, role, payload)
        except typed_router.RouterFailure as failure:
            ctx.trace.event("typed_router", status="error", category=failure.category)
            detail = f"the typed router call returned no valid reply ({failure.category}); the planner ran without a typed route"
            return _Routed(route="error", dropped=DroppedStep(arm="typed_query", reason="typed_router_error", detail=detail))
        ctx.trace.event("typed_router", status="ok", route=reply.route, sub_question=Sensitive(reply.sub_question or ""), reason=Sensitive(reply.reason))
        if reply.route == "retrieval":
            return _Routed(route="retrieval")
        question = (reply.sub_question or "").strip()
        scope, acl = self._scope(ctx, request, {})
        filters = dict(request.filters)
        if typed.mode == "sql_surface":
            from .sql_surface import route

            result = route.run(self, ctx, arm, question, acl, filters, arm_cards=cfg.arm_cards, grounding=request.query, collection=not support_only)
            ctx.trace.event("sql_route", status=result.status, model_calls=result.model_calls, repaired=result.repaired, code=(result.error or {}).get("code"), fingerprint=((result.evidence or {}).get("sql_surface") or {}).get("fingerprint"), router=True)
            if result.status == "executed" and result.evidence is not None:
                return _Routed(route="typed", step=TypedStep(arm="typed_query", question=question), result=ArmResult(arm="typed_query", aggregates=[result.evidence]))
            failure = route.Failure(step=None, result=result)
            return _Routed(route="typed", dropped=failure.dropped(), constraints=failure.constraints())
        return self._typed_route_ir(ctx, decision_request, typed, arm, role, question, scope, acl)

    def _typed_route_ir(self, ctx: RunContext, decision_request: DecisionRequest, typed: Any, arm: Any, role: Any, question: str, scope: Any, acl: list[Any]) -> _Routed:
        """Mode ir under the router: the M6 typed section for the sub-question (stage ``typed_ir``),
        compiled with this request's filters and ACL, executed by the arm (which recompiles it)."""
        from .decision import typed_router
        from .decision.base import DroppedStep
        from .decision.typed_plan import as_draft, error_constraints, shape, typed_payload
        from .decision.typed_query import TypedStepInvalid

        schema = self.config.metadata_schema
        filters = dict(decision_request.filters)
        resolver = ValueResolver(schema, self.records, ctx.generation, [*ctx.constraints(), *self._knowledge_value_scope()], anchor=decision_request.as_of, value_scoped=bool(self._knowledge_value_scope()))
        route_text = "typed query (router)"

        def drop(reason: str, detail: str, constraints: list[UnsupportedConstraint]) -> _Routed:
            ctx.trace.event("typed_router_ir", status=reason)
            return _Routed(route="typed", dropped=DroppedStep(arm="typed_query", reason=reason, detail=detail), constraints=constraints)  # type: ignore[arg-type]

        try:
            planned = typed_router.call_ir(ctx, role, question, filters, typed_payload(typed, schema, resolver))
        except typed_router.RouterFailure as failure:
            flag = UnsupportedConstraint(constraint_text=route_text, reason="typed_route_failed", suggestion=f"the typed route could not write a typed query ({failure.category})")
            return drop("step_not_executable", f"the typed route could not write a typed query ({failure.category})", [flag])
        ctx.trace.event("typed_planner_step", analysis=Sensitive(planned.analysis), answerability=planned.answerability, gaps=Sensitive(list(planned.gaps)), shape=shape(planned.query) if planned.query is not None else None, router=True)
        if planned.answerability == "not_answerable" or planned.query is None:
            text = "the typed route reported that the projection catalog cannot express the sub-question"
            return drop("planner_not_answerable", text, [UnsupportedConstraint(constraint_text=route_text, reason="planner_not_answerable", suggestion=text)])
        draft = as_draft(planned.query)
        try:
            compiled = typed.compiler(resolver).compile(draft, filters, ctx.constraints())
        except TypedStepInvalid:
            flag = UnsupportedConstraint(constraint_text=route_text, reason="typed_route_failed", suggestion="the typed query contradicted a request filter")
            return drop("step_not_executable", "the typed route's query contradicted a request filter (invalid_step)", [flag])
        if compiled.errors:
            return drop("step_not_executable", f"{shape(draft)} is not executable ({len(compiled.errors)} compile error(s)); its constraints are reported as unsupported", error_constraints(compiled.errors))
        result = arm.run(ctx, ArmInput(query=decision_request.query, scope=scope, acl=acl, typed=draft, filters=filters))
        aggregate = result.aggregates[0] if result.aggregates else None
        if aggregate is None or aggregate.get("refused"):
            flags = [UnsupportedConstraint.model_validate(u) for u in (aggregate or {}).get("unsupported", [])]
            return drop("step_not_executable", "the typed route's query was refused at execution; its constraints are reported as unsupported", flags or [UnsupportedConstraint(constraint_text=route_text, reason="typed_route_failed", suggestion="the typed query was refused at execution")])
        gaps = []
        if planned.answerability == "partial":
            gaps.append(UnsupportedConstraint(constraint_text=f"{len(planned.gaps)} planner-reported gap(s) in the typed query", reason="qualifier_not_applied", suggestion="the typed route reported that part of the question has no column in the projection catalog; the typed result covers a wider population"))
        ctx.trace.event("typed_router_ir", status="executed", fingerprint=compiled.fingerprint)
        return _Routed(route="typed", step=TypedStep(arm="typed_query", draft=draft, answerability=planned.answerability, unapplied=gaps), result=result)

    def _apply_typed_route(self, ctx: RunContext, state: _Retrieval, routed: _Routed | None) -> None:
        """Attach the typed router's outcome to the planner's decision. An executed route becomes the
        first step of a decided plan (its result pre-executed; qualifiers of the original question it
        does not express make it ``bounded``); a route that did not execute is a ``DroppedStep`` with
        its constraints and one ``aggregate_intent_without_scope`` per aggregate intent (as
        ``on_step_failure: partial`` for this step); a router error is a ``DroppedStep`` only."""
        from .decision.base import DroppedStep

        if routed is None or routed.route == "retrieval":
            return
        decision = state.decision
        if routed.dropped is not None:
            added = list(routed.constraints)
            if routed.route == "typed":
                added += [UnsupportedConstraint(constraint_text=a.text, reason="aggregate_intent_without_scope", suggestion="the typed route for this aggregate did not execute and was dropped") for a in decision.intents.aggregates]
            ctx.trace.event("typed_route_dropped", reason=routed.dropped.reason, constraints=len(added))
            state.decision = decision.model_copy(update={"dropped_steps": [*decision.dropped_steps, routed.dropped], "unsupported": self._unsupported(decision.unsupported, added)})
            return
        step = routed.step
        assert step is not None and routed.result is not None
        replaced = self._fallback_replaced(state)
        if replaced is not None and self._route_answers_aggregate(state):
            # staging round 2 (N5): the two-halves fallback replaced a planner abstention on a
            # question without an operations half; the executed typed route answers it alone, as it
            # does without the log member (the fallback is withdrawn, recorded in the trace)
            ctx.trace.event("two_halves_fallback_withdrawn", reason="typed_route_only", planner_failure=replaced.failure)
            state.two_halves.fallback = False
            decision = replaced
        if decision.outcome == "abstain" and self._route_answers_aggregate(state):
            # 2026-11-08 parity fix: the planner abstained (e.g. its reply violated the plan schema)
            # but the routed typed result executed and the question asks an aggregate. The executed
            # result is exact evidence on its own: it becomes the plan, the abstention is recorded.
            missing = self._routed_unapplied(ctx, state, step, routed.result)
            if missing:
                step = step.model_copy(update={"unapplied": [*step.unapplied, *missing]})
                ctx.trace.event("qualifiers_not_applied", count=len(missing), reasons=sorted({u.reason for u in missing}), route="typed_router")
            state.routed_steps.add(id(step))
            state.sql_routes[id(step)] = routed.result
            note = f"the planner abstained ({decision.failure or 'no_plan'}); the plan is the executed typed route only"
            ctx.trace.event("typed_route_kept", planner_outcome=decision.outcome, planner_failure=decision.failure)
            state.decision = Decision(
                plan=Plan(steps=[step]),
                outcome="decided",
                agent=decision.agent,
                stage=decision.stage,
                policy_applied="typed_route_only",
                intents=decision.intents,
                failure=decision.failure,
                note=note,
                unsupported=self._unsupported(decision.unsupported, [*step.unapplied]),
                dropped_steps=list(decision.dropped_steps),
            )
            operations = state.two_halves.operations if replaced is not None and state.two_halves is not None else None
            if operations is not None and (operations.unsupported or operations.notes):
                from .decision.two_halves import TwoHalvesPlanner

                # a stated operations refusal or analytics note stays stated beside the typed route
                state.decision = TwoHalvesPlanner._with(state.decision, list(operations.unsupported), list(operations.notes), policy=None)
            return
        if decision.outcome != "decided" or decision.plan is None or len(decision.plan.steps) >= 20:
            why = f"the planner decision is {decision.outcome}" if decision.outcome != "decided" else "the plan has the maximum number of steps"
            ctx.trace.event("typed_route_dropped", reason="typed_route_discarded", outcome=decision.outcome)
            dropped = DroppedStep(arm="typed_query", reason="typed_route_discarded", detail=f"the typed route executed but its result is not used: {why}")
            state.decision = decision.model_copy(update={"dropped_steps": [*decision.dropped_steps, dropped]})
            return
        missing = self._routed_unapplied(ctx, state, step, routed.result)
        if missing:
            step = step.model_copy(update={"unapplied": [*step.unapplied, *missing]})
            ctx.trace.event("qualifiers_not_applied", count=len(missing), reasons=sorted({u.reason for u in missing}), route="typed_router")
        state.routed_steps.add(id(step))
        state.sql_routes[id(step)] = routed.result
        plan = Plan(steps=[step, *decision.plan.steps])
        state.decision = decision.model_copy(update={"plan": plan, "unsupported": self._unsupported(decision.unsupported, [*step.unapplied])})

    @staticmethod
    def _fallback_replaced(state: _Retrieval) -> Decision | None:
        """The abstained knowledge decision the two-halves fallback replaced, when the request has
        no operations step (a support-only question); else ``None``."""
        record = state.two_halves
        decision = state.decision
        if record is None or not record.fallback or decision.fallback != "deterministic_plan":
            return None
        replaced = record.replaced
        if replaced is None or replaced.outcome != "abstain":
            return None
        if record.operations is not None and record.operations.steps:
            return None
        return replaced

    @staticmethod
    def _route_answers_aggregate(state: _Retrieval) -> bool:
        """The question asks an aggregate (an intent, or any aggregate operator word: V3.2's
        permissive cue) and the caller typed none: an executed typed route is then its answer."""
        from .decision.intent import mentions_aggregate_operator

        request = state.decision_request
        if request.aggregate is not None or request.scope is not None:
            return False
        return state.decision.intents.asks_aggregate() or mentions_aggregate_operator(request.query)

    def _routed_unapplied(self, ctx: RunContext, state: _Retrieval, step: TypedStep, result: ArmResult) -> list[UnsupportedConstraint]:
        """Qualifiers of the **original** question the routed typed result does not express (the
        router's sub-question may have dropped one), as the planner path checks them."""
        request = state.decision_request
        if request.aggregate is not None or request.scope is not None:
            return []
        from .decision.intent import untargeted_constraint
        from .decision.qualifiers import NO_OPERATOR, qualifier_candidates

        schema = self.config.metadata_schema
        intents = state.decision.intents
        resolver = ValueResolver(schema, self.records, ctx.generation, [*ctx.constraints(), *self._knowledge_value_scope()], value_scoped=bool(self._knowledge_value_scope()))
        if step.question is not None:
            from .sql_surface import route

            return route.executed_unapplied(result.aggregates[0], request.query, dict(request.filters), schema, resolver, intents)
        from .decision.typed_plan import typed_unapplied

        assert step.draft is not None
        missing = typed_unapplied(qualifier_candidates(request.query, schema, resolver, intents), step.draft, request.filters, schema)
        if not intents.aggregate() and (step.draft.measure is not None or step.draft.group_by):
            missing.extend([untargeted_constraint(u) for u in intents.untargeted] or [NO_OPERATOR])
        return missing

    @staticmethod
    def _clock(zone: Any) -> datetime:
        """The wall clock in ``zone`` (one place, so tests can pin it)."""
        return datetime.now(zone)

    def _anchor(self, ctx: RunContext, request: SearchRequest) -> date:
        """The anchor day for relative calendar expressions in ``metadata_schema.timezone``: the
        request's ``as_of`` (a value without an offset is read in that zone), else today there.
        Recorded in the trace so every window is reproducible."""
        from .backends._scope import zone_of
        from .decision.windows import anchor_date

        zone = zone_of(self.config.metadata_schema)
        source = "request" if request.as_of else "clock"
        moment = datetime.fromisoformat(request.as_of.strip()) if request.as_of else self._clock(timezone.utc)
        day = anchor_date(moment, zone)
        ctx.trace.event("time_anchor", as_of=day.isoformat(), timezone=self.config.metadata_schema.timezone or "UTC", source=source)
        return day

    @staticmethod
    def _steps(decision: Decision, decision_request: DecisionRequest) -> list[Any]:
        """The steps to execute: the decided plan plus the caller's explicit typed scope when the plan lacks one."""
        if decision.plan is None:
            return []
        steps = list(decision.plan.steps)
        explicit = decision_request.scope
        if explicit is not None and decision.outcome == "decided" and not decision.plan.structured_steps():
            steps.append(StructuredStep(arm="structured", scope=explicit))
        return steps

    # --- execution ---------------------------------------------------------------------------

    def _run_step(self, ctx: RunContext, state: _Retrieval, step: Any) -> ArmResult:
        request, arms = state.request, state.pipe.arms
        if isinstance(step, StructuredStep) and step.intent is not None:
            return self._run_log_step(ctx, step)
        if isinstance(step, TypedStep):
            arm = arms.get("typed_query")
            if arm is None:
                raise ValueError("plan selected the typed_query arm, which is not enabled")
            if id(step) in state.routed_steps:
                return state.sql_routes[id(step)]  # executed by the typed router before the planner
            if step.question is not None:
                routed = state.sql_routes.get(id(step))
                return routed if routed is not None else self._sql_route_hop(ctx, state, arm, step)
            scope, acl = self._scope(ctx, request, {})
            # the arm recompiles the draft with this ACL (authoritative); request filters are immutable
            return arm.run(ctx, ArmInput(query=request.query, scope=scope, acl=acl, typed=step.draft, filters=dict(request.filters)))
        if isinstance(step, StructuredStep):
            arm = arms.get("structured")
            if arm is None:
                raise ValueError("plan selected the structured arm, which is not enabled")
            scope, acl = self._scope(ctx, request, {})
            query, aggregate = request.query, step.scope
        else:
            arm = arms.get(step.arm)
            if arm is None:
                raise ValueError(f"plan selected disabled arm: {step.arm}")
            scope, acl = self._scope(ctx, request, dict(step.filters))
            query, aggregate = step.query, None
        if self.two_halves is not None:
            # staging F2: a knowledge-half step reads the knowledge sources only when the collection
            # holds a log member (the plan is unchanged; ``decision/two_halves.knowledge_predicate``)
            limit = self.two_halves.knowledge_predicate(step, request.filters)
            if limit is not None:
                scope = scope.model_copy(update={"predicates": [*scope.predicates, limit]})
                ctx.trace.event("knowledge_scope", arm=step.arm, sources=list(limit.value))
        investigation = None
        if getattr(step, "log_scope", None) is not None:
            from .decision.log_communities import shared_error_query
            if shared_error_query(query):
                from .runtime.log_shared_errors import prepare
            else:
                from .runtime.log_investigation import prepare
            scope, query, investigation = prepare(self, ctx, step.log_scope, scope, query)
        candidates = getattr(arm.cfg, "candidates", 200)
        inputs = ArmInput(
            query=query,
            scope=scope,
            acl=acl,
            candidates=candidates,
            aggregate=aggregate,
            graph=getattr(step, "graph", None),
            edge_kinds=["native_link", "co_firing"] if investigation is not None else None,
            incomplete_stages=list(ctx.trace.root.attributes.get("incomplete_stages", [])),
            allow_incomplete=state.pipe.config.allow_incomplete_arms,
        )
        result = arm.run(ctx, inputs)
        if investigation is not None:
            result.reports.append(investigation)
        return result

    def _route_sql(self, ctx: RunContext, state: _Retrieval) -> None:
        """Run the plan's SQL-route steps (``typed_query`` ``mode: sql_surface``) before anything else
        executes, with this request's ACL and filters (``sql_surface/route.py``): executed results
        are kept for ``_run_step`` with the executed SQL's unapplied qualifiers attached to the step;
        failed routes are dropped and ``on_step_failure`` rewrites the decision (recorded)."""
        decision = state.decision
        routes = [s for s in decision.plan.steps if isinstance(s, TypedStep) and s.question is not None and id(s) not in state.routed_steps] if decision.plan is not None else []
        if not routes:
            return
        from .sql_surface import route

        arm = state.pipe.arms.get("typed_query")
        if arm is None:
            raise ValueError("plan selected the typed_query arm, which is not enabled")
        _scope, acl = self._scope(ctx, state.request, {})
        filters = dict(state.request.filters)
        schema = self.config.metadata_schema
        replaced: dict[int, TypedStep] = {}
        added: list[UnsupportedConstraint] = []
        failures: list[Any] = []
        for step in routes:
            result = route.run(self, ctx, arm, step.question, acl, filters, arm_cards=state.pipe.config.decision.arm_cards, grounding=state.request.query, collection=not self._state_support_only(state))
            ctx.trace.event("sql_route", status=result.status, model_calls=result.model_calls, repaired=result.repaired, code=(result.error or {}).get("code"), fingerprint=((result.evidence or {}).get("sql_surface") or {}).get("fingerprint"))
            if result.status != "executed" or result.evidence is None:
                failures.append(route.Failure(step=step, result=result))
                continue
            missing: list[UnsupportedConstraint] = []
            if state.decision_request.aggregate is None and state.decision_request.scope is None:
                resolver = ValueResolver(schema, self.records, ctx.generation, [*ctx.constraints(), *self._knowledge_value_scope()], value_scoped=bool(self._knowledge_value_scope()))
                missing = route.executed_unapplied(result.evidence, state.request.query, filters, schema, resolver, decision.intents)
            kept = step.model_copy(update={"unapplied": [*step.unapplied, *missing]}) if missing else step
            replaced[id(step)] = kept
            added.extend(missing)
            state.sql_routes[id(kept)] = ArmResult(arm="typed_query", aggregates=[result.evidence])
        if replaced:
            steps = [replaced.get(id(s), s) for s in decision.plan.steps]  # type: ignore[union-attr]
            decision = decision.model_copy(update={"plan": Plan(steps=steps), "unsupported": self._unsupported(decision.unsupported, added)})
            if added:
                ctx.trace.event("qualifiers_not_applied", count=len(added), reasons=sorted({u.reason for u in added}), route="sql_surface")
            failures = [route.Failure(step=replaced.get(id(f.step), f.step), result=f.result) for f in failures]
        if failures:
            cfg = state.pipe.config.decision
            decision = route.apply_failures(decision, failures, policy=cfg.on_step_failure, on_failure=cfg.on_failure, request=state.decision_request, capabilities=state.capabilities, trace=ctx.trace)
        state.decision = decision

    def _sql_route_hop(self, ctx: RunContext, state: _Retrieval, arm: Any, step: TypedStep) -> ArmResult:
        """A SQL-route step outside the first plan (a review hop): run in place; a route that does not
        execute is reported as a refused, bounded aggregate record (never dropped silently)."""
        from .sql_surface import route

        _scope, acl = self._scope(ctx, state.request, {})
        result = route.run(self, ctx, arm, step.question or "", acl, dict(state.request.filters), arm_cards=state.pipe.config.decision.arm_cards, grounding=state.request.query, collection=not self._state_support_only(state))
        ctx.trace.event("sql_route", status=result.status, model_calls=result.model_calls, repaired=result.repaired, code=(result.error or {}).get("code"), hop=True)
        if result.status == "executed" and result.evidence is not None:
            return ArmResult(arm="typed_query", aggregates=[result.evidence])
        return ArmResult(arm="typed_query", aggregates=[route.refusal_record(ctx, result, route.Failure(step=step, result=result).constraints())])

    def _run_log_step(self, ctx: RunContext, step: StructuredStep) -> ArmResult:
        """A typed log intent (review B5): the same ``run_intent`` the log tools call. The aggregate
        record carries ``intent``, ``args``, ``rows`` and ``rendering`` (the deterministic sentence
        the answer quotes for its numbers)."""
        from .arms.structured import aggregate_record

        output = self.run_log_intent(ctx, step.intent)
        result = output.result or ScopeResult(exactness="bounded", denominator=0, unsupported=[])
        if output.status == "refused":
            from .models import UnsupportedConstraint

            result = result.model_copy(update={"exactness": "bounded", "unsupported": [UnsupportedConstraint(constraint_text=step.intent.intent, reason="not_supported_by_backend", suggestion=output.refusal)]})
        else:
            result = result.model_copy(update={"exactness": output.exactness})
        record = aggregate_record(output.scope or step.scope, result)
        record.update(
            {
                "intent": output.intent,
                "args": output.args,
                "rows": output.rows,
                "columns": output.columns,
                "rendering": output.rendering,
                "status": output.status,
                "source_unavailable": output.source_unavailable,
                "methods": output.methods,
                "source_counts": output.source_counts,
                "ranked_by": output.ranked_by,
                "bounded_by": output.bounded_by,
                "notes": output.notes,
            }
        )
        if output.document_ids:
            record["document_ids"] = sorted(set(record.get("document_ids") or []) | set(output.document_ids))
        if self.two_halves is not None:
            # M2b-2 per-source coverage (window, day tiers); kept out of the generator payload
            record["operations_coverage"] = {"window": output.window, "latest_available_day": output.latest_available_day, "tiers": (output.history or {}).get("coverage")}
        # M0: how the question was read (default view, question clock, vocabulary) is stated with
        # the numbers, in the deterministic rendering the answer quotes and in the notes
        stated = [*step.notes, *(u.suggestion for u in step.unapplied if u.suggestion)]
        if stated:
            record["notes"] = [*stated, *(record.get("notes") or [])]
            if record.get("rendering"):
                record["rendering"] = record["rendering"].rstrip() + "\n\n" + " ".join(stated)
        return ArmResult(arm="structured", aggregates=[record])

    def _execute(self, ctx: RunContext, state: _Retrieval, steps: list[Any], query: str | None = None) -> PerArm:
        """Run plan steps in parallel; returns the new per-arm candidates and records the round."""
        started = time.perf_counter()
        pools: PerArm = {}
        step_traces: list[dict[str, Any]] = []

        def execute(step):
            start = time.perf_counter()
            try:
                result = self._run_step(ctx, state, step)
            except IncompleteStageError as exc:
                raise ValueError(str(exc)) from exc
            except LayerUnavailableError as exc:
                # unified merge §6.12: an attached layer whose files no longer match its pin fails
                # only the arm that read it (labelled, coverage incomplete); the other arms serve
                result = ArmResult(arm=step.arm, reports=[{"kind": "layer_unavailable", "arm": step.arm, "layer": exc.layer, "reason": str(exc)}], incomplete=True)
            return step, result, time.perf_counter() - start

        with ThreadPoolExecutor(max_workers=state.pipe.config.concurrency) as pool:
            for step, result, seconds in pool.map(execute, steps):
                filters = {**state.request.filters, **(getattr(step, "filters", None) or {})}
                state.incomplete = state.incomplete or result.incomplete
                if not isinstance(step, (StructuredStep, TypedStep)):
                    pools.setdefault(step.arm, [])  # an arm that ran is reported even when it found nothing
                pinned = step.arm == "exact_id" and "exact_id" in state.pipe.config.selection.pins  # selection.pins honours it
                for hit in result.hits:
                    state.hits_by_id.setdefault(hit.document_id, hit)
                    pools.setdefault(step.arm, []).append(
                        Candidate(record_id=hit.document_id, arm=step.arm, score=hit.score, chunk_ids=list(hit.chunk_ids), source=hit.source, provenance=list(hit.provenance), pinned=pinned,
                                  leads=any(p.get("lexical_front") for p in hit.provenance))
                    )
                predicates = [p.model_dump(mode="json", exclude_defaults=True) for p in step.scope.predicates] if isinstance(step, StructuredStep) else []
                for aggregate in result.aggregates:
                    aggregate["id"] = f"aggregate:{len(state.aggregates) + 1}"
                    if isinstance(step, StructuredStep) and step.scope.text is not None:
                        aggregate["text_scope"] = True  # a literal match over every indexed chunk (``_reconcile_counts``)
                    aggregate["filters"] = filters
                    # The predicates actually counted (QA-09): request filters plus the compiled scope.
                    aggregate["predicates"] = predicates
                    if isinstance(step, TypedStep):
                        self._bound_aggregate(aggregate, self._typed_pack_bounds(aggregate))
                    self._bound_aggregate(aggregate, [*(step.unapplied if isinstance(step, (StructuredStep, TypedStep)) else []), *state.filter_check.unsupported])
                    state.aggregates.append(aggregate)
                    if id(step) in state.routed_steps:
                        state.lead_ids.append(aggregate["id"])
                if isinstance(step, StructuredStep):
                    ctx.trace.event(
                        "structured_scope",
                        predicates=[{"field": p.field, "op": p.op, "value": Sensitive(p.model_dump(mode="json", include={"value", "lower", "upper"}))} for p in step.scope.predicates],
                        unapplied=len(step.unapplied),
                    )
                for report in result.reports:
                    if report.get("kind") == "exact_id":
                        for token, record_id in report["matched"].items():
                            state.exact_matched.setdefault(token, record_id)
                        for token in [*report["unknown"], *report.get("not_resolved_over_limit", [])]:
                            if token not in state.exact_unknown:
                                state.exact_unknown.append(token)
                    else:
                        state.reports.append(report)
                step_traces.append(
                    {
                        "arm": step.arm,
                        "query": getattr(step, "query", state.request.query),
                        "seconds": seconds,
                        "filters": filters,
                        **({"predicates": predicates} if isinstance(step, StructuredStep) else {}),
                        "documents": len(result.hits),
                        "aggregate": bool(result.aggregates),
                        "paths": len(result.paths),
                        **({"graph_template": step.graph.template} if getattr(step, "graph", None) is not None else {}),
                        **({"typed": describe_typed(step.draft)} if isinstance(step, TypedStep) and step.draft is not None else {}),
                        **({"sql_route": self._sql_route_trace(result)} if isinstance(step, TypedStep) and step.question is not None else {}),
                        **({"typed_router": True} if id(step) in state.routed_steps else {}),
                        "incomplete": result.incomplete,
                        **({"layer_unavailable": [r["layer"] for r in result.reports if r.get("kind") == "layer_unavailable"]} if any(r.get("kind") == "layer_unavailable" for r in result.reports) else {}),
                    }
                )
        for arm, candidates in pools.items():
            state.raw_pools.setdefault(arm, []).extend(candidates)
        state.exact_unknown = [t for t in state.exact_unknown if t not in state.exact_matched]
        state.rounds.append({"query": query or state.request.query, "steps": step_traces, "seconds": time.perf_counter() - started})
        return pools

    # --- selection ---------------------------------------------------------------------------

    @staticmethod
    def _selection_policy(sel: Any) -> SelectionPolicy:
        return SelectionPolicy(
            collapse=sel.collapse,
            per_arm=sel.per_arm,
            ordering=sel.ordering,
            rrf_k=sel.rrf_k,
            arm_weights=dict(sel.arm_weights),
            source_weights=dict(sel.source_weights),
            max_documents=sel.max_documents,
            diversity_field=sel.diversity.field if sel.diversity else None,
            diversity_min_per_value=sel.diversity.min_per_value if sel.diversity else 1,
            source_order=list(sel.source_order),
            source_slots=list(sel.source_slots),
            pins=bool(sel.pins),
        )

    def _select(self, ctx: RunContext, state: _Retrieval) -> None:
        sel = state.pipe.config.selection
        per_arm = state.raw_pools
        if sel.diversity and sel.diversity.field not in {"source", "kind"}:
            catalog = self.backends[self.config.storage.catalog].catalog
            ids = list(dict.fromkeys(c.record_id for cs in per_arm.values() for c in cs))
            metadata = {d.id: d.metadata for d in catalog.get(ctx.generation, ids)}
            per_arm = {arm: [c.model_copy(update={"metadata": dict(metadata.get(c.record_id, {}))}) for c in cs] for arm, cs in per_arm.items()}
        with ctx.trace.span("selection") as span:
            ordered, pools, trace = select(per_arm, self._selection_policy(sel))
            detail: dict[str, Any] | None = None
            if sel.rerank and ordered:
                # selection/rerank.py: pins stay first; failures follow reranking.on_error (traced)
                ordered, detail = self._rerank_stage().rerank(state.request.query, ordered, lambda rid: self._rerank_passage(state, rid, sel.reranking.passage_chars), sel.reranking, bool(sel.pins))
            span.set(**trace.model_dump(mode="json"), rerank=bool(sel.rerank), **({"rerank_detail": detail} if detail else {}))
        state.ordered, state.pools = ordered, pools
        state.selection_trace = {**trace.model_dump(mode="json"), "rerank": bool(sel.rerank), **({"rerank_detail": detail} if detail else {})}

    def _rerank_stage(self) -> Any:
        if self._reranker is None:
            from .selection.rerank import CrossEncoderScorer, Reranker

            role = self.config.models.roles.reranker
            self._reranker = Reranker(CrossEncoderScorer(role), model=role.model)
        return self._reranker

    @staticmethod
    def _rerank_passage(state: _Retrieval, record_id: str, limit: int) -> str:
        from .selection.rerank import passage_text

        hit = state.hits_by_id[record_id]
        return passage_text(hit.title, hit.text, limit)

    def _hit(self, state: _Retrieval, record_id: str, score: float, arms: list[str], chunk_ids: list[str], provenance: list[dict[str, Any]]) -> Hit:
        base = state.hits_by_id[record_id]
        return Hit(
            document_id=record_id,
            title=base.title,
            source=base.source,
            url=base.url,
            text=base.text,
            chunk_ids=list(chunk_ids),
            score=score,
            arms=list(arms),
            provenance=[dict(p) for p in provenance],
        )

    @staticmethod
    def _exact_unsupported(state: _Retrieval) -> list[UnsupportedConstraint]:
        """Unknown identifiers are reported verbatim, never guessed (plan §3.8). A record_context id
        that did not resolve within the principal's scope is reported the same way."""
        unknown = [UnsupportedConstraint(constraint_text=token, reason="value_not_observed", suggestion=UNKNOWN_ID_SUGGESTION) for token in state.exact_unknown]
        if state.unresolved_record_id is not None:
            unknown.append(UnsupportedConstraint(constraint_text=state.unresolved_record_id, reason="value_not_observed", suggestion=UNRESOLVED_CONTEXT_SUGGESTION))
        return unknown

    def _result(self, ctx: RunContext, state: _Retrieval, started: float, decision_seconds: float) -> SearchResult:
        hits = [self._hit(state, r.record_id, r.score, r.arms, r.chunk_ids, r.provenance) for r in state.ordered]
        arm_hits = {arm: [self._hit(state, c.record_id, c.score, [arm], c.chunk_ids, c.provenance) for c in cs] for arm, cs in state.pools.items()}
        decision = state.decision
        trace: dict[str, Any] = {
            "decision": {
                "kind": state.mode,
                "agent": decision.agent,
                "outcome": decision.outcome,
                "policy_applied": decision.policy_applied,
                "failure": decision.failure,
                "stage": decision.stage,
                "decision_fallback": decision.outcome == "raw_all_arms",
                "dropped_steps": [d.model_dump() for d in decision.dropped_steps],
                "planner_fallback": decision.fallback,
                "intents": decision.intents.kinds(),
                "unsupported": [u.model_dump(mode="json") for u in decision.unsupported],
                "note": decision.note,
                "seconds": decision_seconds,
            },
            "pipeline": state.pipe.name,
            "mode": state.mode,
            "profile": self.config.profile,
            "trace_id": ctx.trace.id,
            "steps": [s for round_ in state.rounds for s in round_["steps"]],
            "selection": state.selection_trace,
            "reasons": {r.record_id: r.reasons for r in state.ordered},
            "seconds": time.perf_counter() - started,
            "model_calls": self._model_calls(ctx),
            "reports": state.reports,
        }
        if state.exact_matched or state.exact_unknown:
            trace["exact_id"] = {"matched": dict(state.exact_matched), "unknown": list(state.exact_unknown)}
        if state.incomplete:
            trace["coverage"] = {"incomplete": True, "stages": ctx.trace.root.attributes.get("incomplete_stages", [])}
        if self.view.partitioned:
            # M1b coverage labels: per member (unit, tier, sources, as-of, status) and per attached layer
            trace["collection"] = self.backends[self.config.storage.catalog].coverage(ctx.generation)
        if state.unresolved_record_id is not None:
            trace["record_context"] = {"record_id": "unresolved", "used": False}
        if state.request.filters:
            trace["request_filters"] = state.filter_check.trace()
        coverage = self._source_coverage(ctx, state)
        if coverage is not None:
            trace["source_coverage"] = coverage
        return SearchResult(
            query=state.request.query,
            generation=ctx.generation.generation,
            hits=hits,
            arm_hits=arm_hits,
            aggregates=state.aggregates,
            unsupported_constraints=self._unsupported(decision.unsupported, state.filter_check.unsupported, self._exact_unsupported(state)),
            trace=trace,
        )

    @staticmethod
    def _model_calls(ctx: RunContext) -> list[dict[str, Any]]:
        calls = []
        for span in ctx.trace.spans:
            for event in span.events:
                if event.get("name") == "model_call":
                    calls.append({k: v for k, v in event.items() if k not in {"name", "at"}})
        return calls

    def _retrieve(
        self, ctx: RunContext, request: SearchRequest, pipe: PipelineRuntime | None = None, mode: str | None = None, emit: Any = None
    ) -> tuple[_Retrieval, SearchResult]:
        started = time.perf_counter()
        self._check_request(request)
        if pipe is None:
            pipe, mode = self._choose(request)
        mode = mode or pipe.default_mode
        request, check = self._check_filters(ctx, request)
        decision_request, capabilities, decision, _, unresolved, routed = self._decide(ctx, request, pipe, mode)
        decision_seconds = time.perf_counter() - started
        halves = getattr(self._halves, "agent", None)
        state = _Retrieval(
            two_halves=halves.last if halves is not None else None,
            request=request,
            decision_request=decision_request,
            capabilities=capabilities,
            decision=decision,
            pipe=pipe,
            mode=mode,
            unresolved_record_id=unresolved,
            filter_check=check,
        )
        self._apply_typed_route(ctx, state, routed)
        self._route_sql(ctx, state)
        decision = state.decision
        steps = self._steps(decision, decision_request)
        if emit is not None:
            emit(
                DecisionEvent(
                    pipeline=pipe.name,
                    mode=mode,
                    agent=decision.agent,
                    outcome=decision.outcome,
                    policy_applied=decision.policy_applied,
                    failure=decision.failure,
                    intents=decision.intents.kinds(),
                    steps=[step.model_dump(mode="json") for step in steps],
                    unsupported_constraints=list(decision.unsupported),
                )
            )
        if steps:
            pools = self._execute(ctx, state, steps)
            self._emit_arms(emit, state, pools)
        self._reconcile_counts(ctx, state)
        self._cover_aggregate_flags(ctx, state)
        self._select(ctx, state)
        return state, self._result(ctx, state, started, decision_seconds)

    @classmethod
    def _reconcile_counts(cls, ctx: RunContext, state: _Retrieval) -> None:
        """Two different counts for the same mention claim never both reach the generator as
        ``exact`` (2026-11-09, corpus-B B4 q037/q044). When an exact SQL-route result matched text
        in the projection's declared text columns (an applied ``LIKE``/``instr``) and an exact
        structured text-scope count (a literal match over every indexed chunk of the record) states
        other numbers, they answer different populations: for a question about mentions ("mention",
        "contain", "about" ...) the text-column result is the answer and the all-chunk count is
        demoted to ``bounded`` (``conflicting_count``); without a mention cue neither is chosen and
        both are demoted. Equal scalar values are no conflict. Idempotent (review hops re-run it)."""
        from .sql_surface.route import _MENTION

        cls._reconcile_text_counts(ctx, state, _MENTION)
        cls._supersede_counts(ctx, state)  # 2026-11-09 (B5 q023)

    @classmethod
    def _reconcile_text_counts(cls, ctx: RunContext, state: _Retrieval, mention_cue: Any) -> None:
        def applied_text(aggregate: dict[str, Any]) -> bool:
            patterns = (aggregate.get("sql_surface") or {}).get("text_patterns") or []
            return any(p.get("filter") in {"positive", "any_of"} for p in patterns)

        sql = [a for a in state.aggregates if a.get("sql_derived") and a.get("exactness") == "exact" and applied_text(a)]
        text = [a for a in state.aggregates if a.get("text_scope") and a.get("exactness") == "exact"]
        if not sql or not text:
            return

        def same(a: dict[str, Any], b: dict[str, Any]) -> bool:
            return isinstance(a.get("value"), (int, float)) and not isinstance(a.get("value"), bool) and a.get("value") == b.get("value")

        conflicts = [(t, s) for t in text for s in sql if not same(t, s)]
        if not conflicts:
            return
        mention = bool(mention_cue.search(state.request.query))
        demoted: list[str] = []
        for structured, chosen in conflicts:
            note = UnsupportedConstraint(
                constraint_text="text count over every indexed chunk",
                reason="conflicting_count",
                suggestion=f"states other numbers than the exact text-column result {chosen['id']}"
                + ("; the question asks about mentions in the declared text columns, so that result answers it" if mention else "; neither population is the question's"),
            )
            cls._bound_aggregate(structured, [note])
            demoted.append(structured["id"])
            if not mention:
                cls._bound_aggregate(
                    chosen,
                    [UnsupportedConstraint(constraint_text="text-column count", reason="conflicting_count", suggestion=f"states other numbers than the text count {structured['id']}; the question names no text column")],
                )
                demoted.append(chosen["id"])
        ctx.trace.event("conflicting_counts", demoted=sorted(set(demoted)), mention=mention)

    @staticmethod
    def _scalar_count(aggregate: dict[str, Any]) -> bool:
        """A record count with one scalar value: a structured/typed ``count`` measure without groups or
        periods, or an executed SQL aggregate whose outermost statement selects one ``COUNT(...)``
        and nothing else (no GROUP BY, one row)."""
        value = aggregate.get("value")
        if aggregate.get("refused") or aggregate.get("intent") or isinstance(value, bool) or not isinstance(value, int):
            return False
        if aggregate.get("sql_surface") is not None:
            if aggregate.get("operation") != "sql_aggregate" or len(aggregate.get("rows") or []) != 1 or len(aggregate.get("columns") or []) != 1:
                return False
            try:
                import sqlglot
                from sqlglot import exp

                tree = sqlglot.parse_one(str(aggregate["sql_surface"].get("sql") or ""), read="sqlite")
            except Exception:  # noqa: BLE001 - no parser or no statement: not recognised as a count
                return False
            if not isinstance(tree, exp.Select) or tree.args.get("group") is not None or len(tree.selects) != 1:
                return False
            return isinstance(tree.selects[0].unalias(), exp.Count)
        measure = aggregate.get("measure") or {}
        return measure.get("kind") == "count" and not aggregate.get("groups") and not aggregate.get("periods")

    @classmethod
    def _supersede_counts(cls, ctx: RunContext, state: _Retrieval) -> None:
        """One exact count answers a count question; other counts do not reach the generator as
        alternatives (2026-11-09, corpus-B B5 q023: a bounded 24 next to the exact 35). When the
        question asks exactly one aggregate kind, ``count``, and every exact scalar count without
        unsupported constraints (``_scalar_count``) states the same number, each other scalar count
        (bounded, approximate, or demoted by ``_reconcile_text_counts``) that states a different
        number is marked ``superseded_by`` that exact record and is left out of the packed evidence,
        the rendered answer and the contract's aggregates; it stays in the result's aggregate list
        and the trace. Two exact counts with different numbers supersede nothing. Idempotent."""
        if state.decision.intents.kinds() != ["count"]:
            return
        counts = [a for a in state.aggregates if cls._scalar_count(a)]
        exact = [a for a in counts if a.get("exactness") == "exact" and not a.get("unsupported") and not a.get("superseded_by")]
        if len({a["value"] for a in exact}) != 1:
            return
        chosen = exact[0]
        superseded = []
        for aggregate in counts:
            if aggregate in exact or aggregate.get("superseded_by") or aggregate.get("value") == chosen["value"]:
                continue
            aggregate["superseded_by"] = chosen["id"]
            superseded.append(aggregate["id"])
        if superseded:
            ctx.trace.event("counts_superseded", superseded=superseded, by=chosen["id"])

    @staticmethod
    def _cover_aggregate_flags(ctx: RunContext, state: _Retrieval) -> None:
        """Drop the decision's per-intent ``aggregate_intent_without_scope`` flags once an executed
        step of the final plan produced an exact aggregate (2026-11-08 parity fix): a failed typed
        route flags every aggregate intent, but when the planner's own structured (or a typed) step
        executed exactly, the intent has its exact scope. "Covered" is the contract's rule 3
        condition (an exact aggregate without dropped predicates exists); a flag of an untargeted
        list noun (QQ-04) and every other constraint stay."""
        decision = state.decision
        texts = {a.text for a in decision.intents.aggregates}
        flagged = [u for u in decision.unsupported if u.reason == "aggregate_intent_without_scope" and u.constraint_text in texts]
        if not flagged:
            return
        if not any(a.get("exactness") == "exact" and not a.get("refused") and not a.get("unsupported") for a in state.aggregates):
            return
        state.decision = decision.model_copy(update={"unsupported": [u for u in decision.unsupported if u not in flagged]})
        ctx.trace.event("aggregate_flags_covered", count=len(flagged))

    @staticmethod
    def _emit_arms(emit: Any, state: _Retrieval, pools: PerArm) -> None:
        if emit is None:
            return
        round_ = len(state.rounds)
        aggregates = sum(1 for s in state.rounds[-1]["steps"] if s["aggregate"]) if state.rounds else 0
        for arm, candidates in pools.items():
            emit(ArmResultEvent(round=round_, arm=arm, record_ids=list(dict.fromkeys(c.record_id for c in candidates))))
        if aggregates and "structured" not in pools:
            emit(ArmResultEvent(round=round_, arm="structured", aggregates=aggregates))

    def search(self, request: SearchRequest | str, principal: Principal | None = None, session_id: str | None = None) -> SearchResult:
        if isinstance(request, str):
            request = SearchRequest(query=request)
        pipe, mode = self._choose(request)
        ctx, _pinned, session, info, request = self._begin(principal, session_id, request)
        result: SearchResult | None = None
        try:
            result = self._retrieve(ctx, request, pipe, mode)[1]
        finally:
            shown = [h.document_id for h in result.hits] if result is not None else []
            self._finish(ctx, session, info, request.query if result is not None else None, shown)
        if info is not None:
            result.trace["session"] = info
        return result

    def search_page(self, request: Any, principal: Principal | None = None, session_id: str | None = None) -> Any:
        """``search`` plus page/sort/facets/multi-value filters over the retrieved candidates
        (``runtime/listing.py``; additive, ``search`` is unchanged)."""
        from .runtime import listing

        try:
            listing.check(self.config, request)
        except listing.RequestError as exc:
            raise RequestRejected(str(exc)) from exc
        search = request.request
        pipe, mode = self._choose(search)
        ctx, _pinned, session, info, search = self._begin(principal, session_id, search)
        page = None
        try:
            run_ctx, applied, unsupported = listing.narrowed(ctx, self.config.metadata_schema, self.records, request)
            result = self._retrieve(run_ctx, search, pipe, mode)[1]
            page = listing.build_page(self, run_ctx, request, result, applied, unsupported)
        finally:
            shown = [h.document_id for h in page.items] if page is not None else []
            self._finish(ctx, session, info, search.query if page is not None else None, shown)
        if info is not None:
            page.trace["session"] = info
        return page

    # --- evidence packing --------------------------------------------------------------------

    def _field_priority(self, pipe: PipelineRuntime | None = None) -> list[str]:
        cfg = self.config
        generation = (pipe or self.default).config.generation
        packing = (pipe or self.default).config.selection.packing
        return list(dict.fromkeys([*packing.field_priority, *generation.evidence_fields, *cfg.ingestion.chunking.field_order]))

    @staticmethod
    def _typed_pack_bounds(aggregate: dict[str, Any]) -> list[UnsupportedConstraint]:
        """A typed result with more rows/groups than the generator payload carries is ``bounded``."""
        out = []
        for key, cap in (("rows", TYPED_PACK_ROWS), ("groups", TYPED_PACK_GROUPS)):
            if len(aggregate.get(key) or []) > cap:
                out.append(UnsupportedConstraint(constraint_text=f"typed {key}", reason="result_truncated", suggestion=f"only the first {cap} of {len(aggregate[key])} {key} reach the answer evidence"))
        return out

    @staticmethod
    def _typed_extra(ctx: RunContext, aggregate: dict[str, Any]) -> dict[str, Any]:
        """A typed result as packed evidence (M6): bounded rows (each value cited as (document id,
        field) in the stated generation) or bounded groups, the typed fingerprint and the
        aggregate-level contributor lineage. The full result stays on ``SearchResult.aggregates``."""
        skip = {"document_ids", "document_ids_truncated", "groups", "rows", "value", "typed_query"}
        payload = {k: v for k, v in aggregate.items() if k not in skip}
        typed = dict(aggregate.get("typed_query") or {})
        payload["arm"] = "typed_query"
        payload["typed_query"] = {k: typed.get(k) for k in ("fingerprint", "grain", "join", "fanout", "acl_applied")}
        rows = aggregate.get("rows")
        groups = aggregate.get("groups")
        if rows is not None:
            payload["rows"] = [
                {"document_id": r["document_id"], "values": r["values"], "nulls": r["nulls"], "rejected": r["rejected"], "cited": [[c["document_id"], c["field"]] for c in r["citations"]]}
                for r in rows[:TYPED_PACK_ROWS]
            ]
            payload["value"] = aggregate.get("value")
            payload["shown"] = f"showing {min(len(rows), TYPED_PACK_ROWS)} of {aggregate.get('total')}"
        elif groups:
            payload["groups"] = [{k: v for k, v in g.items() if v is not None} for g in groups[:TYPED_PACK_GROUPS]]
            payload["shown"] = f"showing {min(len(groups), TYPED_PACK_GROUPS)} of {len(groups)} groups"
        else:
            payload["value"] = aggregate.get("value")
        ids = list(dict.fromkeys(aggregate.get("document_ids", [])))
        upstream = bool(aggregate.get("document_ids_truncated", False))
        payload["contributor_lineage"] = {
            "scope": "rows_cited" if rows is not None else "aggregate_level_only",
            "citation_form": "(document_id, field, generation)",
            "document_ids": ids[:8],
            "retained_count": min(len(ids), 8),
            "total_matched_records": aggregate.get("total"),
            "upstream_truncated": upstream,
            "local_cap_truncated": len(ids) > 8,
            "truncated": upstream or len(ids) > 8,
            "generation": ctx.generation.model_dump(mode="json"),
        }
        return payload

    @staticmethod
    def _sql_route_trace(result: ArmResult) -> dict[str, Any]:
        lineage = (result.aggregates[0].get("sql_surface") or {}) if result.aggregates else {}
        return {"fingerprint": lineage.get("fingerprint"), "tables": lineage.get("tables"), "status": "refused" if result.aggregates and result.aggregates[0].get("refused") else "executed"}

    @staticmethod
    def _sql_extra(ctx: RunContext, aggregate: dict[str, Any]) -> dict[str, Any]:
        """A SQL-route result as packed evidence, symmetric with ``_typed_extra``: bounded rows (each
        value cited as (document id, table.field) in the stated generation) or bounded result rows of
        a derived (aggregate) statement, plus the executed parameterised SQL (ACL values masked) and
        its fingerprint. The full result stays on ``SearchResult.aggregates``."""
        skip = {"document_ids", "document_ids_truncated", "rows", "value", "sql_surface"}
        payload = {k: v for k, v in aggregate.items() if k not in skip}
        lineage = dict(aggregate.get("sql_surface") or {})
        payload["arm"] = "typed_query"
        payload["sql_surface"] = {k: lineage.get(k) for k in ("fingerprint", "sql", "params", "tables", "acl_applied", "status", "error") if k in lineage}
        rows = aggregate.get("rows") or []
        cited = aggregate.get("operation") == "sql_rows"
        if cited:
            payload["rows"] = [
                {"document_id": r.get("document_id"), "values": r["values"], "nulls": r.get("nulls", []), "cited": [[c["document_id"], f"{c['table']}.{c['field']}"] for c in r.get("citations", [])]}
                for r in rows[:TYPED_PACK_ROWS]
            ]
        else:
            # small groups carry their record ids (B7, ``sql_surface.group_lineage_max``)
            payload["rows"] = [
                {"values": r["values"], **({"tie": True} if r.get("tie") else {}), **({"contributor_ids": r["contributor_ids"]} if r.get("contributor_ids") else {})}
                for r in rows[:TYPED_PACK_ROWS]
            ]
        payload["value"] = aggregate.get("value")
        payload["shown"] = f"showing {min(len(rows), TYPED_PACK_ROWS)} of {len(rows)} result rows"
        ids = list(dict.fromkeys(aggregate.get("document_ids", [])))
        contributors = aggregate.get("contributors")
        upstream = bool(aggregate.get("document_ids_truncated", False))
        # a SQL aggregate's ids are capped by the executor (8, or up to group_lineage_max; B7)
        cap = max(8, len(ids)) if aggregate.get("operation") == "sql_aggregate" else 8
        scope = "aggregate_contributors" if contributors is not None else ("small_group_contributors" if aggregate.get("lineage") == "groups" else "aggregate_level_only")
        payload["contributor_lineage"] = {
            "scope": "rows_cited" if cited else scope,
            "citation_form": "(document_id, table.field, generation)",
            "document_ids": ids[:cap],
            "retained_count": min(len(ids), cap),
            "total_matched_records": contributors if contributors is not None else aggregate.get("total"),
            "upstream_truncated": upstream,
            "local_cap_truncated": len(ids) > cap,
            "truncated": upstream or len(ids) > cap,
            "generation": ctx.generation.model_dump(mode="json"),
        }
        return payload

    @staticmethod
    def _primary_exact(aggregates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        """Exact before bounded/approximate (2026-11-09, corpus-B B7 q038): the aggregates in a
        stable order with every exact, unrefused result without unsupported constraints first, and
        a ``precedence`` entry per aggregate when both kinds are present: an exact result is
        ``primary``; a bounded or approximate one is ``secondary`` with the exact ids it yields to
        when both answer the same question. Nothing is dropped or relabelled here."""

        def exact(aggregate: dict[str, Any]) -> bool:
            return aggregate.get("exactness") == "exact" and not aggregate.get("refused") and not aggregate.get("unsupported")

        primary = [a["id"] for a in aggregates if exact(a)]
        ordered = sorted(aggregates, key=lambda a: not exact(a))
        if not primary or len(primary) == len(aggregates):
            return ordered, {}
        notes: dict[str, dict[str, Any]] = {}
        for aggregate in ordered:
            if exact(aggregate):
                notes[aggregate["id"]] = {"rank": "primary", "note": "exact result: when a bounded or approximate result answers the same question, this one is the answer"}
            else:
                notes[aggregate["id"]] = {"rank": "secondary", "exact_ids": primary, "note": "bounded or approximate: when an exact result answers the same question, answer from the exact one"}
        return ordered, notes

    def _pack_extras(self, ctx: RunContext, aggregates: list[dict[str, Any]]) -> list[PackExtra]:
        """Bounded aggregate-level lineage, never a claim of per-group contributors. Exact results
        are packed first and carry a ``precedence`` entry next to bounded ones (``_primary_exact``)."""
        extras = []
        aggregates, precedence = self._primary_exact(aggregates)
        for aggregate in aggregates:
            special: dict[str, Any] | None = None
            if aggregate.get("typed_query") is not None:
                special = self._typed_extra(ctx, aggregate)
            elif aggregate.get("sql_surface") is not None:
                special = self._sql_extra(ctx, aggregate)
            if special is not None:
                if aggregate["id"] in precedence:
                    special["precedence"] = precedence[aggregate["id"]]
                extras.append(PackExtra(id=aggregate["id"], source="structured", passage=json.dumps(special, default=str)))
                continue
            ids = list(dict.fromkeys(aggregate.get("document_ids", [])))
            upstream = bool(aggregate.get("document_ids_truncated", False))
            capped = len(ids) > 8
            payload = {k: v for k, v in aggregate.items() if k not in {"document_ids", "document_ids_truncated", "groups", "operations_coverage"}}
            payload["contributor_lineage"] = {
                "scope": "aggregate_level_only",
                "document_ids": ids[:8],
                "retained_count": min(len(ids), 8),
                "total_matched_records": aggregate.get("total"),
                "upstream_truncated": upstream,
                "local_cap_truncated": capped,
                "truncated": upstream or capped,
                "generation": ctx.generation.model_dump(mode="json"),
            }
            if aggregate["id"] in precedence:
                payload["precedence"] = precedence[aggregate["id"]]
            extras.append(PackExtra(id=aggregate["id"], source="structured", passage=json.dumps(payload)))
        return extras

    def _pack(self, ctx: RunContext, state: _Retrieval) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        pipe = state.pipe or self.default
        packing = pipe.config.selection.packing
        generator = self.config.models.roles.generator
        budget = generator.evidence_budget.chars if generator and generator.evidence_budget else packing.budget_chars
        if state.request.evidence_chars is not None:  # checked against request.evidence_chars_max in _check_request
            budget = state.request.evidence_chars
        config = packing.model_copy(update={"budget_chars": budget, "field_priority": self._field_priority(pipe)})
        catalog = self.backends[self.config.storage.catalog].catalog
        ids = [r.record_id for r in state.ordered]
        documents = {d.id: d for d in catalog.get(ctx.generation, ids)} if ids else {}
        records = []
        for selected in state.ordered:
            doc = documents.get(selected.record_id)
            if doc is None:
                continue
            records.append(
                PackRecord(
                    id=doc.id,
                    title=doc.title,
                    source=doc.source,
                    url=doc.url,
                    fields=dict(doc.fields),
                    fallback_text=state.hits_by_id[doc.id].text,
                    chunk_ids=list(selected.chunk_ids),
                    metadata=dict(doc.metadata),
                )
            )
        # a count superseded by the exact count of the same question never reaches the generator
        extras = self._pack_extras(ctx, [a for a in state.aggregates if not a.get("superseded_by")])
        # typed-router evidence (``routing: router``) is packed ahead of everything else
        leading = [e for e in extras if e.id in state.lead_ids]
        extras = [e for e in extras if e.id not in state.lead_ids]
        result = pack(records, config, extras, query=state.request.query, leading=leading)
        evidence = [e.context_dict() for e in result.evidence]
        trace = {
            "omitted_ids": result.omitted_ids,
            "truncated_ids": result.truncated_ids,
            "passage_characters": result.passage_characters,
            "fairness": result.fairness,
            "field_priority": result.field_priority,
            "budget_chars": budget,
            "aggregate_reserve_chars": result.aggregate_reserve_chars,
            "passage_recipe": result.passage_recipe,
            "windowed_ids": result.windowed_ids,
            "metadata_omitted_ids": result.metadata_omitted_ids,
            "metadata_dropped_keys": result.metadata_dropped_keys,
            "metadata_protected_over_cap_ids": result.metadata_protected_over_cap_ids,
            "metadata_protected_overflow": result.metadata_protected_overflow,
            "metadata_characters": result.metadata_characters,
            "evidence_characters": result.evidence_characters,
            "leading_ids": result.leading_ids,
        }
        ctx.trace.event("packing", **{k: v for k, v in trace.items() if k != "field_priority"}, records=len(records), extras=len(extras))
        state.pack_trace = trace
        return evidence, trace

    def _evidence(self, ctx: RunContext, result: SearchResult) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Pack the evidence for an existing ``SearchResult`` (Python-side helper; ``answer`` packs inline)."""
        decision_request = DecisionRequest(query=result.query)
        state = _Retrieval(
            request=SearchRequest(query=result.query),
            decision_request=decision_request,
            capabilities=self.capabilities(),
            decision=Decision(outcome="abstain", agent="none", policy_applied="none"),
            pipe=self.default,
            mode=self.default.default_mode,
            hits_by_id={h.document_id: h for h in result.hits},
            ordered=[Selected(record_id=h.document_id, score=h.score, arms=list(h.arms), chunk_ids=list(h.chunk_ids), source=h.source) for h in result.hits],
            aggregates=list(result.aggregates),
        )
        return self._pack(ctx, state)

    # --- answers -----------------------------------------------------------------------------

    def _coverage(self, manifest: dict[str, Any]) -> Coverage | None:
        coverage = manifest.get("coverage") or {}
        policy = coverage.get("policy", "strict")
        stages = coverage.get("stages", {})
        incomplete = sorted(n for n, s in stages.items() if s.get("status") == "incomplete")
        extraction = coverage.get("extraction", {})
        without = extraction.get("documents_without_graph", [])
        rejected = extraction.get("rejected_by_category", {})
        if policy == "strict" and not incomplete and not without and manifest.get("status") != "partial":
            return None
        total = int(manifest.get("documents", 0))
        notes = []
        if without:
            notes.append(f"{len(without)} document(s) have no graph/community outputs (extraction rejected)")
        for name in incomplete:
            stage = stages[name]
            notes.append(f"stage {name} incomplete: processed {stage.get('processed', 0)}, remaining {stage.get('remaining', 0)}")
        return Coverage(
            policy=policy,
            documents_total=total,
            documents_covered=max(0, total - len(without)),
            note="; ".join(notes) or None,
            incomplete_stages=incomplete,
            rejected_by_category=dict(rejected),
        )

    def _generate(self, ctx: RunContext, state: _Retrieval, evidence: list[dict[str, Any]], on_delta: Any = None, operations: Any = None) -> Answer:
        """``operations``: the rendered operations half (``generation/operations.py``; staging round 5,
        N14): sent as ``operations_shown`` with ``OPERATIONS_RULE``; its aggregates are not in
        ``evidence``."""
        decision = state.decision
        if decision.outcome == "abstain":
            reason = decision.failure or "no_plan"
            if reason == "schema_violation":
                # the log-specific hint only where log intents are configured (2026-11-09 cutover:
                # a document deployment told users "the logs have not been checked")
                if self.config.logs is not None and self.config.logs.intents.enabled:
                    return Answer(answer="I could not form a valid search plan for this question. The logs have not been checked. Try naming the service or tenant and a date range.", insufficient_evidence=True)
                return Answer(answer="I could not form a valid search plan for this question, so nothing was searched. Try restating it as a complete question (name the item, group or period you mean).", insufficient_evidence=True)
            return Answer(answer=f"The request could not be planned ({reason}); see unsupported_constraints.", insufficient_evidence=True)
        if decision.policy_applied in {"log_outage_investigation", "log_shared_errors"}:
            if decision.policy_applied == "log_shared_errors":
                from .runtime.log_shared_errors import answer
            else:
                from .runtime.log_investigation import answer
            return answer(self, ctx, evidence, state.reports)
        generator = self.models.get("generator")
        if generator is None:
            return Answer(answer="Answer generation is disabled. Inspect retrieved passages and aggregates.", insufficient_evidence=True)
        if not evidence:
            return Answer(answer="No matching evidence was retrieved.", insufficient_evidence=True)
        cfg = state.pipe.config.generation
        prompt = cfg.prompt or GENERATOR_PROMPT
        voice = voice_instruction(cfg.voice)
        if voice:
            prompt = prompt + "\n" + voice
        role = self.config.models.roles.generator
        bridge = RunBudgetMeter(ctx, "generator", role.price_key())
        # Memory never reaches the generator: the payload is the question, the caller context and
        # the evidence packed for this turn only.
        payload = {"question": state.request.query, "record_context": self._record_context(state), "evidence": evidence}
        if operations is not None:
            payload["operations_shown"] = operations.text(cited=False)
            prompt = prompt + "\n" + OPERATIONS_RULE
        schema: Any = Answer
        if generator.enforces_schema():
            # QA-08: where the provider enforces the schema, cited ids are restricted to the supplied
            # evidence and non-narrative claim kinds to requests that carry aggregate evidence.
            has_aggregate = any(row.get("source") == "structured" for row in evidence)
            kinds = CLAIM_KINDS if has_aggregate else ("narrative",)
            schema = answer_schema([row["id"] for row in evidence], kinds)
            ctx.trace.event("generator_schema", evidence_ids=len(evidence), claim_kinds=list(kinds))
        try:
            if on_delta is not None and generator.streams():
                reply = generator.call_stream(prompt, payload, schema, bridge, "generator", on_delta)
            elif role is not None and role.measure_first_token and generator.streams():
                # streamed only to time the first reply token (``first_token_seconds``); no deltas leave
                reply = generator.call_stream(prompt, payload, schema, bridge, "generator", lambda _text: None)
            else:
                reply = generator.call(prompt, payload, schema, bridge, "generator")
        except SchemaViolation as error:
            # Real-run fix: the stream used to end with a bare "error: schema_violation" and nothing
            # logged. The cause is logged (field paths and error types, the provider's stop reason;
            # never the reply or the evidence) and the run returns its evidence with an abstain.
            details = {"stage": error.stage or "generator", "errors": error.error_count, "paths": error.paths, "stop_reason": error.stop_reason, "model": role.model if role else None}
            LOGGER.warning("generator reply violated the answer schema: %s", json.dumps(details, sort_keys=True))
            ctx.trace.event("generator_schema_violation", **details)
            repaired = self._repair_generator_reply(ctx, cfg.schema_repair_attempts, generator, role, prompt, payload, schema, error)
            if repaired is None:
                reason = "the reply was cut off" if error.stop_reason in {"length", "max_tokens"} else "the reply did not match the answer schema"
                return Answer(
                    answer=f"No answer was generated ({reason}: schema_violation); the retrieved evidence is returned for inspection.",
                    insufficient_evidence=True,
                )
            reply = repaired
        return reply if isinstance(reply, Answer) else Answer.model_validate(reply.model_dump(mode="json"))

    def _repair_generator_reply(self, ctx: RunContext, attempts: int, generator: Any, role: Any, prompt: str, payload: dict[str, Any], schema: Any, error: SchemaViolation) -> Any:
        """The generator's one schema repair (staging round 6, N15;
        ``pipeline.generation.schema_repair_attempts``), or ``None`` when there is none or it failed.

        One more call to the same role, not streamed (deltas of the failed reply may already have
        been sent; the validated reply is the result, as for the M0 retries): the same payload plus
        ``schema_repair`` (the previous reply and its validation paths) and
        ``GENERATOR_REPAIR_PROMPT``. It is metered like any call (it reserves on ``max_llm_calls``,
        ``max_usd``, the deadline and the ``serving.answers`` ledger; its ``model_call`` record says
        ``attempt: 2``, ``retry_reason: schema_violation``) and traced as ``generator_schema_repair``.
        A repair the budget or deadline refuses, a provider failure and a second violation leave the
        original schema-violation answer standing; nothing else is retried."""
        from .core.errors import DeadlineExceeded
        from .profiles.prompts import GENERATOR_REPAIR_PROMPT
        from .providers.base import ProviderError

        if not attempts:
            return None
        if len(error.raw) > 64000:
            ctx.trace.event("generator_schema_repair", attempt=1, status="skipped", reason="previous reply over 64000 characters")
            return None
        repair = {**payload, "schema_repair": {"previous_reply": error.raw, "validation_paths": error.paths}}
        meter = RetryBudgetMeter(ctx, "generator", role.price_key() if role is not None else None, attempt=2, reason="schema_violation")
        ctx.trace.event("generator_schema_repair", attempt=1, status="started")
        try:
            reply = generator.call(prompt + GENERATOR_REPAIR_PROMPT, repair, schema, meter, "generator")
        except SchemaViolation as again:
            ctx.trace.event("generator_schema_repair", attempt=1, status="failed", reason="schema_violation", errors=again.error_count, paths=again.paths, stop_reason=again.stop_reason)
            LOGGER.warning("generator schema repair failed: %s", json.dumps({"errors": again.error_count, "paths": again.paths, "stop_reason": again.stop_reason}, sort_keys=True))
            return None
        except (BudgetExhausted, DeadlineExceeded) as refused:
            ctx.trace.event("generator_schema_repair", attempt=1, status="failed", reason=getattr(refused, "category", type(refused).__name__), detail=str(refused)[:300])
            return None
        except ProviderError as failed:
            ctx.trace.event("generator_schema_repair", attempt=1, status="failed", reason=getattr(failed, "category", type(failed).__name__))
            LOGGER.warning("generator schema repair call failed: %s", getattr(failed, "category", type(failed).__name__))
            return None
        ctx.trace.event("generator_schema_repair", attempt=1, status="schema_valid")
        return reply

    def _rendered_aggregates(self, state: _Retrieval, evidence: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        """The aggregates whose deterministic renderings are the answer text (fix5), or ``None`` for
        the generator path. A pure ranking/count plan: the plan ran, every aggregate has a rendering
        and no record passage reached the evidence; and either every aggregate is a typed log
        intent, no generator is configured, or no aggregate fit the evidence budget (the generator
        would see nothing). Never "No matching evidence" while an aggregate exists."""
        live = [a for a in state.aggregates if not a.get("superseded_by")]  # ``_supersede_counts``
        if state.decision.outcome == "abstain" or not live:
            return None
        if not all(a.get("rendering") for a in live):
            return None
        if any(row.get("source") != "structured" for row in evidence):
            return None
        packed = {row["id"] for row in evidence}
        if self.models.get("generator") is None or not packed or all(a.get("intent") for a in live):
            return self._primary_exact(live)[0]  # an exact rendering leads a bounded one (B7)
        return None

    def _narrative(self, ctx: RunContext, state: _Retrieval, aggregates: list[AggregateEvidence]) -> tuple[str | None, str]:
        """The generator's optional one-sentence summary of a rendered answer: ``(text, status)``.
        Skipped (and traced) when the run budget refuses the call or the reply violates the schema;
        the rendered answer stands either way. Other provider errors propagate as on the generator
        path."""
        if state.aggregates and all("signature_id" in (a.get("args", {}).get("group_by") or []) for a in state.aggregates):
            return None, "none: deterministic client issue table" if all(a.get("args", {}).get("by") == "tenant_signature" for a in state.aggregates) else "none: deterministic service issue table"
        generator = self.models.get("generator")
        if generator is None:
            return None, "none: no generator configured"
        role = self.config.models.roles.generator
        bridge = RunBudgetMeter(ctx, "generator", role.price_key())
        evidence = [{"id": a.id, "source": "structured", "passage": labelled(a)} for a in aggregates]
        payload = {"question": state.request.query, "evidence": evidence}
        schema: Any = answer_schema([a.id for a in aggregates], ("narrative",)) if generator.enforces_schema() else Answer
        try:
            reply = generator.call(AGGREGATE_NARRATIVE_PROMPT, payload, schema, bridge, "generator")
        except BudgetExhausted as exc:
            ctx.trace.event("aggregate_narrative", status="skipped", reason="budget", detail=str(exc))
            return None, "skipped: budget"
        except SchemaViolation as error:
            details = {"errors": error.error_count, "paths": error.paths, "stop_reason": error.stop_reason, "model": role.model if role else None}
            LOGGER.warning("aggregate narrative reply violated the answer schema: %s", json.dumps(details, sort_keys=True))
            ctx.trace.event("aggregate_narrative", status="skipped", reason="schema_violation", **details)
            return None, "skipped: schema_violation"
        answer = reply if isinstance(reply, Answer) else Answer.model_validate(reply.model_dump(mode="json"))
        if answer.insufficient_evidence or not answer.answer.strip():
            ctx.trace.event("aggregate_narrative", status="none", reason="the generator declined")
            return None, "none: the generator declined"
        return answer.answer, "proposed"

    def answer(self, request: SearchRequest | str, principal: Principal | None = None, session_id: str | None = None) -> AnswerResult:
        if isinstance(request, str):
            request = SearchRequest(query=request)
        pipe, mode = self._choose(request)
        started = time.perf_counter()
        ctx, pinned, session, info, request = self._begin(principal, session_id, request)
        return self._answer_turn(ctx, pinned, session, info, request, pipe, mode, started)

    def answer_stream(self, request: SearchRequest | str, principal: Principal | None = None, session_id: str | None = None) -> AnswerStream:
        """``answer`` as a stream of typed events (``runtime/events.py``). Request, pipeline/mode and
        session problems raise here, before any event; failures during the run end the stream with
        an ``error`` event. The ``done`` event carries the same ``AnswerResult`` as ``answer``."""
        if isinstance(request, str):
            request = SearchRequest(query=request)
        pipe, mode = self._choose(request)
        self._check_request(request)
        started = time.perf_counter()
        ctx, pinned, session, info, request = self._begin(principal, session_id, request)

        def run(emit: Any, on_delta: Any) -> AnswerResult:
            return self._answer_turn(ctx, pinned, session, info, request, pipe, mode, started, emit=emit, on_delta=on_delta)

        return AnswerStream(run, cancel_budget=ctx.budget.cancel, usage=ctx.budget.snapshot)

    def answer_from_evidence(
        self, request: SearchRequest | str, evidence_ids: list[str], principal: Principal | None = None, session_id: str | None = None, provenance: dict[str, Any] | None = None
    ) -> AnswerResult:
        """Answer from caller-named evidence records (re-read by id within the access scope; no
        retrieval; answer contract as usual). See ``runtime/evidence_answer.py``; additive."""
        from .runtime.evidence_answer import answer_from_evidence

        return answer_from_evidence(self, request, evidence_ids, principal, session_id, provenance)

    def _answer_turn(
        self,
        ctx: RunContext,
        pinned: Any,
        session: Session | None,
        info: dict[str, Any] | None,
        request: SearchRequest,
        pipe: PipelineRuntime,
        mode: str,
        started: float,
        emit: Any = None,
        on_delta: Any = None,
    ) -> AnswerResult:
        outcome: AnswerResult | None = None
        try:
            outcome = self._answer(ctx, pinned, request, pipe, mode, started, emit=emit, on_delta=on_delta)
        finally:
            shown = [h.document_id for h in outcome.retrieval.hits] if outcome is not None else []
            self._finish(ctx, session, info, request.query if outcome is not None else None, shown, ctx.trace.id if outcome is not None else None)
        if info is not None:
            outcome.trace["session"] = info
        return outcome

    def _answer(
        self, ctx: RunContext, pinned: Any, request: SearchRequest, pipe: PipelineRuntime, mode: str, started: float, emit: Any = None, on_delta: Any = None
    ) -> AnswerResult:
        state, result = self._retrieve(ctx, request, pipe, mode, emit=emit)
        evidence, evidence_trace = self._pack(ctx, state)
        stop_reason = "single_pass"
        review_trace: dict[str, Any] | None = None
        review = pipe.reviews.get(mode)
        if review is not None and state.decision.outcome != "abstain":

            def execute(decision: Decision) -> PerArm:
                steps = list(decision.plan.steps) if decision.plan is not None else []
                # A re-planned hop may legally start with a structured step (no query); the round is
                # labelled with its first retrieval step's query, else the request query.
                hop_query = next((s.query for s in steps if isinstance(s, RetrievalStep)), None)
                pools = self._execute(ctx, state, steps, query=hop_query) if steps else {}
                self._emit_arms(emit, state, pools)
                return pools

            def repack(pools: PerArm) -> list[dict[str, Any]]:
                self._reconcile_counts(ctx, state)
                self._select(ctx, state)
                return self._pack(ctx, state)[0]

            outcome = review.run(
                ctx,
                ReviewInput(request=state.decision_request, capabilities=state.capabilities, pools=state.pools, evidence=evidence, execute=execute, repack=repack),
            )
            evidence, evidence_trace = outcome.evidence, state.pack_trace
            stop_reason = outcome.stop_reason
            review_trace = {"rounds": [r.model_dump(mode="json") for r in outcome.rounds], "hops_executed": outcome.hops_executed, "stop_reason": outcome.stop_reason}
            result = self._result(ctx, state, started, result.trace["decision"]["seconds"])
            if emit is not None:
                for round_ in review_trace["rounds"]:
                    emit(ReviewRoundEvent(round=round_))
        if emit is not None:
            emit(EvidenceEvent(ids=[row["id"] for row in evidence], items=[{k: row.get(k) for k in ("id", "title", "source", "url")} for row in evidence], omitted_ids=list(evidence_trace.get("omitted_ids", [])), truncated_ids=list(evidence_trace.get("truncated_ids", []))))
        manifest = pinned[1]
        self._cover_aggregate_flags(ctx, state)  # review hops may have added an exact aggregate
        rendered = self._rendered_aggregates(state, evidence)
        rendered_trace: dict[str, Any] | None = None
        if rendered is not None:
            # A pure ranking/count (fix5): the aggregates' renderings are the answer text, with
            # their exactness labels; the generator may add one sentence restating their numbers.
            aggregates = [AggregateEvidence(id=a["id"], result=scope_result_of(a), rendering=a["rendering"], rows=a.get("rows") or [], columns=a.get("columns") or [], scalar_count=self._scalar_count(a)) for a in rendered]
            refused = all(a.get("status") == "refused" for a in rendered)
            narrative, status = (None, "none: refused") if refused else self._narrative(ctx, state, aggregates)
            checked = pipe.contract.rendered_answer(
                aggregates,
                intents=state.decision.intents,
                narrative=narrative,
                refused=refused,
                unsupported=self._unsupported(state.decision.unsupported, state.filter_check.unsupported),
                coverage=self._coverage(manifest),
                trace=ctx.trace,
            )
            if narrative is not None:
                status = "kept" if checked.answer.short_answer else "dropped"
            rendered_trace = {"aggregates": [a.id for a in aggregates], "narrative": status}
        else:
            # N14: the operations half is rendered, never written by the model; the generator
            # answers from the other evidence (``generation/operations.py``)
            section = self._operations_section(state)
            shown = evidence if section is None else [row for row in evidence if row["id"] not in section.ids]
            answer = self._generate(ctx, state, shown, on_delta=on_delta, operations=section)
            operations_violations: list[Any] = []
            if section is not None:
                answer, operations_violations = section.check(answer)
                if operations_violations and pipe.contract.on_violation == "fail":
                    from .generation import AnswerContractViolation

                    raise AnswerContractViolation(operations_violations)
            evidence_ids = {row["id"] for row in shown}
            # rows/columns too (2026-11-09 F1): the numbers of a SQL/typed result are its row cells
            aggregates = [
                AggregateEvidence(id=a["id"], result=scope_result_of(a), rendering=a.get("rendering"), rows=a.get("rows") or [], columns=a.get("columns") or [], scalar_count=self._scalar_count(a))
                for a in state.aggregates
                if a["id"] in evidence_ids
            ]
            unsupported = self._unsupported(state.decision.unsupported, state.filter_check.unsupported)
            if section is not None and not answer.answer.strip():
                # nothing of the model's text is left: the rendered operations half stands alone
                from .generation import AnswerResult as CheckedAnswer

                flagged = pipe.contract._flagged(aggregates, state.decision.intents, unsupported)
                checked = CheckedAnswer(answer=Answer(answer="", insufficient_evidence=True), unsupported_constraints=flagged, coverage=self._coverage(manifest))
            else:
                checked = pipe.contract.enforce(
                    answer,
                    evidence_ids=evidence_ids | (section.ids if section is not None else set()),
                    aggregates=aggregates,
                    intents=state.decision.intents,
                    unsupported=unsupported,
                    coverage=self._coverage(manifest),
                    trace=ctx.trace,
                    question=state.request.query,
                    targets=state.capabilities.structured_targets(),
                    passages=[row.get("passage", "") for row in shown if row.get("source") != "structured"],
                )
            if section is not None:
                checked = self._with_operations(ctx, state, checked, section, operations_violations, pipe.contract.on_violation)
        trace: dict[str, Any] = {
            "seconds": time.perf_counter() - started,
            "rounds": state.rounds,
            "stop_reason": stop_reason,
            "review": review_trace,
            "decision": result.trace["decision"],
            "pipeline": pipe.name,
            "mode": mode,
            "selection": result.trace["selection"],
            "model_calls": self._model_calls(ctx),
            "evidence": evidence_trace,
            "contract": {"status": checked.contract, "violations": [v.model_dump(mode="json") for v in checked.violations], **({"limitations": list(checked.limitations)} if checked.limitations else {})},
            "trace_id": ctx.trace.id,
            "profile": self.config.profile,
        }
        latency = self._generator_latency(trace["model_calls"])
        if latency is not None:
            trace["generator_latency"] = latency
        if rendered_trace is not None:
            trace["rendered_answer"] = rendered_trace
        if "exact_id" in result.trace:
            trace["exact_id"] = result.trace["exact_id"]
        if "record_context" in result.trace:
            trace["record_context"] = result.trace["record_context"]
        touched_derived = any(state.pools.get(arm) for arm in DERIVED_ARMS)
        if touched_derived or manifest.get("status") == "partial" or checked.coverage is not None:
            trace["coverage"] = manifest.get("coverage")
        unsupported = [*checked.unsupported_constraints, *self._exact_unsupported(state)]
        limitations = list(getattr(checked, "limitations", []))
        coverage = self._source_coverage(ctx, state, evidence)
        if coverage is not None:
            # M2b-2: per-source coverage in every answer of the unified collection; its gaps
            # (fallback plan, refused operations half, sources not served) are stated limitations
            trace["source_coverage"] = coverage
            limitations.extend(x for x in coverage.get("limitations", []) if x not in limitations)
        if emit is not None:
            emit(ContractEvent(status=checked.contract, violations=list(checked.violations), unsupported_constraints=unsupported))
        return AnswerResult(
            answer=checked.answer,
            retrieval=result,
            trace=trace,
            unsupported_constraints=unsupported,
            coverage=checked.coverage,
            violations=list(checked.violations),
            contract=checked.contract,
            limitations=limitations,
        )

    def _operations_section(self, state: _Retrieval) -> Any:
        """The rendered operations half of a two-halves answer (``generation/operations.py``), or
        ``None``: no two-halves planner, no operations half planned or refused, or a deterministic
        investigation answer."""
        record = state.two_halves
        if self.two_halves is None or record is None or record.operations is None:
            return None
        operations = record.operations
        if not (operations.steps or operations.refused):
            return None
        if state.decision.policy_applied in {"log_outage_investigation", "log_shared_errors"}:
            return None
        from .generation.operations import OperationsSection, backing_of, header_line, section_labels

        live = [a for a in state.aggregates if a.get("intent") and not a.get("superseded_by")]
        aggregates = [
            AggregateEvidence(
                id=a["id"], result=scope_result_of(a), rendering=a.get("rendering") or f"{a['intent']}: {a.get('status')}", rows=a.get("rows") or [], columns=a.get("columns") or [], scalar_count=self._scalar_count(a)
            )
            for a in live
        ]
        renderings = " ".join(" ".join((a.rendering or "").split()) for a in aggregates)
        notes: list[str] = []
        texts = [*operations.notes, *(u.suggestion for u in operations.unsupported if u.suggestion)]
        if operations.refused:
            texts.append(f"Operations evidence was not read: {operations.refused}.")
        for text in texts:
            flat = " ".join(str(text).split())
            if flat and flat not in renderings and flat not in notes:
                notes.append(flat)
        current, past = section_labels(self.two_halves.operation_sources, self.two_halves.knowledge_sources)
        # staging round 6: one reading line (as-of, window, clock, services read, tenant) from the
        # executed aggregates' bound arguments and coverage
        logs = self.config.logs
        log_timezone = logs.timezone if logs is not None else "UTC"
        header = header_line(live, question_timezone=self.config.question.timezone, log_timezone=log_timezone)
        return OperationsSection(aggregates=aggregates, notes=notes, label=current, knowledge_label=past, backing=backing_of(live), header=header)

    def _with_operations(self, ctx: RunContext, state: _Retrieval, checked: Any, section: Any, violations: list[Any], on_violation: str) -> Any:
        """The checked answer with the rendered operations half first (``operations.compose``); the
        post-check removals are recorded as violations (``annotate``: stated as limitations)."""
        from .generation.contract import LIMITATIONS_HEADING
        from .generation.operations import NOTES, compose

        answered = any(a.get("status") != "refused" for a in state.aggregates if a.get("intent") and not a.get("superseded_by"))
        answer = compose(section, checked.answer, answered=answered)
        limitations = list(checked.limitations)
        contract = checked.contract
        if violations:
            if contract == "passed":
                contract = "annotated" if on_violation == "annotate" else "stripped"
            if on_violation == "annotate":
                added = [NOTES[v.rule] for v in violations if NOTES[v.rule] not in limitations]
                if added:
                    block = "\n".join(f"- {n}" for n in added)
                    text = answer.answer + ("\n" + block if f"\n{LIMITATIONS_HEADING}\n" in answer.answer else f"\n\n{LIMITATIONS_HEADING}\n" + block)
                    answer = answer.model_copy(update={"answer": text})
                    limitations.extend(added)
        ctx.trace.event("operations_section", aggregates=sorted(section.ids), notes=len(section.notes), removed={v.rule: v.detail for v in violations}, backing=sorted(section.backing))
        return checked.model_copy(update={"answer": answer, "violations": [*violations, *checked.violations], "contract": contract, "limitations": limitations})

    @staticmethod
    def _generator_latency(calls: list[dict[str, Any]]) -> dict[str, Any] | None:
        """The last generator call's timing (2026-11-09): total ``seconds``, ``first_token_seconds``
        when the call streamed (``None`` otherwise: a non-streamed reply has no first-token time),
        and the thinking/effort that was sent."""
        generator = [c for c in calls if c.get("stage") == "generator"]
        if not generator:
            return None
        call = generator[-1]
        return {
            "seconds": call.get("seconds"),
            "first_token_seconds": call.get("first_token_seconds"),
            "stream": bool(call.get("stream")),
            "thinking": call.get("thinking"),
            "effort": call.get("effort"),
            "reasoning_tokens": call.get("reasoning_tokens"),
        }

    # --- tools (read-only) and actions (allowlisted writes) -------------------------------------

    def call_tool(self, name: str, args: dict[str, Any] | None = None, principal: Principal | None = None, session_id: str | None = None) -> ToolCall:
        """Run an enabled built-in tool in a fresh context (pinned generation, ACL, budget, trace)."""
        if name not in self._tools:
            raise RequestRejected(f"tool {name!r} is not enabled; enabled: {self.tools.names()}")
        tool = self._tools[name]
        parsed = tool.input_model.model_validate(args or {})
        ctx, _pinned, session, info, _ = self._begin(principal, session_id)
        try:
            result = invoke_tool(ctx, tool, parsed)
        finally:
            self._finish(ctx, session, info, None, [])
        return ToolCall(tool=name, result=result, trace=ctx.trace.to_dict(self.config.privacy.trace_masking))

    def run_action(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        *,
        idempotency_key: str,
        principal: Principal | None = None,
        reason: str = "",
        surface: str = "python",
    ) -> ActionCall:
        """Run an allowlisted write action; audited before and after, denials included."""
        request = ActionRequest(action=action, args=dict(args or {}), idempotency_key=idempotency_key, principal=principal or ANONYMOUS, reason=reason)
        trace = Trace()
        with trace.span(f"action.{action}", action=action, surface=surface) as span:
            result = self.actions.invoke(request, surface)  # type: ignore[arg-type]
            span.set(status=result.status, audit_id=result.audit_id)
        return ActionCall(action=action, result=result, trace=trace.to_dict(self.config.privacy.trace_masking))

    def list_values(self, field_name: str, limit: int = 100, principal: Principal | None = None) -> dict[str, Any]:
        """Distinct values of a ``listable`` field within the principal's ACL scope (serving surface
        ``/v2/values/{field}``; works whether or not the ``list_values`` tool is enabled)."""
        ctx, _pinned, _session, _info, _ = self._begin(principal, None)
        try:
            values = self.values_in(ctx, field_name, limit + 1)
        finally:
            self._finish(ctx, None, None, None, [])
        return {"field": field_name, "values": values[:limit], "truncated": len(values) > limit, "generation": ctx.generation.generation}

    # --- LogOperations for the log tools (addendum A2 §4.1.5) ---------------------------------------

    def log_context(self, ctx: RunContext) -> Any:
        """Compilation context of the log intents: the anchor day in ``logs.timezone`` and the
        count queries of the logs source (name -> lower-case levels)."""
        from zoneinfo import ZoneInfo

        from .decision.log_intents import CompileContext

        logs = self.config.logs
        if logs is None or not logs.intents.enabled:
            raise RequestRejected("log tools need logs.intents.enabled")
        source = self.config.sources.get(logs.source)
        counts = {name: [lv.casefold() for lv in q.levels] for name, q in (source.counts.items() if source is not None else [])}
        today = self._clock(ZoneInfo(logs.timezone)).date()
        days = sorted((self.log_manifest(ctx).get("logs") or {}).get("days") or [])
        latest = date.fromisoformat(days[-1]) if days else None
        fallback = latest if latest is not None and latest < today and today.isoformat() not in days else None
        return CompileContext(today=today, default_window=logs.intents.default_window, count_queries=counts, latest_day=fallback, collection_scope=(self.log_manifest(ctx).get("logs") or {}).get("collection_scope"))

    def log_execute(self, ctx: RunContext, spec: ScopeSpec) -> ScopeResult:
        """A compiled log scope on the record store under the caller's constraints (the scope guard
        does not apply: the scope comes from a ``LogIntentCall``)."""
        from .backends._scope import non_additive

        refused = non_additive(spec, self.config.metadata_schema)
        if refused is not None:
            return ScopeResult(exactness="bounded", denominator=0, unsupported=[refused])
        with ctx.trace.span("log_scope", kinds=[p.value for p in spec.predicates if p.field == "kind"]):
            return self.records_execute(ctx.generation, spec, list(ctx.constraints()))

    # --- history tier (H1): day kinds are read from the generation's day shards -------------------

    @property
    def history(self) -> Any:
        from .ingestion.history import open_history, tier_enabled

        if not tier_enabled(self.config):
            return None
        if getattr(self, "_history_store", None) is None:
            self._history_store = open_history(self.config, self.backends)
        return self._history_store

    def history_shards(self, gen: Any) -> list[str] | None:
        """The day shards generation ``gen`` references (``None``: no history tier)."""
        if self.history is None:
            return None
        manifest = self.write_path.registry.manifest(gen.generation) or {}
        return sorted((((manifest.get("logs") or {}).get("history") or {}).get("shards") or {}).values())

    def history_route(self, gen: Any) -> tuple[Any, list[str], frozenset[str]] | None:
        """``(store, shards, day kinds)`` generation ``gen`` reads day documents from: the episode
        archive when its manifest names ``logs.history.store: episode_archive`` (2026-11-08
        standard), else the compose history tier; ``None`` without either."""
        from .ingestion.history import DAY_KINDS

        manifest = self.write_path.registry.manifest(gen.generation) or {}
        section = (manifest.get("logs") or {}).get("history") or {}
        if section.get("store") == "episode_archive":
            from .ingestion.episode_archive import ARCHIVE_KINDS, open_archive

            if getattr(self, "_archive_store", None) is None:
                self._archive_store = open_archive(self.config)  # ConfigError when the archive is not configured
            kinds = frozenset(section.get("kinds") or ARCHIVE_KINDS)
            # I1: the archive serving pointer (advanced per closed day, O(1)) names the served
            # days; without a pointer the generation manifest does (archives before I1)
            view = self._archive_store.served_view()
            shards = (view["shards"] if view is not None else section.get("shards")) or {}
            return self._archive_store, sorted(shards.values()), kinds
        if self.history is None:
            return None
        return self.history, sorted((section.get("shards") or {}).values()), DAY_KINDS

    def records_execute(self, gen: Any, spec: ScopeSpec, constraints: list[Any]) -> ScopeResult:
        """``RecordStore.execute`` with the history tier: a scope over day kinds reads the
        generation's day shards (shard filter), any other scope the generation's records; a scope
        mixing both is refused (``bounded``), never half answered."""
        from .models import UnsupportedConstraint

        route = self.history_route(gen)
        if route is None:
            return self.records.execute(gen, spec, constraints)
        store, shards, day_kinds = route
        kinds: set[str] | None = None
        for p in spec.predicates:
            if p.field == "kind" and not p.negate and p.op in {"eq", "in"}:
                named = {p.value} if p.op == "eq" else set(p.value or [])
                kinds = named if kinds is None else kinds & named
        if kinds is not None and kinds and kinds <= day_kinds:
            return store.execute(shards, spec, constraints, self.config.metadata_schema)
        if kinds is not None and not (kinds & day_kinds):
            return self.records.execute(gen, spec, constraints)
        refused = UnsupportedConstraint(constraint_text="kind", reason="not_supported_by_backend", suggestion="with the history tier a scope names day kinds only or window kinds only (one kind condition)")
        return ScopeResult(exactness="bounded", denominator=0, unsupported=[refused])

    def history_weights(self, ctx: RunContext, link: Document, since: str | None = None) -> dict[str, int]:
        """Per-day weights of a ``graph_link`` edge from the day shards (lines per day, from ``since``)."""
        from .core.types import Measure, Predicate as P

        kind_of = {"emits": ("signature_day", "lines"), "runs": ("unit_day", "lines_total"), "serves": ("endpoint_day", "lines"), "raises": ("exception_day", "lines")}
        m = link.metadata
        kind, measure = kind_of.get(str(m.get("predicate")), (None, None))
        shards = self.history_shards(ctx.generation)
        if kind is None or not shards:
            return {}
        sf, of = str(m.get("subject_field")), str(m.get("object_field"))
        window = [P(field="date", op="range", lower=since)] if since else []
        spec = ScopeSpec(
            predicates=[P(field="kind", op="eq", value=kind), *window, P(field=sf, op="eq", value=m.get(sf)), P(field=of, op="eq", value=m.get(of))],
            group_by=["date"],
            measure=Measure(kind="sum", field=measure),
        )
        result = self.history.execute(shards, spec, list(ctx.constraints()), self.config.metadata_schema)
        return {str(g.key["date"]): int(g.value) for g in result.groups if isinstance(g.value, (int, float))}

    def log_manifest(self, ctx: RunContext) -> dict[str, Any]:
        if self.view.partitioned:  # the live log member's section of the request pin (staging fix B1)
            from .runtime.member_logs import log_manifest

            return log_manifest(self, ctx.generation)
        return self.write_path.registry.manifest(ctx.generation.generation) or {}

    def run_log_intent(self, ctx: RunContext, call: Any) -> Any:
        from .runtime.log_tools import run_intent

        return run_intent(self, ctx, call)

    def graph_view(self, ctx: RunContext, request: Any) -> Any:
        """A bounded neighbourhood of the entity named ``request.entity`` (``graph_neighbourhood``)."""
        from .backends import extras
        from .core.types import TraversalSpec
        from .providers import tokens
        from .runtime.log_tools import GraphEdge, GraphNode, GraphView

        arm = self.default.config.arms.graph
        backend = self.backends[arm.backend if arm is not None else self.config.storage.communities]
        graph = extras.require(backend, extras.GraphExtras, "graph", "find_entities")
        scope = Scope(predicates=list(ctx.constraints()))
        seeds = [e for e in graph.find_entities(ctx.generation, tokens(request.entity), scope) if e.name.casefold() == request.entity.casefold()]
        if not seeds:
            return GraphView(seeds=[], nodes=[], edges=[], note=f"no entity named {request.entity!r} in this generation")
        spec = TraversalSpec(seed_entity_ids=[e.id for e in seeds], hops=request.hops, edge_kinds=list(request.edge_kinds), limit=request.limit + 1)
        paths = backend.graph.traverse(ctx.generation, spec, scope)
        truncated = len(paths) > request.limit
        edges: dict[tuple[str, str, str, str], Any] = {}
        node_ids: set[str] = {e.id for e in seeds}
        for path in paths[: request.limit]:
            node_ids.update(path.nodes)
            for edge in path.edges:
                edges.setdefault((edge.subject_id, edge.predicate, edge.object_id, edge.edge_kind), edge)
        names = {}
        for node in sorted(node_ids):  # one representative mention per node (a node has one per day document)
            found = graph.mentions(ctx.generation, [node], scope, 1)
            if found:
                names[node] = found[0]
        doc_ids = sorted({o.span.document_id for e in edges.values() for o in (e.occurrences or [])} | {e.span.document_id for e in edges.values()})
        docs = {d.id: d for d in self.documents_in(ctx, doc_ids)} if doc_ids else {}
        composed = (self.log_manifest(ctx).get("logs") or {}).get("as_of")  # the generation's last day, not the wall clock
        anchor = request.as_of or (date.fromisoformat(composed) if isinstance(composed, str) else None)
        since = (anchor - timedelta(days=request.days - 1)).isoformat() if anchor is not None else None
        out_edges = []
        for (subject, predicate, object_, kind), edge in sorted(edges.items()):
            occurrence_docs = sorted({o.span.document_id for o in (edge.occurrences or [])} or {edge.span.document_id})
            weights: dict[str, int] = {}
            for doc_id in occurrence_docs:
                doc = docs.get(doc_id)
                if doc is None:
                    continue
                if doc.kind == "graph_link":
                    for day, lines in self.history_weights(ctx, doc, since).items():
                        weights[day] = weights.get(day, 0) + lines
                    continue
                day = doc.metadata.get("date")
                lines = doc.metadata.get("lines", doc.metadata.get("lines_total"))
                if isinstance(day, str) and isinstance(lines, int) and (since is None or day >= since):
                    weights[day] = weights.get(day, 0) + lines
            out_edges.append(
                GraphEdge(
                    subject=subject,
                    predicate=predicate,
                    object=object_,
                    edge_kind=kind,
                    support=edge.support,
                    quote=edge.span.text,
                    document_id=edge.span.document_id,
                    occurrences=len(occurrence_docs),
                    weights=[{"date": d, "lines": n} for d, n in sorted(weights.items())],
                )
            )
        nodes = [GraphNode(id=n, name=names[n].name if n in names else n, type=getattr(names.get(n), "type", "") or "") for n in sorted(node_ids)]
        note = "co_firing edges are statistical co-occurrence, not causality" if "co_firing" in request.edge_kinds else ""
        return GraphView(seeds=[e.id for e in seeds], nodes=nodes, edges=out_edges, truncated=truncated, note=note)

    # --- EngineOperations for the built-in tools -------------------------------------------------

    def search_in(self, ctx: RunContext, request: SearchRequest) -> SearchResult:
        pipe, mode = self._choose(request)
        return self._retrieve(ctx, request, pipe, mode)[1]

    def documents_in(self, ctx: RunContext, ids: list[str]) -> list[Document]:
        if not ids:
            return []
        catalog = self.backends[self.config.storage.catalog].catalog
        scope = Scope(predicates=list(ctx.constraints()), document_ids=list(ids))
        found = list(catalog.iter_scope(ctx.generation, scope, Page(size=len(ids))).items)
        route = self.history_route(ctx.generation)
        missing = [i for i in ids if i not in {d.id for d in found}]
        if route is not None and route[1] and missing:  # day documents live in the day shards (H1, archive)
            found.extend(route[0].documents(route[1], missing))
        return found

    def values_in(self, ctx: RunContext, field_name: str, limit: int) -> list[str]:
        spec = self.config.metadata_schema.field(field_name)
        if spec is None or not spec.listable:
            raise RequestRejected(f"field {field_name!r} is not listable (declare listable: true in metadata_schema)")
        return list(self.records.values(ctx.generation, field_name, list(ctx.constraints()), limit))

    def aggregate_in(self, ctx: RunContext, spec: ScopeSpec, filters: dict[str, Any]) -> ScopeResult:
        from .arms.structured import BUILTIN_FIELDS

        declared = {f.name for f in self.config.metadata_schema.fields} | BUILTIN_FIELDS
        named = [p.field for p in [*spec.predicates, *spec.numerator]] + ([spec.sort.field] if spec.sort is not None else []) + list(spec.group_by) + ([spec.measure.field] if spec.measure is not None and spec.measure.field else [])
        named += list(filters)
        unknown = sorted({f for f in named if f not in declared})
        if unknown:
            raise RequestRejected(f"aggregate fields {unknown} are not declared in metadata_schema")
        # Same filter validation as search/answer (QA-02): coerced when unambiguous, otherwise
        # applied as given and the result is never exact.
        check = check_request_filters(ValueResolver(self.config.metadata_schema, self.records, ctx.generation, ctx.constraints()), dict(filters))
        ctx.trace.event("request_filters", **check.trace())
        constraints = [*ctx.constraints(), *(Predicate(field=k, op="eq", value=v) for k, v in check.applied.items())]
        from .backends._scope import non_additive

        refused = non_additive(spec, self.config.metadata_schema) or (log_scope_guard(spec, context=constraints, log_sources=log_sources(self.config)) if guard_active(self.config) else None)
        if refused is not None:
            return ScopeResult(exactness="bounded", denominator=0, unsupported=[refused])
        result = self.records_execute(ctx.generation, spec, constraints)
        if check.unsupported:
            exactness = "bounded" if result.exactness == "exact" else result.exactness
            result = result.model_copy(update={"exactness": exactness, "unsupported": [*result.unsupported, *check.unsupported]})
        return result
