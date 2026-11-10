"""Sealed, sha256-verified handoff of log units (docs/plans/unified_merge.md §6.7, P4; lane LB).

A **unit** is one sealed directory: a provisional slice of the live log path or an archive
day/month/catalog member. The log host is a producer: it builds and seals units, and the serving
host registers them only after verifying every byte.

Layout of a sealed unit::

    <unit>/unit.json   {"format", "kind": "slice" | "member", "ref": {...}, "files": {relpath: sha256}}
    <unit>/SEAL        sha256 of the canonical JSON of unit.json (the ``SliceRef.sha256``)
    <unit>/<files>     every file listed in unit.json, nothing else
    <unit>/member.json (archive members only) the member registry's listing of the verified
                       ``MemberRef``, written after sealing; outside the seal (``REGISTRY_FILE``)

``verify_unit`` recomputes every file digest and the seal and refuses a missing, extra or changed
file (``HandoffRefused``); there is no repair and no partial acceptance (§6.12: a verification
failure keeps the previous slice/member and the coverage says ``stale``).

``transfer`` copies a sealed unit into an ``incoming`` directory file by file. It is resumable: a
file already present with the sealed digest is not copied again, so an interrupted transfer
resumes where it stopped. A file whose digest still differs after its copy is refused (the source
changed or the medium corrupts data); the transfer never loops until it passes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from ..core.errors import SuError
from ..core.types_members import canonical_sha256

UNIT_FORMAT = 1
UNIT_FILE = "unit.json"
SEAL_FILE = "SEAL"
REGISTRY_FILE = "member.json"  # the member registry's listing file (ingestion/log_members.py), not sealed
PARTIAL_SUFFIX = ".partial"


class HandoffRefused(SuError):
    """A unit failed verification (digest, seal, missing or extra file) and is not registered."""


def file_sha256(path: Path, block: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(block):
            digest.update(chunk)
    return digest.hexdigest()


def _listing(folder: Path) -> list[str]:
    """Every regular file under ``folder`` as a posix relative path, except the seal files."""
    out = []
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            rel = path.relative_to(folder).as_posix()
            if rel not in {UNIT_FILE, SEAL_FILE, REGISTRY_FILE}:
                out.append(rel)
    return out


def seal_unit(folder: Path, kind: str, ref: dict[str, Any]) -> str:
    """Write ``unit.json`` (digests of every file in ``folder``) and ``SEAL``; return the seal sha.
    ``ref`` is the unit's reference without its own ``sha256`` (the seal is that value)."""
    folder = Path(folder)
    files = {rel: file_sha256(folder / rel) for rel in _listing(folder)}
    body = {"format": UNIT_FORMAT, "kind": kind, "ref": ref, "files": files}
    seal = canonical_sha256(body)
    (folder / UNIT_FILE).write_text(json.dumps(body, sort_keys=True, separators=(",", ":")))
    (folder / SEAL_FILE).write_text(seal + "\n")
    return seal


def read_sealed(folder: Path) -> dict[str, Any]:
    """``unit.json`` plus ``seal`` as recorded (not verified)."""
    folder = Path(folder)
    try:
        body = json.loads((folder / UNIT_FILE).read_text())
        seal = (folder / SEAL_FILE).read_text().strip()
    except (OSError, ValueError) as exc:
        raise HandoffRefused(f"{folder.name}: unit.json/SEAL unreadable ({exc})") from exc
    if body.get("format") != UNIT_FORMAT:
        raise HandoffRefused(f"{folder.name}: unit format {body.get('format')!r} is not {UNIT_FORMAT}")
    return {**body, "seal": seal}


def verify_unit(folder: Path, expected_seal: str | None = None) -> dict[str, Any]:
    """Recompute the seal and every file digest; refuse any difference. Returns the unit body."""
    folder = Path(folder)
    body = read_sealed(folder)
    seal = body.pop("seal")
    if canonical_sha256(body) != seal:
        raise HandoffRefused(f"{folder.name}: unit.json does not match its SEAL")
    if expected_seal is not None and seal != expected_seal:
        raise HandoffRefused(f"{folder.name}: seal {seal[:12]} is not the expected {expected_seal[:12]}")
    listed = body["files"]
    present = _listing(folder)
    extra = sorted(set(present) - set(listed))
    missing = sorted(set(listed) - set(present))
    if extra or missing:
        raise HandoffRefused(f"{folder.name}: files differ from the seal (missing {missing[:3]}, extra {extra[:3]})")
    for rel, digest in listed.items():
        if file_sha256(folder / rel) != digest:
            raise HandoffRefused(f"{folder.name}: {rel} does not match its sealed sha256")
    return {**body, "seal": seal}


def transfer(source: Path, incoming: Path) -> Path:
    """Copy the sealed unit ``source`` to ``incoming/<name>`` (resumable; see the module doc).

    The copy goes to ``<name>.partial`` first and is renamed once every file verifies, so a reader
    never sees a half-copied unit under its final name. An existing complete copy with the same
    seal is returned as is; one with another seal is refused."""
    source, incoming = Path(source), Path(incoming)
    body = read_sealed(source)
    final = incoming / source.name
    if final.exists():
        verify_unit(final, expected_seal=body["seal"])
        return final
    partial = incoming / (source.name + PARTIAL_SUFFIX)
    partial.mkdir(parents=True, exist_ok=True)
    for rel, digest in body["files"].items():
        target = partial / rel
        if target.is_file() and file_sha256(target) == digest:
            continue  # resumed: already copied and verified
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        shutil.copyfile(source / rel, tmp)
        if file_sha256(tmp) != digest:
            tmp.unlink(missing_ok=True)
            raise HandoffRefused(f"{source.name}: {rel} differs from its sealed sha256 after the copy")
        os.replace(tmp, target)
    for name in (UNIT_FILE, SEAL_FILE):
        shutil.copyfile(source / name, partial / name)
    for stray in [p for p in partial.rglob("*.tmp") if p.is_file()]:
        stray.unlink()
    verify_unit(partial, expected_seal=body["seal"])
    os.replace(partial, final)
    return final
