"""Compose a unified generation **by reference** (docs/plans/unified_merge.md §6.1, §6.2; M1a, LA).

A unified generation is a manifest that names member partitions (``MemberRef`` values of the member
registry) — it copies, rebuilds or re-indexes nothing, so composing costs O(members), never
O(documents). Activation is the generation registry's compare-and-set on ``CURRENT`` under the
write lease (``ingestion/release.py``): two composers starting from one base cannot both win.

Checks before a manifest is written (each refusal leaves ``CURRENT`` unchanged):

* every member is declared, registered (built, or adopted read-only: ``ingestion/adopt_member.py``)
  and **live** in the id directory, its id count equals the
  directory's rows for the unit (a directory inconsistent with a unit manifest refuses, §6.12) and
  its recorded digests still match (``MemberRegistry.verify``);
* unit ids are unique; at most one catalog member per member id; a day is in one tier
  (``PinnedView``);
* one embedding space for every member the dense arm reads (a member from another space is refused
  here in M1a: labelled per-member degradation arrives with the coverage envelope, M1b/M2b);
* the lexical analyzer is equal within a source; ``fts5`` is refused for new (non-adopted) members;
* every declared attached layer (``ingestion/layers.py``, M1b) is pinned by digest into the
  manifest (``layers``) and enters the unified fingerprint; an unavailable optional layer is left
  out and named (``layers_unavailable``), a required one refuses.

``PartitionedWritePath`` is what the engine holds in partitioned mode in place of ``WritePath``:
``current()``/``pin()`` read the unified ``CURRENT`` and compare **only the collection recipe** with
the configuration (member recipes may differ by declared epoch); ``ingest`` is refused (members are
built with ``Engine.build_member`` and composed).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from ..core.errors import ConfigError, IndexNotReady
from ..core.types import GenerationId
from ..core.types_members import LayerRef, MemberRef, PinnedView
from .lease import stale_base
from .members import MemberRegistry
from .layers import recipes as layer_recipes
from .recipes import collection_fingerprint, embedding_space_ref, unified_fingerprint
from .release import ANY, generation_id
from .write import INDEX_FORMAT

MANIFEST_KIND = "partitioned"
MANIFEST_SCHEMA = 3


def check_members(config: Any, registry: MemberRegistry, members: Sequence[MemberRef]) -> PinnedView:
    """Every compose-time check above; returns the view the generation will serve."""
    if not members:
        raise ValueError("a unified generation needs at least one member")
    declared = config.collection.members
    for ref in members:
        if ref.member not in declared:
            raise ValueError(f"member {ref.unit_id}: {ref.member!r} is not declared in collection.members")
        if ref.tier == "adopted" and declared[ref.member].adopt is None:
            raise ValueError(f"member {ref.unit_id} is adopted but collection.members.{ref.member} declares no adopt block")
    view = PinnedView(members=tuple(members))  # unique units, one tier per member-day
    catalogs = [ref.member for ref in members if ref.tier == "catalog"]
    if len(set(catalogs)) != len(catalogs):
        raise ValueError("at most one catalog member per member id")
    space = embedding_space_ref(config)
    analyzers: dict[str, tuple[str, Any]] = {}
    for ref in members:
        unit = registry.directory.unit(ref.unit_id)
        if unit is None or unit["status"] != "live":
            raise ValueError(f"member {ref.unit_id} is not registered and live in the id directory")
        total = sum(ref.counts.values())
        if unit["documents"] != total or registry.directory.count(ref.unit_id) != total:
            raise ValueError(f"member {ref.unit_id}: the id directory is inconsistent with the member manifest ({unit['documents']} rows vs {total} documents)")
        registry.verify(ref)
        recipe = ref.recipe
        if recipe.embedding_space is not None and recipe.embedding_space != space:
            adopt = declared[ref.member].adopt
            if ref.tier == "adopted" and adopt is not None and adopt.embedding_space is not None:
                # §6.2: an explicit mapping declares the adopted store's space equivalent; compose
                # accepts it only with a passing verify-sample record (``space_equivalence``)
                from .space_equivalence import accepted

                reason = accepted(config, registry, ref)
                if reason is not None:
                    raise ValueError(f"member {ref.unit_id}: embedding space {recipe.embedding_space.id!r} is mapped onto the collection's {space.id!r} but {reason}")
            else:
                raise ValueError(
                    f"member {ref.unit_id}: embedding space {recipe.embedding_space.id!r} ({recipe.embedding_space.model}) is not the collection's "
                    f"{space.id!r} ({space.model}); re-embed it in the collection's space, or map an adopted store's own space id with "
                    f"collection.members.{ref.member}.adopt.embedding_space and verify it (collection verify-space)"
                )
        if recipe.analyzer.engine == "fts5" and ref.tier != "adopted":
            raise ValueError(f"member {ref.unit_id}: lexical engine fts5 is refused for new members (lexical_engine: bm25)")
        for source in ref.sources:
            seen = analyzers.setdefault(source, (ref.unit_id, recipe.analyzer))
            if seen[1] != recipe.analyzer:
                raise ValueError(f"source {source!r}: member {ref.unit_id} and {seen[0]} use different lexical analyzers (one analyzer per source)")
    return view


def _merge_coverage(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    """Coverage of the composed generation: one member's coverage verbatim; otherwise every stage is
    ``incomplete`` when any member's is, with processed/remaining summed."""
    if len(manifests) == 1:
        return manifests[0].get("coverage") or {}
    stages: dict[str, dict[str, Any]] = {}
    without: list[str] = []
    rejected: dict[str, int] = {}
    policies = {m.get("coverage", {}).get("policy", "strict") for m in manifests}
    for manifest in manifests:
        coverage = manifest.get("coverage") or {}
        for name, stage in (coverage.get("stages") or {}).items():
            merged = stages.setdefault(name, {"status": "complete", "processed": 0, "remaining": 0})
            if stage.get("status") == "incomplete":
                merged["status"] = "incomplete"
            merged["processed"] += int(stage.get("processed", 0) or 0)
            merged["remaining"] += int(stage.get("remaining", 0) or 0)
        extraction = coverage.get("extraction") or {}
        without.extend(extraction.get("documents_without_graph", []))
        for category, count in (extraction.get("rejected_by_category") or {}).items():
            rejected[category] = rejected.get(category, 0) + int(count)
    return {
        "policy": policies.pop() if len(policies) == 1 else "partial",
        "stages": stages,
        "extraction": {"documents_without_graph": sorted(without), "rejected_by_category": rejected},
    }


def member_manifest(registry: MemberRegistry, ref: MemberRef) -> dict[str, Any]:
    from .log_members import is_log_unit
    from .release import FileGenerationRegistry

    if is_log_unit(ref):
        # a sealed log unit (M2a wiring) is one complete build: no store generation, no stages
        return {"documents": sum(ref.counts.values()), "status": "complete", "coverage": {}}

    manifest = FileGenerationRegistry(registry.store_dir(ref)).current()
    if manifest is None:
        raise ValueError(f"member {ref.unit_id}: its store has no active generation")
    return manifest


def compose(config: Any, registry: MemberRegistry, members: Sequence[MemberRef], *, expected: str | None = ANY) -> dict[str, Any]:
    """Write and activate a unified generation over ``members`` (compare-and-set on ``expected``,
    the generation the caller composed from; ``ANY``: the current one at lock time)."""
    members = sorted(members, key=lambda r: r.unit_id)
    check_members(config, registry, members)
    layers, unavailable = pinned_layers(config, members)
    manifests = [member_manifest(registry, ref) for ref in members]
    with registry.generations.lock() as lease:
        base = registry.generations.current_generation()
        if expected is not ANY and base != expected:
            raise stale_base(registry.generations.kind, base, expected)
        manifest = {
            "schema_version": MANIFEST_SCHEMA,
            "kind": MANIFEST_KIND,
            "dataset": config.dataset,
            "generation": uuid.uuid4().hex,
            "index_format": INDEX_FORMAT,
            "fingerprint": unified_fingerprint(config, [ref.recipe.fingerprint() for ref in members], layer_recipes(layers)),
            "collection_fingerprint": collection_fingerprint(config),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "base": base,
            "members": [ref.model_dump(mode="json") for ref in members],
            "layers": [ref.model_dump(mode="json") for ref in layers],
            "layers_unavailable": unavailable,
            "documents": sum(int(m.get("documents", 0)) for m in manifests),
            "status": "partial" if any(m.get("status") == "partial" for m in manifests) else (manifests[0].get("status") or "complete"),
            "coverage": _merge_coverage(manifests),
        }
        if lease is not None:
            lease.check()
        registry.generations.write_manifest(manifest)
        registry.generations.activate(manifest, expected=base)
    return manifest


def pinned_layers(config: Any, members: Sequence[MemberRef]) -> tuple[list[LayerRef], dict[str, str]]:
    """``LayerRef`` of every declared layer (M1b, ``ingestion/layers.py``). A layer whose files do
    not match its pin is left out and named in ``layers_unavailable`` (its arm fails, labelled); a
    ``required`` one refuses the compose. Misbound layers (configuration errors) always refuse."""
    from .layers import LayerUnavailable, layer_ref

    adopted = {ref.member: ref for ref in members if ref.tier == "adopted"}
    refs: list[LayerRef] = []
    unavailable: dict[str, str] = {}
    for layer in config.collection.layers:
        try:
            refs.append(layer_ref(config, layer, adopted))
        except LayerUnavailable as exc:
            if layer.required:
                raise ValueError(f"required {exc}") from exc
            unavailable[layer.name] = str(exc)
    return refs, unavailable


def members_of(manifest: dict[str, Any]) -> tuple[MemberRef, ...]:
    return tuple(MemberRef.model_validate(m) for m in manifest.get("members", []))


def layers_of(manifest: dict[str, Any]) -> tuple[LayerRef, ...]:
    return tuple(LayerRef.model_validate(m) for m in manifest.get("layers", []))


def view_of(manifest: dict[str, Any]) -> PinnedView:
    """The pinned view of a unified generation manifest (members and layers)."""
    return PinnedView(generation=manifest.get("generation"), members=members_of(manifest), layers=layers_of(manifest))


class PartitionedWritePath:
    """The engine's write path in partitioned mode (read side + member build/compose entry points)."""

    def __init__(self, config: Any, registry: MemberRegistry):
        self.cfg = config
        self.members = registry
        self.registry = registry.generations
        self.backends: dict[str, Any] = {}

    def index_fingerprint(self) -> str:
        """The collection fingerprint (the only part compared with the configuration)."""
        return collection_fingerprint(self.cfg)

    def legacy_fingerprint(self) -> str | None:
        return None

    def current(self) -> tuple[GenerationId, dict[str, Any]]:
        manifest = self.registry.current()
        if manifest is None:
            raise IndexNotReady("no active collection generation; build members and compose")
        if manifest.get("kind") != MANIFEST_KIND or manifest.get("schema_version") != MANIFEST_SCHEMA:
            raise IndexNotReady("the active generation is not a partitioned collection generation")
        if manifest.get("collection_fingerprint") != self.index_fingerprint():
            raise IndexNotReady("collection recipe mismatch (dataset, members, embedding space or index format changed): restore the config or rebuild the members")
        return generation_id(manifest), manifest

    def pin(self) -> tuple[GenerationId, dict[str, Any]]:
        """The request pin: the active unified generation, plus — with a live log member — the slice
        version vector read once (``ingestion/log_collection.LiveLogs.pin``: a request generation id
        naming generation + slices; the slices are held by slice pins for the request)."""
        gen, manifest = self.current()
        live = getattr(self.members, "live", None)
        if live is not None and live.members:
            gen = live.pin(gen, manifest)
        return gen, manifest

    def status(self) -> dict[str, Any]:
        _, manifest = self.current()
        return {"manifest": manifest, "documents": {}, "failures": self.registry.failures()}

    def ingest(self, documents: Any, **kwargs: Any) -> dict[str, Any]:
        raise ConfigError(
            "a partitioned collection is not ingested as one store: build each member (Engine.build_member / "
            "su-multi-arm collection register) and compose (Engine.compose / collection compose)",
            "collection",
        )

    def delete_generation(self, generation: str) -> dict[str, Any]:
        raise ConfigError("unified generations are retired by composing without a member; member GC lands with M2a", "collection")
