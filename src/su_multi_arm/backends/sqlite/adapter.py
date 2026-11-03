"""``SqliteBackend``: the local default adapter, all eight capabilities on the frozen contracts."""

from __future__ import annotations

import shutil
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .._scope import MAX_MATCHED_DEFAULT
from ...core.errors import BackendError
from ...core.types import GenerationId
from ._db import CATALOG_SCHEMA, KIND, connect, ensure_runtime
from .catalog import SqliteCatalog, SqliteRecords
from .graph import SqliteGraph
from .runtime import SqliteCache, SqliteLedger, SqliteSessions
from .search import SqliteLexical, SqliteVector


class SqliteBackend:
    """One root directory, eight facets. ``protect(gen)`` marks a generation as CURRENT/pinned."""

    kind = KIND
    max_matched = MAX_MATCHED_DEFAULT  # typed records-scan cap (backends/_scope.py); an instance may lower it

    def __init__(
        self,
        path: str | Path,
        vector_engine: str = "numpy",
        timeout_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
    ):
        if vector_engine not in {"numpy", "faiss"}:
            raise BackendError(KIND, "init", f"unknown vector engine {vector_engine!r}")
        self.root = Path(path)
        self.vector_engine = vector_engine
        self.timeout = timeout_seconds
        self.clock = clock
        self.stats: Counter[str] = Counter()
        self._protected: set[str] = set()
        self._registry: Any = None
        self.catalog = SqliteCatalog(self)
        self.lexical = SqliteLexical(self)
        self.vector = SqliteVector(self)
        self.records = SqliteRecords(self)
        self.graph = SqliteGraph(self)
        self.cache = SqliteCache(self)
        self.ledger = SqliteLedger(self)
        self.session = SqliteSessions(self)

    @classmethod
    def from_config(cls, config: Any, clock: Callable[[], float] = time.time) -> SqliteBackend:
        return cls(config.path, config.vector_engine, config.timeout_seconds, clock)

    # --- generation files -------------------------------------------------------------------

    def snapshot_dir(self, gen: GenerationId) -> Path:
        return self.root / "snapshots" / gen.generation

    def catalog_path(self, gen: GenerationId) -> Path:
        return self.snapshot_dir(gen) / "catalog.sqlite"

    def catalog_db_exists(self, gen: GenerationId, create: bool = False) -> Path:
        path = self.catalog_path(gen)
        if not path.exists():
            if not create:
                raise BackendError(KIND, "generation", f"unknown generation {gen.prefix()}")
            path.parent.mkdir(parents=True, exist_ok=True)
            with connect(path, self.timeout) as db:
                db.executescript(CATALOG_SCHEMA)
        return path

    @contextmanager
    def catalog_db(self, gen: GenerationId, create: bool = False) -> Iterator[sqlite3.Connection]:
        path = self.catalog_db_exists(gen, create=create)
        try:
            with connect(path, self.timeout) as db:
                yield db
        except sqlite3.Error as exc:
            raise BackendError(KIND, "catalog", str(exc)) from exc

    @contextmanager
    def runtime_db(self) -> Iterator[sqlite3.Connection]:
        path = self.root / "runtime.sqlite"
        if not path.exists():
            ensure_runtime(path, self.timeout)
        try:
            with connect(path, self.timeout) as db:
                yield db
        except sqlite3.Error as exc:
            raise BackendError(KIND, "runtime", str(exc)) from exc

    # --- lifecycle ---------------------------------------------------------------------------

    def probe(self) -> dict[str, Any]:
        """``doctor --probe``: the runtime database opens and answers; the root is a directory."""
        if self.root.exists() and not self.root.is_dir():
            raise BackendError(KIND, "probe", f"state path {self.root} is not a directory")
        with self.runtime_db() as db:
            version = db.execute("SELECT sqlite_version()").fetchone()[0]
        return {"sqlite_version": version, "root": str(self.root), "generations": len(self.generations()), "vector_engine": self.vector_engine}

    def protect(self, gen: GenerationId) -> None:
        self._protected.add(gen.prefix())

    def unprotect(self, gen: GenerationId) -> None:
        self._protected.discard(gen.prefix())

    def delete_generation(self, gen: GenerationId) -> None:
        if gen.prefix() in self._protected:
            raise BackendError(KIND, "delete_generation", "generation is active or pinned by a live session")
        self.vector.forget(gen)
        folder = self.snapshot_dir(gen)
        if folder.exists():
            shutil.rmtree(folder)
        if (self.root / "runtime.sqlite").exists():
            self.cache.invalidate(gen.prefix())

    def generations(self) -> list[str]:
        base = self.root / "snapshots"
        if not base.exists():
            return []
        return sorted(f"{self.dataset_of(p.name)}/{p.name}/" for p in base.iterdir() if (p / "catalog.sqlite").exists())

    def generation_identity(self, gen: GenerationId) -> dict[str, Any]:
        """Relative to the backend root (no host paths in manifests)."""
        return {"snapshot": f"snapshots/{gen.generation}"}

    def dataset_of(self, generation: str) -> str:
        """Dataset recorded in the generation's manifest when present (for listings only)."""
        manifest = self.generation_registry().manifest(generation)
        return manifest["dataset"] if manifest else "?"

    def generation_registry(self):
        from ...ingestion.release import FileGenerationRegistry

        if self._registry is None:
            self._registry = FileGenerationRegistry(self.root)
        return self._registry
