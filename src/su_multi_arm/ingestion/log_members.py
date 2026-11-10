"""Archive members of the live log source (docs/plans/unified_merge.md §6.1, §6.7; lane LB).

``close_day`` turns a provisional day of the slice ledger into immutable members, written into the
member registry as sealed units (the same unit format as slices, ``ingestion/handoff.py``):

* the **archive day member** (``key: day:YYYY-MM-DD``, tier ``archive``): the day's live rows,
  versions kept, catalog kinds excluded;
* the **catalog member** of the source (``key: catalog``; N3, B9): rebuilt only at close with
  latest-wins by id from the previous catalog member, the day's catalog-provisional documents and
  the parked catalog changes. It is ``None`` when nothing changed, so a quiet close adds no catalog
  version. Both members enter **one** compose, so the catalog adds no activation (≤ 6/day).

The caller composes and activates the generation that includes the returned members (partition
core, lane LA), then calls ``LogSliceLedger.release_day`` so the day's slices retire (one tier per
day; archive wins). Archive members never depend on slices.

``build_month_member`` merges closed day members into one month member (``key: month:YYYY-MM``):
one row per id (highest version; an id continuing across midnight keeps its latest piece) with
unique-text dedupe — embed rows of every day point at the same text hashes, so a text carried by
many days is stored and embedded once in the text-hash vector store.

**Layout (M2a wiring).** ``registry`` is the collection's member registry root
(``ingestion/members.collection_root``); a unit is written to
``members/<member>/<key slug>/<partition id prefix>/`` with its ``member.json`` (the verified
``MemberRef``), so ``MemberRegistry.members()`` lists it next to the write-path members. Facet
locations are relative to that root; ``catalog`` names the unit's ``catalog.sqlite`` (backend
``log_unit``: served by ``backends/sqlite/members.py`` through the unit catalog, never opened as a
write-path store). ``register_closed`` registers the members' ids in the id directory — the day
member replaces the day's current slices, the catalog member replaces the previous catalog version.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ..core.types import Document
from ..core.types_members import MemberRef, canonical_sha256
from .handoff import seal_unit, verify_unit
from .log_slices import UNIT_CATALOG, LogSliceLedger, SliceRefused, UnitBuild, UnitReader, doc_sha, write_unit

MEMBER_FILE = "member.json"
UNIT_BACKEND = "log_unit"  # ``FacetLocation.backend`` of a log unit's catalog facet


def is_log_unit(ref: Any) -> bool:
    """Whether a ``MemberRef`` is a unit-format log member (written here, not by the write path)."""
    catalog = getattr(ref, "facets", {}).get("catalog")
    return catalog is not None and catalog.backend == UNIT_BACKEND


@dataclass(frozen=True)
class ClosedDay:
    day: MemberRef
    day_dir: Path
    catalog: MemberRef | None
    catalog_dir: Path | None


def _member_ref(ledger: LogSliceLedger, key: str, tier: str, documents: Sequence[Document], build: UnitBuild, as_of: datetime,
                location: str, catalog_kinds: Sequence[str] = ()) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for doc in documents:
        counts[doc.kind] = counts.get(doc.kind, 0) + 1
    partition = canonical_sha256({"ids": sorted((d.id, doc_sha(d)) for d in documents), "recipe": ledger.recipe.fingerprint()})
    lexical = build.lexical
    return {
        "member": ledger.member, "key": key, "partition_id": partition, "recipe": ledger.recipe.model_dump(mode="json"), "tier": tier,
        "as_of": as_of.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), "sources": [ledger.source], "counts": dict(sorted(counts.items())),
        "catalog_kinds": list(catalog_kinds),
        "stats": {"documents": build.documents, "chunks": build.chunks, "tokens": build.tokens, "avgdl": lexical.get("avgdl", 0.0),
                  "average_idf": lexical.get("average_idf", 0.0), "unique_embed_texts": len(build.embed_texts)},
        "facets": {"catalog": {"backend": UNIT_BACKEND, "location": f"{location}/{UNIT_CATALOG}"},
                   "records": {"backend": "jsonl", "location": f"{location}/documents.jsonl.gz"},
                   "lexical": {"backend": "bm25", "location": f"{location}/lexical"},
                   "vector": {"backend": "text_vectors", "location": f"{location}/embed.json"}},
        "keyword_epoch": ledger.keyword_epoch,
    }


def _write_member(ledger: LogSliceLedger, registry: Path, name: str, key: str, tier: str, documents: list[Document], versions: dict[str, int],
                  as_of: datetime, catalog_kinds: Sequence[str] = (), tombstones: dict[str, int] | None = None) -> tuple[MemberRef, Path]:
    slug = key.replace(":", "-")
    partition = canonical_sha256({"ids": sorted((d.id, doc_sha(d)) for d in documents), "recipe": ledger.recipe.fingerprint()})
    key_dir = registry / "members" / ledger.member / slug
    final = key_dir / partition[:16]
    if final.exists() or (key != "catalog" and key_dir.exists() and any(key_dir.iterdir())):
        raise SliceRefused(f"member unit {ledger.member}/{key} already exists; an archive member is immutable (a rebuild takes a new key or epoch)")
    location = final.relative_to(registry).as_posix()
    staging = registry / ".staging" / ledger.member / name
    if staging.exists():
        shutil.rmtree(staging)
    try:
        build = write_unit(staging, documents, versions, tombstones or {}, settings=ledger.bm25, chunking=ledger.chunking, embed_fields=ledger.embed_fields,
                           template=ledger.template, metadata_schema=ledger.metadata_schema)
        ref = _member_ref(ledger, key, tier, documents, build, as_of, location, catalog_kinds)
        seal = seal_unit(staging, "member", ref)
        verify_unit(staging, expected_seal=seal)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, final)
        member = member_ref_of(final)
        tmp = final.parent / f".{final.name}.{MEMBER_FILE}.tmp"
        tmp.write_text(member.model_dump_json(indent=2))
        os.replace(tmp, final / MEMBER_FILE)  # outside the seal: the registry's listing file
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if final.exists() and not (final / MEMBER_FILE).exists():
            shutil.rmtree(final, ignore_errors=True)
        raise
    return member, final


def verify_sealed_ref(folder: Path, ref: MemberRef) -> str | None:
    """Seal-level check of a registered log unit (``MemberRegistry.verify``): ``unit.json`` hashes to
    ``SEAL``, the seal is the registered one and the sealed file digests are the registered ones.
    Returns the failure, or ``None``."""
    from .handoff import HandoffRefused, read_sealed

    try:
        body = read_sealed(folder)
    except HandoffRefused as exc:
        return str(exc)
    seal = body.pop("seal")
    if canonical_sha256(body) != seal or ref.digests.get("SEAL") != seal:
        return "the unit's seal does not match the registered member"
    if {k: v for k, v in ref.digests.items() if k != "SEAL"} != body.get("files"):
        return "the unit's sealed files differ from the registered member"
    missing = [name for name in body["files"] if not (folder / name).is_file()]
    if missing:
        return f"sealed files are missing: {missing[:3]}"
    return None


def member_ref_of(folder: Path) -> MemberRef:
    """The ``MemberRef`` of a sealed member unit, with ``digests`` = its sealed file digests plus
    the seal. Verifies the unit first (a mismatch is refused: the member is ``unavailable``)."""
    body = verify_unit(folder)
    if body.get("kind") != "member":
        raise SliceRefused(f"{folder.name} is not a member unit")
    return MemberRef.model_validate({**body["ref"], "digests": {**body["files"], "SEAL": body["seal"]}})


def _documents_of(ledger: LogSliceLedger, rows: Sequence[tuple[str, str, int]]) -> tuple[list[Document], dict[str, int]]:
    """Documents of ``(doc_id, unit_id, version)`` rows, read from their units."""
    by_unit: dict[str, list[str]] = {}
    versions: dict[str, int] = {}
    for doc_id, unit, version in rows:
        by_unit.setdefault(unit, []).append(doc_id)
        versions[doc_id] = version
    documents: list[Document] = []
    for unit, ids in by_unit.items():
        reader = UnitReader(ledger.unit_dir(unit), ledger.bm25)
        try:
            documents.extend(reader.documents(ids))
        finally:
            reader.close()
    if len(documents) != len(versions):
        raise SliceRefused(f"close found {len(documents)} documents for {len(versions)} live rows; the ledger and its units disagree")
    return documents, versions


def close_day(ledger: LogSliceLedger, day: date, registry: str | Path, *, previous_catalog: Path | None = None, as_of: datetime | None = None) -> ClosedDay:
    """Build the archive day member (and, when it changed, the catalog member) of ``day``."""
    registry = Path(registry)
    as_of = as_of or datetime.now(timezone.utc)
    rows = ledger.live_rows(day)
    day_rows = [(d, u, v) for d, u, v, _k, provisional in rows if not provisional]
    catalog_rows = [(d, u, v) for d, u, v, _k, provisional in rows if provisional]
    documents, versions = _documents_of(ledger, day_rows)
    day_ref, day_dir = _write_member(ledger, registry, f"day-{day.isoformat()}", f"day:{day.isoformat()}", "archive", documents, versions, as_of,
                                     tombstones=ledger.archived_tombstones(day))
    # catalog member: previous catalog, then the day's provisional catalog documents, then parked changes (latest wins)
    merged: dict[str, Document] = {}
    merged_versions: dict[str, int] = {}
    previous_shas: dict[str, str] = {}
    if previous_catalog is not None:
        member_ref_of(previous_catalog)  # verified before it is read
        reader = UnitReader(previous_catalog, ledger.bm25)
        try:
            prior_versions = reader.version_of()
            for doc in reader.documents():
                merged[doc.id] = doc
                merged_versions[doc.id] = prior_versions[doc.id]
                previous_shas[doc.id] = doc_sha(doc)
        finally:
            reader.close()
    provisional_docs, provisional_versions = _documents_of(ledger, catalog_rows)
    for doc in provisional_docs:
        merged[doc.id] = doc
        merged_versions[doc.id] = provisional_versions[doc.id]
    for doc in ledger.catalog_pending():
        merged[doc.id] = doc
        merged_versions[doc.id] = max(merged_versions.get(doc.id, 0), 0)
    if {i: doc_sha(d) for i, d in merged.items()} == previous_shas:
        return ClosedDay(day_ref, day_dir, None, None)
    kinds = sorted({d.kind for d in merged.values()})
    catalog_ref, catalog_dir = _write_member(ledger, registry, f"catalog-{day.isoformat()}", "catalog", "catalog", list(merged.values()), merged_versions,
                                             as_of, catalog_kinds=kinds)
    return ClosedDay(day_ref, day_dir, catalog_ref, catalog_dir)


def catalog_shas(folder: Path | None, ledger: LogSliceLedger) -> dict[str, str]:
    """``{id: content sha}`` of a catalog member (the ledger's ``catalog`` callable)."""
    if folder is None:
        return {}
    reader = UnitReader(folder, ledger.bm25)
    try:
        return {doc.id: doc_sha(doc) for doc in reader.documents()}
    finally:
        reader.close()


def build_month_member(ledger: LogSliceLedger, day_dirs: Sequence[Path], month: str, registry: str | Path, *, as_of: datetime | None = None) -> tuple[MemberRef, Path]:
    """Merge the sealed day members of ``month`` (``YYYY-MM``) into one month member."""
    registry = Path(registry)
    chosen: dict[str, tuple[int, str, Document]] = {}
    tombs: dict[str, int] = {}
    for folder in day_dirs:
        ref = member_ref_of(folder)
        if not ref.key.startswith(f"day:{month}-"):
            raise SliceRefused(f"{folder.name} ({ref.key}) is not a day of {month}")
        reader = UnitReader(folder, ledger.bm25)
        try:
            versions = reader.version_of()
            for doc_id, version in reader.tombstones:
                tombs[doc_id] = max(version, tombs.get(doc_id, 0))
            for doc in reader.documents():
                rank = (versions[doc.id], ref.key)
                if doc.id not in chosen or rank > chosen[doc.id][:2]:
                    chosen[doc.id] = (versions[doc.id], ref.key, doc)
        finally:
            reader.close()
    # a tombstone newer than the month's row of an id hides it inside the month itself
    for doc_id, version in tombs.items():
        if doc_id in chosen and chosen[doc_id][0] < version:
            del chosen[doc_id]
    tombs = {d: v for d, v in tombs.items() if d not in chosen}
    documents = [doc for _v, _k, doc in chosen.values()]
    versions = {doc.id: v for v, _k, doc in chosen.values()}
    return _write_member(ledger, registry, f"month-{month}", f"month:{month}", "archive", documents, versions, as_of or datetime.now(timezone.utc),
                         tombstones=tombs)


def _entries(ref: MemberRef, folder: Path, ledger: LogSliceLedger) -> list[Any]:
    from ..core.types_members import IdDirectoryEntry

    reader = UnitReader(folder, ledger.bm25)
    try:
        return [IdDirectoryEntry(doc_id=i, kind=k, unit_id=ref.unit_id, unit_kind="member", day=ref.day(), version=max(1, v))
                for i, k, v in zip(reader.ids, reader.kinds, reader.versions)]
    finally:
        reader.close()


def register_closed(directory: Any, ledger: LogSliceLedger, closed: ClosedDay) -> None:
    """Register the members ``close_day`` built in the id directory (before they are composed):

    * the archive day member with ``replaces`` = the day's current slices (their rows become
      ``superseded_by`` the member; the slices stay readable for views that still pin them);
    * the catalog member, which replaces the previous catalog version: a unit id is registered once
      (``<member>/catalog``), so the previous version's rows are retired and purged first and the
      day's ``catalog_provisional`` slice rows are taken over by the new unit. Log reads never route
      through these rows (the log stratum serves by sealed versions, ``query_stats.LogStratum``), so
      the swap does not change what a pinned request reads.

    Any failure undoes what this call registered (fail closed: the members are not composable)."""
    day = closed.day.day()
    assert day is not None
    slices = [s for s in ledger.vector().get(day, ()) if directory.unit(s) is not None]
    directory.register(closed.day.unit_id, "member", _entries(closed.day, closed.day_dir, ledger), replaces=slices)
    if closed.catalog is None:
        return
    previous = directory.unit(closed.catalog.unit_id)
    kept = [e.model_copy(update={"superseded_by": None, "deleted_by": None}) for e in directory.entries(closed.catalog.unit_id)] if previous is not None else []
    purged = False
    try:
        if previous is not None:
            if previous["status"] == "live":
                directory.retire(closed.catalog.unit_id)
            directory.purge(closed.catalog.unit_id)
            purged = True
        directory.register(closed.catalog.unit_id, "member", _entries(closed.catalog, closed.catalog_dir, ledger))
    except BaseException:
        if purged and directory.unit(closed.catalog.unit_id) is None:
            directory.register(closed.catalog.unit_id, "member", kept)  # the previous version stays composable
        directory.retire(closed.day.unit_id)
        directory.purge(closed.day.unit_id)
        raise
