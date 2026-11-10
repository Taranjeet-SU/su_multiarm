"""FastAPI app factory serving a legacy question-answering contract on package engines.

| Route | Engine call | Notes |
|---|---|---|
| ``POST /api/qa`` | ``Engine.answer_stream`` (drained) or ``Engine.answer_from_evidence`` | legacy envelope (``envelope.py``) |
| ``POST /api/qa/stream`` | the same | legacy SSE (``stream.py``); disconnect cancels |
| ``POST /api/search`` | ``Engine.search_page`` (+ ``result_notes`` on ``enrich``) | typed search envelope (``search.py``) |
| ``GET /source``, ``GET /api/source`` | ``get_document`` tool | record view (``source.py``); UI id aliases resolved |
| ``GET /api/datasets``, ``/api/models`` | profile | served datasets; retired ones are never listed |
| ``GET /api/clusters`` | ``aggregate`` + ``get_document`` tools | metadata groups when ``clusters`` is configured |
| ``GET /api/clusters``, ``/api/evolution``, ``/api/replay-runs`` | profile files | empty-but-valid + ``compat.status: pending`` when absent |
| ``GET /health`` | active generations | liveness, per dataset with ``?dataset=`` or a ``/ds/<id>`` prefix; carries ``ready`` |
| ``GET /ready`` | generation + warm-up + provider keys + spend headroom | 503 with the problems when not ready |

Every route is also served under ``/ds/<dataset>/...``: the prefix names the dataset (a body or query
``dataset`` that disagrees is refused), so a client configured with one base URL per dataset (for
example a gateway health-checking each backend) sees each dataset's own status.

One engine per served dataset; a request naming an unknown dataset or a retired one is refused with
422 (never routed to the default). Sessions: ``X-QA-Session`` (or body ``session_id``) binds a package
session derived from (dataset, client, value); with ``session_cache.enabled`` a follow-up turn may be
answered from the session's prior evidence (``session_cache.py``), otherwise every turn runs a full
search. An expired package session named again is replaced (``session_cache.restarted``), and
expired sessions are swept every ``session_sweep_seconds``. Errors are ``{"detail": ...}`` with the
package's statuses and non-leaking texts; an unclassified failure is a logged 500 (``errors.py``).

Serving limits: at most ``max_concurrent_answers`` answers run at once (one more is refused with 503
and ``Retry-After``); an answer is cancelled when its client disconnects (logged as 499 with the
spend so far) or when ``answer_deadline_seconds`` passes (504). The cheap routes (health, readiness,
catalog, models, research files, clusters, source) never wait for an answer's worker thread: they are
async or run on their own small thread limiter. Every response carries ``X-Request-Id`` (also in the
request log). Every outgoing record text goes through the record page's masking and visibility rule
(``outgoing.py``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from ...core.errors import BackendError, SessionNotFound
from ...core.types import Document
from ...models import SearchRequest
from ...runtime.acl import Principal
from ...runtime.warmup import DEFAULT_TIMEOUT_SECONDS as DEFAULT_WARMUP_TIMEOUT
from ...runtime.warmup import WarmUp
from ...runtime.events import ContractEvent, error_category, DecisionEvent, DoneEvent, ErrorEvent, EvidenceEvent, TokenEvent
from .envelope import legacy_modes, number_evidence, qa_envelope
from .errors import LegacyError, engine_error, internal_stream_error, stream_error
from .outgoing import add_links, filter_citations, filter_qa, filter_search, hidden_ids, qa_record_ids, text_filter
from .profile import DATASET_ID, DatasetProfile, LegacyProfile
from .search import LegacySearchQ, page_request, search_envelope
from .session_cache import Anchor, AnchorStore, context_hash, followup_query, gate
from .source import NOT_AVAILABLE, hidden, source_record
from .stream import KEEP_ALIVE, ProvisionalText, sse, utf8_safe

LOGGER = logging.getLogger(__name__)
LIGHT_THREADS = 8  # worker threads of the cheap routes (separate from the answers' threadpool)
BUSY = "too many answers in progress on this server; retry shortly"
SESSION_VALUE = re.compile(r"^[\x21-\x7e]{1,200}$")
EMPTY_QUESTION = "`question` must be a non-empty string"
PREFIX = "/ds/"
PENDING = {
    "clusters": {"status": "pending", "clusters": []},
    "evolution": {"epochs": [], "current_accuracy": 0, "target": 0.9, "status": "pending"},
    "replay_runs": {"runs": [], "historical_comparison": None},
}


class LegacyQ(BaseModel):
    """The legacy answer request (unknown keys ignored, as the legacy service did)."""

    model_config = ConfigDict(extra="ignore")

    question: str | None = None
    model: str | None = None
    mode: str | None = None
    agentic: bool | None = None
    dataset: str | None = None
    fallback_policy: Literal["allow", "error"] = "allow"
    as_of: str | None = None
    evaluation_mode: bool = False
    session_id: str | None = None
    start_session: bool = False
    record_context: dict[str, Any] | None = None


class _Served:
    def __init__(self, profile: DatasetProfile, engine: Any, warmup_timeout_seconds: float = DEFAULT_WARMUP_TIMEOUT):
        self.profile, self.engine = profile, engine
        roles = engine.config.models.roles
        self.generator = getattr(roles.generator, "model", None) if roles.generator else None
        self.planner = getattr(roles.planner, "model", None) if getattr(roles, "planner", None) else None
        embedder = roles.embedder
        self.embedder = {"model": Path(str(getattr(embedder, "model", "") or "")).name or getattr(embedder, "provider", None), "dimensions": getattr(embedder, "dimensions", None)} if embedder else {}
        selection = engine.default.config.selection
        self.rrf_k = selection.rrf_k
        self.rerank = bool(selection.rerank)
        self.anchors = AnchorStore(profile.session_cache)
        self.warm = WarmUp(warmup_timeout_seconds)
        self.swept_at = float("-inf")  # monotonic time of the last expired-session sweep
        self.sweep_lock = threading.Lock()

    @property
    def warmup(self) -> dict[str, Any]:
        """The warm-up state reported in ``/health`` (``runtime/warmup.py``)."""
        return self.warm.state()


class Admission:
    """At most ``limit`` answers in flight; never waits (a full server refuses with 503)."""

    def __init__(self, limit: int):
        self.limit, self.active, self._lock = limit, 0, threading.Lock()

    def try_enter(self) -> bool:
        with self._lock:
            if self.active >= self.limit:
                return False
            self.active += 1
            return True

    def leave(self) -> None:
        with self._lock:
            self.active = max(0, self.active - 1)


def _check_dataset(profile: DatasetProfile, engine: Any) -> None:
    """Startup validation: everything the profile relies on exists in the engine (fail closed)."""
    config = engine.config
    tools = set(engine.tools.names())
    if profile.source.enabled and "get_document" not in tools:
        raise ValueError(f"dataset {profile.id!r}: source view needs the get_document tool (tools.enabled)")
    declared = set(config.request.context_keys)
    missing = sorted(set(profile.record_context.keys.values()) - declared)
    if missing:
        raise ValueError(f"dataset {profile.id!r}: record_context targets {missing} are not in request.context_keys")
    schema = config.metadata_schema
    if profile.search.enabled:
        for facet in profile.search.facets:
            spec = schema.field(facet.field)
            if spec is None or not spec.groupable:
                raise ValueError(f"dataset {profile.id!r}: facet field {facet.field!r} is not groupable")
        if profile.search.date_field:
            spec = schema.field(profile.search.date_field)
            if spec is None or not spec.sortable:
                raise ValueError(f"dataset {profile.id!r}: date_field {profile.search.date_field!r} is not sortable")
    if (profile.search.enrich.enabled or profile.session_cache.enabled) and config.models.roles.generator is None:
        raise ValueError(f"dataset {profile.id!r}: search.enrich / session_cache need a generator role")
    if profile.source.hide and "get_document" not in tools:
        raise ValueError(f"dataset {profile.id!r}: source.hide applies to every response and needs the get_document tool (tools.enabled)")


def _check_clusters(profile: LegacyProfile, served: dict[str, _Served]) -> None:
    cfg = profile.clusters
    if cfg is None:
        return
    engine = served[cfg.dataset].engine
    tools = set(engine.tools.names())
    needed = {"aggregate"} | ({"get_document"} if cfg.representatives else set())
    if needed - tools:
        raise ValueError(f"clusters: dataset {cfg.dataset!r} needs the tools {sorted(needed - tools)} (tools.enabled)")
    schema = engine.config.metadata_schema
    spec = schema.field(cfg.field)
    if spec is None or not spec.groupable:
        raise ValueError(f"clusters.field {cfg.field!r} is not groupable in dataset {cfg.dataset!r}")
    if cfg.representative_sort:
        sort = schema.field(cfg.representative_sort)
        if sort is None or not sort.sortable:
            raise ValueError(f"clusters.representative_sort {cfg.representative_sort!r} is not sortable")


class RequestLog:
    """One JSONL summary row per call (no question or answer text: a hash and lengths only)."""

    def __init__(self, path: Path | None):
        self.path, self.lock = path, threading.Lock()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, row: dict[str, Any]) -> None:
        if self.path is None:
            return
        line = json.dumps({"at": time.time(), **row}, ensure_ascii=False, default=str)
        with self.lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class DatasetPrefix:
    """ASGI middleware: ``/ds/<dataset>/<rest>`` is served as ``/<rest>`` with the dataset recorded in
    ``request.state.path_dataset`` (an id that is not a valid dataset id is left alone -> 404)."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path", "").startswith(PREFIX):
            dataset, _, rest = scope["path"][len(PREFIX) :].partition("/")
            if re.fullmatch(DATASET_ID, dataset):
                scope = dict(scope)
                scope["path"] = "/" + rest
                scope["raw_path"] = scope["path"].encode()
                scope["state"] = {**(scope.get("state") or {}), "path_dataset": dataset}
        await self.app(scope, receive, send)


def _printable(value: str | None, limit: int = 120) -> str:
    return re.sub(r"[^\x20-\x7e]", "", value or "").strip()[:limit]


def _ms(start: float, end: float | None = None) -> int:
    return int(round(((end if end is not None else time.perf_counter()) - start) * 1000))


def _usd(result: Any) -> float:
    return round(sum(float(c.get("usd") or 0) for c in (result.trace or {}).get("model_calls") or []), 6)


def warm(target: _Served) -> dict[str, Any]:
    """Startup warm-up of one served dataset (no generator or planner call, no retrieval ranking):
    the query embedder is loaded, then ``Engine.warmup`` pre-opens the pinned units and stores the
    first request would load (a live log window: readers, served rows, unit catalogs, dense
    matrices). ``/ready`` reports ready only once this is ``done``; a failure or a run past
    ``warmup_timeout_seconds`` is a readiness problem (``runtime/warmup.py``)."""

    return target.warm.run(lambda: warm_steps(target))


def warm_steps(target: _Served) -> dict[str, Any]:
    embedder = getattr(target.engine, "embedder", None)
    if embedder is not None:
        embedder.encode(["warm-up"], query=True)
    engine_warmup = getattr(target.engine, "warmup", None)
    return {"engine": engine_warmup()} if callable(engine_warmup) else {}


def create_legacy_app(profile: LegacyProfile, engines: dict[str, Any] | None = None):
    """The legacy-contract app. ``engines`` (Python injection) maps dataset ids to constructed
    engines; any served dataset without one is built from its profile ``config``."""
    import anyio
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool

    from ... import __version__
    from ...core.config import load_config
    from ...engine import Engine
    from ...runtime.result_notes import result_notes
    from ...serving.sse import SseResponse

    engines = dict(engines or {})
    served: dict[str, _Served] = {}
    for dataset in profile.datasets:
        if dataset.retired:
            continue
        engine = engines.get(dataset.id)
        if engine is None:
            config_path = dataset.served_config()
            if config_path is None:
                raise ValueError(f"dataset {dataset.id!r} has neither an injected engine nor a config file")
            engine = Engine(load_config(config_path))
        if dataset.unified.enabled and getattr(engine, "two_halves", None) is None:
            # staged cutover (M2b-2): the switch never serves a configuration without the planner
            raise ValueError(f"dataset {dataset.id!r}: unified.enabled needs a configuration with crosssource.planner.enabled")
        _check_dataset(dataset, engine)
        served[dataset.id] = _Served(dataset, engine, profile.warmup_timeout_seconds)
    _check_clusters(profile, served)
    aliases = {d.source.ui_link.dataset: served[d.id] for d in profile.datasets if not d.retired and d.source.ui_link is not None}
    log = RequestLog(profile.request_log)
    app = FastAPI(title="su_multi_arm legacy QA compatibility", version=__version__)
    app.add_middleware(DatasetPrefix)
    app.state.served = served
    admission = Admission(profile.max_concurrent_answers)
    app.state.admission = admission
    clusters_cache: dict[str, Any] = {}
    limiters: dict[str, Any] = {}
    if profile.warmup:
        for target in served.values():
            target.warm.start(lambda t=target: warm_steps(t), name=f"legacy-warmup-{target.profile.id}")

    async def light(func: Any, *args: Any) -> Any:
        """Run a cheap blocking call on the light routes' own thread limiter: health, readiness and
        catalog reads never queue behind answers holding the default threadpool."""
        limiter = limiters.get("light")
        if limiter is None:
            limiter = limiters["light"] = anyio.CapacityLimiter(LIGHT_THREADS)
        return await anyio.to_thread.run_sync(lambda: func(*args), limiter=limiter)

    def error_response(status: int, detail: Any, request_id: str | None = None, headers: dict[str, str] | None = None) -> JSONResponse:
        return JSONResponse({"detail": detail}, status_code=status, headers={**({"X-Request-Id": request_id} if request_id else {}), **(headers or {})})

    def busy(request_id: str) -> JSONResponse:
        return error_response(503, f"{BUSY} (limit {profile.max_concurrent_answers} concurrent answers)", request_id, {"Retry-After": "5"})

    def path_dataset(request: Request) -> str | None:
        return getattr(request.state, "path_dataset", None)

    def dataset_of(request: Request, named: str | None) -> str | None:
        on_path = path_dataset(request)
        if on_path and named and named != on_path:
            raise LegacyError(422, f"dataset '{named}' disagrees with the dataset of the URL prefix '{on_path}'")
        return named or on_path

    def resolve(name: str | None) -> _Served:
        dataset_id = name or profile.default_dataset.id
        spec = profile.dataset(dataset_id)
        if spec is not None and spec.retired:
            raise LegacyError(422, spec.retired_message or f"dataset '{dataset_id}' is not available on this server")
        if dataset_id not in served:
            raise LegacyError(422, f"unknown dataset '{dataset_id}'; valid: {sorted(served)}")
        return served[dataset_id]

    def channel_of(request: Request) -> str:
        return _printable(request.headers.get(profile.channel_header), 40) or "api"

    def principal(request: Request) -> Principal:
        client = _printable(request.headers.get(profile.client_header))
        channel = channel_of(request)
        who = f"legacy:{channel}:{client}" if client else f"legacy:{channel}"
        return Principal(id=who, scopes=["search", "answer", "tools"])

    def context_for(target: _Served, raw: dict[str, Any] | None) -> tuple[dict[str, str], dict[str, str]]:
        mapping = target.profile.record_context.keys
        context, unsupported = {}, {}
        for key, value in (raw or {}).items():
            if value is None or value == "":
                continue
            if key not in mapping:
                unsupported[f"record_context.{key}"] = "key not mapped by the compatibility profile; not forwarded"
                continue
            context[mapping[key]] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return context, unsupported

    def sweep(target: _Served, force: bool = False) -> int:
        """Delete expired package sessions (at most every ``session_sweep_seconds`` unless forced).
        An opportunistic sweep that fails is logged and does not fail the request."""
        now = time.monotonic()
        with target.sweep_lock:
            if not force and now - target.swept_at < profile.session_sweep_seconds:
                return 0
            target.swept_at = now
        try:
            removed = target.engine.sessions.manager.expire()
        except BackendError as exc:
            if force:
                raise
            LOGGER.warning("legacy adapter session sweep failed for %s: %s", target.profile.id, type(exc).__name__)
            return 0
        if removed:
            LOGGER.info("legacy adapter: %d expired session(s) removed from %s", removed, target.profile.id)
        return removed

    def open_session(target: _Served, who: Principal, sid: str) -> tuple[int, bool]:
        """``(turns, restarted)``: the session's turns, creating it (or replacing an expired one)."""
        engine = target.engine
        try:
            return len(engine.session(sid, who).record.turns), False
        except SessionNotFound:
            pass
        try:
            engine.create_session(who, sid)
            return 0, False
        except BackendError:
            pass  # an expired row still holds the id, or a concurrent first turn created it
        sweep(target, force=True)  # deletes the expired row (this one and any other expired)
        try:
            engine.create_session(who, sid)
        except BackendError:
            return len(engine.session(sid, who).record.turns), False  # concurrent creation: reopen (raises if not)
        target.anchors.drop(sid)
        LOGGER.info("legacy adapter: expired session replaced on %s", target.profile.id)
        return 0, True

    def session_for(target: _Served, who: Principal, header: str | None, body: LegacyQ | None) -> tuple[str | None, dict[str, Any] | None, str | None]:
        """(package session id, legacy ``session_cache`` block, the caller's session value)."""
        value, source = (header or "").strip() or None, "header"
        if body is not None and body.session_id:
            if value and value != body.session_id:
                raise LegacyError(422, "X-QA-Session header and body session_id disagree")
            value, source = body.session_id, "body" if not value else "header"
        if value is None and body is not None and body.start_session:
            value, source = uuid.uuid4().hex, "start_session"
        if value is None:
            return None, None, None
        if not profile.sessions:
            raise LegacyError(422, "sessions are not enabled on this server")
        if not SESSION_VALUE.match(value):
            raise LegacyError(422, "X-QA-Session must be 1-200 printable ASCII characters without spaces")
        sid = "lq" + hashlib.sha256(f"{target.profile.id}\0{who.id}\0{value}".encode()).hexdigest()[:40]
        sweep(target)
        turns, restarted = open_session(target, who, sid)
        block = {"enabled": target.profile.session_cache.enabled, "decision": "session_restarted" if restarted else "new_search", "turns_in_window": turns, "source": source}
        if restarted:
            block["restarted"] = True  # the earlier package session expired (sessions.ttl_seconds); a new one was opened
        return sid, block, value

    def prepare(body: LegacyQ, request: Request) -> dict[str, Any]:
        started = time.perf_counter()
        question = utf8_safe((body.question or "").strip())
        if not question:
            raise LegacyError(422, EMPTY_QUESTION)
        target = resolve(dataset_of(request, body.dataset))
        warnings: list[str] = []
        unsupported: dict[str, str] = {}
        legacy_mode = "agentic" if (body.agentic or (body.mode or "").lower() == "agentic") else "hybrid"
        if body.mode and body.mode.lower() not in ("hybrid", "agentic"):
            warnings.append(f"unknown mode '{body.mode}' — used 'hybrid' (valid: hybrid, agentic)")
        route = target.profile.modes.get(legacy_mode)
        if route is None:
            raise LegacyError(422, f"mode '{legacy_mode}' is not available for dataset '{target.profile.id}'")
        known = {m.id for m in profile.models} | ({target.generator} if target.generator else set())
        if body.model and body.model not in known:
            warnings.append(f"unknown model '{body.model}' — used the default answer model")
        aliases_ = {m.id for m in profile.models if m.label == target.generator} | ({target.generator} if target.generator else set())
        if body.model and body.model not in aliases_:
            unsupported["model"] = f"per-request model selection is not supported; answers use {target.generator}"
        if body.fallback_policy == "allow":
            unsupported["fallback_policy"] = "the package never falls back; 'allow' behaves as 'error'"
        if body.evaluation_mode:
            unsupported["evaluation_mode"] = "no evaluation mode in the package; the request ran normally"
        context, context_unsupported = context_for(target, body.record_context)
        unsupported.update(context_unsupported)
        who = principal(request)
        sid, session_cache, session_value = session_for(target, who, request.headers.get(profile.session_header), body)
        try:
            search = SearchRequest(query=question, record_context=context, pipeline=route.pipeline, mode=route.mode, as_of=body.as_of)
        except ValidationError as exc:
            raise LegacyError(422, json.loads(exc.json(include_url=False, include_context=False, include_input=False))) from exc
        return {
            "question": question,
            "target": target,
            "search": search,
            "context": context,
            "who": who,
            "sid": sid,
            "session_cache": session_cache,
            "session_value": session_value,
            "legacy_mode": legacy_mode,
            "warnings": warnings,
            "unsupported": unsupported,
            "context_used": bool(context),
            "channel": channel_of(request),
            "stages": {"prepare_ms": _ms(started)},
        }

    def envelope_for(p: dict[str, Any], result: Any, items: list[dict[str, Any]], wall: float) -> dict[str, Any]:
        target: _Served = p["target"]
        started = time.perf_counter()
        selection = dict(result.retrieval.trace.get("selection") or {})
        selection.setdefault("rrf_k", target.rrf_k)
        env = qa_envelope(
            profile=target.profile,
            question=p["question"],
            result=result,
            evidence_items=items,
            legacy_mode=p["legacy_mode"],
            answer_model=target.generator,
            planner_model=target.planner,
            embedder=target.embedder,
            selection=selection,
            wall_seconds=wall,
            warnings=p["warnings"],
            unsupported=p["unsupported"],
            session_cache=p["session_cache"],
            context_used=p["context_used"],
            channel=p["channel"],
            stages=p["stages"],
        )
        if p["session_value"] is not None:
            env["session_id"] = p["session_value"]
        hide = hidden_ids(target.profile, target.engine, qa_record_ids(env), p["who"])
        filter_qa(env, target.profile, hide)
        if "coverage" in env:
            # M2b-2: labelled cross-source links on the citations (both records visible)
            add_links(env, target.engine, target.profile, p["who"])
        env["timings"]["stages"]["adapter:envelope_ms"] = _ms(started)
        engine_ms = int(round(float((result.trace or {}).get("seconds") or 0) * 1000))
        env["timings"]["stages"]["adapter:overhead_ms"] = max(0, env["timings"]["total_ms"] - engine_ms)
        p["trace_id"] = (result.trace or {}).get("trace_id")
        return env

    def caller(request: Request) -> dict[str, Any]:
        """Who called, for the request log only (printable, truncated)."""
        out: dict[str, Any] = {}
        forwarded = _printable(request.headers.get("x-forwarded-for"), 200)
        if forwarded:
            out["forwarded_for"] = forwarded
        agent = _printable(request.headers.get("user-agent"), 160)
        if agent:
            out["user_agent"] = agent
        if request.client is not None:
            out["peer"] = request.client.host
        return out

    def log_row(route: str, request: Request, status: int, started: float, p: dict[str, Any] | None = None, env: dict[str, Any] | None = None, **extra: Any) -> None:
        row = {
            "route": route,
            "status": status,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "channel": channel_of(request),
            "client": _printable(request.headers.get(profile.client_header)) or None,
            **caller(request),
            **extra,
        }
        if p is not None:
            row["dataset"] = p["target"].profile.id
            row["question_sha256"] = hashlib.sha256(p["question"].encode()).hexdigest()[:16]
            row["question_chars"] = len(p["question"])
            row["mode"] = p["legacy_mode"]
            row["session"] = p["sid"] is not None
            if p["session_cache"] is not None:
                row["session_decision"] = p["session_cache"].get("decision")
                if p["session_cache"].get("restarted"):
                    row["session_restarted"] = True
            if p.get("trace_id"):
                row["trace_id"] = p["trace_id"]
        if env is not None:
            compat = env.get("compat") or {}
            plan = env.get("plan") or {}
            row["n_citations"] = len(env.get("citations") or [])
            row["top_ids"] = [c.get("record_id") for c in (env.get("citations") or [])[:5]]
            row["modes"] = plan.get("selected_modes")
            row["plan_outcome"] = plan.get("outcome")
            row["insufficient_evidence"] = env.get("insufficient_evidence")
            if compat.get("planner_failure"):
                row["decision_failure"] = compat["planner_failure"].get("failure")
            if compat.get("withheld"):
                row["withheld"] = compat["withheld"].get("reason")
            if env.get("warnings"):
                row["warnings"] = len(env["warnings"])
            row["contract"] = compat.get("contract")
            row["generation"] = env.get("bundle_asset_id")
            row["usd"] = (compat.get("usage") or {}).get("usd")
            row["stages"] = (env.get("timings") or {}).get("stages")
        try:
            log.write(row)
        except OSError as exc:  # the request log never fails a request; the failure is logged
            LOGGER.warning("legacy adapter request log write failed: %s", type(exc).__name__)

    # --- session follow-ups ---------------------------------------------------------------------------

    def from_session(p: dict[str, Any]) -> tuple[Any, list[dict[str, Any]]] | None:
        """A follow-up answered from the session anchor's evidence, or ``None`` (full search; the
        reason is recorded in ``session_cache``)."""
        target: _Served = p["target"]
        cfg = target.profile.session_cache
        block = p["session_cache"]
        if not cfg.enabled or p["sid"] is None or block is None:
            return None
        started = time.perf_counter()
        anchor = target.anchors.get(p["sid"])
        ok, reason = gate(cfg, p["question"], anchor, target.engine._current_generation(), context_hash(p["context"]))
        block["precondition"] = reason if not ok else "eligible"
        block["decision"] = reason
        if not ok:
            return None
        request = p["search"].model_copy(update={"query": followup_query(p["question"], anchor)})
        provenance = {"kind": "session_followup", "anchor_turn": anchor.turn, "reason": "follow-up cue matched; anchor evidence re-read by id"}
        result = target.engine.answer_from_evidence(request, anchor.evidence_ids[: cfg.max_evidence], p["who"], p["sid"], provenance)
        p["stages"]["session_cache_ms"] = _ms(started)
        block["anchor_turn"] = anchor.turn
        if result.answer.insufficient_evidence:
            block["decision"] = "cache_declined"
            block["declined_usage_usd"] = _usd(result)
            return None
        block["decision"] = "cache_hit"
        hits = {h.document_id: h for h in result.retrieval.hits}
        packed = (result.trace.get("evidence_answer") or {}).get("packed_ids") or []
        items = [{"id": i, "title": hits[i].title, "source": hits[i].source, "url": hits[i].url} for i in packed if i in hits]
        return result, items

    def remember(p: dict[str, Any], result: Any, items: list[dict[str, Any]]) -> None:
        """A full-search turn becomes the session's anchor (document evidence ids only)."""
        target: _Served = p["target"]
        if not target.profile.session_cache.enabled or p["sid"] is None:
            return
        aggregates = {a.get("id") for a in result.retrieval.aggregates}
        ids = [str(i["id"]) for i in items if i.get("id") not in aggregates and i.get("source") not in {"structured", "aggregate"}]
        # 2026-11-09 cutover: the records an aggregate row lists as its contributors ("records: t:1,
        # t:2") lead the anchor, so "show me those" re-reads exactly the records the answer counted
        contributors = [str(c) for a in result.retrieval.aggregates for row in (a.get("rows") or []) if isinstance(row, dict) for c in (row.get("contributor_ids") or [])]
        ids = list(dict.fromkeys([*contributors, *ids]))[: target.profile.session_cache.max_evidence]
        turns = (p["session_cache"] or {}).get("turns_in_window", 0)
        target.anchors.put(p["sid"], Anchor(question=p["question"], evidence_ids=ids, generation=result.retrieval.generation, context_hash=context_hash(p["context"]), turn=turns + 1))

    # --- answers ------------------------------------------------------------------------------------

    def deadline_of(started: float) -> float:
        return started + profile.answer_deadline_seconds

    def spent(stream: Any) -> float | None:
        usage = stream.usage() if hasattr(stream, "usage") else None
        if not usage:
            return None
        return round(float((usage.get("used") or {}).get("usd") or 0), 6)

    async def settle(stream: Any, reason: str) -> float | None:
        """Cancel the run and wait (bounded) for its worker to stop; the USD it spent so far."""
        stream.cancel(reason)
        await anyio.to_thread.run_sync(stream.worker_done.wait, 10.0, abandon_on_cancel=True)
        return spent(stream)

    async def drain(stream: Any, p: dict[str, Any], request: Request, started: float) -> dict[str, Any]:
        """Consume an answer stream while watching for the client's disconnect and the deadline.
        ``{"result", "items"}``, or ``{"gone": True}`` / ``{"deadline": True}`` / ``{"error": event}``."""
        stages = p["stages"]
        outcome: dict[str, Any] = {}
        items: list[dict[str, Any]] = []
        deadline = deadline_of(started)

        async with anyio.create_task_group() as group:

            async def watch() -> None:
                while True:
                    message = await request.receive()
                    if message["type"] == "http.disconnect":
                        outcome["gone"] = True
                        group.cancel_scope.cancel()
                        return

            group.start_soon(watch)
            try:
                while True:
                    wait = max(0.05, min(profile.heartbeat_seconds, deadline - time.perf_counter()))
                    event = await anyio.to_thread.run_sync(stream.next_event, wait, abandon_on_cancel=True)
                    if event is None:
                        if time.perf_counter() >= deadline:
                            outcome["deadline"] = True
                            break
                        continue
                    if isinstance(event, DecisionEvent):
                        stages.setdefault("to_decision_ms", _ms(started))
                    elif isinstance(event, EvidenceEvent):
                        stages["to_evidence_ms"] = _ms(started)
                        items = list(event.items) or [{"id": i} for i in event.ids]
                    elif isinstance(event, TokenEvent):
                        stages.setdefault("to_first_token_ms", _ms(started))
                    elif isinstance(event, DoneEvent):
                        stages["to_done_ms"] = _ms(started)
                        outcome.update(result=event.result, items=items)
                        break
                    elif isinstance(event, ErrorEvent):
                        outcome["error"] = event
                        break
            except Exception as exc:  # noqa: BLE001 - re-raised below, outside the task group
                outcome["exc"] = exc
            group.cancel_scope.cancel()
        if "exc" in outcome:
            raise outcome["exc"]
        return outcome

    @app.post("/api/qa")
    async def qa(body: LegacyQ, request: Request, response: Response):
        started = time.perf_counter()
        rid = _request_id()
        response.headers["X-Request-Id"] = rid
        if not admission.try_enter():
            log_row("qa", request, 503, started, request_id=rid, refused="admission")
            return busy(rid)
        p = None
        stream = None
        try:
            p = await run_in_threadpool(prepare, body, request)
            cached = await run_in_threadpool(from_session, p)
            if cached is not None:
                result, items = cached
            else:
                target: _Served = p["target"]
                begun = time.perf_counter()
                stream = await run_in_threadpool(target.engine.answer_stream, p["search"], p["who"], p["sid"])
                p["stages"]["engine_start_ms"] = _ms(begun)
                outcome = await drain(stream, p, request, started)
                if outcome.get("gone"):
                    usd = await settle(stream, "client_disconnected")
                    log_row("qa", request, 499, started, p, request_id=rid, usd=usd, cancelled="client_disconnected")
                    return error_response(499, "client disconnected", rid)
                if outcome.get("deadline"):
                    usd = await settle(stream, "deadline_exceeded")
                    log_row("qa", request, 504, started, p, request_id=rid, usd=usd, cancelled="deadline")
                    return error_response(504, f"deadline exceeded (answer_deadline_seconds={profile.answer_deadline_seconds:g})", rid)
                if "error" in outcome:
                    error = stream_error(outcome["error"])
                    raise LegacyError(error["status"], error["detail"]["reason"])
                result, items = outcome["result"], outcome["items"]
                await run_in_threadpool(remember, p, result, items)
            env = await run_in_threadpool(envelope_for, p, result, items, time.perf_counter() - started)
        except Exception as exc:  # noqa: BLE001 - every failure maps to the legacy error shape
            status, detail = engine_error(exc, rid, "qa")
            usd = spent(stream) if stream is not None else None
            log_row("qa", request, status, started, p, request_id=rid, **({"usd": usd} if usd else {}))
            return error_response(status, detail, rid)
        finally:
            if stream is not None:
                stream.cancel("client_disconnected")  # no-op after the terminal event
            admission.leave()
        log_row("qa", request, 200, started, p, env, request_id=rid)
        return env

    def cached_events(p: dict[str, Any], result: Any, items: list[dict[str, Any]], started: float) -> tuple[list[str], dict[str, Any]]:
        """The legacy event sequence of a session follow-up (generated without streaming, as the
        earlier service did: one ``token`` event carries the checked answer)."""
        env = envelope_for(p, result, items, time.perf_counter() - started)
        env["answer_rewritten"] = False
        env["stream"] = {"tokens": 1 if env["answer"] else 0, "streamed_chars": len(env["answer"])}
        chunks = [
            sse("plan", {"modes": ["session_cache"], "planner_model": None, "mode": p["legacy_mode"], "outcome": "session_cache"}),
            sse("evidence", {"citations": [{"id": c["id"], "title": c["title"], "url": c["url"]} for c in env["citations"]], "modes": ["session_cache"]}),
            *([sse("token", {"text": env["answer"]})] if env["answer"] else []),
            sse("final", env),
        ]
        return chunks, env

    @app.post("/api/qa/stream")
    async def qa_stream(body: LegacyQ, request: Request):
        started = time.perf_counter()
        rid = _request_id()
        if not admission.try_enter():
            log_row("qa_stream", request, 503, started, request_id=rid, refused="admission")
            return busy(rid)
        p = None
        try:
            p = await run_in_threadpool(prepare, body, request)
            cached = await run_in_threadpool(from_session, p)
            if cached is not None:
                result, items = cached
                chunks, cached_env = await run_in_threadpool(cached_events, p, result, items, started)
                log_row("qa_stream", request, 200, started, p, cached_env, request_id=rid)

                async def replay():
                    try:
                        for chunk in chunks:
                            yield chunk
                    finally:
                        admission.leave()

                return SseResponse(replay(), headers={"X-Request-Id": rid})
            stream = await run_in_threadpool(p["target"].engine.answer_stream, p["search"], p["who"], p["sid"])
        except Exception as exc:  # noqa: BLE001
            admission.leave()
            status, detail = engine_error(exc, rid, "qa_stream")
            log_row("qa_stream", request, status, started, p, request_id=rid)
            return error_response(status, detail, rid)

        async def events():
            items: list[dict[str, Any]] = []
            plan_modes: list[str] = []
            numbers: dict[str, int] = {}
            provisional: ProvisionalText | None = None
            superseded = False
            withheld = False
            status, env = 200, None
            extra: dict[str, Any] = {}
            target: _Served = p["target"]
            stages = p["stages"]
            mask = text_filter(target.profile)
            redact = mask if target.profile.source.redact else None
            deadline = deadline_of(started)
            try:
                while True:
                    wait = max(0.05, min(profile.heartbeat_seconds, deadline - time.perf_counter()))
                    event = await anyio.to_thread.run_sync(stream.next_event, wait, abandon_on_cancel=True)
                    if event is None:
                        if time.perf_counter() >= deadline:
                            stream.cancel("deadline_exceeded")
                            status, extra = 504, {"cancelled": "deadline"}
                            yield sse("error", {"status": 504, "detail": {"error": "deadline_exceeded", "reason": f"deadline exceeded (answer_deadline_seconds={profile.answer_deadline_seconds:g})"}})
                            return
                        yield KEEP_ALIVE
                        continue
                    if isinstance(event, DecisionEvent):
                        stages.setdefault("to_decision_ms", _ms(started))
                        plan_modes = legacy_modes([s.get("arm") for s in event.steps if s.get("arm")])
                        planner = target.planner if event.mode in {"llm", "cascade"} else event.mode
                        yield sse("plan", {"modes": plan_modes, "planner_model": planner, "mode": p["legacy_mode"], "outcome": event.outcome})
                    elif isinstance(event, EvidenceEvent):
                        stages["to_evidence_ms"] = _ms(started)
                        items = list(event.items) or [{"id": i} for i in event.ids]
                        numbered = number_evidence(target.profile, items, None, p["channel"])
                        numbers = {c["record_id"]: c["id"] for c in numbered}
                        documents = [c["record_id"] for c in numbered if c.get("record_type") != "aggregate"]
                        hide = await run_in_threadpool(hidden_ids, target.profile, target.engine, documents, p["who"]) if target.profile.source.hide else set()
                        withheld = bool(hide)  # the generator read a hidden record: no provisional text
                        provisional = ProvisionalText(numbers, redact)
                        visible = filter_citations(numbered, hide, mask)
                        yield sse("evidence", {"citations": [{"id": c["id"], "title": c["title"], "url": c["url"]} for c in visible], "modes": plan_modes})
                    elif isinstance(event, TokenEvent):
                        stages.setdefault("to_first_token_ms", _ms(started))
                        if withheld:
                            continue
                        if provisional is None:
                            provisional = ProvisionalText(numbers, redact)
                        delta = provisional.feed(event.text)
                        if delta:
                            yield sse("token", {"text": delta})
                    elif isinstance(event, ContractEvent):
                        superseded = event.tokens_superseded
                    elif isinstance(event, DoneEvent):
                        stages["to_done_ms"] = _ms(started)
                        await run_in_threadpool(remember, p, event.result, items)
                        env = await run_in_threadpool(envelope_for, p, event.result, items, time.perf_counter() - started)
                        streamed = provisional.emitted if provisional is not None else ""
                        env["answer_rewritten"] = bool((superseded or withheld) and streamed.strip() != env["answer"].strip())
                        env["stream"] = {"tokens": provisional.tokens if provisional else 0, "streamed_chars": len(streamed)}
                        yield sse("final", env)
                        return
                    elif isinstance(event, ErrorEvent):
                        error = stream_error(event)
                        status = error["status"]
                        extra = {"error": event.category, "stage": event.stage}
                        yield sse("error", error)
                        return
            except Exception as exc:  # noqa: BLE001 - an adapter failure still ends the stream with ``error``
                env = None
                status, detail = engine_error(exc, rid, "qa_stream")  # an unclassified failure is logged with its traceback
                category = error_category(exc)
                extra = {"error": category}
                yield sse("error", internal_stream_error(rid) if status == 500 else {"status": status, "detail": {"error": category, "reason": detail if isinstance(detail, str) else json.dumps(detail)}})
            finally:
                stream.cancel("client_disconnected")
                admission.leave()
                final_status = status if stream.finished or status != 200 else 499
                usd = (env or {}).get("compat", {}).get("usage", {}).get("usd") if env else spent(stream)
                log_row("qa_stream", request, final_status, started, p, env, request_id=rid, **extra, **({"usd": usd} if env is None and usd else {}))

        return SseResponse(events(), headers={"X-Request-Id": rid})

    # --- search -------------------------------------------------------------------------------------

    @app.post("/api/search")
    async def search(body: LegacySearchQ, request: Request, response: Response):
        started = time.perf_counter()
        rid = _request_id()
        response.headers["X-Request-Id"] = rid
        try:
            target = resolve(dataset_of(request, body.dataset))
            if not target.profile.search.enabled:
                raise LegacyError(404, f"typed search is not available for dataset '{target.profile.id}'")
            context, unsupported = context_for(target, body.record_context)
            query = SearchRequest(query=body.searchString.strip(), record_context=context)
            paged, extra = page_request(target.profile, body, query)
            who = principal(request)
            page = await run_in_threadpool(target.engine.search_page, paged, who, None)
            notes = None
            enrich = target.profile.search.enrich
            if body.enrich and enrich.enabled and not body.facetsOnly and page.items:
                visible = await run_in_threadpool(hidden_ids, target.profile, target.engine, [i.document_id for i in page.items[: enrich.max_results]], who)
                items = [{"id": i.document_id, "title": i.title, "text": i.text} for i in page.items[: enrich.max_results] if i.document_id not in visible]
                if items:
                    notes = await run_in_threadpool(lambda: result_notes(target.engine, query.query, items, who, snippet_chars=enrich.snippet_chars, note_chars=enrich.summary_chars))
                if len(page.items) > enrich.max_results:
                    extra["enrich.page"] = f"notes are written for the first {enrich.max_results} results of the page only"
            env = search_envelope(target.profile, body, page, wall_seconds=time.perf_counter() - started, planner_model=target.planner, unsupported={**unsupported, **extra}, channel=channel_of(request), notes=notes)
            hide = await run_in_threadpool(hidden_ids, target.profile, target.engine, [r["id"] for r in env["results"]], who)
            filter_search(env, target.profile, hide)
        except Exception as exc:  # noqa: BLE001
            status, detail = engine_error(exc, rid, "search")
            log_row("search", request, status, started, request_id=rid)
            return error_response(status, detail, rid)
        log_row("search", request, 200, started, request_id=rid, n_results=len(env["results"]), total=env["totalResults"], top_ids=[r["id"] for r in env["results"][:5]], notice=(env["notice"] or {}).get("kind"), facts=len(env["facts"]), usd=env["compat"]["usage"]["usd"], enrich_usd=((env["compat"].get("enrich") or {}).get("usage") or {}).get("usd"))
        return env

    # --- source view --------------------------------------------------------------------------------

    def source_target(dataset: str | None, record_id: str | None) -> tuple[_Served, str]:
        """The served dataset and package id for a record-page request; UI aliases resolve only
        through their own token and rules. Everything else not served is "not available"."""
        if dataset in aliases and record_id:
            target = aliases[dataset]
            resolved = target.profile.source.ui_link.resolve(record_id)  # type: ignore[union-attr]
            if resolved is None:
                raise LegacyError(404, NOT_AVAILABLE)
            return target, resolved
        try:
            return resolve(dataset), record_id or ""
        except LegacyError:
            raise LegacyError(404, NOT_AVAILABLE) from None

    def read_source(dataset: str | None, record_id: str | None, who: Principal) -> dict[str, Any]:
        target, record_id = source_target(dataset, record_id)
        view = target.profile.source
        if not view.enabled or not record_id or not re.fullmatch(view.id_pattern, record_id):
            raise LegacyError(404, NOT_AVAILABLE)
        call = target.engine.call_tool("get_document", {"ids": [record_id]}, who)
        output = call.result.output
        documents = output.get("documents") if isinstance(output, dict) else getattr(output, "documents", [])
        docs = [d if isinstance(d, Document) else Document.model_validate(d) for d in (documents or [])]
        if not docs or hidden(target.profile, docs[0]):
            raise LegacyError(404, NOT_AVAILABLE)
        return source_record(target.profile, docs[0])

    async def source(request: Request):
        rid = _request_id()
        try:
            dataset = dataset_of(request, request.query_params.get("dataset"))
            return JSONResponse(await light(read_source, dataset, request.query_params.get("id"), principal(request)), headers={"X-Request-Id": rid})
        except Exception as exc:  # noqa: BLE001
            status, detail = engine_error(exc, rid, "source")
            return error_response(status, detail, rid)

    app.add_api_route("/source", source, methods=["GET"])
    app.add_api_route("/api/source", source, methods=["GET"])

    # --- catalog, models, research artefacts, health --------------------------------------------------

    @app.get("/api/datasets")
    async def datasets():
        listed = [{"id": d.id, "label": d.label or d.id, "default": d.default} for d in profile.datasets if not d.retired]
        return {"datasets": listed, "default": profile.default_dataset.id, "compat": {"retired": [d.id for d in profile.datasets if d.retired]}}

    @app.get("/api/models")
    async def models():
        if profile.models:
            entries = [m.model_dump(exclude_none=True) for m in profile.models]
        else:
            default = served[profile.default_dataset.id]
            provider = getattr(default.engine.config.models.roles.generator, "provider", None) if default.generator else None
            entries = [{"id": default.generator, "provider": provider or "none", "label": default.generator}] if default.generator else []
        default_id = profile.default_model or (entries[0]["id"] if entries else None)
        return {"models": entries, "default": default_id, "compat": {"selection": "informational: each dataset answers with its configured generator"}}

    def research(kind: str):
        path = profile.research_files.get(kind)  # type: ignore[call-overload]
        if path is not None and path.is_file():
            try:
                return json.loads(path.read_text())
            except (OSError, ValueError):
                return JSONResponse({"detail": f"{kind} artefact is unreadable"}, status_code=503)
        return {**PENDING[kind], "compat": {"status": "pending", "unsupported": f"no {kind.replace('_', ' ')} artefact configured; the package has no equivalent"}}

    def tool_output(engine: Any, name: str, args: dict[str, Any], who: Principal) -> Any:
        output = engine.call_tool(name, args, who).result.output
        return output.model_dump(mode="json") if hasattr(output, "model_dump") else output

    def package_clusters() -> dict[str, Any]:
        cfg = profile.clusters
        target = served[cfg.dataset]  # type: ignore[union-attr]
        engine = target.engine
        generation = engine._current_generation()
        if generation is not None and generation in clusters_cache:
            return clusters_cache[generation]
        who = Principal(id="legacy:clusters", scopes=["search", "tools"])
        grouped = tool_output(engine, "aggregate", {"scope": {"group_by": [cfg.field]}}, who)["result"]
        rows = sorted((g for g in grouped.get("groups") or [] if g["key"].get(cfg.field) not in (None, "")), key=lambda g: (-g["count"], str(g["key"].get(cfg.field))))
        mask = text_filter(target.profile)
        clusters = []
        for index, group in enumerate(rows[: cfg.top_groups]):
            value = group["key"][cfg.field]
            representatives = []
            if cfg.representatives:
                scope: dict[str, Any] = {"predicates": [{"field": cfg.field, "op": "eq", "value": value}], "limit": cfg.representatives}
                if cfg.representative_sort:
                    scope["sort"] = {"field": cfg.representative_sort, "descending": True}
                listed = tool_output(engine, "aggregate", {"scope": scope}, who)["result"].get("listed") or []
                ids = [r["id"] for r in listed]
                docs = tool_output(engine, "get_document", {"ids": ids}, who).get("documents") or [] if ids else []
                visible = {d["id"]: d for d in docs if not hidden(target.profile, Document.model_validate(d))}
                titles = {i: (d.get("title") or "").strip() for i, d in visible.items()}
                representatives = [{"ticket_id": i, "subject": mask(titles.get(i) or i)} for i in ids if i in titles]
            clusters.append({"cluster_id": f"G{index:02d}", "size": group["count"], "label": mask(str(value)), "description": mask(f"records with {cfg.field} = {value}"), "top_terms": [], "representatives": representatives, "subclusters": []})
        clustered = sum(g["count"] for g in rows)  # every record with a value (the clustered population)
        denominator = grouped.get("denominator")
        body = {
            "status": "ready",
            "n_clusters": len(clusters),
            "n_subclusters": 0,
            "total_cases": clustered,
            "clusters": clusters,
            "compat": {
                "source": f"package metadata groups: {cfg.dataset} records grouped by {cfg.field} (exactness {grouped.get('exactness')})",
                "generation": generation,
                "groups_total": len(rows),
                "records_in_scope": denominator,
                "records_without_value": None if denominator is None else max(0, int(denominator) - clustered),
                "unsupported": {
                    "clusters": "metadata groups, not embedding clusters",
                    "top_terms": "no term extraction",
                    "subclusters": "no sub-grouping",
                    "validation": "no cluster validation metrics",
                    "generated": "no generated query sets",
                },
            },
        }
        if len(rows) > cfg.top_groups:
            body["compat"]["approximations"] = {"total_cases": f"counts every record with a {cfg.field} value, including the {len(rows) - cfg.top_groups} group(s) past top_groups"}
        if generation is not None:
            clusters_cache.clear()
            clusters_cache[generation] = body
        return body

    @app.get("/api/clusters")
    async def clusters():
        if profile.clusters is not None:
            rid = _request_id()
            try:
                return await light(package_clusters)
            except Exception as exc:  # noqa: BLE001
                status, detail = engine_error(exc, rid, "clusters")
                return error_response(status, detail, rid)
        return await light(research, "clusters")

    async def evolution():
        return await light(research, "evolution")

    async def replay_runs():
        return await light(research, "replay_runs")

    app.add_api_route("/api/evolution", evolution, methods=["GET"])
    app.add_api_route("/api/replay-runs", replay_runs, methods=["GET"])

    def generation_of(target: _Served) -> str | None:
        try:
            return target.engine._current_generation()
        except Exception:  # noqa: BLE001 - a broken store is "no generation", reported as down
            return None

    def readiness(target: _Served, generation: str | None) -> dict[str, Any]:
        """Whether the dataset can answer now: an active generation, the warm-up done (when
        configured), every provider key the routed pipelines need set, and spend headroom."""
        problems: list[str] = []
        if generation is None:
            problems.append("no active generation")
        warm_problem = target.warm.problem()
        if profile.warmup and warm_problem is not None:
            problems.append(warm_problem)
        engine = target.engine
        routed = {route.pipeline or engine.config.default_pipeline for route in target.profile.modes.values()}
        missing: dict[str, list[str]] = {}
        try:
            report = engine.missing_keys()
        except Exception:  # noqa: BLE001 - reported as not ready, never as a crash of the probe
            report, problems = {}, [*problems, "provider key check failed"]
        for name in sorted(routed):
            for role, names in (report.get(f"pipeline:{name}") or {}).items():
                missing.setdefault(role, [])
                missing[role] = sorted(set(missing[role]) | set(names))
        if missing:
            problems.append("missing provider key variables: " + ", ".join(f"{role}={'/'.join(v)}" for role, v in sorted(missing.items())))
        spend_report: dict[str, Any] | None = None
        try:
            spend = engine._answers_spend()
            if spend is not None:
                usage = spend.usage()
                day = usage["day"]
                remaining = None if day.get("max_usd") is None else round(float(day["max_usd"]) - float(day["committed_usd"]) - float(day["reserved_usd"]), 6)
                spend_report = {"day": day["day"], "remaining_usd_today": remaining, "calls_today": day["calls"], "max_calls_per_day": day["max_calls"]}
                if remaining is not None and remaining <= 0:
                    problems.append("daily answer spend cap reached (serving.answers.max_usd_per_day)")
                if day["calls"] >= day["max_calls"]:
                    problems.append("daily answer call cap reached (serving.answers.max_calls_per_day)")
                for provider, total in (usage.get("totals") or {}).items():
                    spend_report[f"remaining_usd_total:{provider}"] = total.get("remaining_usd")
                    if float(total.get("remaining_usd") or 0) <= 0:
                        problems.append(f"provider {provider} total spend cap reached (budgets.providers.{provider}.max_usd_total)")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"spend ledger unreadable ({type(exc).__name__})")
        out: dict[str, Any] = {"ready": not problems, "problems": problems}
        if missing:
            out["missing_keys"] = missing
        if spend_report is not None:
            out["spend"] = spend_report
        return out

    def dataset_health(dataset_id: str) -> JSONResponse:
        spec = profile.dataset(dataset_id)
        if spec is None:
            return JSONResponse({"ok": False, "dataset": dataset_id, "status": "unknown", "detail": f"unknown dataset '{dataset_id}'; valid: {sorted(served)}"}, status_code=404)
        if spec.retired:
            message = spec.retired_message or f"dataset '{dataset_id}' is not available on this server"
            return JSONResponse({"ok": False, "dataset": dataset_id, "status": "retired", "detail": message}, status_code=410)
        target = served[dataset_id]
        generation = generation_of(target)
        ready = readiness(target, generation)
        body = {"ok": generation is not None, "dataset": dataset_id, "status": "up" if generation else "down", "build_id": generation, "warmup": target.warmup, "ready": ready["ready"], "readiness": ready, "package_mode": "su_multi_arm", "package_version": __version__}
        if spec.unified.enabled:
            body["view"] = "unified"  # M2b-2 staged cutover (additive)
        return JSONResponse(body, status_code=200 if generation else 503)

    def all_health() -> JSONResponse:
        generations = {dataset_id: generation_of(target) for dataset_id, target in served.items()}
        ready = {dataset_id: readiness(target, generations[dataset_id]) for dataset_id, target in served.items()}
        ok = all(generations.values())
        per = {d.id: ({"status": "retired"} if d.retired else {"status": "up" if generations.get(d.id) else "down", "generation": generations.get(d.id), "warmup": served[d.id].warmup, "ready": ready[d.id]["ready"]}) for d in profile.datasets}
        body = {
            "ok": ok,
            "ready": all(r["ready"] for r in ready.values()),
            "build_id": generations.get(profile.default_dataset.id),
            "generations": generations,
            "datasets": per,
            "answers": {"in_flight": admission.active, "max_concurrent": admission.limit},
            "package_mode": "su_multi_arm",
            "package_version": __version__,
        }
        return JSONResponse(body, status_code=200 if ok else 503)

    def health_scope(request: Request) -> tuple[str | None, JSONResponse | None]:
        named = request.query_params.get("dataset")
        on_path = path_dataset(request)
        if on_path and named and named != on_path:
            return None, JSONResponse({"ok": False, "detail": f"dataset '{named}' disagrees with the dataset of the URL prefix '{on_path}'"}, status_code=422)
        return named or on_path, None

    @app.get("/health")
    async def health(request: Request):
        """Liveness (the gateway contract: 200 means up); ``ready`` reports readiness as well."""
        named, refused = health_scope(request)
        if refused is not None:
            return refused
        if named:
            return await light(dataset_health, named)
        return await light(all_health)

    def ready_body(named: str | None) -> JSONResponse:
        targets = served
        if named:
            spec = profile.dataset(named)
            if spec is None or spec.retired:
                return JSONResponse({"ready": False, "dataset": named, "problems": ["unknown or retired dataset"]}, status_code=404 if spec is None else 410)
            targets = {named: served[named]}
        report = {dataset_id: readiness(target, generation_of(target)) for dataset_id, target in targets.items()}
        ok = all(r["ready"] for r in report.values())
        return JSONResponse({"ready": ok, "datasets": report, "package_version": __version__}, status_code=200 if ok else 503)

    @app.get("/ready")
    async def ready(request: Request):
        """Readiness: 503 with the problems unless every (named) dataset can answer now."""
        named, refused = health_scope(request)
        if refused is not None:
            return refused
        return await light(ready_body, named)

    return app


def _request_id() -> str:
    return uuid.uuid4().hex[:16]
