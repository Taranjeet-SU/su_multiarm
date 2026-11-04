"""The built-in extractor component (plan §3.12): gazetteer, regex, spaCy, GLiNER and LLM extraction
producing span-anchored ``ExtractionOutput`` over the v2 types.

Per chunk, every entity is a *mention* with its own id (canonical key + chunk + offsets) so that the
kernel invariant "a relation span contains both endpoint spans in the same chunk" holds exactly; the
write path resolves mentions to canonical ids (``entity_key``) before storage. Quotes are located
with the offset map (``exact``/``normalised``) in the chunk the model saw, or ``relocated`` to another
field of the same record; anything else is a categorised ``ExtractionFailure`` the policy decides on.
Raw model replies are returned for the ledger whether they validated or not (B8).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from ..core.component import Dependencies, Diagnostic, Environment, recipe_fingerprint
from ..core.types import Chunk, Document, Entity, ExtractionOutput, Provenance, RawReply, Relation, Span
from ..models import Extraction, StrictModel
from ..profiles.prompts import EXTRACTION_PROMPT
from .grounding import locate, locate_in_record
from .predicate_support import PredicateSupport

FailureCategory = Literal[
    "grounding", "endpoint_not_in_span", "endpoint_type_ambiguous", "schema", "provider", "budget", "empty_output"
]


class ExtractionFailure(Exception):
    """A unit (chunk) whose extraction cannot be accepted. Never repaired; the policy decides."""

    def __init__(self, category: str, unit: str, message: str, raw_replies: list[RawReply] | None = None):
        self.category, self.unit = category, unit
        self.raw_replies = list(raw_replies or [])  # model replies made before the failure (kept for replay)
        super().__init__(f"{category} [{unit}]: {message}")


def entity_key(kind: str, name: str) -> str:
    """Canonical entity identity: type + name, casefolded (today's graph identity)."""
    return hashlib.sha256(f"{kind.casefold()}\0{name.casefold()}".encode()).hexdigest()


def mention_id(canonical: str, span: Span) -> str:
    return hashlib.sha256(f"{canonical}\0{span.chunk_id}\0{span.start}\0{span.end}".encode()).hexdigest()


@dataclass
class ExtractionInput:
    chunk: Chunk
    document: Document
    chunks: list[Chunk]  # every chunk of the document (for relocation)
    budget: Any = None  # provider call budget (``providers.CallBudget``)


@dataclass
class ExtractionResult:
    output: ExtractionOutput
    raw_replies: list[RawReply] = field(default_factory=list)
    calls: int = 0
    routing: dict[str, Any] = field(default_factory=dict)  # per-unit routing (pattern residue composition)


LOG = logging.getLogger(__name__)


# Recipe keys earlier releases hashed that the config no longer carries, with the value they always
# had then. Kept in the extractor recipe so existing indexes keep their fingerprint.
A1_RECIPE_KEYS: dict[str, Any] = {"ontology": None}


class BuiltinExtractor:
    name = "builtin"
    kind = "extractor"
    config_model: type[StrictModel]

    def __init__(self, config: Any, deps: Dependencies):
        self.cfg = config
        self.model = deps.providers.get("extractor")
        self.prompt = config.prompt or EXTRACTION_PROMPT
        self._patterns = {kind: re.compile(pattern, re.I) for kind, pattern in config.patterns.items()}
        self._support = PredicateSupport(config.stemmer_language, config.predicate_triggers)
        self._spacy = None
        self._gliner = None
        self._pattern = None
        if config.pattern_library is not None:
            from .patterns.extractor import PatternExtractor

            # Lazy: the library files are read on first extraction (never by serving).
            self._pattern = PatternExtractor(config.pattern_library, self._support, self.recipe_fingerprint)

    # --- component contract ------------------------------------------------------------------

    def validate(self, env: Environment, probe: bool = False) -> list[Diagnostic]:
        diagnostics = []
        if "llm" in self.cfg.extractors and self.model is None:
            diagnostics.append(Diagnostic(severity="error", code="extractor_model_missing", message="llm extraction needs models.roles.extractor", component=self.name))
        if any(not name.strip() for names in self.cfg.gazetteer.values() for name in names):
            diagnostics.append(Diagnostic(severity="error", code="empty_gazetteer_entry", message="gazetteer entries cannot be empty", component=self.name))
        if any(e in self.cfg.extractors for e in ("gliner", "llm")) and not self.cfg.labels:
            diagnostics.append(Diagnostic(severity="error", code="labels_missing", message="gliner/llm extraction needs extraction.labels", component=self.name))
        if self._pattern is not None:
            diagnostics.extend(self._validate_pattern(probe))
        return diagnostics

    def _validate_pattern(self, probe: bool) -> list[Diagnostic]:
        """The engine must be installed (no fallback) and the directory must exist. The library files
        are read and linted only under ``probe`` (``doctor --probe``) and at the start of every build."""
        from .patterns.engine import PatternEngineUnavailable
        from .patterns.library import PatternLibraryError

        ref = self.cfg.pattern_library
        found = []
        try:
            self._pattern.engine()
        except PatternEngineUnavailable as exc:
            found.append(Diagnostic(severity="error", code="pattern_engine_unavailable", message=str(exc), component=self.name, path="extraction.pattern_library.engine"))
        if ref.engine == "regex":
            found.append(Diagnostic(severity="warning", code="pattern_engine_nondeterministic", message="the regex engine is a development fallback; timeouts make results non-deterministic", component=self.name, path="extraction.pattern_library.engine"))
        if not ref.path.is_dir():
            found.append(Diagnostic(severity="error", code="pattern_library_missing", message=f"pattern library directory {ref.path} does not exist", component=self.name, path="extraction.pattern_library.path"))
        if probe and not any(d.severity == "error" for d in found):
            try:
                library = self._pattern.load().library
                for issue in library.issues:
                    if issue.severity != "info":
                        found.append(Diagnostic(severity="warning", code=f"pattern_{issue.code}", message=issue.render(), component=self.name, path="extraction.pattern_library"))
            except PatternLibraryError as exc:
                found.append(Diagnostic(severity="error", code="pattern_library_lint", message=str(exc), component=self.name, path="extraction.pattern_library"))
        return found

    def recipe_fingerprint(self) -> str | None:
        data = self.cfg.model_dump(mode="json")
        # Omitted while unset (2.0.0a1/a2 golden fingerprints); when set only its static options
        # enter (engine kind, max_line_bytes, mode): library contents are provenance, not recipe.
        library = data.pop("pattern_library", None)
        if library is not None:
            data["pattern_library"] = self.cfg.pattern_library.recipe()
        # 2.0.0a1 hashed ``ontology`` (always ``None`` when unused). 2.0.0a2 refuses the key, so it
        # is pinned here: a removed key must not change the index fingerprint (a2 review B2).
        for key, value in A1_RECIPE_KEYS.items():
            data.setdefault(key, value)
        data["prompt_sha"] = self.prompt_sha()
        data["model"] = getattr(getattr(self.model, "config", None), "model", None)
        return recipe_fingerprint(self.name, data)

    def prompt_sha(self) -> str:
        return hashlib.sha256(self.prompt.encode()).hexdigest()

    def reuse_salt(self) -> str:
        """Extraction reuse salt (addendum §3.4.3): ``""`` unless the ``pattern`` extractor is
        selected, then the sha of the library provenance and applied decisions. Never part of the
        recipe or index fingerprint."""
        return self._pattern.reuse_salt() if self._pattern is not None else ""

    def prepare_build(self) -> dict[str, Any] | None:
        """Called once at the start of a build: (re)load the library files so the build uses the
        library as it is now. Returns the pattern provenance and snapshot, or ``None`` without the
        ``pattern`` extractor. A lint error raises ``PatternLibraryError`` before anything is written."""
        if self._pattern is None:
            return None
        compiled = self._pattern.load()
        return {"provenance": compiled.provenance(), "snapshot": compiled.snapshot(), "salt": compiled.reuse_salt()}

    def run(self, ctx: Any, inputs: ExtractionInput) -> ExtractionResult:
        return self.extract(inputs)

    # --- extraction -------------------------------------------------------------------------

    def _provenance(self, extractor: str, confidence: float | None = None) -> Provenance:
        model = getattr(getattr(self.model, "config", None), "model", None) if extractor == "llm" else None
        return Provenance(
            extractor=extractor,
            model=model,
            prompt_sha=self.prompt_sha() if extractor == "llm" else None,
            recipe_sha=self.recipe_fingerprint(),
            confidence=confidence,
        )

    def extract(self, inputs: ExtractionInput) -> ExtractionResult:
        cfg = self.cfg
        chunk, text = inputs.chunk, inputs.chunk.text
        mentions: dict[str, Entity] = {}
        relations: list[Relation] = []
        raw_replies: list[RawReply] = []
        calls = 0

        def add(entity: Entity) -> Entity:
            return mentions.setdefault(entity.id, entity)

        def exact(name: str, kind: str, start: int, end: int) -> Entity:
            span = Span(document_id=chunk.document_id, chunk_id=chunk.id, field=chunk.field, start=chunk.start + start, end=chunk.start + end, text=text[start:end], match_level="exact")
            canonical = entity_key(kind, name)
            return Entity(id=mention_id(canonical, span), name=name, type=kind, span=span, canonical_id=canonical)

        source_text = text
        routing: dict[str, Any] = {}
        residue = False
        for position, extractor in enumerate(cfg.extractors):
            if extractor == "pattern":
                found_patterns = self._pattern.extract(chunk, inputs.document)
                for entity in found_patterns.entities:
                    add(entity)
                relations.extend(found_patterns.relations)
                routing.update(found_patterns.routing)
                if position == 0 and len(cfg.extractors) > 1:
                    # Residue composition (N8): later extractors see only the lines the library did
                    # not parse (parsed lines blanked, offsets kept); nothing left -> they are skipped.
                    residue = True
                    routing["residue_only"] = True
                    routing["residue_to"] = [] if found_patterns.residue_empty else list(cfg.extractors[1:])
                    if found_patterns.residue_empty:
                        break
                    text = found_patterns.residue_text
                    chunk = chunk.model_copy(update={"text": text})
                    inputs = ExtractionInput(chunk=chunk, document=inputs.document, chunks=inputs.chunks, budget=inputs.budget)
            elif extractor == "gazetteer":
                seen: set[str] = set()
                for kind, names in cfg.gazetteer.items():
                    for name in names:
                        if not name.strip():
                            raise ExtractionFailure("schema", chunk.id, "gazetteer entries cannot be empty")
                        for match in re.finditer(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I):
                            canonical = entity_key(kind, match.group())
                            if canonical in seen:
                                continue
                            seen.add(canonical)
                            add(exact(match.group(), kind, match.start(), match.end()))
            elif extractor == "regex":
                seen = set()
                for kind, pattern in self._patterns.items():
                    for match in pattern.finditer(text):
                        canonical = entity_key(kind, match.group())
                        if match.group().strip() and canonical not in seen:
                            seen.add(canonical)
                            add(exact(match.group(), kind, match.start(), match.end()))
            elif extractor == "spacy":
                if self._spacy is None:
                    import spacy

                    self._spacy = spacy.load(cfg.spacy_model)
                parsed = self._spacy(text)
                seen = set()
                for e in parsed.ents:
                    if entity_key(e.label_, e.text) not in seen:
                        seen.add(entity_key(e.label_, e.text))
                        add(exact(e.text, e.label_, e.start_char, e.end_char))
                for t in parsed:
                    if t.pos_ in cfg.pos and entity_key(f"POS:{t.pos_}", t.text) not in seen:
                        seen.add(entity_key(f"POS:{t.pos_}", t.text))
                        add(exact(t.text, f"POS:{t.pos_}", t.idx, t.idx + len(t.text)))
            elif extractor == "gliner":
                if self._gliner is None:
                    from gliner import GLiNER

                    self._gliner = GLiNER.from_pretrained(cfg.gliner_model)
                tokenizer = self._gliner.data_processor.transformer_tokenizer
                if len(tokenizer.encode(text, add_special_tokens=True)) > self._gliner.config.max_len:
                    raise ExtractionFailure("provider", chunk.id, "chunk exceeds GLiNER context; reduce chunk size before ingestion")
                seen = set()
                for e in self._gliner.predict_entities(text, cfg.labels, threshold=cfg.threshold):
                    if entity_key(e["label"], text[e["start"] : e["end"]]) not in seen:
                        seen.add(entity_key(e["label"], text[e["start"] : e["end"]]))
                        add(exact(text[e["start"] : e["end"]], e["label"], e["start"], e["end"]))
            elif extractor == "llm":
                found, raw = self._call_model(inputs)
                raw_replies.append(raw)
                calls += 1
                try:
                    grounded = self._ground_entities(found, inputs)
                    for entity in grounded:
                        add(entity)
                    relations.extend(self._ground_relations(found, grounded, inputs, mentions))
                except ExtractionFailure as exc:
                    exc.raw_replies = raw_replies
                    raise
            else:
                raise ExtractionFailure("schema", chunk.id, f"unknown extractor {extractor!r}")
        if residue:
            # A later extractor's match must not reach into a blanked line: every span on this chunk
            # must still be the verbatim source text (fail closed, never repaired).
            for span in [e.span for e in mentions.values()] + [r.span for r in relations]:
                if span.chunk_id == chunk.id and span.field == chunk.field and source_text[span.start - chunk.start : span.end - chunk.start] != span.text:
                    raise ExtractionFailure("grounding", chunk.id, "a residue extractor matched across a parsed line", raw_replies)
            LOG.debug("extraction routing %s: %s", chunk.id, routing)
        try:
            output = ExtractionOutput(entities=list(mentions.values()), relations=relations)
        except ValidationError as exc:
            raise ExtractionFailure("endpoint_not_in_span", chunk.id, str(exc), raw_replies) from None
        return ExtractionResult(output=output, raw_replies=raw_replies, calls=calls, routing=routing)

    def _call_model(self, inputs: ExtractionInput) -> tuple[Extraction, RawReply]:
        chunk = inputs.chunk
        if self.model is None:
            raise ExtractionFailure("provider", chunk.id, "llm extraction needs models.roles.extractor")
        payload = {"passage": chunk.text, "entity_types": self.cfg.labels}
        schema_sha = hashlib.sha256(json.dumps(Extraction.model_json_schema(), sort_keys=True).encode()).hexdigest()[:16]
        input_sha = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        key = f"raw:{self.prompt_sha()[:16]}:{input_sha}:{schema_sha}:{self.model.config.model}"
        try:
            raw_text = self.model.call_text(self.prompt, payload, Extraction, inputs.budget, "extraction")
        except RuntimeError as exc:
            category = "budget" if "budget" in str(exc) else "provider"
            raise ExtractionFailure(category, chunk.id, str(exc)) from exc
        try:
            found = Extraction.model_validate_json(raw_text)
        except ValidationError as exc:
            raise InvalidReply(RawReply(key=key, raw_reply=raw_text, valid=False), chunk.id, str(exc)) from None
        return found, RawReply(key=key, raw_reply=raw_text, valid=True)

    def _record_fields(self, document: Document) -> dict[str, str]:
        return {"title": document.title, **document.fields}

    def _anchor(self, quote: str, inputs: ExtractionInput, ambiguous: str) -> tuple[Span, Chunk] | None:
        """Locate a quote in the chunk the model saw, else relocate to another field of the record."""
        steps = self.cfg.grounding.normalisation
        chunk = inputs.chunk
        own = locate(quote, chunk.text, steps, ambiguous)
        if own is not None:
            return (
                Span(document_id=chunk.document_id, chunk_id=chunk.id, field=chunk.field, start=chunk.start + own.start, end=chunk.start + own.end, text=own.text, match_level=own.match_level),
                chunk,
            )
        fields = {k: v for k, v in self._record_fields(inputs.document).items() if k != chunk.field}
        found = locate_in_record(quote, chunk.field, {chunk.field: "", **fields}, steps, ambiguous)
        if found is None:
            return None
        field_name, located = found
        host = next(
            (c for c in inputs.chunks if c.field == field_name and c.start <= located.start and located.end <= c.end),
            None,
        )
        if host is None:
            return None
        return (
            Span(document_id=chunk.document_id, chunk_id=host.id, field=field_name, start=located.start, end=located.end, text=located.text, match_level="relocated"),
            host,
        )

    def _ground_entities(self, found: Extraction, inputs: ExtractionInput) -> list[Entity]:
        steps = self.cfg.grounding.normalisation
        grounded = []
        for entity in found.entities:
            if not entity.name.strip() or not entity.type.strip() or not entity.quote.strip():
                raise ExtractionFailure("schema", inputs.chunk.id, "empty extracted entity or evidence")
            anchored = self._anchor(entity.quote, inputs, "first")  # a mention is fungible: first occurrence grounds it
            if anchored is None or locate(entity.name, anchored[0].text, steps, "first") is None:
                raise ExtractionFailure("grounding", inputs.chunk.id, f"entity {entity.name!r} is not grounded in its record")
            span, _ = anchored
            canonical = entity_key(entity.type, entity.name)
            grounded.append(Entity(id=mention_id(canonical, span), name=entity.name, type=entity.type, span=span, canonical_id=canonical))
        return grounded

    def _ground_relations(self, found: Extraction, entities: list[Entity], inputs: ExtractionInput, mentions: dict[str, Entity]) -> list[Relation]:
        cfg = self.cfg.grounding
        relations = []
        for relation in found.relations:
            if not relation.quote.strip() or not relation.predicate.strip():
                raise ExtractionFailure("schema", inputs.chunk.id, "relation without quote or predicate")
            anchored = self._anchor(relation.quote, inputs, cfg.ambiguous)
            if anchored is None:
                raise ExtractionFailure("grounding", inputs.chunk.id, f"relation {relation.predicate!r} is not grounded in its record")
            span, host = anchored
            endpoints: dict[str, Entity] = {}
            offsets: dict[str, tuple[int, int]] = {}
            for side in ("subject", "object"):
                name = getattr(relation, side)
                declared = getattr(relation, side + "_type")
                # A name may carry both a POS tag and a semantic type; bind within this response.
                matching = {e.type for e in entities if e.name == name and (declared is None or e.type == declared)}
                if len(matching) != 1:
                    raise ExtractionFailure("endpoint_type_ambiguous", inputs.chunk.id, f"endpoint {name!r} has missing or ambiguous entity type")
                inner = locate(name, span.text, cfg.normalisation, "first")
                if inner is None:
                    raise ExtractionFailure("endpoint_not_in_span", inputs.chunk.id, f"endpoint {name!r} is not inside the relation quote")
                kind = matching.pop()
                end_span = Span(document_id=span.document_id, chunk_id=span.chunk_id, field=span.field, start=span.start + inner.start, end=span.start + inner.end, text=inner.text, match_level=span.match_level if span.match_level == "relocated" else inner.match_level)
                canonical = entity_key(kind, name)
                endpoint = Entity(id=mention_id(canonical, end_span), name=name, type=kind, span=end_span, canonical_id=canonical)
                endpoints[side] = mentions.setdefault(endpoint.id, endpoint)
                offsets[side] = (inner.start, inner.end)
            support = self._support.check(relation.predicate, span.text, offsets["subject"], offsets["object"])
            relations.append(
                Relation(
                    subject_id=endpoints["subject"].id,
                    predicate=relation.predicate,
                    object_id=endpoints["object"].id,
                    span=span,
                    edge_kind="fact" if support == "trigger_in_span" else "co_occurrence",
                    support=support,
                    provenance=self._provenance("llm"),
                )
            )
        return relations


class InvalidReply(ExtractionFailure):
    """A model reply that did not match the schema; carries the raw reply for the ledger."""

    def __init__(self, raw: RawReply, unit: str, message: str):
        self.raw = raw
        super().__init__("schema", unit, message, [raw])


def canonicalise(output: ExtractionOutput) -> tuple[list[Entity], list[Relation]]:
    """Mentions -> storage form: entity id = canonical key, relation endpoints = canonical keys."""
    by_mention = {e.id: e for e in output.entities}
    entities = [
        Entity(id=e.canonical_id or e.id, name=e.name, type=e.type, span=e.span, canonical_id=None, aliases=e.aliases)
        for e in output.entities
    ]
    relations = [
        r.model_copy(update={"subject_id": by_mention[r.subject_id].canonical_id or r.subject_id, "object_id": by_mention[r.object_id].canonical_id or r.object_id})
        for r in output.relations
    ]
    return entities, relations
