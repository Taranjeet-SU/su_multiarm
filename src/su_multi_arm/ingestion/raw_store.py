"""Optional raw-copy store of pulled log units (``logs.partitions.raw``; off by default).

**Privacy.** This store holds source documents **exactly as pulled, before redaction**: e-mail
addresses, IPs, tokens, prompts, customer text, everything the source had. It exists only so that
redaction and parsing can be re-run (``logs reparse --from-raw``) without pulling from the source
again. Enabling it means unredacted log text sits on this machine for ``logs.retention.raw_days``
days. Rules:

- written by the puller (``schedule.LogsPipeline``) before redaction, one gzip (or plain) JSON file
  per unit, directory mode ``0700`` and file mode ``0600``, under a path that must lie outside the
  partition store, the backends and the pattern library (checked at config load);
- the parent directory must not be group- or world-writable (``RawLocationUnsafe``);
- read only in ``ingestion/raw_reparse.py`` (``logs reparse --from-raw``, the ``rederive`` step after
  a redaction change, and ``privacy scan --raw``); this class has no read method. The puller writes
  and retention lists and expires; an import-graph test pins the importers and the readers, and
  ``privacy scan`` skips the raw path unless ``--raw`` is given;
- expired by ``retention run`` after ``raw_days`` (shorter than the partition window), checked by
  ``retention verify`` (no day older than its cutoff, modes ``0700``/``0600``) and listed by
  ``retention report``.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any

from ..core.errors import SuError

READ_PURPOSES = frozenset({"reparse", "audit"})
DIR_MODE, FILE_MODE = 0o700, 0o600


class RawUnavailable(SuError):
    """The raw copy of a partition is missing or incomplete; ``logs settle``/``repull`` pulls the
    units from the source again instead."""


class RawAccessRefused(SuError, PermissionError):
    """A raw read for a purpose outside ``READ_PURPOSES``."""


class RawLocationUnsafe(SuError, PermissionError):
    """The raw path's parent is group- or world-writable (another account could replace it)."""


def check_parent(root: Path) -> Path:
    """The nearest existing ancestor of ``root`` must not be group- or world-writable."""
    parent = Path(root).resolve().parent
    while not parent.exists():
        parent = parent.parent
    mode = stat.S_IMODE(parent.stat().st_mode)
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RawLocationUnsafe(f"the raw copies' parent directory has mode {oct(mode)} (group- or world-writable); choose a private location")
    return parent


class RawStore:
    def __init__(self, root: Path, source: str, compression: str = "gzip"):
        self.root = Path(root)
        self.source = source
        self.compression = compression
        check_parent(self.root)

    # --- layout ------------------------------------------------------------------------------

    def _mkdir(self, path: Path) -> None:
        for folder in [*reversed(path.relative_to(self.root.parent).parents), path.relative_to(self.root.parent)]:
            full = self.root.parent / folder
            if full == self.root.parent or not str(full).startswith(str(self.root)):
                continue
            full.mkdir(exist_ok=True)
            os.chmod(full, DIR_MODE)

    def day_dir(self, key: str) -> Path:
        return self.root / self.source / key

    def unit_path(self, key: str, unit_id: str) -> Path:
        """Where a unit's copy lives (reading it is ``raw_reparse``'s job)."""
        return self._file(key, unit_id)

    def _file(self, key: str, unit_id: str) -> Path:
        suffix = ".json.gz" if self.compression == "gzip" else ".json"
        return self.day_dir(key) / f"{unit_id}{suffix}"

    # --- write -------------------------------------------------------------------------------

    def put_unit(self, key: str, unit_id: str, payload: dict[str, Any]) -> Path:
        """Write one unit's raw documents and pull metadata (mode 0600, atomic rename)."""
        folder = self.day_dir(key)
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, DIR_MODE)
        self._mkdir(folder)
        data = json.dumps(payload, sort_keys=True, default=str).encode()
        if self.compression == "gzip":
            data = gzip.compress(data, mtime=0)
        path = self._file(key, unit_id)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
        return path

    # --- read (restricted) ---------------------------------------------------------------------

    def keys(self) -> list[str]:
        folder = self.root / self.source
        return sorted(p.name for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []

    def unit_ids(self, key: str) -> list[str]:
        folder = self.day_dir(key)
        if not folder.is_dir():
            return []
        return sorted(p.name.split(".", 1)[0] for p in folder.iterdir() if p.is_file() and not p.name.endswith(".tmp"))

    # --- retention -----------------------------------------------------------------------------

    def expire(self, key: str) -> bool:
        folder = self.day_dir(key)
        if folder.exists():
            shutil.rmtree(folder)
            return True
        return False

    def mode_problems(self) -> list[str]:
        """Directories not exactly 0700 and files not exactly 0600, and an unsafe parent."""
        problems = []
        try:
            check_parent(self.root)
        except RawLocationUnsafe as exc:
            problems.append(str(exc))
        if not self.root.exists():
            return problems
        for path in [self.root, *self.root.rglob("*")]:
            mode = stat.S_IMODE(path.stat().st_mode)
            want = DIR_MODE if path.is_dir() else FILE_MODE
            if mode != want:
                problems.append(f"{path.relative_to(self.root.parent)}: mode {oct(mode)}, expected {oct(want)}")
        return problems

    def report(self) -> dict[str, Any]:
        return {"path": str(self.root), "days": self.keys(), "units": {k: len(self.unit_ids(k)) for k in self.keys()}}
