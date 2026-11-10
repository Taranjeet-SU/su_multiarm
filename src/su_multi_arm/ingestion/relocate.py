"""Adopt a store that was moved or copied to another location (``index adopt``; QA-13).

The index fingerprint covers each backend's recipe, and a file backend's recipe includes its
``path``, so a state directory copied elsewhere is refused at serving time ("index/config
fingerprint mismatch") until it is rebuilt. ``adopt`` is the explicit, audited alternative for the
one safe case: the same index at a new location.

``su-multi-arm --config NEW index adopt --from-config OLD`` (``--dry-run`` checks only):

1. the active generation of the store NEW points at must have been built by OLD's recipe: its
   manifest fingerprint equals the fingerprint of OLD's index recipe (computed by NEW's write path
   from OLD's configuration);
2. OLD's and NEW's index recipes may differ **only in location fields**:

   * ``backends.<slot>.path`` (a file backend's directory);
   * a path field of an imported file pinned by a sibling digest (``sha256`` or ``<field>_sha256``,
     unchanged between the two recipes) whose file at NEW's path still has that SHA-256;

   any other difference (chunking, embedder, privacy, extractor, a digest, a backend kind ...) is
   refused, as is a manifest fingerprint that matches neither recipe;
3. then, under the write lease, the manifest of the active generation is re-stamped for NEW
   (``fingerprint``, ``config_fingerprint``, ``legacy_fingerprint``) in the authoritative registry
   and in every secondary registry that holds the generation, with a ``relocations`` audit entry
   (time, both fingerprints, the changed fields, the verified files), and the generation must pin.

Golden fingerprint values are unchanged: nothing about how a fingerprint is computed changes; only
an existing manifest is re-stamped after these checks.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Iterator

from ..core.errors import IndexNotReady

MISSING = object()


class RelocationRefused(ValueError):
    """The two configurations do not describe the same index at another location."""


def _walk(old: Any, new: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any, Any]]:
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new), key=str):
            if key not in old or key not in new:
                yield (*path, str(key)), old.get(key, MISSING), new.get(key, MISSING)
            else:
                yield from _walk(old[key], new[key], (*path, str(key)))
    elif old != new:
        yield path, old, new


def _node(recipe: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = recipe
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return MISSING
        node = node[key]
    return node


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def location_changes(old_recipe: dict[str, Any], new_recipe: dict[str, Any], base: Path | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """``(allowed location changes, refused differences)`` between two index recipes. Imported files
    are verified against their pinned digest at the new path (relative paths against ``base``)."""
    allowed: list[dict[str, Any]] = []
    refused: list[str] = []
    for path, before, after in _walk(old_recipe, new_recipe):
        dotted = ".".join(path)
        if before is MISSING or after is MISSING or not isinstance(before, str) or not isinstance(after, str):
            refused.append(f"{dotted}: {'added' if before is MISSING else 'removed' if after is MISSING else 'changed'}")
            continue
        if len(path) == 3 and path[0] == "backends" and path[2] == "path":
            allowed.append({"field": dotted, "kind": "backend_path", "from": before, "to": after})
            continue
        parent_old, parent_new = _node(old_recipe, path[:-1]), _node(new_recipe, path[:-1])
        digest_key = next((k for k in (f"{path[-1]}_sha256", "sha256") if isinstance(parent_new, dict) and k in parent_new), None)
        if digest_key is None or not isinstance(parent_old, dict) or parent_old.get(digest_key) != parent_new.get(digest_key):
            refused.append(f"{dotted}: changed (not a location field)")
            continue
        target = Path(after)
        if not target.is_absolute() and base is not None:
            target = base / target
        if not target.is_file():
            refused.append(f"{dotted}: imported file {after!r} does not exist")
            continue
        actual = sha256_file(target)
        if actual != parent_new[digest_key]:
            refused.append(f"{dotted}: imported file {after!r} has sha256 {actual}, the recipe pins {parent_new[digest_key]}")
            continue
        allowed.append({"field": dotted, "kind": "imported_file", "from": before, "to": after, "sha256": actual})
    return allowed, refused


def adopt(new_config: Any, old_config: Any, *, dry_run: bool = False, base: Path | None = None) -> dict[str, Any]:
    """Re-stamp the active generation of NEW's store for NEW (see the module docstring)."""
    from ..engine import Engine
    from .write import recipe_fingerprint_of

    if old_config.dataset != new_config.dataset:
        raise RelocationRefused(f"dataset differs: {old_config.dataset!r} (old) vs {new_config.dataset!r} (new)")
    engine = Engine(new_config)
    write = engine.write_path
    old_recipe = write.index_recipe(old_config)
    new_recipe = write.index_recipe()
    old_fingerprint, new_fingerprint = recipe_fingerprint_of(old_recipe), recipe_fingerprint_of(new_recipe)
    with write.registry.lock():
        manifest = write.registry.current()
        if manifest is None:
            raise IndexNotReady("the store of the new configuration has no active generation; nothing to adopt")
        if manifest.get("schema_version") != 2:
            raise RelocationRefused("the active generation was built by a schema_version 1 engine; rebuild instead")
        generation = manifest["generation"]
        report: dict[str, Any] = {"generation": generation, "dataset": manifest.get("dataset"), "from_fingerprint": manifest["fingerprint"], "to_fingerprint": new_fingerprint, "dry_run": dry_run}
        if manifest["fingerprint"] == new_fingerprint:
            return {**report, "status": "already_adopted", "changes": []}
        if manifest["fingerprint"] != old_fingerprint:
            raise RelocationRefused(
                f"the active generation's fingerprint {manifest['fingerprint'][:16]}… matches neither the old configuration's recipe "
                f"({old_fingerprint[:16]}…) nor the new one's; it was not built by the old configuration"
            )
        allowed, refused = location_changes(old_recipe, new_recipe, base)
        if refused:
            raise RelocationRefused("the two index recipes differ in more than location fields: " + "; ".join(refused))
        if not allowed:  # equal recipes but different fingerprints cannot happen; refuse rather than guess
            raise RelocationRefused("no location change found between the two recipes")
        report["changes"] = allowed
        if dry_run:
            return {**report, "status": "verified"}
        entry = {
            "at": time.time(),
            "from_fingerprint": manifest["fingerprint"],
            "to_fingerprint": new_fingerprint,
            "from_config_fingerprint": manifest.get("config_fingerprint"),
            "to_config_fingerprint": new_config.fingerprint(),
            "changes": allowed,
        }
        restamped = {
            **manifest,
            "fingerprint": new_fingerprint,
            "config_fingerprint": new_config.fingerprint(),
            "legacy_fingerprint": write.legacy_fingerprint(),
            "relocations": [*(manifest.get("relocations") or []), entry],
        }
        rewritten = []
        for holder in write.generations.secondaries:
            if holder.activation != "registry":
                continue
            registry = holder.backend.generation_registry()
            if registry.manifest(generation) is not None:
                registry.write_manifest(restamped)
                rewritten.append(holder.name)
        write.registry.write_manifest(restamped)
        rewritten.append(write.cfg.storage.catalog)
    write.pin()  # the adopted generation must now serve (raises otherwise)
    return {**report, "status": "adopted", "registries": rewritten}
