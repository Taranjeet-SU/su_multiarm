"""The ``pattern`` extractor (addendum §3.4.2–3.4.4): staged line matching over a compiled library,
line-scoped facts, header-scoped attribute links and the residue handed to later extractors.

**Stages per line.** A chunk is split on ``\\n``; every line is capped at ``max_line_bytes`` (UTF-8,
cut on a character boundary, flagged ``truncated``) and matched on its own:

1. ``format``: the first approved format pattern (priority, then id) whose selectors apply. Its
   ``then.field_from_group`` group is the *body*; without a format match the body is the whole line.
2. ``message``: the first message pattern matching the body sets the signature.
3. ``annotate``: every matching annotate pattern adds entities.
4. ``noise``: when no message matched, the first matching noise pattern marks the line benign.

Status: ``parsed`` (message), ``noise``, ``format_only`` (format but no message), ``unparsed`` or
``timeout`` (``regex`` engine only; the line's result is non-deterministic). ``fragment`` is assigned
by multiline assembly upstream, never here.

Under RE2 each stage first asks the stage's ``re2.Set`` which patterns can match, and runs only those.

**Outputs.** Entities are the declared entity groups, span-anchored at their exact offsets. A relation
is *line-scoped*: both endpoints (groups of the message match or of its format match) and the trigger
group lie on one line; the relation span is the smallest span covering them. With a matched trigger
group the relation is a ``fact`` unless a negation marker lies inside that span; without one, the
existing predicate-support check decides. Either way an unsupported relation is a ``co_occurrence``.

**Attribute links** (``$service``, ``$tenant``, ``$env``, ``$signature``) come only from a document's
rendered ``header`` field: when the document's ``signature_id`` is the signature of an approved message
pattern with attribute links, and both endpoint values occur in the same header chunk, a
``native_link`` edge with ``support: unchecked`` is emitted, spanning both values. Header chunks are
never line-matched.

**Events and typed fields** are not emitted to the write path; ``LineResult.typed_fields`` exposes them
to partition-stage consumers.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from ...core.types import Chunk, Document, Entity, JsonScalar, Provenance, Relation, Span
from .engine import Hit, PatternEngine, PatternTimeout, select_engine
from .library import ATTRIBUTE_ENDPOINTS, Library, LoadedEntry, load_library

LineStatus = Literal["parsed", "format_only", "unparsed", "noise", "fragment", "timeout"]
HEADER_FIELD = "header"
STAGES = ("format", "message", "annotate", "noise")
_VALUE_EDGE = r"[A-Za-z0-9_.\-]"


def truncate_line(line: str, max_bytes: int) -> tuple[str, bool]:
    data = line.encode("utf-8")
    if len(data) <= max_bytes:
        return line, False
    return data[:max_bytes].decode("utf-8", errors="ignore"), True


@dataclass
class PatternHit:
    entry: LoadedEntry
    start: int
    end: int
    groups: dict[str, tuple[int, int] | None]  # line offsets


@dataclass
class LineResult:
    text: str
    status: LineStatus
    truncated: bool = False
    format: PatternHit | None = None
    message: PatternHit | None = None
    annotations: list[PatternHit] = field(default_factory=list)
    noise: PatternHit | None = None
    attributes: dict[str, str] = field(default_factory=dict)

    @property
    def signature(self) -> str | None:
        sig = self.message.entry.entry.signature if self.message else None
        return sig.id if sig else None

    def hits(self) -> list[PatternHit]:
        return [h for h in (self.format, self.message, *self.annotations, self.noise) if h is not None]

    def typed_fields(self) -> dict[str, JsonScalar]:
        """Declared typed groups of every hit (message wins over format on a name clash). A value
        that does not parse as its type raises ``ValueError`` (the caller decides; never coerced)."""
        values: dict[str, JsonScalar] = {}
        for hit in [h for h in (self.format, *self.annotations, self.message) if h is not None]:
            for name, decl in hit.entry.entry.fields.items():
                span = hit.groups.get(name)
                if span is None:
                    continue
                values[name] = _typed(self.text[span[0] : span[1]], decl.type, decl.values)
        return values


def _typed(raw: str, kind: str, allowed: list[str] | None) -> JsonScalar:
    if kind == "integer":
        return int(raw)
    if kind == "number":
        return float(raw)
    if kind == "boolean":
        if raw.casefold() not in {"true", "false"}:
            raise ValueError(f"{raw!r} is not a boolean")
        return raw.casefold() == "true"
    if kind == "datetime":
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
    if kind == "date":
        return date.fromisoformat(raw).isoformat()
    if kind == "enum":
        if raw not in (allowed or []):
            raise ValueError(f"{raw!r} is not one of {allowed}")
    return raw


class CompiledLibrary:
    """An effective approved library compiled for one engine."""

    def __init__(self, library: Library, engine: PatternEngine, max_line_bytes: int):
        self.library, self.engine, self.max_line_bytes = library, engine, max_line_bytes
        self.stages: dict[str, list[tuple[LoadedEntry, Any]]] = {}
        self.sets: dict[str, Any] = {}
        for kind in STAGES:
            rows = [(e, engine.compile(e.regex)) for e in library.approved(kind)]
            self.stages[kind] = rows
            self.sets[kind] = engine.build_set([e.regex for e, _ in rows])
        self.identity = library.identity()

    def provenance(self) -> dict[str, Any]:
        return {"library_identity": self.identity, **self.engine.identity(), "engine_deterministic": self.engine.deterministic}

    def reuse_salt(self) -> str:
        """sha256 of the provenance and the applied decision ids (addendum §3.4.3, round 2)."""
        data = {"provenance": self.provenance(), "decisions": self.library.applied_decision_ids()}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def snapshot(self) -> dict[str, Any]:
        return self.library.snapshot(self.provenance())

    # matching ------------------------------------------------------------------------------
    @staticmethod
    def _applies(entry: LoadedEntry, field_name: str, attributes: dict[str, str]) -> bool:
        for key in entry.entry.applies_to:
            allowed = entry.entry.selector(key)
            if allowed is None:
                continue
            value = field_name if key == "field" else attributes.get(key)
            if value is None or value.casefold() not in {a.casefold() for a in allowed}:
                return False
        return True

    def _stage(self, kind: str, text: str, offset: int, field_name: str, attributes: dict[str, str], first: bool) -> list[PatternHit]:
        rows = self.stages[kind]
        if not rows:
            return []
        candidates = self.engine.prefilter(self.sets[kind], text, len(rows))
        hits = []
        for index, (entry, compiled) in enumerate(rows):
            if index not in candidates or not self._applies(entry, field_name, attributes):
                continue
            hit: Hit | None = self.engine.search(compiled, text)
            if hit is None:
                continue
            groups = {k: None if v is None else (v[0] + offset, v[1] + offset) for k, v in hit.groups.items()}
            hits.append(PatternHit(entry, hit.start + offset, hit.end + offset, groups))
            if first:
                break
        return hits

    def match_line(self, line: str, *, field_name: str = "message", attributes: dict[str, str] | None = None) -> LineResult:
        text, truncated = truncate_line(line, self.max_line_bytes)
        attrs = dict(attributes or {})
        result = LineResult(text=text, status="unparsed", truncated=truncated, attributes=attrs)
        try:
            formats = self._stage("format", text, 0, field_name, attrs, True)
            body_start, body_end = 0, len(text)
            if formats:
                result.format = formats[0]
                for name, span in result.format.groups.items():
                    if span is not None:
                        attrs[name] = text[span[0] : span[1]]
                body = result.format.groups.get(result.format.entry.entry.then.field_from_group)  # type: ignore[union-attr]
                body_start, body_end = body if body is not None else (0, 0)
            body_text = text[body_start:body_end]
            messages = self._stage("message", body_text, body_start, field_name, attrs, True)
            result.message = messages[0] if messages else None
            result.annotations = self._stage("annotate", body_text, body_start, field_name, attrs, False)
            if result.message is None:
                noise = self._stage("noise", body_text, body_start, field_name, attrs, True)
                result.noise = noise[0] if noise else None
        except PatternTimeout:
            result.status = "timeout"
            return result
        if result.message is not None:
            result.status = "parsed"
        elif result.noise is not None:
            result.status = "noise"
        elif result.format is not None:
            result.status = "format_only"
        return result


@dataclass
class PatternChunkResult:
    entities: list[Entity]
    relations: list[Relation]
    residue_text: str  # the chunk text with parsed and noise lines blanked (offsets preserved)
    residue_empty: bool
    routing: dict[str, Any]
    lines: list[LineResult]


def document_attributes(document: Document) -> dict[str, str]:
    return {k: str(v) for k, v in document.metadata.items() if v is not None and not isinstance(v, bool)}


class PatternExtractor:
    """Loads the library lazily (serving never reads library files) and extracts per chunk."""

    def __init__(self, ref: Any, support: Any, recipe_sha: Any = None):
        self.ref, self.support = ref, support
        self._recipe_sha = recipe_sha
        self._compiled: CompiledLibrary | None = None

    def engine(self) -> PatternEngine:
        return select_engine(self.ref.engine, timeout_ms=self.ref.regex_timeout_ms)

    def load(self) -> CompiledLibrary:
        """(Re)load and compile the library files; a lint error raises ``PatternLibraryError``."""
        engine = self.engine()
        library = load_library(Path(self.ref.path), engine, min_literal_chars=self.ref.min_literal_chars)
        self._compiled = CompiledLibrary(library, engine, self.ref.max_line_bytes)
        return self._compiled

    def compiled(self) -> CompiledLibrary:
        return self._compiled if self._compiled is not None else self.load()

    def reuse_salt(self) -> str:
        return self.compiled().reuse_salt()

    def provenance(self) -> dict[str, Any]:
        return self.compiled().provenance()

    # extraction ----------------------------------------------------------------------------
    def extract(self, chunk: Chunk, document: Document) -> PatternChunkResult:
        compiled = self.compiled()
        attributes = document_attributes(document)
        if chunk.field == HEADER_FIELD:
            entities, relations = self._attribute_links(compiled, chunk, document)
            return PatternChunkResult(entities, relations, chunk.text, not chunk.text.strip(), {"pattern": {"header": True, "links": len(relations)}}, [])
        mentions: dict[str, Entity] = {}
        relations: list[Relation] = []
        residue = list(chunk.text)
        lines: list[LineResult] = []
        offset = 0
        for raw_line in chunk.text.split("\n"):
            if raw_line.strip():
                result = compiled.match_line(raw_line, field_name=chunk.field, attributes=attributes)
                lines.append(result)
                self._line_outputs(result, chunk, offset, mentions, relations)
                if result.status in {"parsed", "noise"}:
                    residue[offset : offset + len(raw_line)] = " " * len(raw_line)
            offset += len(raw_line) + 1
        residue_text = "".join(residue)
        statuses: dict[str, int] = {}
        for line in lines:
            statuses[line.status] = statuses.get(line.status, 0) + 1
        routing = {
            "pattern": {
                "lines": len(lines),
                **statuses,
                "truncated": sum(1 for line in lines if line.truncated),
            }
        }
        return PatternChunkResult(list(mentions.values()), relations, residue_text, not residue_text.strip(), routing, lines)

    def _span(self, chunk: Chunk, start: int, end: int) -> Span:
        return Span(
            document_id=chunk.document_id,
            chunk_id=chunk.id,
            field=chunk.field,
            start=chunk.start + start,
            end=chunk.start + end,
            text=chunk.text[start:end],
            match_level="exact",
        )

    def _mention(self, chunk: Chunk, kind: str, start: int, end: int) -> Entity | None:
        from ..base import entity_key, mention_id

        if end <= start or not chunk.text[start:end].strip():
            return None
        span = self._span(chunk, start, end)
        canonical = entity_key(kind, span.text)
        return Entity(id=mention_id(canonical, span), name=span.text, type=kind, span=span, canonical_id=canonical)

    def _provenance(self, entry: LoadedEntry) -> Provenance:
        recipe = self._recipe_sha() if callable(self._recipe_sha) else self._recipe_sha
        return Provenance(extractor=f"pattern:{entry.entry.id}", recipe_sha=recipe)

    def _line_outputs(self, line: LineResult, chunk: Chunk, offset: int, mentions: dict[str, Entity], relations: list[Relation]) -> None:
        bound: dict[str, dict[str, Entity]] = {}
        for hit in line.hits():
            own: dict[str, Entity] = {}
            for declared in hit.entry.entry.entities:
                span = hit.groups.get(declared.group)
                if span is None:
                    continue
                mention = self._mention(chunk, declared.type, offset + span[0], offset + span[1])
                if mention is not None:
                    own[declared.group] = mentions.setdefault(mention.id, mention)
            bound[hit.entry.entry.id] = own
        format_own = bound.get(line.format.entry.entry.id, {}) if line.format else {}
        for hit in (line.format, line.message):
            if hit is None:
                continue
            own = bound.get(hit.entry.entry.id, {})
            scope = {**format_own, **own} if hit is line.message else own
            for template in hit.entry.entry.relations:
                subject, object_ = scope.get(template.subject), scope.get(template.object)
                if subject is None or object_ is None:
                    continue  # an optional group did not take part: no evidence, no relation
                trigger = hit.groups.get(template.trigger_group) if template.trigger_group else None
                points = [(subject.span.start, subject.span.end), (object_.span.start, object_.span.end)]
                if trigger is not None:
                    points.append((chunk.start + offset + trigger[0], chunk.start + offset + trigger[1]))
                low, high = min(p[0] for p in points), max(p[1] for p in points)
                span = self._span(chunk, low - chunk.start, high - chunk.start)
                rel = lambda e: (e.span.start - low, e.span.end - low)  # noqa: E731
                if trigger is not None:
                    negated = self.support.negated(span.text, 0, len(span.text))
                    support = "unchecked" if negated else "trigger_in_span"
                else:
                    support = self.support.check(template.predicate, span.text, rel(subject), rel(object_))
                relations.append(
                    Relation(
                        subject_id=subject.id,
                        predicate=template.predicate,
                        object_id=object_.id,
                        span=span,
                        edge_kind="fact" if support == "trigger_in_span" else "co_occurrence",
                        support=support,
                        provenance=self._provenance(hit.entry),
                    )
                )

    def _attribute_links(self, compiled: CompiledLibrary, chunk: Chunk, document: Document) -> tuple[list[Entity], list[Relation]]:
        signature = document.metadata.get("signature_id")
        if not isinstance(signature, str):
            return [], []
        mentions: dict[str, Entity] = {}
        relations: list[Relation] = []
        for entry in compiled.library.approved("message"):
            sig = entry.entry.signature
            if sig is None or sig.id != signature:
                continue
            for link in entry.entry.attribute_links:
                ends = []
                for end in (link.subject, link.object):
                    value = document.metadata.get(ATTRIBUTE_ENDPOINTS[end])
                    found = None
                    if isinstance(value, str) and value:
                        found = re.search(rf"(?<!{_VALUE_EDGE}){re.escape(value)}(?!{_VALUE_EDGE})", chunk.text)
                    if found is None:
                        break
                    mention = self._mention(chunk, end[1:], found.start(), found.end())
                    if mention is None:
                        break
                    ends.append(mentions.setdefault(mention.id, mention))
                if len(ends) != 2:
                    continue  # both endpoint names must appear in this header chunk
                low = min(e.span.start for e in ends) - chunk.start
                high = max(e.span.end for e in ends) - chunk.start
                relations.append(
                    Relation(
                        subject_id=ends[0].id,
                        predicate=link.predicate,
                        object_id=ends[1].id,
                        span=self._span(chunk, low, high),
                        edge_kind="native_link",
                        support="unchecked",
                        provenance=self._provenance(entry),
                    )
                )
        return list(mentions.values()), relations
