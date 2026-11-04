"""Partition-stage parsing (addendum A1 §3.4.2, §3.5, §3.10; slice L3a): redacted ``LogRecord``s of
one day through the compiled pattern library into per-record matches, the day's aggregates, the
hourly co-firing cells, exemplars and the unparsed queue.

Every record's head line (``message``) is matched once (``CompiledLibrary.match_line``) with the
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
"""

from __future__ import annotations

import hashlib
import math
import multiprocessing
import re
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import timezone
from typing import Any, Iterable

from ..core.errors import SuError
from ..sources.logs import LogRecord
from .accounting import AccountingPolicy, UnitAccounting, account_unit, deterministic_status, reviewed_ids
from .partitions import UnitArtifacts

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


def _reuse_matches(records: list[LogRecord], library: Any, reuse: dict[str, dict[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
    """Stored rows for reused records (``level_selector`` restored from the unchanged entry),
    fresh matches for the rest, in record order."""
    entries = {e.entry.id: e.entry for e in library.library.approved()}
    reuse = {k: v for k, v in reuse.items() if "final_status" in v}  # rows without accounting are matched afresh
    fresh_records = [r for r in records if r.id not in reuse]
    fresh = iter(parse_matches(fresh_records, library, **kwargs))
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


def match_records(records: list[LogRecord], library: Any, policy: AccountingPolicy | None = None) -> list[dict[str, Any]]:
    """One match row per head record, with its final status before any fallback (A2 §4.2.1):
    ``final_status``, ``reason`` (unresolved only), ``pattern_owner`` and ``extraction_source``
    (N2, N15) and ``gaps`` (required fields missing or invalid)."""
    policy = policy or AccountingPolicy()
    reviewed = reviewed_ids(getattr(library, "library", None))
    out = []
    for record in records:
        result = library.match_line(record.message.split("\n", 1)[0], field_name="message", attributes=_attributes(record))
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
        out.append(row)
    return out


_WORKER: dict[str, Any] = {}


def _init_worker(ref_json: str, policy_json: str | None = None) -> None:
    from ..core.config import PatternLibraryRef
    from ..extraction.patterns.extractor import PatternExtractor

    ref = PatternLibraryRef.model_validate_json(ref_json)
    _WORKER["library"] = PatternExtractor(ref, None).load()
    _WORKER["policy"] = AccountingPolicy.from_json(policy_json) if policy_json else None


def _worker_batch(records_json: list[str]) -> tuple[str, list[dict[str, Any]]]:
    library = _WORKER["library"]
    records = [LogRecord.model_validate_json(r) for r in records_json]
    return library.identity, match_records(records, library, _WORKER.get("policy"))


def parse_matches(
    records: list[LogRecord], library: Any, *, workers: int = 1, batch_size: int = 2000, ref: Any = None, policy: AccountingPolicy | None = None
) -> list[dict[str, Any]]:
    """Matches in record order; a process pool when ``workers > 1`` (needs the library ``ref``)."""
    if workers <= 1 or len(records) <= batch_size:
        return match_records(records, library, policy)
    if ref is None:
        raise ValueError("a parse pool needs the pattern library reference")
    batches = [[r.model_dump_json() for r in records[i : i + batch_size]] for i in range(0, len(records), batch_size)]
    out: list[dict[str, Any]] = []
    # spawn, not fork: the parent holds lease heartbeat threads (fork of a threaded process is unsafe)
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=context, initializer=_init_worker, initargs=(ref.model_dump_json(), policy.to_json() if policy else None)) as pool:
        for identity, matches in pool.map(_worker_batch, batches):
            if identity != library.identity:
                raise LibraryDrift(f"a parse worker loaded library {identity[:12]}, the partition uses {library.identity[:12]}; reparse after the library settles")
            out.extend(matches)
    return out


def _exact(artifacts: UnitArtifacts, selector: list[str] | None) -> bool:
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
        unit, attrs = artifacts.unit, artifacts.unit.attributes
        records = sorted(artifacts.records, key=lambda r: (r.ts, r.id))
        if reuse is None:
            matches = parse_matches(records, library, workers=workers, batch_size=batch_size, ref=ref, policy=policy)
        else:
            matches = _reuse_matches(records, library, reuse, workers=workers, batch_size=batch_size, ref=ref, policy=policy)
        statuses: Counter[str] = Counter()
        by_signature: Counter[str] = Counter()
        meta: dict[str, dict[str, Any]] = {}
        by_level: Counter[tuple[str, str, str]] = Counter()
        by_hour: Counter[tuple[str, str, str, str]] = Counter()
        endpoints: dict[tuple[str, str, str], dict[str, Any]] = {}
        exceptions: Counter[tuple[str, str]] = Counter()
        level_sources: dict[str, set[str]] = defaultdict(set)
        for record, match in zip(records, matches):
            match["unit_id"] = unit.id
            parsed.matches.append(match)
            statuses[match["status"]] += 1
            level_key = match["level"] or "unknown"
            if match["level_source"]:
                level_sources[level_key].add(match["level_source"])
            hour_of = record.ts.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
            by_level[(level_key, match["status"], match["signature"] or "")] += 1
            by_hour[(hour_of, level_key, match["status"], match["signature"] or "")] += 1
            values = field_values(match)
            if isinstance(values.get(ENDPOINT), str):
                key = (endpoint_template(values[ENDPOINT]), str(values.get(HTTP_METHOD) or "").upper() or "UNKNOWN", status_class(values.get(STATUS_CODE)) or "unknown")
                bucket = endpoints.setdefault(key, {"lines": 0, "latencies": []})
                bucket["lines"] += 1
                latency = values.get(LATENCY_MS)
                if isinstance(latency, (int, float)) and not isinstance(latency, bool):
                    bucket["latencies"].append(float(latency))
            if isinstance(values.get(EXCEPTION_CLASS), str) and match["signature"]:
                exceptions[(match["signature"], values[EXCEPTION_CLASS])] += 1
            counts["truncated"] += int(match["truncated"])
            signature = match["signature"]
            if signature:
                by_signature[signature] += 1
                meta.setdefault(signature, match)
                hour = record.ts.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
                cells[(signature, attrs.get("tenant", "unknown"), attrs.get("env", "unknown"), attrs.get("service", "unknown"), hour)] += 1
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
        scale = 1.0 if unit_exact or not pulled else records_total / pulled
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
                "statuses": {s: statuses[s] for s in STATUSES},
                "fragments": artifacts.fragments,
                "method": artifacts.sample.label(),
                "capped": artifacts.capped,
                "row_exactness": "exact" if unit_exact else "approximate",
            }
        )
        method = artifacts.sample.label()

        def estimate(lines: int, exact: bool) -> int:
            return lines if exact else int(round(lines * scale))

        def row(exact: bool, lines: int, **values: Any) -> dict[str, Any]:
            return {
                "unit_id": unit.id,
                "attributes": dict(attrs),
                **values,
                "lines": estimate(lines, exact),
                "lines_pulled": lines,
                "row_exactness": "exact" if exact else "approximate",
                "method": method,
            }

        for (level_key, status, signature), lines in sorted(by_level.items()):
            source_of = ",".join(sorted(level_sources.get(level_key, set()))) or None
            category = (meta[signature]["category"] or "unknown") if signature else None
            level_rows.append(row(_exact(artifacts, [level_key]), lines, level=level_key, level_source=source_of, status=status, signature_id=signature or None, category=category, pattern_owner=meta[signature].get("pattern_owner", "none") if signature else "none"))
        for (hour_of, level_key, status, signature), lines in sorted(by_hour.items()):
            hour_rows.append(row(_exact(artifacts, [level_key]), lines, hour=hour_of, level=level_key, status=status, signature_id=signature or None))
        for (template, http_method, klass), bucket in sorted(endpoints.items()):
            latencies = bucket["latencies"]
            endpoint_rows.append(
                row(
                    unit_exact and artifacts.records_query is None,
                    bucket["lines"],
                    endpoint=template,
                    http_method=http_method,
                    status_class=klass,
                    latency_ms_sum=float(sum(latencies)),
                    latency_ms_count=len(latencies),
                    latency_ms_max=max(latencies) if latencies else None,
                    latency_ms_p95=p95(latencies),
                )
            )
        for (signature, klass), lines in sorted(exceptions.items()):
            exception_rows.append(row(_exact(artifacts, meta[signature]["level_selector"]), lines, signature_id=signature, exception_class=klass))
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
                "lines_pulled": lines,
                "row_exactness": "exact" if exact else "approximate",
                "method": artifacts.sample.label(),
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
