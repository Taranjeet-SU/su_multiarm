"""Reply cache of the LLM log fallback (addendum A2 rev 2 §4.2.3 step 3; review N4).

A SQLite table of validated model replies, so a recurring template, a rebuilt overlay or a resumed
run costs no model call.

- **Cluster replies** are keyed by ``(template sha, service, level, provider, model, prompt
  version, reply schema version)``. A hit reuses only the reply's **labels and regex**: its line
  ids and cluster id belong to the run that paid for it. The caller re-anchors them on today's
  lines and re-runs the full gate on today's members (``slots.py``).
- **Line replies** are keyed by ``(sha of the redacted line text, service, level, requested field
  set, provider, model, prompt version, reply schema version)``; quotes are located again in the
  line, so a hit is grounded exactly like a fresh reply.

Every row is tagged with the partition keys whose lines it was used for. ``purge(before)`` deletes
the tags of expired partitions and every row left without a tag, so cached quotes (substrings of
redacted lines) never outlive their partitions' retention.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .replies import PROMPT_VERSION, SCHEMA_VERSION

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS replies (
        key TEXT PRIMARY KEY, kind TEXT NOT NULL, model TEXT NOT NULL,
        reply TEXT NOT NULL, reply_sha TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS reply_partitions (
        key TEXT NOT NULL, partition_key TEXT NOT NULL, PRIMARY KEY (key, partition_key))""",
)


def sha(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def template_sha(tokens: list[str]) -> str:
    return sha(["template", tokens])


def cluster_key(template: str, service: str | None, level: str | None, provider: str, model: str, task: str = "cluster", extra: Any = None) -> str:
    return sha([task, template, service, level, provider, model, PROMPT_VERSION, SCHEMA_VERSION, extra])


def line_key(text: str, service: str | None, level: str | None, fields: list[str], provider: str, model: str) -> str:
    return sha(["line", hashlib.sha256(text.encode()).hexdigest(), service, level, sorted(fields), provider, model, PROMPT_VERSION, SCHEMA_VERSION])


class ReplyCache:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            for statement in _SCHEMA:
                db.execute(statement)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.execute("PRAGMA busy_timeout = 30000")
        return db

    def get(self, key: str) -> tuple[str, str] | None:
        """``(reply json, reply sha)`` or ``None``."""
        with closing(self._connect()) as db:
            row = db.execute("SELECT reply, reply_sha FROM replies WHERE key = ?", (key,)).fetchone()
        return (row[0], row[1]) if row else None

    def put(self, key: str, kind: str, model: str, reply: str, created_at: str) -> str:
        digest = hashlib.sha256(reply.encode()).hexdigest()
        with closing(self._connect()) as db:
            db.execute("INSERT OR REPLACE INTO replies (key, kind, model, reply, reply_sha, created_at) VALUES (?, ?, ?, ?, ?, ?)", (key, kind, model, reply, digest, created_at))
        return digest

    def tag(self, key: str, partition_key: str) -> None:
        with closing(self._connect()) as db:
            db.execute("INSERT OR IGNORE INTO reply_partitions (key, partition_key) VALUES (?, ?)", (key, partition_key))

    def purge(self, before: str) -> int:
        """Drop the tags of partitions older than ``before`` (YYYY-MM-DD) and every reply left
        without a tag; returns the number of replies deleted."""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM reply_partitions WHERE partition_key < ?", (before,))
            deleted = db.execute("DELETE FROM replies WHERE key NOT IN (SELECT key FROM reply_partitions)").rowcount
            db.execute("COMMIT")
        return deleted

    def count(self) -> int:
        with closing(self._connect()) as db:
            return db.execute("SELECT COUNT(*) FROM replies").fetchone()[0]
