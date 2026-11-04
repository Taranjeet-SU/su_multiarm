"""SQLite ``LexicalIndex`` (FTS5 ``unicode61`` or an in-process Okapi BM25 segment; phrase, boolean)
and ``VectorIndex`` (exact NumPy or Faiss over per-generation files) facets.

Both facets push eligibility down as a document bitset over the generation's own row maps
(``SqliteBackend.document_mask``): scoring never reads the catalog per candidate, and the top-k is
taken over eligible rows only."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

import numpy as np

from ...core.errors import BackendError
from ...core.types import Chunk, GenerationId, LexicalQuery, Page, PageResult, Scope, ScoredChunk
from ._db import KIND, placeholders, scope_sql
from .bm25 import SEGMENT, SOURCE_SCHEMA, Bm25Builder, Bm25Segment, analyzer, contains_sequence, top_rows
from .catalog import page_window

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import SqliteBackend

MAX_TERMS = 128


def tokens(text: str) -> list[str]:
    """Query terms, **not** case-folded here (QA-07). Every term is passed to FTS5 as a quoted string,
    which FTS5 re-tokenises with the same ``unicode61`` tokenizer the text was indexed with, so case
    and diacritic folding is exactly the index's. ``str.casefold`` would fold beyond ``unicode61``
    (``ß``->``ss``, ``ﬁ``->``fi``) and the query could never match its own indexed word."""
    return re.findall(r"\w+", text)


def _distinct(terms: list[str]) -> list[str]:
    """Drop repeated terms (``str.lower`` only: it keeps ``ß`` and ligatures, like ``unicode61``)."""
    seen: dict[str, str] = {}
    for term in terms:
        seen.setdefault(term.lower(), term)
    return list(seen.values())


def _quoted(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def fts_expression(query: LexicalQuery) -> str | None:
    """Fixed FTS5 templates; every user term is quoted, never interpreted as syntax."""
    terms = _distinct(tokens(query.text))[:MAX_TERMS]
    if query.kind == "phrase":
        return _quoted(" ".join(terms)) if terms else None
    if not terms:
        return None
    expression = "(" + " OR ".join(_quoted(t) for t in terms) + ")"
    if query.kind == "boolean":
        for must in query.must:
            words = tokens(must)
            if words:
                expression += " AND " + _quoted(" ".join(words))
        for must_not in query.must_not:
            words = tokens(must_not)
            if words:
                expression += " NOT " + _quoted(" ".join(words))
    return expression


def _page(rows: np.ndarray, offset: int, limit: int, k: int) -> tuple[np.ndarray, str | None]:
    window = rows[offset : offset + limit]
    more = len(rows) > offset + len(window) and offset + len(window) < k
    return window, (str(offset + len(window)) if more else None)


class SqliteLexical:
    """``lexical_engine: fts5`` (default) or ``bm25`` (``bm25.py``). Both keep the chunk rows in the
    generation's catalog; ``bm25`` builds its segment at ``refresh``."""

    def __init__(self, backend: SqliteBackend):
        self._b = backend
        self.engine = backend.lexical_engine
        self.settings = backend.bm25
        self._analyze = analyzer(self.settings) if self.engine == "bm25" else None
        # BM25Okapi adds a repeated query token's contribution again; the arm then sends repeats.
        self.scores_query_repeats = self.engine == "bm25"
        self._segments: dict[str, Bm25Segment] = {}
        self._dirty: set[str] = set()
        self._lock = threading.RLock()

    def unindexable(self, words: Sequence[str]) -> list[str]:
        """Query words the index analyzer turns into no token at all (``bm25`` only). Such a word can
        never be required or matched, so dropping it would silently widen a ``must`` or phrase."""
        if self._analyze is None:
            return []
        return [w for w in words if not self._analyze(w)]

    @property
    def analyze(self):
        """The index analyzer (``text -> tokens``); ``None`` for FTS5, which tokenises inside SQLite.
        The lexical arm's verbatim-window policy needs it."""
        return self._analyze

    def index(self, gen: GenerationId, chunks: Sequence[Chunk], fields: Sequence[str]) -> None:
        # One row per chunk id: a repeated id within one call would otherwise be inserted twice.
        selected = list({c.id: c for c in chunks if not fields or c.field in fields}.values())
        bm25 = self.engine == "bm25"
        with self._b.catalog_db(gen, create=True) as db:
            if bm25:
                db.execute(SOURCE_SCHEMA)
            else:
                # ``chunk_id`` is an UNINDEXED FTS column, so a per-chunk ``DELETE ... WHERE chunk_id=?``
                # scans the whole table each time (quadratic on a large build). Replace the rows of every
                # chunk being indexed with one scan instead.
                db.execute("CREATE TEMP TABLE IF NOT EXISTS _fts_replace(id TEXT PRIMARY KEY)")
                db.execute("DELETE FROM _fts_replace")
                db.executemany("INSERT OR IGNORE INTO _fts_replace VALUES (?)", [(c.id,) for c in selected])
                db.execute("DELETE FROM fts WHERE chunk_id IN (SELECT id FROM _fts_replace)")
                db.execute("DELETE FROM _fts_replace")
            titles: dict[str, str] = {}
            for chunk in selected:
                title = titles.get(chunk.document_id)
                if title is None:
                    row = db.execute("SELECT json_extract(data, '$.title') AS title FROM documents WHERE id=?", (chunk.document_id,)).fetchone()
                    if row is None:
                        raise BackendError(KIND, "index", f"chunk {chunk.id} references unknown document")
                    title = titles[chunk.document_id] = row["title"] or ""
                db.execute(
                    "INSERT OR IGNORE INTO chunks VALUES (?,?,?,?,?,?,?)",
                    (chunk.id, chunk.document_id, chunk.field, chunk.start, chunk.end, chunk.text, chunk.recipe),
                )
                if bm25:
                    db.execute("INSERT OR REPLACE INTO bm25_source VALUES (?,?)", (chunk.id, title))
                else:
                    db.execute("INSERT INTO fts VALUES (?,?,?)", (chunk.id, title, chunk.text))
        if bm25:
            with self._lock:
                self._dirty.add(gen.prefix())
                self._segments.pop(gen.prefix(), None)

    def refresh(self, gen: GenerationId) -> None:
        """FTS5: writes are durable at commit, so this only checks the generation. bm25: (re)builds the
        generation's segment from every chunk indexed so far, when anything changed."""
        self._b.catalog_db_exists(gen)
        if self.engine != "bm25":
            return
        folder = self._b.snapshot_dir(gen) / SEGMENT
        with self._lock:
            if gen.prefix() not in self._dirty and (folder / "meta.json").exists():
                return
            builder = Bm25Builder(self.settings)
            analyze, with_title = self._analyze, self.settings.include_title
            with self._b.catalog_db(gen) as db:
                db.execute(SOURCE_SCHEMA)
                cursor = db.cursor()
                cursor.row_factory = None
                cursor.execute("SELECT c.id, c.document_id, c.field, c.text, s.title FROM bm25_source s JOIN chunks c ON c.id=s.chunk_id ORDER BY c.id")
                for chunk_id, document_id, field, text, title in cursor:
                    words = analyze(text or "")
                    if with_title and title:
                        words = analyze(title) + words
                    builder.add(chunk_id, document_id, field, words)
                builder.write(folder, db)
            self._dirty.discard(gen.prefix())
            self._segments.pop(gen.prefix(), None)

    def forget(self, gen: GenerationId) -> None:
        with self._lock:
            self._segments.pop(gen.prefix(), None)
            self._dirty.discard(gen.prefix())

    def segment(self, gen: GenerationId) -> Bm25Segment:
        """The generation's segment, memory-mapped once per generation and process."""
        prefix = gen.prefix()
        found = self._segments.get(prefix)
        if found is not None:
            return found
        self._b.catalog_db_exists(gen)
        with self._lock:
            if prefix not in self._segments:
                self._segments[prefix] = Bm25Segment(self._b.snapshot_dir(gen) / SEGMENT, self.settings)
            return self._segments[prefix]

    def count(self, gen: GenerationId) -> int:
        if self.engine == "bm25":
            return self.segment(gen).corpus_size
        with self._b.catalog_db(gen) as db:
            return int(db.execute("SELECT count(*) FROM fts").fetchone()[0])

    def search(
        self, gen: GenerationId, query: LexicalQuery, scope: Scope | None, k: int, page: Page | None = None
    ) -> PageResult[ScoredChunk]:
        if self.engine == "bm25":
            return self._bm25_search(gen, query, scope, k, page)
        expression = fts_expression(query)
        if expression is None:
            return PageResult(items=[], next_cursor=None)
        offset, size = page_window(page, k)
        where, params = scope_sql("c.document_id", scope)
        field_clause = ""
        if query.fields:
            field_clause = f" AND c.field IN ({placeholders(query.fields)})"
            params = [*params, *query.fields]
        with self._b.catalog_db(gen) as db:
            rows = db.execute(
                "SELECT f.chunk_id, c.document_id, bm25(fts,0,2,1) AS score FROM fts f JOIN chunks c ON c.id=f.chunk_id "
                f"WHERE fts MATCH ? AND {where}{field_clause} ORDER BY bm25(fts,0,2,1), f.chunk_id LIMIT ? OFFSET ?",
                [expression, *params, min(size, max(0, k - offset)) + 1, offset],
            ).fetchall()
        window = rows[: min(size, max(0, k - offset))]
        items = [ScoredChunk(chunk_id=r["chunk_id"], document_id=r["document_id"], score=-float(r["score"])) for r in window]
        more = len(rows) > len(window) and offset + len(window) < k
        return PageResult(items=items, next_cursor=str(offset + len(window)) if more else None)

    # --- bm25 engine ----------------------------------------------------------------------------

    def _query_tokens(self, text: str) -> list[str]:
        """Analyzed query tokens with repeats (``BM25Okapi`` scores a repeated token again), limited to
        the first ``MAX_TERMS`` distinct tokens."""
        words = self._analyze(text)
        keep = set(list(dict.fromkeys(words))[:MAX_TERMS])
        return [w for w in words if w in keep]

    def term_ids(self, gen: GenerationId, words: Sequence[str]) -> dict[str, tuple[int, int]]:
        """``token -> (term id, chunk frequency)`` for the tokens in the generation's vocabulary."""
        distinct = list(dict.fromkeys(words))
        if not distinct:
            return {}
        self.segment(gen)  # the vocabulary belongs to a built segment
        with self._b.catalog_db(gen) as db:
            rows = db.execute(f"SELECT term, tid, df FROM bm25_terms WHERE term IN ({placeholders(distinct)})", distinct).fetchall()
        return {r[0]: (int(r[1]), int(r[2])) for r in rows}

    def doc_freqs(self, gen: GenerationId, words: Sequence[str]) -> list[int]:
        """Chunk frequency of each analyzed token (0 when absent)."""
        found = self.term_ids(gen, words)
        return [found[w][1] if w in found else 0 for w in words]

    def _eligible_rows(self, gen: GenerationId, segment: Bm25Segment, scope: Scope | None, fields: Sequence[str] | None) -> np.ndarray | None:
        mask = self._b.document_mask(gen, scope, segment.doc_index(), len(segment.doc_ids))
        rows = None if mask is None else mask[np.asarray(segment.row_doc)]
        if fields:
            wanted = np.asarray([i for i, f in enumerate(segment.fields) if f in set(fields)], dtype=np.int32)
            in_field = np.isin(np.asarray(segment.row_field), wanted)
            rows = in_field if rows is None else rows & in_field
        return rows

    def _analyzed_texts(self, gen: GenerationId, chunk_ids: Sequence[str]) -> dict[str, list[list[str]]]:
        """Analyzed token lists (text, and title when indexed) of the given chunks."""
        out: dict[str, list[list[str]]] = {}
        with_title = self.settings.include_title
        with self._b.catalog_db(gen) as db:
            for start in range(0, len(chunk_ids), 500):
                ids = list(chunk_ids[start : start + 500])
                rows = db.execute(
                    f"SELECT c.id, c.text, s.title FROM chunks c JOIN bm25_source s ON s.chunk_id=c.id WHERE c.id IN ({placeholders(ids)})", ids
                ).fetchall()
                for chunk_id, text, title in rows:
                    parts = [self._analyze(text or "")]
                    if with_title and title:
                        parts.append(self._analyze(title))
                    out[chunk_id] = parts
        return out

    def _verified(self, gen: GenerationId, segment: Bm25Segment, words: Sequence[str], candidates: np.ndarray) -> np.ndarray:
        """The candidate rows whose text (or indexed title) holds ``words`` contiguously."""
        if not len(candidates):
            return candidates
        ids = [segment.chunk_ids[int(r)] for r in candidates]
        texts = self._analyzed_texts(gen, ids)
        keep = np.asarray([any(contains_sequence(part, words) for part in texts.get(cid, [])) for cid in ids], dtype=bool)
        return candidates[keep]

    def _containing(self, gen: GenerationId, segment: Bm25Segment, words: Sequence[str]) -> np.ndarray:
        """Rows that contain every token of ``words`` (not yet checked for adjacency)."""
        found = self.term_ids(gen, words)
        if not words or any(w not in found for w in words):
            return np.zeros(0, dtype=np.int64)
        return segment.contains_all([found[w][0] for w in words]).astype(np.int64)

    def phrase_rows(self, gen: GenerationId, words: Sequence[str], scope: Scope | None, max_candidates: int) -> list[ScoredChunk] | None:
        """Chunks inside ``scope`` whose analyzed text holds ``words`` contiguously, in row order; or
        ``None`` when more than ``max_candidates`` eligible chunks contain all the tokens (too common a
        window to verify). Used by the lexical arm's verbatim-window policy."""
        segment = self.segment(gen)
        rows = self._containing(gen, segment, words)
        eligible = self._eligible_rows(gen, segment, scope, None)
        if eligible is not None and len(rows):
            rows = rows[eligible[rows]]
        if len(rows) > max_candidates:
            return None
        rows = self._verified(gen, segment, words, rows)
        return [ScoredChunk(chunk_id=segment.chunk_ids[int(r)], document_id=segment.doc_ids[int(segment.row_doc[int(r)])], score=0.0) for r in rows]

    def _bm25_search(self, gen: GenerationId, query: LexicalQuery, scope: Scope | None, k: int, page: Page | None) -> PageResult[ScoredChunk]:
        offset, size = page_window(page, k)
        segment = self.segment(gen)
        words = self._query_tokens(query.text)
        if not words:
            return PageResult(items=[], next_cursor=None)
        found = self.term_ids(gen, words)
        scores, matched = segment.scores(found[w][0] if w in found else None for w in words)
        eligible = self._eligible_rows(gen, segment, scope, query.fields)
        restrict: np.ndarray | None = None
        if query.kind == "phrase":
            # A phrase word that analyzes to nothing cannot be verified: match nothing, never a
            # shorter phrase.
            if self.unindexable(re.findall(r"\w+", query.text)):
                return PageResult(items=[], next_cursor=None)
            restrict = self._verified(gen, segment, words, self._containing(gen, segment, words))
        elif query.kind == "boolean":
            candidates = np.flatnonzero(matched)
            for must in query.must:
                need = self._analyze(must)
                if not need:
                    # A required word the analyzer drops can never be satisfied: match nothing.
                    candidates = np.zeros(0, dtype=np.int64)
                    break
                if need:
                    inside = np.intersect1d(candidates, self._containing(gen, segment, need), assume_unique=True)
                    candidates = self._verified(gen, segment, need, inside)
            for must_not in query.must_not:
                avoid = self._analyze(must_not)
                if avoid:
                    inside = np.intersect1d(candidates, self._containing(gen, segment, avoid), assume_unique=True)
                    candidates = np.setdiff1d(candidates, self._verified(gen, segment, avoid, inside), assume_unique=True)
            restrict = candidates
        if restrict is not None:
            allowed = np.zeros(segment.corpus_size, dtype=bool)
            allowed[restrict] = True
            eligible = allowed if eligible is None else eligible & allowed
        limit = min(size, max(0, k - offset))
        best = top_rows(scores, matched, eligible, offset + limit + 1)
        window, cursor = _page(best, offset, limit, k)
        items = [
            ScoredChunk(chunk_id=segment.chunk_ids[int(r)], document_id=segment.doc_ids[int(segment.row_doc[int(r)])], score=float(scores[r]))
            for r in window
        ]
        return PageResult(items=items, next_cursor=cursor)


def top_k(scores: np.ndarray, eligible: np.ndarray | None, k: int) -> np.ndarray:
    """Indices of the ``k`` best eligible scores, best first, ties by index (a stable descending sort)."""
    candidates = np.arange(len(scores)) if eligible is None else np.flatnonzero(eligible)
    if not len(candidates) or k <= 0:
        return candidates[:0]
    values = scores[candidates]
    if len(candidates) > k:
        threshold = np.partition(values, len(values) - k)[len(values) - k]
        keep = values >= threshold
        candidates, values = candidates[keep], values[keep]
    return candidates[np.lexsort((candidates, -values))][:k]


class SqliteVector:
    def __init__(self, backend: SqliteBackend):
        self._b = backend
        # Pending upserts are kept as blocks and concatenated once at ``refresh``: re-stacking the
        # whole matrix on every batch made a large ingest quadratic in its size.
        self._pending: dict[tuple[str, str], tuple[list[str], list[np.ndarray], set[str]]] = {}
        self._loaded: dict[tuple[str, str], tuple[list[str], np.ndarray]] = {}
        self._faiss: dict[str, Any] = {}  # snapshot prefix -> loaded Faiss index (read once, not per query)
        # (prefix, namespace) -> (catalog stamp, row -> document ordinal, ordinal -> id, id -> ordinal)
        self._rowmaps: dict[tuple[str, str], tuple[tuple[int, int], np.ndarray, list[str], dict[str, int]]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _names(namespace: str) -> tuple[str, str]:
        if namespace == "default":
            return "vectors.npy", "chunk_ids.json"
        tag = hashlib.sha256(namespace.encode()).hexdigest()[:16]
        return f"vectors.{tag}.npy", f"chunk_ids.{tag}.json"

    def _paths(self, gen: GenerationId, namespace: str) -> tuple[Path, Path]:
        folder = self._b.snapshot_dir(gen)
        vectors, ids = self._names(namespace)
        return folder / vectors, folder / ids

    def _load(self, gen: GenerationId, namespace: str) -> tuple[list[str], np.ndarray | None]:
        key = (gen.prefix(), namespace)
        if key in self._loaded:
            return self._loaded[key]
        self._b.catalog_db_exists(gen)
        vectors, ids = self._paths(gen, namespace)
        if not vectors.exists():
            return [], None
        chunk_ids = json.loads(ids.read_text())
        matrix = np.load(vectors, mmap_mode="r", allow_pickle=False)
        if matrix.shape[0] != len(chunk_ids):
            raise BackendError(KIND, "load", "vector file and id manifest disagree")
        self._loaded[key] = (chunk_ids, matrix)
        return self._loaded[key]

    def _rowmap(self, gen: GenerationId, namespace: str, chunk_ids: list[str]) -> tuple[np.ndarray, list[str], dict[str, int]]:
        """Vector row -> document ordinal, built once per generation from the catalog (and rebuilt if the
        catalog file changed). Every vector id must be a catalogued chunk."""
        key = (gen.prefix(), namespace)
        stat = self._b.catalog_path(gen).stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        cached = self._rowmaps.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1], cached[2], cached[3]
        with self._b.catalog_db(gen) as db:
            cursor = db.cursor()
            cursor.row_factory = None
            if len(chunk_ids) <= 900:
                cursor.execute(f"SELECT id, document_id FROM chunks WHERE id IN ({placeholders(chunk_ids)})", chunk_ids)
            else:
                cursor.execute("SELECT id, document_id FROM chunks")
            owner = dict(cursor.fetchall())
        doc_ids: list[str] = []
        doc_index: dict[str, int] = {}
        row_doc = np.empty(len(chunk_ids), dtype=np.int32)
        for row, cid in enumerate(chunk_ids):
            document_id = owner.get(cid)
            if document_id is None:
                raise BackendError(KIND, "search", f"vector id {cid!r} is not a catalogued chunk")
            ordinal = doc_index.get(document_id)
            if ordinal is None:
                ordinal = doc_index[document_id] = len(doc_ids)
                doc_ids.append(document_id)
            row_doc[row] = ordinal
        with self._lock:
            self._rowmaps[key] = (stamp, row_doc, doc_ids, doc_index)
        return row_doc, doc_ids, doc_index

    def forget(self, gen: GenerationId) -> None:
        for key in [k for k in self._loaded if k[0] == gen.prefix()]:
            del self._loaded[key]
        for key in [k for k in self._rowmaps if k[0] == gen.prefix()]:
            del self._rowmaps[key]
        self._faiss.pop(gen.prefix(), None)
        for key in [k for k in self._pending if k[0] == gen.prefix()]:
            del self._pending[key]

    def upsert(self, gen: GenerationId, ids: Sequence[str], vectors: np.ndarray, namespace: str = "default") -> None:
        self._b.catalog_db_exists(gen, create=True)
        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] != len(ids):
            raise BackendError(KIND, "upsert", "vectors must be a 2-D array with one row per id")
        if len(set(ids)) != len(ids):
            raise BackendError(KIND, "upsert", "duplicate ids")
        if not np.isfinite(matrix).all():
            raise BackendError(KIND, "upsert", "vectors must be finite")
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        matrix = np.divide(matrix, norms, out=np.zeros_like(matrix), where=norms > 0)
        key = (gen.prefix(), namespace)
        if key not in self._pending:
            stored_ids, stored = self._load(gen, namespace)
            self._pending[key] = (list(stored_ids), [] if stored is None else [np.asarray(stored, dtype=np.float32)], set(stored_ids))
        pending_ids, blocks, known = self._pending[key]  # ``known`` mirrors ``pending_ids`` (no per-batch rebuild)
        if blocks and blocks[0].shape[1] != matrix.shape[1]:
            raise BackendError(KIND, "upsert", "vector dimension differs from the stored vectors")
        replaced = known.intersection(ids)
        if replaced:  # rare path (an upsert over existing ids): compact, then append
            merged = np.vstack(blocks)
            keep = [i for i, cid in enumerate(pending_ids) if cid not in replaced]
            pending_ids[:] = [pending_ids[i] for i in keep]
            blocks[:] = [merged[keep]]
            known.difference_update(replaced)
        pending_ids.extend(ids)
        known.update(ids)
        blocks.append(matrix)

    def refresh(self, gen: GenerationId) -> None:
        self._b.catalog_db_exists(gen)
        for key in [k for k in self._pending if k[0] == gen.prefix()]:
            chunk_ids, blocks, _ = self._pending.pop(key)
            if not blocks:
                continue
            matrix = blocks[0] if len(blocks) == 1 else np.vstack(blocks)
            vectors, ids = self._paths(gen, key[1])
            np.save(vectors, np.ascontiguousarray(matrix), allow_pickle=False)
            ids.write_text(json.dumps(chunk_ids))
            self._loaded.pop(key, None)
            self._rowmaps.pop(key, None)
            self._faiss.pop(key[0], None)
            if self._b.vector_engine == "faiss" and key[1] == "default":
                import faiss

                index = faiss.IndexFlatIP(matrix.shape[1])
                index.add(np.ascontiguousarray(matrix))
                faiss.write_index(index, str(vectors.parent / "vectors.faiss"))

    def count(self, gen: GenerationId, namespace: str = "default") -> int:
        chunk_ids, _ = self._load(gen, namespace)
        return len(chunk_ids)

    def search(
        self, gen: GenerationId, vector: np.ndarray, scope: Scope | None, k: int, namespace: str = "default"
    ) -> list[ScoredChunk]:
        """Exact top-``k`` inner product over the eligible rows (the scope is a row bitset applied
        before the top-k, so the depth never has to grow)."""
        chunk_ids, matrix = self._load(gen, namespace)
        if matrix is None or not chunk_ids:
            return []
        query = np.asarray(vector, dtype=np.float32)
        if query.shape != (matrix.shape[1],):
            raise BackendError(KIND, "search", "query vector dimension mismatch")
        norm = np.linalg.norm(query)
        if norm == 0:
            return []
        query = query / norm
        row_doc, doc_ids, doc_index = self._rowmap(gen, namespace, chunk_ids)
        documents = self._b.document_mask(gen, scope, doc_index, len(doc_ids))
        eligible = None if documents is None else documents[row_doc]
        depth = min(k, len(chunk_ids) if eligible is None else int(eligible.sum()))
        if depth <= 0:
            return []
        if self._b.vector_engine == "faiss" and namespace == "default":
            index = self._faiss_index(gen)
            if eligible is None:
                values, positions = index.search(query[None, :], depth)
            else:
                import faiss

                bitmap = np.packbits(eligible, bitorder="little")
                selector = faiss.IDSelectorBitmap(len(bitmap), faiss.swig_ptr(bitmap))
                values, positions = index.search(query[None, :], depth, params=faiss.SearchParameters(sel=selector))
            pairs = [(int(p), float(v)) for p, v in zip(positions[0], values[0]) if p >= 0]
        else:
            raw = np.asarray(matrix) @ query
            pairs = [(int(r), float(raw[r])) for r in top_k(raw, eligible, depth)]
        return [ScoredChunk(chunk_id=chunk_ids[r], document_id=doc_ids[int(row_doc[r])], score=score) for r, score in pairs]

    def _faiss_index(self, gen: GenerationId) -> Any:
        prefix = gen.prefix()
        if prefix not in self._faiss:
            import faiss

            with self._lock:
                if prefix not in self._faiss:
                    self._faiss[prefix] = faiss.read_index(str(self._b.snapshot_dir(gen) / "vectors.faiss"))
        return self._faiss[prefix]
