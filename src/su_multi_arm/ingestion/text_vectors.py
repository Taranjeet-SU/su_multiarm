"""Text-hash vector store of the live log source (docs/plans/unified_merge.md §6.7, §6.10; lane LB).

Vectors are keyed by the sha256 of the embedded text, so a text embedded once is never embedded
or stored again, whatever slice, compaction or archive member carries it (unique-text dedupe). The
store is pinned to **one embedding space** (``EmbeddingSpaceRef``: model, dimensions,
normalization, template, epoch); opening it with another space is refused, so vectors of two
spaces never mix (§6.2). Vectors are L2-normalised and stored as **float16** (2 KB per vector at
1,024 dimensions).

Layout::

    <root>/meta.json             format, embedding space, dtype, dimensions
    <root>/index.sqlite          vectors(hash -> shard, row)
    <root>/shards/<n>.f16        append-only rows of ``dimensions`` float16 values

Writes append the rows first and insert the index rows after them (under one file lock), so a
crash leaves at most unreferenced bytes, never an index row without its vector.

**Retention invariant (§6.10).** Only declared kinds/fields are embedded (``embed_fields``), and
log-line text (``LINE_TEXT_FIELDS``) is refused even if a caller declares it: ``embed_rows`` raises
``RetentionViolation`` instead of skipping. Masked exemplar text stays searchable lexically.

**Embedding device.** ``embed_missing`` takes any object with ``encode(texts) -> ndarray`` (the
package's ``providers.embeddings.Embedder``); it embeds in small batches and returns the measured
throughput. Device choice (GPU when allowed and free, else CPU) belongs to the embedder's
configuration (``models.roles.embedder.device``); ``pick_device`` is a helper for producers and
the benchmark.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Protocol

import numpy as np

from ..core.config_logs import LINE_TEXT_FIELDS
from ..core.errors import SuError
from ..core.types_members import EmbeddingSpaceRef

STORE_FORMAT = 1
SHARD_ROWS = 1 << 16  # rows per shard file
HASH_CHARS = 32


class TextVectorsRefused(SuError):
    """The store refuses the request (other embedding space, wrong shape, unknown hash)."""


class RetentionViolation(SuError, ValueError):
    """An embed declaration names log-line text (never embedded, §6.10)."""


class TextEncoder(Protocol):
    def encode(self, texts: list[str]) -> np.ndarray: ...


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:HASH_CHARS]


def check_embed_fields(embed_fields: Mapping[str, Sequence[str]]) -> None:
    """Refuse log-line text in an embed declaration (the config validator's rule, enforced again at
    the point of embedding so no caller can bypass it)."""
    for kind, fields in embed_fields.items():
        bad = sorted(set(fields) & set(LINE_TEXT_FIELDS.get(kind, ())))
        if bad:
            raise RetentionViolation(f"embed_fields.{kind} names log-line text {bad}; log-line text is never embedded (retention invariant)")


@dataclass(frozen=True)
class EmbedRow:
    """One embeddable chunk of a unit: ``chunk_id`` of the lexical row, its document, field and the
    hash of its text."""

    chunk_id: str
    doc_id: str
    field: str
    hash: str


def embed_rows(chunks: Iterable[Any], kinds: Mapping[str, str], embed_fields: Mapping[str, Sequence[str]],
               compose: Callable[[Any], str] | None = None) -> tuple[list[EmbedRow], dict[str, str]]:
    """The embeddable chunks among ``chunks`` (``Chunk`` objects) and ``{hash: text}`` of their
    texts. ``kinds`` maps document id to kind. Only fields declared for the kind are embedded.
    ``compose`` turns a chunk into the embedded text (the space's template; default: the chunk text)."""
    check_embed_fields(embed_fields)
    allowed = {kind: frozenset(fields) for kind, fields in embed_fields.items()}
    rows: list[EmbedRow] = []
    texts: dict[str, str] = {}
    for chunk in chunks:
        fields = allowed.get(kinds.get(chunk.document_id, ""))
        if not fields or chunk.field not in fields or not chunk.text.strip():
            continue
        text = chunk.text if compose is None else compose(chunk)
        digest = text_hash(text)
        texts.setdefault(digest, text)
        rows.append(EmbedRow(chunk.id, chunk.document_id, chunk.field, digest))
    return rows, texts


class TextVectorStore:
    """Content-addressed float16 vectors of one embedding space (see the module doc)."""

    def __init__(self, root: str | Path, space: EmbeddingSpaceRef):
        self.root = Path(root)
        self.space = space
        self.dims = space.dimensions
        self.row_bytes = self.dims * 2
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "shards").mkdir(exist_ok=True)
        meta = {"format": STORE_FORMAT, "space": space.model_dump(mode="json"), "dtype": "float16", "dimensions": self.dims}
        meta_path = self.root / "meta.json"
        with self._lock():
            if meta_path.exists():
                found = json.loads(meta_path.read_text())
                if found != meta:
                    raise TextVectorsRefused(f"text vector store {self.root.name} holds embedding space {found.get('space', {}).get('id')!r}, "
                                             f"not {space.id!r} (another model, template or epoch); vectors of two spaces never mix")
            else:
                meta_path.write_text(json.dumps(meta, sort_keys=True))
            with self._db() as db:
                db.execute("CREATE TABLE IF NOT EXISTS vectors(hash TEXT PRIMARY KEY, shard INTEGER NOT NULL, row INTEGER NOT NULL) WITHOUT ROWID")
        self._maps: dict[int, np.ndarray] = {}
        self._maps_lock = threading.Lock()

    @contextmanager
    def _lock(self) -> Iterator[None]:
        with open(self.root / ".lock", "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.root / "index.sqlite", timeout=60)) as db:
            with db:
                yield db

    def _shard_path(self, shard: int) -> Path:
        return self.root / "shards" / f"{shard:06d}.f16"

    def _known(self, db: sqlite3.Connection, hashes: Sequence[str]) -> dict[str, tuple[int, int]]:
        found: dict[str, tuple[int, int]] = {}
        unique = list(dict.fromkeys(hashes))
        for start in range(0, len(unique), 900):
            part = unique[start : start + 900]
            marks = ",".join("?" * len(part))
            for h, shard, row in db.execute(f"SELECT hash, shard, row FROM vectors WHERE hash IN ({marks})", part):
                found[h] = (int(shard), int(row))
        return found

    def missing(self, hashes: Iterable[str]) -> list[str]:
        """The hashes without a vector, in first-seen order."""
        wanted = list(dict.fromkeys(hashes))
        with self._db() as db:
            known = self._known(db, wanted)
        return [h for h in wanted if h not in known]

    def put(self, hashes: Sequence[str], vectors: np.ndarray) -> int:
        """Store vectors for ``hashes`` (already present hashes are skipped); returns rows written."""
        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.shape != (len(hashes), self.dims) or not np.isfinite(vectors).all():
            raise TextVectorsRefused(f"vectors must be finite with shape ({len(hashes)}, {self.dims}); got {vectors.shape}")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = vectors / np.maximum(norms, 1e-12)
        with self._lock(), self._db() as db:
            known = self._known(db, hashes)
            fresh = [(i, h) for i, h in enumerate(hashes) if h not in known]
            seen: set[str] = set()
            fresh = [(i, h) for i, h in fresh if not (h in seen or seen.add(h))]
            if not fresh:
                return 0
            shard, used = self._tail(db)
            rows: list[tuple[str, int, int]] = []
            position = 0
            while position < len(fresh):
                if used >= SHARD_ROWS:
                    shard, used = shard + 1, 0
                take = fresh[position : position + (SHARD_ROWS - used)]
                block = np.ascontiguousarray(vectors[[i for i, _ in take]].astype(np.float16))
                with open(self._shard_path(shard), "ab") as handle:
                    handle.write(block.tobytes())
                    handle.flush()
                rows.extend((h, shard, used + j) for j, (_, h) in enumerate(take))
                used += len(take)
                position += len(take)
            db.executemany("INSERT INTO vectors VALUES (?,?,?)", rows)
        return len(rows)

    def _tail(self, db: sqlite3.Connection) -> tuple[int, int]:
        """(last shard, rows referenced in it); bytes beyond the referenced rows (a crash between
        the append and the index insert) are truncated before appending."""
        row = db.execute("SELECT max(shard) FROM vectors").fetchone()
        shard = int(row[0]) if row and row[0] is not None else 0
        used = int(db.execute("SELECT count(*) FROM vectors WHERE shard=?", (shard,)).fetchone()[0])
        path = self._shard_path(shard)
        if path.exists() and path.stat().st_size != used * self.row_bytes:
            with open(path, "r+b") as handle:
                handle.truncate(used * self.row_bytes)
            with self._maps_lock:
                self._maps.pop(shard, None)
        return shard, used

    def _map(self, shard: int, need_rows: int) -> np.ndarray:
        with self._maps_lock:
            found = self._maps.get(shard)
            if found is None or len(found) < need_rows:
                path = self._shard_path(shard)
                rows = path.stat().st_size // self.row_bytes
                found = np.memmap(path, dtype=np.float16, mode="r", shape=(rows, self.dims))
                self._maps[shard] = found
            return found

    def get(self, hashes: Sequence[str]) -> np.ndarray:
        """Vectors of ``hashes`` as float32 (n, dims); an unknown hash is refused."""
        out = np.empty((len(hashes), self.dims), dtype=np.float32)
        if not len(hashes):
            return out
        with self._db() as db:
            known = self._known(db, hashes)
        unknown = [h for h in hashes if h not in known]
        if unknown:
            raise TextVectorsRefused(f"{len(unknown)} text hashes have no vector (e.g. {unknown[0]})")
        by_shard: dict[int, list[tuple[int, int]]] = {}
        for i, h in enumerate(hashes):
            shard, row = known[h]
            by_shard.setdefault(shard, []).append((i, row))
        for shard, pairs in by_shard.items():
            need = max(r for _, r in pairs) + 1
            data = self._map(shard, need)
            out[[i for i, _ in pairs]] = data[[r for _, r in pairs]].astype(np.float32)
        return out

    def get_unique(self, hashes: Sequence[str]) -> tuple[np.ndarray, np.ndarray] | None:
        """``(vectors of the distinct hashes as float32, index of each input hash into them)`` with one
        index lookup per distinct hash, read in (shard, row) order; ``None`` when any hash has no
        vector yet (the caller reports it pending, never guesses)."""
        unique = list(dict.fromkeys(hashes))
        position = {h: i for i, h in enumerate(unique)}
        inverse = np.fromiter((position[h] for h in hashes), dtype=np.int64, count=len(hashes))
        with self._db() as db:
            known = self._known(db, unique)
        if len(known) != len(unique):
            return None
        out = np.empty((len(unique), self.dims), dtype=np.float32)
        by_shard: dict[int, list[tuple[int, int]]] = {}
        for i, h in enumerate(unique):
            shard, row = known[h]
            by_shard.setdefault(shard, []).append((row, i))
        for shard, pairs in by_shard.items():
            pairs.sort()
            data = self._map(shard, pairs[-1][0] + 1)
            out[[i for _, i in pairs]] = data[[r for r, _ in pairs]].astype(np.float32)
        return out, inverse

    def count(self) -> int:
        with self._db() as db:
            return int(db.execute("SELECT count(*) FROM vectors").fetchone()[0])

    def disk_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())


@dataclass
class EmbedReport:
    texts: int = 0
    stored: int = 0
    seconds: float = 0.0

    @property
    def texts_per_second(self) -> float:
        return self.texts / self.seconds if self.seconds else 0.0


def embed_missing(store: TextVectorStore, texts: Mapping[str, str], encoder: TextEncoder, *, batch_size: int = 32) -> EmbedReport:
    """Embed the texts of ``texts`` (``{hash: text}``) that have no vector yet, in batches of
    ``batch_size``, and store them. A failing batch raises (nothing is skipped); batches stored
    before it stay (they are valid vectors of the same space)."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    todo = store.missing(texts)
    report = EmbedReport()
    started = time.perf_counter()
    for start in range(0, len(todo), batch_size):
        batch = todo[start : start + batch_size]
        for h in batch:
            if text_hash(texts[h]) != h:
                raise TextVectorsRefused(f"text of hash {h} does not hash to it")
        vectors = encoder.encode([texts[h] for h in batch])
        report.stored += store.put(batch, vectors)
        report.texts += len(batch)
    report.seconds = time.perf_counter() - started
    return report


def pick_device(allow_gpu: bool = True, min_free_bytes: int = 4 << 30) -> str:
    """``cuda`` when allowed, available and at least ``min_free_bytes`` free (other services share
    the GPU), else ``cpu``. Never raises for a missing torch/CUDA: the answer is then ``cpu``."""
    if not allow_gpu:
        return "cpu"
    try:
        import torch

        if not torch.cuda.is_available():
            return "cpu"
        free, _total = torch.cuda.mem_get_info()
        return "cuda" if free >= min_free_bytes else "cpu"
    except Exception:  # noqa: BLE001 - no torch or no driver: CPU
        return "cpu"
