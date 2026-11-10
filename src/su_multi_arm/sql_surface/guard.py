"""SQL-surface guard: parse, validate, ground and bind one model-written SQLite ``SELECT``.

Nothing the model writes is executed as written. ``guard(sql, ...)``:

1. **Text**: at most ``max_sql_chars``; tokenised with ``sqlglot`` (extra ``su-multi-arm[sql-surface]``;
   missing: ``sqlglot_missing``); any comment (``--``, ``/* */``, hints) is refused (no comment
   smuggling); exactly one statement (one trailing ``;`` allowed).
2. **Statement**: the root is one ``SELECT`` (CTEs allowed; ``WITH RECURSIVE``, ``UNION``/
   ``INTERSECT``/``EXCEPT``, ``PRAGMA``/``ATTACH``/DML/DDL refused). Every node is in an allowlist
   (no window functions, placeholders, blobs, ``COLLATE``, ``ESCAPE``...); functions are in
   ``SQL_FUNCTIONS``; tables are catalog tables or CTEs by bare name (``main.t``/3-part names,
   ``sqlite_*`` tables and table-valued functions refused); joins are ``INNER``/``LEFT`` with
   ``ON``/``USING`` (no cross/comma/natural joins), at most ``max_joins``; at most
   ``max_subqueries`` subqueries/CTEs; every ``LIMIT``/``OFFSET`` is an integer literal.
3. **Names**: ``sqlglot.optimizer.qualify`` against the catalog schema (unknown/ambiguous columns
   refused; the internal ``_rejected`` column and ``rowid`` are not in it).
4. **Values** (``values.extract_value_checks``): every literal compared with an expression is traced
   through scopes to a catalog column and checked: text values against the ``ValueResolver``
   (declared enum values, ACL-scoped observed values for listable columns, else an exact
   ``RecordStore.exists`` lookup; ``lower()``/``upper()`` compare transformed observed values;
   no fuzzy match; since 2026-11-09 a value equal to a stored value under the documented
   normalization of ``decision/value_match.py`` is rewritten to the stored value and recorded
   (``as_written``, ``rule``), several matches are ``ambiguous_value``, and an ``=`` on a bare
   column that no rule grounds moves to the one other column the user's question grounds it in
   (``ValueResolver.other_field``; ``column_as_written``)); numbers/booleans by type; temporal values by their stored ISO
   form; ``LIKE``/``instr`` only on free-text columns (no enumerable universe). A text literal in
   any other filter position cannot be grounded and is refused.
5. **Binding**: request filters (``eq``; applied to every referenced table that has the column,
   else ``filter_not_applicable``) and, unless the test-only ``inject_acl=False``, the ACL
   predicates (layer 1) are pushed into every catalog table reference
   (``(SELECT * FROM t WHERE ...) AS t``); every model literal becomes a named parameter; the
   outer ``LIMIT`` is ``min(model limit, row_limit + 1)``. A row-level outer query (no aggregate,
   ``GROUP BY``, ``DISTINCT`` or derived source) gets hidden ``id``/access columns per source so
   the executor can cite ``(document id, field, generation)`` and run the returned-row check.
6. **Result shape** (2026-11-09, corpus-B B7): a rate/ratio/share column carries its numerator and
   denominator (``_ratio_parts``; added as visible columns when not selected), and a grouped
   statement over one table gets a bounded contributor read of its small groups
   (``_group_lineage_statement``; same bound source, parameters and read-only connection).

7. **Join paths and units** (2026-11-09, unified merge M4 guard): on a catalog whose
   ``join_policy`` is ``enforced`` (a multi-source setup, ``setup.py``) every ``ON`` conjunct, every
   correlated equality of a subquery and every ``IN (SELECT ...)`` across two columns must be an
   equality of bare columns along a declared join path (``SqlCatalog.join_pairs``), else
   ``join_not_declared`` (``_check_joins``). On a catalog with measure units, adding, subtracting,
   comparing or coalescing expressions of different units is refused with ``mixed_units``
   (``_check_units``; a product or ratio of measures is allowed and carries no unit). A projection
   catalog (``join_policy: shown``, no units) is guarded exactly as before.

Refusals are typed (``SqlRefusal.code``): ``security`` codes are final, ``repairable`` codes may be
returned to the model once (planner), value codes are explicit unsupported constraints.

A table-aware value source (``setup.SetupValues``, ``table_aware = True``) grounds values per table
(``table_field``/``table_observed``/``table_exists``/``request_value``); a ``ValueResolver`` grounds
them per field name as before.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from ..core.config_projection import KEY
from ..core.types import Predicate, ScopePredicates
from ..decision.typed_sql import _bound
from ..decision.value_match import match_value
from ..decision.values import BoundError, ValueResolver
from ..models import UnsupportedReason
from ..projection.builder import quote
from .catalog import SqlCatalog

try:  # optional extra ``su-multi-arm[sql-surface]``
    import sqlglot
    from sqlglot import exp
except ImportError:  # pragma: no cover - exercised by monkeypatching ``available``
    sqlglot = None  # type: ignore[assignment]
    exp = None  # type: ignore[assignment]

Kind = Literal["security", "repairable", "value", "unavailable", "final"]

# SQLite function names the read-only authorizer allows in this mode (``open_readonly(functions=)``)
SQL_FUNCTIONS = frozenset(
    {
        "count", "sum", "avg", "min", "max", "total", "group_concat",
        "lower", "upper", "length", "substr", "substring", "trim", "instr",
        "abs", "round", "coalesce", "ifnull", "nullif", "iif",
        "date", "datetime", "julianday", "strftime", "like",
    }
)  # fmt: skip
ANONYMOUS = frozenset({"total", "date", "datetime", "julianday", "strftime", "ifnull", "instr", "substr", "iif"})
TEMPORAL_FUNCTIONS = frozenset({"date", "datetime", "julianday", "strftime"})
DANGEROUS = re.compile(r"^(sqlite_|pragma_|load_extension$|readfile$|writefile$|edit$|fts\d|json_each$|json_tree$|zeroblob$|randomblob$)", re.I)
SECURITY_CODES = frozenset({"comment", "multiple_statements", "not_select", "recursive_cte", "system_object", "nul_byte", "authorizer_denied", "unbound_table"})
FINAL_CODES = frozenset({"timeout"})
REPAIRABLE_CODES = frozenset(
    {"too_long", "parse_error", "set_operation", "placeholder", "unknown_table", "qualified_name", "unknown_column", "construct_not_allowed", "function_not_allowed", "join_not_allowed", "too_complex", "limit_invalid", "cte_shadows_table", "text_match_not_allowed", "free_text_equality", "execution_error",
     "join_not_declared", "mixed_units"}
)
VALUE_REASONS: dict[str, UnsupportedReason] = {
    "value_not_observed": "value_not_observed",
    "type_mismatch": "type_mismatch",
    "unparseable_bound": "unparseable_bound",
    "ungroundable_value": "value_not_observed",
    "ambiguous_value": "value_not_observed",  # several stored values match under the documented normalization
    "filter_not_applicable": "filter_not_applicable",
    "acl_not_applicable": "filter_not_applicable",
}
_DATE_PREFIX = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
_CANONICAL_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$")
_TEMPORAL_TEXT = re.compile(r"^[0-9]{1,4}([-:T. /][0-9]{1,6})*([+-][0-9]{2}:[0-9]{2}|Z)?$")
_MODIFIER = re.compile(r"^[A-Za-z0-9 %:+\-./]{0,40}$")
TEXT_TYPES = {"string", "enum", "id"}
TEMPORAL_TYPES = {"datetime", "date"}


class SqlRefusal(Exception):
    """A typed refusal. ``kind``: security (final), repairable (one re-prompt), value (explicit
    unsupported constraint), unavailable (the extra is not installed)."""

    def __init__(self, code: str, detail: str, *, allowed: list[str] | None = None):
        self.code = code
        self.detail = detail
        self.allowed = list(allowed or [])
        super().__init__(f"{code}: {detail}")

    @property
    def kind(self) -> Kind:
        if self.code == "sqlglot_missing":
            return "unavailable"
        if self.code in SECURITY_CODES:
            return "security"
        if self.code in FINAL_CODES:
            return "final"
        if self.code in VALUE_REASONS:
            return "value"
        return "repairable"

    @property
    def reason(self) -> UnsupportedReason:
        return VALUE_REASONS.get(self.code, "sql_refused")

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "kind": self.kind, "detail": self.detail, "allowed": self.allowed}


def available() -> bool:
    return sqlglot is not None


@dataclass
class GuardedSql:
    sql: str  # the parameterised statement that is executed
    params: dict[str, Any]
    lineage_params: dict[str, Any]  # ACL values masked
    shape: Literal["rows", "derived"]
    columns: list[str]  # visible output columns, in order
    citations: list[list[tuple[str, str, str]]]  # per visible column: (source alias, table, column)
    hidden: dict[str, tuple[str, str]]  # hidden output name -> (source alias, "id" | "acl")
    primary: str | None  # the FROM source alias of a row-level result (its rows' document id)
    tables: list[str]  # catalog tables referenced
    referenced: list[tuple[str, str]]  # catalog columns referenced
    joins: int
    limit: int  # rows kept (row_limit); the statement fetches limit + 1
    acl_applied: bool
    checked: list[dict[str, Any]] = field(default_factory=list)
    text_patterns: list[dict[str, Any]] = field(default_factory=list)
    fingerprint: str = ""
    # Contributor lineage of a scalar aggregate over one table (2026-11-08): the same bound read with
    # the ``id`` of the rows the aggregate ranges over (first ``LINEAGE_IDS + 1`` by id) and their
    # count. ``None`` for every other shape (grouped, joined, nested, limited or row-level SQL).
    lineage_sql: str | None = None
    lineage_count_sql: str | None = None
    # Ranking results (2026-11-09 corpus-B fix F3): ``sort_projected`` names the ORDER BY measures the
    # guard added to the SELECT list (a ranking that did not project its measure); ``tie_columns``
    # are the visible columns whose values define a tie at the LIMIT boundary; the statement fetches
    # up to ``tie_window`` (+1) rows past the model's LIMIT so the executor keeps the rows that tie
    # the last one (never a silent cut of a tie). ``peek_before``: the model's OFFSET was lowered by
    # one so the executor can report a tie with the row before the OFFSET ("second-most").
    sort_projected: list[str] = field(default_factory=list)
    tie_columns: list[int] = field(default_factory=list)
    tie_window: int = 0
    peek_before: bool = False
    # The model's OFFSET of the outermost statement (2026-11-09, B4 q011): the rendering states the
    # rank position of the first returned row ("the second-most" is position 2, not the top row).
    offset: int = 0
    # Ratio parts (2026-11-09, corpus-B B7): per rate/ratio/share column of the outermost statement
    # ``(ratio, numerator, denominator)`` visible positions; ``ratio_projected`` names the parts the
    # guard added because the statement did not select them (``_ratio_parts``).
    ratios: list[tuple[int, int, int]] = field(default_factory=list)
    ratio_projected: list[str] = field(default_factory=list)
    # Small-group lineage (2026-11-09, corpus-B B7): for a grouped statement over one catalog table,
    # a read of ``(group key..., id)`` for every returned group with at most ``group_lineage_max``
    # contributing rows; ``group_keys`` are the visible positions of the group keys (``_group_lineage``).
    group_lineage_sql: str | None = None
    group_keys: list[int] = field(default_factory=list)
    group_lineage_max: int = 0
    lineage_ids: int = 8  # contributor ids a scalar aggregate's lineage keeps
    # The declared join paths the statement's joins use (M4 guard; enforced catalogs only).
    join_paths: list[str] = field(default_factory=list)


# --- step 1-2: text and statement ------------------------------------------------------------------


def _parse(text: str, max_sql_chars: int) -> Any:
    if sqlglot is None:
        raise SqlRefusal("sqlglot_missing", "the sql_surface mode needs sqlglot: install su-multi-arm[sql-surface]")
    if "\x00" in text:
        raise SqlRefusal("nul_byte", "the statement contains a NUL byte")
    text = text.strip()
    if len(text) > max_sql_chars:
        raise SqlRefusal("too_long", f"the statement has {len(text)} characters; at most {max_sql_chars}")
    from sqlglot.errors import ParseError, TokenError
    from sqlglot.tokens import Tokenizer, TokenType

    try:
        tokens = Tokenizer(dialect="sqlite").tokenize(text)
    except (TokenError, ValueError) as error:
        raise SqlRefusal("parse_error", _first_line(error)) from None
    if any(token.comments for token in tokens):
        raise SqlRefusal("comment", "comments are not accepted in the SQL")
    semicolons = [i for i, token in enumerate(tokens) if token.token_type == TokenType.SEMICOLON]
    if semicolons and semicolons != [len(tokens) - 1]:
        raise SqlRefusal("multiple_statements", "exactly one statement is accepted")
    try:
        trees = [t for t in sqlglot.parse(text, read="sqlite") if t is not None]
    except (ParseError, TokenError) as error:
        raise SqlRefusal("parse_error", _first_line(error)) from None
    if len(trees) != 1:
        raise SqlRefusal("multiple_statements", "exactly one statement is accepted")
    root = trees[0]
    if isinstance(root, (exp.Union, exp.Intersect, exp.Except)):
        raise SqlRefusal("set_operation", "UNION/INTERSECT/EXCEPT are not offered; write one SELECT")
    if not isinstance(root, exp.Select):
        raise SqlRefusal("not_select", f"only a SELECT is accepted (got {type(root).__name__.upper()})")
    return root


def _first_line(error: Exception) -> str:
    return str(error).strip().splitlines()[0][:300] if str(error).strip() else type(error).__name__


def _allowed_nodes() -> tuple[type, ...]:
    return (
        exp.Select, exp.From, exp.Join, exp.Where, exp.Group, exp.Having, exp.Order, exp.Ordered, exp.Limit, exp.Offset,
        exp.With, exp.CTE, exp.Subquery, exp.TableAlias, exp.Alias, exp.Table, exp.Column, exp.Identifier, exp.Star,
        exp.Literal, exp.Boolean, exp.Null, exp.Paren, exp.Distinct,
        exp.And, exp.Or, exp.Not, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In, exp.Between, exp.Is, exp.Like, exp.Exists,
        exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Neg, exp.DPipe, exp.Case, exp.If, exp.Cast, exp.DataType,
        exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max, exp.GroupConcat,
        exp.Lower, exp.Upper, exp.Length, exp.Substring, exp.Trim, exp.StrPosition, exp.Abs, exp.Round, exp.Coalesce, exp.Nullif,
        exp.Greatest, exp.Least, exp.Date, exp.TimeToStr, exp.TsOrDsToTimestamp, exp.Anonymous,
    )  # fmt: skip


def _check_nodes(tree: Any, catalog: SqlCatalog, ctes: set[str], max_joins: int) -> None:
    allowed = _allowed_nodes()
    joins = 0
    for node in tree.walk():
        if isinstance(node, exp.Placeholder):
            raise SqlRefusal("placeholder", "parameters are not accepted; write literal values")
        if isinstance(node, (exp.Union, exp.Intersect, exp.Except)):
            raise SqlRefusal("set_operation", "UNION/INTERSECT/EXCEPT are not offered; write one SELECT")
        if isinstance(node, exp.With) and node.args.get("recursive"):
            raise SqlRefusal("recursive_cte", "WITH RECURSIVE is not accepted")
        if isinstance(node, exp.Anonymous):
            name = str(node.this).lower()
            if DANGEROUS.match(name):
                raise SqlRefusal("system_object", f"function {name} is not accepted")
            if name not in ANONYMOUS:
                raise SqlRefusal("function_not_allowed", f"function {name}() is not offered", allowed=sorted(SQL_FUNCTIONS - {"like"}))
            continue
        if isinstance(node, exp.Table):
            _check_table(node, catalog, ctes)
            continue
        if isinstance(node, (exp.Alias, exp.TableAlias)) and (node.alias if isinstance(node, exp.Alias) else node.name).startswith("__"):
            raise SqlRefusal("construct_not_allowed", "names starting with '__' are reserved")
        if isinstance(node, exp.Join):
            joins += 1
            side, kind, method = (str(node.args.get(k) or "").upper() for k in ("side", "kind", "method"))
            if method or kind in {"CROSS", "SEMI", "ANTI", "STRAIGHT_JOIN"} or side in {"RIGHT", "FULL"} or (kind == "OUTER" and side != "LEFT"):
                raise SqlRefusal("join_not_allowed", "only INNER JOIN and LEFT JOIN with an ON/USING condition are offered")
            if node.args.get("on") is None and not node.args.get("using"):
                raise SqlRefusal("join_not_allowed", "every join needs an ON or USING condition (no cross or comma joins)")
            continue
        if isinstance(node, exp.Func) and not isinstance(node, allowed):
            raise SqlRefusal("function_not_allowed", f"function {node.sql_name().lower()}() is not offered", allowed=sorted(SQL_FUNCTIONS - {"like"}))
        if not isinstance(node, allowed):
            raise SqlRefusal("construct_not_allowed", f"{type(node).__name__} is not offered in the SQL surface")
        if isinstance(node, exp.Like) and node.args.get("escape") is not None:
            raise SqlRefusal("construct_not_allowed", "LIKE ... ESCAPE is not offered")
    if joins > max_joins:
        raise SqlRefusal("too_complex", f"{joins} joins; at most {max_joins}")


def _check_table(node: Any, catalog: SqlCatalog, ctes: set[str]) -> None:
    if not isinstance(node.this, exp.Identifier):
        raise SqlRefusal("system_object", "table-valued functions are not accepted")
    name = node.name.lower()
    if DANGEROUS.match(name):
        raise SqlRefusal("system_object", f"{name} is not a catalog table")
    if node.args.get("db") is not None or node.args.get("catalog") is not None:
        raise SqlRefusal("qualified_name", f"use bare table names: {node.sql(dialect='sqlite')} is qualified with a schema/database", allowed=sorted(catalog.tables))
    if name not in catalog.tables and name not in ctes:
        raise SqlRefusal("unknown_table", f"{name} is not a catalog table", allowed=sorted(catalog.tables))


def _check_limits(tree: Any) -> int | None:
    """The outer LIMIT (None: none); every LIMIT/OFFSET must be a non-negative integer literal."""
    for node in tree.find_all(exp.Limit, exp.Offset):
        value = node.expression
        if not (isinstance(value, exp.Literal) and not value.is_string and value.this.isdigit()):
            raise SqlRefusal("limit_invalid", "LIMIT/OFFSET take a non-negative integer literal")
    limit = tree.args.get("limit")
    return int(limit.expression.this) if limit is not None else None


# --- step 3: names ---------------------------------------------------------------------------


def _qualify(tree: Any, catalog: SqlCatalog) -> Any:
    from sqlglot.errors import OptimizeError
    from sqlglot.optimizer.qualify import qualify

    try:
        return qualify(tree, schema=catalog.schema(), dialect="sqlite", validate_qualify_columns=True, quote_identifiers=True, identify=True)
    except OptimizeError as error:
        raise SqlRefusal("unknown_column", _first_line(error)) from None
    except Exception as error:  # noqa: BLE001 - any other optimizer failure is a parse problem
        raise SqlRefusal("parse_error", _first_line(error)) from None


# --- step 4: values --------------------------------------------------------------------------


@dataclass
class Trace:
    alias: str
    table: str
    column: str
    wrappers: tuple[str, ...]


class _Scopes:
    def __init__(self, tree: Any, catalog: SqlCatalog):
        from sqlglot.optimizer.scope import build_scope

        self.catalog = catalog
        root = build_scope(tree)
        if root is None:
            raise SqlRefusal("parse_error", "the statement has no query scope")
        self.root = root
        self.all = list(root.traverse())
        self.by_select = {id(s.expression): s for s in self.all}

    def scope_of(self, node: Any) -> Any:
        current = node
        while current is not None:
            scope = self.by_select.get(id(current))
            if scope is not None:
                return scope
            current = current.parent
        return self.root

    @staticmethod
    def source(alias: str, scope: Any) -> tuple[Any, Any]:
        current = scope
        while current is not None:
            if alias in current.sources:
                return current.sources[alias], current
            current = current.parent
        return None, None

    def trace(self, node: Any, scope: Any, depth: int = 0) -> Trace | None:
        wrappers: list[str] = []
        while True:
            if isinstance(node, exp.Paren):
                node = node.this
            elif isinstance(node, (exp.Lower, exp.Upper, exp.Trim)):
                wrappers.append(type(node).__name__.lower())
                node = node.this
            elif isinstance(node, (exp.Date, exp.TsOrDsToTimestamp)):
                wrappers.append("temporal")
                node = node.this
            elif isinstance(node, exp.TimeToStr):
                wrappers.append("temporal")
                node = node.this
            elif isinstance(node, exp.Anonymous) and str(node.this).lower() in TEMPORAL_FUNCTIONS and node.expressions:
                name = str(node.this).lower()
                wrappers.append("julianday" if name == "julianday" else "temporal")
                node = node.expressions[1] if name == "strftime" and len(node.expressions) > 1 else node.expressions[0]
            else:
                break
        if not isinstance(node, exp.Column) or depth > 16:
            return None
        source, owner = self.source(node.table, scope)
        if isinstance(source, exp.Table) and source.name in self.catalog.tables:
            return Trace(alias=node.table, table=source.name, column=node.name, wrappers=tuple(reversed(wrappers)))
        if source is not None and hasattr(source, "expression") and isinstance(source.expression, exp.Select):
            projection = next((s for s in source.expression.selects if s.alias_or_name == node.name), None)
            if projection is None:
                return None
            inner = self.trace(projection.unalias(), source, depth + 1)
            if inner is None:
                return None
            return Trace(alias=inner.alias, table=inner.table, column=inner.column, wrappers=inner.wrappers + tuple(reversed(wrappers)))
        return None

    def columns_of(self, node: Any) -> list[Trace]:
        out = []
        for column in node.find_all(exp.Column):
            traced = self.trace(column, self.scope_of(column))
            if traced is not None:
                out.append(traced)
        return out


def _string_value(literal: Any) -> str | None:
    return literal.this if isinstance(literal, exp.Literal) and literal.is_string else None


def _number_value(literal: Any) -> float | None:
    if isinstance(literal, exp.Neg):
        inner = _number_value(literal.this)
        return -inner if inner is not None else None
    if isinstance(literal, exp.Literal) and not literal.is_string:
        try:
            return float(literal.this)
        except ValueError:
            return None
    return None


def _transform(value: str, wrappers: tuple[str, ...]) -> str:
    for wrapper in wrappers:
        if wrapper == "lower":
            value = value.lower()
        elif wrapper == "upper":
            value = value.upper()
        elif wrapper == "trim":
            value = value.strip()
    return value


class _Grounder:
    def __init__(self, catalog: SqlCatalog, resolver: ValueResolver):
        self.catalog = catalog
        self.resolver = resolver
        self.checked: list[dict[str, Any]] = []
        self.patterns: list[dict[str, Any]] = []
        # a multi-source setup grounds per table (``setup.SetupValues``); a ``ValueResolver`` per field
        self.table_aware = bool(getattr(resolver, "table_aware", False))

    def _spec(self, table: str, column: str) -> Any:
        """The field spec of ``table.column`` (``None``: a free-text column or the key)."""
        if self.table_aware:
            return self.resolver.table_field(table, column)  # type: ignore[attr-defined]
        return self.resolver.schema.field(column)

    def _universe(self, table: str, column: str) -> list[str] | None:
        """Every value the column may hold for this principal, when enumerable."""
        spec = self._spec(table, column)
        if column == KEY or spec is None:
            return None
        if spec.type == "enum":
            return list(spec.enum_values or [])
        if spec.listable and spec.max_catalog_cardinality > 0:
            if self.table_aware:
                return self.resolver.table_observed(table, column)  # type: ignore[attr-defined]
            return self.resolver.observed(column)
        return None

    def check(self, check: Any, traced: Trace | None) -> None:
        literal = check.literal
        text = _string_value(literal)
        number = _number_value(literal)
        if isinstance(literal, exp.Null):
            return
        if traced is None:
            if text is not None:
                raise SqlRefusal("ungroundable_value", f"{literal.sql(dialect='sqlite')} is compared with an expression that is not a catalog column; compare a column directly")
            return
        column = self.catalog.tables[traced.table].column(traced.column)
        assert column is not None
        kind = column.field_type
        label = f"{traced.table}.{traced.column}"
        if check.op in {"like", "instr"}:
            self._pattern(check, traced, kind, text, label)
            return
        if isinstance(literal, exp.Boolean):
            if kind != "boolean":
                raise SqlRefusal("type_mismatch", f"{label} is {kind}, not boolean")
            self.checked.append({"column": label, "op": check.op, "value": literal.this})
            return
        if "julianday" in traced.wrappers:
            if number is None:
                raise SqlRefusal("type_mismatch", f"julianday({label}) is a number")
            return
        if "temporal" in traced.wrappers:
            if kind not in TEMPORAL_TYPES or text is None or not _TEMPORAL_TEXT.match(text):
                raise SqlRefusal("unparseable_bound", f"{literal.sql(dialect='sqlite')} is not a date/time text value for a date function of {label} (quote it)")
            self.checked.append({"column": label, "op": check.op, "value": text, "via": "date function"})
            return
        if kind in {"integer", "number"}:
            value = number if number is not None else _parse_float(text)
            if value is None:
                raise SqlRefusal("type_mismatch", f"{label} is a {kind}; {literal.sql(dialect='sqlite')} is not a number")
            self.checked.append({"column": label, "op": check.op, "value": value})
            return
        if kind == "boolean":
            if number not in (0.0, 1.0):
                raise SqlRefusal("type_mismatch", f"{label} is stored as 0 or 1")
            self.checked.append({"column": label, "op": check.op, "value": int(number)})  # type: ignore[arg-type]
            return
        if text is None:
            raise SqlRefusal("type_mismatch", f"{label} is {kind}; compare it with a quoted text value")
        if kind in TEMPORAL_TYPES:
            self._temporal(check.op, text, kind, label)
            return
        if check.op not in {"eq", "neq", "in", "case", "is"}:
            raise SqlRefusal("type_mismatch", f"ordering comparisons on the text column {label} are not offered")
        if traced.column != KEY and self._spec(traced.table, traced.column) is None:
            # a free-text projection column (F6): no stored value to ground an equality against
            raise SqlRefusal("free_text_equality", f"{label} is free text; match a mention with LIKE '%word%' or instr(), never = / IN")
        # a bare column of a catalog table read directly (not through a derived projection or a
        # function): the only shape whose column the cross-field rule may rewrite
        direct = isinstance(check.subject, exp.Column) and not traced.wrappers and check.subject.table == traced.alias
        self._ground(traced, text, label, check.op, check, direct)

    def _temporal(self, op: str, text: str, kind: str, label: str) -> None:
        canonical = kind == "datetime" and bool(_CANONICAL_DATETIME.match(text))
        prefix = bool(_DATE_PREFIX.match(text))
        if not (canonical or prefix):
            form = "YYYY-MM-DDTHH:MM:SS.ffffff+00:00" if kind == "datetime" else "YYYY-MM-DD"
            raise SqlRefusal("unparseable_bound", f"{label} is stored as {form}; compare with 'YYYY', 'YYYY-MM', 'YYYY-MM-DD' or that exact form")
        try:
            date.fromisoformat((text + "-01-01")[:10] if len(text) == 4 else (text + "-01")[:10] if len(text) == 7 else text[:10])
        except ValueError:
            raise SqlRefusal("unparseable_bound", f"{text!r} is not a calendar date") from None
        full = canonical or (kind == "date" and len(text) == 10)
        if op in {"eq", "neq", "in", "case", "is"} and not full:
            raise SqlRefusal("unparseable_bound", f"{label}: equality with the partial value {text!r} never matches the stored form; use a range or date({label.split('.')[-1]})")
        self.checked.append({"column": label, "op": op, "value": text})

    def _ground(self, traced: Trace, text: str, label: str, op: str, check: Any = None, direct: bool = False) -> None:
        wrappers = tuple(w for w in traced.wrappers if w in {"lower", "upper", "trim"})
        universe = self._universe(traced.table, traced.column)
        if universe is not None:
            stored = [v for v in universe if isinstance(v, str)]
            if any(_transform(v, wrappers) == text for v in stored):
                self.checked.append({"column": label, "op": op, "value": text, "grounded_by": "observed" if traced.column != KEY else "exists"})
                return
            spec = self._spec(traced.table, traced.column)
            # 2026-11-09 (B5 q020/q041): the documented normalization of ``decision/value_match.py``;
            # a match executes the stored value (the literal is rewritten before binding) and is
            # recorded on the checked value; several matches are an explicit refusal, never a pick
            grounded = match_value(text, stored, casefold=bool(spec is not None and spec.casefold_match)) if spec is not None and traced.column != KEY else None
            if grounded is not None and grounded.ambiguous:
                raise SqlRefusal("ambiguous_value", f"{text!r} matches several values of {label} under the documented normalization: {grounded.ambiguous[:6]}", allowed=grounded.ambiguous[:20])
            if grounded is not None and grounded.found and check is not None:
                value = _transform(grounded.value or "", wrappers)
                check.literal.set("this", value)
                self.checked.append({"column": label, "op": op, "value": value, "grounded_by": "observed", "as_written": text, "rule": grounded.rule})
                return
            # the cross-field rule reads one field-keyed schema: not offered on a table-aware source
            if check is not None and direct and op == "eq" and not wrappers and spec is not None and not self.table_aware:
                moved = self.resolver.other_field(traced.column, text)
                if isinstance(moved, BoundError):
                    raise SqlRefusal("ambiguous_value", f"{text!r} is not a value of {label}; {moved}")
                if moved is not None:
                    other, match = moved
                    table = self.catalog.tables[traced.table]
                    if table.column(other.name) is not None:
                        check.subject.set("this", exp.to_identifier(other.name, quoted=True))
                        check.literal.set("this", match.value)
                        moved_label = f"{traced.table}.{other.name}"
                        self.checked.append({"column": moved_label, "op": op, "value": match.value, "grounded_by": "observed", "as_written": text, "rule": f"question_field:{match.rule}", "column_as_written": label})
                        return
            near = [v for v in stored if v.casefold() == text.casefold()]
            hint = f" (stored as {near[0]!r})" if near else ""
            raise SqlRefusal("value_not_observed", f"{text!r} is not a value of {label}{hint}", allowed=stored[:20])
        if wrappers:
            raise SqlRefusal("ungroundable_value", f"{label} has no enumerable values; compare it directly (exact lookup) without {wrappers[0]}()")
        if self.table_aware:
            if not self.resolver.table_exists(traced.table, traced.column, text):  # type: ignore[attr-defined]
                raise SqlRefusal("value_not_observed", f"{text!r} does not exist in {label} for this principal")
            self.checked.append({"column": label, "op": op, "value": text, "grounded_by": "exists"})
            return
        field_name = KEY if traced.column == KEY else traced.column
        if not self.resolver._store_ready():
            raise SqlRefusal("value_not_observed", f"no record store is available to verify {text!r} for {label}")
        if not self.resolver.records.exists(self.resolver.gen, field_name, text, self.resolver.acl):
            raise SqlRefusal("value_not_observed", f"{text!r} does not exist in {label} for this principal")
        self.checked.append({"column": label, "op": op, "value": text, "grounded_by": "exists"})

    def _pattern(self, check: Any, traced: Trace, kind: str, text: str | None, label: str) -> None:
        if text is None:
            raise SqlRefusal("type_mismatch", f"the {check.op} pattern for {label} must be quoted text")
        if kind != "string" or traced.column == KEY or self._universe(traced.table, traced.column) is not None:
            raise SqlRefusal("text_match_not_allowed", f"{check.op.upper()} is offered only on free-text columns; {label} has known values: use = or IN with one of them")
        self.patterns.append({"column": label, "op": check.op, "pattern": text})


def _parse_float(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


_PREDICATES: tuple[str, ...] = ("EQ", "NEQ", "GT", "GTE", "LT", "LTE", "In", "Between", "Is", "Like", "Where", "Having", "Join")


def _filter_position(predicate: Any, root: Any) -> str:
    """Where a grounded comparison sits, for the executed-SQL qualifier check (``route.py``):
    ``positive``/``negated`` only for a conjunct of the outermost statement's ``WHERE``/``HAVING``
    (through ``AND``, parentheses and ``NOT``; ``<>`` counts as one ``NOT``), or of a derived table or
    CTE the outermost statement reads as its only source (``_effective_roots``, 2026-11-09); anything
    else (``OR``, ``CASE``, select lists, join conditions, filter subqueries) is ``other`` and never
    counts as applying a qualifier."""
    numerator = _numerator_position(predicate, root)
    if numerator is not None:
        return numerator
    return _conjunct_position(predicate, root)


def _effective_roots(root: Any) -> set[int]:
    """The outermost statement and the statements it reads as its **only** source (2026-11-09,
    corpus-B B5 q043): a ``FROM (SELECT ...) AS r`` derived table, or a CTE named in ``FROM``, with no
    join beside it, recursively. Every row the outer statement reads passed that statement's
    ``WHERE``/``HAVING`` and carries its measures, so its conjuncts and conditional measures filter
    the result as the outer statement's own do. A derived table next to a join, a subquery in a
    filter or select list, and any other CTE stay ``other``."""
    ctes = {cte.alias_or_name: cte.this for cte in root.find_all(exp.CTE) if cte.parent is not None and cte.parent.parent is root}
    seen: set[int] = set()
    node = root
    while isinstance(node, exp.Select) and id(node) not in seen:
        seen.add(id(node))
        if node.args.get("joins"):
            break
        source = node.args.get("from_") or node.args.get("from")
        this = source.this if source is not None else None
        if isinstance(this, exp.Subquery) and isinstance(this.this, exp.Select):
            node = this.this
        elif isinstance(this, exp.Table) and this.name in ctes and isinstance(ctes[this.name], exp.Select):
            node = ctes[this.name]
        else:
            break
    return seen


def _conjunct_position(predicate: Any, root: Any) -> str:
    """``positive``/``negated`` when ``predicate`` is a conjunct of the ``WHERE``/``HAVING`` of the
    outermost statement or of a derived statement it reads as its only source (``_effective_roots``;
    through ``AND``, parentheses and ``NOT``), else ``other``."""
    negations = 0
    node = predicate
    roots = _effective_roots(root)
    while node is not None:
        parent = node.parent
        if isinstance(parent, (exp.Where, exp.Having)):
            if id(parent.parent) not in roots:
                return "other"
            if isinstance(predicate, exp.NEQ):
                negations += 1
            return "negated" if negations % 2 else "positive"
        if isinstance(parent, exp.Not):
            negations += 1
        elif not isinstance(parent, (exp.And, exp.Paren)):
            return "other"
        node = parent
    return "other"


def _text_leaf(predicate: Any) -> Any:
    """The boolean node of a text match: the ``LIKE`` itself, or ``instr(col, 'x') > 0`` around an
    ``instr`` (``StrPosition``) needle."""
    if isinstance(predicate, exp.StrPosition) and isinstance(predicate.parent, (exp.GT, exp.GTE, exp.NEQ)):
        return predicate.parent
    return predicate


def _any_of_group(leaf: Any, root: Any) -> Any | None:
    """The ``OR`` group whose disjunct ``leaf`` is, when that group (with its parentheses) is a
    positive conjunct of the outermost ``WHERE`` (2026-11-09, B4 q037/q041/q044 shapes:
    ``(subject LIKE '%x%' OR description LIKE '%x%')``), else ``None``."""
    node, top = leaf, None
    while isinstance(node.parent, (exp.Or, exp.Paren)):
        node = node.parent
        if isinstance(node, exp.Or):
            top = node
    if top is None or _conjunct_position(node, root) != "positive":
        return None
    return top


def _disjuncts(group: Any) -> list[Any]:
    out: list[Any] = []
    stack = [group]
    while stack:
        node = stack.pop()
        if isinstance(node, (exp.Or, exp.Paren)):
            stack.extend([node.this, node.expression] if isinstance(node, exp.Or) else [node.this])
        else:
            out.append(node)
    return out


def _numerator_position(predicate: Any, root: Any) -> str | None:
    """``numerator`` / ``numerator_negated`` (2026-11-09 corpus-B fix F4) when the comparison is a
    conjunct (through ``AND``, parentheses and ``NOT``) of the only condition of a conditional
    measure of the outermost statement: ``COUNT(CASE WHEN c THEN x END)`` (no ``ELSE`` or ``ELSE
    NULL``), ``SUM``/``TOTAL``/``AVG(CASE WHEN c THEN x ELSE 0 END)`` or ``IIF(c, x, 0)`` in the
    SELECT list, ``ORDER BY`` or ``HAVING``: the ratio numerator a share question asks for. A
    ``COUNT(CASE WHEN c THEN 1 ELSE 0 END)`` counts every row and never qualifies."""
    negations = 0
    node = predicate
    while True:
        parent = node.parent
        if isinstance(parent, exp.If) and parent.this is node:
            break
        if isinstance(parent, exp.Not):
            negations += 1
        elif not isinstance(parent, (exp.And, exp.Paren)):
            return None
        node = parent
    branch = parent
    if isinstance(branch.parent, exp.Case):
        case = branch.parent
        if case.this is not None or len(case.args.get("ifs") or []) != 1:
            return None
        default, measure = case.args.get("default"), case.parent
    else:  # IIF(c, x, y)
        default, measure = branch.args.get("false"), branch.parent
    then = branch.args.get("true")
    if then is None or isinstance(then, exp.Null):
        return None
    while isinstance(measure, (exp.Paren, exp.Cast)):
        measure = measure.parent
    empty = default is None or isinstance(default, exp.Null)
    zero = isinstance(default, exp.Literal) and not default.is_string and _number_value(default) == 0
    if isinstance(measure, exp.Count):
        if not empty:
            return None
    elif isinstance(measure, (exp.Sum, exp.Avg)) or (isinstance(measure, exp.Anonymous) and str(measure.this).lower() == "total"):
        if not (empty or zero):
            return None
    else:
        return None
    up = measure.parent
    while up is not None and not isinstance(up, exp.Select):
        if isinstance(up, (exp.Subquery, exp.Where, exp.Join)):
            return None
        up = up.parent
    if up is None or id(up) not in _effective_roots(root):
        return None  # B5 q043: a derived table read as the only source counts (``_effective_roots``)
    if isinstance(predicate, exp.NEQ):
        negations += 1
    return "numerator_negated" if negations % 2 else "numerator"


def _leftover_literals(tree: Any, checked: set[int]) -> None:
    """A text literal outside a checked comparison is refused when it sits in a filter position."""
    predicates = tuple(getattr(exp, name) for name in _PREDICATES)
    for literal in tree.find_all(exp.Literal):
        if not literal.is_string or id(literal) in checked:
            continue
        parent = literal.parent
        if isinstance(parent, exp.TimeToStr) and parent.args.get("format") is literal:
            if not _MODIFIER.match(literal.this):
                raise SqlRefusal("construct_not_allowed", f"strftime format {literal.this!r} is not offered")
            continue
        if isinstance(parent, exp.Anonymous) and str(parent.this).lower() in TEMPORAL_FUNCTIONS:
            if not _MODIFIER.match(literal.this):
                raise SqlRefusal("construct_not_allowed", f"date modifier {literal.this!r} is not offered")
            continue
        # up to the root: a label projected by a subquery that sits in a filter is a filter value too
        current = parent
        while current is not None:
            if isinstance(current, predicates):
                raise SqlRefusal("ungroundable_value", f"the text value {literal.sql(dialect='sqlite')} is used in a filter position the guard cannot ground; compare a catalog column with it directly")
            current = current.parent


# --- step 3b: declared join paths and units (M4 guard) -----------------------------------------


def _column_ref(node: Any, scopes: _Scopes) -> tuple[str, str] | None:
    """``(table, column)`` of a bare column reference that traces (through derived projections,
    without any function around it) to a catalog column; ``None`` for anything else."""
    while isinstance(node, exp.Paren):
        node = node.this
    if not isinstance(node, exp.Column):
        return None
    traced = scopes.trace(node, scopes.scope_of(node))
    if traced is None or traced.wrappers:
        return None
    return traced.table, traced.column


def _and_conjuncts(node: Any) -> list[Any]:
    out: list[Any] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, exp.Paren):
            stack.append(current.this)
        elif isinstance(current, exp.And):
            stack.extend([current.expression, current.this])
        else:
            out.append(current)
    return out


def _single_projection(query: Any) -> Any | None:
    """The one projected expression of a ``(SELECT x ...)`` subquery, else ``None``."""
    select = query.this if isinstance(query, exp.Subquery) else query
    if isinstance(select, exp.Select) and len(select.selects) == 1:
        return select.selects[0].unalias()
    return None


def _check_joins(tree: Any, scopes: _Scopes, catalog: SqlCatalog) -> list[str]:
    """Enforced join allowlist (unified merge §6.8): every way two tables meet on values must be an
    equality of bare columns along a declared path (``SqlCatalog.join_pairs``; an ``id = id``
    self-join of one table is an identity, not a path). Checked: every ``ON`` conjunct (``USING`` is
    expanded to ``ON`` by qualification), every correlated column of a subquery (only as one side of
    an equality conjunct of that subquery's ``WHERE``), and every ``IN (SELECT x ...)`` / ``= (SELECT
    x ...)`` whose subquery projects a non-aggregate (a semi-join on values; a computed measure such
    as ``= (SELECT max(n) ...)`` is a value comparison). Returns the paths used."""
    pairs = catalog.join_pairs()
    used: list[str] = []
    allowed = sorted(pairs.values())

    def declared(left: tuple[str, str] | None, right: tuple[str, str] | None, node: Any, where: str) -> None:
        text = node.sql(dialect="sqlite")
        if left is None or right is None:
            raise SqlRefusal("join_not_declared", f"{where} {text} is not an equality of two catalog columns along a declared join path", allowed=allowed)
        if left == right and left[1] == KEY:
            return
        label = pairs.get(frozenset({left, right}))
        if label is None:
            raise SqlRefusal("join_not_declared", f"{where} {text} joins {left[0]}.{left[1]} with {right[0]}.{right[1]}, which is not a declared join path", allowed=allowed)
        if label not in used:
            used.append(label)

    for join in tree.find_all(exp.Join):
        condition = join.args.get("on")
        if condition is None:
            raise SqlRefusal("join_not_declared", "every join needs an ON condition along a declared join path", allowed=allowed)
        for conjunct in _and_conjuncts(condition):
            if not isinstance(conjunct, exp.EQ):
                raise SqlRefusal("join_not_declared", f"join condition {conjunct.sql(dialect='sqlite')}: only equalities along declared join paths are accepted in ON (filter in WHERE)", allowed=allowed)
            declared(_column_ref(conjunct.this, scopes), _column_ref(conjunct.expression, scopes), conjunct, "join condition")
    for scope in scopes.all:
        for column in scope.external_columns:
            predicate = column.parent
            while isinstance(predicate, exp.Paren):
                predicate = predicate.parent
            position = predicate
            while position is not None and isinstance(position.parent, (exp.And, exp.Paren)):
                position = position.parent
            where = position.parent if position is not None else None
            if not isinstance(predicate, exp.EQ) or not isinstance(where, exp.Where) or where.parent is not scope.expression:
                raise SqlRefusal("join_not_declared", f"the outer column {column.sql(dialect='sqlite')} is used in a subquery other than as an equality along a declared join path in its WHERE", allowed=allowed)
            declared(_column_ref(predicate.this, scopes), _column_ref(predicate.expression, scopes), predicate, "correlated condition")
    for node in tree.find_all(exp.In, exp.EQ):
        if isinstance(node, exp.In):
            query = node.args.get("query")
            subject = node.this
        else:
            query = next((side for side in (node.this, node.expression) if isinstance(side, exp.Subquery)), None)
            subject = node.expression if query is node.this else node.this
        if query is None:
            continue
        inner = _single_projection(query)
        if inner is None:
            raise SqlRefusal("join_not_declared", f"{node.sql(dialect='sqlite')}: a subquery compared with a value projects exactly one column", allowed=allowed)
        if any(_is_aggregate(n) for n in inner.walk()):
            continue  # compared with a computed measure (``= (SELECT max(n) ...)``): a value, not a join
        declared(_column_ref(subject, scopes), _column_ref(inner, scopes), node, "subquery comparison")
    return used


def _joins_enforced(tree: Any, catalog: SqlCatalog) -> bool:
    """``enforced``: always; ``cross_source`` (one member's projection plus other sources' tables,
    ``sql_surface/members.py``): when the statement reads a table of another source (its
    ``SqlTable.source`` is set) -- a statement over the projection's own tables is guarded exactly
    as the projection catalog guards it (``shown``)."""
    if catalog.join_policy == "enforced":
        return True
    if catalog.join_policy != "cross_source":
        return False
    for node in tree.find_all(exp.Table):
        table = catalog.tables.get(node.name)
        if table is not None and table.source:
            return True
    return False


def _check_units(tree: Any, scopes: _Scopes, catalog: SqlCatalog) -> None:
    """Mixed-unit refusal (unified merge §6.8): the unit of an expression is its column's declared
    unit, kept through parentheses, CAST, sign, ``abs``/``round`` and ``sum``/``avg``/``min``/
    ``max``/``total``; ``+``/``-``, comparisons, ``BETWEEN``/``IN`` lists, ``COALESCE``/``IFNULL``/
    ``NULLIF``/``CASE``/``IIF`` branches and multi-argument ``min``/``max`` combine their operands
    and are refused with ``mixed_units`` when two of them carry different units. A product, ratio or
    modulo carries no unit (a rate of two measures is allowed); literals and counts are unitless."""
    memo: dict[int, str | None] = {}

    def combine(node: Any, parts: list[Any]) -> str | None:
        units = sorted({u for u in (unit(p) for p in parts if p is not None) if u})
        if len(units) > 1:
            raise SqlRefusal("mixed_units", f"{node.sql(dialect='sqlite')} combines values of different units {units}; convert or compare like with like (a ratio is allowed)")
        return units[0] if units else None

    def column_unit(node: Any, depth: int) -> str | None:
        source, _ = scopes.source(node.table, scopes.scope_of(node))
        if isinstance(source, exp.Table):
            table = catalog.tables.get(source.name)
            spec = table.column(node.name) if table is not None else None
            return spec.unit if spec is not None else None
        if source is not None and hasattr(source, "expression") and isinstance(source.expression, exp.Select) and depth < 16:
            projection = next((s for s in source.expression.selects if s.alias_or_name == node.name), None)
            return unit(projection.unalias(), depth + 1) if projection is not None else None
        return None

    def unit(node: Any, depth: int = 0) -> str | None:
        key = id(node)
        if key in memo:
            return memo[key]
        memo[key] = None
        result: str | None = None
        if isinstance(node, (exp.Paren, exp.Alias, exp.Cast, exp.Neg, exp.Abs, exp.Round, exp.Sum, exp.Avg)):
            result = unit(node.this, depth)
        elif isinstance(node, (exp.Min, exp.Max, exp.Coalesce, exp.Greatest, exp.Least)):
            result = combine(node, [node.this, *node.expressions])
        elif isinstance(node, exp.Column):
            result = column_unit(node, depth)
        elif isinstance(node, exp.Anonymous) and str(node.this).lower() in {"total", "ifnull"}:
            result = combine(node, list(node.expressions))
        elif isinstance(node, exp.Anonymous) and str(node.this).lower() == "iif":
            result = combine(node, list(node.expressions[1:]))
        elif isinstance(node, (exp.Add, exp.Sub, exp.Nullif)):
            result = combine(node, [node.this, node.expression])
        elif isinstance(node, exp.Case):
            result = combine(node, [*(branch.args.get("true") for branch in node.args.get("ifs") or []), node.args.get("default")])
        elif isinstance(node, exp.If):
            result = combine(node, [node.args.get("true"), node.args.get("false")])
        elif isinstance(node, (exp.Mul, exp.Div, exp.Mod)):
            unit(node.this, depth)
            unit(node.expression, depth)
        elif isinstance(node, exp.Subquery):
            inner = _single_projection(node)
            result = unit(inner, depth) if inner is not None else None
        memo[key] = result
        return result

    for node in tree.walk():
        if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
            combine(node, [node.this, node.expression])
        elif isinstance(node, exp.Between):
            combine(node, [node.this, node.args.get("low"), node.args.get("high")])
        elif isinstance(node, exp.In) and node.args.get("query") is None:
            combine(node, [node.this, *node.expressions])
        else:
            unit(node)


# --- step 5: binding -------------------------------------------------------------------------


def _is_aggregate(node: Any) -> bool:
    return isinstance(node, exp.AggFunc) or (isinstance(node, exp.Anonymous) and str(node.this).lower() in {"total", "group_concat"})


def _row_level(tree: Any, scopes: _Scopes) -> bool:
    if tree.args.get("group") is not None or tree.args.get("having") is not None or tree.args.get("distinct") is not None:
        return False
    if any(_is_aggregate(node) and scopes.scope_of(node) is scopes.root for s in tree.selects for node in s.walk()):
        return False
    sources = scopes.root.sources
    return bool(sources) and all(isinstance(src, exp.Table) and src.name in scopes.catalog.tables for src in sources.values())


def _condition(column: str, param: str, casefold: bool) -> str:
    ref = quote(column)
    return f"lower({ref}) = lower(:{param})" if casefold else f"{ref} = :{param}"


def guard(
    text: str,
    catalog: SqlCatalog,
    resolver: ValueResolver,
    *,
    acl: ScopePredicates,
    filters: dict[str, Any] | None = None,
    row_limit: int = 200,
    max_sql_chars: int = 4000,
    max_joins: int = 3,
    max_subqueries: int = 4,
    inject_acl: bool = True,
    group_lineage_max: int = 10,
) -> GuardedSql:
    from .values import extract_value_checks

    tree = _parse(text, max_sql_chars)
    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    shadow = sorted(ctes & set(catalog.tables))
    if shadow:
        raise SqlRefusal("cte_shadows_table", f"CTE names must differ from catalog tables: {shadow}")
    _check_nodes(tree, catalog, ctes, max_joins)
    model_limit = _check_limits(tree)
    offset_node = tree.args.get("offset")
    model_offset = int(offset_node.expression.this) if offset_node is not None else 0  # literal (``_check_limits``)
    tree = _qualify(tree, catalog)
    _check_nodes(tree, catalog, ctes, max_joins)
    scopes = _Scopes(tree, catalog)
    if len(scopes.all) - 1 > max_subqueries:
        raise SqlRefusal("too_complex", f"{len(scopes.all) - 1} subqueries/CTEs; at most {max_subqueries}")
    join_paths = _check_joins(tree, scopes, catalog) if _joins_enforced(tree, catalog) else []
    if catalog.has_units():
        _check_units(tree, scopes, catalog)

    # values
    grounder = _Grounder(catalog, resolver)
    checked: set[int] = set()
    pattern_groups: dict[int, tuple[Any, list[tuple[Any, dict[str, Any]]]]] = {}
    for check in extract_value_checks(tree):
        traced = scopes.trace(check.subject, scopes.scope_of(check.subject))
        before = len(grounder.checked)
        before_patterns = len(grounder.patterns)
        grounder.check(check, traced)
        position = _filter_position(check.predicate, tree)
        for entry in [*grounder.checked[before:], *grounder.patterns[before_patterns:]]:
            entry["filter"] = position
        for entry in grounder.patterns[before_patterns:]:
            if position == "other":
                leaf = _text_leaf(check.predicate)
                group = _any_of_group(leaf, tree)
                if group is not None:
                    pattern_groups.setdefault(id(group), (group, []))[1].append((leaf, entry))
        checked.add(id(check.literal))
        if isinstance(check.literal, exp.Neg):
            checked.add(id(check.literal.this))
    _leftover_literals(tree, checked)
    # An OR group of text matches only (``filter: any_of``): each disjunct is a LIKE/instr on a
    # free-text column; the route decides which question phrases it applies (``route.py``).
    for number, (group, members) in enumerate(pattern_groups.values(), start=1):
        leaves = {id(leaf) for leaf, _ in members}
        if all(id(node) in leaves for node in _disjuncts(group)):
            for _, entry in members:
                entry["filter"] = "any_of"
                entry["group"] = number

    tables = sorted({t.name for t in tree.find_all(exp.Table) if t.name in catalog.tables})
    referenced = sorted({(tr.table, tr.column) for c in tree.find_all(exp.Column) if (tr := scopes.trace(c, scopes.scope_of(c))) is not None})

    # request filters and the ACL (layer 1), pushed into every catalog table reference
    params: dict[str, Any] = {}
    masked: dict[str, Any] = {}
    conditions: dict[str, list[str]] = {t: [] for t in tables}
    for index, (name, value) in enumerate(sorted((filters or {}).items())):
        holders = [t for t in tables if catalog.tables[t].column(name) is not None]
        if not holders:
            raise SqlRefusal("filter_not_applicable", f"request filter {name!r} names no column of the referenced tables", allowed=tables)
        param = f"f_{index}"
        if grounder.table_aware:
            # per table: the value must ground in every table the filter binds (``SetupValues``)
            bound, casefold = resolver.request_value(holders, name, value)  # type: ignore[attr-defined]
            params[param] = masked[param] = bound
            for table in holders:
                conditions[table].append(_condition(name, param, casefold))
            continue
        resolution = resolver.resolve(Predicate(field=name, op="eq", value=value), from_request=True)
        if resolution.predicate is None:
            reason = resolution.unsupported.reason if resolution.unsupported else "value_not_observed"
            raise SqlRefusal("type_mismatch" if reason == "type_mismatch" else "value_not_observed", f"request filter {name!r}: {resolution.unsupported.suggestion if resolution.unsupported else 'not resolvable'}")
        column = catalog.tables[holders[0]].column(name)
        assert column is not None
        params[param] = masked[param] = _bound(column.field_type, resolution.predicate.value)
        spec = resolver.schema.field(name)
        casefold = bool(spec is not None and spec.casefold_match and column.field_type in TEXT_TYPES)
        for table in holders:
            conditions[table].append(_condition(name, param, casefold))
    if inject_acl:
        for index, predicate in enumerate(acl):
            if predicate.negate or predicate.op not in {"eq", "in"}:
                raise SqlRefusal("acl_not_applicable", "an access constraint shape the SQL surface cannot bind")
            missing = [t for t in tables if catalog.tables[t].column(predicate.field) is None]
            if missing:
                raise SqlRefusal("acl_not_applicable", f"an access constraint names a column {missing} lack")
            values = [predicate.value] if predicate.op == "eq" else list(predicate.value or [])  # type: ignore[arg-type]
            names = []
            for k, value in enumerate(values):
                param = f"acl_{index}_{k}"
                params[param], masked[param] = value, "<acl>"
                names.append(f":{param}")
            for table in tables:
                conditions[table].append(f"{quote(predicate.field)} IN ({', '.join(names)})" if names else "0")

    # row-level shape: hidden id/access columns for citations and the returned-row check
    shape: Literal["rows", "derived"] = "rows" if _row_level(tree, scopes) else "derived"
    sort_projected, tie_columns = _project_order(tree) if shape == "derived" else ([], [])
    ratios, ratio_projected = _ratio_parts(tree) if shape == "derived" else ([], [])
    lineage_alias = _lineage_alias(tree, scopes, model_limit) if shape == "derived" else None
    group_alias, group_keys = _group_keys(tree, scopes) if shape == "derived" and group_lineage_max > 0 else (None, [])
    source = tree.args.get("from_") or tree.args.get("from")  # sqlglot >= 26 names it ``from_``
    primary = source.this.alias_or_name if shape == "rows" and source is not None else None
    columns = [s.alias_or_name for s in tree.selects]
    citations: list[list[tuple[str, str, str]]] = []
    hidden: dict[str, tuple[str, str]] = {}
    if shape == "rows":
        for projection in tree.selects:
            cites = []
            for traced in scopes.columns_of(projection):
                if traced.alias in scopes.root.sources and (traced.alias, traced.table, traced.column) not in cites:
                    cites.append((traced.alias, traced.table, traced.column))
            citations.append(cites)
        extra = []
        for k, alias in enumerate(sorted(scopes.root.sources)):
            hidden[f"__sid_{k}"] = (alias, "id")
            extra.append(exp.alias_(exp.column(KEY, table=alias, quoted=True), f"__sid_{k}", quoted=True))
            source_table = scopes.root.sources[alias].name  # row-level: every source is a catalog table
            # a setup table without the access column (all-rows principals only: a tenant ACL on it
            # was refused above with ``acl_not_applicable``) carries no access value to check
            if catalog.access_column is not None and catalog.tables[source_table].column(catalog.access_column) is not None:
                hidden[f"__sacl_{k}"] = (alias, "acl")
                extra.append(exp.alias_(exp.column(catalog.access_column, table=alias, quoted=True), f"__sacl_{k}", quoted=True))
        tree.set("expressions", [*tree.expressions, *extra])
    else:
        citations = [[] for _ in columns]

    for table_node in list(tree.find_all(exp.Table)):
        name = table_node.name
        if name not in catalog.tables:
            continue
        where = " AND ".join(f"({c})" for c in conditions[name])
        inner = sqlglot.parse_one(f"SELECT * FROM {quote(name)}" + (f" WHERE {where}" if where else ""), read="sqlite")
        table_node.replace(exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier(table_node.alias_or_name, quoted=True))))
    # invariant (fail closed): every remaining table reference is a CTE or one of our bound reads
    bound = {quote(name) for name in catalog.tables}
    for table_node in tree.find_all(exp.Table):
        if table_node.name.lower() not in ctes and table_node.sql(dialect="sqlite") not in bound:
            raise SqlRefusal("unbound_table", f"the table reference {table_node.sql(dialect='sqlite')} could not be bound")

    # model literals -> named parameters (ordinal GROUP BY/ORDER BY positions stay inline)
    counter = 0
    for literal in list(tree.find_all(exp.Literal)):
        if isinstance(literal.parent, (exp.Group, exp.Ordered, exp.Limit, exp.Offset, exp.DataType)):
            continue
        if literal.find_ancestor(exp.DataType) is not None:
            continue
        counter += 1
        param = f"p_{counter}"
        params[param] = masked[param] = literal.this if literal.is_string else _number(literal.this)
        literal.replace(exp.Placeholder(this=param))
    keep = row_limit if model_limit is None else min(model_limit, row_limit)
    fetch = keep + 1 if model_limit is None or model_limit > row_limit else model_limit
    tie_window = 0
    peek_before = False
    if tie_columns and model_limit is not None and model_limit < row_limit:
        # the rows that tie the last one kept: up to ``tie_window`` past the LIMIT, plus one to tell
        # a complete tie group from an overflowing one (reported as truncated, never cut silently)
        tie_window = min(TIE_WINDOW, row_limit - model_limit)
        fetch = model_limit + tie_window + 1
        offset = tree.args.get("offset")
        if offset is not None and int(offset.expression.this) > 0:
            offset.set("expression", exp.Literal.number(int(offset.expression.this) - 1))
            fetch += 1
            peek_before = True
    else:
        tie_columns = []
    params["limit"] = masked["limit"] = fetch
    tree.set("limit", exp.Limit(expression=exp.Placeholder(this="limit")))
    sql = tree.sql(dialect="sqlite")
    _final_check(sql)
    lineage_sql = lineage_count_sql = None
    lineage_ids = max(LINEAGE_IDS, group_lineage_max)
    if lineage_alias is not None:
        lineage_sql, lineage_count_sql = _lineage_statements(tree, lineage_alias, lineage_ids)
        _final_check(lineage_sql)
        _final_check(lineage_count_sql)
    group_lineage_sql = None
    if group_alias is not None:
        group_lineage_sql = _group_lineage_statement(tree, group_alias, group_keys, group_lineage_max, fetch)
        _final_check(group_lineage_sql)

    out = GuardedSql(
        sql=sql,
        params=params,
        lineage_params=masked,
        shape=shape,
        columns=columns,
        citations=citations,
        hidden=hidden,
        primary=primary,
        tables=tables,
        referenced=referenced,
        joins=sum(1 for _ in tree.find_all(exp.Join)),
        limit=keep,
        acl_applied=inject_acl,
        checked=grounder.checked,
        text_patterns=grounder.patterns,
        lineage_sql=lineage_sql,
        lineage_count_sql=lineage_count_sql,
        sort_projected=sort_projected,
        tie_columns=tie_columns,
        tie_window=tie_window,
        peek_before=peek_before,
        offset=model_offset,
        ratios=ratios,
        ratio_projected=ratio_projected,
        group_lineage_sql=group_lineage_sql,
        group_keys=group_keys if group_lineage_sql else [],
        group_lineage_max=group_lineage_max if group_lineage_sql else 0,
        lineage_ids=lineage_ids,
        join_paths=join_paths,
    )
    out.fingerprint = hashlib.sha256(json.dumps([sql, params, catalog.fingerprint, catalog.generation, catalog.acl_key], sort_keys=True, default=str).encode()).hexdigest()
    return out


LINEAGE_IDS = 8  # contributor ids kept on a SQL aggregate (as the structured arm's lineage)
TIE_WINDOW = 50  # rows past a ranking LIMIT read to keep the ties of its last row (F3)


def _projected_at(expression: Any, selects: list[Any]) -> int | None:
    """The SELECT position an ORDER BY key reads: an output alias, or the same expression."""
    if isinstance(expression, exp.Column) and not expression.table:
        for i, select in enumerate(selects):
            if select.alias_or_name == expression.name:
                return i
    text = expression.sql(dialect="sqlite")
    for i, select in enumerate(selects):
        if (select.unalias() if isinstance(select, exp.Alias) else select).sql(dialect="sqlite") == text:
            return i
    if isinstance(expression, exp.Literal) and not expression.is_string and expression.this.isdigit():
        position = int(expression.this) - 1  # ORDER BY 2
        return position if 0 <= position < len(selects) else None
    return None


def _project_order(tree: Any) -> tuple[list[str], list[int]]:
    """Ranking shape (2026-11-09 corpus-B fix F3) of a grouped/aggregate outer statement: every
    aggregate ORDER BY key that is not in the SELECT list is projected (``sort_value``, ...), so the
    result carries the measure it was ranked by; and the tie columns: the positions of the aggregate
    ORDER BY keys (a secondary key such as the name never breaks a tie of the measure), else of
    every ORDER BY key when all are projected."""
    order = tree.args.get("order")
    if order is None or not order.expressions:
        return [], []
    added: list[str] = []
    measures: list[int] = []
    keys: list[int | None] = []
    for ordered in order.expressions:
        expression = ordered.this
        at = _projected_at(expression, tree.selects)
        if at is None and any(_is_aggregate(node) for node in expression.walk()):
            taken = {s.alias_or_name for s in tree.selects}
            name = next(n for n in ("sort_value", *(f"sort_value_{k}" for k in range(2, 100))) if n not in taken)
            tree.set("expressions", [*tree.expressions, exp.alias_(expression.copy(), name, quoted=True)])
            added.append(name)
            at = len(tree.selects) - 1
        keys.append(at)
        if at is not None and any(_is_aggregate(node) for node in tree.selects[at].walk()):
            measures.append(at)
    if measures:
        return added, list(dict.fromkeys(measures))
    return added, ([k for k in dict.fromkeys(keys) if k is not None] if all(k is not None for k in keys) else [])


def _lineage_alias(tree: Any, scopes: _Scopes, model_limit: int | None) -> str | None:
    """The FROM alias when the statement is one ungrouped aggregate over one catalog table with no
    join, subquery, CTE, DISTINCT, HAVING, LIMIT or OFFSET: its contributors are exactly the rows
    its WHERE admits. ``None`` otherwise (no lineage is claimed)."""
    if len(scopes.all) != 1 or model_limit is not None or tree.args.get("offset") is not None:
        return None
    if any(tree.args.get(key) is not None for key in ("group", "having", "distinct", "joins")) or tree.args.get("joins"):
        return None
    sources = scopes.root.sources
    if len(sources) != 1:
        return None
    alias, source = next(iter(sources.items()))
    if not (isinstance(source, exp.Table) and source.name in scopes.catalog.tables):
        return None
    if not tree.selects or not all(any(_is_aggregate(node) for node in s.walk()) for s in tree.selects):
        return None
    return alias


def _lineage_statements(tree: Any, alias: str, keep: int = LINEAGE_IDS) -> tuple[str, str]:
    """The id and count reads over the already bound (ACL, filters, parameters) statement: the
    first ``keep`` (+1, to tell a complete list from a cut one) ids by id, and their count."""
    ids = tree.copy()
    key = exp.column(KEY, table=alias, quoted=True)
    ids.set("expressions", [key])
    ids.set("order", exp.Order(expressions=[exp.Ordered(this=key.copy())]))
    ids.set("limit", exp.Limit(expression=exp.Literal.number(keep + 1)))
    count = tree.copy()
    count.set("expressions", [exp.Count(this=exp.Star())])
    count.set("order", None)
    count.set("limit", None)
    return ids.sql(dialect="sqlite"), count.sql(dialect="sqlite")


MAX_RATIOS = 4  # rate/ratio/share columns of one statement whose parts are made visible


def _ratio_operand(node: Any) -> Any:
    """A ratio operand without its CAST/NULLIF/parentheses and a constant factor ("100.0 * x")."""
    while True:
        if isinstance(node, (exp.Paren, exp.Cast, exp.Nullif)):
            node = node.this
        elif isinstance(node, exp.Mul) and isinstance(node.this, exp.Literal) and not node.this.is_string:
            node = node.expression
        elif isinstance(node, exp.Mul) and isinstance(node.expression, exp.Literal) and not node.expression.is_string:
            node = node.this
        else:
            return node


def _ratio_parts(tree: Any) -> tuple[list[tuple[int, int, int]], list[str]]:
    """Rate/ratio/share columns of the outermost statement (2026-11-09, corpus-B B7): a SELECT item
    whose outermost division divides two computed operands (both aggregates in an aggregate or
    grouped statement; both column expressions otherwise), with CAST/NULLIF and constant factors
    peeled. Each operand the statement does not already select (the same expression) is added as a
    visible column ``<ratio>_numerator`` / ``<ratio>_denominator``, so the result states N of D next
    to the rate. A ``DISTINCT`` statement is left as written (an added column changes its rows)."""
    if tree.args.get("distinct") is not None:
        return [], []
    aggregated = tree.args.get("group") is not None or any(_is_aggregate(n) for s in tree.selects for n in s.walk())
    ratios: list[tuple[int, int, int]] = []
    added: list[str] = []
    for position, select in enumerate(list(tree.selects)):
        if len(ratios) >= MAX_RATIOS:
            break
        division = next((n for n in select.walk() if isinstance(n, exp.Div) and n.find_ancestor(exp.Select) is tree), None)
        if division is None:
            continue
        parts = [_ratio_operand(division.this), _ratio_operand(division.expression)]
        if aggregated:
            if not all(any(_is_aggregate(n) for n in p.walk()) for p in parts):
                continue
        elif not all(p.find(exp.Column) is not None for p in parts):
            continue
        base = re.sub(r"\W+", "_", select.alias_or_name if isinstance(select, exp.Alias) else "ratio").strip("_") or "ratio"
        positions = []
        for part, suffix in zip(parts, ("numerator", "denominator")):
            at = _projected_at(part, tree.selects)
            if at is None or at == position:
                taken = {s.alias_or_name for s in tree.selects}
                name = next(n for n in (f"{base}_{suffix}", *(f"{base}_{suffix}_{k}" for k in range(2, 100))) if n not in taken)
                tree.set("expressions", [*tree.expressions, exp.alias_(part.copy(), name, quoted=True)])
                added.append(name)
                at = len(tree.selects) - 1
            positions.append(at)
        ratios.append((position, positions[0], positions[1]))
    return ratios, added


def _group_keys(tree: Any, scopes: _Scopes) -> tuple[str | None, list[int]]:
    """``(source alias, visible key positions)`` of a statement whose contributors per group can be
    read back: one ``GROUP BY`` statement over one catalog table (no join, subquery, CTE or
    ``DISTINCT``) whose every group key is a selected, non-aggregate expression. ``(None, [])``
    otherwise (no group lineage is claimed)."""
    group = tree.args.get("group")
    if group is None or not group.expressions or len(scopes.all) != 1 or tree.args.get("distinct") is not None or tree.args.get("joins"):
        return None, []
    sources = scopes.root.sources
    if len(sources) != 1:
        return None, []
    alias, source = next(iter(sources.items()))
    if not (isinstance(source, exp.Table) and source.name in scopes.catalog.tables):
        return None, []
    keys: list[int] = []
    for expression in group.expressions:
        at = _projected_at(expression, tree.selects)
        if at is None or any(_is_aggregate(n) for n in tree.selects[at].walk()):
            return None, []
        keys.append(at)
    return alias, list(dict.fromkeys(keys))


def _group_lineage_statement(tree: Any, alias: str, keys: list[int], small: int, fetch: int) -> str:
    """The contributor read of a grouped, already bound statement: ``(key..., id)`` of the rows of
    every group the statement returns (the statement itself, with its ACL/filter-bound source,
    WHERE, HAVING, ORDER BY, LIMIT and OFFSET, is the derived table the groups are taken from) that
    has at most ``small`` rows, ordered by key and id. NULL keys match with ``IS``. At most
    ``fetch * small`` rows qualify; one more is read to detect a surprise (then no lineage)."""
    selects = [s.unalias() if isinstance(s, exp.Alias) else s for s in tree.selects]
    groups = tree.copy()
    extra = [exp.alias_(selects[k].copy(), f"__lk_{i}", quoted=True) for i, k in enumerate(keys)]
    groups.set("expressions", [*groups.expressions, *extra, exp.alias_(exp.Count(this=exp.Star()), "__lc", quoted=True)])
    match = exp.and_(*(exp.Is(this=exp.column(f"__lk_{i}", table="__lg", quoted=True), expression=selects[k].copy()) for i, k in enumerate(keys)))
    size = exp.LTE(this=exp.column("__lc", table="__lg", quoted=True), expression=exp.Literal.number(small))
    probe = exp.select("1").from_(exp.Subquery(this=groups, alias=exp.TableAlias(this=exp.to_identifier("__lg", quoted=True)))).where(exp.and_(size, match))
    ids = tree.copy()
    key = exp.column(KEY, table=alias, quoted=True)
    ids.set("expressions", [*(selects[k].copy() for k in keys), key])
    for part in ("group", "having", "order", "limit", "offset"):
        ids.set(part, None)
    where = ids.args.get("where")
    condition = exp.Exists(this=probe)
    ids.set("where", exp.Where(this=exp.and_(where.this, condition) if where is not None else condition))
    ids.set("order", exp.Order(expressions=[*(exp.Ordered(this=selects[k].copy()) for k in keys), exp.Ordered(this=key.copy())]))
    ids.set("limit", exp.Limit(expression=exp.Literal.number(fetch * small + 1)))
    return ids.sql(dialect="sqlite")


def _number(text: str) -> int | float:
    return int(text) if re.fullmatch(r"\d+", text) else float(text)


def _final_check(sql: str) -> None:
    """The rendered statement is one comment-free SELECT (defence against a rendering surprise)."""
    from sqlglot.tokens import Tokenizer, TokenType

    tokens = Tokenizer(dialect="sqlite").tokenize(sql)
    if any(t.comments for t in tokens) or any(t.token_type == TokenType.SEMICOLON for t in tokens):
        raise SqlRefusal("comment", "the rendered statement is not a single comment-free SELECT")
    if not sql.upper().startswith(("SELECT", "WITH")):
        raise SqlRefusal("not_select", "the rendered statement is not a SELECT")
