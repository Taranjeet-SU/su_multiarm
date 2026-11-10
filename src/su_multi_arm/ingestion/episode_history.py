"""Typed day tables of the episode history, the cross-midnight count rule and the tiered
aggregate (plan ``unified_su_kg_phase1`` rev 2, increment I1, §4.1).

**Tables.** Every closed day writes, next to its EAV day shard, typed rows into the archive file
(append-only; days <= N are never rewritten): ``episode_piece`` (one row per per-day episode piece:
tenant, env, service, rule, category, issue, first/last failure, occurrences, evidence count and
the continuation columns), ``issue_day`` (tenant x env x service x rule x day: occurrences,
``episodes_started``, ``episodes_continued``, first/last) and ``coverage_day`` (one row per captured
source scope). A provisional day's projection file (``provisional.py``) has the same tables with
``provisional = 1`` and ``as_of``. ``history_log`` is a hash chain over per-day row digests of the
three tables and of the day's EAV shard rows: ``verify`` recomputes them, so a changed or deleted
earlier row is detected (row-hash audit; physical page changes are not logical changes).

**Cross-midnight count rule.** Archived rows stay per-day pieces. When day D is built, each piece
whose first failure lies within ``gap_seconds`` after D's midnight is compared with the boundary
tail of D-1 (pieces of the same tenant/env/service/rule ending within ``gap_seconds`` before
midnight; one indexed read, no rewrite). A match sets ``continues_id`` / ``is_continuation`` and
carries ``chain_first``. If D-1 has no rows to compare with (a gap, not archived, the first day),
such a piece gets ``boundary_unknown = 1`` and counts as a new episode (stated in answers). Over a
range [A, B]: ``episodes = sum(episodes_started) + sum(episodes_continued on day A)``: a chain is
counted once, on its first day inside the range. ``occurrences`` sum window-exactly.

**Tiered aggregate.** ``TieredAggregate`` splits the requested days into archive days (the pinned
generation's shards), provisional days (the provisional store's pinned projection files) and gap or
missing days; runs one ``GROUP BY`` per tier inside SQLite over ``issue_day`` and merges the
partial results over the disjoint day sets. Only mergeable measures exist (sums, min, max, counts of
distinct days, which add over disjoint days, and distinct tenants by key union, bounded by
``MAX_GROUPS``); anything else is refused. Listings push ``ORDER BY last_failure DESC LIMIT k`` into
each tier and merge. No row is loaded into Python except the groups and the k listed pieces, so a
month or year range never reaches the EAV ``MAX_MATCHED`` bound.

**Exclusions and liveness reads (O6).** ``HistorySpec.exclude`` drops rows whose value is in a list
(a ``NULL`` value is kept): failure views exclude ``category: success``. ``TieredAggregate.pieces``
reads every matching piece of the tiers in time order (bounded by ``MAX_PIECES``, refused above)
and ``horizons`` the latest captured instant per source scope, for the liveness view
(``runtime/liveness.py``); ``day_recipes`` reads the keyword recipe each archived day was captured
with.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..core.errors import SuError
from .episode_summaries import timestamp

HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS episode_piece(day TEXT NOT NULL, doc_id TEXT NOT NULL, source TEXT NOT NULL, tenant TEXT NOT NULL, env TEXT NOT NULL, service TEXT NOT NULL, signature_id TEXT NOT NULL, category TEXT, issue TEXT, first_failure TEXT NOT NULL, last_failure TEXT NOT NULL, occurrences INTEGER NOT NULL, evidence_count INTEGER NOT NULL, continues_id TEXT, is_continuation INTEGER NOT NULL, boundary_unknown INTEGER NOT NULL, chain_first TEXT NOT NULL, partial INTEGER NOT NULL, provisional INTEGER NOT NULL, as_of TEXT, PRIMARY KEY(day, doc_id));
CREATE INDEX IF NOT EXISTS episode_piece_last ON episode_piece(day, last_failure);
CREATE TABLE IF NOT EXISTS issue_day(day TEXT NOT NULL, source TEXT NOT NULL, tenant TEXT NOT NULL, env TEXT NOT NULL, service TEXT NOT NULL, signature_id TEXT NOT NULL, category TEXT, issue TEXT, occurrences INTEGER NOT NULL, episodes_started INTEGER NOT NULL, episodes_continued INTEGER NOT NULL, boundary_unknown INTEGER NOT NULL, first_failure TEXT, last_failure TEXT, partial INTEGER NOT NULL, provisional INTEGER NOT NULL, as_of TEXT, PRIMARY KEY(day, tenant, env, service, signature_id));
CREATE INDEX IF NOT EXISTS issue_day_source ON issue_day(source, day);
CREATE TABLE IF NOT EXISTS coverage_day(day TEXT NOT NULL, doc_id TEXT NOT NULL, source TEXT NOT NULL, tenant TEXT NOT NULL, env TEXT NOT NULL, service TEXT NOT NULL, window_start TEXT, window_end TEXT, observed_at TEXT, full_day INTEGER NOT NULL, partial INTEGER NOT NULL, provisional INTEGER NOT NULL, as_of TEXT, PRIMARY KEY(day, doc_id));
CREATE TABLE IF NOT EXISTS history_log(seq INTEGER PRIMARY KEY, day TEXT NOT NULL UNIQUE, digests TEXT NOT NULL, prev TEXT NOT NULL, hash TEXT NOT NULL);
"""
TABLES = {
    "episode_piece": ("day", "doc_id"),
    "issue_day": ("day", "tenant", "env", "service", "signature_id"),
    "coverage_day": ("day", "doc_id"),
}
PIECE_COLUMNS = ("day", "doc_id", "source", "tenant", "env", "service", "signature_id", "category", "issue", "first_failure", "last_failure", "occurrences", "evidence_count", "continues_id", "is_continuation", "boundary_unknown", "chain_first", "partial", "provisional", "as_of")
ISSUE_COLUMNS = ("day", "source", "tenant", "env", "service", "signature_id", "category", "issue", "occurrences", "episodes_started", "episodes_continued", "boundary_unknown", "first_failure", "last_failure", "partial", "provisional", "as_of")
COVERAGE_COLUMNS = ("day", "doc_id", "source", "tenant", "env", "service", "window_start", "window_end", "observed_at", "full_day", "partial", "provisional", "as_of")
KEY = ("tenant", "env", "service", "signature_id")
GENESIS = "0" * 64
MAX_GROUPS = 100_000  # merged groups one aggregate may hold (refused above, never cut)
MAX_LIST = 500
MAX_PIECES = 50_000  # pieces one liveness read may load (refused above, never cut)


class HistoryUnavailable(SuError):
    """The history tables cannot answer as asked (days without typed rows, a non-mergeable
    measure, too many groups). Nothing is answered partially."""


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(HISTORY_SCHEMA)


def chain_hash(prev: str, entry: dict[str, Any]) -> str:
    return hashlib.sha256((prev + json.dumps(entry, sort_keys=True, separators=(",", ":"))).encode()).hexdigest()


def previous_day(day: str) -> str:
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


def midnight(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc)


# --- building a day --------------------------------------------------------------------------------


def piece_of(day: str, source: str, metadata: dict[str, Any], doc_id: str) -> dict[str, Any]:
    """One ``episode_piece`` row from an ``incident`` document's metadata (continuation unset)."""
    return {
        "day": day,
        "doc_id": doc_id,
        "source": source,
        "tenant": str(metadata.get("tenant") or ""),
        "env": str(metadata.get("env") or ""),
        "service": str(metadata.get("service") or ""),
        "signature_id": str(metadata.get("signature_id") or ""),
        "category": metadata.get("category"),
        "issue": metadata.get("issue"),
        "first_failure": str(metadata["first_hour"]),
        "last_failure": str(metadata["last_hour"]),
        "occurrences": int(metadata.get("occurrences") or 0),
        "evidence_count": int(metadata.get("evidence_count") or 0),
        "continues_id": None,
        "is_continuation": 0,
        "boundary_unknown": 0,
        "chain_first": str(metadata["first_hour"]),
        "partial": 0,
        "provisional": 0,
        "as_of": None,
    }


def coverage_of(day: str, source: str, metadata: dict[str, Any], doc_id: str) -> dict[str, Any]:
    start, end = midnight(day), midnight(day) + timedelta(days=1)
    full = bool(metadata.get("window_start")) and timestamp(str(metadata["window_start"])) == start and timestamp(str(metadata["window_end"])) == end
    return {
        "day": day,
        "doc_id": doc_id,
        "source": source,
        "tenant": str(metadata.get("tenant") or ""),
        "env": str(metadata.get("env") or ""),
        "service": str(metadata.get("service") or ""),
        "window_start": metadata.get("window_start"),
        "window_end": metadata.get("window_end"),
        "observed_at": metadata.get("observed_at"),
        "full_day": int(full),
        "partial": 0,
        "provisional": 0,
        "as_of": None,
    }


def link_continuations(day: str, pieces: list[dict[str, Any]], tail: Sequence[dict[str, Any]] | None, gap_seconds: int) -> None:
    """The cross-midnight count rule (module docstring). ``tail``: D-1's boundary pieces, or
    ``None`` when D-1 has no rows to compare with (pieces near midnight become ``boundary_unknown``)."""
    start = midnight(day)
    best: dict[tuple, dict[str, Any]] = {}
    for prev in tail or []:
        key = tuple(prev[k] for k in KEY)
        if key not in best or prev["last_failure"] > best[key]["last_failure"]:
            best[key] = prev
    used: set[tuple] = set()
    for piece in sorted(pieces, key=lambda p: (p["first_failure"], p["doc_id"])):
        first = timestamp(piece["first_failure"])
        if (first - start).total_seconds() > gap_seconds:
            continue
        if tail is None:
            piece["boundary_unknown"] = 1
            continue
        key = tuple(piece[k] for k in KEY)
        prev = best.get(key)
        if prev is None or key in used or (first - timestamp(prev["last_failure"])).total_seconds() > gap_seconds:
            continue
        used.add(key)
        piece.update(continues_id=prev["doc_id"], is_continuation=1, chain_first=prev["chain_first"])


def boundary_tail(db: sqlite3.Connection, day: str, gap_seconds: int, *, source: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Pieces of ``day`` ending within ``gap_seconds`` before its end (one indexed read)."""
    end = midnight(day) + timedelta(days=1)
    cutoff = (end - timedelta(seconds=gap_seconds + 60)).isoformat()  # minute cells end at :59
    sql = "SELECT * FROM episode_piece WHERE day=? AND last_failure>=?"
    params: list[Any] = [day, cutoff[:19]]
    if source is not None:
        sql += f" AND source IN ({','.join('?' * len(source)) or 'NULL'})"
        params.extend(source)
    db.row_factory = sqlite3.Row
    return [dict(r) for r in db.execute(sql, params)]


def issue_rows(day: str, source: str, pieces: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out: dict[tuple, dict[str, Any]] = {}
    for p in pieces:
        key = tuple(p[k] for k in KEY)
        row = out.get(key)
        if row is None:
            row = out[key] = {
                "day": day, "source": source, **dict(zip(KEY, key)), "category": p["category"], "issue": p["issue"],
                "occurrences": 0, "episodes_started": 0, "episodes_continued": 0, "boundary_unknown": 0,
                "first_failure": p["first_failure"], "last_failure": p["last_failure"],
                "partial": p["partial"], "provisional": p["provisional"], "as_of": p["as_of"],
            }
        row["occurrences"] += p["occurrences"]
        row["episodes_continued" if p["is_continuation"] else "episodes_started"] += 1
        row["boundary_unknown"] += p["boundary_unknown"]
        row["first_failure"] = min(row["first_failure"], p["first_failure"])
        row["last_failure"] = max(row["last_failure"], p["last_failure"])
    return [out[k] for k in sorted(out)]


def insert_rows(db: sqlite3.Connection, pieces: Sequence[dict[str, Any]], issues: Sequence[dict[str, Any]], coverage: Sequence[dict[str, Any]]) -> None:
    for table, columns, rows in (("episode_piece", PIECE_COLUMNS, pieces), ("issue_day", ISSUE_COLUMNS, issues), ("coverage_day", COVERAGE_COLUMNS, coverage)):
        db.executemany(f"INSERT INTO {table}({','.join(columns)}) VALUES ({','.join('?' * len(columns))})", [tuple(r[c] for c in columns) for r in rows])


def table_digest(db: sqlite3.Connection, table: str, day: str) -> dict[str, Any]:
    """Row count and sha256 of one day's rows of a typed table (canonical: primary-key order)."""
    columns = {"episode_piece": PIECE_COLUMNS, "issue_day": ISSUE_COLUMNS, "coverage_day": COVERAGE_COLUMNS}[table]
    digest, n = hashlib.sha256(), 0
    for row in db.execute(f"SELECT {','.join(columns)} FROM {table} WHERE day=? ORDER BY {','.join(TABLES[table])}", (day,)):
        digest.update(json.dumps(list(row), separators=(",", ":")).encode())
        digest.update(b"\n")
        n += 1
    return {"rows": n, "sha256": digest.hexdigest()}


def shard_digest(db: sqlite3.Connection, shard: str) -> dict[str, Any]:
    """Row count and sha256 of an EAV day shard's documents (id order)."""
    digest, n = hashlib.sha256(), 0
    for row in db.execute("SELECT id, data FROM rows WHERE shard_id=? ORDER BY id", (shard,)):
        digest.update(row[0].encode())
        digest.update(hashlib.sha256(row[1].encode()).digest())
        n += 1
    return {"rows": n, "sha256": digest.hexdigest()}


def day_digests(db: sqlite3.Connection, day: str, shard: str | None) -> dict[str, Any]:
    out = {table: table_digest(db, table, day) for table in TABLES}
    if shard is not None:
        out["shard"] = shard_digest(db, shard)
    return out


def append_history_log(db: sqlite3.Connection, day: str, digests: dict[str, Any]) -> None:
    last = db.execute("SELECT seq, hash FROM history_log ORDER BY seq DESC LIMIT 1").fetchone()
    seq, prev = (last[0] + 1, last[1]) if last else (1, GENESIS)
    body = {"seq": seq, "day": day, "digests": digests}
    db.execute("INSERT INTO history_log VALUES (?,?,?,?,?)", (seq, day, json.dumps(digests, sort_keys=True), prev, chain_hash(prev, body)))


def check_history(db: sqlite3.Connection, shards: dict[str, str]) -> list[str]:
    """Recompute the history chain and every day's row digests (``shards``: day -> shard id)."""
    problems: list[str] = []
    prev = GENESIS
    rows = db.execute("SELECT seq, day, digests, prev, hash FROM history_log ORDER BY seq").fetchall()
    for i, (seq, day, digests, row_prev, row_hash) in enumerate(rows, start=1):
        recorded = json.loads(digests)
        if seq != i or row_prev != prev or chain_hash(prev, {"seq": seq, "day": day, "digests": recorded}) != row_hash:
            problems.append(f"history log entry {i} does not chain (a history entry was removed or edited)")
            break
        prev = row_hash
        actual = day_digests(db, day, shards.get(day) if "shard" in recorded else None)
        changed = sorted(name for name in recorded if recorded[name] != actual.get(name))
        if changed:
            problems.append(f"history rows of day {day} changed after they were written ({', '.join(changed)})")
    return problems


# --- reading ----------------------------------------------------------------------------------------


@dataclass
class DayTier:
    tier: str  # archive | provisional | gap | missing
    closure: str | None = None  # complete | partial (archive days)
    source: str | None = None  # the shard id or the provisional projection file
    as_of: str | None = None
    missing_scopes: list[str] = field(default_factory=list)
    reason: str | None = None


def tier_map(manifest: dict[str, Any], provisional: dict[str, dict[str, Any]], first: str, last: str, view: dict[str, Any] | None = None) -> dict[str, DayTier]:
    """Every day of ``[first, last]`` in exactly one tier: archive when the archive serving
    pointer (``view``, read once per request; else the pinned manifest) serves its shard, else
    provisional when the pinned provisional ledger has a projection file, else gap (a recorded gap)
    or missing. A day is never read from two tiers."""
    logs = manifest.get("logs") or {}
    if view is not None:
        shards, ledger, gaps = view["shards"], view["ledger"], view["gaps"]
    else:
        shards = (logs.get("history") or {}).get("shards") or {}
        ledger = logs.get("ledger") or {}
        gaps = (logs.get("archive") or {}).get("gaps") or {}
    out: dict[str, DayTier] = {}
    day = date.fromisoformat(first)
    while day.isoformat() <= last:
        d = day.isoformat()
        entry = ledger.get(d) or {}
        if d in shards:
            out[d] = DayTier("archive", entry.get("closure") or "complete", shards[d], missing_scopes=list(entry.get("missing_scopes") or []))
        elif d in provisional:
            p = provisional[d]
            out[d] = DayTier("provisional", None, p["file"], as_of=p.get("as_of"), missing_scopes=list(p.get("missing_scopes") or []))
        elif d in gaps:
            out[d] = DayTier("gap", reason=gaps[d])
        else:
            out[d] = DayTier("missing")
        day += timedelta(days=1)
    return out


GROUPS = {"tenant": "tenant", "env": "env", "service": "service", "signature_id": "signature_id", "category": "category", "day": "day", "month": "substr(day,1,7)"}
FILTERS = ("tenant", "env", "service", "signature_id", "category")
MEASURES = {"occurrences": "sum", "episodes": "count rule", "first_failure": "min", "last_failure": "max", "active_days": "distinct days (additive over disjoint days)", "tenants": "distinct tenants (key union)"}


@dataclass
class HistorySpec:
    first: str  # inclusive
    last: str  # inclusive
    filters: dict[str, list[str]] = field(default_factory=dict)
    group_by: list[str] = field(default_factory=lambda: ["signature_id"])
    measures: list[str] = field(default_factory=lambda: list(MEASURES))
    list_k: int = 0
    # values a row must not have (``NULL`` kept); failure views exclude {"category": ["success"]}
    exclude: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class HistoryResult:
    groups: list[dict[str, Any]]
    pieces: list[dict[str, Any]]
    tiers: dict[str, DayTier]
    rows_read: int
    boundary_unknown: int
    excluded_occurrences: int = 0  # rule-matched messages of the excluded rows (``HistorySpec.exclude``)


class TieredAggregate:
    """Per-tier aggregate pushdown over ``issue_day`` and merge over disjoint days (module docstring)."""

    def __init__(self, archive_path: Path | None, tiers: dict[str, DayTier]):
        self.archive_path = archive_path
        self.tiers = tiers
        self.rows_read = 0

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
        db.row_factory = sqlite3.Row
        return db

    def _excluded(self, db: sqlite3.Connection, spec: HistorySpec, days: list[str], sources: list[str] | None) -> int:
        """Occurrences of the rows ``spec.exclude`` drops (stated by the caller, never silent)."""
        inverse = HistorySpec(first=spec.first, last=spec.last, filters=spec.filters)
        where, params = self._where(inverse, days)
        held = [f"{name} IN ({','.join('?' * len(values))})" for name, values in spec.exclude.items() if values]
        params.extend(v for values in spec.exclude.values() for v in values)
        if sources is not None:
            where += f" AND source IN ({','.join('?' * len(sources))})"
            params.extend(sources)
        return int(db.execute(f"SELECT coalesce(sum(occurrences), 0) FROM issue_day WHERE {where} AND ({' OR '.join(held)})", params).fetchone()[0])

    def _where(self, spec: HistorySpec, days: list[str]) -> tuple[str, list[Any]]:
        clauses = [f"day IN ({','.join('?' * len(days))})"]
        params: list[Any] = list(days)
        for name, values in spec.filters.items():
            if name not in FILTERS:
                raise HistoryUnavailable(f"issue history cannot filter on {name!r} (filters: {', '.join(FILTERS)})")
            if values:
                clauses.append(f"{name} IN ({','.join('?' * len(values))})")
                params.extend(values)
        for name, values in spec.exclude.items():
            if name not in FILTERS:
                raise HistoryUnavailable(f"issue history cannot exclude on {name!r} (filters: {', '.join(FILTERS)})")
            if values:
                clauses.append(f"({name} IS NULL OR {name} NOT IN ({','.join('?' * len(values))}))")
                params.extend(values)
        return " AND ".join(clauses), params

    def _plan(self) -> list[tuple[Path, list[str], list[str] | None]]:
        """(file, days, pinned sources) per tier: one archive query, one per provisional file."""
        archive = sorted(d for d, t in self.tiers.items() if t.tier == "archive")
        out: list[tuple[Path, list[str], list[str] | None]] = []
        if archive:
            if self.archive_path is None:
                raise HistoryUnavailable("archive days are served but the archive file is not configured")
            out.append((self.archive_path, archive, sorted({self.tiers[d].source or "" for d in archive})))
        for d, t in sorted(self.tiers.items()):
            if t.tier == "provisional":
                out.append((Path(str(t.source)), [d], None))
        return out

    def _tiers(self, spec: HistorySpec):
        """(tier name, connection, where, params) per tier, the archive restricted to its shards."""
        self.check_built()
        for path, days, sources in self._plan():
            tier = "provisional" if sources is None else "archive"
            where, params = self._where(spec, days)
            if sources is not None:
                where += f" AND source IN ({','.join('?' * len(sources))})"
                params.extend(sources)
            try:
                db = self._connect(path)
            except sqlite3.Error as exc:
                raise HistoryUnavailable(f"the {tier} file of days {days} cannot be opened: {exc}") from exc
            yield tier, db, where, params

    def pieces(self, spec: HistorySpec, limit: int = MAX_PIECES) -> list[dict[str, Any]]:
        """Every matching ``episode_piece`` of the tiers in ``(first_failure, doc_id)`` order (the
        liveness read). More than ``limit`` pieces is refused, never cut."""
        out: list[dict[str, Any]] = []
        for tier, db, where, params in self._tiers(spec):
            try:
                rows = db.execute(f"SELECT * FROM episode_piece WHERE {where} ORDER BY first_failure, doc_id LIMIT ?", [*params, limit + 1 - len(out)]).fetchall()
            finally:
                db.close()
            self.rows_read += len(rows)
            out.extend({**dict(r), "tier": tier} for r in rows)
            if len(out) > limit:
                raise HistoryUnavailable(f"more than {limit} episode pieces match; narrow the window, client or service")
        out.sort(key=lambda p: (p["first_failure"], p["doc_id"]))
        return out

    def horizons(self, spec: HistorySpec) -> dict[tuple[str, str, str], str]:
        """The latest captured instant (``window_end``) per (tenant, env, service) over the tiers
        (``coverage_day``; the category/rule filters do not apply to coverage rows)."""
        scope = HistorySpec(first=spec.first, last=spec.last, filters={k: v for k, v in spec.filters.items() if k in {"tenant", "env", "service"}})
        out: dict[tuple[str, str, str], str] = {}
        for _tier, db, where, params in self._tiers(scope):
            try:
                rows = db.execute(f"SELECT tenant, env, service, max(window_end) AS horizon FROM coverage_day WHERE {where} GROUP BY tenant, env, service", params).fetchall()
            finally:
                db.close()
            self.rows_read += len(rows)
            for r in rows:
                key = (r["tenant"], r["env"], r["service"])
                if r["horizon"] is not None and (key not in out or timestamp(r["horizon"]) > timestamp(out[key])):
                    out[key] = r["horizon"]
        return out

    def check_built(self) -> None:
        archive = sorted(d for d, t in self.tiers.items() if t.tier == "archive")
        if not archive:
            return
        assert self.archive_path is not None
        with self._connect(self.archive_path) as db:
            built = {r[0] for r in db.execute(f"SELECT day FROM history_log WHERE day IN ({','.join('?' * len(archive))})", archive)}
        unbuilt = [d for d in archive if d not in built]
        if unbuilt:
            raise HistoryUnavailable(f"archived days {unbuilt[:5]}{' ...' if len(unbuilt) > 5 else ''} have no history tables yet ({len(unbuilt)} days; run logs policy import to build them)")

    def execute(self, spec: HistorySpec) -> HistoryResult:
        unknown = [m for m in spec.measures if m not in MEASURES]
        if unknown:
            raise HistoryUnavailable(f"measures {unknown} are not mergeable over day tiers (mergeable: {', '.join(MEASURES)})")
        bad = [g for g in spec.group_by if g not in GROUPS]
        if bad:
            raise HistoryUnavailable(f"issue history cannot group by {bad} (dimensions: {', '.join(GROUPS)})")
        if not 0 <= spec.list_k <= MAX_LIST:
            raise HistoryUnavailable(f"list_k must be between 0 and {MAX_LIST}")
        self.check_built()
        cols = [f"{GROUPS[g]} AS g{i}" for i, g in enumerate(spec.group_by)]
        group_sql = ",".join(f"g{i}" for i in range(len(spec.group_by)))
        select = ", ".join(
            [
                *cols,
                "sum(occurrences) AS occ",
                "sum(episodes_started) AS started",
                "sum(CASE WHEN day=? THEN episodes_continued ELSE 0 END) AS heads",
                "min(first_failure) AS first_failure",
                "max(last_failure) AS last_failure",
                "count(DISTINCT day) AS days",
                "sum(boundary_unknown) AS boundary_unknown",
                "max(partial) AS partial",
                "max(issue) AS issue",
                "max(category) AS category",
                "json_group_array(DISTINCT tenant) AS tenants",
            ]
        )
        merged: dict[tuple, dict[str, Any]] = {}
        pieces: list[list[dict[str, Any]]] = []
        boundary = excluded = 0
        for path, days, sources in self._plan():
            tier = "provisional" if sources is None else "archive"
            where, params = self._where(spec, days)
            if sources is not None:
                where += f" AND source IN ({','.join('?' * len(sources))})"
                params.extend(sources)
            try:
                db = self._connect(path)
            except sqlite3.Error as exc:
                raise HistoryUnavailable(f"the {tier} file of days {days} cannot be opened: {exc}") from exc
            with db:
                sql = f"SELECT {select} FROM issue_day WHERE {where}" + (f" GROUP BY {group_sql}" if group_sql else "")
                fetched = db.execute(sql, [spec.first, *params]).fetchall()
                self.rows_read += len(fetched)
                for row in fetched:
                    if row["occ"] is None:
                        continue  # an empty tier (no matching rows) returns one all-NULL row without GROUP BY
                    key = tuple(row[f"g{i}"] for i in range(len(spec.group_by)))
                    entry = merged.get(key)
                    if entry is None:
                        if len(merged) >= MAX_GROUPS:
                            raise HistoryUnavailable(f"the aggregate has more than {MAX_GROUPS} groups; narrow the range or the grouping")
                        entry = merged[key] = {**dict(zip(spec.group_by, key)), "occurrences": 0, "episodes": 0, "first_failure": row["first_failure"], "last_failure": row["last_failure"], "active_days": 0, "tenant_set": set(), "tiers": set(), "partial": False, "provisional": False, "boundary_unknown": 0, "issue": row["issue"], "category": row["category"]}
                    entry["occurrences"] += int(row["occ"])
                    entry["episodes"] += int(row["started"]) + int(row["heads"])
                    entry["first_failure"] = min(entry["first_failure"], row["first_failure"])
                    entry["last_failure"] = max(entry["last_failure"], row["last_failure"])
                    entry["active_days"] += int(row["days"])
                    entry["tenant_set"].update(json.loads(row["tenants"]))
                    entry["tiers"].add(tier)
                    entry["partial"] = entry["partial"] or bool(row["partial"])
                    entry["provisional"] = entry["provisional"] or tier == "provisional"
                    entry["boundary_unknown"] += int(row["boundary_unknown"])
                    entry["issue"] = entry["issue"] or row["issue"]
                    entry["category"] = entry["category"] or row["category"]
                    boundary += int(row["boundary_unknown"])
                if any(spec.exclude.values()):
                    excluded += self._excluded(db, spec, days, sources)
                if spec.list_k:
                    listed = db.execute(f"SELECT * FROM episode_piece WHERE {where} ORDER BY last_failure DESC, doc_id DESC LIMIT ?", [*params, spec.list_k]).fetchall()
                    self.rows_read += len(listed)
                    pieces.append([{**dict(r), "tier": tier} for r in listed])
        groups = []
        for entry in merged.values():
            tenants = entry.pop("tenant_set")
            if "tenants" in spec.measures:
                entry["tenants_affected"] = len(tenants)
            entry["tiers"] = sorted(entry["tiers"])
            groups.append({k: v for k, v in entry.items() if k in spec.group_by or k in spec.measures or k in {"tiers", "partial", "provisional", "boundary_unknown", "issue", "category", "tenants_affected"}})
        groups.sort(key=lambda g: (-g.get("occurrences", 0), *[str(g.get(k)) for k in spec.group_by]))
        listed = list(heapq.merge(*pieces, key=lambda p: (p["last_failure"], p["doc_id"]), reverse=True))[: spec.list_k]
        for p in listed:
            p["continues_before_window"] = bool(p["is_continuation"]) and p["chain_first"][:10] < spec.first
            p["provisional"] = bool(p["provisional"])
            p["partial"] = bool(p["partial"])
            p["is_continuation"] = bool(p["is_continuation"])
            p["boundary_unknown"] = bool(p["boundary_unknown"])
        return HistoryResult(groups=groups, pieces=listed, tiers=self.tiers, rows_read=self.rows_read, boundary_unknown=boundary, excluded_occurrences=excluded)


def day_recipes(archive_path: Path, days: Sequence[str]) -> dict[str, str]:
    """The keyword recipe identity each archived day of ``days`` was captured with (read-only)."""
    if not days:
        return {}
    db = sqlite3.connect(f"file:{archive_path}?mode=ro", uri=True, timeout=60)
    try:
        rows = db.execute(f"SELECT day, recipe FROM archive_days WHERE day IN ({','.join('?' * len(days))})", list(days)).fetchall()
    except sqlite3.Error as exc:
        raise HistoryUnavailable(f"the archive's day ledger cannot be read: {exc}") from exc
    finally:
        db.close()
    return {str(d): str(r) for d, r in rows}
