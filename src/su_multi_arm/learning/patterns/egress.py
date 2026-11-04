"""Egress policy of the pattern learner (addendum §3.2.3, §3.6.1 "Egress"; ``learning.patterns.egress``).

Only text that the configured ``privacy`` redaction has processed may reach a model:

- ``LineRedactor`` applies the configured redaction (``privacy.redact_emails`` and
  ``privacy.patterns``) and stamps each line with the redaction's identity
  (``privacy:<sha16>``). A config without any redaction is refused: nothing would have processed
  the lines.
- ``EgressPolicy.admit`` accepts only ``RedactedLine`` objects stamped by the **current** redaction,
  and refuses any line that still shows an unredacted e-mail, IPv4, JWT, bearer-token or AWS-key
  shape (the library's example lint shapes, as defence in depth). A refusal is an ``EgressRefused``
  for the whole run, raised before any call: lines are never silently dropped or repaired.
- ``lines_from_records`` refuses ``LogRecord``s from a ``LogMapping`` whose redaction is ``none``.
- ``EgressPolicy.check_role`` refuses a hosted model unless ``allow_hosted: true``.
- ``Pseudonymiser`` replaces the values of the ``pseudonymise`` attributes with stable per-run
  aliases (``tenant-1``, ``host-2`` …), in the attributes and wherever the value occurs in the text.

The redaction is today's privacy engine (the ``legacy`` path, Python ``re``); the safe engine with
named built-in redactors is slice B11. Nothing in a model reply is executed except its regex, which
goes through the validator and the gate.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ...core.config_learning import PatternEgressConfig, loopback_role
from ...core.errors import SuError
from ...extraction.patterns.library import pii_shapes
from .bootstrap import LogLine

EMAIL_PATTERN = r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"  # the privacy engine's e-mail rule


class EgressRefused(SuError, PermissionError):
    """Text or a model the egress policy does not allow; raised before any provider call."""


@dataclass(frozen=True)
class RedactedLine(LogLine):
    """A ``LogLine`` processed by the configured redaction; ``redaction`` is its stamp."""

    redaction: str = ""
    redactions: int = 0  # replacements made in this line


class LineRedactor:
    """The configured privacy redaction applied to log lines (and their attribute values)."""

    def __init__(self, privacy: Any):
        patterns = dict(getattr(privacy, "patterns", {}) or {})
        if getattr(privacy, "redact_emails", False):
            patterns["EMAIL"] = EMAIL_PATTERN
        if not patterns:
            raise EgressRefused(
                "no redaction is configured (privacy.redact_emails or privacy.patterns); the pattern learner sends only redacted lines"
            )
        self.patterns = [(label, re.compile(pattern, re.I)) for label, pattern in sorted(patterns.items())]
        digest = hashlib.sha256(json.dumps(sorted(patterns.items())).encode()).hexdigest()[:16]
        self.stamp = f"privacy:{digest}"

    def text(self, text: str) -> tuple[str, int]:
        total = 0
        for label, pattern in self.patterns:
            text, count = pattern.subn(f"[REDACTED_{label}]", text)
            total += count
        return text, total

    def redact(self, line: LogLine) -> RedactedLine:
        text, count = self.text(line.text)
        attributes = {}
        for key, value in line.attributes.items():
            attributes[key], extra = self.text(value)
            count += extra
        return RedactedLine(line.id, text, attributes, line.field_name, self.stamp, count)

    def redact_all(self, lines: Iterable[LogLine]) -> list[RedactedLine]:
        return [self.redact(line) for line in lines]


def lines_from_records(records: Iterable[Any], mapping: Any, attributes: Iterable[str] = ("service", "tenant", "env", "host")) -> list[LogLine]:
    """``LogRecord``s as ``LogLine``s, refusing records mapped without a redactor (L1 open item)."""
    if getattr(mapping, "redaction", "none") == "none":
        raise EgressRefused("the source mapping applied no redaction (LogMapping.redaction is 'none'); its records cannot reach the pattern learner")
    out = []
    for record in records:
        attrs = {k: v for k, v in record.attributes.items() if k in attributes and isinstance(v, str)}
        if record.host and "host" in attributes:
            attrs["host"] = record.host
        if record.level:
            attrs["level"] = record.level.casefold()
        out.append(LogLine(record.id, record.message, attrs))
    return out


@dataclass
class Pseudonymiser:
    """Stable per-run aliases for the values of ``keys`` (first-seen order: deterministic)."""

    keys: list[str]
    aliases: dict[tuple[str, str], str] = field(default_factory=dict)

    def alias(self, key: str, value: str) -> str:
        if (key, value) not in self.aliases:
            count = sum(1 for k, _ in self.aliases if k == key) + 1
            self.aliases[(key, value)] = f"{key}-{count}"
        return self.aliases[(key, value)]

    def attributes(self, attributes: dict[str, str]) -> dict[str, str]:
        return {k: (self.alias(k, v) if k in self.keys and v else v) for k, v in sorted(attributes.items())}

    def text(self, text: str) -> str:
        # Longest value first, so a value that contains another is replaced whole.
        for (key, value), alias in sorted(self.aliases.items(), key=lambda item: (-len(item[0][1]), item[0])):
            if value and key in self.keys:
                text = text.replace(value, alias)
        return text


class EgressPolicy:
    def __init__(self, settings: PatternEgressConfig, redactor: LineRedactor):
        self.settings, self.redactor = settings, redactor

    def check_role(self, role: Any) -> None:
        if role is None:
            raise EgressRefused("models.roles.pattern_learner is not configured")
        if not self.settings.allow_hosted and not loopback_role(role):
            raise EgressRefused(
                f"the pattern_learner role ({role.provider}) is not a loopback openai_compatible model and learning.patterns.egress.allow_hosted is false"
            )

    def admit(self, lines: Iterable[Any]) -> list[RedactedLine]:
        """Every line, or ``EgressRefused`` naming the refused ones (nothing is dropped)."""
        admitted: list[RedactedLine] = []
        refused: list[str] = []
        for line in lines:
            if not isinstance(line, RedactedLine):
                refused.append(f"{getattr(line, 'id', '?')}: not processed by the configured redaction")
                continue
            if line.redaction != self.redactor.stamp:
                refused.append(f"{line.id}: redacted under another privacy configuration ({line.redaction or 'none'})")
                continue
            shapes = sorted(set(pii_shapes(line.text)) | {s for v in line.attributes.values() for s in pii_shapes(v)})
            if shapes:
                refused.append(f"{line.id}: unredacted {', '.join(shapes)} shape after redaction")
                continue
            admitted.append(line)
        if refused:
            shown = "; ".join(refused[:10])
            more = f" (+{len(refused) - 10} more)" if len(refused) > 10 else ""
            raise EgressRefused(f"{len(refused)} line(s) refused by the egress policy: {shown}{more}")
        return admitted

    def pseudonymiser(self) -> Pseudonymiser:
        return Pseudonymiser(list(self.settings.pseudonymise))
