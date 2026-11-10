"""The declared log collection policy, ``logs.collection`` (2026-11-08 standard).

One block states when to pull, which keyword families and rules are in force, how long each data
class lives and how far back the keyword/episode aggregates are served. Every key is consumed by
``ingestion/log_policy.py`` (schedule, keyword recipe, ``logs policy report|verify|import``) or
``ingestion/episode_archive.py`` (the long-term archive). Nothing here names code.

**Defaults reproduce the behaviour before this block existed:** no heavy-pull window, no keyword
files, the archive off, serving windows of at most 31 days (the ``incident_episodes`` bound).

**Retention per data class** reuses ``logs.retention`` (``raw_days``, ``keep_days``,
``keep_generations``, ``derived``); with ``archive.enabled`` the keyword/episode aggregates are
kept forever, so ``retention.derived`` must say ``forever`` (declared, never implied). Staging and
handoff copies (``staging``) are deleted once their day is archived and verified.

**Keyword recipe.** ``keywords.files`` lists the keyword-family files with their pinned sha256. A
file whose bytes no longer match its pin is refused when the policy is loaded; a new pin starts a
new recipe *epoch* only with ``keywords.effective_from`` after the last archived day, so archived
days of two recipes are never silently mixed.

**Liveness (O6).** ``liveness`` sets how ``log_liveness`` reads the success heartbeats (rules with
``category: success``): the cadence lookback, how many intervals a cadence needs and the factor
above the cadence at which a quiet job is reported halted.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from pydantic import Field, field_validator, model_validator

from ..models import StrictModel

HHMM = r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"
SHA256 = r"^[0-9a-f]{64}$"


def minutes(value: str) -> int:
    hours, mins = value.split(":")
    return int(hours) * 60 + int(mins)


class UtcWindow(StrictModel):
    """A daily window in UTC, ``start`` inclusive, ``end`` exclusive (``"24:00"`` allowed as end).
    The window lies inside one UTC day (``start < end``): a heavy pass never crosses midnight."""

    start: str = Field(pattern=HHMM)
    end: str = Field(pattern=r"^(?:(?:[01][0-9]|2[0-3]):[0-5][0-9]|24:00)$")

    @model_validator(mode="after")
    def ordered(self):
        if minutes(self.end) <= minutes(self.start):
            raise ValueError("heavy_window_utc must end after it starts on the same UTC day (a window across midnight would cross a day boundary)")
        return self

    def span_minutes(self) -> int:
        return minutes(self.end) - minutes(self.start)


class CollectionScheduleConfig(StrictModel):
    # continuous summary refresh: the collector re-queries today's per-index summaries this often
    summary_refresh_minutes: int = Field(default=15, ge=1, le=1440)
    # heavy pulls (backfills, evidence downloads, day close/import) only inside this UTC window;
    # ``None``: not restricted (the behaviour before the policy)
    heavy_window_utc: UtcWindow | None = None
    # a UTC day is complete (closable) this many minutes after its end: late-arriving documents
    day_close_delay_minutes: int = Field(default=0, ge=0, le=1440)
    # a day D is closed into the archive in the heavy window of D + close_after_days (late data
    # settles; until then it is served provisional). 1: the next day (the behaviour before I1)
    close_after_days: int = Field(default=1, ge=1, le=30)
    # D + close_after_days + grace_days without a complete set of full-day snapshots: D is archived
    # *partial* from the latest captured scope versions (flagged; missing/stale scopes listed)
    grace_days: int = Field(default=2, ge=0, le=30)

    @model_validator(mode="after")
    def closable_in_window(self):
        window = self.heavy_window_utc
        if window is not None and self.day_close_delay_minutes >= minutes(window.end):
            raise ValueError("day_close_delay_minutes must end before heavy_window_utc closes (otherwise no day can ever be closed)")
        return self


class KeywordFileConfig(StrictModel):
    """One keyword family file (YAML: ``family``, ``phrases``, ``rules``) and its pinned sha256."""

    path: Path
    sha256: str = Field(pattern=SHA256)


class KeywordPolicyConfig(StrictModel):
    # keyword families in force, in precedence order (earlier rules win, see episode_summaries)
    files: list[KeywordFileConfig] = Field(default_factory=list, max_length=32)
    # first UTC day the pinned recipe applies; needed to change the recipe once days are archived
    effective_from: date | None = None
    # quiet gap that separates two observed episodes of one rule
    gap_seconds: int = Field(default=300, ge=60, le=3600)

    @field_validator("files")
    @classmethod
    def unique(cls, value):
        paths = [str(f.path) for f in value]
        if len(set(paths)) != len(paths):
            raise ValueError("keywords.files lists a file twice")
        return value


class ArchiveConfig(StrictModel):
    """The long-term archive of keyword/episode day aggregates: append-only day shards served
    through the history tier (``ingestion/episode_archive.py``)."""

    enabled: bool = False
    # sqlite file of the archive; default ``<partitions>/archive/<source>.episodes.sqlite``. Never
    # the compose history file (that one is garbage-collected by reference).
    path: Path | None = None
    # text fields (exemplars, summaries) of archived rows are cut to this many characters; every
    # aggregate field and the evidence citations are always kept. ``None``: kept as built.
    max_text_chars: int | None = Field(default=None, ge=64, le=1_000_000)


class StagingConfig(StrictModel):
    """Handoff copies written by the collector: ``<path>/<YYYY-MM-DD>/*.json`` summary snapshots.
    A day's directory is deleted once the day is archived and the archived shard verified."""

    path: Path | None = None
    delete_after_import: bool = True


class ProvisionalConfig(StrictModel):
    """Unclosed days served provisional (``ingestion/provisional.py``): per-scope snapshot versions
    and one replaceable, flagged projection file per day version. Needs the archive."""

    enabled: bool = False
    # default ``<partitions>/provisional/<source>``; never under the staging path or the compose history
    path: Path | None = None


class CollectionServingConfig(StrictModel):
    # longest day range one episode query may span (31: the bound before the archive)
    max_window_days: int = Field(default=31, ge=1, le=3660)
    # the default range of ``issue_history`` and of the UI's log views (a default, not a limit)
    default_view_days: int = Field(default=3, ge=1, le=366)


class LivenessConfig(StrictModel):
    """``log_liveness``: last success, expected cadence and halt / recovery from success heartbeats."""

    # days of success heartbeats read for the cadence when the call names no lookback
    cadence_days: int = Field(default=7, ge=1, le=90)
    # intervals between success runs a cadence needs (fewer: the cadence is reported unknown)
    min_intervals: int = Field(default=3, ge=1, le=1000)
    # "halted": no success for more than halt_factor x the median interval between success runs
    halt_factor: float = Field(default=3.0, ge=1.0, le=100.0)


class CollectionPolicyConfig(StrictModel):
    schedule: CollectionScheduleConfig = Field(default_factory=CollectionScheduleConfig)
    keywords: KeywordPolicyConfig = Field(default_factory=KeywordPolicyConfig)
    archive: ArchiveConfig = Field(default_factory=ArchiveConfig)
    staging: StagingConfig = Field(default_factory=StagingConfig)
    provisional: ProvisionalConfig = Field(default_factory=ProvisionalConfig)
    serving: CollectionServingConfig = Field(default_factory=CollectionServingConfig)
    liveness: LivenessConfig = Field(default_factory=LivenessConfig)

    @model_validator(mode="after")
    def coherent(self):
        if self.provisional.enabled and not self.archive.enabled:
            raise ValueError("logs.collection.provisional serves the unclosed days next to the archived ones: it needs archive.enabled")
        if self.serving.default_view_days > self.serving.max_window_days:
            raise ValueError("logs.collection.serving.default_view_days must not exceed max_window_days")
        if self.liveness.cadence_days > self.serving.max_window_days:
            raise ValueError("logs.collection.liveness.cadence_days must not exceed serving.max_window_days")
        if self.archive.enabled and not self.keywords.files:
            raise ValueError("logs.collection.archive needs keywords.files (the archive pins the keyword recipe of every day)")
        if self.keywords.effective_from is not None and not self.keywords.files:
            raise ValueError("logs.collection.keywords.effective_from needs keywords.files")
        return self


def check_against_logs(policy: CollectionPolicyConfig, logs: object) -> None:
    """Cross-checks with the rest of ``logs`` (called by ``LogsConfig``)."""
    retention = logs.retention  # type: ignore[attr-defined]
    if policy.archive.enabled and retention.derived != "forever":
        raise ValueError("logs.collection.archive keeps keyword/episode aggregates forever: set logs.retention.derived: forever")
    if policy.serving.max_window_days > 31 and not (policy.archive.enabled or logs.history.tier):  # type: ignore[attr-defined]
        raise ValueError("logs.collection.serving.max_window_days above 31 needs the archive (collection.archive.enabled) or the history tier")
    if policy.archive.enabled and logs.history.tier:  # type: ignore[attr-defined]
        raise ValueError("logs.collection.archive (source-side episode summaries) and logs.history.tier (compose) are separate collection modes; use separate datasets")
    if policy.archive.path is not None:
        import os

        compose_history = Path(os.path.abspath(Path(logs.partitions.path) / "history"))  # type: ignore[attr-defined]
        archive = Path(os.path.abspath(policy.archive.path.expanduser()))
        if compose_history == archive or compose_history in archive.parents:
            raise ValueError("logs.collection.archive.path must lie outside <partitions>/history (the compose history is garbage-collected by reference)")
    staging = policy.staging.path
    if staging is not None:
        import os

        stage = Path(os.path.abspath(staging.expanduser()))
        archive = Path(os.path.abspath(archive_path(policy, logs).expanduser()))
        if stage == archive or stage in archive.parents:
            raise ValueError("logs.collection.archive.path must lie outside logs.collection.staging.path (staging copies are deleted)")
    if policy.provisional.enabled:
        import os

        root = Path(os.path.abspath(provisional_path(policy, logs).expanduser()))
        compose_history = Path(os.path.abspath(Path(logs.partitions.path) / "history"))  # type: ignore[attr-defined]
        archive = Path(os.path.abspath(archive_path(policy, logs).expanduser()))
        if root == compose_history or compose_history in root.parents or root == archive or root in archive.parents:
            raise ValueError("logs.collection.provisional.path must lie outside <partitions>/history and must not contain the archive file")
        if staging is not None:
            stage = Path(os.path.abspath(staging.expanduser()))
            if root == stage or stage in root.parents or root in stage.parents:
                raise ValueError("logs.collection.provisional.path and logs.collection.staging.path must not contain each other (staging copies are deleted)")


def provisional_path(policy: CollectionPolicyConfig, logs: object) -> Path:
    """The provisional store root: ``provisional.path`` or ``<partitions>/provisional/<source>``."""
    if policy.provisional.path is not None:
        return policy.provisional.path
    return Path(logs.partitions.path) / "provisional" / str(logs.source)  # type: ignore[attr-defined]


def archive_path(policy: CollectionPolicyConfig, logs: object) -> Path:
    """The archive file: ``archive.path`` or ``<partitions>/archive/<source>.episodes.sqlite``."""
    if policy.archive.path is not None:
        return policy.archive.path
    return Path(logs.partitions.path) / "archive" / f"{logs.source}.episodes.sqlite"  # type: ignore[attr-defined]
