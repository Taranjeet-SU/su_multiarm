"""Lexical arm: term query (OR of distinct tokens) with an optional phrase boost under a DF guard."""

from __future__ import annotations

from typing import ClassVar

from ..core.config import LexicalArm as LexicalArmConfig
from ..core.types import LexicalQuery
from ..providers import tokens
from ..runtime.context import RunContext
from .base import ArmInput, ArmResult, BaseArm

MAX_TERMS = 128


class LexicalArm(BaseArm):
    name: ClassVar[str] = "lexical"
    config_model: ClassVar[type] = LexicalArmConfig
    capability: ClassVar[str] = "lexical"

    def _run(self, ctx: RunContext, inputs: ArmInput) -> ArmResult:
        terms = list(dict.fromkeys(tokens(inputs.query)))[:MAX_TERMS]
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
