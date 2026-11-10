"""Session follow-up answering (the earlier service's session cache), on package primitives.

The store keeps, per package session id (already bound to dataset, caller principal and session
value), the **anchor**: the most recent full-search turn's question, the ids of its packed evidence
in packing order, its generation and a hash of its record context. No passages and no answer text
are kept: a follow-up re-reads the records by id (``Engine.answer_from_evidence``), so a cached
answer is never evidence. Entries expire after ``ttl_seconds`` and the oldest are evicted beyond
``max_sessions`` (in memory: a restart empties it, and the next turn runs a full search).

``gate`` decides deterministically (no model call); every outcome is a ``session_cache.decision``:

* ``deep_no_session_evidence`` / ``deep_generation_changed`` / ``deep_record_context_changed`` /
  ``deep_new_record_id``: a precondition failed, full search;
* ``new_search``: no follow-up cue matched (unclear turns are never answered from the session);
* ``cache_hit``: answered from the anchor's evidence;
* ``cache_declined``: the generator found the anchor's evidence insufficient, full search.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from .profile import SessionCache

FOLLOWUP_PREFIX = "Earlier question in this conversation (context only, not evidence): "


@dataclass
class Anchor:
    question: str
    evidence_ids: list[str]
    generation: str | None
    context_hash: str | None
    turn: int
    at: float = field(default_factory=time.time)


def context_hash(context: dict[str, str]) -> str | None:
    if not context:
        return None
    return hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()[:32]


class AnchorStore:
    def __init__(self, config: SessionCache, clock: Any = time.time):
        self.config, self.clock = config, clock
        self._items: OrderedDict[str, Anchor] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, sid: str) -> Anchor | None:
        with self._lock:
            anchor = self._items.get(sid)
            if anchor is None:
                return None
            if self.clock() - anchor.at > self.config.ttl_seconds:
                del self._items[sid]
                return None
            return anchor

    def put(self, sid: str, anchor: Anchor) -> None:
        with self._lock:
            anchor.at = self.clock()
            self._items[sid] = anchor
            self._items.move_to_end(sid)
            while len(self._items) > self.config.max_sessions:
                self._items.popitem(last=False)

    def drop(self, sid: str) -> None:
        """Forget the anchor of a session that was replaced (its package session expired)."""
        with self._lock:
            self._items.pop(sid, None)

    def __len__(self) -> int:
        return len(self._items)


def _ids(patterns: list[str], text: str) -> set[str]:
    return {m.group(0).casefold() for p in patterns for m in re.finditer(p, text or "")}


def gate(config: SessionCache, question: str, anchor: Anchor | None, generation: str | None, ctx_hash: str | None) -> tuple[bool, str]:
    """``(answer from the session?, reason)``; deterministic, fail-closed (unclear -> full search)."""
    if anchor is None or not anchor.evidence_ids:
        return False, "deep_no_session_evidence"
    if generation is None or anchor.generation != generation:
        return False, "deep_generation_changed"
    if ctx_hash != anchor.context_hash:
        return False, "deep_record_context_changed"
    named = _ids(config.record_id_patterns, question)
    known = {i.casefold() for i in anchor.evidence_ids} | _ids(config.record_id_patterns, anchor.question) | _ids(config.record_id_patterns, " ".join(anchor.evidence_ids))
    if named - known:
        return False, "deep_new_record_id"
    if not any(re.search(cue, question) for cue in config.followup_cues):
        return False, "new_search"
    return True, "followup"


def followup_query(question: str, anchor: Anchor) -> str:
    """The follow-up as asked plus the anchor question, labelled as context (user text, not evidence)."""
    return f"{question}\n\n{FOLLOWUP_PREFIX}{anchor.question}"[:16000]
