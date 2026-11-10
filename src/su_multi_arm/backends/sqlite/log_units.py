"""Serving the live log member's units in a partitioned collection (docs/plans/unified_merge.md §6.5,
§6.7; lane LB wiring of M2a into M1a's ``backends/sqlite/members.py``).

A live log member (``collection.members.<id>.cadence: minutes``) is served from **unit-format**
units (``ingestion/log_slices.py``): its archive day/month and catalog members
(``ingestion/log_members.py``) and the provisional slices the request pin names. Together they are
the member's one **log stratum**:

* **served rows** are decided by sealed versions (``query_stats.ServedRows``: the highest version
  per id among the pinned units; a newer tombstone hides older rows). The rows a unit holds but does
  not serve are its ``hidden`` ids; the partitioned facets exclude them from candidates, records,
  counts, hit reads and listings exactly as they exclude id-directory shadowed rows of write-path
  members. ``owners`` routes ``get`` of a log id to the unit serving it (no id-directory lookup:
  the versions are authoritative for the stratum; the directory guards uniqueness and compose);
* **lexical**: ``QueryTimeScorer`` (query-time ``N``, token total and query-term ``df`` over the
  served rows of every pinned unit; the epsilon floor's declared ``average_idf_lag``), with the
  request scope, field restriction, phrase and boolean queries applied as candidate eligibility —
  the semantics of the sqlite bm25 search (``backends/sqlite/search.py``) over one store;
* **dense**: ``DenseLogScorer`` (exact flat top-k over served embed rows; vectors from the
  text-hash store); a unit whose vectors are not all stored is skipped and reported ``pending``;
* **catalog / records**: each unit's ``catalog.sqlite`` served by today's ``SqliteCatalog`` and
  ``SqliteRecords`` SQL (``UnitStore``), records read their values from ``document_fields``.

Caches are keyed by unit id + seal (``LogUnits``) and by the pinned unit set (``ServedRows``), never
by generation: a new slice warms only itself; ``warm`` pre-loads a newly registered unit (the
compaction hook, so the first dense query after a base compaction does not load a cold matrix).
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections import Counter, OrderedDict
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from ...core.errors import BackendError
from ...core.types import GenerationId, LexicalQuery, MetadataSchema, Page, PageResult, Predicate, Scope, ScopePredicates, ScoredChunk
from ...core.types_members import PinnedView
from ...ingestion.log_slices import UNIT_CATALOG, LogSliceLedger, UnitReader
from ._db import KIND, connect, predicate_sql, scope_sql
from .bm25 import contains_sequence
from .catalog import SqliteCatalog, SqliteRecords, page_window
from .query_stats import DenseLogScorer, LogUnits, QueryTimeScorer, ServedRows

MAX_VIEWS = 32


class UnitCatalog(SqliteCatalog):
    """``SqliteCatalog`` over a unit's ``catalog.sqlite``; an exclusion list (hidden rows of a pinned
    view, possibly thousands of ids) is bound through a temporary table instead of an ``IN`` list."""

    def iter_scope(self, gen: GenerationId, scope: Scope | None, page: Page) -> PageResult:
        if scope is None or len(scope.exclude_ids) <= 500:
            return super().iter_scope(gen, scope, page)
        from ...core.types import Document

        offset, size = page_window(page, 50)
        where, params = scope_sql("d.id", Scope(predicates=list(scope.predicates), document_ids=scope.document_ids))
        with self._b.catalog_db(gen) as db:
            db.execute("CREATE TEMP TABLE IF NOT EXISTS _excluded(id TEXT PRIMARY KEY)")
            db.execute("DELETE FROM _excluded")
            db.executemany("INSERT OR IGNORE INTO _excluded VALUES (?)", [(i,) for i in scope.exclude_ids])
            rows = db.execute(
                f"SELECT d.data FROM documents d WHERE {where} AND d.id NOT IN (SELECT id FROM _excluded) ORDER BY d.id LIMIT ? OFFSET ?", [*params, size + 1, offset]
            ).fetchall()
        items = [Document.model_validate_json(r["data"]) for r in rows[:size]]
        return PageResult(items=items, next_cursor=str(offset + size) if len(rows) > size else None)


class UnitRecords(SqliteRecords):
    """``SqliteRecords`` over a unit's ``catalog.sqlite``: one record per document whose values are the
    document's (``document_fields``), typed by the serving configuration's metadata schema."""

    def _schema(self, db) -> MetadataSchema:
        return self._b.schema

    def _where(self, schema: MetadataSchema, predicates: Sequence[Predicate]) -> tuple[str, list[Any]]:
        clauses, params = ["1"], []
        for predicate in predicates:
            sql, extra = predicate_sql("document_fields", "document_id", "r.id", predicate, self._casefold(schema, predicate.field), schema.field(predicate.field))
            clauses.append(sql)
            params.extend(extra)
        return " AND ".join(clauses), params

    def values(self, gen: GenerationId, field: str, acl: ScopePredicates, limit: int) -> list[str]:
        schema = self._b.schema
        spec = schema.field(field)
        if spec is None or not spec.listable:
            raise BackendError(KIND, "values", f"field {field!r} is not listable")
        where, params = self._where(schema, acl)
        with self._b.catalog_db(gen) as db:
            rows = db.execute(
                f"SELECT DISTINCT f.text_value, f.num_value, f.kind FROM document_fields f JOIN records r ON r.id=f.document_id "
                f"WHERE f.field=? AND f.is_null=0 AND {where}",
                [field, *params],
            ).fetchall()
        seen = set()
        for row in rows:
            if row["kind"] in {"integer", "number"}:
                number = row["num_value"]
                seen.add(str(int(number)) if row["kind"] == "integer" else str(number))
            else:
                seen.add(str(row["text_value"]))
        return sorted(seen)[: max(0, limit)]


class UnitStore:
    """The sqlite catalog facets of one unit (read-only): ``catalog`` and ``records`` with the sqlite
    facets' SQL. ``gen`` arguments are ignored (a unit is one immutable sealed build)."""

    kind = KIND

    def __init__(self, folder: Path, *, timeout: float, stats: Any, schema: MetadataSchema, max_matched: int):
        self.folder = Path(folder)
        self.path = self.folder / UNIT_CATALOG
        if not self.path.exists():
            raise BackendError(KIND, "member", f"log unit {self.folder.name} has no {UNIT_CATALOG} (built before the unit catalog; rebuild it)")
        self.timeout, self.stats, self.schema, self.max_matched = timeout, stats, schema, max_matched
        self.catalog = UnitCatalog(self)
        self.records = UnitRecords(self)

    @contextmanager
    def catalog_db(self, gen: Any = None, create: bool = False) -> Iterator[sqlite3.Connection]:
        if create:
            raise BackendError(KIND, "write", "a sealed log unit is read-only")
        try:
            with connect(self.path, self.timeout, readonly=True) as db:
                yield db
        except sqlite3.Error as exc:
            raise BackendError(KIND, "catalog", str(exc)) from exc

    def allowed(self, scope: Scope | None) -> set[str] | None:
        """Document ids of this unit inside ``scope`` (``None``: every document)."""
        if scope is None or (scope.document_ids is None and not scope.exclude_ids and not scope.predicates):
            return None
        if not scope.predicates:
            return None if scope.document_ids is None else set(scope.document_ids)
        where, params = scope_sql("d.id", Scope(predicates=list(scope.predicates), document_ids=scope.document_ids))
        with self.catalog_db() as db:
            cursor = db.cursor()
            cursor.row_factory = None
            return {r[0] for r in cursor.execute(f"SELECT d.id FROM documents d WHERE {where}", params)}

    def chunk_texts(self, chunk_ids: Sequence[str]) -> dict[str, tuple[str, str]]:
        """``chunk id -> (text, document title)``."""
        out: dict[str, tuple[str, str]] = {}
        with self.catalog_db() as db:
            cursor = db.cursor()
            cursor.row_factory = None
            for start in range(0, len(chunk_ids), 500):
                ids = list(chunk_ids[start : start + 500])
                marks = ",".join("?" * len(ids))
                for cid, text, title in cursor.execute(
                    f"SELECT c.id, c.text, json_extract(d.data, '$.title') FROM chunks c JOIN documents d ON d.id=c.document_id WHERE c.id IN ({marks})", ids
                ):
                    out[cid] = (text or "", title or "")
        return out


class LogStratum:
    """The serving side of one live log member (module doc). ``registry`` is the member registry root
    (archive/catalog unit locations are relative to it); ``store`` the text-hash vector store (None:
    the stratum has no dense facet)."""

    def __init__(self, ledger: LogSliceLedger, registry: Path, *, store: Any = None, schema: MetadataSchema | None = None, timeout: float = 30.0,
                 stats: Any = None, max_matched: int = 10_000, average_idf: Any = None):
        self.ledger = ledger
        self.member = ledger.member
        self.units = LogUnits(ledger, registry)
        self.served = ServedRows(max_views=MAX_VIEWS)
        self.lexical = QueryTimeScorer(ledger.member, self.units, served=self.served, average_idf=average_idf)
        self.store = store
        self.dense = DenseLogScorer(ledger.member, self.units, store, encode_query=lambda _q: None, served=self.served) if store is not None else None
        self.schema = schema or MetadataSchema()
        self.timeout, self.max_matched = timeout, max_matched
        self.stats = stats if stats is not None else Counter()
        self._stores: OrderedDict[str, UnitStore] = OrderedDict()
        self._derived: OrderedDict[tuple[str, ...], dict[str, Any]] = OrderedDict()
        self._lock = threading.RLock()
        self.pending: dict[str, tuple[str, ...]] = {}  # view key -> units whose vectors are pending (last dense read)

    # --- units and served rows ---------------------------------------------------------------------

    def readers(self, view: PinnedView) -> list[UnitReader]:
        return self.units.readers(view)

    @staticmethod
    def key(readers: Sequence[UnitReader]) -> tuple[str, ...]:
        return tuple(f"{r.unit_id}@{r.seal}" for r in readers)

    def unit_store(self, reader: UnitReader) -> UnitStore:
        key = f"{reader.unit_id}@{reader.seal}"
        with self._lock:
            found = self._stores.get(key)
            if found is not None:
                self._stores.move_to_end(key)
                return found
            store = UnitStore(reader.folder, timeout=self.timeout, stats=self.stats, schema=self.schema, max_matched=self.max_matched)
            self._stores[key] = store
            while len(self._stores) > 1024:
                self._stores.popitem(last=False)
            return store

    def derived(self, view: PinnedView) -> dict[str, Any]:
        """``{"readers", "masks", "hidden": {unit: ids}}`` of the pinned set (``owners`` is built on
        first use: only id routing needs it, never a search)."""
        readers = self.readers(view)
        key = self.key(readers)
        with self._lock:
            found = self._derived.get(key)
            if found is not None:
                self._derived.move_to_end(key)
                return found
        masks = self.served.masks(readers)
        hidden: dict[str, frozenset[str]] = {}
        for reader in readers:
            doc_mask = masks[reader.unit_id][1]
            ids = reader.ids
            hidden[reader.unit_id] = frozenset(ids[i] for i in np.flatnonzero(~doc_mask))
        value = {"readers": readers, "masks": masks, "hidden": hidden, "owners": None, "key": key}
        with self._lock:
            self._derived[key] = value
            while len(self._derived) > MAX_VIEWS:
                self._derived.popitem(last=False)
        return value

    def hidden(self, view: PinnedView, unit_id: str) -> frozenset[str]:
        return self.derived(view)["hidden"].get(unit_id, frozenset())

    def owners(self, view: PinnedView) -> dict[str, str]:
        """``{served doc id: unit id}`` of the pinned set (built once per pinned set)."""
        derived = self.derived(view)
        owners = derived["owners"]
        if owners is None:
            owners = {}
            for reader in derived["readers"]:
                ids = reader.ids
                for i in np.flatnonzero(derived["masks"][reader.unit_id][1]):
                    owners[ids[i]] = reader.unit_id
            with self._lock:
                derived["owners"] = owners
        return owners

    # --- candidate eligibility -----------------------------------------------------------------------

    def _doc_allowed(self, reader: UnitReader, scope: Scope | None) -> np.ndarray | None:
        """Doc mask over ``reader.ids`` of the scope (``None``: unrestricted)."""
        allowed = self.unit_store(reader).allowed(scope)
        exclude = set(scope.exclude_ids) if scope is not None else set()
        if allowed is None and not exclude:
            return None
        return np.fromiter(((allowed is None or d in allowed) and d not in exclude for d in reader.ids), dtype=bool, count=len(reader.ids))

    @staticmethod
    def _row_mask(reader: UnitReader, doc_mask: np.ndarray | None) -> np.ndarray | None:
        if doc_mask is None:
            return None
        segment = reader.segment
        index = {d: i for i, d in enumerate(reader.ids)}
        seg_docs = np.fromiter((bool(doc_mask[index[d]]) for d in segment.doc_ids), dtype=bool, count=len(segment.doc_ids))
        return seg_docs[np.asarray(segment.row_doc)] if len(segment.doc_ids) else np.zeros(segment.corpus_size, dtype=bool)

    def _containing(self, reader: UnitReader, words: Sequence[str]) -> np.ndarray:
        found = reader.term_ids(words)
        if not words or any(w not in found for w in words):
            return np.zeros(0, dtype=np.int64)
        return reader.segment.contains_all([found[w] for w in words]).astype(np.int64)

    def _verified(self, reader: UnitReader, words: Sequence[str], rows: np.ndarray) -> np.ndarray:
        if not len(rows):
            return rows
        analyze = self.lexical.analyze
        segment = reader.segment
        ids = [segment.chunk_ids[int(r)] for r in rows]
        texts = self.unit_store(reader).chunk_texts(ids)
        with_title = bool(self.ledger.bm25.include_title)
        keep = []
        for cid in ids:
            text, title = texts.get(cid, ("", ""))
            parts = [analyze(text)]
            if with_title and title:
                parts.append(analyze(title))
            keep.append(any(contains_sequence(part, words) for part in parts))
        return rows[np.asarray(keep, dtype=bool)]

    def _restrict(self, reader: UnitReader, query: LexicalQuery, words: Sequence[str], unindexable: Any) -> np.ndarray | None:
        """Row restriction of a ``phrase`` / ``boolean`` query (``None``: none)."""
        segment = reader.segment
        if query.kind == "phrase":
            if unindexable(re.findall(r"\w+", query.text)):
                return np.zeros(segment.corpus_size, dtype=bool)
            rows = self._verified(reader, words, self._containing(reader, words))
        elif query.kind == "boolean":
            analyze = self.lexical.analyze
            matched = np.zeros(segment.corpus_size, dtype=bool)
            for word in dict.fromkeys(words):
                tid = reader.term_ids([word]).get(word)
                if tid is not None:
                    matched[np.asarray(segment.postings(tid)[0])] = True
            rows = np.flatnonzero(matched)
            for must in query.must:
                need = analyze(must)
                if not need:
                    rows = np.zeros(0, dtype=np.int64)
                    break
                inside = np.intersect1d(rows, self._containing(reader, need), assume_unique=True)
                rows = self._verified(reader, need, inside)
            for must_not in query.must_not:
                avoid = analyze(must_not)
                if avoid:
                    inside = np.intersect1d(rows, self._containing(reader, avoid), assume_unique=True)
                    rows = np.setdiff1d(rows, self._verified(reader, avoid, inside), assume_unique=True)
        else:
            return None
        out = np.zeros(segment.corpus_size, dtype=bool)
        out[np.asarray(rows, dtype=np.int64)] = True
        return out

    def eligible_rows(self, view: PinnedView, query: LexicalQuery, scope: Scope | None, unindexable: Any) -> dict[str, np.ndarray | None]:
        """Per unit row eligibility of a lexical query: scope, ``fields`` and phrase/boolean."""
        words = self.lexical.query_tokens(query.text)
        out: dict[str, np.ndarray | None] = {}
        for reader in self.readers(view):
            rows = self._row_mask(reader, self._doc_allowed(reader, scope))
            if query.fields:
                segment = reader.segment
                wanted = np.asarray([i for i, f in enumerate(segment.fields) if f in set(query.fields)], dtype=np.int32)
                in_field = np.isin(np.asarray(segment.row_field), wanted)
                rows = in_field if rows is None else rows & in_field
            restrict = self._restrict(reader, query, words, unindexable)
            if restrict is not None:
                rows = restrict if rows is None else rows & restrict
            out[reader.unit_id] = rows
        return out

    # --- facets -------------------------------------------------------------------------------------

    def search(self, view: PinnedView, query: LexicalQuery, scope: Scope | None, k: int, unindexable: Any) -> tuple[list[ScoredChunk], dict[str, Any]]:
        """The stratum's best ``k`` chunks (best first, ties by chunk id) and the statistics used."""
        if query.kind not in {"terms", "phrase", "boolean"}:
            raise BackendError(KIND, "lexical", f"lexical query kind {query.kind!r} is not served over the live log stratum")
        eligible = self.eligible_rows(view, query, scope, unindexable)
        ranked, info = self.lexical.rank_rows(query.text, k, view, eligible=eligible)
        info["hit_units"] = {c: u for c, _d, _s, u in ranked}
        return [ScoredChunk(chunk_id=c, document_id=d, score=s) for c, d, s, _u in ranked], info

    def owner_of_chunks(self, ranked: Sequence[tuple[str, str, float, str]]) -> dict[str, str]:
        return {c: u for c, _d, _s, u in ranked}

    def doc_freqs(self, view: PinnedView, words: Sequence[str]) -> list[int]:
        """Served chunks holding each analyzed token (the stratum's chunk frequency)."""
        derived = self.derived(view)
        out = []
        for word in words:
            total = 0
            for reader in derived["readers"]:
                tid = reader.term_ids([word]).get(word)
                if tid is None:
                    continue
                rows = np.asarray(reader.segment.postings(tid)[0])
                total += int(derived["masks"][reader.unit_id][0][rows].sum())
            out.append(total)
        return out

    def phrase_rows(self, view: PinnedView, words: Sequence[str], scope: Scope | None, max_candidates: int) -> list[tuple[ScoredChunk, str]] | None:
        """``(chunk, unit id)`` of served chunks in ``scope`` holding ``words`` contiguously, or
        ``None`` above ``max_candidates`` (the sqlite ``phrase_rows`` contract)."""
        derived = self.derived(view)
        found: list[tuple[np.ndarray, UnitReader]] = []
        total = 0
        for reader in derived["readers"]:
            rows = self._containing(reader, words)
            if len(rows):
                eligible = derived["masks"][reader.unit_id][0]
                scoped = self._row_mask(reader, self._doc_allowed(reader, scope))
                if scoped is not None:
                    eligible = eligible & scoped
                rows = rows[eligible[rows]]
            total += len(rows)
            if total > max_candidates:
                return None
            found.append((rows, reader))
        out = []
        for rows, reader in found:
            segment = reader.segment
            for r in self._verified(reader, words, rows):
                out.append((ScoredChunk(chunk_id=segment.chunk_ids[int(r)], document_id=segment.doc_ids[int(segment.row_doc[int(r)])], score=0.0), reader.unit_id))
        return out

    def vector_search(self, view: PinnedView, vector: np.ndarray, scope: Scope | None, k: int) -> tuple[list[tuple[ScoredChunk, str]], list[str]]:
        """Exact dense top-``k`` over the served embed rows in ``scope``: ``([(chunk, unit)], pending
        units)``; ``pending`` units are skipped (vectors not stored yet) and reported."""
        if self.dense is None:
            return [], []
        readers = self.readers(view)
        allowed = {r.unit_id: self._doc_allowed(r, scope) for r in readers}
        ranked, info = self.dense.rank_vector(np.asarray(vector, dtype=np.float32), k, view, eligible=allowed)
        pending = list(info["pending"])
        with self._lock:
            self.pending[":".join(self.key(readers))[:256]] = tuple(pending)
        return [(ScoredChunk(chunk_id=c, document_id=d, score=s), u) for c, d, s, u in ranked], pending

    def dense_pending(self, view: PinnedView) -> list[str]:
        """Pinned units whose vectors are not all stored (checked against the vector store)."""
        if self.dense is None:
            return []
        return [r.unit_id for r in self.readers(view) if self.dense.matrix(r) is None]

    def warm(self, unit_id: str, folder: Path) -> None:
        """Pre-load a newly registered unit (reader, served-row inputs, dense matrix) so the first
        request after a compaction does not pay the cold loads (§6.7 first-request target)."""
        reader = self.units.reader(unit_id, folder)
        reader.segment.doc_index()
        if self.dense is not None:
            self.dense.matrix(reader)

    def warm_view(self, view: PinnedView) -> dict[str, Any]:
        """Pre-open every unit of a pinned view (startup warm-up, ``Engine.warmup``): readers and
        their segments, the served-row masks and hidden ids of the pinned set, each unit's catalog
        store and — with a dense facet — its vector matrix. Returns ``{"units", "dense_pending"}``;
        a unit whose vectors are not all stored stays ``dense_pending`` (reported, never filled)."""
        derived = self.derived(view)
        pending: list[str] = []
        for reader in derived["readers"]:
            reader.segment.doc_index()
            self.unit_store(reader)
            if self.dense is not None and self.dense.matrix(reader) is None:
                pending.append(reader.unit_id)
        return {"units": [r.unit_id for r in derived["readers"]], "dense_pending": pending}
