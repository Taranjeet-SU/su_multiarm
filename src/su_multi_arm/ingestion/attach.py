"""``ingestion.attach``: one-to-one record attributes from an external SQLite file (opt-in).

A knowledge store built outside the package (for example a support knowledge graph's relational
tables) often holds per-record attributes the documents lack: an outcome class, a request type, a
resolution summary. ``attach`` joins declared columns of one table or view onto the metadata of the
documents they describe, at ingest time, so they become ordinary declared metadata fields: they are
filterable, groupable and projectable (``projection.tables.*.columns``) under the same ACL and
request scope as every other field. Many-to-many facts stay in the graph (``mode: attach`` Kuzu).

Safety rules (fail closed):

- the file is opened read-only and immutable, after its sha256 is checked against ``sha256``;
- only the declared ``table`` and ``columns`` are read, with quoted identifiers (no SQL text from
  configuration); the table's key column must be unique (a duplicate key fails the build);
- every target field must be declared in ``metadata_schema``; a document that already carries the
  target key is a conflict (``on_conflict: fail``) unless ``on_conflict: keep_document``;
- the document id is ``document_id`` with ``{key}`` replaced by the key value; documents whose id
  has no row keep their metadata unchanged (counted as ``unmatched``); rows without a document are
  counted as ``orphan_rows``. ``NULL`` values are not attached.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Sequence

IDENT = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"


class AttachError(ValueError):
    """An attach source is missing, tampered with, ambiguous or conflicts with document metadata."""


def _quote(name: str) -> str:
    if not name or any(c not in IDENT for c in name):
        raise AttachError(f"not an identifier: {name!r}")
    return f'"{name}"'


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_source(source: Any) -> dict[str, dict[str, Any]]:
    """``document id -> {field: value}`` for one ``ingestion.attach`` entry."""
    path = Path(source.path)
    if not path.is_file():
        raise AttachError(f"ingestion.attach: {path} does not exist")
    if _sha256(path) != source.sha256:
        raise AttachError(f"ingestion.attach: {path} does not match its sha256")
    columns = list(source.columns)
    query = f"SELECT {_quote(source.key_column)}, {', '.join(_quote(c) for c in columns)} FROM {_quote(source.table)}"
    db = sqlite3.connect(f"file:{path.resolve()}?mode=ro&immutable=1", uri=True)
    try:
        rows = db.execute(query).fetchall()
    except sqlite3.Error as exc:
        raise AttachError(f"ingestion.attach: {source.table}: {exc}") from exc
    finally:
        db.close()
    out: dict[str, dict[str, Any]] = {}
    for key, *values in rows:
        if key is None:
            continue
        doc_id = source.document_id.replace("{key}", str(key))
        if doc_id in out:
            raise AttachError(f"ingestion.attach: {source.table}.{source.key_column} is not unique ({key!r})")
        out[doc_id] = {source.columns[c]: v for c, v in zip(columns, values) if v is not None}
    return out


def apply_attachments(documents: Sequence[Any], sources: Sequence[Any]) -> tuple[list[Any], list[dict[str, Any]]]:
    """Documents with the attached metadata and one report per source."""
    reports = []
    docs = list(documents)
    for source in sources:
        attached = read_source(source)
        matched = conflicts = 0
        updated = []
        for document in docs:
            values = attached.get(document.id)
            if not values:
                updated.append(document)
                continue
            clash = sorted(k for k in values if k in document.metadata)
            if clash and source.on_conflict == "fail":
                raise AttachError(f"ingestion.attach: document {document.id} already has metadata {clash}")
            conflicts += bool(clash)
            metadata = {**values, **document.metadata} if clash else {**document.metadata, **values}
            updated.append(document.model_copy(update={"metadata": metadata}))
            matched += 1
        docs = updated
        ids = {d.id for d in docs}
        reports.append(
            {
                "table": source.table,
                "sha256": source.sha256,
                "rows": len(attached),
                "matched": matched,
                "unmatched": len(docs) - matched,
                "orphan_rows": sum(1 for doc_id in attached if doc_id not in ids),
                "conflicts_kept_document": conflicts,
                "fields": sorted(set(source.columns.values())),
            }
        )
    return docs, reports
