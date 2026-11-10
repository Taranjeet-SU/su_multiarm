"""Backend-free index recipes (docs/plans/unified_merge.md §6.2, §6.3; R2).

``recipe_of(config)`` computes the whole-config index recipe that ``WritePath.index_recipe()``
hashes, **from the configuration alone**: it constructs no ``WritePath``, opens no backend, builds no
provider and reads no file of any store. Read-only adoption (M1b) uses it to recompute the original
fingerprint of an adopted store without touching the store. A golden test
(``tests/unit/test_index_fingerprint_golden.py``) proves ``recipe_of(cfg) == WritePath(cfg).index_recipe()``
on the shipped examples and the unified fixtures, so the two cannot drift silently.

Created by S0 (L0); the per-partition decomposition (collection recipe, member fingerprints, layer
recipes) is added here by the partition core (M1a, lane LA).
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..core.types_members import PARTITION_PROTOCOL, AnalyzerRecipe, EmbeddingSpaceRef, PartitionRecipe, canonical_sha256
from ..extraction.base import BuiltinExtractor
from .generations import attached_read_only
from .write import INDEX_FORMAT, _backend_recipe, chunking_recipe, recipe_fingerprint_of


def write_slots(config: Any) -> dict[str, str]:
    """Projection slot -> backend name, as ``WritePath.__init__`` assigns them."""
    storage, arms = config.storage, config.pipeline.arms
    slots = {"catalog": storage.catalog, "records": storage.records, "ledger": storage.ledger, "communities": storage.communities}
    if arms.lexical:
        slots["lexical"] = arms.lexical.backend
    if arms.dense:
        slots["vector"] = arms.dense.backend
    if arms.graph and not attached_read_only(config, arms.graph.backend):
        slots["graph"] = arms.graph.backend
    if config.projection.enabled:
        slots["projection"] = config.projection.backend
    return slots


def extractor_fingerprint(config: Any) -> str | None:
    """The extractor's recipe fingerprint without a provider: only the extractor role's model name
    enters it (``BuiltinExtractor.recipe_fingerprint``); the pattern library is not read."""
    role = config.models.roles.extractor
    providers = {"extractor": SimpleNamespace(config=role)} if role is not None else {}
    return BuiltinExtractor(config.extraction, SimpleNamespace(providers=providers)).recipe_fingerprint()


def recipe_of(config: Any) -> dict[str, Any]:
    """The index recipe of ``config`` (equal to ``WritePath(config, …).index_recipe()``)."""
    roles = config.models.roles
    slots = write_slots(config)
    data = {
        "dataset": config.dataset,
        "index_format": INDEX_FORMAT,
        "chunking": chunking_recipe(config),
        "privacy": config.privacy.recipe(),
        "extractor": extractor_fingerprint(config),
        "embedder": {
            **roles.embedder.model_dump(mode="json", exclude={"pricing_key", "precomputed"}),
            **({"precomputed": roles.embedder.precomputed.identity()} if roles.embedder.precomputed is not None else {}),
        },
        "communities": config.communities.recipe(),
        "community_model": None if roles.community is None else roles.community.model,
        "backends": {slot: _backend_recipe(config.backends[name]) for slot, name in sorted(slots.items())},
    }
    if config.projection.enabled:
        data["projection"] = config.projection.recipe()
    return data


def fingerprint_of(config: Any) -> str:
    """The index fingerprint of ``config`` (``WritePath.index_fingerprint()`` without a backend)."""
    return recipe_fingerprint_of(recipe_of(config))


# --- per-partition decomposition (M1a, lane LA; §6.2) ------------------------------------------------
#
# A partitioned collection (``schema_version: 3``, ``collection.mode: partitioned``) is never
# fingerprinted as one whole-config recipe. It is decomposed into
#
# * the **collection recipe** — index format, partition protocol, embedding space, the member list
#   with their namespaces/sources/kinds, the schema version and the dataset. It is the only part
#   ``current()`` compares with the configuration in partitioned mode;
# * one **partition recipe** per member (``core.types_members.PartitionRecipe``): embedding space,
#   analyzer, chunking, privacy, content (extractor, communities, backend kinds, keyword epoch,
#   attachments) and projection — location-free (no store path);
# * the layer recipes (pinned digests; attached layers arrive with M1b).
#
# ``unified_fingerprint`` hashes the collection recipe, the sorted member fingerprints and the layer
# recipes; it is recorded in the unified manifest. Non-partitioned configurations keep calling the
# unchanged ``recipe_of`` / ``WritePath.index_recipe`` (golden tests).


def re_identifier(text: str) -> str:
    """A model name as an embedding-space id (``[a-z0-9][a-z0-9_.-]*``, at most 64 characters)."""
    cleaned = re.sub(r"[^a-z0-9_.-]+", "-", text.lower()).strip("-.") or "space"
    return (cleaned if cleaned[0].isalnum() else f"s{cleaned}")[:64]


def embedding_space_ref(config: Any) -> EmbeddingSpaceRef:
    """The collection's embedding space: the embedder role's identity (a precomputed matrix by its
    content identity) plus the declared space id/template/epoch (``collection.embedding_space``;
    default id derived from the model name)."""
    role = config.models.roles.embedder
    declared = getattr(getattr(config, "collection", None), "embedding_space", None)
    model_id = role.model
    if role.precomputed is not None:
        model_id = f"{role.model}@precomputed:{canonical_sha256(role.precomputed.identity())[:16]}"
    return EmbeddingSpaceRef(
        id=declared.id if declared is not None else re_identifier(role.model),
        model=model_id,
        revision=role.revision,
        dimensions=role.dimensions,
        normalization="l2",
        template=declared.template if declared is not None else "engine_template",
        epoch=None if declared is None else declared.epoch,
    )


def analyzer_recipe(config: Any, backend_name: str | None) -> AnalyzerRecipe:
    """The lexical analyzer of the write-path lexical backend."""
    spec = None if backend_name is None else config.backends.get(backend_name)
    if spec is None or getattr(spec, "kind", None) != "sqlite":
        return AnalyzerRecipe()
    if spec.lexical_engine != "bm25":
        return AnalyzerRecipe(engine="fts5", tokenizer="unicode61")
    bm25 = spec.bm25
    return AnalyzerRecipe(engine="bm25", tokenizer=bm25.tokenizer, k1=bm25.k1, b=bm25.b, epsilon=bm25.epsilon)


def collection_recipe(config: Any) -> dict[str, Any]:
    """The collection recipe of a partitioned configuration (§6.2): equal for every generation of the
    collection; the only part ``current()`` compares with the configuration in partitioned mode."""
    collection = getattr(config, "collection", None)
    if collection is None:
        raise ValueError("collection_recipe needs a partitioned configuration (collection block, schema v3)")
    return {
        "dataset": config.dataset,
        "schema_version": config.schema_version,
        "index_format": INDEX_FORMAT,
        "partition_protocol": PARTITION_PROTOCOL,
        "embedding_space": embedding_space_ref(config).model_dump(mode="json"),
        "members": {
            name: {
                "kind": m.kind,
                "sources": sorted(m.sources),
                "id_namespaces": sorted(m.id_namespaces),
                "key": m.key,
                "catalog_kinds": sorted(m.all_catalog_kinds()),
            }
            for name, m in sorted(collection.members.items())
        },
    }


def collection_fingerprint(config: Any) -> str:
    return recipe_fingerprint_of(collection_recipe(config))


def partition_recipe(config: Any, member: str, *, keyword_epoch: str | None = None) -> PartitionRecipe:
    """The ``PartitionRecipe`` of one member built by the shared write path under ``config``: the
    member fragments of today's index recipe without the dataset, store paths or other members
    (``store_recipe`` of the unified configuration)."""
    if member not in config.collection.members:
        raise ValueError(f"no member {member!r} in collection.members")
    declared = config.collection.members[member]
    build = declared.build_recipe()
    if not build:
        return store_recipe(config, keyword_epoch=keyword_epoch)
    # the configuration the member store is built with (``members.member_config``: its embed
    # fields, online vectors, restricted schema, no projection, the attach entries it keeps)
    from .members import member_config

    built = member_config(config, Path("."), member)
    recipe = store_recipe(built, keyword_epoch=keyword_epoch)
    space = embedding_space_ref(config) if recipe.embedding_space is not None else None  # always the collection's space
    return recipe.model_copy(update={"embedding_space": space, "content": {**recipe.content, "member_build": build}})


def store_recipe(config: Any, *, keyword_epoch: str | None = None) -> PartitionRecipe:
    """The location-free ``PartitionRecipe`` of the one store ``config`` builds (§6.2). For a v2
    configuration this is the decomposition of the store's own whole-config recipe: read-only
    adoption (``ingestion/adopt_member.py``) records it for the adopted member, computed from the
    original configuration alone (no backend, no store file)."""
    slots = write_slots(config)
    roles = config.models.roles
    content: dict[str, Any] = {
        "extractor": extractor_fingerprint(config),
        "communities": config.communities.recipe(),
        "community_model": None if roles.community is None else roles.community.model,
        "backends": {slot: {k: v for k, v in _backend_recipe(config.backends[name]).items() if k != "path"} for slot, name in sorted(slots.items())},
    }
    if keyword_epoch is not None:
        content["keyword_epoch"] = keyword_epoch
    if config.ingestion.attach:
        content["attach"] = [a.model_dump(mode="json", exclude={"path"}) for a in config.ingestion.attach]
    return PartitionRecipe(
        embedding_space=embedding_space_ref(config) if "vector" in slots else None,
        analyzer=analyzer_recipe(config, slots.get("lexical")),
        chunking=chunking_recipe(config),
        privacy=config.privacy.recipe(),
        content=content,
        projection=config.projection.recipe() if config.projection.enabled else None,
    )


def unified_fingerprint(config: Any, member_fingerprints: list[str], layers: list[dict[str, Any]] | None = None) -> str:
    """hash(collection recipe, sorted member fingerprints, layer recipes): the unified manifest's
    ``fingerprint`` (§6.2)."""
    return recipe_fingerprint_of({"collection": collection_recipe(config), "members": sorted(member_fingerprints), "layers": layers or []})
