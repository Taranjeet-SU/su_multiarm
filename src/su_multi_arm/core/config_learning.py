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
from typing import Any, Literal
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
    # A self-hosted model on the private network (route ``tier: lan``): its own opt-in, because the
    # lines leave this host even though they stay inside the operator's network.
    allow_lan: bool = False
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
    stop_file: Path | None = None  # clean stop between calls when it exists (default <library>/.learn.stop)
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


FIELD_NAME = r"^[a-z][a-z0-9_]{0,63}$"
ExtractionFieldType = Literal["string", "integer", "number", "boolean", "datetime"]


class TemplateFallbackConfig(StrictModel):
    """Cluster-first fallback (A2 §4.2.3): one ``pattern_learner`` call per residue cluster of at
    least ``min_support`` lines, at most ``max_clusters_per_day`` clusters per run (ranked by size x
    level weight), each prompt with at most ``max_lines_per_prompt`` lines. Calls reserve against
    ``learning.patterns`` (scope ``learning.patterns``). ``write_proposals`` writes passing entries
    through the existing promotion policy (``learned.yaml`` + ``approve`` under ``auto``, otherwise
    ``proposed.yaml``)."""

    enabled: bool = True
    max_clusters_per_day: int = Field(default=150, ge=1, le=100_000)
    min_support: int = Field(default=3, ge=2, le=1000)
    max_lines_per_prompt: int = Field(default=8, ge=3, le=50)
    write_proposals: bool = True


class LineFallbackConfig(StrictModel):
    """Capped per-line tail (A2 §4.2.4): singletons and lines whose provisional application failed
    alignment, at most ``max_per_day`` lines per run in batches of ``batch_size`` lines of one
    ``(service, level)``; lines longer than ``max_line_chars`` are never sent."""

    enabled: bool = False
    max_per_day: int = Field(default=200, ge=0, le=1_000_000)
    batch_size: int = Field(default=8, ge=1, le=64)
    max_line_chars: int = Field(default=2000, ge=64, le=100_000)


class FillFallbackConfig(StrictModel):
    """Missing-field fill (A2 §4.2.4) for lines an approved pattern matched without a valid value
    for a field ``logs.accounting.required_fields`` requires: a cluster ``fix`` proposal first, then
    at most ``max_per_day`` per-line quote calls."""

    enabled: bool = True
    max_per_day: int = Field(default=200, ge=0, le=1_000_000)


class GroundingFallbackConfig(StrictModel):
    """How a model quote is located in its line (``extraction/grounding.py``). ``relocated`` does
    not exist for a single line and is refused; fuzzy matching does not exist at all. ``reject``
    rejects an ambiguous quote unless the located text is identical at every occurrence."""

    levels: list[Literal["exact", "normalised"]] = Field(default_factory=lambda: ["exact", "normalised"], min_length=1)
    ambiguous: Literal["reject"] = "reject"

    @model_validator(mode="before")
    @classmethod
    def no_relocated(cls, data):
        if isinstance(data, dict) and "relocated" in (data.get("levels") or []):
            raise ValueError("grounding level 'relocated' is refused for line extraction (a value must be a span of its own line)")
        return data


class ExtractionEgressConfig(PatternEgressConfig):
    """Egress of the fallback (A2 §4.2.4, N6): a line that still shows a PII shape after redaction is
    ``unresolved: egress_refused`` on its own and never sent; the whole run is refused only when the
    refused share of the residue exceeds ``max_refused_share``."""

    max_refused_share: float = Field(default=0.01, ge=0, le=1)


class FallbackBudgetConfig(StrictModel):
    """Spend caps of the ``log_extractor`` role (scope ``learning.extraction``): per run, per
    calendar day and in total (persistent ledger; the day in ``timezone``). Provider lifetime caps
    are ``budgets.providers.<provider>.max_usd_total``."""

    max_usd_per_day: float | None = Field(default=0.50, gt=0)
    max_usd_total: float | None = Field(default=None, gt=0)
    timezone: str = "UTC"
    max_calls_per_day: int = Field(default=400, ge=1, le=1_000_000)
    max_usd_per_run: float | None = Field(default=None, gt=0)
    max_calls_per_run: int = Field(default=400, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def caps(self):
        if self.max_calls_per_run > self.max_calls_per_day:
            raise ValueError("max_calls_per_run must not exceed max_calls_per_day")
        if self.max_usd_per_run is not None and self.max_usd_per_day is not None and self.max_usd_per_run > self.max_usd_per_day:
            raise ValueError("max_usd_per_run must not exceed max_usd_per_day")
        return self


class ExtractionFallbackConfig(StrictModel):
    """``learning.extraction``: the LLM fallback for log lines the pattern library cannot parse or
    parses without a required field (addendum A2 §4.2). It runs after a day partition is sealed and
    writes a versioned **fallback overlay** next to it; the sealed partition is never changed.

    ``fields`` names the fields a model may label or quote, with their types (the closed set
    below); a label or quote naming any other field is dropped. ``max_residue_lines`` caps the
    residue queue per run; lines beyond it are ``unresolved: budget_deferred`` and reported.
    ``reply_cache`` defaults to ``<spend ledger dir>/.fallback_replies.sqlite``. Nothing here names
    code."""

    enabled: bool = False
    fields: dict[str, ExtractionFieldType] = Field(default_factory=dict)
    max_residue_lines: int = Field(default=200_000, ge=1, le=10_000_000)
    held_out_per_pattern: int = Field(default=200, ge=1, le=100_000)
    template: TemplateFallbackConfig = Field(default_factory=TemplateFallbackConfig)
    lines: LineFallbackConfig = Field(default_factory=LineFallbackConfig)
    fill: FillFallbackConfig = Field(default_factory=FillFallbackConfig)
    grounding: GroundingFallbackConfig = Field(default_factory=GroundingFallbackConfig)
    budget: FallbackBudgetConfig = Field(default_factory=FallbackBudgetConfig)
    egress: ExtractionEgressConfig = Field(default_factory=ExtractionEgressConfig)
    spend_ledger: Path | None = None
    reply_cache: Path | None = None
    # Clean stop between calls: when this file exists (default ``<spend ledger dir>/.fallback.stop``)
    # or the process receives SIGTERM, no further call is made; the rest is ``capacity_deferred``.
    stop_file: Path | None = None
    carry_forward_days: int = Field(default=3, ge=0, le=60)  # earlier sealed days with deferred lines retried after day D

    @field_validator("fields")
    @classmethod
    def field_names(cls, value):
        for name in value:
            if not re.fullmatch(FIELD_NAME, name):
                raise ValueError(f"field name {name!r} must match {FIELD_NAME}")
        return value


class BootstrapConfig(StrictModel):
    """``learning.bootstrap`` (A2 rev 4): the first ``days`` days of the fallback, when the residue
    backlog is largest. Off by default. While active, cluster calls go to self-hosted routes only
    (``local``/``lan``; paid routes are skipped) and are capped at ``max_clusters_per_day``.
    ``require_measured_throughput`` (default) refuses to start unless ``patterns measure-throughput``
    has recorded a self-hosted tier, and the cap is then also bounded by the measured calls/hour over
    ``hours_per_day``. ``max_usd_per_day`` is required (finite) when a paid route can be reached."""

    enabled: bool = False
    require_measured_throughput: bool = True
    days: int = Field(default=3, ge=1, le=30)
    max_clusters_per_day: int = Field(default=150, ge=1, le=100_000)
    hours_per_day: float = Field(default=8.0, gt=0, le=24)
    max_usd_per_day: float | None = Field(default=None, gt=0)


class LearningConfig(StrictModel):
    patterns: PatternLearningConfig | None = None
    extraction: ExtractionFallbackConfig | None = None
    bootstrap: BootstrapConfig = Field(default_factory=BootstrapConfig)


def learning_problems(config: Any) -> list[tuple[str, str]]:
    """Cross-block checks of ``learning.patterns`` and ``learning.extraction`` as ``(path,
    message)``; empty when unset."""
    problems = extraction_problems(config) + answers_problems(config)
    settings = config.learning.patterns
    if settings is None:
        return problems
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


def answers_problems(config: Any) -> list[tuple[str, str]]:
    """rev 4: a paid provider with a lifetime cap is reached at answer time only through a finite
    ``serving.answers`` daily cap (the same fail-closed rule as the learning scopes)."""
    serving = getattr(config, "serving", None)
    answers = getattr(serving, "answers", None)
    if answers is None:
        return []
    problems: list[tuple[str, str]] = []
    capped = set(getattr(config.budgets, "providers", {}) or {})
    for name in ("planner", "generator", "reviewer", "community"):
        role = getattr(config.models.roles, name, None)
        if role is None or loopback_role(role) or role.provider not in capped:
            continue
        if not answers.enabled or answers.max_usd_per_day is None:
            problems.append(("serving.answers", f"models.roles.{name} uses {role.provider}, which has a lifetime cap (budgets.providers); serving.answers needs enabled: true and a finite max_usd_per_day"))
    if answers.enabled and answers.max_usd_per_day is not None and config.budgets.price_table is None:
        problems.append(("serving.answers.max_usd_per_day", "a USD cap needs budgets.price_table"))
    return problems


def extraction_problems(config: Any) -> list[tuple[str, str]]:
    """Cross-block checks of ``learning.extraction`` (A2 §4.6) when it is enabled."""
    settings = config.learning.extraction
    if settings is None or not settings.enabled:
        return []
    problems: list[tuple[str, str]] = []
    if config.extraction.pattern_library is None:
        problems.append(("learning.extraction", "learning.extraction needs extraction.pattern_library (the library whose residue it handles)"))
    if getattr(config, "logs", None) is None:
        problems.append(("learning.extraction", "learning.extraction runs over sealed log partitions and needs logs:"))
    required = getattr(getattr(getattr(config, "logs", None), "accounting", None), "required_fields", {}) or {}
    for category, names in required.items():
        unknown = [n for n in names if n not in settings.fields]
        if unknown:
            problems.append((f"logs.accounting.required_fields.{category}", f"fields {unknown} are not declared in learning.extraction.fields"))
    if settings.template.enabled:
        if config.learning.patterns is None or not role_routes(config, "pattern_learner"):
            problems.append(("learning.extraction.template", "cluster calls run under learning.patterns and need models.roles.pattern_learner or models.routes.pattern_learner"))
    line_calls = settings.lines.enabled or (settings.fill.enabled and settings.fill.max_per_day > 0)
    egress = {"pattern_learner": config.learning.patterns.egress if config.learning.patterns else None, "log_extractor": settings.egress}
    for name, needed in (("pattern_learner", settings.template.enabled), ("log_extractor", line_calls)):
        routes = role_routes(config, name)
        if needed and not routes:
            problems.append((f"models.roles.{name}", f"learning.extraction needs models.roles.{name} or models.routes.{name}"))
        paid = [r for r in routes if r.tier == "paid"]
        for route in routes:
            where = f"models.routes.{name}.{route.name}"
            if route.declared == "local" and not loopback_role(route.role):
                problems.append((where, f"route {route.name!r} is declared local but is not a loopback openai_compatible model"))
            if route.tier == "lan":
                if route.role.provider != "openai_compatible":
                    problems.append((where, "a lan route is a self-hosted openai_compatible server"))
                if egress[name] is not None and not egress[name].allow_lan:
                    problems.append((where, f"route {route.name!r} is a lan model; egress needs allow_lan: true"))
            if route.tier != "paid":
                continue
            if egress[name] is not None and not egress[name].allow_hosted:
                problems.append((f"models.routes.{name}.{route.name}", f"route {route.name!r} is a paid (non-loopback) model; hosted egress needs allow_hosted: true"))
            if config.budgets.price_table is None:
                problems.append((f"models.routes.{name}.{route.name}", "a paid route needs budgets.price_table (USD caps apply to paid routes only)"))
            if route.role.provider not in config.budgets.providers:
                problems.append((f"models.routes.{name}.{route.name}", f"a paid route needs budgets.providers.{route.role.provider}.max_usd_total"))
        # R3-B2: a paid route never runs uncapped. It needs a finite provider total (checked above)
        # and finite daily USD and call caps for the role's scope.
        if paid and name == "log_extractor" and (settings.budget.max_usd_per_run is None or settings.budget.max_usd_per_day is None):
            problems.append(("learning.extraction.budget", "a paid log_extractor route needs finite max_usd_per_run and max_usd_per_day caps"))
        if paid and name == "pattern_learner":
            patterns = config.learning.patterns
            if patterns is None or patterns.max_usd_per_day is None or patterns.max_usd_per_run is None:
                problems.append(("learning.patterns", "a paid pattern_learner route needs finite learning.patterns.max_usd_per_run and max_usd_per_day caps"))
            # rev 4: every learning scope that can reach a paid route has a finite daily cap
            if config.learning.bootstrap.enabled and config.learning.bootstrap.max_usd_per_day is None:
                problems.append(("learning.bootstrap.max_usd_per_day", "learning.bootstrap is enabled and a paid pattern_learner route exists; a finite max_usd_per_day is required"))
    return problems


class _Route:
    """A provider route of a learning role (``models.routes.<role>`` or the single
    ``models.roles.<role>``)."""

    def __init__(self, name: str, role: Any, health_url: str | None = None, capacity_url: str | None = None, source: Any = None):
        self.name, self.role, self.health_url, self.capacity_url, self.source = name, role, health_url, capacity_url, source
        self.declared = getattr(source, "tier", None)

    @property
    def tier(self) -> str:
        """``local`` | ``lan`` | ``paid``: declared, else ``local`` for loopback and ``paid`` otherwise."""
        if self.declared is not None:
            return self.declared
        return "local" if loopback_role(self.role) else "paid"

    @property
    def paid(self) -> bool:
        return self.tier == "paid"


def role_routes(config: Any, name: str) -> list[_Route]:
    """The ordered routes of ``name`` (``pattern_learner`` or ``log_extractor``)."""
    routes = getattr(getattr(config.models, "routes", None), name, None) or []
    if routes:
        return [_Route(r.name, r.role, r.health_url, r.capacity_url, source=r) for r in routes]
    role = getattr(config.models.roles, name, None)
    return [_Route(name, role)] if role is not None else []
