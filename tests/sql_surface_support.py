"""Shared synthetic world for the SQL-surface tests (typed_support domain; no real data).

``world()`` builds the memory backend with records and a sealed projection; ``make_arm`` binds a
``typed_query`` arm in ``mode: sql_surface``; ``FakeRole`` is a scripted ``JsonModel`` stand-in
(``call(system, payload, schema, budget, stage)``) that records every payload.
"""

from __future__ import annotations

from typing import Any

from su_multi_arm.arms.typed_query import TypedQueryArm, TypedQueryRuntime
from su_multi_arm.backends.memory import MemoryBackend
from su_multi_arm.core.component import Dependencies
from su_multi_arm.core.config_projection import ProjectionConfig
from su_multi_arm.core.types import GenerationId, Predicate
from su_multi_arm.projection import ProjectionBuilder, ProjectionSpec
from su_multi_arm.providers.base import SchemaViolation
from su_multi_arm.runtime.context import new_context

import typed_support as ts

GEN = GenerationId(dataset="demo", generation="d" * 32, index_format=3, fingerprint="e" * 64)
OTHER = GEN.model_copy(update={"generation": "9" * 32})


def tenant(value: str) -> list[Predicate]:
    return [Predicate(field="tenant", op="eq", value=value)]


def world() -> tuple[MemoryBackend, Any, dict[str, bytes]]:
    schema = ts.schema()
    backend = MemoryBackend()
    docs = ts.documents()
    backend.records.upsert(GEN, ts.records(docs), schema)
    spec = ProjectionSpec.build(ProjectionConfig.model_validate(ts.PROJECTION), schema, "tenant")
    builder = ProjectionBuilder(spec, schema)
    builder.add(ts.records(docs))
    files, manifest = builder.seal(GEN)
    ProjectionBuilder.write(backend.relational, GEN, files, manifest)
    return backend, schema, files


def make_arm(world_: tuple, *, mode: str = "sql_surface", backend: MemoryBackend | None = None, **limits: Any) -> TypedQueryArm:
    store, schema, _ = world_
    store = backend or store
    surface = limits.pop("sql_surface", {})
    config = TypedQueryRuntime(
        backend="m",
        records="m",
        projection=ProjectionConfig.model_validate(ts.PROJECTION),
        access_column="tenant",
        mode=mode,
        sql_surface=surface,
        **{"row_limit": 50, "max_groups": 100, **limits},
    )
    return TypedQueryArm(config, Dependencies(backends={"m": store}, components={"catalog": store.catalog}, metadata_schema=schema))


def context(gen: GenerationId = GEN):
    return new_context(gen)


class FakeRole:
    """A scripted structured-output model: each reply is a dict (validated against the schema) or
    an exception instance to raise."""

    def __init__(self, *replies: Any):
        self.replies = list(replies)
        self.payloads: list[str] = []
        self.systems: list[str] = []

    def call(self, system: str, payload: Any, schema: type, budget: Any, stage: str) -> Any:
        budget.reserve()
        self.systems.append(system)
        self.payloads.append(payload)
        if not self.replies:
            raise AssertionError("unexpected model call")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        try:
            return schema.model_validate(reply)
        except Exception as error:  # noqa: BLE001 - as providers do: an invalid reply is a SchemaViolation
            raise SchemaViolation(str(error), raw=str(reply), stage=stage) from None


def answer(sql: str, thought: str = "count rows, because asked") -> dict[str, Any]:
    return {"thought": thought, "answerability": "answerable", "gaps": [], "sql_code": sql, "response": "A synthetic summary."}


def not_answerable(*gaps: str) -> dict[str, Any]:
    return {"thought": "no column carries it", "answerability": "not_answerable", "gaps": list(gaps), "sql_code": None, "response": ""}
