"""Log mapping (addendum A1 §3.2.1): source documents to ``LogRecord``, index-name parsing, the
per-index count and records queries built from the index mapping.

Index names are parsed with the configured name grammar (``IndexNameConfig``: optional prefix,
optional deployment prefixes, segment vocabularies, date suffix); no user regular expression is
compiled here. An index that does not fit the grammar exactly once is ``unparsed`` with a typed
reason, ``unknown`` attributes and a diagnostic, never a guess.

``LogMapping`` strips ANSI escapes and control characters, applies the ``redactor`` to the message,
the stack and string ``extra`` values, then cuts the message at ``max_line_bytes`` (on a UTF-8
boundary, flagged ``truncated``). The redactor is the configured safe one (``privacy.engine: safe``,
``privacy/redactors.py``) or any injected callable. ``LogMapping.redaction`` is ``"none"`` without one,
the safe redactor's stamp (``safe:<sha16>``) with it, or ``"injected"``; a consumer that persists or
sends records (partitions, prompts) refuses a mapping whose redaction is ``"none"``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Literal

from pydantic import Field

from ..core.config_sources import CountQueryConfig, IndexNameConfig, LogFieldMap, SourceConfig
from ..models import StrictModel
from .contracts import CountBasis, JsonScalar, SourceDoc, SourceUnit, stable_sha
from .errors import SourceSchemaDrift

UNKNOWN = "unknown"
# CSI (colours, cursor moves) and OSC sequences, then any remaining C0 control except tab/newline.
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
DATE_TYPES = {"date", "date_nanos"}
TEXT_TYPES = {"text", "match_only_text"}
KEYWORD_TYPES = {"keyword", "constant_keyword", "wildcard"}


class LogRecord(StrictModel):
    """One (possibly reassembled) log line. ``file`` and ``truncated`` are additive to the
    addendum's sketch: multiline assembly keys on ``(container, file)`` and the cut at
    ``max_line_bytes`` must be recorded."""

    id: str
    ts: datetime
    partition_key: str
    attributes: dict[str, str] = Field(default_factory=dict)
    level: str | None = None
    level_basis: Literal["level_field", "format_pattern", "none"] = "none"
    message: str
    stack: str | None = None
    host: str | None = None
    container: str | None = None
    file: str | None = None
    offset: int | None = None
    extra: dict[str, JsonScalar] = Field(default_factory=dict)
    source_id: str
    index: str
    truncated: bool = False
    lines: int = 1


# --- index names -------------------------------------------------------------------------------


UnparsedReason = Literal["no_date", "invalid_date", "prefix", "segments", "service", "vocabulary", "ambiguous"]


@dataclass(frozen=True)
class ParsedIndexName:
    """The parse of one index name. ``status`` is ``unparsed`` with a typed ``reason`` (and every
    grammar attribute ``unknown``, except ``index_date`` when the date parsed) whenever the name
    does not fit the grammar exactly once."""

    attributes: dict[str, str]
    index_date: date | None
    problem: str | None = None
    reason: UnparsedReason | None = None

    @property
    def ok(self) -> bool:
        return self.problem is None

    @property
    def status(self) -> Literal["parsed", "unparsed"]:
        return "parsed" if self.ok else "unparsed"


class IndexNameParser:
    """The index-name grammar of ``IndexNameConfig``: optional literal ``prefix``, optional
    deployment prefix (longest first, ``{n}`` = digits), the prefix-field segments, the service
    segment and the date suffix. Segments with a vocabulary take the longest entry that fits;
    others are one separator-free token. No user regular expression is compiled: every pattern is
    built from escaped literals and ``[0-9]+``."""

    def __init__(self, config: IndexNameConfig):
        self.config = config
        self.sep = config.separator
        self.vocab = {key: sorted(values, key=lambda s: (-len(s), s)) for key, values in config.vocabularies.items()}
        self.services = self.vocab.get("service", [])
        pattern = ""
        groups = {"%Y": r"(?P<Y>[0-9]{4})", "%m": r"(?P<m>[0-9]{2})", "%d": r"(?P<d>[0-9]{2})"}
        for part in re.split(r"(%[Ymd])", config.date_format):
            pattern += groups.get(part, re.escape(part))
        self._date = re.compile(re.escape(self.sep) + pattern + r"$")
        ordered = sorted(config.deployment_prefixes, key=lambda s: (-len(s.replace("{n}", "0")), s))
        self._deployments = [
            re.compile("".join("[0-9]+" if piece == "{n}" else re.escape(piece) for piece in re.split(r"(\{n\})", item) if piece) + re.escape(self.sep))
            for item in ordered
        ]
        # the same prefixes without the trailing separator, for ``deployment_position: before_service``
        self._deployment_words = [re.compile(p.pattern[: -len(re.escape(self.sep))]) for p in self._deployments]

    def fields(self) -> list[str]:
        out = [*self.config.prefix_fields, "service", "index_date"]
        return ["deployment", *out] if self.config.deployment_prefixes or self.config.deployment_position == "after_env" else out

    def _unparsed(self, name: str, reason: UnparsedReason, why: str, day: date | None = None) -> ParsedIndexName:
        attrs = {key: UNKNOWN for key in self.fields()}
        if day is not None:
            attrs["index_date"] = day.isoformat()
        return ParsedIndexName(attrs, day, f"index {name!r}: {why}", reason)

    def _segments(self, body: str, service: str | None = None) -> tuple[dict[str, str] | None, UnparsedReason, str]:
        """``body`` (no prefix, deployment or date) into the prefix fields and the service. With
        ``service`` the service segment is that vocabulary entry; otherwise the longest one that ends
        the body (or the last token without a vocabulary). With ``deployment_position:
        before_service`` a declared deployment prefix may stand between the prefix fields and the
        service; it is returned as ``deployment``."""
        sep = self.sep
        if service is None and self.services:
            for candidate in self.services:  # longest first
                if body == candidate or body.endswith(sep + candidate):
                    service = candidate
                    break
            if service is None:
                return None, "service", "no service vocabulary entry ends the name"
        if service is not None:
            rest = body[: -len(service)].removesuffix(sep) if body != service else ""
        else:
            rest, _, service = body.rpartition(sep)
            if not service:
                return None, "segments", "empty service segment"
        values: dict[str, str] = {}
        for name in self.config.prefix_fields:
            if not rest:
                return None, "segments", f"expected prefix fields {self.config.prefix_fields}"
            choices = self.vocab.get(name)
            if choices is not None:
                value = next((v for v in choices if rest == v or rest.startswith(v + sep)), None)
                if value is None:
                    return None, "vocabulary", f"no {name} vocabulary entry fits"
            else:
                value = rest.split(sep, 1)[0]
                if not value:
                    return None, "segments", f"empty {name} segment"
            values[name] = value
            rest = rest[len(value) + len(sep) :] if len(rest) > len(value) else ""
        if rest and self.config.deployment_position == "before_service" and any(p.fullmatch(rest) for p in self._deployment_words):
            values["deployment"] = rest
            rest = ""
        if rest and self.config.deployment_position == "after_env":
            values["deployment"] = rest  # env-anchored: whatever sits between the env and the service
            rest = ""
        if rest:
            return None, "segments", f"expected prefix fields {self.config.prefix_fields}; {rest!r} is left over"
        values["service"] = service
        return values, "segments", ""

    def _parse_before_service(self, name: str, head: str, day: date) -> ParsedIndexName:
        """Every service reading of ``head`` (each vocabulary entry that ends it, longest first; the
        last token without a vocabulary). Exactly one must fit; a name that fits with and without a
        deployment prefix (``ops-admin-kafka`` declared as a service *and* ``ops-admin`` as a
        deployment) is ``ambiguous``."""
        sep = self.sep
        candidates = [c for c in self.services if head == c or head.endswith(sep + c)] if self.services else [None]
        if not candidates:
            return self._unparsed(name, "service", "no service vocabulary entry ends the name", day)
        parses: list[dict[str, str]] = []
        failure: tuple[UnparsedReason, str] | None = None
        for service in candidates:
            values, reason, why = self._segments(head, service)
            if values is None:
                failure = failure or (reason, why)
                continue
            parses.append({"deployment": values.pop("deployment", "none"), **values})
        if not parses:
            reason, why = failure  # type: ignore[misc]
            return self._unparsed(name, reason, why, day)
        if len(parses) > 1:
            return self._unparsed(name, "ambiguous", "fits the grammar with more than one service/deployment split", day)
        return ParsedIndexName({**parses[0], "index_date": day.isoformat()}, day)

    def parse(self, name: str) -> ParsedIndexName:
        match = self._date.search(name)
        if match is None:
            return self._unparsed(name, "no_date", f"does not end with a {self.config.date_format} date")
        try:
            day = date(int(match["Y"]), int(match["m"]), int(match["d"]))
        except ValueError:
            return self._unparsed(name, "invalid_date", "has an invalid date")
        head = name[: match.start()]
        if self.config.prefix is not None:
            lead = self.config.prefix + self.sep
            if not head.startswith(lead):
                return self._unparsed(name, "prefix", f"does not start with {lead!r}", day)
            head = head[len(lead) :]
        if self.config.deployment_position == "after_env" or (self.config.deployment_prefixes and self.config.deployment_position == "before_service"):
            return self._parse_before_service(name, head, day)
        # Every reading of the name: with the longest deployment prefix that matches, and without one.
        readings: list[tuple[str | None, str]] = []
        for pattern in self._deployments:
            hit = pattern.match(head)
            if hit is not None:
                readings.append((head[: hit.end() - len(self.sep)], head[hit.end() :]))
                break
        readings.append((None, head))
        parses: list[tuple[str | None, dict[str, str]]] = []
        failure: tuple[UnparsedReason, str] | None = None
        for deployment, body in readings:
            values, reason, why = self._segments(body)
            if values is None:
                failure = failure or (reason, why)
                continue
            if self.config.deployment_prefixes:
                values = {"deployment": deployment or "none", **values}
            parses.append((deployment, values))
        if not parses:
            reason, why = failure  # type: ignore[misc]
            return self._unparsed(name, reason, why, day)
        if len(parses) > 1:
            return self._unparsed(name, "ambiguous", "parses both with and without a deployment prefix", day)
        if len(readings) > 1 and parses[0][0] is None:
            # It starts with a declared deployment prefix but fits only without it: a guess either way.
            return self._unparsed(name, "ambiguous", f"starts with deployment prefix {readings[0][0]!r} but fits the grammar only without it", day)
        return ParsedIndexName({**parses[0][1], "index_date": day.isoformat()}, day)


# --- mappings and queries ----------------------------------------------------------------------


def flatten_mapping(payload: dict[str, Any], index: str) -> dict[str, str]:
    """``GET /{idx}/_mapping`` -> ``{"field.path": "type"}`` including multi-fields (``level.keyword``)."""
    body = payload.get(index) if isinstance(payload, dict) else None
    if not isinstance(body, dict) or not isinstance(body.get("mappings"), dict):
        raise SourceSchemaDrift(f"mapping response for {index!r} has no mappings", "mapping")
    out: dict[str, str] = {}

    def walk(props: dict[str, Any], prefix: str) -> None:
        for name, spec in props.items():
            if not isinstance(spec, dict):
                continue
            path = f"{prefix}{name}"
            if "properties" in spec:
                walk(spec["properties"], path + ".")
            if "type" in spec:
                out[path] = str(spec["type"])
            for sub, sub_spec in (spec.get("fields") or {}).items():
                if isinstance(sub_spec, dict) and "type" in sub_spec:
                    out[f"{path}.{sub}"] = str(sub_spec["type"])

    walk(body["mappings"].get("properties") or {}, "")
    return out


def exact_field(fields: dict[str, str], name: str) -> str | None:
    """The field (or its ``.keyword`` sub-field) that supports exact terms, else ``None``."""
    if fields.get(name) in KEYWORD_TYPES:
        return name
    if fields.get(f"{name}.keyword") in KEYWORD_TYPES:
        return f"{name}.keyword"
    return None


def window_filter(mapping: LogFieldMap, fields: dict[str, str], start: datetime, end: datetime) -> dict[str, Any]:
    if fields.get(mapping.timestamp) not in DATE_TYPES:
        raise SourceSchemaDrift(f"timestamp field {mapping.timestamp!r} is not a date field in this index", "mapping")
    return {"range": {mapping.timestamp: {"gte": _iso(start), "lt": _iso(end), "format": "strict_date_optional_time"}}}


def count_query(name: str, spec: CountQueryConfig, mapping: LogFieldMap, fields: dict[str, str]) -> tuple[dict[str, Any], CountBasis]:
    """Per-index count query from the mapping (§3.1.2): a level terms query only where the level
    field exists, otherwise a token query on the message field."""
    if spec.levels and mapping.level:
        target = exact_field(fields, mapping.level)
        if target is not None:
            return {"terms": {target: list(spec.levels)}}, "level_field"
        if fields.get(mapping.level) in TEXT_TYPES:
            return {"match": {mapping.level: {"query": " ".join(spec.levels), "operator": "or"}}}, "level_field"
    if spec.tokens:
        if fields.get(mapping.message) not in TEXT_TYPES:
            raise SourceSchemaDrift(f"count {name!r}: message field {mapping.message!r} is not a text field; a token query would not match words", "count")
        return {"match": {mapping.message: {"query": " ".join(spec.tokens), "operator": "or"}}}, "token_match"
    raise SourceSchemaDrift(f"count {name!r}: level field {mapping.level!r} is missing from this index and no tokens are declared", "count")


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# --- documents to records ----------------------------------------------------------------------


def lookup(source: dict[str, Any], path: str) -> Any:
    """Dotted path in a nested ``_source``; a literal dotted key wins (``{"log.offset": 3}``)."""
    if path in source:
        return source[path]
    node: Any = source
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
    if isinstance(value, str) and value:
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def clean_text(value: str) -> str:
    return CONTROL.sub("", ANSI.sub("", value))


def cut_utf8(value: str, limit: int) -> tuple[str, bool]:
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value, False
    return raw[:limit].decode("utf-8", errors="ignore"), True


@dataclass
class MappingStats:
    records: int = 0
    malformed: int = 0
    truncated: int = 0
    ansi_stripped: int = 0
    problems: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"records": self.records, "malformed": self.malformed, "truncated": self.truncated, "ansi_stripped": self.ansi_stripped, "problems": dict(self.problems)}


class LogMapping:
    """``SourceDoc`` -> ``LogRecord``. A document without a usable timestamp or message is counted
    as ``malformed`` with the reason (never dropped silently); ``map`` raises on the first such
    document only when ``strict=True``."""

    def __init__(self, config: SourceConfig, redactor: Callable[[str], str] | None = None):
        self.config = config
        self.fields = config.mapping
        self.redactor = redactor
        stamp = getattr(redactor, "stamp", None)
        self.redaction: str = "none" if redactor is None else (stamp if isinstance(stamp, str) else "injected")
        self.stats = MappingStats()

    def redaction_report(self) -> dict[str, Any]:
        """Content-free: mode/stamp and, for the safe redactor, replacements per redactor."""
        report = getattr(self.redactor, "report", None)
        return {"mode": self.redaction, **(report() if callable(report) else {})}

    def record(self, doc: SourceDoc, unit: SourceUnit, partition_key: str) -> LogRecord:
        source = doc.fields
        ts = parse_timestamp(lookup(source, self.fields.timestamp))
        if ts is None:
            raise SourceSchemaDrift(f"document has no usable {self.fields.timestamp!r}", "map")
        message = lookup(source, self.fields.message)
        if not isinstance(message, str):
            raise SourceSchemaDrift(f"document has no string {self.fields.message!r}", "map")
        cleaned = clean_text(message)
        if cleaned != message:
            self.stats.ansi_stripped += 1
        if self.redactor is not None:
            cleaned = self.redactor(cleaned)
        text, truncated = cut_utf8(cleaned, self.config.max_line_bytes)
        level = lookup(source, self.fields.level) if self.fields.level else None
        stack = lookup(source, self.fields.stack) if self.fields.stack else None
        if isinstance(stack, str):
            stack = clean_text(stack)
            if self.redactor is not None:
                stack = self.redactor(stack)
        else:
            stack = None
        offset = lookup(source, self.fields.offset) if self.fields.offset else None
        extra: dict[str, JsonScalar] = {}
        for path in self.fields.extra:
            value = lookup(source, path)
            if isinstance(value, str) and self.redactor is not None:
                extra[path] = self.redactor(value)
            elif value is None or isinstance(value, (str, int, float, bool)):
                extra[path] = value
            else:
                dumped = cut_utf8(json.dumps(value, sort_keys=True, default=str), 1024)[0]
                extra[path] = self.redactor(dumped) if self.redactor is not None else dumped
        return LogRecord(
            id=stable_sha([doc.index, doc.source_id])[:32],
            ts=ts,
            partition_key=partition_key,
            attributes=dict(unit.attributes),
            level=level if isinstance(level, str) and level else None,
            level_basis="level_field" if isinstance(level, str) and level else "none",
            message=text,
            stack=stack,
            host=_text(lookup(source, self.fields.host)) if self.fields.host else None,
            container=_text(lookup(source, self.fields.container)) if self.fields.container else None,
            file=_text(lookup(source, self.fields.file)) if self.fields.file else None,
            offset=offset if isinstance(offset, int) and not isinstance(offset, bool) else None,
            extra=extra,
            source_id=doc.source_id,
            index=doc.index,
            truncated=truncated,
        )

    def map(self, docs: list[SourceDoc], unit: SourceUnit, partition_key: str, strict: bool = False) -> list[LogRecord]:
        out = []
        for doc in docs:
            try:
                record = self.record(doc, unit, partition_key)
            except SourceSchemaDrift as error:
                if strict:
                    raise
                self.stats.malformed += 1
                reason = str(error).split(": ", 1)[-1]
                self.stats.problems[reason] = self.stats.problems.get(reason, 0) + 1
                continue
            self.stats.records += 1
            self.stats.truncated += int(record.truncated)
            out.append(record)
        return out


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
