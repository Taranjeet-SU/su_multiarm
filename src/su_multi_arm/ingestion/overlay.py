"""Fallback overlays of sealed log partitions (addendum A2 rev 2 §4.2.3 step 7, §4.2.8; review B2).

The LLM fallback runs **after** a partition version ``v`` is sealed. Its result is an overlay next to
the version; the sealed artifacts are never changed::

    v0001/fallback/<fallback_identity>/units/<unit>.json.gz   HeadAccount updates of one unit
    v0001/fallback/<fallback_identity>/manifest.json          written last: the overlay is sealed

``fallback_identity = sha(partition key, version, library identity, proposed-set identity, model
ids, prompt version, reply schema version, settings)``. Each unit file is written atomically (temp
file + rename), so a crash leaves complete unit files and no manifest: the next run skips the units
whose file exists and seals. Before the manifest every unit's identity H (``heads = Σ
final_status``) is checked again (``AccountingImbalance`` refuses the seal).

An overlay is **current** only when its identity equals the identity computed now. A stale overlay
(the library, the proposed set, a model or a prompt changed, or the partition was reparsed) is
never read as current; the fallback rebuilds it, from the reply cache when the replies are there.
Heads whose fallback has not been sealed keep ``unresolved: fallback_pending`` from the seal.
Overlays live inside the version directory, so ``expire`` deletes them with the partition.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import Field

from ..models import StrictModel
from .accounting import HeadAccount, UnitAccounting
from .partitions import FilePartitionStore, PartitionConflict, _atomic_write, _gz_json, _read_gz_json  # noqa: F401

OVERLAY_FORMAT = 1
IDENTITY = re.compile(r"^[0-9a-f]{32}$")


def proposed_identity(library: Any) -> str:
    """sha of the proposed (not approved) entries: a change in what waits for review changes the
    overlay identity, because provisional values come from those entries."""
    rows = sorted((e.entry.id, e.regex) for e in library.entries.values() if e.status == "proposed")
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def fallback_identity(
    *, partition_key: str, version: int, library_identity: str, proposed: str, models: dict[str, str], prompt_version: str, schema_version: str, settings_sha: str
) -> str:
    data = [OVERLAY_FORMAT, partition_key, version, library_identity, proposed, sorted(models.items()), prompt_version, schema_version, settings_sha]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()[:32]


class OverlayManifest(StrictModel):
    format: int = OVERLAY_FORMAT
    identity: str
    partition_key: str
    version: int
    library_identity: str
    units: list[str] = Field(default_factory=list)
    accounting: dict[str, UnitAccounting] = Field(default_factory=dict)
    report: dict[str, Any] = Field(default_factory=dict)  # content-free: counts, calls, spend, backlog
    created_at: datetime


class OverlayStore:
    """Overlays of one ``FilePartitionStore``."""

    def __init__(self, store: FilePartitionStore):
        self.store = store

    def _dir(self, key: str, version: int, identity: str) -> Path:
        if not IDENTITY.fullmatch(identity):
            raise ValueError(f"overlay identity {identity!r} is not a 32-hex digest")
        return self.store._vdir(key, version) / "fallback" / identity

    def _sealed_version(self, key: str, version: int) -> None:
        manifest = self.store.version(key, version)
        if manifest is None or manifest.status != "sealed":
            raise PartitionConflict(f"partition {self.store.source}/{key} v{version} is not sealed; an overlay derives from a sealed version")

    def has_unit(self, key: str, version: int, identity: str, unit_id: str) -> bool:
        return (self._dir(key, version, identity) / "units" / f"{unit_id}.json.gz").exists()

    def write_unit(self, key: str, version: int, identity: str, unit_id: str, heads: list[HeadAccount]) -> None:
        self._sealed_version(key, version)
        rows = [h.model_dump(mode="json") for h in sorted(heads, key=lambda h: h.head_id)]
        _atomic_write(self._dir(key, version, identity) / "units" / f"{unit_id}.json.gz", _gz_json(rows))

    def heads(self, key: str, version: int, identity: str, unit_id: str) -> list[HeadAccount]:
        path = self._dir(key, version, identity) / "units" / f"{unit_id}.json.gz"
        return [HeadAccount.model_validate(r) for r in _read_gz_json(path)] if path.exists() else []

    def seal(self, manifest: OverlayManifest) -> OverlayManifest:
        """Check identity H of every unit, then write the manifest (the overlay is now sealed)."""
        self._sealed_version(manifest.partition_key, manifest.version)
        for unit_id in manifest.units:
            if not self.has_unit(manifest.partition_key, manifest.version, manifest.identity, unit_id):
                raise PartitionConflict(f"overlay {manifest.identity} misses unit {unit_id}")
        for account in manifest.accounting.values():
            account.check()
        _atomic_write(self._dir(manifest.partition_key, manifest.version, manifest.identity) / "manifest.json", manifest.model_dump_json(indent=1).encode())
        return manifest

    def read(self, key: str, version: int, identity: str) -> OverlayManifest | None:
        path = self._dir(key, version, identity) / "manifest.json"
        return OverlayManifest.model_validate_json(path.read_text()) if path.exists() else None

    def _order(self, manifest: OverlayManifest) -> tuple[Any, int, str]:
        """Most recent first: ``created_at``, then the manifest's write time (two runs with one
        clock value, as in a rebuild within the same run or under a fixed test clock), then identity."""
        path = self._dir(manifest.partition_key, manifest.version, manifest.identity) / "manifest.json"
        return manifest.created_at, path.stat().st_mtime_ns if path.exists() else 0, manifest.identity

    def identities(self, key: str, version: int) -> list[str]:
        folder = self.store._vdir(key, version) / "fallback"
        return sorted(p.name for p in folder.iterdir() if p.is_dir() and IDENTITY.fullmatch(p.name)) if folder.exists() else []

    def current(self, key: str, identity: str) -> OverlayManifest | None:
        """The sealed overlay of the newest sealed version whose identity matches ``identity``;
        ``None`` when there is none (stale or missing: the caller rebuilds or reports pending)."""
        sealed = self.store.get(key)
        if sealed is None:
            return None
        return self.read(key, sealed.version, identity)

    def latest(self, key: str) -> OverlayManifest | None:
        """The most recently sealed overlay of the newest sealed version (for reports only; it may be
        stale, and ``report["stale"]`` says so when compared with a current identity)."""
        sealed = self.store.get(key)
        if sealed is None:
            return None
        found = [m for i in self.identities(key, sealed.version) if (m := self.read(key, sealed.version, i)) is not None]
        return max(found, key=lambda m: self._order(m)) if found else None


    def rows(self, key: str, identity: str | None = None, *, version: int | None = None) -> tuple[OverlayManifest, list[HeadAccount]] | None:
        """The read API for compose and snapshots: a sealed overlay and every ``HeadAccount`` it
        holds, ordered by unit then head id. ``identity`` selects it (the current one); ``None`` takes
        the most recently sealed overlay, which may be stale. ``version`` reads that partition
        version (default: the newest sealed one). ``None`` when there is none: the heads keep their
        sealed status (``fallback_pending`` for residue)."""
        if version is None:
            sealed = self.store.get(key)
            if sealed is None:
                return None
            version = sealed.version
        if identity is not None:
            manifest = self.read(key, version, identity)
        else:
            found = [m for i in self.identities(key, version) if (m := self.read(key, version, i)) is not None]
            manifest = max(found, key=lambda m: self._order(m)) if found else None
        if manifest is None:
            return None
        heads = [h for unit in manifest.units for h in self.heads(key, manifest.version, manifest.identity, unit)]
        return manifest, heads


def overlay_payload_digest(heads: list[HeadAccount]) -> str:
    """Deterministic digest of overlay content (determinism tests and run reports)."""
    return hashlib.sha256("\n".join(h.model_dump_json() for h in sorted(heads, key=lambda h: h.head_id)).encode()).hexdigest()


__all__ = ["OVERLAY_FORMAT", "OverlayManifest", "OverlayStore", "fallback_identity", "overlay_payload_digest", "proposed_identity"]
