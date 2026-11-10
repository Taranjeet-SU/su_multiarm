"""Member partitions of a unified collection (docs/plans/unified_merge.md §6.1; M1a, lane LA).

A **member partition** is one immutable store built by the **shared write path** into a
member-scoped location of the collection's member registry::

    <collection root>/members/<member>/<key slug>/<build>/store/        one sqlite backend root
    <collection root>/members/<member>/<key slug>/<build>/member.json   the frozen ``MemberRef``
    <collection root>/id_directory.sqlite                               ``ingestion/id_directory.py``
    <collection root>/generations/                                      unified generations (``compose.py``)

``build`` validates the documents against the member declaration (sources, record kinds, id
namespaces, the catalog-kind rule), runs today's ``WritePath`` against the member store (staged,
validated and activated exactly as a monolithic build), registers the ids in the id directory
(refusing duplicates — fail closed) and only then writes ``member.json``. A failure at any step
removes the staged build; nothing is half registered.

Catalog kinds (§6.1, B9): kinds whose ids do not depend on the day (``signature`` and a member's
``catalog_kinds``) never enter day/month/snapshot members; they live in one catalog member per
source (key ``catalog``), rebuilt latest-wins at close. ``catalog_documents`` gives the latest-wins
union a caller passes to ``build(..., key="catalog")``.

Adopted members (read-only adoption of an existing store, §6.3, M1b) are registered by
``ingestion/adopt_member.py`` (``MemberRegistry.adopt``), never built here: their store stays where
it is and only ``member.json``/``adoption.json`` are written into this registry.

The live log member (``cadence: minutes``, M2a wiring) is never built here either: its archive
day/month and catalog members are sealed **log units** written by ``ingestion/log_members.py``
into ``members/<member>/<key>/<partition>/`` (``catalog`` facet backend ``log_unit``), its live window
by the slice ledger under ``slices/<member>/``; ``live`` is the ``ingestion/log_collection.LiveLogs``
of the configuration (set by ``runtime/collection.PartitionedView``).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.errors import ConfigError
from ..core.types import Document
from ..core.types_members import CATALOG_MEMBER_KEY, FacetLocation, IdDirectoryEntry, MemberRef, canonical_sha256, is_catalog_kind
from .id_directory import SqliteIdDirectory
from .recipes import partition_recipe
from .release import FileGenerationRegistry

MEMBER_FILE = "member.json"
STORE = "store"
FACETS = ("catalog", "records", "lexical", "vector")


def collection_root(config: Any) -> Path:
    """The member registry of a partitioned configuration: ``<catalog sqlite path>/collection``."""
    spec = config.backends.get(config.storage.catalog)
    if getattr(spec, "kind", None) != "sqlite":
        raise ConfigError("a partitioned collection keeps its member registry next to a sqlite catalog backend (M1a)", "storage.catalog")
    return Path(spec.path) / "collection"


def id_directory_path(config: Any) -> Path:
    declared = config.collection.id_directory.path
    return Path(declared) if declared is not None else collection_root(config) / "id_directory.sqlite"


def key_slug(key: str) -> str:
    return key.replace(":", "-")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def partition_id_of(documents: Sequence[Document], recipe_fingerprint: str) -> str:
    """sha256 over the document ids, their content and the partition recipe (§6.1)."""
    digest = hashlib.sha256(recipe_fingerprint.encode())
    for document in sorted(documents, key=lambda d: d.id):
        digest.update(b"\0" + document.id.encode() + b"\0" + hashlib.sha256(document.model_dump_json().encode()).digest())
    return digest.hexdigest()


def tier_of(key: str) -> str:
    if key == CATALOG_MEMBER_KEY:
        return "catalog"
    head = key.partition(":")[0]
    return {"snapshot": "snapshot", "day": "archive", "month": "archive", "adopted": "adopted"}[head]


def member_config(config: Any, store_root: Path, member: str | None = None) -> Any:
    """The v2 configuration the shared write path builds one member store with: the unified
    configuration with every sqlite backend rooted at the member store and the v3 blocks removed
    (a member store is one ordinary generation; it is never served through its own fingerprint).

    With ``member`` (a declared write-path member; ``CollectionMember.build_recipe``) its build
    settings apply: its ``embed_fields``
    override ``ingestion.projections.embed_fields`` for its kinds; ``vectors: online`` drops the
    embedder role's precomputed import (every chunk is embedded by the role's model);
    ``metadata_fields`` seals the store with the collection schema restricted to those fields
    (``member_schema``), builds no typed projection (a write-path member serves none) and keeps only
    the ``ingestion.attach`` entries whose target fields the member declares."""
    backends = {name: spec.model_copy(update={"path": store_root}) if getattr(spec, "kind", None) == "sqlite" else spec for name, spec in config.backends.items()}
    communities = config.communities.model_copy(update={"layers": []})
    update: dict[str, Any] = {"schema_version": 2, "collection": None, "crosssource": None, "backends": backends, "communities": communities}
    declared = None if member is None else config.collection.members.get(member)
    if declared is not None and declared.build_recipe():  # write-path members with build settings only
        ingestion = config.ingestion
        if declared.embed_fields:
            from ..core.config_logs import ProjectionsConfig

            projections = ingestion.projections or ProjectionsConfig()
            embed = {**projections.embed_fields, **{kind: list(fields) for kind, fields in declared.embed_fields.items()}}
            ingestion = ingestion.model_copy(update={"projections": projections.model_copy(update={"embed_fields": embed})})
        if declared.metadata_fields is not None:
            schema = member_schema(config.metadata_schema, declared.metadata_fields)
            allowed = {f.name for f in schema.fields}
            attach = [a for a in ingestion.attach if set(a.columns.values()) <= allowed]
            ingestion = ingestion.model_copy(update={"attach": attach})
            update["metadata_schema"] = schema
            update["projection"] = config.projection.model_copy(update={"enabled": False})
        update["ingestion"] = ingestion
        if declared.vectors == "online" and config.models.roles.embedder.precomputed is not None:
            roles = config.models.roles
            update["models"] = config.models.model_copy(update={"roles": roles.model_copy(update={"embedder": roles.embedder.model_copy(update={"precomputed": None})})})
    return config.model_copy(update=update)


def member_schema(schema: Any, fields: Sequence[str]) -> Any:
    """``schema`` restricted to ``fields`` plus ``source``/``kind`` and the equivalence field (when
    declared), in the collection's field order."""
    keep = {*fields, "source", "kind", *([schema.equivalence_field] if schema.equivalence_field else [])}
    return schema.model_copy(update={"fields": [f for f in schema.fields if f.name in keep]})


def catalog_documents(members: Iterable[Sequence[Document]], kinds: Sequence[str]) -> list[Document]:
    """Latest-wins union by id of the catalog-kind documents of ``members`` (in order)."""
    latest: dict[str, Document] = {}
    for documents in members:
        for document in documents:
            if is_catalog_kind(document.kind, kinds):
                latest[document.id] = document
    return [latest[k] for k in sorted(latest)]


class MemberRegistry:
    """The member registry + id directory of one partitioned configuration."""

    def __init__(self, config: Any):
        if getattr(config, "collection", None) is None:
            raise ConfigError("the member registry needs a partitioned configuration (collection block, schema v3)", "collection")
        self.config = config
        self.root = collection_root(config)
        self.directory = SqliteIdDirectory(id_directory_path(config), timeout_seconds=config.backends[config.storage.catalog].timeout_seconds)
        self.generations = FileGenerationRegistry(self.root / "generations")
        self.live: Any = None  # ingestion/log_collection.LiveLogs when a member has cadence: minutes

    # --- layout ------------------------------------------------------------------------------------

    def unit_dir(self, ref: MemberRef) -> Path:
        """The registry folder of a member (``member.json``; for an adopted member also
        ``adoption.json`` — the adopted store itself lives outside the registry)."""
        if ref.tier == "adopted":
            return self.root / "members" / ref.member / key_slug(ref.key) / ref.partition_id[:16]
        location = ref.facets.get("catalog")
        if location is None:
            raise ValueError(f"member {ref.unit_id} has no catalog facet")
        return self.root / Path(location.location).parent

    def store_dir(self, ref: MemberRef) -> Path:
        """The sqlite store root of a member: under the registry, or (adopted, M1b) the store the
        configuration's ``adopt.store`` names; a log unit's folder (M2a wiring)."""
        from .log_members import is_log_unit

        if is_log_unit(ref):
            return self.unit_dir(ref)
        if ref.tier == "adopted":
            from .adopt_member import adopted_store

            return adopted_store(self.config, ref)
        return self.root / ref.facets["catalog"].location

    def digest_base(self, ref: MemberRef) -> Path:
        """The folder ``ref.digests`` names are relative to."""
        return self.store_dir(ref) if ref.tier == "adopted" else self.unit_dir(ref)

    def members(self) -> list[MemberRef]:
        """Every registered member (``member.json`` files), ordered by member and key."""
        base = self.root / "members"
        if not base.exists():
            return []
        refs = [MemberRef.model_validate_json(p.read_text()) for p in base.glob(f"*/*/*/{MEMBER_FILE}")]
        return sorted(refs, key=lambda r: (r.member, r.key, r.as_of))

    def live_members(self) -> list[MemberRef]:
        """Registered members whose unit is live in the id directory."""
        live = {u["unit_id"] for u in self.directory.units(status="live")}
        return [r for r in self.members() if r.unit_id in live]

    def verify(self, ref: MemberRef) -> None:
        """Every recorded digest still matches (fail closed, §6.12). A log unit is verified at its
        seal (``unit.json`` + ``SEAL`` against the recorded file digests): its bytes were verified
        when it was sealed or received, as a write-path member's digests cover its manifest and
        ``CURRENT``, so compose stays O(members) (§6.7 compose ≤ 10 s)."""
        from .log_members import is_log_unit, verify_sealed_ref

        if is_log_unit(ref):
            failure = verify_sealed_ref(self.unit_dir(ref), ref)
            if failure is not None:
                raise MemberUnavailable(ref.unit_id, failure)
            return
        folder = self.digest_base(ref)
        for name, expected in ref.digests.items():
            path = folder / name
            if not path.exists() or file_sha256(path) != expected:
                raise MemberUnavailable(ref.unit_id, f"digest of {name} does not match the registered member")

    # --- build -------------------------------------------------------------------------------------

    def check_documents(self, member: str, key: str, documents: Sequence[Document]) -> None:
        """The member declaration accepts ``documents`` under ``key`` (refused otherwise)."""
        declared = self.config.collection.members.get(member)
        if declared is None:
            raise ValueError(f"no member {member!r} in collection.members")
        if declared.cadence == "minutes":
            raise ValueError(f"member {member!r} is the live log member (cadence: minutes): its slices and archive members come from the slice ledger (ingestion/log_collection.py), never the write path")
        scheme = declared.key
        catalog = key == CATALOG_MEMBER_KEY
        allowed_heads = {"snapshot": {"snapshot"}, "day": {"day", "month"}, "month": {"month"}, "adopted": set()}[scheme]
        if scheme == "adopted":
            raise ValueError(f"member {member!r} is adopted read-only (collection adopt); it is never built by the write path")
        if catalog:
            if not declared.all_catalog_kinds():
                raise ValueError(f"member {member!r} declares no catalog kinds; key {key!r} is not available")
        elif key.partition(":")[0] not in allowed_heads:
            raise ValueError(f"member {member!r} (key: {scheme}) cannot build key {key!r}")
        kinds = declared.all_catalog_kinds()
        for document in documents:
            if document.dataset != self.config.dataset:
                raise ValueError(f"document {document.id!r}: dataset {document.dataset!r} is not {self.config.dataset!r}")
            if document.source not in declared.sources:
                raise ValueError(f"document {document.id!r}: source {document.source!r} is not one of member {member!r}'s sources {declared.sources}")
            if document.kind not in declared.record_kinds:
                raise ValueError(f"document {document.id!r}: kind {document.kind!r} is not one of member {member!r}'s record_kinds")
            if not any(document.id.startswith(prefix) for prefix in declared.id_namespaces):
                raise ValueError(f"document {document.id!r} is outside member {member!r}'s id namespaces {declared.id_namespaces}")
            if declared.metadata_fields is not None:
                extra = sorted(set(document.metadata) - set(declared.metadata_fields))
                if extra:
                    raise ValueError(f"document {document.id!r}: metadata {extra} outside member {member!r}'s metadata_fields")
            if catalog != is_catalog_kind(document.kind, kinds):
                where = "the catalog member holds catalog kinds only" if catalog else "catalog kinds live in the member's catalog member (key: catalog), never in day/snapshot members"
                raise ValueError(f"document {document.id!r} (kind {document.kind!r}): {where}")

    def build(
        self,
        member: str,
        key: str,
        documents: Sequence[Any],
        *,
        embedder: Any,
        extractor: Any,
        community_model: Any = None,
        keyword_epoch: str | None = None,
        as_of: datetime | None = None,
        replaces: Sequence[str] = (),
    ) -> MemberRef:
        """Build, register and record one member partition (see the module docstring)."""
        from ..extraction.chunking import to_v2_document
        from .write import WritePath

        docs = [to_v2_document(d) for d in documents]
        if len({d.id for d in docs}) != len(docs):
            raise ValueError("duplicate document IDs in input")
        if not docs:
            raise ValueError(f"member {member}/{key}: no documents")
        self.check_documents(member, key, docs)
        unit_id = f"{member}/{key}"
        if self.directory.unit(unit_id) is not None:
            raise ValueError(f"member unit {unit_id!r} is already registered; retire it before building the key again")
        recipe = partition_recipe(self.config, member, keyword_epoch=keyword_epoch)
        partition_id = partition_id_of(docs, recipe.fingerprint())
        relative = Path("members") / member / key_slug(key) / f"{partition_id[:16]}-{uuid.uuid4().hex[:8]}"
        folder = self.root / relative
        store = folder / STORE
        store.mkdir(parents=True)
        registered = False
        try:
            from ..backends.sqlite import SqliteBackend

            cfg = member_config(self.config, store, member)
            backend = SqliteBackend.from_config(cfg.backends[cfg.storage.catalog])
            backends = {name: backend for name, spec in cfg.backends.items() if getattr(spec, "kind", None) == "sqlite"}
            write = WritePath(cfg, backends, extractor=extractor, embedder=embedder, community_model=community_model, profile=cfg.profile)
            manifest = write.ingest(docs, replace=True)
            generation = manifest["generation"]
            manifest_rel = Path(STORE) / "snapshots" / generation / "manifest.json"
            current_rel = Path(STORE) / "CURRENT"
            location = FacetLocation(backend="sqlite", location=(relative / STORE).as_posix())
            ref = MemberRef(
                member=member,
                key=key,
                partition_id=partition_id,
                recipe=recipe,
                tier=tier_of(key),
                as_of=as_of or datetime.now(timezone.utc),
                sources=tuple(self.config.collection.members[member].sources),
                counts=dict(sorted(Counter(d.kind for d in docs).items())),
                catalog_kinds=tuple(self.config.collection.members[member].all_catalog_kinds()) if key == CATALOG_MEMBER_KEY else (),
                stats={"documents": len(docs), "generation": generation},
                facets={name: location for name in FACETS},
                digests={manifest_rel.as_posix(): file_sha256(folder / manifest_rel), current_rel.as_posix(): file_sha256(folder / current_rel)},
                keyword_epoch=keyword_epoch,
            )
            day = ref.day()
            entries = [IdDirectoryEntry(doc_id=d.id, kind=d.kind, unit_id=unit_id, unit_kind="member", day=day) for d in docs]
            self.directory.register(unit_id, "member", entries, replaces=replaces)
            registered = True
            tmp = folder / f".{MEMBER_FILE}.tmp"
            tmp.write_text(ref.model_dump_json(indent=2))
            os.replace(tmp, folder / MEMBER_FILE)
            return ref
        except BaseException:
            if registered:
                self.directory.retire(unit_id)
                self.directory.purge(unit_id)
            shutil.rmtree(folder, ignore_errors=True)
            raise

    def adopt(self, member: str, *, as_of: datetime | None = None) -> MemberRef:
        """Read-only adoption of the store declared by ``collection.members.<member>.adopt``
        (``ingestion/adopt_member.py``): never writes, re-stamps or relocates that store."""
        from .adopt_member import adopt

        return adopt(self.config, member, self, as_of=as_of)

    def supersede(self, doc_ids: Sequence[str], by: MemberRef) -> None:
        """Mark ``doc_ids`` of earlier units as superseded by the registered member ``by`` (e.g. a
        daily ticket member superseding the adopted ticket portion, M3). The ids stay in their
        units' files; every facet excludes them for views that pin ``by`` (id directory)."""
        if self.directory.unit(by.unit_id) is None:
            raise ValueError(f"member {by.unit_id} is not registered; register it before it supersedes anything")
        self.directory.supersede(list(doc_ids), by.unit_id)

    def retire(self, ref: MemberRef, *, purge: bool = True) -> None:
        """Retire a member that no active generation references: the id directory forgets its ids and
        its build is removed (``purge``). A member of the current generation is refused."""
        current = self.generations.current()
        if current is not None and any(f"{m['member']}/{m['key']}" == ref.unit_id and m["partition_id"] == ref.partition_id for m in current.get("members", [])):
            raise ValueError(f"member {ref.unit_id} is part of the active generation; compose without it first")
        self.directory.retire(ref.unit_id)
        if purge:
            self.directory.purge(ref.unit_id)
            # an adopted member's registry folder only; the adopted store is never removed
            shutil.rmtree(self.unit_dir(ref), ignore_errors=True)


class MemberUnavailable(RuntimeError):
    """A registered member's files no longer match its digests (fail closed for that member)."""

    def __init__(self, unit_id: str, message: str):
        self.unit_id = unit_id
        super().__init__(f"member {unit_id} unavailable: {message}")


def describe_member(ref: MemberRef) -> dict[str, Any]:
    """A content-free summary for CLI/status output."""
    return {
        "unit_id": ref.unit_id,
        "tier": ref.tier,
        "partition_id": ref.partition_id,
        "fingerprint": ref.recipe.fingerprint(),
        "counts": dict(ref.counts),
        "as_of": ref.as_of.isoformat(),
        "digest": canonical_sha256(ref.digests),
    }


def dump_refs(refs: Iterable[MemberRef]) -> list[dict[str, Any]]:
    return [json.loads(r.model_dump_json()) for r in refs]
