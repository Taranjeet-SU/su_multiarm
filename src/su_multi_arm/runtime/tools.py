"""Tools: read-only, deterministic components exposed to decision agents and MCP (plan §3.4).

A tool is a ``Component`` of kind ``tool`` with ``input_model``/``output_model``; ``invoke`` returns
``ToolResult{output, evidence_ids, trace_span}``. Arms, ``aggregate``, ``lookup_record``,
``list_values`` and ``resolve_alias`` are tools (ported in later slices). Tools never write.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from pydantic import Field

from ..core.component import Dependencies, Diagnostic, Environment
from ..core.errors import RegistryError
from ..core.registry import Registry
from ..models import StrictModel

if TYPE_CHECKING:  # pragma: no cover
    from .context import RunContext


class ToolResult(StrictModel):
    output: Any
    evidence_ids: list[str] = Field(default_factory=list)
    trace_span: str | None = None


@runtime_checkable
class Tool(Protocol):
    name: ClassVar[str]
    kind: ClassVar[str]
    config_model: ClassVar[type[StrictModel]]
    input_model: ClassVar[type[StrictModel]]
    output_model: ClassVar[type[StrictModel]]
    description: ClassVar[str]

    def __init__(self, config: StrictModel, deps: Dependencies): ...

    def validate(self, env: Environment, probe: bool = False) -> list[Diagnostic]: ...

    def run(self, ctx: RunContext, inputs: Any) -> Any: ...

    def recipe_fingerprint(self) -> str | None: ...

    def invoke(self, ctx: RunContext, args: StrictModel) -> ToolResult: ...


class ToolRegistry(Registry[Any]):
    """``Registry`` for kind ``tool`` that also requires input/output models and ``invoke``."""

    def __init__(self):
        super().__init__("tool")

    def register(self, cls):
        for attribute in ("input_model", "output_model"):
            model = getattr(cls, attribute, None)
            if not (isinstance(model, type) and issubclass(model, StrictModel)):
                raise RegistryError(f"tool {getattr(cls, 'name', cls)!r} must declare {attribute} as a StrictModel")
        if not callable(getattr(cls, "invoke", None)):
            raise RegistryError(f"tool {getattr(cls, 'name', cls)!r} must implement invoke(ctx, args)")
        return super().register(cls)

    def schemas(self) -> dict[str, dict[str, Any]]:
        """JSON schemas for MCP / decision-agent exposure, same registry, same contract."""
        return {
            name: {
                "description": getattr(self.get(name), "description", ""),
                "input": self.get(name).input_model.model_json_schema(),
                "output": self.get(name).output_model.model_json_schema(),
            }
            for name in self.names()
        }


def invoke_tool(ctx: RunContext, tool: Any, args: StrictModel) -> ToolResult:
    """Run a tool inside a trace span and check its result type (no writes are possible here)."""
    with ctx.trace.span(f"tool.{tool.name}", tool=tool.name) as span:
        result = tool.invoke(ctx, args)
        if not isinstance(result, ToolResult):
            raise TypeError(f"tool {tool.name} must return ToolResult")
        span.set(evidence_ids=len(result.evidence_ids))
        return result.model_copy(update={"trace_span": span.id})
