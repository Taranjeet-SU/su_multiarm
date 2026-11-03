"""SQLAlchemy Core tables, typed-field encoding and predicate compilation for the generic SQL adapter.

Dialect-agnostic by construction: only portable column types (``String``, ``Text``, ``Integer``,
``Double``, ``Boolean``), no dialect-specific upsert (replace = delete + insert in one transaction),
no JSON or array operators, every value bound. Layout under ``table_prefix``:

* runtime tables ``<prefix>_generations`` (``key`` = ``GenerationId.prefix()``, its table token and
  metadata schema), ``<prefix>_meta`` (generation pointer, manifests, failures, write lease; owned
  by the generation registry), ``<prefix>_cache``, ``<prefix>_ledger``, ``<prefix>_jobs``,
  ``<prefix>_raw_replies``, ``<prefix>_sessions``;
* per-generation tables ``<prefix>_<token>_{docs,chunks,dfields,records,rfields}`` where ``token``
  is ``sha256(dataset/generation)[:12]``: the plan's ``<prefix>_<dataset>_<generation>`` does not
  fit the 63/64-byte identifier limits of PostgreSQL and MySQL, so the readable mapping lives in
  ``<prefix>_generations``. ``delete_generation`` drops the five tables.

Typed scope uses entity-attribute-value rows (kind, raw text, casefolded text, ``num_value``):
numbers as themselves, temporal values as UTC epoch seconds (naive = UTC, a date = its midnight UTC,
the ``backends/_scope.py`` storage rule) because no portable timezone-aware type exists across
dialects. Results equal the sqlite adapter and the in-memory fake.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import sqlalchemy as sa

from ...core.errors import BackendError
from ...core.types import Page, Predicate, Scope
from .._scope import instant, temporal_key

KIND = "sqlalchemy"
PREFIX_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
ID = 512  # bounded key columns (MySQL index-length limits); longer ids are a typed error
TESTED_DIALECTS = frozenset({"sqlite", "postgresql"})


def check_prefix(prefix: str) -> str:
    if not PREFIX_RE.match(prefix):
        raise BackendError(KIND, "naming", f"table_prefix {prefix!r} must match {PREFIX_RE.pattern}")
    return prefix


def generation_token(dataset: str, generation: str) -> str:
    return hashlib.sha256(f"{dataset}/{generation}".encode()).hexdigest()[:12]


def check_ids(ids: Any, operation: str) -> None:
    for value in ids:
        if len(value) > ID:
            raise BackendError(KIND, operation, f"id longer than {ID} characters")


# --- tables -------------------------------------------------------------------------------------


def runtime_tables(prefix: str) -> tuple[sa.MetaData, dict[str, sa.Table]]:
    md = sa.MetaData()
    tables = {
        "generations": sa.Table(
            f"{prefix}_generations",
            md,
            sa.Column("key", sa.String(ID), primary_key=True),
            sa.Column("dataset", sa.String(ID), nullable=False),
            sa.Column("generation", sa.String(32), nullable=False),
            sa.Column("token", sa.String(12), nullable=False, unique=True),
            sa.Column("index_format", sa.Integer, nullable=False),
            sa.Column("fingerprint", sa.String(64), nullable=False),
            sa.Column("metadata_schema", sa.Text),
            sa.Column("created_at", sa.Double, nullable=False),
        ),
        "meta": sa.Table(f"{prefix}_meta", md, sa.Column("key", sa.String(ID), primary_key=True), sa.Column("value", sa.Text, nullable=False)),
        "cache": sa.Table(
            f"{prefix}_cache",
            md,
            sa.Column("key", sa.String(768), primary_key=True),
            sa.Column("value", sa.Text, nullable=False),
            sa.Column("expires_at", sa.Double, nullable=False),
        ),
        "ledger": sa.Table(
            f"{prefix}_ledger",
            md,
            sa.Column("job_id", sa.String(ID), primary_key=True),
            sa.Column("sequence", sa.BigInteger, primary_key=True, autoincrement=False),
            sa.Column("kind", sa.String(255), nullable=False),
            sa.Column("at", sa.String(255), nullable=False),
            sa.Column("payload", sa.Text, nullable=False),
        ),
        "jobs": sa.Table(f"{prefix}_jobs", md, sa.Column("job_id", sa.String(ID), primary_key=True), sa.Column("status", sa.String(64), nullable=False)),
        "raw_replies": sa.Table(
            f"{prefix}_raw_replies",
            md,
            sa.Column("key", sa.String(768), primary_key=True),
            sa.Column("raw_reply", sa.Text, nullable=False),
            sa.Column("valid", sa.Boolean, nullable=False),
        ),
        "sessions": sa.Table(
            f"{prefix}_sessions",
            md,
            sa.Column("id", sa.String(ID), primary_key=True),
            sa.Column("data", sa.Text, nullable=False),
            sa.Column("last_seen_at", sa.Double, nullable=False),
        ),
    }
    return md, tables


def _fields(md: sa.MetaData, name: str, index_base: str) -> sa.Table:
    return sa.Table(
        name,
        md,
        sa.Column("owner_id", sa.String(ID), nullable=False),
        sa.Column("field", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("text_value", sa.Text),
        sa.Column("fold_value", sa.Text),
        sa.Column("num_value", sa.Double),
        sa.Column("is_null", sa.Boolean, nullable=False),
        sa.Index(f"{index_base}o", "owner_id", "field"),
        sa.Index(f"{index_base}n", "field", "num_value"),
    )


def generation_tables(prefix: str, token: str) -> tuple[sa.MetaData, dict[str, sa.Table]]:
    md = sa.MetaData()
    base = f"{prefix}_{token}"
    tables = {
        "documents": sa.Table(f"{base}_docs", md, sa.Column("id", sa.String(ID), primary_key=True), sa.Column("data", sa.Text, nullable=False)),
        "chunks": sa.Table(
            f"{base}_chunks",
            md,
            sa.Column("id", sa.String(ID), primary_key=True),
            sa.Column("document_id", sa.String(ID), nullable=False),
            sa.Column("field", sa.String(255), nullable=False),
            sa.Column("start_offset", sa.Integer, nullable=False),
            sa.Column("end_offset", sa.Integer, nullable=False),
            sa.Column("text", sa.Text, nullable=False),
            sa.Column("recipe", sa.String(255), nullable=False),
            sa.Index(f"{base}_cd", "document_id"),
        ),
        "document_fields": _fields(md, f"{base}_dfields", f"{base}_df"),
        "records": sa.Table(
            f"{base}_records",
            md,
            sa.Column("id", sa.String(ID), primary_key=True),
            sa.Column("schema_id", sa.String(255), nullable=False),
            sa.Column("data", sa.Text, nullable=False),
        ),
        "record_fields": _fields(md, f"{base}_rfields", f"{base}_rf"),
    }
    return md, tables


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


def binary_order(column: Any, dialect: str) -> Any:
    """Byte-order ordering of id columns so pages match Python's ``sorted`` on every tested dialect."""
    if dialect == "postgresql":
        return column.collate("C")
    if dialect in {"mysql", "mariadb"}:
        return column.collate("utf8mb4_bin")
    return column  # SQLite's default BINARY collation


# --- typed field rows ---------------------------------------------------------------------------


def field_row(owner: str, field: str, value: Any) -> dict[str, Any]:
    if value is None:
        return {"owner_id": owner, "field": field, "kind": "null", "text_value": None, "fold_value": None, "num_value": None, "is_null": True}
    if isinstance(value, bool):
        return {"owner_id": owner, "field": field, "kind": "boolean", "text_value": "true" if value else "false", "fold_value": None, "num_value": 1.0 if value else 0.0, "is_null": False}
    if isinstance(value, int):
        return {"owner_id": owner, "field": field, "kind": "integer", "text_value": None, "fold_value": None, "num_value": float(value), "is_null": False}
    if isinstance(value, float):
        return {"owner_id": owner, "field": field, "kind": "number", "text_value": None, "fold_value": None, "num_value": value, "is_null": False}
    text = str(value)
    stamp = instant(text)
    kind = "string" if stamp is None else "datetime"
    return {
        "owner_id": owner,
        "field": field,
        "kind": kind,
        "text_value": text,
        "fold_value": text.casefold(),
        "num_value": None if stamp is None else stamp.timestamp(),
        "is_null": False,
    }


def row_as_string(kind: str, text_value: Any, num_value: Any) -> str:
    if kind == "integer":
        return str(int(num_value))
    if kind == "number":
        return str(num_value)
    return str(text_value)


# --- predicate compilation ----------------------------------------------------------------------

NUMERIC = ("integer", "number")


def _scalar(f: sa.Table, value: Any, casefold: bool, spec: Any = None) -> Any:
    if value is None:
        return f.c.is_null.is_(True)
    if spec is not None and getattr(spec, "type", None) in {"datetime", "date"}:
        key = temporal_key(value, spec)
        if key is None:
            return sa.false()
        if spec.type == "date":
            return sa.and_(f.c.kind == "datetime", f.c.num_value >= key.timestamp(), f.c.num_value < key.timestamp() + 86400)
        return sa.and_(f.c.kind == "datetime", f.c.num_value == key.timestamp())
    if isinstance(value, bool):
        return sa.and_(f.c.kind == "boolean", f.c.num_value == (1.0 if value else 0.0))
    if isinstance(value, (int, float)):
        return sa.and_(f.c.kind.in_(NUMERIC), f.c.num_value == float(value))
    text = str(value)
    return f.c.fold_value == text.casefold() if casefold else f.c.text_value == text


def _bound(f: sa.Table, bound: Any, op: str, spec: Any = None) -> Any:
    if isinstance(bound, bool) or bound is None:
        return sa.false()
    compare = {">=": lambda c, v: c >= v, ">": lambda c, v: c > v, "<=": lambda c, v: c <= v, "<": lambda c, v: c < v}[op]
    if isinstance(bound, (int, float)):
        return sa.and_(f.c.kind.in_(NUMERIC), compare(f.c.num_value, float(bound)))
    text = str(bound)
    stamp = temporal_key(text, spec)
    if stamp is not None:
        return sa.and_(f.c.kind == "datetime", compare(f.c.num_value, stamp.timestamp()))
    return sa.and_(f.c.kind == "string", compare(f.c.text_value, text))


def predicate_clause(fields: sa.Table, owner: Any, predicate: Predicate, casefold: bool, spec: Any = None) -> Any:
    """``EXISTS`` clause for one predicate against a typed-field table, correlated to ``owner``."""
    f = fields.alias()
    base = [f.c.owner_id == owner, f.c.field == predicate.field]
    if predicate.op == "missing":
        clause = ~sa.exists().where(*base, f.c.is_null.is_(False))
    elif predicate.op == "eq":
        clause = sa.exists().where(*base, _scalar(f, predicate.value, casefold, spec))
    elif predicate.op == "in":
        options = [_scalar(f, c, casefold, spec) for c in list(predicate.value or [])]
        clause = sa.exists().where(*base, sa.or_(*options) if options else sa.false())
    else:
        conditions = [f.c.is_null.is_(False)]
        if predicate.lower is not None:
            conditions.append(_bound(f, predicate.lower, ">=" if predicate.inclusive else ">", spec))
        if predicate.upper is not None:
            inclusive_upper = predicate.inclusive and predicate.op == "range"
            conditions.append(_bound(f, predicate.upper, "<=" if inclusive_upper else "<", spec))
        clause = sa.exists().where(*base, *conditions)
    return sa.not_(clause) if predicate.negate else clause


def scope_clause(document_fields: sa.Table, document_id: Any, scope: Scope | None) -> Any:
    """Eligibility of a document under a pushed-down ``Scope`` (document scope never casefolds)."""
    if scope is None:
        return sa.true()
    clauses = []
    if scope.document_ids is not None:
        if not scope.document_ids:
            return sa.false()
        clauses.append(document_id.in_(list(scope.document_ids)))
    if scope.exclude_ids:
        clauses.append(document_id.not_in(list(scope.exclude_ids)))
    for predicate in scope.predicates:
        clauses.append(predicate_clause(document_fields, document_id, predicate, False))
    return sa.and_(*clauses) if clauses else sa.true()
