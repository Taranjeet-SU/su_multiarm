"""Operator probe (addendum A1 §4 L0): what a source cluster permits and how fast it serves, with
bounded load, through the same allow-list transport.

The report is **aggregate only**: version and capabilities, the self-check result, per-unit counts
and count bases, field presence for the mapped fields, index-name parse results, PIT and
``random_score`` support, scroll throughput on a bounded pull, and per-row request timings. It never
contains a message, a document field value, a header or a credential. The operator keeps it outside
any repository.
"""

from __future__ import annotations

import statistics
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import SourceWindow
from .elasticsearch_rest import ElasticsearchRestSource
from .errors import SourceError
from .logs import LogMapping
from .multiline import assemble
from .sampling import random_score_query


def day_window(day: str, tz: str) -> SourceWindow:
    zone = ZoneInfo(tz)
    start = datetime.fromisoformat(day).replace(tzinfo=zone)
    return SourceWindow(partition_key=day, start=start.astimezone(timezone.utc), end=(start + timedelta(days=1)).astimezone(timezone.utc))


def pit_check_status(source: ElasticsearchRestSource, caps: Any, requested: bool) -> tuple[bool, str]:
    """Whether the probe may open a PIT: only on request (``--probe-pit``), when the configuration
    does not disallow it and when the capabilities say the server supports it (a version that could
    not be read, e.g. after a 403 on ``GET /``, counts as unsupported). Returns ``(run, reason)``."""
    if not requested:
        return False, "not_requested"
    if "pit" in source.config.disallow:
        return False, "disallowed_by_config"
    if not caps.pit:
        return False, "unsupported_or_unknown_version"
    return True, "requested"


def run_probe(
    source: ElasticsearchRestSource,
    day: str,
    *,
    max_units: int = 3,
    max_docs: int = 1000,
    counts: bool = True,
    probe_pit: bool = False,
    random_score: bool = True,
) -> dict[str, Any]:
    """Bounded probe. ``max_units`` units get counts and the ``random_score`` check; one unit gets a
    scroll pull of at most ``max_docs`` documents for throughput and mapping statistics.

    The PIT check (open and close one point-in-time context per sampled unit) is **opt-in**
    (``probe_pit``) and runs only where ``pit_check_status`` allows it, so a probe never sends a
    request the deployment or the server does not permit. A skipped check leaves ``capabilities.pit``
    as the version probe reported it and records why under ``pit_check``."""
    report: dict[str, Any] = {"source": source.source_name, "kind": source.name, "day": day, "errors": []}
    caps = source.capabilities(probe=True)
    report["self_check"] = [d.model_dump() for d in source.self_check()]
    run_pit, pit_reason = pit_check_status(source, caps, probe_pit)
    report["pit_check"] = {"status": "run" if run_pit else "skipped", "reason": pit_reason}
    window = day_window(day, source.config.timezone)
    units = source.discover(window)
    report["units_discovered"] = len(units)
    parsed = [u for u in units if "unknown" not in u.attributes.values()]
    report["index_names"] = {
        "parsed": len(parsed),
        "unparsed": len(units) - len(parsed),
        "excluded_scope": dict(source.excluded_scope),
        "services": sorted({u.attributes.get("service", "unknown") for u in units}),
    }
    mapped = source.config.mapping.requested()
    report["field_presence"] = {field: sum(1 for u in units if field in source.mappings.get(u.index, {})) for field in mapped}
    report["units"] = []
    sample_units = units[:max_units]
    for unit in sample_units:
        entry: dict[str, Any] = {"index": unit.index, "attributes": unit.attributes}
        try:
            if counts:
                counted = source.count(unit)
                entry.update(total=counted.total, by_query=counted.by_query, basis=counted.basis)
            if run_pit:
                entry["pit"] = source.probe_pit(unit)
            if random_score:
                entry["random_score_seq_no"] = _random_score_ok(source, unit)
        except SourceError as error:
            entry["error"] = f"{type(error).__name__}: {error}"
        report["units"].append(entry)
    if random_score and report["units"]:
        caps.random_score_seq_no = any(e.get("random_score_seq_no") for e in report["units"])
    if run_pit and report["units"]:
        caps.pit = any(e.get("pit") for e in report["units"])
    if sample_units and max_docs > 0:
        report["throughput"] = _throughput(source, sample_units[0], window, max_docs)
    report["capabilities"] = caps.model_dump()
    report["discovery_diagnostics"] = [d.model_dump() for d in source.diagnostics]
    report["requests"] = _request_stats(source)
    report["open_cursors_after"] = len(source.cursors.open_cursors())
    return report


def _random_score_ok(source: ElasticsearchRestSource, unit) -> bool:
    body = {"query": random_score_query({"match_all": {}}, 1), "size": 1, "_source": [source.config.mapping.timestamp]}
    try:
        source.transport.request("POST", f"/{unit.index}/_search", body=body)
    except SourceError:
        return False
    return True


def _throughput(source: ElasticsearchRestSource, unit, window: SourceWindow, max_docs: int) -> dict[str, Any]:
    from .contracts import PartitionBudget
    from .sampling import plan

    started = time.perf_counter()
    budget = PartitionBudget(limit=max_docs)
    sample = plan("none", cap=max_docs, seed=0, unit_total=max_docs)
    docs = []
    pages = 0
    try:
        for batch in source.pull(unit, sample=sample, budget=budget):
            pages += 1
            docs.extend(batch.docs)
    except SourceError as error:
        return {"error": f"{type(error).__name__}: {error}"}
    seconds = time.perf_counter() - started
    mapping = LogMapping(source.config)
    records = mapping.map(docs, unit, window.partition_key)
    _, stats = assemble(records, source.config.multiline)
    return {
        "cursor": source.config.pull.cursor,
        "docs": len(docs),
        "pages": pages,
        "seconds": round(seconds, 3),
        "docs_per_second": round(len(docs) / seconds, 1) if seconds > 0 else None,
        "mapping": mapping.stats.as_dict(),
        "multiline": stats.as_dict(),
        "levels_present": sum(1 for r in records if r.level is not None),
    }


def _request_stats(source: ElasticsearchRestSource) -> dict[str, Any]:
    by_row: dict[int, list[float]] = {}
    statuses: dict[str, int] = {}
    for record in source.transport.records:
        by_row.setdefault(record.row, []).append(record.ms)
        statuses[str(record.status)] = statuses.get(str(record.status), 0) + 1
    return {
        "sent": source.transport.sent,
        "statuses": statuses,
        "ms_by_row": {str(row): {"n": len(v), "p50": round(statistics.median(v), 1), "max": round(max(v), 1)} for row, v in sorted(by_row.items())},
    }
