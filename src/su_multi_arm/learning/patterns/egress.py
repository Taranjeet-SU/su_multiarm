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

Under ``privacy.engine: safe`` the redaction is the safe redactor (``privacy/redactors.py``) and the
stamp is its ``safe:<sha16>``; records a ``LogMapping`` redacted with the same redactor are already
stamped by ``lines_from_records`` and are not redacted twice. Under ``legacy`` it is the
``redact_emails``/``patterns`` path with Python ``re`` (stamp ``privacy:<sha16>``). Nothing in a model reply is executed except its regex, which
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
        from ...privacy import safe_redactor

        self.safe = safe_redactor(privacy) if getattr(privacy, "engine", "legacy") == "safe" else None
        if self.safe is not None:
            self.patterns = []
            self.stamp = self.safe.stamp
            return
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
        if self.safe is not None:
            out, made = self.safe.redact(text)
            return out, sum(made.values())
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


def lines_from_records(
    records: Iterable[Any], mapping: Any, attributes: Iterable[str] = ("service", "tenant", "env", "host"), redactor: "LineRedactor | None" = None
) -> list[LogLine]:
    """``LogRecord``s as ``LogLine``s, refusing records mapped without a redactor (L1 open item).
    When ``redactor`` carries the same stamp as the mapping (the same safe redaction), the lines are
    returned as ``RedactedLine``s with that stamp: the egress policy admits them as they are."""
    if getattr(mapping, "redaction", "none") == "none":
        raise EgressRefused("the source mapping applied no redaction (LogMapping.redaction is 'none'); its records cannot reach the pattern learner")
    stamped = redactor is not None and getattr(mapping, "redaction", None) == redactor.stamp
    out: list[LogLine] = []
    for record in records:
        attrs = {k: v for k, v in record.attributes.items() if k in attributes and isinstance(v, str)}
        if record.host and "host" in attributes:
            attrs["host"] = record.host
        if record.level:
            attrs["level"] = record.level.casefold()
        if stamped:
            # attribute values come from index names and fields the mapping did not redact
            redacted = {}
            for key, value in attrs.items():
                redacted[key], _ = redactor.text(value)  # type: ignore[union-attr]
            out.append(RedactedLine(record.id, record.message, redacted, "message", redactor.stamp, 0))  # type: ignore[union-attr]
        else:
            out.append(LogLine(record.id, record.message, attrs))
    return out


@dataclass
class Pseudonymiser:
    """Stable per-run aliases for the values of ``keys`` (first-seen order: deterministic)."""

    keys: list[str]
    aliases: dict[tuple[str, str], str] = field(default_factory=dict)

    def alias(self, key: str, value: str) -> str:
        # Case-insensitive identity (real-run fix): "Tenant-Alpha" and "tenant-alpha" are one value.
        folded = value.casefold()
        if (key, folded) not in self.aliases:
            count = sum(1 for k, _ in self.aliases if k == key) + 1
            self.aliases[(key, folded)] = f"{key}-{count}"
        return self.aliases[(key, folded)]

    def attributes(self, attributes: dict[str, str]) -> dict[str, str]:
        return {k: (self.alias(k, v) if k in self.keys and v else v) for k, v in sorted(attributes.items())}

    def text(self, text: str) -> str:
        """Every known value replaced by its alias: case-insensitive and on token boundaries (a value
        is not replaced inside a longer identifier). Longest value first, so a value that contains
        another is replaced whole."""
        for (key, value), alias in sorted(self.aliases.items(), key=lambda item: (-len(item[0][1]), item[0])):
            if value and key in self.keys:
                pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(value) + r"(?![A-Za-z0-9_])", re.IGNORECASE)
                text = pattern.sub(alias, text)
        return text


# B11 built-ins used as an egress detector (real-run fix): every secret, personal and payload class,
# cookies, session and tracking ids included. ``uuid`` and ``request_id`` are identifiers that log
# templates legitimately carry, so they are not egress refusals.
EGRESS_SCAN_EXCLUDED = frozenset({"uuid", "request_id"})
_SCANNER: list[Any] = []


def _scanner() -> Any:
    if not _SCANNER:
        from ...extraction.patterns.engine import re2_available, select_engine
        from ...privacy.redactors import ORDER, SafeRedactor, builtin_redactors

        # Detection only (never the configured redaction): RE2 when installed, else the stdlib
        # engine over the same linear, linted patterns.
        engine = select_engine("re2" if re2_available() else "regex")
        _SCANNER.append(SafeRedactor(builtin_redactors([n for n in ORDER if n not in EGRESS_SCAN_EXCLUDED]), engine))
    return _SCANNER[0]


def egress_shapes(text: str) -> list[str]:
    """Names of every unredacted shape in ``text``: the library's PII shapes plus every B11
    built-in redactor (cookie headers, session and tracking ids, secrets, payload classes) that
    would still replace a span. Existing redaction tokens are not findings."""
    names = set(pii_shapes(text))
    names.update(name for name, _, _ in _scanner().findings(text))
    return sorted(names)


class EgressPolicy:
    def __init__(self, settings: PatternEgressConfig, redactor: LineRedactor):
        self.settings, self.redactor = settings, redactor

    def check_role(self, role: Any, name: str = "pattern_learner", setting: str = "learning.patterns.egress.allow_hosted") -> None:
        if role is None:
            raise EgressRefused(f"models.roles.{name} is not configured")
        if not self.settings.allow_hosted and not loopback_role(role):
            raise EgressRefused(f"the {name} role ({role.provider}) is not a loopback openai_compatible model and {setting} is false")

    def refusal(self, line: Any) -> str | None:
        """Why ``line`` may not leave the process, or ``None``."""
        if not isinstance(line, RedactedLine):
            return "not processed by the configured redaction"
        if line.redaction != self.redactor.stamp:
            return f"redacted under another privacy configuration ({line.redaction or 'none'})"
        shapes = sorted({s for text in (line.text, *line.attributes.values()) for s in egress_shapes(text)})
        if shapes:
            return f"unredacted {', '.join(shapes)} shape after redaction"
        return None

    def split(self, lines: Iterable[Any], max_refused_share: float) -> tuple[list[RedactedLine], dict[str, str]]:
        """Per-line egress (A2 rev 2 §4.2.4, N6): ``(admitted, refused id -> why)``. A refused line
        is never sent and never repaired; the whole batch is refused (``EgressRefused``) only when
        the refused share exceeds ``max_refused_share``."""
        admitted: list[RedactedLine] = []
        refused: dict[str, str] = {}
        total = 0
        for line in lines:
            total += 1
            why = self.refusal(line)
            if why is None:
                admitted.append(line)
            else:
                refused[str(getattr(line, "id", "?"))] = why
        if total and len(refused) / total > max_refused_share:
            shown = "; ".join(f"{k}: {v}" for k, v in list(refused.items())[:10])
            raise EgressRefused(f"{len(refused)} of {total} line(s) refused by the egress policy (share above {max_refused_share}): {shown}")
        return admitted, refused

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
            shapes = sorted({s for text in (line.text, *line.attributes.values()) for s in egress_shapes(text)})
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
