"""Line coverage of a compiled library (addendum §3.4.2 "Line status").

Coverage counts only ``parsed + noise`` over the lines that are not ``fragment``s. Fragments (head-less
multiline continuations, assigned upstream) are reported separately and are excluded from the target.
``timeout`` lines (``regex`` engine only) are counted and reported; they are not covered.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .extractor import LineResult


@dataclass
class CoverageReport:
    lines: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    by_signature: dict[str, int] = field(default_factory=dict)
    truncated: int = 0

    @property
    def fragments(self) -> int:
        return self.by_status.get("fragment", 0)

    @property
    def coverage(self) -> float:
        denominator = self.lines - self.fragments
        covered = self.by_status.get("parsed", 0) + self.by_status.get("noise", 0)
        return covered / denominator if denominator else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "lines": self.lines,
            "by_status": dict(sorted(self.by_status.items())),
            "fragments": self.fragments,
            "coverage": round(self.coverage, 6),
            "truncated": self.truncated,
            "by_signature": dict(sorted(self.by_signature.items())),
        }


def coverage(results: Iterable[LineResult], fragments: int = 0) -> CoverageReport:
    statuses: Counter[str] = Counter()
    signatures: Counter[str] = Counter()
    report = CoverageReport()
    for result in results:
        statuses[result.status] += 1
        report.lines += 1
        report.truncated += int(result.truncated)
        if result.signature:
            signatures[result.signature] += 1
    if fragments:
        statuses["fragment"] += fragments
        report.lines += fragments
    report.by_status = dict(statuses)
    report.by_signature = dict(signatures)
    return report
