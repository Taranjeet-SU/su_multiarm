"""SQLite schema and SQL helpers shared by the sqlite facets.

Layout under the backend root: ``snapshots/<generation>/catalog.sqlite`` (one immutable catalog per
generation: documents, chunks, FTS5, typed field rows, records, graph, communities) plus the
per-generation vector files, and ``runtime.sqlite`` (cache, ledger, raw replies, sessions; keyed by
generation prefix where the contract requires it). Column names stay a superset of the v1 catalog so
the legacy reader's queries keep working on the tables it knows.

Typed scope is pushed down through an entity-attribute-value table (``document_fields`` /
``record_fields``): one row per value with its kind, text, casefolded text and numeric form (numbers
as themselves, timezone-aware stamps as epoch seconds). Predicates compile to ``EXISTS`` subqueries
with bound parameters; no value is ever interpolated into SQL text.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ...core.errors import BackendError
from ...core.types import Predicate, Scope
from .._scope import instant, temporal_key

KIND = "sqlite"

CATALOG_SCHEMA = """
CREATE TABLE documents(id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE chunks(id TEXT PRIMARY KEY, document_id TEXT, field TEXT, start INTEGER, end INTEGER, text TEXT, recipe TEXT NOT NULL DEFAULT 'default');
CREATE INDEX chunk_document ON chunks(document_id);
CREATE VIRTUAL TABLE fts USING fts5(chunk_id UNINDEXED, title, text, tokenize='unicode61');
CREATE TABLE document_fields(document_id TEXT, field TEXT, kind TEXT, text_value TEXT, fold_value TEXT, num_value REAL, is_null INTEGER);
CREATE INDEX document_fields_doc ON document_fields(document_id, field);
CREATE INDEX document_fields_value ON document_fields(field, text_value);
CREATE TABLE records(id TEXT PRIMARY KEY, schema_id TEXT, data TEXT NOT NULL);
CREATE TABLE record_fields(record_id TEXT, field TEXT, kind TEXT, text_value TEXT, fold_value TEXT, num_value REAL, is_null INTEGER);
CREATE INDEX record_fields_rec ON record_fields(record_id, field);
CREATE INDEX record_fields_value ON record_fields(field, text_value);
CREATE TABLE entities(key TEXT, name TEXT, kind TEXT, document_id TEXT, chunk_id TEXT, quote TEXT, start INTEGER, end INTEGER, match_level TEXT, field TEXT, canonical_id TEXT, aliases TEXT, first_token TEXT);
CREATE INDEX entity_key ON entities(key);
CREATE INDEX entity_first_token ON entities(first_token);
CREATE INDEX entity_document ON entities(document_id);
CREATE TABLE relations(subject TEXT, predicate TEXT, object TEXT, document_id TEXT, chunk_id TEXT, quote TEXT, start INTEGER, end INTEGER, match_level TEXT, edge_kind TEXT, support TEXT, field TEXT, status TEXT, provenance TEXT, occurrences TEXT);
CREATE INDEX relation_subject ON relations(subject);
CREATE INDEX relation_object ON relations(object);
CREATE TABLE memberships(community_id TEXT, member_id TEXT, role TEXT, basis TEXT);
CREATE TABLE communities(id TEXT PRIMARY KEY, label TEXT, documents TEXT, entities TEXT, report TEXT);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

RUNTIME_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, value TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ledger(job_id TEXT, sequence INTEGER, kind TEXT, at TEXT, payload TEXT, PRIMARY KEY(job_id, sequence));
CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS raw_replies(key TEXT PRIMARY KEY, raw_reply TEXT NOT NULL, valid INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, data TEXT NOT NULL, last_seen_at REAL NOT NULL);
"""


@contextmanager
def connect(path: Path, timeout: float, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    if readonly and not path.exists():
        raise BackendError(KIND, "connect", f"no catalog at {path}")
    uri = f"file:{path}?mode=ro" if readonly else f"file:{path}"
    db = sqlite3.connect(uri, uri=True, timeout=timeout)
    db.row_factory = sqlite3.Row
    try:
        yield db
        if not readonly:
            db.commit()
    finally:
        db.close()


def ensure_runtime(path: Path, timeout: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path, timeout) as db:
        db.executescript(RUNTIME_SCHEMA)


# --- typed field rows -------------------------------------------------------------------------


def field_row(owner: str, field: str, value: Any) -> tuple:
    """``(owner, field, kind, text_value, fold_value, num_value, is_null)`` for one scalar."""
    if value is None:
        return (owner, field, "null", None, None, None, 1)
    if isinstance(value, bool):
        return (owner, field, "boolean", "true" if value else "false", None, 1.0 if value else 0.0, 0)
    if isinstance(value, int):
        return (owner, field, "integer", None, None, float(value), 0)
    if isinstance(value, float):
        return (owner, field, "number", None, None, value, 0)
    text = str(value)
    stamp = instant(text)
    if stamp is not None:
        # Temporal values are stored as UTC instants (naive = UTC; a date = its midnight UTC) with a
        # sortable canonical text, so comparisons never fall back to text.
        return (owner, field, "datetime", stamp.isoformat(), text.casefold(), stamp.timestamp(), 0)
    return (owner, field, "string", text, text.casefold(), None, 0)


def _scalar_condition(alias: str, value: Any, casefold: bool, spec: Any = None) -> tuple[str, list[Any]]:
    if value is None:
        return f"{alias}.is_null=1", []
    if spec is not None and getattr(spec, "type", None) in {"datetime", "date"}:
        key = temporal_key(value, spec)
        if key is None:
            return "0", []
        if spec.type == "date":  # calendar-date equality: the stored instant's UTC day
            return f"{alias}.kind='datetime' AND {alias}.num_value >= ? AND {alias}.num_value < ?", [key.timestamp(), key.timestamp() + 86400]
        return f"{alias}.kind='datetime' AND {alias}.num_value=?", [key.timestamp()]
    if isinstance(value, bool):
        return f"{alias}.kind='boolean' AND {alias}.num_value=?", [1.0 if value else 0.0]
    if isinstance(value, (int, float)):
        return f"{alias}.kind IN ('integer','number') AND {alias}.num_value=?", [float(value)]
    text = str(value)
    if casefold:
        return f"{alias}.fold_value=?", [text.casefold()]
    return f"{alias}.text_value=?", [text]


def _bound_condition(alias: str, bound: Any, operator: str, spec: Any = None) -> tuple[str, list[Any]]:
    if isinstance(bound, bool) or bound is None:
        return "0", []
    if isinstance(bound, (int, float)):
        return f"{alias}.kind IN ('integer','number') AND {alias}.num_value {operator} ?", [float(bound)]
    text = str(bound)
    stamp = temporal_key(text, spec)
    if stamp is not None:
        # Instants on both sides. Date fields: the stored value is a midnight instant and the bound is
        # truncated to its UTC date, so ``<`` / ``>=`` on days behave as calendar comparisons.
        return f"{alias}.kind='datetime' AND {alias}.num_value {operator} ?", [stamp.timestamp()]
    return f"{alias}.kind='string' AND {alias}.text_value {operator} ?", [text]


def predicate_sql(table: str, owner_column: str, owner_expr: str, predicate: Predicate, casefold: bool, spec: Any = None) -> tuple[str, list[Any]]:
    """``EXISTS``-style SQL for one predicate against an EAV table; parameters are bound. ``spec`` is
    the field's ``FieldSpec`` when known (typed records); document scope has none."""
    alias = "f"
    params: list[Any] = [predicate.field]
    base = f"SELECT 1 FROM {table} {alias} WHERE {alias}.{owner_column}={owner_expr} AND {alias}.field=?"
    if predicate.op == "missing":
        sql = f"NOT EXISTS ({base} AND {alias}.is_null=0)"
    elif predicate.op == "eq":
        cond, extra = _scalar_condition(alias, predicate.value, casefold, spec)
        params.extend(extra)
        sql = f"EXISTS ({base} AND ({cond}))"
    elif predicate.op == "in":
        parts = []
        for candidate in list(predicate.value or []):
            cond, extra = _scalar_condition(alias, candidate, casefold, spec)
            parts.append(f"({cond})")
            params.extend(extra)
        sql = f"EXISTS ({base} AND ({' OR '.join(parts) if parts else '0'}))"
    else:  # range / date_interval
        conditions = [f"{alias}.is_null=0"]
        if predicate.lower is not None:
            cond, extra = _bound_condition(alias, predicate.lower, ">=" if predicate.inclusive else ">", spec)
            conditions.append(f"({cond})")
            params.extend(extra)
        if predicate.upper is not None:
            inclusive_upper = predicate.inclusive and predicate.op == "range"
            cond, extra = _bound_condition(alias, predicate.upper, "<=" if inclusive_upper else "<", spec)
            conditions.append(f"({cond})")
            params.extend(extra)
        sql = f"EXISTS ({base} AND {' AND '.join(conditions)})"
    return (f"NOT ({sql})" if predicate.negate else sql), params


def placeholders(values: list[Any]) -> str:
    return ",".join("?" for _ in values)


def scope_sql(document_expr: str, scope: Scope | None, casefold: Any = None) -> tuple[str, list[Any]]:
    """Eligibility of a document (by expression) under a pushed-down ``Scope``; ``1`` when unscoped."""
    if scope is None:
        return "1", []
    clauses, params = [], []
    if scope.document_ids is not None:
        if not scope.document_ids:
            return "0", []
        clauses.append(f"{document_expr} IN ({placeholders(scope.document_ids)})")
        params.extend(scope.document_ids)
    if scope.exclude_ids:
        clauses.append(f"{document_expr} NOT IN ({placeholders(scope.exclude_ids)})")
        params.extend(scope.exclude_ids)
    for predicate in scope.predicates:
        sql, extra = predicate_sql("document_fields", "document_id", document_expr, predicate, bool(casefold and casefold(predicate.field)))
        clauses.append(sql)
        params.extend(extra)
    return (" AND ".join(clauses) if clauses else "1"), params


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
