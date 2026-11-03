"""PostgreSQL ``LexicalIndex`` (full-text search) and ``VectorIndex`` (pgvector) facets.

**Lexical analyzer semantics.** Chunks are analysed with the generation's text search configuration
(``text_search_config``, default ``english``: Snowball stemming, stop words removed), recorded in the
``generations`` row at creation and reported as ``analyzer_id = pg_ts_<config>``; a backend whose
configured analyzer changed still searches an old generation with the analyzer it was built with.
Title and body are separate ``tsvector`` columns. Scores are ``2 * ts_rank_cd(title) +
ts_rank_cd(body)`` (cover density, not BM25) and are not comparable with FTS5, MongoDB or Lucene
scores; the conformance kit asserts membership, scope, determinism, phrase/boolean semantics and
paging only (plan §3.5). Engine differences that follow from the analyzer: stop words never match
(a ``must`` made only of stop words matches nothing; FTS5 ``unicode61`` has no stop words), and
stemming conflates inflections that FTS5 keeps apart.

**Vector.** Vectors are L2-normalised at upsert (a zero vector stays zero) and ranked by inner
product, so scores are cosine similarities as in the sqlite adapter. Default search is **exact**: a
sequential scan with the scope pushed into the ``WHERE`` clause, ordered by ``(distance, chunk_id)``.
``vector_index: hnsw`` is opt-in: ``refresh`` builds one partial HNSW index per namespace
(``vector_ip_ops`` over ``embedding::vector(d)``) and then runs the filter-recall gate (HNSW top-k
versus exact top-k on sampled stored vectors, unfiltered and with a document allow-list); a recall
below ``min_filter_recall`` fails the refresh with a typed error, so a build with a lossy index never
activates. Searches use ``hnsw.iterative_scan = strict_order`` (pgvector >= 0.8) so filtered
searches keep scanning instead of returning short.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

import numpy as np

from ...core.errors import BackendError
from ...core.types import Chunk, GenerationId, LexicalQuery, Page, PageResult, Scope, ScoredChunk
from ._db import C, KIND, ident, page_window, scope_sql, tsquery_parts, vector_ddl, vector_literal
from .catalog import document_title, insert_chunks, known_documents

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import PostgresBackend

MAX_DIMENSIONS_EXACT = 16000  # pgvector's vector type limit
MAX_DIMENSIONS_HNSW = 2000  # pgvector's HNSW limit for vector


@dataclass(frozen=True)
class HnswSettings:
    m: int = 16
    ef_construction: int = 64
    ef_search: int = 100
    min_filter_recall: float = 0.95
    sample: int = 32
    k: int = 10


def _match_sql(parts: dict[str, Any], cfg_param: str) -> tuple[str, list[Any], str, list[Any]]:
    """``(where, params, rank_query, rank_params)`` from ``tsquery_parts``; every value bound."""
    def both(expr: str) -> str:
        return f"(l.title_tsv @@ {expr} OR l.body_tsv @@ {expr})"

    if parts["kind"] == "phrase":
        tq = f"phraseto_tsquery({cfg_param}, %s)"
        return both(tq), [parts["phrase"], parts["phrase"]], tq, [parts["phrase"]]
    tq = "(" + " || ".join(f"plainto_tsquery({cfg_param}, %s)" for _ in parts["terms"]) + ")"
    clauses, params = [both(tq)], [*parts["terms"], *parts["terms"]]
    for must in parts["must"]:
        clauses.append(both(f"phraseto_tsquery({cfg_param}, %s)"))
        params += [must, must]
    for must_not in parts["must_not"]:
        clauses.append("NOT " + both(f"phraseto_tsquery({cfg_param}, %s)"))
        params += [must_not, must_not]
    return " AND ".join(clauses), params, tq, list(parts["terms"])


class PostgresLexical:
    def __init__(self, backend: PostgresBackend):
        self._b = backend

    def index(self, gen: GenerationId, chunks: Sequence[Chunk], fields: Sequence[str]) -> None:
        selected = [c for c in chunks if not fields or c.field in fields]
        with self._b.tx("index") as cur:
            s = self._b.require_generation(cur, gen, create=True)
            if not selected:
                return
            cfg = self._b.generation_info(cur, gen)["text_search_config"]
            docs = known_documents(cur, s, sorted({c.document_id for c in selected}))
            for chunk in selected:
                if chunk.document_id not in docs:
                    raise BackendError(KIND, "index", f"chunk {chunk.id} references unknown document")
            insert_chunks(cur, s, selected, replace=False)
            cur.executemany(
                f"INSERT INTO {s}.lexical(chunk_id, document_id, field, title_tsv, body_tsv) "
                "VALUES (%s,%s,%s,to_tsvector(%s::regconfig, %s),to_tsvector(%s::regconfig, %s)) "
                "ON CONFLICT (chunk_id) DO UPDATE SET document_id=EXCLUDED.document_id, field=EXCLUDED.field, "
                "title_tsv=EXCLUDED.title_tsv, body_tsv=EXCLUDED.body_tsv",
                [(c.id, c.document_id, c.field, cfg, document_title(docs[c.document_id]), cfg, c.text) for c in selected],
            )

    def refresh(self, gen: GenerationId) -> None:
        """Committed writes are visible; refresh verifies the generation exists."""
        with self._b.tx("refresh") as cur:
            self._b.require_generation(cur, gen)

    def count(self, gen: GenerationId) -> int:
        with self._b.tx("count") as cur:
            s = self._b.require_generation(cur, gen)
            cur.execute(f"SELECT count(*) FROM {s}.lexical")
            return int(cur.fetchone()[0])

    def search(
        self, gen: GenerationId, query: LexicalQuery, scope: Scope | None, k: int, page: Page | None = None
    ) -> PageResult[ScoredChunk]:
        parts = tsquery_parts(query)
        offset, size = page_window(page, k)
        with self._b.tx("search") as cur:
            info = self._b.generation_info(cur, gen)
            if parts is None:
                return PageResult(items=[], next_cursor=None)
            s, cfg = info["schema"], info["text_search_config"]
            match, match_params, tq, tq_params = _match_sql(parts, "%s::regconfig")
            # Interleave the configuration parameter in front of every tsquery function's text.
            match_params = _with_cfg(match_params, cfg)
            tq_params = _with_cfg(tq_params, cfg)
            where, scope_params = scope_sql(s, "l.document_id", scope)
            field_clause, field_params = "", []
            if query.fields:
                field_clause, field_params = " AND l.field = ANY(%s)", [list(query.fields)]
            window = min(size, max(0, k - offset))
            cur.execute(
                f"WITH q AS (SELECT {tq} AS tq) "
                f"SELECT l.chunk_id, l.document_id, 2 * ts_rank_cd(l.title_tsv, q.tq) + ts_rank_cd(l.body_tsv, q.tq) AS score "
                f"FROM {s}.lexical l, q WHERE {match} AND {where}{field_clause} "
                f"ORDER BY score DESC, l.chunk_id {C} LIMIT %s OFFSET %s",
                [*tq_params, *match_params, *scope_params, *field_params, window + 1, offset],
            )
            rows = cur.fetchall()
        kept = rows[:window]
        items = [ScoredChunk(chunk_id=r[0], document_id=r[1], score=float(r[2])) for r in kept]
        more = len(rows) > len(kept) and offset + len(kept) < k
        return PageResult(items=items, next_cursor=str(offset + len(kept)) if more else None)


def _with_cfg(params: list[Any], cfg: str) -> list[Any]:
    out: list[Any] = []
    for value in params:
        out += [cfg, value]
    return out


def _index_name(namespace: str) -> str:
    return "vectors_hnsw_" + hashlib.sha256(namespace.encode()).hexdigest()[:16]


class PostgresVector:
    def __init__(self, backend: PostgresBackend):
        self._b = backend

    def _space(self, cur: Any, s: str, namespace: str) -> int | None:
        cur.execute("SELECT to_regclass(%s)", (f"{s}.vector_spaces",))
        if cur.fetchone()[0] is None:
            return None
        cur.execute(f"SELECT dims FROM {s}.vector_spaces WHERE namespace = %s", (namespace,))
        row = cur.fetchone()
        return None if row is None else int(row[0])

    def upsert(self, gen: GenerationId, ids: Sequence[str], vectors: np.ndarray, namespace: str = "default") -> None:
        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] != len(ids):
            raise BackendError(KIND, "upsert", "vectors must be a 2-D array with one row per id")
        if len(set(ids)) != len(ids):
            raise BackendError(KIND, "upsert", "duplicate ids")
        if not np.isfinite(matrix).all():
            raise BackendError(KIND, "upsert", "vectors must be finite")
        limit = MAX_DIMENSIONS_HNSW if self._b.vector_index == "hnsw" else MAX_DIMENSIONS_EXACT
        if matrix.shape[1] > limit or matrix.shape[1] < 1:
            raise BackendError(KIND, "upsert", f"{matrix.shape[1]} dimensions; pgvector {self._b.vector_index} supports 1..{limit}")
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        matrix = np.divide(matrix, norms, out=np.zeros_like(matrix), where=norms > 0)
        with self._b.tx("upsert") as cur:
            s = self._b.require_generation(cur, gen, create=True)
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            for statement in vector_ddl(s):
                cur.execute(statement)
            dims = self._space(cur, s, namespace)
            if dims is None:
                cur.execute(f"INSERT INTO {s}.vector_spaces(namespace, dims) VALUES (%s,%s)", (namespace, matrix.shape[1]))
            elif dims != matrix.shape[1]:
                raise BackendError(KIND, "upsert", "vector dimension differs from the stored vectors")
            cur.execute(f"SELECT id, document_id FROM {s}.chunks WHERE id = ANY(%s)", (list(ids),))
            documents = dict(cur.fetchall())
            for cid in ids:
                if cid not in documents:
                    raise BackendError(KIND, "upsert", f"vector id {cid!r} is not a catalogued chunk")
            cur.executemany(
                f"INSERT INTO {s}.vectors(namespace, chunk_id, document_id, embedding) VALUES (%s,%s,%s,%s::vector) "
                "ON CONFLICT (namespace, chunk_id) DO UPDATE SET document_id=EXCLUDED.document_id, embedding=EXCLUDED.embedding",
                [(namespace, cid, documents[cid], vector_literal(row)) for cid, row in zip(ids, matrix)],
            )

    def refresh(self, gen: GenerationId) -> None:
        """Exact: committed writes are visible, nothing to build. HNSW: build the per-namespace
        indexes, then enforce the filter-recall gate (typed error below ``min_filter_recall``)."""
        with self._b.tx("refresh") as cur:
            s = self._b.require_generation(cur, gen)
            if self._b.vector_index != "hnsw":
                return
            cur.execute("SELECT to_regclass(%s)", (f"{s}.vector_spaces",))
            if cur.fetchone()[0] is None:
                return
            cur.execute(f"SELECT namespace, dims FROM {s}.vector_spaces ORDER BY namespace {C}")
            spaces = cur.fetchall()
            settings = self._b.hnsw
            for namespace, dims in spaces:
                literal = "'" + namespace.replace("'", "''") + "'"
                cur.execute(
                    f"CREATE INDEX IF NOT EXISTS {ident(_index_name(namespace))} ON {s}.vectors "
                    f"USING hnsw ((embedding::vector({int(dims)})) vector_ip_ops) "
                    f"WITH (m = {int(settings.m)}, ef_construction = {int(settings.ef_construction)}) WHERE namespace = {literal}"
                )
            cur.execute(f"ANALYZE {s}.vectors")
        for namespace, _dims in spaces:
            recall = self.filter_recall(gen, namespace)
            if recall["recall"] < self._b.hnsw.min_filter_recall:
                raise BackendError(
                    KIND,
                    "refresh",
                    f"HNSW filter recall {recall['recall']:.3f} < min_filter_recall {self._b.hnsw.min_filter_recall} "
                    f"(namespace {namespace!r}, {recall['queries']} queries); use vector.index: exact or raise ef_search",
                )

    def count(self, gen: GenerationId, namespace: str = "default") -> int:
        with self._b.tx("count") as cur:
            s = self._b.require_generation(cur, gen)
            if self._space(cur, s, namespace) is None:
                return 0
            cur.execute(f"SELECT count(*) FROM {s}.vectors WHERE namespace = %s", (namespace,))
            return int(cur.fetchone()[0])

    def search(
        self, gen: GenerationId, vector: np.ndarray, scope: Scope | None, k: int, namespace: str = "default"
    ) -> list[ScoredChunk]:
        return self._search(gen, vector, scope, k, namespace, approximate=self._b.vector_index == "hnsw")

    def _search(
        self, gen: GenerationId, vector: np.ndarray, scope: Scope | None, k: int, namespace: str, approximate: bool, force_index: bool = False
    ) -> list[ScoredChunk]:
        if k < 1:
            raise BackendError(KIND, "search", "k must be positive")
        with self._b.tx("search") as cur:
            s = self._b.require_generation(cur, gen)
            dims = self._space(cur, s, namespace)
            if dims is None:
                return []
            query = np.asarray(vector, dtype=np.float32)
            if query.shape != (dims,):
                raise BackendError(KIND, "search", "query vector dimension mismatch")
            norm = float(np.linalg.norm(query))
            if norm == 0 or not np.isfinite(norm):
                return []
            literal = vector_literal(query / norm)
            where, params = scope_sql(s, "v.document_id", scope)
            if approximate:
                settings = self._b.hnsw
                cur.execute(f"SET LOCAL hnsw.ef_search = {int(settings.ef_search)}")
                cur.execute("SET LOCAL hnsw.iterative_scan = strict_order")
                if force_index:  # recall gate: measure the index itself, not a plan the optimiser made exact
                    for flag in ("enable_seqscan", "enable_bitmapscan", "enable_sort"):
                        cur.execute(f"SET LOCAL {flag} = off")
                ns = "'" + namespace.replace("'", "''") + "'"  # literal: the partial index predicate must match
                cur.execute(
                    f"SELECT chunk_id, document_id, score FROM (SELECT v.chunk_id, v.document_id, "
                    f"-((v.embedding::vector({dims})) <#> %s::vector({dims})) AS score FROM {s}.vectors v "
                    f"WHERE v.namespace = {ns} AND {where} ORDER BY (v.embedding::vector({dims})) <#> %s::vector({dims}) LIMIT %s) t "
                    f"ORDER BY score DESC, chunk_id {C}",
                    [literal, *params, literal, k],
                )
            else:
                cur.execute(
                    f"SELECT v.chunk_id, v.document_id, -(v.embedding <#> %s::vector) AS score FROM {s}.vectors v "
                    f"WHERE v.namespace = %s AND {where} ORDER BY v.embedding <#> %s::vector, v.chunk_id {C} LIMIT %s",
                    [literal, namespace, *params, literal, k],
                )
            rows = cur.fetchall()
        return [ScoredChunk(chunk_id=r[0], document_id=r[1], score=float(r[2])) for r in rows]

    def filter_recall(self, gen: GenerationId, namespace: str = "default") -> dict[str, Any]:
        """Mean recall@k of the HNSW index against exact search on up to ``sample`` stored vectors used
        as queries, unfiltered and restricted to every other sampled document (the filter case). The
        index path is forced (sequential, bitmap and sort plans disabled), so the figure is a lower
        bound for searches where the optimiser may still choose an exact plan."""
        settings = self._b.hnsw
        with self._b.tx("filter_recall") as cur:
            s = self._b.require_generation(cur, gen)
            cur.execute(
                f"SELECT chunk_id, document_id, embedding::text FROM {s}.vectors WHERE namespace = %s ORDER BY chunk_id {C} LIMIT %s",
                (namespace, settings.sample),
            )
            sample = cur.fetchall()
        if not sample:
            return {"recall": 1.0, "queries": 0}
        allowed = sorted({r[1] for r in sample})[::2]
        scores: list[float] = []
        for _cid, _doc, text in sample:
            query = np.array([float(x) for x in text.strip("[]").split(",")], dtype=np.float32)
            for scope in (None, Scope(document_ids=allowed)):
                exact = {h.chunk_id for h in self._search(gen, query, scope, settings.k, namespace, approximate=False)}
                if not exact:
                    continue
                approx = {h.chunk_id for h in self._search(gen, query, scope, settings.k, namespace, approximate=True, force_index=True)}
                scores.append(len(exact & approx) / len(exact))
        return {"recall": float(np.mean(scores)) if scores else 1.0, "queries": len(scores)}
