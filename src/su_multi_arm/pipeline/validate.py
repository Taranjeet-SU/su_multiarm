"""Pipeline validation and ``doctor --probe`` (plan §3.3, Phase 1 rows "Pipeline spec + engine v2 +
validation", "doctor --probe + CLI v2", "Storage selectors").

``config_diagnostics`` is the static layer run by ``Config`` at load: every check runs and every
problem is returned (not first-error-only); ``raise_for`` turns the errors into one ``ConfigError``
whose ``diagnostics`` lists them all. Checks: backend kinds available; every ``storage`` slot and
every arm of **every** pipeline names a backend whose adapter serves the capability it needs; the
model roles a pipeline uses are configured; guards, tools, actions and plugin components exist in a
registry; named pipelines only use index projections the write path builds (those of the default
``pipeline``); the request allowlists name existing pipelines and buildable decision modes; budgets
can pay for one pass. Nothing here connects to anything.

``probe_report`` is the dynamic layer behind ``doctor --probe``: it calls ``probe()`` on every
constructed backend and reports the declared ``(provider, model)`` capability pairs from the
registry. It never calls a model provider (no paid calls).
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any

from ..backends import capabilities as caps
from ..core.component import Diagnostic
from ..core.errors import ConfigError

if TYPE_CHECKING:  # pragma: no cover
    from ..core.config import Config, DecisionConfig, PipelineConfig

PROJECTED_ARMS: tuple[str, ...] = ("dense", "lexical", "graph", "community")  # arms reading write-path projections
MAX_ALIASES = 10000


def _error(path: str, code: str, message: str) -> Diagnostic:
    return Diagnostic(severity="error", code=code, message=message, path=path)


def _warning(path: str, code: str, message: str) -> Diagnostic:
    return Diagnostic(severity="warning", code=code, message=message, path=path)


def raise_for(diagnostics: list[Diagnostic]) -> None:
    """One ``ConfigError`` carrying every error diagnostic (path of the first)."""
    errors = [d for d in diagnostics if d.severity == "error"]
    if not errors:
        return
    if len(errors) == 1:
        raise ConfigError(errors[0].message, errors[0].path, diagnostics=errors)
    listed = "; ".join(f"{d.path}: {d.message}" if d.path else d.message for d in errors)
    raise ConfigError(f"{len(errors)} configuration errors: {listed}", errors[0].path, diagnostics=errors)


def decision_for_mode(decision: DecisionConfig, mode: str) -> DecisionConfig:
    """The pipeline's decision config with ``kind`` overridden by a request ``mode``.

    ``rules``/``llm`` drop cascade stages and classifier artifacts; ``classifier`` needs the
    pipeline's own artifact or plugin component; ``cascade`` is only the pipeline's own cascade.
    Raises ``ValueError`` when the mode cannot be built from the pipeline's config.
    """
    if mode == decision.kind:
        return decision
    if mode == "cascade":
        raise ValueError("mode cascade is only available on a pipeline whose decision kind is cascade")
    if mode == "classifier":
        if decision.artifact is None and decision.component is None:
            raise ValueError("mode classifier needs the pipeline's decision to declare an artifact or component")
        return decision.model_copy(update={"kind": "classifier", "stages": []})
    return decision.model_copy(update={"kind": mode, "stages": [], "artifact": None, "component": None})


def _llm_calls(decision: DecisionConfig, generator: bool) -> int:
    return (1 if decision.uses_llm() else 0) + (1 if generator else 0)


def _pipeline_diagnostics(config: Config, name: str, pipeline: PipelineConfig) -> list[Diagnostic]:
    from ..core.config import BUILTIN_GUARDS, DEFAULT_PIPELINE

    prefix = "pipeline" if name == DEFAULT_PIPELINE else f"pipelines.{name}"
    out: list[Diagnostic] = []
    roles = config.models.roles
    for arm, spec in pipeline.arms.enabled().items():
        backend_name = getattr(spec, "backend", None)
        if backend_name is not None:
            problem = config.capability_problem(backend_name, *caps.ARM_CAPABILITY[arm])
            if problem:
                out.append(_error(f"{prefix}.arms.{arm}.backend", "capability_missing", problem))
    if name != DEFAULT_PIPELINE:
        base = config.pipeline.arms
        for arm in PROJECTED_ARMS:
            spec = getattr(pipeline.arms, arm)
            if spec is None:
                continue
            projected = getattr(base, arm)
            if projected is None:
                out.append(
                    _error(
                        f"{prefix}.arms.{arm}",
                        "projection_missing",
                        f"the {arm} arm reads an index projection the write path builds only for arms of the default "
                        "pipeline; enable it in pipeline.arms as well",
                    )
                )
            elif projected.backend != spec.backend:
                out.append(
                    _error(
                        f"{prefix}.arms.{arm}.backend",
                        "projection_mismatch",
                        f"backend {spec.backend!r} differs from pipeline.arms.{arm}.backend {projected.backend!r}, "
                        "the only one the write path projects into",
                    )
                )
    if pipeline.decision.uses_llm() and roles.planner is None:
        out.append(_error(f"{prefix}.decision", "role_missing", f"{prefix}.decision uses an llm stage: models.roles.planner is required"))
    if pipeline.review.enabled and roles.reviewer is None:
        out.append(_error(f"{prefix}.review", "role_missing", f"{prefix}.review.enabled requires models.roles.reviewer"))
    if pipeline.selection.rerank and roles.reranker is None:
        out.append(_error(f"{prefix}.selection.rerank", "role_missing", f"{prefix}.selection.rerank requires models.roles.reranker"))
    for guard in pipeline.guards:
        if guard not in BUILTIN_GUARDS and guard not in config.plugins:
            out.append(_error(f"{prefix}.guards", "unknown_component", f"{guard!r} is neither a built-in guard nor a listed plugin"))
    if pipeline.guards:
        # Fail closed: this release has no guard runtime, so a configured guard (built-in name or
        # listed plugin) would validate and then never run. Reject it until guards ship (Phase 2).
        out.append(
            _error(
                f"{prefix}.guards",
                "not_executed",
                "guards are Phase 2: they are not executed in this release; remove "
                f"{prefix}.guards (a configured guard would otherwise be silently skipped)",
            )
        )
    if pipeline.decision.component and pipeline.decision.component not in config.plugins:
        out.append(_error(f"{prefix}.decision.component", "unknown_component", "pipeline.decision.component must be listed under plugins"))
    if pipeline.arms.structured is not None and not config.metadata_schema.filterable():
        out.append(
            _warning(
                f"{prefix}.arms.structured",
                "no_filterable_fields",
                "the structured arm is enabled but metadata_schema declares no filterable fields; declare fields to enable typed scope",
            )
        )
    exact = pipeline.arms.exact_id
    if exact is not None:
        schema = config.metadata_schema
        patterns = [f for f in schema.fields if f.type == "id" and f.pattern]
        if exact.alias_map is None and not patterns:
            out.append(
                _error(
                    f"{prefix}.arms.exact_id",
                    "no_identifier_source",
                    "exact_id has nothing to recognise identifiers with: declare a pattern on an id field of "
                    "metadata_schema or an alias_map",
                )
            )
        for field_name in exact.alias_fields:
            spec = schema.field(field_name)
            if spec is None or spec.type not in {"id", "string"} or not spec.filterable:
                out.append(
                    _error(
                        f"{prefix}.arms.exact_id.alias_fields",
                        "alias_field_invalid",
                        f"{field_name!r} must be a declared, filterable id or string field of metadata_schema",
                    )
                )
    calls = _llm_calls(pipeline.decision, roles.generator is not None)
    budgets = config.budgets
    if calls > budgets.max_llm_calls:
        out.append(_error("budgets.max_llm_calls", "budget_incoherent", f"{prefix} needs {calls} model calls per answer, more than max_llm_calls {budgets.max_llm_calls}"))
    if pipeline.review.enabled:
        worst = calls + (pipeline.review.max_hops - 1) * (1 + (1 if pipeline.decision.uses_llm() else 0))
        if worst > budgets.max_llm_calls:
            out.append(
                _warning(
                    "budgets.max_llm_calls",
                    "budget_bounds_review",
                    f"{prefix} may need {worst} model calls with review (max_hops {pipeline.review.max_hops}); "
                    f"max_llm_calls {budgets.max_llm_calls} stops review early with budget_exhausted",
                )
            )
    session = budgets.session
    if session is not None and session.max_llm_calls is not None and calls > session.max_llm_calls:
        out.append(_error("budgets.session.max_llm_calls", "budget_incoherent", f"{prefix} needs {calls} model calls per answer, more than the session allows ({session.max_llm_calls})"))
    return out


def config_diagnostics(config: Config) -> list[Diagnostic]:
    """Every static problem of a parsed v2 config (profile overlay already applied)."""
    from ..core.config import AVAILABLE_PROFILES, BUILTIN_ACTIONS, BUILTIN_TOOLS, DEFAULT_PIPELINE

    out: list[Diagnostic] = []
    if config.profile not in AVAILABLE_PROFILES:
        out.append(_error("profile", "unknown_profile", f"profile {config.profile!r} is not available; built-in profiles: {sorted(AVAILABLE_PROFILES)}"))
    for name, backend in config.backends.items():
        if caps.adapter(backend.kind).status == "planned":
            out.append(_error(f"backends.{name}", "backend_planned", f"kind {backend.kind!r} is planned but not available in this release"))
    for slot, backend_name in config.storage.model_dump().items():
        problem = config.capability_problem(backend_name, *caps.STORAGE_CAPABILITY[slot])
        if problem and slot == "catalog" and problem.endswith("'registry' flag"):
            problem = f"{problem}: it has no generation registry and cannot hold the authoritative catalog; use it for arms/records only"
        if problem:
            out.append(_error(f"storage.{slot}", "capability_missing", problem))
    if config.sessions.store is not None:
        problem = config.capability_problem(config.sessions.store, "session")
        if problem:
            out.append(_error("sessions.store", "capability_missing", problem))
    roles = config.models.roles
    if "llm" in config.extraction.extractors and roles.extractor is None:
        out.append(_error("extraction.extractors", "role_missing", "extraction.extractors includes llm: models.roles.extractor is required"))
    pipelines = config.all_pipelines()
    for name, pipeline in pipelines.items():
        out.extend(_pipeline_diagnostics(config, name, pipeline))
    if config.default_pipeline not in pipelines:
        out.append(_error("default_pipeline", "unknown_pipeline", f"{config.default_pipeline!r} is not a pipeline; known: {sorted(pipelines)}"))
    for name in config.request.pipelines:
        if name not in pipelines:
            out.append(_error("request.pipelines", "unknown_pipeline", f"{name!r} is not a pipeline; known: {sorted(pipelines)}"))
    selectable = [n for n in dict.fromkeys([config.default_pipeline, *config.request.pipelines]) if n in pipelines]
    for mode in config.request.modes:
        for name in selectable:
            try:
                decision = decision_for_mode(pipelines[name].decision, mode)
            except ValueError as exc:
                out.append(_error("request.modes", "mode_unbuildable", f"{mode!r} on pipeline {name!r}: {exc}"))
                continue
            if decision.uses_llm() and roles.planner is None:
                out.append(_error("request.modes", "role_missing", f"{mode!r} on pipeline {name!r} needs models.roles.planner"))
    for tool in config.tools.enabled:
        if tool not in BUILTIN_TOOLS:
            out.append(_error("tools.enabled", "unknown_component", f"{tool!r} is not a built-in tool ({', '.join(BUILTIN_TOOLS)})"))
    for action in config.actions.allow:
        if action not in BUILTIN_ACTIONS:
            out.append(_error("actions.allow", "unknown_component", f"{action!r} is not a built-in action ({', '.join(BUILTIN_ACTIONS)})"))
    for action in config.serving.actions.allow:
        if action not in config.actions.allow:
            out.append(_error("serving.actions.allow", "action_not_allowed", f"{action!r} must also be listed in actions.allow"))
    if DEFAULT_PIPELINE in config.pipelines:  # pragma: no cover - the field validator rejects it first
        out.append(_error(f"pipelines.{DEFAULT_PIPELINE}", "reserved_name", "reserved"))
    return out


def alias_pattern(aliases: list[str], casefold: bool) -> str | None:
    """One regex recognising any alias as a whole token sequence (longest first)."""
    words = sorted({a for a in aliases if a}, key=len, reverse=True)
    if not words:
        return None
    body = "|".join(re.escape(w) for w in words)
    return ("(?i)" if casefold else "") + r"(?<![\w-])(?:" + body + r")(?![\w-])"


# --- doctor --probe ------------------------------------------------------------------------------


def _probe_backend(name: str, backend: Any, kind: str) -> dict[str, Any]:
    declared = {cap: decl.status for cap, decl in caps.adapter(kind).capabilities.items()}
    report: dict[str, Any] = {"kind": kind, "declared": declared, "contracts": sorted(caps.conformance(backend))}
    probe = getattr(backend, "probe", None)
    if not callable(probe):
        report.update(status="no_probe", detail="adapter exposes no probe()")
        return report
    try:
        report.update(status="ok", detail=probe())
    except Exception as exc:  # noqa: BLE001 - every failure is reported, never hidden
        report.update(status="error", error=f"{type(exc).__name__}: {exc}")
    return report


def probe_report(engine: Any) -> dict[str, Any]:
    """Backends probed, provider capability pairs reported from the registry; no model calls."""
    from ..providers.capabilities import lookup

    config = engine.config
    backends = {name: _probe_backend(name, engine.backends[name], spec.kind) for name, spec in config.backends.items()}
    models: dict[str, Any] = {}
    for role, spec in config.models.roles.chat_roles().items():
        entry: dict[str, Any] = {
            "provider": spec.provider,
            "model": spec.model,
            "declared": spec.capabilities.model_dump(mode="json"),
            "api_key_env_set": None if not spec.api_key_env else bool(os.environ.get(spec.api_key_env)),
            "called": False,
        }
        try:
            entry["registry"] = lookup(spec.provider, spec.model).as_dict()
        except KeyError as exc:
            entry["registry"] = None
            entry["error"] = str(exc)
        models[role] = entry
    embedder = config.models.roles.embedder
    models["embedder"] = {"provider": embedder.provider, "model": embedder.model, "dimensions": embedder.dimensions, "called": False}
    pipelines = {
        name: {
            "arms": sorted(runtime.arms),
            "decision": runtime.config.decision.kind,
            "modes": sorted(runtime.decisions),
            "review": runtime.review_enabled(),
            "id_patterns": len(runtime.id_patterns()),
        }
        for name, runtime in engine.pipelines.items()
    }
    ok = all(b["status"] in {"ok", "no_probe"} for b in backends.values()) and all("error" not in m for m in models.values())
    return {
        "probe": "ok" if ok else "failed",
        "backends": backends,
        "storage": config.storage.model_dump(),
        "sessions_store": config.sessions.store or config.storage.sessions,
        "models": models,
        "model_calls": 0,
        "pipelines": pipelines,
        "default_pipeline": config.default_pipeline,
        "tools": engine.tools.names(),
        "actions": engine.actions.names(),
    }
