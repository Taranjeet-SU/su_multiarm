"""Generic SQL adapter (config kind ``sqlalchemy``) on the frozen contracts, through SQLAlchemy Core
(plan §3.5 ``sqlalchemy`` row, Phase 1): catalog, typed records with pushed-down predicates, exact
cache, ledger with raw replies, sessions. Lexical and vector search are dialect-specific and stay
``planned``. Tested dialects: SQLite and PostgreSQL."""

from ._db import TESTED_DIALECTS
from .adapter import SqlBackend

__all__ = ["SqlBackend", "TESTED_DIALECTS"]
