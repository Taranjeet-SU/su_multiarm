"""MongoDB naming, typed-field encoding and predicate compilation shared by the mongodb facets.

Layout in one database under ``collection_prefix`` (``<prefix>_<dataset>_<generation>_<kind>`` per
generation, the invariant from ``backends/contracts.py``):

* ``<prefix>_generations``: one document per known generation (``_id`` = ``GenerationId.prefix()``)
  carrying the dataset, the generation and the metadata schema used by the record store;
* ``<gen>_documents`` (``{_id, doc, fields}``), ``<gen>_chunks``, ``<gen>_lexical`` (text index on
  ``text`` and ``title``), ``<gen>_records`` (``{_id, schema_id, rec, fields}``);
* ``<prefix>_cache`` (TTL index), ``<prefix>_ledger``, ``<prefix>_jobs``, ``<prefix>_raw_replies``,
  ``<prefix>_sessions`` (optional TTL index), ``<prefix>_meta`` (generation pointer, manifests,
  failure records, write lock; owned by the generation registry, never read by the facets).

Typed scope is pushed down as query predicates over an array of typed field rows
``fields: [{name, k, t, f, n, null}]`` (kind, text, casefolded text, numeric form: numbers as
themselves, temporal values as epoch seconds of their UTC instant). Temporal values follow the
``backends/_scope.py`` storage rule shared with sqlite/postgres/Lucene: aware stamps convert to UTC,
a naive stamp (only reachable through an ``assume_utc`` field) and a date are read as UTC, ``t`` holds
the canonical UTC ISO text. Comparisons are on instants; for ``date`` fields bounds and equality
values are truncated to their UTC date (``_scope.temporal_key``). Field names are data, never document keys, so
metadata keys containing ``.`` or ``$`` are safe. A predicate compiles to ``$elemMatch`` on that
array; negation wraps it in ``$nor``. Pydantic models are stored as JSON strings (``doc``, ``rec``,
``data``) for the same reason.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from ...core.errors import BackendError
from ...core.types import Page, Predicate, Scope
from .._scope import instant, temporal_key

KIND = "mongodb"
FIELDS = "fields"
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_TERMS = 128


# --- naming -------------------------------------------------------------------------------------


def check_name(value: str, what: str) -> str:
    """Datasets and prefixes become collection names: refuse anything MongoDB could misread."""
    if not NAME_RE.match(value) or value.startswith("system."):
        raise BackendError(KIND, "naming", f"{what} {value!r} is not a safe collection-name component")
    return value


def generation_name(prefix: str, dataset: str, generation: str) -> str:
    return f"{prefix}_{check_name(dataset, 'dataset')}_{generation}"


# --- error mapping ------------------------------------------------------------------------------


@contextmanager
def mapped(operation: str) -> Iterator[None]:
    """Translate every pymongo failure (unreachable server, refused or failed operation) into a
    typed ``BackendError``; never retried, never downgraded."""
    from pymongo.errors import PyMongoError

    try:
        yield
    except BackendError:
        raise
    except PyMongoError as exc:
        raise BackendError(KIND, operation, f"{type(exc).__name__}: {exc}") from exc


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


def field_row(name: str, value: Any) -> dict[str, Any]:
    """One typed row for a scalar; the same classification as the sqlite adapter's EAV rows."""
    if value is None:
        return {"name": name, "k": "null", "t": None, "f": None, "n": None, "null": True}
    if isinstance(value, bool):
        return {"name": name, "k": "boolean", "t": "true" if value else "false", "f": None, "n": 1.0 if value else 0.0, "null": False}
    if isinstance(value, int):
        return {"name": name, "k": "integer", "t": None, "f": None, "n": float(value), "null": False}
    if isinstance(value, float):
        return {"name": name, "k": "number", "t": None, "f": None, "n": value, "null": False}
    text = str(value)
    stamp = instant(text)
    if stamp is not None:
        # A UTC instant (naive = UTC, a date = its midnight UTC) with a sortable canonical text, so
        # comparisons never fall back to text.
        return {"name": name, "k": "datetime", "t": stamp.isoformat(), "f": text.casefold(), "n": stamp.timestamp(), "null": False}
    return {"name": name, "k": "string", "t": text, "f": text.casefold(), "n": None, "null": False}


def field_rows(values: dict[str, Any]) -> list[dict[str, Any]]:
    return [field_row(name, value) for name, value in values.items()]


def row_as_string(row: dict[str, Any]) -> str:
    """The listing form of a typed row (``values``): integers without a fraction, others as text."""
    if row.get("k") == "integer":
        return str(int(row["n"]))
    if row.get("k") == "number":
        return str(row["n"])
    return str(row.get("t"))


def _scalar_condition(value: Any, casefold: bool, spec: Any = None) -> dict[str, Any] | None:
    """``None`` means the value can never match (a non-temporal value against a temporal field)."""
    if value is None:
        return {"null": True}
    if spec is not None and getattr(spec, "type", None) in {"datetime", "date"}:
        key = temporal_key(value, spec)
        if key is None:
            return None
        if spec.type == "date":  # calendar-date equality: the stored instant's UTC day
            return {"k": "datetime", "n": {"$gte": key.timestamp(), "$lt": key.timestamp() + 86400}}
        return {"k": "datetime", "n": key.timestamp()}
    if isinstance(value, bool):
        return {"k": "boolean", "n": 1.0 if value else 0.0}
    if isinstance(value, (int, float)):
        return {"k": {"$in": ["integer", "number"]}, "n": float(value)}
    text = str(value)
    return {"f": text.casefold()} if casefold else {"t": text}


def _bound_condition(bound: Any, operator: str, spec: Any = None) -> dict[str, Any] | None:
    """``None`` means the bound can never be satisfied (matches sqlite's ``0`` condition)."""
    if isinstance(bound, bool) or bound is None:
        return None
    if isinstance(bound, (int, float)):
        return {"k": {"$in": ["integer", "number"]}, "n": {operator: float(bound)}}
    text = str(bound)
    stamp = temporal_key(text, spec)
    if stamp is not None:
        # Instants on both sides. Date fields: the stored value is a midnight instant and the bound is
        # truncated to its UTC date, so ``<`` / ``>=`` on days behave as calendar comparisons.
        return {"k": "datetime", "n": {operator: stamp.timestamp()}}
    return {"k": "string", "t": {operator: text}}  # temporal vs non-temporal is incomparable


IMPOSSIBLE: dict[str, Any] = {"_id": {"$in": []}}


def predicate_filter(predicate: Predicate, casefold: bool, path: str = FIELDS, spec: Any = None) -> dict[str, Any]:
    """MongoDB filter for one typed predicate over the ``fields`` array at ``path``. ``spec`` is the
    field's ``FieldSpec`` when known (typed records); document scope has none."""
    name = predicate.field
    if predicate.op == "missing":
        positive: dict[str, Any] = {path: {"$not": {"$elemMatch": {"name": name, "null": False}}}}
    elif predicate.op == "eq":
        condition = _scalar_condition(predicate.value, casefold, spec)
        positive = dict(IMPOSSIBLE) if condition is None else {path: {"$elemMatch": {"name": name, **condition}}}
    elif predicate.op == "in":
        options = [c for c in (_scalar_condition(v, casefold, spec) for v in list(predicate.value or [])) if c is not None]
        if not options:
            positive = dict(IMPOSSIBLE)
        else:
            positive = {path: {"$elemMatch": {"name": name, "$or": options}}}
    else:  # range / date_interval
        conditions: list[dict[str, Any]] = [{"null": False}]
        impossible = False
        if predicate.lower is not None:
            cond = _bound_condition(predicate.lower, "$gte" if predicate.inclusive else "$gt", spec)
            impossible |= cond is None
            conditions.append(cond or {})
        if predicate.upper is not None:
            inclusive_upper = predicate.inclusive and predicate.op == "range"
            cond = _bound_condition(predicate.upper, "$lte" if inclusive_upper else "$lt", spec)
            impossible |= cond is None
            conditions.append(cond or {})
        positive = dict(IMPOSSIBLE) if impossible else {path: {"$elemMatch": {"name": name, "$and": conditions}}}
    return {"$nor": [positive]} if predicate.negate else positive


def scope_filter(scope: Scope | None, casefold: Any = None, document_key: str = "_id", path: str = FIELDS) -> dict[str, Any] | None:
    """Eligibility filter for a pushed-down ``Scope``; ``{}`` when unscoped, ``None`` when nothing
    can match (empty allow-list) so callers can short-circuit without a round trip."""
    if scope is None:
        return {}
    clauses: list[dict[str, Any]] = []
    if scope.document_ids is not None:
        if not scope.document_ids:
            return None
        clauses.append({document_key: {"$in": list(scope.document_ids)}})
    if scope.exclude_ids:
        clauses.append({document_key: {"$nin": list(scope.exclude_ids)}})
    for predicate in scope.predicates:
        clauses.append(predicate_filter(predicate, bool(casefold and casefold(predicate.field)), path))
    if not clauses:
        return {}
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def combine(*filters: dict[str, Any]) -> dict[str, Any]:
    parts = [f for f in filters if f]
    if not parts:
        return {}
    return parts[0] if len(parts) == 1 else {"$and": parts}


# --- text search --------------------------------------------------------------------------------


def tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def text_search(query: Any) -> str | None:
    """``$text.$search`` string for a ``LexicalQuery``. Every user term is reduced to ``\\w+`` tokens,
    so no quote or ``-`` of the user's can act as operator syntax; phrases are quoted (MongoDB
    phrase matching is a case-insensitive contiguous match on the raw text, so a phrase never
    matches non-contiguous tokens), ``must`` entries become required phrases and ``must_not``
    entries negated terms. MongoDB text scores are term-frequency based and are **not** BM25; the
    conformance kit asserts membership, scope and ordering invariants only."""
    terms = list(dict.fromkeys(tokens(query.text)))[:MAX_TERMS]
    if query.kind == "phrase":
        return '"' + " ".join(terms) + '"' if terms else None
    if not terms:
        return None
    parts = list(terms)
    if query.kind == "boolean":
        for must in query.must:
            words = tokens(must)
            if words:
                parts.append('"' + " ".join(words) + '"')
        for must_not in query.must_not:
            for word in tokens(must_not):
                parts.append("-" + word)
    return " ".join(parts)


# --- time ---------------------------------------------------------------------------------------


def as_date(seconds: float) -> datetime:
    """Epoch seconds as a timezone-aware datetime for TTL indexes."""
    return datetime.fromtimestamp(seconds, tz=timezone.utc)
