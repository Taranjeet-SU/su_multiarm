"""Lexical arm: term query with optional policies, all off by default.

* ``phrase_boost``: the whole term list as one phrase, added on top under a DF guard.
* ``pin_identifiers``: documents named by an identifier (``id`` field pattern) in the query lead the
  list, resolved by exact field equality inside the request scope.
* ``verbatim_boost``: documents holding a quoted string of the query, or several verbatim
  ``window_tokens`` windows of it, lead the list (after identifier pins). Needs an index that exposes
  its analyzer and term statistics (sqlite ``lexical_engine: bm25``).

Front documents keep their order through the arm's score sort: they are given scores above the best
term score, and their provenance says why (``lexical_front``).
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from ..core.component import Diagnostic, Environment
from ..core.config import LexicalArm as LexicalArmConfig
from ..core.types import LexicalQuery, Page, Predicate, Scope
from ..models import Hit
from ..runtime.context import RunContext
from .base import ArmInput, ArmResult, BaseArm

MAX_TERMS = 128
EXCERPT_CHARS = 500
_QUOTED = re.compile(r"[\"“‘']([^\"”’\n]{8,300}?)[\"”’'](?!\w)")


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


def query_text(text: str) -> str:
    """The words of the first ``MAX_TERMS`` distinct terms in query order, repeats kept, for an index
    that scores repeated query tokens (``scores_query_repeats``: Okapi BM25)."""
    keep = {t.lower() for t in query_terms(text)[:MAX_TERMS]}
    return " ".join(w for w in re.findall(r"\w+", text) if w.lower() in keep)


class LexicalArm(BaseArm):
    name: ClassVar[str] = "lexical"
    config_model: ClassVar[type] = LexicalArmConfig
    capability: ClassVar[str] = "lexical"

    def __init__(self, config, deps):
        super().__init__(config, deps)
        fields = [f for f in (self.schema.fields if self.schema is not None else []) if f.type == "id" and f.pattern]
        wanted = set(config.pin_identifiers.fields)
        self.id_fields: list[tuple[str, re.Pattern[str]]] = [(f.name, re.compile(f.pattern)) for f in fields if not wanted or f.name in wanted]
        self._unknown_id_fields = sorted(wanted - {f.name for f in fields})

    def validate(self, env: Environment, probe: bool = False) -> list[Diagnostic]:
        diagnostics = super().validate(env, probe)
        if self.cfg.pin_identifiers.enabled:
            if self._unknown_id_fields:
                diagnostics.append(Diagnostic(
                    severity="error", code="pin_identifiers_field",
                    message=f"lexical.pin_identifiers.fields {self._unknown_id_fields} are not id fields with a pattern in metadata_schema",
                    component=self.name,
                ))
            elif not self.id_fields:
                diagnostics.append(Diagnostic(severity="error", code="pin_identifiers_field", message="lexical.pin_identifiers needs an id field with a pattern in metadata_schema", component=self.name))
        if self.cfg.verbatim_boost.enabled and self.backend is not None:
            facet = self._facet()
            if not (callable(getattr(facet, "analyze", None)) and callable(getattr(facet, "doc_freqs", None)) and callable(getattr(facet, "phrase_rows", None))):
                diagnostics.append(Diagnostic(
                    severity="error", code="verbatim_boost_unsupported",
                    message=f"lexical.verbatim_boost needs a lexical index with analyzer and term statistics (sqlite lexical_engine: bm25); backend {getattr(self.backend, 'kind', '?')} has none",
                    component=self.name,
                ))
        return diagnostics

    def _run(self, ctx: RunContext, inputs: ArmInput) -> ArmResult:
        terms = query_terms(inputs.query)[:MAX_TERMS]
        if not terms:
            return ArmResult(arm=self.name)
        facet = self._facet()
        # Repeated query words reach only an index that scores them (Okapi BM25); every other index
        # receives the distinct terms exactly as before.
        text = query_text(inputs.query) if getattr(facet, "scores_query_repeats", False) else " ".join(terms)
        page = facet.search(ctx.generation, LexicalQuery(kind="terms", text=text), inputs.scope, inputs.candidates)
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
        hits = self.collapse(ctx, [(cid, s, extra) for cid, (s, extra) in scored.items()])
        front: list[Hit] = []
        if self.cfg.pin_identifiers.enabled:
            front.extend(self._identifier_front(ctx, inputs, hits))
        if self.cfg.verbatim_boost.enabled:
            taken = {h.document_id for h in front}
            front.extend(h for h in self._verbatim_front(ctx, inputs) if h.document_id not in taken)
        if front:
            hits = self._lead(front, hits)
        return ArmResult(arm=self.name, hits=hits)

    # --- front policies -------------------------------------------------------------------------

    @staticmethod
    def _lead(front: list[Hit], hits: list[Hit]) -> list[Hit]:
        """``front`` first (in order), then the other hits; scores stay strictly decreasing."""
        taken = {h.document_id for h in front}
        rest = [h for h in hits if h.document_id not in taken]
        top = max([0.0, *(h.score for h in rest)])
        for rank, hit in enumerate(front):
            hit.score = top + float(len(front) - rank)
        return [*front, *rest]

    def _identifier_front(self, ctx: RunContext, inputs: ArmInput, hits: list[Hit]) -> list[Hit]:
        policy = self.cfg.pin_identifiers
        found: list[tuple[int, str, str]] = []
        for field_name, pattern in self.id_fields:
            found.extend((m.start(), field_name, m.group(0)) for m in pattern.finditer(inputs.query) if m.group(0))
        by_id = {h.document_id: h for h in hits}
        front: list[Hit] = []
        for _, field_name, token in sorted(found):
            if len(front) >= policy.max_records:
                break
            scope = Scope(
                predicates=[*inputs.scope.predicates, Predicate(field=field_name, op="eq", value=token)],
                document_ids=inputs.scope.document_ids,
                exclude_ids=list(inputs.scope.exclude_ids),
            )
            page = self.catalog.iter_scope(ctx.generation, scope, Page(size=policy.max_records))
            for doc in page.items:
                if len(front) >= policy.max_records or any(h.document_id == doc.id for h in front):
                    continue
                mark = {"lexical_front": "identifier", "identifier": token, "field": field_name}
                hit = by_id.get(doc.id)
                if hit is not None:
                    hit = hit.model_copy(update={"provenance": [{**hit.provenance[0], **mark}, *hit.provenance[1:]] if hit.provenance else [mark]})
                else:
                    text = next((v for v in doc.fields.values() if v), "") or doc.title or doc.id
                    hit = Hit(document_id=doc.id, title=doc.title or doc.id, source=doc.source, url=doc.url, text=text[:EXCERPT_CHARS], chunk_ids=[], score=0.0, arms=[self.name], provenance=[mark])
                front.append(hit)
        ctx.trace.event("lexical.pin_identifiers", identifiers=len(found), pinned=len(front))
        return front

    def _windows(self, ctx: RunContext, facet: Any, query: str) -> list[tuple[tuple[str, ...], str]]:
        policy = self.cfg.verbatim_boost
        analyze = facet.analyze
        windows: list[tuple[tuple[str, ...], str]] = []
        if policy.quoted:
            for match in _QUOTED.finditer(query):
                words = tuple(analyze(match.group(1)))
                if len(words) >= 2 and (words, "quoted") not in windows:
                    windows.append((words, "quoted"))
        words = analyze(query)
        size = policy.window_tokens
        if len(words) >= size:
            candidates = list(dict.fromkeys(tuple(words[i : i + size]) for i in range(len(words) - size + 1)))
            distinct = sorted({w for c in candidates for w in c})
            frequency = dict(zip(distinct, facet.doc_freqs(ctx.generation, distinct)))
            windows.extend((w, "verbatim") for w in candidates if min(frequency[t] for t in w) <= policy.common_df)
        return windows

    def _verbatim_front(self, ctx: RunContext, inputs: ArmInput) -> list[Hit]:
        policy = self.cfg.verbatim_boost
        facet = self._facet()
        windows = self._windows(ctx, facet, inputs.query)
        per_chunk: dict[str, dict[str, Any]] = {}
        skipped = 0
        for index, (words, kind) in enumerate(windows):
            rows = facet.phrase_rows(ctx.generation, list(words), inputs.scope, policy.max_candidates)
            if rows is None or (kind == "verbatim" and len(rows) > policy.max_window_hits):
                skipped += 1
                continue
            for row in rows:
                entry = per_chunk.setdefault(row.chunk_id, {"document_id": row.document_id, "windows": set(), "quoted": False})
                entry["windows"].add(index)
                entry["quoted"] |= kind == "quoted"
        best: dict[str, tuple[tuple[int, int], str]] = {}
        for chunk_id in sorted(per_chunk):
            entry = per_chunk[chunk_id]
            if not (entry["quoted"] or len(entry["windows"]) >= policy.min_windows):
                continue
            key = (int(entry["quoted"]), len(entry["windows"]))
            prior = best.get(entry["document_id"])
            if prior is None or key > prior[0]:
                best[entry["document_id"]] = (key, chunk_id)
        ranked = sorted(best.values(), key=lambda item: (-item[0][0], -item[0][1], item[1]))[: policy.max_records]
        ctx.trace.event("lexical.verbatim_boost", windows=len(windows), skipped=skipped, records=len(ranked))
        scored = [
            (chunk_id, float(len(ranked) - rank), {"lexical_front": "quoted" if key[0] else "verbatim", "windows": key[1]})
            for rank, (key, chunk_id) in enumerate(ranked)
        ]
        return self.collapse(ctx, scored)
