"""Okapi BM25 lexical segment for the sqlite adapter (``lexical_engine: bm25``).

One immutable segment per generation, written at ingest (``SqliteLexical.refresh``) next to the
generation's catalog and read through memory maps:

* ``bm25/meta.json``: format, analyzer settings, ``corpus_size``, ``avgdl``, ``average_idf``;
* ``bm25/{offsets,post_rows,post_freqs,idf,doc_len,row_doc,row_field}.npy``: the inverted index
  (term id -> ascending row postings with term frequencies), per-term idf and per-row length,
  document ordinal and field ordinal;
* ``bm25/chunk_ids.json``, ``bm25/doc_ids.json``, ``bm25/fields.json``: row/ordinal -> id;
* ``bm25_terms(term, tid, df)`` in the generation's ``catalog.sqlite``: the vocabulary (looked up per
  query term; never loaded whole).

Rows are the indexed chunks in ascending ``chunk_id`` order, so equal scores rank by chunk id (the
FTS5 path's tie rule) independently of ingest batch order.

Scores are those of ``rank_bm25.BM25Okapi`` over the same token lists in the same row order, bit for
bit: term ids are assigned in first-occurrence order (``BM25Okapi``'s ``nd`` dict order), idf is
computed in that order with the same running float sum and epsilon floor, and a query adds, per
query token in order (repeats included), the identical float64 expression to the rows that contain
the token. Rows without the token receive exactly ``0.0`` in ``BM25Okapi`` (``idf * 0 / x``), so
skipping them leaves every score unchanged.

A row matches when it contains at least one query token (the ``LexicalIndex`` contract: a term
search finds every chunk with the term). A token found in exactly half of the rows has idf 0 under
this formula, so such rows can match with score 0; they rank after every positive score.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from array import array
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import numpy as np

from ...core.errors import BackendError
from ._db import KIND

FORMAT = 1
SEGMENT = "bm25"
TERMS_SCHEMA = "CREATE TABLE IF NOT EXISTS bm25_terms(term TEXT PRIMARY KEY, tid INTEGER NOT NULL, df INTEGER NOT NULL) WITHOUT ROWID"
SOURCE_SCHEMA = "CREATE TABLE IF NOT EXISTS bm25_source(chunk_id TEXT PRIMARY KEY, title TEXT NOT NULL) WITHOUT ROWID"
_PATTERNS = {"unicode_word": re.compile(r"\w+"), "ascii_alnum": re.compile(r"[a-z0-9]+")}


def analyzer(settings: Any) -> Callable[[str], list[str]]:
    """``text -> tokens`` for the configured tokenizer, case folding and stemmer.

    ``ascii_alnum`` matches ``[a-z0-9]+``, so it is applied to lower-cased text whatever
    ``lowercase`` says (upper-case letters would otherwise split words)."""
    pattern = _PATTERNS[settings.tokenizer]
    lower = settings.lowercase or settings.tokenizer == "ascii_alnum"
    stemmer = None
    lock = threading.Lock()  # Snowball stemmer objects keep per-call state
    if settings.stemming != "none":
        import snowballstemmer

        stemmer = snowballstemmer.stemmer(settings.stemming)

    def analyze(text: str) -> list[str]:
        words = pattern.findall(text.lower() if lower else text)
        if stemmer is None or not words:
            return words
        with lock:
            return list(stemmer.stemWords(words))

    return analyze


def settings_id(settings: Any) -> str:
    """The analyzer id recorded in the generation manifest (``bm25_okapi:<tokenizer>:<case>:<stem>``)."""
    case = "lower" if settings.lowercase or settings.tokenizer == "ascii_alnum" else "keep"
    title = ":title" if settings.include_title else ""
    return f"bm25_okapi:{settings.tokenizer}:{case}:{settings.stemming}{title}"


def _settings_dump(settings: Any) -> dict[str, Any]:
    return settings.model_dump(mode="json")


class Bm25Builder:
    """Streams ``(chunk_id, document_id, field, tokens)`` rows (in row order) into a segment."""

    def __init__(self, settings: Any):
        self.settings = settings
        self.vocab: dict[str, int] = {}
        self.df: list[int] = []
        self.terms, self.rows, self.freqs = array("i"), array("i"), array("i")
        self.doc_len = array("i")
        self.row_doc = array("i")
        self.row_field = array("i")
        self.chunk_ids: list[str] = []
        self.doc_ids: list[str] = []
        self._doc_index: dict[str, int] = {}
        self.fields: list[str] = []
        self._field_index: dict[str, int] = {}
        self.total_tokens = 0

    def add(self, chunk_id: str, document_id: str, field: str, tokens: Sequence[str]) -> None:
        row = len(self.chunk_ids)
        self.chunk_ids.append(chunk_id)
        ordinal = self._doc_index.get(document_id)
        if ordinal is None:
            ordinal = self._doc_index[document_id] = len(self.doc_ids)
            self.doc_ids.append(document_id)
        self.row_doc.append(ordinal)
        field_ordinal = self._field_index.get(field)
        if field_ordinal is None:
            field_ordinal = self._field_index[field] = len(self.fields)
            self.fields.append(field)
        self.row_field.append(field_ordinal)
        self.doc_len.append(len(tokens))
        self.total_tokens += len(tokens)
        frequencies: dict[str, int] = {}
        for token in tokens:
            frequencies[token] = frequencies.get(token, 0) + 1
        vocab, df = self.vocab, self.df
        for token, freq in frequencies.items():  # first-occurrence order, as BM25Okapi's nd
            tid = vocab.get(token)
            if tid is None:
                tid = vocab[token] = len(df)
                df.append(0)
            df[tid] += 1
            self.terms.append(tid)
            self.rows.append(row)
            self.freqs.append(freq)

    def write(self, folder: Path, db: sqlite3.Connection) -> dict[str, Any]:
        n = len(self.chunk_ids)
        k1, b, epsilon = self.settings.k1, self.settings.b, self.settings.epsilon
        # BM25Okapi._calc_idf: same iteration order, the same running float sum and floor.
        idf = np.zeros(len(self.df), dtype=np.float64)
        idf_sum = 0
        negative: list[int] = []
        for tid, freq in enumerate(self.df):
            value = math.log(n - freq + 0.5) - math.log(freq + 0.5)
            idf[tid] = value
            idf_sum += value
            if value < 0:
                negative.append(tid)
        average_idf = idf_sum / len(self.df) if self.df else 0.0
        eps = epsilon * average_idf
        for tid in negative:
            idf[tid] = eps
        terms = np.frombuffer(self.terms, dtype=np.int32) if len(self.terms) else np.zeros(0, dtype=np.int32)
        order = np.argsort(terms, kind="stable")  # stable: each posting list stays in ascending row order
        post_rows = (np.frombuffer(self.rows, dtype=np.int32)[order] if len(self.rows) else np.zeros(0, dtype=np.int32)).astype(np.int32)
        post_freqs = (np.frombuffer(self.freqs, dtype=np.int32)[order] if len(self.freqs) else np.zeros(0, dtype=np.int32)).astype(np.int32)
        counts = np.bincount(terms, minlength=len(self.df)) if len(terms) else np.zeros(len(self.df), dtype=np.int64)
        offsets = np.concatenate([[0], np.cumsum(counts)]).astype(np.int64)
        folder.mkdir(parents=True, exist_ok=True)
        arrays = {
            "offsets": offsets,
            "post_rows": post_rows,
            "post_freqs": post_freqs,
            "idf": idf,
            "doc_len": np.frombuffer(self.doc_len, dtype=np.int32).copy() if n else np.zeros(0, dtype=np.int32),
            "row_doc": np.frombuffer(self.row_doc, dtype=np.int32).copy() if n else np.zeros(0, dtype=np.int32),
            "row_field": np.frombuffer(self.row_field, dtype=np.int32).copy() if n else np.zeros(0, dtype=np.int32),
        }
        for name, value in arrays.items():
            np.save(folder / f"{name}.npy", np.ascontiguousarray(value), allow_pickle=False)
        (folder / "chunk_ids.json").write_text(json.dumps(self.chunk_ids))
        (folder / "doc_ids.json").write_text(json.dumps(self.doc_ids))
        (folder / "fields.json").write_text(json.dumps(self.fields))
        db.execute("DROP TABLE IF EXISTS bm25_terms")
        db.execute(TERMS_SCHEMA)
        db.executemany("INSERT INTO bm25_terms VALUES (?,?,?)", ((term, tid, self.df[tid]) for term, tid in self.vocab.items()))
        meta = {
            "format": FORMAT,
            "settings": _settings_dump(self.settings),
            "corpus_size": n,
            "avgdl": self.total_tokens / n if n else 0.0,
            "average_idf": average_idf,
            "terms": len(self.df),
            "documents": len(self.doc_ids),
            "postings": int(len(post_rows)),
            "k1": k1,
            "b": b,
        }
        (folder / "meta.json").write_text(json.dumps(meta, sort_keys=True))
        return meta


class Bm25Segment:
    """A loaded (memory-mapped) segment of one generation."""

    def __init__(self, folder: Path, settings: Any):
        meta_path = folder / "meta.json"
        if not meta_path.exists():
            raise BackendError(KIND, "lexical", "the generation has no bm25 segment; rebuild the index with lexical_engine: bm25")
        meta = json.loads(meta_path.read_text())
        if meta.get("format") != FORMAT:
            raise BackendError(KIND, "lexical", f"bm25 segment format {meta.get('format')!r} is not {FORMAT}; rebuild the index")
        if meta.get("settings") != _settings_dump(settings):
            raise BackendError(KIND, "lexical", "bm25 segment was built with other analyzer/scoring settings; rebuild the index")
        self.meta = meta
        self.folder = folder
        self.k1, self.b, self.avgdl = float(settings.k1), float(settings.b), float(meta["avgdl"])
        self.corpus_size = int(meta["corpus_size"])

        def load(name: str) -> np.ndarray:
            return np.load(folder / f"{name}.npy", mmap_mode="r", allow_pickle=False)

        self.offsets, self.post_rows, self.post_freqs = load("offsets"), load("post_rows"), load("post_freqs")
        self.idf, self.doc_len, self.row_doc, self.row_field = load("idf"), load("doc_len"), load("row_doc"), load("row_field")
        self.chunk_ids: list[str] = json.loads((folder / "chunk_ids.json").read_text())
        self.doc_ids: list[str] = json.loads((folder / "doc_ids.json").read_text())
        self.fields: list[str] = json.loads((folder / "fields.json").read_text())
        if not (len(self.chunk_ids) == len(self.doc_len) == len(self.row_doc) == len(self.row_field) == self.corpus_size):
            raise BackendError(KIND, "lexical", "bm25 segment files disagree on the row count")
        self._doc_index: dict[str, int] | None = None

    def doc_index(self) -> dict[str, int]:
        if self._doc_index is None:
            self._doc_index = {d: i for i, d in enumerate(self.doc_ids)}
        return self._doc_index

    def postings(self, tid: int) -> tuple[np.ndarray, np.ndarray]:
        start, end = int(self.offsets[tid]), int(self.offsets[tid + 1])
        return self.post_rows[start:end], self.post_freqs[start:end]

    def scores(self, tids: Iterable[int | None]) -> tuple[np.ndarray, np.ndarray]:
        """``(BM25Okapi.get_scores, matched)`` over the query's token ids in order (``None`` = a token
        outside the vocabulary). ``matched`` marks the rows that contain at least one query token."""
        score = np.zeros(self.corpus_size)
        matched = np.zeros(self.corpus_size, dtype=bool)
        k1, b, avgdl = self.k1, self.b, self.avgdl
        for tid in tids:
            if tid is None:
                continue
            rows, freqs = self.postings(tid)
            if not len(rows):
                continue
            f = freqs.astype(np.float64)
            dl = self.doc_len[rows]
            score[rows] += float(self.idf[tid]) * (f * (k1 + 1) / (f + k1 * (1 - b + b * dl / avgdl)))
            matched[rows] = True
        return score, matched

    def contains_all(self, tids: Sequence[int]) -> np.ndarray:
        """Rows containing every token (ascending), smallest posting list first."""
        ordered = sorted(set(tids), key=lambda t: int(self.offsets[t + 1] - self.offsets[t]))
        if not ordered:
            return np.zeros(0, dtype=np.int32)
        rows = np.asarray(self.postings(ordered[0])[0])
        for tid in ordered[1:]:
            if not len(rows):
                break
            rows = np.intersect1d(rows, self.postings(tid)[0], assume_unique=True)
        return rows


def top_rows(scores: np.ndarray, matched: np.ndarray, eligible: np.ndarray | None, k: int) -> np.ndarray:
    """Matched (and eligible) rows, best score first, ties by row; at most ``k``."""
    candidates = np.flatnonzero(matched if eligible is None else matched & eligible)
    if not len(candidates) or k <= 0:
        return candidates[:0]
    values = scores[candidates]
    if len(candidates) > k:
        # Keep every row tied with the k-th best so the tie order below is by row, not by partition.
        threshold = np.partition(values, len(values) - k)[len(values) - k]
        keep = values >= threshold
        candidates, values = candidates[keep], values[keep]
    order = np.lexsort((candidates, -values))
    return candidates[order][:k]


def contains_sequence(tokens: Sequence[str], needle: Sequence[str]) -> bool:
    n = len(needle)
    if n == 0 or n > len(tokens):
        return False
    first = needle[0]
    needle = list(needle)
    for i in range(len(tokens) - n + 1):
        if tokens[i] == first and list(tokens[i : i + n]) == needle:
            return True
    return False
