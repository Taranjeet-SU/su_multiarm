"""One SQL surface over the members of a partitioned collection (unified merge plan §6.8, M4
completion; staging fix 2026-11-10).

Before, a projection over a collection with more than one member was refused, so adding the live
log member removed the support SQL surface. Now:

* **The projection member's tables stay exactly as today.** The configured typed projection is one
  member's (the adopted support store's own sealed files, ``PartitionedProjection``); its catalog,
  grounding (values are read from that member only: ``projection_scope``), guard and single-file
  execution are unchanged, so a statement over those tables returns what it returned without the
  log member.
* **The live log member contributes its own tables** — ``issue_day`` (one row per episode piece per
  day: ``incident`` records) and ``coverage_day`` (one row per captured source scope per day:
  ``summary_coverage`` records). Their rows are derived **per pinned unit** (an archive day/month
  member or a live slice; immutable) into a cache file under the member registry
  (``sql/<unit>.sqlite``, built once, atomic) and joined by TEMP views of the multi-source setup
  (``setup.py``): one part per pinned unit, pinned by its unit id, with the rows the view does not
  serve (superseded versions, tombstones: the log stratum's served-row owners) excluded by id
  (``not_in`` pin).
* **Identity**: one view per declared namespace (``customer_<source>_<field>``: the raw value as
  ``id`` and its registry ``customer_id``), materialised from the pinned registry
  (``crosssource.identity``; ``IdentityView``: matched aliases only) at query time, pinned by the
  registry version. Declared join paths: ``issue_day.tenant`` / ``coverage_day.tenant`` → the
  tenant namespace view, each projection table's ``<field>`` → its customer namespace view, and the
  two namespace views on ``customer_id``. A disabled registry (missing, digest mismatch) offers no
  identity views and no cross-source join paths (fail closed).
* **Joins** (``join_policy: cross_source``): a statement over the projection's own tables is guarded
  as before; a statement that reads a log or identity table may join only along declared paths
  (``join_not_declared``). The projection tables are then read through views over the attached
  projection file (role ``projection``: the sealed file of the pinned member generation is its pin).

Operator-only (the log member's visibility, §14): a principal with ACL constraints gets the
projection surface alone. Nothing here writes to an adopted store; the derived files are caches of
immutable units (rebuilt when missing, never edited in place).
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.config_projection import KEY
from ..core.types import Page, Predicate, Scope
from .catalog import SqlCatalog, SqlColumn, SqlJoin, SqlTable, _display, sealed
from .setup import SqlSurfaceSetup

FORMAT = 2  # 2: coverage rows carry ``success_recorded`` (staging fix N3: the member history file)
PAGE = 1000
LOG_SOURCE_LABEL = "logs"
IDENTITY_SOURCE_LABEL = "identity"

# (name, physical, sqlite type, field type, unit, description, flags)
_ISSUE = [
    ("tenant", "tenant", "TEXT", "string", None, "Log tenant (raw value of the identity namespace of log tenants).", "lg"),
    ("env", "env", "TEXT", "string", None, "Environment of the tenant deployment.", "lg"),
    ("service", "service", "TEXT", "string", None, "Service the episode was observed on.", "lg"),
    ("signature_id", "signature_id", "TEXT", "string", None, "Detection rule id of the episode.", "lg"),
    ("category", "category", "TEXT", "string", None, "Rule category (``success`` marks heartbeats, not failures).", "lg"),
    ("issue", "issue", "TEXT", "string", None, "Rule title of the episode (a classified signal, not a cause).", "g"),
    ("date", "day", "TEXT", "date", None, "UTC day of the episode piece (an episode continued across midnight has a piece per day).", "gs"),
    ("first_hour", "first_failure", "TEXT", "datetime", None, "First matched message of the piece (UTC).", "s"),
    ("last_hour", "last_failure", "TEXT", "datetime", None, "Last matched message of the piece (UTC).", "s"),
    ("occurrences", "occurrences", "INTEGER", "integer", "messages", "Rule-matched source messages of the piece.", "s"),
    ("evidence_count", "evidence_count", "INTEGER", "integer", "examples", "Retained log examples of the piece.", "s"),
    ("observed_window_seconds", "observed_seconds", "REAL", "number", "seconds", "Last minus first matched message (not an outage duration).", "s"),
]
_COVERAGE = [
    ("tenant", "tenant", "TEXT", "string", None, "Log tenant of the captured source scope.", "lg"),
    ("env", "env", "TEXT", "string", None, "Environment of the captured scope.", "lg"),
    ("service", "service", "TEXT", "string", None, "Service of the captured scope.", "lg"),
    ("date", "day", "TEXT", "date", None, "UTC day of the capture.", "gs"),
    ("window_start", "window_start", "TEXT", "datetime", None, "Start of the captured window.", "s"),
    ("window_end", "window_end", "TEXT", "datetime", None, "End of the captured window (a live day is captured up to here).", "s"),
    ("observed_at", "observed_at", "TEXT", "datetime", None, "When the capture was taken.", "s"),
    ("source_documents", "source_documents", "INTEGER", "integer", "messages", "Source messages in the captured scope.", "s"),
    ("keyword_documents", "keyword_documents", "INTEGER", "integer", "messages", "Source messages matching a keyword.", "s"),
    ("rule_documents", "rule_documents", "INTEGER", "integer", "messages", "Source messages matching a detection rule.", "s"),
]
# columns of the derived files that the SQL surface does not expose (read by the member history file,
# ``runtime/member_logs.history_file``): 1 when the coverage record carries ``success_documents``
# (captured with success heartbeat rules)
HIDDEN = {"coverage_day": [("success_recorded", "INTEGER")]}
LOG_TABLES = {
    "issue_day": ("incident", "Log issue episodes per day from the live log member (archive days and live slices).", _ISSUE),
    "coverage_day": ("summary_coverage", "Captured log source scopes per day from the live log member (a missing scope is not zero issues).", _COVERAGE),
}
_META = {  # physical column -> metadata field of the record
    "first_failure": "first_hour", "last_failure": "last_hour", "observed_seconds": "observed_window_seconds", "day": "date",
}


def _cache_name(key: str) -> str:
    return f"v{FORMAT}_{hashlib.sha256(key.encode()).hexdigest()[:32]}.sqlite"


def _atomic(path: Path, build: Any) -> Path:
    """Write a derived file once (temp file + rename); a present file is reused (immutable unit)."""
    if path.is_file():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_", suffix=".sqlite")
    os.close(handle)
    try:
        db = sqlite3.connect(temp)
        try:
            build(db)
            db.commit()
        finally:
            db.close()
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return path


def unit_file(root: Path, unit: Any) -> Path:
    """The derived ``issue_day``/``coverage_day`` rows of one pinned log unit (cached by its
    immutable key: a member's partition id, a slice's id)."""
    from ..backends.sqlite.members import unit_key

    def build(db: sqlite3.Connection) -> None:
        for table, (kind, _desc, columns) in LOG_TABLES.items():
            physical = ["doc_id", "unit", *[c[1] for c in columns]]
            hidden = [f"{name} {sql_type}" for name, sql_type in HIDDEN.get(table, [])]
            db.execute(f'CREATE TABLE "{table}" ({", ".join([*(f"{name} {kind_of(name, columns)}" for name in physical), *hidden])})')
        cursor: str | None = None
        while True:
            page = unit.backend.catalog.iter_scope(unit.gen, Scope(predicates=[Predicate(field="kind", op="in", value=[k for k, _d, _c in LOG_TABLES.values()])]), Page(cursor=cursor, size=PAGE))
            for doc in page.items:
                for table, (kind, _desc, columns) in LOG_TABLES.items():
                    if doc.kind != kind:
                        continue
                    values = [doc.id, unit.unit_id, *[doc.metadata.get(_META.get(c[1], c[1])) for c in columns]]
                    values += [int("success_documents" in doc.metadata) for _name, _type in HIDDEN.get(table, [])]
                    db.execute(f'INSERT INTO "{table}" VALUES ({", ".join("?" for _ in values)})', values)
            cursor = page.next_cursor
            if cursor is None or not page.items:
                break

    return _atomic(root / "sql" / _cache_name(f"unit:{unit_key(unit.ref)}:{unit.unit_id}"), build)


MAX_VIEW_FILES = 16  # per-pin files kept (older ones are removed when a new pin is built)


def view_file(root: Path, backend: Any, gen: Any, member: str) -> tuple[Path, dict[str, int]]:
    """The served ``issue_day``/``coverage_day`` rows of the log member under one request pin, in
    one file (the pin id is the request generation: generation + slice vector, so the file is
    immutable for it). Built from the per-unit caches (``unit_file``) one unit at a time, rows the
    pin does not serve (superseded versions, tombstones: the log stratum's owners) left out; every
    row carries the pin in ``pin``. One file per pin keeps the surface's attachments bounded
    (SQLite attaches at most 10 databases) whatever the number of pinned units."""
    folder = root / "sql"
    path = folder / _cache_name(f"view:{gen.generation}:{member}")
    if not path.is_file():
        view = backend.view(gen)
        owners = backend.log_stratum(member).owners(view)
        units = sorted(backend.units(gen, refs=[r for r in backend.pinned_refs(gen) if r.member == member]), key=lambda u: u.unit_id)
        sources = [(unit, unit_file(root, unit)) for unit in units]

        def build(db: sqlite3.Connection) -> None:
            for table, (_kind, _desc, columns) in LOG_TABLES.items():
                physical = ["doc_id", "unit", *[c[1] for c in columns]]
                hidden = [f"{name} {sql_type}" for name, sql_type in HIDDEN.get(table, [])]
                db.execute(f'CREATE TABLE "{table}" ({", ".join([*(f"{name} {kind_of(name, columns)}" for name in physical), *hidden])}, pin TEXT)')
            db.execute("CREATE TEMP TABLE served (doc_id TEXT PRIMARY KEY)")
            db.commit()
            for unit, source in sources:
                db.execute("DELETE FROM served")
                db.executemany("INSERT OR IGNORE INTO served VALUES (?)", [(d,) for d, owner in owners.items() if owner == unit.unit_id])
                db.commit()  # ATTACH runs outside a transaction
                db.execute("ATTACH DATABASE ? AS u", (str(source),))  # read only (SELECT); the unit cache is never written
                try:
                    for table, (_kind, _desc, columns) in LOG_TABLES.items():
                        names = ", ".join(["doc_id", "unit", *[c[1] for c in columns], *[name for name, _type in HIDDEN.get(table, [])]])
                        db.execute(f'INSERT INTO main."{table}" SELECT {names}, ? FROM u."{table}" WHERE doc_id IN (SELECT doc_id FROM temp.served)', (gen.generation,))
                finally:
                    db.commit()
                    db.execute("DETACH DATABASE u")

        _atomic(path, build)
        prune(folder, keep=path, kind=_is_view)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        counts = {t: int(db.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]) for t in LOG_TABLES}
    return path, counts


def prune(folder: Path, keep: Path, kind: Any) -> None:
    """Remove the oldest per-pin files of one ``kind`` (a predicate on the path: the per-pin view
    files, the member history files) beyond ``MAX_VIEW_FILES`` (a request still reading one keeps
    its open handle; a request that had not opened it yet refuses, labelled)."""
    marker = f"v{FORMAT}_"
    files = sorted((f for f in folder.glob(f"{marker}*.sqlite") if f.name.startswith(marker) and f != keep and kind(f)), key=lambda f: f.stat().st_mtime)
    for old in files[: max(0, len(files) - (MAX_VIEW_FILES - 1))]:
        old.unlink(missing_ok=True)


def _is_view(path: Path) -> bool:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            return "pin" in {r[1] for r in db.execute('PRAGMA table_info("issue_day")')}
    except sqlite3.Error:
        return False


def kind_of(name: str, columns: list[tuple]) -> str:
    if name in {"doc_id", "unit"}:
        return "TEXT"
    return next(c[2] for c in columns if c[1] == name)


def identity_file(root: Path, view: Any) -> Path:
    """The pinned registry's matched aliases as one table (cached by the registry digest)."""

    def build(db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE alias (namespace TEXT, raw TEXT, customer_id TEXT, registry_version TEXT)")
        db.executemany("INSERT INTO alias VALUES (?,?,?,?)", [(ns, raw, cid, view.registry_version) for (ns, raw), cid in sorted(view.matched.items())])

    return _atomic(root / "sql" / _cache_name(f"identity:{view.sha256}:{view.registry_version}"), build)


def _namespace_view(namespace: str) -> str:
    return "customer_" + re.sub(r"[^a-z0-9_]", "_", namespace.replace(".", "_"))[:50]


@dataclass
class CollectionSurface:
    """The cross-source part of one request's SQL surface: the setup (projection file + log units +
    identity) and the extra catalog tables/join paths the projection catalog is extended with."""

    setup: SqlSurfaceSetup
    tables: dict[str, SqlTable]
    joins: list[SqlJoin]
    log_tables: tuple[str, ...]


def projection_scope(facet: Any, gen: Any) -> list[Predicate]:
    """The value scope of the projection member (``source in`` its sources): grounding and catalog
    samples of the projection's columns read that member's records only, as without other
    members. Empty for a single store."""
    sources = getattr(facet, "projection_sources", None)
    if not callable(sources):
        return []
    found = sources(gen)
    return [Predicate(field="source", op="in", value=list(found))] if found else []


def collection_surface(facet: Any, gen: Any, sql_cat: SqlCatalog, acl: Any, file: str | None) -> CollectionSurface | None:
    """The cross-source part for a request (module doc), or ``None``: not a partitioned facet, no
    live log member in the pin, a principal with ACL constraints, or no all-rows projection file."""
    backend = getattr(facet, "partitioned_backend", None)
    if backend is None or list(acl) or file is None:
        return None
    config = backend.config
    from ..ingestion.log_collection import live_members

    live = [m for m in live_members(config) if m in backend.live_members_of(gen)]
    if len(live) != 1:
        return None
    member = live[0]
    root = backend.registry.root
    projection = [a for a in facet.attachments(gen) if Path(a["path"]).name == file]
    if len(projection) != 1:
        return None
    attachments: list[dict[str, Any]] = [{"alias": "projection", "path": projection[0]["path"], "role": "projection"}]
    views: list[dict[str, Any]] = []
    for name, table in sql_cat.tables.items():
        columns = [{"name": c.name, "physical": c.source_column if c.source_column != c.name else None, "type": "id" if c.name == KEY else c.field_type,
                    "description": c.description or c.display_name, "nullable": c.name != KEY} for c in table.columns]
        views.append({"name": name, "source": f"projection:{name}", "kind": table.kind, "description": table.description or table.display_name, "columns": columns,
                      "parts": [{"attachment": "projection", "table": name, "pins": []}]})
    # the log member: the served rows of the pin, one file per request pin (see ``view_file``)
    view = backend.view(gen)
    path, _counts = view_file(root, backend, gen, member)
    attachments.append({"alias": "logs", "path": str(path.resolve()), "role": "slice"})
    parts: dict[str, list[dict[str, Any]]] = {t: [{"attachment": "logs", "table": t, "pins": [{"label": "pin", "column": "pin", "op": "eq", "value": gen.generation}]}] for t in LOG_TABLES}
    log_names = []
    for table, (kind, description, columns) in LOG_TABLES.items():
        log_names.append(table)
        flags = {c[0]: c[6] for c in columns}
        cols = [{"name": KEY, "physical": "doc_id", "type": "id", "description": "Document id of the log record.", "nullable": False}]
        cols += [{"name": c[0], "physical": c[1] if c[1] != c[0] else None, "type": c[3], "description": c[5], "unit": c[4],
                  "listable": "l" in flags[c[0]], "groupable": "g" in flags[c[0]], "sortable": "s" in flags[c[0]]} for c in columns]
        views.append({"name": table, "source": LOG_SOURCE_LABEL, "kind": kind, "description": description, "columns": cols, "parts": parts[table]})
    if not log_names:
        return None
    joins: list[dict[str, Any]] = []
    pins: dict[str, str | int] = {"member_set": ",".join(sorted({r.unit_id for r in view.members})), "slice": ",".join(sorted(s for d in view.slices.values() for ids in d.values() for s in ids))}
    identity = _identity_view(config)
    if identity is not None and identity.joins_enabled:
        attachments.append({"alias": "registry", "path": str(identity_file(root, identity).resolve()), "role": "registry"})
        planner = getattr(getattr(config, "crosssource", None), "planner", None)
        tenant_ns = planner.tenant_namespace if planner is not None else "logs.tenant"
        customer_ns = list(planner.customer_namespaces) if planner is not None else ["support.account"]
        pins["registry_version"] = str(identity.registry_version)
        for namespace in dict.fromkeys([tenant_ns, *customer_ns]):
            name = _namespace_view(namespace)
            views.append({
                "name": name, "source": IDENTITY_SOURCE_LABEL, "kind": "customer_alias",
                "description": f"Matched registry aliases of namespace {namespace}: the raw value (id) and its registry customer id.",
                "columns": [{"name": KEY, "physical": "raw", "type": "string", "description": f"Raw {namespace} value.", "nullable": False},
                            {"name": "customer_id", "type": "id", "description": "Registry customer id (matched aliases only)."}],
                "parts": [{"attachment": "registry", "table": "alias", "pins": [{"label": "registry_version", "column": "registry_version", "op": "eq", "value": str(identity.registry_version)},
                                                                                {"label": "namespace", "column": "namespace", "op": "eq", "value": namespace}]}],
            })
        tenant_view = _namespace_view(tenant_ns)
        for table in log_names:
            joins.append({"left": table, "left_column": "tenant", "right": tenant_view, "right_column": KEY, "description": "Log tenant resolved through the pinned identity registry (matched aliases only)."})
        for namespace in customer_ns:
            name = _namespace_view(namespace)
            if name == tenant_view:
                continue
            joins.append({"left": name, "left_column": "customer_id", "right": tenant_view, "right_column": "customer_id", "cardinality": "many_to_many",
                          "description": f"The same registry customer in {namespace} and {tenant_ns}."})
            field = namespace.split(".", 1)[1]
            for table_name, table in sql_cat.tables.items():
                if table.column(field) is not None:
                    joins.append({"left": table_name, "left_column": field, "right": name, "right_column": KEY, "description": f"{table_name}.{field} resolved through the pinned identity registry ({namespace})."})
    setup = SqlSurfaceSetup.model_validate({"generation": gen.generation, "attachments": attachments, "views": views, "joins": joins, "pins": pins})
    extra: dict[str, SqlTable] = {}
    for item in setup.views:
        if item.source.startswith("projection:"):
            continue
        extra[item.name] = SqlTable(
            name=item.name, kind=item.kind, display_name=_display(item.name), description=item.description, source=item.source,
            columns=[SqlColumn(source_column=c.name, name=c.name, display_name=_display(c.name), datatype="TEXT" if c.name == KEY else _sql_type(c.type), field_type=c.type,
                               source_field=c.name, description=c.description, groupable=c.groupable, sortable=c.sortable, nullable=c.nullable, unit=c.unit) for c in item.columns],
        )
    return CollectionSurface(setup=setup, tables=extra, joins=[SqlJoin(**j.model_dump()) for j in setup.joins], log_tables=tuple(log_names))


def _sql_type(kind: str) -> str:
    from ..projection.spec import SQL_TYPES

    return SQL_TYPES[kind]


_IDENTITY_VIEWS: dict[tuple, Any] = {}  # (path, pinned sha256, file stat) -> IdentityView


def _identity_view(config: Any) -> Any:
    """The pinned registry view, parsed once per file state (staging round 2: the export was parsed
    twice per request). Keyed by the configured digest and the file's stat, so an edited or replaced
    file is read (and digest-checked) again; a missing file is never cached."""
    identity = getattr(getattr(config, "crosssource", None), "identity", None)
    if identity is None:
        return None
    from ..crosssource.identity import IdentityView

    path = Path(identity.registry_path)
    try:
        stat = path.stat()
    except OSError:
        return IdentityView.from_config(identity, path)
    key = (str(path), identity.sha256, getattr(identity, "auto_match", "exact"), getattr(identity, "fuzzy_min_confidence", 0.8), stat.st_mtime_ns, stat.st_size, stat.st_ino)
    found = _IDENTITY_VIEWS.get(key)
    if found is None:
        found = IdentityView.from_config(identity, path)
        if len(_IDENTITY_VIEWS) >= 8:
            _IDENTITY_VIEWS.clear()
        _IDENTITY_VIEWS[key] = found
    return found


def extend(sql_cat: SqlCatalog, surface: CollectionSurface) -> SqlCatalog:
    """The projection catalog plus the surface's tables and join paths (``cross_source``)."""
    data = sql_cat.model_dump()
    data["tables"] = {**sql_cat.tables, **surface.tables}
    data["joins"] = [*sql_cat.joins, *surface.joins]
    data["join_policy"] = "cross_source"
    data["pins"] = {**sql_cat.pins, **surface.setup.pins}
    data["projection_fingerprint"] = hashlib.sha256((sql_cat.projection_fingerprint + surface.setup.fingerprint()).encode()).hexdigest()
    data["fingerprint"] = ""
    return sealed(SqlCatalog.model_validate(data))


def statement_reads(sql: str, surface: CollectionSurface | None) -> bool:
    """Whether ``sql`` names a table only the collection surface serves (decided before the guard,
    so the guard grounds those tables' values over the whole collection and the projection's over
    its member only). An unparseable statement: ``False`` (the guard refuses it)."""
    if surface is None:
        return False
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception:  # noqa: BLE001 - the guard reports the parse error
        return False
    return any(t.name in surface.tables for t in tree.find_all(exp.Table))


def reads_collection(guarded: Any, surface: CollectionSurface | None) -> bool:
    """Whether the guarded statement reads a table only the collection surface serves."""
    return surface is not None and any(t in surface.tables for t in guarded.tables)


def execute(surface: CollectionSurface, guarded: Any, timeout_ms: int, lineage: dict[str, Any] | None = None) -> tuple[list[str], list[tuple]]:
    """Run a guarded cross-source statement on a fresh surface of the setup (read-only, authorizer)."""
    from .executor import run_statement
    from .setup import open_surface

    conn = open_surface(surface.setup, timeout_ms)
    return run_statement(conn, guarded, timeout_ms, lineage)
