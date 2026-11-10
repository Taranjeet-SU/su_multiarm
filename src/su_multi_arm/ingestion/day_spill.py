"""Bounded-memory build of one day's summary documents (plan K §4.1, "bounded-memory close").

``DaySpill`` streams snapshots one at a time through ``SummaryBuilder`` into a temporary sqlite
file: per-scope documents are written as they are built, the episodes wait in a spill table until
every snapshot is read and are then merged one (tenant, env, service, rule) group at a time. What
stays in memory is one snapshot, one episode group and the small shared-rule counters, never the
day's documents. The documents equal ``summary_documents`` over the same snapshots (shared code),
so a streamed close writes the same content-addressed shard as before.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from ..core.types import Document
from .episode_summaries import SummaryBuilder

SPILL_SCHEMA = """
CREATE TABLE ep(seq INTEGER PRIMARY KEY, key TEXT NOT NULL, first TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX ep_key ON ep(key, first, seq);
CREATE TABLE docs(id TEXT PRIMARY KEY, kind TEXT NOT NULL, date TEXT, data TEXT NOT NULL);
CREATE INDEX docs_kind ON docs(kind, id);
"""


class SpilledEpisodes:
    """``SummaryBuilder`` episode buffer in the spill file (same grouping and order as in memory)."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def add(self, key: tuple, episode: dict) -> None:
        self.db.execute("INSERT INTO ep(key, first, data) VALUES (?,?,?)", (json.dumps(list(key)), episode["first_failure"], json.dumps(episode)))

    def groups(self) -> Iterator[tuple[tuple, list[dict]]]:
        current: tuple | None = None
        values: list[dict] = []
        for key, data in self.db.execute("SELECT key, data FROM ep ORDER BY key, first, seq"):
            parsed = tuple(json.loads(key))
            if current is not None and parsed != current:
                yield current, values
                values = []
            current = parsed
            values.append(json.loads(data))
        if current is not None:
            yield current, values


class DaySpill:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()
        self.db = sqlite3.connect(self.path)
        self.db.executescript(SPILL_SCHEMA)
        self.scopes = 0
        self.peak_snapshot_bytes = 0

    def close(self) -> None:
        self.db.close()
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)

    def __enter__(self) -> "DaySpill":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _put(self, doc: Document) -> None:
        try:
            self.db.execute("INSERT INTO docs VALUES (?,?,?,?)", (doc.id, doc.kind, doc.metadata.get("date"), doc.model_dump_json()))
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"duplicate summary document {doc.id}") from exc

    def build(self, dataset: str, rules: list[Any], snapshots: Iterable[dict], *, citations: bool, check: Callable[[dict], None] | None = None) -> None:
        """Stream ``snapshots`` (each checked by ``check`` first) into the spill file."""
        builder = SummaryBuilder(dataset, rules, citations=citations, episodes=SpilledEpisodes(self.db))
        for snap in snapshots:
            if check is not None:
                check(snap)
            for doc in builder.add(snap):
                self._put(doc)
            self.scopes += 1
        for doc in builder.finish():
            self._put(doc)
        self.db.commit()

    def kinds(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT DISTINCT kind FROM docs")}

    def dates(self, kinds: Iterable[str]) -> set[str]:
        kinds = list(kinds)
        return {str(r[0]) for r in self.db.execute(f"SELECT DISTINCT date FROM docs WHERE kind IN ({','.join('?' * len(kinds))})", kinds)}

    def count(self, kinds: Iterable[str] | None = None) -> int:
        if kinds is None:
            return int(self.db.execute("SELECT count(*) FROM docs").fetchone()[0])
        kinds = list(kinds)
        return int(self.db.execute(f"SELECT count(*) FROM docs WHERE kind IN ({','.join('?' * len(kinds))})", kinds).fetchone()[0])

    def documents(self, kinds: Iterable[str] | None = None) -> Iterator[Document]:
        """Documents in id order, parsed one at a time."""
        if kinds is None:
            cursor = self.db.execute("SELECT data FROM docs ORDER BY id")
        else:
            kinds = list(kinds)
            cursor = self.db.execute(f"SELECT data FROM docs WHERE kind IN ({','.join('?' * len(kinds))}) ORDER BY id", kinds)
        for (data,) in cursor:
            yield Document.model_validate_json(data)

    def content_id(self, day: str, kinds: Iterable[str], transform: Callable[[Document], Document]) -> str:
        """``history.content_shard_id`` of the (transformed) documents of ``kinds``, streamed."""
        digest = hashlib.sha256()
        for doc in self.documents(kinds):  # id order == sorted(documents, key=id)
            doc = transform(doc)
            digest.update(doc.id.encode())
            digest.update(hashlib.sha256(doc.model_dump_json().encode()).digest())
        return f"{day}:{digest.hexdigest()[:24]}"
