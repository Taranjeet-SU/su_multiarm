"""``KuzuBackend``: the ``graph`` facet (plus a generation pointer) over one embedded Kuzu database.

Graph is the only capability. No service is involved: the database is a local file opened in-process
(the optional ``kuzu`` extra). Every statement is one of the fixed templates in ``_cypher.py``; a
write is one explicit transaction (``BEGIN TRANSACTION`` ... ``COMMIT``, rolled back on any error),
never retried or downgraded. Every Kuzu failure maps to ``BackendError(adapter="kuzu", operation,
message)``. One connection serves the process; calls are serialised by a lock (Kuzu admits one
write transaction at a time and a connection is not shared across threads concurrently).
"""

from __future__ import annotations

import re
import threading
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from ...core.errors import BackendError
from ...core.types import GenerationId
from ...ingestion.lease import stale_base
from ...ingestion.release import ANY
from . import _cypher as q
from .graph import KuzuGraph

KIND = q.KIND
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def check_name(value: str, what: str) -> str:
    """Namespaces and datasets become key components joined by ``|``: refuse anything else."""
    if not isinstance(value, str) or not NAME_RE.match(value):
        raise BackendError(KIND, "naming", f"{what} {value!r} must match {NAME_RE.pattern}")
    return value


def import_kuzu() -> Any:
    try:
        import kuzu
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise BackendError(KIND, "init", "the kuzu package is not installed; install the 'kuzu' extra") from exc
    return kuzu


@contextmanager
def mapped(operation: str) -> Iterator[None]:
    """Translate every Kuzu failure into a typed ``BackendError``; never retried."""
    try:
        yield
    except BackendError:
        raise
    except (RuntimeError, ValueError, TypeError) as exc:
        raise BackendError(KIND, operation, f"{type(exc).__name__}: {exc}") from exc


class KuzuSession:
    """One database + one connection, serialised by a lock. ``run`` executes a statement and returns
    rows as dicts; only statements in ``allowed`` may run (defence in depth)."""

    def __init__(self, path: Path, read_only: bool, timeout_seconds: float, buffer_pool_mb: int, allowed: frozenset[str] | None):
        kuzu = import_kuzu()
        self.path = Path(path)
        self.read_only = read_only
        self.allowed = allowed
        self.lock = threading.RLock()
        self.stats: Counter[str] = Counter()
        if read_only and not self.path.exists():
            raise BackendError(KIND, "init", f"no Kuzu database at {self.path} (attach mode never creates one)")
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with mapped("init"):
            self.db = kuzu.Database(str(self.path), read_only=read_only, buffer_pool_size=buffer_pool_mb * 1024 * 1024)
            self.conn = kuzu.Connection(self.db)
            self.conn.set_query_timeout(int(timeout_seconds * 1000))

    def check(self, statement: str) -> None:
        if self.allowed is not None and statement not in self.allowed:
            raise BackendError(KIND, "query", "refusing a statement that is not a fixed template")

    def run(self, operation: str, statement: str, **params: Any) -> list[dict[str, Any]]:
        self.check(statement)
        # Kuzu cannot bind the element type of an empty list parameter. Every top-level list parameter
        # of the templates is a list of non-empty strings used with ``IN`` (ids, keys, document ids,
        # predicates), so an empty one becomes ``[""]``, which matches nothing; ``rows`` is never empty.
        params = {k: ([""] if isinstance(v, list) and not v else v) for k, v in params.items()}
        with self.lock, mapped(operation):
            self.stats[f"{operation}.statements"] += 1
            result = self.conn.execute(statement, params)
            columns = result.get_column_names()
            rows = []
            while result.has_next():
                rows.append(dict(zip(columns, result.get_next())))
            return rows

    @contextmanager
    def transaction(self, operation: str) -> Iterator[Callable[..., list[dict[str, Any]]]]:
        """An explicit write transaction: committed when the block exits normally, rolled back otherwise."""
        if self.read_only:
            raise BackendError(KIND, operation, "the database is attached read-only; writes are refused")
        with self.lock:
            with mapped(operation):
                self.conn.execute("BEGIN TRANSACTION")
            try:

                def run(statement: str, **params: Any) -> list[dict[str, Any]]:
                    return self.run(operation, statement, **params)

                yield run
            except BaseException:
                try:
                    self.conn.execute("ROLLBACK")
                except RuntimeError:  # Kuzu already rolled a failed transaction back
                    pass
                raise
            with mapped(operation):
                self.conn.execute("COMMIT")

    def close(self) -> None:
        with self.lock, mapped("close"):
            self.conn.close()
            self.db.close()


class KuzuBackend:
    """One embedded database, one ``namespace``, one facet (``graph``). ``protect(gen)`` marks a
    generation as CURRENT/pinned in this process; the persisted pointer additionally protects the
    generation it names from ``delete_generation``."""

    kind = KIND

    def __init__(
        self,
        path: str | Path,
        namespace: str = "su_multi_arm",
        timeout_seconds: float = 30.0,
        buffer_pool_mb: int = 512,
        clock: Callable[[], float] = time.time,
    ):
        if timeout_seconds <= 0:
            raise BackendError(KIND, "init", "timeout_seconds must be positive")
        self.namespace = check_name(namespace, "namespace")
        self.clock = clock
        self._protected: set[str] = set()
        self.session = KuzuSession(Path(path), False, timeout_seconds, buffer_pool_mb, _TEMPLATES)
        for statement in q.SCHEMA:
            self.session.run("schema", statement)
        self.graph = KuzuGraph(self)

    @classmethod
    def from_config(cls, config: Any, clock: Callable[[], float] = time.time) -> KuzuBackend:
        return cls(config.path, config.namespace, config.timeout_seconds, config.buffer_pool_mb, clock)

    @property
    def stats(self) -> Counter[str]:
        return self.session.stats

    # --- keys ----------------------------------------------------------------------------------

    def gen_key(self, gen: GenerationId) -> str:
        return f"{self.namespace}|{check_name(gen.dataset, 'dataset')}|{gen.generation}"

    def pointer_key(self, dataset: str) -> str:
        return f"{self.namespace}|{check_name(dataset, 'dataset')}"

    # --- statements ----------------------------------------------------------------------------

    def run(self, operation: str, statement: str, **params: Any) -> list[dict[str, Any]]:
        return self.session.run(operation, statement, **params)

    def transaction(self, operation: str):
        return self.session.transaction(operation)

    def require_generation(self, gen: GenerationId, operation: str) -> None:
        if not self.run(operation, q.GENERATION_STATE, g=self.gen_key(gen)):
            raise BackendError(KIND, operation, f"unknown generation {gen.prefix()}")

    # --- generations and the pointer -----------------------------------------------------------

    def activate_generation(self, gen: GenerationId, expected: str | None = ANY) -> None:
        """Point ``<namespace>|<dataset>`` at ``gen``; refused (pointer unchanged) when the generation has
        no committed load. With ``expected`` it is a compare-and-set inside the (exclusive) write
        transaction: ``WriteConflict(stale_base)`` and a rollback when the pointer names anything else."""
        g, pointer = self.gen_key(gen), self.pointer_key(gen.dataset)
        with self.transaction("activate") as run:
            if not run(q.GENERATION_LOADED, g=g)[0]["loaded"]:
                raise BackendError(KIND, "activate", f"generation {gen.prefix()} has no committed load; pointer unchanged")
            if expected is not ANY:
                current = run(q.POINTER_READ, pointer=pointer)
                actual = current[0]["generation"] if current else None
                if actual != expected:
                    raise stale_base(KIND, actual, expected)
            run(q.POINTER_WRITE, pointer=pointer, generation=gen.generation, g=g, now=float(self.clock()))

    def deactivate_generation(self, gen: GenerationId) -> None:
        """Remove the pointer only when it still names ``gen`` (rollback of a failed release)."""
        with self.transaction("deactivate") as run:
            run(q.POINTER_DEACTIVATE, g=self.gen_key(gen), pointer=self.pointer_key(gen.dataset))

    def active_generation(self, dataset: str) -> str | None:
        rows = self.run("pointer", q.POINTER_READ, pointer=self.pointer_key(dataset))
        return rows[0]["generation"] if rows else None

    def generation_identity(self, gen: GenerationId) -> dict[str, Any]:
        return {"path": str(self.session.path), "namespace": self.namespace, "key": self.gen_key(gen), "pointer": self.pointer_key(gen.dataset)}

    def protect(self, gen: GenerationId) -> None:
        self._protected.add(gen.prefix())

    def unprotect(self, gen: GenerationId) -> None:
        self._protected.discard(gen.prefix())

    def delete_generation(self, gen: GenerationId) -> None:
        """Refused for a protected generation or the one the pointer names; one transaction."""
        if gen.prefix() in self._protected:
            raise BackendError(KIND, "delete_generation", "generation is active or pinned by a live session")
        key = self.gen_key(gen)
        with self.transaction("delete_generation") as run:
            pointer = run(q.POINTER_READ, pointer=self.pointer_key(gen.dataset))
            if pointer and pointer[0]["g"] == key:
                raise BackendError(KIND, "delete_generation", "generation is the active one (pointer)")
            run(q.GENERATION_DROP, g=key)
            for statement in q.DATA_DROP:
                run(statement, g=key)

    def generations(self) -> list[str]:
        rows = self.run("generations", q.GENERATIONS_LIST, namespace=self.namespace)
        return [f"{r['dataset']}/{r['generation']}/" for r in rows]

    # --- operations ----------------------------------------------------------------------------

    def probe(self) -> dict[str, Any]:
        """``doctor --probe``: the database opens and answers (embedded; no service)."""
        return self.ping()

    def ping(self) -> dict[str, Any]:
        kuzu = import_kuzu()
        self.run("ping", q.GENERATIONS_LIST, namespace=self.namespace)
        return {"engine": f"kuzu/{kuzu.__version__}", "path": str(self.session.path), "namespace": self.namespace, "mode": "native"}

    def drop_namespace(self) -> None:
        """Test helper: removes every row under this namespace (never another namespace's)."""
        with self.transaction("drop_namespace") as run:
            for statement in q.NAMESPACE_DROP:
                run(statement, prefix=self.namespace + "|")

    def close(self) -> None:
        self.session.close()


_TEMPLATES = frozenset(q.ALL_TEMPLATES)
