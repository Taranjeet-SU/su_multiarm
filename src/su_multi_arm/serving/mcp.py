"""MCP server (plan §3.15) over the ``mcp`` SDK's ``FastMCP``; transports ``stdio`` and
``streamable_http``.

Tools mirror the HTTP surface and take the same models as the Python API:

* ``search`` / ``answer`` — the ``SearchRequest`` fields as arguments (typed ``filters`` values,
  ``exclude_ids``, ``record_context``, typed ``aggregate``, ``pipeline``, ``mode``). ``answer`` runs
  ``Engine.answer_stream`` and sends every event except the terminal one as a progress notification
  (``message`` = the event JSON, including provisional ``token`` deltas) when the client supplied a
  progress token; the tool result is the final ``AnswerResult``.
* ``get_document`` / ``aggregate`` — the read-only built-in tools (only when enabled in
  ``tools.enabled``); ``list_values`` — like ``GET /v2/values/{field}`` (``listable`` + ACL).
* ``session_get`` / ``session_delete`` — the engine session bound to this MCP session.
* ``capabilities`` — discovery: datasets, pipelines and modes, filterable/listable fields, tools.

Session binding: every ``search``/``answer``/tool call runs in an engine session bound to the MCP
session (the ``mcp-session-id`` of a streamable-HTTP connection; one id per stdio server process)
and to the calling principal; it is created on first use and re-created after it expired or was
deleted. Principals: stdio is the local operator (``LocalResolver``); streamable HTTP resolves every
HTTP request through the ``PrincipalResolver`` (``serving/auth.py``) before the MCP layer sees it.

Schema drift: at start-up every tool's generated input schema is compared field-for-field with the
model it mirrors (``serving.parity.tool_differences``); a drift refuses to start the server.
"""

import json
import threading
import uuid
from typing import Any, Literal

from ..core.errors import AccessDenied, BudgetExhausted, DeadlineExceeded, SessionConflict, SessionNotFound, WriteConflict, failure_category
from ..core.types import ScopeSpec
from ..models import Aggregate, SearchRequest
from ..pipeline.spec import RequestRejected
from ..runtime.acl import Principal
from ..runtime.builtin_tools import AggregateInput, GetDocumentInput, ListValuesInput
from ..runtime.events import DoneEvent, ErrorEvent
from .auth import LOCAL_PRINCIPAL, LocalResolver, PrincipalResolver, Unauthenticated, default_resolver
from .discovery import describe
from .parity import request_from_arguments, tool_differences

FilterValues = dict[str, str | int | float | bool]
Mode = Literal["rules", "classifier", "llm", "cascade"]
GENERIC = "request could not be completed; check configuration and server logs"

# Tool -> the model whose fields it mirrors (start-up drift check); ``None`` = takes no arguments.
TOOL_MODELS: dict[str, Any] = {
    "search": SearchRequest,
    "answer": SearchRequest,
    "get_document": GetDocumentInput,
    "list_values": ListValuesInput,
    "aggregate": AggregateInput,
    "session_get": None,
    "session_delete": None,
    "capabilities": None,
}
# The log tools (addendum A2 §4.5.1): one MCP tool per enabled log tool, arguments = the tool's
# input model field for field (``log_tools.py``); all read-only.
from ..runtime.log_tools import LOG_TOOL_CLASSES as _LOG_TOOL_CLASSES  # noqa: E402

LOG_TOOL_MODELS: dict[str, Any] = {cls.name: cls.input_model for cls in _LOG_TOOL_CLASSES}
TOOL_MODELS.update(LOG_TOOL_MODELS)


def safe_message(exc: BaseException) -> str:
    """What an MCP client sees for a failure: request-shaped problems verbatim, the rest generic."""
    from pydantic import ValidationError

    if isinstance(exc, (RequestRejected, ValidationError)):
        return str(exc)
    if isinstance(exc, SessionNotFound):
        return "unknown or expired session"
    if isinstance(exc, AccessDenied):
        return "session does not belong to this principal"
    if isinstance(exc, BudgetExhausted):
        return f"budget exhausted ({exc.kind})"
    if isinstance(exc, DeadlineExceeded):
        return "deadline exceeded"
    if isinstance(exc, (WriteConflict, SessionConflict)):  # typed, as HTTP 409; no backend text
        category, reason = failure_category(exc)
        return f"{category}: {reason}"
    return GENERIC


class SessionBinder:
    """MCP session id + principal -> engine session id (created on first use)."""

    def __init__(self, engine: Any):
        self.engine = engine
        self.stdio_key = "stdio-" + uuid.uuid4().hex
        self._bound: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def bind(self, principal: Principal, mcp_key: str) -> str:
        key = (principal.key(), mcp_key)
        with self._lock:
            session_id = self._bound.get(key)
            if session_id is not None:
                try:
                    self.engine.session(session_id, principal)
                    return session_id
                except SessionNotFound:
                    pass  # expired or deleted: a new engine session for the same MCP session
            session_id = self.engine.create_session(principal)
            self._bound[key] = session_id
            return session_id

    def current(self, principal: Principal, mcp_key: str) -> str | None:
        with self._lock:
            return self._bound.get((principal.key(), mcp_key))

    def forget(self, principal: Principal, mcp_key: str) -> None:
        with self._lock:
            self._bound.pop((principal.key(), mcp_key), None)


def create_mcp(
    config=None,
    *,
    engine=None,
    principal_resolver: PrincipalResolver | None = None,
    host: str = "127.0.0.1",
    port: int = 8100,
    streamable_http_path: str = "/mcp",
    allowed_hosts: list[str] | None = None,
):
    """The FastMCP server. ``engine`` reuses a constructed engine (Python injection);
    ``principal_resolver`` is used by ``mcp_http_app`` for the streamable-HTTP transport."""
    import anyio
    from mcp.server.fastmcp import Context, FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

    from ..engine import Engine

    if engine is None:
        if config is None:
            raise ValueError("create_mcp needs a config or an engine")
        engine = Engine(config)
    from ..runtime.acl import require_single_tenant

    require_single_tenant(engine.config, engine.acl)
    options: dict[str, Any] = {"streamable_http_path": streamable_http_path}
    if allowed_hosts is not None:  # mounted on the HTTP app: the SDK's DNS-rebinding protection
        from mcp.server.transport_security import TransportSecuritySettings

        options["transport_security"] = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=list(allowed_hosts), allowed_origins=[])
    server = FastMCP("su_multi_arm", host=host, port=port, **options)
    server.engine = engine  # type: ignore[attr-defined]
    server.principal_resolver = principal_resolver  # type: ignore[attr-defined]
    binder = SessionBinder(engine)
    server.session_binder = binder  # type: ignore[attr-defined]

    def caller(ctx: Context) -> tuple[Principal, str]:
        """The principal and the MCP session key of this tool call."""
        try:
            request = getattr(ctx.request_context, "request", None)
        except (ValueError, LookupError):  # called in-process (``server.call_tool``), not over a transport
            request = None
        if request is None:  # stdio or in-process
            return LOCAL_PRINCIPAL, binder.stdio_key
        principal = getattr(request.state, "principal", None)
        if principal is None:
            raise ToolError("unauthenticated")
        key = request.headers.get("mcp-session-id") or binder.stdio_key
        return principal, "http-" + key

    def in_request(ctx: Context) -> bool:
        try:
            return ctx.request_context is not None
        except (ValueError, LookupError):
            return False

    def require(principal: Principal, scope: str) -> None:
        if not principal.has_scope(scope):
            raise ToolError(f"principal lacks the {scope!r} scope")

    async def run(fn, *args, **kwargs):
        try:
            return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - mapped to a safe message, never echoed raw
            raise ToolError(safe_message(exc)) from exc

    async def bound(ctx: Context, scope: str) -> tuple[Principal, str]:
        principal, key = caller(ctx)
        require(principal, scope)
        return principal, await run(binder.bind, principal, key)

    @server.tool()
    async def search(
        query: str,
        ctx: Context,
        filters: FilterValues | None = None,
        exclude_ids: list[str] | None = None,
        record_context: dict[str, str] | None = None,
        aggregate: Aggregate | None = None,
        pipeline: str | None = None,
        mode: Mode | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve cited source documents from the configured dataset (same request model as HTTP)."""
        request = request_from_arguments(query=query, filters=filters, exclude_ids=exclude_ids, record_context=record_context, aggregate=aggregate, pipeline=pipeline, mode=mode, as_of=as_of)
        principal, session_id = await bound(ctx, "search")
        result = await run(engine.search, request, principal, session_id)
        return result.model_dump(mode="json")

    @server.tool()
    async def answer(
        query: str,
        ctx: Context,
        filters: FilterValues | None = None,
        exclude_ids: list[str] | None = None,
        record_context: dict[str, str] | None = None,
        aggregate: Aggregate | None = None,
        pipeline: str | None = None,
        mode: Mode | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """Answer using configured models and grounded evidence; optional record context is data.
        Progress notifications carry the stream events (``token`` deltas are provisional);
        ``unsupported_constraints`` lists what the answer could not express exactly."""
        request = request_from_arguments(query=query, filters=filters, exclude_ids=exclude_ids, record_context=record_context, aggregate=aggregate, pipeline=pipeline, mode=mode, as_of=as_of)
        principal, session_id = await bound(ctx, "answer")
        stream = await run(engine.answer_stream, request, principal, session_id)
        try:
            while True:
                event = await anyio.to_thread.run_sync(stream.next_event, 1.0, abandon_on_cancel=True)
                if event is None:
                    continue
                if isinstance(event, DoneEvent):
                    return event.result.model_dump(mode="json")
                if isinstance(event, ErrorEvent):
                    raise ToolError(f"{event.category}: {event.message if event.category in {'rejected', 'invalid_request'} else GENERIC}")
                if in_request(ctx):
                    await ctx.report_progress(event.seq, None, json.dumps(event.model_dump(mode="json"), ensure_ascii=False))
        finally:
            stream.cancel("client_disconnected")

    if "get_document" in engine.tools.names():

        @server.tool()
        async def get_document(ids: list[str], ctx: Context) -> dict[str, Any]:
            """Fetch catalog records by exact id within the caller's access scope."""
            principal, session_id = await bound(ctx, "tools")
            return (await run(engine.call_tool, "get_document", {"ids": ids}, principal, session_id)).model_dump(mode="json")

    if "aggregate" in engine.tools.names():

        @server.tool()
        async def aggregate(scope: ScopeSpec, ctx: Context, filters: FilterValues | None = None) -> dict[str, Any]:
            """Execute a typed ScopeSpec on the record store; returns exactness and denominator."""
            principal, session_id = await bound(ctx, "tools")
            args = {"scope": scope.model_dump(mode="json"), **({"filters": filters} if filters else {})}
            return (await run(engine.call_tool, "aggregate", args, principal, session_id)).model_dump(mode="json")

    @server.tool()
    async def list_values(field: str, ctx: Context, limit: int = 50) -> dict[str, Any]:
        """Distinct values of a listable metadata field within the caller's access scope."""
        principal, _ = caller(ctx)
        require(principal, "search")
        page_max = engine.config.serving.http.page_size_max
        if limit < 1 or limit > page_max:
            raise ToolError(f"limit must be between 1 and serving.http.page_size_max ({page_max})")
        return await run(engine.list_values, field, limit, principal)

    @server.tool()
    async def session_get(ctx: Context) -> dict[str, Any]:
        """The engine session bound to this MCP session (turn hashes and IDs, usage)."""
        principal, key = caller(ctx)
        session_id = await run(binder.bind, principal, key)
        return (await run(engine.session, session_id, principal)).record.model_dump(mode="json")

    @server.tool()
    async def session_delete(ctx: Context) -> dict[str, Any]:
        """Close the engine session bound to this MCP session; the next call starts a new one."""
        principal, key = caller(ctx)
        session_id = binder.current(principal, key)
        if session_id is None:
            return {"closed": None}
        try:
            await run(engine.close_session, session_id, principal)
        finally:
            binder.forget(principal, key)
        return {"closed": session_id}

    @server.tool()
    async def capabilities(ctx: Context) -> dict[str, Any]:
        """Datasets, selectable pipelines and modes, filterable/listable fields, enabled tools."""
        principal, _ = caller(ctx)
        require(principal, "search")
        return describe(engine)

    if engine.config.serving.mcp.log_tools:
        for name in [n for n in engine.tools.names() if n in LOG_TOOL_MODELS]:
            _add_log_tool(server, engine, name, bound, run)
    check_drift(server)
    return server


def _add_log_tool(server, engine, name: str, bound, run) -> None:
    """Register one log tool with a signature generated from its input model (so the drift check
    compares it field for field) and the read-only annotations."""
    import inspect

    from mcp.server.fastmcp import Context
    from mcp.types import ToolAnnotations

    model = LOG_TOOL_MODELS[name]
    params = []
    annotations: dict[str, Any] = {}
    for field_name, info in model.model_fields.items():
        default = inspect.Parameter.empty if info.is_required() else (None if info.default_factory is not None else info.default)
        annotation = info.annotation if info.is_required() or default is not None else (info.annotation | None)
        params.append(inspect.Parameter(field_name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation))
        annotations[field_name] = annotation
    params.append(inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context))
    annotations["ctx"] = Context

    async def tool(**kwargs: Any) -> dict[str, Any]:
        ctx = kwargs.pop("ctx")
        principal, session_id = await bound(ctx, "tools")
        args = {k: (v.model_dump(mode="json") if hasattr(v, "model_dump") else v) for k, v in kwargs.items() if v is not None}
        call = await run(engine.call_tool, name, args, principal, session_id)
        return call.result.output.model_dump(mode="json")

    tool.__signature__ = inspect.Signature(params, return_annotation=dict[str, Any])  # type: ignore[attr-defined]
    tool.__annotations__ = {**annotations, "return": dict[str, Any]}
    tool.__name__ = name
    description = engine.tools.get(name).description
    server.add_tool(tool, name=name, description=description, annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False), structured_output=True)


def check_drift(server) -> None:
    """Refuse to start when a tool's input schema drifted from the model it mirrors."""
    for tool in server._tool_manager.list_tools():
        if tool.name not in TOOL_MODELS:
            raise RuntimeError(f"MCP tool {tool.name!r} has no declared request model")
        model = TOOL_MODELS[tool.name]
        if model is None:
            if tool.parameters.get("properties"):
                raise RuntimeError(f"MCP tool {tool.name!r} must take no arguments")
            continue
        drift = tool_differences(tool.parameters, model)
        if drift:
            raise RuntimeError(f"MCP tool {tool.name!r} drifted from {model.__name__}: {'; '.join(drift)}")


class PrincipalMiddleware:
    """Resolves the principal of every HTTP request of the streamable-HTTP transport (401 when the
    resolver refuses) and stores it on ``request.state.principal`` for the tools."""

    def __init__(self, app, resolver: PrincipalResolver):
        self.app, self.resolver = app, resolver

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            from starlette.datastructures import Headers
            from starlette.responses import JSONResponse

            try:
                principal = self.resolver.resolve(Headers(scope=scope))
            except Unauthenticated:
                response = JSONResponse({"detail": "Invalid bearer token"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
                await response(scope, receive, send)
                return
            scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)


def mcp_http_app(server, token_env: str | None = None, bind: str | None = None):
    """The streamable-HTTP ASGI app (path ``/mcp``) behind the principal resolver and the Host/Origin
    guard (active when ``serving.allowed_hosts`` is set)."""
    from .hosts import HostGuard, guard_hosts

    engine = server.engine
    if "streamable_http" not in engine.config.serving.mcp.transports:
        raise RuntimeError("serving.mcp.transports does not enable streamable_http")
    resolver = server.principal_resolver or default_resolver(engine.config, token_env)
    serving = engine.config.serving
    return HostGuard(PrincipalMiddleware(server.streamable_http_app(), resolver), guard_hosts(bind or serving.http.bind, serving.allowed_hosts))


def run_stdio(server) -> None:
    if "stdio" not in server.engine.config.serving.mcp.transports:
        raise RuntimeError("serving.mcp.transports does not enable stdio")
    server.run(transport="stdio")


__all__ = ["LocalResolver", "PrincipalMiddleware", "TOOL_MODELS", "check_drift", "create_mcp", "mcp_http_app", "run_stdio", "safe_message"]
