"""One generation across several backends (plan §2.1 principle 1, §3.5, §3.14 multi-backend write path).

**Placement.** Each generation-scoped projection lives on the backend(s) the configuration selects:

=============== ===================================================================================
projection      backends
=============== ===================================================================================
``catalog``     ``storage.catalog`` (authoritative; its generation registry holds ``CURRENT``)
``records``     ``storage.records`` and ``pipeline.arms.structured.backend``
``lexical``     ``pipeline.arms.lexical.backend``
``vector``      ``pipeline.arms.dense.backend``
``graph``       ``pipeline.arms.graph.backend``, ``pipeline.arms.community.backend``, ``storage.communities``
``communities`` ``storage.communities`` and ``pipeline.arms.community.backend``
=============== ===================================================================================

``storage.ledger``/``sessions``/``cache`` are not generation-scoped and take no part in staging,
activation or pinning. Every *secondary* holder (a backend with projections but not the catalog)
that serves the ``catalog`` capability also receives a **scope mirror** of the generation's
documents and chunks: lexical, vector and graph facets push eligibility down against their own
backend's catalog rows (and refuse chunks of unknown documents), so the mirror is what makes scope
pushdown possible there. A mirror is never read as the catalog.

**Activation.** Each holder has an activation mechanism, discovered structurally:

* ``current`` — the authoritative registry of ``storage.catalog`` (file/row/document pointer, or the
  Elasticsearch docs alias); switched **last**;
* ``pointer`` — the backend's own persisted pointer (``extras.GenerationPointer``: the neo4j pointer
  node, the OpenSearch/Elasticsearch dataset aliases);
* ``registry`` — the secondary backend's own generation registry (sqlite/memory file or state,
  PostgreSQL/SQL pointer row, MongoDB pointer document): manifest written, ``CURRENT`` moved;
* ``none`` — an injected backend with neither; recorded in the manifest, never checked.

Secondaries are switched first in projection order (vector, lexical, records, graph, communities),
the authoritative ``CURRENT`` last; when any step fails, every pointer already moved is put back to
what it named before (or removed) in reverse order and ``CURRENT`` is unchanged.

**Pinning.** A reader resolves ``CURRENT`` and then requires every secondary pointer to name the
same generation; any disagreement is a typed ``GenerationDisagreement`` (never a mixed read).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..backends import capabilities as caps
from ..backends import extras
from ..core.errors import ConfigError, GenerationDisagreement
from ..core.types import GenerationId

PROJECTION_ORDER: tuple[str, ...] = ("vector", "lexical", "records", "graph", "communities", "catalog")
NEEDS_CATALOG_ROWS = ("lexical", "vector")  # facets that refuse chunks of documents they do not hold


def _unique(*names: str | None) -> list[str]:
    return list(dict.fromkeys(n for n in names if n))


def placement(config: Any) -> dict[str, list[str]]:
    """Projection -> backend names (configuration order, without duplicates)."""
    storage, arms = config.storage, config.pipeline.arms
    graph = arms.graph.backend if arms.graph else None
    community = arms.community.backend if arms.community else None
    placed = {
        "catalog": [storage.catalog],
        "records": _unique(storage.records, arms.structured.backend if arms.structured else None),
        "graph": _unique(graph, community, storage.communities),
        "communities": _unique(storage.communities, community),
    }
    if arms.lexical:
        placed["lexical"] = [arms.lexical.backend]
    if arms.dense:
        placed["vector"] = [arms.dense.backend]
    return placed


def serves_catalog(backend: Any) -> bool:
    kind = getattr(backend, "kind", None)
    if kind in caps.MATRIX:
        return caps.serves(kind, "catalog") and caps.satisfied(backend, "catalog")
    return caps.satisfied(backend, "catalog")


def activation_kind(backend: Any, authoritative: bool) -> str:
    if authoritative:
        return "current"
    if isinstance(backend, extras.GenerationPointer):
        return "pointer"
    if callable(getattr(backend, "generation_registry", None)):
        return "registry"
    return "none"


@dataclass
class Holder:
    """One backend instance holding projections of the generation."""

    name: str
    backend: Any
    projections: list[str] = field(default_factory=list)
    authoritative: bool = False
    mirror: bool = False
    activation: str = "none"

    @property
    def kind(self) -> str:
        return str(getattr(self.backend, "kind", type(self.backend).__name__))


class GenerationSet:
    """The holders of one dataset's generation, in activation order (secondaries, then the catalog)."""

    def __init__(self, config: Any, backends: dict[str, Any]):
        self.placement = placement(config)
        by_name: dict[str, Holder] = {}
        for projection in PROJECTION_ORDER:
            for name in self.placement.get(projection, []):
                if name not in backends:
                    raise ConfigError(f"no backend instance named {name!r} for the {projection} projection", "backends")
                holder = by_name.setdefault(name, Holder(name=name, backend=backends[name]))
                holder.projections.append(projection)
        catalog_name = config.storage.catalog
        for holder in by_name.values():
            holder.authoritative = holder.name == catalog_name
            holder.activation = activation_kind(holder.backend, holder.authoritative)
            if not holder.authoritative:
                holder.mirror = serves_catalog(holder.backend)
                missing = [p for p in holder.projections if p in NEEDS_CATALOG_ROWS]
                if missing and not holder.mirror:
                    raise ConfigError(
                        f"backend {holder.name!r} ({holder.kind}) holds {missing} but does not serve the catalog capability, "
                        "so it cannot receive the scope mirror its search facets need",
                        "pipeline.arms",
                    )
        self.authoritative = by_name[catalog_name]
        # PROJECTION_ORDER puts catalog last, so the authoritative holder (which may also hold earlier
        # projections) is moved to the end explicitly.
        self.secondaries = [h for h in by_name.values() if not h.authoritative]
        self.holders = [*self.secondaries, self.authoritative]

    def targets(self, projection: str) -> list[Holder]:
        names = self.placement.get(projection, [])
        return [h for h in self.holders if h.name in names]

    def mirrors(self) -> list[Holder]:
        return [h for h in self.secondaries if h.mirror]

    # --- pointers -------------------------------------------------------------------------------

    @staticmethod
    def active(holder: Holder, dataset: str) -> str | None:
        """What the holder's own pointer names (``None`` = no pointer); never used for the authoritative one."""
        if holder.activation == "pointer":
            return holder.backend.active_generation(dataset)
        if holder.activation == "registry":
            registry = holder.backend.generation_registry()
            if callable(getattr(registry, "current_generation", None)):
                return registry.current_generation()
            current = registry.current()
            return None if current is None else current["generation"]
        return None

    @staticmethod
    def activate(holder: Holder, gen: GenerationId, manifest: dict[str, Any]) -> None:
        if holder.activation == "pointer":
            holder.backend.activate_generation(gen)
        elif holder.activation == "registry":
            registry = holder.backend.generation_registry()
            registry.write_manifest(manifest)
            registry.activate(manifest)

    @staticmethod
    def restore(holder: Holder, gen: GenerationId, before: str | None) -> None:
        """Put the holder's pointer back to ``before`` (or remove it when it named nothing)."""
        if holder.activation == "pointer":
            if before is None:
                holder.backend.deactivate_generation(gen)
            else:
                holder.backend.activate_generation(gen.model_copy(update={"generation": before}))
        elif holder.activation == "registry":
            registry = holder.backend.generation_registry()
            if before is None:
                registry.deactivate(gen.generation)
            else:
                previous = registry.manifest(before)
                if previous is None:
                    raise ValueError(f"backend {holder.name!r}: no manifest for the previous generation {before}; pointer left on {gen.generation}")
                registry.activate(previous)

    def pointers(self, dataset: str) -> dict[str, str | None]:
        """Every secondary pointer (holders with activation ``none`` are not listed)."""
        return {h.name: self.active(h, dataset) for h in self.secondaries if h.activation != "none"}

    def check(self, gen: GenerationId) -> None:
        """Refuse when any secondary pointer names another generation than ``CURRENT``."""
        pointers = self.pointers(gen.dataset)
        if any(value != gen.generation for value in pointers.values()):
            raise GenerationDisagreement(gen.dataset, gen.generation, pointers)

    # --- manifest -------------------------------------------------------------------------------

    @staticmethod
    def identity(holder: Holder, gen: GenerationId) -> dict[str, Any]:
        if isinstance(holder.backend, extras.GenerationIdentity):
            return dict(holder.backend.generation_identity(gen))
        return {"key": gen.prefix()}

    @staticmethod
    def analyzer_id(holder: Holder, gen: GenerationId) -> str | None:
        """Per-generation analyzer when the adapter records one (postgres), else the matrix flag."""
        recorded = getattr(holder.backend, "analyzer_id", None)
        if callable(recorded):
            return str(recorded(gen))
        kind = holder.kind
        if kind in caps.MATRIX and caps.declares(kind, "lexical"):
            value = caps.flags(kind, "lexical").get("analyzer_id")
            return None if value is None else str(value)
        return None

    def manifest_entry(self, gen: GenerationId) -> dict[str, Any]:
        entries: dict[str, Any] = {}
        for holder in self.holders:
            entry: dict[str, Any] = {
                "kind": holder.kind,
                "projections": list(holder.projections),
                "role": "authoritative" if holder.authoritative else "secondary",
                "scope_mirror": holder.mirror,
                "activation": holder.activation,
                "generation": gen.generation,
                "key": gen.prefix(),
                "identity": self.identity(holder, gen),
            }
            if "lexical" in holder.projections:
                entry["analyzer_id"] = self.analyzer_id(holder, gen)
            entries[holder.name] = entry
        return {"activation_order": [h.name for h in self.holders], "backends": entries}
