"""``exact_id`` arm (plan §3.8): exact identifier lookups, pinned first by ``selection.pins``.

Identifiers are recognised in the step query by the ``pattern`` of ``id`` fields in
``metadata_schema`` and by the keys of the alias map; nothing else is an identifier. Each one
resolves, in order, through the alias map (``alias|id -> canonical id``), as a document id, and
through the configured ``alias_fields`` (an ``eq`` predicate on the record store under the
principal's ACL). Every resolved id is then confirmed inside the request's eligibility scope (ACL,
filters, exclusions) on the pinned generation with one bounded catalog read. An identifier that
does not resolve, or resolves only outside the scope, is reported as unknown — never fuzzily
matched, never revealed as "exists elsewhere". Hits carry ``pinned`` provenance; the engine marks
their selection candidates ``pinned``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, ClassVar

import yaml

from ..core.component import Diagnostic, Environment
from ..core.config import ExactIdArm as ExactIdArmConfig
from ..core.types import Page, Predicate, Scope, ScopeSpec
from ..models import Hit
from ..runtime.context import RunContext
from .base import ArmInput, ArmResult, BaseArm

MAX_ALIASES = 10000
EXCERPT_CHARS = 500
REPORT_KIND = "exact_id"


def load_alias_map(path: Path, casefold: bool) -> tuple[dict[str, str], list[str]]:
    """``{aliases: {alias: canonical_id}}`` from YAML or JSON -> (lookup index, aliases as written).
    Conflicting targets for one (normalised) alias and oversize maps are errors."""
    text = Path(path).read_text(encoding="utf-8")
    data = json.loads(text) if Path(path).suffix.lower() == ".json" else yaml.safe_load(text)
    if not isinstance(data, dict) or set(data) != {"aliases"} or not isinstance(data["aliases"], dict):
        raise ValueError("alias map must be a mapping with exactly one key 'aliases' (alias -> canonical id)")
    aliases = data["aliases"]
    if len(aliases) > MAX_ALIASES:
        raise ValueError(f"alias map has {len(aliases)} entries; at most {MAX_ALIASES} are supported")
    index: dict[str, str] = {}
    for alias, target in aliases.items():
        if not isinstance(alias, str) or not isinstance(target, str) or not alias.strip() or not target.strip():
            raise ValueError("alias map entries must be nonempty strings (alias -> canonical id)")
        key = alias.casefold() if casefold else alias
        if key in index and index[key] != target:
            raise ValueError(f"alias {alias!r} maps to more than one canonical id")
        index[key] = target
    return index, list(aliases)


class ExactIdArm(BaseArm):
    name: ClassVar[str] = "exact_id"
    config_model: ClassVar[type] = ExactIdArmConfig
    capability: ClassVar[str] = "catalog"

    def __init__(self, config: ExactIdArmConfig, deps):
        super().__init__(config, deps)
        self.records = deps.backends.get("records")
        self.load_error: str | None = None
        self.aliases: dict[str, str] = {}
        self.alias_keys: list[str] = []
        if config.alias_map is not None:
            try:
                self.aliases, self.alias_keys = load_alias_map(config.alias_map, config.casefold)
            except (OSError, ValueError, yaml.YAMLError) as exc:
                self.load_error = f"alias map {config.alias_map}: {exc}"
        schema = self.schema
        self.field_patterns: list[str] = [f.pattern for f in (schema.fields if schema is not None else []) if f.type == "id" and f.pattern]

    # --- component --------------------------------------------------------------------------

    def validate(self, env: Environment, probe: bool = False) -> list[Diagnostic]:
        diagnostics = []
        if self.catalog is None:
            diagnostics.append(Diagnostic(severity="error", code="catalog_missing", message="exact_id: catalog facet not provided", component=self.name))
        if self.load_error:
            diagnostics.append(Diagnostic(severity="error", code="alias_map_invalid", message=self.load_error, component=self.name))
        if self.cfg.alias_fields and self.records is None:
            diagnostics.append(Diagnostic(severity="error", code="records_missing", message="exact_id: alias_fields need the records facet", component=self.name))
        if not self.id_patterns():
            diagnostics.append(Diagnostic(severity="error", code="no_identifier_source", message="exact_id: no id field pattern and no alias map", component=self.name))
        return diagnostics

    def id_patterns(self) -> list[str]:
        """Regexes for ``Capabilities.id_patterns``: id-field patterns plus one alias alternation."""
        from ..pipeline.validate import alias_pattern

        patterns = list(dict.fromkeys(self.field_patterns))
        alias = alias_pattern(self.alias_keys, self.cfg.casefold)
        if alias:
            patterns.append(alias)
        return patterns

    # --- run ------------------------------------------------------------------------------------

    def identifiers(self, query: str) -> list[str]:
        """Identifiers in ``query`` in order of appearance (exact pattern/alias matches only)."""
        found: list[tuple[int, str]] = []
        for raw in self.id_patterns():
            found.extend((m.start(), m.group(0)) for m in re.finditer(raw, query) if m.group(0))
        return list(dict.fromkeys(token for _, token in sorted(found)))

    def _resolve(self, ctx: RunContext, inputs: ArmInput, token: str) -> list[tuple[str, str]]:
        """``[(canonical_id, how)]`` candidates for one token (existence is checked afterwards)."""
        key = token.casefold() if self.cfg.casefold else token
        candidates: list[tuple[str, str]] = []
        if key in self.aliases:
            candidates.append((self.aliases[key], "alias_map"))
        candidates.append((token, "document_id"))
        for field_name in self.cfg.alias_fields:
            spec = ScopeSpec(predicates=[Predicate(field=field_name, op="eq", value=token)])
            result = self.records.execute(ctx.generation, spec, list(inputs.acl))
            candidates.extend((record_id, f"field:{field_name}") for record_id in result.matched_ids[: self.cfg.max_ids])
        return list(dict.fromkeys(candidates))

    def _run(self, ctx: RunContext, inputs: ArmInput) -> ArmResult:
        tokens = self.identifiers(inputs.query)
        over = tokens[self.cfg.max_ids :]
        tokens = tokens[: self.cfg.max_ids]
        resolved: dict[str, list[tuple[str, str]]] = {t: self._resolve(ctx, inputs, t) for t in tokens}
        wanted = list(dict.fromkeys(cid for options in resolved.values() for cid, _ in options))
        documents: dict[str, Any] = {}
        if wanted:
            scope = Scope(predicates=list(inputs.scope.predicates), document_ids=wanted, exclude_ids=list(inputs.scope.exclude_ids))
            if inputs.scope.document_ids is not None:
                allowed = set(inputs.scope.document_ids)
                scope.document_ids = [i for i in wanted if i in allowed]
            if scope.document_ids:
                page = self.catalog.iter_scope(ctx.generation, scope, Page(size=len(scope.document_ids)))
                documents = {d.id: d for d in page.items}
        hits: list[Hit] = []
        matched: dict[str, str] = {}
        unknown: list[str] = []
        for token in tokens:
            found = [(cid, how) for cid, how in resolved[token] if cid in documents]
            if not found:
                unknown.append(token)
                continue
            for cid, how in found:
                matched.setdefault(token, cid)
                if any(h.document_id == cid for h in hits):
                    continue
                doc = documents[cid]
                text = next((v for v in doc.fields.values() if v), "") or doc.title or doc.id
                hits.append(
                    Hit(
                        document_id=doc.id,
                        title=doc.title or doc.id,
                        source=doc.source,
                        url=doc.url,
                        text=text[:EXCERPT_CHARS],
                        chunk_ids=[],
                        score=1.0,
                        arms=[self.name],
                        provenance=[{"match": how, "identifier": token, "pinned": True}],
                    )
                )
        report = {"kind": REPORT_KIND, "requested": len(tokens) + len(over), "matched": matched, "unknown": unknown, "not_resolved_over_limit": over}
        ctx.trace.event("exact_id", requested=len(tokens) + len(over), matched=len(matched), unknown=len(unknown), over_limit=len(over))
        return ArmResult(arm=self.name, hits=hits, reports=[report])
