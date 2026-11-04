"""Imported document vectors (``models.roles.embedder.precomputed``).

Each chunk is matched to an imported key by (document, field, SHA-256 of its exact text): nothing
fuzzy, and a chunk without a key is an error unless ``on_missing: embed``. The key on line ``i`` of
the keys file names row ``i`` of the vectors file; that row/key pairing is positional and cannot be
proven from the two files. What the audit does prove: both files are byte-identical to the pinned
digests (``sha256``, ``keys_sha256``, both recorded in the manifest), row and key counts agree,
rows have the declared dimension, are finite and non-zero, and keys are unique. The optional
``verify`` spot check re-embeds sampled chunks with the configured provider and compares them with
their rows; it is the check that detects a permuted or mismatched keys file, and also tests the
claim that the rows came from the configured model.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


class PrecomputedVectorError(ValueError):
    """The imported vectors failed the audit; ``unit`` names the file (and line) at fault."""

    def __init__(self, projection: str, unit: str, message: str):
        self.projection, self.unit = projection, unit
        super().__init__(f"{unit}: {message}")


NORM_TOLERANCE = 1e-3
# What an imported row embeds: whatever the producer embedded for that text. The engine's own input
# template (title + field + text) does not apply to imported rows.
IMPORTED_INPUT = "imported: row produced outside the engine for the keyed chunk text; producer template not recorded"


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class PrecomputedAudit:
    rows: int
    dimensions: int
    sha256: str
    keys_sha256: str
    normalised: bool
    matched: int = 0
    embedded: int = 0
    unused: int = 0
    verify: dict[str, Any] | None = None
    problems: list[str] = field(default_factory=list)

    def manifest(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "dimensions": self.dimensions,
            "sha256": self.sha256,
            "keys_sha256": self.keys_sha256,
            "pairing": "positional (row i <-> keys line i); pinned by both digests",
            "input": IMPORTED_INPUT,
            "normalised": self.normalised,
            "matched": self.matched,
            "embedded_by_provider": self.embedded,
            "unused_rows": self.unused,
            "verify": self.verify,
        }


class PrecomputedVectors:
    """Loaded, audited rows; ``lookup`` returns the row for a chunk or ``None``."""

    def __init__(self, config: Any, dimensions: int):
        self.config = config
        vectors_path, keys_path = Path(config.vectors), Path(config.keys)
        for path in (vectors_path, keys_path):
            if not path.is_file():
                raise PrecomputedVectorError("vector", str(path), "precomputed vector file is missing")
        digest = file_sha256(vectors_path)
        if digest != config.sha256:
            raise PrecomputedVectorError("vector", str(vectors_path), f"precomputed vectors sha256 {digest} differs from the pinned {config.sha256}")
        keys_digest = file_sha256(keys_path)
        if keys_digest != config.keys_sha256:
            raise PrecomputedVectorError("vector", str(keys_path), f"precomputed keys sha256 {keys_digest} differs from the pinned {config.keys_sha256}")
        matrix = np.load(vectors_path, mmap_mode="r", allow_pickle=False)
        if matrix.ndim != 2:
            raise PrecomputedVectorError("vector", str(vectors_path), "precomputed vectors must be a 2-D array")
        if matrix.shape[1] != dimensions:
            raise PrecomputedVectorError("vector", str(vectors_path), f"precomputed vectors have {matrix.shape[1]} dimensions, the embedder declares {dimensions}")
        self.index: dict[tuple[str, str, str], int] = {}
        with open(keys_path) as handle:
            for row, line in enumerate(handle):
                try:
                    item = json.loads(line)
                    key = (str(item["document_id"]), str(item["field"]), str(item["text_sha256"]))
                except (ValueError, KeyError, TypeError) as exc:
                    raise PrecomputedVectorError("vector", f"{keys_path}:{row + 1}", f"unreadable precomputed key: {exc}") from exc
                if key in self.index:
                    raise PrecomputedVectorError("vector", f"{keys_path}:{row + 1}", f"duplicate precomputed key for document {key[0]!r} field {key[1]!r}")
                self.index[key] = row
        if len(self.index) != matrix.shape[0]:
            raise PrecomputedVectorError("vector", str(keys_path), f"{len(self.index)} keys for {matrix.shape[0]} vector rows")
        norms = np.empty(matrix.shape[0], dtype=np.float64)
        for start in range(0, matrix.shape[0], 65536):
            block = np.asarray(matrix[start : start + 65536], dtype=np.float32)
            if not np.isfinite(block).all():
                raise PrecomputedVectorError("vector", str(vectors_path), f"non-finite values in rows {start}..{start + len(block) - 1}")
            norms[start : start + len(block)] = np.linalg.norm(block, axis=1)
        if (norms == 0).any():
            raise PrecomputedVectorError("vector", str(vectors_path), f"row {int(np.argmin(norms))} is a zero vector")
        self.matrix = matrix
        self.audit = PrecomputedAudit(
            rows=int(matrix.shape[0]),
            dimensions=int(matrix.shape[1]),
            sha256=digest,
            keys_sha256=keys_digest,
            normalised=bool(np.all(np.abs(norms - 1) <= NORM_TOLERANCE)),
        )
        self._used: set[int] = set()

    def lookup(self, document_id: str, field_name: str, text: str) -> np.ndarray | None:
        row = self.index.get((document_id, field_name, text_sha256(text)))
        if row is None:
            return None
        self._used.add(row)
        return np.asarray(self.matrix[row], dtype=np.float32)

    def finish(self) -> PrecomputedAudit:
        self.audit.unused = self.audit.rows - len(self._used)
        return self.audit


def spot_check(imported: "PrecomputedVectors", chunks: list[Any], encode: Any, sample: int, min_cosine: float, seed: int) -> dict[str, Any]:
    """Re-embed up to ``sample`` matched chunks (seeded choice) with ``encode(texts)`` and compare each
    with its imported row. Raises ``PrecomputedVectorError`` when any cosine is below ``min_cosine``."""
    import random

    matched = [c for c in chunks if (c.document_id, c.field, text_sha256(c.text)) in imported.index]
    if not matched:
        return {"sample": 0, "min_cosine_required": min_cosine, "checked": 0}
    picked = random.Random(seed).sample(matched, min(sample, len(matched)))
    fresh = np.asarray(encode([c.text for c in picked]), dtype=np.float32)
    rows = np.vstack([np.asarray(imported.matrix[imported.index[(c.document_id, c.field, text_sha256(c.text))]], dtype=np.float32) for c in picked])
    fresh /= np.linalg.norm(fresh, axis=1, keepdims=True)
    rows /= np.linalg.norm(rows, axis=1, keepdims=True)
    cosines = (fresh * rows).sum(axis=1)
    worst = int(np.argmin(cosines))
    report = {"checked": len(picked), "min_cosine_required": min_cosine, "min": round(float(cosines.min()), 6), "median": round(float(np.median(cosines)), 6), "below": int((cosines < min_cosine).sum())}
    if report["below"]:
        bad = picked[worst]
        raise PrecomputedVectorError(
            "vector",
            f"{bad.document_id}/{bad.field}",
            f"spot check: {report['below']} of {len(picked)} re-embedded chunks fall below cosine {min_cosine} (min {report['min']}); the vectors and keys files are not paired or not from the configured model",
        )
    return report
