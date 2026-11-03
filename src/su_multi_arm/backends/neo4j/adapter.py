"""``Neo4jBackend``: the ``graph`` facet (plus a generation pointer) over one Neo4j database.

Graph is the only declared capability (plan §3.5 ``neo4j`` row). Every statement is one of the fixed
templates in ``_cypher.py``; transactions are explicit (never the driver's managed, auto-retrying
``execute_*`` helpers), so a failure surfaces once as a typed ``BackendError`` and is never retried
or downgraded. An unreachable server, refused credentials, a constraint violation or a failed
statement all map to ``BackendError(adapter="neo4j", operation, message)``.
"""

from __future__ import annotations

import os
import re
import time
from collections import Counter
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from ...core.errors import BackendError
from ...core.types import GenerationId
from . import _cypher as q
from .graph import Neo4jGraph

KIND = q.KIND
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def check_name(value: str, what: str) -> str:
    """Namespaces and datasets become key components joined by ``|``: refuse anything else."""
    if not isinstance(value, str) or not NAME_RE.match(value):
        raise BackendError(KIND, "naming", f"{what} {value!r} must match {NAME_RE.pattern}")
    return value


@contextmanager
def mapped(operation: str) -> Iterator[None]:
    """Translate every driver/server failure into a typed ``BackendError``; never retried."""
    try:
        from neo4j.exceptions import DriverError, Neo4jError
    except ImportError:  # only an injected driver can be in use; it cannot raise driver errors
        DriverError = Neo4jError = ()  # type: ignore[assignment,misc]
    try:
        yield
    except BackendError:
        raise
    except Neo4jError as exc:
        code = getattr(exc, "code", None) or type(exc).__name__
        raise BackendError(KIND, operation, f"{code}: {getattr(exc, 'message', None) or exc}") from exc
    except DriverError as exc:
        raise BackendError(KIND, operation, f"{type(exc).__name__}: {exc}") from exc


class Neo4jBackend:
    """One database, one ``namespace``, one facet (``graph``). ``protect(gen)`` marks a generation as
    CURRENT/pinned in this process; the persisted pointer node additionally protects the generation
    it names from ``delete_generation`` in every process. ``driver`` may be injected (tests)."""

    kind = KIND

    def __init__(
        self,
        uri: str,
        user: str,
        password: str,
        database: str = "neo4j",
        namespace: str = "su_multi_arm",
        timeout_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
        driver: Any = None,
    ):
        if timeout_seconds <= 0:
            raise BackendError(KIND, "init", "timeout_seconds must be positive")
        self.namespace = check_name(namespace, "namespace")
        self.database = check_name(database, "database")
        self.timeout = timeout_seconds
        self.clock = clock
        self.stats: Counter[str] = Counter()
        self._protected: set[str] = set()
        self._schema_ready = False
        if driver is None:
            try:
                from neo4j import GraphDatabase
            except ImportError as exc:  # pragma: no cover - exercised only without the extra
                raise BackendError(KIND, "init", "the neo4j driver is not installed; install the 'neo4j' extra") from exc
            with mapped("init"):
                driver = GraphDatabase.driver(
                    uri,
                    auth=(user, password),
                    connection_timeout=timeout_seconds,
                    connection_acquisition_timeout=timeout_seconds,
                    max_transaction_retry_time=0,
                )
        self.driver = driver
        self.graph = Neo4jGraph(self)

    @classmethod
    def from_config(cls, config: Any, clock: Callable[[], float] = time.time) -> Neo4jBackend:
        password = os.environ.get(config.password_env)
        if not password:
            raise BackendError(KIND, "init", f"environment variable {config.password_env} (password_env) is not set")
        return cls(config.uri, config.user, password, config.database, config.namespace, config.timeout_seconds, clock)

    # --- keys ----------------------------------------------------------------------------------

    def gen_key(self, gen: GenerationId) -> str:
        return f"{self.namespace}|{check_name(gen.dataset, 'dataset')}|{gen.generation}"

    def pointer_key(self, dataset: str) -> str:
        return f"{self.namespace}|{check_name(dataset, 'dataset')}"

    # --- statements ----------------------------------------------------------------------------

    def _check(self, statement: str) -> None:
        if statement not in _TEMPLATES:  # defence in depth: only the fixed templates ever run
            raise BackendError(KIND, "query", "refusing a statement that is not a fixed template")

    @contextmanager
    def transaction(self, operation: str, write: bool) -> Iterator[Callable[..., list[dict[str, Any]]]]:
        """An explicit transaction: committed when the block exits normally, rolled back otherwise.
        Yields ``run(statement, **params) -> rows``."""
        self.ensure_schema()
        with mapped(operation):
            with self.driver.session(database=self.database, default_access_mode="WRITE" if write else "READ") as session:
                tx = session.begin_transaction(timeout=self.timeout)
                try:

                    def run(statement: str, **params: Any) -> list[dict[str, Any]]:
                        self._check(statement)
                        self.stats[f"{operation}.statements"] += 1
                        return [record.data() for record in tx.run(statement, params)]

                    yield run
                    tx.commit()
                finally:
                    if not tx.closed():
                        tx.rollback()

    def autocommit(self, operation: str, statement: str, **params: Any) -> list[dict[str, Any]]:
        """Auto-commit statement (schema DDL and ``CALL {...} IN TRANSACTIONS`` deletes only)."""
        self._check(statement)
        with mapped(operation):
            with self.driver.session(database=self.database) as session:
                return [record.data() for record in session.run(statement, params)]

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        for statement in q.SCHEMA:
            self.autocommit("schema", statement)
        self._schema_ready = True

    # --- generations and the pointer -----------------------------------------------------------

    def require_generation(self, run: Callable[..., list[dict[str, Any]]], gen: GenerationId) -> None:
        if not run(q.GENERATION_STATE, g=self.gen_key(gen)):
            raise BackendError(KIND, "generation", f"unknown generation {gen.prefix()}")

    def activate_generation(self, gen: GenerationId) -> None:
        """Point ``<namespace>|<dataset>`` at ``gen`` in one statement; refused (pointer unchanged) when the
        generation has no committed load."""
        with self.transaction("activate", write=True) as run:
            rows = run(q.POINTER_ACTIVATE, g=self.gen_key(gen), pointer=self.pointer_key(gen.dataset), generation=gen.generation, now=self.clock())
        if not rows:
            raise BackendError(KIND, "activate", f"generation {gen.prefix()} has no committed load; pointer unchanged")

    def deactivate_generation(self, gen: GenerationId) -> None:
        """Remove the pointer only when it still names ``gen`` (rollback of a failed release)."""
        with self.transaction("deactivate", write=True) as run:
            run(q.POINTER_DEACTIVATE, g=self.gen_key(gen), pointer=self.pointer_key(gen.dataset))

    def active_generation(self, dataset: str) -> str | None:
        with self.transaction("pointer", write=False) as run:
            rows = run(q.POINTER_READ, pointer=self.pointer_key(dataset))
        return rows[0]["generation"] if rows else None

    def generation_identity(self, gen: GenerationId) -> dict[str, Any]:
        return {"database": self.database, "namespace": self.namespace, "key": self.gen_key(gen), "pointer": self.pointer_key(gen.dataset)}

    def protect(self, gen: GenerationId) -> None:
        self._protected.add(gen.prefix())

    def unprotect(self, gen: GenerationId) -> None:
        self._protected.discard(gen.prefix())

    def delete_generation(self, gen: GenerationId) -> None:
        """Refused for a protected generation or the one the pointer names. The generation node goes
        first (with the pointer check, in one transaction), then the data in batches."""
        if gen.prefix() in self._protected:
            raise BackendError(KIND, "delete_generation", "generation is active or pinned by a live session")
        key = self.gen_key(gen)
        with self.transaction("delete_generation", write=True) as run:
            pointer = run(q.POINTER_READ, pointer=self.pointer_key(gen.dataset))
            if pointer and pointer[0]["g"] == key:
                raise BackendError(KIND, "delete_generation", "generation is the active one (pointer node)")
            run(q.GENERATION_DROP, g=key)
        for statement in q.DATA_DROP:
            self.autocommit("delete_generation", statement, g=key)

    def generations(self) -> list[str]:
        with self.transaction("generations", write=False) as run:
            rows = run(q.GENERATIONS_LIST, namespace=self.namespace)
        return [f"{r['dataset']}/{r['generation']}/" for r in rows]

    # --- operations ----------------------------------------------------------------------------

    def probe(self) -> dict[str, Any]:
        """``doctor --probe``: server reachability and version (``ping``); failures are typed."""
        return self.ping()

    def ping(self) -> dict[str, Any]:
        """Server reachability, version and edition; a typed error when unavailable."""
        with mapped("ping"):
            self.driver.verify_connectivity()
            info = self.driver.get_server_info()
        return {"agent": getattr(info, "agent", None), "protocol": str(getattr(info, "protocol_version", "")), "database": self.database, "namespace": self.namespace}

    def drop_namespace(self) -> None:
        """Test helper: removes every node under this namespace (never another namespace's)."""
        for statement in q.NAMESPACE_DROP:
            self.autocommit("drop_namespace", statement, prefix=self.namespace + "|")

    def close(self) -> None:
        with mapped("close"):
            self.driver.close()


_TEMPLATES = frozenset(q.ALL_TEMPLATES)
