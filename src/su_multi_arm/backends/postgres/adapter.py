"""``PostgresBackend``: catalog, lexical (FTS), vector (pgvector), records, cache, ledger and session
facets over one PostgreSQL database.

Graph is not served (matrix status ``planned``). Connections: one lazily opened connection per
backend instance in autocommit mode, every operation in its own transaction, serialised by a lock;
a connection that the driver reports broken is discarded and the **next** operation reconnects (the
failed operation itself is never retried). Every driver failure becomes a typed ``BackendError``.
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections import Counter
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from .._scope import MAX_MATCHED_DEFAULT
from ...core.errors import BackendError
from ...core.types import GenerationId, MetadataSchema
from ._db import KIND, check_prefix, check_ts_config, generation_ddl, ident, runtime_ddl, schema_name
from .catalog import PostgresCatalog, PostgresRecords
from .registry import PostgresGenerationRegistry
from .runtime import PostgresCache, PostgresLedger, PostgresSessions
from .search import HnswSettings, PostgresLexical, PostgresVector


def _driver_errors() -> tuple[type[BaseException], ...]:
    try:
        import psycopg
    except ImportError:  # pragma: no cover - exercised only without the extra
        return ()
    return (psycopg.Error,)


class PostgresBackend:
    """One database and one table prefix, seven facets. ``protect(gen)`` marks a generation as
    CURRENT/pinned so ``delete_generation`` refuses it. ``connect`` may be injected (tests)."""

    kind = KIND
    max_matched = MAX_MATCHED_DEFAULT  # typed records-scan cap (backends/_scope.py); an instance may lower it

    def __init__(
        self,
        dsn: str,
        table_prefix: str = "su_multi_arm",
        text_search_config: str = "english",
        timeout_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
        vector_index: str = "exact",
        hnsw: HnswSettings | None = None,
        max_values_cardinality: int = 10000,
        connect: Callable[[], Any] | None = None,
    ):
        if timeout_seconds <= 0:
            raise BackendError(KIND, "init", "timeout_seconds must be positive")
        if max_values_cardinality < 1:
            raise BackendError(KIND, "init", "max_values_cardinality must be positive")
        if vector_index not in {"exact", "hnsw"}:
            raise BackendError(KIND, "init", f"unknown vector index {vector_index!r} (exact | hnsw)")
        self.prefix = check_prefix(table_prefix)
        self.text_search_config = check_ts_config(text_search_config)
        self.timeout = timeout_seconds
        self.clock = clock
        self.vector_index = vector_index
        self.hnsw = hnsw or HnswSettings()
        self.max_values_cardinality = max_values_cardinality
        self.stats: Counter[str] = Counter()
        self._dsn = dsn
        self._connect_fn = connect
        self._errors = _driver_errors()
        self._lock = threading.RLock()
        self._conn: Any = None
        self._runtime_ready = False
        self._protected: set[str] = set()
        self._registry: PostgresGenerationRegistry | None = None
        self.rt = ident(self.prefix)
        self.catalog = PostgresCatalog(self)
        self.lexical = PostgresLexical(self)
        self.vector = PostgresVector(self)
        self.records = PostgresRecords(self)
        self.cache = PostgresCache(self)
        self.ledger = PostgresLedger(self)
        self.session = PostgresSessions(self)

    @classmethod
    def from_config(cls, config: Any, clock: Callable[[], float] = time.time) -> PostgresBackend:
        dsn = os.environ.get(config.dsn_env)
        if not dsn:
            raise BackendError(KIND, "init", f"environment variable {config.dsn_env} (dsn_env) is not set")
        vector = config.vector
        hnsw = HnswSettings(
            m=vector.hnsw_m,
            ef_construction=vector.hnsw_ef_construction,
            ef_search=vector.hnsw_ef_search,
            min_filter_recall=vector.min_filter_recall,
        )
        return cls(dsn, config.table_prefix, config.text_search_config, config.timeout_seconds, clock, vector.index, hnsw)

    # --- connections ---------------------------------------------------------------------------

    def new_connection(self) -> Any:
        """A fresh autocommit connection (the registry's advisory lock holds its own)."""
        if self._connect_fn is not None:
            return self._connect_fn()
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise BackendError(KIND, "init", "psycopg is not installed; install the 'postgres' extra") from exc
        return psycopg.connect(
            self._dsn,
            autocommit=True,
            connect_timeout=max(1, math.ceil(self.timeout)),
            options=f"-c statement_timeout={int(self.timeout * 1000)}",
            prepare_threshold=None,
        )

    def _connection(self) -> Any:
        conn = self._conn
        if conn is None or getattr(conn, "closed", False) or getattr(conn, "broken", False):
            self._conn = conn = self.new_connection()
        return conn

    @contextmanager
    def tx(self, operation: str, runtime: bool = True) -> Iterator[Any]:
        """One transaction and cursor; driver failures become ``BackendError(postgres, operation)``."""
        with self._lock:
            try:
                conn = self._connection()
                with conn.transaction():
                    with conn.cursor() as cur:
                        if runtime and not self._runtime_ready:
                            for statement in runtime_ddl(self.rt):
                                cur.execute(statement)
                        yield cur
                if runtime:
                    self._runtime_ready = True
            except BackendError:
                raise
            except self._errors as exc:
                if getattr(self._conn, "broken", False) or getattr(self._conn, "closed", False):
                    self._conn = None
                raise BackendError(KIND, operation, f"{type(exc).__name__}: {str(exc).strip()}") from exc

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # --- generations ---------------------------------------------------------------------------

    def table(self, name: str) -> str:
        """A runtime table under the prefix schema (``cache``, ``ledger``, ``meta``, ...)."""
        return f"{self.rt}.{ident(name)}"

    def require_generation(self, cur: Any, gen: GenerationId, create: bool = False) -> str:
        """Every generation-scoped operation starts here; returns the quoted schema. Unknown
        generations are a typed error unless ``create`` (write paths)."""
        cur.execute(f"SELECT schema_name, dataset, generation FROM {self.table('generations')} WHERE key = %s", (gen.prefix(),))
        row = cur.fetchone()
        if row is not None:
            if (row[1], row[2]) != (gen.dataset, gen.generation):  # pragma: no cover - key is dataset/generation
                raise BackendError(KIND, "generation", "generation registry row does not match its key")
            return ident(row[0])
        if not create:
            raise BackendError(KIND, "generation", f"unknown generation {gen.prefix()}")
        name = schema_name(self.prefix, gen.dataset, gen.generation)
        cur.execute(f"SELECT key FROM {self.table('generations')} WHERE schema_name = %s", (name,))
        clash = cur.fetchone()
        if clash is not None:
            raise BackendError(KIND, "generation", f"schema {name} already belongs to {clash[0]}")
        schema = ident(name)
        for statement in generation_ddl(schema):
            cur.execute(statement)
        cur.execute(
            f"INSERT INTO {self.table('generations')}(key, dataset, generation, schema_name, index_format, fingerprint, "
            "text_search_config, metadata_schema, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,NULL,%s)",
            (gen.prefix(), gen.dataset, gen.generation, name, gen.index_format, gen.fingerprint, self.text_search_config, self.clock()),
        )
        return schema

    def generation_info(self, cur: Any, gen: GenerationId) -> dict[str, Any]:
        cur.execute(
            f"SELECT schema_name, text_search_config, metadata_schema FROM {self.table('generations')} WHERE key = %s", (gen.prefix(),)
        )
        row = cur.fetchone()
        if row is None:
            raise BackendError(KIND, "generation", f"unknown generation {gen.prefix()}")
        schema = MetadataSchema.model_validate_json(row[2]) if row[2] else MetadataSchema()
        return {"schema": ident(row[0]), "schema_name": row[0], "text_search_config": row[1], "metadata_schema": schema}

    def analyzer_id(self, gen: GenerationId) -> str:
        """The generation's recorded analyzer (``pg_ts_<config>``) for the manifest."""
        with self.tx("analyzer_id") as cur:
            return "pg_ts_" + self.generation_info(cur, gen)["text_search_config"]

    def generation_identity(self, gen: GenerationId) -> dict[str, Any]:
        """The generation's schema and text search configuration as recorded at creation."""
        with self.tx("generation_identity") as cur:
            info = self.generation_info(cur, gen)
        return {"schema": info["schema_name"], "text_search_config": info["text_search_config"]}

    # --- lifecycle -----------------------------------------------------------------------------

    def protect(self, gen: GenerationId) -> None:
        self._protected.add(gen.prefix())

    def unprotect(self, gen: GenerationId) -> None:
        self._protected.discard(gen.prefix())

    def delete_generation(self, gen: GenerationId) -> None:
        """Drops the generation's schema and its cache keys; refused for a protected generation."""
        if gen.prefix() in self._protected:
            raise BackendError(KIND, "delete_generation", "generation is active or pinned by a live session")
        with self.tx("delete_generation") as cur:
            cur.execute(f"SELECT schema_name FROM {self.table('generations')} WHERE key = %s", (gen.prefix(),))
            row = cur.fetchone()
            if row is not None:
                cur.execute(f"DROP SCHEMA IF EXISTS {ident(row[0])} CASCADE")
                cur.execute(f"DELETE FROM {self.table('generations')} WHERE key = %s", (gen.prefix(),))
            cur.execute(f"DELETE FROM {self.table('cache')} WHERE left(key, %s) = %s", (len(gen.prefix()), gen.prefix()))

    def generations(self) -> list[str]:
        with self.tx("generations") as cur:
            cur.execute(f'SELECT key FROM {self.table("generations")} ORDER BY key COLLATE "C"')
            return [r[0] for r in cur.fetchall()]

    def generation_registry(self) -> PostgresGenerationRegistry:
        if self._registry is None:
            self._registry = PostgresGenerationRegistry(self)
        return self._registry

    def probe(self) -> dict[str, Any]:
        """``doctor --probe``: server reachability and version (``ping``); failures are typed."""
        return self.ping()

    def ping(self) -> dict[str, Any]:
        """Server reachability and versions (server, pgvector when installed); typed error when down."""
        with self.tx("ping", runtime=False) as cur:
            cur.execute("SHOW server_version")
            server = cur.fetchone()[0]
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            row = cur.fetchone()
        return {"version": server, "pgvector": row[0] if row else None, "prefix": self.prefix}

    def drop_all(self) -> None:
        """Test helper: drops every generation schema registered under this prefix and the runtime schema."""
        with self.tx("drop_all", runtime=False) as cur:
            cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (self.prefix,))
            if cur.fetchone() is not None:
                cur.execute(f"SELECT schema_name FROM {self.table('generations')}")
                for (name,) in cur.fetchall():
                    cur.execute(f"DROP SCHEMA IF EXISTS {ident(name)} CASCADE")
                cur.execute(f"DROP SCHEMA {self.rt} CASCADE")
        self._runtime_ready = False
