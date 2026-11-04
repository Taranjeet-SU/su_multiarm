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

**Parse pool.** ``workers > 1`` runs batches in a process pool. Each worker loads the library
itself and reports its identity; a batch parsed under another identity than the parent's fails the
parse (``LibraryDrift``), never mixes.
"""

from __future__ import annotations

import hashlib
import multiprocessing
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import timezone
from typing import Any, Iterable

from ..core.errors import SuError
from ..sources.logs import LogRecord
from .partitions import UnitArtifacts

STATUSES = ("parsed", "format_only", "unparsed", "noise", "timeout")
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


def match_records(records: list[LogRecord], library: Any) -> list[dict[str, Any]]:
    out = []
    for record in records:
        result = library.match_line(record.message.split("\n", 1)[0], field_name="message", attributes=_attributes(record))
        message = result.message.entry.entry if result.message else None
        level = record.level or (result.attributes.get("level") if result.format else None)
        out.append(
            {
                "id": record.id,
                "status": result.status,
                "signature": result.signature,
                "pattern_id": message.id if message else (result.noise.entry.entry.id if result.noise else None),
                "category": message.signature.category if message and message.signature else None,
                "level": level.casefold() if isinstance(level, str) else None,
                "level_selector": message.selector("level") if message else None,
                "truncated": bool(result.truncated or record.truncated),
            }
        )
    return out


_WORKER: dict[str, Any] = {}


def _init_worker(ref_json: str) -> None:
    from ..core.config import PatternLibraryRef
    from ..extraction.patterns.extractor import PatternExtractor

    ref = PatternLibraryRef.model_validate_json(ref_json)
    _WORKER["library"] = PatternExtractor(ref, None).load()


def _worker_batch(records_json: list[str]) -> tuple[str, list[dict[str, Any]]]:
    library = _WORKER["library"]
    records = [LogRecord.model_validate_json(r) for r in records_json]
    return library.identity, match_records(records, library)


def parse_matches(records: list[LogRecord], library: Any, *, workers: int = 1, batch_size: int = 2000, ref: Any = None) -> list[dict[str, Any]]:
    """Matches in record order; a process pool when ``workers > 1`` (needs the library ``ref``)."""
    if workers <= 1 or len(records) <= batch_size:
        return match_records(records, library)
    if ref is None:
        raise ValueError("a parse pool needs the pattern library reference")
    batches = [[r.model_dump_json() for r in records[i : i + batch_size]] for i in range(0, len(records), batch_size)]
    out: list[dict[str, Any]] = []
    # spawn, not fork: the parent holds lease heartbeat threads (fork of a threaded process is unsafe)
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=context, initializer=_init_worker, initargs=(ref.model_dump_json(),)) as pool:
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
) -> ParsedPartition:
    parsed = ParsedPartition()
    counts: Counter[str] = Counter()
    unit_rows: list[dict[str, Any]] = []
    signature_rows: dict[tuple[str, str], dict[str, Any]] = {}
    cells: Counter[tuple[str, str, str, str, str]] = Counter()
    exemplars: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for artifacts in sorted(units, key=lambda a: a.unit.id):
        unit, attrs = artifacts.unit, artifacts.unit.attributes
        records = sorted(artifacts.records, key=lambda r: (r.ts, r.id))
        matches = parse_matches(records, library, workers=workers, batch_size=batch_size, ref=ref)
        statuses: Counter[str] = Counter()
        by_signature: Counter[str] = Counter()
        meta: dict[str, dict[str, Any]] = {}
        for record, match in zip(records, matches):
            match["unit_id"] = unit.id
            parsed.matches.append(match)
            statuses[match["status"]] += 1
            counts["truncated"] += int(match["truncated"])
            signature = match["signature"]
            if signature:
                by_signature[signature] += 1
                meta.setdefault(signature, match)
                hour = record.ts.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
                cells[(signature, attrs.get("tenant", "unknown"), attrs.get("env", "unknown"), attrs.get("service", "unknown"), hour)] += 1
                if len(exemplars[signature]) < max_exemplars:
                    exemplars[signature].append({"id": record.id, "unit_id": unit.id, "ts": record.ts.isoformat(), "service": attrs.get("service"), "text": record_text(record)})
            elif match["status"] in {"unparsed", "format_only"} and len(parsed.unparsed) < max_unparsed:
                parsed.unparsed.append({"id": record.id, "unit_id": unit.id, "status": match["status"], "level": match["level"], "service": attrs.get("service"), "text": record_text(record)})
        for fragment in artifacts.fragment_samples:
            if len(parsed.unparsed) < max_unparsed:
                parsed.unparsed.append({"id": fragment.id, "unit_id": unit.id, "status": "fragment", "level": None, "service": attrs.get("service"), "text": record_text(fragment)})
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
            }
    parsed.aggregates = {"units": unit_rows, "signatures": [signature_rows[k] for k in sorted(signature_rows)]}
    parsed.hourly_cells = [
        {"signature_id": s, "tenant": t, "env": e, "service": v, "hour": h, "lines": n} for (s, t, e, v, h), n in sorted(cells.items())
    ]
    parsed.exemplars = dict(sorted(exemplars.items()))
    parsed.counts = dict(counts)
    for match in parsed.matches:
        match.pop("level_selector", None)
    return parsed


def matches_digest(matches: list[dict[str, Any]]) -> str:
    return hashlib.sha256("\n".join(f"{m['id']}:{m['status']}:{m['signature']}" for m in matches).encode()).hexdigest()
