"""The setup contract of the multi-source SQL surface (unified merge plan §6.8, increment M4 guard).

One SQL surface over many sources: a request opens a **fresh** in-memory connection, ``ATTACH``es
the pinned files (member projections, slices, identity registry, links) **read-only**, creates one
**TEMP view** per logical table with a bare name (``ticket``, ``tracker_issue``, ``issue_day``,
``search_query_day``, ``customer``, ``link`` ...) whose definition carries its **pins** (member set,
slice, ``history_seq``, ``registry_version``, ``link_version`` as declared predicates), checks every
view against its declared columns, and only **then** sets ``query_only`` and installs the
SELECT/READ authorizer (``open_surface``). After that the connection cannot ``ATTACH``, create,
write, or read anything but the declared view columns (and, for the views' own reads, the declared
physical columns of their attached tables): the model's SQL still goes through the unchanged
``guard`` (one ``SELECT``, bare catalog names, no ``UNION``/``ATTACH``/DDL), and the authorizer is
the second, independent layer.

* ``SqlSurfaceSetup`` — attachments, views (per-source tables with their ``source``, columns with
  type, ``unit`` and reviewed description), the declared join paths (incl. identity and link
  tables), the access column and the pin labels. Validated: identifiers only, every view part
  pinned, join paths between declared columns of compatible types. Nothing in it is executed as
  code; physical names are quoted identifiers and pin values are typed literals.
* ``setup_catalog`` — the ``SqlCatalog`` of a setup for one principal: tables per source, units,
  join paths with ``join_policy: enforced`` (the guard refuses any other join with
  ``join_not_declared``), the pins; ``render()`` is the SQL writer's schema text.
* ``SetupValues`` — table-aware value grounding for the guard over the same read-only views
  (ACL-scoped observed values for listable columns, exact existence otherwise).
* ``execute_setup`` — run a guarded statement on a fresh surface; ACL binding is the guard's
  (unchanged: ``acl_not_applicable`` when a referenced table lacks the access column) and a tenant
  principal's row-level result goes through the executor's returned-row check.

What is not here yet (M4 completion, after M1b): the attach list from ``projection/store.py`` for a
pinned collection view, and wiring the setup into the ``typed_query`` arm (evidence, routes).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import urllib.parse
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator

from ..core.config_projection import KEY
from ..core.types import FieldSpec, FieldType, Predicate, ScopePredicates
from ..decision.typed_sql import _bound
from ..models import StrictModel
from ..projection.builder import quote
from ..projection.spec import SQL_TYPES
from ..runtime.acl import acl_key
from .catalog import TEMPORAL_FORMAT, SqlCatalog, SqlColumn, SqlJoin, SqlTable, _display, sealed
from .guard import SQL_FUNCTIONS, GuardedSql, SqlRefusal

IDENT = r"^[a-z][a-z0-9_]{0,62}$"
PHYSICAL = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
UNIT = r"^[a-z][a-z0-9_ /%.-]{0,31}$"
RESERVED_SCHEMAS = frozenset({"main", "temp"})
TEXT_TYPES = frozenset({"string", "enum", "id", "date", "datetime"})
NUMERIC_TYPES = frozenset({"integer", "number", "boolean"})


class SqlSetupError(Exception):
    """The setup could not be opened as declared (missing file, view/column mismatch, SQLite
    error during setup). Fail closed: the request has no SQL surface; nothing is retried."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class SqlPin(StrictModel):
    """One pin predicate of a view part, rendered as a typed literal into the TEMP view definition:
    ``eq`` (member/slice/version id), ``le`` (a sequence such as ``history_seq``) or ``in``."""

    label: str = Field(pattern=IDENT)  # member_set | slice | history_seq | registry_version | link_version | ...
    column: str = Field(pattern=PHYSICAL)
    # ``not_in`` (staging fixes, M4 completion): rows a pinned unit holds but the view does not
    # serve (versions superseded by a later slice of the day, tombstones), excluded by document id
    op: Literal["eq", "le", "in", "not_in"]
    value: StrictInt | StrictStr | list[StrictInt | StrictStr]  # strict: no bool/float coercion

    @model_validator(mode="after")
    def typed(self):
        values = self.value if isinstance(self.value, list) else [self.value]
        if any(isinstance(v, str) and "\x00" in v for v in values):
            raise ValueError(f"pin {self.label}: NUL in a pin value")
        if self.op in {"in", "not_in"} and (not isinstance(self.value, list) or not self.value):
            raise ValueError(f"pin {self.label}: op {self.op} takes a non-empty list")
        if self.op not in {"in", "not_in"} and isinstance(self.value, list):
            raise ValueError(f"pin {self.label}: op {self.op} takes one value")
        if self.op == "le" and not isinstance(self.value, int):
            raise ValueError(f"pin {self.label}: op le takes an integer (a sequence number)")
        return self

    def sql(self, table: str) -> str:
        # table-qualified: a misspelled column is an error, never SQLite's double-quoted string literal
        ref = f"{quote(table)}.{quote(self.column)}"
        if self.op in {"in", "not_in"}:
            return f"{ref} {'NOT IN' if self.op == 'not_in' else 'IN'} ({', '.join(_literal(v) for v in self.value)})"  # type: ignore[union-attr]
        return f"{ref} {'<=' if self.op == 'le' else '='} {_literal(self.value)}"  # type: ignore[arg-type]


def _literal(value: int | str) -> str:
    if isinstance(value, int):
        return str(int(value))
    return "'" + value.replace("'", "''") + "'"


class SqlAttachment(StrictModel):
    """A file attached read-only under ``alias`` (an identifier; never ``main``/``temp``)."""

    alias: str = Field(pattern=IDENT)
    path: str = Field(min_length=1)
    role: Literal["member", "slice", "registry", "links", "projection"] = "member"

    @field_validator("alias")
    @classmethod
    def not_reserved(cls, value: str) -> str:
        if value in RESERVED_SCHEMAS or value.startswith("sqlite_"):
            raise ValueError(f"attachment alias {value!r} is reserved")
        return value

    @field_validator("path")
    @classmethod
    def absolute(cls, value: str) -> str:
        if not Path(value).is_absolute() or "\x00" in value:
            raise ValueError("an attachment path is absolute")
        return value


class SqlViewPart(StrictModel):
    """One pinned read of an attached table; a view of several parts is their ``UNION ALL``
    (e.g. the slices of a live day)."""

    attachment: str = Field(pattern=IDENT)
    table: str = Field(pattern=PHYSICAL)
    # an unpinned read is not a view of the surface; the one exception is a part over a ``projection``
    # attachment: that file is the sealed projection of the pinned member generation (the file is the
    # pin) and its tables are read exactly as the single-file surface reads them (checked by the setup)
    pins: list[SqlPin] = Field(default_factory=list)


class SqlViewColumn(StrictModel):
    name: str = Field(pattern=IDENT)
    physical: str | None = Field(default=None, pattern=PHYSICAL)  # default: ``name``
    type: FieldType
    description: str = Field(min_length=1, max_length=1000)
    unit: str | None = Field(default=None, pattern=UNIT)  # measure columns: the unit of their values
    free_text: bool = False  # LIKE/instr only; no stored-value grounding
    listable: bool = False
    max_catalog_cardinality: int = Field(default=200, ge=0)
    enum_values: list[str] | None = None
    casefold_match: bool = False
    nullable: bool = True
    filterable: bool = True
    groupable: bool = False
    sortable: bool = False

    @property
    def source_column(self) -> str:
        return self.physical or self.name

    @model_validator(mode="after")
    def coherent(self):
        if self.unit is not None and self.type not in {"integer", "number"}:
            raise ValueError(f"column {self.name}: a unit belongs to a numeric measure")
        if self.free_text and self.type != "string":
            raise ValueError(f"column {self.name}: free text is a string column")
        if self.name == KEY and (self.free_text or self.type not in {"id", "string"}):
            raise ValueError(f"column {KEY}: the key is an id")
        return self

    def field_spec(self) -> FieldSpec | None:
        """The guard's grounding spec (``None``: the key or free text, as a projection's)."""
        if self.name == KEY or self.free_text:
            return None
        return FieldSpec(
            name=self.name, type=self.type, nullable=self.nullable, enum_values=self.enum_values, filterable=self.filterable, listable=self.listable,
            groupable=self.groupable, sortable=self.sortable, max_catalog_cardinality=self.max_catalog_cardinality, casefold_match=self.casefold_match,
            description=self.description,
        )  # fmt: skip


class SqlView(StrictModel):
    """One logical table: a TEMP view with a bare name over pinned parts of attached tables."""

    name: str = Field(pattern=IDENT)
    source: str = Field(min_length=1, max_length=64)  # the source/member the rows come from
    kind: str = Field(min_length=1, max_length=64)  # the record kind (business term)
    description: str = Field(min_length=1, max_length=1000)
    columns: list[SqlViewColumn] = Field(min_length=1)
    parts: list[SqlViewPart] = Field(min_length=1)

    @model_validator(mode="after")
    def coherent(self):
        names = [c.name for c in self.columns]
        if len(set(names)) != len(names):
            raise ValueError(f"view {self.name}: duplicate column names")
        if KEY not in names:
            raise ValueError(f"view {self.name}: needs the key column {KEY!r} (the row's document id)")
        if self.name.startswith("sqlite_"):
            raise ValueError(f"view {self.name}: reserved name")
        return self

    def column(self, name: str) -> SqlViewColumn | None:
        return next((c for c in self.columns if c.name == name), None)


class SqlJoinPath(StrictModel):
    """A declared join path: ``left.left_column = right.right_column`` (e.g. ``ticket.customer_id
    -> customer.id``, ``link.subject_id -> ticket.id``)."""

    left: str = Field(pattern=IDENT)
    left_column: str = Field(pattern=IDENT)
    right: str = Field(pattern=IDENT)
    right_column: str = Field(pattern=IDENT)
    cardinality: Literal["many_to_one", "one_to_one", "many_to_many"] = "many_to_one"
    description: str = Field(min_length=1, max_length=500)


class SqlSurfaceSetup(StrictModel):
    """The declared multi-source surface of one pinned collection view."""

    generation: str = Field(min_length=1, max_length=128)  # label of the pinned view (reported)
    attachments: list[SqlAttachment] = Field(min_length=1)
    views: list[SqlView] = Field(min_length=1)
    joins: list[SqlJoinPath] = Field(default_factory=list)
    access_column: str | None = Field(default=None, pattern=IDENT)
    pins: dict[str, StrictStr | StrictInt] = Field(default_factory=dict)  # member_set, slice, history_seq, registry_version, link_version

    @field_validator("pins")
    @classmethod
    def pin_labels(cls, value: dict[str, str | int]) -> dict[str, str | int]:
        for key in value:
            if not re.match(IDENT, key):
                raise ValueError(f"pin label {key!r}: an identifier")
        return value

    @model_validator(mode="after")
    def coherent(self):
        aliases = [a.alias for a in self.attachments]
        if len(set(aliases)) != len(aliases):
            raise ValueError("duplicate attachment aliases")
        names = [v.name for v in self.views]
        if len(set(names)) != len(names):
            raise ValueError("duplicate view names")
        if set(names) & set(aliases):
            raise ValueError(f"view names and attachment aliases overlap: {sorted(set(names) & set(aliases))}")
        roles = {a.alias: a.role for a in self.attachments}
        for view in self.views:
            for part in view.parts:
                if part.attachment not in aliases:
                    raise ValueError(f"view {view.name}: unknown attachment {part.attachment!r}")
                if not part.pins and roles[part.attachment] != "projection":
                    raise ValueError(f"view {view.name}: a part over the {roles[part.attachment]} attachment {part.attachment!r} needs at least 1 pin (only a projection attachment is pinned by its file)")
        views = {v.name: v for v in self.views}
        seen: set[frozenset[tuple[str, str]]] = set()
        for join in self.joins:
            ends = []
            for table, column in ((join.left, join.left_column), (join.right, join.right_column)):
                view = views.get(table)
                spec = view.column(column) if view is not None else None
                if spec is None:
                    raise ValueError(f"join path {join.left}.{join.left_column} = {join.right}.{join.right_column}: {table}.{column} is not a declared column")
                if spec.free_text:
                    raise ValueError(f"join path on the free-text column {table}.{column}")
                ends.append(spec)
            if (ends[0].type in NUMERIC_TYPES) != (ends[1].type in NUMERIC_TYPES):
                raise ValueError(f"join path {join.left}.{join.left_column} = {join.right}.{join.right_column}: incompatible column types")
            pair = frozenset({(join.left, join.left_column), (join.right, join.right_column)})
            if len(pair) != 2 or pair in seen:
                raise ValueError(f"join path {join.left}.{join.left_column} = {join.right}.{join.right_column}: duplicate or self path")
            seen.add(pair)
        return self

    def view(self, name: str) -> SqlView | None:
        return next((v for v in self.views if v.name == name), None)

    def view_sql(self, view: SqlView) -> str:
        """The TEMP view definition: the declared columns of every pinned part, ``UNION ALL``."""
        selects = []
        for part in view.parts:
            columns = ", ".join(f"{quote(part.table)}.{quote(c.source_column)} AS {quote(c.name)}" for c in view.columns)
            pins = " AND ".join(f"({pin.sql(part.table)})" for pin in part.pins)
            selects.append(f"SELECT {columns} FROM {quote(part.attachment)}.{quote(part.table)}" + (f" WHERE {pins}" if pins else ""))
        return " UNION ALL ".join(selects)

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


# --- the connection ------------------------------------------------------------------------------


def _ro_uri(path: str) -> str:
    return "file:" + urllib.parse.quote(path) + "?mode=ro"


def _authorizer(setup: SqlSurfaceSetup, functions: frozenset[str]) -> Any:
    """SELECT; READ of a declared view column (``temp``), or — for a view's own read — of the
    declared physical columns (and pin columns) of its parts' attached tables; the allowlisted
    functions. Everything else (ATTACH, CREATE, PRAGMA, writes, other reads) is denied."""
    view_columns = {v.name: {c.name for c in v.columns} for v in setup.views}
    under: dict[str, dict[tuple[str, str], set[str]]] = {}
    for view in setup.views:
        reads = under.setdefault(view.name, {})
        for part in view.parts:
            reads.setdefault((part.attachment, part.table), set()).update({c.source_column for c in view.columns} | {p.column for p in part.pins})

    def authorize(action: int, arg1: str | None, arg2: str | None, db: str | None, origin: str | None) -> int:
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            # a view column (``temp``), or the same read through a materialised subquery of a
            # compound (UNION ALL) view, which SQLite reports without a schema
            if db in {"temp", None} and origin is None and arg1 in view_columns:
                return sqlite3.SQLITE_OK if arg2 in view_columns[arg1] or arg2 == "" else sqlite3.SQLITE_DENY
            columns = under.get(origin or "", {}).get((db or "", arg1 or ""))
            return sqlite3.SQLITE_OK if columns is not None and (arg2 in columns or arg2 == "") else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_OK if (arg2 or "").lower() in functions else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY

    return authorize


def open_surface(setup: SqlSurfaceSetup, timeout_ms: int, functions: frozenset[str] | None = None) -> sqlite3.Connection:
    """A fresh read-only connection of the setup. Order (the contract the tests assert): attach
    every file read-only, create every TEMP view, check each view's columns, set ``query_only``,
    install the authorizer and the deadline. Any failure closes the connection and raises
    ``SqlSetupError`` (no partial surface)."""
    allowed = frozenset(f.lower() for f in (functions if functions is not None else SQL_FUNCTIONS))
    for attachment in setup.attachments:
        if not Path(attachment.path).is_file():
            raise SqlSetupError("attachment_missing", f"{attachment.alias}: the pinned file is not present")
    db = sqlite3.connect("file::memory:", uri=True)  # private in-memory main database (no shared cache)
    try:
        for attachment in setup.attachments:
            db.execute(f"ATTACH DATABASE ? AS {quote(attachment.alias)}", (_ro_uri(attachment.path),))
        for view in setup.views:
            for part in view.parts:
                stored = {r[1] for r in db.execute(f"PRAGMA {quote(part.attachment)}.table_info({quote(part.table)})").fetchall()}
                needed = {c.source_column for c in view.columns} | {p.column for p in part.pins}
                if not stored or needed - stored:
                    raise SqlSetupError("view_mismatch", f"view {view.name}: {part.attachment}.{part.table} lacks {sorted(needed - stored) or 'the table'}")
        for view in setup.views:
            db.execute(f"CREATE TEMP VIEW {quote(view.name)} AS {setup.view_sql(view)}")
        for view in setup.views:
            columns = [d[0] for d in db.execute(f"SELECT * FROM {quote(view.name)} LIMIT 0").description or []]
            if columns != [c.name for c in view.columns]:
                raise SqlSetupError("view_mismatch", f"view {view.name} has columns {columns}, declared {[c.name for c in view.columns]}")
        db.execute("PRAGMA query_only=1")
        db.set_authorizer(_authorizer(setup, allowed))
        deadline = time.monotonic() + timeout_ms / 1000.0
        db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
        return db
    except sqlite3.Error as error:
        db.close()
        raise SqlSetupError("setup_failed", str(error)[:300]) from None
    except BaseException:
        db.close()
        raise


# --- the catalog ---------------------------------------------------------------------------------


def setup_catalog(setup: SqlSurfaceSetup, acl: ScopePredicates, values: SetupValues | None = None, *, sample_values: int = 5, list_values_up_to: int = 0) -> SqlCatalog:
    """The ``SqlCatalog`` of a setup for one principal (``acl``): one table per view with its
    ``source``, columns with type, unit and description, sample values (as ``build_sql_catalog``:
    enum values, ACL-scoped observed values of listable columns from ``values``; never the access
    column, the key or free text), the declared join paths with ``join_policy: enforced`` and the
    setup's pins. The fingerprint covers the setup's fingerprint (views, pins, attachments) and
    the ACL key."""

    def shown(found: list[str]) -> tuple[list[str], bool]:
        if list_values_up_to and len(found) <= list_values_up_to:
            return list(found), True
        return found[:sample_values], len(found) <= sample_values

    tables: dict[str, SqlTable] = {}
    for view in setup.views:
        columns = []
        for column in view.columns:
            spec = column.field_spec()
            samples: list[str] = []
            complete = False
            state = "disabled"
            if sample_values > 0:
                if column.name == setup.access_column:
                    state = "withheld_access_column"
                elif spec is None or not spec.listable or spec.max_catalog_cardinality <= 0:
                    state = "withheld_not_enumerable"
                elif spec.type == "enum":
                    (samples, complete), state = shown(list(spec.enum_values or [])), "observed"
                else:
                    observed = values.table_observed(view.name, column.name) if values is not None else None
                    if observed is None:
                        state = "withheld_over_cap"
                    else:
                        (samples, complete), state = shown([str(v) for v in observed]), "observed"
            free = " [free text: match with LIKE '%word%' or instr(); never =]" if column.free_text else ""
            columns.append(
                SqlColumn(
                    source_column=column.source_column, name=column.name, display_name=_display(column.name),
                    datatype="TEXT" if column.name == KEY else SQL_TYPES[column.type], field_type=column.type, source_field=column.source_column,
                    description=column.description + free, filterable=column.filterable, groupable=column.groupable, sortable=column.sortable,
                    nullable=column.name != KEY and column.nullable, format=TEMPORAL_FORMAT.get(column.type), unit=column.unit,
                    sample_values=samples, samples=state, values_complete=complete and bool(samples),  # type: ignore[arg-type]
                )
            )  # fmt: skip
        tables[view.name] = SqlTable(name=view.name, kind=view.kind, display_name=_display(view.name), description=view.description, source=view.source, columns=columns)
    joins = [SqlJoin(left=j.left, left_column=j.left_column, right=j.right, right_column=j.right_column, cardinality=j.cardinality, description=j.description) for j in setup.joins]
    out = SqlCatalog(
        generation=setup.generation, projection_fingerprint=setup.fingerprint(), access_column=setup.access_column, acl_key=acl_key(list(acl)),
        tables=tables, joins=joins, join_policy="enforced", pins=dict(setup.pins),
    )  # fmt: skip
    return sealed(out)


# --- values --------------------------------------------------------------------------------------


class SetupValues:
    """Table-aware value grounding for the guard (``guard._Grounder`` reads ``table_aware``):
    observed values and existence come from the setup's own read-only views, filtered by the
    principal's ACL (a view without the access column grounds nothing for a restricted principal).
    One surface connection per instance; ``close()`` it (or use it as a context manager)."""

    table_aware = True

    def __init__(self, setup: SqlSurfaceSetup, acl: ScopePredicates, *, timeout_ms: int = 5000, question: str | None = None):
        self.setup = setup
        self.acl = list(acl)
        self.question = question
        self._timeout = timeout_ms
        self._conn: sqlite3.Connection | None = None
        self._observed: dict[tuple[str, str], list[str] | None] = {}

    def __enter__(self) -> SetupValues:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = open_surface(self.setup, self._timeout)
        return self._conn

    def table_field(self, table: str, column: str) -> FieldSpec | None:
        view = self.setup.view(table)
        spec = view.column(column) if view is not None else None
        return spec.field_spec() if spec is not None else None

    def _scope(self, table: str) -> tuple[str, list[Any]] | None:
        """The ACL condition on ``table`` (``None``: the principal may not read it)."""
        if not self.acl:
            return "", []
        view = self.setup.view(table)
        access = self.setup.access_column
        if view is None or access is None or view.column(access) is None:
            return None
        clauses, params = [], []
        for predicate in self.acl:
            if predicate.negate or predicate.op not in {"eq", "in"} or predicate.field != access:
                return None
            found = [predicate.value] if predicate.op == "eq" else list(predicate.value or [])  # type: ignore[arg-type]
            if not found:
                return None
            clauses.append(f"{quote(access)} IN ({', '.join('?' for _ in found)})")
            params.extend(found)
        return " AND ".join(clauses), params

    def table_observed(self, table: str, column: str) -> list[str] | None:
        """ACL-scoped distinct values of a listable column under its cap; else ``None``."""
        key = (table, column)
        if key in self._observed:
            return self._observed[key]
        spec = self.table_field(table, column)
        result: list[str] | None = None
        scope = self._scope(table)
        if spec is not None and spec.listable and spec.max_catalog_cardinality > 0:
            if scope is None:
                result = []
            else:
                where, params = scope
                sql = f"SELECT DISTINCT {quote(column)} FROM {quote(table)} WHERE {quote(column)} IS NOT NULL" + (f" AND {where}" if where else "")
                rows = self._db().execute(sql + f" ORDER BY 1 LIMIT {spec.max_catalog_cardinality + 1}", params).fetchall()
                found = [str(r[0]) for r in rows]
                result = found if len(found) <= spec.max_catalog_cardinality else None
        self._observed[key] = result
        return result

    def table_exists(self, table: str, column: str, value: Any, *, casefold: bool = False) -> bool:
        """Whether ``table.column`` holds ``value`` in a row the principal may read (exact)."""
        view = self.setup.view(table)
        scope = self._scope(table)
        if view is None or view.column(column) is None or scope is None:
            return False
        where, params = scope
        test = f"lower({quote(column)}) = lower(?)" if casefold else f"{quote(column)} = ?"
        sql = f"SELECT 1 FROM {quote(table)} WHERE {test}" + (f" AND {where}" if where else "") + " LIMIT 1"
        return self._db().execute(sql, [value, *params]).fetchone() is not None

    def request_value(self, tables: list[str], name: str, value: Any) -> tuple[Any, bool]:
        """A request filter ``name = value`` bound into ``tables``: typed per the column, and a text
        value must exist in every one of them for this principal. Returns ``(bound, casefold)``."""
        specs = [self.table_field(t, name) for t in tables]
        if name != KEY and any(s is None for s in specs):
            raise SqlRefusal("filter_not_applicable", f"request filter {name!r} names a free-text column; it is not an equality filter")
        kinds = {s.type for s in specs if s is not None} or {"id"}
        if len(kinds) != 1:
            raise SqlRefusal("type_mismatch", f"request filter {name!r} has different types in {tables}")
        kind = next(iter(kinds))
        casefold = all(s is not None and s.casefold_match for s in specs) and kind in {"string", "enum", "id"}
        if kind in NUMERIC_TYPES:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                if kind != "boolean" or not isinstance(value, bool):
                    raise SqlRefusal("type_mismatch", f"request filter {name!r} is {kind}")
            return _bound(kind, value), False
        if not isinstance(value, str):
            raise SqlRefusal("type_mismatch", f"request filter {name!r} is {kind}; a text value is expected")
        bound = _bound(kind, value)
        for table in tables:
            if not self.table_exists(table, name, bound, casefold=casefold):
                raise SqlRefusal("value_not_observed", f"request filter {name!r}: {value!r} does not exist in {table}.{name} for this principal")
        return bound, casefold


# --- execution -----------------------------------------------------------------------------------


def execute_setup(setup: SqlSurfaceSetup, guarded: GuardedSql, *, timeout_ms: int, tenant: str | None = None, lineage: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run a guarded statement on a fresh surface of ``setup`` and return ``names``, ``rows``, the
    ``pins`` and the setup ``fingerprint``. A tenant principal's row-level result passes the
    executor's returned-row check (every source's access value equals ``tenant``; else
    ``BackendError``, result discarded)."""
    from .executor import _check_rows, run_statement

    conn = open_surface(setup, timeout_ms)
    names, rows = run_statement(conn, guarded, timeout_ms, lineage)
    if tenant is not None and guarded.shape == "rows":
        _check_rows(guarded, names, rows, "tenant", tenant)
    return {"names": names, "rows": rows, "pins": dict(setup.pins), "setup": setup.fingerprint(), "join_paths": list(guarded.join_paths)}


def tenant_acl(field: str, tenant: str) -> list[Predicate]:
    """The ACL of one tenant principal on ``field`` (convenience for callers and tests)."""
    return [Predicate(field=field, op="eq", value=tenant)]
