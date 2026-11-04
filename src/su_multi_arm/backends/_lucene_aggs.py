"""Native aggregation pushdown for the Lucene-engine records facet (``records.aggregation: native``).

The client path (default) scans every matched record and computes the ``ScopeSpec`` operations in
Python (``_scope.scope_result``), bounded by ``max_matched``. This module compiles the same
operations to OpenSearch / Elasticsearch aggregations so a scope over millions of records is
answered without fetching them, with the **same** ``ScopeResult`` whenever the client path can answer:

* ``total`` / ``count``: ``track_total_hits: true`` (exact); the denominator is a ``_count``;
* groups and cross-tabs: ``composite`` paging (exact enumeration) over the canonical value field
  ``v.<field>`` (decoded back to the stored JSON type), calendar groups as a ``date_histogram``
  source with the schema's ``time_zone``, nulls through ``missing_bucket``; group ranking
  (``top_n``/``nth``/``order``, boundary ties) and the group-field sort reuse ``_scope.order_groups``.
  ``top_n``/``nth`` of a count over one field use a ``terms`` aggregation ordered by count, grown
  until every group that can reach the boundary rank is known and the engine reports
  ``doc_count_error_upper_bound: 0`` (the records index has one shard, verified from its settings,
  but OpenSearch 3.x may still search it in concurrent segment slices whose terms counts carry an
  error until the request covers enough groups);
* measures: ``stats`` (sum/avg/min/max over ``n.*``), ``value_count`` + ``top_hits`` (first/last
  seen), ``date_histogram`` (bucket), ``filter`` sub-aggregations (share numerator, null counts of
  negated predicates); periods: a ``filters`` aggregation;
* ``count_distinct``: exact through ``composite`` paging of (group, unit) pairs up to
  ``distinct_exact_cap`` pairs; above it the engine's ``cardinality`` (HyperLogLog++) is used and
  the result is labelled ``approximate`` with the reason in ``basis``;
* the record list (``limit``): a sorted search of at most ``limit`` hits;
* ``matched_ids``: the first ``matched_ids_cap`` ids in id order, ``matched_ids_truncated`` when
  more records matched.

Never a silent cut: more than ``max_groups`` groups gives a ``bounded`` result with the reason as an
``UnsupportedConstraint`` and no groups; a group key that the index keeps apart but Python equates
(``3`` and ``3.0``, ``1`` and ``true`` in one undeclared field) is a typed ``BackendError``.

Resolution: temporal values are compared at the index's millisecond resolution (``t.*`` fields), the
same as predicate pushdown on both paths; first/last-seen ties within one millisecond are broken by
record id. Sums are the engine's double (compensated) sums; integral doubles are reported as ``int``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Sequence

from ..core.errors import BackendError
from ..core.types import GroupRow, ListedRecord, MetadataSchema, Predicate, ScopeResult, ScopeSpec, StructuredRecord
from ._lucene_common import MATCH_NONE, predicate_query
from ._scope import _basis, _flag, _numerator_share, calendar_label, order_groups, temporal_key, unsupported_reason, zone_of

if TYPE_CHECKING:  # pragma: no cover
    from .opensearch.adapter import OpenSearchBackend

MATCHED_IDS_CAP_DEFAULT = 10_000
MAX_GROUPS_DEFAULT = 10_000
DISTINCT_EXACT_CAP_DEFAULT = 100_000
COMPOSITE_PAGE = 1000
PAIR_PAGE = 10_000
ID_PAGE = 10_000
CARDINALITY_PRECISION = 40_000  # the engines' maximum precision_threshold
SAFE_INTEGER = 2**53
INTERVALS = {"day": "1d", "month": "1M", "year": "1y"}
KINDS = ("s", "n", "b")


class TooManyGroups(Exception):
    """More groups than ``max_groups``: the caller reports a ``bounded`` result."""


def number(value: Any) -> Any:
    """An engine double as the JSON number the client path reports (integral → ``int``)."""
    if isinstance(value, float) and value.is_integer() and abs(value) < SAFE_INTEGER:
        return int(value)
    return value


def _parse_number(text: str) -> int | float:
    try:
        return int(text)
    except ValueError:
        return float(text)


def _boolean(value: Any) -> bool:
    """A boolean terms key: ``true``/``false``, ``1``/``0`` or ``"true"``/``"false"`` by engine and source."""
    return value == "true" if isinstance(value, str) else bool(value)


def _exists(field: str) -> dict[str, Any]:
    return {"exists": {"field": field}}


def _missing(field: str) -> dict[str, Any]:
    return {"bool": {"must_not": [{"term": {"p": field}}]}}


def _and(query: dict[str, Any], *extra: dict[str, Any]) -> dict[str, Any]:
    return {"bool": {"filter": [query, *extra]}}


class Source:
    """One composite source of a group key (or of the distinct unit) and its decoder."""

    def __init__(self, field: str, parts: list[tuple[str, dict[str, Any]]], decode: Callable[[dict[str, Any]], Any]):
        self.field, self.parts, self.decode = field, parts, decode


def _value_decoder(kinds: set[str], names: dict[str, str]) -> Callable[[dict[str, Any]], Any]:
    """Decode the canonical text ``v.<field>`` back to the stored JSON value (``_lucene_common.canonical_text``)."""

    def decode(key: dict[str, Any]) -> Any:
        text = key.get(names["v"])
        if text is None:
            return None
        if "b" in names and key.get(names["b"]) is not None:
            return _boolean(key[names["b"]])
        if "s" in names and key.get(names["s"]) is not None:
            return str(key[names["s"]])
        only = next(iter(kinds)) if len(kinds) == 1 else "n"
        if only == "s":
            return str(text)
        if only == "b":
            return text == "true"
        return _parse_number(str(text))

    return decode


class NativeAggregation:
    """One ``execute`` of a scope on one records index (built per call)."""

    def __init__(
        self,
        backend: OpenSearchBackend,
        index: str,
        schema: MetadataSchema,
        casefold: Callable[[str], bool],
    ):
        self._b = backend
        self.index = index
        self.schema = schema
        self.casefold = casefold
        self.zone = zone_of(schema)
        self.zone_name = schema.timezone or "UTC"
        self.approximate: list[str] = []

    # --- transport ---------------------------------------------------------------------------

    def _search(self, body: dict[str, Any]) -> dict[str, Any]:
        self._b.stats["records.native.search"] += 1
        return self._b.call("execute", "POST", f"/{self.index}/_search", body=body)

    def _single_shard(self) -> bool:
        cache = self._b.shard_counts
        if self.index not in cache:
            reply = self._b.call("execute", "GET", f"/{self.index}/_settings", params={"filter_path": "*.settings.index.number_of_shards"})
            shards = None
            if isinstance(reply, dict):
                for value in reply.values():
                    shards = value.get("settings", {}).get("index", {}).get("number_of_shards")
            cache[self.index] = str(shards) == "1"
        return cache[self.index]

    # --- field layout --------------------------------------------------------------------------

    def _spec(self, field: str) -> Any:
        return self.schema.field(field)

    def _temporal(self, field: str) -> bool:
        spec = self._spec(field)
        return spec is not None and spec.type in {"datetime", "date"}

    def _filter(self, predicates: Sequence[Predicate]) -> list[dict[str, Any]]:
        return [predicate_query(p, self.casefold(p.field), self._spec(p.field)) for p in predicates]

    def _numerator_filter(self, predicate: Predicate) -> dict[str, Any]:
        """The client path evaluates a numerator against ``{**values, "id": record.id}``: an undeclared
        ``id`` predicate therefore compares the record id (the index's top-level ``id`` keyword)."""
        if predicate.field != "id" or self._spec("id") is not None:
            return self._filter([predicate])[0]
        values = [predicate.value] if predicate.op == "eq" else list(predicate.value or [])
        query = {"terms": {"id": [str(v) for v in values if isinstance(v, str)]}} if any(isinstance(v, str) for v in values) else MATCH_NONE
        return {"bool": {"must_not": [query]}} if predicate.negate else query

    def _group_source(self, index: int, field: str, scope: ScopeSpec, kinds: set[str]) -> Source | None:
        """``None`` when the field carries no value in the matched records (every key is null)."""
        name = f"g{index}"
        if scope.granularity is not None and self._temporal(field):
            if "t" not in kinds:
                return None
            granularity = scope.granularity
            source = {"date_histogram": {"field": f"t.{field}", "calendar_interval": INTERVALS[granularity], "time_zone": self.zone_name, "missing_bucket": True}}

            def decode(key: dict[str, Any]) -> Any:
                millis = key.get(name)
                if millis is None:
                    return None
                return calendar_label(datetime.fromtimestamp(millis / 1000, timezone.utc), granularity, self.zone)

            return Source(field, [(name, source)], decode)
        return self._value_source(name, field, kinds)

    def _value_source(self, name: str, field: str, kinds: set[str]) -> Source | None:
        present = kinds & set(KINDS)
        if not present:
            return None
        names = {"v": name}
        parts: list[tuple[str, dict[str, Any]]] = [(name, {"terms": {"field": f"v.{field}", "missing_bucket": True}})]
        if len(present) > 1:  # a field holding several JSON types: the typed fields keep "1", 1 and true apart
            for kind in ("s", "b"):
                if kind in present:
                    names[kind] = f"{name}{kind}"
                    parts.append((names[kind], {"terms": {"field": f"{kind}.{field}", "missing_bucket": True}}))
        return Source(field, parts, _value_decoder(present, names))

    # --- measures --------------------------------------------------------------------------------

    def _measure_aggs(self, scope: ScopeSpec) -> dict[str, Any]:
        measure = scope.measure
        if measure is None or measure.kind in {"count", "share", "count_distinct"}:
            return {}
        field = measure.field or ""
        if measure.kind in {"sum", "avg", "min", "max"}:
            return {"m_stats": {"stats": {"field": f"n.{field}"}}}
        valid = {"m_valid": {"value_count": {"field": f"t.{field}"}}}
        if measure.kind == "bucket":
            interval = INTERVALS[measure.granularity or "day"]
            return {**valid, "m_hist": {"date_histogram": {"field": f"t.{field}", "calendar_interval": interval, "time_zone": self.zone_name, "min_doc_count": 1}}}
        order = "asc" if measure.kind == "first_seen" else "desc"
        top = {"top_hits": {"size": 1, "_source": ["data"], "sort": [{f"t.{field}": {"order": order, "unmapped_type": "date"}}, {"id": {"order": order}}]}}
        return {**valid, "m_top": top}

    def _measure_value(self, scope: ScopeSpec, bucket: dict[str, Any], count: int, population: int) -> tuple[Any, int]:
        """``(value, missing_or_invalid)`` of one bucket, as ``_scope.measure_of`` computes it."""
        measure = scope.measure
        if measure is None or measure.kind == "count":
            return count, 0
        if measure.kind == "share":
            return (count / population) if population else 0.0, 0
        field = measure.field or ""
        if measure.kind in {"sum", "avg", "min", "max"}:
            stats = bucket["m_stats"]
            valid = int(stats.get("count") or 0)
            if not valid:
                return None, count
            if measure.kind == "avg":
                return number(stats["sum"]) / valid, count - valid
            return number(stats[measure.kind]), count - valid
        valid = int(bucket["m_valid"]["value"] or 0)
        if not valid:
            return (None if measure.kind != "bucket" else {}), count
        if measure.kind == "bucket":
            counts = {}
            for b in bucket["m_hist"]["buckets"]:
                if b["doc_count"]:
                    counts[datetime.fromtimestamp(b["key"] / 1000, self.zone).isoformat()] = int(b["doc_count"])
            return dict(sorted(counts.items())), count - valid
        hit = bucket["m_top"]["hits"]["hits"][0]
        record = StructuredRecord.model_validate(hit["_source"]["data"])
        moment = temporal_key(record.values.get(field), self._spec(field))
        if moment is None:  # cannot happen when value_count saw a value; never invent one
            raise BackendError(self._b.kind, "execute", f"{measure.kind}: record {record.id} has no {field} instant")
        return {"value": moment.isoformat(), "record_id": record.id}, count - valid

    # --- count_distinct ---------------------------------------------------------------------------

    def _distinct_mode(self, scope: ScopeSpec) -> str | None:
        """``None`` (not a distinct count), ``identity`` (distinct records), ``equivalence`` or ``values``."""
        measure = scope.measure
        if measure is None or measure.kind != "count_distinct":
            return None
        unit = measure.unit or self.schema.equivalence_field or "id"
        if unit in {"id", self.schema.canonical_unit} and self.schema.field(unit) is None:
            return "identity"
        return "equivalence" if unit == self.schema.equivalence_field else "values"

    def _unit(self, scope: ScopeSpec) -> str:
        measure = scope.measure
        assert measure is not None
        return measure.unit or self.schema.equivalence_field or "id"

    def _distinct(
        self, query: dict[str, Any], sources: list[Source], unit: str, unit_kinds: set[str]
    ) -> dict[tuple, tuple[int, int]] | None:
        """``{group key: (distinct non-null units, records without a unit)}`` by composite paging of
        (group, unit) pairs; ``None`` above ``distinct_exact_cap`` pairs (the caller goes approximate)."""
        unit_source = self._value_source("u", unit, unit_kinds)
        parts = [p for s in sources for p in s.parts] + (unit_source.parts if unit_source else [])
        seen: dict[tuple, set] = {}
        missing: dict[tuple, int] = {}
        if not parts:  # neither groups nor unit values: every matched record lacks the unit
            total = self._total(query)
            return {(): (0, total)}
        pairs = 0
        after: dict[str, Any] | None = None
        while True:
            composite: dict[str, Any] = {"size": PAIR_PAGE, "sources": [{n: s} for n, s in parts]}
            if after is not None:
                composite["after"] = after
            reply = self._search({"size": 0, "query": query, "track_total_hits": False, "aggs": {"pairs": {"composite": composite}}})
            agg = reply["aggregations"]["pairs"]
            for bucket in agg["buckets"]:
                key = tuple(s.decode(bucket["key"]) for s in sources)
                value = unit_source.decode(bucket["key"]) if unit_source else None
                if value is None:
                    missing[key] = missing.get(key, 0) + int(bucket["doc_count"])
                    seen.setdefault(key, set())
                else:
                    seen.setdefault(key, set()).add(value)
                    pairs += 1
            if pairs > self._b.distinct_exact_cap:
                return None
            after = agg.get("after_key")
            if not agg["buckets"] or after is None:
                break
        return {key: (len(values), missing.get(key, 0)) for key, values in seen.items()}

    def _distinct_value(self, mode: str, distinct: int, missing: int) -> tuple[int, int]:
        if mode == "equivalence":  # a record without an equivalence value is its own canonical unit
            return distinct + missing, 0
        return distinct, missing

    def _cardinality_aggs(self, unit: str) -> dict[str, Any]:
        return {
            "d_card": {"cardinality": {"field": f"v.{unit}", "precision_threshold": CARDINALITY_PRECISION}},
            "d_missing": {"filter": _missing(unit)},
        }

    def _cardinality_value(self, mode: str, bucket: dict[str, Any]) -> tuple[int, int]:
        return self._distinct_value(mode, int(bucket["d_card"]["value"]), int(bucket["d_missing"]["doc_count"]))

    def _note_approximate(self, unit: str) -> None:
        label = (
            f"count_distinct {unit}: approximate (engine cardinality, HyperLogLog++); more than "
            f"{self._b.distinct_exact_cap} distinct values exceed the exact cap (records.distinct_exact_cap)"
        )
        if label not in self.approximate:
            self.approximate.append(label)

    # --- helpers ---------------------------------------------------------------------------------

    def _total(self, query: dict[str, Any]) -> int:
        reply = self._search({"size": 0, "query": query, "track_total_hits": True})
        return _hits_total(self._b.kind, reply)

    def _kinds_aggs(self, fields: Sequence[str]) -> dict[str, Any]:
        aggs: dict[str, Any] = {}
        for i, field in enumerate(fields):
            for kind in (*KINDS, "t"):
                aggs[f"k{i}_{kind}"] = {"filter": _exists(f"{kind}.{field}")}
        return aggs

    @staticmethod
    def _kinds(reply: dict[str, Any], fields: Sequence[str]) -> dict[str, set[str]]:
        aggs = reply.get("aggregations", {})
        return {field: {k for k in (*KINDS, "t") if aggs[f"k{i}_{k}"]["doc_count"]} for i, field in enumerate(fields)}

    # --- execute ---------------------------------------------------------------------------------

    def execute(self, scope: ScopeSpec, acl: Sequence[Predicate], applied: list[Predicate], unsupported: list) -> ScopeResult:
        unsupported = list(unsupported)
        if scope.text is not None:
            unsupported.append(
                _flag(
                    f"text {scope.text.kind} {scope.text.text!r}",
                    "not_supported_by_backend",
                    "a text condition is resolved through the lexical index by the structured arm; a record store cannot evaluate it",
                )
            )
        population = int(self._b.call("execute", "POST", f"/{self.index}/_count", body={"query": {"bool": {"filter": self._filter(acl)}}})["count"])
        query: dict[str, Any] = {"bool": {"filter": self._filter([*acl, *applied])}}
        mode = self._distinct_mode(scope)
        probe_fields: list[str] = list(dict.fromkeys([*scope.group_by, *([self._unit(scope)] if mode in {"equivalence", "values"} else []), *([scope.sort.field] if scope.sort and scope.limit is not None else [])]))
        unknown_fields = [
            p.field
            for p in applied
            if p.negate and p.op in {"eq", "in"} and (spec := self._spec(p.field)) is not None and spec.nullable
        ]
        aggs: dict[str, Any] = {**self._kinds_aggs(probe_fields)}
        for i, field in enumerate(unknown_fields):
            aggs[f"unknown{i}"] = {"filter": _missing(field)}
        grouped = bool(scope.periods) or bool(scope.group_by)
        share_numerator = _numerator_share(scope)
        numerator_ok = True
        if share_numerator and scope.numerator and not grouped:
            for predicate in scope.numerator:
                reason = unsupported_reason(self.schema, predicate)
                if reason is not None:
                    unsupported.append(_flag(f"numerator {predicate.field}", reason, "the share numerator cannot be applied; no share is computed"))
                    numerator_ok = False
                    break
            if numerator_ok:
                aggs["numerator"] = {"filter": {"bool": {"filter": [self._numerator_filter(p) for p in scope.numerator]}}}
        elif not grouped and not share_numerator:
            aggs.update(self._measure_aggs(scope))
        first = self._search({"size": 0, "query": query, "track_total_hits": True, **({"aggs": aggs} if aggs else {})})
        total = _hits_total(self._b.kind, first)
        kinds = self._kinds(first, probe_fields)
        unknown: dict[str, int] = {}
        for i, field in enumerate(unknown_fields):
            count = int(first["aggregations"][f"unknown{i}"]["doc_count"])
            if count:
                unknown[field] = unknown.get(field, 0) + count

        groups: list[GroupRow] = []
        ties: list[GroupRow] = []
        missing = 0
        numerator: int | None = None
        value: Any
        if scope.periods:
            value, groups, missing = self._periods(scope, query, population, unsupported, kinds)
        elif scope.group_by:
            try:
                groups, missing = self._groups(scope, query, total, population, kinds)
                groups, ties, value = order_groups(scope, groups)
            except TooManyGroups:
                unsupported.append(
                    _flag(
                        f"group_by {', '.join(scope.group_by)}",
                        "not_supported_by_backend",
                        f"more than {self._b.max_groups} groups match; narrow the scope or raise records.max_groups (native aggregation never truncates groups)",
                    )
                )
                groups, ties, value, missing = [], [], None, 0
        elif share_numerator:
            if scope.numerator and numerator_ok:
                numerator = int(first["aggregations"]["numerator"]["doc_count"])
            value = (numerator / total) if numerator is not None and total else None
        elif mode is not None:
            value, missing = self._distinct_scalar(scope, mode, query, total, kinds)
        else:
            value, missing = self._measure_value(scope, first.get("aggregations", {}), total, population)

        ids, truncated = self._matched_ids(query, total)
        listed = self._listed(scope, query, kinds) if scope.limit is not None else []
        basis = _basis(scope, self.schema, applied, unknown) + self.approximate
        exactness = "bounded" if unsupported else ("approximate" if self.approximate else "exact")
        return ScopeResult(
            exactness=exactness,
            denominator=population,
            value=value,
            groups=groups,
            applied=applied,
            unsupported=unsupported,
            zero_match=bool(scope.predicates) and not total,
            matched_ids=ids,
            matched_ids_truncated=truncated,
            missing_or_invalid=missing,
            total=total,
            listed=listed,
            ties=ties,
            unknown=unknown,
            numerator=numerator,
            basis=basis,
        )

    def _distinct_scalar(self, scope: ScopeSpec, mode: str, query: dict[str, Any], total: int, kinds: dict[str, set[str]]) -> tuple[int, int]:
        if mode == "identity":
            return total, 0
        unit = self._unit(scope)
        counted = self._distinct(query, [], unit, kinds.get(unit, set()))
        if counted is None:
            self._note_approximate(unit)
            reply = self._search({"size": 0, "query": query, "track_total_hits": False, "aggs": self._cardinality_aggs(unit)})
            return self._cardinality_value(mode, reply["aggregations"])
        distinct, missing = counted.get((), (0, 0))
        return self._distinct_value(mode, distinct, missing)

    def _periods(self, scope: ScopeSpec, query: dict[str, Any], population: int, unsupported: list, kinds: dict[str, set[str]]) -> tuple[dict[str, Any], list[GroupRow], int]:
        field = scope.time_basis or ""
        spec = self._spec(field)
        windows: list[tuple[str, dict[str, Any]]] = []
        for period in scope.periods:
            window = Predicate(field=field, op="date_interval", lower=period.lower, upper=period.upper, granularity="day")
            reason = unsupported_reason(self.schema, window) if spec is not None and spec.type in {"datetime", "date"} else "field_not_filterable"
            if reason is not None:
                unsupported.append(_flag(f"period {period.label}", reason, "a comparison period needs valid bounds on a declared datetime/date time_basis field"))
                continue
            windows.append((period.label, predicate_query(window, False, spec)))
        by_period: dict[str, Any] = {}
        groups: list[GroupRow] = []
        missing = 0
        if not windows:
            return by_period, groups, missing
        mode = self._distinct_mode(scope)
        sub = self._measure_aggs(scope)
        filters = {"filters": {"filters": {f"p{i}": q for i, (_, q) in enumerate(windows)}}}
        if sub:
            filters["aggs"] = sub
        reply = self._search({"size": 0, "query": query, "track_total_hits": False, "aggs": {"periods": filters}})
        buckets = reply["aggregations"]["periods"]["buckets"]
        for i, (label, window) in enumerate(windows):
            bucket = buckets[f"p{i}"]
            count = int(bucket["doc_count"])
            if mode is not None:
                period_value, period_missing = self._distinct_scalar(scope, mode, _and(query, window), count, kinds)
            else:
                period_value, period_missing = self._measure_value(scope, bucket, count, population)
            missing += period_missing
            by_period[label] = period_value
            groups.append(GroupRow(key={"period": label}, value=period_value, count=count))
        return by_period, groups, missing

    def _groups(self, scope: ScopeSpec, query: dict[str, Any], total: int, population: int, kinds: dict[str, set[str]]) -> tuple[list[GroupRow], int]:
        sources = [self._group_source(i, f, scope, kinds.get(f, set())) for i, f in enumerate(scope.group_by)]
        live = [s for s in sources if s is not None]
        measure = scope.measure
        counting = measure is None or measure.kind == "count"
        if (
            counting
            and len(scope.group_by) == 1
            and live
            and len(live[0].parts) == 1
            and "terms" in live[0].parts[0][1]
            and (scope.top_n is not None or scope.nth is not None)
            and not (scope.sort and scope.sort.field in scope.group_by)
            and self._single_shard()
        ):
            return self._ranked_terms(scope, query, total, live[0]), 0
        mode = self._distinct_mode(scope)
        sub = self._measure_aggs(scope)
        rows: dict[tuple, tuple[int, dict[str, Any]]] = {}
        if not live:  # every group field is null in every matched record: one group of nulls
            reply = self._search({"size": 0, "query": query, "track_total_hits": False, **({"aggs": sub} if sub else {})})
            if total:
                rows[tuple(None for _ in scope.group_by)] = (total, reply.get("aggregations", {}))
        else:
            parts = [p for s in live for p in s.parts]
            after: dict[str, Any] | None = None
            while True:
                composite: dict[str, Any] = {"composite": {"size": COMPOSITE_PAGE, "sources": [{n: s} for n, s in parts]}}
                if after is not None:
                    composite["composite"]["after"] = after
                if sub:
                    composite["aggs"] = sub
                reply = self._search({"size": 0, "query": query, "track_total_hits": False, "aggs": {"groups": composite}})
                agg = reply["aggregations"]["groups"]
                for bucket in agg["buckets"]:
                    key = self._key(sources, bucket["key"])
                    if key in rows:
                        raise BackendError(self._b.kind, "execute", f"group key {key!r} is stored under two index values that Python treats as equal; use records.aggregation: client for this field")
                    rows[key] = (int(bucket["doc_count"]), bucket)
                    if len(rows) > self._b.max_groups:
                        raise TooManyGroups
                after = agg.get("after_key")
                if not agg["buckets"] or after is None:
                    break
        distinct: dict[tuple, tuple[int, int]] | None = None
        cardinality: dict[tuple, dict[str, Any]] = {}
        if mode in {"equivalence", "values"}:
            unit = self._unit(scope)
            distinct = self._distinct(query, live, unit, kinds.get(unit, set()))
            if distinct is None:
                self._note_approximate(unit)
                cardinality = self._group_cardinality(query, sources, live, unit)
            else:
                distinct = {self._pad(sources, live, k): v for k, v in distinct.items()}
        groups: list[GroupRow] = []
        missing = 0
        for key, (count, bucket) in rows.items():
            if mode in {"equivalence", "values"}:
                if distinct is not None:
                    group_value, group_missing = self._distinct_value(mode, *distinct.get(key, (0, 0)))
                else:
                    group_value, group_missing = self._cardinality_value(mode, cardinality[key])
            elif mode == "identity":
                group_value, group_missing = count, 0
            else:
                group_value, group_missing = self._measure_value(scope, bucket, count, population)
            missing += group_missing
            share = (count / total) if scope.with_share and total else None
            groups.append(GroupRow(key=dict(zip(scope.group_by, key)), value=group_value, count=count, share=share))
        return groups, missing

    @staticmethod
    def _key(sources: list[Source | None], raw: dict[str, Any]) -> tuple:
        return tuple(None if s is None else s.decode(raw) for s in sources)

    @staticmethod
    def _pad(sources: list[Source | None], live: list[Source], key: tuple) -> tuple:
        """A key over the live sources, re-expanded to every group field (null where no source)."""
        values = iter(key)
        return tuple(None if s is None else next(values) for s in sources)

    def _group_cardinality(self, query: dict[str, Any], sources: list[Source | None], live: list[Source], unit: str) -> dict[tuple, dict[str, Any]]:
        found: dict[tuple, dict[str, Any]] = {}
        if not live:
            reply = self._search({"size": 0, "query": query, "track_total_hits": False, "aggs": self._cardinality_aggs(unit)})
            found[tuple(None for _ in sources)] = reply["aggregations"]
            return found
        parts = [p for s in live for p in s.parts]
        after: dict[str, Any] | None = None
        while True:
            composite: dict[str, Any] = {"composite": {"size": COMPOSITE_PAGE, "sources": [{n: s} for n, s in parts]}, "aggs": self._cardinality_aggs(unit)}
            if after is not None:
                composite["composite"]["after"] = after
            agg = self._search({"size": 0, "query": query, "track_total_hits": False, "aggs": {"groups": composite}})["aggregations"]["groups"]
            for bucket in agg["buckets"]:
                found[self._key(sources, bucket["key"])] = bucket
            after = agg.get("after_key")
            if not agg["buckets"] or after is None:
                return found

    def _ranked_terms(self, scope: ScopeSpec, query: dict[str, Any], total: int, source: Source) -> list[GroupRow]:
        """Exact ``top_n``/``nth`` of a single-field count without enumerating every group.

        A one-shard ``terms`` aggregation ordered by count (key ascending on ties) returns every group
        whose count is above the last returned count; the request grows until those groups decide the
        boundary rank (``top_n`` groups, or ``nth`` distinct counts), so ``rank_groups`` sees every
        group that can be kept or tie. More than ``max_groups`` needed is ``TooManyGroups``."""
        descending = (scope.order or "desc") == "desc"
        (name, part), = source.parts
        field = part["terms"]["field"]
        need = scope.top_n if scope.nth is None else scope.nth
        size = max(16, 2 * (need or 1) + 1)
        while True:
            size = min(size, self._b.max_groups + 1)
            terms = {"terms": {"field": field, "size": size, "order": [{"_count": "desc" if descending else "asc"}, {"_key": "asc"}]}}
            reply = self._search({"size": 0, "query": query, "track_total_hits": False, "aggs": {"top": terms, "nulls": {"filter": {"bool": {"must_not": [_exists(field)]}}}}})
            buckets = reply["aggregations"]["top"]["buckets"]
            nulls = int(reply["aggregations"]["nulls"]["doc_count"])
            # The engine's own exactness report: OpenSearch 3.x searches even a one-shard index in
            # concurrent segment slices (``concurrent_segment_search.mode: auto``), each with its own
            # top-k, so counts are exact only when the reply says so. Otherwise grow the request.
            if reply["aggregations"]["top"].get("doc_count_error_upper_bound") != 0:
                if size > self._b.max_groups:
                    raise TooManyGroups
                size *= 2
                continue
            exhausted = len(buckets) < size
            candidates: list[tuple[Any, int]] = []
            for bucket in buckets:
                candidates.append((source.decode({name: bucket["key_as_string"] if "key_as_string" in bucket else bucket["key"]}), int(bucket["doc_count"])))
            if not exhausted:
                edge = candidates[-1][1]
                known = [c for c in candidates if (c[1] > edge if descending else c[1] < edge)]
                if nulls and (nulls > edge if descending else nulls < edge):
                    known.append((None, nulls))
                counts = sorted({c for _, c in known}, reverse=descending)
                decided = len(known) >= (scope.top_n or 0) if scope.nth is None else len(counts) >= scope.nth
                if decided:
                    candidates = known
                    nulls = 0
                elif size > self._b.max_groups:
                    raise TooManyGroups
                else:
                    size *= 2
                    continue
            if nulls:
                candidates.append((None, nulls))
            keys = [k for k, _ in candidates]
            if len(set(keys)) != len(keys):  # 3 and 3.0 are one Python key but two index terms
                raise BackendError(self._b.kind, "execute", f"group keys of {scope.group_by[0]!r} collide in Python; use records.aggregation: client for this field")
            return [GroupRow(key={scope.group_by[0]: k}, value=c, count=c, share=(c / total) if scope.with_share and total else None) for k, c in candidates]

    # --- ids and the record list ------------------------------------------------------------------

    def _matched_ids(self, query: dict[str, Any], total: int) -> tuple[list[str], bool]:
        cap = self._b.matched_ids_cap
        want = min(cap, total)
        ids: list[str] = []
        cursor: list[Any] | None = None
        while len(ids) < want:
            body: dict[str, Any] = {"size": min(ID_PAGE, want - len(ids)), "_source": False, "sort": [{"id": "asc"}], "query": query, "track_total_hits": False}
            if cursor is not None:
                body["search_after"] = cursor
            hits = self._search(body)["hits"]["hits"]
            if not hits:
                break
            ids.extend(h["_id"] for h in hits)
            cursor = hits[-1]["sort"]
        return ids, total > len(ids)

    def _listed(self, scope: ScopeSpec, query: dict[str, Any], kinds: dict[str, set[str]]) -> list[ListedRecord]:
        """``limit`` records in ``sort`` order, values missing last, ties by id (``_scope._list_records``)."""
        limit = scope.limit or 0
        if scope.sort is None:
            hits = self._search({"size": limit, "_source": False, "sort": [{"id": "asc"}], "query": query, "track_total_hits": False})["hits"]["hits"]
            return [ListedRecord(id=h["_id"]) for h in hits]
        field = scope.sort.field
        order = "desc" if scope.sort.descending else "asc"
        present_kinds = kinds.get(field, set())
        if self._temporal(field):
            present: dict[str, Any] | None = _exists(f"t.{field}") if "t" in present_kinds else None
            sort: list[dict[str, Any]] = [{f"t.{field}": {"order": order, "unmapped_type": "date"}}, {"id": "asc"}]
        else:
            sortable = present_kinds & {"n", "s"}
            if not sortable:
                present, sort = None, [{"id": "asc"}]
            elif len(sortable) == 2:  # mixed text and numbers never order: present records by id
                present = {"bool": {"should": [_exists(f"n.{field}"), _exists(f"s.{field}")], "minimum_should_match": 1}}
                sort = [{"id": "asc"}]
            else:
                kind = next(iter(sortable))
                present = _exists(f"{kind}.{field}")
                sort = [{f"{kind}.{field}": {"order": order}}, {"id": "asc"}]
        rows: list[StructuredRecord] = []
        if present is not None:
            body = {"size": limit, "_source": ["data"], "sort": sort, "query": _and(query, present), "track_total_hits": False}
            rows.extend(StructuredRecord.model_validate(h["_source"]["data"]) for h in self._search(body)["hits"]["hits"])
        if len(rows) < limit:
            absent_query = {"bool": {"filter": [query], "must_not": [present]}} if present is not None else query
            body = {"size": limit - len(rows), "_source": ["data"], "sort": [{"id": "asc"}], "query": absent_query, "track_total_hits": False}
            rows.extend(StructuredRecord.model_validate(h["_source"]["data"]) for h in self._search(body)["hits"]["hits"])
        return [ListedRecord(id=r.id, sort_value=r.values.get(field)) for r in rows]


def _hits_total(kind: str, reply: dict[str, Any]) -> int:
    total = reply["hits"]["total"]
    if isinstance(total, dict):
        if total.get("relation", "eq") != "eq":
            raise BackendError(kind, "execute", "the engine returned a lower-bound hit count despite track_total_hits")
        return int(total["value"])
    return int(total)

