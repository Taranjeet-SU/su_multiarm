"""Long-term archive of keyword/episode day aggregates (2026-11-08 standard; ``logs.collection.archive``).

The source-side summary path (``episode_summaries.py``) keeps keyword counters, observed episodes
(category, issue, first/last failure, occurrences, recovery basis, bounded masked evidence) and the
shared-rule communities per index and UTC day. This module keeps them **forever** and serves every
archived day through the H1 history tier, while the serving generation stays small.

**Day shards.** ``close_day(D)`` turns the complete snapshots of one UTC day into the canonical
documents (``summary_documents``, with evidence citations) and writes the aggregate kinds
(``ARCHIVE_KINDS``) as one append-only, content-addressed day shard of an ``EpisodeArchiveStore``
(a ``SqliteHistoryStore`` with its own kinds and file). Appending day N+1 writes rows for N+1 only;
days <= N are never read or rewritten. A rollup pass covers exactly one day: a snapshot whose
window is not ``[D 00:00Z, D+1 00:00Z)`` is refused, so a pass never crosses a day boundary.
Archived days are never garbage-collected (``gc`` refuses) and an archived day is never replaced.

**Recipe.** Every day records the keyword recipe identity it was captured with
(``log_policy.load_keyword_recipe``: pinned files). A snapshot whose summary recipe differs from
the policy's compiled recipe is refused, and so is a day whose recipe differs from the archived
days of the current epoch (``keywords.effective_from``): no silent mixing.

**Ledger.** Each closed day and each explicit gap record is appended to a hash-chained log; the
head is also written next to the archive file. ``verify`` (``log_policy.verify_policy``) recomputes
the chain, so a deleted day or log entry is detected.

**Serving.** ``publish`` builds the serving generation: the rule catalog (``signature``) and the
live window, every document of the last ``logs.retention.keep_days`` days as built (untrimmed;
``unclassified_example`` exists only here), so search, the graph and communities cover recent days
as before. Its manifest ``logs.history`` names ``store: episode_archive``, the archive kinds and
every archived day shard; the engine routes every structured scope over archive kinds to the
archive (``Engine.records_execute``), so a count never mixes the two copies.

**I1 (plan K §4.1).** A close streams the snapshots one at a time through a spill file
(``day_spill.DaySpill``: bounded memory), writes the day's typed history rows
(``episode_history``: ``episode_piece``, ``issue_day``, ``coverage_day``, the cross-midnight count
rule against D-1's boundary tail, the row-hash ``history_log``) in the same transaction as the
ledger entry, and may be ``partial``: a day without a complete set of full-day snapshots by
D + ``close_after_days`` + ``grace_days`` is archived from the latest captured scope versions,
flagged, with its missing and stale scopes listed (a later complete set is refused: append-only).
``import_staging`` closes days in order at D + ``close_after_days``; ``refresh_provisional``
serves every unclosed day from the provisional store (``provisional.py``). The published manifest
carries ``logs.ledger`` (closure and missing scopes per archived day).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from ..core.errors import ConfigError, SuError
from ..core.types import Document
from .episode_history import (
    append_history_log,
    boundary_tail,
    check_history,
    coverage_of,
    day_digests,
    ensure_schema,
    insert_rows,
    issue_rows,
    link_continuations,
    piece_of,
    previous_day,
)
from .episode_summaries import CONTRACT, compile_summary, timestamp
from .history import SqliteHistoryStore, summarise

ARCHIVE_KINDS = frozenset({"summary_coverage", "unit_day", "episode_cell", "incident", "signature_day", "shared_error"})
BOUNDED_KINDS = frozenset({"unclassified_example"})  # never archived: kept for the live window only
CATALOG_KINDS = frozenset({"signature"})
STORE = "episode_archive"
UNTRIMMED_FIELDS = frozenset({"header", "evidence_refs"})
TRIM_MARK = " [trimmed]"
GENESIS = "0" * 64

ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_days(day TEXT PRIMARY KEY, shard_id TEXT NOT NULL, recipe TEXT NOT NULL, scopes INTEGER NOT NULL, rows INTEGER NOT NULL, closed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_gaps(day TEXT PRIMARY KEY, reason TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_log(seq INTEGER PRIMARY KEY, day TEXT NOT NULL, entry TEXT NOT NULL, shard_id TEXT, recipe TEXT, rows INTEGER, prev TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_catalog(rule_id TEXT PRIMARY KEY, title TEXT NOT NULL, recipe TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_bounded(day TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(day, id));
"""
# columns added by I1 (additive; days archived before carry the defaults: a complete close)
DAY_COLUMNS = {
    "closure": "TEXT NOT NULL DEFAULT 'complete'",
    "scopes_expected": "INTEGER",
    "missing": "TEXT NOT NULL DEFAULT '[]'",
    "stale": "TEXT NOT NULL DEFAULT '[]'",
    "delta": "TEXT",
}
CLOSED_ENTRIES = ("closed", "closed_partial")


class ArchiveRefused(SuError):
    """A day cannot be archived as asked (incomplete, crosses a day boundary, recipe mismatch,
    already archived with other content). Nothing was written."""


def chain_hash(prev: str, entry: dict[str, Any]) -> str:
    return hashlib.sha256((prev + json.dumps(entry, sort_keys=True, separators=(",", ":"))).encode()).hexdigest()


class EpisodeArchiveStore(SqliteHistoryStore):
    """Append-only day shards of the episode aggregates plus the archive ledger (one sqlite file)."""

    kind = STORE
    day_kinds = ARCHIVE_KINDS

    def __init__(self, path: Path):
        super().__init__(path)
        with self._db() as db:
            db.executescript(ARCHIVE_SCHEMA)
            ensure_schema(db)
            present = {r["name"] for r in db.execute("PRAGMA table_info(archive_days)")}
            for name, decl in DAY_COLUMNS.items():
                if name not in present:
                    db.execute(f"ALTER TABLE archive_days ADD COLUMN {name} {decl}")

    @staticmethod
    def _pushdown(spec: Any) -> tuple[str, list[Any]]:
        """The H1 pushdown plus hour bounds: every archived row lies inside its shard's UTC day (a
        rollup pass never crosses midnight), so ``first_hour``/``last_hour``/``ts_hour`` ranges imply
        day bounds on the indexed ``day`` column (a superset; the EAV conditions stay)."""
        sql, params = SqliteHistoryStore._pushdown(spec)
        clauses = [sql] if sql != "1" else []
        for p in spec.predicates:
            if p.negate or p.field not in {"first_hour", "last_hour", "ts_hour"} or p.op != "range":
                continue
            if p.field in {"last_hour", "ts_hour"} and isinstance(p.lower, str) and len(p.lower) >= 10:
                clauses.append("r.day>=?")
                params.append(p.lower[:10])
            if p.field in {"first_hour", "ts_hour"} and isinstance(p.upper, str) and len(p.upper) >= 10:
                clauses.append("r.day<=?")
                params.append(p.upper[:10])
        return " AND ".join(clauses) or "1", params

    @property
    def head_path(self) -> Path:
        return self.path.with_name(self.path.name + ".head.json")

    # --- serving pointer (I1: incremental publish) ----------------------------------------------

    @property
    def serving_path(self) -> Path:
        return self.path.with_name(self.path.name + ".serving.json")

    def serve(self, served_at: str) -> dict[str, Any]:
        """Advance the serving pointer to the archive log head: every day closed (or gapped) up to
        it is served from the archive. O(1): no generation is rebuilt and no row is read."""
        with self._db() as db:
            last = db.execute("SELECT seq, hash FROM archive_log ORDER BY seq DESC LIMIT 1").fetchone()
        pointer = {"seq": int(last["seq"]) if last else 0, "hash": last["hash"] if last else GENESIS, "served_at": served_at}
        tmp = self.serving_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(pointer, sort_keys=True))
        tmp.replace(self.serving_path)
        return pointer

    def pointer(self) -> dict[str, Any] | None:
        return json.loads(self.serving_path.read_text()) if self.serving_path.exists() else None

    def served_view(self, pointer: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """``{shards, gaps, ledger, seq}`` of the days the pointer serves (log entries up to its
        seq), read once per request; ``None`` without a pointer (the generation manifest serves)."""
        pointer = pointer if pointer is not None else self.pointer()
        if pointer is None:
            return None
        seq = int(pointer["seq"])
        with self._db() as db:
            entries = db.execute("SELECT day, entry FROM archive_log WHERE seq<=?", (seq,)).fetchall()
            closed = {r["day"] for r in entries if r["entry"] in CLOSED_ENTRIES}
            gapped = {r["day"] for r in entries if r["entry"] == "gap"}
            days = {r["day"]: dict(r) for r in db.execute("SELECT * FROM archive_days")}
            gaps = {r["day"]: r["reason"] for r in db.execute("SELECT day, reason FROM archive_gaps")}
        return {
            "seq": seq,
            "shards": {d: days[d]["shard_id"] for d in sorted(closed) if d in days},
            "gaps": {d: gaps[d] for d in sorted(gapped) if d in gaps},
            "ledger": {d: {"closure": days[d].get("closure") or "complete", "missing_scopes": json.loads(days[d].get("missing") or "[]")} for d in sorted(closed) if d in days},
        }

    def gc(self, referenced: set[str]) -> list[str]:  # noqa: ARG002 - the signature of the history store
        raise ArchiveRefused("the episode archive is append-only: archived days are kept forever and never garbage-collected")

    # --- ledger ------------------------------------------------------------------------------------

    def days(self) -> dict[str, dict[str, Any]]:
        with self._db() as db:
            return {r["day"]: dict(r) for r in db.execute("SELECT * FROM archive_days ORDER BY day")}

    def gaps(self) -> dict[str, str]:
        with self._db() as db:
            return {r["day"]: r["reason"] for r in db.execute("SELECT day, reason FROM archive_gaps ORDER BY day")}

    def log(self) -> list[dict[str, Any]]:
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM archive_log ORDER BY seq")]

    def catalog(self) -> dict[str, dict[str, str]]:
        with self._db() as db:
            return {r["rule_id"]: {"title": r["title"], "recipe": r["recipe"]} for r in db.execute("SELECT * FROM archive_catalog ORDER BY rule_id")}

    def shard_rows(self, shard: str) -> int:
        with self._db() as db:
            return int(db.execute("SELECT count(*) FROM rows WHERE shard_id=?", (shard,)).fetchone()[0])

    def _append_log(self, db: Any, day: str, entry: str, shard: str | None, recipe: str | None, rows: int | None) -> dict[str, Any]:
        last = db.execute("SELECT seq, hash FROM archive_log ORDER BY seq DESC LIMIT 1").fetchone()
        seq, prev = (last["seq"] + 1, last["hash"]) if last else (1, GENESIS)
        body = {"seq": seq, "day": day, "entry": entry, "shard_id": shard, "recipe": recipe, "rows": rows}
        digest = chain_hash(prev, body)
        db.execute("INSERT INTO archive_log VALUES (?,?,?,?,?,?,?,?)", (seq, day, entry, shard, recipe, rows, prev, digest))
        return {"seq": seq, "hash": digest}

    def _write_head(self, head: dict[str, Any]) -> None:
        tmp = self.head_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(head, sort_keys=True))
        tmp.replace(self.head_path)

    def append_stream(self, shard: str, day: str, documents: Iterable[Document], schema: Any = None) -> int:
        """``append`` from an iterator (one transaction; documents are inserted as they arrive and
        never held together). Returns the rows written (0 when the shard existed)."""
        from ..backends._scope import document_values
        from ..backends.sqlite._db import field_row

        with self._lock, self._db() as db:
            if db.execute("SELECT 1 FROM shards WHERE shard_id=?", (shard,)).fetchone() is not None:
                return 0
            count = 0

            def written() -> Iterator[Document]:
                nonlocal count
                for doc in documents:
                    if doc.kind not in self.day_kinds:
                        raise ValueError(f"document {doc.id} of kind {doc.kind!r} is not a day kind")
                    db.execute("INSERT INTO rows VALUES (?,?,?,?,?)", (shard, doc.id, doc.kind, day, doc.model_dump_json()))
                    db.executemany("INSERT INTO history_fields VALUES (?,?,?,?,?,?,?,?)", [(shard, *field_row(doc.id, key, value)) for key, value in document_values(doc).items()])
                    count += 1
                    yield doc

            summary = summarise(written())
            db.execute("INSERT INTO shards VALUES (?,?,?,?,strftime('%s','now'))", (shard, day, count, json.dumps(summary, sort_keys=True)))
        self.writes += count
        return count

    def record_day(
        self,
        day: str,
        shard: str,
        recipe: str,
        scopes: int,
        rows: int,
        closed_at: str,
        catalog: dict[str, str],
        bounded: Iterable[Document],
        *,
        closure: str = "complete",
        expected: int | None = None,
        missing: Sequence[str] = (),
        stale: Sequence[str] = (),
        delta: dict[str, Any] | None = None,
        history: tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]] | None = None,
    ) -> None:
        """The ledger entry of a closed day, its live-window rows and (I1) its typed history rows
        with their row-hash log entry, in one transaction."""
        with self._lock, self._db() as db:
            db.execute(
                "INSERT INTO archive_days(day, shard_id, recipe, scopes, rows, closed_at, closure, scopes_expected, missing, stale, delta) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (day, shard, recipe, scopes, rows, closed_at, closure, expected, json.dumps(sorted(missing)), json.dumps(sorted(stale)), json.dumps(delta, sort_keys=True) if delta is not None else None),
            )
            for rule_id, title in catalog.items():
                db.execute("INSERT OR REPLACE INTO archive_catalog VALUES (?,?,?)", (rule_id, title, recipe))
            for doc in bounded:
                db.execute("INSERT OR REPLACE INTO archive_bounded VALUES (?,?,?)", (day, doc.id, doc.model_dump_json()))
            if history is not None:
                insert_rows(db, *history)
                append_history_log(db, day, day_digests(db, day, shard))
            head = self._append_log(db, day, "closed_partial" if closure == "partial" else "closed", shard, recipe, rows)
        self._write_head(head)

    # --- typed history (I1) ------------------------------------------------------------------------

    def history_days(self) -> set[str]:
        with self._db() as db:
            return {r["day"] for r in db.execute("SELECT day FROM history_log")}

    def boundary_tail(self, day: str, gap_seconds: int) -> list[dict[str, Any]] | None:
        """D's pieces ending near midnight, or ``None`` when D has no typed rows (gap, not archived)."""
        with self._db() as db:
            if db.execute("SELECT 1 FROM history_log WHERE day=?", (day,)).fetchone() is None:
                return None
            return boundary_tail(db, day, gap_seconds)

    def day_documents(self, shard: str, kinds: Sequence[str]) -> Iterator[Document]:
        with self._db() as db:
            for row in db.execute(f"SELECT data FROM rows WHERE shard_id=? AND kind IN ({','.join('?' * len(kinds))}) ORDER BY id", [shard, *kinds]):
                yield Document.model_validate_json(row["data"])

    def build_history(self, day: str, gap_seconds: int) -> dict[str, Any]:
        """Typed rows of a day archived before I1, from its shard (append-only: new rows only)."""
        entry = self.days()[day]
        if day in self.history_days():
            return {"day": day, "status": "built_already"}
        shard, partial = entry["shard_id"], int(entry.get("closure") == "partial")
        pieces = [piece_of(day, shard, d.metadata, d.id) for d in self.day_documents(shard, ["incident"])]
        coverage = [coverage_of(day, shard, d.metadata, d.id) for d in self.day_documents(shard, ["summary_coverage"])]
        link_continuations(day, pieces, self.boundary_tail(previous_day(day), gap_seconds), gap_seconds)
        for row in (*pieces, *coverage):
            row["partial"] = partial
        with self._lock, self._db() as db:
            insert_rows(db, pieces, issue_rows(day, shard, pieces), coverage)
            append_history_log(db, day, day_digests(db, day, shard))
        return {"day": day, "status": "built", "pieces": len(pieces)}

    def backfill_history(self, gap_seconds: int) -> list[str]:
        """Build the typed rows of every archived day that has none, oldest first."""
        built = self.history_days()
        done = []
        for day in sorted(set(self.days()) - built):
            self.build_history(day, gap_seconds)
            done.append(day)
        return done

    def record_gap(self, day: str, reason: str, recorded_at: str) -> None:
        with self._lock, self._db() as db:
            if db.execute("SELECT 1 FROM archive_days WHERE day=?", (day,)).fetchone() is not None:
                raise ArchiveRefused(f"day {day} is archived; a gap record is only for a day without a rollup")
            if db.execute("SELECT 1 FROM archive_gaps WHERE day=?", (day,)).fetchone() is not None:
                raise ArchiveRefused(f"day {day} already has a gap record")
            db.execute("INSERT INTO archive_gaps VALUES (?,?,?)", (day, reason, recorded_at))
            head = self._append_log(db, day, "gap", None, None, None)
        self._write_head(head)

    def bounded(self, since: str) -> list[Document]:
        with self._db() as db:
            rows = db.execute("SELECT data FROM archive_bounded WHERE day>=? ORDER BY day, id", (since,)).fetchall()
        return [Document.model_validate_json(r["data"]) for r in rows]

    def bounded_days(self) -> list[str]:
        with self._db() as db:
            return [r["day"] for r in db.execute("SELECT DISTINCT day FROM archive_bounded ORDER BY day")]

    def prune_bounded(self, before: str) -> list[str]:
        """Delete live-window rows of days before ``before`` (the archive's day shards stay)."""
        with self._lock, self._db() as db:
            days = [r["day"] for r in db.execute("SELECT DISTINCT day FROM archive_bounded WHERE day<?", (before,))]
            db.execute("DELETE FROM archive_bounded WHERE day<?", (before,))
        return days

    def check_chain(self) -> list[str]:
        """Recompute the hash chain, the head and every closed day's shard (content-free)."""
        problems: list[str] = []
        prev = GENESIS
        log = self.log()
        for i, row in enumerate(log, start=1):
            body = {k: row[k] for k in ("seq", "day", "entry", "shard_id", "recipe", "rows")}
            if row["seq"] != i or row["prev"] != prev or chain_hash(prev, body) != row["hash"]:
                problems.append(f"archive log entry {i} does not chain (an archive entry was removed or edited)")
                break
            prev = row["hash"]
        head = json.loads(self.head_path.read_text()) if self.head_path.exists() else None
        last = {"seq": log[-1]["seq"], "hash": log[-1]["hash"]} if log else None
        if head != last:
            problems.append(f"archive head {head} does not match the last log entry {last} (log entries were removed)")
        days = self.days()
        gaps = self.gaps()
        for row in log:
            if row["entry"] in CLOSED_ENTRIES:
                day = days.get(row["day"])
                if day is None or day["shard_id"] != row["shard_id"]:
                    problems.append(f"archived day {row['day']} was deleted from the archive")
                elif self.shard_rows(row["shard_id"]) != row["rows"]:
                    problems.append(f"archived day {row['day']} lost rows ({self.shard_rows(row['shard_id'])} of {row['rows']})")
            elif row["entry"] == "gap" and row["day"] not in gaps:
                problems.append(f"gap record of {row['day']} was deleted")
        logged = {r["day"] for r in log if r["entry"] in CLOSED_ENTRIES}
        unlogged = sorted(set(days) - logged)
        if unlogged:
            problems.append(f"archived days {unlogged} are not in the archive log")
        with self._db() as db:
            problems.extend(check_history(db, {d: row["shard_id"] for d, row in days.items()}))
        return problems


def open_archive(config: Any) -> EpisodeArchiveStore:
    from ..core.config_log_policy import archive_path

    logs = config.logs
    if logs is None or not logs.collection.archive.enabled:
        raise ConfigError("the episode archive is not enabled (logs.collection.archive.enabled)", "logs.collection.archive")
    return EpisodeArchiveStore(archive_path(logs.collection, logs))


def day_bounds(day: str) -> tuple[datetime, datetime]:
    start = datetime.combine(date.fromisoformat(day), time(0), tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def trim(doc: Document, limit: int | None) -> Document:
    """Text fields cut to ``limit`` characters; aggregate metadata and citations untouched."""
    if limit is None:
        return doc
    fields, cut = {}, False
    for name, value in doc.fields.items():
        if name not in UNTRIMMED_FIELDS and len(value) > limit:
            fields[name], cut = value[:limit] + TRIM_MARK, True
        else:
            fields[name] = value
    if not cut:
        return doc
    return doc.model_copy(update={"fields": fields, "metadata": {**doc.metadata, "archive_text_trimmed": True}})


class EpisodeArchive:
    """Close days into the archive, import staged snapshots, publish the serving generation."""

    def __init__(self, config: Any, *, store: EpisodeArchiveStore | None = None, now: Callable[[], datetime] | None = None, recipe: Any = None):
        if config.logs is None:
            raise ConfigError("no logs: section is configured", "logs")
        self.config, self.logs = config, config.logs
        self.policy = config.logs.collection
        self.store = store or open_archive(config)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._recipe = recipe

    def recipe(self) -> Any:
        if self._recipe is None:
            from .log_policy import load_keyword_recipe

            self._recipe = load_keyword_recipe(self.config)
        return self._recipe

    # --- close one day -------------------------------------------------------------------------

    def _check_epoch(self, day: str, recipe: str) -> None:
        start = self.policy.keywords.effective_from
        if start is not None and day < start.isoformat():
            raise ArchiveRefused(f"day {day} precedes keywords.effective_from {start}: it belongs to an earlier recipe epoch")
        floor = start.isoformat() if start is not None else ""
        mixed = sorted(d for d, row in self.store.days().items() if d >= floor and row["recipe"] != recipe)
        if mixed:
            raise ArchiveRefused(
                f"keyword recipe {recipe[:12]} differs from archived days {mixed[:3]} of the current epoch; "
                "pin the new files with keywords.effective_from after the last archived day (an explicit new epoch), never mixed"
            )

    @property
    def gap_seconds(self) -> int:
        return self.policy.keywords.gap_seconds

    def _field_map(self) -> Any:
        return self.config.sources[self.logs.source].mapping if self.logs.source in self.config.sources else None

    def close_day(self, day: str, snapshots: Iterable[dict[str, Any]], *, closure: str = "complete", expected: Sequence[str] | None = None) -> dict[str, Any]:
        """Archive one UTC day (see the module docstring). Idempotent for identical content.

        ``snapshots`` is consumed once, one snapshot at a time (bounded memory). ``closure``:
        ``complete`` needs every snapshot's window to be exactly the day (and every ``expected``
        scope present); ``partial`` (the overdue close) accepts windows that start at the day's
        midnight and end within it, and lists the missing and stale scopes."""
        from .day_spill import DaySpill
        from .log_policy import day_complete

        date.fromisoformat(day)
        if closure not in {"complete", "partial"}:
            raise ValueError("closure is complete or partial")
        now = self.now()
        if not day_complete(self.policy, date.fromisoformat(day), now):
            raise ArchiveRefused(f"day {day} is not complete at {now.isoformat()} (schedule.day_close_delay_minutes)")
        if day in self.store.gaps():
            raise ArchiveRefused(f"day {day} has a gap record")
        recipe = self.recipe()
        start, end = day_bounds(day)
        mapping = self._field_map()
        full_recipe = compile_summary(recipe.phrases, recipe.rules, start.isoformat(), end.isoformat(), field_map=mapping)["recipe"]
        seen: list[str] = []
        stale: list[str] = []

        def check(snap: dict[str, Any]) -> None:
            summary = snap["summary"]
            if summary.get("contract") != CONTRACT:
                raise ArchiveRefused("snapshot is not an incident_episode_summaries_v1 summary")
            a, b = timestamp(summary["start"]), timestamp(summary["end"])
            if closure == "complete" and (a != start or b != end):
                raise ArchiveRefused(f"snapshot window {summary['start']}..{summary['end']} is not exactly the UTC day {day}; a rollup pass never crosses a day boundary")
            if closure == "partial" and (a != start or not start < b <= end):
                raise ArchiveRefused(f"snapshot window {summary['start']}..{summary['end']} does not start at {day} 00:00Z and end within it; a rollup pass never crosses a day boundary")
            wanted = full_recipe if b == end else compile_summary(recipe.phrases, recipe.rules, summary["start"], summary["end"], field_map=mapping)["recipe"]
            if summary.get("recipe") != wanted:
                raise ArchiveRefused(f"snapshot of {snap['scope'].get('index')} was captured with another keyword recipe than the pinned policy; recapture it")
            seen.append(str(snap["scope"].get("index")))
            if b != end:
                stale.append(str(snap["scope"].get("index")))

        limit = self.policy.archive.max_text_chars
        spill_path = self.store.path.with_name(f"{self.store.path.name}.close-{day}.spill")
        with DaySpill(spill_path) as spill:
            try:
                spill.build(self.config.dataset, recipe.rules, snapshots, citations=True, check=check)
            except ValueError as exc:
                if isinstance(exc, ArchiveRefused):
                    raise
                raise ArchiveRefused(f"day {day}: {exc}") from exc
            if spill.scopes == 0:
                raise ArchiveRefused(f"day {day} has no snapshots; record an explicit gap instead (logs policy record-gap)")
            self._check_epoch(day, recipe.identity)
            missing = sorted(set(expected or []) - set(seen))
            if closure == "complete" and missing:
                raise ArchiveRefused(f"day {day} lacks expected scopes {missing[:5]} ({len(missing)}); it closes partial only after grace_days")
            unknown = spill.kinds() - ARCHIVE_KINDS - BOUNDED_KINDS - CATALOG_KINDS
            if unknown:
                raise ArchiveRefused(f"summary documents of unknown kinds {sorted(unknown)}")
            outside = sorted(spill.dates(ARCHIVE_KINDS) - {day})
            if outside:
                raise ArchiveRefused(f"documents of day {day} carry other dates {outside}")
            catalog = {str(d.metadata["signature_id"]): d.title or str(d.metadata["signature_id"]) for d in spill.documents(CATALOG_KINDS)}
            shard = spill.content_id(day, ARCHIVE_KINDS, lambda d: trim(d, limit))
            archived = self.store.days().get(day)
            if archived is not None:
                if archived["shard_id"] == shard:
                    return {"day": day, "status": "archived_already", "shard": shard, "rows": archived["rows"], "closure": archived.get("closure", "complete")}
                note = " (a complete set for a partial day is refused; record it as a supersedes note only)" if archived.get("closure") == "partial" else ""
                raise ArchiveRefused(f"day {day} is archived with other content ({archived['shard_id']}); archived days are append-only{note}")
            expected_rows = spill.count(ARCHIVE_KINDS)
            written = self.store.append_stream(shard, day, (trim(d, limit) for d in spill.documents(ARCHIVE_KINDS)), self.config.metadata_schema)
            rows = self.store.shard_rows(shard)
            if rows != expected_rows:
                raise ArchiveRefused(f"day {day}: shard {shard} holds {rows} rows, expected {expected_rows}")
            self.store.backfill_history(self.gap_seconds)  # days archived before I1 get their typed rows first
            pieces = [piece_of(day, shard, d.metadata, d.id) for d in spill.documents(["incident"])]
            coverage = [coverage_of(day, shard, d.metadata, d.id) for d in spill.documents(["summary_coverage"])]
            link_continuations(day, pieces, self.store.boundary_tail(previous_day(day), self.gap_seconds), self.gap_seconds)
            partial = int(closure == "partial")
            for row in (*pieces, *coverage):
                row["partial"] = partial
            delta = self._closure_delta(day, pieces)
            bounded_count = spill.count() - spill.count(CATALOG_KINDS)
            self.store.record_day(
                day, shard, recipe.identity, spill.scopes, rows, now.isoformat(), catalog,
                (d for d in spill.documents() if d.kind not in CATALOG_KINDS),
                closure=closure, expected=len(expected) if expected is not None else None, missing=missing, stale=stale, delta=delta,
                history=(pieces, issue_rows(day, shard, pieces), coverage),
            )
        result = {"day": day, "status": "archived", "shard": shard, "rows": rows, "written": written, "scopes": len(seen), "bounded": bounded_count, "closure": closure}
        if closure == "partial":
            result.update(missing_scopes=missing, stale_scopes=sorted(stale))
        if delta is not None:
            result["closure_delta"] = delta
        return result

    def _closure_delta(self, day: str, pieces: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Last provisional vs closed counts of the day (``None`` without a provisional entry)."""
        from .provisional import open_provisional, provisional_enabled

        if not provisional_enabled(self.config):
            return None
        entry = open_provisional(self.config).current().get(day)
        if entry is None or not Path(entry["file"]).exists():
            return None
        import sqlite3

        db = sqlite3.connect(f"file:{entry['file']}?mode=ro", uri=True)
        try:
            occ, started = db.execute("SELECT coalesce(sum(occurrences),0), coalesce(sum(episodes_started),0) FROM issue_day").fetchone()
        finally:
            db.close()
        closed_occ = sum(p["occurrences"] for p in pieces)
        closed_started = sum(1 for p in pieces if not p["is_continuation"])
        return {"provisional_as_of": entry.get("as_of"), "provisional_occurrences": int(occ), "closed_occurrences": closed_occ, "provisional_episodes_started": int(started), "closed_episodes_started": closed_started}

    def record_gap(self, day: str, reason: str) -> dict[str, Any]:
        date.fromisoformat(day)
        if not reason.strip():
            raise ArchiveRefused("a gap record needs a reason")
        self.store.record_gap(day, reason.strip()[:500], self.now().isoformat())
        return {"day": day, "status": "gap_recorded"}

    # --- staging -------------------------------------------------------------------------------

    def staged_days(self) -> list[str]:
        root = self.policy.staging.path
        if root is None or not root.exists():
            return []
        out = []
        for path in sorted(root.iterdir()):
            try:
                date.fromisoformat(path.name)
            except ValueError:
                continue
            if path.is_dir():
                out.append(path.name)
        return out

    def _staged(self, day: str) -> list[dict[str, Any]]:
        """``(path, index, window end)`` of a staged day's snapshot files (read one at a time)."""
        from .provisional import staged_snapshots

        root = self.policy.staging.path
        folder = root / day if root is not None else None
        out = []
        if folder is None or not folder.is_dir():
            return out
        for path in staged_snapshots(folder):
            snap = json.loads(path.read_text())
            out.append({"path": path, "scope": str(snap["scope"]["index"]), "end": timestamp(snap["summary"]["end"]), "start": timestamp(snap["summary"]["start"])})
        return out

    def _expected(self, day: str, provisional: Any) -> list[str] | None:
        from .provisional import read_expected

        root = self.policy.staging.path
        if root is not None and (root / day).is_dir():
            found = read_expected(root / day)
            if found is not None:
                return found
        return provisional.expected(day) if provisional is not None else None

    def import_staging(self) -> dict[str, Any]:
        """Close the due days, oldest first and in order (a pending day stops the run): a day D is
        due in the heavy window of D + ``close_after_days``; it closes **complete** from its staged
        full-day snapshots when every staged snapshot covers the whole day (and every expected scope
        is staged), else, once D + ``close_after_days`` + ``grace_days`` has passed, **partial**
        from the best capture per scope (staged or provisional), or as a gap record when nothing
        was captured. Each closed day's shard is verified before its staging copy is deleted."""
        from .log_policy import day_complete, heavy_window_open
        from .provisional import open_provisional, provisional_enabled

        root = self.policy.staging.path
        if root is None:
            raise ConfigError("logs.collection.staging.path is not set", "logs.collection.staging.path")
        now = self.now()
        if not heavy_window_open(self.policy, now):
            return {"status": "outside_heavy_window", "now": now.isoformat(), "days": []}
        report: dict[str, Any] = {"status": "ok", "days": [], "pending": [], "deleted": [], "gaps": [], "refused": []}
        report["history_backfilled"] = self.store.backfill_history(self.gap_seconds)
        provisional = open_provisional(self.config) if provisional_enabled(self.config) else None
        schedule = self.policy.schedule
        candidates = sorted(set(self.staged_days()) | set(provisional.days() if provisional is not None else []))
        for i, day in enumerate(candidates):
            folder = root / day
            archived = self.store.days().get(day)
            if archived is not None:
                if folder.is_dir():
                    if archived.get("closure") == "partial":
                        report["refused"].append({"day": day, "reason": "the day is archived partial; a later set is refused (append-only), staging kept"})
                        continue
                    result = self.close_day(day, (json.loads(p["path"].read_text()) for p in self._staged(day)))
                    report["days"].append(result)
                    self._delete_staged(day, result, report)
                continue
            if day in self.store.gaps():
                continue
            d = date.fromisoformat(day)
            due = d + timedelta(days=schedule.close_after_days)
            if not day_complete(self.policy, d, now) or now.date() < due:
                report["pending"].extend(candidates[i:])
                break
            staged = self._staged(day)
            expected = self._expected(day, provisional)
            start, end = day_bounds(day)
            complete = bool(staged) and all(s["start"] == start and s["end"] == end for s in staged) and (expected is None or set(expected) <= {s["scope"] for s in staged})
            if complete:
                result = self.close_day(day, (json.loads(s["path"].read_text()) for s in staged), expected=expected)
            elif now.date() >= due + timedelta(days=schedule.grace_days):
                best: dict[str, tuple[datetime, int, Any]] = {}
                for s in staged:  # staged copies win a tie (rank 1)
                    if s["start"] == start and (s["scope"] not in best or (s["end"], 1) > best[s["scope"]][:2]):
                        best[s["scope"]] = (s["end"], 1, ("staged", s["path"]))
                if provisional is not None:
                    for row in provisional.latest(day):
                        end_ = timestamp(row["window_end"])
                        if row["scope"] not in best or (end_, 0) > best[row["scope"]][:2]:
                            best[row["scope"]] = (end_, 0, ("provisional", row["sha"]))
                if not best:
                    self.record_gap(day, f"no summary snapshot captured by the overdue close (D+{schedule.close_after_days}+{schedule.grace_days} days)")
                    report["gaps"].append(day)
                    continue
                chosen = [best[k][2] for k in sorted(best)]

                def load(items: list[tuple[str, Any]] = chosen, day: str = day) -> Iterator[dict[str, Any]]:
                    for origin, ref in items:
                        yield json.loads(ref.read_text()) if origin == "staged" else provisional.snapshot(day, ref)  # type: ignore[union-attr]

                result = self.close_day(day, load(), closure="partial", expected=expected)
            else:
                report["pending"].extend(candidates[i:])
                break
            report["days"].append(result)
            self._delete_staged(day, result, report)
        return report

    def _delete_staged(self, day: str, result: dict[str, Any], report: dict[str, Any]) -> None:
        folder = self.policy.staging.path / day  # type: ignore[operator]
        if self.store.shard_rows(result["shard"]) != result["rows"]:  # verified before the copy goes
            raise ArchiveRefused(f"day {day} could not be verified after import; staging kept")
        if self.policy.staging.delete_after_import and folder.is_dir():
            shutil.rmtree(folder)
            report["deleted"].append(day)

    # --- provisional (I1) ----------------------------------------------------------------------

    def _tail(self, provisional: Any, built: dict[str, dict[str, Any]]) -> Callable[[str], tuple[str, list[dict[str, Any]] | None]]:
        def tail(day: str) -> tuple[str, list[dict[str, Any]] | None]:
            archived = self.store.days().get(day)
            if archived is not None and day in self.store.history_days():
                return f"archive:{archived['shard_id']}", self.store.boundary_tail(day, self.gap_seconds)
            entry = built.get(day)
            if entry is not None and Path(entry["file"]).exists():
                import sqlite3

                db = sqlite3.connect(f"file:{entry['file']}?mode=ro", uri=True)
                try:
                    return f"provisional:{entry['digest']}", boundary_tail(db, day, self.gap_seconds)
                finally:
                    db.close()
            if day in self.store.gaps():
                return "gap", None
            return "unknown", None

        return tail

    def refresh_provisional(self) -> dict[str, Any]:
        """Store the staged snapshots of every unclosed day as per-scope versions, rebuild the
        changed days' projection files (oldest first, so each day sees its predecessor's boundary)
        and garbage-collect replaced files. Light: no generation is rebuilt."""
        from .provisional import check_recipe, open_provisional, read_expected, staged_snapshots

        provisional = open_provisional(self.config, now=self.now)
        recipe = self.recipe()
        report: dict[str, Any] = {"status": "ok", "put": 0, "days": {}}
        with provisional.lock():
            archived = self.store.days()
            root = self.policy.staging.path
            if root is not None and root.exists():
                for day in self.staged_days():
                    if day in archived or day in self.store.gaps():
                        continue
                    found = read_expected(root / day)
                    if found is not None:
                        provisional.set_expected(day, found)
                    for path in staged_snapshots(root / day):
                        snap = json.loads(path.read_text())
                        check_recipe(snap, recipe, self._field_map())
                        provisional.put_scope(day, snap)
                        report["put"] += 1
            built = provisional.current()
            tail = self._tail(provisional, built)
            for day in provisional.days():
                if day in archived:
                    continue  # closed: kept as it is until a published generation serves it
                before = built.get(day, {}).get("digest")
                entry = provisional.build(day, dataset=self.config.dataset, rules=recipe.rules, gap_seconds=self.gap_seconds, tail=tail)
                built[day] = entry
                report["days"][day] = {"as_of": entry["as_of"], "scopes": entry["scopes"], "rebuilt": entry["digest"] != before, "missing_scopes": entry["missing_scopes"]}
            report["gc"] = provisional.gc()
        return report

    # --- serving -------------------------------------------------------------------------------

    def live_cutoff(self, today: date) -> str:
        from .logs_compose import expiry_cutoff

        return expiry_cutoff(today, self.logs.retention.keep_days)

    def prune(self, today: date) -> dict[str, Any]:
        return {"bounded_pruned": self.store.prune_bounded(self.live_cutoff(today))}

    def generation_documents(self, today: date) -> list[Document]:
        from .rollups import _document

        docs = [
            _document(self.config.dataset, "signature", [rule_id], entry["title"], {"signature_id": rule_id, "pattern_id": rule_id, "collection_scope": CONTRACT}, {"template": entry["title"]})
            for rule_id, entry in self.store.catalog().items()
        ]
        docs.extend(self.store.bounded(self.live_cutoff(today)))
        return docs

    def manifest_section(self, today: date) -> dict[str, Any]:
        days = self.store.days()
        shards = {d: row["shard_id"] for d, row in days.items()}
        epochs: dict[str, dict[str, str]] = {}
        for d, row in days.items():
            epoch = epochs.setdefault(row["recipe"], {"first": d, "last": d})
            epoch["last"] = d
        head = json.loads(self.store.head_path.read_text()) if self.store.head_path.exists() else None
        return {
            "source": self.logs.source,
            "collection_scope": CONTRACT,
            "as_of": today.isoformat(),
            "composed_at": self.now().isoformat(),
            "partitions": [],
            "days": sorted(days),
            "date_basis": "event_time",
            "date_timezone": "UTC",
            "window": {"live_from": self.live_cutoff(today), "keep_days": self.logs.retention.keep_days},
            "history": {"store": STORE, "kinds": sorted(ARCHIVE_KINDS), "shards": shards, "tables": sorted(self.store.history_days()) == sorted(days)},
            "archive": {"gaps": self.store.gaps(), "recipes": epochs, "head": head},
            "ledger": {
                d: {"tier": "archive", "closure": row.get("closure") or "complete", "shard": row["shard_id"], "recipe": row["recipe"], "scopes_captured": row["scopes"], "scopes_expected": row.get("scopes_expected"), "missing_scopes": json.loads(row.get("missing") or "[]"), "stale_scopes": json.loads(row.get("stale") or "[]")}
                for d, row in days.items()
            },
            "close_after_days": self.policy.schedule.close_after_days,
        }

    def publish(self, write_path: Any, today: date | None = None) -> dict[str, Any]:
        """Prune the live-window rows beyond ``keep_days``, then build the serving generation over
        every archived day (unchanged archive and window: nothing built).

        The archive tier is served by the pointer (``serve``, O(1)) before the generation is
        touched; the generation holds the catalog and the live window only, so its build is
        bounded by ``keep_days`` and never grows with the number of archived days."""
        today = today or self.now().date()
        served = self.serve()
        pruned = self.prune(today)["bounded_pruned"]
        section = self.manifest_section(today)
        docs = self.generation_documents(today)
        identity = hashlib.sha256(json.dumps({"shards": section["history"]["shards"], "gaps": section["archive"]["gaps"], "docs": sorted(d.id + hashlib.sha256(d.model_dump_json().encode()).hexdigest() for d in docs)}, sort_keys=True).encode()).hexdigest()
        active = write_path.registry.current()
        if active is not None and (active.get("logs") or {}).get("compose_identity") == identity:
            return {"status": "unchanged", "generation": active["generation"], "days": len(section["days"]), "bounded_pruned": pruned, "provisional_dropped": served["provisional_dropped"]}
        if not docs:
            raise ArchiveRefused("nothing to publish: the archive has no closed day and no rule catalog")
        section["compose_identity"] = identity
        manifest = write_path.ingest(docs, replace=True, rebuild=True, manifest_extra={"logs": section})
        return {"status": "published", "generation": manifest["generation"], "days": len(section["days"]), "documents": len(docs), "bounded_pruned": pruned, "provisional_dropped": served["provisional_dropped"]}

    def serve(self) -> dict[str, Any]:
        """Serve every closed day from the archive (pointer advance, O(1)) and drop those days from
        the provisional store. Adding day N+1 costs one day's close plus this, never a rebuild."""
        pointer = self.store.serve(self.now().isoformat())
        dropped: list[str] = []
        from .provisional import open_provisional, provisional_enabled

        if provisional_enabled(self.config):
            provisional = open_provisional(self.config)
            with provisional.lock():
                dropped = provisional.drop(sorted(self.store.served_view(pointer)["shards"]))  # type: ignore[index]
        return {"pointer": pointer, "provisional_dropped": dropped}
