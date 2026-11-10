# SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Modified by su_multi_arm contributors, 2026-11-08: adapted from NVIDIA/auto-ontology
# c447003f72a822301e927196a4ecb5b597c308d5 (auto_ontology/semantic/models.py ColumnAttributeSpec,
# TermProposal/TermAttributeAssignment; auto_ontology/utils/embedding_rows.py build_table_text,
# build_column_text; auto_ontology/retrieval/text_to_sql/formatters_util.py
# format_tables_for_prompt, _column_row, _pipe_table, _plain_cell). Changes: terms and columns are
# built deterministically from the sealed ProjectionCatalog and reviewed FieldSpec descriptions (no
# LLM term proposal, no fallback assignment, no pandas/pgvector rows); physical source binding is
# the projection table/column plus the document kind and metadata field; sample values come only
# from listable, enumerable, non-access columns through the ACL-scoped ValueResolver observed
# values; the catalog is fingerprinted with the ACL key; SQLite bare table names only.
"""``SqlCatalog``: the logical schema a model may write SQLite ``SELECT`` statements against.

One ``SqlTable`` per projection table (the business term: one document ``kind``), one
``SqlColumn`` per catalog column with its physical binding (projection table/column, metadata
field), SQL and logical type, flags, the reviewed description and, for listable columns under
their ``max_catalog_cardinality``, every ACL-scoped observed value when there are at most
``list_values_up_to`` (marked complete), else up to ``sample_values`` of them. Columns with
no enumerable universe (ids, free text, cap 0) and the access column never show values: the former
are grounded by literal lookup, the latter is bound by the arm. Declared ``many_to_one`` relations
are the only join paths shown. Descriptions and samples are untrusted model input data.

2026-11-09 (unified merge M4 guard): a catalog may also come from a multi-source setup
(``sql_surface/setup.py``): tables carry their ``source``, measure columns their ``unit``, and the
declared join paths (``joins``: column pairs such as ``link.subject_id = ticket.id``) are
**enforced** (``join_policy: enforced``; the guard refuses any other join with
``join_not_declared``). A projection catalog keeps ``join_policy: shown`` (paths shown, joins on
other columns still accepted), so the existing single-file surface is unchanged.

``render()`` is the prompt text (pipe tables as upstream ``format_tables_for_prompt``);
``fingerprint`` covers the projection catalog fingerprint, the rendered content and the ACL key,
so a catalog built for one principal is never mistaken for another's.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import Field

from ..core.config_projection import KEY
from ..core.types import FieldType, MetadataSchema, ScopePredicates
from ..decision.values import ValueResolver
from ..models import StrictModel
from ..projection.catalog import ProjectionCatalog
from ..projection.spec import SQL_TYPES
from ..runtime.acl import acl_key

SampleState = Literal["observed", "withheld_access_column", "withheld_not_enumerable", "withheld_over_cap", "disabled"]


class SqlColumn(StrictModel):
    """Upstream ``ColumnAttributeSpec`` + ``TermAttributeAssignment``: a physical column bound to a
    logical attribute with its datatype and reviewed description."""

    source_column: str  # physical projection column
    name: str  # logical name (the same identifier: projection columns are declared field names)
    display_name: str
    datatype: str  # SQLite storage type
    field_type: FieldType  # metadata_schema type
    source_field: str  # the metadata field (``id``: the document id)
    description: str = ""
    filterable: bool = True
    groupable: bool = False
    sortable: bool = False
    nullable: bool = True
    foreign_key: str | None = None  # ``<parent>.id`` for a declared many_to_one relation
    format: str | None = None  # storage notation (temporal columns)
    unit: str | None = None  # measure unit (setup catalogs); the guard refuses mixed-unit arithmetic
    sample_values: list[str] = Field(default_factory=list)
    samples: SampleState = "disabled"
    values_complete: bool = False  # ``sample_values`` is every value the principal may see (F7)


class SqlTable(StrictModel):
    """Upstream ``TermProposal``: one business term (a document kind) and its columns."""

    name: str  # physical projection table (bare SQLite name)
    kind: str  # document kind the rows come from
    display_name: str
    description: str = ""
    key: str = KEY
    source: str | None = None  # the source/member the table's rows come from (setup catalogs)
    columns: list[SqlColumn] = Field(default_factory=list)

    def column(self, name: str) -> SqlColumn | None:
        return next((c for c in self.columns if c.name == name), None)


class SqlRelation(StrictModel):
    name: str
    child: str
    column: str
    parent: str
    cardinality: Literal["many_to_one"] = "many_to_one"
    description: str


class SqlJoin(StrictModel):
    """A declared join path between a column of one table and a column of another (setup catalogs):
    the only equalities a join (``ON`` conjunct, correlated equality, ``IN (SELECT ...)``) may use
    when the catalog's ``join_policy`` is ``enforced``."""

    left: str
    left_column: str
    right: str
    right_column: str
    cardinality: Literal["many_to_one", "one_to_one", "many_to_many"] = "many_to_one"
    description: str = ""


# ``cross_source`` (M4 completion): one member's projection tables joined as before (``shown``); a
# statement that reads any table of another source (``SqlTable.source`` set) is guarded ``enforced``
JoinPolicy = Literal["shown", "enforced", "cross_source"]


class SqlCatalog(StrictModel):
    generation: str
    projection_fingerprint: str
    access_column: str | None = None
    acl_key: str
    tables: dict[str, SqlTable]
    relations: list[SqlRelation] = Field(default_factory=list)
    # M4 guard (setup catalogs): declared join paths beyond ``relations``, whether the guard enforces
    # them, and the pins the setup's views were created with (fingerprinted and reported)
    joins: list[SqlJoin] = Field(default_factory=list)
    join_policy: JoinPolicy = "shown"
    pins: dict[str, str | int] = Field(default_factory=dict)
    fingerprint: str = ""

    def table(self, name: str) -> SqlTable | None:
        return self.tables.get(name)

    def join_pairs(self) -> dict[frozenset[tuple[str, str]], str]:
        """Every declared join path as ``{(table, column), (table, column)} -> label``: the
        relations (``child.column = parent.id``) and the declared ``joins``."""
        out: dict[frozenset[tuple[str, str]], str] = {}
        for r in self.relations:
            out[frozenset({(r.child, r.column), (r.parent, KEY)})] = f"{r.child}.{r.column} = {r.parent}.{KEY}"
        for j in self.joins:
            out[frozenset({(j.left, j.left_column), (j.right, j.right_column)})] = f"{j.left}.{j.left_column} = {j.right}.{j.right_column}"
        return out

    def has_units(self) -> bool:
        return any(c.unit for t in self.tables.values() for c in t.columns)

    def schema(self) -> dict[str, dict[str, str]]:
        """table -> column -> SQL type (the parser's resolution schema)."""
        return {t: {c.name: c.datatype for c in table.columns} for t, table in self.tables.items()}

    def render(self) -> str:
        """Schema context for the prompt (upstream ``format_tables_for_prompt``, SQLite bare names)."""
        blocks: list[str] = []
        sources: dict[str, list[str]] = {}
        for table in self.tables.values():
            if table.source:
                sources.setdefault(table.source, []).append(table.name)
        if sources:
            lines = [f"- {_plain_cell(source)}: {', '.join(names)}" for source, names in sorted(sources.items())]
            blocks.append("TABLES PER SOURCE (each table holds the rows of one source):\n" + "\n".join(lines))
        for table in self.tables.values():
            row = {"table": _plain_cell(table.name), "label": _plain_cell(table.display_name), "source": _plain_cell(table.source), "document kind": _plain_cell(table.kind), "description": _plain_cell(table.description), "primary key": table.key}
            parts = [_pipe_table(_TABLE_HEADERS, [row])]
            parts.append(_pipe_table(_COLUMN_HEADERS, [_column_row(column) for column in table.columns]))
            blocks.append("\n".join(part for part in parts if part))
        if self.join_policy in {"enforced", "cross_source"}:
            lines = [f"- {r.child}.{r.column} = {r.parent}.{KEY} ({r.cardinality}): {_plain_cell(r.description)}" for r in self.relations]
            lines += [f"- {j.left}.{j.left_column} = {j.right}.{j.right_column} ({j.cardinality}): {_plain_cell(j.description)}" for j in self.joins]
            text = "\n".join(lines) if lines else "- none: every question reads one table"
            scope = "every join condition is one or more of these equalities" if self.join_policy == "enforced" else (
                "a statement that reads a table listed under a source is limited to these equalities in every join condition")
            blocks.append(f"DECLARED JOIN PATHS (enforced: {scope}; any other join is refused):\n" + text)
        elif self.relations:
            lines = [f"- {r.child}.{r.column} = {r.parent}.{KEY} ({r.cardinality}): {_plain_cell(r.description)}" for r in self.relations]
            blocks.append("DECLARED JOIN PATHS (the only joins between tables):\n" + "\n".join(lines))
        if self.has_units():
            blocks.append("UNITS: a column with a unit holds values in that unit. Never add, subtract, compare or COALESCE columns of different units; a ratio of two measures is allowed.")
        return "\n\n".join(blocks)

    def column_text(self, table: str, column: str) -> str:
        """Upstream ``build_column_text``: one column's identity line (catalog search, traces)."""
        entry = self.tables[table]
        spec = entry.column(column)
        if spec is None:
            raise KeyError(f"{table}.{column}")
        text = f"table_name: {table}, column_name: {column}, data_type: {spec.datatype}"
        if spec.description:
            text += f", column_description: {spec.description}"
        if spec.sample_values:
            text += f", sample_values: {', '.join(spec.sample_values)}"
        return text

    def table_text(self, table: str) -> str:
        """Upstream ``build_table_text``: a table's identity line with its compact column list."""
        entry = self.tables[table]
        text = f"table_name: {table}"
        if entry.description:
            text += f", table_description: {entry.description}"
        pieces = []
        for column in entry.columns:
            piece = f"{{name: {column.name}, data_type: {column.datatype}"
            if column.description:
                piece += f", description: {column.description}"
            pieces.append(piece + "}")
        return text + f", columns: {','.join(pieces)}"


# --- rendering (upstream formatters_util) ---------------------------------------------------------

_TABLE_HEADERS = ["table", "label", "source", "document kind", "description", "primary key"]
_COLUMN_HEADERS = ["column", "type", "unit", "is_nullable", "foreign key", "description", "sample values", "format"]


def _plain_cell(value: Any) -> str:
    """One pipe-table cell: no newlines, and no ``|`` that would add a column."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        parts = [_plain_cell(item) for item in value]
        return ", ".join(part for part in parts if part)
    return " ".join(str(value).replace("|", "/").split())


def _pipe_table(headers: list[str], rows: list[dict[str, str]]) -> str:
    """``header 1 | header 2`` then one ``|``-separated row per record; a header whose cells are all
    empty is omitted."""
    active = [header for header in headers if any(row.get(header, "").strip() for row in rows)]
    if not active:
        return ""
    lines = [" | ".join(active)]
    for row in rows:
        lines.append(" | ".join(row.get(header, "") for header in active))
    return "\n".join(lines)


def _column_row(column: SqlColumn) -> dict[str, str]:
    samples = column.sample_values if column.samples == "observed" else []
    return {
        "column": _plain_cell(column.name),
        "type": _plain_cell(f"{column.datatype} ({column.field_type})"),
        "unit": _plain_cell(column.unit),
        "is_nullable": "true" if column.nullable else "false",
        "foreign key": _plain_cell(column.foreign_key),
        "description": _plain_cell(column.description),
        "sample values": _plain_cell([json.dumps(v, ensure_ascii=False) for v in samples]) + (" (all values)" if samples and column.values_complete else ""),
        "format": _plain_cell(column.format),
    }


# --- construction ------------------------------------------------------------------------------

TEMPORAL_FORMAT = {
    "datetime": "UTC ISO 8601 text YYYY-MM-DDTHH:MM:SS.ffffff+00:00; compare with 'YYYY', 'YYYY-MM', 'YYYY-MM-DD' or that exact form",
    "date": "ISO 8601 text YYYY-MM-DD",
    "boolean": "0 or 1",
}


def _display(name: str) -> str:
    return " ".join(part.capitalize() for part in name.split("_") if part)


def build_sql_catalog(catalog: ProjectionCatalog, schema: MetadataSchema, resolver: ValueResolver | None, acl: ScopePredicates, *, sample_values: int = 5, list_values_up_to: int = 0) -> SqlCatalog:
    """The logical SQL catalog of one sealed projection for one principal (``acl``).
    ``list_values_up_to`` (F7): a listable column with at most that many values (ACL-scoped
    observed values under the column's ``max_catalog_cardinality``, or declared enum values) shows
    all of them, marked complete; otherwise the first ``sample_values``."""

    def shown(values: list[str]) -> tuple[list[str], bool]:
        if list_values_up_to and len(values) <= list_values_up_to:
            return list(values), True
        return values[:sample_values], len(values) <= sample_values

    foreign = {(r.child, r.column): f"{r.parent}.{KEY}" for r in catalog.relations.values()}
    tables: dict[str, SqlTable] = {}
    for name, table in catalog.tables.items():
        columns: list[SqlColumn] = []
        for column, entry in table.columns.items():
            spec = schema.field(column) if column != KEY else None
            samples: list[str] = []
            complete = False
            state: SampleState = "disabled"
            if sample_values > 0:  # 0: no values at all
                if column == catalog.access_column:
                    state = "withheld_access_column"
                elif spec is None or not spec.listable or spec.max_catalog_cardinality <= 0:
                    state = "withheld_not_enumerable"
                elif spec.type == "enum":
                    (samples, complete), state = shown(list(spec.enum_values or [])), "observed"
                else:
                    observed = resolver.observed(column) if resolver is not None else None
                    if observed is None:
                        state = "withheld_over_cap"
                    else:
                        (samples, complete), state = shown([str(v) for v in observed]), "observed"
            columns.append(
                SqlColumn(
                    source_column=column,
                    name=column,
                    display_name=_display(column),
                    datatype=SQL_TYPES[entry.type] if column != KEY else "TEXT",
                    field_type=entry.type,
                    source_field=column,
                    # a free-text column (F6; no declared field): matched with LIKE/instr only
                    description=entry.description + (" [free text: match with LIKE '%word%' or instr(); never =]" if spec is None and column != KEY else ""),
                    filterable=entry.filterable,
                    groupable=entry.groupable,
                    sortable=entry.sortable,
                    nullable=column != KEY and (spec.nullable if spec is not None else True),
                    foreign_key=foreign.get((name, column)),
                    format=TEMPORAL_FORMAT.get(entry.type),
                    sample_values=samples,
                    samples=state,
                    values_complete=complete and bool(samples),
                )
            )
        tables[name] = SqlTable(name=name, kind=table.kind, display_name=_display(name), description=table.description or "", columns=columns)
    relations = [SqlRelation(name=n, child=r.child, column=r.column, parent=r.parent, description=r.description) for n, r in sorted(catalog.relations.items())]
    out = SqlCatalog(generation=catalog.generation, projection_fingerprint=catalog.fingerprint, access_column=catalog.access_column, acl_key=acl_key(list(acl)), tables=tables, relations=relations)
    return sealed(out)


# Fields added by the M4 guard leave the digest at their defaults, so a projection catalog's
# fingerprint (and every guarded-statement fingerprint derived from it) is unchanged.
_LATER_CATALOG = {"joins": [], "join_policy": "shown", "pins": {}}
_LATER_TABLE = {"source": None}
_LATER_COLUMN = {"unit": None}


def _drop_defaults(data: dict[str, Any], defaults: dict[str, Any]) -> None:
    for key, default in defaults.items():
        if key in data and data[key] == default:
            del data[key]


def sealed(catalog: SqlCatalog) -> SqlCatalog:
    """``catalog`` with its ``fingerprint``: the digest of its content, the ACL key included."""
    data = catalog.model_dump(mode="json", exclude={"fingerprint"})
    _drop_defaults(data, _LATER_CATALOG)
    for table in data["tables"].values():
        _drop_defaults(table, _LATER_TABLE)
        for column in table["columns"]:
            _drop_defaults(column, _LATER_COLUMN)
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    return catalog.model_copy(update={"fingerprint": digest})
