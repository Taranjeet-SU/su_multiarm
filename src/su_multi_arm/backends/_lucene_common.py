"""Shared pieces for the Lucene-engine adapters (OpenSearch, Elasticsearch): index naming, the typed
field layout that lets eligibility scope be pushed down as a native ``bool.filter``, predicate and
lexical query DSL compilation, pagination windows, the transport protocol and bulk-reply checks.

Index layout per generation (``<prefix>_<dataset>_<generation>_<kind>``):

* ``docs``: one document per catalog ``Document`` (``id``, ``data`` = canonical JSON, typed scope fields);
* ``chunks``: one document per ``Chunk`` (``chunk_id``, ``document_id``, ``field``, ``text``, ``title``,
  offsets, ``lexical`` flag set by ``LexicalIndex.index``, ``vec_<namespace>`` k-NN fields set by
  ``VectorIndex.upsert``, and the parent document's typed scope fields denormalised so chunk searches
  filter natively without a join);
* ``records``: one document per ``StructuredRecord`` (``id``, ``schema_id``, ``data``, typed fields);
  the ``MetadataSchema`` is stored in the index mapping ``_meta``.

Typed scope fields (same shape as the sqlite adapter's typed rows, one sub-object per kind so a
boolean never matches a number and a timestamp range never compares text):

* ``s.<field>``: keyword (exact text) with a ``.fold`` sub-field under a ``lowercase`` normalizer;
* ``n.<field>``: double (integers and numbers);
* ``b.<field>``: boolean;
* ``t.<field>``: ``epoch_millis`` of the value's comparison key (``_scope.temporal_key``: aware stamps
  convert to UTC, a naive stamp or a date-only string is UTC, which only ``assume_utc`` fields admit;
  ``date`` fields keep the calendar date, so a datetime bound against them truncates to its UTC date);
* ``v.<field>``: keyword with the canonical string of every non-null value (value listings);
* ``p``: keyword array of the fields that carry a non-null value (``missing`` = not in ``p``).

Dataset-level aliases ``<prefix>_<dataset>_<kind>`` point at the active generation's indices;
activation is one atomic ``_aliases`` call. Adapters never read ``CURRENT``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

from ..core.errors import BackendError
from ..core.types import LexicalQuery, Page, Predicate, Scope
from ._scope import temporal_key

MAPPING_VERSION = 1
INDEX_KINDS: tuple[str, ...] = ("docs", "chunks", "records")
DATASET = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
PREFIX = re.compile(r"[a-z][a-z0-9_]{0,63}")
MAX_QUERY_WORDS = 128
MAX_MATCHED_DEFAULT = 10_000
MAX_VALUES = 10_000
SCAN_PAGE = 1000


# --- naming -----------------------------------------------------------------------------------


def check_prefix(kind: str, prefix: str) -> str:
    if not PREFIX.fullmatch(prefix):
        raise BackendError(kind, "init", f"index_prefix {prefix!r} must match {PREFIX.pattern}")
    return prefix


def check_dataset(kind: str, operation: str, dataset: str) -> str:
    """Index names are lowercase and restricted; a dataset that does not fit is refused, never mangled."""
    if not DATASET.fullmatch(dataset):
        raise BackendError(kind, operation, f"dataset {dataset!r} cannot name an index (must match {DATASET.pattern})")
    return dataset


def index_name(prefix: str, dataset: str, generation: str, index_kind: str) -> str:
    return f"{prefix}_{dataset}_{generation}_{index_kind}"


def alias_name(prefix: str, dataset: str, index_kind: str) -> str:
    return f"{prefix}_{dataset}_{index_kind}"


def generation_pattern(prefix: str, dataset: str, generation: str) -> str:
    return f"{prefix}_{dataset}_{generation}_*"


# --- typed field layout -----------------------------------------------------------------------


def canonical_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


FieldSpecLookup = Callable[[str], Any]


def typed_fields(values: dict[str, Any], field_spec: FieldSpecLookup | None = None) -> dict[str, Any]:
    """``{"s": {...}, "n": {...}, "b": {...}, "t": {...}, "v": {...}, "p": [...]}`` for a flat value mapping.
    ``field_spec`` (the metadata schema's ``field``) selects the temporal key of ``date`` fields."""
    s: dict[str, str] = {}
    n: dict[str, float] = {}
    b: dict[str, bool] = {}
    t: dict[str, int] = {}
    v: dict[str, str] = {}
    present: list[str] = []
    for field, value in values.items():
        if value is None:
            continue
        present.append(field)
        v[field] = canonical_text(value)
        if isinstance(value, bool):
            b[field] = value
        elif isinstance(value, (int, float)):
            n[field] = float(value)
        else:
            text = str(value)
            s[field] = text
            stamp = temporal_key(text, field_spec(field) if field_spec else None)
            if stamp is not None:
                t[field] = _millis(stamp)
    return {"s": s, "n": n, "b": b, "t": t, "v": v, "p": present}


NORMALIZER_SETTINGS = {"analysis": {"normalizer": {"fold": {"type": "custom", "filter": ["lowercase"]}}}}

DYNAMIC_TEMPLATES = [
    {"typed_text": {"path_match": "s.*", "mapping": {"type": "keyword", "fields": {"fold": {"type": "keyword", "normalizer": "fold"}}}}},
    {"typed_number": {"path_match": "n.*", "mapping": {"type": "double"}}},
    {"typed_boolean": {"path_match": "b.*", "mapping": {"type": "boolean"}}},
    {"typed_time": {"path_match": "t.*", "mapping": {"type": "date", "format": "epoch_millis"}}},
    {"typed_value": {"path_match": "v.*", "mapping": {"type": "keyword"}}},
]

TYPED_PROPERTIES = {
    "s": {"type": "object", "dynamic": True},
    "n": {"type": "object", "dynamic": True},
    "b": {"type": "object", "dynamic": True},
    "t": {"type": "object", "dynamic": True},
    "v": {"type": "object", "dynamic": True},
    "p": {"type": "keyword"},
}


def _millis(moment: Any) -> int:
    return int(round(moment.timestamp() * 1000))


def meta(index_kind: str, dataset: str, generation: str, **extra: Any) -> dict[str, Any]:
    return {"su_multi_arm": {"mapping_version": MAPPING_VERSION, "kind": index_kind, "dataset": dataset, "generation": generation, **extra}}


# --- predicate and scope compilation ----------------------------------------------------------

MATCH_NONE = {"match_none": {}}


def _scalar_query(field: str, value: Any, casefold: bool, spec: Any = None) -> dict[str, Any]:
    if value is None:
        return {"bool": {"must_not": [{"term": {"p": field}}]}}
    if isinstance(value, bool):
        return {"term": {f"b.{field}": value}}
    if isinstance(value, (int, float)):
        return {"term": {f"n.{field}": float(value)}}
    text = str(value)
    stamp = temporal_key(text, spec)
    if stamp is not None:  # temporal equality is instant equality (across offsets), never text equality
        return {"term": {f"t.{field}": _millis(stamp)}}
    return {"term": {f"s.{field}.fold" if casefold else f"s.{field}": text}}


def _bound(field: str, bound: Any, operator: str, spec: Any = None) -> dict[str, Any]:
    if isinstance(bound, bool) or bound is None:
        return MATCH_NONE
    if isinstance(bound, (int, float)):
        return {"range": {f"n.{field}": {operator: float(bound)}}}
    text = str(bound)
    stamp = temporal_key(text, spec)
    if stamp is not None:
        return {"range": {f"t.{field}": {operator: _millis(stamp)}}}
    return {"range": {f"s.{field}": {operator: text}}}


def predicate_query(predicate: Predicate, casefold: bool = False, spec: Any = None) -> dict[str, Any]:
    """Query DSL for one typed predicate against the typed field layout. Values are JSON, never text.
    ``spec`` (the field's ``FieldSpec``, when the caller has a schema) selects date-field semantics."""
    field = predicate.field
    if predicate.op == "missing":
        query: dict[str, Any] = {"bool": {"must_not": [{"term": {"p": field}}]}}
    elif predicate.op == "eq":
        query = _scalar_query(field, predicate.value, casefold, spec)
    elif predicate.op == "in":
        candidates = list(predicate.value or [])
        query = {"bool": {"should": [_scalar_query(field, c, casefold, spec) for c in candidates], "minimum_should_match": 1}} if candidates else MATCH_NONE
    else:  # range / date_interval
        clauses: list[dict[str, Any]] = [{"term": {"p": field}}]
        if predicate.lower is not None:
            clauses.append(_bound(field, predicate.lower, "gte" if predicate.inclusive else "gt", spec))
        if predicate.upper is not None:
            inclusive_upper = predicate.inclusive and predicate.op == "range"  # date_interval is half-open
            clauses.append(_bound(field, predicate.upper, "lte" if inclusive_upper else "lt", spec))
        query = {"bool": {"filter": clauses}}
    return {"bool": {"must_not": [query]}} if predicate.negate else query


def scope_filter(scope: Scope | None, document_field: str, casefold: Callable[[str], bool] | None = None) -> list[dict[str, Any]]:
    """Filter clauses for a pushed-down ``Scope``; ``document_field`` names the document id field of the index."""
    if scope is None:
        return []
    clauses: list[dict[str, Any]] = []
    if scope.document_ids is not None:
        if not scope.document_ids:
            return [MATCH_NONE]
        clauses.append({"terms": {document_field: list(scope.document_ids)}})
    if scope.exclude_ids:
        clauses.append({"bool": {"must_not": [{"terms": {document_field: list(scope.exclude_ids)}}]}})
    for predicate in scope.predicates:
        clauses.append(predicate_query(predicate, bool(casefold and casefold(predicate.field))))
    return clauses


def predicates_filter(
    predicates: Sequence[Predicate], casefold: Callable[[str], bool], field_spec: FieldSpecLookup | None = None
) -> list[dict[str, Any]]:
    return [predicate_query(p, casefold(p.field), field_spec(p.field) if field_spec else None) for p in predicates]


# --- lexical query compilation ----------------------------------------------------------------


def bounded_text(text: str) -> str:
    """The engine analyses the text; the adapter only bounds the number of whitespace-separated words."""
    return " ".join(text.split()[:MAX_QUERY_WORDS])


def lexical_query(query: LexicalQuery, fields: Sequence[str]) -> dict[str, Any] | None:
    """``multi_match`` templates; user text is never parsed as query syntax (no ``query_string``)."""
    text = bounded_text(query.text)
    if not text:
        return None
    if query.kind == "phrase":
        return {"multi_match": {"query": text, "fields": list(fields), "type": "phrase"}}
    terms = {"multi_match": {"query": text, "fields": list(fields), "type": "most_fields", "operator": "or"}}
    if query.kind != "boolean":
        return terms
    must = [{"multi_match": {"query": bounded_text(m), "fields": list(fields), "type": "phrase"}} for m in query.must if m.split()]
    must_not = [{"multi_match": {"query": bounded_text(m), "fields": list(fields), "type": "phrase"}} for m in query.must_not if m.split()]
    return {"bool": {"must": [terms, *must], "must_not": must_not}}


# --- paging -----------------------------------------------------------------------------------


def page_window(kind: str, page: Page | None, default_size: int) -> tuple[int, int]:
    """``(offset, size)`` of an offset cursor page; invalid pages are typed errors."""
    if default_size < 1:
        raise BackendError(kind, "page", "k must be positive")
    try:
        page = page or Page(size=default_size)
    except ValueError as exc:
        raise BackendError(kind, "page", f"invalid page: {exc}") from None
    try:
        offset = int(page.cursor) if page.cursor else 0
    except ValueError:
        raise BackendError(kind, "page", "invalid cursor") from None
    if offset < 0:
        raise BackendError(kind, "page", "invalid cursor")
    return offset, page.size


# --- transport --------------------------------------------------------------------------------


class TransportError(Exception):
    """One failed HTTP round trip: ``status`` is ``None`` when no reply arrived (connection, timeout)."""

    def __init__(self, status: int | None, error_type: str, reason: str, body: Any = None):
        self.status, self.error_type, self.reason, self.body = status, error_type, reason, body
        super().__init__(f"{status if status is not None else 'no reply'} {error_type}: {reason}")


@runtime_checkable
class Transport(Protocol):
    """Minimal JSON-over-HTTP transport; ``body`` is a JSON value or NDJSON text (bulk)."""

    def request(self, method: str, path: str, params: dict[str, Any] | None = None, body: Any = None) -> Any: ...


def error_reason(body: Any) -> tuple[str, str]:
    """``(type, reason)`` from an engine error reply, tolerating non-JSON bodies."""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            cause = error.get("root_cause") or []
            first = cause[0] if cause and isinstance(cause[0], dict) else error
            return str(first.get("type", "error")), str(first.get("reason", error.get("reason", "")))
        if isinstance(error, str):
            return "error", error
    return "error", "" if body is None else str(body)[:500]


def bulk_body(actions: Sequence[tuple[dict[str, Any], dict[str, Any] | None]]) -> str:
    lines = []
    for action, source in actions:
        lines.append(json.dumps(action, ensure_ascii=False, separators=(",", ":")))
        if source is not None:
            lines.append(json.dumps(source, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + "\n"


def check_bulk(kind: str, operation: str, reply: Any, describe: Callable[[str], str] | None = None) -> None:
    """A bulk reply with any failed item is a typed error (no partial success is ever silent)."""
    if not isinstance(reply, dict) or "items" not in reply:
        raise BackendError(kind, operation, f"unexpected bulk reply: {str(reply)[:200]}")
    if not reply.get("errors"):
        return
    for item in reply["items"]:
        (_action, result), = item.items()
        error = result.get("error")
        if error:
            doc_id = str(result.get("_id", "?"))
            error_type = error.get("type", "error") if isinstance(error, dict) else str(error)
            reason = error.get("reason", "") if isinstance(error, dict) else ""
            prefix = describe(doc_id) if describe else f"item {doc_id!r}"
            if error_type == "document_missing_exception":
                raise BackendError(kind, operation, f"{prefix}: not a catalogued chunk")
            raise BackendError(kind, operation, f"{prefix}: {error_type}: {reason}")
    raise BackendError(kind, operation, "bulk reply flagged errors without a failing item")
