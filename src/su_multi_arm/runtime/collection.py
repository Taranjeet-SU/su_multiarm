"""The collection view of an engine (docs/plans/unified_merge.md §6, §10.7.2).

``CollectionView.for_config(config)`` is the one seam the engine constructs (``Engine.view``, R6).

* ``SingleMemberView`` (every v2 configuration): today's one dataset served by today's one
  generation; the view adds nothing to any request path.
* ``PartitionedView`` (M1a, lane LA; ``collection.mode: partitioned``): the request pin is the
  member set of the unified generation (``ingestion/compose.py``); the engine replaces its sqlite
  backend with the partitioned facets (``backends/sqlite/members.py``), which route
  ``get_document``, listings, records and values through the members. The live log path (M2a, lane
  LB) adds the slice version vector to ``pin``: with a live log member (``cadence: minutes``,
  ``ingestion/log_collection.py``) the request pin is the generation **plus** the not-yet-archived
  days' slices, held by slice pins for the request (or a session) and released after.

M1b serves read-only adopted members (``collection.members.<id>.adopt``), the attached layers
``graph_attach``, ``segment_sidecar`` and ``communities_imported`` (``collection.layers``), imported
``communities.layers``, and — over those — the graph, segment, community and typed_query arms and
one member's typed projection.

Staging fixes (2026-11-10): the v2 ``logs`` block (log intents, issue history, liveness, episodes,
shared errors) is served over the live log member (``log_refusals``; ``runtime/member_logs.py``),
and the typed projection of one member is served next to it (the log member's own SQL tables join
through ``sql_surface/members.py``).

What the engine does not serve yet is refused here with a ``ConfigError`` naming the key
(``unserved_blocks``: other layer kinds and community strategies;
``partitioned_refusals``: features without a partition contract yet): the engine never serves a
partitioned collection as if its members were one monolithic store.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import ConfigError
from ..core.types_members import PinnedView

SINGLE_MEMBER = "default"


def unserved_blocks(config: Any) -> list[str]:
    """Schema v3 blocks set in ``config`` that this release validates but does not serve.

    Served: ``collection`` members (built: M1a; adopted read-only: M1b), the layer kinds of
    ``ingestion.layers.SERVED_KINDS`` (M1b) and ``communities.layers`` with ``strategy: imported``
    stored in a ``communities_imported`` layer (M1b). Not yet: other layer kinds and community
    strategies (shared errors M2a/M2b, overlay M5, unified epoch M6). ``crosssource`` is served
    since M2b-2 (customer grounding through the pinned registry, the two-halves planner, labelled
    link display through the pinned link store)."""
    from ..ingestion.layers import SERVED_KINDS

    found = []
    collection = getattr(config, "collection", None)
    if collection is not None:
        found.extend(f"collection.layers.{layer.name}" for layer in collection.layers if layer.kind not in SERVED_KINDS)
    stored = {layer.name for layer in collection.layers if layer.kind == "communities_imported"} if collection is not None else set()
    for layer in getattr(getattr(config, "communities", None), "layers", None) or []:
        if layer.strategy != "imported" or layer.layer not in stored:
            found.append(f"communities.layers.{layer.name}")
    return found


def _layers(config: Any, kind: str) -> list[Any]:
    return [layer for layer in config.collection.layers if layer.kind == kind]


def _same_path(a: Any, b: Any) -> bool:
    from pathlib import Path

    return Path(a).resolve() == Path(b).resolve()


def arm_refusals(config: Any, prefix: str, arms: Any) -> list[tuple[str, str]]:
    """The graph, community, segment and typed_query arms over a partitioned collection (M1b): each
    reads an attached layer or one member's projection, never member stores as if they were one."""
    found: list[tuple[str, str]] = []
    if arms.graph is not None:
        bound = [layer for layer in _layers(config, "graph_attach") if layer.backend == arms.graph.backend]
        if not bound:
            found.append((f"{prefix}.arms.graph", "the graph arm over a partitioned collection reads a graph_attach layer naming its backend (M1b); native member graphs land with M5"))
    if arms.community is not None:
        if len(_layers(config, "communities_imported")) != 1 or arms.community.backend != config.storage.catalog:
            found.append((f"{prefix}.arms.community", "the community arm over a partitioned collection reads one communities_imported layer through the catalog backend (M1b); unified communities land with M6"))
    if arms.segment is not None:
        if not any(layer.path is not None and _same_path(layer.path, arms.segment.path) for layer in _layers(config, "segment_sidecar")):
            found.append((f"{prefix}.arms.segment", "the segment arm over a partitioned collection reads a segment_sidecar layer pinning its path (M1b)"))
    if arms.typed_query is not None and not config.projection.enabled:
        found.append((f"{prefix}.arms.typed_query", "the typed_query arm needs projection.enabled"))
    return found


# Engine features a partitioned collection does not serve yet: each needs its partition contract
# first (log intents/history: M2a/M2b; several members' projections as one SQL surface: M4; native
# member graphs: M5). The engine refuses them instead of serving them over one member.
def partitioned_refusals(config: Any) -> list[tuple[str, str]]:
    """``(config path, reason)`` for every setting the partition core cannot serve yet."""
    found: list[tuple[str, str]] = []
    storage, backends = config.storage, config.backends
    slot_names = {storage.catalog, storage.records}
    pipelines = {"pipeline": config.pipeline, **{f"pipelines.{name}": p for name, p in config.pipelines.items()}}
    for prefix, pipeline in pipelines.items():
        arms = pipeline.arms
        for name in ("lexical", "dense", "structured", "exact_id"):
            arm = getattr(arms, name)
            if arm is not None and getattr(arm, "backend", None):
                slot_names.add(arm.backend)
        found.extend(arm_refusals(config, prefix, arms))
    if len(slot_names) != 1:
        found.append(("storage.catalog", f"a partitioned collection serves catalog, records, lexical and vector from one sqlite backend (M1a); found {sorted(slot_names)}"))
    for name in sorted(slot_names):
        spec = backends.get(name)
        if getattr(spec, "kind", None) != "sqlite":
            found.append((f"backends.{name}", "partitioned members are sqlite stores in M1a (non-sqlite write-path facets are refused, §6.4)"))
        elif spec.lexical_engine != "bm25" and any(p.arms.lexical is not None for p in pipelines.values()):
            found.append((f"backends.{name}.lexical_engine", "fts5 is refused for new members: set lexical_engine: bm25 (per-stratum statistics, §6.5)"))
    from ..ingestion.log_collection import live_members, live_refusals

    if config.projection.enabled:
        # M4 completion: the configured typed projection is one member's (the adopted support
        # store's own files, or one write-path member's); live log members contribute their own SQL
        # tables through the multi-source setup (``sql_surface/members.py``), never a projection spec.
        # A write-path member declaring ``metadata_fields`` builds no projection (``members.member_config``;
        # outage onboarding 2026-11-10): its records are read by the structured arm, not the SQL surface
        live = live_members(config)
        projected = [name for name, member in config.collection.members.items() if name not in live and member.metadata_fields is None]
        if len(projected) != 1:
            found.append(("projection", f"the typed projection is served for exactly one non-log member (found {sorted(projected)}); other members join the SQL surface through their own tables (live log members) or land with later increments"))
        elif config.projection.backend != storage.catalog:
            found.append(("projection.backend", "the projection of a partitioned collection is read through the catalog backend's members"))
    found.extend(log_refusals(config))
    found.extend(live_refusals(config))
    return found


def log_refusals(config: Any) -> list[tuple[str, str]]:
    """The v2 ``logs`` block over a partitioned collection (staging fix B1): log intents and tools
    are served over the **live log member** — its archive day/month members and the live slices of
    the request pin (``runtime/member_logs.py``: source-side episode summaries, the shape a log host
    publishes) — never over a separate episode archive file, provisional store or history tier (those
    are not opened). Refused: not exactly one live log member, a member fed by another configured
    log source than ``logs.source``, and the compose history tier."""
    from ..ingestion.log_collection import live_members

    logs = getattr(config, "logs", None)
    if logs is None:
        return []
    live = live_members(config)
    found: list[tuple[str, str]] = []
    if len(live) != 1:
        found.append(("logs", f"over a partitioned collection the logs block is served by exactly one live log member (collection.members.<id> with cadence: minutes); found {live}"))
        return found
    member = config.collection.members[live[0]]
    if member.producer.source is not None and member.producer.source != logs.source:
        found.append(("logs.source", f"logs.source {logs.source!r} is not the configured source {member.producer.source!r} feeding the live log member {live[0]!r}"))
    if logs.history.tier:
        found.append(("logs.history.tier", "the compose history tier is not served over a partitioned collection: closed days are the log member's archive day members"))
    return found


class CollectionView:
    """Base of the collection views. ``partitioned`` is ``False`` for the single-member view."""

    partitioned: bool = False

    @classmethod
    def for_config(cls, config: Any) -> CollectionView:
        unserved = unserved_blocks(config)
        if unserved:
            raise ConfigError(
                f"{', '.join(unserved)}: validated by this release but not served yet (unified merge: shared-error and unified "
                "community layers, the graph overlay, identity and links land later); the engine refuses rather than serving the members as one store",
                unserved[0],
            )
        if getattr(config, "collection", None) is not None:
            refusals = partitioned_refusals(config)
            if refusals:
                path, reason = refusals[0]
                raise ConfigError(f"partitioned collection: {reason}", path)
            return PartitionedView(config)
        return SingleMemberView(config)

    def member_ids(self) -> tuple[str, ...]:
        raise NotImplementedError

    def pin(self, generation: str | None) -> PinnedView:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        raise NotImplementedError

    def hold_session(self, session_id: str, generation: str, ttl: float) -> None:
        """A session holds what its turn's request pin reads for ``ttl`` seconds (nothing to hold
        without live slices)."""
        return None

    def release_session(self, session_id: str) -> None:
        """End a session's hold (closed or expired session)."""
        return None


class SingleMemberView(CollectionView):
    """Today's engine as a collection of one member: the dataset's one generation holds every
    document; the request pin is that generation and nothing else (no members, slices or layers)."""

    def __init__(self, config: Any):
        self.dataset = config.dataset

    def member_ids(self) -> tuple[str, ...]:
        return (SINGLE_MEMBER,)

    def pin(self, generation: str | None) -> PinnedView:
        """The view a request reads: exactly the generation ``WritePath.pin`` returned."""
        return PinnedView(generation=generation)

    def describe(self) -> dict[str, Any]:
        return {"mode": "single", "dataset": self.dataset, "members": list(self.member_ids())}


class PartitionedView(CollectionView):
    """A partitioned collection (M1a): the request pin is the unified generation's member set read
    from its manifest (``ingestion/compose.py``); slices (M2a) and layers (M1b) join it later.

    Constructing the view opens nothing; ``attach`` (called by the engine after its backends are
    built) binds the member registry, id directory and unified generation registry."""

    partitioned = True

    def __init__(self, config: Any):
        self.config = config
        self.dataset = config.dataset
        self.registry: Any = None

    def attach(self) -> Any:
        """The ``MemberRegistry`` of this collection (created on first use), with its live log
        members (``registry.live``: ``ingestion/log_collection.LiveLogs``) when one is declared."""
        if self.registry is None:
            from ..ingestion.log_collection import LiveLogs, live_members
            from ..ingestion.members import MemberRegistry

            registry = MemberRegistry(self.config)
            if live_members(self.config):
                registry.live = LiveLogs(self.config, registry)
            self.registry = registry
        return self.registry

    def member_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.config.collection.members))

    def pin(self, generation: str | None) -> PinnedView:
        """The view a request reads: the members and layers the unified generation names and, with a
        live log member, the slice version vector of its not-yet-archived days (read once, held by
        slice pins until ``release`` or the lease lapses). ``generation`` may also be a request
        generation id returned by the write path's ``pin`` (then that request's view)."""
        if generation is None:
            return PinnedView()
        from ..ingestion.compose import view_of
        from ..ingestion.release import generation_id

        registry = self.attach()
        live = registry.live
        if live is not None:
            pinned = live.view(generation)
            if pinned is not None:
                return pinned
        manifest = registry.generations.manifest(generation)
        if manifest is None:
            raise ConfigError(f"unknown unified generation {generation}", "collection")
        if live is None:
            return view_of(manifest)
        request = live.pin(generation_id(manifest), manifest)
        return live.view(request.generation) or view_of(manifest)

    def release(self, generation: str) -> None:
        """End the request's (or a session's) hold on the slices of a request generation id."""
        live = self.attach().live
        if live is not None:
            live.release(generation)

    def hold_session(self, session_id: str, generation: str, ttl: float) -> None:
        """A session's turn read ``generation`` (a request generation id): the session holds those
        slices — in addition to what its earlier turns pinned — for ``ttl`` seconds, so a slice its
        conversation read is not collected mid-conversation (``LiveLogs.hold``)."""
        live = self.attach().live
        if live is not None:
            live.hold(session_id, generation, ttl)

    def release_session(self, session_id: str) -> None:
        live = self.attach().live
        if live is not None:
            live.release_session(session_id)

    def describe(self) -> dict[str, Any]:
        collection = self.config.collection
        return {
            "mode": "partitioned",
            "dataset": self.dataset,
            "members": list(self.member_ids()),
            "adopted": sorted(name for name, m in collection.members.items() if m.adopt is not None),
            "layers": [{"name": layer.name, "kind": layer.kind, "arms": list(layer.arms), "member": layer.member} for layer in collection.layers],
        }
