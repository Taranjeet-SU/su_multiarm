"""Nested trace spans with a masked JSONL export (plan §3.4).

The default export (``masking="privacy"``) carries field names, counts, IDs and hashes, never raw
query text or passages. Sensitive values are masked by key name (``SENSITIVE_KEYS`` covers the
query, passages, prompts, answers, ``UnsupportedConstraint.constraint_text``/``suggestion`` and
review follow-up queries, highlights, titles, snippets) or by wrapping a value in ``Sensitive(...)``;
components must wrap free text they introduce under other keys. ``masking="raw"`` is an explicit
operator opt-in.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Mapping

Masking = Literal["privacy", "raw"]
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "query",
        "query_text",
        "rewritten_query",
        "followup_query",
        "text",
        "passage",
        "passages",
        "evidence_text",
        "prompt",
        "system",
        "messages",
        "answer",
        "record_context",
        "constraint_text",
        "suggestion",
        "raw_reply",
        "quote",
        "value_text",
        "highlights",
        "title",
        "snippet",
        "content",
        "question",
    }
)
# Arms and generators must wrap any other free text in ``Sensitive(...)``; key names are a safety
# net, not the contract.


@dataclass(frozen=True)
class Sensitive:
    """Explicit marker for a value that must be masked in a privacy export regardless of key."""

    value: Any


def mask_value(value: Any) -> dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return {"sha256": hashlib.sha256(text.encode()).hexdigest(), "chars": len(text), "masked": True}


def mask(data: Any, masking: Masking, sensitive_keys: frozenset[str] = SENSITIVE_KEYS) -> Any:
    if isinstance(data, Sensitive):
        return mask_value(data.value) if masking == "privacy" else mask(data.value, masking, sensitive_keys)
    if isinstance(data, Mapping):
        result = {}
        for key, value in data.items():
            if masking == "privacy" and key in sensitive_keys and value is not None:
                result[key] = mask_value(value.value if isinstance(value, Sensitive) else value)
            else:
                result[key] = mask(value, masking, sensitive_keys)
        return result
    if isinstance(data, (list, tuple)):
        return [mask(v, masking, sensitive_keys) for v in data]
    if hasattr(data, "model_dump"):
        return mask(data.model_dump(mode="json"), masking, sensitive_keys)
    return data


@dataclass
class Span:
    id: str
    name: str
    parent_id: str | None
    started: float
    ended: float | None = None
    status: Literal["ok", "error"] = "ok"
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    def set(self, **attributes: Any) -> None:
        self.attributes.update(attributes)

    def event(self, name: str, **attributes: Any) -> None:
        self.events.append({"name": name, "at": time.time(), **attributes})


class Trace:
    def __init__(self, trace_id: str | None = None, clock: Callable[[], float] = time.time):
        self.id = trace_id or uuid.uuid4().hex
        self.clock = clock
        self.spans: list[Span] = []
        self.root = Span(id=uuid.uuid4().hex, name="request", parent_id=None, started=clock())
        self.spans.append(self.root)
        self._stack = threading.local()
        self._lock = threading.Lock()

    def _current(self) -> Span:
        stack = getattr(self._stack, "items", None)
        return stack[-1] if stack else self.root

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        parent = self._current()
        span = Span(id=uuid.uuid4().hex, name=name, parent_id=parent.id, started=self.clock(), attributes=dict(attributes))
        with self._lock:
            self.spans.append(span)
        stack = getattr(self._stack, "items", None)
        if stack is None:
            stack = self._stack.items = []
        stack.append(span)
        try:
            yield span
        except BaseException as exc:
            span.status = "error"
            span.event("error", type=type(exc).__name__, message=str(exc))
            raise
        finally:
            span.ended = self.clock()
            stack.pop()

    def set(self, **attributes: Any) -> None:
        self._current().set(**attributes)

    def event(self, name: str, **attributes: Any) -> None:
        self._current().event(name, **attributes)

    def finish(self) -> None:
        if self.root.ended is None:
            self.root.ended = self.clock()

    def records(self, masking: Masking = "privacy") -> list[dict[str, Any]]:
        self.finish()
        with self._lock:
            spans = list(self.spans)
        return [
            {
                "trace_id": self.id,
                "span_id": s.id,
                "parent_id": s.parent_id,
                "name": s.name,
                "started": s.started,
                "ended": s.ended,
                "duration_ms": None if s.ended is None else round((s.ended - s.started) * 1000, 3),
                "status": s.status,
                "attributes": mask(s.attributes, masking),
                "events": mask(s.events, masking),
            }
            for s in spans
        ]

    def export(self, path: str | Path, masking: Masking = "privacy") -> int:
        """Write one JSON object per span. Returns the number of spans written."""
        records = self.records(masking)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return len(records)

    def to_dict(self, masking: Masking = "privacy") -> dict[str, Any]:
        return {"trace_id": self.id, "spans": self.records(masking)}
