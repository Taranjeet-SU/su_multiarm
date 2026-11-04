"""Leak scan (B11 acceptance helper; ``su-multi-arm privacy scan``).

Scans files and directories for spans the given safe redactors would still replace: text files
line by line, ``.gz`` files decompressed, SQLite databases (``.sqlite``, ``.sqlite3``, ``.db``)
through every text column of every table, opened read-only. Files with NUL bytes that are not
SQLite are skipped and listed. The report is **content-free**: path, location (line, or
table/column/rowid) and redactor name per finding, never the matched text.
"""

from __future__ import annotations

import gzip
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from .redactors import SafeRedactor

SQLITE_SUFFIXES = {".sqlite", ".sqlite3", ".db"}
SQLITE_MAGIC = b"SQLite format 3\x00"


@dataclass
class ScanReport:
    files: int = 0
    skipped: list[str] = field(default_factory=list)
    by_redactor: dict[str, int] = field(default_factory=dict)
    findings: list[dict[str, Any]] = field(default_factory=list)
    max_findings: int = 1000
    truncated: bool = False

    @property
    def hits(self) -> int:
        return sum(self.by_redactor.values())

    def add(self, path: str, location: str, redactor: str) -> None:
        self.by_redactor[redactor] = self.by_redactor.get(redactor, 0) + 1
        if len(self.findings) < self.max_findings:
            self.findings.append({"path": path, "location": location, "redactor": redactor})
        else:
            self.truncated = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "hits": self.hits,
            "by_redactor": dict(sorted(self.by_redactor.items())),
            "findings": self.findings,
            "findings_truncated": self.truncated,
            "skipped": self.skipped,
            "clean": self.hits == 0,
        }


def scan_text(text: str, redactor: SafeRedactor) -> list[tuple[str, int, int]]:
    """``(redactor, start, end)`` of every residual span in ``text``."""
    return redactor.findings(text)


def _files(paths: Iterable[Path]) -> Iterator[tuple[Path, Path]]:
    for root in paths:
        root = Path(root)
        if root.is_dir():
            for path in sorted(p for p in root.rglob("*") if p.is_file()):
                yield root, path
        elif root.is_file():
            yield root.parent, root
        else:
            raise FileNotFoundError(f"{root} does not exist")


def _sqlite(path: Path, shown: str, redactor: SafeRedactor, report: ScanReport) -> None:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            columns = [r[1] for r in db.execute(f"PRAGMA table_info({quoted})")]
            try:
                cursor = db.execute(f"SELECT rowid, * FROM {quoted}")
            except sqlite3.OperationalError:
                try:  # WITHOUT ROWID tables: number the rows
                    cursor = ((n, *r) for n, r in enumerate(db.execute(f"SELECT * FROM {quoted}"), start=1))
                except sqlite3.OperationalError as exc:  # e.g. a virtual table whose module is absent
                    report.skipped.append(f"{shown}:{table}: {exc}")
                    continue
            for row in cursor:
                for column, value in zip(columns, row[1:]):
                    if isinstance(value, bytes):
                        try:
                            value = value.decode("utf-8")
                        except UnicodeDecodeError:
                            continue
                    if isinstance(value, str) and value:
                        for name, _, _ in redactor.findings(value):
                            report.add(shown, f"{table}.{column}#{row[0]}", name)


def scan_paths(paths: Iterable[Path | str], redactor: SafeRedactor, *, max_findings: int = 1000, exclude: Iterable[Path | str] = ()) -> ScanReport:
    """``exclude``: roots never read (the raw-copy store, unless ``privacy scan --raw`` audits it);
    files under them are listed as skipped."""
    report = ScanReport(max_findings=max_findings)
    excluded = [Path(e).resolve() for e in exclude]
    for root, path in _files(Path(p) for p in paths):
        shown = str(path.relative_to(root)) if path != root else path.name
        resolved = path.resolve()
        if any(resolved == e or e in resolved.parents for e in excluded):
            report.skipped.append(f"{shown}: raw store (audit it with --raw)")
            continue
        raw = path.read_bytes()
        report.files += 1
        if path.suffix in SQLITE_SUFFIXES or raw.startswith(SQLITE_MAGIC):
            _sqlite(path, shown, redactor, report)
            continue
        if path.suffix == ".gz":
            try:
                raw = gzip.decompress(raw)
            except OSError:
                report.skipped.append(f"{shown}: not gzip")
                continue
        if b"\x00" in raw:
            report.skipped.append(f"{shown}: binary")
            continue
        text = raw.decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            for name, _, _ in redactor.findings(line):
                report.add(shown, f"line {number}", name)
    return report
