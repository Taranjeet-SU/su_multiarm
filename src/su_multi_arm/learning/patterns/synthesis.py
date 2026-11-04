"""Deterministic regex synthesis from a mined template (addendum §3.6.1 row 2, L4a).

A template becomes ``^ *<token>( +<token>)* *$``:

- a literal token is escaped (metacharacters only);
- a ``<*>`` token becomes ``(?P<vN>\\S+)``;
- a mask placeholder becomes ``(?P<maskN>(?:<mask regex>))``, and literal text around it in the same
  token stays literal.

Every group gets a typed field inferred from the cluster's example values (``integer`` when all
parse as integers, ``number`` when all parse as numbers, else ``string``). The output is a
``proposed`` ``message`` entry with a deterministic id and signature (``syn.<sha12>``), no issue name,
the example lines as positive examples, and ``applies_to`` set to the cluster's service and level.

A candidate that fails the validator/ReDoS lint, has fewer than ``min_literal_chars`` literal
characters, or does not match every example it came from is **rejected with its reason**, never
repaired or loosened.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ...extraction.patterns.engine import PatternEngine, PatternTimeout, literal_chars, validate_regex
from ...extraction.patterns.library import PatternEntry, escape_literal
from .mining import _CLOSE, _OPEN, WILDCARD, Cluster


@dataclass
class Synthesis:
    cluster_id: str
    entry: PatternEntry | None
    reason: str | None = None  # why it was rejected


def template_regex(tokens: list[str], masks: dict[str, str]) -> tuple[str, dict[str, str]]:
    """The anchored regex of a template and ``group -> origin`` (``"*"`` or the mask name)."""
    parts: list[str] = []
    origins: dict[str, str] = {}
    counters: dict[str, int] = {}

    def group(origin: str, body: str) -> str:
        base = "v" if origin == "*" else "".join(c if c.isalnum() else "_" for c in origin).lower()
        counters[base] = counters.get(base, 0) + 1
        name = f"{base}{counters[base]}"
        origins[name] = origin
        return f"(?P<{name}>{body})"

    for token in tokens:
        if token == WILDCARD:
            parts.append(group("*", r"\S+"))
            continue
        out, rest = [], token
        while _OPEN in rest:
            before, _, tail = rest.partition(_OPEN)
            name, _, rest = tail.partition(_CLOSE)
            out.append(escape_literal(before))
            if name not in masks:
                raise ValueError(f"unknown mask placeholder {name!r}")
            out.append(group(name, f"(?:{masks[name]})"))
        out.append(escape_literal(rest))
        parts.append("".join(out))
    return "^ *" + " +".join(parts) + " *$", origins


def _infer_type(values: list[str]) -> str:
    def ok(cast: Any) -> bool:
        try:
            for value in values:
                cast(value)
        except ValueError:
            return False
        return bool(values)

    if ok(int):
        return "integer"
    if ok(float):
        return "number"
    return "string"


def synthesise(cluster: Cluster, masks: dict[str, str], engine: PatternEngine, *, min_literal_chars: int = 4, created: datetime | None = None) -> Synthesis:
    try:
        regex, origins = template_regex(cluster.tokens, masks)
    except ValueError as exc:
        return Synthesis(cluster.id, None, str(exc))
    errors = [i for i in validate_regex(regex) if i.severity == "error"]
    if errors:
        return Synthesis(cluster.id, None, f"lint: {errors[0].code}: {errors[0].message}")
    if literal_chars(regex) < min_literal_chars:
        return Synthesis(cluster.id, None, f"over-broad: fewer than {min_literal_chars} literal characters")
    compiled = engine.compile(regex)
    values: dict[str, list[str]] = {name: [] for name in origins}
    try:
        for _, text in cluster.examples:
            hit = engine.search(compiled, text)
            if hit is None:
                return Synthesis(cluster.id, None, f"does not match its own example {text[:80]!r}")
            for name, span in hit.groups.items():
                if span is not None:
                    values[name].append(text[span[0] : span[1]])
    except PatternTimeout as exc:
        return Synthesis(cluster.id, None, f"timeout: {exc}")
    applies: dict[str, list[str]] = {}
    if cluster.service:
        applies["service"] = [cluster.service]
    if cluster.level:
        applies["level"] = [cluster.level]
    digest = hashlib.sha256(f"{regex}\0{sorted(applies.items())}".encode()).hexdigest()[:12]
    entry = PatternEntry(
        id=f"syn.{digest}",
        kind="message",
        status="proposed",
        priority=0,
        applies_to=applies,
        regex=regex,
        fields={name: {"type": _infer_type(v)} for name, v in values.items()},
        signature={"id": f"syn.{digest}", "category": "unknown"},
        examples={"positive": [text for _, text in cluster.examples]},
        provenance={
            "author": "system:synthesis",
            "created": (created or datetime.now(timezone.utc)).isoformat(),
            "cluster_id": cluster.id,
            "cluster_size": cluster.size,
            "template": cluster.template(),
        },
    )
    return Synthesis(cluster.id, entry)
