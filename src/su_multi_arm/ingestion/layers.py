"""Attached layers of a unified collection (docs/plans/unified_merge.md §6.4; M1b, lane LA).

A **layer** is a read-only store outside the write path, pinned by digest in the unified manifest
(``LayerRef``), covering declared sources and serving declared arms. M1b serves three kinds, the
validated support build's companions:

=======================  ==========================================================================
kind                     served as
=======================  ==========================================================================
``graph_attach``         the ``kuzu`` ``mode: attach`` backend named by the layer. The attached store
                         answers for the store generation of the adopted member the layer names
                         (``member``); the engine wraps the backend in ``LayerBackend``, which maps a
                         unified generation to that store generation, so the graph arm reads it
                         exactly as today. Digest: sha256 of the mapping file + a listing digest
                         (name, size, mtime) of the database path — the plan's "directory listing"
                         for an attached graph, which is too large to re-hash per process.
``segment_sidecar``      a sealed segment index directory (``segments/``) at ``path`` (the segment
                         arm's ``path``); digest: sha256 over every file's sha256.
``communities_imported`` the imported community file (``communities.imported``) the adopted member's
                         store materialised at its build; digest: the file's sha256, which must equal
                         the adopted store's original ``communities.imported.sha256``. The community
                         arm reads the communities from that member's store (``PartitionedGraph``).
=======================  ==========================================================================

Layers are **exempt** from the "non-sqlite write-path facets are refused" rule (they are not
write-path facets). A layer's digest is recomputed when a request first reads it (once per process
and generation); a mismatch makes the layer ``unavailable``: the arm it serves fails for that request
(labelled in the trace), the other arms keep serving (§6.12). A ``required`` layer that is
unavailable refuses the compose.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..core.errors import BackendError, ConfigError
from ..core.types import GenerationId
from ..core.types_members import FacetLocation, LayerRef, MemberRef, canonical_sha256
from .members import file_sha256

# Layer kinds M1b serves (the others are validated by the schema and refused by the engine until
# their increment: shared-error communities M2a/M2b, identity/links M2b/M3, overlay M5, unified M6).
SERVED_KINDS = frozenset({"graph_attach", "segment_sidecar", "communities_imported"})
MEMBER_KINDS = frozenset({"graph_attach", "communities_imported"})  # layers bound to an adopted member's store


class LayerUnavailable(RuntimeError):
    """A layer's files no longer match its pinned digest (fail closed for that layer)."""

    def __init__(self, name: str, message: str):
        self.name = name
        super().__init__(f"layer {name} unavailable: {message}")


class LayerUnavailableError(BackendError):
    """A request read an unavailable layer: the engine fails only the arm that read it (an empty
    result with a ``layer_unavailable`` report, coverage incomplete), the other arms keep serving."""

    def __init__(self, name: str, message: str):
        self.layer = name
        super().__init__("layer", name, message)


def path_digest(path: Path) -> str:
    """sha256 of a file, or of every file of a directory (relative name -> sha256, canonical)."""
    if path.is_file():
        return file_sha256(path)
    if path.is_dir():
        files = sorted(p for p in path.rglob("*") if p.is_file())
        return canonical_sha256([[p.relative_to(path).as_posix(), file_sha256(p)] for p in files])
    raise FileNotFoundError(str(path))


def listing_digest(path: Path) -> str:
    """Digest of a file's or directory's listing (relative name, size, mtime in ns)."""
    entries = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else []
    if not entries:
        raise FileNotFoundError(str(path))
    base = path if path.is_dir() else path.parent
    return canonical_sha256([[p.relative_to(base).as_posix(), p.stat().st_size, p.stat().st_mtime_ns] for p in entries])


def backend_layer_digest(config: Any, layer: Any) -> str:
    """Digest of a backend layer (``graph_attach``): mapping file content + database listing."""
    spec = config.backends[layer.backend]
    attach = getattr(spec, "attach", None)
    mapping = Path(attach.mapping) if attach is not None else None
    parts = {"database": listing_digest(Path(spec.path))}
    if mapping is not None:
        parts["mapping"] = file_sha256(mapping)
    return canonical_sha256(parts)


def compute_digest(config: Any, layer: Any) -> str:
    if layer.backend is not None:
        return backend_layer_digest(config, layer)
    return path_digest(Path(layer.path))


def layer_ref(config: Any, layer: Any, members: dict[str, MemberRef]) -> LayerRef:
    """The ``LayerRef`` of one declared layer (verifies its pin; checks its binding to a member)."""
    if layer.kind not in SERVED_KINDS:
        raise ConfigError(f"collection.layers {layer.name!r}: layer kind {layer.kind} is served from a later increment", "collection.layers")
    try:
        digest = compute_digest(config, layer)
    except FileNotFoundError as exc:
        raise LayerUnavailable(layer.name, f"missing file {Path(str(exc)).name}") from exc
    if layer.sha256 is not None and digest != layer.sha256:
        raise LayerUnavailable(layer.name, f"digest {digest[:12]} is not the pinned {layer.sha256[:12]}")
    version = None
    member = getattr(layer, "member", None)
    if layer.kind in MEMBER_KINDS:
        ref = members.get(member) if member else None
        if ref is None or ref.tier != "adopted":
            raise ConfigError(f"collection.layers {layer.name!r}: {layer.kind} names the adopted member whose store it belongs to (member:)", "collection.layers")
        generation = str(ref.stats.get("generation"))
        version = f"{ref.unit_id}@{generation}"
        if layer.kind == "graph_attach":
            check_attach_generation(config, layer, generation)
        if layer.kind == "communities_imported":
            pinned = ref.stats.get("communities_sha256")
            if pinned != digest:
                raise ConfigError(
                    f"collection.layers {layer.name!r}: the adopted store {ref.unit_id} imported communities with sha256 {str(pinned)[:12]}, not this file ({digest[:12]})",
                    "collection.layers",
                )
    elif layer.kind == "segment_sidecar":
        manifest = json.loads((Path(layer.path) / "manifest.json").read_text())
        if manifest.get("dataset") != config.dataset:
            raise ConfigError(f"collection.layers {layer.name!r}: the segment index serves dataset {manifest.get('dataset')!r}", "collection.layers")
    return LayerRef(
        name=layer.name,
        kind=layer.kind,
        sources=tuple(layer.sources),
        arms=tuple(layer.arms),
        digest=digest,
        version=version,
        location=FacetLocation(backend=layer.kind, location=f"layers/{layer.name}"),
    )


def check_attach_generation(config: Any, layer: Any, generation: str) -> None:
    """The attached graph declares the adopted store's generation (or any generation) for the dataset."""
    from ..backends.kuzu.attach import AttachMapping

    spec = config.backends[layer.backend]
    mapping = AttachMapping.load(spec.attach.mapping)
    if mapping.dataset != config.dataset or not (mapping.any_generation or generation in mapping.generations):
        raise ConfigError(
            f"collection.layers {layer.name!r}: the attached graph answers for dataset {mapping.dataset!r} generations {mapping.generations}, "
            f"not the adopted store generation {generation}",
            "collection.layers",
        )


def layer_refs(config: Any, members: Sequence[MemberRef]) -> list[LayerRef]:
    """``LayerRef`` of every declared layer, in configuration order (compose time)."""
    by_member = {ref.member: ref for ref in members if ref.tier == "adopted"}
    return [layer_ref(config, layer, by_member) for layer in config.collection.layers]


def declared(config: Any, name: str) -> Any:
    for layer in config.collection.layers:
        if layer.name == name:
            return layer
    raise ConfigError(f"no collection layer named {name!r}", "collection.layers")


def verify(config: Any, ref: LayerRef) -> None:
    """The layer's files still match the digest pinned in the generation (``LayerUnavailable``)."""
    layer = declared(config, ref.name)
    try:
        digest = compute_digest(config, layer)
    except FileNotFoundError as exc:
        raise LayerUnavailable(ref.name, f"missing file {Path(str(exc)).name}") from exc
    if digest != ref.digest:
        raise LayerUnavailable(ref.name, "files changed since the generation pinned them")


def recipes(refs: Sequence[LayerRef]) -> list[dict[str, Any]]:
    """Layer recipes for the unified fingerprint (§6.2): kind, sources, arms, digest, version."""
    return [{"name": r.name, "kind": r.kind, "sources": list(r.sources), "arms": list(r.arms), "digest": r.digest, "version": r.version} for r in refs]


def layer_hash(name: str, digest: str) -> str:
    return hashlib.sha256(f"{name}\0{digest}".encode()).hexdigest()


# --- serving a backend layer -------------------------------------------------------------------------


class MappedGraph:
    """The graph facet of an attached layer: every call's unified generation is replaced by the store
    generation the layer answers for (``resolve``); writes are refused."""

    def __init__(self, inner: Any, resolve: Any, name: str):
        self._inner, self._resolve, self._name = inner, resolve, name

    def __getattr__(self, name: str) -> Any:
        # generation-free extras the store may offer (``ontology``, ``entity_types``): passed through
        # only when the store has them, so callers probing with ``getattr(..., None)`` see the same
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def upsert(self, gen: GenerationId, entities: Any, relations: Any, memberships: Any) -> None:
        raise BackendError("layer", "upsert", f"layer {self._name} is attached read-only")

    def put_communities(self, gen: GenerationId, communities: Any) -> None:
        raise BackendError("layer", "put_communities", f"layer {self._name} is attached read-only")

    def traverse(self, gen: GenerationId, spec: Any, scope: Any) -> Any:
        return self._inner.traverse(self._resolve(gen), spec, scope)

    def find_entities(self, gen: GenerationId, tokens: Sequence[str], scope: Any) -> Any:
        return self._inner.find_entities(self._resolve(gen), tokens, scope)

    def mentions(self, gen: GenerationId, entity_ids: Sequence[str], scope: Any, limit: int) -> Any:
        return self._inner.mentions(self._resolve(gen), entity_ids, scope, limit)

    def memberships(self, gen: GenerationId) -> Any:
        return self._inner.memberships(self._resolve(gen))

    def communities(self, gen: GenerationId) -> Any:
        return self._inner.communities(self._resolve(gen))


class LayerBackend:
    """A backend instance served as an attached layer of a partitioned collection: its ``graph``
    facet answers for the bound member's store generation (``MappedGraph``); the layer is verified
    against its pinned digest on first use per generation. Everything else is the backend's own."""

    def __init__(self, inner: Any, layer: Any, partitioned: Any):
        self._inner = inner
        self.layer = layer
        self.kind = getattr(inner, "kind", "layer")
        self._partitioned = partitioned
        self.graph = MappedGraph(inner.graph, self.resolve, layer.name)

    def resolve(self, gen: GenerationId) -> GenerationId:
        return self._partitioned.layer_generation(gen, self.layer.name)

    def covered_sources(self) -> list[str]:
        """The ``Document.source`` values the layer covers (``collection.layers[].sources``): an arm
        reading it serves no document of another source (``arms/graph.py`` ``layer_scope``)."""
        return list(self.layer.sources)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
