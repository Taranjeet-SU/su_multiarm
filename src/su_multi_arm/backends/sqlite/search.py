"""SQLite ``LexicalIndex`` (FTS5 ``unicode61``, phrase, boolean) and ``VectorIndex`` (exact NumPy or
Faiss over per-generation files) facets."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

import numpy as np

from ...core.errors import BackendError
from ...core.types import Chunk, GenerationId, LexicalQuery, Page, PageResult, Scope, ScoredChunk
from ._db import KIND, placeholders, scope_sql
from .catalog import page_window

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import SqliteBackend

MAX_TERMS = 128


def tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def _quoted(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def fts_expression(query: LexicalQuery) -> str | None:
    """Fixed FTS5 templates; every user term is quoted, never interpreted as syntax."""
    terms = list(dict.fromkeys(tokens(query.text)))[:MAX_TERMS]
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


class SqliteLexical:
    def __init__(self, backend: SqliteBackend):
        self._b = backend

    def index(self, gen: GenerationId, chunks: Sequence[Chunk], fields: Sequence[str]) -> None:
        with self._b.catalog_db(gen, create=True) as db:
            for chunk in chunks:
                if fields and chunk.field not in fields:
                    continue
                row = db.execute("SELECT json_extract(data, '$.title') AS title FROM documents WHERE id=?", (chunk.document_id,)).fetchone()
                if row is None:
                    raise BackendError(KIND, "index", f"chunk {chunk.id} references unknown document")
                db.execute(
                    "INSERT OR IGNORE INTO chunks VALUES (?,?,?,?,?,?,?)",
                    (chunk.id, chunk.document_id, chunk.field, chunk.start, chunk.end, chunk.text, chunk.recipe),
                )
                db.execute("DELETE FROM fts WHERE chunk_id=?", (chunk.id,))
                db.execute("INSERT INTO fts VALUES (?,?,?)", (chunk.id, row["title"] or "", chunk.text))

    def refresh(self, gen: GenerationId) -> None:
        self._b.catalog_db_exists(gen)  # writes are durable at commit; refresh only checks the generation

    def count(self, gen: GenerationId) -> int:
        with self._b.catalog_db(gen) as db:
            return int(db.execute("SELECT count(*) FROM fts").fetchone()[0])

    def search(
        self, gen: GenerationId, query: LexicalQuery, scope: Scope | None, k: int, page: Page | None = None
    ) -> PageResult[ScoredChunk]:
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


class SqliteVector:
    def __init__(self, backend: SqliteBackend):
        self._b = backend
        self._pending: dict[tuple[str, str], tuple[list[str], np.ndarray]] = {}
        self._loaded: dict[tuple[str, str], tuple[list[str], np.ndarray]] = {}

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

    def forget(self, gen: GenerationId) -> None:
        for key in [k for k in self._loaded if k[0] == gen.prefix()]:
            del self._loaded[key]
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
        existing_ids, existing = self._pending.get(key, self._load(gen, namespace))
        if existing is None:
            existing_ids, existing = [], np.zeros((0, matrix.shape[1]), dtype=np.float32)
        if existing.shape[1] != matrix.shape[1]:
            raise BackendError(KIND, "upsert", "vector dimension differs from the stored vectors")
        replaced = set(ids)
        keep = [i for i, cid in enumerate(existing_ids) if cid not in replaced]
        merged_ids = [existing_ids[i] for i in keep] + list(ids)
        merged = np.vstack([np.asarray(existing)[keep], matrix]) if keep else matrix
        self._pending[key] = (merged_ids, merged)

    def refresh(self, gen: GenerationId) -> None:
        self._b.catalog_db_exists(gen)
        for key in [k for k in self._pending if k[0] == gen.prefix()]:
            chunk_ids, matrix = self._pending.pop(key)
            vectors, ids = self._paths(gen, key[1])
            np.save(vectors, np.ascontiguousarray(matrix), allow_pickle=False)
            ids.write_text(json.dumps(chunk_ids))
            self._loaded.pop(key, None)
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
        if self._b.vector_engine == "faiss" and namespace == "default":
            import faiss

            index = faiss.read_index(str(self._b.snapshot_dir(gen) / "vectors.faiss"))
            scores_, positions = index.search(query[None, :], len(chunk_ids))
            order = [int(p) for p in positions[0] if p >= 0]
            scores = np.zeros(len(chunk_ids), dtype=np.float32)
            scores[order] = scores_[0][: len(order)]
        else:
            scores = np.asarray(matrix) @ query
            order = [int(i) for i in np.argsort(-scores, kind="stable")]
        hits: list[ScoredChunk] = []
        batch = max(16, 2 * k)
        with self._b.catalog_db(gen) as db:
            for start in range(0, len(order), batch):
                window = order[start : start + batch]
                ids = [chunk_ids[i] for i in window]
                rows = db.execute(f"SELECT id, document_id FROM chunks WHERE id IN ({placeholders(ids)})", ids).fetchall()
                documents = {r["id"]: r["document_id"] for r in rows}
                for cid in ids:
                    if cid not in documents:
                        raise BackendError(KIND, "search", f"vector id {cid!r} is not a catalogued chunk")
                candidates = [(cid, documents[cid]) for cid in ids]
                doc_ids = list(dict.fromkeys(d for _, d in candidates))
                where, params = scope_sql("d.id", scope)
                eligible = {
                    r["id"]
                    for r in db.execute(f"SELECT d.id FROM documents d WHERE d.id IN ({placeholders(doc_ids)}) AND {where}", [*doc_ids, *params])
                } if scope is not None else set(doc_ids)
                for index, (cid, document_id) in zip(window, candidates):
                    if document_id in eligible:
                        hits.append(ScoredChunk(chunk_id=cid, document_id=document_id, score=float(scores[index])))
                        if len(hits) >= k:
                            return hits
        return hits
