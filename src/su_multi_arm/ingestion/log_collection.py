"""The live log member inside a partitioned collection (docs/plans/unified_merge.md §6.5–§6.7; lane
LB wiring of M2a into the M1a/M1b partition core).

A member declared with ``cadence: minutes`` (``collection.members.<id>``) is the **live log
member**: its closed days are archive day/month members and its catalog kinds one catalog member
(unit format, ``ingestion/log_members.py``); its live window is served from the provisional slices
of its ``LogSliceLedger`` (``ingestion/log_slices.py``). ``LiveLogs`` is built from the
configuration and owns, per live member:

* the **ledger** — root ``<member registry>/slices/<member>``; the member's ``PartitionRecipe``
  (``ingestion/recipes.partition_recipe`` plus the bm25 analyzer options of the catalog backend, so
  slices analyse text exactly as the stratum's queries do); ``embed_fields``, ``catalog_kinds``,
  ``collection.slices``, the metadata schema; the collection's **id directory** (LA) and the slice
  pins stored next to the unified generation registry; ``catalog`` = the content shas of the
  catalog member of the **active** generation (the N3 check: a known catalog document is not
  resealed);
* the **text-hash vector store** — ``<member registry>/vectors/<member>``, pinned to the collection's
  embedding space;
* the **serving stratum** (``backends/sqlite/log_units.LogStratum``).

**Request pin (§6.7).** ``pin(gen, manifest)`` reads every live ledger's version vector once, drops
the days the generation already serves from an archive member (one tier per day), and — when any
slice is pinned — returns a **request generation id**: the same dataset, index format and
fingerprint with ``generation`` = sha256(unified generation + slice vector)[:32]. The engine passes
that id to every facet unchanged (no engine change), and ``view(id)`` gives the pinned view
(members, layers, slices). Equal vectors give equal ids, so caches keyed by the id are shared
until a slice changes. The pinned slices are held by **slice pins** (``SlicePins`` rows in the
generation registry's pin store) for a lease of ``budgets.deadline_seconds`` + 60 s after the last
request that pinned them (renewed per request, released when it lapses or by ``release``); a
session may hold its pin for its lifetime (``hold``/``release``). Slice ``gc`` deletes a retired
slice only when no pin holds it and its ``gc_grace_seconds`` (≥ the deadline) has passed.

**Close (§6.7 compose).** ``close_day(member, day)`` builds the archive day member and (when it
changed) the catalog member, registers their ids (the day member replaces the day's slices), composes
**one** generation = the active generation's members with the new day member and catalog version,
and only then calls ``release_day`` (the day's slices retire; requests pinned before keep reading
them until their pins lapse). Compose is O(members), references only; the compose seconds are
returned.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..core.types import GenerationId
from ..core.types_members import PinnedView

LEASE_MARGIN_SECONDS = 60.0
MAX_IDLE_PINS = 256


def live_members(config: Any) -> list[str]:
    """Members served by the live log path (``cadence: minutes``), sorted."""
    collection = getattr(config, "collection", None)
    if collection is None:
        return []
    return sorted(name for name, member in collection.members.items() if member.cadence == "minutes")


def live_refusals(config: Any) -> list[tuple[str, str]]:
    """``(config path, reason)`` for live log members this release cannot serve (fail closed)."""
    found: list[tuple[str, str]] = []
    for name in live_members(config):
        member = config.collection.members[name]
        path = f"collection.members.{name}"
        if len(member.sources) != 1:
            found.append((f"{path}.sources", "a live log member carries exactly one source (one slice ledger per source)"))
        if member.key != "day":
            found.append((f"{path}.key", "a live log member is keyed by day (archive day/month members, slices per day)"))
        if member.adopt is not None:
            found.append((path, "a live log member is not adopted"))
        if config.collection.stats_of(name) != "query_time":
            found.append((f"collection.lexical.{name}.stats", "the live log stratum uses query-time statistics (member_rrf is never used for live slices)"))
    return found


def member_recipe(config: Any, member: str) -> Any:
    """The ``PartitionRecipe`` of a live log member: ``partition_recipe`` plus the catalog backend's
    bm25 analyzer options (``content.bm25``: lowercase, stemming, include_title)."""
    from .recipes import partition_recipe

    recipe = partition_recipe(config, member)
    spec = config.backends[config.storage.catalog]
    bm25 = spec.bm25
    content = {**recipe.content, "bm25": {"lowercase": bm25.lowercase, "stemming": bm25.stemming, "include_title": bm25.include_title}, "live_log_unit": 1}
    return recipe.model_copy(update={"content": content})


def request_id(generation: str, vector: dict[str, dict[date, tuple[str, ...]]]) -> str:
    body = {m: {d.isoformat(): list(ids) for d, ids in sorted(days.items())} for m, days in sorted(vector.items())}
    return hashlib.sha256(json.dumps({"generation": generation, "slices": body}, sort_keys=True).encode()).hexdigest()[:32]


class LiveLogs:
    """The live log members of one partitioned configuration (module doc). ``registry`` is the
    collection's ``MemberRegistry``. Constructing it opens nothing; ledgers, stores and strata are
    opened on first use."""

    def __init__(self, config: Any, registry: Any, *, clock: Callable[[], float] = time.time):
        self.config = config
        self.registry = registry
        self.members = live_members(config)
        self.clock = clock
        self.lease_seconds = float(config.budgets.deadline_seconds) + LEASE_MARGIN_SECONDS
        self._ledgers: dict[str, Any] = {}
        self._strata: dict[str, Any] = {}
        self._stores: dict[str, Any] = {}
        self._pins_store: Any = None
        self._lock = threading.RLock()
        self._views: OrderedDict[str, PinnedView] = OrderedDict()  # request id -> pinned view
        self._leases: dict[str, float] = {}  # holder (request id or session) -> lease expiry
        self._held: dict[str, tuple[str, ...]] = {}  # holder -> slice ids it holds
        self._tokens: dict[str, Any] = {}  # slice id -> registry pin token of this process
        self._catalog: tuple[str, dict[str, str]] | None = None
        from .pins import holder_id

        self.holder = holder_id()

    # --- components ---------------------------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self.registry.root

    def slice_pins(self) -> Any:
        if self._pins_store is None:
            from .pins import slice_pins

            self._pins_store = slice_pins(self.registry.generations, self.clock)
        return self._pins_store

    def ledger(self, member: str) -> Any:
        """The member's ``LogSliceLedger`` built from the configuration (module doc)."""
        with self._lock:
            found = self._ledgers.get(member)
            if found is not None:
                return found
            if member not in self.members:
                raise ValueError(f"member {member!r} is not a live log member (cadence: minutes)")
            from .log_slices import LogSliceLedger

            declared = self.config.collection.members[member]
            ledger = LogSliceLedger(
                self.root / "slices" / member,
                member=member,
                source=declared.sources[0],
                recipe=member_recipe(self.config, member),
                settings=self.config.collection.slices,
                embed_fields=declared.embed_fields,
                catalog_kinds=declared.all_catalog_kinds(),
                catalog=lambda: self.catalog_shas(member),
                directory=self.registry.directory,
                pins=self.slice_pins(),
                clock=self.clock,
                metadata_schema=self.config.metadata_schema,
            )
            ledger.on_registered.append(lambda ref, folder, m=member: self._registered(m, ref, folder))
            self._ledgers[member] = ledger
            return ledger

    def vector_store(self, member: str) -> Any:
        with self._lock:
            found = self._stores.get(member)
            if found is None:
                from .recipes import embedding_space_ref
                from .text_vectors import TextVectorStore

                found = self._stores[member] = TextVectorStore(self.root / "vectors" / member, embedding_space_ref(self.config))
            return found

    def stratum(self, member: str, *, stats: Any = None, max_matched: int = 10_000) -> Any:
        """The serving ``LogStratum`` of a live member."""
        with self._lock:
            found = self._strata.get(member)
            if found is not None:
                return found
            from ..backends.sqlite.log_units import LogStratum

            ledger = self.ledger(member)
            spec = self.config.backends[self.config.storage.catalog]
            found = self._strata[member] = LogStratum(ledger, self.root, store=self.vector_store(member), schema=self.config.metadata_schema,
                                                      timeout=spec.timeout_seconds, stats=stats, max_matched=max_matched)
            return found

    def _registered(self, member: str, ref: Any, folder: Path) -> None:
        """Warm a newly compacted unit in this process (§6.7: the first dense request after a base
        compaction must not load a cold matrix). Refresh slices are small and warm on first read."""
        if ref.level in {"base", "day", "hourly"} and member in self._strata:
            self._strata[member].warm(ref.slice_id, folder)

    # --- the catalog check (N3) ----------------------------------------------------------------------

    def current_catalog(self, member: str) -> Any:
        """``MemberRef`` of the member's catalog unit in the **active** unified generation, or None."""
        from .compose import members_of

        manifest = self.registry.generations.current()
        if manifest is None:
            return None
        return next((ref for ref in members_of(manifest) if ref.member == member and ref.tier == "catalog"), None)

    def catalog_shas(self, member: str) -> dict[str, str]:
        """``{id: content sha}`` of the active catalog member (cached by its partition id)."""
        from .log_members import catalog_shas

        ref = self.current_catalog(member)
        if ref is None:
            return {}
        cached = self._catalog
        if cached is not None and cached[0] == ref.partition_id:
            return cached[1]
        shas = catalog_shas(self.registry.unit_dir(ref), self.ledger(member))
        self._catalog = (ref.partition_id, shas)
        return shas

    # --- the request pin ------------------------------------------------------------------------------

    def request_view(self, manifest: dict[str, Any]) -> PinnedView:
        """The generation's view plus every live member's slice vector minus archived days."""
        from .compose import view_of

        base = view_of(manifest)
        slices: dict[str, dict[date, tuple[str, ...]]] = {}
        for member in self.members:
            archived = [ref for ref in base.members if ref.member == member]
            vector = self.ledger(member).vector_for(lambda day, refs=archived: any(r.covers(day) for r in refs))
            if vector:
                slices[member] = vector
        return base.model_copy(update={"slices": slices}) if slices else base

    def pin(self, gen: GenerationId, manifest: dict[str, Any]) -> GenerationId:
        """The request generation id of ``gen`` (``gen`` itself when no slice is pinned); the slices are
        held for the lease (module doc)."""
        view = self.request_view(manifest)
        if not view.slices:
            return gen
        pin_id = request_id(gen.generation, view.slices)
        with self._lock:
            self._views[pin_id] = view
            self._views.move_to_end(pin_id)
            self._lease(pin_id, tuple(s for days in view.slices.values() for ids in days.values() for s in ids), self.clock() + self.lease_seconds)
        return gen.model_copy(update={"generation": pin_id})

    def view(self, generation: str) -> PinnedView | None:
        """The pinned view of a request generation id (``None``: not a request id of this process)."""
        with self._lock:
            return self._views.get(generation)

    def base_generation(self, generation: str) -> str:
        view = self.view(generation)
        return generation if view is None or view.generation is None else view.generation

    def hold(self, holder: str, generation: str, ttl: float) -> None:
        """A session holds the slices of a request generation for ``ttl`` seconds from now (renewed
        by each turn: the session's idle lifetime). The slices of its earlier turns stay held with
        them until the session is released or its lease lapses (an answer may cite them again)."""
        view = self.view(generation)
        if view is None:
            return
        key = f"session:{holder}"
        with self._lock:
            slices = tuple(s for days in view.slices.values() for ids in days.values() for s in ids)
            if key in self._leases and self._leases[key] > self.clock():
                slices = tuple(dict.fromkeys((*self._held.get(key, ()), *slices)))
            self._lease(key, slices, self.clock() + ttl)

    def release(self, holder: str) -> None:
        """End a request's or a session's hold now (its slices stay pinned while another holder needs them)."""
        with self._lock:
            key = holder if holder in self._leases else f"session:{holder}"
            if key in self._leases:
                self._leases[key] = self.clock()
            self._expire()

    def release_session(self, session_id: str) -> None:
        """End a session's hold now (closed or expired session)."""
        with self._lock:
            key = f"session:{session_id}"
            if key in self._leases:
                self._leases[key] = self.clock()
            self._expire()

    def held_slices(self) -> set[str]:
        with self._lock:
            return set(self._tokens)

    def _lease(self, holder: str, slices: tuple[str, ...], expiry: float) -> None:
        self._leases[holder] = max(expiry, self._leases.get(holder, 0.0))
        self._held[holder] = slices
        pins = self.slice_pins()
        until = self._leases[holder]
        # the registry pin outlives the lease (another process's gc must see it until the lease ends)
        ttl = max(self.lease_seconds * 2, until - self.clock() + self.lease_seconds)
        for slice_id in slices:
            token = self._tokens.get(slice_id)
            # one registry write per slice per lease period, not per request
            if token is None or token.expires_at < until:
                self._tokens[slice_id] = pins.pin(slice_id, self.holder, ttl)
        self._expire()

    def _expire(self) -> None:
        now = self.clock()
        active = {h for h, until in self._leases.items() if until > now}
        needed = {s for h in active for s in self._held.get(h, ())}
        for holder in [h for h in self._leases if h not in active]:
            self._leases.pop(holder, None)
            self._held.pop(holder, None)
        pins = self.slice_pins()
        for slice_id in [s for s in self._tokens if s not in needed]:
            pins.release(self._tokens.pop(slice_id))
        idle = [v for v in self._views if v not in active]
        for pin_id in idle[: max(0, len(idle) - MAX_IDLE_PINS)]:
            self._views.pop(pin_id, None)

    # --- close: archive members, one compose, release ------------------------------------------------

    def close_day(self, member: str, day: date, *, as_of: datetime | None = None, compose: Callable[[list[Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
        """Close ``day`` of a live member (module doc). Returns the closed members, the composed
        generation and the measured compose seconds. ``compose`` defaults to
        ``ingestion.compose.compose`` over the active generation's members plus the new ones."""
        from .compose import compose as compose_generation
        from .compose import members_of
        from .log_members import close_day, register_closed

        ledger = self.ledger(member)
        active = self.registry.generations.current()
        current = list(members_of(active)) if active is not None else []
        previous = next((r for r in current if r.member == member and r.tier == "catalog"), None)
        closed = close_day(ledger, day, self.root, previous_catalog=None if previous is None else self.registry.unit_dir(previous), as_of=as_of)
        register_closed(self.registry.directory, ledger, closed)
        members = [r for r in current if not (closed.catalog is not None and r.unit_id == closed.catalog.unit_id)]
        members.append(closed.day)
        if closed.catalog is not None:
            members.append(closed.catalog)
        started = time.perf_counter()
        manifest = compose(members) if compose is not None else compose_generation(self.config, self.registry, members, expected=None if active is None else active["generation"])
        seconds = time.perf_counter() - started
        retired = ledger.release_day(day)
        return {"day": closed.day, "catalog": closed.catalog, "generation": manifest["generation"], "compose_seconds": seconds, "retired_slices": retired}
