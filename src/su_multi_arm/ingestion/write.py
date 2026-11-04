"""The single write path on the frozen contracts (plan §3.14), multi-backend since Phase 1.

``WritePath.ingest`` builds a staged generation on every backend that holds one of its projections
(placement and activation rules: ``ingestion/generations.py``): catalog (+ scope mirrors on the
secondary backends) -> lexical -> records -> extraction (under the configured policy) ->
embeddings -> communities/reports -> graph, then runs per-projection validation on every target
(row counts against the catalog, a sample of ids round-tripping through catalog, mirrors, records,
lexical and vector, graph memberships and sampled entities, community/membership consistency,
``refresh`` visible) and only then activates: secondary pointers first, the authoritative
``CURRENT`` last, with compensation (pointers already moved are put back) when any step fails. Any
failure leaves the old generation active everywhere, removes the staged generation from every
backend it reached and records a failure manifest naming the failing unit, its category, every
staged ``(backend, projection)`` pair and anything left for cleanup. The manifest lists every
backend's generation identity, activation mechanism and lexical analyzer id. Per-document status
lives in the ledger (``document_status`` entries keyed by the generation id); extraction outputs and
raw model replies are kept in the ledger for reuse and replay.

**Writers.** A build runs under the registry's write lease (``ingestion/lease.py``: owner token,
heartbeat renewal, owner-only release); a second writer is refused with ``WriteConflict(held)``.
Before activation the lease is renewed synchronously, and every pointer switch is a compare-and-set
on what it named before (``CURRENT``: the generation the build started from). A lost lease or a
moved base fails the build with ``IngestConflict`` (also a ``WriteConflict``) and never switches
``CURRENT``.

**Incremental embeddings and budgets.** Embedding inputs are streamed in ``batch_size`` windows; a
vector is reused from the ledger when its exact input (title + field + chunk text) and recipe were
embedded before, so an upsert embeds only new or changed chunks (manifest ``embedded`` /
``reused_embeddings``). Everything else of the new generation is still rebuilt (manifest
``incremental``). LLM calls, embedding requests and USD of the build are limited by
``budgets.ingest`` (``ingestion/budget.py``), never by the per-request ``budgets.max_llm_calls``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Sequence

import numpy as np

from ..backends import extras
from ..backends._scope import document_values
from ..communities.base import build_communities
from ..communities.reports import ReportFailure, community_report, disabled_report
from ..communities.stats import community_stats
from ..core.errors import BackendError, BudgetExhausted, ConfigError, GenerationInUse, IndexNotReady, WriteConflict
from ..core.types import (
    Chunk,
    Document,
    Entity,
    ExtractionOutput,
    GenerationId,
    LedgerEntry,
    LexicalQuery,
    Membership,
    Provenance,
    Relation,
    Scope,
    StructuredRecord,
)
from ..extraction.base import BuiltinExtractor, ExtractionFailure, ExtractionInput, canonicalise
from ..extraction.chunking import chunk_document, to_v2_document
from ..extraction.dedupe import dedupe_relations
from ..providers import tokens
from ..runtime.budget import load_price_table
from .budget import IngestMeter, check_prices, require_ingest_cap
from .normalize import redact_document
from .generations import GenerationSet
from .policy import ExtractionPolicy, PolicyViolation
from .precomputed import IMPORTED_INPUT, PrecomputedVectorError, PrecomputedVectors, spot_check
from .release import GenerationRegistry, generation_id

INDEX_FORMAT = 3
SAMPLE = 5
EMBEDDING_INPUT = "{title}\n{field}: {text}"  # the template ``WritePath.embedding_input`` applies
# What an upsert (``ingest`` without ``replace``, the ``ingest_delta`` action) does incrementally
# today, recorded in every manifest. Everything else is rebuilt into the new generation.
INCREMENTAL = {
    "embeddings": "reused by exact input and recipe; only new or changed chunks are embedded",
    "extraction": "reused per chunk and extractor recipe from the ledger; only new or changed chunks are extracted",
    "rebuilt": ["catalog", "lexical", "records", "vectors (re-written, not re-embedded)", "graph", "communities", "community reports"],
}


class IngestError(RuntimeError):
    """A build failed. ``failure`` is the recorded failure manifest; the old generation is active."""

    def __init__(self, failure: dict[str, Any]):
        self.failure = failure
        super().__init__(f"{failure['category']}: {failure['message']}")


class IngestConflict(IngestError, WriteConflict):
    """A build lost the write path to another writer: its lease was lost, or ``CURRENT`` no longer
    names the generation it started from. Nothing was activated; ``failure`` is recorded as for any
    failed build (category ``write_conflict:<reason>``). Also a ``WriteConflict``, like the
    ``held`` refusal raised before a build starts."""

    def __init__(self, failure: dict[str, Any], conflict: WriteConflict):
        self.failure, self.conflict = failure, conflict
        self.adapter, self.operation, self.reason = conflict.adapter, conflict.operation, conflict.reason
        RuntimeError.__init__(self, f"{failure['category']}: {failure['message']}")


class WritePath:
    def __init__(
        self,
        config: Any,
        backends: dict[str, Any],
        *,
        extractor: BuiltinExtractor,
        embedder: Any,
        community_model: Any = None,
        profile: str = "neutral",
        config_fingerprint: str | None = None,
    ):
        self.cfg = config
        self.backends = backends
        self.extractor = extractor
        self.embedder = embedder
        self.community_model = community_model
        self.profile = profile
        self.config_fingerprint = config_fingerprint or config.fingerprint()
        storage, arms = config.storage, config.pipeline.arms
        self.slots: dict[str, str] = {"catalog": storage.catalog, "records": storage.records, "ledger": storage.ledger, "communities": storage.communities}
        if arms.lexical:
            self.slots["lexical"] = arms.lexical.backend
        if arms.dense:
            self.slots["vector"] = arms.dense.backend
        if arms.graph:
            self.slots["graph"] = arms.graph.backend
        for slot, name in self.slots.items():
            if name not in backends:
                raise ConfigError(f"no backend instance named {name!r} for {slot}", f"storage.{slot}")
        self.generations = GenerationSet(config, backends)
        registry = getattr(backends[storage.catalog], "generation_registry", None)
        if not callable(registry):
            kind = getattr(backends[storage.catalog], "kind", "?")
            raise ConfigError(f"backend {storage.catalog!r} ({kind}) has no generation registry and cannot hold the authoritative catalog", "storage.catalog")
        self.registry: GenerationRegistry = registry()
        budgets = config.budgets
        self.price_table = load_price_table(budgets.price_table) if budgets.price_table else None
        check_prices(config, self.price_table, "llm" in getattr(config.extraction, "extractors", []))
        require_ingest_cap(config)

    # --- helpers -----------------------------------------------------------------------------

    def _facet(self, slot: str, capability: str | None = None) -> Any:
        backend = self.backends[self.slots[slot]]
        return getattr(backend, capability or slot)

    def index_fingerprint(self) -> str:
        """Composed from the index-affecting recipes: chunking, privacy, extractor, embedder, communities, backends."""
        roles = self.cfg.models.roles
        data = {
            "dataset": self.cfg.dataset,
            "index_format": INDEX_FORMAT,
            "chunking": self.cfg.ingestion.chunking.model_dump(mode="json"),
            "privacy": self.cfg.privacy.model_dump(mode="json", exclude={"trace_masking"}),
            "extractor": self.extractor.recipe_fingerprint(),
            # ``precomputed`` enters only when set (2.0.0a1 golden values unchanged) and only as its
            # content identity (file digests), never its paths: moving the files keeps the index.
            "embedder": {
                **roles.embedder.model_dump(mode="json", exclude={"pricing_key", "precomputed"}),
                **({"precomputed": roles.embedder.precomputed.identity()} if roles.embedder.precomputed is not None else {}),
            },
            "communities": self.cfg.communities.recipe(),
            "community_model": None if roles.community is None else roles.community.model,
            "backends": {slot: _backend_recipe(self.cfg.backends[name]) for slot, name in sorted(self.slots.items())},
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def legacy_fingerprint(self) -> str | None:
        try:
            return self.cfg.legacy_fingerprint()
        except (ConfigError, ValueError):
            return None

    def current(self) -> tuple[GenerationId, dict[str, Any]]:
        """The active generation, checked against this configuration's recipe."""
        manifest = self.registry.current()
        if manifest is None:
            raise IndexNotReady("no active index; ingest documents first")
        if manifest.get("schema_version") != 2:
            raise IndexNotReady("active generation was built by a schema_version 1 engine; run ingest --replace --rebuild")
        if manifest["fingerprint"] != self.index_fingerprint():
            raise IndexNotReady("index/config fingerprint mismatch: restore original config or rebuild explicitly")
        return generation_id(manifest), manifest

    def pin(self) -> tuple[GenerationId, dict[str, Any]]:
        """The one generation a request reads on every backend: ``CURRENT`` (checked as ``current``)
        after every secondary pointer has been confirmed to name it. A disagreement (a failed
        compensation, a concurrent writer, a backend switched by hand) is a typed
        ``GenerationDisagreement``; the request is refused, never served from mixed generations."""
        gen, manifest = self.current()
        self.generations.check(gen)
        return gen, manifest

    def status(self) -> dict[str, Any]:
        gen, manifest = self.current()
        ledger = self._facet("ledger")
        documents = {
            e.payload["document_id"]: {k: v for k, v in e.payload.items() if k != "document_id"}
            for e in ledger.replay(gen.generation)
            if e.kind == "document_status"
        }
        return {"manifest": manifest, "documents": documents, "failures": self.registry.failures()}

    # --- ingest ------------------------------------------------------------------------------

    def ingest(
        self,
        documents: Sequence[Any],
        *,
        replace: bool = False,
        delete_ids: Sequence[str] = (),
        rebuild: bool = False,
        allow_partial: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        cfg = self.cfg
        documents = [to_v2_document(d) for d in documents]
        if len({d.id for d in documents}) != len(documents):
            raise ValueError("duplicate document IDs in input")
        if any(d.dataset != cfg.dataset for d in documents):
            raise ValueError("document dataset must match configured dataset")
        if set(delete_ids) & {d.id for d in documents}:
            raise ValueError("cannot upsert and delete the same ID")
        with self.registry.lock() as lease:
            previous: GenerationId | None = None
            existing: dict[str, Document] = {}
            active = self.registry.current()
            base = None if active is None else active["generation"]  # activation is a compare-and-set on it
            if active is not None:
                try:
                    previous, _ = self.current()
                except ValueError:
                    if not (replace and rebuild):
                        raise
                if previous is not None and not replace:
                    existing = self._load_corpus(previous)
            for doc_id in delete_ids:
                existing.pop(doc_id, None)
            redactions: dict[str, int] = {}
            for document in documents:
                document, counts = redact_document(document, cfg.privacy)
                existing[document.id] = document
                for label, count in counts.items():
                    redactions[label] = redactions.get(label, 0) + count
            corpus = [existing[k] for k in sorted(existing)]
            for document in corpus:
                violations = cfg.metadata_schema.validate_document(document)
                if violations:
                    first = violations[0]
                    raise IngestError(self._failure(None, f"metadata.{first.category}", document.id, f"{first.field}: {first.message}", "validate", []))
            generation = uuid.uuid4().hex
            gen = GenerationId(dataset=cfg.dataset, generation=generation, index_format=INDEX_FORMAT, fingerprint=self.index_fingerprint())
            for backend in self._distinct_backends():
                if previous is not None:
                    backend.protect(previous)
            policy = ExtractionPolicy(cfg.extraction.policy)
            reached: list[tuple[str, str]] = []
            try:
                manifest = self._build(gen, corpus, policy, reached, redactions, started)
                if policy.coverage.incomplete and not allow_partial:
                    stage = next(n for n, s in policy.coverage.stages.items() if s.status == "incomplete")
                    raise PolicyViolation("incomplete_stage", policy.coverage.stages[stage].first_unprocessed, f"stage {stage} is incomplete; pass allow_partial to activate a partial generation", stage=stage)
                if lease is not None:
                    lease.check()  # one synchronous renewal: a lost lease never reaches activation
                    manifest["lease"] = lease.state()
                self.registry.write_manifest(manifest)
                self._activate(gen, manifest, base)
                for backend in self._distinct_backends():  # the new generation is the one to keep now
                    backend.protect(gen)
                    if previous is not None:
                        backend.unprotect(previous)
                self._facet("ledger").mark(generation, policy.manifest_status())
                return manifest
            except BaseException as exc:
                failure = self._failure(gen, *self._categorise(exc), reached)
                if isinstance(exc, ActivationError):
                    failure["compensation"] = exc.compensation
                self._cleanup(gen, failure)
                self.registry.record_failure(failure)
                try:
                    self._facet("ledger").mark(generation, "failed")
                except BackendError:
                    pass
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                conflict = _conflict(exc)
                if conflict is not None:
                    raise IngestConflict(failure, conflict) from exc
                raise IngestError(failure) from exc

    def _activate(self, gen: GenerationId, manifest: dict[str, Any], base: str | None) -> None:
        """Secondary pointers move first (``GenerationSet`` order: derived projections), then the
        authoritative ``CURRENT`` as a compare-and-set on ``base`` (the generation the build started
        from; each secondary on what it named just before). If any step fails, the pointers already moved are put back, in
        reverse order, to what they named before (or removed), ``CURRENT`` is left unchanged and an
        ``ActivationError`` carries the per-backend compensation outcome. A pointer that cannot be
        put back keeps naming the failed generation: its cleanup is refused (listed in
        ``left_for_cleanup``) and readers are refused with ``GenerationDisagreement`` until an
        operator or the next successful build realigns it."""
        moved: list[tuple[Any, str | None]] = []
        step = self.generations.authoritative.name
        try:
            for holder in self.generations.secondaries:
                if holder.activation == "none":
                    continue
                step = holder.name
                before = self.generations.active(holder, gen.dataset)
                self.generations.activate(holder, gen, manifest, expected=before)
                moved.append((holder, before))
            step = self.generations.authoritative.name
            self.registry.activate(manifest, expected=base)
        except BaseException as exc:
            compensation = []
            for holder, before in reversed(moved):
                try:
                    self.generations.restore(holder, gen, before)
                    compensation.append({"backend": holder.name, "restored_to": before, "ok": True})
                except Exception as err:  # recorded, never swallowed: the reader check refuses the mix
                    compensation.append({"backend": holder.name, "restored_to": before, "ok": False, "error": str(err)})
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ActivationError(step, exc, compensation) from exc

    def delete_generation(self, generation: str) -> dict[str, Any]:
        """Remove one generation from every backend that held it (plan §3.5 destructive-operation rule).

        Refused with ``GenerationInUse`` before anything is deleted when the generation is the
        authoritative ``CURRENT``, is named by any secondary pointer, or is protected (active or
        pinned by a live session) on the first backend asked. Backends are visited in activation
        order; a later refusal or error is reported in ``left_for_cleanup``, never ignored. The
        manifest stays in the registry as the audit record (``verify``/``gc`` arrive with Phase 2)."""
        with self.registry.lock():
            manifest = self.registry.manifest(generation)
            if manifest is None:
                raise ValueError(f"unknown generation {generation!r}")
            gen = generation_id(manifest)
            current = self.registry.current()
            if current is not None and current["generation"] == generation:
                raise GenerationInUse(f"generation {generation} is CURRENT for dataset {gen.dataset!r}")
            holding = sorted(name for name, value in self.generations.pointers(gen.dataset).items() if value == generation)
            if holding:
                raise GenerationInUse(f"generation {generation} is still active on {holding}")
            recorded = list((manifest.get("generations") or {}).get("backends", {})) or [h.name for h in self.generations.holders]
            order = [h.name for h in self.generations.holders if h.name in recorded] + [n for n in recorded if n not in {h.name for h in self.generations.holders}]
            deleted: list[str] = []
            left: list[dict[str, Any]] = []
            for name in order:
                backend = self.backends.get(name)
                if backend is None:
                    left.append({"backend": name, "generation": generation, "error": "backend is not configured"})
                    continue
                try:
                    _delete_on(backend, gen)
                except BackendError as exc:
                    if not deleted and "active or pinned" in str(exc):
                        raise GenerationInUse(f"generation {generation} is protected on {name!r}: {exc}") from exc
                    left.append({"backend": name, "generation": generation, "error": str(exc)})
                    continue
                deleted.append(name)
            return {"generation": generation, "deleted": deleted, "left_for_cleanup": left}

    def _distinct_backends(self) -> list[Any]:
        """Backends holding generation-scoped projections (ledger/session/cache-only backends excluded)."""
        seen: dict[int, Any] = {}
        for holder in self.generations.holders:
            seen.setdefault(id(holder.backend), holder.backend)
        return list(seen.values())

    def _load_corpus(self, gen: GenerationId) -> dict[str, Document]:
        from ..core.types import Page

        catalog = self._facet("catalog")
        found: dict[str, Document] = {}
        cursor = None
        while True:
            page = catalog.iter_scope(gen, None, Page(cursor=cursor, size=500))
            for document in page.items:
                found[document.id] = document
            if page.next_cursor is None:
                return found
            cursor = page.next_cursor

    @staticmethod
    def _categorise(exc: BaseException) -> tuple[str, str | None, str, str | None]:
        conflict = _conflict(exc)
        if conflict is not None:
            return f"write_conflict:{conflict.reason}", None, str(exc), "activate"
        if isinstance(exc, PolicyViolation):
            return exc.category, exc.unit, str(exc), exc.stage
        if isinstance(exc, ExtractionFailure):
            return exc.category, exc.unit, str(exc), "extraction"
        if isinstance(exc, ReportFailure):
            return exc.category, exc.community_id, str(exc), "community_reports"
        if isinstance(exc, BackendError):
            return f"backend:{exc.adapter}.{exc.operation}", None, str(exc), None
        if isinstance(exc, ProjectionError):
            return f"projection:{exc.projection}", exc.unit, str(exc), "validate"
        if isinstance(exc, ActivationError):
            return f"activation:{exc.backend}", None, str(exc), "activate"
        return type(exc).__name__, None, str(exc), None

    def _failure(self, gen: GenerationId | None, category: str, unit: str | None, message: str, stage: str | None, reached: list[tuple[str, str]]) -> dict[str, Any]:
        return {
            "generation": gen.generation if gen else None,
            "dataset": self.cfg.dataset,
            "policy": self.cfg.extraction.policy,
            "category": category,
            "unit": unit,
            "stage": stage,
            "message": message,
            "staged_projections": sorted({name for name, _ in reached}),
            "staged": [{"backend": name, "projection": projection} for name, projection in dict.fromkeys(reached)],
            "left_for_cleanup": [],
            "at": datetime.now(timezone.utc).isoformat(),
        }

    def _cleanup(self, gen: GenerationId, failure: dict[str, Any]) -> None:
        for holder in self.generations.holders:
            try:
                _delete_on(holder.backend, gen)
            except Exception as exc:  # the failure record lists what the operator must remove
                failure["left_for_cleanup"].append({"backend": holder.name, "kind": holder.kind, "generation": gen.generation, "error": str(exc)})

    # --- build --------------------------------------------------------------------------------

    def _build(self, gen: GenerationId, corpus: list[Document], policy: ExtractionPolicy, reached: list[str], redactions: dict[str, int], started: float) -> dict[str, Any]:
        cfg = self.cfg
        chunking = cfg.ingestion.chunking
        ledger = self._facet("ledger")
        meter = IngestMeter(cfg.budgets.ingest, self.price_table)
        roles = cfg.models.roles
        sequence = [0]

        def log(kind: str, **payload: Any) -> None:
            ledger.append(gen.generation, LedgerEntry(sequence=sequence[0], kind=kind, at=datetime.now(timezone.utc).isoformat(), payload=payload))
            sequence[0] += 1

        log("start", policy=policy.name, fingerprint=gen.fingerprint)
        chunks_by_doc = {d.id: chunk_document(d, chunking.size, chunking.overlap, chunking.field_order) for d in corpus}
        all_chunks = [c for d in corpus for c in chunks_by_doc[d.id]]
        # 1. catalog (authoritative), then the scope mirrors on secondary backends
        reached.append((self.slots["catalog"], "catalog"))
        self._facet("catalog").put_documents(gen, corpus, all_chunks)
        for holder in self.generations.mirrors():
            reached.append((holder.name, "scope_mirror"))
            holder.backend.catalog.put_documents(gen, corpus, all_chunks)
        # 2. lexical
        if "lexical" in self.slots:
            reached.append((self.slots["lexical"], "lexical"))
            lexical = self._facet("lexical")
            lexical.index(gen, all_chunks, [])
            lexical.refresh(gen)
        # 3. records (storage.records and the structured arm's backend)
        records = [StructuredRecord(schema_id="document", id=d.id, values=document_values(d), provenance=Provenance(extractor="catalog")) for d in corpus]
        for holder in self.generations.targets("records"):
            reached.append((holder.name, "records"))
            holder.backend.records.upsert(gen, records, cfg.metadata_schema)
        # 4. extraction under policy
        extraction_meter = meter.role(roles.extractor.price_key() if roles.extractor else None, "extraction")
        outputs, reused, llm_calls = self._extract(gen, corpus, chunks_by_doc, policy, extraction_meter, ledger)
        # 5. embeddings (streamed in batches; unchanged inputs reuse their stored vectors)
        if "vector" in self.slots:
            reached.append((self.slots["vector"], "vector"))
        embedded = self._embed(gen, corpus, all_chunks, policy, meter, ledger)
        # 6. graph: canonical mentions, deduped relations, communities
        mentions: list[Entity] = []
        relations: list[Relation] = []
        per_doc: dict[str, dict[str, int]] = {}
        for document in corpus:
            counts = {"entities": 0, "facts": 0, "co_occurrences": 0}
            for output in outputs.get(document.id, []):
                entities, rels = canonicalise(output)
                mentions.extend(entities)
                relations.extend(rels)
                counts["entities"] += len(entities)
                counts["facts"] += sum(1 for r in rels if r.edge_kind == "fact")
                counts["co_occurrences"] += sum(1 for r in rels if r.edge_kind != "fact")
            per_doc[document.id] = counts
        relations = dedupe_relations(relations)
        membership_map: dict[str, set[str]] = {}
        names: dict[str, str] = {}
        for mention in mentions:
            membership_map.setdefault(mention.id, set()).add(mention.span.document_id)
            names.setdefault(mention.id, mention.name)
        communities = build_communities(cfg.communities.strategy, membership_map, names, cfg.communities.min_documents, cfg.communities.seed, cfg.communities.hierarchy)
        if cfg.communities.stats is not None:
            stats_cfg = cfg.communities.stats
            by_id = {d.id: d for d in corpus}
            for community in communities:
                community.stats = community_stats(
                    community, by_id, membership_map, len(corpus), cfg.metadata_schema, stats_cfg.time_basis, stats_cfg.granularity, stats_cfg.top_entities, names
                )
        report_meter = meter.role(roles.community.price_key() if roles.community else None, "community_reports")
        communities = self._reports(communities, all_chunks, policy, report_meter)
        memberships = [Membership(community_id=c.id, member_id=doc_id, basis="entity") for c in communities for doc_id in c.document_ids]
        community_count = {doc_id: 0 for doc_id in per_doc}
        for membership in memberships:
            community_count[membership.member_id] += 1
        for holder in self.generations.targets("graph"):
            reached.append((holder.name, "graph"))
            holder.backend.graph.upsert(gen, mentions, relations, memberships)
        for holder in self.generations.targets("communities"):
            reached.append((holder.name, "communities"))
            extras.require(holder.backend, extras.GraphExtras, "graph", "put_communities").put_communities(gen, communities)
        # 7. per-document status into the ledger
        for document in corpus:
            parts = chunks_by_doc[document.id]
            counts = per_doc[document.id]
            log(
                "document_status",
                document_id=document.id,
                chunks=len(parts),
                dense=("indexed" if embedded is not None and all(c.id in embedded.ids for c in parts) else "incomplete") if parts else "empty",
                lexical="indexed" if parts else "empty",
                structured="canonical_record",
                extraction=policy.document_status(document.id),
                entities=counts["entities"],
                explicit_relations=counts["facts"],
                co_occurrence_relations=counts["co_occurrences"],
                graph=(
                    "grounded_entities"
                    if counts["entities"]
                    else ("excluded" if document.id in policy.rejected else ("incomplete" if document.id in policy.not_processed else "no_entities_found"))
                ),
                fields={k: len(v) for k, v in document.fields.items()},
                community_memberships=community_count[document.id],
            )
        # 8. per-projection validation
        projections = self._validate_projections(gen, corpus, all_chunks, embedded)
        projections.update(self._validate_graph(gen, corpus, mentions, communities, memberships))
        facts = [r for r in relations if r.edge_kind == "fact"]
        co_occurrences = [r for r in relations if r.edge_kind == "co_occurrence"]
        manifest = {
            "schema_version": 2,
            "generation": gen.generation,
            "dataset": cfg.dataset,
            "index_format": INDEX_FORMAT,
            "fingerprint": gen.fingerprint,
            "legacy_fingerprint": self.legacy_fingerprint(),
            "config_fingerprint": self.config_fingerprint,
            "profile": self.profile,
            "policy": policy.name,
            "status": policy.manifest_status(),
            "documents": len(corpus),
            "chunks": len(all_chunks),
            "entities": len(mentions),
            "relations": len(facts),
            "relation_occurrences": sum(len(r.occurrences) for r in facts),
            "co_occurrence_relations": len(co_occurrences),
            "co_occurrence_occurrences": sum(len(r.occurrences) for r in co_occurrences),
            "communities": len(communities),
            "reused_extractions": reused,
            "seconds": time.perf_counter() - started,
            "redactions_in_input": redactions,
            "llm_calls": meter.calls,
            "embedded": embedded.embedded if embedded else 0,
            "reused_embeddings": embedded.reused if embedded else 0,
            "imported_embeddings": embedded.imported if embedded else 0,
            **({"precomputed_vectors": embedded.precomputed} if embedded and embedded.precomputed else {}),
            "budget": meter.summary(),
            "incremental": INCREMENTAL,
            "embedding": {
                **cfg.models.roles.embedder.model_dump(mode="json", exclude={"precomputed"}),
                **({"precomputed": cfg.models.roles.embedder.precomputed.identity()} if cfg.models.roles.embedder.precomputed is not None else {}),
            },
            "embedding_recipe": self.embedding_recipe(),
            "coverage": policy.coverage.to_manifest(),
            "projections": projections,
            "backends": dict(sorted(self.slots.items())),
            "generations": self.generations.manifest_entry(gen),
        }
        log("manifest", status=manifest["status"], documents=len(corpus), chunks=len(all_chunks))
        return manifest

    def _extract(self, gen: GenerationId, corpus: list[Document], chunks_by_doc: dict[str, list[Chunk]], policy: ExtractionPolicy, budget: Any, ledger: Any):
        outputs: dict[str, list[ExtractionOutput]] = {}
        recipe = (self.extractor.recipe_fingerprint() or "none")[:16]
        reused = llm_calls = processed = 0
        total = sum(len(v) for v in chunks_by_doc.values())
        for document in corpus:
            parts = chunks_by_doc[document.id]
            collected: list[ExtractionOutput] = []
            for chunk in parts:
                key = f"extraction:{recipe}:{chunk.id}"
                cached = ledger.get_raw(key)
                if cached is not None and cached.valid:
                    collected.append(ExtractionOutput.model_validate_json(cached.raw_reply))
                    reused += 1
                    processed += 1
                    continue
                try:
                    result = self.extractor.run(None, ExtractionInput(chunk=chunk, document=document, chunks=parts, budget=budget))
                except ExtractionFailure as exc:
                    for raw in exc.raw_replies:  # every model reply is kept, valid or not (B8)
                        ledger.put_raw(raw.key, raw.raw_reply, raw.valid)
                    if exc.category in {"provider", "budget"}:
                        unreached = [d.id for d in corpus[corpus.index(document) :] if d.id not in policy.rejected]
                        policy.stage_failed("extraction", exc.category, str(exc), processed, total - processed, chunk.id, unreached)
                        policy.coverage.extraction.documents_without_graph.extend(
                            d for d in unreached if d not in policy.coverage.extraction.documents_without_graph
                        )
                        return outputs, reused, llm_calls
                    policy.reject_document(document.id, exc.category, exc.unit, str(exc))
                    collected = []
                    break
                for raw in result.raw_replies:
                    ledger.put_raw(raw.key, raw.raw_reply, raw.valid)
                llm_calls += result.calls
                ledger.put_raw(key, result.output.model_dump_json(), True)
                collected.append(result.output)
                processed += 1
            if document.id in policy.rejected:
                processed += len(parts) - len(collected)  # the rest of the document's chunks are skipped
                continue
            policy.accept_document(document.id)
            outputs[document.id] = collected
        policy.stage_complete("extraction", processed)
        return outputs, reused, llm_calls

    def embedding_recipe(self) -> dict[str, Any]:
        """Everything besides the input text that determines a stored vector: provider, model,
        revision, dimension, endpoint of hosted models, document prefix, normalisation and the input
        template (title and field name are embedded with the chunk text)."""
        role = self.cfg.models.roles.embedder
        recipe = {
            "provider": role.provider,
            "model": role.model,
            "revision": role.revision,
            "dimensions": role.dimensions,
            "endpoint": role.base_url if role.provider in {"openai", "openai_compatible"} else None,
            "document_prefix": role.document_prefix,
            "normalisation": "l2",
            "input": EMBEDDING_INPUT,
        }
        if role.precomputed is not None:  # absent otherwise, so earlier embedding reuse keys stay valid
            recipe["precomputed"] = role.precomputed.identity()
            recipe["input"] = IMPORTED_INPUT  # imported rows do not embed the engine's input template
        return recipe

    @staticmethod
    def embedding_input(title: str, chunk: Chunk) -> str:
        return EMBEDDING_INPUT.format(title=title, field=chunk.field, text=chunk.text)

    @staticmethod
    def embedding_key(recipe_sha: str, text: str) -> str:
        return f"embedding:{recipe_sha}:{hashlib.sha256(text.encode()).hexdigest()}"

    def _embed(self, gen: GenerationId, corpus: list[Document], chunks: list[Chunk], policy: ExtractionPolicy, meter: IngestMeter, ledger: Any) -> "Embedded | None":
        """Stream the chunks in ``batch_size`` windows: a vector whose exact input and recipe were
        embedded before (by any generation) is reused from the ledger; the rest of the window is
        embedded in one budgeted provider request, stored for reuse and upserted. Only the window
        and the sampled vectors are held. A provider or budget failure stops the stage (policy)."""
        if "vector" not in self.slots:
            return None
        role = self.cfg.models.roles.embedder
        recipe_sha = hashlib.sha256(json.dumps(self.embedding_recipe(), sort_keys=True).encode()).hexdigest()[:32]
        titles = {d.id: d.title for d in corpus}
        vector = self._facet("vector")
        result = Embedded()
        imported: PrecomputedVectors | None = None
        if role.precomputed is not None:
            try:
                imported = PrecomputedVectors(role.precomputed, role.dimensions)
                check = role.precomputed.verify
                if check.sample:
                    reservations = meter.reserve_embedding(sum(len(c.text) for c in chunks[: check.sample]), role.price_key())
                    try:
                        imported.audit.verify = spot_check(imported, chunks, self.embedder.encode, check.sample, check.min_cosine, check.seed)
                    finally:
                        meter.commit_embedding(reservations, getattr(getattr(self.embedder, "provider", None), "last_usage", None))
            except PrecomputedVectorError as exc:
                raise ProjectionError("vector", exc.unit, str(exc)) from exc
        sample_ids = {c.id for c in chunks[:SAMPLE]}
        price_key = role.price_key()
        window: list[Chunk] = []
        try:
            for start in range(0, len(chunks), role.batch_size):
                window = chunks[start : start + role.batch_size]
                texts = [self.embedding_input(titles[c.document_id], c) for c in window]
                keys = [self.embedding_key(recipe_sha, t) for t in texts]
                if imported is not None:
                    # Imported rows are matched by (document, field, exact text digest), never by position.
                    rows = [imported.lookup(c.document_id, c.field, c.text) for c in window]
                    absent = [c for c, row in zip(window, rows) if row is None]
                    if absent and role.precomputed.on_missing == "error":
                        first = absent[0]
                        raise ProjectionError("vector", first.id, f"no precomputed vector for document {first.document_id!r} field {first.field!r} ({len(absent)} in this batch)")
                    imported.audit.matched += len(window) - len(absent)
                    imported.audit.embedded += len(absent)
                else:
                    rows = [self._cached_vector(ledger, k, role.dimensions) for k in keys]
                missing = [i for i, row in enumerate(rows) if row is None]
                if missing:
                    reservations = meter.reserve_embedding(sum(len(texts[i]) for i in missing), price_key)
                    try:
                        fresh = self.embedder.encode([texts[i] for i in missing])
                    finally:
                        meter.commit_embedding(reservations, getattr(getattr(self.embedder, "provider", None), "last_usage", None))
                    fresh = np.asarray(fresh, dtype=np.float32)
                    if fresh.shape != (len(missing), role.dimensions):
                        raise ValueError(f"embedder returned shape {fresh.shape}, expected ({len(missing)}, {role.dimensions})")
                    for i, row in zip(missing, fresh):
                        rows[i] = row
                        ledger.put_raw(keys[i], base64.b64encode(row.astype("<f4").tobytes()).decode("ascii"), True)
                matrix = np.vstack(rows).astype(np.float32)
                vector.upsert(gen, [c.id for c in window], matrix)
                result.embedded += len(missing)
                result.reused += len(window) - len(missing) if imported is None else 0
                result.imported += len(window) - len(missing) if imported is not None else 0
                result.ids.update(c.id for c in window)
                for chunk, row in zip(window, matrix):
                    if chunk.id in sample_ids:
                        result.sample[chunk.id] = row
        except (ConfigError, ProjectionError, BackendError):
            raise
        except BudgetExhausted as exc:
            policy.stage_failed("embeddings", "budget", str(exc), len(result.ids), len(chunks) - len(result.ids), window[0].id if window else None)
        except Exception as exc:  # provider outage, shape error: the stage stops here
            policy.stage_failed("embeddings", "provider", str(exc), len(result.ids), len(chunks) - len(result.ids), window[0].id if window else None)
        else:
            policy.stage_complete("embeddings", len(chunks))
        vector.refresh(gen)
        if imported is not None:
            result.precomputed = imported.finish().manifest()
        return result

    @staticmethod
    def _cached_vector(ledger: Any, key: str, dimensions: int) -> np.ndarray | None:
        cached = ledger.get_raw(key)
        if cached is None or not cached.valid:
            return None
        try:
            row = np.frombuffer(base64.b64decode(cached.raw_reply, validate=True), dtype="<f4")
        except ValueError as exc:  # never silently re-embedded: the ledger entry is damaged
            raise ProjectionError("vector", key, f"stored embedding is unreadable: {exc}") from exc
        if row.shape != (dimensions,) or not np.isfinite(row).all():
            raise ProjectionError("vector", key, f"stored embedding has {row.shape[0]} values, the recipe needs {dimensions}")
        return row.astype(np.float32)

    def _reports(self, communities, chunks: list[Chunk], policy: ExtractionPolicy, budget: Any):
        role = self.cfg.models.roles.community
        if self.community_model is None or role is None:
            for community in communities:
                community.report = disabled_report()
            return communities
        evidence_chars = role.evidence_budget.chars if role.evidence_budget else 30000
        done = []
        for index, community in enumerate(communities):
            try:
                community.report = community_report(community, chunks, self.community_model, self.cfg.communities.prompt, evidence_chars, budget)
            except ReportFailure as exc:
                if exc.category in {"provider", "budget"}:
                    policy.stage_failed("community_reports", exc.category, str(exc), index, len(communities) - index, community.id)
                    for rest in communities[index:]:
                        rest.report = {"status": "pending", "claims": [], "reason": "stage incomplete"}
                    return [*done, *communities[index:]]
                policy.reject_report(community.id, exc.category, str(exc))
                community.report = {"status": "rejected", "claims": [], "reason": exc.category}
            done.append(community)
        policy.stage_complete("community_reports", len(communities))
        return done

    def _validate_projections(self, gen: GenerationId, corpus: list[Document], chunks: list[Chunk], embedded: "Embedded | None") -> dict[str, Any]:
        """Counts against the catalog and a sample of ids round-tripping through every target: the
        same sampled chunk ids must come back from the catalog, every scope mirror, the lexical and
        vector projections (with the catalog's document id), and their documents from every records
        target."""
        results: dict[str, Any] = {}
        catalog = self._facet("catalog")
        counts = extras.require(self.backends[self.slots["catalog"]], extras.CatalogCounts, "catalog", "validate")
        _expect("catalog", counts.count_documents(gen), len(corpus), "documents")
        _expect("catalog", counts.count_chunks(gen), len(chunks), "chunks")
        sample = chunks[:SAMPLE]
        fetched = {c.id: c for c in catalog.get_chunks(gen, [c.id for c in sample])}
        for chunk in sample:
            if fetched.get(chunk.id) != chunk:
                raise ProjectionError("catalog", chunk.id, "sampled chunk did not round-trip")
        results["catalog"] = {"backend": self.slots["catalog"], "documents": len(corpus), "chunks": len(chunks), "verified": True}
        for holder in self.generations.mirrors():
            projection = f"scope_mirror:{holder.name}"
            mirror = extras.require(holder.backend, extras.CatalogCounts, "catalog", "validate")
            _expect(projection, mirror.count_documents(gen), len(corpus), "documents")
            _expect(projection, mirror.count_chunks(gen), len(chunks), "chunks")
            mirrored = {c.id: c for c in holder.backend.catalog.get_chunks(gen, [c.id for c in sample])}
            for chunk in sample:
                if mirrored.get(chunk.id) != chunk:
                    raise ProjectionError(projection, chunk.id, "sampled chunk did not round-trip through the scope mirror")
            results[projection] = {"backend": holder.name, "documents": len(corpus), "chunks": len(chunks), "verified": True}
        sampled_documents = list(dict.fromkeys([*(d.id for d in corpus[:SAMPLE]), *(c.document_id for c in sample)]))
        for holder in self.generations.targets("records"):
            projection = _projection_key("records", holder.name, self.generations.placement)
            records = extras.require(holder.backend, extras.Countable, "records", "validate")
            _expect(projection, records.count(gen), len(corpus), "records")
            for document_id in sampled_documents:
                if not records.exists(gen, "id", document_id, []):
                    raise ProjectionError(projection, document_id, "sampled record is not queryable")
            results[projection] = {"backend": holder.name, "rows": len(corpus), "verified": True}
        if "lexical" in self.slots:
            holder = self.generations.targets("lexical")[0]
            lexical = extras.require(holder.backend, extras.Countable, "lexical", "validate")
            _expect("lexical", lexical.count(gen), len(chunks), "indexed chunks")
            for chunk in sample:
                # Words the lexical analyzer keeps (an ASCII-only tokenizer drops non-ASCII words).
                analyze = getattr(lexical, "analyze", None)
                words = [w for w in chunk.text.split() if w.isalnum() and (analyze is None or analyze(w))][:3]
                if not words:
                    continue
                hits = lexical.search(gen, LexicalQuery(text=" ".join(words)), Scope(document_ids=[chunk.document_id]), 50)
                found = {h.chunk_id: h.document_id for h in hits.items}
                if chunk.id not in found:
                    raise ProjectionError("lexical", chunk.id, "sampled chunk is not visible after refresh")
                if found[chunk.id] != chunk.document_id:
                    raise ProjectionError("lexical", chunk.id, "sampled chunk carries another document id than the catalog")
            results["lexical"] = {"backend": holder.name, "rows": len(chunks), "verified": True, "analyzer_id": self.generations.analyzer_id(holder, gen)}
        if "vector" in self.slots:
            vector = self._facet("vector")
            expected = len(embedded.ids) if embedded is not None else 0
            _expect("vector", vector.count(gen), expected, "vectors")
            complete = expected == len(chunks)
            for chunk in sample:
                if embedded is None or chunk.id not in embedded.sample:
                    continue
                hits = vector.search(gen, np.asarray(embedded.sample[chunk.id]), Scope(document_ids=[chunk.document_id]), 1)
                if not hits or hits[0].chunk_id != chunk.id or hits[0].document_id != chunk.document_id:
                    raise ProjectionError("vector", chunk.id, "sampled vector did not round-trip")
            results["vector"] = {"backend": self.slots["vector"], "rows": expected, "verified": complete, "incomplete": not complete}
        return results

    def _validate_graph(self, gen: GenerationId, corpus: list[Document], mentions: list[Entity], communities: list[Any], memberships: list[Membership]) -> dict[str, Any]:
        """Graph/community consistency on every target: memberships equal what was written and equal
        the stored communities' member lists; every community member is a catalogued document;
        sampled entities are found again inside their document's scope."""
        results: dict[str, Any] = {}
        corpus_ids = {d.id for d in corpus}
        expected_members = {(m.community_id, m.member_id) for m in memberships}
        expected_communities = {c.id: c for c in communities}
        for community in communities:
            outside = sorted(set(community.document_ids) - corpus_ids)
            if outside:
                raise ProjectionError("communities", community.id, f"members {outside[:3]} are not catalogued documents")
        first_mentions: dict[str, Entity] = {}
        for mention in mentions:
            first_mentions.setdefault(mention.id, mention)
        sample = list(first_mentions.values())[:SAMPLE]
        for holder in self.generations.targets("graph"):
            projection = _projection_key("graph", holder.name, self.generations.placement)
            graph = extras.require(holder.backend, extras.GraphExtras, "graph", "validate")
            stored = {(m.community_id, m.member_id) for m in graph.memberships(gen)}
            if stored != expected_members:
                raise ProjectionError(projection, None, f"memberships: projection holds {len(stored)}, build wrote {len(expected_members)}")
            for mention in sample:
                found = graph.find_entities(gen, tokens(mention.name), Scope(document_ids=[mention.span.document_id]))
                if mention.id not in {e.id for e in found}:
                    raise ProjectionError(projection, mention.id, "sampled entity is not found inside its document's scope")
            results[projection] = {"backend": holder.name, "entities": len(first_mentions), "memberships": len(expected_members), "verified": True}
        for holder in self.generations.targets("communities"):
            projection = _projection_key("communities", holder.name, self.generations.placement)
            graph = extras.require(holder.backend, extras.GraphExtras, "graph", "validate")
            stored_communities = {c.id: c for c in graph.communities(gen)}
            if set(stored_communities) != set(expected_communities):
                raise ProjectionError(projection, None, f"communities: projection holds {len(stored_communities)}, build wrote {len(expected_communities)}")
            members = {(c.id, doc_id) for c in stored_communities.values() for doc_id in c.document_ids}
            if members != expected_members:
                raise ProjectionError(projection, None, "community member lists disagree with the memberships written to the graph")
            results[projection] = {"backend": holder.name, "communities": len(stored_communities), "verified": True}
        return results


class Embedded:
    """What the embedding stage wrote: chunk ids upserted, fresh vs reused counts, sampled vectors."""

    def __init__(self) -> None:
        self.ids: set[str] = set()
        self.embedded = 0
        self.reused = 0
        self.imported = 0
        self.precomputed: dict[str, Any] | None = None
        self.sample: dict[str, np.ndarray] = {}


class ProjectionError(RuntimeError):
    def __init__(self, projection: str, unit: str | None, message: str):
        self.projection, self.unit = projection, unit
        super().__init__(f"{projection}: {message}")


class ActivationError(RuntimeError):
    """Activation failed at ``backend``; ``compensation`` lists every pointer put back (or not)."""

    def __init__(self, backend: str, cause: BaseException, compensation: list[dict[str, Any]]):
        self.backend, self.cause, self.compensation = backend, cause, compensation
        failed = [c["backend"] for c in compensation if not c["ok"]]
        suffix = f"; compensation FAILED on {failed}" if failed else (f"; {len(compensation)} pointer(s) restored" if compensation else "")
        super().__init__(f"activation failed on {backend!r}: {type(cause).__name__}: {cause}{suffix}; CURRENT unchanged")


def _backend_recipe(spec: Any) -> dict[str, Any]:
    """A backend's part of the index recipe; a backend config may narrow its dump (``recipe()``)."""
    recipe = getattr(spec, "recipe", None)
    return recipe() if callable(recipe) else spec.model_dump(mode="json")


def _delete_on(backend: Any, gen: GenerationId) -> None:
    """Adapter-level ``delete_generation`` when present (drops every projection of the generation),
    else the catalog facet's (memory fake)."""
    delete = getattr(backend, "delete_generation", None)
    if not callable(delete):
        delete = backend.catalog.delete_generation
    delete(gen)


def _conflict(exc: BaseException) -> WriteConflict | None:
    """The write conflict behind a failure (directly, or the cause of a failed activation step)."""
    if isinstance(exc, WriteConflict):
        return exc
    if isinstance(exc, ActivationError) and isinstance(exc.cause, WriteConflict):
        return exc.cause
    return None


def _expect(projection: str, actual: int, expected: int, what: str) -> None:
    if actual != expected:
        raise ProjectionError(projection, None, f"{what}: projection holds {actual}, catalog expects {expected}")


def _projection_key(projection: str, backend: str, placement: dict[str, list[str]]) -> str:
    """``projection`` for its first configured backend (``storage.*`` or the arm), else ``projection:backend``."""
    return projection if placement[projection][0] == backend else f"{projection}:{backend}"
