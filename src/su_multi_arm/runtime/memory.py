"""Working and episodic memory (plan §3.4). **Memory is never evidence.**

``working`` lives for one request. ``episodic`` is the session's turn list: query hash, shown IDs,
answer id, trace id and the generation each turn ran on. Memory may rewrite a follow-up query or
add exclusions; both are returned as explicit, logged effects. ``semantic`` memory is deferred and
its config key is rejected until a later phase defines storage, writer and retention.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.types import Turn


def query_sha(query: str) -> str:
    return hashlib.sha256(query.encode()).hexdigest()


@dataclass
class WorkingMemory:
    values: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value


@dataclass
class EpisodicMemory:
    turns: list[Turn] = field(default_factory=list)
    store_text: bool = False

    def record(
        self,
        query: str,
        shown_ids: list[str],
        generation: str,
        trace_id: str,
        answer_id: str | None = None,
    ) -> Turn:
        turn = Turn(
            query_sha=query_sha(query),
            shown_ids=list(dict.fromkeys(shown_ids)),
            answer_id=answer_id,
            trace_id=trace_id,
            generation=generation,
            query_text=query if self.store_text else None,
        )
        self.turns.append(turn)
        return turn

    def shown_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for turn in self.turns:
            for doc_id in turn.shown_ids:
                seen.setdefault(doc_id, None)
        return list(seen)

    def revalidate(self, generation: str, exists: Callable[[list[str]], set[str]]) -> dict[str, Any]:
        """After a generation switch, drop IDs that no longer exist. Returns what changed (for the trace)."""
        ids = self.shown_ids()
        if not ids:
            return {"generation": generation, "dropped": []}
        present = exists(ids)
        dropped = [i for i in ids if i not in present]
        if dropped:
            self.turns = [
                t.model_copy(update={"shown_ids": [i for i in t.shown_ids if i in present]}) for t in self.turns
            ]
        return {"generation": generation, "dropped": dropped}


@dataclass
class Memory:
    working: WorkingMemory = field(default_factory=WorkingMemory)
    episodic: EpisodicMemory = field(default_factory=EpisodicMemory)

    def exclusions(self) -> list[str]:
        """IDs already shown in this session; a caller may add them to ``exclude_ids`` (logged)."""
        return self.episodic.shown_ids()
