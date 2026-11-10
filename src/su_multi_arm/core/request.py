"""Request conventions (new in v2): which caller-context keys a deployment accepts and which record
fields are echoed back as the canonical record. Neutral default: no context keys, nothing echoed;
profiles such as ``support_example`` declare theirs. Keeping this in configuration is what lets
custom context keys survive end to end without code changes.

``pipelines`` and ``modes`` are the allowlists for the per-request ``SearchRequest.pipeline`` and
``SearchRequest.mode`` overrides (plan §3.3): empty means a request cannot override anything; the
default pipeline and its configured decision kind are always allowed.

``evidence_chars_max`` opts in to the per-request evidence budget ``SearchRequest.evidence_chars``
(characters of packed evidence handed to the generator); unset, a request carrying it is rejected."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from ..models import StrictModel

KEY = r"^[a-z][a-z0-9_]{0,63}$"
DecisionMode = Literal["rules", "classifier", "llm", "cascade"]


class RequestConfig(StrictModel):
    context_keys: list[str] = Field(default_factory=list)  # allowed keys of SearchRequest.record_context
    # Size of ``record_context`` as UTF-8 JSON bytes (``ensure_ascii=False``: a non-ASCII character
    # counts its UTF-8 length, not a 6-byte escape). The default admits nine 4000-character fields of
    # any BMP text (up to 3 bytes per character) plus keys; a larger context is refused with 422.
    context_max_bytes: int = Field(default=131072, ge=256, le=1_000_000)
    context_max_field_chars: int = Field(default=4000, ge=1, le=100_000)
    record_id_key: str = Field(default="record_id", pattern=KEY)  # context key naming a catalog record to exclude/echo
    canonical_fields: list[str] = Field(default_factory=list)  # document fields/metadata echoed as the canonical record
    pipelines: list[str] = Field(default_factory=list)  # named pipelines a request may select (allowlist)
    modes: list[DecisionMode] = Field(default_factory=list)  # decision kinds a request may select (allowlist)
    # Per-request evidence budget (``SearchRequest.evidence_chars``): the largest value a request may
    # ask for. ``None`` (default) refuses the override, so the configured budget always applies.
    evidence_chars_max: int | None = Field(default=None, ge=500, le=2_000_000)

    @field_validator("modes")
    @classmethod
    def unique_modes(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("modes must be unique")
        return value

    @field_validator("context_keys", "canonical_fields", "pipelines")
    @classmethod
    def identifiers(cls, value):
        import re

        for name in value:
            if not re.fullmatch(KEY, name):
                raise ValueError(f"{name!r} must be an identifier")
        if len(set(value)) != len(value):
            raise ValueError("names must be unique")
        return value
