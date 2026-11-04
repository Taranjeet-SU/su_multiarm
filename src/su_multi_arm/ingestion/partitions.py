"""Day partitions of a log source (addendum A1 §3.5, slice L3a).

A partition is one day (``YYYY-MM-DD``; what a day means is the partition clock, see
``core/config_logs.py``) of one source, stored in versions under ``<root>/<source>/<day>/``::

    v0001/manifest.json          PartitionManifest (content-free: counts, ids, hashes, provenance)
    v0001/units/<unit>.json.gz   one pulled unit: unit, count, sample plan, redacted LogRecords
    v0001/matches.jsonl.gz       per record: status, signature, pattern (no text)
    v0001/aggregates.json        unit rows and signature rows of the day
    v0001/hourly_cells.json      signature x deployment x hour counts (co-firing substrate, N6)
    v0001/exemplars.json         up to k redacted records per signature
    v0001/unparsed.jsonl.gz      unparsed / format_only records and sampled fragments (redacted)
    v0001/fallback/<identity>/   LLM fallback overlay derived after the seal (``ingestion/overlay.py``)
    tombstone.json               after ``expire``: content-free, with the verify sample hashes

``begin`` opens a version (or resumes the open one, only with the same ``source_query_sha``);
``put_unit`` stores one unit atomically (temp file + ``os.replace``), so a crashed pull resumes at
the next missing unit without duplicates; ``seal`` writes the parse artifacts and freezes the
version. A sealed version is immutable: ``reparse`` (new library) and ``repull`` (settle) write
``v+1``. ``expire`` deletes every version and leaves a tombstone.

**Schedule lock.** The store is also a ``LeaseStore`` (``ingestion/lease.py``): an exclusive
``flock`` on ``<root>/.schedule.lock``. ``run-daily`` holds only this lock, never the registry write
lease; compose and ``gc`` take the write lease per call (§3.8.1, B5).

Raw records are kept per unit as gzip JSON (the addendum's ``raw.jsonl.zst``: zstd is not a package
dependency); they are already redacted (the mapping refuses to run without a redactor).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal

from pydantic import Field

from ..core.errors import SuError
from ..models import StrictModel
from ..sources.contracts import SamplePlan, SourceUnit, UnitCount
from ..sources.logs import LogRecord
from .lease import LEASE_TTL_SECONDS, hold

KEY = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
SOURCE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
UNIT = re.compile(r"^[0-9a-f]{8,64}$")


class PartitionConflict(SuError):
    """``begin`` on an open partition with another source query, or a write to a sealed version."""


class PartitionMissing(SuError):
    pass


class UnitArtifacts(StrictModel):
    unit: SourceUnit
    count: UnitCount
    sample: SamplePlan
    records: list[LogRecord] = Field(default_factory=list)
    fragments: int = 0
    fragment_samples: list[LogRecord] = Field(default_factory=list)
    multiline: dict[str, int] = Field(default_factory=dict)
    mapping: dict[str, Any] = Field(default_factory=dict)
    capped: bool = False
    restarts: int = 0
    records_query: str | None = None  # the count query that selects the records tier (None: every line)
    records_levels: list[str] = Field(default_factory=list)
    records_basis: Literal["level_field", "token_match"] | None = None
    # Accounting (A2 review B1): documents the puller handed over and the mapper's malformed count
    # for this unit. ``None`` on units stored before accounting existed (identities "not recorded").
    docs_pulled: int | None = None
    malformed: int | None = None


class PartitionManifest(StrictModel):
    source: str
    key: str
    version: int = Field(ge=1)
    status: Literal["open", "sealed"] = "open"
    # Parse-artefact format (``logs_parse.PARTITION_FORMAT``); manifests written before format 2
    # carry no key and read as 1. Compose reparses an older format from the retained records.
    format: int = Field(default=1, ge=1)
    reason: Literal["pull", "reparse", "repull"] = "pull"
    parent_version: int | None = None
    source_query_sha: str
    clock: str = "index_date"
    timezone: str = "UTC"
    window_start: datetime | None = None
    window_end: datetime | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)  # pattern provenance (library identity, engine)
    units: list[str] = Field(default_factory=list)
    unit_counts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    sample_methods: dict[str, str] = Field(default_factory=dict)
    capped: bool = False
    counts: dict[str, int] = Field(default_factory=dict)
    redaction: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)  # name -> sha256
    # Per-unit accounting and coverage (A2 §4.2.2): the identities are checked before the seal.
    accounting: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    sealed_at: datetime | None = None

    @property
    def library_identity(self) -> str | None:
        return self.provenance.get("library_identity")


class Tombstone(StrictModel):
    source: str
    key: str
    expired_at: datetime
    versions: list[int] = Field(default_factory=list)
    deleted: dict[str, int] = Field(default_factory=dict)
    # sha256 of up to N redacted lines and of their source ids (content-based verify, §3.8.3)
    verify_sample: list[str] = Field(default_factory=list)
    verify_source_ids: list[str] = Field(default_factory=list)


def line_hash(text: str) -> str:
    return hashlib.sha256(("line\0" + text.strip()).encode()).hexdigest()


def source_id_hash(source_id: str) -> str:
    return hashlib.sha256(("source_id\0" + source_id).encode()).hexdigest()


def record_lines(record: LogRecord) -> list[str]:
    """The text lines of a record (message, then stack lines), non-blank."""
    lines = record.message.split("\n") + (record.stack.split("\n") if record.stack else [])
    return [line for line in lines if line.strip()]


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _gz_json(payload: Any) -> bytes:
    return gzip.compress(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode(), mtime=0)


def _read_gz_json(path: Path) -> Any:
    return json.loads(gzip.decompress(path.read_bytes()))


def _gz_jsonl(rows: Iterable[dict[str, Any]]) -> bytes:
    text = "".join(json.dumps(r, sort_keys=True, ensure_ascii=False, default=str) + "\n" for r in rows)
    return gzip.compress(text.encode(), mtime=0)


def _read_gz_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in gzip.decompress(path.read_bytes()).decode().splitlines() if line]


class FilePartitionStore:
    """The partition store on a local directory (one per ``logs.partitions.path``)."""

    kind = "partitions"

    def __init__(self, root: Path, source: str, clock: Any = None):
        if not SOURCE.fullmatch(source):
            raise ValueError(f"source name {source!r} is not an identifier")
        self.root = Path(root)
        self.source = source
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._handles: dict[str, Any] = {}
        self._guard = threading.Lock()

    # --- schedule lock (LeaseStore) ------------------------------------------------------------

    def lease_acquire(self, token: str, ttl: float) -> bool:
        import fcntl

        self.root.mkdir(parents=True, exist_ok=True)
        handle = (self.root / ".schedule.lock").open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(token)
        handle.flush()
        with self._guard:
            self._handles[token] = handle
        return True

    def lease_renew(self, token: str, ttl: float) -> bool:
        with self._guard:
            handle = self._handles.get(token)
        return handle is not None and not handle.closed

    def lease_release(self, token: str) -> bool:
        import fcntl

        with self._guard:
            handle = self._handles.pop(token, None)
        if handle is None:
            return False
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
        return True

    @contextmanager
    def schedule_lock(self, ttl: float = LEASE_TTL_SECONDS) -> Iterator[Any]:
        """The schedule lock: a held lock is ``WriteConflict(held)``, never a wait."""
        with hold(self, "partitions", ttl=ttl, held_message="the schedule lock is held by another run") as lease:
            yield lease

    # --- layout --------------------------------------------------------------------------------

    def _dir(self, key: str) -> Path:
        if not KEY.fullmatch(key):
            raise ValueError(f"partition key {key!r} is not YYYY-MM-DD")
        return self.root / self.source / key

    def _vdir(self, key: str, version: int) -> Path:
        return self._dir(key) / f"v{version:04d}"

    def _versions(self, key: str) -> list[int]:
        folder = self._dir(key)
        if not folder.exists():
            return []
        return sorted(int(p.name[1:]) for p in folder.iterdir() if p.is_dir() and re.fullmatch(r"v[0-9]{4}", p.name))

    def _read_manifest(self, key: str, version: int) -> PartitionManifest | None:
        path = self._vdir(key, version) / "manifest.json"
        return PartitionManifest.model_validate_json(path.read_text()) if path.exists() else None

    def _write_manifest(self, manifest: PartitionManifest) -> None:
        _atomic_write(self._vdir(manifest.key, manifest.version) / "manifest.json", manifest.model_dump_json(indent=2).encode())

    def latest(self, key: str) -> PartitionManifest | None:
        versions = self._versions(key)
        return self._read_manifest(key, versions[-1]) if versions else None

    def get(self, key: str) -> PartitionManifest | None:
        """The newest **sealed** version (what compose reads), else ``None``."""
        for version in reversed(self._versions(key)):
            manifest = self._read_manifest(key, version)
            if manifest is not None and manifest.status == "sealed":
                return manifest
        return None

    def version(self, key: str, version: int) -> PartitionManifest | None:
        return self._read_manifest(key, version)

    def keys(self) -> list[str]:
        base = self.root / self.source
        if not base.exists():
            return []
        return sorted(p.name for p in base.iterdir() if p.is_dir() and KEY.fullmatch(p.name) and self._versions(p.name))

    def list(self, since: str | None = None) -> list[PartitionManifest]:
        out = []
        for key in self.keys():
            if since is not None and key < since:
                continue
            manifest = self.get(key)
            if manifest is not None:
                out.append(manifest)
        return out

    def tombstone(self, key: str) -> Tombstone | None:
        path = self._dir(key) / "tombstone.json"
        return Tombstone.model_validate_json(path.read_text()) if path.exists() else None

    def tombstones(self) -> list[Tombstone]:
        base = self.root / self.source
        if not base.exists():
            return []
        return [t for p in sorted(base.iterdir()) if KEY.fullmatch(p.name) and (t := self.tombstone(p.name)) is not None]

    # --- write ---------------------------------------------------------------------------------

    def begin(self, key: str, source_query_sha: str, provenance: dict[str, Any], *, clock: str = "index_date", tz: str = "UTC", window: tuple[datetime, datetime] | None = None) -> PartitionManifest:
        """Open ``v1`` (or ``v+1`` after a sealed version), or resume the open version when its
        ``source_query_sha`` matches; another sha is ``PartitionConflict`` (N12)."""
        latest = self.latest(key)
        if latest is not None and latest.status == "open":
            if latest.source_query_sha != source_query_sha:
                raise PartitionConflict(f"partition {self.source}/{key} v{latest.version} is open with another source query; finish or expire it first")
            return latest
        manifest = PartitionManifest(
            source=self.source,
            key=key,
            version=1 if latest is None else latest.version + 1,
            parent_version=None if latest is None else latest.version,
            source_query_sha=source_query_sha,
            clock=clock,
            timezone=tz,
            window_start=window[0] if window else None,
            window_end=window[1] if window else None,
            provenance=dict(provenance),
            created_at=self.clock(),
        )
        self._write_manifest(manifest)
        return manifest

    def _open(self, key: str) -> PartitionManifest:
        latest = self.latest(key)
        if latest is None:
            raise PartitionMissing(f"partition {self.source}/{key} has not begun")
        if latest.status != "open":
            raise PartitionConflict(f"partition {self.source}/{key} v{latest.version} is sealed (immutable)")
        return latest

    def has_unit(self, key: str, unit_id: str) -> bool:
        latest = self.latest(key)
        return latest is not None and unit_id in latest.units

    def put_unit(self, key: str, unit_id: str, artifacts: UnitArtifacts) -> None:
        """Store one unit atomically, then record it in the manifest (a crash between the two
        leaves an unlisted file that the resumed pull overwrites)."""
        if not UNIT.fullmatch(unit_id) or artifacts.unit.id != unit_id:
            raise ValueError(f"unit id {unit_id!r} does not match the artifacts")
        manifest = self._open(key)
        path = self._vdir(key, manifest.version) / "units" / f"{unit_id}.json.gz"
        _atomic_write(path, _gz_json(artifacts.model_dump(mode="json")))
        if unit_id not in manifest.units:
            manifest.units.append(unit_id)
        manifest.unit_counts[unit_id] = {"total": artifacts.count.total, "by_query": dict(artifacts.count.by_query), "basis": dict(artifacts.count.basis), "index": artifacts.unit.index}
        manifest.sample_methods[unit_id] = artifacts.sample.label()
        manifest.capped = manifest.capped or artifacts.capped
        self._write_manifest(manifest)

    def units(self, key: str, version: int | None = None) -> Iterator[UnitArtifacts]:
        manifest = self.latest(key) if version is None else self._read_manifest(key, version)
        if manifest is None:
            raise PartitionMissing(f"partition {self.source}/{key} has no version {version}")
        folder = self._vdir(key, manifest.version) / "units"
        for unit_id in sorted(manifest.units):
            yield UnitArtifacts.model_validate(_read_gz_json(folder / f"{unit_id}.json.gz"))

    def seal(self, key: str, parsed: Any, *, provenance: dict[str, Any] | None = None, redaction: dict[str, Any] | None = None) -> PartitionManifest:
        """Write the parse artifacts of the open version and freeze it. ``parsed`` is a
        ``logs_parse.ParsedPartition``."""
        manifest = self._open(key)
        accounts = list(getattr(parsed, "accounting", None) or [])
        for account in accounts:
            account.check()  # AccountingImbalance: nothing is written, the version stays open
        folder = self._vdir(key, manifest.version)
        files = {
            "matches.jsonl.gz": _gz_jsonl(parsed.matches),
            "aggregates.json": json.dumps(parsed.aggregates, sort_keys=True, indent=1).encode(),
            "hourly_cells.json": json.dumps(parsed.hourly_cells, sort_keys=True).encode(),
            "exemplars.json": json.dumps(parsed.exemplars, sort_keys=True, ensure_ascii=False).encode(),
            "unparsed.jsonl.gz": _gz_jsonl(parsed.unparsed),
        }
        if getattr(parsed, "patterns", None):  # format 2: library digest for the delta reparse
            files["patterns.json"] = json.dumps(parsed.patterns, sort_keys=True).encode()
        artifacts = {}
        for name, data in files.items():
            _atomic_write(folder / name, data)
            artifacts[name] = hashlib.sha256(data).hexdigest()
        for unit_id in sorted(manifest.units):
            artifacts[f"units/{unit_id}.json.gz"] = _sha_file(folder / "units" / f"{unit_id}.json.gz")
        manifest.artifacts = artifacts
        manifest.counts = dict(parsed.counts)
        manifest.format = int(getattr(parsed, "format", 1))
        if accounts:
            from .accounting import coverage_metrics

            manifest.accounting = {"units": {a.unit_id: a.model_dump(mode="json") for a in accounts}, "coverage": coverage_metrics(accounts)}
        if provenance is not None:
            manifest.provenance = dict(provenance)
        if redaction is not None:
            manifest.redaction = dict(redaction)
        manifest.status = "sealed"
        manifest.sealed_at = self.clock()
        self._write_manifest(manifest)
        return manifest

    def read(self, key: str, name: str, version: int | None = None) -> Any:
        """A parse artifact of the newest sealed version (or ``version``)."""
        manifest = self.get(key) if version is None else self._read_manifest(key, version)
        if manifest is None:
            raise PartitionMissing(f"partition {self.source}/{key} is not sealed")
        path = self._vdir(key, manifest.version) / name
        if name.endswith(".jsonl.gz"):
            return _read_gz_jsonl(path)
        return json.loads(path.read_text())

    def _derive(self, key: str, reason: Literal["reparse", "repull"], keep_units: Iterable[str], provenance: dict[str, Any]) -> PartitionManifest:
        base = self.get(key)
        if base is None:
            raise PartitionMissing(f"partition {self.source}/{key} has no sealed version to {reason}")
        latest = self.latest(key)
        if latest is not None and latest.status == "open":
            raise PartitionConflict(f"partition {self.source}/{key} v{latest.version} is open; finish it before a {reason}")
        child = base.model_copy(
            update={
                "version": latest.version + 1,
                "parent_version": base.version,
                "status": "open",
                "reason": reason,
                "provenance": dict(provenance),
                "units": [],
                "unit_counts": {},
                "sample_methods": {},
                "capped": False,
                "counts": {},
                "artifacts": {},
                "accounting": {},
                "created_at": self.clock(),
                "sealed_at": None,
            }
        )
        source_dir, target_dir = self._vdir(key, base.version) / "units", self._vdir(key, child.version) / "units"
        target_dir.mkdir(parents=True, exist_ok=True)
        for unit_id in sorted(keep_units):
            src, dst = source_dir / f"{unit_id}.json.gz", target_dir / f"{unit_id}.json.gz"
            try:
                os.link(src, dst)  # immutable files: a hard link is a copy
            except OSError:
                shutil.copy2(src, dst)
            child.units.append(unit_id)
            child.unit_counts[unit_id] = base.unit_counts[unit_id]
            child.sample_methods[unit_id] = base.sample_methods[unit_id]
            artifacts = UnitArtifacts.model_validate(_read_gz_json(dst))
            child.capped = child.capped or artifacts.capped
        self._write_manifest(child)
        return child

    def reparse(self, key: str, library: Any, parse: Any) -> PartitionManifest:
        """``v+1`` from the retained redacted records under ``library`` (a ``CompiledLibrary``);
        ``parse(units, library)`` returns the ``ParsedPartition``."""
        base = self.get(key)
        if base is None:
            raise PartitionMissing(f"partition {self.source}/{key} has no sealed version to reparse")
        provenance = library.provenance()
        child = self._derive(key, "reparse", base.units, provenance)
        parsed = parse(list(self.units(key, child.version)), library)
        return self.seal(key, parsed, provenance=provenance, redaction=base.redaction)

    def restart(self, key: str, provenance: dict[str, Any]) -> PartitionManifest:
        """``v+1`` (open, no units) of a sealed day, rebuilt by the caller from another input
        (``logs reparse --from-raw``)."""
        return self._derive(key, "reparse", [], provenance)

    def repull(self, key: str, units: list[str]) -> PartitionManifest:
        """``v+1`` (open) holding every unit of the sealed version except ``units``, which the
        caller pulls again before sealing (settle, N12)."""
        base = self.get(key)
        if base is None:
            raise PartitionMissing(f"partition {self.source}/{key} has no sealed version to repull")
        unknown = sorted(set(units) - set(base.units))
        if unknown:
            raise ValueError(f"units {unknown} are not in {self.source}/{key} v{base.version}")
        return self._derive(key, "repull", [u for u in base.units if u not in set(units)], base.provenance)

    def expire(self, key: str, sample_size: int = 1000, keep_hashes: set[str] | None = None) -> Tombstone:
        """Delete every version; leave a content-free tombstone with the verify sample: hashes of up
        to ``sample_size`` redacted lines (exemplars and the unparsed queue first, then the rest)
        that do **not** also occur in ``keep_hashes`` (lines of in-window partitions), and of
        their source ids."""
        folder = self._dir(key)
        versions = self._versions(key)
        lines: list[str] = []
        source_ids: list[str] = []
        seen: set[str] = set()
        keep = keep_hashes or set()

        def add_line(text: str) -> None:
            digest = line_hash(text)
            if digest not in seen and digest not in keep and len(lines) < sample_size:
                seen.add(digest)
                lines.append(digest)

        files = 0
        if versions:
            newest = versions[-1]
            vdir = self._vdir(key, newest)
            for name in ("exemplars.json",):
                if (vdir / name).exists():
                    for rows in json.loads((vdir / name).read_text()).values():
                        for row in rows:
                            for text in row["text"].split("\n"):
                                if text.strip():
                                    add_line(text)
            if (vdir / "unparsed.jsonl.gz").exists():
                for row in _read_gz_jsonl(vdir / "unparsed.jsonl.gz"):
                    for text in row["text"].split("\n"):
                        if text.strip():
                            add_line(text)
            if (vdir / "units").exists():
                for artifacts in self.units(key, newest):
                    for record in artifacts.records:
                        if len(source_ids) < sample_size:
                            source_ids.append(source_id_hash(record.source_id))
                        for text in record_lines(record):
                            add_line(text)
        for version in versions:
            files += sum(1 for p in self._vdir(key, version).rglob("*") if p.is_file())
        tombstone = Tombstone(
            source=self.source,
            key=key,
            expired_at=self.clock(),
            versions=versions,
            deleted={"versions": len(versions), "files": files},
            verify_sample=lines,
            verify_source_ids=source_ids,
        )
        for version in versions:
            shutil.rmtree(self._vdir(key, version))
        for leftover in folder.glob(".*.tmp") if folder.exists() else []:
            leftover.unlink()
        _atomic_write(folder / "tombstone.json", tombstone.model_dump_json(indent=2).encode())
        return tombstone

    # --- content scan (retention verify) --------------------------------------------------------

    def line_hashes(self, key: str) -> set[str]:
        """Line hashes of every version of a live partition (records, exemplars, unparsed queue)."""
        out: set[str] = set()
        for version in self._versions(key):
            vdir = self._vdir(key, version)
            manifest = self._read_manifest(key, version)
            if manifest is None:
                continue
            for artifacts in self.units(key, version):
                for record in artifacts.records:
                    out.update(line_hash(t) for t in record_lines(record))
                for record in artifacts.fragment_samples:
                    out.update(line_hash(t) for t in record_lines(record))
            if (vdir / "unparsed.jsonl.gz").exists():
                for row in _read_gz_jsonl(vdir / "unparsed.jsonl.gz"):
                    out.update(line_hash(t) for t in row["text"].split("\n") if t.strip())
        return out

    def source_id_hashes(self, key: str) -> set[str]:
        out: set[str] = set()
        for version in self._versions(key):
            for artifacts in self.units(key, version):
                out.update(source_id_hash(r.source_id) for r in artifacts.records)
        return out
