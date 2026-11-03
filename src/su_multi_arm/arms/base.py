"""Arms are ``Component``s of kind ``arm`` (plan §3.8): ``run(ctx, ArmInput) -> ArmResult``.

Invariants: an arm receives the already-applied eligibility ``Scope`` (request filters, exclusions
and the principal's ACL predicates) and never widens it; it reads the catalog only for the chunks it
hits (bounded by ``candidates``); it refuses to run on a generation whose stage it depends on is
incomplete unless the pipeline allows it, in which case every result is flagged ``incomplete``.
Hits are the v1 wire ``Hit`` (document-level, with chunk ids and per-arm provenance) so today's
fusion and answer paths consume them unchanged.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from ..backends import capabilities as caps
from ..core.component import Dependencies, Diagnostic, Environment
from ..core.errors import BackendError, ConfigError
from ..core.types import Chunk, Document, Page, Path, Scope, ScopePredicates, ScopeSpec
from ..models import Hit, StrictModel
from ..runtime.context import RunContext

MAX_CHUNKS_PER_DOCUMENT = 4
# Upper bound on the eligible set a graph backend without predicate pushdown may receive as an
# explicit document-id list (see ``BaseArm.graph_scope``); larger sets are a typed error, never truncated.
MAX_RESOLVED_SCOPE_DOCUMENTS = 10000


class ArmInput(StrictModel):
    query: str = Field(min_length=1)
    scope: Scope = Field(default_factory=Scope)  # eligibility incl. ACL; arms never widen it
    acl: ScopePredicates = Field(default_factory=list)  # ACL alone (RecordStore denominators)
    candidates: int = Field(default=200, ge=1, le=10000)
    aggregate: ScopeSpec | None = None  # structured arm only
    incomplete_stages: list[str] = Field(default_factory=list)  # from the generation manifest
    allow_incomplete: bool = False


class ArmResult(StrictModel):
    arm: str
    hits: list[Hit] = Field(default_factory=list)
    aggregates: list[dict[str, Any]] = Field(default_factory=list)
    paths: list[Path] = Field(default_factory=list)
    reports: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    incomplete: bool = False


class IncompleteStageError(RuntimeError):
    """The arm depends on a stage the generation did not complete (partial policy)."""


class BaseArm:
    name: ClassVar[str] = "base"
    kind: ClassVar[str] = "arm"
    config_model: ClassVar[type[StrictModel]] = StrictModel
    capability: ClassVar[str] = "catalog"
    depends_on_stage: ClassVar[str | None] = None

    def __init__(self, config: StrictModel, deps: Dependencies):
        self.cfg = config
        self.deps = deps
        backend_name = getattr(config, "backend", None)
        self.backend = deps.backends.get(backend_name) if backend_name else None
        self.catalog = deps.components.get("catalog")
        self.schema = deps.metadata_schema

    def validate(self, env: Environment, probe: bool = False) -> list[Diagnostic]:
        diagnostics = []
        if self.backend is None:
            diagnostics.append(Diagnostic(severity="error", code="backend_missing", message=f"{self.name}: backend instance not provided", component=self.name))
        elif not caps.satisfied(self.backend, self.capability):
            diagnostics.append(Diagnostic(severity="error", code="capability_missing", message=f"{self.name}: backend {getattr(self.backend, 'kind', '?')} lacks {self.capability}", component=self.name))
        if self.catalog is None:
            diagnostics.append(Diagnostic(severity="error", code="catalog_missing", message=f"{self.name}: catalog facet not provided", component=self.name))
        return diagnostics

    def recipe_fingerprint(self) -> str | None:
        return None

    def run(self, ctx: RunContext, inputs: ArmInput) -> ArmResult:
        with ctx.trace.span(f"arm.{self.name}", arm=self.name, candidates=inputs.candidates) as span:
            incomplete = bool(self.depends_on_stage and self.depends_on_stage in inputs.incomplete_stages)
            if incomplete and not inputs.allow_incomplete:
                span.event("refused", reason="incomplete_stage", stage=self.depends_on_stage)
                raise IncompleteStageError(
                    f"arm {self.name} depends on stage {self.depends_on_stage!r}, which is incomplete in this generation; "
                    "set pipeline.allow_incomplete_arms to run it flagged"
                )
            result = self._run(ctx, inputs)
            result.incomplete = incomplete
            span.set(documents=len(result.hits), aggregates=len(result.aggregates), paths=len(result.paths), incomplete=incomplete)
            return result

    def _run(self, ctx: RunContext, inputs: ArmInput) -> ArmResult:  # pragma: no cover - abstract
        raise NotImplementedError

    # --- shared helpers --------------------------------------------------------------------

    def _facet(self) -> Any:
        return getattr(self.backend, self.capability)

    def graph_scope(self, ctx: RunContext, scope: Scope) -> Scope:
        """The scope to push into this arm's graph facet. Backends whose graph capability flags
        ``predicate_scope: false`` (neo4j) evaluate eligibility as document-id allow/deny lists only, so
        predicates (request filters, ACL constraints) are first resolved to document ids through the
        catalog, never dropped. Unknown backend kinds are treated the same way (resolution is always
        correct, only less direct). More than ``MAX_RESOLVED_SCOPE_DOCUMENTS`` eligible documents is a
        typed ``BackendError``: the list is never truncated."""
        kind = getattr(self.backend, "kind", None)
        pushdown = kind in caps.MATRIX and bool(caps.flags(kind, "graph").get("predicate_scope", False))
        if pushdown or not scope.predicates:
            return scope
        found: list[str] = []
        cursor = None
        while True:
            page = self.catalog.iter_scope(ctx.generation, scope, Page(cursor=cursor, size=1000))
            found.extend(d.id for d in page.items)
            if len(found) > MAX_RESOLVED_SCOPE_DOCUMENTS:
                raise BackendError(
                    str(kind), "scope", f"the eligible set exceeds {MAX_RESOLVED_SCOPE_DOCUMENTS} documents and the graph backend cannot evaluate predicates; narrow the request"
                )
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        return Scope(document_ids=sorted(set(found)), exclude_ids=list(scope.exclude_ids))

    def collapse(self, ctx: RunContext, scored: list[tuple[str, float, dict[str, Any]]]) -> list[Hit]:
        """Chunk hits -> document hits (first chunk's text, up to four chunk ids), sorted by score then id.
        Catalog reads are bounded by the number of hits."""
        if not scored:
            return []
        chunk_ids = list(dict.fromkeys(cid for cid, _, _ in scored))
        chunks: dict[str, Chunk] = {c.id: c for c in self.catalog.get_chunks(ctx.generation, chunk_ids)}
        doc_ids = list(dict.fromkeys(chunks[cid].document_id for cid in chunk_ids if cid in chunks))
        documents: dict[str, Document] = {d.id: d for d in self.catalog.get(ctx.generation, doc_ids)}
        result: dict[str, Hit] = {}
        for cid, score, extra in scored:
            chunk = chunks.get(cid)
            if chunk is None or chunk.document_id not in documents:
                continue
            provenance = {"chunk_id": chunk.id, "field": chunk.field, "start": chunk.start, "end": chunk.end, **extra}
            doc = documents[chunk.document_id]
            if doc.id not in result:
                result[doc.id] = Hit(
                    document_id=doc.id,
                    title=doc.title or doc.id,
                    source=doc.source,
                    url=doc.url,
                    text=chunk.text,
                    chunk_ids=[chunk.id],
                    score=float(score),
                    arms=[self.name],
                    provenance=[provenance],
                )
            else:
                hit = result[doc.id]
                if len(hit.chunk_ids) < MAX_CHUNKS_PER_DOCUMENT and cid not in hit.chunk_ids:
                    hit.chunk_ids.append(cid)
                    hit.provenance.append(provenance)
        return sorted(result.values(), key=lambda h: (-h.score, h.document_id))


def arm_capability(name: str) -> str:
    try:
        return caps.ARM_CAPABILITY[name][0]
    except KeyError:
        raise ConfigError(f"unknown arm {name!r}", "pipeline.arms") from None
