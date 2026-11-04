"""Structured arm: executes a typed ``ScopeSpec`` through ``RecordStore`` and returns the
``ScopeResult`` exactness metadata as an aggregate record. Today's typed aggregates (count,
group_count, first_seen) map onto ``ScopeSpec`` in the engine; nothing here builds SQL from text.

A ``ScopeSpec.text`` condition (a literal topic) is resolved here, before the record store runs:
every indexed chunk that matches it is enumerated through the lexical index of ``text_backend``
(the arm's own backend by default) under the request's eligibility scope, so the count is over the
whole eligible corpus, never a ranked top-k. The matched document ids become an ``id in`` predicate
(``role: scope``) or the share numerator (``role: numerator``); the result carries the match basis.
No lexical index, no phrase support for a phrase, or more than ``MAX_TEXT_CHUNKS`` matching chunks
is reported (``not_supported_by_backend``) or refused (``BackendError``), never guessed or cut.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from ..backends import capabilities as caps
from ..core.component import Diagnostic, Environment
from ..core.config import StructuredArm as StructuredArmConfig
from ..core.errors import BackendError
from ..core.types import GenerationId, LexicalQuery, Page, Predicate, Scope, ScopeResult, ScopeSpec, TextMatch, UnsupportedConstraint
from ..runtime.context import RunContext
from .base import ArmInput, ArmResult, BaseArm

BUILTIN_FIELDS = frozenset({"id", "source", "dataset", "kind"})  # document identity, always queryable
MAX_TEXT_CHUNKS = 50_000  # matching chunks one text condition may enumerate on the request path
_TEXT_PAGE = 10_000


def operation_name(spec: ScopeSpec) -> tuple[str, str | None]:
    if spec.periods:
        return "compare_periods", spec.time_basis
    if spec.group_by:
        if len(spec.group_by) == 2:
            return "cross_tab", "|".join(spec.group_by)
        if spec.nth is not None or spec.top_n is not None or spec.order is not None:
            return "rank", spec.group_by[0]
        return "group_count", spec.group_by[0]
    if spec.limit is not None:
        return "list", spec.sort.field if spec.sort else None
    if spec.measure is None or spec.measure.kind == "count":
        return "count", None
    if spec.measure.kind == "count_distinct":
        return "count_distinct", spec.measure.unit
    return spec.measure.kind, spec.measure.field


def text_query(text: TextMatch) -> LexicalQuery | None:
    words = re.findall(r"\w+", text.text)
    if not words:
        return None
    if text.kind == "phrase":
        return LexicalQuery(kind="phrase", text=" ".join(words))
    return LexicalQuery(kind="boolean", text=" ".join(words), must=words)


def text_basis(text: TextMatch, analyzer: Any) -> str:
    how = "the exact phrase" if text.kind == "phrase" else "every one of the words"
    words = " ".join(re.findall(r"\w+", text.text))
    role = "share numerator" if text.role == "numerator" else "scope"
    return (
        f"text {role}: documents with an indexed chunk containing {how} {words!r} under the lexical analyzer "
        f"{analyzer or 'unknown'}; a literal whole-corpus match, not a semantic judgement (other wordings are not counted)"
    )


def text_match_ids(lexical: Any, gen: GenerationId, query: LexicalQuery, scope: Scope, limit: int = MAX_TEXT_CHUNKS) -> list[str]:
    """Every document with a chunk matching ``query`` inside ``scope`` (sorted ids). Pages through
    ``LexicalIndex.search`` until exhausted; more than ``limit`` matching chunks is a typed refusal."""
    documents: set[str] = set()
    seen = 0
    cursor: str | None = None
    while True:
        page = lexical.search(gen, query, scope, limit + 1, Page(cursor=cursor, size=_TEXT_PAGE))
        seen += len(page.items)
        if seen > limit:
            raise BackendError("structured", "text", f"text condition matches more than {limit} indexed chunks; narrow the scope")
        documents.update(hit.document_id for hit in page.items)
        cursor = page.next_cursor
        if cursor is None or not page.items:
            return sorted(documents)


class StructuredArm(BaseArm):
    name: ClassVar[str] = "structured"
    config_model: ClassVar[type] = StructuredArmConfig
    capability: ClassVar[str] = "records"

    def _text_backend(self) -> Any:
        name = getattr(self.cfg, "text_backend", None)
        return self.deps.backends.get(name) if name else self.backend

    def validate(self, env: Environment, probe: bool = False) -> list[Diagnostic]:
        diagnostics = super().validate(env, probe)
        name = getattr(self.cfg, "text_backend", None)
        if name:
            backend = self.deps.backends.get(name)
            if backend is None or not self._serves_lexical(backend):
                diagnostics.append(
                    Diagnostic(severity="error", code="capability_missing", message=f"{self.name}: text_backend {name!r} does not serve lexical", path="text_backend", component=self.name)
                )
        return diagnostics

    @staticmethod
    def _serves_lexical(backend: Any) -> bool:
        kind = getattr(backend, "kind", "")
        return caps.satisfied(backend, "lexical") and kind in caps.MATRIX and caps.serves(kind, "lexical")

    def allowed_fields(self) -> set[str]:
        declared = {f.name for f in self.schema.fields} if self.schema is not None else set()
        return declared | BUILTIN_FIELDS

    def check_fields(self, spec: ScopeSpec) -> None:
        allowed = self.allowed_fields()
        named = [p.field for p in [*spec.predicates, *spec.numerator]] + list(spec.group_by)
        if spec.measure is not None and spec.measure.field:
            named.append(spec.measure.field)
        if spec.sort is not None:
            named.append(spec.sort.field)
        for field in named:
            if field not in allowed:
                raise ValueError(f"aggregate field {field!r} is not allowlisted (declare it in metadata_schema)")

    def resolve_text(self, ctx: RunContext, spec: ScopeSpec, scope: Scope) -> tuple[ScopeSpec, list[UnsupportedConstraint], list[str]]:
        """``(spec without text, unsupported, basis)``: the text condition as an id predicate."""
        text = spec.text
        if text is None:
            return spec, [], []
        stripped = spec.model_copy(update={"text": None})
        backend = self._text_backend()
        kind = getattr(backend, "kind", "")
        query = text_query(text)
        problem = None
        if backend is None or not self._serves_lexical(backend):
            problem = "no lexical index serves this structured arm (set pipeline.arms.structured.text_backend)"
        elif query is None:
            problem = "the text condition has no indexable words"
        elif text.kind == "phrase" and not caps.flags(kind, "lexical").get("phrase"):
            problem = f"the lexical index of {kind} has no phrase semantics"
        elif dropped := list(getattr(backend.lexical, "unindexable", lambda _w: [])(re.findall(r"\w+", text.text))):
            problem = f"the lexical analyzer indexes no token for {', '.join(dropped)!s}; the text count cannot be exact"
        if problem is not None:
            flagged = UnsupportedConstraint(constraint_text=f"text {text.kind} {text.text!r}", reason="not_supported_by_backend", suggestion=problem)
            return stripped, [flagged], []
        ids = text_match_ids(backend.lexical, ctx.generation, query, scope)  # type: ignore[arg-type]
        ctx.trace.event("text_match", kind=text.kind, role=text.role, documents=len(ids))
        condition = Predicate(field="id", op="in", value=list(ids))
        if text.role == "numerator":
            stripped = stripped.model_copy(update={"numerator": [*stripped.numerator, condition]})
        else:
            stripped = stripped.model_copy(update={"predicates": [*stripped.predicates, condition]})
        # The generation's recorded analyzer when the backend reports one (sqlite bm25, postgres).
        recorded = getattr(backend, "analyzer_id", None)
        analyzer_id = recorded(ctx.generation) if callable(recorded) else caps.flags(kind, "lexical").get("analyzer_id")
        return stripped, [], [text_basis(text, analyzer_id)]

    def _run(self, ctx: RunContext, inputs: ArmInput) -> ArmResult:
        spec = inputs.aggregate
        if spec is None:
            return ArmResult(arm=self.name)
        self.check_fields(spec)
        scope = inputs.scope
        constraints: list[Predicate] = [*inputs.acl, *scope.predicates]
        if scope.exclude_ids:
            constraints.append(Predicate(field="id", op="in", value=list(scope.exclude_ids), negate=True))
        if scope.document_ids is not None:
            constraints.append(Predicate(field="id", op="in", value=list(scope.document_ids)))
        executable, flagged, basis = self.resolve_text(ctx, spec, scope)
        result = self._facet().execute(ctx.generation, executable, constraints)
        if flagged:
            result = result.model_copy(update={"exactness": "bounded", "unsupported": [*result.unsupported, *flagged]})
        if basis:
            result = result.model_copy(update={"basis": [*result.basis, *basis]})
        return ArmResult(arm=self.name, aggregates=[aggregate_record(spec, result)])


def aggregate_record(spec: ScopeSpec, result: ScopeResult) -> dict[str, Any]:
    """The aggregate record the engine packs (``groups``/``document_ids`` stay out of the prompt)."""
    operation, field = operation_name(spec)
    aggregate: dict[str, Any] = {
        "operation": operation,
        "field": field,
        "value": result.value,
        "missing_or_invalid": result.missing_or_invalid,
        "document_ids": result.matched_ids,
        "scope": "filtered canonical records in this snapshot",
        "exactness": result.exactness,
        "denominator": result.denominator,
        "zero_match": result.zero_match,
        "unsupported": [u.model_dump(mode="json") for u in result.unsupported],
        "groups": [g.model_dump(mode="json") for g in result.groups],
        "total": result.total,
        "basis": list(result.basis),
    }
    if any(g.rank is not None or g.share is not None for g in result.groups) or spec.periods:
        aggregate["rows"] = [{k: v for k, v in g.model_dump(mode="json").items() if v is not None} for g in result.groups]
    if result.ties:
        aggregate["ties"] = [g.model_dump(mode="json", exclude_none=True) for g in result.ties]
        aggregate["note"] = f"{len(result.ties)} more group(s) tie the last group shown"
    if spec.periods and len(result.groups) >= 2 and all(isinstance(g.value, (int, float)) and not isinstance(g.value, bool) for g in result.groups[:2]):
        first, second = result.groups[0], result.groups[1]
        aggregate["comparison"] = {
            "from": first.key["period"],
            "to": second.key["period"],
            "difference": second.value - first.value,  # type: ignore[operator]
            "ratio": (second.value / first.value) if first.value else None,  # type: ignore[operator]
        }
    if spec.limit is not None:
        aggregate["listed"] = [r.model_dump(mode="json", exclude_none=True) for r in result.listed]
        aggregate["shown"] = f"showing {len(result.listed)} of {result.total}"
    if result.unknown:
        aggregate["unknown"] = dict(result.unknown)
    if result.numerator is not None:
        aggregate["numerator"] = result.numerator
    if spec.measure is not None and spec.measure.kind in {"sum", "avg", "min", "max", "first_seen", "last_seen"} and result.total is not None:
        aggregate["with_value"] = max(0, result.total - result.missing_or_invalid)
    return aggregate
