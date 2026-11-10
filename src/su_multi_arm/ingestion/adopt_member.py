"""Read-only adoption of an existing store as a collection member (docs/plans/unified_merge.md §6.3; M1b, LA).

``adopt(config, member, registry)`` makes a validated store — built and served today by a v2
configuration — the first member of a unified collection **without writing, re-stamping or
relocating it** (unlike ``index adopt``, ``ingestion/relocate.py``):

1. the original configuration file (``collection.members.<id>.adopt.original_config``) must match
   its pinned ``original_config_sha256``; it is loaded as an ordinary v2 configuration;
2. the store's **original** whole-config fingerprint is recomputed **backend-free** with
   ``recipes.fingerprint_of(original)`` (no ``WritePath``, no backend, no provider) and compared
   with the fingerprint in the store's own active manifest; the original configuration must name
   this store as its sqlite catalog backend and serve the collection's dataset;
3. every backing file of the active generation (``CURRENT`` and every file under
   ``snapshots/<generation>/``: catalog, records, bm25 arrays, vectors, faiss index, projection
   files, manifest) is hashed; the runtime file (``runtime.sqlite``) and the write lock belong to
   the service that serves the store and are neither read nor hashed;
4. document ids and kinds are read from the catalog over a ``mode=ro`` connection, checked against
   the member declaration (sources, record kinds, id namespaces) and registered in the id
   directory; the decomposed recipe (``recipes.store_recipe``), the digests and the adoption record
   are written **into the unified member registry** (a separate directory) as ``member.json`` and
   ``adoption.json``.

Nothing under the adopted store is opened for writing at any step; a failure leaves the registry
as it was (registered ids retired and purged, the unit folder removed). At serve time
``MemberRegistry.verify`` re-hashes the files: a mismatch makes the member ``unavailable`` (fail
closed for that member, §6.12), and the store is opened by a ``SqliteBackend(readonly=True)``
(``backends/sqlite/members.py``) with the store's own backend settings recorded here.

No host path is written into a manifest: the member's facet location is the symbolic
``adopted/<member>``, resolved through the configuration's ``adopt.store`` at serve time, so a
store moved with identical digests keeps serving and a changed one is refused.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.errors import ConfigError
from ..core.types_members import FacetLocation, IdDirectoryEntry, MemberRef, canonical_sha256
from .members import MEMBER_FILE, MemberRegistry, file_sha256, key_slug
from .recipes import fingerprint_of, store_recipe

ADOPTION_FILE = "adoption.json"
ADOPTED_LOCATION = "adopted"  # FacetLocation prefix: resolved through collection.members.<id>.adopt.store
# Files of a store root that belong to the serving process, never to the index: not hashed, not read.
RUNTIME_FILES = frozenset({"runtime.sqlite", "runtime.sqlite-journal", "runtime.sqlite-wal", "runtime.sqlite-shm", ".write.lock"})
HEX32 = re.compile(r"^[0-9a-f]{32}$")


class AdoptionError(ValueError):
    """The store cannot be adopted read-only (nothing was registered)."""


def read_only_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def store_generation(store: Path) -> tuple[str, dict[str, Any]]:
    """``(generation, manifest)`` of the store's active generation, read with plain file reads."""
    pointer = store / "CURRENT"
    if not pointer.is_file():
        raise AdoptionError(f"{store}: no CURRENT pointer (not a built store)")
    generation = pointer.read_text().strip()
    if not HEX32.match(generation):
        raise AdoptionError(f"{store}: CURRENT does not name a generation")
    path = store / "snapshots" / generation / "manifest.json"
    if not path.is_file():
        raise AdoptionError(f"{store}: CURRENT names generation {generation} but its manifest is missing")
    return generation, json.loads(path.read_text())


def store_files(store: Path, generation: str) -> list[str]:
    """Store-relative names of every backing file of ``generation`` (sorted)."""
    snapshot = store / "snapshots" / generation
    names = ["CURRENT"]
    for path in sorted(snapshot.rglob("*")):
        if path.is_file():
            names.append(path.relative_to(store).as_posix())
    return names


def store_digests(store: Path, generation: str) -> dict[str, str]:
    """sha256 of every backing file of ``generation`` keyed by its store-relative name."""
    return {name: file_sha256(store / name) for name in store_files(store, generation)}


def tree_digest(digests: dict[str, str]) -> str:
    return canonical_sha256(sorted(digests.items()))


def catalog_documents(store: Path, generation: str) -> list[tuple[str, str | None, str | None]]:
    """``(id, kind, source)`` of every catalog document, over a ``mode=ro`` connection."""
    path = store / "snapshots" / generation / "catalog.sqlite"
    if not path.is_file():
        raise AdoptionError(f"{store}: generation {generation} has no catalog")
    with closing(sqlite3.connect(read_only_uri(path), uri=True)) as db:
        db.execute("PRAGMA query_only=1")
        rows = db.execute("SELECT id, json_extract(data, '$.kind'), json_extract(data, '$.source') FROM documents ORDER BY id").fetchall()
    return [(str(i), k, s) for i, k, s in rows]


def backend_settings(spec: Any) -> dict[str, Any]:
    """The adopted store's own sqlite backend settings (path excluded): how it is opened at serve time."""
    return {
        "vector_engine": spec.vector_engine,
        "lexical_engine": spec.lexical_engine,
        "timeout_seconds": spec.timeout_seconds,
        "bm25": spec.bm25.model_dump(mode="json") if spec.bm25 is not None else None,
    }


def load_original(path: Path, sha256: str) -> Any:
    """The original v2 configuration, pinned by sha256 (refused when it differs or is not v2)."""
    from ..core.config import load_config

    if not path.is_file():
        raise AdoptionError(f"original configuration {path} is missing")
    actual = file_sha256(path)
    if actual != sha256:
        raise AdoptionError(f"original configuration {path.name}: sha256 {actual} is not the pinned {sha256}")
    original = load_config(path)
    if getattr(original, "collection", None) is not None:
        raise AdoptionError("the original configuration of an adopted store is a v2 (monolithic) configuration")
    return original


def adopt(config: Any, member: str, registry: MemberRegistry, *, as_of: datetime | None = None) -> MemberRef:
    """Adopt the store declared by ``collection.members.<member>.adopt`` (see the module docstring).
    Idempotent: adopting an unchanged, already registered store returns its ``MemberRef``."""
    declared = config.collection.members.get(member)
    if declared is None or declared.adopt is None:
        raise ConfigError(f"collection.members.{member}: not an adopted member (adopt block, key: adopted)", f"collection.members.{member}")
    spec = declared.adopt
    original = load_original(Path(spec.original_config), spec.original_config_sha256)
    store = Path(spec.store).resolve()
    catalog_spec = original.backends.get(original.storage.catalog)
    if getattr(catalog_spec, "kind", None) != "sqlite":
        raise AdoptionError("the original configuration's catalog backend is not sqlite (only sqlite stores are adopted in M1b)")
    if Path(catalog_spec.path).resolve() != store:
        raise AdoptionError(f"the original configuration builds {Path(catalog_spec.path).name!r}, not the declared store {store.name!r}")
    if original.dataset != config.dataset:
        raise AdoptionError(f"the adopted store serves dataset {original.dataset!r}; the collection is {config.dataset!r}")
    generation, manifest = store_generation(store)
    fingerprint = fingerprint_of(original)
    if manifest.get("fingerprint") != fingerprint:
        raise AdoptionError(
            f"store generation {generation}: manifest fingerprint {str(manifest.get('fingerprint'))[:12]} is not the original configuration's "
            f"recipe {fingerprint[:12]} (the store was not built by this configuration, or was re-stamped)"
        )
    if manifest.get("dataset") != original.dataset or manifest.get("generation") != generation:
        raise AdoptionError(f"store generation {generation}: manifest dataset/generation disagree with CURRENT")
    if manifest.get("status") not in {"complete", None}:
        raise AdoptionError(f"store generation {generation}: status {manifest.get('status')!r} (only a complete generation is adopted)")
    recipe = store_recipe(original)
    digests = store_digests(store, generation)
    documents = catalog_documents(store, generation)
    if not documents:
        raise AdoptionError(f"store generation {generation}: no documents")
    for doc_id, kind, source in documents:
        if source not in declared.sources:
            raise AdoptionError(f"document {doc_id!r}: source {source!r} is not one of member {member!r}'s sources {declared.sources}")
        if kind not in declared.record_kinds:
            raise AdoptionError(f"document {doc_id!r}: kind {kind!r} is not one of member {member!r}'s record_kinds")
        if not any(doc_id.startswith(prefix) for prefix in declared.id_namespaces):
            raise AdoptionError(f"document {doc_id!r} is outside member {member!r}'s id namespaces {declared.id_namespaces}")
    tree = tree_digest(digests)
    partition_id = canonical_sha256({"recipe": recipe.fingerprint(), "store_fingerprint": fingerprint, "tree": tree})
    key = f"adopted:{tree[:16]}"
    unit_id = f"{member}/{key}"
    location = FacetLocation(backend="sqlite", location=f"{ADOPTED_LOCATION}/{member}")
    facets = {name: location for name in ("catalog", "records", "lexical", "vector", "graph")}
    if original.projection.enabled:
        facets["projection"] = location
    ref = MemberRef(
        member=member,
        key=key,
        partition_id=partition_id,
        recipe=recipe,
        tier="adopted",
        as_of=as_of or datetime.now(timezone.utc),
        sources=tuple(declared.sources),
        counts=dict(sorted(Counter(kind for _, kind, _ in documents).items())),
        stats={
            "documents": len(documents),
            "generation": generation,
            "store_fingerprint": fingerprint,
            "original_config_sha256": spec.original_config_sha256,
            "store_backend": backend_settings(catalog_spec),
            "lexical": "adopted",
            # the imported community file the store materialised (bound by a communities_imported layer)
            "communities_sha256": original.communities.imported.sha256 if original.communities.strategy == "imported" and original.communities.imported is not None else None,
        },
        facets=facets,
        digests=digests,
    )
    existing = registry.directory.unit(unit_id)
    if existing is not None:
        for found in registry.members():
            if found.unit_id == unit_id:
                if found.partition_id != partition_id:
                    raise AdoptionError(f"member unit {unit_id} is registered with another partition; retire it first")
                return found
        raise AdoptionError(f"member unit {unit_id} is in the id directory without a member record; retire and purge it first")
    folder = registry.root / "members" / member / key_slug(key) / partition_id[:16]
    if folder.exists():
        raise AdoptionError(f"{folder.name}: an adoption folder exists without a registered unit; remove it first")
    folder.mkdir(parents=True)
    registered = False
    try:
        entries = [IdDirectoryEntry(doc_id=doc_id, kind=str(kind), unit_id=unit_id, unit_kind="member") for doc_id, kind, _ in documents]
        registry.directory.register(unit_id, "member", entries)
        registered = True
        record = {
            "member": member,
            "unit_id": unit_id,
            "generation": generation,
            "store_fingerprint": fingerprint,
            "original_config_sha256": spec.original_config_sha256,
            "recipe_fingerprint": recipe.fingerprint(),
            "tree_digest": tree,
            "files": len(digests),
        }
        _write_atomic(folder / ADOPTION_FILE, json.dumps(record, indent=2, sort_keys=True))
        _write_atomic(folder / MEMBER_FILE, ref.model_dump_json(indent=2))
        return ref
    except BaseException:
        if registered:
            registry.directory.retire(unit_id)
            registry.directory.purge(unit_id)
        shutil.rmtree(folder, ignore_errors=True)
        raise


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def adopted_store(config: Any, ref: MemberRef) -> Path:
    """The store path of an adopted member (from the configuration, never from a manifest)."""
    declared = config.collection.members.get(ref.member)
    if declared is None or declared.adopt is None:
        raise ConfigError(f"member {ref.unit_id} is adopted but collection.members.{ref.member} has no adopt block", f"collection.members.{ref.member}.adopt")
    return Path(declared.adopt.store).resolve()
