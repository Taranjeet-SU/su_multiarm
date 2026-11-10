"""The v2 ``logs`` block served over the live log member of a partitioned collection (staging fix
B1; docs/plans/unified_merge.md §6.7, §6.11).

On a single-member deployment the log tools read a log-profile generation whose manifest carries a
``logs`` section (served days, the episode archive pointer, the provisional store) and, for
``issue_history``/``log_liveness``, the typed history tables of the episode archive file and of the
provisional day files. A partitioned collection has none of those: the closed days of the log host
are the live log member's **archive day/month members** and its unclosed days are the **live
slices** pinned per request (``ingestion/log_collection.py``). This module gives the log tools the
same two inputs over that member:

* ``log_section(engine, gen)`` — the manifest ``logs`` section of a request pin, derived from the
  pinned view only: ``collection_scope`` = the episode-summary contract (the only shape a log host
  publishes),
  ``days`` = the days of the member's pinned archive members plus the days of its pinned slices
  (a day is in exactly one tier, ``PinnedView``), ``history.store = collection_member`` with the
  per-day tier ``ledger`` (``archive`` / ``partial`` with the unit id, ``provisional`` for a slice
  day) and ``as_of`` = the latest served day. Every other tool reads records through the
  partitioned facets, which already merge the member's units under the pin.
* ``MemberHistory`` — the typed history tables (``episode_piece``, ``issue_day``, ``coverage_day``
  of ``ingestion/episode_history``) of the member under the request pin, built exactly as the
  archive builds them at close (``piece_of``, ``coverage_of``, ``link_continuations``,
  ``issue_rows``) and read by ``TieredAggregate`` unchanged (tenant/service/rule/category filters
  and the day range are pushed into its SQL). Archive days carry ``partial`` when their member is a
  ``partial`` tier; slice days are ``provisional`` with ``as_of`` = their latest capture. Days
  without a pinned unit are ``missing`` (never zero).

**Per-pin history file (staging fix N3, 2026-11-10).** The tables are no longer built per request
from the window's catalog records (≈ 47 s for 3 real days, 10 days refused): ``history_file``
derives them **once per request pin** from the pin's SQL-surface file of the member
(``sql_surface/members.view_file``: the served ``incident``/``summary_coverage`` rows of every
pinned unit, itself built from per-unit caches) into a file next to it, covering **every served
day**, so any window up to ``serving.max_window_days`` is one indexed aggregate. A day's rows are a
function of its served rows (``(doc_id, unit)`` of the day, its tier and closure) and of the
previous day's rows (the cross-midnight rule chains ``chain_first`` across days): each day gets a
chained key over both, and a new pin **copies** the rows of every day whose key an earlier pin's
history file already holds, so a new slice recomputes only its own day (and the days after it).
The file is immutable for its pin; the oldest per-pin files are pruned like the view files.

Liveness reads which days recorded success heartbeats from the data itself: a day whose
``summary_coverage`` records carry ``success_documents`` was captured with success rules
(``MemberHistory.recorded_days``); per-day keyword-recipe identities are not recorded by log units,
so the archive's recipe-identity comparison is replaced by this (stated in the liveness notes by the
caller as "days before heartbeat recording").
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..ingestion.episode_history import (
    PIECE_COLUMNS,
    DayTier,
    HistoryUnavailable,
    TieredAggregate,
    coverage_of,
    ensure_schema,
    insert_rows,
    issue_rows,
    link_continuations,
    midnight,
    piece_of,
)
from ..ingestion.episode_summaries import CONTRACT, timestamp

MEMBER_STORE = "collection_member"
HISTORY_FORMAT = 1  # the derivation of the per-pin history file (part of every day key)
_BUILD_LOCK = threading.Lock()  # one history build per process at a time (the same pin is built once)


def log_member(config: Any) -> str | None:
    """The live log member serving ``logs.source`` (``None`` without one)."""
    from ..ingestion.log_collection import live_members

    logs = getattr(config, "logs", None)
    if logs is None or getattr(config, "collection", None) is None:
        return None
    found = live_members(config)
    return found[0] if len(found) == 1 else None


def _month_days(month: str) -> list[date]:
    first = date.fromisoformat(f"{month}-01")
    out, day = [], first
    while day.month == first.month:
        out.append(day)
        day += timedelta(days=1)
    return out


def served_days(view: Any, member: str) -> dict[str, dict[str, Any]]:
    """``{day: {"tier", "unit"}}`` of the member in a pinned view: archive/partial members (a
    ``month:`` member every day of its month) and the days of its pinned slices."""
    out: dict[str, dict[str, Any]] = {}
    for ref in view.members:
        if ref.member != member or ref.tier not in {"archive", "partial"}:
            continue
        head, _, rest = ref.key.partition(":")
        days = [date.fromisoformat(rest)] if head == "day" else (_month_days(rest) if head == "month" else [])
        for day in days:
            out[day.isoformat()] = {"tier": "archive", "closure": "partial" if ref.tier == "partial" else "complete", "unit": ref.unit_id}
    for day, slices in (view.slices.get(member) or {}).items():
        out[day.isoformat()] = {"tier": "provisional", "slices": list(slices)}
    return dict(sorted(out.items()))


def log_section(engine: Any, gen: Any) -> dict[str, Any]:
    """The ``logs`` manifest section of a request pin (module doc)."""
    config = engine.config
    member = log_member(config)
    if member is None:
        return {}
    backend = engine.backends[config.storage.catalog]
    view = backend.view(gen)
    days = served_days(view, member)
    served = sorted(days)
    return {
        "source": config.logs.source,
        "collection_scope": CONTRACT,
        "member": member,
        "as_of": served[-1] if served else None,
        "partitions": [],
        "days": served,
        "date_basis": "event_time",
        "date_timezone": "UTC",
        "history": {"store": MEMBER_STORE, "member": member},
        "archive": {"gaps": {}, "recipes": {}},
        "ledger": days,
    }


def log_manifest(engine: Any, gen: Any) -> dict[str, Any]:
    """What ``Engine.log_manifest`` returns on a partitioned collection: ``{"logs": section}``."""
    section = log_section(engine, gen)
    return {"logs": section} if section else {}


class _Borrowed:
    """The request's one read-only connection handed to ``TieredAggregate`` (which opens and closes
    a connection per tier): every tier reads the same per-pin file, opened once, so a file pruned
    after the request opened it stays readable for the request."""

    def __init__(self, db: sqlite3.Connection):
        self._db = db

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        return self._db.execute(sql, params)

    def close(self) -> None:
        return None

    def __enter__(self) -> _Borrowed:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class MemberAggregate(TieredAggregate):
    """``TieredAggregate`` over the request pin's history file (module doc): archive days are
    restricted to their unit ids by the ``source`` column (as the archive restricts them to its
    shards), provisional days read the same file; the tables hold every served day."""

    def __init__(self, path: Path, db: sqlite3.Connection, tiers: dict[str, DayTier]):
        super().__init__(path, tiers)
        self._db = db

    def _connect(self, path: Path) -> Any:  # type: ignore[override]
        return _Borrowed(self._db)

    def check_built(self) -> None:
        return None


def _is_history(path: Path) -> bool:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='history_days'").fetchone() is not None
    except sqlite3.Error:
        return False


def _fingerprints(db: sqlite3.Connection, days: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Per served day: sha256 over its tier entry and the ``(doc_id, unit)`` of its served
    ``incident`` and ``summary_coverage`` rows in the pin's view file (attached as ``src``). Units
    are immutable, so equal fingerprints mean equal input rows."""
    digests = {d: hashlib.sha256(json.dumps([d, e.get("tier"), e.get("closure"), e.get("unit")]).encode()) for d, e in days.items()}
    for table in ("issue_day", "coverage_day"):
        cursor = db.execute(f'SELECT day, doc_id, unit FROM src."{table}" ORDER BY day, doc_id, unit')
        for day, doc_id, unit in cursor:
            digest = digests.get(day)
            if digest is not None:
                digest.update(f"{table}\x1f{doc_id}\x1f{unit}\n".encode())
    return {d: h.hexdigest() for d, h in digests.items()}


def day_keys(fingerprints: dict[str, str], gap_seconds: int) -> dict[str, str]:
    """Chained key per served day: its fingerprint, the gap rule and the previous served day's key
    when that day is adjacent (its rows decide this day's continuations and ``chain_first``)."""
    out: dict[str, str] = {}
    previous: str | None = None
    for day in sorted(fingerprints):
        adjacent = previous is not None and date.fromisoformat(day) - date.fromisoformat(previous) == timedelta(days=1)
        body = [HISTORY_FORMAT, gap_seconds, day, fingerprints[day], out[previous] if adjacent and previous is not None else None]
        out[day] = hashlib.sha256(json.dumps(body).encode()).hexdigest()
        previous = day
    return out


_PIECE_META = {"first_failure": "first_hour", "last_failure": "last_hour"}
_ISSUE_COLUMNS = ("doc_id", "tenant", "env", "service", "signature_id", "category", "issue", "first_failure", "last_failure", "occurrences", "evidence_count")
_COVERAGE_COLUMNS = ("doc_id", "tenant", "env", "service", "window_start", "window_end", "observed_at", "success_recorded")


def _compute_day(db: sqlite3.Connection, day: str, entry: dict[str, Any], tail: list[dict[str, Any]] | None, gap_seconds: int) -> dict[str, Any]:
    """Build one day's rows into ``main`` from the view file's served rows of the day (the archive
    builder's functions, unchanged); returns its ``history_days`` entry."""
    provisional = entry["tier"] == "provisional"
    source = "provisional" if provisional else entry["unit"]
    pieces = []
    for row in db.execute(f"SELECT {', '.join(_ISSUE_COLUMNS)} FROM src.issue_day WHERE day=? ORDER BY doc_id", (day,)):
        values = dict(zip(_ISSUE_COLUMNS, row))
        if values["first_failure"] is None or values["last_failure"] is None:
            raise HistoryUnavailable(f"incident record {values['doc_id']} of {day} has no first/last failure time; the history tables cannot be built")
        metadata = {_PIECE_META.get(k, k): v for k, v in values.items() if k != "doc_id"}
        pieces.append(piece_of(day, source, metadata, values["doc_id"]))
    coverage, recorded = [], False
    for row in db.execute(f"SELECT {', '.join(_COVERAGE_COLUMNS)} FROM src.coverage_day WHERE day=? ORDER BY doc_id", (day,)):
        values = dict(zip(_COVERAGE_COLUMNS, row))
        recorded = recorded or bool(values.pop("success_recorded"))
        coverage.append(coverage_of(day, source, values, values["doc_id"]))
    as_of = max((str(c["observed_at"]) for c in coverage if c.get("observed_at")), default=None) if provisional else None
    for row in [*pieces, *coverage]:
        row["partial"] = int(entry.get("closure") == "partial")
        row["provisional"] = int(provisional)
        row["as_of"] = as_of
    link_continuations(day, pieces, tail, gap_seconds)
    insert_rows(db, pieces, issue_rows(day, source, pieces), coverage)
    return {"as_of": as_of, "recorded": int(recorded)}


def _stored_tail(db: sqlite3.Connection, day: str, gap_seconds: int) -> list[dict[str, Any]]:
    """The boundary tail of an already built ``day`` (``_tail`` over its stored pieces)."""
    pieces = [dict(zip(PIECE_COLUMNS, r)) for r in db.execute(f"SELECT {', '.join(PIECE_COLUMNS)} FROM main.episode_piece WHERE day=?", (day,))]
    return _tail(pieces, day, gap_seconds) or []


def history_file(engine: Any, gen: Any, member: str, days: dict[str, dict[str, Any]], gap_seconds: int) -> tuple[Path, dict[str, dict[str, Any]]]:
    """The history tables of ``member`` under the request pin ``gen`` (module doc): the file and
    ``{day: {"key", "tier", "closure", "unit", "as_of", "recorded"}}`` of its served days."""
    from ..sql_surface import members as surface

    backend = engine.backends[engine.config.storage.catalog]
    root = Path(backend.registry.root)
    folder = root / "sql"
    path = folder / surface._cache_name(f"history:{HISTORY_FORMAT}:{gen.generation}:{member}:{gap_seconds}")
    stats = backend.stats
    with _BUILD_LOCK:
        if not path.is_file():
            view_path, _counts = surface.view_file(root, backend, gen, member)

            def build(db: sqlite3.Connection) -> None:
                ensure_schema(db)
                db.execute("CREATE TABLE history_days(day TEXT PRIMARY KEY, key TEXT NOT NULL, tier TEXT NOT NULL, closure TEXT, unit TEXT, as_of TEXT, recorded INTEGER NOT NULL)")
                db.commit()
                db.execute("ATTACH DATABASE ? AS src", (str(view_path),))
                keys = day_keys(_fingerprints(db, days), gap_seconds)
                reused = _copy_reusable(db, folder, path, keys)
                previous: str | None = None
                for day in sorted(days):
                    entry = days[day]
                    adjacent = previous is not None and date.fromisoformat(day) - date.fromisoformat(previous) == timedelta(days=1)
                    if day not in reused:
                        tail = _stored_tail(db, previous, gap_seconds) if adjacent and previous is not None else None
                        built = _compute_day(db, day, entry, tail, gap_seconds)
                        db.execute("INSERT INTO history_days VALUES (?,?,?,?,?,?,?)", (day, keys[day], entry["tier"], entry.get("closure"), entry.get("unit"), built["as_of"], built["recorded"]))
                        stats["collection.history_days_built"] += 1
                    previous = day
                stats["collection.history_days_reused"] += len(reused)
                db.commit()
                db.execute("DETACH DATABASE src")

            surface._atomic(path, build)
            stats["collection.history_files_built"] += 1
            surface.prune(folder, keep=path, kind=_is_history)
        else:
            stats["collection.history_files_reused"] += 1
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        meta = {r[0]: {"key": r[1], "tier": r[2], "closure": r[3], "unit": r[4], "as_of": r[5], "recorded": bool(r[6])} for r in db.execute("SELECT * FROM history_days")}
    return path, meta


def _copy_reusable(db: sqlite3.Connection, folder: Path, path: Path, keys: dict[str, str]) -> set[str]:
    """Copy into ``main`` the rows of every day whose key an earlier history file holds (newest
    file first); returns the days copied."""
    wanted = {k: d for d, k in keys.items()}
    copied: set[str] = set()
    marker = path.name.split("_", 1)[0] + "_"
    candidates = sorted((f for f in folder.glob(f"{marker}*.sqlite") if f != path and _is_history(f)), key=lambda f: f.stat().st_mtime, reverse=True)
    for old in candidates:
        if len(copied) == len(wanted):
            break
        try:
            with sqlite3.connect(f"file:{old}?mode=ro", uri=True) as probe:
                held = [r[0] for r in probe.execute("SELECT key FROM history_days")]
        except sqlite3.Error:
            continue
        days = sorted(wanted[k] for k in held if k in wanted and wanted[k] not in copied)
        if not days:
            continue
        db.commit()
        db.execute("ATTACH DATABASE ? AS old", (str(old),))  # read only (SELECT); an earlier pin's file is never written
        try:
            marks = ",".join("?" * len(days))
            for table in ("episode_piece", "issue_day", "coverage_day", "history_days"):
                db.execute(f"INSERT INTO main.{table} SELECT * FROM old.{table} WHERE day IN ({marks})", days)
        finally:
            db.commit()
            db.execute("DETACH DATABASE old")
        copied.update(days)
    return copied


class MemberHistory:
    """Typed history tables of the live log member under one request pin (module doc). Keep the
    instance alive while its aggregates run: it owns the read connection to the pin's file."""

    def __init__(self, engine: Any, ctx: Any):
        self.engine = engine
        self.ctx = ctx
        self.config = engine.config
        member = log_member(self.config)
        if member is None:
            raise HistoryUnavailable("no live log member serves the logs block")
        self.member = member
        backend = engine.backends[self.config.storage.catalog]
        self.days = served_days(backend.view(ctx.generation), member)
        self._db: sqlite3.Connection | None = None
        self._path: Path | None = None
        self._meta: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def __enter__(self) -> MemberHistory:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def tiers(self, first: str, last: str) -> dict[str, DayTier]:
        """Every day of ``[first, last]`` in exactly one tier (archive, provisional or missing)."""
        out: dict[str, DayTier] = {}
        day = date.fromisoformat(first)
        while day.isoformat() <= last:
            d = day.isoformat()
            entry = self.days.get(d)
            if entry is None:
                out[d] = DayTier("missing")
            elif entry["tier"] == "archive":
                out[d] = DayTier("archive", entry["closure"], entry["unit"])
            else:
                out[d] = DayTier("provisional", None, "provisional")
            day += timedelta(days=1)
        return out

    def aggregate(self, tiers: dict[str, DayTier], gap_seconds: int) -> MemberAggregate:
        """The aggregate over the pin's history file (built once per pin, module doc); provisional
        tiers get their ``as_of``."""
        if self._db is None:
            try:
                self._path, self._meta = history_file(self.engine, self.ctx.generation, self.member, self.days, gap_seconds)
                self._db = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True, check_same_thread=False)
            except sqlite3.Error as exc:
                raise HistoryUnavailable(f"the member history file cannot be built or opened: {exc}") from exc
            self._db.row_factory = sqlite3.Row
        assert self._path is not None
        for day, tier in tiers.items():
            if tier.tier == "provisional":
                tiers[day] = DayTier("provisional", None, str(self._path), as_of=(self._meta.get(day) or {}).get("as_of"))
        return MemberAggregate(self._path, self._db, tiers)

    def subset(self, tiers: dict[str, DayTier]) -> MemberAggregate:
        """An aggregate over some of the days of the pin's file (no rebuild)."""
        if self._db is None or self._path is None:
            raise HistoryUnavailable("the member history tables are not built")
        return MemberAggregate(self._path, self._db, tiers)

    def recorded_days(self, days: list[str]) -> set[str]:
        """Days whose coverage records carry ``success_documents``: captured with success heartbeat
        rules (module doc)."""
        return {d for d in days if (self._meta.get(d) or {}).get("recorded")}


def _tail(pieces: list[dict[str, Any]] | None, day: str | None, gap_seconds: int) -> list[dict[str, Any]] | None:
    """The previous day's pieces ending within ``gap_seconds`` of its end (``boundary_tail``)."""
    if pieces is None or day is None:
        return None
    end = midnight(day) + timedelta(days=1)
    cutoff = end - timedelta(seconds=gap_seconds + 60)
    return [p for p in pieces if timestamp(p["last_failure"]) >= cutoff]
