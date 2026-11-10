"""Child-record segment index (``pipeline.arms.segment``; opt-in).

A *segment* is a child record of one canonical record (its *parent*): a description, a comment, a
resolution note or any other part the source system keeps per record, with a declared ``kind``.
Segments are not canonical records: they never enter the catalog, the record store, the lexical or
vector projections of the write path, counts or the projection. They live in a sealed sidecar
index built against one generation and are read only by the ``segment`` arm, which maps every
segment hit to its parent, keeps the parent only when the request scope (filters, exclusions and
the principal's ACL predicates) admits it, and cites the best segments as provenance.

Index layout (one directory, all files written before ``manifest.json``, which is the seal):

- ``segments.sqlite``: ``segment(row, id, parent_id, kind, text, field, start, end, seq)`` and an
  FTS5 table over ``text`` for the optional lexical half;
- ``vectors.npy``: float32 unit vectors, row ``i`` = ``segment.row = i``;
- ``manifest.json``: format, dataset, the generation it was anchored against, counts per kind,
  the embedder recipe (provider/model/revision, dimensions, prefixes) and a sha256 per file.

Anchoring (``anchor``): a segment row may name the parent ``field`` it was taken from. ``required``
(default) locates the segment text verbatim in that field of the parent (first exact occurrence)
and stores the offsets; a row without a field or whose text is not found fails the build.
``optional`` keeps such rows as ``anchored: false`` (the segment text itself is then the cited
source text, as for any input field) and counts them in the manifest. A parent that is not in the
generation always fails the build. Nothing is repaired or guessed.

The arm refuses (typed ``SegmentIndexError``) a missing or unsealed index, a digest mismatch, a
dataset mismatch and an embedder recipe different from the query embedder's.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
from pydantic import Field

from ..core.errors import SuError
from ..models import StrictModel

FORMAT = 1
MANIFEST = "manifest.json"
ROWS = "segments.sqlite"
VECTORS = "vectors.npy"
TOKEN = re.compile(r"[A-Za-z0-9]+")


class SegmentIndexError(SuError, RuntimeError):
    """A segment index is missing, unsealed, tampered with or incompatible with the configuration."""


class SegmentRecord(StrictModel):
    """One input row (JSONL)."""

    id: str = Field(min_length=1, max_length=512)
    parent_id: str = Field(min_length=1, max_length=512)
    kind: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1)
    field: str | None = Field(default=None, min_length=1, max_length=128)
    seq: int = 0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def embedder_recipe(embedder: Any) -> dict[str, Any]:
    """What makes query and segment vectors comparable (device and batch size do not)."""
    cfg = embedder.config
    return {**embedder.recipe(), "dimensions": cfg.dimensions, "query_prefix": cfg.query_prefix, "document_prefix": cfg.document_prefix}


def read_segments(path: Path) -> list[SegmentRecord]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if line.strip():
                try:
                    rows.append(SegmentRecord.model_validate_json(line))
                except ValueError as exc:
                    raise SegmentIndexError(f"{path}:{number}: invalid segment row: {exc}") from exc
    return rows


def build_segment_index(
    rows: Sequence[SegmentRecord],
    out_dir: Path,
    *,
    embedder: Any,
    parents: dict[str, Any],
    dataset: str,
    generation: str,
    anchor: str = "required",
    batch_size: int = 64,
) -> dict[str, Any]:
    """Build and seal an index. ``parents``: parent id -> ``Document`` of ``generation`` (the caller
    reads them through the catalog). Returns the manifest."""
    if anchor not in ("required", "optional"):
        raise ValueError("anchor must be 'required' or 'optional'")
    if not rows:
        raise SegmentIndexError("no segment rows")
    out_dir.mkdir(parents=True, exist_ok=True)
    if (out_dir / MANIFEST).exists():
        raise SegmentIndexError(f"{out_dir} already holds a sealed segment index; build into a new directory")
    seen: set[str] = set()
    missing: list[str] = []
    unanchored: list[str] = []
    stored: list[tuple[Any, ...]] = []
    for row in sorted(rows, key=lambda r: (r.parent_id, r.seq, r.id)):
        if row.id in seen:
            raise SegmentIndexError(f"duplicate segment id {row.id!r}")
        seen.add(row.id)
        parent = parents.get(row.parent_id)
        if parent is None:
            missing.append(row.parent_id)
            continue
        start = end = None
        if row.field is not None:
            source = parent.fields.get(row.field)
            at = source.find(row.text) if isinstance(source, str) else -1
            if at >= 0:
                start, end = at, at + len(row.text)
        if start is None:
            unanchored.append(row.id)
        stored.append((len(stored), row.id, row.parent_id, row.kind, row.text, row.field if start is not None else None, start, end, row.seq))
    if missing:
        raise SegmentIndexError(f"{len(set(missing))} parent ids are not in generation {generation}: {sorted(set(missing))[:10]}")
    if unanchored and anchor == "required":
        raise SegmentIndexError(f"{len(unanchored)} segments are not found verbatim in their declared parent field (anchor: required): {unanchored[:10]}")
    db_path = out_dir / ROWS
    if db_path.exists():
        db_path.unlink()
    db = sqlite3.connect(db_path)
    try:
        db.execute("CREATE TABLE segment(row INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, parent_id TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, field TEXT, start INTEGER, end INTEGER, seq INTEGER NOT NULL)")
        db.execute("CREATE INDEX segment_parent ON segment(parent_id)")
        db.executemany("INSERT INTO segment VALUES (?,?,?,?,?,?,?,?,?)", stored)
        db.execute("CREATE VIRTUAL TABLE segment_fts USING fts5(text, content='segment', content_rowid='row', tokenize='unicode61')")
        db.execute("INSERT INTO segment_fts(rowid, text) SELECT row, text FROM segment")
        db.commit()
    finally:
        db.close()
    texts = [r[4] for r in stored]
    blocks = [embedder.encode(texts[i : i + batch_size]) for i in range(0, len(texts), batch_size)]
    vectors = np.ascontiguousarray(np.vstack(blocks), dtype="float32")
    np.save(out_dir / VECTORS, vectors)
    kinds: dict[str, int] = {}
    for r in stored:
        kinds[r[3]] = kinds.get(r[3], 0) + 1
    manifest = {
        "format": FORMAT,
        "sealed": True,
        "dataset": dataset,
        "generation": generation,
        "segments": len(stored),
        "parents": len({r[2] for r in stored}),
        "kinds": dict(sorted(kinds.items())),
        "anchor": anchor,
        "anchored": len(stored) - len(unanchored),
        "unanchored": len(unanchored),
        "embedder": embedder_recipe(embedder),
        "files": {ROWS: sha256_file(db_path), VECTORS: sha256_file(out_dir / VECTORS)},
    }
    (out_dir / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    return manifest


class SegmentIndex:
    """A sealed index opened read-only (digests verified at open)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        manifest_path = self.path / MANIFEST
        if not manifest_path.is_file():
            raise SegmentIndexError(f"no sealed segment index at {self.path} (manifest.json missing)")
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if self.manifest.get("format") != FORMAT or not self.manifest.get("sealed"):
            raise SegmentIndexError(f"segment index {self.path}: unsupported format or unsealed")
        for name in (ROWS, VECTORS):
            expected = self.manifest["files"].get(name)
            if not (self.path / name).is_file() or sha256_file(self.path / name) != expected:
                raise SegmentIndexError(f"segment index {self.path}: {name} is missing or does not match its manifest digest")
        self.vectors = np.load(self.path / VECTORS, mmap_mode="r")
        uri = f"file:{(self.path / ROWS).resolve()}?mode=ro"
        self._db_uri = uri
        db = self._connect()
        try:
            self.parent_of = [None] * self.manifest["segments"]
            self.kind_of = [None] * self.manifest["segments"]
            for row, parent, kind in db.execute("SELECT row, parent_id, kind FROM segment ORDER BY row"):
                self.parent_of[row] = parent
                self.kind_of[row] = kind
        finally:
            db.close()
        if self.vectors.shape[0] != len(self.parent_of):
            raise SegmentIndexError(f"segment index {self.path}: {self.vectors.shape[0]} vectors for {len(self.parent_of)} rows")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_uri, uri=True, check_same_thread=False)

    def check(self, dataset: str, recipe: dict[str, Any]) -> None:
        if self.manifest.get("dataset") != dataset:
            raise SegmentIndexError(f"segment index {self.path} belongs to dataset {self.manifest.get('dataset')!r}, not {dataset!r}")
        if self.manifest.get("embedder") != recipe:
            raise SegmentIndexError(f"segment index {self.path} was embedded with {self.manifest.get('embedder')}, the query embedder is {recipe}; rebuild the index")

    def mask(self, kinds: Iterable[str]) -> np.ndarray | None:
        wanted = set(kinds)
        if not wanted:
            return None
        return np.fromiter((k in wanted for k in self.kind_of), dtype=bool, count=len(self.kind_of))

    def dense(self, vector: np.ndarray, mask: np.ndarray | None) -> list[tuple[int, float]]:
        """Every (allowed) segment ranked by cosine (exact; vectors are unit length)."""
        scores = np.asarray(self.vectors @ np.asarray(vector, dtype="float32"))
        rows = np.arange(len(scores)) if mask is None else np.flatnonzero(mask)
        order = rows[np.argsort(-scores[rows], kind="stable")]
        return [(int(r), float(scores[r])) for r in order]

    def lexical(self, query: str, limit: int, mask: np.ndarray | None) -> list[tuple[int, float]]:
        """BM25 over segment text for an OR of the query's distinct alphanumeric tokens."""
        tokens = list(dict.fromkeys(t.lower() for t in TOKEN.findall(query) if len(t) >= 2))[:32]
        if not tokens:
            return []
        expression = " OR ".join('"' + t + '"' for t in tokens)
        db = self._connect()
        try:
            found = db.execute("SELECT rowid, bm25(segment_fts) FROM segment_fts WHERE segment_fts MATCH ? ORDER BY bm25(segment_fts) LIMIT ?", (expression, limit * 4 if mask is not None else limit)).fetchall()
        finally:
            db.close()
        out = [(int(r), -float(s)) for r, s in found if mask is None or mask[int(r)]]
        return out[:limit]

    def rows(self, rows: Sequence[int]) -> dict[int, dict[str, Any]]:
        if not rows:
            return {}
        db = self._connect()
        try:
            out = {}
            ids = list(dict.fromkeys(int(r) for r in rows))
            for start in range(0, len(ids), 500):
                batch = ids[start : start + 500]
                query = f"SELECT row, id, parent_id, kind, text, field, start, end, seq FROM segment WHERE row IN ({','.join('?' * len(batch))})"
                for row, sid, parent, kind, text, field, s, e, seq in db.execute(query, batch):
                    out[row] = {"segment_id": sid, "parent_id": parent, "segment_kind": kind, "text": text, "field": field, "start": s, "end": e, "seq": seq}
            return out
        finally:
            db.close()


__all__ = ["SegmentIndex", "SegmentIndexError", "SegmentRecord", "build_segment_index", "embedder_recipe", "read_segments"]
