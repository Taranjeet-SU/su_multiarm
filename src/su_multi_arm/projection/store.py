"""Projection file stores (the ``relational`` capability facet) and read-only connections.

``FileProjectionFiles`` (sqlite backend) keeps a generation's files under
``<root>/snapshots/<generation>/projection/``, so ``delete_generation`` (and retention ``gc``,
which calls it) removes them with the generation and a protected (CURRENT/pinned) generation keeps
them. ``MemoryProjectionFiles`` (in-memory fake) keeps the bytes in the backend state. Files are
written only by the write path (``put_file``, atomic replace); nothing on the request path writes.

``open_readonly`` returns a connection for one file that cannot write or read outside the catalog:
the file is opened ``mode=ro`` (memory: deserialised), ``PRAGMA query_only=1`` is set, and an
authorizer allows only ``SELECT``, ``READ`` of the granted tables/columns (plus the internal
``_rejected`` column and ``json_each``'s value for bound id sets) and a fixed function list; a
progress handler aborts statements running past the deadline.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from ..core.errors import BackendError
from ..core.types import GenerationId

FILE_NAME = re.compile(r"^[a-z0-9_]{1,64}\.(?:sqlite|json)$")
FUNCTIONS = frozenset({"count", "sum", "avg", "min", "max", "total", "lower", "coalesce", "instr"})


def _check(name: str) -> str:
    if not FILE_NAME.match(name):
        raise BackendError("projection", "file", f"invalid projection file name {name!r}")
    return name


class FileProjectionFiles:
    """``relational`` facet of the sqlite backend."""

    def __init__(self, folder: Callable[[GenerationId], Path], timeout: float = 30.0, readonly: bool = False):
        self._folder = folder
        self.timeout = timeout
        self.readonly = readonly  # an adopted member store: files are read, never written or removed

    def _refuse(self, operation: str) -> None:
        if self.readonly:
            raise BackendError("projection", operation, "the projection files of an adopted member store are read-only")

    def folder(self, gen: GenerationId) -> Path:
        return self._folder(gen) / "projection"

    def put_file(self, gen: GenerationId, name: str, data: bytes) -> None:
        self._refuse("put_file")
        folder = self.folder(gen)
        folder.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=folder, prefix=".tmp_")
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(data)
            os.replace(temp, folder / _check(name))
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise

    def read_file(self, gen: GenerationId, name: str) -> bytes | None:
        path = self.folder(gen) / _check(name)
        return path.read_bytes() if path.exists() else None

    def file_names(self, gen: GenerationId) -> list[str]:
        folder = self.folder(gen)
        return sorted(p.name for p in folder.iterdir() if FILE_NAME.match(p.name)) if folder.exists() else []

    def connect_readonly(self, gen: GenerationId, name: str) -> sqlite3.Connection:
        path = self.folder(gen) / _check(name)
        if not path.exists():
            raise BackendError("projection", "open", f"projection file {name} of generation {gen.generation} is missing")
        return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=self.timeout, check_same_thread=False)

    def attachments(self, gen: GenerationId) -> list[dict[str, str]]:
        """Read-only attachments of the generation's projection databases: alias = file stem,
        absolute path, role ``projection`` (``attach_list``)."""
        folder = self.folder(gen)
        return [{"alias": name.rsplit(".", 1)[0], "path": str((folder / name).resolve()), "role": "projection"} for name in self.file_names(gen) if name.endswith(".sqlite")]

    def forget(self, gen: GenerationId) -> None:
        self._refuse("forget")
        folder = self.folder(gen)
        for path in folder.iterdir() if folder.exists() else []:
            path.unlink()
        if folder.exists():
            folder.rmdir()


class MemoryProjectionFiles:
    """``relational`` facet of the in-memory fake (bytes per generation in the backend state)."""

    def __init__(self, files: dict[str, dict[str, bytes]]):
        self._files = files

    def put_file(self, gen: GenerationId, name: str, data: bytes) -> None:
        self._files.setdefault(gen.prefix(), {})[_check(name)] = bytes(data)

    def read_file(self, gen: GenerationId, name: str) -> bytes | None:
        return self._files.get(gen.prefix(), {}).get(_check(name))

    def file_names(self, gen: GenerationId) -> list[str]:
        return sorted(self._files.get(gen.prefix(), {}))

    def connect_readonly(self, gen: GenerationId, name: str) -> sqlite3.Connection:
        data = self.read_file(gen, name)
        if data is None:
            raise BackendError("projection", "open", f"projection file {name} of generation {gen.generation} is missing")
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.deserialize(data)
        return db

    def forget(self, gen: GenerationId) -> None:
        self._files.pop(gen.prefix(), None)


def attach_list(facet: Any, gen: GenerationId) -> list[dict[str, str]]:
    """The projection files a pinned view reads, as read-only attachments for the multi-source SQL
    setup (``sql_surface/setup.py`` ``SqlAttachment``: ``alias``, absolute ``path``, ``role``; a
    partitioned collection adds the member ``unit``). Single store: its projection databases
    (alias = file stem); partitioned collection (M1b): every pinned member's files, alias
    ``<member>_<n>_<stem>``. An in-memory facet has no files to attach (``BackendError``)."""
    attachments = getattr(facet, "attachments", None)
    if not callable(attachments):
        raise BackendError("projection", "attach", "this projection facet keeps no files that can be attached")
    return list(attachments(gen))


def served_generation(facet: Any, gen: GenerationId) -> GenerationId:
    """The generation the facet's files were sealed for: ``gen`` itself, or (a partitioned
    collection, M1b) the store generation of the member that serves the projection."""
    mapped = getattr(facet, "store_generation", None)
    return mapped(gen) if callable(mapped) else gen


def open_readonly(facet: Any, gen: GenerationId, name: str, grant: dict[str, set[str]], timeout_ms: int, functions: frozenset[str] | None = None) -> sqlite3.Connection:
    """A read-only connection to one projection file. ``grant``: table -> readable columns.
    ``functions``: the SQL function allowlist (default ``FUNCTIONS``; the ``sql_surface`` mode passes
    its own, ``sql_surface.guard.SQL_FUNCTIONS``)."""
    # ``json_each`` (bound id sets of the IR lowering) only with the default list
    allowed = FUNCTIONS | {"json_each"} if functions is None else frozenset(f.lower() for f in functions)
    db = facet.connect_readonly(gen, name)
    try:
        db.execute("PRAGMA query_only=1")
        tables = {table: {*columns, "_rejected"} for table, columns in grant.items()}

        def authorize(action: int, arg1: str | None, arg2: str | None, _db: str | None, _trigger: str | None) -> int:
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ:
                if arg1 == "json_each" and arg2 == "value" and "json_each" in allowed:
                    return sqlite3.SQLITE_OK
                # ``COUNT(*)`` reads a granted table with an empty column name
                return sqlite3.SQLITE_OK if arg1 in tables and (arg2 in tables[arg1] or arg2 == "") else sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_FUNCTION:
                return sqlite3.SQLITE_OK if (arg2 or "").lower() in allowed else sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_DENY

        db.set_authorizer(authorize)
        deadline = time.monotonic() + timeout_ms / 1000.0
        db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
        return db
    except BaseException:
        db.close()
        raise
