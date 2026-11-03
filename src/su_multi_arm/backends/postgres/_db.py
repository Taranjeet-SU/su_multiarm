"""PostgreSQL naming, DDL, typed-field encoding and predicate compilation shared by the postgres facets.

Layout (one database, one ``table_prefix``):

* runtime schema ``<prefix>``: ``generations`` (one row per known generation: dataset, generation,
  its schema name, text-search configuration and metadata schema), ``meta`` (generation pointer
  ``CURRENT``, manifests, failure records; owned by the generation registry, never read by the
  facets), ``cache``, ``ledger``, ``jobs``, ``raw_replies``, ``sessions``;
* one schema **per generation**, ``<prefix>_<dataset>_<generation>`` (plan §3.5 naming) when that
  fits PostgreSQL's 63-byte identifier limit and the dataset is a lowercase identifier, otherwise
  ``<prefix>_g<sha256(dataset/generation)[:20]>``; the ``generations`` row records the mapping. It
  holds ``documents``, ``chunks``, ``document_fields``, ``records``, ``record_fields``, ``lexical``
  (two ``tsvector`` columns, GIN-indexed), ``vectors`` and ``vector_spaces`` (pgvector, created on
  first use). ``delete_generation`` is a single ``DROP SCHEMA ... CASCADE``.

Typed scope is pushed down through entity-attribute-value rows with **native typed columns**: one row
per value with its kind, raw text, casefolded text, ``double precision`` number and ``timestamptz``
instant. Temporal values are stored as UTC instants (naive = UTC, a date = its midnight UTC: the
``backends/_scope.py`` storage rule) and compared as instants, date fields by calendar day, so
results equal the sqlite adapter and the in-memory fake. Every value is a bound parameter; schema
and table names are generated here from validated identifiers and always double-quoted.
"""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from typing import Any

from ...core.errors import BackendError
from ...core.types import Page, Predicate, Scope
from .._scope import instant, temporal_key

KIND = "postgres"
PREFIX_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
DATASET_RE = re.compile(r"^[a-z0-9_]+$")
TS_CONFIG_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}(\.[a-z_][a-z0-9_]{0,62})?$")
MAX_IDENTIFIER = 63
MAX_TERMS = 128
C = 'COLLATE "C"'  # byte order for every id ordering, so pages match Python's sorted()


# --- naming -------------------------------------------------------------------------------------


def check_prefix(prefix: str) -> str:
    if not PREFIX_RE.match(prefix):
        raise BackendError(KIND, "naming", f"table_prefix {prefix!r} must match {PREFIX_RE.pattern}")
    return prefix


def check_ts_config(name: str) -> str:
    if not TS_CONFIG_RE.match(name):
        raise BackendError(KIND, "naming", f"text_search_config {name!r} is not a text search configuration name")
    return name


def schema_name(prefix: str, dataset: str, generation: str) -> str:
    """Per-generation schema: readable when it fits, otherwise a digest (mapping kept in ``generations``)."""
    readable = f"{prefix}_{dataset}_{generation}"
    if DATASET_RE.match(dataset) and len(readable) <= MAX_IDENTIFIER:
        return readable
    digest = hashlib.sha256(f"{dataset}/{generation}".encode()).hexdigest()[:20]
    return f"{prefix}_g{digest}"


def ident(name: str) -> str:
    """Double-quoted identifier for names this module generated (``[a-z0-9_]`` only)."""
    if not re.match(r"^[a-z0-9_]{1,63}$", name):
        raise BackendError(KIND, "naming", f"refusing identifier {name!r}")
    return f'"{name}"'


def runtime_ddl(rt: str) -> list[str]:
    return [
        f"CREATE SCHEMA IF NOT EXISTS {rt}",
        f"CREATE TABLE IF NOT EXISTS {rt}.generations(key text PRIMARY KEY, dataset text NOT NULL, generation text NOT NULL, "
        "schema_name text NOT NULL UNIQUE, index_format integer NOT NULL, fingerprint text NOT NULL, "
        "text_search_config text NOT NULL, metadata_schema text, created_at double precision NOT NULL)",
        f"CREATE TABLE IF NOT EXISTS {rt}.meta(key text PRIMARY KEY, value text NOT NULL)",
        f"CREATE TABLE IF NOT EXISTS {rt}.cache(key text PRIMARY KEY, value text NOT NULL, expires_at double precision NOT NULL)",
        f"CREATE TABLE IF NOT EXISTS {rt}.ledger(job_id text NOT NULL, sequence bigint NOT NULL, kind text NOT NULL, at text NOT NULL, "
        "payload text NOT NULL, PRIMARY KEY(job_id, sequence))",
        f"CREATE TABLE IF NOT EXISTS {rt}.jobs(job_id text PRIMARY KEY, status text NOT NULL)",
        f"CREATE TABLE IF NOT EXISTS {rt}.raw_replies(key text PRIMARY KEY, raw_reply text NOT NULL, valid boolean NOT NULL)",
        f"CREATE TABLE IF NOT EXISTS {rt}.sessions(id text PRIMARY KEY, data text NOT NULL, last_seen_at double precision NOT NULL)",
    ]


def _fields_table(s: str, name: str) -> list[str]:
    return [
        f"CREATE TABLE {s}.{name}(owner_id text NOT NULL, field text NOT NULL, kind text NOT NULL, text_value text, "
        "fold_value text, num_value double precision, ts_value timestamptz, is_null boolean NOT NULL)",
        f"CREATE INDEX ON {s}.{name}(owner_id, field)",
        f"CREATE INDEX ON {s}.{name}(field, text_value)",
        f"CREATE INDEX ON {s}.{name}(field, num_value)",
        f"CREATE INDEX ON {s}.{name}(field, ts_value)",
    ]


def generation_ddl(s: str) -> list[str]:
    return [
        f"CREATE SCHEMA {s}",
        f"CREATE TABLE {s}.documents(id text PRIMARY KEY, data text NOT NULL)",
        f"CREATE TABLE {s}.chunks(id text PRIMARY KEY, document_id text NOT NULL, field text NOT NULL, start_offset integer NOT NULL, "
        "end_offset integer NOT NULL, text text NOT NULL, recipe text NOT NULL)",
        f"CREATE INDEX ON {s}.chunks(document_id)",
        *_fields_table(s, "document_fields"),
        f"CREATE TABLE {s}.records(id text PRIMARY KEY, schema_id text NOT NULL, data text NOT NULL)",
        *_fields_table(s, "record_fields"),
        f"CREATE TABLE {s}.lexical(chunk_id text PRIMARY KEY, document_id text NOT NULL, field text NOT NULL, "
        "title_tsv tsvector NOT NULL, body_tsv tsvector NOT NULL)",
        f"CREATE INDEX ON {s}.lexical USING gin(title_tsv)",
        f"CREATE INDEX ON {s}.lexical USING gin(body_tsv)",
        f"CREATE INDEX ON {s}.lexical(document_id)",
    ]


def vector_ddl(s: str) -> list[str]:
    return [
        f"CREATE TABLE IF NOT EXISTS {s}.vector_spaces(namespace text PRIMARY KEY, dims integer NOT NULL)",
        f"CREATE TABLE IF NOT EXISTS {s}.vectors(namespace text NOT NULL, chunk_id text NOT NULL, document_id text NOT NULL, "
        "embedding vector NOT NULL, PRIMARY KEY(namespace, chunk_id))",
    ]


# --- paging -------------------------------------------------------------------------------------


def page_window(page: Page | None, default_size: int) -> tuple[int, int]:
    if default_size < 1:
        raise BackendError(KIND, "page", "k must be positive")
    try:
        page = page or Page(size=default_size)
    except ValueError as exc:
        raise BackendError(KIND, "page", f"invalid page: {exc}") from None
    try:
        offset = int(page.cursor) if page.cursor else 0
    except ValueError:
        raise BackendError(KIND, "page", "invalid cursor") from None
    if offset < 0:
        raise BackendError(KIND, "page", "invalid cursor")
    return offset, page.size


# --- typed field rows ---------------------------------------------------------------------------


def field_row(owner: str, field: str, value: Any) -> tuple:
    """``(owner, field, kind, text_value, fold_value, num_value, ts_value, is_null)`` for one scalar."""
    if value is None:
        return (owner, field, "null", None, None, None, None, True)
    if isinstance(value, bool):
        return (owner, field, "boolean", "true" if value else "false", None, 1.0 if value else 0.0, None, False)
    if isinstance(value, int):
        return (owner, field, "integer", None, None, float(value), None, False)
    if isinstance(value, float):
        return (owner, field, "number", None, None, value, None, False)
    text = str(value)
    stamp = instant(text)
    if stamp is not None:
        return (owner, field, "datetime", text, text.casefold(), None, stamp, False)
    return (owner, field, "string", text, text.casefold(), None, None, False)


def row_as_string(kind: str, text_value: Any, num_value: Any) -> str:
    """The listing form of a typed row (``values``): integers without a fraction, others as text."""
    if kind == "integer":
        return str(int(num_value))
    if kind == "number":
        return str(num_value)
    return str(text_value)


# --- predicate compilation ----------------------------------------------------------------------


def _scalar_condition(value: Any, casefold: bool, spec: Any = None) -> tuple[str, list[Any]]:
    if value is None:
        return "f.is_null", []
    if spec is not None and getattr(spec, "type", None) in {"datetime", "date"}:
        key = temporal_key(value, spec)
        if key is None:
            return "FALSE", []
        if spec.type == "date":  # calendar-date equality: the stored instant's UTC day
            return "f.kind='datetime' AND f.ts_value >= %s AND f.ts_value < %s", [key, key + timedelta(days=1)]
        return "f.kind='datetime' AND f.ts_value = %s", [key]
    if isinstance(value, bool):
        return "f.kind='boolean' AND f.num_value = %s", [1.0 if value else 0.0]
    if isinstance(value, (int, float)):
        return "f.kind IN ('integer','number') AND f.num_value = %s", [float(value)]
    text = str(value)
    if casefold:
        return "f.fold_value = %s", [text.casefold()]
    return "f.text_value = %s", [text]


def _bound_condition(bound: Any, operator: str, spec: Any = None) -> tuple[str, list[Any]]:
    if isinstance(bound, bool) or bound is None:
        return "FALSE", []
    if isinstance(bound, (int, float)):
        return f"f.kind IN ('integer','number') AND f.num_value {operator} %s", [float(bound)]
    text = str(bound)
    stamp = temporal_key(text, spec)
    if stamp is not None:
        # Instants on both sides; date fields: the bound is truncated to its UTC date.
        return f"f.kind='datetime' AND f.ts_value {operator} %s", [stamp]
    return f"f.kind='string' AND f.text_value {operator} %s", [text]


def predicate_sql(table: str, owner_expr: str, predicate: Predicate, casefold: bool, spec: Any = None) -> tuple[str, list[Any]]:
    """``EXISTS`` SQL for one predicate against a typed-field table (``table`` already qualified)."""
    params: list[Any] = [predicate.field]
    base = f"SELECT 1 FROM {table} f WHERE f.owner_id = {owner_expr} AND f.field = %s"
    if predicate.op == "missing":
        sql = f"NOT EXISTS ({base} AND NOT f.is_null)"
    elif predicate.op == "eq":
        cond, extra = _scalar_condition(predicate.value, casefold, spec)
        params.extend(extra)
        sql = f"EXISTS ({base} AND ({cond}))"
    elif predicate.op == "in":
        parts = []
        for candidate in list(predicate.value or []):
            cond, extra = _scalar_condition(candidate, casefold, spec)
            parts.append(f"({cond})")
            params.extend(extra)
        sql = f"EXISTS ({base} AND ({' OR '.join(parts) if parts else 'FALSE'}))"
    else:  # range / date_interval
        conditions = ["NOT f.is_null"]
        if predicate.lower is not None:
            cond, extra = _bound_condition(predicate.lower, ">=" if predicate.inclusive else ">", spec)
            conditions.append(f"({cond})")
            params.extend(extra)
        if predicate.upper is not None:
            inclusive_upper = predicate.inclusive and predicate.op == "range"  # date_interval is half-open
            cond, extra = _bound_condition(predicate.upper, "<=" if inclusive_upper else "<", spec)
            conditions.append(f"({cond})")
            params.extend(extra)
        sql = f"EXISTS ({base} AND {' AND '.join(conditions)})"
    return (f"NOT ({sql})" if predicate.negate else sql), params


def scope_sql(schema: str, document_expr: str, scope: Scope | None) -> tuple[str, list[Any]]:
    """Eligibility of a document (by expression) under a pushed-down ``Scope``; ``TRUE`` when unscoped.
    Document scope has no field specs and never casefolds (same as sqlite and the memory fake)."""
    if scope is None:
        return "TRUE", []
    clauses: list[str] = []
    params: list[Any] = []
    if scope.document_ids is not None:
        if not scope.document_ids:
            return "FALSE", []
        clauses.append(f"{document_expr} = ANY(%s)")
        params.append(list(scope.document_ids))
    if scope.exclude_ids:
        clauses.append(f"NOT ({document_expr} = ANY(%s))")
        params.append(list(scope.exclude_ids))
    for predicate in scope.predicates:
        sql, extra = predicate_sql(f"{schema}.document_fields", document_expr, predicate, False)
        clauses.append(sql)
        params.extend(extra)
    return (" AND ".join(clauses) if clauses else "TRUE"), params


# --- text search --------------------------------------------------------------------------------


def tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def tsquery_parts(query: Any) -> dict[str, Any] | None:
    """Fixed tsquery templates for a ``LexicalQuery``; ``None`` when nothing is searchable.

    Every user term is reduced to ``\\w+`` tokens and passed as a bound parameter to
    ``plainto_tsquery``/``phraseto_tsquery``, so no character of the user's is ever tsquery
    syntax. ``terms``: any term (OR of one ``plainto_tsquery`` per token, the FTS5 adapter's
    semantics); ``phrase``: ``phraseto_tsquery`` (contiguous lexemes, evaluated per column so a
    phrase never spans title and body); ``boolean``: the terms plus every ``must`` phrase and no
    ``must_not`` phrase, evaluated over the row (title or body), as FTS5 does across columns.
    """
    terms = list(dict.fromkeys(tokens(query.text)))[:MAX_TERMS]
    if not terms:
        return None
    if query.kind == "phrase":
        return {"kind": "phrase", "phrase": " ".join(terms), "must": [], "must_not": []}
    must = [" ".join(w) for w in (tokens(m) for m in query.must) if w] if query.kind == "boolean" else []
    must_not = [" ".join(w) for w in (tokens(m) for m in query.must_not) if w] if query.kind == "boolean" else []
    return {"kind": query.kind, "terms": terms, "must": must, "must_not": must_not}


def vector_literal(values: Any) -> str:
    return "[" + ",".join(repr(float(v)) for v in values) + "]"
