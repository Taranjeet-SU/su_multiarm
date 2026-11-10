"""CAS-safe writer for the system-owned library files (addendum §3.6.3, owner amendment 2026-11-04).

The writer only appends, and only to ``learned.yaml``, ``proposed.yaml`` and ``decisions.yaml``.
Asked to write ``library.yaml`` (human-owned) or any other file, it raises ``SystemFileRefused``
before opening anything.

Append algorithm (the CAS key is the file's sha256):

1. read the file bytes, sha ``h0``; refuse a file that is not a YAML sequence (``SystemFileCorrupt``);
2. ``content = bytes + new rows`` (existing bytes are never rewritten);
3. write a temp file in the same directory and ``fsync`` it;
4. re-read the target; if its sha is no longer ``h0`` (a concurrent save), discard and restart;
5. ``os.replace``;
6. re-read; if the sha is not that of ``content`` (a writer landed after the replace) and the new
   rows are not in the file, restart from step 1. After ``retries`` attempts: ``SystemFileConflict``.

The sha of every write is recorded in ``.writer_state.json`` so the loader can report hand edits.

**Git (step 7, optional).** When the library directory is inside a git work tree and the caller
enables it (``learning.patterns.git_commit``), ``require_clean_system_files`` refuses to start
writing while a system file has uncommitted changes (``SystemFileDirty``: commit or revert the hand
edits first), and ``commit_system_files`` commits exactly the system files with the author
``system:<actor>``. ``library.yaml`` is never added or committed by the system.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from ...core.errors import SuError
from .library import DECISIONS_FILE, LEARNED_FILE, PROPOSED_FILE, SYSTEM_FILES, WRITER_STATE_FILE, Decision, PatternEntry


class SystemFileRefused(SuError, PermissionError):
    """A write to a file the system does not own (``library.yaml``) or does not know."""


class SystemFileCorrupt(SuError, ValueError):
    """A system file that no longer parses as a YAML sequence; the writer never repairs it."""


class SystemFileConflict(SuError, RuntimeError):
    """Concurrent writers kept changing the file; the append was not made."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(path: Path) -> bytes:
    return path.read_bytes() if path.exists() else b""


def _rows(data: bytes, name: str) -> list[Any]:
    if not data.strip():
        return []
    try:
        rows = yaml.safe_load(data.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise SystemFileCorrupt(f"{name} does not parse: {exc}") from None
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise SystemFileCorrupt(f"{name} must be a YAML sequence")
    return rows


def _record_state(root: Path, name: str, sha: str) -> None:
    path = root / WRITER_STATE_FILE
    try:
        state = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        state = {}
    state[name] = sha
    tmp = root / f".{WRITER_STATE_FILE}.{uuid.uuid4().hex}.tmp"
    tmp.write_text(json.dumps(state, sort_keys=True, indent=2))
    os.replace(tmp, path)


def append_rows(root: str | Path, name: str, rows: list[dict[str, Any]], *, retries: int = 5, _before_replace: Callable[[], None] | None = None) -> str:
    """Append ``rows`` to the system file ``name`` with the sha CAS; returns the new file sha."""
    if name not in SYSTEM_FILES:
        raise SystemFileRefused(f"the system never writes {name!r}; it appends only to {list(SYSTEM_FILES)}")
    root = Path(root)
    if not root.is_dir():
        raise SystemFileRefused(f"library directory {root} does not exist")
    target = root / name
    block = yaml.safe_dump(rows, sort_keys=False, allow_unicode=True, default_flow_style=False).encode("utf-8")
    for _ in range(retries):
        existing = _read(target)
        h0 = _sha(existing)
        _rows(existing, name)
        content = existing + (b"\n" if existing and not existing.endswith(b"\n") else b"") + block
        tmp = root / f".{name}.{uuid.uuid4().hex}.tmp"
        with open(tmp, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if _before_replace is not None:
            _before_replace()
        if _sha(_read(target)) != h0:
            tmp.unlink()
            continue
        os.replace(tmp, target)
        written = _read(target)
        if _sha(written) == _sha(content) or all(row in _rows(written, name) for row in rows):
            _record_state(root, name, _sha(written))
            return _sha(written)
    raise SystemFileConflict(f"{name} changed under the writer {retries} times; nothing was appended")


def append_decision(root: str | Path, decision: Decision, **kwargs: Any) -> str:
    return append_rows(root, DECISIONS_FILE, [decision.model_dump(mode="json", exclude_none=True)], **kwargs)


def append_proposal(root: str | Path, entry: PatternEntry, **kwargs: Any) -> str:
    if entry.status != "proposed":
        raise ValueError("a proposal is written with status 'proposed'; approval is a decision record")
    return append_rows(root, PROPOSED_FILE, [entry.model_dump(mode="json", exclude_none=True)], **kwargs)


def append_learned(root: str | Path, entry: PatternEntry, **kwargs: Any) -> str:
    if entry.status != "approved":
        raise ValueError("learned.yaml holds approved entries; pending ones go to proposed.yaml")
    return append_rows(root, LEARNED_FILE, [entry.model_dump(mode="json", exclude_none=True)], **kwargs)


# --- git (step 7) -------------------------------------------------------------------------------


class SystemFileDirty(SuError, RuntimeError):
    """A system file has uncommitted changes in the library's git work tree; nothing was written."""


class GitCommitFailed(SuError, RuntimeError):
    """``git commit`` of the system files failed; the appended files stay on disk, uncommitted."""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False, timeout=60)


def git_work_tree(root: str | Path) -> bool:
    """Whether ``root`` is inside a git work tree (``False`` when git is not installed)."""
    try:
        result = _git(Path(root), "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def dirty_system_files(root: str | Path) -> list[str]:
    """System files with uncommitted (or never committed) changes, by name."""
    root = Path(root)
    present = [name for name in SYSTEM_FILES if (root / name).exists()]
    if not present:
        return []
    result = _git(root, "status", "--porcelain", "--untracked-files=all", "--", *present)
    if result.returncode != 0:
        raise GitCommitFailed(f"git status failed: {result.stderr.strip()[:200]}")
    dirty = []
    for line in result.stdout.splitlines():
        name = Path(line[3:].strip().strip('"')).name
        if name in present and name not in dirty:
            dirty.append(name)
    return sorted(dirty)


def require_clean_system_files(root: str | Path) -> None:
    dirty = dirty_system_files(root)
    if dirty:
        raise SystemFileDirty(f"{dirty} have uncommitted changes in the library's git work tree; commit or revert them before the system writes")


def commit_system_files(root: str | Path, actor: str, message: str) -> str | None:
    """Commit the changed system files (only those); returns the commit sha, ``None`` when unchanged."""
    root = Path(root)
    changed = dirty_system_files(root)
    if not changed:
        return None
    added = _git(root, "add", "--", *changed)
    if added.returncode != 0:
        raise GitCommitFailed(f"git add failed: {added.stderr.strip()[:200]}")
    name = actor if actor.startswith("system:") else f"system:{actor}"
    result = _git(root, "commit", "--no-verify", "-m", message, f"--author={name} <pattern-library@su-multi-arm.invalid>", "--", *changed)
    if result.returncode != 0:
        raise GitCommitFailed(f"git commit failed: {(result.stderr or result.stdout).strip()[:200]}")
    return _git(root, "rev-parse", "HEAD").stdout.strip() or None
