"""The persistent id directory of a unified collection (docs/plans/unified_merge.md §6.1, B9, B12).

One sqlite file maps every document id to the unit (member partition or provisional slice) that
holds it: ``(doc_id, kind, unit_id, unit_kind, day, version, superseded_by, deleted_by,
catalog_provisional)``. It implements the frozen ``core.types_members.IdDirectory`` protocol and is
the only router of ``get_document``, ``/source`` and exact-id reads in partitioned mode: an id is
resolved with **one indexed lookup** filtered to the request's pinned units, never by scanning
members and never by parsing the id (log ids are hashes without a day).

Maintenance is **incremental**: ``register`` inserts the rows of one new unit (cost O(documents of
the unit) with indexed probes, never O(collection)); ``supersede`` / ``tombstone`` mark rows;
``retire`` marks a unit as no longer composable; ``purge`` drops the rows of a retired unit (slice
GC, after its grace period).

Uniqueness (§6.1 invariants), checked at registration **inside one transaction** (a refusal leaves
the directory unchanged — fail closed, §6.12):

* a non-catalog id may be live in one live unit only; a duplicate is refused (``IdConflict``),
  except
  - a slice superseding an earlier version of the **same member and day** (recorded as
    ``superseded_by``), and
  - a unit listed in ``replaces`` (compaction, close: an archive day member replacing the day's
    slices, a catalog member replacing its previous version): the earlier rows are marked
    ``superseded_by``;
* ``catalog_provisional`` rows (a catalog-kind document first seen on a provisional day, carried by
  that day's slice) are exempt from the cross-day check and are superseded by the member's catalog
  unit (``<member>/catalog``) when it is registered.

Pinned resolution: a row superseded or tombstoned by a unit that is **not** in the pinned view is
still the live row for that view (a request pinned before a compaction keeps reading what it
pinned); a row tombstoned by a unit **in** the view is not found.

Unit ids are ``MemberRef.unit_id`` (``<member>/<key>``) and ``SliceRef.slice_id``. A unit id is
registered once; rebuilding the same key retires and purges the earlier unit first.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from ..core.errors import BackendError
from ..core.types_members import CATALOG_MEMBER_KEY, IdDirectoryEntry, PinnedView, UnitKind

ADAPTER = "id_directory"
SCHEMA = """
CREATE TABLE IF NOT EXISTS units(
  unit_id TEXT PRIMARY KEY,
  unit_kind TEXT NOT NULL,
  member TEXT NOT NULL,
  day TEXT,
  status TEXT NOT NULL DEFAULT 'live',
  documents INTEGER NOT NULL,
  seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ids(
  doc_id TEXT NOT NULL,
  unit_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  unit_kind TEXT NOT NULL,
  member TEXT NOT NULL,
  day TEXT,
  version INTEGER NOT NULL,
  superseded_by TEXT,
  deleted_by TEXT,
  catalog_provisional INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(doc_id, unit_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ids_unit ON ids(unit_id);
CREATE INDEX IF NOT EXISTS ids_member_day ON ids(member, day);
CREATE INDEX IF NOT EXISTS ids_superseded ON ids(superseded_by) WHERE superseded_by IS NOT NULL;
CREATE INDEX IF NOT EXISTS ids_deleted ON ids(deleted_by) WHERE deleted_by IS NOT NULL;
"""
_COLUMNS = "doc_id, kind, unit_id, unit_kind, day, version, superseded_by, deleted_by, catalog_provisional"
_BATCH = 500


class IdConflict(ValueError):
    """A registration would make a document id live in two units of one view (refused)."""

    def __init__(self, doc_id: str, unit_id: str, holder: str):
        self.doc_id, self.unit_id, self.holder = doc_id, unit_id, holder
        super().__init__(f"document id {doc_id!r} of unit {unit_id!r} is already live in unit {holder!r} (ids are unique across the collection)")


def member_of_unit(unit_id: str) -> str | None:
    """The member id of a member unit id (``<member>/<key>``); ``None`` for a slice id."""
    head, sep, _ = unit_id.partition("/")
    return head if sep else None


def _entry(row: sqlite3.Row | tuple) -> IdDirectoryEntry:
    doc_id, kind, unit_id, unit_kind, day, version, superseded_by, deleted_by, provisional = tuple(row)
    return IdDirectoryEntry(
        doc_id=doc_id,
        kind=kind,
        unit_id=unit_id,
        unit_kind=unit_kind,
        day=None if day is None else date.fromisoformat(day),
        version=int(version),
        superseded_by=superseded_by,
        deleted_by=deleted_by,
        catalog_provisional=bool(provisional),
    )


def _chunks(values: Sequence[Any], size: int = _BATCH) -> Iterator[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


class SqliteIdDirectory:
    """``IdDirectory`` over one sqlite file (``collection.id_directory.path``)."""

    def __init__(self, path: str | Path, timeout_seconds: float = 30.0):
        self.path = Path(path)
        self.timeout = timeout_seconds
        self._lock = threading.RLock()
        self.lookups = 0  # indexed resolve queries issued (tests assert O(1) per resolve)
        self._writes = 0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._db() as db:
                db.executescript(SCHEMA)
        except (OSError, sqlite3.Error) as exc:
            raise BackendError(ADAPTER, "open", f"id directory unavailable: {exc}") from exc

    @contextmanager
    def _db(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(str(self.path), timeout=self.timeout, isolation_level=None)
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                if write:
                    db.execute("ROLLBACK")
                raise
            if write:
                db.execute("COMMIT")
                self._writes += 1
        except sqlite3.Error as exc:
            raise BackendError(ADAPTER, "write" if write else "read", str(exc)) from exc
        finally:
            db.close()

    def stamp(self) -> tuple[int, int, int]:
        """Changes whenever the directory changes (this process's commits, or the file's
        modification time and size for another process's): readers key derived caches by it."""
        try:
            stat = self.path.stat()
        except OSError as exc:
            raise BackendError(ADAPTER, "stat", f"id directory unavailable: {exc}") from exc
        return (stat.st_mtime_ns, stat.st_size, self._writes)

    # --- registration ----------------------------------------------------------------------------

    def register(
        self,
        unit_id: str,
        unit_kind: UnitKind,
        entries: Sequence[IdDirectoryEntry],
        *,
        member: str | None = None,
        replaces: Sequence[str] = (),
    ) -> None:
        """Insert the rows of one new unit after the uniqueness checks above (one transaction).

        ``member`` is required for a slice (slice ids carry no member); for a member unit it is the
        ``<member>`` of the unit id. ``replaces`` names registered units whose live rows this unit
        supersedes (compaction, close, catalog rebuild)."""
        owner = member_of_unit(unit_id) if unit_kind == "member" else member
        if owner is None:
            raise ValueError(f"unit {unit_id!r}: a {unit_kind} unit needs its member" + (" (unit id <member>/<key>)" if unit_kind == "member" else ""))
        if unit_kind == "member" and member is not None and member != owner:
            raise ValueError(f"unit {unit_id!r} belongs to member {owner!r}, not {member!r}")
        ids = [e.doc_id for e in entries]
        if len(set(ids)) != len(ids):
            raise ValueError(f"unit {unit_id!r}: duplicate document ids in one unit")
        for entry in entries:
            if entry.unit_id != unit_id or entry.unit_kind != unit_kind:
                raise ValueError(f"entry {entry.doc_id!r} names unit {entry.unit_id!r}/{entry.unit_kind}, not {unit_id!r}/{unit_kind}")
            if not entry.live:
                raise ValueError(f"entry {entry.doc_id!r}: a new unit registers live rows only")
        days = {e.day for e in entries if e.day is not None}
        unit_day = next(iter(days)).isoformat() if len(days) == 1 and unit_kind == "slice" else None
        catalog_unit = unit_kind == "member" and unit_id == f"{owner}/{CATALOG_MEMBER_KEY}"
        with self._lock, self._db(write=True) as db:
            if db.execute("SELECT 1 FROM units WHERE unit_id=?", (unit_id,)).fetchone() is not None:
                raise ValueError(f"unit {unit_id!r} is already registered (retire and purge it before registering the key again)")
            for replaced in replaces:
                if db.execute("SELECT 1 FROM units WHERE unit_id=?", (replaced,)).fetchone() is None:
                    raise ValueError(f"unit {unit_id!r} replaces {replaced!r}, which is not registered")
            replaced_set = set(replaces)
            by_id = {e.doc_id: e for e in entries}
            supersede: list[tuple[str, str]] = []
            for batch in _chunks(ids):
                rows = db.execute(
                    "SELECT i.doc_id, i.unit_id, i.unit_kind, i.member, i.day, i.catalog_provisional FROM ids i JOIN units u ON u.unit_id=i.unit_id "
                    f"WHERE i.doc_id IN ({','.join('?' * len(batch))}) AND i.superseded_by IS NULL AND i.deleted_by IS NULL AND u.status='live'",
                    list(batch),
                ).fetchall()
                for doc_id, holder, holder_kind, holder_member, holder_day, holder_provisional in rows:
                    entry = by_id[doc_id]
                    day = None if entry.day is None else entry.day.isoformat()
                    if holder in replaced_set:
                        supersede.append((doc_id, holder))
                    elif unit_kind == "slice" and holder_kind == "slice" and holder_member == owner and holder_day == day:
                        supersede.append((doc_id, holder))  # same member and day: the later version wins
                    elif holder_provisional and catalog_unit and holder_member == owner:
                        supersede.append((doc_id, holder))  # the catalog member takes over at close
                    elif holder_provisional and entry.catalog_provisional:
                        continue  # a rule first seen on two provisional days: exempt (catalog kinds)
                    else:
                        raise IdConflict(doc_id, unit_id, holder)
            seq = int(db.execute("SELECT coalesce(max(seq), 0) + 1 FROM units").fetchone()[0])
            db.execute(
                "INSERT INTO units(unit_id, unit_kind, member, day, status, documents, seq) VALUES (?,?,?,?, 'live', ?, ?)",
                (unit_id, unit_kind, owner, unit_day, len(entries), seq),
            )
            db.executemany(
                f"INSERT INTO ids({_COLUMNS}, member) VALUES (?,?,?,?,?,?,NULL,NULL,?,?)",
                [
                    (e.doc_id, e.kind, unit_id, unit_kind, None if e.day is None else e.day.isoformat(), e.version, int(e.catalog_provisional), owner)
                    for e in entries
                ],
            )
            db.executemany("UPDATE ids SET superseded_by=? WHERE doc_id=? AND unit_id=?", [(unit_id, doc_id, holder) for doc_id, holder in supersede])

    def _require_unit(self, db: sqlite3.Connection, unit_id: str) -> str:
        row = db.execute("SELECT status FROM units WHERE unit_id=?", (unit_id,)).fetchone()
        if row is None:
            raise ValueError(f"unit {unit_id!r} is not registered")
        return row[0]

    def _mark(self, column: str, doc_ids: Sequence[str], by: str) -> None:
        with self._lock, self._db(write=True) as db:
            if self._require_unit(db, by) != "live":
                raise ValueError(f"unit {by!r} is retired")
            for doc_id in doc_ids:
                rows = db.execute("SELECT unit_id, superseded_by, deleted_by FROM ids WHERE doc_id=? AND unit_id<>?", (doc_id, by)).fetchall()
                live = [r[0] for r in rows if r[1] is None and r[2] is None]
                if not live:
                    if any(r[1 if column == "superseded_by" else 2] == by for r in rows):
                        continue  # already marked by this unit (idempotent)
                    raise ValueError(f"{column.split('_')[0]}: document id {doc_id!r} has no live row outside {by!r} (inconsistent with the unit manifest)")
                db.executemany(f"UPDATE ids SET {column}=? WHERE doc_id=? AND unit_id=?", [(by, doc_id, u) for u in live])

    def supersede(self, doc_ids: Sequence[str], by: str) -> None:
        """Mark the live rows of ``doc_ids`` outside unit ``by`` as superseded by it."""
        self._mark("superseded_by", doc_ids, by)

    def tombstone(self, doc_ids: Sequence[str], by: str) -> None:
        """Mark the live rows of ``doc_ids`` as deleted by unit ``by`` (B12: a document that vanished
        from a day rebuild); a tombstoned id is never served by a view that pins ``by``."""
        self._mark("deleted_by", doc_ids, by)

    def retire(self, unit_id: str) -> None:
        """The unit is no longer composable; its rows stay for views that still pin it."""
        with self._lock, self._db(write=True) as db:
            self._require_unit(db, unit_id)
            db.execute("UPDATE units SET status='retired' WHERE unit_id=?", (unit_id,))

    def purge(self, unit_id: str) -> int:
        """Drop the rows of a retired unit (GC after its grace period); returns the rows removed."""
        with self._lock, self._db(write=True) as db:
            if self._require_unit(db, unit_id) != "retired":
                raise ValueError(f"unit {unit_id!r} is live; retire it before purging")
            removed = db.execute("DELETE FROM ids WHERE unit_id=?", (unit_id,)).rowcount
            db.execute("DELETE FROM units WHERE unit_id=?", (unit_id,))
            return int(removed)

    # --- reads -------------------------------------------------------------------------------------

    @staticmethod
    def _pick(rows: Iterable[Any], units: set[str]) -> IdDirectoryEntry | None:
        candidates = []
        for row in rows:
            entry = _entry(row)
            if entry.unit_id not in units:
                continue
            if entry.deleted_by is not None and entry.deleted_by in units:
                return None  # tombstoned by a pinned unit: the document vanished in this view
            if entry.superseded_by is not None and entry.superseded_by in units:
                continue
            candidates.append(entry)
        if not candidates:
            return None
        # several live rows only for catalog_provisional carriers of one rule: the latest day wins
        return max(candidates, key=lambda e: (e.day or date.min, e.version, e.unit_id))

    def resolve(self, doc_id: str, view: PinnedView) -> IdDirectoryEntry | None:
        """One indexed lookup (``doc_id`` prefix of the primary key) filtered to the view's units."""
        units = set(view.unit_ids())
        with self._db() as db:
            self.lookups += 1
            rows = db.execute(f"SELECT {_COLUMNS} FROM ids WHERE doc_id=?", (doc_id,)).fetchall()
        return self._pick(rows, units)

    def resolve_many(self, doc_ids: Sequence[str], view: PinnedView) -> dict[str, IdDirectoryEntry]:
        """``resolve`` for a batch (one indexed ``IN`` lookup per 500 ids); unresolvable ids are absent."""
        units = set(view.unit_ids())
        wanted = list(dict.fromkeys(doc_ids))
        grouped: dict[str, list[Any]] = {}
        with self._db() as db:
            for batch in _chunks(wanted):
                self.lookups += 1
                for row in db.execute(f"SELECT {_COLUMNS} FROM ids WHERE doc_id IN ({','.join('?' * len(batch))})", list(batch)):
                    grouped.setdefault(row[0], []).append(row)
        found = {}
        for doc_id, rows in grouped.items():
            entry = self._pick(rows, units)
            if entry is not None:
                found[doc_id] = entry
        return found

    def shadowed(self, unit_id: str, view: PinnedView) -> frozenset[str]:
        """Ids of ``unit_id`` whose row is superseded or tombstoned by another unit of ``view``: they
        are excluded from that unit's candidates, records, counts and statistics (§6.5)."""
        others = sorted(set(view.unit_ids()) - {unit_id})
        if not others:
            return frozenset()
        out: set[str] = set()
        with self._db() as db:
            for batch in _chunks(others, 400):
                marks = ",".join("?" * len(batch))
                out.update(
                    r[0]
                    for r in db.execute(
                        f"SELECT doc_id FROM ids WHERE unit_id=? AND (superseded_by IN ({marks}) OR deleted_by IN ({marks}))",
                        [unit_id, *batch, *batch],
                    )
                )
        return frozenset(out)

    def live_ids(self, member: str, day: date) -> frozenset[str]:
        """Live ids of a member's day across its live slices (the tombstone diff at sealing)."""
        with self._db() as db:
            rows = db.execute(
                "SELECT i.doc_id FROM ids i JOIN units u ON u.unit_id=i.unit_id WHERE i.member=? AND i.day=? AND i.unit_kind='slice' "
                "AND u.status='live' AND i.superseded_by IS NULL AND i.deleted_by IS NULL",
                (member, day.isoformat()),
            ).fetchall()
        return frozenset(r[0] for r in rows)

    def unit(self, unit_id: str) -> dict[str, Any] | None:
        """``{unit_id, unit_kind, member, day, status, documents}`` of a registered unit."""
        with self._db() as db:
            row = db.execute("SELECT unit_id, unit_kind, member, day, status, documents FROM units WHERE unit_id=?", (unit_id,)).fetchone()
        if row is None:
            return None
        return dict(zip(("unit_id", "unit_kind", "member", "day", "status", "documents"), row))

    def units(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._db() as db:
            query = "SELECT unit_id, unit_kind, member, day, status, documents FROM units"
            rows = db.execute(query + (" WHERE status=? ORDER BY seq" if status else " ORDER BY seq"), (status,) if status else ()).fetchall()
        return [dict(zip(("unit_id", "unit_kind", "member", "day", "status", "documents"), r)) for r in rows]

    def count(self, unit_id: str) -> int:
        with self._db() as db:
            return int(db.execute("SELECT count(*) FROM ids WHERE unit_id=?", (unit_id,)).fetchone()[0])

    def entries(self, unit_id: str) -> list[IdDirectoryEntry]:
        with self._db() as db:
            return [_entry(r) for r in db.execute(f"SELECT {_COLUMNS} FROM ids WHERE unit_id=? ORDER BY doc_id", (unit_id,))]
