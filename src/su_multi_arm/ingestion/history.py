"""History tier H1 (addendum A2 rev 4 §4.8): append-only day shards of the derived day documents.

**Shards.** A served day's day documents (``DAY_KINDS``: the A1 day rollups and the analytics day
kinds) form one **day shard**. Its id is content-addressed, ``<day>:<sha>`` over the documents' ids
and content; the store also remembers which ``source_key`` (snapshot identity + document recipe)
built which shard, so an unchanged day is referenced without reading its documents, a reparsed day
whose documents did not change maps onto its existing shard, and appending a new day writes rows
for that day only. Everything else of a log generation (signatures, examples,
window rollups, the signature catalog, incidents, graph links, lineage) is the **window shard**: the
generation the write path builds, small and bounded by the window and the number of signatures.
A generation's manifest is therefore a list of day-shard refs (``logs.history.shards``) plus its own
window build.

**Store.** ``SqliteHistoryStore`` keeps the shards append-only in one sqlite file
(``<partitions>/history/<source>.sqlite``): ``shards`` (id, day, row count, a content-free summary
for the incremental catalog/links/history), ``rows`` (shard, id, kind, day, the document JSON) and
``fields`` (the typed values of each row, the same EAV layout as the sqlite records adapter). A
shard is written in one transaction or not at all; an existing shard is never rewritten.

**Reads.** ``execute`` evaluates a typed ``ScopeSpec`` over the shards a generation references,
with a backend-native shard filter (``shard_id IN (...)``) and the kind and day pushed down to
indexed columns, then the shared ``scope_result`` semantics: results equal the records adapter's
for the same rows (contract tests compare them).

**gc.** ``gc(referenced)`` deletes the shards no remaining generation references (by reference);
the ``derived`` retention bound decides which days a compose references at all.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from ..backends._scope import document_values, scope_result, unsupported_reason
from ..backends.sqlite._db import field_row, predicate_sql
from ..core.types import Document, MetadataSchema, Predicate, Provenance, ScopeResult, ScopeSpec, StructuredRecord, UnsupportedConstraint

DAY_KINDS = frozenset(
    {"unit_day", "signature_day", "service_day", "signature_service_day", "signature_hour", "log_day", "log_hour", "endpoint_day", "exception_day", "coverage_day"}
)
SCHEMA = """
CREATE TABLE IF NOT EXISTS shards(shard_id TEXT PRIMARY KEY, day TEXT NOT NULL, rows INTEGER NOT NULL, summary TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS rows(shard_id TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL, day TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(shard_id, id));
CREATE INDEX IF NOT EXISTS rows_kind_day ON rows(kind, day, shard_id);
CREATE TABLE IF NOT EXISTS shard_keys(source_key TEXT PRIMARY KEY, shard_id TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS rows_id ON rows(id);
CREATE TABLE IF NOT EXISTS history_fields(shard_id TEXT NOT NULL, record_id TEXT NOT NULL, field TEXT, kind TEXT, text_value TEXT, fold_value TEXT, num_value REAL, is_null INTEGER);
CREATE INDEX IF NOT EXISTS history_fields_rec ON history_fields(record_id, field);
CREATE INDEX IF NOT EXISTS history_fields_value ON history_fields(field, text_value);
"""
MAX_MATCHED = 200_000  # rows one typed scope may read from the history tier (refused above, never cut)


def source_key(day: str, snapshot_identity: str, recipe: dict[str, Any]) -> str:
    """What a day shard is built from: the day's derived snapshot identity and the document recipe."""
    digest = hashlib.sha256(json.dumps({"identity": snapshot_identity, "recipe": recipe}, sort_keys=True).encode()).hexdigest()[:24]
    return f"{day}:{digest}"


def content_shard_id(day: str, documents: Sequence[Document]) -> str:
    """Content address of a day shard: its documents' ids and content. A reparse (new snapshot
    identity) that leaves the day's documents unchanged yields the same shard: nothing is written."""
    digest = hashlib.sha256()
    for doc in sorted(documents, key=lambda d: d.id):
        digest.update(doc.id.encode())
        digest.update(hashlib.sha256(doc.model_dump_json().encode()).digest())
    return f"{day}:{digest.hexdigest()[:24]}"


def summarise(documents: Iterable[Document]) -> dict[str, Any]:
    """The content-free per-shard summary the window shard is built from incrementally (no row
    reads for old days): signature lines/tenants/services/category, header links with lines."""
    signatures: dict[str, dict[str, Any]] = {}
    links: dict[str, int] = defaultdict(int)
    for doc in documents:
        m = doc.metadata
        lines = m.get("lines") if isinstance(m.get("lines"), int) else m.get("lines_total")
        lines = int(lines) if isinstance(lines, int) else 0
        if doc.kind == "signature_day":
            entry = signatures.setdefault(str(m.get("signature_id")), {"lines": 0, "tenants": [], "services": [], "category": m.get("category"), "exact": True})
            entry["lines"] += lines
            entry["tenants"] = sorted(set(entry["tenants"]) | {str(m.get("tenant"))})
            entry["services"] = sorted(set(entry["services"]) | {str(m.get("service"))})
            entry["exact"] = entry["exact"] and m.get("row_exactness") == "exact"
            links[json.dumps(["emits", "service", m.get("service"), "signature_id", m.get("signature_id")])] += lines
        elif doc.kind == "unit_day":
            links[json.dumps(["runs", "tenant", m.get("tenant"), "service", m.get("service")])] += lines
        elif doc.kind == "endpoint_day":
            links[json.dumps(["serves", "service", m.get("service"), "endpoint", m.get("endpoint")])] += lines
        elif doc.kind == "exception_day":
            links[json.dumps(["raises", "signature_id", m.get("signature_id"), "exception_class", m.get("exception_class")])] += lines
    return {"signatures": signatures, "links": dict(sorted(links.items()))}


class SqliteHistoryStore:
    kind = "sqlite_history"

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.writes = 0  # rows written by this instance (acceptance: appending a day writes that day only)
        with self._db() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=60)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    # --- write -------------------------------------------------------------------------------------

    def has_shard(self, shard: str) -> bool:
        with self._db() as db:
            return db.execute("SELECT 1 FROM shards WHERE shard_id=?", (shard,)).fetchone() is not None

    def lookup(self, key: str) -> str | None:
        """The shard a ``source_key`` was built into (an unchanged day: no rows read or written)."""
        with self._db() as db:
            row = db.execute("SELECT k.shard_id FROM shard_keys k JOIN shards s ON s.shard_id=k.shard_id WHERE k.source_key=?", (key,)).fetchone()
        return row["shard_id"] if row else None

    def remember(self, key: str, shard: str) -> None:
        with self._lock, self._db() as db:
            db.execute("INSERT OR REPLACE INTO shard_keys VALUES (?,?)", (key, shard))

    def summary(self, shard: str) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT summary FROM shards WHERE shard_id=?", (shard,)).fetchone()
        return json.loads(row["summary"]) if row else {}

    def append(self, shard: str, day: str, documents: Sequence[Document], schema: MetadataSchema | None = None) -> int:
        """Write one day shard (all rows in one transaction); an existing shard is left untouched.
        Returns the rows written (0 when the shard existed)."""
        with self._lock, self._db() as db:
            if db.execute("SELECT 1 FROM shards WHERE shard_id=?", (shard,)).fetchone() is not None:
                return 0
            for doc in documents:
                if doc.kind not in DAY_KINDS:
                    raise ValueError(f"document {doc.id} of kind {doc.kind!r} is not a day kind")
                db.execute("INSERT INTO rows VALUES (?,?,?,?,?)", (shard, doc.id, doc.kind, day, doc.model_dump_json()))
                db.executemany(
                    "INSERT INTO history_fields VALUES (?,?,?,?,?,?,?,?)",
                    [(shard, *field_row(doc.id, key, value)) for key, value in document_values(doc).items()],
                )
            db.execute("INSERT INTO shards VALUES (?,?,?,?,strftime('%s','now'))", (shard, day, len(documents), json.dumps(summarise(documents), sort_keys=True)))
        self.writes += len(documents)
        return len(documents)

    def shards(self) -> list[dict[str, Any]]:
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT shard_id, day, rows FROM shards ORDER BY day, shard_id")]

    def gc(self, referenced: set[str]) -> list[str]:
        """Delete every shard no remaining generation references."""
        with self._lock, self._db() as db:
            dead = [r["shard_id"] for r in db.execute("SELECT shard_id FROM shards") if r["shard_id"] not in referenced]
            for shard in dead:
                db.execute("DELETE FROM shard_keys WHERE shard_id=?", (shard,))
                db.execute("DELETE FROM history_fields WHERE shard_id=?", (shard,))
                db.execute("DELETE FROM rows WHERE shard_id=?", (shard,))
                db.execute("DELETE FROM shards WHERE shard_id=?", (shard,))
        return dead

    def row_count(self, shards: Sequence[str] | None = None) -> int:
        with self._db() as db:
            if shards is None:
                return int(db.execute("SELECT count(*) FROM rows").fetchone()[0])
            return int(db.execute(f"SELECT count(*) FROM rows WHERE shard_id IN ({','.join('?' * len(shards))})", list(shards)).fetchone()[0])

    # --- read --------------------------------------------------------------------------------------

    @staticmethod
    def _pushdown(spec: ScopeSpec) -> tuple[str, list[Any]]:
        """Indexed column conditions implied by the scope (a superset; the EAV conditions stay)."""
        clauses, params = [], []
        for p in spec.predicates:
            if p.negate:
                continue
            if p.field == "kind" and p.op == "eq":
                clauses.append("r.kind=?")
                params.append(p.value)
            elif p.field == "kind" and p.op == "in":
                values = list(p.value or [])
                clauses.append(f"r.kind IN ({','.join('?' * len(values)) or 'NULL'})")
                params.extend(values)
            elif p.field in {"date", "ts_hour"} and p.op in {"range", "date_interval"}:
                if isinstance(p.lower, str) and len(p.lower) >= 10:
                    clauses.append("r.day>=?")
                    params.append(p.lower[:10])
                if isinstance(p.upper, str) and len(p.upper) >= 10:
                    exclusive = p.field == "date" and p.op == "date_interval" and len(p.upper) == 10
                    clauses.append("r.day<?" if exclusive else "r.day<=?")
                    params.append(p.upper[:10])
            elif p.field == "date" and p.op == "eq" and isinstance(p.value, str):
                clauses.append("r.day=?")
                params.append(p.value[:10])
        return " AND ".join(clauses) or "1", params

    def _where(self, schema: MetadataSchema, predicates: Sequence[Predicate]) -> tuple[str, list[Any]]:
        clauses, params = ["1"], []
        for predicate in predicates:
            spec = schema.field(predicate.field)
            sql, extra = predicate_sql("history_fields", "record_id", "r.id AND f.shard_id=r.shard_id", predicate, bool(spec and spec.casefold_match), spec)
            clauses.append(sql)
            params.extend(extra)
        return " AND ".join(clauses), params

    def execute(self, shards: Sequence[str], spec: ScopeSpec, acl: Sequence[Predicate], schema: MetadataSchema) -> ScopeResult:
        from ..core.errors import BackendError

        applied, unsupported = [], []
        for predicate in spec.predicates:
            reason = unsupported_reason(schema, predicate)
            if reason is not None:
                unsupported.append(UnsupportedConstraint(constraint_text=predicate.field, reason=reason))  # type: ignore[arg-type]
            else:
                applied.append(predicate)
        marks = ",".join("?" * len(shards)) or "NULL"
        with self._db() as db:
            acl_where, acl_params = self._where(schema, acl)
            if acl:
                population = int(db.execute(f"SELECT count(*) FROM rows r WHERE r.shard_id IN ({marks}) AND {acl_where}", [*shards, *acl_params]).fetchone()[0])
            else:  # the referenced shards' row counts (O(shards), not O(rows))
                population = int(db.execute(f"SELECT coalesce(sum(rows), 0) FROM shards WHERE shard_id IN ({marks})", list(shards)).fetchone()[0])
            push, push_params = self._pushdown(spec)
            where, params = self._where(schema, [*acl, *applied])
            rows = db.execute(
                f"SELECT r.data FROM rows r WHERE r.shard_id IN ({marks}) AND {push} AND {where} ORDER BY r.id LIMIT ?",
                [*shards, *push_params, *params, MAX_MATCHED + 1],
            ).fetchall()
        if len(rows) > MAX_MATCHED:
            raise BackendError(self.kind, "execute", f"scope matches more than {MAX_MATCHED} history rows; narrow the window")
        provenance = Provenance(extractor="history")
        matched = []
        for row in rows:
            doc = Document.model_validate_json(row["data"])
            matched.append(StructuredRecord(schema_id="document", id=doc.id, values=document_values(doc), provenance=provenance))
        return scope_result(spec, matched, population, schema, applied, unsupported)

    def documents(self, shards: Sequence[str], ids: Sequence[str]) -> list[Document]:
        if not ids or not shards:
            return []
        with self._db() as db:
            rows = db.execute(
                f"SELECT data FROM rows WHERE shard_id IN ({','.join('?' * len(shards))}) AND id IN ({','.join('?' * len(ids))})",
                [*shards, *ids],
            ).fetchall()
        return [Document.model_validate_json(r["data"]) for r in rows]

    def values(self, shards: Sequence[str], field: str, limit: int) -> list[str]:
        if not shards:
            return []
        with self._db() as db:
            rows = db.execute(
                f"SELECT DISTINCT text_value FROM history_fields WHERE field=? AND is_null=0 AND kind='string' AND shard_id IN ({','.join('?' * len(shards))}) LIMIT ?",
                [field, *shards, limit],
            ).fetchall()
        return sorted(str(r["text_value"]) for r in rows)

    def summaries(self, shards: Sequence[str]) -> dict[str, dict[str, Any]]:
        if not shards:
            return {}
        with self._db() as db:
            rows = db.execute(f"SELECT shard_id, day, summary FROM shards WHERE shard_id IN ({','.join('?' * len(shards))})", list(shards)).fetchall()
        return {r["shard_id"]: {"day": r["day"], **json.loads(r["summary"])} for r in rows}


SHARD_FIELD = "shard_id"


class RecordsHistoryStore(SqliteHistoryStore):
    """The history rows in a records backend (``logs.history.backend``, e.g. OpenSearch), the shard
    index (shards, summaries, source keys: content-free) in the sqlite sidecar. Rows go to one fixed
    history collection of the backend (its own generation id, never activated, never served
    directly), tagged ``shard_id``; every read adds ``shard_id in (...)`` as a native filter, and
    ``gc`` deletes a dead shard's rows with the backend's delete-by-query."""

    kind = "records_history"

    def __init__(self, path: Path, backend: Any, dataset: str):
        super().__init__(path)
        from ..core.types import GenerationId

        self.backend = backend
        self.gen = GenerationId(dataset=dataset, generation=hashlib.sha256(f"history:{dataset}".encode()).hexdigest()[:32], index_format=1, fingerprint="0" * 64)

    @staticmethod
    def _schema(schema: MetadataSchema) -> MetadataSchema:
        from ..core.types import FieldSpec

        if schema.field(SHARD_FIELD) is not None:
            return schema
        return schema.model_copy(update={"fields": [*schema.fields, FieldSpec(name=SHARD_FIELD, type="string", filterable=True, listable=False)]})

    def append(self, shard: str, day: str, documents: Sequence[Document], schema: MetadataSchema | None = None) -> int:
        with self._lock, self._db() as db:
            if db.execute("SELECT 1 FROM shards WHERE shard_id=?", (shard,)).fetchone() is not None:
                return 0
        provenance = Provenance(extractor="history")
        records = []
        for doc in documents:
            if doc.kind not in DAY_KINDS:
                raise ValueError(f"document {doc.id} of kind {doc.kind!r} is not a day kind")
            records.append(StructuredRecord(schema_id="document", id=f"{shard}|{doc.id}", values={**document_values(doc), SHARD_FIELD: shard}, provenance=provenance))
        self.backend.records.upsert(self.gen, records, self._schema(schema or MetadataSchema()))
        with self._lock, self._db() as db:  # the shard is listed only after its rows are written
            db.execute("INSERT INTO shards VALUES (?,?,?,?,strftime('%s','now'))", (shard, day, len(documents), json.dumps(summarise(documents), sort_keys=True)))
        self.writes += len(documents)
        return len(documents)

    def gc(self, referenced: set[str]) -> list[str]:
        with self._lock, self._db() as db:
            dead = [r["shard_id"] for r in db.execute("SELECT shard_id FROM shards") if r["shard_id"] not in referenced]
        if dead:
            name = self.backend.index(self.gen, "records")
            query = {"terms": {f"s.{SHARD_FIELD}": dead}}
            self.backend.call("history.gc", "POST", f"/{name}/_delete_by_query", params={"refresh": "true", "conflicts": "proceed"}, body={"query": query})
            with self._lock, self._db() as db:
                for shard in dead:
                    db.execute("DELETE FROM shard_keys WHERE shard_id=?", (shard,))
                    db.execute("DELETE FROM shards WHERE shard_id=?", (shard,))
        return dead

    def row_count(self, shards: Sequence[str] | None = None) -> int:
        with self._db() as db:
            if shards is None:
                return int(db.execute("SELECT coalesce(sum(rows), 0) FROM shards").fetchone()[0])
            return int(db.execute(f"SELECT coalesce(sum(rows), 0) FROM shards WHERE shard_id IN ({','.join('?' * len(shards))})", list(shards)).fetchone()[0])

    def execute(self, shards: Sequence[str], spec: ScopeSpec, acl: Sequence[Predicate], schema: MetadataSchema) -> ScopeResult:
        shard_filter = Predicate(field=SHARD_FIELD, op="in", value=list(shards))
        result = self.backend.records.execute(self.gen, spec, [*acl, shard_filter])
        # ids are ``<shard>|<document id>``: callers see document ids
        update: dict[str, Any] = {"matched_ids": sorted(i.split("|", 1)[1] for i in result.matched_ids)}
        if result.listed:
            update["listed"] = [r.model_copy(update={"id": r.id.split("|", 1)[1]}) for r in result.listed]
        return result.model_copy(update=update)

    def documents(self, shards: Sequence[str], ids: Sequence[str]) -> list[Document]:
        """Day documents rebuilt from their stored typed values (metadata; the rendered header is
        not kept in the backend collection)."""
        if not ids or not shards:
            return []

        keys = [f"{shard}|{i}" for shard in shards for i in ids]
        out = []
        for start in range(0, len(keys), 500):
            spec = ScopeSpec(predicates=[Predicate(field="id", op="in", value=keys[start : start + 500])], limit=500)
            result = self.backend.records.execute(self.gen, spec, [Predicate(field=SHARD_FIELD, op="in", value=list(shards))])
            for listed in result.listed:
                out.append(listed.id)
        records = self._records(out)
        return [Document(id=r.id.split("|", 1)[1], dataset=self.gen.dataset, source="logs", kind=str(r.values.get("kind")), metadata={k: v for k, v in r.values.items() if k not in {"id", "kind", "dataset", "source", SHARD_FIELD}}) for r in records]

    def _records(self, ids: list[str]) -> list[StructuredRecord]:
        if not ids:
            return []
        name = self.backend.index(self.gen, "records")
        reply = self.backend.call("history.get", "POST", f"/{name}/_mget", body={"ids": ids})
        return [StructuredRecord.model_validate(d["_source"]["data"]) for d in reply.get("docs", []) if d.get("found")]

    def values(self, shards: Sequence[str], field: str, limit: int) -> list[str]:
        return []  # listable values come from the window shard (the generation)


def history_path(config: Any) -> Path:
    logs = config.logs
    return Path(logs.partitions.path) / "history" / f"{logs.source}.sqlite"


def tier_enabled(config: Any) -> bool:
    logs = getattr(config, "logs", None)
    return bool(logs is not None and logs.history.tier)


def open_history(config: Any, backends: dict[str, Any]) -> SqliteHistoryStore:
    """The history store a configuration names: rows in ``logs.history.backend`` (a records
    backend) or, by default, in the sqlite file next to the shard index."""
    name = config.logs.history.backend
    if name is None:
        return SqliteHistoryStore(history_path(config))
    return RecordsHistoryStore(history_path(config), backends[name], config.dataset)
