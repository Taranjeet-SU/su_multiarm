"""Multiline assembly (addendum A1 §3.2.2).

A record whose message matches a **continuation** rule attaches to the preceding head record of the
same ``(container, file)`` within ``max_gap_ms``, up to ``max_lines``/``max_bytes``; continuation text
goes to the head's ``stack``. A continuation with no eligible head is a ``fragment``: counted and
reported separately (excluded from coverage targets) and sampled at ``fragment_sample_rate`` with a
deterministic hash so the unparsed queue gets the same sample on every run (N13).

Continuation rules are a closed set of package-authored, linear patterns (``BUILTIN_CONTINUATION``),
plus the **approved ``continuation`` entries of the pattern library** when a compiled library is
passed (``library=``): a line matching any of them continues the open head. The head test is "not a
continuation".

Two built-in rules are **contextual** (they look at the open head's stack, never across streams):

- ``python_traceback``: a Python traceback whose lines arrive as separate column-0 documents. The
  chained-exception markers (``During handling of the above exception …``, ``The above exception
  was the direct cause …``) always continue; inside an open traceback (after ``Traceback (most
  recent call last):`` or a ``File "…", line N`` frame) the source line after a frame, the
  ``^~~~`` marker lines and the final ``SomeError: message`` line continue too. Outside a traceback
  those lines are ordinary heads, so a lone ``ValueError: …`` log line is never hidden as a fragment.
- ``closing_bracket``: a line that is only closing brackets (``}``, ``]``, ``)``, ``});``, ``},``)
  always continues (the tail of a multi-line JSON or object dump).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..core.config_sources import MultilineConfig
from .logs import LogRecord

BUILTIN_CONTINUATION: dict[str, re.Pattern[str]] = {
    "indented": re.compile(r"^[ \t]+\S"),
    "java_frame": re.compile(r"^[ \t]+at \S"),
    # indented (the usual rendering) or at column 0 (each traceback line shipped as its own document)
    "python_frame": re.compile(r'^[ \t]*File "[^"\n]*", line [0-9]+'),
    "caused_by": re.compile(r"^Caused by: "),
    "traceback_header": re.compile(r"^Traceback \(most recent call last\):"),
    "ellipsis_more": re.compile(r"^[ \t]*\.\.\. [0-9]+ more\b"),
    "closing_bracket": re.compile(r"^[ \t]*[}\])][}\])]*[;,]?[ \t]*$"),
    "python_traceback": re.compile(
        r"^(?:During handling of the above exception, another exception occurred:"
        r"|The above exception was the direct cause of the following exception:)[ \t]*$"
    ),
}
# Lines that continue only inside an open Python traceback (rule ``python_traceback``).
PY_CARET = re.compile(r"^[ \t]*[\^~]+[ \t]*$")
PY_EXCEPTION = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,200}(?::[ \t].*|:)?$")
_TB_START = ("traceback_header", "python_traceback")


def continuation_predicate(rules: Iterable[str], library: Any = None) -> Callable[[str], bool]:
    """The context-free part of the configured rules (plus approved library ``continuation``
    entries when ``library`` is a ``CompiledLibrary``): ``text -> bool``."""
    compiled = [BUILTIN_CONTINUATION[name] for name in rules]
    extra = getattr(library, "is_continuation", None)
    if extra is None:
        return lambda text: any(p.match(text) for p in compiled)
    return lambda text: any(p.match(text) for p in compiled) or extra(text)


def _traceback_state(rules: set[str], state: str | None, text: str) -> str | None:
    """The traceback state after ``text`` joined a head (``None`` outside a traceback)."""
    if "python_traceback" not in rules and "python_frame" not in rules and "traceback_header" not in rules:
        return None
    if BUILTIN_CONTINUATION["python_frame"].match(text):
        return "file"
    if any(BUILTIN_CONTINUATION[name].match(text) for name in _TB_START):
        return "header"
    if state == "file":
        return "source"  # the source line after a frame
    if state in {"header", "source"} and PY_CARET.match(text):
        return state
    if state is not None and PY_EXCEPTION.match(text):
        return None  # the final exception line ends the traceback
    return state


def _in_traceback(state: str | None, text: str) -> bool:
    """A contextual continuation of an open traceback (rule ``python_traceback``)."""
    if state is None or not text.strip():
        return False
    if state == "file":
        return True  # the frame's source line (or the exception line of a frame without source)
    return bool(PY_CARET.match(text) or PY_EXCEPTION.match(text))


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
    library: Any = None,
) -> tuple[list[LogRecord], MultilineStats]:
    """Reassemble one unit's records. Output order: by ``(container, file, ts, offset, id)``.
    ``library`` (a ``CompiledLibrary``) adds its approved ``continuation`` entries to the rules."""
    stats = MultilineStats()
    ordered = sorted(records, key=lambda r: (r.container or "", r.file or "", r.ts, r.offset if r.offset is not None else -1, r.id))
    if not config.enabled:
        stats.heads = len(ordered)
        return ordered, stats
    rules = set(config.continuation)
    cont = is_continuation or continuation_predicate(config.continuation, library)
    head_test = is_head or (lambda text: not cont(text))
    contextual = "python_traceback" in rules
    out: list[LogRecord] = []
    open_heads: dict[tuple[str, str], dict] = {}
    for record in ordered:
        key = (record.container or "", record.file or "")
        state = open_heads.get(key)
        gap_ms = (record.ts - state["last"]).total_seconds() * 1000 if state else None
        in_gap = state is not None and gap_ms is not None and 0 <= gap_ms <= config.max_gap_ms
        continues = cont(record.message) and not (is_head is not None and head_test(record.message))
        if not continues and contextual and in_gap and _in_traceback(state["tb"], record.message):
            continues = True
        if not continues:
            state = {"record": record, "stack": [record.stack] if record.stack else [], "lines": 1, "bytes": len(record.message.encode()), "last": record.ts, "truncated": record.truncated, "tb": None}
            state["tb"] = _traceback_state(rules, None, record.message)
            open_heads[key] = state
            out.append(record)  # placeholder, replaced on flush
            state["slot"] = len(out) - 1
            stats.heads += 1
            continue
        if not in_gap:
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
        state["tb"] = _traceback_state(rules, state["tb"], record.message)
        state["last"] = record.ts
        _flush(out, state)
    return out, stats


def _flush(out: list[LogRecord], state: dict) -> None:
    head: LogRecord = state["record"]
    out[state["slot"]] = head.model_copy(update={"stack": "\n".join(state["stack"]) or None, "lines": state["lines"], "truncated": state["truncated"]})
