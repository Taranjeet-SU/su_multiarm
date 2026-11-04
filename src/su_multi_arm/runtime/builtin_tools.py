"""Built-in read-only tools mapped onto engine operations (plan §3.4): ``search``,
``get_document``, ``list_values``, ``aggregate``.

Each tool is a ``Component`` of kind ``tool`` with ``input_model``/``output_model`` and runs inside
the caller's ``RunContext`` (pinned generation, principal, ACL, budget, trace). The engine supplies
an ``EngineOperations`` object through ``Dependencies.components["engine"]``; tools never write and
cannot be loaded from configuration (``tools.enabled`` only names these registered classes).
``evidence_ids`` lists the catalog records a tool returned, so a caller can cite them; memory is
never among them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from pydantic import Field

from ..core.component import Dependencies, Diagnostic, Environment
from ..core.types import Document, JsonScalar, ScopeResult, ScopeSpec
from ..models import SearchRequest, SearchResult, StrictModel, UnsupportedConstraint
from .tools import ToolRegistry, ToolResult

if TYPE_CHECKING:  # pragma: no cover
    from .context import RunContext


class EngineOperations(Protocol):
    """What the built-in tools need from the engine; every call is ACL-scoped by ``ctx``."""

    def search_in(self, ctx: RunContext, request: SearchRequest) -> SearchResult: ...

    def documents_in(self, ctx: RunContext, ids: list[str]) -> list[Document]: ...

    def values_in(self, ctx: RunContext, field: str, limit: int) -> list[str]: ...

    def aggregate_in(self, ctx: RunContext, spec: ScopeSpec, filters: dict[str, JsonScalar]) -> ScopeResult: ...


class ToolConfig(StrictModel):
    """Built-in tools take no configuration."""


class _BuiltinTool:
    kind: ClassVar[str] = "tool"
    config_model: ClassVar[type[StrictModel]] = ToolConfig
    description: ClassVar[str] = ""

    def __init__(self, config: StrictModel, deps: Dependencies):
        self.config = config
        self.engine: EngineOperations | None = deps.components.get("engine")

    def validate(self, env: Environment, probe: bool = False) -> list[Diagnostic]:
        if self.engine is None:
            return [Diagnostic(severity="error", code="engine_missing", message=f"tool {self.name}: engine operations not provided", component=self.name)]  # type: ignore[attr-defined]
        return []

    def recipe_fingerprint(self) -> str | None:
        return None

    def run(self, ctx: RunContext, inputs: Any) -> ToolResult:
        return self.invoke(ctx, inputs)  # type: ignore[attr-defined]


# --- search -------------------------------------------------------------------------------------


class SearchInput(StrictModel):
    query: str = Field(min_length=1, max_length=16000)
    filters: dict[str, str | int | float | bool] = Field(default_factory=dict)
    exclude_ids: list[str] = Field(default_factory=list)
    pipeline: str | None = None
    limit: int = Field(default=10, ge=1, le=50)


class ToolHit(StrictModel):
    id: str
    title: str
    source: str
    score: float
    arms: list[str]


class SearchOutput(StrictModel):
    generation: str
    hits: list[ToolHit]
    aggregates: int = 0
    unsupported_constraints: list[UnsupportedConstraint] = Field(default_factory=list)


class SearchTool(_BuiltinTool):
    name: ClassVar[str] = "search"
    description: ClassVar[str] = "Retrieve cited source records for a question (same pipeline as Engine.search)."
    input_model: ClassVar[type[StrictModel]] = SearchInput
    output_model: ClassVar[type[StrictModel]] = SearchOutput

    def invoke(self, ctx: RunContext, args: SearchInput) -> ToolResult:
        request = SearchRequest(query=args.query, filters=dict(args.filters), exclude_ids=list(args.exclude_ids), pipeline=args.pipeline)
        result = self.engine.search_in(ctx, request)  # type: ignore[union-attr]
        hits = [ToolHit(id=h.document_id, title=h.title, source=h.source, score=h.score, arms=list(h.arms)) for h in result.hits[: args.limit]]
        output = SearchOutput(generation=result.generation, hits=hits, aggregates=len(result.aggregates), unsupported_constraints=list(result.unsupported_constraints))
        return ToolResult(output=output, evidence_ids=[h.id for h in hits])


# --- get_document -------------------------------------------------------------------------------


class GetDocumentInput(StrictModel):
    ids: list[str] = Field(min_length=1, max_length=50)


class GetDocumentOutput(StrictModel):
    documents: list[Document]
    missing: list[str] = Field(default_factory=list)  # unknown or not visible to this principal (indistinguishable)


class GetDocumentTool(_BuiltinTool):
    name: ClassVar[str] = "get_document"
    description: ClassVar[str] = "Fetch catalog records by exact id within the caller's access scope."
    input_model: ClassVar[type[StrictModel]] = GetDocumentInput
    output_model: ClassVar[type[StrictModel]] = GetDocumentOutput

    def invoke(self, ctx: RunContext, args: GetDocumentInput) -> ToolResult:
        ids = list(dict.fromkeys(args.ids))
        found = {d.id: d for d in self.engine.documents_in(ctx, ids)}  # type: ignore[union-attr]
        documents = [found[i] for i in ids if i in found]
        output = GetDocumentOutput(documents=documents, missing=[i for i in ids if i not in found])
        return ToolResult(output=output, evidence_ids=[d.id for d in documents])


# --- list_values --------------------------------------------------------------------------------


class ListValuesInput(StrictModel):
    field: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    limit: int = Field(default=100, ge=1, le=1000)


class ListValuesOutput(StrictModel):
    field: str
    values: list[str]
    truncated: bool  # more distinct values exist than ``limit``; never silently cut


class ListValuesTool(_BuiltinTool):
    name: ClassVar[str] = "list_values"
    description: ClassVar[str] = "Distinct values of a listable metadata field within the caller's access scope."
    input_model: ClassVar[type[StrictModel]] = ListValuesInput
    output_model: ClassVar[type[StrictModel]] = ListValuesOutput

    def invoke(self, ctx: RunContext, args: ListValuesInput) -> ToolResult:
        values = self.engine.values_in(ctx, args.field, args.limit + 1)  # type: ignore[union-attr]
        output = ListValuesOutput(field=args.field, values=values[: args.limit], truncated=len(values) > args.limit)
        return ToolResult(output=output)


# --- aggregate ----------------------------------------------------------------------------------


class AggregateInput(StrictModel):
    scope: ScopeSpec
    filters: dict[str, str | int | float | bool] = Field(default_factory=dict)


class AggregateOutput(StrictModel):
    result: ScopeResult


class AggregateTool(_BuiltinTool):
    name: ClassVar[str] = "aggregate"
    description: ClassVar[str] = "Execute a typed ScopeSpec on the record store; returns exactness and denominator."
    input_model: ClassVar[type[StrictModel]] = AggregateInput
    output_model: ClassVar[type[StrictModel]] = AggregateOutput

    def invoke(self, ctx: RunContext, args: AggregateInput) -> ToolResult:
        result = self.engine.aggregate_in(ctx, args.scope, dict(args.filters))  # type: ignore[union-attr]
        return ToolResult(output=AggregateOutput(result=result), evidence_ids=[])


BUILTIN_TOOL_CLASSES: tuple[type, ...] = (SearchTool, GetDocumentTool, ListValuesTool, AggregateTool)


def builtin_tool_registry(enabled: list[str]) -> ToolRegistry:
    """A ``ToolRegistry`` holding exactly the enabled built-ins (config names only registered classes)."""
    from .log_tools import LOG_TOOL_CLASSES

    registry = ToolRegistry()
    known = {cls.name: cls for cls in (*BUILTIN_TOOL_CLASSES, *LOG_TOOL_CLASSES)}
    for name in enabled:
        registry.register(known[name])
    return registry


class ToolCall(StrictModel):
    """``Engine.call_tool`` result: the tool's ``ToolResult`` and the masked trace of the call."""

    tool: str
    result: ToolResult
    trace: dict[str, Any]
