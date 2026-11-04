"""Bootstrap coverage without an LLM (L4a): classify lines with the current library, mine the
``unparsed``/``format_only`` bodies, synthesise one proposal per cluster with enough support, and
measure coverage before and after (proposals evaluated as if approved, below every approved entry).

Nothing is written: ``BootstrapReport.proposals`` go through the gate and the promotion policy
(``gate.py``, ``promotion.py``) like any other proposal.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ...extraction.patterns.coverage import CoverageReport, coverage
from ...extraction.patterns.extractor import CompiledLibrary
from ...extraction.patterns.library import LoadedEntry, PatternEntry, expand
from .mining import MinerConfig, TemplateMiner
from .promotion import assigned_priority
from .synthesis import synthesise


@dataclass(frozen=True)
class LogLine:
    id: str
    text: str
    attributes: dict[str, str] = field(default_factory=dict)  # service, level, tenant, env, ...
    field_name: str = "message"


@dataclass
class BootstrapReport:
    before: CoverageReport
    after: CoverageReport
    proposals: list[PatternEntry]
    rejected: list[tuple[str, str]]  # (cluster id, reason)
    miner: dict[str, int]
    clusters: int
    support: dict[str, list[str]] = field(default_factory=dict)  # proposal id -> matched line ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "proposals": [p.id for p in self.proposals],
            "rejected": self.rejected,
            "miner": self.miner,
            "clusters": self.clusters,
        }


def with_proposals(compiled: CompiledLibrary, proposals: list[PatternEntry]) -> CompiledLibrary:
    """A copy of the library with ``proposals`` approved below every approved entry of their kind."""
    library = copy.copy(compiled.library)
    library.entries = dict(library.entries)
    for entry in proposals:
        promoted = entry.model_copy(update={"status": "approved", "priority": assigned_priority(compiled.library, entry.kind)})
        regex, _ = expand(entry.regex, library.vocabularies, library.masks)  # as the gate compiled it
        library.entries[entry.id] = LoadedEntry(promoted, "proposed", "bootstrap", regex, "approved")
    return CompiledLibrary(library, compiled.engine, compiled.max_line_bytes)


def bootstrap(
    lines: Iterable[LogLine],
    compiled: CompiledLibrary,
    *,
    miner: MinerConfig | None = None,
    min_support: int = 3,
    min_literal_chars: int = 4,
    created: datetime | None = None,
) -> BootstrapReport:
    lines = list(lines)
    results = [compiled.match_line(line.text, field_name=line.field_name, attributes=line.attributes) for line in lines]
    before = coverage(results)
    mining = TemplateMiner(compiled.engine, compiled.library.masks, miner)
    for line, result in zip(lines, results):
        if result.status not in {"unparsed", "format_only"}:
            continue
        body_start, body_end = 0, len(result.text)
        if result.format is not None:
            then = result.format.entry.entry.then
            body = result.format.groups.get(then.field_from_group) if then else None
            if body is None:
                continue
            body_start, body_end = body
        attrs = result.attributes
        mining.add(line.id, result.text[body_start:body_end], attrs.get("service"), attrs.get("level"))
    proposals: list[PatternEntry] = []
    rejected: list[tuple[str, str]] = []
    clusters = mining.result()
    for cluster in clusters:
        if cluster.size < min_support:
            rejected.append((cluster.id, f"support {cluster.size} < {min_support}"))
            continue
        made = synthesise(cluster, compiled.library.masks, compiled.engine, min_literal_chars=min_literal_chars, created=created)
        if made.entry is None:
            rejected.append((cluster.id, made.reason or "rejected"))
        elif any(p.id == made.entry.id for p in proposals):
            rejected.append((cluster.id, f"duplicate of {made.entry.id}"))
        else:
            proposals.append(made.entry)
    extended = with_proposals(compiled, proposals)
    after_results = [extended.match_line(line.text, field_name=line.field_name, attributes=line.attributes) for line in lines]
    support: dict[str, list[str]] = {}
    for line, result in zip(lines, after_results):
        if result.message is not None and result.message.entry.entry.id in {p.id for p in proposals}:
            support.setdefault(result.message.entry.entry.id, []).append(line.id)
    return BootstrapReport(before, coverage(after_results), proposals, rejected, mining.stats(), len(clusters), support)
