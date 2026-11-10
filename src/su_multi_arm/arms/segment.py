"""Segment arm: child-record segments (``segments/``) ranked, mapped to their parent records.

Order of work: the query vector is compared with every segment of the configured kinds (exact
cosine), optionally fused with a BM25 list over segment text by reciprocal rank (``rrf_k``); the
fused segment ranking is walked in order and parents are admitted in batches through
``Catalog.iter_scope`` with the request's eligibility scope (filters, exclusions and ACL
predicates are evaluated on the parent; a segment never widens it). The walk stops when
``candidates`` parents are admitted or the ranking is exhausted, so a restrictive scope is exact,
not a post-filter of a truncated list.

One hit per parent (dedupe in the arm): the best segment's score and text, up to
``max_segments_per_parent`` segments as provenance (``segment_id``, ``segment_kind``,
``parent_id``, ``field``/``start``/``end`` and the anchor status). An anchored segment is re-checked
against the parent's field in the serving generation; a segment whose text is no longer at its
offsets is dropped and counted in the arm report (``stale_anchors``), never cited.
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np

from ..core.component import Diagnostic, Environment
from ..core.config import SegmentArm as SegmentArmConfig
from ..core.types import Page, Scope
from ..models import Hit
from ..runtime.context import RunContext
from ..segments import SegmentIndex, embedder_recipe
from .base import ArmInput, ArmResult, BaseArm

PARENT_BATCH = 200


class SegmentArm(BaseArm):
    name: ClassVar[str] = "segment"
    config_model: ClassVar[type] = SegmentArmConfig
    capability: ClassVar[str] = "catalog"

    def __init__(self, config: SegmentArmConfig, deps):
        super().__init__(config, deps)
        self.embedder = deps.providers.get("embedder")
        self._index: SegmentIndex | None = None

    def validate(self, env: Environment, probe: bool = False) -> list[Diagnostic]:
        diagnostics = []
        if self.catalog is None:
            diagnostics.append(Diagnostic(severity="error", code="catalog_missing", message="segment: catalog facet not provided", component=self.name))
        if self.embedder is None:
            diagnostics.append(Diagnostic(severity="error", code="embedder_missing", message="segment arm needs models.roles.embedder", component=self.name))
        if not (self.cfg.path / "manifest.json").is_file():
            diagnostics.append(Diagnostic(severity="error", code="segment_index_missing", message=f"segment: no sealed index at {self.cfg.path}", component=self.name))
        return diagnostics

    def index(self, dataset: str) -> SegmentIndex:
        if self._index is None:
            index = SegmentIndex(self.cfg.path)
            index.check(dataset, embedder_recipe(self.embedder))
            self._index = index
        return self._index

    def _admit(self, ctx: RunContext, scope: Scope, parent_ids: list[str]) -> dict[str, Any]:
        """Parents of ``parent_ids`` the scope admits, as catalog ``Document``s."""
        allowed = [p for p in parent_ids if p not in set(scope.exclude_ids)]
        if scope.document_ids is not None:
            allowed = [p for p in allowed if p in set(scope.document_ids)]
        if not allowed:
            return {}
        narrowed = Scope(predicates=scope.predicates, document_ids=sorted(set(allowed)), exclude_ids=list(scope.exclude_ids))
        found: dict[str, Any] = {}
        cursor = None
        while True:
            page = self.catalog.iter_scope(ctx.generation, narrowed, Page(cursor=cursor, size=500))
            found.update((d.id, d) for d in page.items)
            if page.next_cursor is None:
                return found
            cursor = page.next_cursor

    def _ranking(self, index: SegmentIndex, query: str) -> tuple[list[tuple[int, float, list[str]]], dict[str, int]]:
        mask = index.mask(self.cfg.kinds)
        vector = self.embedder.encode([query], query=True)[0]
        dense = index.dense(vector, mask) if np.any(vector) else []
        lexical = index.lexical(query, self.cfg.lexical_candidates, mask) if self.cfg.lexical else []
        counts = {"dense_ranked": len(dense), "lexical_matched": len(lexical)}
        if not lexical:
            return [(row, score, ["dense"]) for row, score in dense], counts
        fused: dict[int, list[Any]] = {}
        for arm, ranked in (("dense", dense), ("lexical", lexical)):
            for rank, (row, _score) in enumerate(ranked, 1):
                entry = fused.setdefault(row, [0.0, []])
                entry[0] += 1.0 / (self.cfg.rrf_k + rank)
                entry[1].append(arm)
        ordered = sorted(fused.items(), key=lambda item: (-item[1][0], item[0]))
        return [(row, entry[0], entry[1]) for row, entry in ordered], counts

    def _run(self, ctx: RunContext, inputs: ArmInput) -> ArmResult:
        # partitioned collection (M1b): the sidecar is an attached layer pinned by digest; an
        # unavailable layer fails this arm (labelled), never serves changed files
        layer_check = getattr(self.catalog, "layer_check", None)
        if callable(layer_check):
            layer_check(ctx.generation, self.name)
        index = self.index(ctx.generation.dataset)  # SegmentIndexError: missing/tampered/incompatible
        ranking, counts = self._ranking(index, inputs.query)
        admitted: dict[str, Any] = {}
        refused: set[str] = set()
        best: dict[str, list[tuple[int, float, list[str]]]] = {}
        position = 0
        while position < len(ranking) and len(admitted) < inputs.candidates:
            pending: list[str] = []
            scan = position
            while scan < len(ranking) and len(pending) < PARENT_BATCH:
                parent = index.parent_of[ranking[scan][0]]
                if parent not in admitted and parent not in refused and parent not in pending:
                    pending.append(parent)
                scan += 1
            found = self._admit(ctx, inputs.scope, pending)
            admitted.update(found)
            refused.update(p for p in pending if p not in found)
            for row, score, via in ranking[position:scan]:
                parent = index.parent_of[row]
                if parent in admitted:
                    best.setdefault(parent, []).append((row, score, via))
            position = scan
        # the first ``candidates`` admitted parents in ranking order
        order = sorted(best, key=lambda p: (-best[p][0][1], p))[: inputs.candidates]
        rows = index.rows([row for p in order for row, _s, _v in best[p][: self.cfg.max_segments_per_parent * 2]])
        hits: list[Hit] = []
        stale = 0
        for parent_id in order:
            document = admitted[parent_id]
            provenance = []
            for row, score, via in best[parent_id]:
                if len(provenance) >= self.cfg.max_segments_per_parent:
                    break
                seg = rows.get(row)
                if seg is None:
                    continue
                status = "unanchored"
                if seg["field"] is not None:
                    source = document.fields.get(seg["field"])
                    if not isinstance(source, str) or source[seg["start"] : seg["end"]] != seg["text"]:
                        stale += 1
                        continue
                    status = "verified"
                provenance.append({**seg, "anchor": status, "score": round(score, 6), "via": via, "membership": "child_segment_of_parent"})
            if not provenance:
                continue
            hits.append(
                Hit(
                    document_id=parent_id,
                    title=document.title or parent_id,
                    source=document.source,
                    url=document.url,
                    text=provenance[0]["text"],
                    chunk_ids=[],
                    score=float(provenance[0]["score"]),
                    arms=[self.name],
                    provenance=provenance,
                )
            )
        report = {
            "kind": "segment",
            "segments_ranked": len(ranking),
            **counts,
            "parents_admitted": len(admitted),
            "parents_refused_by_scope": len(refused),
            "stale_anchors": stale,
            "index_generation": index.manifest.get("generation"),
        }
        hits.sort(key=lambda h: (-h.score, h.document_id))
        return ArmResult(arm=self.name, hits=hits, reports=[report])
