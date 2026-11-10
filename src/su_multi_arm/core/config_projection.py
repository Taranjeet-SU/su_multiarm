"""Typed projection configuration (typed-query plan §5.2, §5.8, §6; MVP M3).

``projection`` declares a per-generation relational projection of the canonical records: one table
per document ``kind`` (key = document id), columns that name declared ``metadata_schema`` fields,
and at most the declared ``many_to_one`` relations. Everything is opt-in: ``enabled: false`` (the
default) leaves the index recipe, the write path and every request unchanged. Cross-key rules that
need the metadata schema, the access policy or the backends are in ``projection_problems`` (called by
``pipeline/validate.py``). Not its job: building or reading the projection (``projection/``).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ConfigDict, Field, model_validator

from ..models import StrictModel

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config

NAME = r"^[a-z][a-z0-9_]{0,40}$"
KEY = "id"  # every table's key: the document id
RESERVED = frozenset({KEY, "_rejected"})  # projection-internal column names
TYPED_ARM = "typed_query"


TEXT_SOURCE = r"^(?:title|fields\.[A-Za-z0-9_][A-Za-z0-9_.-]{0,63})$"
MAX_TEXT_COLUMNS = 16


class ProjectionTextColumn(StrictModel):
    """A free-text column (2026-11-09 corpus-B fix F6): the document's ``title`` or one of its text
    ``fields.<name>`` copied verbatim into the projection, so the SQL surface can match mentions with
    ``LIKE``/``instr`` (never ``=``, never grouped, sorted or enumerated; no values reach a prompt).
    ``description`` is the reviewed planner-visible meaning (required)."""

    source: str = Field(pattern=TEXT_SOURCE)
    description: str = Field(min_length=1, max_length=1000)


class ProjectionTable(StrictModel):
    """One table: the records of one document ``kind``. ``columns`` name declared metadata fields
    (the key ``id`` is implicit). ``descriptions`` overrides ``FieldSpec.description`` per column;
    every column needs a reviewed description from one of the two (planner-visible meaning, §5.8).
    ``text_columns`` (F6): free-text columns sourced from the document title or text fields."""

    kind: str = Field(min_length=1, max_length=128)
    columns: list[str] = Field(default_factory=list, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    descriptions: dict[str, str] = Field(default_factory=dict)
    text_columns: dict[str, ProjectionTextColumn] = Field(default_factory=dict)

    @model_validator(mode="after")
    def coherent(self):
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("projection table columns must be unique")
        if len(self.text_columns) > MAX_TEXT_COLUMNS:
            raise ValueError(f"at most {MAX_TEXT_COLUMNS} text_columns per table")
        for column in self.text_columns:
            if not re.fullmatch(NAME, column) or column in RESERVED or column in self.columns:
                raise ValueError(f"text column {column!r} must be an identifier other than {sorted(RESERVED)} and the table's columns")
        for column in self.columns:
            if not re.fullmatch(NAME, column) or column in RESERVED:
                raise ValueError(f"column {column!r} must be an identifier other than {sorted(RESERVED)}")
        unknown = sorted(set(self.descriptions) - set(self.columns) - {KEY})
        if unknown:
            raise ValueError(f"descriptions name columns not in the table: {unknown}")
        if any(not text.strip() or len(text) > 1000 for text in self.descriptions.values()):
            raise ValueError("a column description is non-empty text of at most 1000 characters")
        return self


class ProjectionRelation(StrictModel):
    """``from: <child table>.<column>`` -> ``to: <parent table>.id``; cardinality is declared, never
    inferred, and ``many_to_one`` is the only one offered (outer joins are not). ``from`` is a Python
    keyword, so the attribute is ``from_``; it is dumped as ``from`` so a dumped config validates again."""

    model_config = ConfigDict(extra="forbid", serialize_by_alias=True)

    from_: str = Field(alias="from", pattern=r"^[a-z][a-z0-9_]{0,40}\.[a-z][a-z0-9_]{0,40}$")
    to: str = Field(pattern=r"^[a-z][a-z0-9_]{0,40}\.id$")
    cardinality: Literal["many_to_one"]
    description: str = Field(min_length=1, max_length=1000)

    def child(self) -> tuple[str, str]:
        table, column = self.from_.split(".")
        return table, column

    def parent(self) -> str:
        return self.to.split(".")[0]


class ProjectionConfig(StrictModel):
    enabled: bool = False
    backend: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")  # a sqlite backend serving ``relational``
    tables: dict[str, ProjectionTable] = Field(default_factory=dict)
    relations: dict[str, ProjectionRelation] = Field(default_factory=dict)

    @model_validator(mode="after")
    def coherent(self):
        for name in [*self.tables, *self.relations]:
            if not re.fullmatch(NAME, name):
                raise ValueError(f"projection name {name!r} must be an identifier of at most 41 characters")
        if not self.enabled:
            return self
        if self.backend is None:
            raise ValueError("projection.enabled needs projection.backend (a declared sqlite backend)")
        if not self.tables:
            raise ValueError("projection.enabled needs at least one table")
        kinds = [t.kind for t in self.tables.values()]
        if len(set(kinds)) != len(kinds):
            raise ValueError("each document kind maps to one projection table")
        if len(self.relations) > 8:
            raise ValueError("at most 8 declared relations")
        for name, relation in self.relations.items():
            child, column = relation.child()
            parent = relation.parent()
            if child not in self.tables or parent not in self.tables:
                raise ValueError(f"relation {name}: {relation.from_} -> {relation.to} names an undeclared table")
            if column not in self.tables[child].columns:
                raise ValueError(f"relation {name}: {column!r} is not a column of {child!r}")
            if child == parent:
                raise ValueError(f"relation {name}: self joins are not offered")
        return self

    def recipe(self) -> dict[str, Any]:
        """The index-recipe part (only entered while enabled)."""
        return self.model_dump(mode="json", by_alias=True)


def projection_problems(config: Config) -> list[tuple[str, str]]:
    """``(path, message)`` for every cross-key problem of ``projection`` and the ``typed_query`` arm."""
    from ..backends import capabilities as caps

    out: list[tuple[str, str]] = []
    projection = config.projection
    arms = [(name, p) for name, p in config.all_pipelines().items() if p.arms.typed_query is not None]
    for name, pipeline in config.all_pipelines().items():
        if pipeline.decision.typed_query.enabled and pipeline.arms.typed_query is None:
            prefix = "pipeline" if name == "default" else f"pipelines.{name}"
            out.append((f"{prefix}.decision.typed_query.enabled", "typed_query planning needs the typed_query arm (pipeline.arms.typed_query)"))
        elif pipeline.decision.typed_query.repair_attempts and pipeline.arms.typed_query is not None and pipeline.arms.typed_query.mode == "sql_surface":
            # the IR compile-error re-plan has nothing to repair on the SQL route (no key without a reader)
            prefix = "pipeline" if name == "default" else f"pipelines.{name}"
            out.append((f"{prefix}.decision.typed_query.repair_attempts", "mode sql_surface repairs the SQL with pipeline.arms.typed_query.sql_surface.repair_attempts; set this to 0"))
    if not projection.enabled:
        for name, _ in arms:
            out.append((f"{'pipeline' if name == 'default' else 'pipelines.' + name}.arms.typed_query", "the typed_query arm needs projection.enabled"))
        return out
    backend = config.backends.get(projection.backend or "")
    if backend is None:
        out.append(("projection.backend", f"unknown backend {projection.backend!r}; declared: {sorted(config.backends)}"))
    elif not caps.serves(backend.kind, "relational"):
        out.append(("projection.backend", f"backend {projection.backend!r} ({backend.kind}) does not serve the relational capability"))
    schema = config.metadata_schema
    access = config.access.field if config.access.policy == "tenant" else None
    for table_name, table in projection.tables.items():
        for column in table.columns:
            spec = schema.field(column)
            if spec is None:
                out.append((f"projection.tables.{table_name}.columns", f"{column!r} is not a declared metadata_schema field"))
                continue
            if not (table.descriptions.get(column) or spec.description):
                out.append((f"projection.tables.{table_name}.descriptions", f"column {column!r} has no reviewed description (FieldSpec.description or descriptions.{column})"))
        for column in table.text_columns:
            if schema.field(column) is not None:
                out.append((f"projection.tables.{table_name}.text_columns", f"text column {column!r} shadows a declared metadata_schema field; use another name"))
        if access is not None and access not in table.columns:
            out.append((f"projection.tables.{table_name}.columns", f"access.policy tenant: every table needs the access column {access!r}"))
    if config.logs is not None and getattr(config, "collection", None) is None:
        # a partitioned collection's logs block builds no log-profile generation: the projection is
        # the support member's, the log member's SQL tables join it (``runtime/collection.py``)
        out.append(("projection.enabled", "log-profile generations get no projection in this release (projection_unavailable)"))
    for name, pipeline in arms:
        arm = pipeline.arms.typed_query
        typed = pipeline.decision.typed_query
        if arm is not None and typed.enabled and typed.routing == "router":
            role = getattr(config.models.roles, typed.router_role)
            if role is None or role.provider == "none":
                path = f"{'pipeline' if name == 'default' else 'pipelines.' + name}.decision.typed_query.router_role"
                out.append((path, f"routing router needs models.roles.{typed.router_role} (the chat role of the typed router call)"))
        if arm is not None and arm.mode == "sql_surface":
            role = getattr(config.models.roles, arm.sql_surface.role)
            if role is None or role.provider == "none":
                path = f"{'pipeline' if name == 'default' else 'pipelines.' + name}.arms.typed_query.sql_surface.role"
                out.append((path, f"mode sql_surface needs models.roles.{arm.sql_surface.role} (a chat role that writes the SQL)"))
    return out
