"""The partitioned sqlite backend of a unified collection (docs/plans/unified_merge.md §6.1, §6.5; M1a).

``PartitionedSqliteBackend`` stands in for the configured sqlite backend when the configuration is
partitioned (``collection.mode: partitioned``). The engine's arms, selection and packing keep
calling the same facets with the same ``GenerationId``; here that id names a **unified generation**
(``ingestion/compose.py``) whose manifest lists member partitions by reference. Each facet resolves
the generation to its pinned units, opens only the units in the request's scope, calls today's
sqlite facet of each unit with the unit's own generation, and merges:

=================  =================================================================================
facet              partition contract
=================  =================================================================================
catalog ``get``     id → unit through the **id directory** (one indexed lookup per batch, filtered to
                   the pinned units), then that unit's catalog; unresolvable → not found (never a scan)
``iter_scope``     per-unit pages merged by document id (k-way); cursor = per-unit offsets; a scope
                   with ``document_ids`` is routed through the id directory
records            per-unit matched rows concatenated (id order) and ``scope_result`` computed once,
                   so groups, shares, ranks and denominators are those of one store
lexical            per **stratum** (a member id): one unit → today's search on that unit unchanged;
                   several units → **query-time statistics** (N, token total and query-token df
                   summed over the units minus superseded/tombstoned rows; idf recomputed with the
                   BM25Okapi formula; ``average_idf`` over the union vocabulary, cached per unit set)
                   so the ranking equals one store built from the same documents; strata fused by
                   RRF under ``source_quotas`` (``arms/stratified.py``)
vector             exact top-k per unit (flat NumPy/Faiss) merged by score (ties in corpus order): one
                   embedding space is enforced at compose, so scores are comparable
=================  =================================================================================

Scope pruning: a unit whose member's ``sources`` (or ``record_kinds``) cannot satisfy the scope's
``source`` (``kind``) predicates is **never opened** (counted in ``stats['collection.unit_reads:*']``).

Caches are keyed **per unit** (``partition_id``): the unit pool keeps one sqlite backend per
partition, whose segment, vector, row-map and faiss caches are keyed by the member's own generation.
Composing a new generation reopens nothing that did not change.

Superseded and tombstoned rows (id directory, relative to the pinned view) are excluded from
candidates, records, counts and statistics. Fail closed: a member whose files no longer match their
digests is refused (``BackendError``) for the request; per-member labelled degradation arrives with
the coverage envelope (M1b/M2b).

**Live log member (M2a wiring, lane LB).** A member with ``cadence: minutes`` is served from sealed
log units — its archive day/month and catalog members and the provisional slices of the request pin
(``ingestion/log_collection.py``: a request generation id names generation + slice vector) — as one
**log stratum** (``backends/sqlite/log_units.LogStratum``): lexical ranks it with query-time
statistics (``QueryTimeScorer``), dense with ``DenseLogScorer`` (exact, merged with the other units
by score; units whose vectors are pending are skipped and reported ``dense: pending`` in
``coverage`` and ``stats``), and catalog/records/listings read each unit's ``catalog.sqlite``
through the sqlite facets' SQL. Served rows of the stratum are decided by sealed versions (a unit's
superseded/tombstoned rows are its ``hidden`` ids); ``get`` routes a log id to the unit serving it.
The other strata are untouched: with or without log units in the view, a support-only request opens
the same units and returns the same ranking (R1, §6.5 guarantee).
"""

from __future__ import annotations

import hashlib
import heapq
import json
import sqlite3
import threading
from collections import Counter, OrderedDict
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

import numpy as np

from ...core.errors import BackendError
from ...core.types import (
    Chunk,
    Document,
    GenerationId,
    LexicalQuery,
    MetadataSchema,
    Page,
    PageResult,
    Predicate,
    Scope,
    ScopePredicates,
    ScopeResult,
    ScopeSpec,
    ScoredChunk,
    UnsupportedConstraint,
)
from ...core.types_members import MemberRef, PinnedView
from .._scope import check_matched, matched_cap, scope_result, unsupported_reason
from ._db import KIND
from .adapter import SqliteBackend
from .bm25 import average_idf_of, stratum_idf
from .catalog import page_window
from ...arms.stratified import fuse_strata
from ...ingestion.log_members import is_log_unit

CURSOR_PREFIX = "units:"
# The generation argument a log unit's catalog facets receive (ignored: a sealed unit is one build).
LOG_UNIT_GEN = GenerationId(dataset="log-unit", generation="0" * 32, index_format=1, fingerprint="0" * 64)
CHUNK_OWNERS = 200_000  # remembered chunk -> unit owners (cleared when full)
MAX_PAGE = 10_000  # the largest ``Page.size``
CACHE_ENTRIES = 4096  # bound of the per-view derived caches (shadowed ids, stratum vocabularies)
VALUE_CACHE_ENTRIES = 65_536  # per-unit ``values``/``exists`` answers kept (least recently used out)


class Unit:
    """One opened member partition: its frozen ref, its sqlite backend and its store generation. A
    log unit (``log``) has a ``UnitStore`` (catalog/records of its ``catalog.sqlite``) as backend;
    its lexical and dense facets are its stratum's (``LogStratum``)."""

    __slots__ = ("ref", "backend", "gen", "log")

    def __init__(self, ref: Any, backend: Any, gen: GenerationId, log: bool = False):
        self.ref, self.backend, self.gen, self.log = ref, backend, gen, log

    @property
    def unit_id(self) -> str:
        return self.ref.unit_id

    @property
    def stratum(self) -> str:
        return self.ref.member

    @property
    def key(self) -> str:
        return unit_key(self.ref)


class SliceUnit:
    """The unit reference of one pinned slice of a live log member (the request pin's slice vector)."""

    __slots__ = ("member", "unit_id", "sources", "day", "partition_id", "tier")

    def __init__(self, member: str, slice_id: str, sources: Sequence[str], day: Any):
        self.member, self.unit_id, self.sources, self.day = member, slice_id, tuple(sources), day
        self.partition_id = slice_id  # a slice id is sealed once and never rebuilt
        self.tier = "slice"


def unit_key(ref: Any) -> str:
    """Pool / chunk-owner key of a unit: a member's ``partition_id``, a slice's id."""
    return ref.partition_id


def _named(predicates: Sequence[Predicate], field: str) -> set[str] | None:
    """Values a positive ``eq``/``in`` predicate on ``field`` allows (``None``: unconstrained)."""
    allowed: set[str] | None = None
    for p in predicates:
        if p.field != field or p.negate or p.op not in {"eq", "in"}:
            continue
        values = {str(p.value)} if p.op == "eq" else {str(v) for v in (p.value or [])}
        allowed = values if allowed is None else allowed & values
    return allowed


class PartitionedSqliteBackend:
    """See the module docstring. ``root`` is the configured sqlite backend (runtime facets: cache,
    ledger, sessions); ``registry`` the collection's ``MemberRegistry``."""

    kind = KIND

    def __init__(self, root: SqliteBackend, registry: Any, config: Any):
        self.root = root
        self.registry = registry
        self.config = config
        self.collection = config.collection
        self.stats: Counter[str] = root.stats
        self._pool: dict[str, Unit] = {}
        self._views: dict[str, PinnedView] = {}
        self._shadowed: dict[tuple[str, str, tuple[int, int, int]], frozenset[str]] = {}
        self._vocab: dict[tuple[tuple[str, frozenset[str]], ...], tuple[int, float]] = {}
        self._chunks: dict[str, str] = {}  # chunk id -> partition id of the unit that returned it
        self._lock = threading.RLock()
        self._layers_ok: dict[tuple[str, str], str | None] = {}  # (generation, layer) -> None (verified) or the failure
        self._schemas: dict[str, MetadataSchema | None] = {}  # unit key -> its own sealed metadata schema
        self.catalog = PartitionedCatalog(self)
        self.records = PartitionedRecords(self)
        self.lexical = StratifiedLexical(self)
        self.vector = MergedVector(self)
        self.graph = PartitionedGraph(self)
        self.relational = PartitionedProjection(self)
        self.cache, self.ledger, self.session = root.cache, root.ledger, root.session
        self.live = getattr(registry, "live", None)  # ingestion/log_collection.LiveLogs (live log members)
        self.pending: dict[str, tuple[str, ...]] = {}  # request generation -> log units with dense pending

    @property
    def max_matched(self) -> int:
        return self.root.max_matched

    # --- lifecycle (the unified registry is the authority; members are immutable) -------------------

    def warm(self, gen: GenerationId) -> dict[str, Any]:
        """Startup warm-up of a pinned view (``Engine.warmup``): every pinned unit is opened (members
        verified, slices checked on disk), write-path member stores load their lexical segment and
        vectors, and each live log stratum pre-opens its units (readers, served rows, catalog stores,
        dense matrices). A unit that cannot be opened raises (``BackendError``), as a request would.
        Returns the units warmed and the log units whose vectors are pending."""
        units = self.units(gen)
        for unit in units:
            if not unit.log:
                warm = getattr(unit.backend, "warm", None)
                if warm is not None:
                    warm(unit.gen)
        view = self.view(gen)
        pending: list[str] = []
        for member in self.live_members_of(gen):
            pending.extend(self.log_stratum(member).warm_view(view)["dense_pending"])
        return {"units": len(units), "log_members": self.live_members_of(gen), "dense_pending": pending}

    def protect(self, gen: GenerationId) -> None:  # members are never deleted while composed
        return None

    def unprotect(self, gen: GenerationId) -> None:
        return None

    def generation_registry(self) -> Any:
        return self.registry.generations

    def analyzer_id(self, gen: GenerationId) -> str:
        return self.root.analyzer_id(gen)

    def probe(self) -> dict[str, Any]:
        report = self.root.probe()
        report["partitioned"] = {"members": len(self.registry.members()), "generations": len(self.registry.generations.generations())}
        return report

    def delete_generation(self, gen: GenerationId) -> None:
        raise BackendError(KIND, "delete_generation", "a unified generation references member partitions; retire members instead")

    def generations(self) -> list[str]:
        return self.registry.generations.generations()

    # --- views and units -------------------------------------------------------------------------------

    def view(self, gen: GenerationId) -> PinnedView:
        """The pinned view of a unified generation (manifests are immutable: cached per generation),
        or of a request generation id naming generation + slices (``LiveLogs.pin``)."""
        found = self._views.get(gen.generation)
        if found is not None:
            return found
        if self.live is not None:
            pinned = self.live.view(gen.generation)
            if pinned is not None:
                return pinned
        from ...ingestion.compose import view_of

        manifest = self.registry.generations.manifest(gen.generation)
        if manifest is None or manifest.get("kind") != "partitioned":
            raise BackendError(KIND, "generation", f"unknown unified generation {gen.prefix()}")
        view = view_of(manifest)
        with self._lock:
            self._views[gen.generation] = view
        return view

    # --- the live log member ----------------------------------------------------------------------

    def is_live(self, member: str) -> bool:
        return self.live is not None and member in self.live.members

    def log_stratum(self, member: str) -> Any:
        """The ``LogStratum`` of a live log member (its units' served rows, scorers and stores)."""
        if not self.is_live(member):
            raise BackendError(KIND, "member", f"member {member!r} is not a live log member")
        return self.live.stratum(member, stats=self.stats, max_matched=self.root.max_matched)

    def _open_log(self, ref: Any) -> Unit:
        stratum = self.log_stratum(ref.member)
        if isinstance(ref, SliceUnit):
            folder = stratum.ledger.unit_dir(ref.unit_id)
            if not (folder / "SEAL").exists():
                raise BackendError(KIND, "member", f"slice {ref.unit_id} of the request pin is no longer on disk (unpinned and collected)")
        else:
            if not is_log_unit(ref):
                raise BackendError(KIND, "member", f"member {ref.unit_id} of live log member {ref.member!r} is not a sealed log unit (built by the write path?)")
            from ...ingestion.members import MemberUnavailable

            try:
                self.registry.verify(ref)
            except MemberUnavailable as exc:
                raise BackendError(KIND, "member", str(exc)) from exc
            folder = self.registry.unit_dir(ref)
        reader = stratum.units.reader(ref.unit_id, folder)
        return Unit(ref, stratum.unit_store(reader), LOG_UNIT_GEN, log=True)

    def pinned_refs(self, gen: GenerationId) -> list[Any]:
        """Every pinned unit reference of the view: members, then the slices of live members."""
        view = self.view(gen)
        refs: list[Any] = list(view.members)
        if view.slices and self.live is not None:
            members = self.collection.members
            for member in sorted(view.slices):
                for day, ids in sorted(view.slices[member].items()):
                    refs.extend(SliceUnit(member, slice_id, members[member].sources, day) for slice_id in ids)
        return refs

    def log_owners(self, gen: GenerationId, ids: Sequence[str]) -> dict[str, str]:
        """``{doc id: unit id}`` of the ids served by a live log unit of the view (sealed versions
        decide; no id-directory lookup)."""
        if self.live is None or not ids:
            return {}
        view = self.view(gen)
        live = {r.member for r in view.members if self.is_live(r.member)} | {m for m in view.slices if self.is_live(m)}
        found: dict[str, str] = {}
        for member in sorted(live):
            owners = self.log_stratum(member).owners(view)
            for doc_id in ids:
                unit = owners.get(doc_id)
                if unit is not None and doc_id not in found:
                    found[doc_id] = unit
        return found

    def live_members_of(self, gen: GenerationId) -> list[str]:
        """Live log members with a unit in the view."""
        if self.live is None:
            return []
        view = self.view(gen)
        return sorted({r.member for r in view.members if self.is_live(r.member)} | {m for m in view.slices if self.is_live(m)})

    def open(self, ref: Any) -> Unit:
        """The unit of ``ref`` from the pool (keyed by ``partition_id``; a slice by its id), verified on first open."""
        key = unit_key(ref)
        unit = self._pool.get(key)
        if unit is not None:
            return unit
        if self.is_live(ref.member):
            with self._lock:
                unit = self._pool.get(key)
                if unit is None:
                    unit = self._open_log(ref)
                    self._pool[key] = unit
                    self.stats["collection.unit_loads"] += 1
                return unit
        with self._lock:
            unit = self._pool.get(ref.partition_id)
            if unit is not None:
                return unit
            from ...ingestion.members import MemberUnavailable
            from ...ingestion.release import FileGenerationRegistry, generation_id

            try:
                self.registry.verify(ref)
            except MemberUnavailable as exc:
                raise BackendError(KIND, "member", str(exc)) from exc
            store = self.registry.store_dir(ref)
            manifest = FileGenerationRegistry(store).current()
            if manifest is None:
                raise BackendError(KIND, "member", f"member {ref.unit_id} has no active store generation")
            root = self.root
            if ref.tier == "adopted":
                # §6.3: the adopted store is opened read-only with its own backend settings (the
                # recorded ones, not the unified configuration's) and its own generation
                if manifest.get("generation") != ref.stats.get("generation") or manifest.get("fingerprint") != ref.stats.get("store_fingerprint"):
                    raise BackendError(KIND, "member", f"member {ref.unit_id} unavailable: the adopted store's active generation is not the adopted one")
                backend = adopted_backend(store, ref, root.clock)
            else:
                backend = SqliteBackend(store, root.vector_engine, root.timeout, root.clock, root.lexical_engine, root.bm25)
            unit = Unit(ref, backend, generation_id(manifest))
            self._pool[ref.partition_id] = unit
            self.stats["collection.unit_loads"] += 1
            return unit

    def units(self, gen: GenerationId, predicates: Sequence[Predicate] = (), refs: Sequence[MemberRef] | None = None) -> list[Unit]:
        """The pinned units a request with ``predicates`` may read, in unit-id order; units whose
        member cannot hold a document of the requested ``source``/``kind`` are not opened."""
        sources, kinds = _named(predicates, "source"), _named(predicates, "kind")
        members = self.collection.members
        out = []
        for ref in self.pinned_refs(gen) if refs is None else refs:
            declared = members.get(ref.member)
            if sources is not None and not (sources & set(ref.sources)):
                continue
            if kinds is not None and declared is not None and not (kinds & set(declared.record_kinds)):
                continue
            out.append(self.open(ref))
            self.stats[f"collection.unit_reads:{ref.unit_id}"] += 1
        return out

    def unit_schema(self, unit: Unit) -> MetadataSchema | None:
        """The metadata schema a unit's records were sealed with (cached per unit: units are
        immutable); ``None`` when its records facet cannot tell (then it is asked as before). A log
        unit is typed by the collection schema."""
        key = unit_key(unit.ref)
        if key in self._schemas:
            return self._schemas[key]
        reader = getattr(unit.backend.records, "schema", None)
        schema = reader(unit.gen) if callable(reader) and not unit.log else (self.config.metadata_schema if unit.log else None)
        with self._lock:
            self._schemas[key] = schema
        return schema

    def unit_lacks(self, unit: Unit, field: str) -> bool:
        """Whether ``field`` is declared by the collection but not by the unit's own sealed schema
        (the unit holds no value of it; asking its store would refuse an unknown field)."""
        if self.config.metadata_schema.field(field) is None:
            return False
        schema = self.unit_schema(unit)
        return schema is not None and schema.field(field) is None

    def member_unit(self, gen: GenerationId, unit_id: str) -> Unit:
        """The pinned unit ``unit_id`` (``<member>/<key>``) of the view."""
        for ref in self.view(gen).members:
            if ref.unit_id == unit_id:
                self.stats[f"collection.unit_reads:{ref.unit_id}"] += 1
                return self.open(ref)
        raise BackendError(KIND, "member", f"unit {unit_id} is not part of unified generation {gen.prefix()}")

    # --- attached layers (M1b, ``ingestion/layers.py``) ---------------------------------------------

    def layer(self, gen: GenerationId, name: str) -> Any:
        """The verified ``LayerRef`` ``name`` of the view (digest re-checked once per process and
        generation); ``BackendError`` when it is absent or its files changed (fail closed for it)."""
        from ...ingestion.layers import LayerUnavailable, LayerUnavailableError, verify

        view = self.view(gen)
        ref = next((layer for layer in view.layers if layer.name == name), None)
        if ref is None:
            raise LayerUnavailableError(name, f"layer {name} is not pinned by unified generation {gen.prefix()} (unavailable at compose)")
        key = (gen.generation, name)
        if key not in self._layers_ok:
            try:
                verify(self.config, ref)
                failure = None
            except LayerUnavailable as exc:
                failure = str(exc)
            with self._lock:
                self._layers_ok[key] = failure
        failure = self._layers_ok[key]
        if failure is not None:
            self.stats[f"collection.layer_unavailable:{name}"] += 1
            raise LayerUnavailableError(name, failure)
        self.stats[f"collection.layer_reads:{name}"] += 1
        return ref

    def layer_unit(self, gen: GenerationId, name: str) -> Unit:
        """The adopted member unit a member-bound layer belongs to (``version`` = ``<unit>@<generation>``)."""
        ref = self.layer(gen, name)
        unit_id, _, generation = (ref.version or "").partition("@")
        unit = self.member_unit(gen, unit_id)
        if unit.gen.generation != generation:
            raise BackendError(KIND, "layer", f"layer {name} answers for store generation {generation}, the member serves {unit.gen.generation}")
        return unit

    def layer_generation(self, gen: GenerationId, name: str) -> GenerationId:
        """The store generation an attached backend layer answers for (``LayerBackend``)."""
        return self.layer_unit(gen, name).gen

    def layers_for(self, arm: str) -> list[str]:
        """Names of the declared layers serving ``arm``."""
        return [layer.name for layer in self.collection.layers if arm in layer.arms]

    def layer_check(self, gen: GenerationId, arm: str) -> list[str]:
        """Verify every declared layer serving ``arm`` for this request (``BackendError`` on the
        first unavailable one); returns their names."""
        names = self.layers_for(arm)
        for name in names:
            self.layer(gen, name)
        return names

    def coverage(self, gen: GenerationId) -> dict[str, Any]:
        """Per-member and per-layer coverage labels of the pinned view (§6.11 ``coverage``): status
        is what this process observed — ``available`` once opened/verified, ``unavailable`` with the
        reason, ``unverified`` before any request read the unit or layer."""
        view = self.view(gen)
        manifest = self.registry.generations.manifest(view.generation or gen.generation) or {}
        opened = {unit.ref.unit_id for unit in self._pool.values()}
        members = [
            {
                "unit": ref.unit_id,
                "member": ref.member,
                "tier": ref.tier,
                "sources": list(ref.sources),
                "as_of": ref.as_of.isoformat(),
                "counts": dict(ref.counts),
                "status": "available" if ref.unit_id in opened else "unverified",
            }
            for ref in view.members
        ]
        layers: list[dict[str, Any]] = []
        for ref in view.layers:
            failure = self._layers_ok.get((gen.generation, ref.name), "")
            status = "unverified" if failure == "" else ("available" if failure is None else "unavailable")
            layers.append({"layer": ref.name, "kind": ref.kind, "sources": list(ref.sources), "arms": list(ref.arms), "status": status, **({"reason": failure} if status == "unavailable" else {})})
        for name, reason in sorted((manifest.get("layers_unavailable") or {}).items()):
            layers.append({"layer": name, "status": "unavailable", "reason": reason})
        out = {"generation": view.generation or gen.generation, "members": members, "layers": layers}
        if view.slices:
            pending = set(self.pending.get(gen.generation, ()))
            out["slices"] = [
                {"unit": ref.unit_id, "member": ref.member, "day": ref.day.isoformat(), "tier": "slice", "status": "available" if ref.unit_id in opened else "unverified",
                 "dense": "pending" if ref.unit_id in pending else ("ready" if gen.generation in self.pending else "unverified")}
                for ref in self.pinned_refs(gen) if isinstance(ref, SliceUnit)
            ]
            if pending:
                out["dense"] = "pending"
        return out

    def shadowed(self, gen: GenerationId, unit: Unit) -> frozenset[str]:
        """Ids of ``unit`` superseded or tombstoned by another unit of the view (cached per view); a
        log unit's are the rows its stratum does not serve (sealed versions)."""
        if unit.log:
            return self.log_stratum(unit.stratum).hidden(self.view(gen), unit.unit_id)
        key = (gen.generation, unit.unit_id, self.registry.directory.stamp())
        found = self._shadowed.get(key)
        if found is None:
            found = self.registry.directory.shadowed(unit.unit_id, self.view(gen))
            with self._lock:
                if len(self._shadowed) >= CACHE_ENTRIES:
                    self._shadowed.clear()
                self._shadowed[key] = found
        return found

    def remember(self, unit: Unit, hits: Sequence[ScoredChunk]) -> Sequence[ScoredChunk]:
        """Record which unit returned each chunk (bounded), so hit reads open that unit only."""
        if hits:
            with self._lock:
                if len(self._chunks) + len(hits) > CHUNK_OWNERS:
                    self._chunks.clear()
                for hit in hits:
                    self._chunks[hit.chunk_id] = unit.key
        return hits

    def remember_keys(self, owners: Sequence[tuple[str, str]]) -> None:
        """Record ``(chunk id, unit key)`` owners (log stratum hits)."""
        if owners:
            with self._lock:
                if len(self._chunks) + len(owners) > CHUNK_OWNERS:
                    self._chunks.clear()
                for chunk_id, key in owners:
                    self._chunks[chunk_id] = key

    def chunk_owner(self, chunk_id: str) -> str | None:
        return self._chunks.get(chunk_id)

    def unit_scope(self, gen: GenerationId, unit: Unit, scope: Scope | None) -> Scope | None:
        hidden = self.shadowed(gen, unit)
        if not hidden:
            return scope
        base = scope or Scope()
        return Scope(predicates=list(base.predicates), document_ids=base.document_ids, exclude_ids=sorted({*base.exclude_ids, *hidden}))

    def routed(self, gen: GenerationId, scope: Scope | None) -> list[tuple[Unit, Scope | None]]:
        """``(unit, unit scope)`` pairs for ``scope``. An explicit ``document_ids`` list is routed
        through the id directory: only the units holding those ids are opened, each with its ids."""
        predicates = list(scope.predicates) if scope is not None else []
        if scope is None or scope.document_ids is None:
            return [(u, self.unit_scope(gen, u, scope)) for u in self.units(gen, predicates)]
        by_unit = self.route_ids(gen, scope.document_ids)
        refs = [m for m in self.pinned_refs(gen) if m.unit_id in by_unit]
        out = []
        for unit in self.units(gen, predicates, refs):
            narrowed = Scope(predicates=predicates, document_ids=by_unit[unit.unit_id], exclude_ids=list(scope.exclude_ids))
            out.append((unit, narrowed))
        return out

    def route_ids(self, gen: GenerationId, ids: Sequence[str]) -> dict[str, list[str]]:
        """``{unit id: [doc ids]}``: log ids by their serving log unit, the others through the id
        directory (one indexed lookup per batch, filtered to the pinned units)."""
        wanted = list(dict.fromkeys(ids))
        owners = self.log_owners(gen, wanted)
        rest = [i for i in wanted if i not in owners]
        resolved = self.registry.directory.resolve_many(rest, self.view(gen)) if rest else {}
        by_unit: dict[str, list[str]] = {}
        for doc_id in wanted:
            unit = owners.get(doc_id)
            if unit is None:
                entry = resolved.get(doc_id)
                unit = None if entry is None else entry.unit_id
            if unit is not None:
                by_unit.setdefault(unit, []).append(doc_id)
        return by_unit

    def stratum_vocabulary(self, gen: GenerationId, units: Sequence[Unit]) -> tuple[int, float]:
        """``(rows, average_idf)`` of a query-time stratum over ``units`` (union vocabulary minus
        superseded/tombstoned rows), computed once per pinned unit set."""
        key = tuple((u.ref.partition_id, self.shadowed(gen, u)) for u in units)
        found = self._vocab.get(key)
        if found is not None:
            return found
        merged: Counter[str] = Counter()
        rows = 0
        for unit in units:
            hidden = self.shadowed(gen, unit)
            merged.update(unit.backend.lexical.vocabulary(unit.gen, hidden))
            rows += unit.backend.lexical.stratum_part(unit.gen, [], hidden)["n"]
        result = (rows, average_idf_of(rows, (merged[t] for t in sorted(merged))))
        with self._lock:
            if len(self._vocab) >= CACHE_ENTRIES:
                self._vocab.clear()
            self._vocab[key] = result
        return result


def adopted_backend(store: Any, ref: MemberRef, clock: Any) -> SqliteBackend:
    """A read-only ``SqliteBackend`` over an adopted store with the store's own recorded settings."""
    from ...core.config import Bm25Settings

    settings = ref.stats.get("store_backend") or {}
    bm25 = settings.get("bm25")
    return SqliteBackend(
        store,
        settings.get("vector_engine", "numpy"),
        float(settings.get("timeout_seconds", 30.0)),
        clock,
        settings.get("lexical_engine", "fts5"),
        Bm25Settings.model_validate(bm25) if bm25 is not None else None,
        readonly=True,
    )


class PartitionedCatalog:
    def __init__(self, backend: PartitionedSqliteBackend):
        self._b = backend

    def layer_check(self, gen: GenerationId, arm: str) -> list[str]:
        """Verify the attached layers serving ``arm`` (``PartitionedSqliteBackend.layer_check``)."""
        return self._b.layer_check(gen, arm)

    def put_documents(self, gen: GenerationId, documents: Sequence[Document], chunks: Sequence[Chunk]) -> None:
        raise BackendError(KIND, "put_documents", "a unified generation is composed from member partitions; build a member instead")

    def delete_generation(self, gen: GenerationId) -> None:
        self._b.delete_generation(gen)

    def get(self, gen: GenerationId, ids: Sequence[str]) -> list[Document]:
        """Through the id directory: one indexed lookup per batch, then each owning unit."""
        if not ids:
            return []
        b = self._b
        by_unit = b.route_ids(gen, list(ids))
        found: dict[str, Document] = {}
        for ref in b.pinned_refs(gen):
            wanted = by_unit.get(ref.unit_id)
            if wanted:
                unit = b.open(ref)
                b.stats[f"collection.unit_reads:{ref.unit_id}"] += 1
                found.update((d.id, d) for d in unit.backend.catalog.get(unit.gen, wanted))
        return [found[i] for i in ids if i in found]

    def _fanout(self, gen: GenerationId) -> list[tuple[Unit, frozenset[str]]]:
        return [(u, self._b.shadowed(gen, u)) for u in self._b.units(gen)]

    def _chunk_groups(self, gen: GenerationId, chunk_ids: Sequence[str]) -> list[tuple[Unit, frozenset[str], list[str]]]:
        """Chunk ids grouped by the unit that returned them (remembered from this backend's search
        results); ids never seen are probed on every pinned unit (an indexed ``IN`` read each)."""
        b = self._b
        known: dict[str, list[str]] = {}
        unknown: list[str] = []
        pinned = {unit_key(m): m for m in b.pinned_refs(gen)}
        for cid in dict.fromkeys(chunk_ids):
            owner = b.chunk_owner(cid)
            if owner is not None and owner in pinned:
                known.setdefault(owner, []).append(cid)
            else:
                unknown.append(cid)
        if unknown:
            # keep the caller's order: ``hit_parts`` returns the text of each document's *first*
            # chunk in that order, so a reordered id list would change which chunk carries text
            ordered = list(dict.fromkeys(chunk_ids))
            probe = set(unknown)
            out = []
            for u, h in self._fanout(gen):
                mine = probe | set(known.get(u.key, []))
                out.append((u, h, [cid for cid in ordered if cid in mine]))
            return out
        refs = [m for key, m in pinned.items() if key in known]
        return [(u, b.shadowed(gen, u), known[u.key]) for u in b.units(gen, (), refs)]

    def get_chunks(self, gen: GenerationId, chunk_ids: Sequence[str]) -> list[Chunk]:
        if not chunk_ids:
            return []
        found: dict[str, Chunk] = {}
        for unit, hidden, ids in self._chunk_groups(gen, chunk_ids):
            for chunk in unit.backend.catalog.get_chunks(unit.gen, ids):
                if chunk.document_id not in hidden:
                    found.setdefault(chunk.id, chunk)
        return [found[c] for c in chunk_ids if c in found]

    def hit_parts(self, gen: GenerationId, chunk_ids: Sequence[str]) -> tuple[dict[str, tuple], dict[str, tuple]]:
        """``extras.HitParts`` over the units (chunk ids are content hashes: each unit is probed with
        one indexed ``IN`` read; bounded by the number of hits)."""
        chunks: dict[str, tuple] = {}
        heads: dict[str, tuple] = {}
        if not chunk_ids:
            return chunks, heads
        for unit, hidden, ids in self._chunk_groups(gen, chunk_ids):
            part_chunks, part_heads = unit.backend.catalog.hit_parts(unit.gen, ids)
            for cid, ref in part_chunks.items():
                if ref[0] not in hidden:
                    chunks.setdefault(cid, ref)
            for doc_id, head in part_heads.items():
                if doc_id not in hidden:
                    heads.setdefault(doc_id, head)
        return chunks, heads

    def iter_scope(self, gen: GenerationId, scope: Scope | None, page: Page) -> PageResult[Document]:
        routed = self._b.routed(gen, scope)
        if len(routed) == 1 and (page.cursor is None or not page.cursor.startswith(CURSOR_PREFIX)):
            unit, unit_scope = routed[0]
            return unit.backend.catalog.iter_scope(unit.gen, unit_scope, page)
        size = page.size
        offsets: dict[str, int] = {}
        if page.cursor:
            if not page.cursor.startswith(CURSOR_PREFIX):
                raise BackendError(KIND, "page", "invalid cursor for a partitioned listing")
            try:
                offsets = {str(k): int(v) for k, v in json.loads(page.cursor[len(CURSOR_PREFIX):]).items()}
            except (ValueError, AttributeError) as exc:
                raise BackendError(KIND, "page", "invalid cursor") from exc
        streams: list[list[Document]] = []
        more: list[bool] = []
        for unit, unit_scope in routed:
            offset = offsets.get(unit.unit_id, 0)
            result = unit.backend.catalog.iter_scope(unit.gen, unit_scope, Page(cursor=str(offset) if offset else None, size=size))
            streams.append(list(result.items))
            more.append(result.next_cursor is not None)
        taken = [0] * len(streams)
        merged = heapq.merge(*[[(d.id, i, d) for d in s] for i, s in enumerate(streams)])
        items: list[Document] = []
        for _, index, document in merged:
            if len(items) >= size:
                break
            items.append(document)
            taken[index] += 1
        remaining = any(more[i] or taken[i] < len(streams[i]) for i in range(len(streams)))
        if not remaining:
            return PageResult(items=items, next_cursor=None)
        cursor = {unit.unit_id: offsets.get(unit.unit_id, 0) + taken[i] for i, (unit, _) in enumerate(routed)}
        return PageResult(items=items, next_cursor=CURSOR_PREFIX + json.dumps(cursor, sort_keys=True, separators=(",", ":")))

    def count_documents(self, gen: GenerationId) -> int:
        return sum(unit.backend.catalog.count_documents(unit.gen) - len(hidden) for unit, hidden in self._fanout(gen))

    def count_chunks(self, gen: GenerationId) -> int:
        total = 0
        for unit, hidden in self._fanout(gen):
            total += unit.backend.catalog.count_chunks(unit.gen)
            if hidden:
                ids = sorted(hidden)
                with unit.backend.catalog_db(unit.gen) as db:
                    for start in range(0, len(ids), 500):
                        batch = ids[start : start + 500]
                        total -= int(db.execute(f"SELECT count(*) FROM chunks WHERE document_id IN ({','.join('?' * len(batch))})", batch).fetchone()[0])
        return total


class PartitionedRecords:
    """Records over the pinned units (see the module docstring). ``values`` and ``exists`` answers are
    cached **per unit** (staging fix N4): a unit is immutable (a member partition by its content
    id, a log slice by its sealed id), so a unit's observed values for (field, ACL) never change; a
    new slice is scanned once and every other unit is served from the cache. The caches are bounded
    (least recently used out) and never keyed by generation. Unscoped value lists (no ACL) are also
    kept in ``<registry>/sql/unit_values_v1.sqlite`` (``UnitValuesFile``), so a restarted process
    does not rescan every unit on its first request; that file is a cache (a read or write error
    falls back to the scan, counted) and drops the rows of units no longer pinned."""

    def __init__(self, backend: PartitionedSqliteBackend):
        self._b = backend
        self._values: OrderedDict[tuple[str, str, str], tuple[int, tuple[str, ...]]] = OrderedDict()
        self._exists: OrderedDict[tuple[str, str, str, str], bool] = OrderedDict()
        self._lock = threading.Lock()
        root = getattr(getattr(backend, "registry", None), "root", None)
        self._file = UnitValuesFile(Path(root) / "sql" / UnitValuesFile.NAME, backend.stats) if root is not None else None

    def declared_fields(self, gen: GenerationId, sources: Sequence[str]) -> frozenset[str] | None:
        """The field names the pinned units able to hold a document of ``sources`` were sealed with
        (union of their own schemas; staging round 5, N13): the knowledge half plans over the
        knowledge members' fields only, as it does without a log member. ``None`` when a unit's own
        schema is unknown (a log unit is typed by the collection schema) or no unit qualifies: the
        caller keeps the collection schema."""
        units = self._b.units(gen, [Predicate(field="source", op="in", value=list(sources))])
        names: set[str] = set()
        for unit in units:
            if unit.log:
                return None
            schema = self._b.unit_schema(unit)
            if schema is None:
                return None
            names.update(f.name for f in schema.fields)
        return frozenset(names) if units else None

    def upsert(self, gen: GenerationId, records: Any, schema: MetadataSchema) -> None:
        raise BackendError(KIND, "upsert", "a unified generation is composed from member partitions; build a member instead")

    def execute(self, gen: GenerationId, scope: ScopeSpec, acl: ScopePredicates) -> ScopeResult:
        """Matched rows of every unit in scope, concatenated in id order, then ``scope_result``
        once: the semantics of one store over the same records."""
        b = self._b
        # pruned by the ACL only: the population (denominator) counts every record the principal may
        # read, whatever the scope predicates select
        units = b.units(gen, acl)
        if not units:
            return self._empty(scope)
        if len(units) == 1 and not b.shadowed(gen, units[0]):
            return units[0].backend.records.execute(units[0].gen, scope, acl)
        cap = matched_cap(b)
        rows, population = [], 0
        schema = applied = unsupported = None
        lacking: dict[str, list[str]] = {}
        reasons: dict[tuple[str, str], Any] = {}
        for unit in units:
            # a member whose own sealed schema lacks a field a positive predicate names holds no
            # record that can match it (a log-only field over the adopted support store): it
            # contributes its population only, and its "field not filterable" is not the
            # collection's (the collection declares the field)
            absent = [p.field for p in scope.predicates if not p.negate and p.op != "missing" and b.unit_lacks(unit, p.field)]
            if absent:
                lacking[unit.unit_id] = absent
                population += unit.backend.records.population(unit.gen, acl, b.shadowed(gen, unit))
                continue
            schema, matched, count, applied, unsupported = unit.backend.records.matched(unit.gen, scope, acl, b.shadowed(gen, unit), cap)
            for flag in unsupported:
                if not b.unit_lacks(unit, flag.constraint_text):
                    reasons.setdefault((flag.constraint_text, flag.reason), flag)
            rows.extend(matched)
            population += count
        check_matched(KIND, rows, cap)
        rows.sort(key=lambda r: r.id)
        if lacking:
            b.stats["collection.records_member_lacks_field"] += len(lacking)
        if schema is None or lacking or len(units) > 1:
            # the collection's semantics: its schema; unsupported = what any reading member could
            # not apply (a field the collection declares but a member lacks is not unsupported)
            schema = b.config.metadata_schema
            dropped = {field for field, _ in reasons}
            unsupported = list(reasons.values())
            applied = [p for p in scope.predicates if p.field not in dropped and unsupported_reason(schema, p) is None]
            unsupported += [UnsupportedConstraint(constraint_text=p.field, reason=unsupported_reason(schema, p)) for p in scope.predicates
                            if p.field not in dropped and unsupported_reason(schema, p) is not None]
        return scope_result(scope, rows, population, schema, applied, unsupported)

    def _empty(self, scope: ScopeSpec) -> ScopeResult:
        schema = self._b.config.metadata_schema
        applied, unsupported = [], []
        for predicate in scope.predicates:
            reason = unsupported_reason(schema, predicate)
            if reason is None:
                applied.append(predicate)
            else:
                unsupported.append(UnsupportedConstraint(constraint_text=predicate.field, reason=reason))
        return scope_result(scope, [], 0, schema, applied, unsupported)

    def exists(self, gen: GenerationId, field: str, value: Any, acl: ScopePredicates) -> bool:
        return any(self._unit_exists(u, field, value, acl) for u in self._b.units(gen, acl) if not self._b.unit_lacks(u, field))

    def _unit_exists(self, unit: Unit, field: str, value: Any, acl: ScopePredicates) -> bool:
        key = (unit.key, field, json.dumps(value, sort_keys=True, default=str), _acl_key(acl))
        with self._lock:
            found = self._exists.get(key)
            if found is not None:
                self._exists.move_to_end(key)
                self._b.stats["collection.exists_cache_hits"] += 1
                return found
        found = bool(unit.backend.records.exists(unit.gen, field, value, acl))
        self._b.stats["collection.exists_unit_reads"] += 1
        with self._lock:
            self._exists[key] = found
            while len(self._exists) > VALUE_CACHE_ENTRIES:
                self._exists.popitem(last=False)
        return found

    def values(self, gen: GenerationId, field: str, acl: ScopePredicates, limit: int) -> list[str]:
        """Union of per-unit observed values (each unit's first ``limit`` sorted values contain the
        union's first ``limit``), capped as today. A unit whose **own** sealed schema does not list
        ``field`` (an adopted store sealed before a log-only field such as ``tenant`` or ``category``
        joined the collection schema) holds no value of it and contributes nothing; whether the
        collection lists the field at all is the caller's check (``Engine.values_in``)."""
        seen: set[str] = set()
        units = self._b.units(gen, acl)
        if self._file is not None and not list(acl):
            self._file.retain(gen.generation, [u.key for u in units])
        for unit in units:
            schema = self._b.unit_schema(unit)
            spec = None if schema is None else schema.field(field)
            if schema is not None and (spec is None or not spec.listable):
                self._b.stats[f"collection.values_skipped:{field}"] += 1
                continue
            seen.update(self._unit_values(unit, field, acl, limit))
        return sorted(seen)[: max(0, limit)]

    def _unit_values(self, unit: Unit, field: str, acl: ScopePredicates, limit: int) -> tuple[str, ...]:
        """A unit's first ``limit`` sorted values of ``field`` (cached per unit, class doc). A cached
        list read with a larger limit serves any smaller one, and a list shorter than the limit it
        was read with is complete (serves any limit)."""
        key = (unit.key, field, _acl_key(acl))
        with self._lock:
            found = self._values.get(key)
            if found is not None and (limit <= found[0] or len(found[1]) < found[0]):
                self._values.move_to_end(key)
                self._b.stats["collection.values_cache_hits"] += 1
                return found[1][: max(0, limit)]
        stored = self._file.get(unit.key, self._field_key(field)) if self._file is not None and key[2] == "[]" else None
        if stored is not None and (limit <= stored[0] or len(stored[1]) < stored[0]):
            read_limit, values = stored
        else:
            read_limit, values = limit, tuple(unit.backend.records.values(unit.gen, field, acl, limit))
            self._b.stats["collection.values_unit_scans"] += 1
            if self._file is not None and key[2] == "[]":
                self._file.put(unit.key, self._field_key(field), read_limit, values)
        with self._lock:
            self._values[key] = (read_limit, values)
            self._values.move_to_end(key)
            while len(self._values) > VALUE_CACHE_ENTRIES:
                self._values.popitem(last=False)
        return values[: max(0, limit)]

    def _field_key(self, field: str) -> str:
        """The field and its collection declaration (a log unit's values are typed by it)."""
        spec = self._b.config.metadata_schema.field(field)
        declared = json.dumps(spec.model_dump(mode="json") if spec is not None else None, sort_keys=True, default=str)
        return f"{field}|{hashlib.sha256(declared.encode()).hexdigest()[:16]}"

    def count(self, gen: GenerationId) -> int:
        return sum(u.backend.records.count(u.gen) - len(self._b.shadowed(gen, u)) for u in self._b.units(gen))


class UnitValuesFile:
    """Unscoped per-unit value lists persisted across processes (``PartitionedRecords`` doc): one row
    per (unit key, field + declaration) with the limit it was read with. A cache only: a read or
    write error is counted and the caller scans."""

    NAME = "unit_values_v1.sqlite"

    def __init__(self, path: Path, stats: Counter[str]):
        self.path, self.stats = path, stats
        self._retained: str | None = None
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("CREATE TABLE IF NOT EXISTS unit_values(unit TEXT NOT NULL, field TEXT NOT NULL, lim INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(unit, field))")
        return db

    def get(self, unit: str, field: str) -> tuple[int, tuple[str, ...]] | None:
        if not self.path.is_file():
            return None
        try:
            with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)) as db:
                row = db.execute("SELECT lim, payload FROM unit_values WHERE unit=? AND field=?", (unit, field)).fetchone()
        except sqlite3.Error:
            self.stats["collection.values_file_unreadable"] += 1
            return None
        if row is None:
            return None
        self.stats["collection.values_file_hits"] += 1
        return int(row[0]), tuple(json.loads(row[1]))

    def put(self, unit: str, field: str, limit: int, values: tuple[str, ...]) -> None:
        try:
            with self._lock, closing(self._connect()) as db:
                db.execute("INSERT OR REPLACE INTO unit_values VALUES (?,?,?,?)", (unit, field, limit, json.dumps(list(values))))
                db.commit()
        except (sqlite3.Error, OSError):
            self.stats["collection.values_file_unwritable"] += 1

    def retain(self, generation: str, units: Sequence[str]) -> None:
        """Drop the rows of units the pin of ``generation`` does not hold (once per generation per
        process; a request on an older pin rescans what it misses)."""
        if self._retained == generation or not self.path.is_file():
            return
        try:
            with self._lock, closing(self._connect()) as db:
                db.execute("DELETE FROM unit_values WHERE unit NOT IN (SELECT value FROM json_each(?))", (json.dumps(list(units)),))
                db.commit()
            self._retained = generation
        except (sqlite3.Error, OSError):
            self.stats["collection.values_file_unwritable"] += 1


def _acl_key(acl: ScopePredicates) -> str:
    """A stable key of an ACL predicate list (the per-unit value caches)."""
    return json.dumps([p.model_dump(mode="json") for p in acl], sort_keys=True, default=str)


def ranked(unit: Unit, query: LexicalQuery, scope: Scope | None, k: int) -> list[ScoredChunk]:
    """A unit's best ``k`` chunks. One page holds at most ``MAX_PAGE`` items (``Page.size``): a
    larger ``k`` (the structured arm's text condition asks for ``MAX_TEXT_CHUNKS + 1``) is read page
    by page until ``k`` items or the end, never asked as one oversized page (staging B3)."""
    if k <= MAX_PAGE:
        return list(unit.backend.lexical.search(unit.gen, query, scope, k).items)
    out: list[ScoredChunk] = []
    cursor: str | None = None
    while len(out) < k:
        page = unit.backend.lexical.search(unit.gen, query, scope, k, Page(cursor=cursor, size=min(MAX_PAGE, k - len(out))))
        out.extend(page.items)
        cursor = page.next_cursor
        if cursor is None or not page.items:
            break
    return out[:k]


class StratifiedLexical:
    """Per-stratum lexical search (module docstring). Requires ``lexical_engine: bm25``."""

    def __init__(self, backend: PartitionedSqliteBackend):
        self._b = backend
        root = backend.root.lexical
        self.engine = root.engine
        self.settings = root.settings
        self.scores_query_repeats = root.scores_query_repeats

    @property
    def analyze(self):
        return self._b.root.lexical.analyze

    def unindexable(self, words: Sequence[str]) -> list[str]:
        return self._b.root.lexical.unindexable(words)

    def index(self, gen: GenerationId, chunks: Any, fields: Any) -> None:
        raise BackendError(KIND, "index", "a unified generation is composed from member partitions; build a member instead")

    def refresh(self, gen: GenerationId) -> None:
        self._b.view(gen)

    def count(self, gen: GenerationId) -> int:
        b = self._b
        total = sum(u.backend.lexical.count(u.gen) for u in b.units(gen) if not u.log)
        view = b.view(gen)
        for member in b.live_members_of(gen):
            derived = b.log_stratum(member).derived(view)
            total += sum(int(derived["masks"][r.unit_id][0].sum()) for r in derived["readers"])
        return total

    def _strata(self, gen: GenerationId, scope: Scope | None) -> dict[str, list[tuple[Unit, Scope | None]]]:
        strata: dict[str, list[tuple[Unit, Scope | None]]] = {}
        for unit, unit_scope in self._b.routed(gen, scope):
            strata.setdefault(unit.stratum, []).append((unit, unit_scope))
        return strata

    def search(self, gen: GenerationId, query: LexicalQuery, scope: Scope | None, k: int, page: Page | None = None) -> PageResult[ScoredChunk]:
        strata = self._strata(gen, scope)
        units = [pair for pairs in strata.values() for pair in pairs]
        if len(units) == 1 and not units[0][0].log and not self._b.shadowed(gen, units[0][0]):
            unit, unit_scope = units[0]
            result = unit.backend.lexical.search(unit.gen, query, unit_scope, k, page)
            self._b.remember(unit, result.items)
            return result
        offset, size = page_window(page, k)
        if self.engine != "bm25":
            raise BackendError(KIND, "lexical", "a partitioned collection needs lexical_engine: bm25 (fts5 is refused for new members)")
        rankings: dict[str, list[ScoredChunk]] = {}
        for name, pairs in strata.items():
            if self._b.is_live(name):
                rankings[name] = self.log_rank(gen, name, query, scope, k)
            else:
                rankings[name] = self._rank_stratum(gen, name, pairs, query, k)
            self._b.stats[f"collection.stratum:{name}"] += 1
        sources = {name: list(self._b.collection.members[name].sources) for name in rankings}
        fused = fuse_strata(rankings, k, sources=sources, quotas=self._b.collection.selection.source_quotas)
        limit = min(size, max(0, k - offset))
        window = fused[offset : offset + limit]
        more = offset + len(window) < len(fused)
        return PageResult(items=window, next_cursor=str(offset + len(window)) if more else None)

    def log_rank(self, gen: GenerationId, name: str, query: LexicalQuery, scope: Scope | None, k: int) -> list[ScoredChunk]:
        """The live log stratum's ranking (query-time statistics over its pinned units, §6.5)."""
        b = self._b
        stratum = b.log_stratum(name)
        view = b.view(gen)
        hits, info = stratum.search(view, query, scope, k, b.root.lexical.unindexable)
        for approximation in info.get("approximations", ()):
            b.stats[f"collection.approximation:{approximation}"] += 1
        units = info.get("hit_units", {})
        keys = {u.unit_id: u.key for u in b.units(gen, (), [r for r in b.pinned_refs(gen) if r.member == name])}
        b.remember_keys([(h.chunk_id, keys[units[h.chunk_id]]) for h in hits if units.get(h.chunk_id) in keys])
        return hits

    def _rank_stratum(self, gen: GenerationId, name: str, pairs: list[tuple[Unit, Scope | None]], query: LexicalQuery, k: int) -> list[ScoredChunk]:
        b = self._b
        if len(pairs) == 1 and not b.shadowed(gen, pairs[0][0]):
            unit, unit_scope = pairs[0]
            return list(b.remember(unit, ranked(unit, query, unit_scope, k)))
        model = b.collection.stats_of(name)
        if model == "member_rrf":
            lists = {u.unit_id: list(b.remember(u, ranked(u, query, s, k))) for u, s in pairs}
            return fuse_strata(lists, k)
        return self.query_time(gen, pairs, query, k)

    def query_time(self, gen: GenerationId, pairs: list[tuple[Unit, Scope | None]], query: LexicalQuery, k: int) -> list[ScoredChunk]:
        """B10: one stratum over several units, scored with statistics summed at query time."""
        b = self._b
        lexical = b.root.lexical
        words = lexical._query_tokens(query.text)
        if not words:
            return []
        units = [u for u, _ in pairs]
        n, total_tokens = 0, 0
        df: Counter[str] = Counter()
        for unit in units:
            part = unit.backend.lexical.stratum_part(unit.gen, words, b.shadowed(gen, unit))
            n += part["n"]
            total_tokens += part["tokens"]
            df.update(part["df"])
        if n == 0:
            return []
        _, average_idf = b.stratum_vocabulary(gen, units)
        distinct = list(dict.fromkeys(words))
        idf = dict(zip(distinct, stratum_idf(n, [df[w] for w in distinct], average_idf, float(self.settings.epsilon))))
        avgdl = total_tokens / n
        merged: list[ScoredChunk] = []
        for unit, unit_scope in pairs:
            merged.extend(b.remember(unit, unit.backend.lexical.rank_rows(unit.gen, query, unit_scope, k, idf=idf, avgdl=avgdl, excluded_docs=b.shadowed(gen, unit))))
        merged.sort(key=lambda h: (-h.score, h.chunk_id))
        return merged[:k]

    def doc_freqs(self, gen: GenerationId, words: Sequence[str]) -> list[int]:
        b = self._b
        totals = np.zeros(len(words), dtype=np.int64)
        for unit in b.units(gen):
            if not unit.log:
                totals += np.asarray(unit.backend.lexical.doc_freqs(unit.gen, words), dtype=np.int64)
        for member in b.live_members_of(gen):
            totals += np.asarray(b.log_stratum(member).doc_freqs(b.view(gen), words), dtype=np.int64)
        return [int(v) for v in totals]

    def phrase_rows(self, gen: GenerationId, words: Sequence[str], scope: Scope | None, max_candidates: int) -> list[ScoredChunk] | None:
        b = self._b
        rows: list[ScoredChunk] = []
        logs: set[str] = set()
        for unit, unit_scope in b.routed(gen, scope):
            if unit.log:
                logs.add(unit.stratum)
                continue
            found = unit.backend.lexical.phrase_rows(unit.gen, words, unit_scope, max_candidates)
            if found is None:
                return None
            b.remember(unit, found)
            rows.extend(found)
            if len(rows) > max_candidates:
                return None
        for member in sorted(logs):
            found_log = b.log_stratum(member).phrase_rows(b.view(gen), words, scope, max_candidates - len(rows))
            if found_log is None:
                return None
            keys = {u.unit_id: u.key for u in b.units(gen, (), [r for r in b.pinned_refs(gen) if r.member == member])}
            b.remember_keys([(hit.chunk_id, keys[unit_id]) for hit, unit_id in found_log if unit_id in keys])
            rows.extend(hit for hit, _ in found_log)
        return rows


class MemberStratumScorer:
    """``core.types_members.StratumScorer`` for one member stratum of a partitioned collection: the
    member's pinned units ranked as ``StratifiedLexical`` ranks them (one unit: today's search;
    several: query-time statistics minus superseded/tombstoned rows; ``member_rrf``: rank fusion).
    The live log path (M2a) plugs its slices in as further units of the log stratum."""

    def __init__(self, backend: PartitionedSqliteBackend, stratum: str, scope: Scope | None = None):
        if stratum not in backend.collection.members:
            raise ValueError(f"no member {stratum!r} in collection.members")
        self._b, self.stratum, self.scope = backend, stratum, scope

    def rank(self, query: str, k: int, view: PinnedView) -> Any:
        from ...arms.stratified import ranking
        from ...ingestion.release import generation_id

        if view.generation is None:
            return ranking(self.stratum, [], [])
        manifest = self._b.registry.generations.manifest(view.generation)
        if manifest is None:
            raise BackendError(KIND, "generation", f"unknown unified generation {view.generation}")
        gen = generation_id(manifest)
        if self._b.is_live(self.stratum):
            hits, info = self._b.log_stratum(self.stratum).search(view, LexicalQuery(kind="terms", text=query), self.scope, k, self._b.root.lexical.unindexable)
            return ranking(self.stratum, hits, list(info.get("units", ())), info.get("approximations", ()))
        refs = [m for m in view.members if m.member == self.stratum]
        pairs = [(u, self._b.unit_scope(gen, u, self.scope)) for u in self._b.units(gen, list(self.scope.predicates) if self.scope else [], refs)]
        if not pairs:
            return ranking(self.stratum, [], [])
        hits = self._b.lexical._rank_stratum(gen, self.stratum, pairs, LexicalQuery(kind="terms", text=query), k)
        return ranking(self.stratum, hits, [u.unit_id for u, _ in pairs])


class MergedVector:
    """Exact top-k over each unit's flat index, merged by score (ties as one store breaks them)."""

    def __init__(self, backend: PartitionedSqliteBackend):
        self._b = backend

    def upsert(self, gen: GenerationId, ids: Any, vectors: Any, namespace: str = "default") -> None:
        raise BackendError(KIND, "upsert", "a unified generation is composed from member partitions; build a member instead")

    def refresh(self, gen: GenerationId) -> None:
        self._b.view(gen)

    def count(self, gen: GenerationId, namespace: str = "default") -> int:
        b = self._b
        total = sum(u.backend.vector.count(u.gen, namespace) for u in b.units(gen) if not u.log)
        view = b.view(gen)
        for member in b.live_members_of(gen):
            total += sum(len(r.embed()[0]) for r in b.log_stratum(member).readers(view))
        return total

    def pending(self, gen: GenerationId) -> list[str]:
        """Log units of the request whose vectors were pending at its last dense read."""
        return list(self._b.pending.get(gen.generation, ()))

    def search(self, gen: GenerationId, vector: np.ndarray, scope: Scope | None, k: int, namespace: str = "default") -> list[ScoredChunk]:
        b = self._b
        routed = b.routed(gen, scope)
        if len(routed) == 1 and not routed[0][0].log:
            unit, unit_scope = routed[0]
            return list(b.remember(unit, unit.backend.vector.search(unit.gen, vector, unit_scope, k, namespace)))
        # A store's vector rows follow its corpus order (document id, then chunk order), and its exact
        # top-k breaks score ties by row: merging by (score, document id, rank inside the unit)
        # reproduces the order one store over the same documents returns.
        merged: list[tuple[float, str, int, ScoredChunk]] = []
        logs: set[str] = set()
        for unit, unit_scope in routed:
            if unit.log:
                logs.add(unit.stratum)
                continue
            for rank, hit in enumerate(b.remember(unit, unit.backend.vector.search(unit.gen, vector, unit_scope, k, namespace))):
                merged.append((-hit.score, hit.document_id, rank, hit))
        pending: list[str] = []
        for member in sorted(logs):
            if namespace != "default":
                raise BackendError(KIND, "vector", f"vector namespace {namespace!r} is not served over the live log stratum")
            found, waiting = b.log_stratum(member).vector_search(b.view(gen), vector, scope, k)
            pending.extend(waiting)
            keys = {u.unit_id: u.key for u in b.units(gen, (), [r for r in b.pinned_refs(gen) if r.member == member])}
            b.remember_keys([(hit.chunk_id, keys[unit_id]) for hit, unit_id in found if unit_id in keys])
            for rank, (hit, _unit) in enumerate(found):
                merged.append((-hit.score, hit.document_id, rank, hit))
        if logs:
            with b._lock:
                if len(b.pending) >= CACHE_ENTRIES:
                    b.pending.clear()
                b.pending[gen.generation] = tuple(pending)
            for unit_id in pending:
                b.stats[f"collection.dense_pending:{unit_id}"] += 1
        merged.sort(key=lambda item: item[:3])
        return [item[3] for item in merged[:k]]



class PartitionedGraph:
    """The graph facet of a partitioned collection (M1b): communities, entity lookups and mentions
    served from the adopted member a ``communities_imported`` layer binds (today's community arm
    over today's store files). Native member graphs and stitched traversal arrive with M5: a graph
    read without such a layer is refused (never answered from one member as if it were all)."""

    def __init__(self, backend: PartitionedSqliteBackend):
        self._b = backend

    def _unit(self, gen: GenerationId) -> Unit:
        b = self._b
        names = [layer.name for layer in b.collection.layers if layer.kind == "communities_imported"]
        if len(names) != 1:
            raise BackendError(
                KIND,
                "graph",
                "the graph/community facet of a partitioned collection is served from one communities_imported layer bound to an adopted member (M1b); native member graphs land with M5",
            )
        return b.layer_unit(gen, names[0])

    def upsert(self, gen: GenerationId, entities: Any, relations: Any, memberships: Any) -> None:
        raise BackendError(KIND, "upsert", "a unified generation is composed from member partitions; build a member instead")

    def put_communities(self, gen: GenerationId, communities: Any) -> None:
        raise BackendError(KIND, "put_communities", "a unified generation is composed from member partitions; build a member instead")

    def traverse(self, gen: GenerationId, spec: Any, scope: Scope | None) -> Any:
        raise BackendError(KIND, "traverse", "graph traversal over a partitioned collection reads an attached graph layer (graph_attach) or member graphs (M5)")

    def find_entities(self, gen: GenerationId, tokens_: Sequence[str], scope: Scope | None) -> Any:
        unit = self._unit(gen)
        return unit.backend.graph.find_entities(unit.gen, tokens_, self._b.unit_scope(gen, unit, scope))

    def mentions(self, gen: GenerationId, entity_ids: Sequence[str], scope: Scope | None, limit: int) -> Any:
        unit = self._unit(gen)
        found = unit.backend.graph.mentions(unit.gen, entity_ids, self._b.unit_scope(gen, unit, scope), limit)
        chunk_ids = [m.span.chunk_id for m in found if getattr(getattr(m, "span", None), "chunk_id", None)]
        if chunk_ids:
            with self._b._lock:
                for chunk_id in chunk_ids:
                    self._b._chunks[chunk_id] = unit.ref.partition_id
        return found

    def memberships(self, gen: GenerationId) -> Any:
        unit = self._unit(gen)
        return unit.backend.graph.memberships(unit.gen)

    def layers(self) -> list[Any]:
        """The served ``communities.layers`` as ``CommunityLayerReader`` values (M1b: imported)."""
        from ...communities.imported import ImportedCommunityLayer

        return [ImportedCommunityLayer(layer.name, tuple(layer.sources), self) for layer in self._b.config.communities.layers if layer.strategy == "imported"]

    def communities(self, gen: GenerationId) -> Any:
        unit = self._unit(gen)
        hidden = self._b.shadowed(gen, unit)
        found = unit.backend.graph.communities(unit.gen)
        if not hidden:
            return found
        return [c.model_copy(update={"document_ids": [d for d in c.document_ids if d not in hidden]}) for c in found]


class PartitionedProjection:
    """The ``relational`` facet (typed projection files) of a partitioned collection (M1b).

    One member's projection — the adopted store's own files or a single write-path member's — is
    served as that store serves it (live log members carry no projection facet: their own SQL tables
    join the surface through ``sql_surface/members.py``, M4 completion) (``store_generation`` maps the unified generation to the member's,
    so ``ProjectionCatalog.load`` and the SQL route read today's sealed manifest and file). Several
    projection-bearing members are one SQL surface only through the multi-source setup
    (``sql_surface/setup.py``, M4 completion): ``attachments`` lists their files for it, and a
    single-file read is refused (never one member answered as if it were the collection)."""

    def __init__(self, backend: PartitionedSqliteBackend):
        self._b = backend

    def units(self, gen: GenerationId) -> list[Unit]:
        """Pinned units with a projection facet (unit-id order); opening verifies their digests."""
        return [self._b.member_unit(gen, ref.unit_id) for ref in self._b.view(gen).members if "projection" in ref.facets]

    def _unit(self, gen: GenerationId) -> Unit:
        units = self.units(gen)
        if len(units) != 1:
            detail = (
                f"{len(units)} members carry a typed projection in generation {gen.prefix()}: one SQL surface over several members needs the multi-source setup (M4)"
                if units
                else f"no member of generation {gen.prefix()} carries a typed projection"
            )
            raise BackendError("projection", "open", detail)
        return units[0]

    def store_generation(self, gen: GenerationId) -> GenerationId:
        """The generation the served projection files were sealed for."""
        return self._unit(gen).gen

    def put_file(self, gen: GenerationId, name: str, data: bytes) -> None:
        raise BackendError("projection", "put_file", "a unified generation is composed from member partitions; build a member instead")

    def read_file(self, gen: GenerationId, name: str) -> bytes | None:
        unit = self._unit(gen)
        return unit.backend.relational.read_file(unit.gen, name)

    def file_names(self, gen: GenerationId) -> list[str]:
        unit = self._unit(gen)
        return unit.backend.relational.file_names(unit.gen)

    def connect_readonly(self, gen: GenerationId, name: str) -> Any:
        unit = self._unit(gen)
        return unit.backend.relational.connect_readonly(unit.gen, name)

    def forget(self, gen: GenerationId) -> None:
        return None

    @property
    def partitioned_backend(self) -> PartitionedSqliteBackend:
        """The collection backend (``sql_surface/members.py`` reads the pinned log units through it)."""
        return self._b

    def projection_sources(self, gen: GenerationId) -> list[str]:
        """The ``source`` values of the projection member when the pinned view holds other members
        too (the projection's grounding and samples then read that member only); ``[]`` when the
        projection member is the whole view (nothing to scope: today's resolver)."""
        unit = self._unit(gen)
        view = self._b.view(gen)
        others = any(r.unit_id != unit.unit_id for r in view.members) or any(view.slices.values())
        return list(unit.ref.sources) if others else []

    def attachments(self, gen: GenerationId) -> list[dict[str, str]]:
        """Read-only attachments of every pinned member's projection files (the M4 setup input,
        ``projection.store.attach_list``): alias ``<member>_<n>_<file stem>``, absolute path,
        role ``member`` and the unit id."""
        out: list[dict[str, str]] = []
        for index, unit in enumerate(self.units(gen)):
            for item in unit.backend.relational.attachments(unit.gen):
                out.append({"alias": f"{unit.ref.member}_{index}_{item['alias']}"[:64], "path": item["path"], "role": "member", "unit": unit.unit_id})
        return out
