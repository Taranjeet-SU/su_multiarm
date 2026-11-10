"""Live log path: provisional delta slices and their ledger (docs/plans/unified_merge.md §6.7, B5,
B12, N3; lane LB). Implements the S0 ``SliceLedger`` protocol (``core/types_members.py``).

**Slices.** Each refresh of a live day produces a **delta slice**: the documents whose content
changed since the day's previous slices (content sha per ``(day, id)``; an unchanged document is
not sealed again), sealed with sha256 (``ingestion/handoff.py``) and registered in the member's
ledger — never a generation activation. A slice records

* ``supersedes``: ids whose live row (in an earlier unit, any day of the member) this slice
  replaces — latest version wins;
* ``tombstones`` (B12): with a **full-day build** (``full_day=True``) the ids live on that day but
  absent from the build — e.g. late lines fill a gap and merge two episode/incident groups, so the
  later group's id vanishes. Tombstoned ids are never served again;
* ``catalog_provisional`` (N3): catalog-kind documents (``signature``; day-independent ids) are
  excluded from slices, except a document **first seen** on a provisional day (absent from the
  catalog member), which the slice carries until the catalog member supersedes it at close. A
  changed catalog document that the catalog member already holds is parked (``catalog_pending``)
  and enters the catalog member at the next close — it adds no activation (≤ 6/day holds);
* ``excluded_stats``: documents/chunks/tokens of the superseded and tombstoned rows (§6.5).

**Versions.** Every sealed row carries a **version** = the sequence of the slice that first sealed
that content (tombstones too); compaction preserves versions. For any pinned set of units the
served row of an id is the one with the highest version, and a tombstone with a higher version
hides every older row. This makes the result independent of compaction timing and of slices of
other days interleaving (an id may continue across midnight inside the live window).

**Compaction (background, never on the request path).** ``compact(day, "hourly", hour=h)`` turns
the five-minute slices of a finished hour into one hourly slice; ``compact(day, "base")`` turns all
of a day's slices into one day base (every ``day_base_hours``); ``due(now)`` lists what the ladder
asks for. A compacted slice holds exactly the live rows of the slices it ``replaces`` (versions
kept) and the tombstones still needed by older units; the writer lock is held for the whole
compaction, so a compaction never races a refresh. The day's pinned units stay bounded by
``collection.slices.max_units_per_day`` (1 base + ≤ 6 hourly + ≤ 12 five-minute = 19); a seal that
would exceed it is refused (``SliceRefused``), never silently admitted.

**Close and retention.** ``log_members.close_day`` builds the archive day member (and the catalog
member) from the ledger; after the generation that includes it is active, ``release_day`` retires
the day's slices (one tier per day: a reader whose pinned generation already serves the day from
the archive drops it from the vector, ``vector_for``). A replaced or retired slice is deleted by
``gc`` only when no slice pin holds it (``ingestion/pins.py``) and it was retired more than
``gc_grace_seconds`` ago (R3).

**Unit catalog (M2a wiring).** Every unit also carries ``catalog.sqlite`` — documents, chunks, typed
document fields and one record per document (``schema_id: document``, the write path's records) —
so a partitioned collection serves ``get``, hit reads, listings, typed scope and records of slices
and archive members with the sqlite facets' own SQL (``backends/sqlite/members.py``). Embed texts
follow the member's embedding-space ``template``: ``engine_template`` embeds the write path's
``title + field + text`` input (one space with the write-path members, §6.2), ``chunk_text`` the
bare chunk text.

**Ledger files** (``<root>``): ``ledger.sqlite`` (slices, unit rows, live rows, per-day content
shas, tombstones, pending catalog documents), ``CURRENT.json`` (the slice version vector, replaced
atomically; read once per request), ``<day>/<name>/`` sealed units, ``.staging/`` builds,
``pins.sqlite`` (slice pins when no generation registry is shared).

**Id directory.** When an ``IdDirectory`` (LA, ``ingestion/id_directory.py``) is given, registration
mirrors into it inside the ledger transaction: ``register(slice, member=, replaces=)`` where
``replaces`` names every unit holding a row this slice supersedes — the compacted slices, a slice
of another live day (an id continuing across midnight, F0 chain C) or the archive day member of a
recently closed day (F0 chain B, ``replaces=["<member>/day:<closed>"]``) — so the directory never
sees a duplicate live id; then ``tombstone`` and ``retire`` of the compacted slices. ``gc`` purges
a deleted slice's rows. A failing directory call rolls the registration back (fail closed, §6.12).
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..backends._scope import document_values, record_violations
from ..backends.sqlite._db import field_row
from ..backends.sqlite.bm25 import TERMS_SCHEMA, Bm25Builder, Bm25Segment, analyzer
from ..core.config import Bm25Settings
from ..core.config_collection import DEFAULT_CATALOG_KINDS, SliceSettings
from ..core.errors import SuError
from ..core.types import Document, MetadataSchema, Provenance, StructuredRecord
from ..core.types_members import IdDirectory, IdDirectoryEntry, PartitionRecipe, SliceLevel, SliceRef
from ..extraction.chunking import chunk_document
from .handoff import HandoffRefused, seal_unit, verify_unit
from .text_vectors import EmbedRow, check_embed_fields, embed_rows

LEDGER_FORMAT = 1
_ID_PREFIX = '{"id":'
# Closed days whose archived rows the ledger remembers (an id continues past midnight at most into the next day).
ARCHIVED_ROW_DAYS = 2
LEVEL_CODE = {"five_minute": "m", "hourly": "h", "base": "b", "day": "d"}
UNIT_CATALOG = "catalog.sqlite"
# The write path's embedding input (``ingestion.write.EMBEDDING_INPUT``), used for ``engine_template``.
ENGINE_TEMPLATE = "{title}\n{field}: {text}"
EMBED_TEMPLATES = ("engine_template", "chunk_text")
# ``catalog.sqlite`` of a unit: the sqlite catalog's tables (``backends/sqlite/_db.CATALOG_SCHEMA``)
# for documents, chunks and typed fields, plus one record per document (the write path's records);
# record predicates read ``document_fields`` (a record's values are its document's values).
UNIT_CATALOG_SCHEMA = """
CREATE TABLE documents(id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE chunks(id TEXT PRIMARY KEY, document_id TEXT, field TEXT, start INTEGER, end INTEGER, text TEXT, recipe TEXT NOT NULL DEFAULT 'default');
CREATE TABLE document_fields(document_id TEXT, field TEXT, kind TEXT, text_value TEXT, fold_value TEXT, num_value REAL, is_null INTEGER);
CREATE TABLE records(id TEXT PRIMARY KEY, schema_id TEXT, data TEXT NOT NULL);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
UNIT_CATALOG_INDEXES = """
CREATE INDEX chunk_document ON chunks(document_id);
CREATE INDEX document_fields_doc ON document_fields(document_id, field);
CREATE INDEX document_fields_value ON document_fields(field, text_value);
"""
SCHEMA = """
CREATE TABLE IF NOT EXISTS slices(slice_id TEXT PRIMARY KEY, day TEXT NOT NULL, level TEXT NOT NULL, sequence INTEGER NOT NULL UNIQUE,
  status TEXT NOT NULL, as_of TEXT NOT NULL, sealed_at REAL NOT NULL, retired_at REAL, replaced_by TEXT, folder TEXT NOT NULL,
  sha256 TEXT NOT NULL, dense_ready INTEGER NOT NULL DEFAULT 0, ref TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS slices_day ON slices(day, status);
CREATE TABLE IF NOT EXISTS unit_docs(unit_id TEXT NOT NULL, doc_id TEXT NOT NULL, version INTEGER NOT NULL, sha TEXT NOT NULL, kind TEXT NOT NULL,
  chunks INTEGER NOT NULL, tokens INTEGER NOT NULL, PRIMARY KEY(unit_id, doc_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS unit_docs_doc ON unit_docs(doc_id);
CREATE TABLE IF NOT EXISTS tombs(unit_id TEXT NOT NULL, doc_id TEXT NOT NULL, version INTEGER NOT NULL, PRIMARY KEY(unit_id, doc_id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS live(doc_id TEXT PRIMARY KEY, day TEXT NOT NULL, unit_id TEXT NOT NULL, version INTEGER NOT NULL, sha TEXT NOT NULL,
  kind TEXT NOT NULL, catalog_provisional INTEGER NOT NULL) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS live_day ON live(day);
CREATE INDEX IF NOT EXISTS live_unit ON live(unit_id);
CREATE TABLE IF NOT EXISTS day_rows(day TEXT NOT NULL, doc_id TEXT NOT NULL, sha TEXT NOT NULL, PRIMARY KEY(day, doc_id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS catalog_pending(doc_id TEXT PRIMARY KEY, day TEXT NOT NULL, sha TEXT NOT NULL, document TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS closed_days(day TEXT PRIMARY KEY, released_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS archived(doc_id TEXT PRIMARY KEY, day TEXT NOT NULL, version INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class SliceRefused(SuError, ValueError):
    """A slice is not sealed or not registered (closed day, unit bound, inconsistent lists, a
    compaction conflict, a verification failure); the previous slices stay current."""


def doc_sha(doc: Document) -> str:
    """Content sha of one document version (its canonical JSON)."""
    return hashlib.sha256(doc.model_dump_json().encode()).hexdigest()[:32]


def collapse_upserts(documents: Iterable[Document]) -> list[Document]:
    """A collector's refresh is an ordered upsert log: the last upsert of an id wins. ``seal``
    itself refuses a repeated id (a full-day build never repeats one), so producers collapse the
    log with this function first."""
    latest: dict[str, Document] = {}
    for doc in documents:
        latest.pop(doc.id, None)
        latest[doc.id] = doc
    return list(latest.values())


def bm25_settings_of(recipe: PartitionRecipe) -> Bm25Settings:
    """The bm25 analyzer/scoring settings of a log member: ``recipe.analyzer`` plus the optional
    ``recipe.content["bm25"]`` (``lowercase``, ``stemming``, ``include_title``)."""
    a = recipe.analyzer
    if a.engine != "bm25":
        raise SliceRefused("the live log path builds bm25 segments only (analyzer.engine: bm25)")
    extra = dict(recipe.content.get("bm25") or {})
    values: dict[str, Any] = {"tokenizer": a.tokenizer, **extra}
    for name in ("k1", "b", "epsilon"):
        if getattr(a, name) is not None:
            values[name] = getattr(a, name)
    return Bm25Settings(**values)


@dataclass(frozen=True)
class Chunking:
    size: int
    overlap: int
    field_order: tuple[str, ...] = ()

    @classmethod
    def of(cls, recipe: PartitionRecipe) -> Chunking:
        spec = recipe.chunking
        if "size" not in spec or "overlap" not in spec:
            raise SliceRefused("the member recipe declares chunking.size and chunking.overlap")
        return cls(int(spec["size"]), int(spec["overlap"]), tuple(spec.get("field_order") or ()))


# --- the unit format (slices and archive members) -----------------------------------------------------


@dataclass
class UnitBuild:
    """What ``write_unit`` wrote: per-document stats and the embed texts of the unit."""

    documents: int = 0
    chunks: int = 0
    tokens: int = 0
    per_doc: dict[str, tuple[int, int]] = field(default_factory=dict)  # id -> (chunks, tokens)
    embed_texts: dict[str, str] = field(default_factory=dict)  # hash -> text
    lexical: dict[str, Any] = field(default_factory=dict)


def _gzip_lines(path: Path, lines: Iterable[str]) -> None:
    with open(path, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=1) as gz:
        for line in lines:
            gz.write(line.encode())
            gz.write(b"\n")


def write_unit_catalog(path: Path, documents: Sequence[Document], chunks: Sequence[Any], schema: MetadataSchema | None = None) -> None:
    """``catalog.sqlite`` of a unit (see ``UNIT_CATALOG_SCHEMA``). With a ``schema`` every record is
    validated as the write path validates it (a violation refuses the unit, never stored)."""
    provenance = Provenance(extractor="catalog")
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA journal_mode=OFF")
        db.execute("PRAGMA synchronous=OFF")
        with db:
            db.executescript(UNIT_CATALOG_SCHEMA)
            for start in range(0, len(documents), 2000):
                batch = documents[start : start + 2000]
                records = [StructuredRecord(schema_id="document", id=d.id, values=document_values(d), provenance=provenance) for d in batch]
                if schema is not None:
                    for record in records:
                        violations = record_violations(schema, record)
                        if violations:
                            first = violations[0]
                            raise SliceRefused(f"record {record.id}: {first.field} {first.category}: {first.message}")
                db.executemany("INSERT INTO documents VALUES (?,?)", [(d.id, d.model_dump_json()) for d in batch])
                db.executemany("INSERT INTO records VALUES (?,?,?)", [(r.id, r.schema_id, r.model_dump_json()) for r in records])
                db.executemany("INSERT INTO document_fields VALUES (?,?,?,?,?,?,?)",
                               [field_row(d.id, key, value) for d in batch for key, value in document_values(d).items()])
            db.executemany("INSERT INTO chunks VALUES (?,?,?,?,?,?,?)", [(c.id, c.document_id, c.field, c.start, c.end, c.text, c.recipe) for c in chunks])
            if schema is not None:
                db.execute("INSERT INTO meta VALUES ('metadata_schema', ?)", (schema.model_dump_json(),))
            db.executescript(UNIT_CATALOG_INDEXES)


def write_unit(folder: Path, documents: Sequence[Document], versions: Mapping[str, int], tombstones: Mapping[str, int], *,
               settings: Bm25Settings, chunking: Chunking, embed_fields: Mapping[str, Sequence[str]], template: str = "chunk_text",
               metadata_schema: MetadataSchema | None = None) -> UnitBuild:
    """Write one unit's files into ``folder`` (not sealed): ``documents.jsonl.gz`` (sorted by id),
    ``rows.json`` (id, version, kind, sha, chunks, tokens per document), ``tombstones.json``,
    ``lexical/`` (a bm25 segment over the unit's chunks in chunk-id order + ``terms.sqlite``),
    ``embed.json`` (embeddable chunks by text hash, with the texts, composed by ``template``) and
    ``catalog.sqlite`` (``write_unit_catalog``)."""
    if template not in EMBED_TEMPLATES:
        raise SliceRefused(f"embedding template {template!r} is not one of {list(EMBED_TEMPLATES)}")
    folder.mkdir(parents=True, exist_ok=True)
    docs = sorted(documents, key=lambda d: d.id)
    ids = [d.id for d in docs]
    if len(set(ids)) != len(ids):
        raise SliceRefused("a unit holds each document id once")
    if set(ids) & set(tombstones):
        raise SliceRefused("an id is either content or a tombstone of a unit, not both")
    analyze = analyzer(settings)
    chunks = []
    for doc in docs:
        chunks.extend(chunk_document(doc, chunking.size, chunking.overlap, list(chunking.field_order)))
    chunks.sort(key=lambda c: c.id)
    titles = {d.id: d.title for d in docs}
    builder = Bm25Builder(settings)
    build = UnitBuild(documents=len(docs))
    per_doc_chunks: dict[str, int] = dict.fromkeys(ids, 0)
    per_doc_tokens: dict[str, int] = dict.fromkeys(ids, 0)
    for chunk in chunks:
        words = analyze(chunk.text or "")
        title = titles.get(chunk.document_id)
        if settings.include_title and title:
            words = analyze(title) + words
        builder.add(chunk.id, chunk.document_id, chunk.field, words)
        per_doc_chunks[chunk.document_id] += 1
        per_doc_tokens[chunk.document_id] += len(words)
    lexical = folder / "lexical"
    lexical.mkdir(exist_ok=True)
    with closing(sqlite3.connect(lexical / "terms.sqlite")) as db:
        with db:
            db.execute(TERMS_SCHEMA)
            build.lexical = builder.write(lexical, db)
        db.execute("VACUUM")
    kinds = {d.id: d.kind for d in docs}
    compose = None
    if template == "engine_template":
        compose = lambda c: ENGINE_TEMPLATE.format(title=titles.get(c.document_id) or "", field=c.field, text=c.text)  # noqa: E731
    rows, texts = embed_rows(chunks, kinds, embed_fields, compose=compose)
    build.embed_texts = texts
    (folder / "embed.json").write_text(json.dumps({"rows": [[r.chunk_id, r.doc_id, r.field, r.hash] for r in rows], "texts": texts}, sort_keys=True, separators=(",", ":")))
    shas = [doc_sha(d) for d in docs]
    (folder / "rows.json").write_text(json.dumps({
        "ids": ids, "versions": [int(versions[i]) for i in ids], "kinds": [kinds[i] for i in ids], "shas": shas,
        "chunks": [per_doc_chunks[i] for i in ids], "tokens": [per_doc_tokens[i] for i in ids]}, separators=(",", ":")))
    (folder / "tombstones.json").write_text(json.dumps(sorted([i, int(v)] for i, v in tombstones.items()), separators=(",", ":")))
    _gzip_lines(folder / "documents.jsonl.gz", (d.model_dump_json() for d in docs))
    write_unit_catalog(folder / UNIT_CATALOG, docs, chunks, metadata_schema)
    build.chunks = len(chunks)
    build.tokens = sum(per_doc_tokens.values())
    build.per_doc = {i: (per_doc_chunks[i], per_doc_tokens[i]) for i in ids}
    return build


class UnitReader:
    """Read access to one sealed unit (slice or archive member). Files are opened read-only; the
    bm25 segment is memory-mapped. ``unit_id`` keys every cache (never a generation)."""

    def __init__(self, folder: Path, settings: Bm25Settings, unit_id: str | None = None):
        self.folder = Path(folder)
        body = json.loads((self.folder / "unit.json").read_text())
        self.ref: dict[str, Any] = body["ref"]
        self.seal = (self.folder / "SEAL").read_text().strip()
        self.unit_id = unit_id or self.ref.get("slice_id") or f"{self.ref['member']}/{self.ref['key']}"
        rows = json.loads((self.folder / "rows.json").read_text())
        self.ids: list[str] = rows["ids"]
        self.versions: list[int] = rows["versions"]
        self.kinds: list[str] = rows["kinds"]
        self.chunks: list[int] = rows["chunks"]
        self.tokens: list[int] = rows["tokens"]
        self.tombstones: list[tuple[str, int]] = [(i, int(v)) for i, v in json.loads((self.folder / "tombstones.json").read_text())]
        self.segment = Bm25Segment(self.folder / "lexical", settings)
        self._terms = sqlite3.connect(f"file:{self.folder / 'lexical' / 'terms.sqlite'}?mode=ro", uri=True, check_same_thread=False)
        self._embed: tuple[list[EmbedRow], dict[str, str]] | None = None

    def version_of(self) -> dict[str, int]:
        return dict(zip(self.ids, self.versions))

    def term_ids(self, words: Sequence[str]) -> dict[str, int]:
        distinct = list(dict.fromkeys(words))
        if not distinct:
            return {}
        marks = ",".join("?" * len(distinct))
        return {t: int(tid) for t, tid in self._terms.execute(f"SELECT term, tid FROM bm25_terms WHERE term IN ({marks})", distinct)}

    def embed(self) -> tuple[list[EmbedRow], dict[str, str]]:
        if self._embed is None:
            data = json.loads((self.folder / "embed.json").read_text())
            self._embed = ([EmbedRow(*r) for r in data["rows"]], data["texts"])
        return self._embed

    def documents(self, ids: Iterable[str] | None = None) -> Iterator[Document]:
        wanted = None if ids is None else set(ids)
        decoder = json.JSONDecoder()
        with gzip.open(self.folder / "documents.jsonl.gz", "rt") as handle:
            for line in handle:
                if wanted is not None:
                    # ``id`` is the first key of the canonical dump: decode only that value
                    if not line.startswith(_ID_PREFIX):
                        raise SliceRefused(f"{self.unit_id}: documents.jsonl.gz is not in the unit format")
                    if decoder.raw_decode(line, len(_ID_PREFIX))[0] not in wanted:
                        continue
                yield Document.model_validate_json(line)

    def close(self) -> None:
        self._terms.close()


# --- the ledger ------------------------------------------------------------------------------------------


@dataclass
class _Plan:
    """A slice to seal: content (with versions), tombstones and the protocol lists."""

    day: date
    level: SliceLevel
    sequence: int
    documents: list[Document]
    versions: dict[str, int]
    tombstones: dict[str, int]
    supersedes: list[str]
    catalog_provisional: list[str]
    replaces: list[str]
    as_of: datetime
    excluded: dict[str, int]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class LogSliceLedger:
    """The provisional slice ledger of one live log member (see the module doc)."""

    def __init__(
        self,
        root: str | Path,
        *,
        member: str,
        source: str,
        recipe: PartitionRecipe,
        settings: SliceSettings | None = None,
        embed_fields: Mapping[str, Sequence[str]] | None = None,
        catalog_kinds: Sequence[str] = DEFAULT_CATALOG_KINDS,
        catalog: Callable[[], Mapping[str, str]] | None = None,
        directory: IdDirectory | None = None,
        pins: Any = None,
        keyword_epoch: str | None = None,
        clock: Callable[[], float] = time.time,
        metadata_schema: MetadataSchema | None = None,
    ):
        self.root = Path(root)
        self.metadata_schema = metadata_schema
        space = recipe.embedding_space
        self.template = "chunk_text" if space is None or space.template not in EMBED_TEMPLATES else space.template
        # ``(slice ref, unit folder)`` callbacks after a slice is registered (e.g. warming a reader's
        # caches for a new compacted base, §6.7 "a new slice warms only itself")
        self.on_registered: list[Callable[[SliceRef, Path], None]] = []
        self.member, self.source, self.recipe = member, source, recipe
        self.settings = settings or SliceSettings()
        self.embed_fields = {k: tuple(v) for k, v in (embed_fields or {}).items()}
        check_embed_fields(self.embed_fields)
        self.catalog_kinds = frozenset(catalog_kinds)
        self.catalog = catalog or (lambda: {})
        self.directory = directory
        self.keyword_epoch = keyword_epoch
        self.clock = clock
        self.bm25 = bm25_settings_of(recipe)
        self.chunking = Chunking.of(recipe)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".staging").mkdir(exist_ok=True)
        if pins is None:
            from .pins import SlicePins, SqlitePins

            pins = SlicePins(SqlitePins(self.root / "pins.sqlite", clock))
        self.pins = pins
        with closing(sqlite3.connect(self.root / "ledger.sqlite", timeout=60)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
        with self._db() as db:
            found ={k: v for k, v in db.execute("SELECT key, value FROM meta")}
            identity = json.dumps({"format": LEDGER_FORMAT, "member": member, "source": source, "recipe": recipe.fingerprint()}, sort_keys=True)
            if "identity" in found and found["identity"] != identity:
                raise SliceRefused(f"ledger {self.root.name} belongs to another member or recipe; a recipe change opens a new ledger (epoch)")
            db.execute("INSERT OR IGNORE INTO meta VALUES ('identity', ?)", (identity,))
            db.execute("INSERT OR IGNORE INTO meta VALUES ('sequence', '0')")
        if not self.current_path.exists():
            self._write_current_unlocked()

    # --- plumbing ----------------------------------------------------------------------------------

    @property
    def current_path(self) -> Path:
        return self.root / "CURRENT.json"

    @contextmanager
    def _db(self, write: bool = True) -> Iterator[sqlite3.Connection]:
        """A ledger connection; ``write`` runs it in one ``BEGIN IMMEDIATE`` transaction (rolled
        back on any exception), otherwise a WAL snapshot read that never blocks a writer."""
        with closing(sqlite3.connect(self.root / "ledger.sqlite", timeout=60, isolation_level=None)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    @contextmanager
    def lock(self) -> Iterator[None]:
        """One writer at a time: seal, compaction, release and gc."""
        with open(self.root / ".lock", "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _write_current_unlocked(self, db: sqlite3.Connection | None = None) -> None:
        def body(conn: sqlite3.Connection) -> dict[str, Any]:
            days: dict[str, list[str]] = {}
            for slice_id, day in conn.execute("SELECT slice_id, day FROM slices WHERE status='current' ORDER BY sequence"):
                days.setdefault(day, []).append(slice_id)
            seq = conn.execute("SELECT value FROM meta WHERE key='sequence'").fetchone()
            return {"format": LEDGER_FORMAT, "member": self.member, "sequence": int(seq[0]) if seq else 0, "days": dict(sorted(days.items()))}

        if db is None:
            with self._db() as conn:
                data = body(conn)
        else:
            data = body(db)
        tmp = self.current_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True))
        os.replace(tmp, self.current_path)

    def unit_dir(self, slice_id: str) -> Path:
        _, day, name = slice_id.split(":", 2)
        return self.root / day / name

    # --- SliceLedger protocol --------------------------------------------------------------------------

    def vector(self) -> dict[date, tuple[str, ...]]:
        """The slice version vector ``{day: (slice ids in sequence order)}`` (read once per request)."""
        data = json.loads(self.current_path.read_text())
        return {date.fromisoformat(d): tuple(ids) for d, ids in data["days"].items()}

    def vector_for(self, archived: Callable[[date], bool]) -> dict[date, tuple[str, ...]]:
        """The vector minus the days the reader's pinned generation already serves from the archive
        (one tier per day while ``release_day`` has not run yet)."""
        return {d: ids for d, ids in self.vector().items() if not archived(d)}

    def get(self, slice_id: str) -> SliceRef:
        with self._db(write=False) as db:
            row = db.execute("SELECT ref, dense_ready FROM slices WHERE slice_id=?", (slice_id,)).fetchone()
        if row is None:
            raise KeyError(slice_id)
        return SliceRef.model_validate_json(row[0]).model_copy(update={"dense_pending": not bool(row[1])})

    def seal(self, day: date, level: SliceLevel, documents: Sequence[Any], *, replaces: Sequence[str] = (), full_day: bool = False,
             as_of: datetime | None = None) -> SliceRef | None:
        """Seal one refresh of ``day`` (``level`` ``five_minute``; a full-day rebuild may also be
        sealed as ``day``). ``documents``: the refresh's upserts, or with ``full_day=True`` the
        complete day build (then ids live on the day but absent from it are tombstoned). Returns
        ``None`` when nothing changed (no slice is sealed). Compaction uses ``compact``."""
        if replaces:
            raise SliceRefused("a refresh slice replaces nothing; compaction uses compact()")
        if level not in {"five_minute", "day"}:
            raise SliceRefused("a refresh is sealed as five_minute (or day for a full-day rebuild); hourly/base come from compact()")
        docs = [d if isinstance(d, Document) else Document.model_validate(d) for d in documents]
        with self.lock():
            plan = self._plan_refresh(day, level, docs, full_day, as_of or _now_utc())
            if plan is None:
                return None
            return self._build_and_register(plan)

    def retire(self, slice_id: str) -> bool:
        """Delete one replaced/retired slice if no pin holds it and it was retired more than
        ``gc_grace_seconds`` ago; ``False`` otherwise (it stays for a later ``gc``)."""
        with self.lock():
            return self._delete_if_free(slice_id, self.clock())

    # --- refresh planning ---------------------------------------------------------------------------------

    def _closed(self, db: sqlite3.Connection, day: date) -> bool:
        return db.execute("SELECT 1 FROM closed_days WHERE day=?", (day.isoformat(),)).fetchone() is not None

    def _next_sequence(self, db: sqlite3.Connection) -> int:
        return int(db.execute("SELECT value FROM meta WHERE key='sequence'").fetchone()[0]) + 1

    def _plan_refresh(self, day: date, level: SliceLevel, docs: list[Document], full_day: bool, as_of: datetime) -> _Plan | None:
        ids = [d.id for d in docs]
        if len(set(ids)) != len(ids):
            raise SliceRefused("a refresh carries each document id once")
        foreign = sorted({d.source for d in docs} - {self.source})
        if foreign:
            raise SliceRefused(f"documents of source {foreign} do not belong to member {self.member!r} (source {self.source!r})")
        catalog = self.catalog()
        day_s = day.isoformat()
        with self._db() as db:
            if self._closed(db, day):
                raise SliceRefused(f"day {day_s} is closed (archived); late data goes to a rebuild of the archive member")
            sequence = self._next_sequence(db)
            last_day = {r[0]: r[1] for r in self._rows_in(db, "SELECT doc_id, sha FROM day_rows WHERE day=? AND doc_id IN ({})", [day_s], ids)}
            live = {r[0]: (r[1], r[2]) for r in self._rows_in(db, "SELECT doc_id, sha, catalog_provisional FROM live WHERE doc_id IN ({})", [], ids)}
            content: list[Document] = []
            provisional: list[str] = []
            parked: list[tuple[str, str, str, str]] = []
            for doc in docs:
                sha = doc_sha(doc)
                if doc.kind in self.catalog_kinds:
                    if doc.id in catalog:
                        if catalog[doc.id] != sha:
                            parked.append((doc.id, day_s, sha, doc.model_dump_json()))
                        continue
                    if doc.id in live and live[doc.id][0] == sha:
                        continue
                    content.append(doc)
                    provisional.append(doc.id)
                    continue
                if last_day.get(doc.id) == sha:
                    continue
                content.append(doc)
            tombstones: dict[str, int] = {}
            if full_day:
                present = set(ids)
                for (doc_id,) in db.execute("SELECT doc_id FROM live WHERE day=? AND catalog_provisional=0", (day_s,)):
                    if doc_id not in present:
                        tombstones[doc_id] = sequence
            if parked:
                db.executemany("INSERT OR REPLACE INTO catalog_pending VALUES (?,?,?,?)", parked)
            if not content and not tombstones:
                return None
            content_ids = [d.id for d in content]
            supersedes = sorted(i for i in content_ids if i in live)
            excluded = self._excluded(db, [*supersedes, *tombstones])
            current = db.execute("SELECT count(*) FROM slices WHERE day=? AND status='current'", (day_s,)).fetchone()[0]
            if current + 1 > self.settings.max_units_per_day:
                raise SliceRefused(f"day {day_s} would pin {current + 1} slices (max_units_per_day {self.settings.max_units_per_day}); compaction is behind")
        return _Plan(day, level, sequence, content, dict.fromkeys(content_ids, sequence), tombstones, supersedes, sorted(provisional), [], as_of, excluded)

    @staticmethod
    def _rows_in(db: sqlite3.Connection, sql: str, head: list[Any], values: Sequence[str]) -> Iterator[tuple]:
        values = list(values)
        for start in range(0, len(values), 900):
            part = values[start : start + 900]
            yield from db.execute(sql.format(",".join("?" * len(part))), [*head, *part])

    def _excluded(self, db: sqlite3.Connection, doc_ids: Sequence[str]) -> dict[str, int]:
        """documents/chunks/tokens of the live rows of ``doc_ids`` (rows a slice supersedes or tombstones)."""
        out = {"documents": 0, "chunks": 0, "tokens": 0}
        query = "SELECT count(*), coalesce(sum(u.chunks),0), coalesce(sum(u.tokens),0) FROM live l JOIN unit_docs u ON u.unit_id=l.unit_id AND u.doc_id=l.doc_id WHERE l.doc_id IN ({})"
        for n, c, t in self._rows_in(db, query, [], doc_ids):
            out["documents"] += int(n)
            out["chunks"] += int(c)
            out["tokens"] += int(t)
        return out

    # --- compaction ------------------------------------------------------------------------------------

    def compact(self, day: date, level: SliceLevel, *, hour: int | None = None, as_of: datetime | None = None) -> SliceRef | None:
        """Compact the day's current slices into one ``hourly`` slice (the five-minute slices whose
        refresh ended within hour ``hour``) or one ``base`` (every current slice of the day).
        Returns ``None`` when there is nothing to compact."""
        if level not in {"hourly", "base"}:
            raise SliceRefused("compaction produces hourly or base slices")
        if (level == "hourly") != (hour is not None):
            raise SliceRefused("hourly compaction names its hour; base compaction does not")
        day_s = day.isoformat()
        with self.lock():
            with self._db() as db:
                if self._closed(db, day):
                    raise SliceRefused(f"day {day_s} is closed")
                rows = db.execute("SELECT slice_id, level, as_of FROM slices WHERE day=? AND status='current' ORDER BY sequence", (day_s,)).fetchall()
                if level == "hourly":
                    run = [r[0] for r in rows if r[1] == "five_minute" and self._hour_of(r[2], day) == hour]
                else:
                    run = [r[0] for r in rows]
                if not run or (level == "base" and len(run) == 1 and rows[0][1] == "base"):
                    return None
                sequence = self._next_sequence(db)
                marks = ",".join("?" * len(run))
                live_rows = db.execute(f"SELECT doc_id, unit_id, version, catalog_provisional FROM live WHERE unit_id IN ({marks})", run).fetchall()
                versions = {doc_id: int(v) for doc_id, _u, v, _c in live_rows}
                provisional = sorted(doc_id for doc_id, _u, _v, c in live_rows if c)
                holders: dict[str, list[str]] = {}
                for doc_id, unit, _v, _c in live_rows:
                    holders.setdefault(unit, []).append(doc_id)
                # tombstones of the run still needed: an older row of the id lives in a current unit outside the run
                tombs = {d: int(v) for d, v in db.execute(f"SELECT doc_id, max(version) FROM tombs WHERE unit_id IN ({marks}) GROUP BY doc_id", run)}
                outside = "SELECT DISTINCT u.doc_id FROM unit_docs u JOIN slices s ON s.slice_id=u.unit_id WHERE s.status='current' AND u.unit_id NOT IN ({run}) AND u.doc_id IN ({{}})"
                outside = outside.format(run=marks)
                candidates = [d for d in tombs if d not in versions]
                needed = {r[0] for r in self._rows_in(db, outside, run, candidates)}
                # ... or an archive member of a recently closed day still holds an older row of it
                needed |= {r[0] for r in self._rows_in(db, "SELECT doc_id, version FROM archived WHERE doc_id IN ({})", [], candidates) if int(r[1]) < tombs[r[0]]}
                tombstones = {d: v for d, v in tombs.items() if d in needed}
                older = {r[0] for r in self._rows_in(db, outside, run, list(versions))}
                as_of_value = as_of or max(datetime.fromisoformat(r[2].replace("Z", "+00:00")) for r in rows if r[0] in run)
            documents: list[Document] = []
            for unit, doc_ids in holders.items():
                reader = UnitReader(self.unit_dir(unit), self.bm25)
                try:
                    documents.extend(reader.documents(doc_ids))
                finally:
                    reader.close()
            if len(documents) != len(versions):
                raise SliceRefused(f"compaction of {day_s} found {len(documents)} documents for {len(versions)} live rows; the ledger and its units disagree")
            plan = _Plan(day, level, sequence, documents, versions, tombstones, sorted(older), provisional, run, as_of_value, {})
            return self._build_and_register(plan)

    def _hour_of(self, as_of: str, day: date) -> int:
        """The hour of ``day`` a refresh slice belongs to: its refresh ended within ``(h, h+1]``."""
        moment = datetime.fromisoformat(as_of.replace("Z", "+00:00")) - timedelta(microseconds=1)
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        return max(0, min(23, int((moment - start).total_seconds() // 3600)))

    def due(self, now: datetime) -> list[tuple[date, SliceLevel, int | None]]:
        """Compactions the ladder asks for at ``now``: hourly for each finished hour that still has
        five-minute slices, then a base when the day holds ``day_base_hours`` hourly slices or the
        day has ended."""
        hour_seconds = self.settings.compaction.hourly_seconds
        out: list[tuple[date, SliceLevel, int | None]] = []
        with self._db(write=False) as db:
            rows = db.execute("SELECT day, level, as_of FROM slices WHERE status='current' ORDER BY sequence").fetchall()
        by_day: dict[str, list[tuple[str, str]]] = {}
        for day_s, level, as_of in rows:
            by_day.setdefault(day_s, []).append((level, as_of))
        hours_per_base = (self.settings.compaction.day_base_hours * 3600) // hour_seconds
        for day_s, items in sorted(by_day.items()):
            day = date.fromisoformat(day_s)
            start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
            hours = sorted({self._hour_of(a, day) for level, a in items if level == "five_minute"})
            finished = [h for h in hours if start + timedelta(hours=h + 1) <= now]
            hourly = sum(1 for level, _ in items if level == "hourly") + len(finished)
            day_over = start + timedelta(days=1) <= now
            if hourly >= hours_per_base or (day_over and len(items) > 1):
                out.append((day, "base", None))  # the base absorbs the finished hours too
            else:
                out.extend((day, "hourly", h) for h in finished)
        return out

    def run_due(self, now: datetime) -> list[SliceRef]:
        """Run every compaction ``due(now)`` lists (the background job's one tick)."""
        done = []
        for day, level, hour in self.due(now):
            ref = self.compact(day, level, hour=hour)
            if ref is not None:
                done.append(ref)
        return done

    # --- build, seal, register -------------------------------------------------------------------------

    def _build_and_register(self, plan: _Plan) -> SliceRef:
        name = f"{LEVEL_CODE[plan.level]}{plan.sequence:08d}"
        slice_id = f"slice:{plan.day.isoformat()}:{name}"
        staging = self.root / ".staging" / name
        if staging.exists():
            shutil.rmtree(staging)
        try:
            build = write_unit(staging, plan.documents, plan.versions, plan.tombstones, settings=self.bm25, chunking=self.chunking, embed_fields=self.embed_fields,
                               template=self.template, metadata_schema=self.metadata_schema)
            counts: dict[str, int] = {}
            for doc in plan.documents:
                counts[doc.kind] = counts.get(doc.kind, 0) + 1
            day_dir = f"{plan.day.isoformat()}/{name}"
            ref = {
                "slice_id": slice_id, "member": self.member, "source": self.source, "day": plan.day.isoformat(), "level": plan.level,
                "sequence": plan.sequence, "as_of": _iso(plan.as_of), "recipe": self.recipe.model_dump(mode="json"), "keyword_epoch": self.keyword_epoch,
                "documents": len(plan.documents), "counts": dict(sorted(counts.items())), "replaces": list(plan.replaces),
                "supersedes": list(plan.supersedes), "tombstones": sorted(plan.tombstones), "catalog_provisional": list(plan.catalog_provisional),
                "excluded_stats": plan.excluded,
                "facets": {"records": {"backend": "jsonl", "location": f"{day_dir}/documents.jsonl.gz"},
                           "lexical": {"backend": "bm25", "location": f"{day_dir}/lexical"},
                           "vector": {"backend": "text_vectors", "location": f"{day_dir}/embed.json"}},
                "dense_pending": bool(build.embed_texts),
            }
            seal = seal_unit(staging, "slice", ref)
            body = verify_unit(staging, expected_seal=seal)
            final = self.unit_dir(slice_id)
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, final)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        try:
            ref = self._register(final, body)
        except BaseException:
            shutil.rmtree(final, ignore_errors=True)
            raise
        self._notify(ref, final)
        return ref

    def _notify(self, ref: SliceRef, folder: Path) -> None:
        for hook in list(self.on_registered):
            hook(ref, folder)

    def receive(self, folder: Path) -> SliceRef:
        """Register a slice sealed by the log host and transferred with ``handoff.transfer``: every
        byte is verified, the lists are checked against this ledger's state and the unit is moved
        under the ledger. Any failure refuses it and keeps the previous slices."""
        folder = Path(folder)
        try:
            body = verify_unit(folder)
        except HandoffRefused as exc:
            raise SliceRefused(f"handoff verification failed: {exc}") from exc
        if body.get("kind") != "slice":
            raise SliceRefused("the unit is not a slice")
        slice_id = body["ref"]["slice_id"]
        final = self.unit_dir(slice_id)
        with self.lock():
            if final.exists():
                raise SliceRefused(f"{slice_id} is already registered")
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(folder, final)
            try:
                verify_unit(final, expected_seal=body["seal"])
                ref = self._register(final, body)
            except BaseException:
                shutil.rmtree(final, ignore_errors=True)
                raise
        self._notify(ref, final)
        return ref

    def _register(self, folder: Path, body: dict[str, Any]) -> SliceRef:
        """Check a sealed slice against the ledger state and register it (caller holds the lock)."""
        ref = SliceRef.model_validate({**body["ref"], "sha256": body["seal"]})
        if ref.member != self.member or ref.source != self.source:
            raise SliceRefused(f"{ref.slice_id} belongs to member {ref.member!r}, not {self.member!r}")
        if ref.recipe.fingerprint() != self.recipe.fingerprint():
            raise SliceRefused(f"{ref.slice_id} was built with another partition recipe (a new epoch needs a new ledger)")
        reader = UnitReader(folder, self.bm25, ref.slice_id)
        try:
            rows = list(zip(reader.ids, reader.versions, reader.kinds, json.loads((folder / "rows.json").read_text())["shas"], reader.chunks, reader.tokens))
            tombs = reader.tombstones
        finally:
            reader.close()
        day_s = ref.day.isoformat()
        now = self.clock()
        with self._db() as db:
            if self._closed(db, ref.day):
                raise SliceRefused(f"day {day_s} is closed")
            if db.execute("SELECT 1 FROM slices WHERE slice_id=?", (ref.slice_id,)).fetchone():
                raise SliceRefused(f"{ref.slice_id} is already registered")
            if ref.sequence != self._next_sequence(db):
                raise SliceRefused(f"{ref.slice_id}: sequence {ref.sequence} is not the next sequence {self._next_sequence(db)} of this ledger")
            ids = [r[0] for r in rows]
            live = {r[0]: (r[1], int(r[2])) for r in self._rows_in(db, "SELECT doc_id, unit_id, version FROM live WHERE doc_id IN ({})", [], ids + [t[0] for t in tombs])}
            if ref.replaces:
                current = {r[0]: r[1] for r in db.execute("SELECT slice_id, day FROM slices WHERE status='current'")}
                stale = [s for s in ref.replaces if current.get(s) != day_s]
                if stale:
                    raise SliceRefused(f"{ref.slice_id} replaces slices that are not current slices of {day_s}: {stale}")
                expected = {d: (u, v) for d, u, v in self._rows_in(db, "SELECT doc_id, unit_id, version FROM live WHERE unit_id IN ({})", [], ref.replaces)}
                if {i: v for i, v, *_ in rows} != {d: v for d, (_u, v) in expected.items()}:
                    raise SliceRefused(f"{ref.slice_id}: compacted content differs from the live rows of the slices it replaces")
            else:
                if any(v != ref.sequence for _i, v, *_ in rows) or any(v != ref.sequence for _i, v in tombs):
                    raise SliceRefused(f"{ref.slice_id}: a refresh slice's rows carry its own sequence as version")
                if sorted(i for i in ids if i in live) != list(ref.supersedes):
                    raise SliceRefused(f"{ref.slice_id}: supersedes list disagrees with the live rows of this ledger")
                missing = [t for t, _v in tombs if t not in live]
                if missing:
                    raise SliceRefused(f"{ref.slice_id}: tombstones name ids that are not live here: {missing[:3]}")
            holders = self._holders(db, ref, ids, live) if self.directory is not None else []
            # register
            db.execute("INSERT INTO slices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                ref.slice_id, day_s, ref.level, ref.sequence, "current", _iso(ref.as_of), now, None, None,
                folder.relative_to(self.root).as_posix(), ref.sha256, 0 if ref.dense_pending else 1, ref.model_dump_json()))
            db.executemany("INSERT INTO unit_docs VALUES (?,?,?,?,?,?,?)", [(ref.slice_id, i, v, k, s, c, t) for i, v, s, k, c, t in
                                                                            ((r[0], r[1], r[3], r[2], r[4], r[5]) for r in rows)])
            db.executemany("INSERT INTO tombs VALUES (?,?,?)", [(ref.slice_id, d, v) for d, v in tombs])
            provisional = set(ref.catalog_provisional)
            if ref.replaces:
                marks = ",".join("?" * len(ref.replaces))
                db.execute(f"UPDATE live SET unit_id=? WHERE unit_id IN ({marks})", [ref.slice_id, *ref.replaces])
                db.execute(f"UPDATE slices SET status='replaced', replaced_by=?, retired_at=? WHERE slice_id IN ({marks})", [ref.slice_id, now, *ref.replaces])
            else:
                db.executemany("INSERT OR REPLACE INTO live VALUES (?,?,?,?,?,?,?)",
                               [(i, day_s, ref.slice_id, v, s, k, 1 if i in provisional else 0) for i, v, k, s, _c, _t in rows])
                db.executemany("INSERT OR REPLACE INTO day_rows VALUES (?,?,?)", [(day_s, i, s) for i, _v, _k, s, _c, _t in rows if i not in provisional])
                db.executemany("DELETE FROM live WHERE doc_id=?", [(d,) for d, _v in tombs])
                db.executemany("DELETE FROM day_rows WHERE day=? AND doc_id=?", [(day_s, d) for d, _v in tombs])
            db.execute("UPDATE meta SET value=? WHERE key='sequence'", (str(ref.sequence),))
            if ref.level in {"base", "day"} or db.execute("SELECT value FROM meta WHERE key='average_idf'").fetchone() is None:
                lexical = json.loads((folder / "lexical" / "meta.json").read_text())
                db.execute("INSERT OR REPLACE INTO meta VALUES ('average_idf', ?)", (json.dumps({"value": lexical["average_idf"], "unit": ref.slice_id}),))
            if self.directory is not None:
                self._directory_register(ref, rows, tombs, holders)
            self._write_current_unlocked(db)
        return ref

    def _directory_register(self, ref: SliceRef, rows: list[tuple], tombs: list[tuple[str, int]], holders: Sequence[str]) -> None:
        """Mirror the registration into the id directory (LA): the slice registers with ``replaces`` =
        every unit holding a live row it supersedes (``holders``: the compacted slices, a slice of
        another day, the archive day member of a closed day an id continued from), so its ids never
        collide; then its tombstones and the retirement of compacted slices. Any exception aborts the
        ledger transaction (fail closed)."""
        directory = self.directory
        assert directory is not None
        provisional = set(ref.catalog_provisional)
        entries = [IdDirectoryEntry(doc_id=i, kind=k, unit_id=ref.slice_id, unit_kind="slice", day=ref.day, version=max(1, v),
                                    catalog_provisional=i in provisional) for i, v, k, *_ in rows]
        directory.register(ref.slice_id, "slice", entries, member=self.member, replaces=list(holders))
        if tombs and not ref.replaces:
            directory.tombstone([d for d, _ in tombs], ref.slice_id)
        for old in ref.replaces:
            directory.retire(old)

    def _holders(self, db: sqlite3.Connection, ref: SliceRef, ids: Sequence[str], live: Mapping[str, tuple[str, int]]) -> list[str]:
        """Registered units of the id directory holding a live row that ``ref`` supersedes (see
        ``_directory_register``)."""
        if ref.replaces:
            return list(ref.replaces)
        units = {live[i][0] for i in ids if i in live and live[i][0] != ref.slice_id}
        # F0 chain B: an id that continued out of a recently archived day; its live row is the
        # archive day member's (M1a rule: the slice registers replaces=["<member>/day:<closed>"]).
        # Between close_day and release_day the ledger still lists the closed day's slice as the
        # holder while the directory already serves the row from the archive day member.
        other_days = self._rows_in(db, "SELECT doc_id, day FROM live WHERE day<>? AND doc_id IN ({})", [ref.day.isoformat()], [i for i in ids if i in live])
        archived = self._rows_in(db, "SELECT doc_id, day FROM archived WHERE doc_id IN ({})", [], [i for i in ids if i not in live])
        for _doc_id, day in [*other_days, *archived]:
            units.add(f"{self.member}/day:{day}")
        unit_of = getattr(self.directory, "unit", None)
        if unit_of is not None:
            kept = set()
            for unit in units:
                found = unit_of(unit)
                if found is not None and found.get("status") == "live":
                    kept.add(unit)
            units = kept
        return sorted(units)

    # --- dense readiness ----------------------------------------------------------------------------------

    def pending_dense(self) -> list[str]:
        with self._db(write=False) as db:
            return [r[0] for r in db.execute("SELECT slice_id FROM slices WHERE status='current' AND dense_ready=0 ORDER BY sequence")]

    def mark_dense_ready(self, slice_id: str) -> None:
        with self._db() as db:
            db.execute("UPDATE slices SET dense_ready=1 WHERE slice_id=?", (slice_id,))

    # --- queries used by close, stats and tests -------------------------------------------------------------

    def live_ids(self, day: date) -> frozenset[str]:
        """Live ids whose row is in a slice of ``day`` (catalog-provisional rows included)."""
        with self._db(write=False) as db:
            return frozenset(r[0] for r in db.execute("SELECT doc_id FROM live WHERE day=?", (day.isoformat(),)))

    def live_rows(self, day: date) -> list[tuple[str, str, int, str, bool]]:
        """``(doc_id, unit_id, version, kind, catalog_provisional)`` of the day's live rows."""
        with self._db(write=False) as db:
            return [(r[0], r[1], int(r[2]), r[3], bool(r[4])) for r in
                    db.execute("SELECT doc_id, unit_id, version, kind, catalog_provisional FROM live WHERE day=? ORDER BY doc_id", (day.isoformat(),))]

    def archived_tombstones(self, day: date) -> dict[str, int]:
        """Tombstones held by the day's current slices that still hide an older row in an archive
        member of an earlier closed day (an id that continued past midnight and then merged away);
        the archive day member carries them so the older row stays hidden after the slices retire."""
        with self._db(write=False) as db:
            rows = db.execute(
                "SELECT t.doc_id, max(t.version) FROM tombs t JOIN slices s ON s.slice_id=t.unit_id JOIN archived a ON a.doc_id=t.doc_id "
                "WHERE s.day=? AND s.status='current' AND a.version < t.version AND t.doc_id NOT IN (SELECT doc_id FROM live) GROUP BY t.doc_id",
                (day.isoformat(),)).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def catalog_pending(self) -> list[Document]:
        with self._db(write=False) as db:
            return [Document.model_validate_json(r[0]) for r in db.execute("SELECT document FROM catalog_pending ORDER BY doc_id")]

    def average_idf(self) -> float | None:
        """The per-source ``average_idf`` for the epsilon floor, maintained at base/day compaction
        (declared approximation: it lags until the next base; §6.5)."""
        with self._db(write=False) as db:
            row = db.execute("SELECT value FROM meta WHERE key='average_idf'").fetchone()
        return None if row is None else float(json.loads(row[0])["value"])

    def slices(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._db(write=False) as db:
            query = "SELECT slice_id, day, level, sequence, status, retired_at, folder FROM slices"
            rows = db.execute(query + (" WHERE status=?" if status else "") + " ORDER BY sequence", (status,) if status else ()).fetchall()
        return [{"slice_id": r[0], "day": r[1], "level": r[2], "sequence": r[3], "status": r[4], "retired_at": r[5], "folder": r[6]} for r in rows]

    # --- close and retention -------------------------------------------------------------------------------

    def release_day(self, day: date) -> list[str]:
        """After the generation that includes the day's archive member is active: retire the day's
        slices (they leave ``CURRENT``), drop its live rows and refuse later refreshes of the day.
        Catalog-provisional rows of the day are dropped too (the catalog member holds them now).
        Returns the retired slice ids."""
        day_s = day.isoformat()
        now = self.clock()
        with self.lock(), self._db() as db:
            retired = [r[0] for r in db.execute("SELECT slice_id FROM slices WHERE day=? AND status='current'", (day_s,))]
            db.execute("UPDATE slices SET status='retired', retired_at=? WHERE day=? AND status='current'", (now, day_s))
            # remember the archived rows for a while: a tombstone of a later live day must keep hiding
            # an older archived row of an id that continued past midnight (compaction checks this)
            db.execute("INSERT OR REPLACE INTO archived SELECT doc_id, day, version FROM live WHERE day=?", (day_s,))
            db.execute("DELETE FROM archived WHERE day<?", ((day - timedelta(days=ARCHIVED_ROW_DAYS)).isoformat(),))
            db.execute("DELETE FROM live WHERE day=?", (day_s,))
            db.execute("DELETE FROM day_rows WHERE day=?", (day_s,))
            db.execute("DELETE FROM catalog_pending WHERE day<=?", (day_s,))
            db.execute("INSERT OR REPLACE INTO closed_days VALUES (?,?)", (day_s, now))
            if self.directory is not None:
                for slice_id in retired:
                    self.directory.retire(slice_id)
            self._write_current_unlocked(db)
        return retired

    def _delete_if_free(self, slice_id: str, now: float) -> bool:
        with self._db() as db:
            row = db.execute("SELECT status, retired_at, folder FROM slices WHERE slice_id=?", (slice_id,)).fetchone()
            if row is None or row[0] not in {"replaced", "retired"} or row[1] is None:
                return False
            if now - float(row[1]) < self.settings.gc_grace_seconds:
                return False
            if self.pins.pinned(slice_id, now):
                return False
            db.execute("UPDATE slices SET status='deleted' WHERE slice_id=?", (slice_id,))
            db.execute("DELETE FROM unit_docs WHERE unit_id=?", (slice_id,))
            db.execute("DELETE FROM tombs WHERE unit_id=?", (slice_id,))
            self._directory_purge(slice_id)
            folder = self.root / row[2]
        shutil.rmtree(folder, ignore_errors=True)
        return True

    def _directory_purge(self, slice_id: str) -> None:
        """The id directory forgets a deleted slice's rows (it is retired and no pin holds it)."""
        purge, unit_of = getattr(self.directory, "purge", None), getattr(self.directory, "unit", None)
        if purge is None or unit_of is None:
            return
        found = unit_of(slice_id)
        if found is not None and found.get("status") == "retired":
            purge(slice_id)

    def gc(self, now: float | None = None) -> list[str]:
        """Delete every replaced/retired slice that no pin holds and whose grace has passed."""
        now = self.clock() if now is None else now
        with self.lock():
            candidates = [s["slice_id"] for s in self.slices() if s["status"] in {"replaced", "retired"}]
            return [s for s in candidates if self._delete_if_free(s, now)]


def embed_pending_slices(ledger: LogSliceLedger, store: Any, encoder: Any, *, batch_size: int = 32, limit: int | None = None) -> list[dict[str, Any]]:
    """The dense worker's tick: for each current slice whose vectors are pending (oldest first),
    embed its texts that the text-hash store does not hold yet and mark it dense-ready. A compacted
    slice usually needs no new vector (its texts were embedded when first sealed). Returns one
    report per slice (texts embedded, seconds). A failing batch raises; the slice stays pending."""
    from .text_vectors import embed_missing

    reports = []
    for slice_id in ledger.pending_dense()[:limit]:
        reader = UnitReader(ledger.unit_dir(slice_id), ledger.bm25, slice_id)
        try:
            report = embed_missing(store, reader.embed()[1], encoder, batch_size=batch_size)
        finally:
            reader.close()
        ledger.mark_dense_ready(slice_id)
        reports.append({"slice_id": slice_id, "texts": report.texts, "seconds": report.seconds})
    return reports
