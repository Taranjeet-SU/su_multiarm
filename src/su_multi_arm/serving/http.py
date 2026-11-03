"""HTTP surface (plan §3.15) on FastAPI. Request bodies **are** the Python models (``SearchRequest``;
tool inputs are the tools' ``input_model``s); responses are the Python results' JSON.

| Route | What |
|---|---|
| ``POST /v2/search`` (v1 alias ``/search``) | ``Engine.search`` → ``SearchResult`` |
| ``POST /v2/answer`` (v1 alias ``/answer``) | ``Engine.answer`` → ``AnswerResult`` |
| ``POST /v2/answer/stream`` | ``Engine.answer_stream`` as server-sent events (``serving/sse.py``) |
| ``POST /v2/sessions``; ``GET``/``DELETE /v2/sessions/{id}`` | create / show / close a session of this principal |
| ``GET /v2/tools``; ``POST /v2/tools/{name}`` | enabled read-only tools; body = the tool's input model |
| ``POST /v2/actions/{name}`` | allowlisted write action (``serving.actions.allow`` + ``actions`` scope), audited |
| ``GET /v2/values/{field}?limit=`` | distinct values of a ``listable`` field within the ACL scope |
| ``GET /v2/capabilities`` | pipelines, modes, fields, tools (``serving/discovery.py``) |
| ``GET /health`` | liveness (no backend access) |
| ``GET /ready`` | every backend probed + provider capability pairs + active generation; 503 when not ready |

Headers: ``Authorization`` (``PrincipalResolver``, ``serving/auth.py``), ``X-Session-Id`` (binds
search/answer/tools to a stored session of the principal), ``X-Record-Context`` (JSON; must agree
with the body). Errors: 401 unauthenticated, 403 missing scope / foreign session / denied action,
404 unknown session or tool, 422 rejected or invalid request (generic detail; no provider, source or
config text reaches the client), 429 budget exhausted, 499 run cancelled, 502 model provider failure,
503 backend failure / generation disagreement / no usable index, 504 deadline exceeded.
"""

import json
from typing import Any

from pydantic import Field, ValidationError

from ..core.errors import (
    AccessDenied,
    BackendError,
    BudgetExhausted,
    DeadlineExceeded,
    GenerationDisagreement,
    IndexNotReady,
    RunCancelled,
    SessionNotFound,
)
from ..models import SearchRequest, StrictModel
from ..pipeline.spec import RequestRejected
from ..providers.base import ProviderError
from ..runtime.acl import Principal
from .auth import PrincipalResolver, Unauthenticated, default_resolver
from .discovery import describe
from .sse import SseResponse, event_source

GENERIC = "Request could not be completed; check configuration and server logs"
UNAVAILABLE = "Search backend unavailable; check server logs"
UPSTREAM = "Model provider request failed; check server logs"


class ActionBody(StrictModel):
    args: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=2000)


def http_error(exc: BaseException):
    """The HTTP status for an engine failure; details only for request-shaped problems."""
    from fastapi import HTTPException

    if isinstance(exc, SessionNotFound):
        return HTTPException(status_code=404, detail="unknown or expired session")
    if isinstance(exc, AccessDenied):
        return HTTPException(status_code=403, detail="session does not belong to this principal")
    if isinstance(exc, BudgetExhausted):
        return HTTPException(status_code=429, detail=f"budget exhausted ({exc.kind})")
    if isinstance(exc, DeadlineExceeded):
        return HTTPException(status_code=504, detail="deadline exceeded")
    if isinstance(exc, RunCancelled):
        return HTTPException(status_code=499, detail="run cancelled")
    if isinstance(exc, (BackendError, GenerationDisagreement, IndexNotReady)):
        return HTTPException(status_code=503, detail=UNAVAILABLE)  # dependency/state failure, not the client's
    if isinstance(exc, ProviderError):
        return HTTPException(status_code=502, detail=UPSTREAM)
    if isinstance(exc, RequestRejected):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, ValidationError):
        return HTTPException(status_code=422, detail=json.loads(exc.json(include_url=False, include_context=False, include_input=False)))
    return HTTPException(status_code=422, detail=GENERIC)


def create_app(
    config=None,
    token_env: str | None = None,
    *,
    engine=None,
    principal_resolver: PrincipalResolver | None = None,
    heartbeat_seconds: float = 15.0,
):
    """The FastAPI app. ``engine`` (Python injection) reuses a constructed engine; otherwise one is
    built from ``config``. ``principal_resolver`` replaces the default bearer/local resolver."""
    from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool

    from .. import __version__
    from ..engine import Engine

    if engine is None:
        if config is None:
            raise ValueError("create_app needs a config or an engine")
        engine = Engine(config)
    resolver = principal_resolver or default_resolver(engine.config, token_env)
    app = FastAPI(title="su_multi_arm", version=__version__)
    app.state.engine = engine
    page_max = engine.config.serving.http.page_size_max

    def principal(request: Request) -> Principal:
        try:
            return resolver.resolve(request.headers)
        except Unauthenticated as exc:
            raise HTTPException(status_code=401, detail="Invalid bearer token", headers={"WWW-Authenticate": "Bearer"}) from exc

    def scoped(scope: str):
        def check(who: Principal = Depends(principal)) -> Principal:
            if not who.has_scope(scope):
                raise HTTPException(status_code=403, detail=f"principal lacks the {scope!r} scope")
            return who

        return check

    def merge_context(request: SearchRequest, header: str | None) -> SearchRequest:
        if not header:
            return request
        if len(header.encode()) > 8192:
            raise HTTPException(status_code=422, detail="record context header too large")
        try:
            context = json.loads(header)
            if request.record_context and context != request.record_context:
                raise ValueError("body/header record_context conflict")
            return SearchRequest.model_validate({**request.model_dump(), "record_context": context})
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail="Invalid or conflicting record_context") from exc

    async def call(method, *args, **kwargs):
        try:
            return await run_in_threadpool(method, *args, **kwargs)
        except HTTPException:
            raise
        except (ValueError, RuntimeError, KeyError, PermissionError) as exc:
            raise http_error(exc) from exc

    # --- liveness / readiness / discovery -------------------------------------------------------

    @app.get("/health")
    def health():
        return {"status": "ok", "dataset": engine.config.dataset}

    @app.get("/ready")
    async def ready():
        from ..pipeline.validate import probe_report

        report = await run_in_threadpool(probe_report, engine)
        generation = engine._current_generation()
        ok = report["probe"] == "ok" and generation is not None
        body = {
            "status": "ready" if ok else "not_ready",
            "generation": generation,
            "backends": {name: {k: v for k, v in entry.items() if k in {"kind", "status", "category"}} for name, entry in report["backends"].items()},
            "models": {
                role: {k: entry.get(k) for k in ("provider", "model", "declared", "registry", "api_key_env_set", "error") if k in entry}
                for role, entry in report["models"].items()
            },
        }
        if generation is None:
            body["reason"] = "no active generation"
        return JSONResponse(body, status_code=200 if ok else 503)

    @app.get("/v2/capabilities")
    def capabilities(who: Principal = Depends(scoped("search"))):
        return describe(engine)

    # --- search / answer --------------------------------------------------------------------------

    @app.post("/v2/search")
    @app.post("/search")
    async def search(
        request: SearchRequest,
        x_record_context: str | None = Header(default=None),
        x_session_id: str | None = Header(default=None),
        who: Principal = Depends(scoped("search")),
    ):
        return await call(engine.search, merge_context(request, x_record_context), who, x_session_id)

    @app.post("/v2/answer")
    @app.post("/answer")
    async def answer(
        request: SearchRequest,
        x_record_context: str | None = Header(default=None),
        x_session_id: str | None = Header(default=None),
        who: Principal = Depends(scoped("answer")),
    ):
        return await call(engine.answer, merge_context(request, x_record_context), who, x_session_id)

    @app.post("/v2/answer/stream")
    async def answer_stream(
        request: SearchRequest,
        x_record_context: str | None = Header(default=None),
        x_session_id: str | None = Header(default=None),
        who: Principal = Depends(scoped("answer")),
    ):
        stream = await call(engine.answer_stream, merge_context(request, x_record_context), who, x_session_id)
        return SseResponse(event_source(stream, heartbeat_seconds))

    # --- sessions -------------------------------------------------------------------------------

    @app.post("/v2/sessions")
    async def create_session(who: Principal = Depends(principal)):
        return {"session_id": await call(engine.create_session, who)}

    @app.get("/v2/sessions/{session_id}")
    async def get_session(session_id: str, who: Principal = Depends(principal)):
        session = await call(engine.session, session_id, who)
        return session.record.model_dump(mode="json")

    @app.delete("/v2/sessions/{session_id}")
    async def delete_session(session_id: str, who: Principal = Depends(principal)):
        await call(engine.close_session, session_id, who)
        return {"closed": session_id}

    # --- tools, actions, values -----------------------------------------------------------------

    @app.get("/v2/tools")
    def tools(who: Principal = Depends(scoped("tools"))):
        return {"tools": [{"name": n, "input_schema": engine.tools.get(n).input_model.model_json_schema()} for n in engine.tools.names()]}

    @app.post("/v2/tools/{name}")
    async def tool(name: str, args: dict[str, Any], x_session_id: str | None = Header(default=None), who: Principal = Depends(scoped("tools"))):
        if name not in engine.tools.names():
            raise HTTPException(status_code=404, detail=f"tool {name!r} is not enabled")
        return await call(engine.call_tool, name, args, who, x_session_id)

    @app.post("/v2/actions/{name}")
    async def action(name: str, body: ActionBody, who: Principal = Depends(principal)):
        result = await call(engine.run_action, name, body.args, idempotency_key=body.idempotency_key, principal=who, reason=body.reason, surface="http")
        status = 403 if result.result.status == "denied" else 200
        return JSONResponse(result.model_dump(mode="json"), status_code=status)

    @app.get("/v2/values/{field}")
    async def values(field: str, limit: int = Query(default=50, ge=1), who: Principal = Depends(scoped("search"))):
        if limit > page_max:
            raise HTTPException(status_code=422, detail=f"limit exceeds serving.http.page_size_max ({page_max})")
        return await call(engine.list_values, field, limit, who)

    return app

