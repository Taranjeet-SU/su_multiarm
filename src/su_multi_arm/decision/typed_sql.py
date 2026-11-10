"""``typed_sql.lower`` (typed-query plan §5.3, MVP M5): a compiled typed query -> parameterised SQL.

Identifiers come only from the ``ProjectionCatalog`` (validated, double-quoted); every value is a
bound parameter; ``in`` value sets (including text-match document ids) are one JSON parameter read
through ``json_each(?)``. No model text reaches SQL. Deterministic: the same compiled query and
limits give byte-identical statements (golden-tested).

Predicate semantics follow the record stores (``backends/_scope.matches``): a negated predicate
also matches NULL (``NOT COALESCE(expr, 0)``); ``casefold_match`` columns compare ``lower()`` of
both sides (ASCII folding: SQLite's ``lower``); datetimes compare as normalised UTC instants and
date columns as ISO dates; ``date_interval`` is half-open; ``range`` honours ``inclusive``.

Statements (each over ``FROM grain g [JOIN joined j ON <declared key>] WHERE ...``):
``main`` (rows: ``SELECT [DISTINCT] g.id, columns, access columns ... ORDER BY g.id LIMIT n+1``;
groups: ``GROUP BY`` with the measure, the distinct grain count and NULL/rejected counts, ranked
or keyed order, ``LIMIT n+1``; scalar: the measure, the distinct grain count, the measure's NULL
count and the in-scope rejected count), ``contributors`` (up to 9 distinct grain ids),
``total`` (rows: distinct grain ids), ``pick`` (first/last_seen: the record carrying the value).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..backends._scope import instant
from ..core.config_projection import KEY
from ..core.types import Predicate
from ..core.types_typed import CompiledTypedQuery, TypedPredicate
from ..projection.builder import quote
from ..projection.catalog import ProjectionCatalog

CONTRIBUTORS = 8


@dataclass
class Lowered:
    statements: dict[str, tuple[str, list[Any]]] = field(default_factory=dict)
    shape: str = "rows"  # rows | groups | scalar
    columns: list[tuple[str, str]] = field(default_factory=list)  # (table, column) of selected values / group keys
    rejected_tables: list[str] = field(default_factory=list)  # rows: tables whose ``_rejected`` ends the row
    limit: int = 0


def _bound(column_type: str, value: Any) -> Any:
    """A predicate value in the projection's stored representation."""
    if value is None:
        return None
    if column_type == "boolean":
        return int(bool(value))
    if column_type == "datetime":
        moment = instant(value)
        return moment.isoformat(timespec="microseconds") if moment is not None else value
    if column_type == "date":
        moment = instant(value)
        return moment.date().isoformat() if moment is not None else value
    return value


def _ref(alias: str, column: str) -> str:
    return f"{quote(alias)}.{quote(column)}"


def _predicate(catalog: ProjectionCatalog, table: str, alias: str, predicate: Predicate) -> tuple[str, list[Any]]:
    column = catalog.tables[table].columns[predicate.field]
    ref = _ref(alias, predicate.field)
    casefold = column.casefold and column.type in {"string", "id", "enum"}
    params: list[Any] = []
    if predicate.op == "missing":
        expr = f"{ref} IS NULL"
    elif predicate.op == "eq":
        value = _bound(column.type, predicate.value)
        if value is None:
            expr = f"{ref} IS NULL"
        elif casefold and isinstance(value, str):
            expr, params = f"lower({ref}) = lower(?)", [value]
        else:
            expr, params = f"{ref} = ?", [value]
    elif predicate.op == "in":
        values = [_bound(column.type, v) for v in predicate.value or []]  # type: ignore[union-attr]
        if not values:
            expr = "0"
        elif casefold:
            expr, params = f"lower({ref}) IN (SELECT lower(value) FROM json_each(?))", [json.dumps(values)]
        else:
            expr, params = f"{ref} IN (SELECT value FROM json_each(?))", [json.dumps(values)]
    else:  # range / date_interval
        parts = [f"{ref} IS NOT NULL"]
        if predicate.lower is not None:
            parts.append(f"{ref} {'>=' if predicate.inclusive else '>'} ?")
            params.append(_bound(column.type, predicate.lower))
        if predicate.upper is not None:
            inclusive_upper = predicate.inclusive and predicate.op == "range"
            parts.append(f"{ref} {'<=' if inclusive_upper else '<'} ?")
            params.append(_bound(column.type, predicate.upper))
        expr = " AND ".join(parts)
    if predicate.negate:
        expr = f"NOT COALESCE(({expr}), 0)"
    return f"({expr})", params


def lower(compiled: CompiledTypedQuery, catalog: ProjectionCatalog, *, row_limit: int, max_groups: int, extra: list[TypedPredicate] | None = None) -> Lowered:
    """``extra``: predicates the arm adds at execution (the text match as an ``id in`` set)."""
    if compiled.errors:
        raise ValueError("a typed query with compile errors is never lowered")
    query = compiled.query
    grain = query.grain
    aliases = {grain: "g"}
    source = f"{quote(grain)} AS {quote('g')}"
    if query.join is not None:
        relation = catalog.relations[query.join]
        joined = relation.parent if grain == relation.child else relation.child
        aliases[joined] = "j"
        if grain == relation.child:
            on = f"{_ref('g', relation.column)} = {_ref('j', KEY)}"
        else:
            on = f"{_ref('j', relation.column)} = {_ref('g', KEY)}"
        source += f" JOIN {quote(joined)} AS {quote('j')} ON {on}"
    conditions: list[str] = []
    params: list[Any] = []
    for item in [*compiled.filters_applied, *query.where, *(extra or [])]:
        sql, values = _predicate(catalog, item.table, aliases[item.table], item.predicate)
        conditions.append(sql)
        params.extend(values)
    where = " AND ".join(conditions) if conditions else "1"
    base = f"FROM {source} WHERE {where}"
    distinct_grain = f"COUNT(DISTINCT {_ref('g', KEY)})"
    referenced = sorted(
        {(item.table, item.predicate.field) for item in query.where}
        | {(c.table, c.column) for c in [*query.group_by, *query.select]}
        | ({_measure_ref(query, grain)} if _measure_ref(query, grain) else set())
    )
    rejected_sql = " OR ".join(f"instr(COALESCE({_ref(aliases[t], '_rejected')}, ''), ?) > 0" for t, c in referenced if c != KEY) or "0"
    rejected_params = [json.dumps(c) for t, c in referenced if c != KEY]
    rejected = f"SUM(CASE WHEN {rejected_sql} THEN 1 ELSE 0 END)"
    lowered = Lowered()
    lowered.statements["contributors"] = (f"SELECT DISTINCT {_ref('g', KEY)} {base} ORDER BY {_ref('g', KEY)} LIMIT {CONTRIBUTORS + 1}", list(params))
    access = catalog.access_column
    if query.measure is None and not query.group_by:
        lowered.shape = "rows"
        lowered.columns = [(c.table, c.column) for c in query.select]
        limit = min(query.limit or row_limit, row_limit)
        lowered.limit = limit
        # a distinct-guarded (parent) list reads grain columns only; otherwise the joined row's key
        # is cited and every alias's access column is returned for the arm's row check
        read = [grain] if compiled.fanout == "distinct_guarded" else list(aliases)
        extras = [(t, KEY) for t in read if t != grain] + ([(t, access) for t in read] if access else [])
        selected = [_ref("g", KEY)] + [_ref(aliases[t], c) for t, c in lowered.columns] + [_ref(aliases[t], c) for t, c in extras] + [_ref(aliases[t], "_rejected") for t in read]
        keyword = "SELECT DISTINCT" if compiled.fanout == "distinct_guarded" else "SELECT"
        lowered.columns += extras
        lowered.rejected_tables = read
        lowered.statements["main"] = (f"{keyword} {', '.join(selected)} {base} ORDER BY {_ref('g', KEY)} LIMIT {limit + 1}", list(params))
        lowered.statements["total"] = (f"SELECT {distinct_grain}, {rejected} {base}", [*rejected_params, *params])
        return lowered
    measure_sql, null_sql = _measure_sql(query, grain, aliases, compiled.fanout)
    if query.group_by:
        lowered.shape = "groups"
        lowered.columns = [(c.table, c.column) for c in query.group_by]
        keys = [_ref(aliases[t], c) for t, c in lowered.columns]
        limit = min(query.top_n or max_groups, max_groups)
        lowered.limit = limit
        # columns: 1 measure, 2 distinct grain rows, 3 NULL measure values, 4 rejected, 5.. keys
        key_order = ", ".join(str(i + 5) for i in range(len(keys)))
        order = f"1 {'DESC' if query.order == 'desc' else 'ASC'}, {key_order}" if query.order is not None else key_order
        sql = f"SELECT {measure_sql}, {distinct_grain}, {null_sql}, {rejected}, {', '.join(keys)} {base} GROUP BY {', '.join(keys)} ORDER BY {order} LIMIT {limit + 1}"
        lowered.statements["main"] = (sql, [*rejected_params, *params])
        lowered.statements["total"] = (f"SELECT {distinct_grain}, {rejected} {base}", [*rejected_params, *params])
        return lowered
    lowered.shape = "scalar"
    lowered.statements["main"] = (f"SELECT {measure_sql}, {distinct_grain}, {null_sql}, {rejected} {base}", [*rejected_params, *params])
    kind = query.measure.kind if query.measure is not None else "count"
    if kind in {"first_seen", "last_seen"}:
        table, column = _measure_ref(query, grain)  # type: ignore[misc]
        ref = _ref(aliases[table], column)
        direction = "ASC" if kind == "first_seen" else "DESC"
        lowered.statements["pick"] = (f"SELECT {ref}, {_ref('g', KEY)} {base} AND {ref} IS NOT NULL ORDER BY {ref} {direction}, {_ref('g', KEY)} {direction} LIMIT 1", list(params))
    return lowered


def _measure_ref(query: Any, grain: str) -> tuple[str, str] | None:
    measure = query.measure
    if measure is None or measure.kind == "count":
        return None
    name = measure.unit if measure.kind == "count_distinct" else measure.field
    if not name:
        return None
    table, _, column = name.rpartition(".")
    return (table or grain, column)


def _measure_sql(query: Any, grain: str, aliases: dict[str, str], fanout: str) -> tuple[str, str]:
    """``(measure expression, NULL-count expression)``."""
    measure = query.measure
    kind = "count" if measure is None else measure.kind
    if kind == "count":
        return (f"COUNT(DISTINCT {_ref('g', KEY)})" if fanout == "distinct_guarded" else "COUNT(*)"), "0"
    ref_pair = _measure_ref(query, grain)
    if ref_pair is None:  # count_distinct of the grain key
        return f"COUNT(DISTINCT {_ref('g', KEY)})", "0"
    ref = _ref(aliases[ref_pair[0]], ref_pair[1])
    nulls = f"SUM(CASE WHEN {ref} IS NULL THEN 1 ELSE 0 END)"
    function = {"count_distinct": "COUNT(DISTINCT {})", "sum": "SUM({})", "avg": "AVG({})", "min": "MIN({})", "max": "MAX({})", "first_seen": "MIN({})", "last_seen": "MAX({})"}[kind]
    return function.format(ref), nulls
