"""Lexical arm: term query (OR of distinct tokens) with an optional phrase boost under a DF guard."""

from __future__ import annotations

import re
from typing import ClassVar

from ..core.config import LexicalArm as LexicalArmConfig
from ..core.types import LexicalQuery
from ..runtime.context import RunContext
from .base import ArmInput, ArmResult, BaseArm

MAX_TERMS = 128


def query_terms(text: str) -> list[str]:
    """Distinct query words in their original spelling (QA-07).

    The backend's own analyzer folds the query exactly as it folded the indexed text (FTS5
    ``unicode61``, PostgreSQL ``to_tsvector``, the Lucene ``standard`` analyzer; the memory index
    casefolds both sides). Folding here with ``str.casefold`` went further than ``unicode61`` and
    the Lucene analyzers (``ß``->``ss``, ``ﬁ``->``fi``), so the query could never match its own
    indexed word. Repeats are dropped by ``str.lower`` (which, like the analyzers, keeps ``ß``)."""
    seen: dict[str, str] = {}
    for word in re.findall(r"\w+", text):
        seen.setdefault(word.lower(), word)
    return list(seen.values())


class LexicalArm(BaseArm):
    name: ClassVar[str] = "lexical"
    config_model: ClassVar[type] = LexicalArmConfig
    capability: ClassVar[str] = "lexical"

    def _run(self, ctx: RunContext, inputs: ArmInput) -> ArmResult:
        terms = query_terms(inputs.query)[:MAX_TERMS]
        if not terms:
            return ArmResult(arm=self.name)
        facet = self._facet()
        page = facet.search(ctx.generation, LexicalQuery(kind="terms", text=" ".join(terms)), inputs.scope, inputs.candidates)
        scored = {h.chunk_id: (h.score, {}) for h in page.items}
        boost = self.cfg.phrase_boost
        if boost.enabled and 2 <= len(terms) <= boost.window_tokens and scored:
            phrase = facet.search(ctx.generation, LexicalQuery(kind="phrase", text=" ".join(terms)), inputs.scope, inputs.candidates)
            # DF guard: a phrase that matches most of the term hits carries no extra signal.
            if phrase.items and len(phrase.items) * 2 <= len(page.items):
                top = max(s for s, _ in scored.values())
                for hit in phrase.items:
                    base = scored.get(hit.chunk_id, (0.0, {}))[0]
                    scored[hit.chunk_id] = (base + top, {"phrase_match": True})
        return ArmResult(arm=self.name, hits=self.collapse(ctx, [(cid, s, extra) for cid, (s, extra) in scored.items()]))
