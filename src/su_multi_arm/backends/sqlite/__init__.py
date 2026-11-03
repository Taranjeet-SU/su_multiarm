"""SQLite adapter on the frozen contracts (plan §3.5 ``sqlite`` row): catalog, FTS5 lexical, exact
NumPy/Faiss vectors, typed records, passage-backed graph with ``TraversalSpec``, cache, ledger with
raw replies, sessions."""

from .adapter import SqliteBackend

__all__ = ["SqliteBackend"]
