"""Multiline assembly (addendum A1 §3.2.2).

A record whose message matches a **continuation** rule attaches to the preceding head record of the
same ``(container, file)`` within ``max_gap_ms``, up to ``max_lines``/``max_bytes``; continuation text
goes to the head's ``stack``. A continuation with no eligible head is a ``fragment``: counted and
reported separately (excluded from coverage targets) and sampled at ``fragment_sample_rate`` with a
deterministic hash so the unparsed queue gets the same sample on every run (N13).

In this slice the head test is "not a continuation", and the continuation rules are a closed set of
package-authored, linear patterns. The pattern library's ``format`` and ``continuation`` kinds (L2)
replace both through the ``is_head``/``is_continuation`` parameters.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

from ..core.config_sources import MultilineConfig
from .logs import LogRecord

BUILTIN_CONTINUATION: dict[str, re.Pattern[str]] = {
    "indented": re.compile(r"^[ \t]+\S"),
    "java_frame": re.compile(r"^[ \t]+at \S"),
    "python_frame": re.compile(r'^[ \t]+File "[^"\n]*", line [0-9]+'),
    "caused_by": re.compile(r"^Caused by: "),
    "traceback_header": re.compile(r"^Traceback \(most recent call last\):"),
    "ellipsis_more": re.compile(r"^[ \t]*\.\.\. [0-9]+ more\b"),
}


def continuation_predicate(rules: Iterable[str]) -> Callable[[str], bool]:
    compiled = [BUILTIN_CONTINUATION[name] for name in rules]
    return lambda text: any(p.match(text) for p in compiled)


@dataclass
class MultilineStats:
    heads: int = 0
    joined: int = 0
    fragments: int = 0
    overflow: int = 0  # continuation lines beyond max_lines/max_bytes (head marked truncated)
    fragment_samples: list[LogRecord] = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        return {"heads": self.heads, "joined": self.joined, "fragments": self.fragments, "overflow": self.overflow, "fragment_samples": len(self.fragment_samples)}


def _sampled(record_id: str, rate: float) -> bool:
    if rate <= 0:
        return False
    return int(hashlib.sha256(record_id.encode()).hexdigest()[:15], 16) / float(16**15) < rate


def assemble(
    records: Iterable[LogRecord],
    config: MultilineConfig,
    *,
    is_continuation: Callable[[str], bool] | None = None,
    is_head: Callable[[str], bool] | None = None,
) -> tuple[list[LogRecord], MultilineStats]:
    """Reassemble one unit's records. Output order: by ``(container, file, ts, offset, id)``."""
    stats = MultilineStats()
    ordered = sorted(records, key=lambda r: (r.container or "", r.file or "", r.ts, r.offset if r.offset is not None else -1, r.id))
    if not config.enabled:
        stats.heads = len(ordered)
        return ordered, stats
    cont = is_continuation or continuation_predicate(config.continuation)
    head_test = is_head or (lambda text: not cont(text))
    out: list[LogRecord] = []
    open_heads: dict[tuple[str, str], dict] = {}
    for record in ordered:
        key = (record.container or "", record.file or "")
        if head_test(record.message) or not cont(record.message):
            state = {"record": record, "stack": [record.stack] if record.stack else [], "lines": 1, "bytes": len(record.message.encode()), "last": record.ts, "truncated": record.truncated}
            open_heads[key] = state
            out.append(record)  # placeholder, replaced on flush
            state["slot"] = len(out) - 1
            stats.heads += 1
            continue
        state = open_heads.get(key)
        gap_ms = (record.ts - state["last"]).total_seconds() * 1000 if state else None
        if state is None or gap_ms is None or gap_ms < 0 or gap_ms > config.max_gap_ms:
            stats.fragments += 1
            if _sampled(record.id, config.fragment_sample_rate):
                stats.fragment_samples.append(record)
            continue
        size = len(record.message.encode()) + 1
        if state["lines"] + 1 > config.max_lines or state["bytes"] + size > config.max_bytes:
            stats.overflow += 1
            state["truncated"] = True
        else:
            state["stack"].append(record.message)
            state["lines"] += 1
            state["bytes"] += size
            stats.joined += 1
        state["last"] = record.ts
        _flush(out, state)
    return out, stats


def _flush(out: list[LogRecord], state: dict) -> None:
    head: LogRecord = state["record"]
    out[state["slot"]] = head.model_copy(update={"stack": "\n".join(state["stack"]) or None, "lines": state["lines"], "truncated": state["truncated"]})
