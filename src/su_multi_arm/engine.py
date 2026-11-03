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
import os
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
from .core.config import Config, LuceneBackend, MemoryBackendConfig, MongoBackend as MongoBackendConfig, Neo4jBackend as Neo4jBackendConfig
from .core.config import PostgresBackend as PostgresBackendConfig
from .core.config import SqlAlchemyBackend as SqlAlchemyBackendConfig
from .core.config import SqliteBackend as SqliteBackendConfig
from .core.errors import BackendError, ConfigError
from .core.types import Document, GroupRow, Measure, Page, Predicate, Scope, ScopeResult, ScopeSpec
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
    RetrievalStep,
    RulesAgent,
    RulesConfig,
    StructuredStep,
    detect_intents,
)
from .extraction.base import BuiltinExtractor
from .generation import AggregateEvidence, AnswerContract
from .ingestion.write import WritePath
from .models import Aggregate, Answer, AnswerResult, Coverage, Hit, SearchRequest, SearchResult, UnsupportedConstraint
from .pipeline import JsonModelReviewer, ReviewInput, ReviewStage, ReviewStageConfig
from .pipeline.spec import ARM_ORDER, PipelineRuntime, RequestRejected, allowed_modes, choose
from .pipeline.validate import decision_for_mode, raise_for
from .profiles.prompts import GENERATOR_PROMPT, voice_instruction
from .providers import Embedder, JsonModel, RunBudgetMeter
from .runtime.acl import ANONYMOUS, AccessPolicy, NoConstraints, Principal
from .runtime.actions import ActionRequest, ActionRunner
from .runtime.budget import BudgetLimits, load_price_table
from .runtime.builtin_actions import BUILTIN_ACTIONS, ActionCall, LedgerAuditLog
from .runtime.builtin_tools import ToolCall, builtin_tool_registry
from .runtime.context import RunContext, new_context
from .runtime.events import AnswerStream, ArmResultEvent, ContractEvent, DecisionEvent, EvidenceEvent, ReviewRoundEvent
from .runtime.session import Session, SessionManager
from .runtime.tools import invoke_tool
from .runtime.trace import Trace
from .runtime.turns import SessionTurns
from .selection import Candidate, PackExtra, PackRecord, PerArm, Selected, SelectionPolicy, pack, select

ARM_NAMES = ARM_ORDER
DERIVED_ARMS = ("graph", "community")
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
        missing_or_invalid=aggregate.get("missing_or_invalid", 0),
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


class Engine:
    def __init__(self, config: Config | ConfigV1, components: Iterable[Any] = (), acl: AccessPolicy | None = None):
        if isinstance(config, ConfigV1):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                config = upgrade(config).config
        if not isinstance(config, Config):
            raise ConfigError("Engine needs a su_multi_arm.core.config.Config (v2) or a v1 Config")
        self.config = config
        self.acl = acl or NoConstraints()
        self.backends = self._build_backends()
        roles = config.models.roles
        self.embedder = Embedder(roles.embedder)
        self.models = {name: JsonModel(role) for name, role in roles.chat_roles().items()}
        catalog = getattr(self.backends[config.storage.catalog], "catalog")
        self.records = getattr(self.backends[config.storage.records], "records")
        injected = self._sort_components(components)
        deps = Dependencies(
            backends={**self.backends, "records": self.records},
            providers={"embedder": self.embedder, **self.models},
            components={"catalog": catalog, **injected["models"]},
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
        self.write_path = WritePath(
            config,
            self.backends,
            extractor=self.extractor,
            embedder=self.embedder,
            community_model=self.models.get("community"),
            profile=config.profile,
            config_fingerprint=config.fingerprint(),
        )
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
        self._reranker = None

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
                return LlmAgent(LlmConfig(on_failure=cfg.on_failure, role=roles.planner, max_steps=cfg.max_steps, prompt=cfg.prompt), deps)
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
        """``validate()`` of every component of every pipeline and every tool; all errors at once."""
        env = Environment(env=dict(os.environ), base_path=Path.cwd())
        problems: list[Diagnostic] = []
        stages: list[tuple[str, Any]] = [("extraction", self.extractor)]
        for name, runtime in self.pipelines.items():
            prefix = "pipeline" if name == "default" else f"pipelines.{name}"
            stages.extend((f"{prefix}.arms.{arm}", component) for arm, component in runtime.arms.items())
            stages.extend((f"{prefix}.decision", agent) for agent in runtime.decisions.values())
            stages.extend((f"{prefix}.review", review) for review in runtime.reviews.values() if review is not None)
        stages.extend((f"tools.{name}", tool) for name, tool in self._tools.items())
        seen: set[int] = set()
        for path, component in stages:
            if id(component) in seen:
                continue
            seen.add(id(component))
            for diagnostic in component.validate(env):
                if diagnostic.severity == "error":
                    problems.append(diagnostic.model_copy(update={"path": diagnostic.path or path, "message": f"{diagnostic.component or component.name}: {diagnostic.message}"}))
        if problems:
            raise ConfigError("; ".join(d.message for d in problems), problems[0].path if len(problems) == 1 else None, diagnostics=problems)

    # --- lifecycle ---------------------------------------------------------------------------

    def ingest(self, documents, **kwargs):
        return self.write_path.ingest(documents, **kwargs)

    def manifest(self) -> dict[str, Any]:
        return self.write_path.current()[1]

    def status(self) -> dict[str, Any]:
        return self.write_path.status()

    def _current_generation(self) -> str | None:
        try:
            return self.write_path.current()[0].generation
        except ValueError:
            return None

    def capabilities(self, pipeline: str | None = None) -> Capabilities:
        """What the decision agent may plan with: enabled arms, schema, step cap and the exact_id
        identifier patterns (id-field patterns + alias map) of the chosen pipeline."""
        runtime = self.pipelines[pipeline or self.config.default_pipeline]
        return Capabilities(
            arms=runtime.enabled_arms(),
            metadata_schema=self.config.metadata_schema,
            max_steps=runtime.config.decision.max_steps,
            id_patterns=runtime.id_patterns(),
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
        )
        ctx.trace.root.set(incomplete_stages=self._incomplete_stages(manifest))
        return ctx

    # --- sessions ------------------------------------------------------------------------------

    def create_session(self, principal: Principal | None = None, session_id: str | None = None) -> str:
        """A new session in the configured session store, bound to ``(principal, dataset)``."""
        return self.sessions.create(principal or ANONYMOUS, session_id).id

    def close_session(self, session_id: str, principal: Principal | None = None) -> None:
        self.sessions.close(principal or ANONYMOUS, session_id)

    def session(self, session_id: str, principal: Principal | None = None) -> Session:
        """The stored session (turns hold hashes and IDs, never passages unless ``store_text``)."""
        return self.sessions.open(principal or ANONYMOUS, session_id)

    def _begin(self, principal: Principal | None, session_id: str | None, request: SearchRequest | None = None):
        """Pin the generation, open the session (budget clipped, memory re-validated) and build the
        context. Returns ``(ctx, pinned, session, session_trace, request)``."""
        pinned = self.write_path.pin()
        if session_id is None:
            return self._context_for(principal, pinned), pinned, None, None, request
        session = self.sessions.open(principal or ANONYMOUS, session_id)
        limits = self.sessions.clip(session, self._limits())
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
        return ctx, pinned, session, info, request

    def _finish(self, ctx: RunContext, session: Session | None, info: dict[str, Any] | None, query: str | None, shown: list[str], answer_id: str | None = None) -> None:
        if session is None or info is None:
            return
        info["usage"] = self.sessions.finish(ctx, session, query, shown, answer_id)

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
        if len(json.dumps(request.record_context).encode()) > rc.context_max_bytes or any(
            len(v) > rc.context_max_field_chars for v in request.record_context.values()
        ):
            raise ValueError(f"record_context exceeds {rc.context_max_bytes} bytes or {rc.context_max_field_chars} characters per field")

    def _scope(self, ctx: RunContext, request: SearchRequest, extra_filters: dict[str, Any]) -> tuple[Scope, list[Predicate]]:
        acl = ctx.constraints()
        filters = {**request.filters, **extra_filters}
        excluded = list(dict.fromkeys(request.exclude_ids))
        record_id = request.record_context.get(self.config.request.record_id_key)
        if record_id and record_id not in excluded:
            excluded.append(record_id)  # the record being looked at never retrieves itself
        predicates = [*acl, *(Predicate(field=k, op="eq", value=v) for k, v in filters.items())]
        return Scope(predicates=predicates, exclude_ids=excluded), acl

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
    ) -> tuple[DecisionRequest, Capabilities, Decision, Intents, str | None]:
        capabilities = self.capabilities(pipe.name)
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
        )
        intents = detect_intents(request.query, capabilities.id_patterns)
        decision = pipe.decisions[mode].decide(ctx, decision_request, intents, capabilities)
        return decision_request, capabilities, decision, intents, unresolved

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
        candidates = getattr(arm.cfg, "candidates", 200)
        inputs = ArmInput(
            query=query,
            scope=scope,
            acl=acl,
            candidates=candidates,
            aggregate=aggregate,
            incomplete_stages=list(ctx.trace.root.attributes.get("incomplete_stages", [])),
            allow_incomplete=state.pipe.config.allow_incomplete_arms,
        )
        return arm.run(ctx, inputs)

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
            return step, result, time.perf_counter() - start

        with ThreadPoolExecutor(max_workers=state.pipe.config.concurrency) as pool:
            for step, result, seconds in pool.map(execute, steps):
                filters = {**state.request.filters, **(getattr(step, "filters", None) or {})}
                state.incomplete = state.incomplete or result.incomplete
                if not isinstance(step, StructuredStep):
                    pools.setdefault(step.arm, [])  # an arm that ran is reported even when it found nothing
                pinned = step.arm == "exact_id" and "exact_id" in state.pipe.config.selection.pins  # selection.pins honours it
                for hit in result.hits:
                    state.hits_by_id.setdefault(hit.document_id, hit)
                    pools.setdefault(step.arm, []).append(
                        Candidate(record_id=hit.document_id, arm=step.arm, score=hit.score, chunk_ids=list(hit.chunk_ids), source=hit.source, provenance=list(hit.provenance), pinned=pinned)
                    )
                for aggregate in result.aggregates:
                    aggregate["id"] = f"aggregate:{len(state.aggregates) + 1}"
                    aggregate["filters"] = filters
                    state.aggregates.append(aggregate)
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
                        "documents": len(result.hits),
                        "aggregate": bool(result.aggregates),
                        "paths": len(result.paths),
                        "incomplete": result.incomplete,
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
            if sel.rerank and ordered:
                if self._reranker is None:
                    from sentence_transformers import CrossEncoder

                    self._reranker = CrossEncoder(self.config.models.roles.reranker.model, trust_remote_code=False)
                pairs = [(state.request.query, state.hits_by_id[r.record_id].title + "\n" + state.hits_by_id[r.record_id].text) for r in ordered]
                for record, score in zip(ordered, self._reranker.predict(pairs), strict=True):
                    record.score = float(score)
                    record.reasons.append("rerank:cross_encoder")
                # Reranking reorders within the pinned/unpinned partitions; pins stay first.
                ordered.sort(key=lambda r: (not (r.pinned and sel.pins), -r.score, r.record_id))
            span.set(**trace.model_dump(mode="json"), rerank=bool(sel.rerank))
        state.ordered, state.pools = ordered, pools
        state.selection_trace = {**trace.model_dump(mode="json"), "rerank": bool(sel.rerank)}

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
        if state.unresolved_record_id is not None:
            trace["record_context"] = {"record_id": "unresolved", "used": False}
        return SearchResult(
            query=state.request.query,
            generation=ctx.generation.generation,
            hits=hits,
            arm_hits=arm_hits,
            aggregates=state.aggregates,
            unsupported_constraints=[*decision.unsupported, *self._exact_unsupported(state)],
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
            pipe, mode = choose(self.config, self.pipelines, request.pipeline, request.mode)
        mode = mode or pipe.default_mode
        decision_request, capabilities, decision, _, unresolved = self._decide(ctx, request, pipe, mode)
        decision_seconds = time.perf_counter() - started
        state = _Retrieval(
            request=request, decision_request=decision_request, capabilities=capabilities, decision=decision, pipe=pipe, mode=mode, unresolved_record_id=unresolved
        )
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
        self._select(ctx, state)
        return state, self._result(ctx, state, started, decision_seconds)

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
        pipe, mode = choose(self.config, self.pipelines, request.pipeline, request.mode)
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

    # --- evidence packing --------------------------------------------------------------------

    def _field_priority(self, pipe: PipelineRuntime | None = None) -> list[str]:
        cfg = self.config
        generation = (pipe or self.default).config.generation
        packing = (pipe or self.default).config.selection.packing
        return list(dict.fromkeys([*packing.field_priority, *generation.evidence_fields, *cfg.ingestion.chunking.field_order]))

    def _pack(self, ctx: RunContext, state: _Retrieval) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        pipe = state.pipe or self.default
        packing = pipe.config.selection.packing
        generator = self.config.models.roles.generator
        budget = generator.evidence_budget.chars if generator and generator.evidence_budget else packing.budget_chars
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
                )
            )
        extras = [
            # Aggregates are exact derived records; member IDs stay in the search output, not in the prompt.
            PackExtra(id=a["id"], source="structured", passage=json.dumps({k: v for k, v in a.items() if k not in {"document_ids", "groups"}}))
            for a in state.aggregates
        ]
        result = pack(records, config, extras)
        evidence = [e.model_dump(mode="json", exclude={"truncated"}) for e in result.evidence]
        trace = {
            "omitted_ids": result.omitted_ids,
            "truncated_ids": result.truncated_ids,
            "passage_characters": result.passage_characters,
            "fairness": result.fairness,
            "field_priority": result.field_priority,
            "budget_chars": budget,
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

    def _generate(self, ctx: RunContext, state: _Retrieval, evidence: list[dict[str, Any]], on_delta: Any = None) -> Answer:
        decision = state.decision
        if decision.outcome == "abstain":
            reason = decision.failure or "no_plan"
            return Answer(answer=f"The request could not be planned ({reason}); see unsupported_constraints.", insufficient_evidence=True)
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
        if on_delta is not None and generator.streams():
            reply = generator.call_stream(prompt, payload, Answer, bridge, "generator", on_delta)
        else:
            reply = generator.call(prompt, payload, Answer, bridge, "generator")
        return reply if isinstance(reply, Answer) else Answer.model_validate(reply.model_dump(mode="json"))

    def answer(self, request: SearchRequest | str, principal: Principal | None = None, session_id: str | None = None) -> AnswerResult:
        if isinstance(request, str):
            request = SearchRequest(query=request)
        pipe, mode = choose(self.config, self.pipelines, request.pipeline, request.mode)
        started = time.perf_counter()
        ctx, pinned, session, info, request = self._begin(principal, session_id, request)
        return self._answer_turn(ctx, pinned, session, info, request, pipe, mode, started)

    def answer_stream(self, request: SearchRequest | str, principal: Principal | None = None, session_id: str | None = None) -> AnswerStream:
        """``answer`` as a stream of typed events (``runtime/events.py``). Request, pipeline/mode and
        session problems raise here, before any event; failures during the run end the stream with
        an ``error`` event. The ``done`` event carries the same ``AnswerResult`` as ``answer``."""
        if isinstance(request, str):
            request = SearchRequest(query=request)
        pipe, mode = choose(self.config, self.pipelines, request.pipeline, request.mode)
        self._check_request(request)
        started = time.perf_counter()
        ctx, pinned, session, info, request = self._begin(principal, session_id, request)

        def run(emit: Any, on_delta: Any) -> AnswerResult:
            return self._answer_turn(ctx, pinned, session, info, request, pipe, mode, started, emit=emit, on_delta=on_delta)

        return AnswerStream(run, cancel_budget=ctx.budget.cancel)

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
            emit(EvidenceEvent(ids=[row["id"] for row in evidence], omitted_ids=list(evidence_trace.get("omitted_ids", [])), truncated_ids=list(evidence_trace.get("truncated_ids", []))))
        answer = self._generate(ctx, state, evidence, on_delta=on_delta)
        evidence_ids = {row["id"] for row in evidence}
        aggregates = [AggregateEvidence(id=a["id"], result=scope_result_of(a)) for a in state.aggregates if a["id"] in evidence_ids]
        manifest = pinned[1]
        checked = pipe.contract.enforce(
            answer,
            evidence_ids=evidence_ids,
            aggregates=aggregates,
            intents=state.decision.intents,
            unsupported=state.decision.unsupported,
            coverage=self._coverage(manifest),
            trace=ctx.trace,
        )
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
            "contract": {"status": checked.contract, "violations": [v.model_dump(mode="json") for v in checked.violations]},
            "trace_id": ctx.trace.id,
            "profile": self.config.profile,
        }
        if "exact_id" in result.trace:
            trace["exact_id"] = result.trace["exact_id"]
        if "record_context" in result.trace:
            trace["record_context"] = result.trace["record_context"]
        touched_derived = any(state.pools.get(arm) for arm in DERIVED_ARMS)
        if touched_derived or manifest.get("status") == "partial" or checked.coverage is not None:
            trace["coverage"] = manifest.get("coverage")
        unsupported = [*checked.unsupported_constraints, *self._exact_unsupported(state)]
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
        )

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
        ctx = self._context_for(principal)
        values = self.values_in(ctx, field_name, limit + 1)
        return {"field": field_name, "values": values[:limit], "truncated": len(values) > limit, "generation": ctx.generation.generation}

    # --- EngineOperations for the built-in tools -------------------------------------------------

    def search_in(self, ctx: RunContext, request: SearchRequest) -> SearchResult:
        pipe, mode = choose(self.config, self.pipelines, request.pipeline, request.mode)
        return self._retrieve(ctx, request, pipe, mode)[1]

    def documents_in(self, ctx: RunContext, ids: list[str]) -> list[Document]:
        if not ids:
            return []
        catalog = self.backends[self.config.storage.catalog].catalog
        scope = Scope(predicates=list(ctx.constraints()), document_ids=list(ids))
        return list(catalog.iter_scope(ctx.generation, scope, Page(size=len(ids))).items)

    def values_in(self, ctx: RunContext, field_name: str, limit: int) -> list[str]:
        spec = self.config.metadata_schema.field(field_name)
        if spec is None or not spec.listable:
            raise RequestRejected(f"field {field_name!r} is not listable (declare listable: true in metadata_schema)")
        return list(self.records.values(ctx.generation, field_name, list(ctx.constraints()), limit))

    def aggregate_in(self, ctx: RunContext, spec: ScopeSpec, filters: dict[str, Any]) -> ScopeResult:
        from .arms.structured import BUILTIN_FIELDS

        declared = {f.name for f in self.config.metadata_schema.fields} | BUILTIN_FIELDS
        named = [p.field for p in spec.predicates] + list(spec.group_by) + ([spec.measure.field] if spec.measure is not None and spec.measure.field else [])
        named += list(filters)
        unknown = sorted({f for f in named if f not in declared})
        if unknown:
            raise RequestRejected(f"aggregate fields {unknown} are not declared in metadata_schema")
        constraints = [*ctx.constraints(), *(Predicate(field=k, op="eq", value=v) for k, v in filters.items())]
        return self.records.execute(ctx.generation, spec, constraints)
