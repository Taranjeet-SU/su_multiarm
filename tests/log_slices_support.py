"""Shared helpers of the live log path tests (unified merge M2a): a member recipe, a ledger factory,
a fake id directory implementing the S0 ``IdDirectory`` protocol, a synthetic document maker and a
reference BM25 (``rank_bm25.BM25Okapi``) over a set of final documents. Synthetic data only."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

from su_multi_arm.backends.sqlite.bm25 import analyzer
from su_multi_arm.core.config_collection import SliceSettings
from su_multi_arm.core.types import Document
from su_multi_arm.core.types_members import (
    AnalyzerRecipe,
    EmbeddingSpaceRef,
    IdDirectoryEntry,
    PartitionRecipe,
    PinnedView,
)
from su_multi_arm.extraction.chunking import chunk_document
from su_multi_arm.ingestion.log_slices import LogSliceLedger
from su_multi_arm.ingestion.pins import MemoryPins, SlicePins

DAY = date(2026, 11, 8)
SPACE = EmbeddingSpaceRef(id="fixture-space-1", model="hash-demo-v1", dimensions=64, template="engine_template")
EMBED_FIELDS = {"incident": ["title", "summary"], "unclassified_example": ["exemplars"]}


def recipe(**analyzer_kwargs) -> PartitionRecipe:
    return PartitionRecipe(
        embedding_space=SPACE,
        analyzer=AnalyzerRecipe(engine="bm25", tokenizer="unicode_word", **analyzer_kwargs),
        chunking={"size": 1600, "overlap": 160, "field_order": []},
        content={"keyword_epoch": "kw-e2"},
    )


class Clock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def make_ledger(root: Path, *, clock: Clock | None = None, directory=None, catalog=None, settings: SliceSettings | None = None,
                pins: SlicePins | None = None, member: str = "logs") -> LogSliceLedger:
    clock = clock or Clock()
    return LogSliceLedger(root, member=member, source="logs", recipe=recipe(), settings=settings, embed_fields=EMBED_FIELDS,
                          directory=directory, catalog=catalog, pins=pins or SlicePins(MemoryPins(clock)), clock=clock)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


def doc(doc_id: str, text: str, *, kind: str = "incident", title: str = "Synthetic incident", source: str = "logs") -> Document:
    return Document(id=doc_id, dataset="unified", source=source, kind=kind, title=title, fields={"summary": text})


def view_of(ledger: LogSliceLedger, members=()) -> PinnedView:
    return PinnedView(generation="g1", members=tuple(members), slices={ledger.member: ledger.vector()} if ledger.vector() else {})


class FakeDirectory:
    """In-memory ``IdDirectory`` with ``SqliteIdDirectory``'s registration rules: a duplicate live
    non-catalog id is refused unless its holder is listed in ``replaces`` or is a slice of the same
    member and day (then it is superseded); retired units keep their rows."""

    def __init__(self, fail_on: str | None = None):
        self.rows: dict[tuple[str, str], IdDirectoryEntry] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_on = fail_on
        self.retired: set[str] = set()

    def _check(self, op: str) -> None:
        self.calls.append((op, ""))
        if self.fail_on == op:
            raise RuntimeError(f"id directory unavailable ({op})")

    def register(self, unit_id, unit_kind, entries, *, member=None, replaces=()):
        self._check("register")
        supersede = []
        for entry in entries:
            for (d, u), held in self.rows.items():
                if d != entry.doc_id or not held.live or u in self.retired or (held.catalog_provisional and entry.catalog_provisional):
                    continue
                if u in replaces or (held.unit_kind == unit_kind == "slice" and held.day == entry.day):
                    supersede.append((d, u))
                else:
                    raise ValueError(f"duplicate live id {entry.doc_id}")
        for key in supersede:
            self.rows[key] = self.rows[key].model_copy(update={"superseded_by": unit_id})
        for entry in entries:
            self.rows[(entry.doc_id, unit_id)] = entry

    def unit(self, unit_id):
        if not any(u == unit_id for (_d, u) in self.rows):
            return None
        return {"unit_id": unit_id, "status": "retired" if unit_id in self.retired else "live"}

    def supersede(self, doc_ids, by):
        self._check("supersede")
        wanted = set(doc_ids)
        for key, entry in list(self.rows.items()):
            if key[0] in wanted and entry.live:
                self.rows[key] = entry.model_copy(update={"superseded_by": by})

    def tombstone(self, doc_ids, by):
        self._check("tombstone")
        wanted = set(doc_ids)
        for key, entry in list(self.rows.items()):
            if key[0] in wanted and entry.live:
                self.rows[key] = entry.model_copy(update={"deleted_by": by})

    def retire(self, unit_id):
        self._check("retire")
        for key in [k for k in self.rows if k[1] == unit_id]:
            del self.rows[key]

    def resolve(self, doc_id, view):
        units = set(view.unit_ids())
        found = [e for (d, u), e in self.rows.items() if d == doc_id and u in units and e.live]
        return found[0] if found else None

    def live_ids(self, member, day):
        return frozenset(e.doc_id for e in self.rows.values() if e.live and e.day == day)

    def live(self) -> dict[str, str]:
        return {d: u for (d, u), e in self.rows.items() if e.live}


def reference_rank(documents: Iterable[Document], query: str, k: int, ledger: LogSliceLedger) -> list[tuple[str, float]]:
    """``(chunk id, score)`` best first from ``rank_bm25.BM25Okapi`` over the chunks of the final
    documents in chunk-id order (the monolithic reference), matched rows only, ties by chunk id."""
    from rank_bm25 import BM25Okapi

    analyze = analyzer(ledger.bm25)
    chunks = []
    for d in documents:
        chunks.extend(chunk_document(d, ledger.chunking.size, ledger.chunking.overlap, list(ledger.chunking.field_order)))
    chunks.sort(key=lambda c: c.id)
    corpus = [analyze(c.text) for c in chunks]
    model = BM25Okapi(corpus, k1=ledger.bm25.k1, b=ledger.bm25.b, epsilon=ledger.bm25.epsilon)
    words = analyze(query)
    scores = model.get_scores(words)
    wanted = set(words)
    matched = [i for i, tokens in enumerate(corpus) if wanted & set(tokens)]
    matched.sort(key=lambda i: (-scores[i], chunks[i].id))
    return [(chunks[i].id, float(scores[i])) for i in matched[:k]]


def served_documents(ledger: LogSliceLedger, view: PinnedView) -> dict[str, Document]:
    """What a reader of ``view`` is served: highest version per id, tombstones hide older rows."""
    from su_multi_arm.ingestion.log_slices import UnitReader

    best: dict[str, tuple[int, Document | None]] = {}
    for _day, ids in sorted(view.slices.get(ledger.member, {}).items()):
        for slice_id in ids:
            reader = UnitReader(ledger.unit_dir(slice_id), ledger.bm25)
            try:
                versions = reader.version_of()
                for d in reader.documents():
                    if versions[d.id] > best.get(d.id, (-1, None))[0]:
                        best[d.id] = (versions[d.id], d)
                for d, v in reader.tombstones:
                    if v > best.get(d, (-1, None))[0]:
                        best[d] = (v, None)
            finally:
                reader.close()
    return {i: d for i, (_v, d) in best.items() if d is not None}


def hash_query_encoder(dims: int = 64):
    """A deterministic query encoder in the fixture space (the ``hash`` provider's math)."""
    from su_multi_arm.core.config import EmbedderRole
    from su_multi_arm.providers.embeddings import Embedder

    embedder = Embedder(EmbedderRole(provider="hash", model="hash-demo-v1", dimensions=dims))
    return embedder, (lambda text: embedder.encode([text], query=True)[0])


def ids_of(docs: Sequence[Document]) -> list[str]:
    return [d.id for d in docs]


def unit_norms(matrix: np.ndarray) -> np.ndarray:
    return np.linalg.norm(matrix, axis=1)
