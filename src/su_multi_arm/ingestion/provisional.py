"""Provisional store: every unclosed day served, flagged (plan K §4.1; ``logs.collection.provisional``).

**Per-scope versions.** ``put_scope(D, snapshot)`` stores a collector snapshot of one source scope
(index) of day D under the next version of that scope, content-addressed (an unchanged snapshot is
not a new version). A refresh carrying some scopes never drops the others: a day's state is the
latest version of every scope seen for it.

**Projection file.** ``build(D)`` materializes the day's typed rows (``episode_history`` tables,
``provisional = 1``, ``as_of`` = latest capture) from the latest scope versions into a new file
``projection/<D>.<digest>.sqlite`` (written to a temporary name, then renamed) and replaces the
day's entry in ``CURRENT.json`` atomically. The digest covers the scope versions and the boundary
tail D-1 was compared with, so an unchanged day is not rebuilt. Readers read ``CURRENT.json`` once
per request and open the named files read-only; a replaced file stays until ``gc`` (age-bounded), a
missing file makes that day ``source_unavailable`` only.

**Tier rule.** A day is provisional for a reader exactly when the reader's pinned generation does
not serve it from the archive (``episode_history.tier_map``). ``drop(days)`` removes days that the
active generation serves from the archive; until then a closed-but-unpublished day stays here, so
there is never a day without a tier and never a day in two.
"""

from __future__ import annotations

import fcntl
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from ..core.errors import ConfigError
from .episode_summaries import CONTRACT, compile_summary, identity, timestamp

SCOPES_SCHEMA = """
CREATE TABLE IF NOT EXISTS scope_versions(day TEXT NOT NULL, scope TEXT NOT NULL, version INTEGER NOT NULL, sha TEXT NOT NULL, window_start TEXT NOT NULL, window_end TEXT NOT NULL, observed_at TEXT, stored_at TEXT NOT NULL, PRIMARY KEY(day, scope, version));
CREATE TABLE IF NOT EXISTS expected_scopes(day TEXT PRIMARY KEY, scopes TEXT NOT NULL, stored_at TEXT NOT NULL);
"""
GC_GRACE_SECONDS = 3600


class ProvisionalRefused(ValueError):
    """A snapshot or build is refused (wrong day window, recipe mismatch, unknown scope)."""


def snapshot_window(day: str, snapshot: dict[str, Any]) -> tuple[datetime, datetime]:
    """The snapshot's summary window; it must start at D 00:00Z and end within D (a rolling
    snapshot of an unclosed day ends at its capture time)."""
    summary = snapshot["summary"]
    if summary.get("contract") != CONTRACT:
        raise ProvisionalRefused("snapshot is not an incident_episode_summaries_v1 summary")
    start, end = timestamp(summary["start"]), timestamp(summary["end"])
    midnight = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    if start != midnight or not start < end <= midnight + timedelta(days=1):
        raise ProvisionalRefused(f"snapshot window {summary['start']}..{summary['end']} does not start at {day} 00:00Z and end within that day")
    return start, end


def check_recipe(snapshot: dict[str, Any], recipe: Any, field_map: Any = None) -> None:
    """The snapshot's summary recipe equals the pinned keyword recipe compiled over its window."""
    summary = snapshot["summary"]
    expected = compile_summary(recipe.phrases, recipe.rules, summary["start"], summary["end"], field_map=field_map)["recipe"]
    if summary.get("recipe") != expected:
        raise ProvisionalRefused(f"snapshot of {snapshot['scope'].get('index')} was captured with another keyword recipe than the pinned policy; recapture it")


class ProvisionalStore:
    def __init__(self, root: Path, now: Callable[[], datetime] | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "projection").mkdir(exist_ok=True)
        (self.root / "snapshots").mkdir(exist_ok=True)
        self.now = now or (lambda: datetime.now(timezone.utc))
        with self._db() as db:
            db.executescript(SCOPES_SCHEMA)

    @property
    def current_path(self) -> Path:
        return self.root / "CURRENT.json"

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.root / "scopes.sqlite", timeout=60)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    @contextmanager
    def lock(self) -> Iterator[None]:
        """One writer at a time (refresh and the heavy job's drop)."""
        with open(self.root / ".lock", "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    # --- scope versions -------------------------------------------------------------------------

    def put_scope(self, day: str, snapshot: dict[str, Any]) -> int:
        date.fromisoformat(day)
        start, end = snapshot_window(day, snapshot)
        scope = str(snapshot["scope"]["index"])
        sha = identity(snapshot)
        with self._db() as db:
            last = db.execute("SELECT version, sha FROM scope_versions WHERE day=? AND scope=? ORDER BY version DESC LIMIT 1", (day, scope)).fetchone()
            if last is not None and last["sha"] == sha:
                return int(last["version"])
            folder = self.root / "snapshots" / day
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{sha}.json"
            if not path.exists():
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(snapshot, sort_keys=True))
                tmp.replace(path)
            version = (int(last["version"]) + 1) if last is not None else 1
            db.execute("INSERT INTO scope_versions VALUES (?,?,?,?,?,?,?,?)", (day, scope, version, sha, start.isoformat(), end.isoformat(), snapshot.get("observed_at"), self.now().isoformat()))
        return version

    def set_expected(self, day: str, scopes: list[str]) -> None:
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO expected_scopes VALUES (?,?,?)", (day, json.dumps(sorted(set(scopes))), self.now().isoformat()))

    def expected(self, day: str) -> list[str] | None:
        with self._db() as db:
            row = db.execute("SELECT scopes FROM expected_scopes WHERE day=?", (day,)).fetchone()
        return json.loads(row["scopes"]) if row else None

    def days(self) -> list[str]:
        with self._db() as db:
            return [r["day"] for r in db.execute("SELECT DISTINCT day FROM scope_versions ORDER BY day")]

    def latest(self, day: str) -> list[dict[str, Any]]:
        """The latest version of every scope of ``day`` (scope order)."""
        with self._db() as db:
            rows = db.execute(
                "SELECT s.* FROM scope_versions s JOIN (SELECT scope, max(version) AS v FROM scope_versions WHERE day=? GROUP BY scope) m ON s.scope=m.scope AND s.version=m.v WHERE s.day=? ORDER BY s.scope",
                (day, day),
            ).fetchall()
        return [dict(r) for r in rows]

    def snapshot(self, day: str, sha: str) -> dict[str, Any]:
        return json.loads((self.root / "snapshots" / day / f"{sha}.json").read_text())

    def snapshots(self, day: str) -> Iterator[dict[str, Any]]:
        for row in self.latest(day):
            yield self.snapshot(day, row["sha"])

    # --- ledger ----------------------------------------------------------------------------------

    def current(self) -> dict[str, dict[str, Any]]:
        """The pinned provisional ledger: ``{day: {file, digest, as_of, scopes, versions, missing_scopes}}``."""
        if not self.current_path.exists():
            return {}
        return json.loads(self.current_path.read_text())

    def _write_current(self, current: dict[str, dict[str, Any]]) -> None:
        tmp = self.current_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(current, sort_keys=True, indent=1))
        tmp.replace(self.current_path)

    def build(self, day: str, *, dataset: str, rules: list[Any], gap_seconds: int, tail: Callable[[str], tuple[str, list[dict[str, Any]] | None]]) -> dict[str, Any]:
        """Materialize ``day``'s projection file from its latest scope versions. ``tail(D-1)`` gives
        ``(boundary identity, boundary pieces or None)`` of the previous day from whichever tier
        holds it. Returns the day's ledger entry (unchanged input: the existing entry)."""
        from .day_spill import DaySpill
        from .episode_history import coverage_of, ensure_schema, insert_rows, issue_rows, link_continuations, piece_of

        latest = self.latest(day)
        if not latest:
            raise ProvisionalRefused(f"day {day} has no provisional scope versions")
        boundary, tail_pieces = tail((date.fromisoformat(day) - timedelta(days=1)).isoformat())
        versions = {r["scope"]: r["version"] for r in latest}
        expected = self.expected(day)
        digest = identity({"versions": versions, "boundary": boundary, "expected": expected, "gap_seconds": gap_seconds})[:24]
        current = self.current()
        entry = current.get(day)
        if entry is not None and entry.get("digest") == digest and (self.root / "projection" / Path(entry["file"]).name).exists():
            return entry
        as_of = max((r["observed_at"] or r["window_end"]) for r in latest)
        name = f"{day}.{digest}.sqlite"
        target = self.root / "projection" / name
        tmp = target.with_suffix(".building")
        tmp.unlink(missing_ok=True)
        with DaySpill(self.root / "projection" / f"{day}.{digest}.spill") as spill:
            spill.build(dataset, rules, self.snapshots(day), citations=True)
            pieces = [piece_of(day, name, d.metadata, d.id) for d in spill.documents(["incident"])]
            coverage = [coverage_of(day, name, d.metadata, d.id) for d in spill.documents(["summary_coverage"])]
        link_continuations(day, pieces, tail_pieces, gap_seconds)
        for row in (*pieces, *coverage):
            row.update(provisional=1, as_of=as_of)
        db = sqlite3.connect(tmp)
        try:
            ensure_schema(db)
            insert_rows(db, pieces, issue_rows(day, name, pieces), coverage)
            db.commit()
        finally:
            db.close()
        tmp.replace(target)
        stale = [r["scope"] for r in latest if timestamp(r["window_end"]) < datetime.fromisoformat(day).replace(tzinfo=timezone.utc) + timedelta(days=1)]
        entry = {
            "file": str(target),
            "digest": digest,
            "as_of": as_of,
            "scopes": len(latest),
            "versions": versions,
            "scopes_expected": len(expected) if expected is not None else None,
            "missing_scopes": sorted(set(expected or []) - set(versions)),
            "rolling_scopes": len(stale),
            "built_at": self.now().isoformat(),
        }
        current = self.current()
        current[day] = entry
        self._write_current(current)
        return entry

    def drop(self, days: list[str]) -> list[str]:
        """Remove days the active generation serves from the archive (versions, snapshots, ledger)."""
        current = self.current()
        dropped = []
        with self._db() as db:
            for day in days:
                known = day in current or db.execute("SELECT 1 FROM scope_versions WHERE day=? LIMIT 1", (day,)).fetchone() is not None
                if not known:
                    continue
                current.pop(day, None)
                db.execute("DELETE FROM scope_versions WHERE day=?", (day,))
                db.execute("DELETE FROM expected_scopes WHERE day=?", (day,))
                folder = self.root / "snapshots" / day
                if folder.exists():
                    for path in folder.iterdir():
                        path.unlink()
                    folder.rmdir()
                dropped.append(day)
        self._write_current(current)
        return dropped

    def gc(self, grace_seconds: int = GC_GRACE_SECONDS) -> list[str]:
        """Delete projection files no ledger entry names, once older than ``grace_seconds`` (a
        reader that read the previous ``CURRENT.json`` keeps its file for that long)."""
        named = {Path(e["file"]).name for e in self.current().values()}
        removed = []
        cutoff = time.time() - grace_seconds
        for path in (self.root / "projection").iterdir():
            if path.name in named or path.stat().st_mtime > cutoff:
                continue
            path.unlink()
            removed.append(path.name)
        return sorted(removed)


def open_provisional(config: Any, now: Callable[[], datetime] | None = None) -> ProvisionalStore:
    from ..core.config_log_policy import provisional_path

    logs = config.logs
    if logs is None or not logs.collection.provisional.enabled:
        raise ConfigError("the provisional store is not enabled (logs.collection.provisional.enabled)", "logs.collection.provisional")
    return ProvisionalStore(provisional_path(logs.collection, logs), now=now)


def provisional_enabled(config: Any) -> bool:
    logs = getattr(config, "logs", None)
    return bool(logs is not None and logs.collection.provisional.enabled)


def staged_snapshots(folder: Path) -> Iterator[Path]:
    """The snapshot files of one staged day (``_*.json`` are control files, e.g. ``_expected.json``)."""
    for path in sorted(folder.glob("*.json")):
        if not path.name.startswith("_"):
            yield path


def read_expected(folder: Path) -> list[str] | None:
    """``_expected.json``: the collector's inventory of the day's source scopes (index names)."""
    path = folder / "_expected.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    scopes = data.get("scopes") if isinstance(data, dict) else data
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        raise ProvisionalRefused(f"{path.name} must list the expected scope index names ({{\"scopes\": [...]}})")
    return sorted(set(scopes))

