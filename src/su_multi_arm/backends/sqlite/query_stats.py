"""Query-time statistics of a log stratum (docs/plans/unified_merge.md §6.5, B10; lane LB).

Implements the S0 ``StratumScorer`` protocol for the live log source: one stratum = the pinned log
view (archive day/month members of the member and its pinned slices). No unit holds global
statistics; per query they are computed over the pinned units **minus superseded and tombstoned
rows**:

* every sealed row carries a **version** (``ingestion/log_slices.py``); the served row of an id is
  the one with the highest version among the pinned units, and a tombstone with a higher version
  hides every older row. Rows that are not served are excluded from ``N``, the token total, ``df``
  and candidates — exactly like rows that do not exist;
* ``N`` and the token total are summed over the served rows of each unit; ``df`` is computed for the
  **query terms only** by counting served rows in each unit's posting list; idf is recomputed per
  query term with the ``BM25Okapi`` formula ``log(N - df + 0.5) - log(df + 0.5)``; the score of a
  row is the identical float64 expression ``Bm25Segment.scores`` uses. Ties rank by chunk id, the
  monolithic segment's row order. So the ranking over N slices equals the ranking over one
  compacted day built from the same final documents (ids, order, and scores to rounding).
* **Declared approximation** (``average_idf_lag``): the epsilon floor of a term in more than half of
  the rows uses the source's ``average_idf`` maintained at base compaction/close (the ledger's
  ``average_idf()``), which lags the live corpus by at most one base period. A ranking that used
  the floor lists the approximation.

Cost: O(query terms × pinned units) posting reads plus a once-per-view served-row mask (cached by
the pinned unit set; a new slice changes the set, so the first request after it pays one mask
pass). Unit readers are cached by unit id + seal, so a new slice warms only itself.

``DenseLogScorer`` is the dense counterpart: exact flat top-k over the served embed rows of the
pinned units, vectors from the text-hash store (``ingestion/text_vectors.py``); a unit whose
vectors are not all stored yet is skipped and reported (``dense_pending:<unit>``), never guessed.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from ...core.types_members import PinnedView, StratumRanking
from ...ingestion.log_slices import LogSliceLedger, UnitReader
from ...ingestion.text_vectors import TextVectorStore
from .bm25 import analyzer, top_rows
from .search import MAX_TERMS

AVERAGE_IDF_LAG = "average_idf_lag"


class LogUnits:
    """Opens the units of one log member that a ``PinnedView`` pins: its archive/catalog members
    (``registry`` + the member's lexical facet location) and its slices (the ledger). Readers are
    cached by ``(unit id, seal)``; ``opened`` counts cold opens (cache misses)."""

    def __init__(self, ledger: LogSliceLedger, registry: str | Path | None = None, max_cached: int = 512):
        self.ledger = ledger
        self.registry = None if registry is None else Path(registry)
        self.max_cached = max_cached
        self._cache: OrderedDict[str, UnitReader] = OrderedDict()
        self._lock = threading.Lock()
        self.opened = 0

    def unit_folders(self, view: PinnedView) -> list[tuple[str, Path]]:
        out: list[tuple[str, Path]] = []
        for ref in view.members:
            if ref.member != self.ledger.member:
                continue
            if self.registry is None:
                raise ValueError("member units need the member registry root")
            out.append((ref.unit_id, self.registry / Path(ref.facets["lexical"].location).parent))
        for _day, ids in sorted(view.slices.get(self.ledger.member, {}).items()):
            out.extend((slice_id, self.ledger.unit_dir(slice_id)) for slice_id in ids)
        return out

    def readers(self, view: PinnedView) -> list[UnitReader]:
        return [self.reader(unit_id, folder) for unit_id, folder in self.unit_folders(view)]

    def reader(self, unit_id: str, folder: Path) -> UnitReader:
        """The cached reader of one unit (keyed by unit id + seal)."""
        seal = (folder / "SEAL").read_text().strip()
        key = f"{unit_id}@{seal}"
        with self._lock:
            reader = self._cache.get(key)
            if reader is not None:
                self._cache.move_to_end(key)
                return reader
        reader = UnitReader(folder, self.ledger.bm25, unit_id)
        with self._lock:
            found = self._cache.get(key)
            if found is not None:
                reader.close()
                return found
            self.opened += 1
            self._cache[key] = reader
            while len(self._cache) > self.max_cached:
                # an evicted reader is not closed: a concurrent request may still score with it
                self._cache.popitem(last=False)
        return reader


class ServedRows:
    """Which rows of each pinned unit are served (highest version per id; tombstones hide older
    rows). Cached per pinned unit set."""

    def __init__(self, max_views: int = 16):
        self._cache: OrderedDict[tuple[str, ...], dict[str, tuple[np.ndarray, np.ndarray]]] = OrderedDict()
        self._lock = threading.Lock()
        self.max_views = max_views
        self.computed = 0

    def masks(self, readers: Sequence[UnitReader]) -> dict[str, tuple[np.ndarray, np.ndarray]]:
        """``{unit id: (row mask over lexical rows, doc mask over rows.json ids)}``."""
        key = tuple(f"{r.unit_id}@{r.seal}" for r in readers)
        with self._lock:
            found = self._cache.get(key)
            if found is not None:
                self._cache.move_to_end(key)
                return found
        decided: dict[str, int] = {}
        get = decided.get
        for reader in readers:
            for doc_id, version in zip(reader.ids, reader.versions):
                if version > get(doc_id, -1):
                    decided[doc_id] = version
            for doc_id, version in reader.tombstones:
                if version > get(doc_id, -1):
                    decided[doc_id] = version
        out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        for reader in readers:
            served = {doc_id: decided[doc_id] == version for doc_id, version in zip(reader.ids, reader.versions)}
            doc_mask = np.fromiter((served[d] for d in reader.ids), dtype=bool, count=len(reader.ids))
            segment = reader.segment
            seg_docs = np.fromiter((served.get(d, False) for d in segment.doc_ids), dtype=bool, count=len(segment.doc_ids))
            row_mask = seg_docs[np.asarray(segment.row_doc)] if len(segment.doc_ids) else np.zeros(segment.corpus_size, dtype=bool)
            out[reader.unit_id] = (row_mask, doc_mask)
        with self._lock:
            self.computed += 1
            self._cache[key] = out
            while len(self._cache) > self.max_views:
                self._cache.popitem(last=False)
        return out


class QueryTimeScorer:
    """``StratumScorer`` of a log stratum with query-time statistics (see the module doc)."""

    def __init__(self, stratum: str, units: LogUnits, *, average_idf: Callable[[], float | None] | None = None, served: ServedRows | None = None):
        self.stratum = stratum
        self.units = units
        self.settings = units.ledger.bm25
        self.analyze = analyzer(self.settings)
        self.average_idf = average_idf or units.ledger.average_idf
        self.served = served or ServedRows()

    def query_tokens(self, text: str) -> list[str]:
        words = self.analyze(text)
        keep = set(list(dict.fromkeys(words))[:MAX_TERMS])
        return [w for w in words if w in keep]

    def statistics(self, readers: Sequence[UnitReader], masks: dict[str, tuple[np.ndarray, np.ndarray]], words: Sequence[str]) -> dict[str, Any]:
        """``N``, token total, ``avgdl`` and per-term ``df`` over the served rows."""
        n = 0
        total = 0
        df = dict.fromkeys(dict.fromkeys(words), 0)
        tids: dict[str, dict[str, int]] = {}
        for reader in readers:
            rows_mask = masks[reader.unit_id][0]
            n += int(rows_mask.sum())
            total += int(np.asarray(reader.segment.doc_len, dtype=np.int64)[rows_mask].sum())
            found = reader.term_ids(list(df))
            tids[reader.unit_id] = found
            for word, tid in found.items():
                rows, _freqs = reader.segment.postings(tid)
                df[word] += int(rows_mask[np.asarray(rows)].sum())
        return {"N": n, "tokens": total, "avgdl": total / n if n else 0.0, "df": df, "tids": tids}

    def rank_rows(self, query: str, k: int, view: PinnedView, *, eligible: dict[str, np.ndarray | None] | None = None) -> tuple[list[tuple[str, str, float, str]], dict[str, Any]]:
        """``[(chunk_id, doc_id, score, unit_id)]`` best first, plus the statistics used. ``eligible``
        restricts the candidates of a unit (request scope, fields, phrase); statistics stay over every
        served row of the stratum, as one store's search keeps its corpus statistics under a scope."""
        readers = self.units.readers(view)
        words = self.query_tokens(query)
        if not readers or not words or k <= 0:
            return [], {"N": 0, "units": [r.unit_id for r in readers], "approximations": []}
        masks = self.served.masks(readers)
        stats = self.statistics(readers, masks, words)
        n, avgdl = stats["N"], stats["avgdl"]
        approximations: list[str] = []
        idf: dict[str, float] = {}
        for word, freq in stats["df"].items():
            if freq == 0:
                continue
            value = math.log(n - freq + 0.5) - math.log(freq + 0.5)
            if value < 0:
                average = self.average_idf()
                if average is None:
                    average = 0.0
                value = float(self.settings.epsilon) * average
                if AVERAGE_IDF_LAG not in approximations:
                    approximations.append(AVERAGE_IDF_LAG)
            idf[word] = value
        k1, b = float(self.settings.k1), float(self.settings.b)
        hits: list[tuple[float, str, str, str]] = []
        for reader in readers:
            segment = reader.segment
            rows_mask = masks[reader.unit_id][0]
            restrict = None if eligible is None else eligible.get(reader.unit_id)
            if restrict is not None:
                rows_mask = rows_mask & restrict
            tids = stats["tids"][reader.unit_id]
            score = np.zeros(segment.corpus_size)
            matched = np.zeros(segment.corpus_size, dtype=bool)
            for word in words:
                tid = tids.get(word)
                if tid is None or word not in idf:
                    continue
                rows, freqs = segment.postings(tid)
                if not len(rows):
                    continue
                f = freqs.astype(np.float64)
                dl = segment.doc_len[rows]
                score[rows] += idf[word] * (f * (k1 + 1) / (f + k1 * (1 - b + b * dl / avgdl)))
                matched[rows] = True
            for row in top_rows(score, matched, rows_mask, k):
                row = int(row)
                hits.append((float(score[row]), segment.chunk_ids[row], segment.doc_ids[int(segment.row_doc[row])], reader.unit_id))
        hits.sort(key=lambda h: (-h[0], h[1]))
        ranked = [(chunk, doc, score, unit) for score, chunk, doc, unit in hits[:k]]
        info = {"N": n, "tokens": stats["tokens"], "avgdl": avgdl, "df": stats["df"], "units": [r.unit_id for r in readers], "approximations": approximations}
        return ranked, info

    def rank(self, query: str, k: int, view: PinnedView) -> StratumRanking:
        ranked, info = self.rank_rows(query, k, view)
        return StratumRanking(stratum=self.stratum, ids=tuple(r[0] for r in ranked), scores=tuple(r[2] for r in ranked),
                              units=tuple(info["units"]), approximations=tuple(info["approximations"]))


class DenseLogScorer:
    """Exact dense top-k over the served embed rows of the pinned log units (see the module doc).
    ``encode_query`` returns the L2-normalised query vector of the store's embedding space."""

    def __init__(self, stratum: str, units: LogUnits, store: TextVectorStore, encode_query: Callable[[str], np.ndarray], served: ServedRows | None = None):
        self.stratum = stratum
        self.units = units
        self.store = store
        self.encode_query = encode_query
        self.served = served or ServedRows()
        self._matrices: OrderedDict[str, tuple[np.ndarray, np.ndarray, list[str], list[str], np.ndarray]] = OrderedDict()
        self._lock = threading.Lock()
        self._loading: dict[str, threading.Lock] = {}

    def matrix(self, reader: UnitReader) -> tuple[np.ndarray, np.ndarray, list[str], list[str], np.ndarray] | None:
        """(unique-text vectors, row -> unique index, chunk ids, doc ids, position of each row's
        document in ``reader.ids``), or ``None`` while a vector of the unit is missing (dense
        pending). One store lookup per distinct text; a unit is loaded once even under concurrent
        first requests."""
        key = f"{reader.unit_id}@{reader.seal}"
        with self._lock:
            found = self._matrices.get(key)
            if found is not None:
                return found
            gate = self._loading.setdefault(key, threading.Lock())
        with gate:
            with self._lock:
                found = self._matrices.get(key)
            if found is not None:
                return found
            rows, _texts = reader.embed()
            loaded = self.store.get_unique([r.hash for r in rows]) if rows else (np.zeros((0, self.store.dims), dtype=np.float32), np.zeros(0, dtype=np.int64))
            if loaded is None:
                with self._lock:
                    self._loading.pop(key, None)
                return None
            unique, inverse = loaded
            index = {d: i for i, d in enumerate(reader.ids)}
            positions = np.fromiter((index[r.doc_id] for r in rows), dtype=np.int64, count=len(rows))
            value = (unique, inverse, [r.chunk_id for r in rows], [r.doc_id for r in rows], positions)
            with self._lock:
                self._matrices[key] = value
                self._loading.pop(key, None)
                while len(self._matrices) > 512:
                    self._matrices.popitem(last=False)
            return value

    def rank_rows(self, query: str, k: int, view: PinnedView) -> tuple[list[tuple[str, str, float, str]], dict[str, Any]]:
        if not self.units.unit_folders(view) or k <= 0:
            return [], {"units": [], "pending": []}
        return self.rank_vector(np.asarray(self.encode_query(query), dtype=np.float32).reshape(-1), k, view)

    def rank_vector(self, q: np.ndarray, k: int, view: PinnedView, *, eligible: dict[str, np.ndarray | None] | None = None) -> tuple[list[tuple[str, str, float, str]], dict[str, Any]]:
        """``rank_rows`` for an already encoded query vector; ``eligible`` restricts a unit's
        documents (doc mask over ``reader.ids``: the request scope)."""
        readers = self.units.readers(view)
        pending: list[str] = []
        if not readers or k <= 0:
            return [], {"units": [], "pending": pending}
        masks = self.served.masks(readers)
        q = np.asarray(q, dtype=np.float32).reshape(-1)
        hits: list[tuple[float, str, str, str]] = []
        for reader in readers:
            found = self.matrix(reader)
            if found is None:
                pending.append(reader.unit_id)
                continue
            unique, inverse, chunk_ids, doc_ids, positions = found
            if not len(chunk_ids):
                continue
            docs = masks[reader.unit_id][1]
            restrict = None if eligible is None else eligible.get(reader.unit_id)
            if restrict is not None:
                docs = docs & restrict
            allowed = docs[positions]
            scores = (unique @ q)[inverse]
            for row in top_rows(scores.astype(np.float64), allowed, None, k):
                row = int(row)
                hits.append((float(scores[row]), chunk_ids[row], doc_ids[row], reader.unit_id))
        hits.sort(key=lambda h: (-h[0], h[1]))
        return [(c, d, s, u) for s, c, d, u in hits[:k]], {"units": [r.unit_id for r in readers], "pending": pending}

    def rank(self, query: str, k: int, view: PinnedView) -> StratumRanking:
        ranked, info = self.rank_rows(query, k, view)
        return StratumRanking(stratum=self.stratum, ids=tuple(r[0] for r in ranked), scores=tuple(r[2] for r in ranked),
                              units=tuple(u for u in info["units"] if u not in info["pending"]),
                              approximations=tuple(f"dense_pending:{u}" for u in info["pending"]))
