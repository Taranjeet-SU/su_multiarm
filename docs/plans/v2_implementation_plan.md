# V2 implementation plan — modular, pluggable, harness-like retrieval package

Status: **revision 3 — Phase 0 backend contracts frozen as implemented in S2** (2026-11-03). Nothing in this
document is implemented unless a "current state" note says so. The package name is a placeholder;
`<pkg>` below means the import root (today `su_multi_arm`) until the owner chooses the public name.

## Changelog

| Date | Rev | Change |
|---|---|---|
| 2026-11-03 | 1 | Initial draft from owner requirements, four historical inventories, current source at `36d2394`, shared notes §4–14 and §22–25. |
| 2026-11-03 | 2 | Round-1 review (`docs/plans/reviews/v2_plan_review_r1.md`, verdict REVISE). All eight blocking findings resolved; 18 non-blocking findings addressed (none rejected). Phase 0 and Phase 1 re-scoped and re-estimated (contracts frozen in Phase 0; five arms, ingestion write path, review stage, answer contract, metadata schema, `doctor --probe`/CLI v2 added as rows); lexical acceptance replaced by per-engine conformance plus an agreement report; decision fail-policy table with defaults; `MetadataSchema` contract; grounding offset map, endpoint containment and `partial` policy defined; safe classifier-artifact contract; `ReviewStage` component; reproducible grounding acceptance; private-evaluation numbers removed; storage selectors, action/audit types, deferred-items list added. Totals re-computed (§4.1). |
| 2026-11-03 | 3 | Phase 0 S2 kernel landed (`e4f2ff1`) and reviewed (`docs/plans/reviews/p0_s2_kernel_review.md`, REVISE → fixed in `v2/p0-s2-fix`). Frozen-contract text amended to match the code: §3.1a adds `MetadataSchema.unknown_fields`; §3.5 adds `Catalog.get_chunks`, `LedgerStore.get_raw`, `LexicalIndex.search -> PageResult[ScoredChunk]`, `VectorIndex` namespaces, the `Scope` (eligibility pushdown) vs `ScopeSpec` (typed query) split, per-capability adapter facets and per-`(adapter, capability)` status in the matrix; §5 narrows the N6 round-trip guarantee to the v1-effective subset and requires lossy upgrades to be reported. No other section changed. |

## Review response (round 1)

| Id | Disposition | What changed (section) |
|---|---|---|
| B1 | Accepted | Backend contracts + capability matrix moved to Phase 0 (frozen before the sqlite adapter); conformance kit stays in Phase 1. New Phase 0 rows: five arms on `Component`/`ArmResult` (incl. a minimal `graph` arm with `TraversalSpec`), ingestion write path through the contracts on sqlite with per-projection validation. New Phase 1 rows: multi-backend projection writes + validation before activation, `doctor --probe` + CLI v2, storage selectors. Phase 3 traversal row reduced to extensions. Totals and calendar re-computed (§4, §4.1). |
| B2 | Accepted | Phase 1 acceptance now requires membership identity only for `dense` and `structured`; `lexical` has a per-engine conformance contract plus a cross-engine agreement report recorded in `VALIDATION.md` (§4 Phase 1 acceptance, §3.5). |
| B3 | Accepted | Default `fail_closed` stated and made the schema default; per-agent/per-stage policy table with failure conditions, permitted outcomes and `Decision` contents (§3.7.1); deterministic intent pre-pass always runs; `raw_all_arms` carries `scope=None` plus an `UnsupportedConstraint` per detected aggregate intent; new Phase 0 component `generation/contract.py::AnswerContract` injects `unsupported_constraints` into the answer object and permits count/exhaustive-list/absence claims only against a `ScopeResult` with `exactness: exact` (§3.9a). Phase 2 `exact_set` becomes the hook form of the same rule. |
| B4 | Accepted | `MetadataSchema`/`FieldSpec` contract added (§3.1a) with type, nullability, enum values, time basis and timezone policy, `filterable/listable/groupable/sortable`, `max_catalog_cardinality`; `ValueResolver` resolution defined per predicate type incl. range/date and high-cardinality lookup via `RecordStore.exists` (§3.7.2). Consumed by config validation, `RecordStore.upsert`, `ScopeSpec` compilation, `/v2/values` and `list_values`. |
| B5 | Accepted | Invariants added: relation span contains both endpoint spans (offset containment under the same normalisation), endpoints resolve to typed entities from the same chunk; `locate()` index-map contract for length-changing normalisation with the listed unit cases; `strict`/`quarantine`/`partial` policies defined precisely (§3.12, §3.14.1). |
| B6 | Accepted | Classifier artifacts: registered entry-point plugins listed under `plugins:` or an ONNX file with recorded sha256 and a declared feature/label manifest; pickle and dotted-import paths are validation errors; Python callables only via `Engine(..., components=[...])`; unit test named (§3.7.3). |
| B7 | Accepted | `pipeline/review.py::ReviewStage` defined (kind `reviewer`, post-retrieval, hop budget, stop reasons, re-planning through the decision agent with immutable scope, rounds traced); Phase 0 row and tests; migration row corrected to `decision {kind: llm}` + `review {enabled: true, max_hops}` (§3.9b, §5). |
| B8 | Accepted | Phase 0 grounding acceptance replaced by (a) a CI-runnable synthetic fixture pack per rejection category and (b) an owner-authorised, bounded local-model re-run over the unchanged frozen cohorts with raw replies (valid and invalid) cached this time, recorded as external evidence. Raw-reply caching added to the ledger contract (§3.14). |
| N1 | Accepted | `ActionRequest`/`ActionResult`/`AuditRecord` with idempotency key, invocation surfaces listed, `Memory.semantic` deferred (config key rejected until implemented), learning `apply` explicitly not an `Action` (§3.4). |
| N2 | Accepted | `storage:` selectors with exactly-one-authoritative-backend validation (§3.3). |
| N3 | Accepted | Semantic cache tier uses a `VectorIndex` namespace under the cache key prefix; `CacheStore` stays key/value (§3.11). |
| N4 | Accepted | Guard failure semantics: raising safety/exactness guards block; others `noop` only where configured; unit test (§3.10). |
| N5 | Accepted | Capabilities declared per `(provider, model)` in the role config and probed by `doctor --probe`; OpenAI caching reported from usage, not controlled; capped manual real-provider smoke added to open decisions (§3.6, §9). |
| N6 | Accepted | v1 `Config`/`index_fingerprint` retained in `compat/`, round-trip test, `RecordStore` view over v1 document JSON for legacy-read (§5). |
| N7 | Accepted | Invariant added to §2.1 and a conformance/trace check (§3.5). |
| N8 | Accepted | ACL scope and `listable` flag applied to `/v2/values` and `list_values` (§3.15). |
| N9 | Accepted | "Deferred or re-homed items" list added (§4.2); pagination/sort/facets, request-time redaction, claim-status guard, QASPER and neutral-voice option folded into phases. |
| N10 | Accepted | Private-evaluation numbers removed from §3.11 and §6.4; findings keep direction only; docs lint rule added (§6.2). |
| N11 | Accepted | Phase 1 re-estimated to 102 ed (providers 11, serving 9, buffer 8, plus the B1 rows); Phase 0 to 61 ed; streaming deferral stays an open decision. |
| N12 | Accepted | Replacement tests keep the support edge cases under the `support_example` profile (Phase 0 acceptance). |
| N13 | Accepted | Open decision on `2.0.0a1` scope vs the owner's "first release" (§9). |
| N14 | Accepted | `entailment_checked` marked reserved, no producer before Phase 5 (§3.1). |
| N15 | Accepted | Generation switch mid-session defined (§3.4). |
| N16 | Accepted | `UnsupportedConstraint` text and suggestion included in masking (§3.4). |
| N17 | Accepted | Six tiers mapped to the owner's four (§6.1). |
| N18 | Accepted | `refresh(gen)` and generation-scoped index naming/aliases added to the Lucene rows (§3.5). |
| Coverage note (lemmatiser) | Accepted | Predicate trigger matching uses a declared pure-Python suffix stemmer; spaCy lemmatiser optional (§3.12). |

---

## 1. Executive summary

The alpha at `36d2394` (~2,100 lines, `src/su_multi_arm/*.py` plus `adapters/`) already has the
right spine: canonical records with field offsets, immutable generations with an atomic `CURRENT`
switch and recipe fingerprint (`store.py`), strict grounding (`extraction.py`), a closed typed config
(`config.py`), and one engine behind Python/CLI/HTTP/MCP. The 2026-10-12 public-data diagnostics
(`docs/VALIDATION.md`, notes §22–25) showed that the spine holds but four things break on real
corpora: exact-substring quote grounding aborts whole builds on case/whitespace drift; relation
predicates are never checked (a false `hosted` edge passed every existing check); the planner
fabricates counts and guesses filter values; and generator JSON failure rose steeply with prompt size
on a local model (an association, not a controlled result).

V2 keeps the spine and replaces the monolith around it with a **component harness**: every retrieval
arm, extractor, decision agent, review stage, guard, tool and action is a `Component` with one
lifecycle (configure → validate → run → trace), registered in explicit registries, selected by
declarative config, and executed under a `RunContext` that carries the **session, memory, budget,
trace, principal and pinned generation** as first-class objects. Storage is split by **capability**
(catalog, lexical, vector, records, graph, cache, ledger, session) rather than by product, so the six
first-release backends (OpenSearch, Elasticsearch, generic SQL via SQLAlchemy, PostgreSQL with
pgvector+FTS, Neo4j, MongoDB) plug into the same frozen contracts and the same conformance test kit;
the config validator refuses an arm whose backend does not declare the needed capability instead of
silently degrading.

Delivery is five phases. **Phase 0** (kernel, 61 engineer-days) freezes the contracts, ports the five
existing arms and the ingestion write path onto them, fixes the measured failures (span-anchored
grounding with offset map, predicate support, typed metadata schema and query scope, answer contract
that cannot emit unsupported counts) and removes domain coupling. **Phase 1** (102 ed) is the first
shippable release `2.0.0a1` with all six backends, three provider families with native structured
output and prompt caching, the declarative pipeline, sessions/tools/actions, the configurable decision
agent with stated fail policies, and HTTP (SSE) + MCP (stdio and streamable HTTP) parity. **Phase 2**
(61 ed) adds the cache arm, guard hooks, ACL hooks, incremental/resumable ingestion with ledgers and
dollar caps, release manifests/rollback, pagination/facets and trace/cost export. **Phase 3** (56 ed)
adds the ontology contract, entity resolution/glossary, event/record extractors, hierarchical
communities with deterministic stats, traversal extensions and the selection/phrase-boost policies.
**Phase 4** (48 ed) delivers the evaluation harness (paired tests, judges, replay, sealed holdouts),
the learning module and public benchmark runs. Total 328 engineer-days; with three engineers the
critical path (contracts → arms/ingestion on contracts → six adapters → pipeline validation → serving
parity) puts `2.0.0a1` at roughly 13 calendar weeks and `2.0.0` after Phase 4 at roughly 7 months.

Measured-negative historical items (trained routers, DSPy, synthetic Q&A cache, generic
cross-encoder rerank, KG entity boost, strict source hierarchy, RRF/min-max/z-score as default) are
**not** ported as defaults. Where the owner requires the capability (classifier decision agent), V2
ships the interface, a safe artifact contract and a fail policy, no weights, default off, and the
evaluation harness carries the historical finding as a regression scenario.

---

## 2. Target architecture

### 2.1 Principles carried forward (unchanged from the alpha) plus one added

1. Immutable generations; readers pin one generation for a whole request (including multi-hop and
   cached answers); atomic `CURRENT` switch; failed builds leave the old generation active.
2. Strict grounding is the default and fails closed. Recovery policies (`quarantine`, `partial`) are
   explicit (§3.14.1), recorded in the manifest, and never make a failed build look complete.
3. No model-generated SQL/Cypher on the request path by default. Structured operations are typed and
   validated before execution; generated queries exist only behind an opt-in guard (Phase 5).
4. No silent fallback: an unavailable backend, provider or capability is a validation error or a
   typed failure in the trace, never an empty result.
5. Evidence levels are kept separate: implemented / contract-mocked / real-service / measured.
6. Public-repo hygiene: vendor-neutral core, domain behaviour in optional profiles, synthetic fixtures
   only.
7. **No full-corpus loads on the request path** (new; today `retrieval.py::Retriever.__init__` loads
   every document and chunk and `allowed()` scans all documents). Eligibility is pushed down to the
   backend; catalog reads per request are bounded by the number of hits, not corpus size (§3.5).

### 2.2 Layer diagram

```mermaid
flowchart TB
    subgraph Serving["serving/ (HTTP+SSE, MCP stdio+streamable HTTP, CLI)"]
        HTTP[http.py] --- MCP[mcp.py] --- CLI[cli.py]
    end
    subgraph Runtime["runtime/ (harness)"]
        RC[RunContext: principal, session, memory, budget, trace, generation]
        SESS[Session store] --- MEM[Memory: working / episodic]
        TOOLS[Tool registry] --- ACT[Action registry + audit]
        BUD[Budget: calls, tokens, USD, deadline] --- TR[Trace + cost ledger export]
    end
    subgraph Pipeline["pipeline/ (declarative DAG)"]
        INT[decision/intent.py deterministic pre-pass]
        DEC[decision/: rules | classifier | llm | cascade + ScopeSpec compiler + ValueResolver]
        ARMS[arms/: exact_id, dense, lexical, structured, graph, community, cache]
        SEL[selection/: collapse, limits, union, ordering, diversity, phrase boost, evidence packing]
        REV[pipeline/review.py ReviewStage: bounded follow-up]
        GEN[generation/: structured output, AnswerContract, citation + quote verification]
        GUARD[guards/: pre-request, post-retrieval, post-generation, pre-response hooks]
    end
    subgraph Knowledge["extraction/ + communities/ + ingestion/"]
        EXT[extractors: gazetteer, regex, spacy, gliner, llm, events, records, tiered]
        GRD[grounding: span-anchored locate with offset map, fail-closed]
        PRED[predicate support + ontology contract + resolution/glossary]
        COMM[communities: entity, louvain, leiden-hierarchical + stats + grounded reports]
        ING[ingestion jobs: ledger with raw replies, budgets, delta, strict/quarantine/partial, release manifests]
    end
    subgraph Backends["backends/ (capability contracts, frozen in Phase 0)"]
        CAT[Catalog] --- LEX[LexicalIndex] --- VEC[VectorIndex] --- REC[RecordStore]
        GRA[GraphStore] --- CACHE[CacheStore] --- LED[LedgerStore] --- SST[SessionStore]
        AD1[sqlite] --- AD2[postgres] --- AD3[sqlalchemy] --- AD4[opensearch] --- AD5[elasticsearch] --- AD6[neo4j] --- AD7[mongodb]
    end
    subgraph Providers["providers/"]
        P1[openai] --- P2[anthropic] --- P3[openai_compatible / ollama] --- P4[sentence_transformers]
        CAPS[capability registry per provider+model: structured_output, prompt_caching, streaming, batch, pricing]
    end
    subgraph Learning["evaluation/ + learning/"]
        EVAL[harness: tiers, replay, paired tests, judges, sealed holdouts, regression pack]
        LEARN[learning: failure batch -> proposals (reviewable artifacts, never auto-applied)]
    end
    Serving --> Runtime --> Pipeline
    Pipeline --> Backends
    Pipeline --> Providers
    Knowledge --> Backends
    Knowledge --> Providers
    EVAL --> Pipeline
    LEARN --> EVAL
```

### 2.3 Package layout (target)

```
src/<pkg>/
  core/        types.py config.py registry.py component.py errors.py schema.py
  compat/      config_v1.py snapshot_v1.py
  runtime/     context.py session.py memory.py tools.py actions.py budget.py trace.py acl.py
  pipeline/    spec.py engine.py validate.py review.py
  decision/    base.py intent.py rules.py classifier.py llm.py cascade.py scope.py values.py
  arms/        base.py exact_id.py dense.py lexical.py structured.py graph.py community.py cache.py
  selection/   collapse.py limits.py union.py ordering.py diversity.py phrase.py packing.py
  generation/  generator.py contract.py citations.py quotes.py
  guards/      base.py safety.py prompt_leak.py exact_set.py claim_status.py directives.py redaction.py request_redaction.py
  extraction/  base.py grounding.py predicate_support.py ontology.py resolution.py glossary.py dedupe.py
               extractors/ gazetteer.py regex.py spacy.py gliner.py llm.py events.py records.py tiered.py
  communities/ base.py entity.py louvain.py leiden.py stats.py reports.py
  ingestion/   job.py ledger.py delta.py policy.py normalize.py release.py write.py
  backends/    contracts.py capabilities.py sqlite/ postgres/ sqlalchemy/ opensearch/ elasticsearch/ neo4j/ mongodb/
  providers/   base.py capabilities.py openai.py anthropic.py openai_compatible.py ollama.py sentence_transformers.py pricing.py
  serving/     http.py sse.py mcp.py auth.py parity.py
  evaluation/  harness.py manifests.py replay.py paired.py judges.py slices.py regression/
  learning/    batch.py analyzers/ proposals.py validate.py
  profiles/    neutral.yaml support_example.yaml research_example.yaml collections_example.yaml
  testing/     backend_conformance.py provider_fakes.py fixtures/ (incl. fixtures/grounding/)
tests/         unit/ contract/ conformance/ services/ parity/
benchmarks/    protocol.md runners/ (code only; data, qrels and results live in the external workspace)
```

---

## 3. Module contracts

Format: purpose · public interface · inputs/outputs · invariants · failure modes · not its job.
Types are Pydantic v2 `StrictModel`s (`extra="forbid"`) as today (`models.py::StrictModel`).

### 3.1 `core/types.py` — canonical data

```python
class Span(StrictModel):            # exact source anchor; the only legal quote carrier
    document_id: str; chunk_id: str; field: str; start: int; end: int; text: str   # text == source_field[start:end] always
    match_level: Literal["exact", "normalised", "relocated"]                        # how it was located
class Document(...)   # as today + optional `kind` (neutral default "document"), metadata validated against MetadataSchema
class Chunk(...)      # as today; `recipe` id added (embedding vs extraction chunk policies may differ)
class Entity(StrictModel):  id: str; name: str; type: str; span: Span; canonical_id: str | None; aliases: list[str]
class Relation(StrictModel):
    subject_id: str; predicate: str; object_id: str; span: Span
    edge_kind: Literal["fact", "co_occurrence", "mention", "native_link", "derived"]
    support: Literal["trigger_in_span", "entailment_checked", "unchecked"]   # entailment_checked is RESERVED: no producer before Phase 5
    status: str | None            # ontology-defined (e.g. proposed/verified) when a contract is loaded
    provenance: Provenance        # extractor id, model id, prompt sha, recipe sha, extracted_at, confidence
class Event(StrictModel):   type: str; participants: list[tuple[str, str]]; time: str | None; span: Span; provenance: Provenance
class StructuredRecord(StrictModel): schema_id: str; values: dict[str, JsonScalar]; spans: dict[str, Span]; provenance: Provenance
class Membership(StrictModel): community_id: str; member_id: str; role: str | None; basis: Literal["entity", "similarity", "declared"]
class ExtractionOutput(StrictModel): entities, relations, events, records, memberships   # all lists, all span-anchored
class Evidence(StrictModel):  id: str; kind: Literal["document", "aggregate", "graph_path", "community_report"]; ...
class GenerationId(StrictModel): dataset: str; generation: str; index_format: int; fingerprint: str
```

Invariants: every quote is a `Span` whose `text` equals the stored source substring; no model text is
stored as a quote. A `fact` or `co_occurrence` relation's span **contains both endpoint entity spans**
(offset containment, same chunk) and both endpoints resolve to typed entities from the same chunk's
extraction — this keeps today's `extraction.py` endpoint-in-quote check. `Relation.edge_kind ==
"fact"` requires `support != "unchecked"`. Cached answers are never `Evidence`. Failure modes:
validation errors at construction. Not its job: normalisation logic (`extraction/grounding.py`).

### 3.1a `core/types.py::MetadataSchema` — typed corpus metadata (new)

```python
class FieldSpec(StrictModel):
    name: str
    type: Literal["string", "integer", "number", "boolean", "datetime", "date", "enum", "id"]
    nullable: bool = True
    enum_values: list[str] | None = None          # required when type == "enum"; closed set
    filterable: bool = True                       # may appear in ScopeSpec.predicates
    listable: bool = False                        # may be enumerated via /v2/values and list_values (ACL-scoped)
    groupable: bool = False; sortable: bool = False
    max_catalog_cardinality: int = 200            # observed values offered to an LLM planner only when count <= this
    time_basis: str | None = None                 # datetime/date only: label such as "created" / "resolved"
    timezone_policy: Literal["require_offset", "assume_utc"] = "require_offset"
    casefold_match: bool = False                  # eq/in matching for string/id fields
class MetadataSchema(StrictModel):
    fields: list[FieldSpec]
    canonical_unit: str = "document"              # population counted by default
    equivalence_field: str | None = None          # optional cross-record identity for count_distinct
    unknown_fields: Literal["allow", "reject"] = "allow"   # rev 3: undeclared metadata keys stay untyped and
                                                           # never filterable/listable; "reject" makes them an
                                                           # ingestion violation (category unknown_field)
```

Consumed by: config validation (planner-filterable fields = `filterable` fields), `Document.metadata`
validation at ingestion (type/nullability/enum; violations are ingestion failures by category),
`RecordStore.upsert(records, schema)` (typed columns/indexes), `ScopeSpec` compilation, `ValueResolver`
(§3.7.2), `/v2/values/{field}` and `list_values` (`listable` + ACL), `communities/stats.py`
(time trend uses a declared `time_basis`). Replaces today's bare `arms.sql_fields` allowlist and the
untyped `Document.metadata` scalars. A profile may ship a schema; `neutral` ships none and therefore
exposes no filterable fields until the operator declares them.

### 3.2 `core/component.py` and `core/registry.py` — lifecycle and registries

```python
class Component(Protocol):
    name: ClassVar[str]; kind: ClassVar[ComponentKind]      # arm | extractor | decision | reviewer | tool | action | guard | selector | generator | community | provider | backend
    config_model: ClassVar[type[StrictModel]]
    def __init__(self, config, deps: Dependencies): ...
    def validate(self, env: Environment, probe: bool = False) -> list[Diagnostic]: ...   # no network unless probe
    def run(self, ctx: RunContext, inputs): ...
    def recipe_fingerprint(self) -> str | None: ...          # non-None => affects index identity
class Registry(Generic[T]): register(cls) ; get(name) ; names()
```

Registries are populated by explicit imports in `<pkg>/__init__.py` and, for third parties, by the
entry-point group `<pkg>.components` **only** for names listed under `plugins:` in config. No
import-by-string from YAML, no pickled artifacts (§3.7.3). Python objects can be injected only through
the Python API `Engine(config, components=[...])`. `validate()` returns diagnostics with severity;
any `error` aborts engine construction. `recipe_fingerprint()` replaces today's hand-maintained
`Config.index_fingerprint()` field list.

### 3.3 `core/config.py` — declarative configuration v2

`schema_version: 2`. Top-level keys: `dataset`, `profile`, `plugins`, `backends` (named instances),
`storage` (authoritative backend per non-arm capability), `metadata_schema`, `models.roles` (planner,
reviewer, extractor, generator, community, judge, embedder, reranker, cache_gate), `pipeline`
(decision, arms, selection, review, guards, generation), `extraction`, `communities`, `ingestion`,
`sessions`, `budgets`, `privacy`, `serving`. A profile is a YAML overlay applied before validation;
the effective config and the applied profile name are recorded in every trace and manifest (notes §4
acceptance 3). `Config.model_json_schema()` regenerates `docs/config.schema.json`; CI fails on drift.

Validation invariants: every `pipeline.arms.<arm>.backend` must name a backend whose adapter declares
the capability the arm needs (`backends/capabilities.py`); every `storage.<capability>` names exactly
one backend that declares it (so two Lucene backends cannot both claim `catalog`); every model role
used by a selected component must be configured; `structured_output: native` is rejected for a
`(provider, model)` pair not declared to support it; `pipeline.decision.on_failure` defaults to
`fail_closed` in the generated schema; `sessions.memory.semantic` is rejected until implemented
(§3.4); unknown keys are errors. Failure mode: `ConfigError` with a path to the offending key. Not its
job: connecting to anything (that is `doctor --probe`).

Sketch (abridged):

```yaml
schema_version: 2
dataset: demo
profile: neutral
backends:
  local:   {kind: sqlite, path: ./state}
  search:  {kind: opensearch, hosts_env: OS_HOSTS, index_prefix: pkg_demo, knn: {method: hnsw, exact_rescore: true}}
  graph:   {kind: neo4j, uri: bolt://127.0.0.1:7687, user: neo4j, password_env: NEO4J_PASSWORD}
storage: {catalog: local, records: local, ledger: local, sessions: local, cache: local, communities: local}
metadata_schema:
  fields:
    - {name: kind, type: enum, enum_values: [note, record, report], filterable: true, groupable: true, listable: true}
    - {name: created_at, type: datetime, time_basis: created, timezone_policy: require_offset, sortable: true}
    - {name: project, type: string, filterable: true, listable: true, max_catalog_cardinality: 200}
    - {name: external_id, type: id, filterable: true, listable: false, max_catalog_cardinality: 0}
models:
  roles:
    planner:   {provider: anthropic, model: "<pinned-id>", capabilities: {structured_output: native, prompt_caching: controlled}}
    generator: {provider: openai, model: "<pinned-id>", capabilities: {structured_output: json_schema, prompt_caching: passive}, evidence_budget: {chars: 24000}}
    extractor: {provider: openai_compatible, base_url: http://127.0.0.1:11434/v1, model: "<local-id>", capabilities: {structured_output: json_schema}}
    reviewer:  {provider: anthropic, model: "<pinned-id>"}
    embedder:  {provider: sentence_transformers, model: "<id>", revision: "<sha>", dimensions: 384}
pipeline:
  decision: {kind: cascade, stages: [rules, llm], on_failure: fail_closed}
  arms:
    exact_id:   {alias_map: aliases.yaml}
    dense:      {backend: search, candidates: 200}
    lexical:    {backend: search, candidates: 200, phrase_boost: {enabled: true, window_tokens: 6}}
    structured: {backend: local}
    graph:      {backend: graph, hops: 2, predicates: [resolved_by, affects], direction: any}
    community:  {backend: local}
    cache:      {backend: local, mode: exact, ttl_seconds: 3600}
  selection: {collapse: document, per_arm: 20, union: preserve, ordering: reciprocal_rank, diversity: null, pins: exact_id}
  review:    {enabled: false, max_hops: 2}
  guards: [safety_refusal, prompt_leak, exact_set, output_redaction]
  generation: {contract: {on_violation: abstain_with_reason}, voice: none}
extraction:
  extractors: [gazetteer, regex, gliner, llm]
  ontology: ontology.yaml
  grounding: {normalisation: nfkc_casefold_ws, ambiguous: reject}
  policy: strict
sessions: {store: local, ttl_seconds: 3600, memory: {episodic: true}}
budgets: {max_llm_calls: 12, max_usd: 0.50, deadline_seconds: 60, price_table: prices.yaml}
serving: {http: {bind: 127.0.0.1:8099, auth: bearer_env, page_size_max: 50}, mcp: {transports: [stdio, streamable_http]}}
```

### 3.4 `runtime/` — harness objects

| Object | Interface (abridged) | Invariants |
|---|---|---|
| `RunContext` | `principal, session, memory, budget, trace, generation, deadline, acl` | Immutable for a request; every component receives the same one; generation pinned at request start. |
| `Session` (`session.py`) | `create(principal, dataset) -> Session; get(id); touch(); close()`; keyed `(principal, dataset, session_id)`; records the generation of each turn | A session from another principal is a 403-class error; sessions store IDs and hashes, not raw passages, unless `memory.episodic.store_text: true`. **Generation switch mid-session:** the session continues; episodic IDs are re-validated against the new generation at the next turn (missing IDs are dropped from exclusions/"shown" sets and listed in the trace); the trace records `generation_changed: {from, to}`. |
| `Memory` (`memory.py`) | `working` (request), `episodic` (turns: query hash, shown IDs, answer id, trace id, generation); `semantic` **deferred** — the config key is rejected until a later phase defines storage, writer and retention | **Memory is never evidence.** It may rewrite a follow-up query or add exclusions; both are logged. |
| `Tool` (`tools.py`) | `Component` with `input_model/output_model`; `invoke(ctx, args) -> ToolResult{output, evidence_ids, trace_span}`; arms, `aggregate`, `lookup_record`, `list_values(field)`, `resolve_alias` are tools | Tools are read-only. Deterministic given generation + inputs. Exposed to decision agents and MCP through the same registry. |
| `Action` (`actions.py`) | `ActionRequest{action, args, idempotency_key, principal, reason}` → `ActionResult{status: applied|skipped_duplicate|denied|failed, outputs, audit_id}`; `AuditRecord{id, action, principal, idempotency_key, args_hash, started, finished, status, generation_before, generation_after}` written **before** execution and finalised after; built-ins `ingest_delta`, `invalidate_cache`, `promote_generation`, `rollback_generation` | Invocation surfaces: CLI and Python API always; HTTP/MCP only when `serving.actions.allow: [names]` and the principal has the `actions` scope — default denied. Duplicate `idempotency_key` within the audit retention window → `skipped_duplicate`. **Learning `apply` is not an `Action`** and has no serving surface. |
| `Budget` (`budget.py`) | `reserve(kind, estimate)`, `commit(actual)`; kinds: calls, input/output tokens, USD, wall-clock | Reservation before every provider call; USD requires a price table; exhaustion raises `BudgetExhausted`, recorded in trace. |
| `Trace` (`trace.py`) | nested spans; `export(path, masking=privacy)` JSONL; optional OpenTelemetry exporter | Default export carries field names, counts, IDs and hashes, not raw query/passages; the masking rule also covers `UnsupportedConstraint.constraint_text` and `.suggestion` (query fragments) and `ReviewStage` follow-up queries. |
| `AccessPolicy` (`acl.py`) | `constraints(principal) -> ScopePredicates`; applied inside every arm, hop, cache key and value listing | Eligibility filters are not authorisation; caller context never grants access. |

### 3.5 `backends/contracts.py` — capability contracts (frozen in Phase 0)

Frozen as implemented in `backends/contracts.py` (rev 3; the code is normative, this block mirrors it):

```python
# Scope      = eligibility pushed down to a backend: predicates (incl. ACL constraints), document_ids allow-list, exclude_ids
# ScopeSpec  = typed structured query: predicates, group_by, top_n, sort, measure, time_basis, population (RecordStore only)
# Page{cursor, size} / PageResult[T]{items, next_cursor}: every listing is paged, never the corpus
class Catalog(Protocol):      put_documents(gen, documents, chunks); get(gen, ids) -> list[Document]
                              get_chunks(gen, chunk_ids) -> list[Chunk]; iter_scope(gen, scope: Scope | None, page) -> PageResult[Document]
                              delete_generation(gen)
class LexicalIndex(Protocol): index(gen, chunks, fields); refresh(gen)
                              search(gen, query: LexicalQuery, scope, k, page=None) -> PageResult[ScoredChunk]
                              # LexicalQuery = terms | phrase | boolean; flags: phrase, highlight, field_boost, analyzer_id
class VectorIndex(Protocol):  upsert(gen, ids, vectors, namespace="default"); refresh(gen)
                              search(gen, vector, scope, k, namespace="default") -> list[ScoredChunk]
                              # flags: exact, ann(method), filter_pushdown, max_dimensions, namespaces (semantic cache tier)
class RecordStore(Protocol):  upsert(gen, records: StructuredRecord[], schema: MetadataSchema); execute(gen, scope: ScopeSpec, acl) -> ScopeResult
                              exists(gen, field, value, acl) -> bool; values(gen, field, acl, limit) -> list[str]   # listable fields only
                              # ScopeResult: exactness (exact | approximate | bounded), denominator, value/groups, applied/unsupported
                              # predicates (field_not_filterable, field_not_nullable, unparseable_bound, timezone_missing ...), zero_match, matched_ids
class GraphStore(Protocol):   upsert(gen, entities, relations, memberships); traverse(gen, spec: TraversalSpec, scope: Scope | None) -> list[Path]
                              # Path carries edges with provenance, edge_kind and support; templated queries only; flag memberships
class CacheStore(Protocol):   get(key) -> CacheEntry | None; put(key, value, ttl); invalidate(prefix) -> int   # key starts with GenerationId.prefix()
class LedgerStore(Protocol):  append(job_id, entry); replay(job_id); mark(job_id, status); put_raw(key, raw_reply, valid); get_raw(key) -> RawReply | None
class SessionStore(Protocol): create(record); get(session_id); touch(session_id, now, record=None); expire(now, ttl_seconds) -> int
```

Because the protocols share method names (`get`, `upsert`, `search`, `refresh`), an adapter exposes
one **facet per capability** (`adapter.catalog`, `adapter.lexical`, `adapter.vector`,
`adapter.records`, `adapter.graph`, `adapter.cache`, `adapter.ledger`, `adapter.session`); the
conformance kit and config validation address facets, never the adapter object itself.

`backends/capabilities.py` is a static matrix (adapter → contracts, flags and a **per-(adapter,
capability) status** `contracts | legacy | planned`) used by config validation and by `doctor`.
Configuration may reference a `(backend, capability)` pair only when its status is not `planned`
(and, for `community`/`storage.communities`, only when the graph capability flags `memberships`). Invariants: every operation is generation-scoped; adapters never read
`CURRENT`; schema/index names are `<prefix>_<dataset>_<generation>` (Lucene engines additionally
maintain a per-generation alias and expose `refresh(gen)` so conformance tests are deterministic);
destructive operations are limited to `delete_generation(gen)` for a generation that is not `CURRENT`
and not pinned by a live session; `iter_scope`/`search` return pages, never the corpus; a conformance
check asserts that the catalog read count per request is bounded by the number of hits (principle
2.1.7). Failure modes: typed `BackendError` with operation and adapter name; timeouts configured per
adapter. Not their job: ranking fusion, eligibility logic beyond pushed-down scope, or any ML.

**First-release adapter operation sets** (what "all relevant operations" means per backend):

| Adapter | Catalog | Lexical | Vector | Records (typed scope) | Graph | Cache | Ledger | Session | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `sqlite` (local default) | yes | FTS5 `unicode61`, phrase | NumPy/Faiss exact (local files) | yes | passage-backed, `TraversalSpec` | yes | yes | yes | Existing alpha behaviour refactored onto contracts (Phase 0). |
| `postgres` | yes | FTS (`websearch_to_tsquery`, configurable text search config), phrase via `phraseto_tsquery` | pgvector exact; optional HNSW with filter-recall test gate | yes (SQL aggregates, date buckets, top-N) | no | yes | yes | yes | Extends current `adapters/postgres.py`. |
| `sqlalchemy` (generic SQL) | yes | dialect-gated: MySQL `FULLTEXT`, SQLite FTS5, PostgreSQL via dialect; otherwise capability absent | none | yes | no | exact only | yes | yes | Tested dialects: SQLite, PostgreSQL, MySQL/MariaDB. Unknown dialects validate but are "untested". |
| `opensearch` | yes (doc index) | BM25, `match_phrase`, highlights, field boosts; `refresh(gen)`; per-generation index + alias | k-NN (HNSW/faiss engines) + exact `script_score` rescore | aggregations: terms, date_histogram, min/max/avg/sum, cardinality (approximate, flagged) | no | no | no | no | Shared `_lucene_common.py` with Elasticsearch. |
| `elasticsearch` | yes | same as OpenSearch | `dense_vector` kNN + exact rescore | same | no | no | no | no | Separate client; same conformance suite. |
| `neo4j` | no | no | optional later (vector index, Phase 5) | no | typed traversal, provenance on edges, communities as nodes, generation-scoped labels, `delete_generation` | no | no | no | Extends current `adapters/neo4j.py`. |
| `mongodb` | yes | text index; Atlas Search optional | Atlas Vector Search only (flagged; absent on community server) | aggregation pipeline (count, group, min/max/avg/sum, date buckets, top-N) | no | yes | yes | yes | |

All seven adapters run the same `testing/backend_conformance.py` suite per declared capability.
**Lexical conformance semantics** (what is tested per engine, replacing any cross-engine identity
claim): scope pushdown (no out-of-scope hit, ever), determinism (same index + query → same ordered
result), phrase support honours the `phrase` flag (a phrase query never matches a document lacking the
contiguous tokens as analysed by that engine), pagination is stable and non-overlapping, generation
isolation, `refresh` visibility, declared `analyzer_id` recorded in the generation manifest. Ranking
and tokenisation parity across engines is **not** promised (notes §12): FTS5 `unicode61`, PostgreSQL
text-search configurations and Lucene analyzers differ. Instead, a **cross-engine agreement report**
on a fixed synthetic fixture (overlap@k, rank correlation, per-query differing members) is produced by
`tests/services/test_lexical_agreement.py` and recorded in `VALIDATION.md` as information, not as a
pass/fail gate.

### 3.6 `providers/` — model providers

```python
class ChatProvider(Protocol):
    def capabilities(model: str) -> ProviderCapabilities   # per (provider, model): structured_output: none|json_mode|json_schema|native, prompt_caching: none|passive|controlled, streaming, batch, reasoning_controls, pricing_key
    def complete(ctx, role_cfg, system, messages, schema: type[T] | None, stream: bool) -> Completion[T]
class EmbeddingProvider(Protocol): encode(texts, query: bool) -> ndarray  ; recipe() -> EmbeddingRecipe
class RerankProvider(Protocol):    score(query, passages) -> list[float]
```

Implementations: `openai` (Chat/Responses with `json_schema` strict mode; prompt caching is
**passive** prefix caching — the ledger reports `cached_tokens` from usage, the package does not claim
control), `anthropic` (structured output via tool schema or native structured output where the pinned
model supports it; `cache_control` on system and static blocks = `controlled`), `openai_compatible`
(vLLM, Ollama OpenAI endpoint, LM Studio; capabilities declared in the role config because servers
differ), `ollama` native (optional), `sentence_transformers` (embeddings, cross-encoder). The role
config declares the capabilities it relies on for its `(provider, model)`; `doctor --probe` verifies
them with one minimal call per role when authorised. Invariants: no hidden retry; truncation is a
failure (as today, `providers.py::JsonModel`); requested and returned model identity, usage, cache
hits, sampling/reasoning settings recorded; a declared-but-unsupported capability fails validation or
the probe rather than downgrading. Pricing comes from an operator-supplied versioned table
(`pricing.py`), never bundled defaults. Evidence level: OpenAI/Anthropic native structured output and
caching ship **contract-mocked** unless the owner authorises a capped manual real-provider smoke (§9).
Not its job: prompt content (components and profiles).

### 3.7 `decision/` — decision agent and typed query scope

```python
class DecisionAgent(Component):
    def decide(ctx, request, intents: Intents, capabilities: Capabilities) -> Decision
class Decision(StrictModel):
    plan: Plan | None                            # typed steps; each arm has its own step schema (no aggregate on non-structured steps)
    scope: ScopeSpec | None                      # typed structured scope (below); None under raw_all_arms
    unsupported: list[UnsupportedConstraint]     # constraint text, reason, suggestion
    outcome: Literal["decided", "abstain", "raw_all_arms"]
    confidence: float | None; agent: str; stage: str | None; policy_applied: str
class ScopeSpec(StrictModel):
    predicates: list[Predicate]                  # eq | in | range | date_interval | missing | not; typed by MetadataSchema
    group_by: list[str]; top_n: int | None; sort: Sort | None
    measure: Measure | None                      # count | count_distinct(unit) | min | max | avg | sum(field) | first_seen | last_seen | share | bucket(date, granularity)
    time_basis: str | None                       # which declared datetime field
    population: Literal["canonical_record", "chunk"] = "canonical_record"
```

**Intent pre-pass** (`decision/intent.py`): deterministic, always runs first, never fails: detects
aggregate intent (count/how many/list all/total/earliest/latest grammar), exact identifiers (patterns
from the alias map), quoted phrases, and follow-up markers. Its output feeds every agent and the
`AnswerContract` (§3.9a).

Implementations: `rules` (deterministic grammar → `ScopeSpec`; ID patterns → `exact_id`; quoted
strings → phrase boost; intent predicates → arm add/remove), `classifier` (§3.7.3; default off),
`llm` (planner with per-arm step schemas, native structured output; receives observed values only for
fields whose cardinality is under the cap and within ACL scope), `cascade` (ordered stages; first
stage that returns `decided` wins).

#### 3.7.1 Fail policy — per agent type and per stage (B3)

Default everywhere: **`fail_closed`** (abstain with reason). The generated schema carries this default.

| Agent / stage | Failure condition | Default outcome | Permitted outcomes (config) | `Decision` carries |
|---|---|---|---|---|
| `intent` pre-pass | none (deterministic) | — | — | `intents` |
| `rules` (standalone) | no rule produced a plan | `abstain` | `abstain`, `raw_all_arms` | `outcome`, `unsupported` from unparsed grammar |
| `rules` (cascade stage) | same | `fallback_to_next_stage` | `fallback_to_next_stage`, `abstain` | stage note in trace |
| `classifier` (standalone) | confidence below threshold; artifact failed validation/probe | `abstain` | `abstain`, `raw_all_arms` | `confidence` |
| `classifier` (cascade stage) | same | `fallback_to_next_stage` | `fallback_to_next_stage`, `abstain` | same |
| `llm` | schema violation, timeout, budget exhausted, invalid step (disabled arm, non-filterable field, override of request filter), unresolvable value when the operation requires it | `abstain` | `abstain`, `fallback_to_next_stage` (cascade), `raw_all_arms` (explicit opt-in) | `unsupported` incl. unresolvable values; failure category in trace |
| `cascade` | every stage failed | `abstain` | `abstain`, `raw_all_arms` | last stage failure |

`raw_all_arms` (the only fail-open outcome): `plan` = raw query on every enabled non-structured arm,
`scope = None`, `decision_fallback = true` in the trace, and **one `UnsupportedConstraint` per
aggregate intent detected by the pre-pass**. Because `AnswerContract` (§3.9a) forbids count,
exhaustive-list and absence claims without an exact `ScopeResult`, a fail-open answer to "how many…"
can only be narrative with an explicit unsupported-constraint note. Request filters are immutable by
every agent (as today). Not its job: executing anything.

#### 3.7.2 `ValueResolver` and predicate compilation (B4)

| Predicate | Field types | Resolution | Outcome when it fails |
|---|---|---|---|
| `eq`, `in` | `string`, `enum`, `id` | `enum`: value must be in `enum_values`. Otherwise: if observed cardinality ≤ `max_catalog_cardinality`, the (ACL-scoped) observed values were supplied to the planner and the value must match one (`casefold_match` per field); else the planner's candidate is checked with `RecordStore.exists(gen, field, value, acl)`. A value written by the caller in `request.filters` is applied as given (explicit user filter ≠ model-invented identifier) and a zero match is reported with `zero_match: true`. | `UnsupportedConstraint{reason: value_not_observed}`; operation downgraded to `abstain` if the measure depends on it |
| `eq`, `in` | `integer`, `number`, `boolean` | type parse only | `UnsupportedConstraint{reason: type_mismatch}` |
| `range` | `integer`, `number`, `datetime`, `date` | type parse; datetime requires an explicit offset under `require_offset`; bounds inclusive by default, declared in the predicate | `UnsupportedConstraint{reason: unparseable_bound | timezone_missing}` |
| `date_interval` | `datetime`, `date` | as `range` plus declared granularity (day/month/year) with half-open `[start, end)` boundaries computed in the field's timezone policy | same |
| `missing` | any `nullable` field | field must be nullable | `UnsupportedConstraint{reason: field_not_nullable}` |
| any | unknown or non-`filterable` field | — | `UnsupportedConstraint{reason: field_not_filterable}` |
| `group_by` / `sort` | fields flagged `groupable` / `sortable` | — | `UnsupportedConstraint` |
| `measure` | `sum/avg/min/max` require `integer|number`; `first_seen/last_seen/bucket` require a declared `time_basis` | — | `UnsupportedConstraint{reason: measure_unsupported}`; **never substituted by `count`** |

Observed-value catalogs are built per generation at ingestion for fields under the cap and filtered by
ACL at request time; fields with `max_catalog_cardinality: 0` (IDs, free text) are never enumerated
to a model.

#### 3.7.3 Classifier artifact contract (B6)

A `classifier` agent may be configured only as (a) a registered component from an entry-point plugin
listed under `plugins:`, or (b) the built-in `onnx_classifier` with `{model_path, sha256, manifest}`
where `manifest` declares feature extraction (`hashed_ngrams` or `embedding:<role>`), label set and
threshold; the file's sha256 is verified at `validate()` and recorded in the trace. Pickled artifacts
(`.pkl`, `.joblib`, any `sklearn`-style path) and dotted import paths are **validation errors**. A
Python callable is accepted only through `Engine(config, components=[...])`. Unit test:
`tests/unit/test_classifier_artifacts.py` asserts that a pickle path and a dotted path both fail
validation and that a sha256 mismatch fails. Historical finding (measured negative on the historical
support-search services) is carried as a regression scenario; the agent ships without weights and
defaults off.

### 3.8 `arms/` — retrieval arms

Each arm is a `Component` (`kind=arm`) with `run(ctx, step) -> ArmResult(hits, aggregates, paths,
reports, diagnostics)`. All five existing arms are ported onto this contract in Phase 0 against the
sqlite adapter: `dense`, `lexical` (terms + optional phrase/verbatim-window boost with DF guard),
`structured` (executes `ScopeSpec` via `RecordStore`; returns `ScopeResult` exactness metadata),
`graph` (`TraversalSpec{seeds, predicates, direction, hops, edge_kinds}`; returns paths with edge
provenance; `fact` required for factual hops, `co_occurrence` only when requested — the minimal spec
ships in Phase 0, extensions in Phase 3), `community` (membership + grounded report objects with
status). New arms: `exact_id` (alias map `id|alias → canonical id`, pin to front, no fuzzy alias
inference; Phase 1) and `cache` (§3.11; Phase 2). Invariant: arms receive the already-applied ACL
scope and never widen it.

### 3.9 `selection/` — inclusion, ordering, packing (notes §5)

Separate, individually configured stages: `collapse` (passage → canonical record), `limits`
(per-arm), `union` (`preserve` default), `ordering` (`reciprocal_rank` default = today's `per_arm`
behaviour; `best_arm_rank`; `stable_interleave`; `rrf`; `minmax`; `zscore`), `diversity` (opt-in
groups by any metadata field with documented fallback when a group is empty), `pins` (from
`exact_id`), `packing` (evidence budget with fair per-record allocation and a configurable field
priority — no hardcoded `details/resolution` ordering). Invariant: ordering-only changes never change
union membership; trace records the effective policy and the selection reason per record.

### 3.9a `generation/contract.py::AnswerContract` (new, Phase 0; B3)

```python
class Claim(StrictModel):  text: str; kind: Literal["narrative", "count", "exhaustive_list", "absence"]; evidence_ids: list[str]
class Answer(StrictModel): answer: str; claims: list[Claim]; citations: list[str]; insufficient_evidence: bool; short_answer: str | None = None
class AnswerResult(StrictModel):
    answer: Answer; unsupported_constraints: list[UnsupportedConstraint]   # populated by the engine from Decision + ScopeResults; the generator cannot set it
    coverage: Coverage | None                                              # from the generation manifest under quarantine/partial
```

Rules enforced by the engine after generation, before any guard: (1) every `citations`/`evidence_ids`
entry exists in supplied evidence (today's check); (2) a `count`, `exhaustive_list` or `absence`
claim must reference an aggregate evidence whose `ScopeResult.exactness == "exact"` (absence
additionally requires the scope to be exhaustive for the population); (3) when the intent pre-pass
detected an aggregate intent and no exact `ScopeResult` exists, the answer must not contain a
`count`/`exhaustive_list` claim and `unsupported_constraints` is non-empty; (4) a factual answer
without citations is rejected (today's check). Violation handling: `on_violation:
abstain_with_reason` (default; returns `insufficient_evidence: true` with the violated rule in the
trace) or `error`. Serving layers render `unsupported_constraints` verbatim after the answer text.
The Phase 2 `exact_set` guard is the hook form of rule (2)/(3) that additionally screens prose
numerals; the contract itself ships in Phase 0 so Phase 1 has no fabricated-count path.

### 3.9b `pipeline/review.py::ReviewStage` (new, Phase 0; B7)

`Component(kind="reviewer")`, runs after selection and before generation when `pipeline.review.enabled`.
Uses role `reviewer` with a `Review{sufficient, missing, followup_query}` schema (today's
`engine.py::REVIEW_PROMPT`). Loop bounded by `max_hops`; stop reasons `sufficient |
no_actionable_gap | repeated_query | hop_limit | budget_exhausted`, recorded per round. A follow-up
query is re-planned through the configured decision agent (never raw-all-arms), with request filters
and ACL immutable and the generation pinned; new hits merge through `selection/` with one vote per
record per arm (today's `fuse` rule). Tests `test_agentic_hops_are_bounded_and_generation_cites_evidence`
and `test_agentic_no_false_missing_none_retry` move to `tests/contract/test_review_stage.py` unchanged
in intent.

### 3.10 `guards/` — hook framework

Hook points: `pre_request`, `pre_retrieval`, `post_retrieval`, `pre_generation`,
`post_generation`, `pre_response`. Built-ins (all configurable, none domain-specific by default):
`request_redaction` (`pre_request`: configurable masking of query/context before any remote stage;
notes §9), `safety_refusal` (rule set supplied by profile; empty in `neutral`), `prompt_leak`
(n-gram shingle overlap against the effective prompts), `exact_set` (hook form of §3.9a rules 2–3
plus prose-numeral screening), `absence` (rewrites "does not exist in the corpus" to "not found in
retrieved evidence" unless an exhaustive exact scope exists), `claim_status` (Phase 3, with the
ontology: a claim asserting an ontology status such as "verified" requires an edge carrying that
status), `source_directives` (strips instruction-like text from evidence before generation; logged),
`output_redaction` (serve-time regex redaction with labels kept). A guard returns `allow | modify |
block` with a reason recorded in the trace. **Failure semantics:** a guard that raises is treated as
`block` for `safety_refusal`, `exact_set`, `absence`, `claim_status` and `output_redaction`; for
`prompt_leak`, `source_directives` and `request_redaction` it is `block` unless the guard is configured
`on_error: noop`; never `allow` by default. Unit test `tests/unit/test_guard_failure_semantics.py`.

### 3.11 `arms/cache.py` — cache arm

Key = `sha256(GenerationId + effective_config_fingerprint + acl_scope_hash + normalised_query +
context_hash)`. Tiers: `exact` (key match in `CacheStore`) and `semantic` (query embedding stored in a
`VectorIndex` namespace `cache:<GenerationId>:<acl_scope_hash>`; similarity ≥ threshold **and** a
gate: `rule` (same scope, same filters) or `llm` role `cache_gate` returning `answer_from_cache |
new_search`; requires a backend with `VectorIndex`, otherwise only `exact` validates). Value = answer
id, evidence IDs, citations, trace id, created at. Invalidation: generation switch changes the key
prefix and namespace (nothing stale is reachable); TTL; explicit `invalidate_cache` action. A cache hit
short-circuits the pipeline (`shallow_before_deep`) and the response carries `served_from_cache: true`
plus the original evidence IDs re-fetched from the pinned generation; **cached answers are never
passed to the generator as evidence**. Historical note: this generalises the session-level answer
cache of the historical support-search services (measured latency-positive there; private
evaluation, not reproducible here) and is unrelated to the measured-negative synthetic Q&A cache,
which is not ported.

### 3.12 `extraction/` — extractors, grounding, support, ontology, resolution

```python
class Extractor(Component):  run(ctx, chunk, schema: ExtractionSchema) -> ExtractionOutput
def locate(quote: str, source: str, normalisation: Normalisation, ambiguous: Literal["reject","first"]) -> Span | None
class PredicateSupport:      check(relation, span_text, ontology | None) -> Literal["trigger_in_span", "unchecked"]  # negation-aware
class OntologyContract:      entity_types, relation_types{domain, range, triggers, status_enum, required_provenance}; validate(output) -> Diagnostics
class Resolver:              resolve(entity, scope) -> (canonical_id, method, needs_review)  # vocabulary alias → noise strip → glossary synonym → compatible-type alias → slug → new
```

**Grounding with an offset map (B5).** `normalise(source) -> (norm_text, index_map)` applies the
declared steps per source code point: NFKC (an expansion such as a ligature or full-width form maps
every output char to the same source index), casefold (`ß → ss` likewise), whitespace runs (incl.
NBSP) collapse to one space mapped to the first source index of the run; combining marks stay attached
to their base index. The quote is normalised the same way; a match `[i, j)` in `norm_text` maps to
`start = index_map[i]`, `end = index_map[j-1] + width(source char at that index)`, and the invariant
`Span.text == source[start:end]` is asserted. More than one match → `reject` (default) or `first`.
Unit cases in `testing/fixtures/grounding/`: exact, case-only, whitespace-only, NBSP, ligature `ﬁ`,
full-width Latin, combining marks, `ß`, relocated to another field of the same record, ambiguous,
no-match. Levels `exact | normalised | relocated`; anything else is rejected under `strict`, or
recorded as a categorised failure under `quarantine`/`partial` (§3.14.1) and excluded from every
derived store and from evidence.

**Endpoint containment (B5).** For LLM extractors that return endpoint names, each endpoint is located
inside the relation span with the same `locate()`; the entity span must be contained in the relation
span and must resolve to a typed entity from the same chunk's output (type declared or unique, as
today's `extraction.py` binding rule). Failure rejects the relation (categorised
`endpoint_not_in_span` / `endpoint_type_ambiguous`).

**Predicate support.** A relation is `fact` only if the predicate or a declared trigger term appears
in the span without a negation marker between trigger and endpoints; otherwise it is stored as
`co_occurrence` (explicit separate kind, never a silent downgrade of a fact). Trigger matching uses
declared trigger lists plus a pure-Python suffix stemmer (`snowballstemmer`, language configurable)
over span tokens; the spaCy lemmatiser is used instead when the `spacy` extra is installed and
selected. Dedupe: identical `(subject, predicate, object, source span text)` across chunks that inherit
the same header/title text collapse to one fact with all occurrence provenance kept. Not its job:
deciding domain vocabularies (profiles/ontology files do).

### 3.13 `communities/`

Strategies: `entity` (today), `louvain` (today), `leiden_hierarchical` (levels 0–2 over a declared
substrate: entity bipartite, similarity kNN, declared links). `stats.py` computes deterministic
per-community statistics only from declared fields (size, members by `kind`, time trend over a
declared `time_basis` field, top entities by lift); no severity or domain labels in core. Reports are
objects with `status ∈ {pending, valid, rejected, partial}` and generation recipe; a rejected report
never counts as coverage. Incremental augmentation: new members attach to existing communities when
membership overlap ≥ configured Jaccard, else form new ones; existing stats are recomputed, existing
valid reports are kept and flagged `stale: true` until regenerated.

### 3.14 `ingestion/` — write path, jobs, ledger, delta, release

`ingestion/write.py` is the single write path (Phase 0 on sqlite, Phase 1 multi-backend): it builds a
staged generation by calling `Catalog.put_documents`, `LexicalIndex.index`, `VectorIndex.upsert`,
`RecordStore.upsert(records, schema)`, `GraphStore.upsert` and the community store through the
`storage:`/arm backend selectors, then runs **per-projection validation** (row/vector counts match
the catalog, a sample of IDs round-trips through every projection, `refresh` visible) and only then
switches `CURRENT`. Any projection failure leaves the old generation active and the staged external
projections are listed in the failure manifest for cleanup (today's "external partial generations
are unreachable" rule).

`IngestJob` (id, recipe fingerprint, policy, budgets incl. `max_usd`, status) writes a `LedgerStore`
entry per unit keyed `(prompt_sha, input_sha, schema_sha, model_id)`; **raw provider replies are
stored for every call, valid or invalid** (`put_raw`), so grounding and schema changes can be replayed
without inference (B8). Resume replays successes without inference and re-attempts recorded failures
only when asked. Embedding reuse keyed by the exact effective input (title + field + text + prefix +
model/revision/dimension). Delta ingestion: only changed documents re-chunk/re-embed/re-extract;
vectors append; graph upserts remove stale facts of changed documents; affected communities/reports
are invalidated. `release.py`: sha256 manifest of a generation's local files and external index
names, `verify`, `promote`, `rollback`, `gc` (unreferenced generations only, never the active one or
one pinned by a live session).

#### 3.14.1 Extraction policies (B5)

| Policy | Unit that may fail | Behaviour | Manifest / exposure | Activation |
|---|---|---|---|---|
| `strict` (default) | any chunk/document/report | first failure aborts the job; old generation stays active; staged directory removed | failure manifest with the failing unit and category | never |
| `quarantine` | chunk or document extraction (grounding, endpoint, schema, predicate, ontology violations) | the document stays in catalog/lexical/dense/structured projections (they do not depend on extraction); its extraction outputs are excluded from graph/community/report stores; per-document status `extraction: rejected:<category>` | `coverage.extraction = {accepted, rejected_by_category, documents_without_graph}`; `AnswerResult.coverage` carries it on every answer that touched graph/community/report evidence | allowed; manifest `policy: quarantine` |
| `partial` | as `quarantine` **plus** whole stages stopped by budget/provider failure (LLM extraction past `max_usd`, community reports, embeddings for a provider outage) | the stopped stage is marked `incomplete` with the first unprocessed unit; arms that depend on an incomplete stage refuse to run on that generation unless `pipeline.allow_incomplete_arms: true`, in which case every result from them carries `coverage.incomplete: true` | `coverage.stages = {name: complete | incomplete{processed, remaining, reason}}` | only with `ingest --allow-partial`; manifest `policy: partial`; resume can complete the stage into a new generation |

No policy ever drops a unit silently or converts a rejected relation into a fact.

### 3.15 `serving/`

HTTP (`http.py`): `POST /v2/search`, `POST /v2/answer`, `POST /v2/answer/stream` (SSE events:
`decision`, `arm_result`, `evidence`, `review_round`, `token`, `guard`, `unsupported_constraints`,
`done`, `error`), `GET /v2/capabilities`, `GET /v2/values/{field}` (only `listable` fields; results
filtered by `AccessPolicy`; paged), `POST /v2/sessions`, `GET /health` (liveness), `GET /ready`
(probes backends of the pinned generation). Search responses support `page`, `page_size`
(≤ `serving.http.page_size_max`), `sort` (`relevance` or a `sortable` field) and `facets` (terms
counts over `groupable` fields within ACL scope, exactness-flagged) from Phase 2. Auth:
`PrincipalResolver` hook; built-ins `bearer_env` (today) and `static_api_keys`; OAuth2 is an adapter
outside core (open decision). MCP (`mcp.py`): tools `search`, `answer`, `capabilities`, `list_values`
(same `listable` + ACL rule), `lookup_record`; `answer` streams progress notifications; transports
`stdio` and `streamable_http` with session binding to the creating principal; resources expose the
active generation manifest. **Parity** (`parity.py`): the HTTP and MCP schemas are generated from the
same `SearchRequest`/`AnswerRequest` models and a test asserts field-for-field parity, including
`exclude_ids` and typed filter values that today's MCP wrapper drops (`mcp.py` accepts only
`dict[str, str]` filters).

### 3.16 `evaluation/` and `learning/`

Evaluation: `manifests.py` (frozen corpus/config/recipe/question identity and hashes), `replay.py`
(stored per-stage rank lists: per-arm candidates, selected union, ordered union, supplied evidence,
cited evidence), `paired.py` (same inputs, two configs; exact McNemar and bootstrap CIs; wins/losses/
ties), `judges.py` (`Judge` interface; built-ins: deterministic must-facts and citation-support
checks; LLM judge with recorded identity, blinded, swapped-order; never the generator as sole judge),
`slices.py` (by source kind, intent, label availability; original denominators preserved when
documents are rejected), `regression/` (scenario pack encoding measured negatives, §6.4).

Learning: `batch.py` loads graded interactions (trace ids + grades + failure category);
`analyzers/` per arm and per stage (routing priors from arm-recall-vs-selected; alias candidates from
exact-ID misses; extraction recipe diffs from rejection categories; selection policy diffs from
omitted-relevant-in-budget; scope grammar gaps from unsupported constraints); `proposals.py` emits
`Proposal{kind, evidence_trace_ids, config_patch (JSON Patch), expected_effect, validation_plan}`
into `proposals/<batch>/` as reviewable artifacts; `validate.py` runs the harness on a proposal in an
isolated workspace and attaches paired results. **No proposal is applied to a serving config by the
module**; `apply` is a separate human-invoked CLI that writes a new versioned config file, is not an
`Action`, and has no HTTP/MCP surface.

---

## 4. Phased delivery

Estimates are engineer-days (ed) for one competent engineer including tests and docs for that
module. Each phase ends with `ruff check src tests`, the relevant test tiers green, regenerated
`docs/config.schema.json`, and an updated `docs/VALIDATION.md` entry with actual results.

### Phase 0 — Kernel, frozen contracts, arms and write path on contracts, measured-failure fixes, domain decoupling (61 ed)

Scope: establish the component harness and config v2; **freeze the backend contracts** and port the
existing local backend, the five arms and the ingestion write path onto them; fix the four measured
failures; remove support-domain coupling (notes §4, §14 P0). No new external backends.

| Module | Files | Contract / config surface | Tests | ed |
|---|---|---|---|---|
| Canonical types with spans | `core/types.py` | §3.1; endpoint-containment invariant | unit: invariants, span equality with source, containment | 3 |
| Metadata schema | `core/types.py::MetadataSchema`, config validation hooks | §3.1a | unit: type/nullability/enum validation at ingestion; filterable/listable/groupable gating | 2 |
| Component + registry | `core/component.py`, `core/registry.py`, `core/errors.py` | §3.2; `plugins:` entry points; artifact rules | unit: registration, validate diagnostics, unknown plugin rejected, pickle/dotted path rejected | 3 |
| Config v2 + v1 upgrade + schema | `core/config.py`, `core/schema.py`, `compat/config_v1.py` (keeps v1 `Config` + `index_fingerprint`) | §3.3; `schema_version: 2`; `storage:`; `config upgrade` | unit: every `examples/*.yaml` upgrades deterministically; fingerprint round-trip (N6); schema drift check | 4 |
| Backend contracts + capability matrix (**freeze**) | `backends/contracts.py`, `backends/capabilities.py` | §3.5 incl. `refresh`, paging, `exists/values`, `put_raw` | unit: in-memory fake adapter satisfies every protocol; matrix consistency | 4 |
| sqlite adapter on contracts | `backends/sqlite/` | all eight capabilities (local) | conformance kit v0 (in-memory fake + sqlite) | 4 |
| Five arms as Components | `arms/base.py`, `dense.py`, `lexical.py`, `structured.py`, `graph.py` (minimal `TraversalSpec`), `community.py` | §3.8; `ArmResult` | contract: each arm against sqlite reproduces today's `test_core.py` behaviours (filters before selection, dedup, grounded traversal, community membership) | 8 |
| Ingestion write path on contracts (sqlite) + per-projection validation | `ingestion/write.py`, `ingestion/policy.py` | §3.14, §3.14.1 (`strict`/`quarantine`/`partial`) | unit: failed projection keeps old generation; quarantine excludes derived outputs and records coverage; partial refuses activation without flag | 5 |
| Runtime context, budget, trace | `runtime/context.py`, `budget.py`, `trace.py` | reservation API; USD via price table; deadline; masked export incl. unsupported-constraint text | unit: exhaustion, deadline, masking | 4 |
| Span-anchored grounding with offset map | `extraction/grounding.py`, `testing/fixtures/grounding/` | `grounding: {normalisation, ambiguous}` | unit: all fixture categories incl. length-changing normalisation; `Span.text == source[start:end]` property test | 4 |
| Predicate support + edge kinds + dedupe | `extraction/predicate_support.py`, `extraction/dedupe.py` | triggers from ontology or config; stemmer; negation | unit: the `at`→`hosted` case stored as `co_occurrence`; negation; dedupe keeps occurrence provenance | 3 |
| Intent pre-pass, typed scope, value resolution, fail policies | `decision/intent.py`, `scope.py`, `values.py`, `base.py` | §3.7, §3.7.1, §3.7.2 | unit: every row of the resolution table; every row of the fail-policy table; unsupported total never becomes count; zero-count with absent value flagged | 6 |
| Answer contract | `generation/contract.py` | §3.9a | unit: count claim without exact ScopeResult → abstain_with_reason; engine-injected unsupported constraints; generator cannot set them | 2 |
| Review stage | `pipeline/review.py` | §3.9b | contract: the two existing agentic tests moved; stop reasons; re-plan through decision agent | 3 |
| Selection split | `selection/*.py` | §3.9; `ordering: reciprocal_rank` reproduces today's `per_arm` order | unit: membership preserved under ordering changes; packing fairness; configured field priority beats `details` | 3 |
| Neutral prompts + profiles + voice option | `profiles/*.yaml`, `generation/generator.py`, `decision/llm.py` prompts | `profile:` overlay; prompts have no domain authority rules; `generation.voice` optional | contract: three synthetic domains (support, research notes, collection records) through one engine; custom context keys survive | 3 |

Acceptance: all current 40 tests pass or are replaced by equivalents that keep the same edge cases
(support-domain fixtures move under the `support_example` profile, N12); notes §4 acceptance 1–5
hold; the grounding fixture pack passes with the offset-map property test; the five `hosted`
occurrences from the public HybridQA diagnostic, reproduced as a synthetic fixture, become one
`co_occurrence` edge with five provenance entries; the nine NL-count probe shapes from the public
HybridQA diagnostic, reproduced as synthetic requests, yield abstentions or flagged unsupported
constraints and zero fabricated numbers under both `fail_closed` and `raw_all_arms`; a config that
upgrades from v1 round-trips its fingerprint. **External evidence (owner-authorised, not a CI gate):**
one bounded local-model re-run of extraction over the unchanged frozen public cohorts with raw
replies cached, reporting accepted/rejected counts per category under v1 exact matching and v2
span-anchoring, recorded in `VALIDATION.md` (B8). Dependencies: none. Risks: contract churn after the
freeze — any change after Phase 0 requires a changelog entry here and a conformance-kit update.

### Phase 1 — First shippable release `2.0.0a1` (102 ed)

Scope: the owner's first-release list. Six backends on the frozen contracts with their operation
sets (§3.5), three provider families with native structured output and prompt caching, declarative
pipeline, first-class sessions/memory/tools/actions (minimal but real), configurable decision agents
with the stated fail policies, `exact_id` arm, HTTP+SSE and MCP stdio+streamable HTTP with parity,
`doctor --probe` and CLI v2, pip extras, CI services job.

| Module | Files | Contract / config surface | Tests | ed |
|---|---|---|---|---|
| Conformance kit (full) | `testing/backend_conformance.py` | per-capability suites incl. lexical semantics (§3.5), paging, refresh, generation isolation, bounded catalog reads | runs against the in-memory fake on every push; real adapters in the services job | 3 |
| Multi-backend write path + per-projection validation | `ingestion/write.py` | projections through `storage:` and arm backends; failure manifest lists staged external projections | contract with fake backends; services: mixed sqlite+postgres+opensearch config | 5 |
| postgres adapter | `backends/postgres/` | catalog, FTS, pgvector exact + optional HNSW (gated by filter-recall test ≥ configured floor), records (`exists/values`), cache, ledger, session | conformance (container); HNSW recall test | 6 |
| sqlalchemy adapter | `backends/sqlalchemy/` | catalog, records, ledger, session, exact cache; dialect-gated lexical | conformance on SQLite, PostgreSQL, MySQL containers; unsupported dialect lexical → validation error | 7 |
| opensearch adapter | `backends/opensearch/`, `_lucene_common.py` | BM25/phrase/highlight, per-generation index + alias, `refresh`, kNN + exact rescore, aggregations with approximate flags, doc index | conformance (container); aggregation exactness flags verified | 8 |
| elasticsearch adapter | `backends/elasticsearch/` | same operation set | conformance (container) | 6 |
| neo4j adapter | `backends/neo4j/` | typed upsert with provenance, `TraversalSpec` (Phase 0 minimal spec), communities as nodes, generation-scoped labels, `delete_generation` | conformance (container); predicate-supported one- and two-hop traversal; `co_occurrence` excluded unless requested | 6 |
| mongodb adapter | `backends/mongodb/` | catalog, aggregation-pipeline records, text-index lexical, Atlas vector flagged, cache, ledger, session | conformance (container, community server); Atlas vector marked untested | 6 |
| Providers | `providers/*.py` | §3.6; per-(provider, model) capabilities; streaming; usage ledger incl. cached tokens; price table | contract: mocked HTTP per provider incl. structured-output request shapes, cache_control, stream parsing, truncation = failure; real-service: one local OpenAI-compatible server smoke per role | 11 |
| Pipeline spec + engine v2 + validation | `pipeline/spec.py`, `engine.py`, `validate.py` | `pipeline:` block; capability-vs-backend and storage-selector validation | unit: invalid arm/backend pairing and double-catalog rejected; contract: end-to-end on sqlite with fake providers | 6 |
| `doctor --probe` + CLI v2 | `cli.py`, `pipeline/validate.py` | probes backends and declared provider capabilities; `config upgrade`, `release *` stubs | contract: probe reports per backend/role; unit: CLI parity with Python API | 3 |
| Storage selectors | `core/config.py` | `storage:` (§3.3) | unit: exactly-one-authoritative validation | 1 |
| Runtime: session, memory, tools, actions | `runtime/session.py`, `memory.py`, `tools.py`, `actions.py` | §3.4 incl. `ActionRequest/Result/AuditRecord`, generation-switch rule | unit: principal binding, TTL, memory never in evidence, idempotency, actions denied in serving, generation_changed trace | 5 |
| Decision agents | `decision/rules.py`, `llm.py`, `cascade.py`, `classifier.py` (interface + ONNX artifact contract, no weights) | §3.7.1, §3.7.3 | unit: per-arm step schemas; every fail-policy row; cascade ordering; artifact validation (pickle/dotted path/sha mismatch rejected) | 6 |
| `exact_id` arm | `arms/exact_id.py` | alias map file; pins | unit: pin precedence, unknown ID → no fuzzy guess, alias scope | 2 |
| Serving | `serving/http.py`, `sse.py`, `mcp.py`, `auth.py`, `parity.py` | §3.15 (paging/facets in Phase 2) | contract: TestClient + SSE event order; real MCP stdio and streamable-HTTP client round-trips; parity test; `list_values` honours `listable` + ACL stub | 9 |
| Packaging, CI, docs | `pyproject.toml` extras (`opensearch`, `elasticsearch`, `sql`, `postgres`, `neo4j`, `mongodb`, `openai`, `anthropic`, `local`, `api`, `mcp`, `dev`), `.github/workflows/*.yml`, `docs/*` | — | wheel/sdist + clean-install smoke; schema drift job; docs lint | 4 |
| Integration buffer | — | — | cross-adapter semantics found by conformance | 8 |

Acceptance (B2-revised): every adapter passes the conformance suite for every capability it declares
against a real containerised service; a config that selects an arm on a backend lacking that
capability, or two backends for one `storage` capability, fails validation with a path; on the same
synthetic corpus, `dense` returns identical membership and order on sqlite, postgres (exact),
opensearch and elasticsearch (exact rescore) for the same vectors and scope, and `structured` returns
identical `ScopeResult`s on sqlite, postgres, sqlalchemy (all tested dialects), opensearch,
elasticsearch and mongodb for every operation each declares exact (approximate aggregations are
compared with their declared tolerance); `lexical` passes the per-engine conformance semantics on
every engine and the cross-engine agreement report is recorded in `VALIDATION.md`; `graph` passes
the predicate-supported one- and two-hop traversal on sqlite and neo4j; HTTP and MCP accept and
return the same request/response schemas; `pip install '<pkg>[opensearch,anthropic,mcp]'` works in a
clean venv; `docs/VALIDATION.md` records each adapter as real-service tested with image digests.
Dependencies: Phase 0 (frozen contracts). Critical path: multi-backend write path → adapters →
pipeline validation → serving parity. Risks: lexical/aggregation semantic divergence across engines
(capability flags, no parity-of-scores promise); MCP SDK streamable HTTP API changes (pin `mcp`);
Mongo vector only on Atlas (documented, flagged).

### Phase 2 — Harness depth, cache arm, guards, incremental ingestion, release ops (61 ed)

| Module | Files | Contract / config surface | Tests | ed |
|---|---|---|---|---|
| Cache arm | `arms/cache.py` | §3.11; `cache: {mode, ttl_seconds, semantic: {threshold, gate}}` | unit: key includes generation; generation switch → miss; hit never in evidence; gate decisions logged; real-service on postgres/mongo cache stores and a vector namespace | 6 |
| Guard framework + built-ins | `guards/*.py` | §3.10 incl. failure semantics and `request_redaction` | unit per guard with positive controls; `test_guard_failure_semantics.py`; `exact_set` consumes `ScopeResult.exactness`; redaction never alters IDs/URLs | 9 |
| ACL hook | `runtime/acl.py` | `AccessPolicy` resolver; mandatory constraints in every arm, hop, cache key, value listing and facet | contract: scenario fixtures with positive controls (allowed/denied/deleted) across all arms and follow-ups | 4 |
| Sessions/memory persistence + follow-ups | `runtime/memory.py`, backends session stores | episodic memory; shown-vs-eligible IDs; standalone rewrite by `cache_gate`/planner | unit + real-service session stores | 5 |
| Ingestion jobs, ledger, delta, budgets | `ingestion/job.py`, `ledger.py`, `delta.py` | §3.14; `ingestion: {policy, max_usd, resume}`; raw replies incl. invalid; optional provider batch API | unit: resume replays without inference; strict aborts and keeps old generation; unchanged vectors byte-identical; title change re-embeds; delete removes derived facts/memberships | 12 |
| Release manifests, promote/rollback, GC | `ingestion/release.py`, CLI `release verify|promote|rollback|gc` | sha256 manifest incl. external index names | unit: tampered file fails verify; rollback atomic; gc refuses active/pinned | 5 |
| Trace/cost export | `runtime/trace.py` export, OTel optional | JSONL with masking; per-request usage by role/model | unit: no raw passages or constraint text in default export | 4 |
| Classifier decision adapter (full) | `decision/classifier.py` | ONNX artifact + manifest; calibration; `on_low_confidence` | unit with a synthetic ONNX stub; regression scenario documents the historical negative | 3 |
| Serving hardening + paging/sort/facets | `serving/*` | deadlines, cancellation on client disconnect, admission limits, `/ready`; `page/page_size/sort/facets` | contract: deadline → typed error; paging stability; facets within ACL; load smoke | 8 |
| Docs + VALIDATION | — | — | — | 5 |

Acceptance: notes §10 acceptance (reuse, resume, deletes, failed staging); a 1,000-record synthetic
delta re-embeds only changed chunks (verified by vector byte-identity); cache hit returns the
original evidence IDs from the pinned generation and `served_from_cache: true`; guard decisions
appear in trace with reasons and a raising safety guard blocks; rollback restores the previous
generation in one command. Release `2.0.0a2`. Dependencies: Phase 1. Risks: delta graph/community
invalidation complexity — keep the "invalidate and recompute affected communities" rule simple and
tested before optimising.

### Phase 3 — Knowledge depth: ontology, resolution, extractors, communities, selection policies (56 ed)

| Module | Files | Contract / config surface | Tests | ed |
|---|---|---|---|---|
| Ontology contract | `extraction/ontology.py` | §3.12; `extraction.ontology` YAML: types, domain/range, triggers, status enum, required provenance | unit: unsupported predicate rejected or demoted per policy; unrelated ontology (research notes) loads without engine edits | 6 |
| Claim-status guard | `guards/claim_status.py` | §3.10 | unit: status claim without matching edge status → block | 2 |
| Entity resolution, aliases, glossary | `extraction/resolution.py`, `glossary.py` | auditable method chain; `needs_review` queue; placeholder rule (redaction tokens never become entity names) | unit: each resolution method with provenance; scoped identity (same name, different collections) | 7 |
| Extractors: events, records, membership, GLiNER2 (if available), tiered composite | `extraction/extractors/*.py` | per-extractor config; `ExtractionSchema` | contract with fake providers; real-service GLiNER/spaCy smoke; tiered routing logged | 10 |
| Extraction chunk policy | chunk `recipe` | separate embedding vs extraction chunking; no repeated title-only extraction | unit: repeated inherited header extracted once, provenance kept | 2 |
| Hierarchical communities + stats + reports | `communities/leiden.py`, `stats.py`, `reports.py` | §3.13 | unit: deterministic with seed; stats from declared fields only; report status machine; stale flag after augmentation; real: Leiden via optional extra | 10 |
| Traversal extensions | `arms/graph.py`, backend `traverse` | predicate groups, path provenance rendered as `graph_path` evidence, hop-level edge-kind policy | conformance: path provenance in answers; entity-only matches not counted as traversal | 2 |
| Selection policies | `selection/phrase.py`, `diversity.py`, `ordering.py` | phrase/verbatim-window boost with DF and boilerplate guards; stable interleave; opt-in diversity/source priority; record expansion policy | unit: deterministic ties; empty-group fallback; minority-source regression scenario | 8 |
| Quote verification at generation | `generation/quotes.py` | normalised locate of answer quotes against supplied evidence; flag-not-drop | unit: exact/normalised/unverified | 4 |
| Docs + VALIDATION | — | — | — | 5 |

Acceptance: notes §6 and §8 acceptance lists; the joint "Graph behavior and traces" criterion (new
evidence through predicate-supported one- and two-hop paths with provenance); communities expose
overlapping membership, counted population and stale/rejected report state. Release `2.0.0b1`.
Dependencies: Phase 0 grounding/support; Phase 1 graph adapters. Risks: Leiden dependency licensing
and wheels (`leidenalg` is GPL — keep behind an extra, provide `louvain` fallback; open decision).

### Phase 4 — Evaluation harness, learning module, benchmarks (48 ed)

| Module | Files | Contract / config surface | Tests | ed |
|---|---|---|---|---|
| Evaluation harness | `evaluation/*.py`, `benchmarks/protocol.md`, `benchmarks/runners/` | §3.16; frozen manifests; per-stage rank lists; paired McNemar + bootstrap; judges; slices; original-denominator rule; cost/latency distributions; sealed-holdout protocol (hashes in repo, data external) | unit: denominators, tie handling, judge blinding; contract: replay determinism | 16 |
| Regression scenario pack | `evaluation/regression/*.yaml` | §6.4 | each scenario has a positive control | (in above) |
| Learning module | `learning/*.py`, CLI `learn propose|validate|apply` | §3.16; proposals as JSON Patch + report; validation in isolated workspace; `apply` human-only | unit: analyzers on synthetic failure batches; `propose` never writes to serving config; `validate` runs harness and attaches paired result | 14 |
| Public benchmark runs (external workspace) | `benchmarks/runners/` | declared local recipe; HotpotQA + HybridQA re-run on unchanged frozen cohorts, then WikiTableQuestions, SciFact and QASPER | results sanitised into `docs/VALIDATION.md` with scope limits | 12 |
| Docs, README maturity labels | — | — | — | 6 |

Acceptance: the joint "Evaluation reproducibility" and "Answer support" criteria; a declared local
recipe builds the unchanged frozen HotpotQA/HybridQA cohorts with visible rejection counts (full
pipeline, strict policy) — this is the readiness gate for any general-purpose claim; the learning
module produces at least one proposal per analyzer on a synthetic failure batch, each with evidence
trace IDs and a validation result, and zero writes outside `proposals/`. Release `2.0.0rc1` → `2.0.0`.
Dependencies: Phases 1–3. Risks: paid-judge policy (open decision); benchmark time on CPU-only hosts.

### Phase 5 — Scale and later backends (indicative, 30+ ed, post-2.0)

Qdrant, Kuzu, Milvus/Weaviate, LanceDB, MySQL-native FTS; ANN benchmarks with filter-recall gates;
async core and multi-host coordination; opt-in guarded generated SQL/Cypher behind read-only
allow-lists; hedged/speculative provider calls and warmup; existing-vector import contract;
multi-dataset serving; `entailment_checked` predicate support; external dashboards consuming trace
export; optional fine-tuning workflows that consume the harness. Deferred because none is on the
owner's first-release list and each needs a concrete use case plus conformance evidence (notes §12).

### 4.1 Critical path and calendar

```
P0 types/config/contracts freeze (16) ─► sqlite on contracts (4) ─► five arms (8) + write path (5) ─► P0 done (61 total, ~3 streams)
P0 ─► P1 conformance kit (3) ─► multi-backend write path (5) ─► six adapters (39, parallel ×3 ≈ 14) ─► pipeline validation (6) ─► serving parity (9) ─► 2.0.0a1
                                   └─► providers (11) ───────────────────────────────────────────────┘
P1 ─► P2 ingestion jobs (12) ─► cache arm (6) ─► release ops (5) ─► 2.0.0a2
P0 grounding/support ─► P3 ontology (6) ─► resolution (7) ─► communities (10) ─► 2.0.0b1
P2/P3 ─► P4 harness (16) ─► learning (14) ─► benchmarks (12) ─► 2.0.0
```

Totals: P0 61 · P1 102 · P2 61 · P3 56 · P4 48 = **328 ed**. With three engineers and the
parallelism above: `2.0.0a1` ≈ week 13, `2.0.0a2` ≈ week 19, `2.0.0b1` ≈ week 25, `2.0.0` ≈
week 30. Single-engineer sequential: ~15 months. Deferring SSE/MCP streaming to `2.0.0a2`
(open decision 12) shortens the Phase 1 critical path by about one week.

### 4.2 Deferred or re-homed items (N9)

| Item (gap table / inventories / notes) | Disposition | Reason |
|---|---|---|
| Pagination / sort / facets | Phase 2 serving row | needs `sortable/groupable` from `MetadataSchema` (Phase 0) and ACL (Phase 2) |
| Extraction provider batch API | Phase 2 ingestion row (optional) | cost optimisation; after the ledger exists |
| Hedged calls, speculative execution, warmup | Phase 5 | latency engineering after measured baselines |
| Confirmed-cause / ontology-status claim guard | Phase 3 `claim_status` guard | depends on the ontology contract |
| Request-time query/context redaction | Phase 2 `request_redaction` guard | hook framework |
| Existing-vector import contract | Phase 5 | needs row/ID/content alignment contract; no first-release use case |
| Multi-dataset serving | Phase 5 / open decision | one dataset per config in 2.0; multiple engines behind one server is a serving concern |
| QASPER | Phase 4 benchmark list | added |
| Neutral-voice rule | Phase 0 `generation.voice` option, default `none` | small; measured-positive only on a private evaluation |
| Record expansion policy | Phase 3 selection row | configurable field priority replaces fixed ordering |
| Shadow traffic, dashboards | outside core (consume trace export) | notes §12 |

---

## 5. Migration and compatibility (version gate)

| Artifact | v1 (`36d2394`) | v2 | Gate behaviour |
|---|---|---|---|
| Config | `schema_version: 1`, flat `arms.dense: numpy` etc. | `schema_version: 2`, named `backends`, `storage`, `pipeline`, `metadata_schema` | `load_config` accepts v1, runs `compat/config_v1.py::upgrade` (deterministic mapping table in that module: `arms.dense numpy/faiss → backends.local` vectors, `pgvector → backends.pg`, `arms.lexical sqlite/postgres`, `arms.graph neo4j`, `routing.mode raw → pipeline.decision {kind: rules}`, `planned → {kind: llm}` with `review.enabled: false`, `agentic → {kind: llm}` with `review {enabled: true, max_hops}`, `fusion.strategy → selection.ordering`, `extraction.providers → extraction.extractors` with `tags → gazetteer`, `arms.sql_fields → metadata_schema` fields of type `string`, `filterable: true`, with a warning to declare real types, `max_llm_calls → budgets.max_llm_calls`), emits a warning with the diff, and `config upgrade` writes the v2 file. `compat/` keeps the v1 `Config` model and `index_fingerprint()` so legacy-read can verify a v1 generation; a round-trip test (`upgrade(v1) → downgrade → same v1 fingerprint`) guards it (N6). **Rev 3 scope of N6:** the guarantee holds for every v1 setting that had an effect; v1 settings v2 cannot carry — a `provider: none` model block with non-default fields, or an `evidence_char_budget` for a stage without a model — are reported by `upgrade()` as `lossy_upgrade` warning diagnostics (and refused under `--strict`); for `community.*`/`extraction.model` such settings were part of the v1 fingerprint, so the operator must remove them or rebuild. v1 acceptance is kept for two minor releases, then removed. |
| Snapshot manifest | `index_format: 2`, `schema_version: 1` | `index_format: 3` (spans, edge kinds, ledger with raw replies, release manifest, metadata schema) | Engine v2 opens a v1 generation in **legacy-read** mode for `dense`, `lexical`, `structured` arms only (tables `documents`, `chunks`, `fts` are compatible; `structured` runs on a read-only `RecordStore` view over the v1 `documents` JSON blobs with the upgraded string-typed schema). `graph`, `community` and `cache` arms refuse v1 generations with a typed error (relations lack `edge_kind/support/span`). Any write (upsert, delta) on a v1 generation is refused; the operator runs `migrate-snapshot` or `ingest --replace --rebuild`. |
| `migrate-snapshot` | — | CLI | Reads the v1 `extraction_cache` (accepted extractions only — v1 never stored rejected raw replies), re-grounds every cached entity/relation with span-anchoring (no inference), types relations via predicate support, rebuilds communities, writes a new `index_format: 3` generation under the same recipe fingerprint rules, leaves the v1 generation in place for rollback. Reports accepted/rejected counts. |
| Fingerprint | `Config.index_fingerprint()` hand-listed fields | composed from `recipe_fingerprint()` of index-affecting components | A v1 fingerprint never equals a v2 one; the v2 manifest records both `fingerprint_v2` and, for migrated generations, `migrated_from`. |
| Python API | `Engine(config).search/answer/ingest` | same names kept as thin wrappers over `pipeline/engine.py`; new `Engine.run(request, session=...)`, `Engine(config, components=[...])` | `SearchRequest`/`AnswerResult` keep existing fields; `Answer.claims`, `unsupported_constraints`, `coverage` are additive. |
| HTTP | `/search`, `/answer` | `/v2/...`; v1 paths served by a compatibility router for two minor releases | Response adds fields; never removes. |
| MCP | tools `search`, `answer` | same names, extended schemas | Additive. |
| Env variable prefix | `SU_*` | unchanged until the package is renamed | Open decision §9. |

---

## 6. Test and evaluation strategy

### 6.1 Tiers

The owner's four tiers map as: **unit** = unit; **contract (mocked)** = contract + parity;
**real-service** = conformance (real adapters) + services; **benchmark** = benchmark.

| Tier | Location | What | Dependencies | Where it runs |
|---|---|---|---|---|
| Unit | `tests/unit/` | pure logic: types, metadata schema, config, grounding fixtures, scope compiler/resolver, fail policies, answer contract, selection, guards incl. failure semantics, budget, dedupe, stats, classifier artifact rules | none | every push |
| Contract (mocked) | `tests/contract/` | components against `testing/provider_fakes.py` and the in-memory backend fake; review stage; HTTP TestClient; MCP in-process; three synthetic domains | none | every push |
| Conformance | `tests/conformance/` | `backend_conformance.py` parametrised over adapters; in-memory fake on every push, real adapters in the services job | containers | services job |
| Real-service | `tests/services/` | containerised PostgreSQL/pgvector, MySQL, OpenSearch, Elasticsearch, Neo4j, MongoDB; lexical agreement report; local OpenAI-compatible model server smoke (Ollama/vLLM) when available; GLiNER/spaCy CPU smoke | Docker, optional local models | nightly + on demand; isolated databases/index prefixes; never a pre-existing database |
| Parity | `tests/parity/` | Python ↔ HTTP ↔ MCP request/response schema parity | none | every push |
| Benchmark | `benchmarks/` (code) + the external test workspace designated in `AGENTS.md` (per agent/run subdirectory; data, results) | public datasets under the frozen protocol; original denominators; declared recipes; sanitised aggregates to `docs/VALIDATION.md` | external workspace, local models by default | manual, owner-authorised |

Rules carried from `AGENTS.md`: pytest temp/cache and model caches point at the external workspace;
no bytecode written into the repo; external tests use isolated resources; results are recorded with
their evidence level; no relevance claim from ingestion counts or tiny fixtures.

### 6.2 CI plan (`.github/workflows/`)

| Job | Trigger | Steps |
|---|---|---|
| `lint-unit-contract` | push, PR; matrix 3.11/3.12/3.13 | `ruff check src tests`; `pytest tests/unit tests/contract tests/parity tests/conformance -k fake`; schema drift (`config.schema.json` regenerated and diffed); wheel/sdist build; clean-venv install smoke (`tests/smoke_wheel.py`) |
| `services` | nightly, `workflow_dispatch`, label `services` on PR | docker services: `pgvector/pgvector:pg17`, `mysql:8`, `opensearchproject/opensearch:2`, `docker.elastic.co/elasticsearch/elasticsearch:8`, `neo4j:5-community`, `mongo:7`; `pytest tests/services tests/conformance`; image digests and the lexical agreement report written to the job summary for `VALIDATION.md` |
| `local-model-smoke` | manual | optional self-hosted runner with a local OpenAI-compatible server; per-role structured-output smoke; never paid providers |
| `docs` | push | link check; `docs/plans` and `VALIDATION.md` evidence-level lint (each results table row has an evidence column); hygiene denylist grep over `src/`, `docs/`, `examples/`; a rule that any numeric result quoted from a private evaluation carries the label "historical, private evaluation, not reproducible in this repository" |

Paid providers are never called in CI. CI uses its own temporary workspace (`$RUNNER_TEMP`), never
the owner's home path.

### 6.3 Evaluation harness outputs

Per run: manifest hash, per-question per-stage rank lists, supplied and cited evidence, decision
output with unsupported constraints and fail-policy outcome, review rounds, guard decisions,
coverage, usage/cost by role, latency percentiles, failures by category (schema, grounding, budget,
backend), and official vs semantic vs diagnostic metrics kept in separate columns. Paired runs
produce wins/losses/ties with exact McNemar p and bootstrap CIs over the same question set.

### 6.4 Regression scenario pack (measured negatives kept as findings, not defaults)

Findings marked † come from private evaluations on the historical support-search services; their
direction is carried, their numbers are not reproducible here and are not quoted.

| Scenario | Historical finding | Encoded check |
|---|---|---|
| `fusion_vs_union` † | RRF/min-max/z-score measured worse than per-arm union | union default; alternatives must not drop minority-arm records on the synthetic set |
| `source_hierarchy_minority_loss` † | strict source priority lost minority-source targets | opt-in diversity/priority must keep ≥1 slot for each present source kind unless configured otherwise |
| `trained_router_negative` † | classifier/LoRA routers not promoted | classifier agent default off; cascade must fall to rules/LLM on low confidence; harness compares against rules baseline |
| `cross_encoder_negative` † | generic rerank and answer-path rerank measured negative / reverted | reranker default off; enabling it must not change union membership |
| `kg_entity_boost_negative` † | entity-match boost measured negative | no score boost from entity match count; graph arm contributes paths/evidence only |
| `raw_vs_planned_complementary` † | raw and planned retrieved different complementary documents; neither dominated | both modes retrievable; harness reports complementarity, not a winner |
| `generator_schema_failure_vs_prompt_size` | public-data diagnostic 2026-10-12: schema rejection rose with prompt size (association) | role evidence budgets declared per recipe; harness records schema-failure rate by prompt-size bucket |
| `quote_mismatch_categories` | public-data diagnostic: case/whitespace aborts | grounding fixture pack reports exact/normalised/relocated/rejected counts |
| `unsupported_predicate` | public-data diagnostic: `at` → `hosted` | fact edge requires trigger; co_occurrence otherwise |
| `planner_fabrication` | public-data diagnostic: fabricated total, guessed identifiers → confident zero | unresolvable value → unsupported; unsupported total never a count; answer contract blocks count claims |
| `fail_open_no_counts` | derived from the above | under `raw_all_arms` an aggregate-intent question yields no count claim and a non-empty `unsupported_constraints` |
| `synthetic_qa_cache_not_evidence` † | pre-generated Q&A cache measured negative | cache arm hits never enter evidence; no pre-generated Q&A ported |

---

## 7. Traceability matrix

| # | Requirement (owner) | Module(s) | Phase | Acceptance test |
|---|---|---|---|---|
| R1 | Scalable, modular, harness-like architecture (sessions, memory, tools, actions first-class) | `core/component.py`, `core/registry.py`, `runtime/*` | 0, 1 | `tests/unit/test_component_lifecycle.py`; `tests/contract/test_runtime_session_memory.py` (memory never in evidence; principal binding; idempotent actions denied in serving; generation_changed) |
| R2 | Pluggable backends via adapter contracts | `backends/contracts.py` (frozen Phase 0), `capabilities.py`, `testing/backend_conformance.py` | 0, 1 | conformance suite parametrised over adapters; config rejects arm/backend capability mismatch and double storage ownership |
| R2a | OpenSearch adapter with operation set | `backends/opensearch/` | 1 | conformance in services job (container digest recorded); lexical agreement report |
| R2b | Elasticsearch adapter | `backends/elasticsearch/` | 1 | same |
| R2c | Generic SQL (SQLAlchemy) | `backends/sqlalchemy/` | 1 | conformance on SQLite/PostgreSQL/MySQL; unsupported dialect lexical → validation error |
| R2d | PostgreSQL incl. pgvector + FTS | `backends/postgres/` | 1 | conformance; HNSW filter-recall gate |
| R2e | Neo4j | `backends/neo4j/` | 1, 3 | conformance; predicate-supported two-hop traversal with provenance |
| R2f | MongoDB | `backends/mongodb/` | 1 | conformance (community server); Atlas vector flagged untested |
| R3 | Multiple pluggable extractors with typed outputs (entities, relations, events, records, memberships) via NER/POS/LLM/GLiNER/rules | `extraction/extractors/*`, `core/types.py::ExtractionOutput` | 0, 3 | contract tests per extractor with fake providers; GLiNER/spaCy real smoke; events/records schema tests |
| R3a | Grounding: span-anchored, normalised locate, fail-closed, endpoint containment | `extraction/grounding.py` | 0 | fixture pack incl. length-changing normalisation; property test; endpoint containment |
| R3b | Predicate-support checks | `extraction/predicate_support.py` | 0 | `unsupported_predicate` scenario |
| R3c | Optional validated ontology contract | `extraction/ontology.py` | 3 | unrelated ontology loads; unsupported predicate rejected/demoted |
| R4 | Providers: OpenAI, Anthropic, local; per role; native structured output; prompt caching | `providers/*`, `models.roles` | 1 | mocked request-shape contracts per provider; local server smoke per role; declared-but-unsupported capability rejected; `doctor --probe` |
| R5 | Declarative config selects pipeline/arms/extractors/decision; profiles; strict validation; generated schema | `core/config.py`, `pipeline/validate.py`, `profiles/`, `MetadataSchema` | 0, 1 | schema drift CI job; three-domain profile contract test; unknown key rejected; storage selectors |
| R6 | Serving: HTTP (streaming) and MCP (stdio, streamable HTTP) with parity | `serving/*` | 1, 2 | `tests/parity/`; SSE event-order test; real MCP stdio + streamable-HTTP round-trips; `listable` + ACL on values |
| R7 | Decision agent configurable (rules / classifier / LLM); explicit fail policy per type/stage with defaults; typed scope with unsupported-constraint reporting; no fabricated counts or guessed values | `decision/*`, `generation/contract.py` | 0, 1, 2 | every fail-policy row; `planner_fabrication` and `fail_open_no_counts` scenarios; classifier artifact test |
| R8 | Cache arm with invalidation tied to generation; cached answers never evidence | `arms/cache.py` | 2 | generation-switch miss test; `synthetic_qa_cache_not_evidence` scenario |
| R9 | Learning module: failure batch → proposed fixes per arm as reviewable artifacts, never auto-applied | `learning/*` | 4 | proposals written only under `proposals/`; `apply` requires human CLI flag and is not an Action; validation attaches paired results |
| R10 | pip-installable with optional extras per backend/provider | `pyproject.toml` | 1 | clean-venv install smoke per extra group |
| R11 | Exact-ID aliasing/pinning | `arms/exact_id.py`, `selection/pins` | 1 | pin precedence; no fuzzy alias inference |
| R12 | Selection/ordering policies | `selection/*` | 0, 3 | membership preserved under ordering; minority-source scenario |
| R13 | Phrase boost | `selection/phrase.py` | 3 | DF/boilerplate guard tests |
| R14 | Typed query-scope compiler on a typed metadata schema | `core/types.py::MetadataSchema`, `decision/scope.py`, `values.py`, `arms/structured.py`, `RecordStore` | 0, 1 | resolution-table rows; identical `ScopeResult` across exact backends; timezone/null/enum tests |
| R15 | Entity resolution / glossary | `extraction/resolution.py`, `glossary.py` | 3 | method-chain provenance; placeholder rule |
| R16 | Fuzzy quote verification with stored source spans | `extraction/grounding.py`, `generation/quotes.py` | 0, 3 | levels recorded; flag-not-drop at generation |
| R17 | Hierarchical communities + deterministic stats | `communities/leiden.py`, `stats.py` | 3 | seeded determinism; stats from declared fields only; stale/rejected report states |
| R18 | Incremental/resumable ingestion with ledgers and budgets | `ingestion/*` | 0, 2 | resume without inference; raw replies stored; USD cap reservation; byte-identical unchanged vectors; per-projection validation |
| R19 | Guard hooks (safety refusal, prompt-leak, exact-set/absence, output redaction) | `guards/*` | 2, 3 | per-guard tests with positive controls; failure semantics; `exact_set` uses exactness metadata |
| R20 | Sessions/ACL hooks | `runtime/session.py`, `acl.py` | 1, 2 | ACL scenario fixtures across all arms, hops, cache keys, value listings, facets |
| R21 | Structured trace/log export with cost accounting | `runtime/trace.py`, `budget.py`, `providers/pricing.py` | 0, 2 | masked export test incl. constraint text; usage-by-role test; price-table provenance recorded |
| R22 | Evaluation harness (paired tests, judges, replay, sealed holdouts) | `evaluation/*` | 4 | replay determinism; McNemar correctness on synthetic pairs; denominators rule |
| R23 | Release manifests/rollback | `ingestion/release.py` | 2 | tamper detection; one-command rollback; gc safety |
| R24 | Measured-negative items not ported as defaults; kept as regression findings | `evaluation/regression/` | 4 (scenarios), defaults from 0 | §6.4 pack; defaults asserted in `tests/unit/test_defaults.py` |
| R25 | Public-repo hygiene: vendor-neutral, no customer names/paths/prompts, no private-evaluation numbers | `profiles/neutral.yaml`, docs lint | all | docs job denylist grep and private-number label rule |
| R26 | Immutable generations + strict grounding preserved; migration/version gate; bounded reviewer loop retained | `compat/*`, `ingestion/release.py`, `pipeline/review.py` | 0, 2 | v1 config upgrade determinism and fingerprint round-trip; legacy-read gate; `migrate-snapshot` counts; review-stage tests |

---

## 8. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Breadth of Phase 1 (102 ed) | slip of first release | contracts frozen in Phase 0; adapters parallelisable; 8 ed buffer; streaming deferral available |
| Lexical/aggregation semantics differ across OpenSearch/ES/PG/Mongo/SQL | surprising ranking or exactness differences | capability flags (`approximate`), no score-parity promise, per-engine conformance plus agreement report |
| ANN with filters loses recall | silent relevance loss | exact by default; HNSW only behind a measured filter-recall gate |
| MongoDB vector needs Atlas | capability gap on community server | flagged capability; validation error if selected without Atlas |
| Local models and native structured output | schema failures rising with prompt size | role budgets in recipes; capability-aware mode; failures counted, never repaired silently |
| Neo4j generation cleanup and label growth | storage bloat | generation-scoped labels, `delete_generation`, gc after rollback window |
| MCP streamable HTTP SDK churn | serving breakage | pin SDK; parity tests; stdio always available |
| Classifier agent reintroduces a measured-negative path or unsafe loading | quality regression / code execution | default off; ONNX-with-sha256 or registered plugin only; cascade falls back; regression scenario |
| Learning module proposes spurious changes | config drift | artifacts only; validation attaches paired results; human `apply`, not an Action |
| Leiden dependency licensing (GPL) | distribution constraint | separate extra; `louvain` fallback; open decision |
| Privacy in traces/sessions/value listings | leakage | masked default export incl. constraint text; sessions store IDs/hashes; `listable` + ACL on values |
| Team size | calendar | critical path given; Phase 3 and Phase 2 can overlap after Phase 1 |

---

## 9. Open decisions for the owner

1. **Package and CLI name** (placeholder `<pkg>`); whether to keep the `SU_*` environment prefix for one
   compatibility release.
2. **Later backends and timing**: Qdrant, Kuzu, Milvus/Weaviate, LanceDB, MySQL-native FTS (Phase 5)
   — which, and in what order.
3. **Paid-judge policy**: whether the evaluation harness may call paid providers as judges, with what
   monthly cap, and whether paid extraction/generation may be used for benchmark runs.
4. **Classifier decision agent shipping policy**: interface + ONNX artifact contract only
   (recommended) vs a small reference ONNX model trained on synthetic data; both default off.
5. **ANN defaults**: exact everywhere (recommended for 2.0) vs HNSW enabled by default on
   OpenSearch/ES/pgvector behind the recall gate.
6. **OAuth2/API-key management**: outside core (recommended; `PrincipalResolver` hook) vs an optional
   in-package gateway module.
7. **Hierarchical communities dependency**: accept a GPL `leidenalg` extra, or implement Leiden-like
   refinement on NetworkX (slower, permissive).
8. **MongoDB vector**: Atlas-only flagged capability (recommended) vs none in 2.0.
9. **Async core**: keep threaded sync core for 2.0 (recommended) vs async rewrite (adds ~20 ed).
10. **Default evidence budgets per model class**: whether to ship tested recipes for one local model
    family (which one) alongside provider examples.
11. **Compatibility window**: two minor releases for v1 config/HTTP paths (proposed) or longer.
12. **Streaming in first release**: SSE + MCP progress are in Phase 1 as planned; confirm or defer to
    `2.0.0a2` to shorten the critical path by about one week.
13. **Apache-2.0 for profiles and example ontologies** (same licence as the package) — confirm.
14. **Real-provider smoke** (N5): authorise a capped (e.g. ≤ USD 5 per release) manual smoke of
    OpenAI/Anthropic native structured output and prompt caching so those capabilities can be recorded
    as real-service tested rather than contract-mocked.
15. **`2.0.0a1` scope vs "first release"** (N13): the owner's list names the cache arm and learning
    module without a release tag; the plan puts them in `2.0.0a2` and `2.0.0`. Confirm, or pull the
    cache arm into Phase 1 (+6 ed) — the learning module depends on the Phase 4 harness and cannot
    move earlier without a throwaway grader.
16. **Owner-authorised grounding re-run** (B8): approve one bounded local-model extraction re-run over
    the unchanged frozen public cohorts with raw replies cached, to measure the span-anchoring effect
    as external evidence.
