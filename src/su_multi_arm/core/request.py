"""Request conventions (new in v2): which caller-context keys a deployment accepts and which record
fields are echoed back as the canonical record. Neutral default: no context keys, nothing echoed;
profiles such as ``support_example`` declare theirs. Keeping this in configuration is what lets
custom context keys survive end to end without code changes.

``pipelines`` and ``modes`` are the allowlists for the per-request ``SearchRequest.pipeline`` and
``SearchRequest.mode`` overrides (plan §3.3): empty means a request cannot override anything; the
default pipeline and its configured decision kind are always allowed."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from ..models import StrictModel

KEY = r"^[a-z][a-z0-9_]{0,63}$"
DecisionMode = Literal["rules", "classifier", "llm", "cascade"]


class RequestConfig(StrictModel):
    context_keys: list[str] = Field(default_factory=list)  # allowed keys of SearchRequest.record_context
    context_max_bytes: int = Field(default=8192, ge=256, le=1_000_000)
    context_max_field_chars: int = Field(default=4000, ge=1, le=100_000)
    record_id_key: str = Field(default="record_id", pattern=KEY)  # context key naming a catalog record to exclude/echo
    canonical_fields: list[str] = Field(default_factory=list)  # document fields/metadata echoed as the canonical record
    pipelines: list[str] = Field(default_factory=list)  # named pipelines a request may select (allowlist)
    modes: list[DecisionMode] = Field(default_factory=list)  # decision kinds a request may select (allowlist)

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
