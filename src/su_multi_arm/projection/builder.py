"""``ProjectionBuilder``: canonical records -> sealed per-generation projection files.

``add(records)`` takes the same ``StructuredRecord`` batches the write path stores in ``records``
(values = document metadata plus ``id``/``source``/``dataset``/``kind``); a record whose ``kind``
has no table is skipped. Each value is checked with ``MetadataSchema.validate_value``: a rejected
value is stored as ``NULL``, counted per column and listed in the row's internal ``_rejected``
column (so a query can tell an in-scope rejected value from a true NULL). Temporal values are
normalised (datetimes to UTC ISO with microseconds, dates to ISO dates).

``seal(gen)`` returns the files and the manifest: one ``all.sqlite`` with every row (served to
principals without ACL constraints) and, under a tenant access column, one ``t_<sha>.sqlite`` per
tenant value holding only that tenant's rows (rows without an access value go only to the
all-rows file). File names never contain a tenant value. The manifest records per-table rows,
per-column rejected counts, dangling child keys per relation, the tenant files, a sha256 per file
and a content fingerprint; ``manifest.json`` is written last and is the seal (no manifest: the
projection is unavailable). The build is deterministic (rows in id order). Not resumable (L4).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Sequence

from ..backends._scope import instant
from ..core.config_projection import KEY
from ..core.types import FieldSpec, GenerationId, MetadataSchema, StructuredRecord
from .spec import SQL_TYPES, ProjectionSpec

MANIFEST = "manifest.json"
ALL_ROWS = "all.sqlite"
FORMAT = 1


def tenant_file(value: str) -> str:
    """The file of one tenant value (a hash: no tenant value in a path)."""
    return f"t_{hashlib.sha256(value.encode()).hexdigest()[:32]}.sqlite"


def quote(name: str) -> str:
    """A catalog identifier, double-quoted (names are validated identifiers; never model text)."""
    if not name.replace("_", "").isalnum() or '"' in name:
        raise ValueError(f"not an identifier: {name!r}")
    return f'"{name}"'


def coerce(spec: FieldSpec, value: Any, schema: MetadataSchema) -> tuple[Any, bool]:
    """``(stored value, rejected)``."""
    if value is None:
        return None, False
    if schema.validate_value(spec, value) is not None:
        return None, True
    if spec.type == "boolean":
        return int(bool(value)), False
    if spec.type == "datetime":
        moment = instant(value)
        if moment is None:
            return None, True
        return moment.isoformat(timespec="microseconds"), False
    if spec.type == "number":
        return float(value), False
    return value, False


class ProjectionBuilder:
    def __init__(self, spec: ProjectionSpec, schema: MetadataSchema):
        self.spec = spec
        self.schema = schema
        self.rows: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in spec.tables}
        self.rejected: dict[str, dict[str, int]] = {name: {c: 0 for c in t.columns} for name, t in spec.tables.items()}
        self.skipped = 0

    def add(self, records: Sequence[StructuredRecord], documents: Sequence[Any] | None = None) -> None:
        """``documents`` (the records' source documents, by id) feed the free-text columns
        (``text_sources``: the title or a text field, copied verbatim); a table with text columns
        refuses a batch without them (never an empty text column that looks like no mention)."""
        texts = {d.id: d for d in documents or []}
        for record in records:
            table = self.spec.table_of_kind(str(record.values.get("kind")))
            if table is None:
                self.skipped += 1
                continue
            row: dict[str, Any] = {KEY: record.id}
            rejected: list[str] = []
            document = texts.get(record.id)
            if table.text_sources and document is None:
                raise ValueError(f"projection table {table.name!r} has text columns; record {record.id!r} came without its document")
            for column, field_spec in table.columns.items():
                source = table.text_sources.get(column)
                if source is not None:
                    text = document.title if source == "title" else document.fields.get(source.split(".", 1)[1])  # type: ignore[union-attr]
                    row[column] = text if isinstance(text, str) and text != "" else None
                    continue
                value, bad = coerce(field_spec, record.values.get(column), self.schema)
                row[column] = value
                if bad:
                    rejected.append(column)
                    self.rejected[table.name][column] += 1
            row["_rejected"] = json.dumps(sorted(rejected)) if rejected else None
            if record.id in self.rows[table.name]:
                raise ValueError(f"duplicate record id {record.id!r} in projection table {table.name!r}")
            self.rows[table.name][record.id] = row

    # --- seal ----------------------------------------------------------------------------------

    def _database(self, rows: dict[str, list[dict[str, Any]]]) -> bytes:
        db = sqlite3.connect(":memory:")
        try:
            for name, table in self.spec.tables.items():
                columns = [f"{quote(KEY)} TEXT PRIMARY KEY"] + [f"{quote(c)} {SQL_TYPES[s.type]}" for c, s in table.columns.items()] + [f"{quote('_rejected')} TEXT"]
                db.execute(f"CREATE TABLE {quote(name)} ({', '.join(columns)})")
                names = [KEY, *table.columns, "_rejected"]
                db.executemany(
                    f"INSERT INTO {quote(name)} ({', '.join(quote(n) for n in names)}) VALUES ({', '.join('?' for _ in names)})",
                    [[row[n] for n in names] for row in rows.get(name, [])],
                )
                indexed = {r.column for r in self.spec.relations.values() if r.child == name}
                if self.spec.access_column:
                    indexed.add(self.spec.access_column)
                for column in sorted(indexed):
                    db.execute(f"CREATE INDEX {quote('ix_' + name + '_' + column)} ON {quote(name)} ({quote(column)})")
            db.commit()
            return db.serialize()
        finally:
            db.close()

    def seal(self, gen: GenerationId) -> tuple[dict[str, bytes], dict[str, Any]]:
        ordered = {name: [rows[k] for k in sorted(rows)] for name, rows in self.rows.items()}
        files = {ALL_ROWS: self._database(ordered)}
        access = self.spec.access_column
        tenants: dict[str, int] = {}
        values: list[str] = []
        if access is not None:
            values = sorted({row[access] for rows in ordered.values() for row in rows if isinstance(row.get(access), str)})
            for value in values:
                subset = {name: [r for r in rows if r.get(access) == value] for name, rows in ordered.items()}
                name = tenant_file(value)
                files[name] = self._database(subset)
                tenants[name] = sum(len(r) for r in subset.values())
        def unresolved(subset: dict[str, list[dict[str, Any]]], relation: Any) -> int:
            parents = {row[KEY] for row in subset.get(relation.parent, [])}
            return sum(1 for row in subset.get(relation.child, []) if row.get(relation.column) not in parents)

        subsets = {ALL_ROWS: ordered}
        if access is not None:
            subsets.update({tenant_file(v): {n: [r for r in rows if r.get(access) == v] for n, rows in ordered.items()} for v in values})
        # child rows whose parent key is NULL or names no parent row of the same file (an inner
        # join leaves them out), per relation and per file
        dangling = {name: {file: unresolved(subset, relation) for file, subset in subsets.items()} for name, relation in self.spec.relations.items()}
        content = hashlib.sha256(json.dumps(ordered, sort_keys=True, default=str).encode()).hexdigest()
        manifest = {
            "format": FORMAT,
            "sealed": True,
            "generation": gen.generation,
            "spec_fingerprint": self.spec.fingerprint,
            "access_column": access,
            "tables": {name: {"rows": len(rows), "rejected": dict(self.rejected[name])} for name, rows in ordered.items()},
            "relations": {name: {"dangling": counts[ALL_ROWS], "dangling_by_file": counts} for name, counts in dangling.items()},
            "tenant_files": dict(sorted(tenants.items())),
            "unscoped_rows": sum(1 for rows in ordered.values() for row in rows if access is not None and not isinstance(row.get(access), str)),
            "skipped_records": self.skipped,
            "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
            "fingerprint": hashlib.sha256(f"{self.spec.fingerprint}:{content}".encode()).hexdigest(),
        }
        return files, manifest

    @staticmethod
    def write(facet: Any, gen: GenerationId, files: dict[str, bytes], manifest: dict[str, Any]) -> None:
        """Every file, then the manifest (the seal)."""
        for name, data in sorted(files.items()):
            facet.put_file(gen, name, data)
        facet.put_file(gen, MANIFEST, json.dumps(manifest, sort_keys=True).encode())
