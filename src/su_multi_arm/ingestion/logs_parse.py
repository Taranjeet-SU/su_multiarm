"""Partition-stage parsing (addendum A1 §3.4.2, §3.5, §3.10; slice L3a): redacted ``LogRecord``s of
one day through the compiled pattern library into per-record matches, the day's aggregates, the
hourly co-firing cells, exemplars and the unparsed queue.

Every record's head line (``message``) is processed with the
record's attributes (tenant, env, service from the index name; ``level`` from the level field when
the source has one). Statuses: ``parsed``, ``format_only``, ``unparsed``, ``noise``, ``timeout``;
head-less continuation lines are ``fragment`` (counted by multiline assembly, sampled into the
unparsed queue, excluded from coverage).

**Row exactness (§3.9.1).** A signature row is ``exact`` only when the coverage condition holds: the
unit was pulled whole (sample method ``none``, not capped) and either every line was pulled (no
records-tier query) or the tier query is level-field based and the pattern's ``applies_to.level``
is a subset of its levels. Otherwise the count is an estimate scaled from the pulled share,
``approximate`` with the sample method.

**Typed field values (format 2, addendum A2 §4.1.2, review B6).** Every match row also stores the
declared typed groups of its hits (``fields``: ``{name, value, type, start, end, source}`` with
head-line offsets; a value that fails its type or range validator is a ``gaps`` entry, never a
coerced value) and ``pattern_owner`` / ``pattern_reviewed``. The day's aggregates add rows the analytics kinds are composed from: ``levels``
(unit x level x status x signature), ``level_hours`` (the same per UTC hour), ``endpoints`` (unit x
normalised endpoint x method x status class, with latency sum/count/max and a per-row p95) and
``exceptions`` (unit x signature x exception class). Partitions sealed before this format carry
``format: 1`` and are reparsed from their retained records before compose (``PARTITION_FORMAT``).

**Parse pool.** ``workers > 1`` runs batches in a process pool. Each worker loads the library
itself and reports its identity; a batch parsed under another identity than the parent's fails the
parse (``LibraryDrift``), never mixes.

Exact repeated heads reuse a bounded in-memory match result with the same matching attributes,
library and accounting policy. Every occurrence remains in the output and aggregates; parse-work
counters distinguish regex evaluations, exact reuse, and stored delta-reparse reuse.
"""

from __future__ import annotations

import hashlib
import math
import multiprocessing
import re
from collections import Counter, defaultdict, deque
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import timezone
from typing import Any, Iterable

from ..core.errors import SuError
from ..sources.logs import LogRecord
from .accounting import AccountingPolicy, UnitAccounting, account_unit, deterministic_status, reviewed_ids
from .partitions import UnitArtifacts
from ..extraction.patterns.cache import ExactMatchCache

STATUSES = ("parsed", "format_only", "unparsed", "noise", "timeout")
# Partition artefact format: 1 = A1 (no typed values), 2 = typed values and analytics aggregates.
PARTITION_FORMAT = 2
# Typed group names the analytics kinds read (a library declares them as ``fields`` of a pattern).
ENDPOINT, HTTP_METHOD, STATUS_CODE, LATENCY_MS, EXCEPTION_CLASS = "endpoint", "http_method", "status_code", "latency_ms", "exception_class"
_ID_SEGMENT = re.compile(
    r"^(?:[0-9]+|[0-9a-fA-F]{8,}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[A-Za-z0-9_-]*[0-9][A-Za-z0-9_-]{15,})$"
)
WORST = {"exact": 0, "approximate": 1}


class LibraryDrift(SuError):
    """A pool worker parsed under another library identity than the partition's."""


@dataclass
class ParsedPartition:
    matches: list[dict[str, Any]] = field(default_factory=list)
    aggregates: dict[str, Any] = field(default_factory=dict)
    hourly_cells: list[dict[str, Any]] = field(default_factory=list)
    exemplars: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    unparsed: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    accounting: list[UnitAccounting] = field(default_factory=list)  # one per unit, checked before the seal
    format: int = PARTITION_FORMAT
    patterns: dict[str, Any] = field(default_factory=dict)  # library digest at parse (delta reparse)
    parse_work: dict[str, dict[str, int]] = field(default_factory=dict)  # operational diagnostics, not semantic aggregates


def _reuse_matches(records: list[LogRecord], library: Any, reuse: dict[str, dict[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
    """Stored rows for reused records (``level_selector`` restored from the unchanged entry),
    fresh matches for the rest, in record order."""
    entries = {e.entry.id: e.entry for e in library.library.approved()}
    reuse = {k: v for k, v in reuse.items() if "final_status" in v}  # rows without accounting are matched afresh
    fresh_records = [r for r in records if r.id not in reuse]
    fresh = iter(parse_matches(fresh_records, library, **kwargs))
    if kwargs.get("stats") is not None:
        kwargs["stats"]["stored_rows_reused"] = len(records) - len(fresh_records)
    out = []
    for record in records:
        stored = reuse.get(record.id)
        if stored is None:
            out.append(next(fresh))
            continue
        row = {k: v for k, v in stored.items() if k != "unit_id"}
        entry = entries.get(row.get("pattern_id") or "")
        row["level_selector"] = entry.selector("level") if entry is not None and entry.kind == "message" else None
        out.append(row)
    return out


def endpoint_template(path: str) -> str:
    """``/api/orders/12345?x=1`` -> ``/api/orders/{id}``: the query string is dropped and every
    path segment that looks like an identifier (digits, long hex, a UUID, a long token with a
    digit) becomes ``{id}``. Deterministic; never consults the data."""
    bare = path.split("?", 1)[0].split("#", 1)[0]
    return "/".join("{id}" if seg and _ID_SEGMENT.match(seg) else seg for seg in bare.split("/")) or "/"


def status_class(code: Any) -> str | None:
    if isinstance(code, bool) or not isinstance(code, int) or not 100 <= code <= 599:
        return None
    return f"{code // 100}xx"


def p95(values: list[float]) -> float | None:
    """Nearest-rank 95th percentile (a per-row value; not additive across rows)."""
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)])


def weighted_p95(values: list[tuple[float, int]]) -> float | None:
    """Exact nearest-rank percentile without materializing repeated occurrences."""
    if not values:
        return None
    rank = (95 * sum(n for _, n in values) + 99) // 100
    for value, n in sorted(values):
        rank -= n
        if rank <= 0:
            return value
    raise AssertionError("invalid weighted percentile counts")


HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"})


def _range_ok(name: str, value: Any) -> bool:
    """Per-field range validators of the analytics groups (review N17); other names pass."""
    if name == STATUS_CODE:
        return isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599
    if name == LATENCY_MS:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 3_600_000
    if name == HTTP_METHOD:
        return isinstance(value, str) and value.upper() in HTTP_METHODS
    if name == ENDPOINT:
        return isinstance(value, str) and value.startswith("/")
    return True


def typed_values(result: Any) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """``(fields, gaps)`` of the declared typed groups of a ``LineResult`` (message wins over
    annotations and format on a name clash). ``fields``: ``{name, value, type, start, end, source:
    "pattern"}`` with head-line offsets; a group that fails its type or range validator is a gap
    ``{name, error: type_invalid | range_invalid}``, never a coerced value."""
    from ..extraction.patterns.extractor import _typed

    fields: dict[str, dict[str, Any]] = {}
    gaps: dict[str, str] = {}
    for hit in [h for h in (result.format, *result.annotations, result.message) if h is not None]:
        for name, decl in hit.entry.entry.fields.items():
            span = hit.groups.get(name)
            if span is None:
                continue
            try:
                value = _typed(result.text[span[0] : span[1]], decl.type, decl.values)
            except ValueError:
                fields.pop(name, None)
                gaps[name] = "type_invalid"
                continue
            if not _range_ok(name, value):
                fields.pop(name, None)
                gaps[name] = "range_invalid"
                continue
            fields[name] = {"name": name, "value": value, "type": decl.type, "start": span[0], "end": span[1], "source": "pattern"}
            gaps.pop(name, None)
    return [fields[k] for k in sorted(fields)], [{"name": k, "error": gaps[k]} for k in sorted(gaps)]


def field_values(match: dict[str, Any]) -> dict[str, Any]:
    """``name -> value`` of a match row's typed fields."""
    return {f["name"]: f["value"] for f in match.get("fields") or []}


def record_text(record: LogRecord) -> str:
    """The rendered text of one record: head line, then stack lines; blank lines removed (the
    ``line`` recipe separates records with a blank line)."""
    lines = [line for line in record.message.split("\n") if line.strip()]
    if record.stack:
        lines.extend(line for line in record.stack.split("\n") if line.strip())
    return "\n".join(lines)


def _attributes(record: LogRecord) -> dict[str, str]:
    attrs = {k: v for k, v in record.attributes.items() if v}
    if record.level:
        attrs["level"] = record.level.casefold()
    return attrs


def match_records(
    records: list[LogRecord], library: Any, policy: AccountingPolicy | None = None, *,
    cache: ExactMatchCache | None = None, cache_enabled: bool = True, stats: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """One match row per head record, with its final status before any fallback (A2 §4.2.1):
    ``final_status``, ``reason`` (unresolved only), ``pattern_owner`` and ``extraction_source``
    (N2, N15) and ``gaps`` (required fields missing or invalid)."""
    policy = policy or AccountingPolicy()
    reviewed = reviewed_ids(getattr(library, "library", None))
    cache = (cache if cache is not None else ExactMatchCache()) if cache_enabled else None
    if cache is not None:
        cache.bind(library, policy)
    before = dict(cache.counts) if cache is not None else {}
    body_cache = getattr(library, "body_cache", None)
    body_before = dict(body_cache.counts) if body_cache is not None else {}
    out = []
    for record in records:
        head, attrs = record.message.split("\n", 1)[0], _attributes(record)
        key = (head, tuple(sorted(attrs.items())), record.level, record.truncated) if cache is not None and cache.should_lookup() else None
        cached = cache.get(key) if cache is not None and key is not None else None
        if cached is not None:
            out.append({**cached, "id": record.id})
            continue
        match_options = {"cache_body": cache_enabled} if body_cache is not None else {}
        result = library.match_line(head, field_name="message", attributes=attrs, **match_options)
        message = result.message.entry.entry if result.message else None
        level = record.level or (result.attributes.get("level") if result.format else None)
        fields, gaps = typed_values(result) if result.status != "timeout" else ([], [])
        row = {
            "id": record.id,
            "status": result.status,
            "signature": result.signature,
            "pattern_id": message.id if message else (result.noise.entry.entry.id if result.noise else None),
            "category": message.signature.category if message and message.signature else None,
            "level": level.casefold() if isinstance(level, str) else None,
            "level_source": "field" if record.level else ("format" if isinstance(level, str) else None),
            # review N2: who owns the matched message entry, and whether a human reviewed it
            "pattern_owner": ("user" if result.message.entry.owner == "user" else "learned") if result.message else "none",
            "pattern_reviewed": bool(result.message and library.library.reviewed(result.message.entry)),
            "level_selector": message.selector("level") if message else None,
            "truncated": bool(result.truncated or record.truncated),
        }
        # A2 accounting (C1): final status, reason, account owner, required-field gaps
        row.update(deterministic_status(result, required=policy.required, reviewed=reviewed, validators=policy.validators, pending=policy.pending))
        if fields:
            row["fields"] = fields
        if gaps:
            row["gaps"] = gaps
        if cache is not None and key is not None:
            cache.put(key, row)
        out.append(row)
    if stats is not None:
        stats.clear()
        for name, counter in (("head_match_evaluations", "evaluations"), ("exact_match_reuses", "reuses"), ("cache_evictions", "evictions"), ("oversized_results", "oversized"), ("cache_bypasses", "bypasses"), ("cache_admission_skips", "admission_skips")):
            stats[name] = cache.counts[counter] - before.get(counter, 0) if cache is not None else (len(records) if name == "head_match_evaluations" else 0)
        stats["stored_rows_reused"] = 0
        for name, counter in (("body_match_evaluations", "evaluations"), ("body_match_reuses", "reuses")):
            stats[name] = body_cache.counts[counter] - body_before.get(counter, 0) if body_cache is not None else 0
    return out


_WORKER: dict[str, Any] = {}


def _init_worker(ref_json: str, policy_json: str | None = None, cache_enabled: bool = True) -> None:
    from ..core.config import PatternLibraryRef
    from ..extraction.patterns.extractor import PatternExtractor

    ref = PatternLibraryRef.model_validate_json(ref_json)
    _WORKER["library"] = PatternExtractor(ref, None).load()
    _WORKER["policy"] = AccountingPolicy.from_json(policy_json) if policy_json else None
    _WORKER["cache"] = ExactMatchCache()
    _WORKER["cache_enabled"] = cache_enabled


def _worker_batch(records_json: list[str]) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    library = _WORKER["library"]
    records = [LogRecord.model_validate_json(r) for r in records_json]
    stats: dict[str, int] = {}
    matches = match_records(records, library, _WORKER.get("policy"), cache=_WORKER["cache"], cache_enabled=_WORKER["cache_enabled"], stats=stats)
    return library.identity, matches, stats


class _ParsePool:
    """One lazily started pool for a partition, with bounded queued JSON batches."""

    def __init__(self, library: Any, *, workers: int, batch_size: int, ref: Any, policy: AccountingPolicy | None, cache_enabled: bool = True):
        if batch_size < 1:
            raise ValueError("parse batch_size must be positive")
        self.library, self.workers, self.batch_size = library, workers, batch_size
        self.ref, self.policy, self.cache_enabled = ref, policy, cache_enabled
        self.pool: ProcessPoolExecutor | None = None

    def __enter__(self) -> _ParsePool:
        return self

    def __exit__(self, *_: Any) -> None:
        if self.pool is not None:
            self.pool.shutdown(wait=True, cancel_futures=True)
            self.pool = None

    def match(self, records: list[LogRecord], stats: dict[str, int] | None = None) -> list[dict[str, Any]]:
        if self.workers <= 1 or len(records) <= self.batch_size:
            return match_records(records, self.library, self.policy, cache_enabled=self.cache_enabled, stats=stats)
        if self.ref is None:
            raise ValueError("a parse pool needs the pattern library reference")
        if self.pool is None:
            # The parent holds lease heartbeat threads: spawn instead of unsafe threaded fork.
            self.pool = ProcessPoolExecutor(max_workers=self.workers, mp_context=multiprocessing.get_context("spawn"),
                initializer=_init_worker, initargs=(self.ref.model_dump_json(), self.policy.to_json() if self.policy else None, self.cache_enabled))
        batches = ([r.model_dump_json() for r in records[i:i + self.batch_size]]
                   for i in range(0, len(records), self.batch_size))
        pending: deque[Any] = deque()
        for _ in range(self.workers * 2):
            batch = next(batches, None)
            if batch is None:
                break
            pending.append(self.pool.submit(_worker_batch, batch))
        out: list[dict[str, Any]] = []
        totals: Counter[str] = Counter()
        while pending:
            identity, matches, batch_stats = pending.popleft().result()
            if identity != self.library.identity:
                raise LibraryDrift(f"a parse worker loaded library {identity[:12]}, the partition uses {self.library.identity[:12]}; reparse after the library settles")
            out.extend(matches)
            totals.update(batch_stats)
            batch = next(batches, None)
            if batch is not None:
                pending.append(self.pool.submit(_worker_batch, batch))
        if stats is not None:
            stats.clear()
            stats.update(totals)
        return out


def parse_matches(
    records: list[LogRecord], library: Any, *, workers: int = 1, batch_size: int = 2000, ref: Any = None, policy: AccountingPolicy | None = None,
    cache_enabled: bool = True, stats: dict[str, int] | None = None, _session: _ParsePool | None = None,
) -> list[dict[str, Any]]:
    """Matches in record order; a process pool when ``workers > 1`` (needs the library ``ref``)."""
    if _session is not None:
        return _session.match(records, stats)
    with _ParsePool(library, workers=workers, batch_size=batch_size, ref=ref, policy=policy, cache_enabled=cache_enabled) as session:
        return session.match(records, stats)


def _exact(artifacts: UnitArtifacts, selector: list[str] | None) -> bool:
    if artifacts.incident is not None:
        return True  # exact observed subset count, never a whole-source claim
    if artifacts.sample.method != "none" or artifacts.capped:
        return False
    if artifacts.records_query is None:
        return True
    if artifacts.records_basis != "level_field" or selector is None:
        return False
    return {s.casefold() for s in selector} <= {lv.casefold() for lv in artifacts.records_levels}


def parse_units(
    units: Iterable[UnitArtifacts],
    library: Any,
    *,
    workers: int = 1,
    batch_size: int = 2000,
    ref: Any = None,
    max_exemplars: int = 3,
    max_unparsed: int = 2000,
    policy: AccountingPolicy | None = None,
    reuse: dict[str, dict[str, Any]] | None = None,
) -> ParsedPartition:
    """``reuse`` (delta reparse, ``ingestion/reparse.py``): stored match rows by record id that
    are copied instead of re-matched; every other record is matched under ``library``."""
    with _ParsePool(library, workers=workers, batch_size=batch_size, ref=ref, policy=policy) as session:
        return _parse_units(units, library, workers=workers, batch_size=batch_size, ref=ref,
                            max_exemplars=max_exemplars, max_unparsed=max_unparsed, policy=policy,
                            reuse=reuse, _session=session)


def _parse_units(
    units: Iterable[UnitArtifacts], library: Any, *, workers: int, batch_size: int, ref: Any,
    max_exemplars: int, max_unparsed: int, policy: AccountingPolicy | None,
    reuse: dict[str, dict[str, Any]] | None, _session: _ParsePool,
) -> ParsedPartition:
    from .reparse import library_digest

    parsed = ParsedPartition()
    parsed.patterns = library_digest(library) if hasattr(library, "library") else {}
    counts: Counter[str] = Counter()
    unit_rows: list[dict[str, Any]] = []
    signature_rows: dict[tuple[str, str], dict[str, Any]] = {}
    cells: Counter[tuple[str, str, str, str, str]] = Counter()
    exemplars: dict[str, list[dict[str, Any]]] = defaultdict(list)
    level_rows: list[dict[str, Any]] = []
    hour_rows: list[dict[str, Any]] = []
    endpoint_rows: list[dict[str, Any]] = []
    exception_rows: list[dict[str, Any]] = []
    for artifacts in sorted(units, key=lambda a: a.unit.id):
        artifacts.validate_grouped()  # also protects callers that mutated a model after construction
        unit, attrs = artifacts.unit, artifacts.unit.attributes
        incident_meta = ({"collection_scope": artifacts.incident.collection_scope,
                          "count_scope": "selected_seed_records", "incident_policy_id": artifacts.incident.policy_id,
                          "incident_query_id": artifacts.incident.query_id,
                          "collection_window_start": artifacts.incident.window_start.isoformat(),
                          "collection_window_end": artifacts.incident.window_end.isoformat()}
                         if artifacts.incident else {})
        attrs = {**attrs, **incident_meta}
        incident_members = artifacts.incident.members() if artifacts.incident else {}
        records = sorted(artifacts.records, key=lambda r: (r.ts, r.id))
        parse_work: dict[str, int] = {}
        if reuse is None:
            matches = parse_matches(records, library, workers=workers, batch_size=batch_size, ref=ref, policy=policy, stats=parse_work, _session=_session)
        else:
            matches = _reuse_matches(records, library, reuse, workers=workers, batch_size=batch_size, ref=ref, policy=policy, stats=parse_work, _session=_session)
        parsed.parse_work[unit.id] = parse_work
        statuses: Counter[str] = Counter()
        represented_statuses: Counter[str] = Counter()
        by_signature: Counter[str] = Counter()
        observed_signature: Counter[str] = Counter()
        meta: dict[str, dict[str, Any]] = {}
        by_level: Counter[tuple[str, str, str]] = Counter()
        observed_level: Counter[tuple[str, str, str]] = Counter()
        by_hour: Counter[tuple[str, str, str, str]] = Counter()
        observed_hour: Counter[tuple[str, str, str, str]] = Counter()
        endpoints: dict[tuple[str, str, str], dict[str, Any]] = {}
        exceptions: Counter[tuple[str, str]] = Counter()
        observed_exceptions: Counter[tuple[str, str]] = Counter()
        level_sources: dict[str, set[str]] = defaultdict(set)
        for record, match in zip(records, matches):
            # Reuse only the regex result; occurrence metadata belongs to this input version.
            for key in ("group_id", "occurrence_count", "hour_start", "first_seen", "last_seen", "count_basis", "exemplar_only_fields"):
                match.pop(key, None)
            group = artifacts.grouped.get(record.id)
            weight = group.occurrence_count if group else 1
            if group:
                match.update(group_id=group.group_id, occurrence_count=weight,
                             hour_start=group.hour_start.isoformat(), first_seen=group.first_seen.isoformat(),
                             last_seen=group.last_seen.isoformat(), count_basis="exact_groups",
                             exemplar_only_fields=list(group.exemplar_only_fields))
            match["unit_id"] = unit.id
            if artifacts.incident:
                match["collection_scope"] = artifacts.incident.collection_scope
                match["incident_roles"] = incident_members[record.id]
            parsed.matches.append(match)
            statuses[match["status"]] += 1
            represented_statuses[match["status"]] += weight
            if artifacts.incident and "seed" not in incident_members[record.id]:
                # Context is parsed evidence, not another policy-selected event. In particular,
                # ERROR context outside the seed window must not inflate daily incident totals.
                counts["truncated"] += int(match["truncated"])
                continue
            level_key = match["level"] or "unknown"
            if match["level_source"]:
                level_sources[level_key].add(match["level_source"])
            hour_of = record.ts.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
            by_level[(level_key, match["status"], match["signature"] or "")] += weight
            observed_level[(level_key, match["status"], match["signature"] or "")] += 1
            by_hour[(hour_of, level_key, match["status"], match["signature"] or "")] += weight
            observed_hour[(hour_of, level_key, match["status"], match["signature"] or "")] += 1
            values = field_values(match)
            if isinstance(values.get(ENDPOINT), str):
                key = (endpoint_template(values[ENDPOINT]), str(values.get(HTTP_METHOD) or "").upper() or "UNKNOWN", status_class(values.get(STATUS_CODE)) or "unknown")
                bucket = endpoints.setdefault(key, {"lines": 0, "observed": 0, "latencies": []})
                bucket["lines"] += weight
                bucket["observed"] += 1
                latency = values.get(LATENCY_MS)
                if isinstance(latency, (int, float)) and not isinstance(latency, bool):
                    bucket["latencies"].append((float(latency), weight))
            if isinstance(values.get(EXCEPTION_CLASS), str) and match["signature"]:
                exceptions[(match["signature"], values[EXCEPTION_CLASS])] += weight
                observed_exceptions[(match["signature"], values[EXCEPTION_CLASS])] += 1
            counts["truncated"] += int(match["truncated"])
            signature = match["signature"]
            if signature:
                by_signature[signature] += weight
                observed_signature[signature] += 1
                meta.setdefault(signature, match)
                hour = record.ts.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
                cells[(signature, attrs.get("tenant", "unknown"), attrs.get("env", "unknown"), attrs.get("service", "unknown"), hour)] += weight
                # up to k per (signature, tenant): a tenant-scoped exemplar document never needs
                # another tenant's lines (review B4)
                tenant = attrs.get("tenant")
                if sum(1 for e in exemplars[signature] if e.get("tenant") == tenant) < max_exemplars:
                    exemplars[signature].append({"id": record.id, "unit_id": unit.id, "ts": record.ts.isoformat(), "service": attrs.get("service"), "tenant": tenant, "text": record_text(record)})
            elif match["status"] in {"unparsed", "format_only"} and len(parsed.unparsed) < max_unparsed:
                parsed.unparsed.append({"id": record.id, "unit_id": unit.id, "status": match["status"], "level": match["level"], "service": attrs.get("service"), "text": record_text(record)})
        for fragment in artifacts.fragment_samples:
            if len(parsed.unparsed) < max_unparsed:
                parsed.unparsed.append({"id": fragment.id, "unit_id": unit.id, "status": "fragment", "level": None, "service": attrs.get("service"), "text": record_text(fragment)})
        parsed.accounting.append(account_unit(artifacts, matches))
        pulled = len(records)
        unit_exact = artifacts.sample.method == "none" and not artifacts.capped
        records_total = artifacts.count.by_query.get(artifacts.records_query, artifacts.count.total) if artifacts.records_query else artifacts.count.total
        scale = 1.0 if artifacts.incident or unit_exact or not pulled else records_total / pulled
        for status in STATUSES:
            counts[status] += statuses[status]
        counts["records"] += pulled
        counts["fragments"] += artifacts.fragments
        unit_rows.append(
            {
                "unit_id": unit.id,
                "index": unit.index,
                "attributes": dict(attrs),
                "lines_total": artifacts.count.total,
                "by_query": dict(artifacts.count.by_query),
                "basis": dict(artifacts.count.basis),
                "pulled": pulled,
                # Independent counting units: source documents vs assembled parser records.
                # Old derived aggregates omit these counters and remain unavailable, not 100%.
                "documents_collected": artifacts.docs_pulled,
                "documents_in_collection_scope": records_total,
                "collection_scope": artifacts.incident.collection_scope if artifacts.incident else ("all_source_documents" if artifacts.records_query is None else "selected_records_tier"),
                "parser_heads_scanned": len(matches),
                "statuses": {s: statuses[s] for s in STATUSES},
                "fragments": artifacts.fragments,
                "method": artifacts.sample.label(),
                "capped": artifacts.capped,
                "row_exactness": "exact" if unit_exact else "approximate",
            }
        )
        if artifacts.incident:
            unit_rows[-1].update(artifacts.incident.counters())
            unit_rows[-1]["incident_provenance"] = artifacts.incident.summary().model_dump(mode="json")
        method = "incident_subset" if artifacts.incident else ("exact_groups" if artifacts.grouped else artifacts.sample.label())
        if artifacts.incident:
            unit_rows[-1]["method"] = method
        if artifacts.grouped:
            unit_rows[-1].update(
                method=method, documents_represented=artifacts.count.total,
                records_represented=sum(represented_statuses.values()),
                represented_statuses={s: represented_statuses[s] for s in STATUSES},
                grouped_records=len(artifacts.grouped),
            )

        def estimate(lines: int, exact: bool) -> int:
            return lines if exact else int(round(lines * scale))

        def row(exact: bool, lines: int, *, observed: int | None = None, **values: Any) -> dict[str, Any]:
            return {
                "unit_id": unit.id,
                "attributes": dict(attrs),
                **values,
                "lines": estimate(lines, exact),
                "lines_pulled": lines if observed is None else observed,
                "row_exactness": "exact" if exact else "approximate",
                "method": method,
            }

        for (level_key, status, signature), lines in sorted(by_level.items()):
            source_of = ",".join(sorted(level_sources.get(level_key, set()))) or None
            category = (meta[signature]["category"] or "unknown") if signature else None
            level_rows.append(row(_exact(artifacts, [level_key]), lines, observed=observed_level[(level_key, status, signature)], level=level_key, level_source=source_of, status=status, signature_id=signature or None, category=category, pattern_owner=meta[signature].get("pattern_owner", "none") if signature else "none"))
        for (hour_of, level_key, status, signature), lines in sorted(by_hour.items()):
            hour_rows.append(row(_exact(artifacts, [level_key]), lines, observed=observed_hour[(hour_of, level_key, status, signature)], hour=hour_of, level=level_key, status=status, signature_id=signature or None))
        for (template, http_method, klass), bucket in sorted(endpoints.items()):
            latencies = bucket["latencies"]
            endpoint_rows.append(
                row(
                    unit_exact and artifacts.records_query is None,
                    bucket["lines"],
                    observed=bucket["observed"],
                    endpoint=template,
                    http_method=http_method,
                    status_class=klass,
                    latency_ms_sum=float(sum(v * n for v, n in latencies)),
                    latency_ms_count=sum(n for _, n in latencies),
                    latency_ms_max=max(v for v, _ in latencies) if latencies else None,
                    latency_ms_p95=weighted_p95(latencies),
                )
            )
        for (signature, klass), lines in sorted(exceptions.items()):
            exception_rows.append(row(_exact(artifacts, meta[signature]["level_selector"]), lines, observed=observed_exceptions[(signature, klass)], signature_id=signature, exception_class=klass))
        for signature, lines in sorted(by_signature.items()):
            first = meta[signature]
            exact = _exact(artifacts, first["level_selector"])
            signature_rows[(unit.id, signature)] = {
                "unit_id": unit.id,
                "attributes": dict(attrs),
                "signature_id": signature,
                "pattern_id": first["pattern_id"],
                "category": first["category"] or "unknown",
                "lines": lines if exact else int(round(lines * scale)),
                "lines_pulled": observed_signature[signature],
                "row_exactness": "exact" if exact else "approximate",
                "method": method,
                "pattern_owner": first.get("pattern_owner", "none"),
                "pattern_reviewed": bool(first.get("pattern_reviewed")),
            }
    parsed.aggregates = {
        "format": PARTITION_FORMAT,
        "units": unit_rows,
        "signatures": [signature_rows[k] for k in sorted(signature_rows)],
        "levels": level_rows,
        "level_hours": hour_rows,
        "endpoints": endpoint_rows,
        "exceptions": exception_rows,
    }
    exact_units = {(r["attributes"].get("tenant", "unknown"), r["attributes"].get("env", "unknown"), r["attributes"].get("service", "unknown")): r["row_exactness"] for r in unit_rows}
    deployments = {(r["attributes"].get("tenant", "unknown"), r["attributes"].get("env", "unknown"), r["attributes"].get("service", "unknown")): r["attributes"].get("deployment") for r in unit_rows}
    parsed.hourly_cells = [
        {
            "signature_id": s,
            "tenant": t,
            "env": e,
            "service": v,
            # format 2 (review N8): the deployment (index-name attribute, else tenant-env) and the
            # unit's exactness, so a co-firing incident can report the worst of its cells
            "deployment": deployments.get((t, e, v)) or f"{t}-{e}",
            "hour": h,
            "lines": n,
            "row_exactness": exact_units.get((t, e, v), "approximate"),
        }
        for (s, t, e, v, h), n in sorted(cells.items())
    ]
    parsed.exemplars = dict(sorted(exemplars.items()))
    parsed.counts = dict(counts)
    for match in parsed.matches:
        match.pop("level_selector", None)
    return parsed


def matches_digest(matches: list[dict[str, Any]]) -> str:
    return hashlib.sha256("\n".join(f"{m['id']}:{m['status']}:{m['signature']}" for m in matches).encode()).hexdigest()
