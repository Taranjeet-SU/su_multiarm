"""Configuration of the LLM pattern-learning loop (addendum A1 §3.6.1–3.6.3, slice L4b):
``learning.patterns`` plus the chat role ``models.roles.pattern_learner``.

The loop proposes patterns for unparsed log-line clusters with the ``pattern_learner`` role; the
deterministic gate (``extraction.pattern_library.gate``) decides, and
``extraction.pattern_library.promotion`` decides where a passing proposal is written. This block
holds the egress policy, the spend caps (per run and per calendar day, persisted in a spend ledger)
and the optional git commit of the system-owned library files. Nothing here names code.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator

from ..models import StrictModel

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def loopback_role(role: Any) -> bool:
    """``openai_compatible`` on a loopback host: the only role allowed without hosted egress."""
    if role is None or role.provider != "openai_compatible" or not role.base_url:
        return False
    host = (urlparse(role.base_url).hostname or "").casefold()
    return host in LOOPBACK_HOSTS or host.endswith(".localhost")


class PatternEgressConfig(StrictModel):
    """What may leave the process for the ``pattern_learner`` model.

    Only lines processed by the configured ``privacy`` redaction are ever sent (anything else is
    refused, not repaired). ``allow_hosted: false`` (the default) permits only an
    ``openai_compatible`` role on a loopback host. ``pseudonymise`` lists line attributes whose
    values are replaced by stable per-run aliases (in the attributes and in the line text) before
    the prompt is built."""

    allow_hosted: bool = False
    pseudonymise: list[str] = Field(default_factory=lambda: ["tenant", "host"])

    @field_validator("pseudonymise")
    @classmethod
    def names(cls, value):
        for name in value:
            if not re.fullmatch(r"^[a-z][a-z0-9_]{0,63}$", name):
                raise ValueError(f"pseudonymise entry {name!r} must be an attribute name")
        if len(set(value)) != len(value):
            raise ValueError("pseudonymise entries must be unique")
        return value


class PatternLearningConfig(StrictModel):
    """``learning.patterns``: one ``patterns learn`` run mines the unparsed lines, asks
    ``pattern_learner`` about at most ``max_clusters_per_run`` clusters (each with at most
    ``max_lines_per_cluster`` example lines and ``nearest_patterns`` approved patterns), gates every
    reply, writes the passing ones under the promotion policy and reports coverage before and after.

    **Spend.** Every call is reserved before it is sent, against the run caps
    (``max_calls_per_run``, ``max_usd_per_run``) and the persistent per-day caps
    (``max_calls_per_day``, ``max_usd_per_day``; the day is the calendar day in ``timezone``). USD
    caps need ``budgets.price_table``; a hosted role needs both USD caps. A refused reservation stops
    the run; nothing is retried or downgraded. The ledger is ``spend_ledger`` (default
    ``<library>/.spend_ledger.sqlite``)."""

    max_clusters_per_run: int = Field(default=10, ge=1, le=1000)
    max_lines_per_cluster: int = Field(default=8, ge=3, le=50)
    nearest_patterns: int = Field(default=3, ge=0, le=20)
    max_calls_per_run: int = Field(default=10, ge=1, le=10_000)
    max_calls_per_day: int = Field(default=50, ge=1, le=100_000)
    max_usd_per_run: float | None = Field(default=None, gt=0)
    max_usd_per_day: float | None = Field(default=None, gt=0)
    timezone: str = "UTC"
    spend_ledger: Path | None = None
    git_commit: bool = False  # commit learned/proposed/decisions when the library directory is a git work tree
    egress: PatternEgressConfig = Field(default_factory=PatternEgressConfig)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone {value!r}") from None
        return value

    @model_validator(mode="after")
    def caps(self):
        if self.max_calls_per_run > self.max_calls_per_day:
            raise ValueError("max_calls_per_run must not exceed max_calls_per_day")
        if self.max_usd_per_run is not None and self.max_usd_per_day is not None and self.max_usd_per_run > self.max_usd_per_day:
            raise ValueError("max_usd_per_run must not exceed max_usd_per_day")
        return self


class LearningConfig(StrictModel):
    patterns: PatternLearningConfig | None = None


def learning_problems(config: Any) -> list[tuple[str, str]]:
    """Cross-block checks of ``learning.patterns`` as ``(path, message)``; empty when unset."""
    settings = config.learning.patterns
    if settings is None:
        return []
    problems: list[tuple[str, str]] = []
    role = config.models.roles.pattern_learner
    if config.extraction.pattern_library is None:
        problems.append(("learning.patterns", "learning.patterns needs extraction.pattern_library (the library it learns into)"))
    if role is None:
        problems.append(("models.roles.pattern_learner", "learning.patterns needs models.roles.pattern_learner"))
    if (settings.max_usd_per_run is not None or settings.max_usd_per_day is not None) and config.budgets.price_table is None:
        problems.append(("learning.patterns", "max_usd_per_run/max_usd_per_day require budgets.price_table (operator-supplied; no bundled prices)"))
    if role is not None and not loopback_role(role):
        if not settings.egress.allow_hosted:
            problems.append(("learning.patterns.egress.allow_hosted", "models.roles.pattern_learner is not a loopback openai_compatible model; hosted egress needs allow_hosted: true"))
        if settings.max_usd_per_run is None or settings.max_usd_per_day is None:
            problems.append(("learning.patterns", "a hosted pattern_learner needs explicit max_usd_per_run and max_usd_per_day caps"))
    return problems
