"""Structured arm: executes a typed ``ScopeSpec`` through ``RecordStore`` and returns the
``ScopeResult`` exactness metadata as an aggregate record. Today's typed aggregates (count,
group_count, first_seen) map onto ``ScopeSpec`` in the engine; nothing here builds SQL from text."""

from __future__ import annotations

from typing import Any, ClassVar

from ..core.config import StructuredArm as StructuredArmConfig
from ..core.types import Predicate, ScopeSpec
from ..runtime.context import RunContext
from .base import ArmInput, ArmResult, BaseArm

BUILTIN_FIELDS = frozenset({"id", "source", "dataset", "kind"})  # document identity, always queryable


def operation_name(spec: ScopeSpec) -> tuple[str, str | None]:
    if spec.group_by:
        return "group_count", spec.group_by[0]
    if spec.measure is None or spec.measure.kind == "count":
        return "count", None
    return spec.measure.kind, spec.measure.field


class StructuredArm(BaseArm):
    name: ClassVar[str] = "structured"
    config_model: ClassVar[type] = StructuredArmConfig
    capability: ClassVar[str] = "records"

    def allowed_fields(self) -> set[str]:
        declared = {f.name for f in self.schema.fields} if self.schema is not None else set()
        return declared | BUILTIN_FIELDS

    def check_fields(self, spec: ScopeSpec) -> None:
        allowed = self.allowed_fields()
        named = [p.field for p in spec.predicates] + list(spec.group_by)
        if spec.measure is not None and spec.measure.field:
            named.append(spec.measure.field)
        for field in named:
            if field not in allowed:
                raise ValueError(f"aggregate field {field!r} is not allowlisted (declare it in metadata_schema)")

    def _run(self, ctx: RunContext, inputs: ArmInput) -> ArmResult:
        spec = inputs.aggregate
        if spec is None:
            return ArmResult(arm=self.name)
        self.check_fields(spec)
        scope = inputs.scope
        constraints: list[Predicate] = [*inputs.acl, *scope.predicates]
        if scope.exclude_ids:
            constraints.append(Predicate(field="id", op="in", value=list(scope.exclude_ids), negate=True))
        if scope.document_ids is not None:
            constraints.append(Predicate(field="id", op="in", value=list(scope.document_ids)))
        result = self._facet().execute(ctx.generation, spec, constraints)
        operation, field = operation_name(spec)
        aggregate: dict[str, Any] = {
            "operation": operation,
            "field": field,
            "value": result.value,
            "missing_or_invalid": result.missing_or_invalid,
            "document_ids": result.matched_ids,
            "scope": "filtered canonical records in this snapshot",
            "exactness": result.exactness,
            "denominator": result.denominator,
            "zero_match": result.zero_match,
            "unsupported": [u.model_dump(mode="json") for u in result.unsupported],
            "groups": [g.model_dump(mode="json") for g in result.groups],
        }
        return ArmResult(arm=self.name, aggregates=[aggregate])
