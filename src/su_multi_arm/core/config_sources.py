"""Configuration of read-only log sources (addendum A1 §3.1–3.2.2, slice L1): ``sources.<name>``.

A source is read-only, bounded and self-checking. Its configuration names **environment variables**
for every secret (``url_env``, ``username_env``, ``password_env``, ``api_key_env``): no credential,
host or token is ever a config value, a log line or a trace field. Nothing here names code, and no
user regular expression is accepted here: index names are parsed with a declared grammar of
literals (``IndexNameConfig``: prefix, deployment prefixes, segment vocabularies, date format), and
multiline assembly uses a closed set
of built-in continuation rules. User-authored patterns arrive with the pattern library (L2) and the
safe redaction engine (B11), which compile them through the RE2-subset validator.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ..models import StrictModel

ENV_NAME = r"^[A-Z_][A-Z0-9_]{0,127}$"
SOURCE_NAME = r"^[a-z][a-z0-9_]{0,63}$"
# A document field path in the source (``host.name``, ``@timestamp``, ``kubernetes.container.name``).
FIELD_PATH = r"^[A-Za-z@_][A-Za-z0-9@_.\-]{0,255}$"
# Index expression for discovery and counts: comma-separated items of lower-case names that may use
# ``*`` and the ``{date}`` placeholder; never a leading ``_`` (``_all``, ``_cluster`` …) or ``-``.
INDEX_ITEM = r"[a-z0-9*{][a-z0-9._*{}-]*"
INDEX_EXPRESSION = rf"^{INDEX_ITEM}(,{INDEX_ITEM})*$"
DURATION = re.compile(r"^([1-9][0-9]{0,5})(ms|s|m)$")
DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0}
# Hard ceiling on any scroll keepalive, whatever the config says (addendum §3.1.1 "bounded keepalive").
KEEPALIVE_CEILING_SECONDS = 600.0
# Optional request families a deployment can forbid on top of the transport allow-list
# (``sources.<n>.disallow``), with the allow-list rows each one covers.
DISALLOWABLE = {"pit": (8, 9)}
CONTINUATION_RULES = (
    "indented", "java_frame", "python_frame", "caused_by", "traceback_header", "ellipsis_more", "closing_bracket", "python_traceback",
)


def disallowed_rows(names: list[str]) -> frozenset[int]:
    """Allow-list rows covered by ``sources.<n>.disallow``."""
    return frozenset(row for name in names for row in DISALLOWABLE[name])


def duration_seconds(value: str) -> float:
    """``"90s"`` -> 90.0. Only ``ms``, ``s`` and ``m`` with a positive integer are accepted."""
    match = DURATION.fullmatch(value)
    if match is None:
        raise ValueError(f"duration {value!r} must be a positive integer with unit ms, s or m (e.g. '1m')")
    return int(match.group(1)) * DURATION_UNITS[match.group(2)]


class LogFieldMap(StrictModel):
    """Which source fields become ``LogRecord`` fields. Only these (plus ``extra``) are requested
    through ``_source`` includes; ``drop_fields`` are never requested."""

    timestamp: str = Field(default="@timestamp", pattern=FIELD_PATH)
    message: str = Field(default="message", pattern=FIELD_PATH)
    level: str | None = Field(default=None, pattern=FIELD_PATH)
    stack: str | None = Field(default=None, pattern=FIELD_PATH)
    host: str | None = Field(default=None, pattern=FIELD_PATH)
    container: str | None = Field(default=None, pattern=FIELD_PATH)
    file: str | None = Field(default=None, pattern=FIELD_PATH)
    offset: str | None = Field(default=None, pattern=FIELD_PATH)
    extra: list[str] = Field(default_factory=list)
    drop_fields: list[str] = Field(default_factory=list)

    @field_validator("extra", "drop_fields")
    @classmethod
    def paths(cls, value):
        for item in value:
            if not re.fullmatch(FIELD_PATH, item):
                raise ValueError(f"{item!r} is not a field path")
        if len(set(value)) != len(value):
            raise ValueError("field paths must be unique")
        return value

    def requested(self) -> list[str]:
        """The ``_source`` includes, in a stable order."""
        named = [self.timestamp, self.message, self.level, self.stack, self.host, self.container, self.file, self.offset]
        out: list[str] = []
        for item in [*named, *self.extra]:
            if item and item not in out:
                out.append(item)
        return out

    @model_validator(mode="after")
    def dropped_never_requested(self):
        clash = sorted(set(self.requested()) & set(self.drop_fields))
        if clash:
            raise ValueError(f"drop_fields {clash} are also mapped or listed in extra; a dropped field is never requested")
        return self


class IndexNameConfig(StrictModel):
    """Index-name grammar: ``[<prefix>-][<deployment prefix>-]<prefix fields>-<service>-<date>``
    (``deployment_position: leading``) or ``[<prefix>-]<prefix fields>-[<deployment prefix>-]<service>-<date>``
    (``deployment_position: before_service``).

    - ``date_format`` matches the trailing date (``%Y``, ``%m``, ``%d`` and literal separators).
    - ``prefix`` (optional) is a literal every name must start with, followed by ``separator``. It
      is stripped before the segments are read, so it never becomes part of the first prefix field
      (``prefix: logs`` reads ``logs-acme-prod-crawler-…`` as tenant ``acme``).
    - ``deployment_prefixes`` (optional) are deployment or infrastructure prefixes, tried longest
      first; ``{n}`` stands for a run of digits (``os-data-{n}`` matches ``os-data-1``). A matched
      prefix is recorded as the ``deployment`` attribute (``none`` when the name has none).
      ``deployment_position`` says where they stand: at the left of the prefix fields (``leading``,
      the default) or between the prefix fields and the service (``before_service``). ``after_env``
      is env-anchored and needs no list: the deployment is whatever lies between the last prefix
      field (matched by its vocabulary) and the service (matched by its vocabulary).
    - ``service_suffix: numeric`` reads a trailing digit run (at least ``service_suffix_min_digits``)
      after the service as the ``deployment_id`` attribute (``ml-service-1658254295-2026.03.01`` is
      service ``ml-service``, deployment id ``1658254295``).
    - ``prefix_fields`` (default ``tenant``, ``env``) and ``service`` are the segments. A segment
      with a vocabulary takes the **longest** vocabulary entry that fits (``query-gateway`` wins
      over ``gateway``); a segment without one is exactly one separator-free token, so service
      names such as ``slack_bot`` parse with the ``-`` separator.

    A name that does not fit the grammar, or that fits it in more than one way (with and without
    a deployment prefix), is ``unparsed`` with a typed reason and ``unknown`` attributes: never a
    guess.
    """

    separator: str = Field(default="-", min_length=1, max_length=3)
    date_format: str = "%Y.%m.%d"
    prefix: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    deployment_prefixes: list[str] = Field(default_factory=list)
    deployment_position: Literal["leading", "before_service", "after_env"] = "leading"
    # ``include``: an index whose name does not parse is a unit with ``unknown`` attributes (and a
    # diagnostic); ``exclude``: it is not a unit and is counted as ``excluded_scope.unparsed_name``.
    unparsed: Literal["include", "exclude"] = "include"
    prefix_fields: list[str] = Field(default_factory=lambda: ["tenant", "env"])
    # Keys: ``service`` or a prefix field. Values are literal lower-case names (``_`` allowed).
    vocabularies: dict[str, list[str]] = Field(default_factory=dict)
    # ``numeric``: a trailing run of at least ``service_suffix_min_digits`` digits after the service
    # (``ml-service-1658254295``) is a deployment id, not part of the service: service ``ml-service``,
    # attribute ``deployment_id`` ``1658254295`` (``none`` when the name has no such suffix).
    service_suffix: Literal["none", "numeric"] = "none"
    service_suffix_min_digits: int = Field(default=6, ge=1, le=32)

    @field_validator("date_format")
    @classmethod
    def date_tokens(cls, value):
        stripped = re.sub(r"%[Ymd]", "", value)
        if "%" in stripped or not all(token in value for token in ("%Y", "%m", "%d")):
            raise ValueError("date_format may use only %Y, %m and %d (each once) and literal separators")
        return value

    @field_validator("prefix_fields")
    @classmethod
    def prefix_names(cls, value):
        for name in value:
            if not re.fullmatch(r"^[a-z][a-z0-9_]{0,31}$", name) or name in {"service", "date", "deployment", "index_date"}:
                raise ValueError(f"prefix field {name!r} must be an identifier other than 'service', 'date', 'deployment' or 'index_date'")
        if len(set(value)) != len(value):
            raise ValueError("prefix fields must be unique")
        return value

    @field_validator("deployment_prefixes")
    @classmethod
    def deployment_literals(cls, value):
        for item in value:
            if not re.fullmatch(r"^(?:[a-z0-9._-]|\{n\})+$", item) or item.replace("{n}", "0")[0] in "._-":
                raise ValueError(f"deployment prefix {item!r} must be a literal lower-case name; '{{n}}' stands for digits")
        if len(set(value)) != len(value):
            raise ValueError("deployment prefixes must be unique")
        return value

    @field_validator("vocabularies")
    @classmethod
    def literal_names(cls, value):
        for key, names in value.items():
            if not names:
                raise ValueError(f"vocabulary {key!r} is empty")
            for name in names:
                if not re.fullmatch(r"^[a-z0-9][a-z0-9._-]{0,127}$", name):
                    raise ValueError(f"vocabulary entry {name!r} must be a literal lower-case name")
            if len(set(names)) != len(names):
                raise ValueError(f"vocabulary {key!r} has duplicates")
        return value

    @model_validator(mode="after")
    def coherent(self):
        unknown = sorted(set(self.vocabularies) - {"service", *self.prefix_fields})
        if unknown:
            raise ValueError(f"vocabularies {unknown} name no segment (service or a prefix field)")
        for item in self.deployment_prefixes:
            if item.endswith(self.separator):
                raise ValueError(f"deployment prefix {item!r} must not end with the separator")
        if self.prefix is not None and self.prefix.endswith(self.separator):
            raise ValueError("prefix must not end with the separator")
        if self.deployment_position == "after_env":
            anchor = self.prefix_fields[-1] if self.prefix_fields else None
            if "service" not in self.vocabularies or anchor is None or anchor not in self.vocabularies:
                raise ValueError(
                    "deployment_position: after_env needs a service vocabulary and a vocabulary for the last prefix field "
                    "(the env anchor): the deployment is whatever lies between them"
                )
        return self


class MultilineConfig(StrictModel):
    """Join continuation documents (stack frames, ``Caused by:``, column-0 Python tracebacks, lone
    closing brackets …) onto the preceding head of the same ``(container, file)`` within
    ``max_gap_ms``. ``continuation`` names built-in rules (``sources/multiline.py``); the approved
    ``continuation`` entries of the pattern library are applied in addition wherever a library is
    loaded (``logs`` pulls, ``patterns coverage|mine``)."""

    enabled: bool = True
    continuation: list[Literal[CONTINUATION_RULES]] = Field(  # type: ignore[valid-type]
        default_factory=lambda: ["java_frame", "python_frame", "traceback_header", "python_traceback", "caused_by", "ellipsis_more", "closing_bracket"]
    )
    max_gap_ms: int = Field(default=2000, ge=0, le=600_000)
    max_lines: int = Field(default=200, ge=1, le=10_000)
    max_bytes: int = Field(default=16_384, ge=256, le=1_048_576)
    fragment_sample_rate: float = Field(default=0.01, ge=0, le=1)


class CountQueryConfig(StrictModel):
    """A named count query. Built **per index from its mapping**: a ``level_field`` terms query on
    ``mapping.level`` where that field exists in the index, and otherwise a ``token_match`` query on
    the message field. Without ``tokens`` an index lacking the level field is a typed schema-drift
    error, never a zero."""

    levels: list[str] = Field(default_factory=list)
    tokens: list[str] = Field(default_factory=list)

    @field_validator("levels", "tokens")
    @classmethod
    def plain(cls, value):
        for item in value:
            if not item or len(item) > 64 or not re.fullmatch(r"^[A-Za-z0-9_.:-]+$", item):
                raise ValueError(f"{item!r} must be a plain word (letters, digits, _ . : -)")
        return value

    @model_validator(mode="after")
    def something(self):
        if not self.levels and not self.tokens:
            raise ValueError("a count query needs levels, tokens or both")
        return self


class PullConfig(StrictModel):
    """Records-tier pull: cursor, page size, keepalive bound and stratified caps (§3.1.3)."""

    cursor: Literal["scroll", "search_after"] = "scroll"
    tiebreaker: str | None = Field(default=None, pattern=FIELD_PATH)
    page_size: int = Field(default=500, ge=1, le=10_000)
    keepalive: str = "1m"
    keepalive_max: str = "2m"
    records_query: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    max_docs_per_unit: int = Field(default=20_000, ge=1)
    max_docs_per_partition: int = Field(default=500_000, ge=1)
    sample_method: Literal["hourly_quota", "random_score"] = "hourly_quota"
    seed: int = Field(default=0, ge=0, le=2**31 - 1)
    max_concurrency: int = Field(default=1, ge=1, le=8)
    slices: int = Field(default=1, ge=1, le=16)
    max_restarts: int = Field(default=1, ge=0, le=3)

    @model_validator(mode="after")
    def coherent(self):
        keep, ceiling = duration_seconds(self.keepalive), duration_seconds(self.keepalive_max)
        if ceiling > KEEPALIVE_CEILING_SECONDS:
            raise ValueError(f"keepalive_max must be at most {int(KEEPALIVE_CEILING_SECONDS)}s")
        if keep > ceiling:
            raise ValueError("keepalive must not exceed keepalive_max")
        if self.cursor == "search_after" and not self.tiebreaker:
            raise ValueError("cursor: search_after needs a declared unique tiebreaker field")
        if self.max_docs_per_unit > self.max_docs_per_partition:
            raise ValueError("max_docs_per_unit must not exceed max_docs_per_partition")
        return self

    def max_open_contexts(self) -> int:
        """At most ``max_concurrency × slices`` open contexts per process (§3.1.1)."""
        return self.max_concurrency * self.slices


class SourceLimits(StrictModel):
    requests_per_second: float = Field(default=2.0, gt=0, le=50)
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    max_response_bytes: int = Field(default=64 * 1024 * 1024, ge=1024, le=512 * 1024 * 1024)
    max_context_per_partition: int = Field(default=200, ge=0, le=100_000)


class SourceConfig(StrictModel):
    """One read-only log source. ``kind: elasticsearch_rest`` covers Elasticsearch ≥ 7.9 and
    OpenSearch ≥ 1.0 over plain REST (older 7.x discover through ``/{expr}/_alias``)."""

    kind: Literal["elasticsearch_rest"]
    url_env: str = Field(pattern=ENV_NAME)
    auth: Literal["none", "basic", "api_key"] = "none"
    username_env: str | None = Field(default=None, pattern=ENV_NAME)
    password_env: str | None = Field(default=None, pattern=ENV_NAME)
    api_key_env: str | None = Field(default=None, pattern=ENV_NAME)
    verify_tls: bool = True
    ca_bundle: Path | None = None
    indices: list[str] = Field(min_length=1)
    timezone: str = "UTC"
    require_read_only_role: bool = False
    # Request families this deployment forbids although the allow-list permits them (e.g. a cluster
    # policy that bans point-in-time contexts): refused by the transport before any I/O, and the
    # probe skips the corresponding check.
    disallow: list[Literal["pit"]] = Field(default_factory=list)
    mapping: LogFieldMap = Field(default_factory=LogFieldMap)
    index_name: IndexNameConfig = Field(default_factory=IndexNameConfig)
    multiline: MultilineConfig = Field(default_factory=MultilineConfig)
    counts: dict[str, CountQueryConfig] = Field(default_factory=dict)
    pull: PullConfig = Field(default_factory=PullConfig)
    limits: SourceLimits = Field(default_factory=SourceLimits)
    max_line_bytes: int = Field(default=8192, ge=256, le=1_048_576)

    @field_validator("indices")
    @classmethod
    def expressions(cls, value):
        for item in value:
            if not re.fullmatch(INDEX_EXPRESSION, item):
                raise ValueError(
                    f"index expression {item!r} must be lower-case names, '*', ',' and '{{date}}' "
                    "(no leading '_' or '-')"
                )
            if "{" in item.replace("{date}", ""):
                raise ValueError(f"index expression {item!r}: the only placeholder is {{date}}")
        return value

    @field_validator("counts")
    @classmethod
    def count_names(cls, value):
        for name in value:
            if not re.fullmatch(r"^[a-z][a-z0-9_]{0,63}$", name):
                raise ValueError(f"count query name {name!r} must be an identifier")
        return value

    @field_validator("timezone")
    @classmethod
    def known_zone(cls, value):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone {value!r}") from None
        return value

    @model_validator(mode="after")
    def coherent(self):
        if self.auth == "basic" and not (self.username_env and self.password_env):
            raise ValueError("auth: basic needs username_env and password_env")
        if self.auth == "api_key" and not self.api_key_env:
            raise ValueError("auth: api_key needs api_key_env")
        if self.auth == "none" and (self.username_env or self.password_env or self.api_key_env):
            raise ValueError("credential variables are set but auth is 'none'")
        if self.pull.records_query is not None and self.pull.records_query not in self.counts:
            raise ValueError(f"pull.records_query {self.pull.records_query!r} is not a declared count query")
        return self
