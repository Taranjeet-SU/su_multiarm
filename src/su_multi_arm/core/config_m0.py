"""Configuration of the M0 log-side fixes (docs/plans/unified_merge.md §6.11, §6.12, §6.13, §9).

Kept out of ``core/config.py`` so the kernel file changes by a few imports and fields:

* ``RoleResilience`` (mixed into ``models.roles.<role>``): ``on_incomplete`` and
  ``retry_transient``, the only provider retries the package makes. Both are explicit, bounded,
  reserved on the request budget as separate metered calls and traced; nothing is retried by
  default and no model is ever substituted.
* ``PlanCheckMode`` (``budgets.plan_check``): the startup worst-case spend check
  (``runtime/plan_cost.py``); ``warn`` by default, ``refuse`` only when set.
* ``QuestionConfig`` (top-level ``question``): the **question clock** (the timezone in which a
  bare clock hour such as "at 2" is read; separate from ``serving.ui.display_timezone``, which is
  display only) and the reviewed **vocabulary** that grounds surface terms ("crawl") to indexed
  field values. Domain terms live in deployment config or a sha256-pinned file, never in code.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ..models import StrictModel

PlanCheckMode = Literal["off", "warn", "refuse"]
OnIncomplete = Literal["fail", "retry_shorter", "retry_larger"]
# Upper bound of ``ChatRole.max_tokens`` (kept equal to its ``le``).
MAX_OUTPUT_TOKENS = 64000


class RetryTransient(StrictModel):
    """One (or two) more attempts after a transient provider failure: HTTP 5xx/overloaded, 429,
    timeout, connection error, or a stream that ended before its final event without a
    ``max_tokens`` stop. Each attempt is a separate metered call; an attempt is made only when the
    request deadline leaves more than ``backoff_seconds`` plus the role timeout's floor of one
    second, and only when the budget can reserve it (otherwise the original error stands)."""

    attempts: int = Field(default=1, ge=1, le=2)
    backoff_seconds: float = Field(default=2.0, ge=0, le=30)


class RoleResilience(StrictModel):
    """Truncation and transient-failure policy of one chat role (§6.13).

    ``on_incomplete`` applies when the provider stops the reply at the output ceiling
    (``stop_reason: max_tokens`` / ``finish_reason: length``): ``fail`` (default) raises
    ``ProviderIncomplete``; ``retry_shorter`` sends the call once more with the same ceiling and an
    explicit instruction to answer more briefly; ``retry_larger`` sends it once more with
    ``incomplete_retry_max_tokens``. A second truncation is ``ProviderIncomplete``. ``retry_transient``
    unset: no retry (the 2.0.0a3 behaviour)."""

    on_incomplete: OnIncomplete = "fail"
    incomplete_retry_max_tokens: int | None = Field(default=None, ge=128, le=MAX_OUTPUT_TOKENS)
    retry_transient: RetryTransient | None = None

    @model_validator(mode="after")
    def coherent_retry(self):
        if self.on_incomplete == "retry_larger":
            if self.incomplete_retry_max_tokens is None:
                raise ValueError("on_incomplete: retry_larger needs incomplete_retry_max_tokens")
            if self.incomplete_retry_max_tokens <= getattr(self, "max_tokens", 0):
                raise ValueError("incomplete_retry_max_tokens must be larger than max_tokens")
        elif self.incomplete_retry_max_tokens is not None:
            raise ValueError("incomplete_retry_max_tokens is read only with on_incomplete: retry_larger")
        return self

    def retry_output_tokens(self) -> int | None:
        """The output ceiling of the truncation retry, or ``None`` when there is none."""
        if self.on_incomplete == "retry_larger":
            return self.incomplete_retry_max_tokens
        if self.on_incomplete == "retry_shorter":
            return getattr(self, "max_tokens", None)
        return None


# --- question grounding ---------------------------------------------------------------------------

TERM = r"^[a-z][a-z0-9 _-]{0,47}$"


class VocabularyTerm(StrictModel):
    """One reviewed surface term. ``match``: ``word`` (the term as a whole word) or ``prefix``
    (words starting with it: "crawl" matches "crawling", "crawler"). ``field: service`` grounds the
    term to the listed service values (only the values the generation has indexed are applied; none
    indexed refuses the question's scope rather than widening it). ``field: symptom`` marks a
    descriptive word ("slow") that is not a literal record term; it is stated, never filtered on.
    ``note`` is stated in the answer when the term is used."""

    term: str = Field(pattern=TERM)
    match: Literal["word", "prefix"] = "word"
    field: Literal["service", "symptom"] = "service"
    values: list[str] = Field(default_factory=list, max_length=100)
    note: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def coherent(self):
        if self.field == "service" and not self.values:
            raise ValueError(f"vocabulary term {self.term!r}: a service term needs values")
        if self.field == "symptom" and self.values:
            raise ValueError(f"vocabulary term {self.term!r}: a symptom term has no values")
        return self

    def pattern(self) -> re.Pattern[str]:
        words = r"[\s_-]+".join(re.escape(part) for part in re.split(r"[\s_-]+", self.term))
        tail = r"[a-z0-9]*" if self.match == "prefix" else ""
        return re.compile(rf"(?<![A-Za-z0-9]){words}{tail}(?![A-Za-z0-9])", re.I)


class QuestionVocabulary(StrictModel):
    """``question.vocabulary``: inline ``terms`` and/or a YAML ``file`` (a list of terms, relative
    to the config file) pinned by ``sha256``. A file whose digest differs refuses to start
    (§6.12, "vocabulary file pin mismatch"). Terms must be unique across both."""

    terms: list[VocabularyTerm] = Field(default_factory=list, max_length=500)
    file: Path | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def pinned(self):
        if (self.file is None) != (self.sha256 is None):
            raise ValueError("question.vocabulary.file and sha256 go together (the file is pinned)")
        return self

    def entries(self) -> list[VocabularyTerm]:
        """Inline terms plus the pinned file's (read and verified here; ``ConfigError`` on a missing
        file, a digest mismatch, an invalid entry or a duplicate term)."""
        import yaml
        from pydantic import TypeAdapter, ValidationError

        from .errors import ConfigError

        terms = list(self.terms)
        if self.file is not None:
            try:
                raw = self.file.read_bytes()
            except OSError as exc:
                raise ConfigError(f"cannot read the vocabulary file: {exc.strerror}", "question.vocabulary.file") from exc
            digest = hashlib.sha256(raw).hexdigest()
            if digest != self.sha256:
                raise ConfigError(f"vocabulary file sha256 {digest} does not match the pinned {self.sha256}", "question.vocabulary.sha256")
            try:
                terms += TypeAdapter(list[VocabularyTerm]).validate_python(yaml.safe_load(raw) or [])
            except (ValidationError, yaml.YAMLError) as exc:
                raise ConfigError(f"invalid vocabulary file: {exc}", "question.vocabulary.file") from exc
        seen: set[str] = set()
        for term in terms:
            if term.term in seen:
                raise ConfigError(f"duplicate vocabulary term {term.term!r}", "question.vocabulary")
            seen.add(term.term)
        return terms


class QuestionConfig(StrictModel):
    """How a question's surface terms are read (§6.11). ``timezone``: the question clock, the IANA
    zone in which an hour without a date ("at 2", "around 14:00") is read before it is converted
    to the log partition clock (``logs.timezone``); default UTC. It never changes partition dates
    and is independent of ``serving.ui.display_timezone``. An undated outage/presence question ("is
    there any outage") reads the last ``logs.collection.serving.default_view_days`` days."""

    timezone: str = Field(default="UTC", max_length=64)
    vocabulary: QuestionVocabulary = Field(default_factory=QuestionVocabulary)

    @field_validator("timezone")
    @classmethod
    def known_zone(cls, value):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown IANA timezone {value!r}") from None
        return value
