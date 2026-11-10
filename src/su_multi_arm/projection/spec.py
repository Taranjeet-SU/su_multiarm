"""``ProjectionSpec``: the projection layout resolved against the metadata schema.

Tables are keyed by document ``kind`` (key column ``id`` = document id); every other column is a
declared ``FieldSpec``. Validation mirrors ``core/config_projection.projection_problems`` (Looker
rule: every table has its key; relation targets are keys; under a tenant policy every table has the
access column; every column has a reviewed description) so a spec built outside a validated
``Config`` (tests, tools) is held to the same rules. The SQL type of a column follows its field
type; temporal values are stored normalised (UTC ISO datetimes, ISO dates) so comparisons are
lexicographic and exact.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from ..core.config_projection import KEY, ProjectionConfig
from ..core.types import FieldSpec, MetadataSchema

SQL_TYPES = {"string": "TEXT", "id": "TEXT", "enum": "TEXT", "datetime": "TEXT", "date": "TEXT", "integer": "INTEGER", "number": "REAL", "boolean": "INTEGER"}


def text_field(name: str, description: str) -> FieldSpec:
    """The column spec of a free-text projection column (F6): filterable text, never listed,
    grouped or sorted, no enumerable universe (``max_catalog_cardinality: 0``)."""
    return FieldSpec(name=name, type="string", nullable=True, filterable=True, listable=False, groupable=False, sortable=False, max_catalog_cardinality=0, description=description)


@dataclass(frozen=True)
class TableSpec:
    name: str
    kind: str
    columns: dict[str, FieldSpec]  # declared order; the key ``id`` is implicit
    descriptions: dict[str, str]
    description: str | None = None
    # free-text columns (F6): column -> ``title`` | ``fields.<name>`` (their ``columns`` entry is a
    # synthesized non-listable string FieldSpec that no metadata schema declares)
    text_sources: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RelationSpec:
    name: str
    child: str
    column: str
    parent: str
    cardinality: str
    description: str


@dataclass(frozen=True)
class ProjectionSpec:
    tables: dict[str, TableSpec]
    relations: dict[str, RelationSpec]
    access_column: str | None
    fingerprint: str = field(default="")

    @classmethod
    def build(cls, config: ProjectionConfig, schema: MetadataSchema, access_column: str | None) -> ProjectionSpec:
        problems: list[str] = []
        tables: dict[str, TableSpec] = {}
        for name, table in config.tables.items():
            columns: dict[str, FieldSpec] = {}
            descriptions: dict[str, str] = {}
            for column in table.columns:
                spec = schema.field(column)
                if spec is None:
                    problems.append(f"{name}.{column}: not a declared metadata field")
                    continue
                text = table.descriptions.get(column) or spec.description
                if not text:
                    problems.append(f"{name}.{column}: no reviewed description")
                columns[column] = spec
                descriptions[column] = text or ""
            for column, text in table.text_columns.items():
                if schema.field(column) is not None:
                    problems.append(f"{name}.{column}: a text column shadows a declared metadata field")
                    continue
                columns[column] = text_field(column, text.description)
                descriptions[column] = text.description
            if KEY in table.descriptions:
                descriptions[KEY] = table.descriptions[KEY]
            if access_column is not None and access_column not in columns:
                problems.append(f"{name}: access column {access_column!r} missing")
            sources = {c: t.source for c, t in table.text_columns.items() if c in columns}
            tables[name] = TableSpec(name=name, kind=table.kind, columns=columns, descriptions=descriptions, description=table.description, text_sources=sources)
        relations: dict[str, RelationSpec] = {}
        for name, relation in config.relations.items():
            child, column = relation.child()
            parent = relation.parent()
            if child not in tables or parent not in tables or column not in tables[child].columns:
                problems.append(f"relation {name}: undeclared table or column")
                continue
            relations[name] = RelationSpec(name=name, child=child, column=column, parent=parent, cardinality=relation.cardinality, description=relation.description)
        if problems:
            raise ValueError("projection spec is invalid: " + "; ".join(problems))
        layout = {
            # ``text_sources`` only when declared: the fingerprint of every earlier spec is unchanged
            "tables": {
                n: {"kind": t.kind, "columns": {c: s.model_dump(mode="json") for c, s in t.columns.items()}, "descriptions": t.descriptions, **({"text_sources": t.text_sources} if t.text_sources else {})}
                for n, t in sorted(tables.items())
            },
            "relations": {n: [r.child, r.column, r.parent, r.cardinality] for n, r in sorted(relations.items())},
            "access_column": access_column,
        }
        digest = hashlib.sha256(json.dumps(layout, sort_keys=True).encode()).hexdigest()
        return cls(tables=tables, relations=relations, access_column=access_column, fingerprint=digest)

    def table_of_kind(self, kind: str) -> TableSpec | None:
        return next((t for t in self.tables.values() if t.kind == kind), None)
