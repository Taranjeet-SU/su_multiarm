"""Generation pointer, manifests and failure records (plan §3.14 ``release.py``, Phase 0 subset).

Adapters never read ``CURRENT``; the registry does. ``activate`` is an atomic pointer switch and the
only way a generation becomes current. A failed build never touches the pointer: the failure record
lists the failing unit, its category and any staged external projections left for cleanup. The
sha256 release manifest, ``verify``/``rollback``/``gc`` arrive with Phase 2.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol, runtime_checkable

from ..core.types import GenerationId

HEX32 = set("0123456789abcdef")


def generation_id(manifest: dict[str, Any]) -> GenerationId:
    return GenerationId(
        dataset=manifest["dataset"],
        generation=manifest["generation"],
        index_format=int(manifest["index_format"]),
        fingerprint=manifest["fingerprint"],
    )


@runtime_checkable
class GenerationRegistry(Protocol):
    def current(self) -> dict[str, Any] | None: ...

    def manifest(self, generation: str) -> dict[str, Any] | None: ...

    def write_manifest(self, manifest: dict[str, Any]) -> None: ...

    def activate(self, manifest: dict[str, Any]) -> None: ...

    def generations(self) -> list[str]: ...

    # Used by the multi-backend write path when this registry is a *secondary* pointer (the backend
    # holds derived projections of a generation whose authoritative ``CURRENT`` lives elsewhere).
    def current_generation(self) -> str | None: ...

    def deactivate(self, generation: str) -> None:
        """Remove ``CURRENT`` only while it still names ``generation`` (rollback of a failed first release)."""
        ...

    def record_failure(self, failure: dict[str, Any]) -> None: ...

    def failures(self) -> list[dict[str, Any]]: ...

    def lock(self) -> Any: ...


class FileGenerationRegistry:
    """``<root>/CURRENT``, ``<root>/snapshots/<gen>/manifest.json``, ``<root>/failures/<gen>.json``."""

    def __init__(self, root: Path):
        self.root = Path(root)

    @contextmanager
    def lock(self) -> Iterator[None]:
        import fcntl

        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".write.lock").open("a") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield

    def snapshot_dir(self, generation: str) -> Path:
        return self.root / "snapshots" / generation

    def current_generation(self) -> str | None:
        pointer = self.root / "CURRENT"
        if not pointer.exists():
            return None
        generation = pointer.read_text().strip()
        if len(generation) != 32 or set(generation) - HEX32:
            raise ValueError("invalid index pointer")
        return generation

    def current(self) -> dict[str, Any] | None:
        generation = self.current_generation()
        if generation is None:
            return None
        manifest = self.manifest(generation)
        if manifest is None:
            raise ValueError(f"CURRENT names generation {generation} but its manifest is missing")
        return manifest

    def manifest(self, generation: str) -> dict[str, Any] | None:
        path = self.snapshot_dir(generation) / "manifest.json"
        return json.loads(path.read_text()) if path.exists() else None

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        path = self.snapshot_dir(manifest["generation"])
        path.mkdir(parents=True, exist_ok=True)
        (path / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))

    def activate(self, manifest: dict[str, Any]) -> None:
        generation = manifest["generation"]
        if self.manifest(generation) is None:
            raise ValueError("cannot activate a generation without a written manifest")
        pointer = self.root / f".CURRENT.{generation}"
        pointer.write_text(generation)
        os.replace(pointer, self.root / "CURRENT")

    def deactivate(self, generation: str) -> None:
        if self.current_generation() == generation:
            (self.root / "CURRENT").unlink()

    def generations(self) -> list[str]:
        base = self.root / "snapshots"
        return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.exists() else []

    def record_failure(self, failure: dict[str, Any]) -> None:
        folder = self.root / "failures"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{failure['generation']}.json").write_text(json.dumps(failure, indent=2, sort_keys=True))

    def failures(self) -> list[dict[str, Any]]:
        folder = self.root / "failures"
        if not folder.exists():
            return []
        return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))]


class MemoryGenerationRegistry:
    """In-memory registry for the memory backend (tests, contract suites)."""

    def __init__(self, backend: Any = None):
        self.backend = backend
        self._manifests: dict[str, dict[str, Any]] = {}
        self._current: str | None = None
        self._failures: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    @contextmanager
    def lock(self) -> Iterator[None]:
        with self._lock:
            yield

    def current(self) -> dict[str, Any] | None:
        return None if self._current is None else dict(self._manifests[self._current])

    def manifest(self, generation: str) -> dict[str, Any] | None:
        found = self._manifests.get(generation)
        return None if found is None else dict(found)

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        self._manifests[manifest["generation"]] = dict(manifest)

    def activate(self, manifest: dict[str, Any]) -> None:
        if manifest["generation"] not in self._manifests:
            raise ValueError("cannot activate a generation without a written manifest")
        self._current = manifest["generation"]

    def current_generation(self) -> str | None:
        return self._current

    def deactivate(self, generation: str) -> None:
        if self._current == generation:
            self._current = None

    def generations(self) -> list[str]:
        return sorted(self._manifests)

    def record_failure(self, failure: dict[str, Any]) -> None:
        self._failures.append(dict(failure))

    def failures(self) -> list[dict[str, Any]]:
        return [dict(f) for f in self._failures]
