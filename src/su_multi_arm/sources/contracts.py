"""The read-only source contract (addendum A1 §3.1).

A ``Source`` discovers units (index × optional time slice) for a partition window, counts them with
named count queries, pulls documents through a bounded cursor with stratified caps, and closes every
cursor it opened. It never writes. ``SourceRequestRefused`` is raised before any I/O for a request
that is not on the transport allow-list (``transport.py``).

Shared minimal types for later slices (partition store, pattern extractor) live here too:
``SamplePlan``/``Stratum`` (what the manifest records about a capped pull) and ``PartitionBudget``
(the partition-level cap that marks a pull ``capped`` instead of silently truncating).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Iterator, Literal, Protocol, runtime_checkable

from pydantic import Field

from ..core.component import Diagnostic
from ..core.config_sources import CountQueryConfig
from ..models import StrictModel

JsonScalar = str | int | float | bool | None
CountBasis = Literal["level_field", "token_match"]
CountQuery = CountQueryConfig


def stable_sha(payload: Any) -> str:
    """sha256 of a JSON-serialisable payload with sorted keys (unit ids, query shas)."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


class SourceCapabilities(StrictModel):
    server: Literal["elasticsearch", "opensearch", "unknown"]
    version: str | None = None  # None when GET / is forbidden (recorded, not fatal)
    pit: bool = False
    scroll: bool = True
    sliced_scroll: bool = False
    search_after: bool = True
    count: bool = True
    resolve_index: bool = False
    has_privileges: bool = False
    random_score_seq_no: bool = False
    probed: bool = False  # False: derived from the version only, no request beyond GET /
    notes: list[str] = Field(default_factory=list)


class SourceWindow(StrictModel):
    partition_key: str = Field(min_length=1)
    start: datetime
    end: datetime


class SourceUnit(StrictModel):
    id: str  # sha(index, slice, query_sha): stable across runs
    index: str
    slice: tuple[datetime, datetime] | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    query_sha: str


class UnitCount(StrictModel):
    unit_id: str
    query_sha: str
    total: int = Field(ge=0)  # documents of the unit in the window (exact for the window query)
    by_query: dict[str, int] = Field(default_factory=dict)
    basis: dict[str, CountBasis] = Field(default_factory=dict)
    at: datetime


class SourceCursor(StrictModel):
    unit_id: str
    mode: Literal["pit", "scroll", "search_after"]
    state: dict[str, Any] = Field(default_factory=dict)
    fetched: int = 0
    opened_ids: list[str] = Field(default_factory=list)  # ids this process opened (close scope)
    closed: bool = False


class SourceDoc(StrictModel):
    source_id: str
    index: str
    fields: dict[str, Any] = Field(default_factory=dict)


class Stratum(StrictModel):
    key: str  # "all" or "HH" (hour of the window, in the window's clock)
    start: datetime | None = None
    end: datetime | None = None
    count: int = Field(ge=0)  # source count of the records query in the stratum
    quota: int = Field(ge=0)
    fetched: int = 0


class SamplePlan(StrictModel):
    """What a pull of one unit fetches. ``method: none`` means the whole unit fits under the cap."""

    method: Literal["none", "hourly_quota", "random_score"]
    seed: int = 0
    cap: int = Field(ge=1)
    unit_total: int = Field(ge=0)
    strata: list[Stratum] = Field(default_factory=list)

    @property
    def approximate(self) -> bool:
        return self.method != "none"

    def scale(self, key: str) -> float:
        """Count-per-fetched factor for estimates in one stratum (1.0 when exact)."""
        for stratum in self.strata:
            if stratum.key == key:
                return 1.0 if not stratum.fetched else stratum.count / stratum.fetched
        raise KeyError(key)

    def label(self) -> str:
        return "exact" if not self.approximate else f"approximate ({self.method}, seed {self.seed})"


class SourceBatch(StrictModel):
    unit_id: str
    docs: list[SourceDoc] = Field(default_factory=list)
    cursor: SourceCursor
    stratum: str | None = None
    done: bool = False
    capped: bool = False
    restarts: int = 0


@dataclass
class PartitionBudget:
    """``max_docs_per_partition``: once spent, the pull stops at a page boundary and every later
    batch reports ``capped``. A cap never silently truncates (§3.1.3)."""

    limit: int
    fetched: int = 0
    capped: bool = False
    capped_units: list[str] = field(default_factory=list)

    def remaining(self) -> int:
        return max(0, self.limit - self.fetched)

    def spend(self, unit_id: str, count: int) -> None:
        self.fetched += count
        if self.fetched >= self.limit and not self.capped:
            self.capped = True
            self.capped_units.append(unit_id)


@runtime_checkable
class Source(Protocol):
    """``ComponentKind`` ``source``. Construction never connects; every method that talks to the
    cluster goes through the allow-list transport."""

    name: ClassVar[str]
    kind: ClassVar[str]

    def capabilities(self, probe: bool = False) -> SourceCapabilities: ...

    def self_check(self) -> list[Diagnostic]: ...

    def discover(self, window: SourceWindow) -> list[SourceUnit]: ...

    def count(self, unit: SourceUnit, queries: dict[str, CountQuery]) -> UnitCount: ...

    def pull(
        self, unit: SourceUnit, cursor: SourceCursor | None, page_size: int, sample: SamplePlan | None
    ) -> Iterator[SourceBatch]: ...

    def context(self, doc: SourceDoc, before: int, after: int) -> list[SourceDoc]: ...

    def close(self, cursor: SourceCursor) -> None: ...
