"""``SqlBackend`` (config kind ``sqlalchemy``): catalog, records, cache, ledger and session facets on
any SQLAlchemy dialect through SQLAlchemy Core.

Lexical and vector search are dialect-specific and stay ``planned`` (no facet is exposed, so a
configuration naming them is rejected). Tested dialects: SQLite and PostgreSQL (``TESTED_DIALECTS``);
any other dialect builds, reports ``dialect_tested: false`` from ``ping()`` and is unverified —
MySQL/MariaDB in particular compare text with their (usually case-insensitive) column collation,
which would change ``eq`` semantics. Every driver failure becomes a typed ``BackendError``.
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections import Counter
from contextlib import contextmanager
from typing import Any, Callable, Iterator

import sqlalchemy as sa

from .._scope import MAX_MATCHED_DEFAULT
from ...core.errors import BackendError
from ...core.types import GenerationId, MetadataSchema
from ._db import KIND, TESTED_DIALECTS, check_prefix, generation_tables, generation_token, runtime_tables
from .catalog import SqlCatalog, SqlRecords
from .registry import SqlGenerationRegistry
from .runtime import SqlCache, SqlLedger, SqlSessions


def _connect_args(url: sa.engine.URL, timeout: float) -> dict[str, Any]:
    backend = url.get_backend_name()
    if backend == "sqlite":
        return {"timeout": timeout}
    if backend == "postgresql" and url.get_driver_name() in {"psycopg", "psycopg2"}:
        return {"connect_timeout": max(1, math.ceil(timeout)), "options": f"-c statement_timeout={int(timeout * 1000)}"}
    return {}


class SqlBackend:
    """One SQLAlchemy engine and one table prefix, five facets. ``protect(gen)`` marks a generation as
    CURRENT/pinned so ``delete_generation`` refuses it. ``engine`` may be injected (tests)."""

    kind = KIND
    max_matched = MAX_MATCHED_DEFAULT  # typed records-scan cap (backends/_scope.py); an instance may lower it

    def __init__(
        self,
        url: str | None = None,
        table_prefix: str = "su_multi_arm",
        timeout_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
        engine: Any = None,
        max_values_cardinality: int = 10000,
    ):
        if timeout_seconds <= 0:
            raise BackendError(KIND, "init", "timeout_seconds must be positive")
        if max_values_cardinality < 1:
            raise BackendError(KIND, "init", "max_values_cardinality must be positive")
        self.prefix = check_prefix(table_prefix)
        self.timeout = timeout_seconds
        self.clock = clock
        self.max_values_cardinality = max_values_cardinality
        self.stats: Counter[str] = Counter()
        self._protected: set[str] = set()
        self._registry: SqlGenerationRegistry | None = None
        self._runtime_ready = False
        self._lock = threading.RLock()
        if engine is None:
            if not url:
                raise BackendError(KIND, "init", "a database URL is required")
            try:
                parsed = sa.engine.make_url(url)
                engine = sa.create_engine(parsed, connect_args=_connect_args(parsed, timeout_seconds), pool_pre_ping=True)
            except (sa.exc.ArgumentError, sa.exc.NoSuchModuleError, ImportError) as exc:
                raise BackendError(KIND, "init", f"cannot build an engine for this URL: {type(exc).__name__}: {exc}") from exc
        self.engine = engine
        self.dialect = engine.dialect.name
        self.runtime_md, self.rt = runtime_tables(self.prefix)
        self.catalog = SqlCatalog(self)
        self.records = SqlRecords(self)
        self.cache = SqlCache(self)
        self.ledger = SqlLedger(self)
        self.session = SqlSessions(self)

    @classmethod
    def from_config(cls, config: Any, clock: Callable[[], float] = time.time) -> SqlBackend:
        url = os.environ.get(config.url_env)
        if not url:
            raise BackendError(KIND, "init", f"environment variable {config.url_env} (url_env) is not set")
        return cls(url, config.table_prefix, config.timeout_seconds, clock)

    # --- transactions --------------------------------------------------------------------------

    @contextmanager
    def tx(self, operation: str, runtime: bool = True) -> Iterator[sa.Connection]:
        """One transaction; SQLAlchemy/driver failures become ``BackendError(sqlalchemy, operation)``."""
        with self._lock:
            try:
                with self.engine.begin() as conn:
                    if runtime and not self._runtime_ready:
                        self.runtime_md.create_all(conn, checkfirst=True)
                    yield conn
                if runtime:
                    self._runtime_ready = True
            except BackendError:
                raise
            except sa.exc.SQLAlchemyError as exc:
                raise BackendError(KIND, operation, f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}") from exc

    # --- generations ---------------------------------------------------------------------------

    def tables(self, conn: sa.Connection, gen: GenerationId, create: bool = False) -> dict[str, sa.Table]:
        """The generation's tables; unknown generations are a typed error unless ``create``."""
        g = self.rt["generations"]
        row = conn.execute(sa.select(g.c.token).where(g.c.key == gen.prefix())).first()
        if row is not None:
            return generation_tables(self.prefix, row[0])[1]
        if not create:
            raise BackendError(KIND, "generation", f"unknown generation {gen.prefix()}")
        token = generation_token(gen.dataset, gen.generation)
        clash = conn.execute(sa.select(g.c.key).where(g.c.token == token)).first()
        if clash is not None:  # pragma: no cover - 48-bit digest collision
            raise BackendError(KIND, "generation", f"table token {token} already belongs to {clash[0]}")
        md, tables = generation_tables(self.prefix, token)
        md.create_all(conn)
        conn.execute(
            g.insert().values(
                key=gen.prefix(),
                dataset=gen.dataset,
                generation=gen.generation,
                token=token,
                index_format=gen.index_format,
                fingerprint=gen.fingerprint,
                metadata_schema=None,
                created_at=self.clock(),
            )
        )
        return tables

    def metadata_schema(self, conn: sa.Connection, gen: GenerationId) -> MetadataSchema:
        g = self.rt["generations"]
        row = conn.execute(sa.select(g.c.metadata_schema).where(g.c.key == gen.prefix())).first()
        if row is None:
            raise BackendError(KIND, "generation", f"unknown generation {gen.prefix()}")
        return MetadataSchema.model_validate_json(row[0]) if row[0] else MetadataSchema()

    def generation_identity(self, gen: GenerationId) -> dict[str, Any]:
        return {"table_prefix": self.prefix, "token": generation_token(gen.dataset, gen.generation), "dialect": self.dialect}

    # --- lifecycle -----------------------------------------------------------------------------

    def protect(self, gen: GenerationId) -> None:
        self._protected.add(gen.prefix())

    def unprotect(self, gen: GenerationId) -> None:
        self._protected.discard(gen.prefix())

    def delete_generation(self, gen: GenerationId) -> None:
        """Drops the generation's tables and its cache keys; refused for a protected generation."""
        if gen.prefix() in self._protected:
            raise BackendError(KIND, "delete_generation", "generation is active or pinned by a live session")
        g, cache = self.rt["generations"], self.rt["cache"]
        with self.tx("delete_generation") as conn:
            row = conn.execute(sa.select(g.c.token).where(g.c.key == gen.prefix())).first()
            if row is not None:
                md, _ = generation_tables(self.prefix, row[0])
                md.drop_all(conn, checkfirst=True)
                conn.execute(g.delete().where(g.c.key == gen.prefix()))
            conn.execute(cache.delete().where(cache.c.key.startswith(gen.prefix(), autoescape=True)))

    def generations(self) -> list[str]:
        g = self.rt["generations"]
        with self.tx("generations") as conn:
            return sorted(r[0] for r in conn.execute(sa.select(g.c.key)))

    def generation_registry(self) -> SqlGenerationRegistry:
        if self._registry is None:
            self._registry = SqlGenerationRegistry(self)
        return self._registry

    def probe(self) -> dict[str, Any]:
        """``doctor --probe``: server reachability and version (``ping``); failures are typed."""
        return self.ping()

    def ping(self) -> dict[str, Any]:
        """Reachability, dialect and server version; typed error when the database is unavailable."""
        with self.tx("ping", runtime=False) as conn:
            conn.execute(sa.text("SELECT 1"))
            info = self.engine.dialect.server_version_info
        return {
            "dialect": self.dialect,
            "driver": self.engine.dialect.driver,
            "server_version": ".".join(str(p) for p in info) if info else None,
            "dialect_tested": self.dialect in TESTED_DIALECTS,
            "prefix": self.prefix,
        }

    def drop_all(self) -> None:
        """Test helper: drops every table this prefix created (generations first, then runtime)."""
        with self.tx("drop_all", runtime=False) as conn:
            inspector = sa.inspect(conn)
            if inspector.has_table(self.rt["generations"].name):
                for (token,) in conn.execute(sa.select(self.rt["generations"].c.token)).all():
                    generation_tables(self.prefix, token)[0].drop_all(conn, checkfirst=True)
            self.runtime_md.drop_all(conn, checkfirst=True)
        self._runtime_ready = False

    def close(self) -> None:
        self.engine.dispose()
