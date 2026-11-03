# V2 execution log

Branch: `v2/phase-0` (then `v2/phase-N`). Plan: `v2_implementation_plan.md` (rev 2, approved).
Each slice appends a dated entry: scope, what landed, deviations, test counts, follow-ups.
Owner decisions (plan §9) not yet taken are applied as the plan's recommended defaults and listed
under "Assumed defaults"; they can be reversed later.

## Assumed defaults pending owner decision (plan §9)

| # | Decision | Assumed |
|---|---|---|
| 1 | Package name | keep `su_multi_arm` import/CLI until renamed; no `SU_*` alias work |
| 2 | Later backends | Phase 5 only |
| 3 | Paid judges / paid benchmark calls | none; local models only |
| 4 | Classifier agent | interface + ONNX artifact contract, no weights, default off |
| 5 | ANN defaults | exact everywhere |
| 6 | OAuth2 | outside core; `PrincipalResolver` hook only |
| 7 | Hierarchical communities | NetworkX-based refinement (permissive licence); no GPL extra |
| 8 | MongoDB vector | Atlas-only flagged capability |
| 9 | Async core | threaded sync core |
| 10 | Local-model recipes | one tested OpenAI-compatible local recipe, documented as example |
| 11 | Compatibility window | two minor releases |
| 12 | Streaming | in Phase 1 as planned |
| 13 | Apache-2.0 for profiles/ontologies | yes |
| 14 | Real-provider smoke | not run (no paid calls) |
| 15 | `2.0.0a1` scope | cache arm in Phase 2 as planned |
| 16 | Grounding re-run on frozen cohorts | not run; awaiting owner |

## Slices

### 2026-11-03 — P0/S1 grounding + predicate support — DONE
Landed: `grounding.py`, `predicate_support.py`, span-anchored `GroundedExtraction`, catalog columns
(offsets, match_level, edge_kind, support), `index_format` 3, graph arm traverses `fact` only,
Neo4j adapter exports facts only, config `extraction.grounding` / `predicate_triggers` /
`stemmer_language`, `snowballstemmer` core dependency. Tests 40 → 64 passed. Docs: ARCHITECTURE,
CONFIGURATION, VALIDATION, schema. Not yet: `relocated` level, fact dedupe across inherited title
chunks, quarantine/partial policies (→ P0/S3).

### 2026-11-03 — P1/prep real-service test infrastructure — DONE
Landed: `tests/services/docker-compose.yml` (OpenSearch 2.19.1, Elasticsearch 8.17.4,
pgvector 0.8.0-pg17, Neo4j 5.26.0-community, Mongo 7.0.16; project `su-multi-arm-tests`; loopback-only
non-default ports from `SU_TEST_*_PORT`; security off on the two Lucene engines; 512 MiB heaps;
tmpfs data dirs, no volumes; healthchecks), `tests/services/wait_ready.py` (stdlib protocol probes +
readiness table), `tests/services/README.md`, `.github/workflows/services.yml` (nightly /
dispatch / PR label `services`; `pytest -m external`; digests to job summary; `down -v` always).
Verified locally: cold pull 17 min, healthy within 11 s of creation, all probes ready, 3 existing
`external` tests passed against the stack, clean teardown, `docker ps` unchanged. Details and
digests in `docs/VALIDATION.md` ("Real-service test infrastructure — 2026-11-03").
Deviations: tags pinned to exact versions rather than the plan's major floats; MySQL omitted (not a
first-release backend); the CI job runs `-m external` today because `tests/services/` and
`tests/conformance/` suites do not exist yet — switch its pytest target when they land. No src/ changes.
Follow-ups: adapters + conformance kit (R2a–R2f) consume `SU_TEST_OPENSEARCH_URL`,
`SU_TEST_ELASTICSEARCH_URL`, `SU_TEST_MONGO_URI`; add `opensearch`/`elasticsearch`/`mongodb`
extras to the workflow's install step once they exist; re-measure Neo4j under the 1.5 GiB cap.

### 2026-11-03 — P0/S2 kernel (types, metadata schema, component/registry, config v2, backend contracts, runtime) — DONE
Landed: `core/` (types incl. `MetadataSchema`, component + artifact rules, registries + entry-point
plugins, config v2 + `validate_config`/`load_config`, schema helper, errors), `compat/config_v1.py`
(v1 model moved; `config.py` re-exports; `upgrade`/`downgrade`/`upgrade_text` with mapping table),
`backends/` (eight frozen protocols, capability matrix with `contracts|legacy|planned` status,
in-memory fake satisfying every protocol), `runtime/` (RunContext, Budget + PriceTable, Trace with
masked export, SessionManager, Memory, ToolRegistry, ActionRunner + audit, Principal/AccessPolicy),
CLI `config upgrade|validate|schema`, `docs/config.schema.json` regenerated as v2 and
`docs/config_v1.schema.json` added. Tests 62 → 154 passed (same interpreter, `mcp` absent → 2 skips).
Deviations / interpretations: (1) `Catalog.get_chunks` and `LedgerStore.get_raw` added to the
listed contract methods; `LexicalIndex.search` returns `PageResult[ScoredChunk]` (items + cursor)
so paging is explicit. (2) Adapters expose per-capability facets (`adapter.catalog`, ...) because
the plan's protocols share method names. (3) `Scope` (eligibility pushdown) is distinct from
`ScopeSpec` (typed structured query). (4) `MetadataSchema.unknown_fields` (`allow` default) added so
undeclared metadata stays untyped rather than failing ingestion. (5) `structured_output: native`
is always rejected until a provider declares a `(provider, model)` pair; `profile` accepts only
`neutral`; `extraction.policy` accepts only `strict`; planned backend kinds are rejected; all with
explicit messages. (6) Engine still consumes v1 `su_multi_arm.config.Config`; v2 `Config` lives at
`su_multi_arm.core.config.Config` until the arms/write-path slices switch the engine. (7) v2
`fingerprint()` hashes the effective config; the recipe-composed index fingerprint waits for
components (`legacy_fingerprint()` covers N6 meanwhile). (8) Fingerprint round-trip is exact for all
examples; it would differ only for a v1 file that set `community`/`extraction` model fields or
`community.evidence_char_budget` without a provider (settings that had no effect).
Follow-ups: sqlite adapter on contracts (S3); arms/tools on `RunContext`; `Engine(config,
components=[...])`; CI schema-drift job; `config upgrade` for prompt files inlines content (v1
loader semantics) — the written v2 file keeps `prompt_file` only when the v1 file used it unresolved.

### 2026-11-03 — P0/S2-fix kernel review fixes (branch `v2/p0-s2-fix`) — DONE
Review: `docs/plans/reviews/p0_s2_kernel_review.md` (REVISE). Fixed B1 (scanner accepts `name:tag`
model ids and bare `.onnx` names; dotted factories/pickles still rejected), B2 (per-(adapter,
capability) status in `backends/capabilities.py`; `Config._require_capability` rejects `planned`
pairs and graph backends without `memberships` for community slots), B3 (plan rev 3: §3.1a
`unknown_fields`, §3.5 frozen method list/`PageResult`/namespaces/`Scope` vs `ScopeSpec`/facets/
per-capability status, §5 narrowed N6; `upgrade()` emits `lossy_upgrade` diagnostics, `--strict`
refuses). Non-blocking fixed: N1 (`ChatRole.price_key()`), N2 (extra masked keys), N3 (page cap =
candidates cap, `BackendError`), N4 (fake honours `unknown_fields: reject`, `field_not_filterable`),
N5 (unknown vector id → `BackendError`), N6 (datetime `range` bounds), N7 (`SessionNotFound`), N8
(`get_chunks` asserted; tautological memory test replaced), N10 (fingerprint docstring), N14
(interpreter recorded). Tests 156 → 169 passed (`.venv`, `mcp` present).
Follow-ups (not done): N9 support-domain defaults in `ExtractionConfig.labels`/`field_order` move to
the `support_example` profile with the profiles slice; N11 decide sqlite vector namespaces before S3;
N12 budget overrun trace event; N13 aggregate all pydantic errors in `validate_config`; §2.1.7
hits-bounded catalog-read conformance check needs an arm (S3/arms).


### 2026-11-03 — P0/S3 decision agent, typed scope, answer contract, review stage, selection split — DONE
Branch `worktree-agent-a1a8201a5f0aed248` (commit `893e668`), to be merged by the coordinator.
Landed: `decision/` (`intent.py` pre-pass, `base.py` request/plan/decision types + fail-policy
machinery, `values.py` ValueResolver, `scope.py` ScopeCompiler, `rules.py`, `classifier.py`,
`llm.py`, `cascade.py`), `selection/` (`types`, `collapse`, `limits`, `union`, `ordering`,
`diversity`, `pins`, `packing`, `select()` chain), `generation/contract.py` (Claim/Answer/
AnswerResult/AggregateEvidence/AnswerContract), `pipeline/review.py` (ReviewStage, Review,
JsonModelReviewer, REVIEW_PROMPT moved), `core/config_decision.py` (`StagePolicies`),
`core/config_selection.py` (`OrderingKind`, `PackingConfig`); `core/config.py` touched for two
imports and three fields (`decision.stage_policies`, `selection.ordering` kinds/default,
`selection.packing`); `compat/config_v1.py` ordering map; `backends/memory.py` date-field
`date_interval` bounds; `tests/conftest.py` v2 fixtures; schema regenerated; CONFIGURATION,
ARCHITECTURE, VALIDATION updated. Tests 156 → 300 passed (144 new: `tests/unit/test_{intent,
scope_values,fail_policies,classifier_artifacts,llm_agent,answer_contract,selection,count_probes}.py`,
`tests/contract/test_review_stage.py`, `tests/parity/test_ordering_parity.py`, one config test).
Deviations / interpretations: (1) `selection.ordering` default is `reciprocal_rank` (plan §3.9) and
the v1 map is `per_arm → reciprocal_rank`, `rrf → rrf`; S2 had `per_arm → per_arm`, `rrf →
reciprocal_rank`, which would have changed the formula of an upgraded `rrf` config. `per_arm` stays
accepted as a deprecated alias; `best_arm_rank`/`stable_interleave` have no v1 form and `downgrade`
rejects them with a key path. Two S2 config assertions were updated accordingly. (2) The nine
HybridQA probe shapes are not enumerated in the repo (VALIDATION.md records only "0/9"); they are
reproduced from the two documented failure modes (invented identifiers → confident zero, numeric
total answered with a record count) plus seven deterministic variants, listed in
`tests/unit/test_count_probes.py`. (3) `rules` binds predicates only from observed values of
listable fields / enum values occurring verbatim (unit words excluded) and from request filters; any
other qualifier fails the plan. The plan's "quoted strings → phrase boost" is exposed as
`Intents.quoted_phrases` for the lexical arm; `RetrievalStep` has no phrase field. (4) The classifier
ships as interface + artifact contract; there is no ONNX inference dependency, so a configured
classifier fails `validate()` unless a `ClassifierModel` is injected via `Dependencies.components`.
(5) `AnswerContractConfig.on_violation` keeps S2's `fail` literal (plan says `error`). (6) The review
stage treats a `raw_all_arms` re-plan as `replan_abstained`. (7) `date_interval` on a `datetime`
field under `require_offset` rejects calendar-shaped bounds (`2024-03`) with `timezone_missing`;
`assume_utc` or `date` fields accept them. (8) The two v1 agentic tests moved with S4 to `tests/contract/test_engine_support_example.py` (S3 wrote "stay in `tests/test_core.py`"; that file was replaced)
against the v1 engine; their intents are reproduced in `tests/contract/test_review_stage.py` and the
originals can be retired when the engine is ported. (9) `selection/phrase.py` not created (lexical
arm concern). (10) `DecisionRequest` is a slice-local request type until the engine defines the v2
request; `Capabilities` carries enabled arms, metadata schema, step cap and id patterns.
Follow-ups: engine wiring (`decide` → `select` → `ReviewStage.run` → `AnswerContract.enforce`);
`exact_set` guard (Phase 2) screening prose numerals; `profiles` prompts for the planner; a bare
relative artifact filename such as `model.onnx` is rejected by the kernel's dotted-path rule
(`core/component.py::DOTTED_IMPORT`) — the kernel-fix branch's scanner change should cover it.

### 2026-11-03 — P0/S4 storage + arms on the contracts, write path with policies, neutral profiles — DONE
Commits `v2(phase-0/s4a)` and `v2(phase-0/s4b)`. Landed: `backends/sqlite/` (all eight capabilities
over `snapshots/<gen>/catalog.sqlite` + vector files + `runtime.sqlite`; schema a superset of the v1
tables, `index_format` stays 3), `backends/_scope.py` (shared predicate/measure semantics),
`backends/extras.py` (non-frozen extension protocols: counts, `find_entities`/`mentions`, community
objects), `testing/backend_conformance.py` + `tests/conformance/` (kit v0 over memory, sqlite,
sqlite+faiss), `ingestion/{write,policy,release}.py` (single write path, per-projection validation,
failure manifest, old generation stays active, strict/quarantine/partial with coverage, ledger reuse +
raw replies), `extraction/` package (v2 `BuiltinExtractor` on core types, `relocated` level,
`dedupe.py`; S1 modules moved with shims), `communities/{base,reports}.py`, `arms/` (five components),
`engine.py` on config v2 with `Engine(config, components=[...])`, `profiles/` (`neutral`,
`support_example`, neutral prompts, voices), `core/request.py`, CLI on v2 (`ingest --allow-partial`).
Tests 156 → 229 passed (`tests/test_core.py` replaced by `tests/contract/test_engine_support_example.py`
under `support_example`; `test_relative_config_paths` covered by `test_config_v2.py`); wheel/CLI smoke
passed. Docs: CONFIGURATION, ARCHITECTURE, VALIDATION, schema regenerated.
Deviations / interpretations: (1) `Relation.occurrences` (list of `Occurrence{span, provenance}`)
added to the kernel type for dedupe. (2) Per-chunk `ExtractionOutput` uses mention-level entity ids
(canonical key + chunk + offsets) so the endpoint-containment invariant holds; the write path
canonicalises to `entity_key(type, name)` before storage; relation endpoint mentions are stored as
their own span rows. (3) Graph/community arms and projection validation need operations outside the
frozen protocols; they live in `backends/extras.py` as separately named protocols (candidates for
the §3.5 changelog), not in `contracts.py`. (4) Community objects are stored through the graph facet
(`put_communities`/`communities`), report statuses keep today's values (`disabled`, `partial`,
`complete_input`, `rejected`, `pending`). (5) `core/config.py` edits beyond "one import + one field":
`extraction.policy` no longer rejected, `extraction.prompt`, `review.prompt`,
`generation.evidence_fields`, `pipeline.allow_incomplete_arms`, `request: RequestConfig`, neutral
defaults (`labels`, `field_order` empty; `gliner`/`llm` require labels), profile overlay applied in
`validate_config`. (6) `SearchRequest.record_context` keys are validated by the engine against
`request.context_keys` (wire model keeps only a size bound). (7) v1 configs upgrade to
`profile: support_example` (v1 had those conventions built in); `downgrade` maps overlay-equal
prompts back to `None` so N6 round-trips hold. (8) `postgres`/`neo4j` validate but the engine refuses
them (typed error) until Phase 1; `tests/test_external.py` is skip-marked with that reason.
(9) `JsonModel.call_text` added (raw reply for the ledger); `call` validates on top of it.
(10) Aggregate `denominator` is the eligible population (ACL + request filters), not the ACL-only corpus.
(11) Lexical phrase boost ships with a simple DF guard (skip when the phrase matches more than half
of the term hits). (12) `AnswerResult.coverage` is carried in `trace["coverage"]` until
`generation/contract.py` lands (other agent's slice).
Merge note for `v2/p0-s2-fix`: `backends/capabilities.py` here marks `sqlite` as `status="contracts"`
(and `tests/unit/test_backends.py` expects `{"memory", "sqlite"}`); under the per-(adapter,
capability) `CapabilityDecl` layout every sqlite capability should become `contracts`. The engine
selects adapters by kind (`sqlite`, `memory`), not by matrix status, so the merge order is safe.
Follow-ups: promote `extras` operations into the contracts via the §3.5 changelog; swap the engine's
decision/fusion/answer paths for `decision/`, `selection/`, `generation/`, `pipeline/review.py`
(move the two agentic tests to `tests/contract/test_review_stage.py`); recipe-composed fingerprint
from every component (`WritePath.index_fingerprint` composes extractor/embedder/chunking/communities
today); Phase 1 ports of postgres/neo4j onto the kit; `examples/*_v2.yaml` once the engine no longer
needs v1 files for the smoke script.

### 2026-11-03 — Merges: `v2/p0-s2-fix` and S3 (`worktree-agent-a1a8201a5f0aed248`) into `v2/phase-0` — DONE
Commits `v2(phase-0): merge kernel fixes (v2/p0-s2-fix)` and `v2(phase-0): merge
decision/selection/contract/review (S3)`. Tests: 242 after the fix merge (229 + 13), 386 after the
S3 merge (+ 144); ruff clean; `docs/config.schema.json` regenerated, drift-free.
Judgement calls: (1) `backends/capabilities.py` is the fix side's per-(adapter, capability)
`CapabilityDecl` layout with every `sqlite` capability `contracts` (memory `contracts`; postgres
vector + lexical and neo4j graph `legacy`; the rest `planned`); `ARM_CAPABILITY` is now
`(capability, flag)` and `arms/base.py` reads the first element. (2) The fix side's record-store
gating (`unknown_fields: reject`, undeclared/non-filterable predicates → `field_not_filterable`,
`missing` on non-nullable → `field_not_nullable`, datetime/date range bounds → `unparseable_bound` /
`timezone_missing`) and the record-identity rule now live in `backends/_scope.py` and apply to the
memory fake **and** the sqlite adapter alike; the page-size cap is `core/types.py::Page` (`le=10000`)
and each adapter converts an over-limit page into a `BackendError`; the unknown-vector-id error is
implemented per adapter; S3's date-field
`date_interval` handling is subsumed by that helper. (3) Fix-side `RECORD_KEYS` (`id`, `dataset`,
`kind` bypass the schema) collided with the S3 fixture that declares `kind` as a non-nullable enum:
identity keys now bypass the schema only while it does not declare them. (4) `compat/config_v1.py`
keeps the fix's lossy-upgrade diagnostics/`strict`, S3's ordering map (`per_arm → reciprocal_rank`,
`rrf → rrf`, `ORDERING_BACK` accepts both) and S4's `profile: support_example` mapping with prompt
round-trip. (5) `fusion.FusionLike` maps v2 `reciprocal_rank`/`per_arm` to the alpha's per-arm
formula and `rrf` to rrf; `best_arm_rank`/`stable_interleave` raise a typed `ConfigError` at engine
construction until `selection/` is wired (next slice; decision/contract/review/selection are not
wired into the engine by these merges). (6) `tests/conftest.py` is one file: S4's support_example
engine fixtures plus S3's `v2_*` synthetic fixtures. (7) Docs keep every side's entry in the order
S2-fix, S3, S4.

### 2026-11-03 — P0/S5 engine integration: harness end to end, serving parity — DONE
Commit `v2(phase-0/s5)`. Landed: `engine.py` rewritten on the harness (`RunContext` → intent pre-pass
→ `DecisionAgent` built from `pipeline.decision` (`rules`/`llm`/`classifier`/`cascade`) → arms →
`selection.select`/`pack` → `ReviewStage` when enabled → generation → `AnswerContract.enforce`;
explicit v1 `aggregate` mapped to `DecisionRequest.scope`; `Engine(config, components=[...])` also
accepts a decision-agent instance, a reviewer object and a classifier runtime); v1 planner/fusion/
agentic paths and the `best_arm_rank`/`stable_interleave` stubs removed (`fusion.fuse` kept as the
parity oracle only); `models.py` owns the wire `Answer`/`Claim`/`Coverage`/`Violation`/
`UnsupportedConstraint` (contract and `core.types` re-export them), `AnswerResult` + `SearchResult`
gain additive fields, v1 `Plan`/`PlanStep`/`Review` retired; `serving/parity.py` + typed `mcp.py`
tools with a start-up drift check; HTTP `/v2/search`, `/v2/answer`; `doctor` reports a construction
error instead of a traceback; neutral prompts: one planner prompt (`profiles.prompts`, used by
`decision/llm.py`), review prompt shared with `pipeline/review.py`, generator prompts ask for
`claims`. Tests 386 → 429 passed (`tests/contract/test_engine_harness.py` 41,
`tests/parity/test_request_parity.py` 2; `test_engine_support_example.py` llm/agentic/generation
tests ported to `LlmPlan`/`pipeline.Review`/contract outcomes, `test_per_arm_preserves_union...`
replaced by the engine-level ordering test; `test_domains.py` planner payload keys). Docs:
ARCHITECTURE ("Retrieval and selection semantics", "Model flow", request flow diagram),
CONFIGURATION, VALIDATION; schema regenerated (no drift — `Config` unchanged).
Deviations / interpretations: (1) An explicit caller `aggregate` bypasses the `ScopeCompiler` and
runs as given (the arm allowlist and the record store's `unsupported` cover it): the compiler is
stricter than the backend about declared types (v1 upgrades declare `created_at` as `string`, which
would have refused `first_seen`), and a typed request is not a model-invented value. With the
`rules` agent it is added to the plan next to the raw retrieval steps (today's raw mode); with
`llm` the engine appends it when the planner's plan has no structured step. (2) `DecisionRequest`
gained `scope` and `canonical_record`; the llm payload gained `canonical_record`/`explicit_scope`
next to the unchanged `caller_context_unverified`. (3) Effective packing priority =
`selection.packing.field_priority` + `generation.evidence_fields` + chunking order; the generator's
`evidence_budget.chars` overrides `packing.budget_chars` when set (v1 compat). (4) A planner failure
(disabled arm, request-filter override, undeclared filter field) is no longer a `ValueError`: it is
an abstention with the category in `trace.decision` and the constraint in
`unsupported_constraints`; a generator citing unsupplied evidence abstains with `unknown_evidence`
(default) instead of raising. (5) `arm_hits` keeps a key for every arm that ran (v1 behaviour)
even when empty. (6) `AnswerResult.coverage` is set when the manifest policy is not `strict`, a
stage is incomplete or the status is `partial`; `trace.coverage` keeps the raw manifest coverage.
(7) `LlmAgent.validate` sees the process environment (`Environment(env=os.environ)`), so a planner
role with an unset `api_key_env` fails engine construction (fail closed; `doctor` reports it).
(8) `/v2/*` HTTP paths are aliases of the existing routes; streaming, `capabilities`,
`list_values`, `lookup_record` stay Phase 1. (9) The nine probes and the two agentic intents are
asserted on declared claims; prose numerals are Phase 2.
Follow-ups: cascade and classifier through the engine with an injected runtime; exact_id arm +
alias map → `Capabilities.id_patterns`; `generation/generator.py` as a component (the engine calls
`JsonModel` directly through `BudgetBridge`); `tests/smoke_wheel.py` still asserts the old MCP tool
set only by name (unchanged); `docs/plans/reviews/p0_s3_s4_review.md` (untracked, another agent's)
not touched.

### 2026-11-03 — P1 MongoDB adapter on the contracts (branch `worktree-agent-abd12a2ec9a5ab040`) — DONE
Commit `v2(phase-1/mongodb)` (`f214fc7`), to be merged by the coordinator. Landed:
`backends/mongodb/` (`adapter.py` MongoBackend with six facets, `_db.py` naming/typed rows/predicate
compilation/text-search strings/error mapping, `catalog.py` Catalog + RecordStore, `search.py`
text-index LexicalIndex with `$lookup` scope pushdown, `runtime.py` cache/ledger/sessions with TTL
indexes, `registry.py` MongoGenerationRegistry with an atomic `CURRENT` pointer document and a write
lease); `backends/capabilities.py` mongodb rows `contracts` for catalog/lexical/records/cache/ledger/
session, `vector` kept `planned` (assumed default #8: Atlas-only, never claimed against community
MongoDB), no graph; `testing/backend_conformance.py::check_protocols` compares only non-`planned`
declarations; `engine.py` builds `kind: mongodb` via `MongoBackend.from_config`; pyproject extra
`mongodb` (`pymongo>=4.6,<5`), services workflow installs it; `tests/conformance/
test_mongodb_conformance.py` (16 `external` tests: kit per capability + `run_all`, collection naming
and isolation, lexical predicate pushdown, cardinality cap, TTL indexes, registry pointer/failure/
lock, `from_config`, unreachable server); `tests/unit/test_mongodb_adapter.py` (12: compilation,
naming, paging, text strings, fake-client error mapping). Docs: CONFIGURATION (mongodb row, kinds),
ARCHITECTURE, VALIDATION (real-service entry with digest and version), services README.
Tests: non-external 386 → 398 passed; external `-k mongo` 16 passed against `mongo:7.0.16`
(`sha256:c630c593…`), ruff clean, `docs/config.schema.json` drift-free (Config unchanged).
Deviations / interpretations: (1) measures and groups are computed over the matched rows with the
shared `_scope` helpers (sqlite does the same; rows are fetched anyway for `matched_ids`), so the
plan's "aggregation pipeline" is used for predicates, population counts, `exists` and `values`
only; results are identical to sqlite/memory by construction. (2) `values` refuses fields with more
than `max_values_cardinality` (10000) distinct values instead of truncating silently. (3) Session TTL
index only when `session_ttl_seconds` is passed on the Python API (the contract's `expire()` is the
explicit path; config has no such field). (4) `lock()` takes a lease document; a held lease is a
typed error, a lease older than 600 s is taken over (documented). (5) Lexical scores are MongoDB
`textScore`, documented as not BM25; `analyzer_id` is `mongo_text_english`. (6) The kit's
`check_protocols` change was needed because mongodb legitimately declares a `planned` vector row;
the memory/sqlite suites are unaffected. (7) The ground-rules schema command imports the v1
re-export `su_multi_arm.config.Config`; the checked-in file is the v2 `core.config.Config` schema
and was regenerated from that (unchanged). (8) The external suite defaults `SU_TEST_MONGO_URI` to
the stack default and skips with a reason only when the variable is unset and the default is
unreachable; an explicitly configured unreachable server fails.
Follow-ups: engine end-to-end on a mongodb catalog needs the multi-backend write path slice
(`generation_registry()` is ready); Atlas vector search remains unimplemented and untested; cross-
engine lexical agreement report (`tests/services/test_lexical_agreement.py`) once the Lucene
adapters land; the `.venv` editable install points at the main checkout, so worktree runs need
`PYTHONPATH=<worktree>/src`.

### 2026-11-03 — P0 review fixes for S3/S4 (branch `v2/p0-review-fix`, base `9f26b88`) — DONE
Fixes all four blocking findings of `docs/plans/reviews/p0_s3_s4_review.md` and the cheap
non-blocking ones (N1, N2, N4, N5, N6, N7, N9, N10); see `docs/VALIDATION.md` for the per-finding
summary. Tests 386 → 418; conformance 31/31 on memory, sqlite, sqlite_faiss; ruff clean; no schema
drift. Judgement calls: (1) the storage rule for temporal values is "naive = UTC" on both adapters,
which is sound because `require_offset` rejects naive values at ingestion, so a naive value can
only come from an `assume_utc` field (an undeclared field that happens to hold an ISO stamp gets the
same reading); document-scope predicates (no schema) compare instants without date truncation,
typed records truncate for `date` fields. (2) `detect_intents(query, id_patterns, targets=None)`
keeps the permissive grammar when no targets are given (schema-less callers); agents always pass
`Capabilities.structured_targets()`; targets are whole phrases, never split into words.
(3) `ReviewConfig`/`ReviewStageConfig` keep `ge=1` but reject `enabled` with `max_hops < 2`
(a v1 `routing.max_hops: 1` file still upgrades when review is disabled). (4) `remaining` in
`coverage.stages` stays in units (chunks/communities); `documents_not_processed` lists the documents.
Left to the integration slice: N3 (records facet convention), N8 (single planner prompt),
N11 (`zero_match_fields` on `AnswerResult`), N12 (relocation-aware reuse key).
### 2026-11-03 — P1/opensearch OpenSearch adapter on the frozen contracts — DONE
Branch `worktree-agent-a1c794b75ac6aff4c` (commit `v2(phase-1/opensearch)`), to be merged by the coordinator.
Landed: `backends/opensearch/` (`OpenSearchBackend` with `catalog`, `lexical`, `vector`, `records`
facets; `transport.py` over `opensearch-py` with `max_retries=0`), `backends/_lucene_common.py`
(index naming, typed field layout `s/n/b/t/v/p`, predicate → DSL, scope → `bool.filter`, lexical
templates, paging, `Transport`/`TransportError`, bulk checks; shared with the future Elasticsearch
adapter), matrix rows (`opensearch`: four capabilities `contracts`, flags `analyzer_id:
lucene_standard`, `highlight`, `field_boost`, `ann: hnsw_optional`, `max_dimensions: 16000`,
`namespaces`, `exactness: exact`, `max_matched: 10000`), engine wiring (`kind: opensearch` →
`OpenSearchBackend.from_config`, env-var hosts), pyproject extra `opensearch = ["opensearch-py>=2.8,<4"]`,
services workflow installs it, `tests/unit/test_opensearch_backend.py` (fake transport, 14),
`tests/conformance/` parametrised over `opensearch` / `opensearch_hnsw` (marked `external`, random
`su_test_<hex>` prefix per test, test-side cleanup), `tests/services/test_opensearch_adapter.py` (6),
docs (CONFIGURATION backend row + "OpenSearch semantics", ARCHITECTURE, services README, VALIDATION).
Real-service result against `opensearchproject/opensearch:2.19.1` (digest in VALIDATION.md):
conformance 12 passed / 8 skipped (undeclared capabilities) / 0 failed; adapter checks 6 passed.
Tests 386 → 401 passed (`-m 'not external'`), ruff clean, schema drift-free.
Deviations / interpretations: (1) graph/cache/ledger/session are **not declared** for `opensearch`
rather than `planned`: the plan's §3.5 table says "no" for them, and a `planned` declaration would
make the derived adapter status `legacy` (meaning "v1 code serves it"), which is false; configuration
naming them fails with "does not declare capability". (2) Records exactness is `exact` via native
filter pushdown + a bounded id-ordered scan feeding `_scope.scope_result`, not the plan's approximate
`cardinality`/`date_histogram` aggregations: the frozen `ScopeResult` carries `matched_ids`, so the
scan is unavoidable and the measures then come for free and identical to sqlite/memory; a scope above
`max_matched` (10000) is a typed `BackendError`, never a truncated result. (3) `knn.exact_rescore:
false` with `method: hnsw` is refused at construction (ANN scores are engine-normalised, not cosine);
HNSW uses the Lucene engine with the eligibility filter inside the graph search and oversamples
`max(50, 4k)` candidates. (4) Per-generation identity is the three generation-named indices; the
plan's "per-generation alias" is interpreted as the dataset-level aliases
`<prefix>_<dataset>_{docs,chunks,records}` switched atomically by `activate(gen)`; `delete_generation`
refuses an aliased or protected generation. (5) Chunk documents carry the parent document's typed
fields (denormalised at write; re-putting a document re-derives its chunks with `_update_by_query`,
`conflicts: abort`) so lexical/vector scope is a native filter without a join. (6) `casefold_match`
is Lucene `lowercase`, not Python `casefold`. (7) Catalog `iter_scope`/counts refresh before reading
and records write with `refresh=true` (RecordStore has no `refresh`); lexical/vector writes need
`refresh(gen)` as the contract says. (8) Conformance tests for adapters that declare a subset of the
capabilities skip the undeclared checks explicitly (`tests/conformance/test_backend_conformance.py`).
(9) The container `su-multi-arm-tests-opensearch-1` was started alone (another agent's `mongo`
service shares the compose project), so teardown removed only the `opensearch` service, not `down -v`.
(10) Tests must run with `-o pythonpath=src` from a worktree: the `.venv` editable install points at
the main checkout.
Follow-ups: call `backend.activate(gen)` from the multi-backend write path's release step (Phase 1
row "Multi-backend write path"); `doctor --probe` should call `OpenSearchBackend.probe()`; the
cross-engine lexical agreement report (`tests/services/test_lexical_agreement.py`); an HNSW
filter-recall gate on a larger fixture before recommending `method: hnsw`; Elasticsearch adapter
reusing `_lucene_common.py` (`dense_vector` mapping, `knn` top-level search, ES error shapes).


### 2026-11-03 — P1 model providers (branch `worktree-agent-ac4c7bd31a141cbc7`, base `783d821`) — DONE
Commit `v2(phase-1/providers)` (`5bf8f55`), to be merged by the coordinator. Landed: `providers/`
package replacing `providers.py` (`__init__` re-exports `JsonModel`, `Embedder`, `CallBudget`,
`tokens`): `base.py` (typed `ProviderError` categories, `SchemaViolation(ValueError)` with the raw
reply, normalised `Usage`, `CallEstimate`, `CallMeter`, `RoleSpec`, `ChatProvider` protocol,
strict-schema conversion, single-request `HttpCall`), `capabilities.py` (registry keyed
`(provider, model-pattern)`: native flag and kind, request modes, prompt caching, output-token and
reasoning parameter names), `anthropic.py`, `openai.py`, `openai_compatible.py`, `none.py`,
`embeddings.py` (`hash`, `sentence_transformers`, `openai`, `openai_compatible`), `accounting.py`
(`RunBudgetMeter`: Budget calls/tokens/USD + `model_call` trace events), `json_model.py`.
`core/config.py`: `ChatRole.provider` adds `openai`, `reasoning_effort`, registry-backed validation of
`structured_output`/`prompt_caching`/`reasoning_effort`/openai token parameter, `openai` requires
`api_key_env`; `EmbedderRole.provider` adds `openai`. `runtime/budget.py`: `ModelPrice.
cache_write_input_per_million`, `PriceTable.cost(..., cache_write_tokens)`, USD reserved whenever the
table prices the key. `decision/llm.py` (`_BudgetBridge`/`BudgetBridge` are aliases of
`RunBudgetMeter`; `ProviderTimeout` → `timeout`, `SchemaViolation` → `schema_violation`),
`pipeline/review.py`, `engine.py` switched to `RunBudgetMeter`; `compat/config_v1.py::downgrade`
refuses `openai` roles. Tests 473 → 558 passed (+85: `tests/contract/test_providers.py`,
`tests/unit/test_provider_capabilities.py`); `tests/services/test_local_llm.py` (`external`, skipped:
`SU_TEST_LOCAL_LLM_URL` unset; no model server touched). Ruff clean; schema regenerated.
Docs: CONFIGURATION (providers section with Anthropic, OpenAI and local examples), ARCHITECTURE,
VALIDATION (hosted providers contract-mocked only).
Deviations / interpretations: (1) `CapabilityDeclaration.structured_output` default changed from
`json_schema` to `none`: `none` is exactly the alpha's wire behaviour (prompt schema + strict
validation), whereas keeping `json_schema` would have started sending `response_format` to every
existing `openai_compatible` server and is meaningless for Anthropic (rejected there now). The
effective-config `fingerprint()` of v2 configs changes accordingly (it is recorded, not checked; the
index fingerprint is unaffected). (2) Semantics per mode: `native` = provider-enforced (Anthropic forced
tool use; OpenAI `strict: true`), `json_schema` = `response_format` with `strict: false`,
`json_mode` = `json_object`; the last two are OpenAI-style only. (3) OpenAI native with a schema strict
mode cannot express (free-form `dict` fields, e.g. `LlmPlan.filters`) is a `CapabilityMismatch` before
any request — no silent non-strict fallback; such roles declare `json_schema`. (4) Anthropic native uses
tool use, not the newer `output_format` structured-outputs beta (stable, widely available; recorded in
the registry as `native_kind: tool_use`). (5) `max_tokens_parameter` keeps applying to
`openai_compatible` only; `anthropic` ignores it (v1 upgrades always emit it), `openai` must match the
registry. (6) Reservation before each request-path call = 1 call + input estimate (chars/4) + the
`max_tokens` output ceiling (+ USD when priced); commit = reported usage, or the estimate when the
server reports none (`usage_reported: false`), including for failed calls. (7) Anthropic
`cache_creation_input_tokens` are priced with the new optional `cache_write_input_per_million`
(defaults to the input rate). (8) The protocol method is `complete_json(system, payload, schema,
budget, ctx)` per the slice brief rather than the plan's `complete(ctx, role_cfg, ..., stream)`;
streaming is not implemented in this slice. (9) Plan says `openai_compatible` capabilities come from the
role config: the registry entry is `role_declared` and only bounds what may be declared (never native,
caching at most passive). (10) Missing key = `MissingSecret(ProviderError, ValueError)` so existing
`ValueError` handlers keep working.
Follow-ups: streaming (plan Phase 1 row) and `doctor --probe` per role; Responses API; embedding usage
into the request budget (recorded on the provider as `last_usage` only); the write path's `CallBudget`
records usage but does not enforce token/USD limits; real-provider smoke awaits owner decision #14;
`examples/*_v2.yaml` with provider examples once the examples move to v2.

### 2026-11-03 — P1/elasticsearch adapter on the frozen contracts — DONE
Branch `worktree-agent-adc476aefbf7e9366` (merge of `v2/phase-0` at 74b8595 included), commit
`v2(phase-1/elasticsearch)`. Landed: `backends/elasticsearch/` (`adapter.py`, `transport.py`,
`search.py`, `registry.py`) as subclasses of the OpenSearch classes over `backends/_lucene_common.py`:
same three indices per generation, dataset aliases switched atomically by `activate(gen)`, typed-field
pushdown, BM25 lexical, bounded records scan with the shared `_scope` measures; Elasticsearch-specific
transport (official client, `TransportError` mapping, ndjson bulk), `probe` (8.x only), `dense_vector`
exact (`index: false` + `script_score` cosine) / float HNSW opt-in (top-level `knn` candidates, exact
rescore), no `index.knn` setting, and an alias-backed `generation_registry()` (manifests, failures,
create-only lock in `<prefix>__registry`; `current()` through the `docs` alias). Engine dispatch for
`kind: elasticsearch` (passes `config.dataset` for the registry), extra `elasticsearch`
(`elasticsearch>=8.17,<9`), services workflow install step, matrix rows `contracts` for
catalog/lexical/vector/records, conformance conftest params `elasticsearch`/`elasticsearch_hnsw`
(engine-neutral cleanup by index name), `tests/conformance/test_elasticsearch_conformance.py`
(probe, isolation/listing/delete, registry alias switch + failed build + lock, exact-vector parity
with the fake), `tests/unit/test_elasticsearch_backend.py` (10 fake-transport tests). Docs:
CONFIGURATION (row, semantics section renamed, typed-field bullet), ARCHITECTURE, VALIDATION
(real-service entry with digest), services README.
Tests: `-m 'not external'` 498 passed on the merged tree (pre-merge worktree 418 → merged baseline +
10 new unit tests); external `-k elasticsearch` **16 passed, 8 skipped** against Elasticsearch
8.17.4 (kit over both parametrisations + 4 module tests; skips are the undeclared capabilities);
ruff clean; schema unchanged.
Shared-code changes (beyond the agreed files, made after the coordinator's merge instruction):
(1) `backends/opensearch/{adapter,catalog,search}.py` are engine-parametrised: typed errors use
`self.kind`/`self._b.kind`, and four override hooks were added (`_default_transport`, `index_body`,
`OpenSearchVector._exact_script`/`_ann_candidate_ids` plus `vector_type`/`dimension_key`); no
behaviour change for OpenSearch, its fake-transport unit tests pass. (2) `_lucene_common.py`:
`typed_fields`, `_scalar_query`, `_bound`, `predicate_query`, `predicates_filter` take an optional
field spec and use `_scope.temporal_key` (naive stamps/date-only strings as UTC instants, `date`
fields as calendar dates, temporal `eq` as instant equality); the OpenSearch records facet passes
`schema.field`. This is what the kit's new `assume_utc`/`date` checks (merged with S5) require and
it applies to OpenSearch identically; its external suite was not re-run here (container not up).
Deviations / interpretations: (1) the plan's `_lucene_common.py` is used, but the engine-neutral
adapter/facet classes still live in `backends/opensearch/`; Elasticsearch subclasses them rather
than duplicating them — hoisting them into a neutral `backends/lucene/` package is a follow-up.
(2) Records use the shared bounded scan (exact, `max_matched`), not native `cardinality`/
`date_histogram` aggregations; the pre-merge private implementation with native aggregations was
dropped for consistency with OpenSearch (matrix flags `exactness: exact`, `max_matched: 10000`).
(3) `generation_registry()` exists only on the Elasticsearch adapter (the write path needs it for
`storage.catalog`); OpenSearch has `activate(gen)` but no registry — follow-up to share the registry.
(4) `test_config_v2.py`'s "planned kind" case now uses `sqlalchemy` (elasticsearch is on the
contracts). (5) `tests/unit/test_backends.py` matrix assertions include elasticsearch as `contracts`.
(6) The `elasticsearch` client is 8.19.3 against server 8.17.4 (8.x major gate in `probe`).
Follow-ups: hoist the shared classes out of `backends/opensearch/`; registry for OpenSearch; re-run
the OpenSearch external suite after the temporal-key change; end-to-end `ingest` through `Engine`
on Elasticsearch/OpenSearch (`storage.catalog`); HNSW filter-recall gate; the cross-engine lexical
agreement report (`tests/services/test_lexical_agreement.py`); `tests/services/` adapter test for
Elasticsearch mirroring `test_opensearch_adapter.py`.


### 2026-11-03 — P1 Neo4j graph adapter on the contracts (branch `worktree-agent-abe51c291233d8741`) — DONE
Commit `v2(phase-1/neo4j)` (`6e8f98a`), to be merged by the coordinator. Landed: `backends/neo4j/`
(`_cypher.py` every statement as a constant, hop templates precomputed per edge-kind set from the
closed `EdgeKind` set; `adapter.py` Neo4jBackend with explicit non-retrying transactions, error
mapping, template allow-list, pointer node, `protect`/`delete_generation`, `drop_namespace` test
helper; `graph.py` facet + GraphExtras + communities as nodes); `backends/capabilities.py` neo4j
graph `contracts` with `memberships: true`, new flag `predicate_scope` (true for memory/sqlite,
false for neo4j); `backends/extras.py::GenerationPointer`; `ingestion/write.py::_activate` (secondary
pointers move before `CURRENT`, moved back/removed on failure); `arms/base.py::graph_scope` used by
graph + community arms; shared `backends/_scope.py::validated_hops` (sqlite, memory, neo4j);
`engine.py` builds `kind: neo4j`; `core/config.py` `Neo4jBackend.namespace` (downgrade refuses a
non-default namespace); kit `check_graph` rewritten with `graph_fixture()`; v1 `adapters/neo4j.py`
deleted, `store.py`/`retrieval.py` v1 neo4j branches now raise, two v1 neo4j tests removed from
`tests/test_external.py` (their cases moved to `tests/services/test_neo4j_engine.py`). Docs:
CONFIGURATION (neo4j row), ARCHITECTURE, VALIDATION (real-service entry), services README; schema
regenerated.
Tests: non-external 473 → 496 passed (+17 `tests/unit/test_neo4j_adapter.py`, +7
`tests/contract/test_graph_backend_seams.py`, +1 config memberships test replacing two parametrised
cases that used neo4j as the "no memberships" example, now done with a monkeypatched matrix row);
external neo4j 13 passed (10 conformance, 3 engine) against `neo4j:5.26.0-community`
(`sha256:5a015e53…`); memory/sqlite/sqlite_faiss pass the extended graph check; ruff clean.
Deviations / interpretations: (1) Neo4j cannot evaluate document predicates (no catalog), so the
arms resolve predicate scopes to document ids via `catalog.iter_scope` before calling the graph
facet; catalog reads for that step are bounded by the eligible set (cap 10000 → typed error), not
by hits — principle 2.1.7 holds only for unfiltered requests on this backend. (2) "Generation-scoped
labels" are implemented as fixed labels + a generation-key property `g` (labels cannot be
parameterised; interpolating them would mean generated Cypher). (3) `TraversalSpec.edge_kinds` is
honoured as on sqlite (fact by default; co-occurrence only when requested — the arm never requests
it); fact-only is structural (separate relationship types). (4) The pointer node is a secondary
pointer next to the catalog registry's authoritative `CURRENT`; adapters still never read it on the
request path. (5) One transaction per `upsert` call (atomic; size bounded by server tx memory).
(6) MERGE makes re-loads idempotent on neo4j while sqlite/memory append duplicate relation rows;
the kit does not assert duplicate behaviour. (7) Hop validation added to sqlite/memory so the shared
kit can assert typed refusal for specs built with `model_construct`. (8) `CALL (n) {…} IN
TRANSACTIONS` requires Neo4j ≥ 5.23 (documented).
Follow-ups: staged multi-transaction load for very large graphs; optional predicate pushdown by
mirroring typed document fields onto neo4j (would restore 2.1.7 for filtered requests); doctor
probe for neo4j (`ping()` exists); vector index on neo4j stays Phase 5; the services CI job already
installs the `neo4j` extra and runs `-m external`, so the new suites run there unchanged.

### 2026-11-03 — P1 PostgreSQL and generic SQL adapters on the contracts (branch `worktree-agent-ac47e981ef8e1495f`, base `783d821`) — DONE
Commit `v2(phase-1/postgres-sql)` (`15d4f3a`), to be merged by the coordinator. Landed:
`backends/postgres/` (`adapter.py` PostgresBackend with seven facets and a lazily opened connection,
`_db.py` naming/DDL/typed rows/predicate and tsquery compilation, `catalog.py` Catalog + RecordStore,
`search.py` FTS LexicalIndex + pgvector VectorIndex with `HnswSettings` and `filter_recall`,
`runtime.py` cache/ledger/sessions, `registry.py` PostgresGenerationRegistry with a `CURRENT` pointer
row and a `pg_try_advisory_lock` write lock); `backends/sql/` (config kind `sqlalchemy`; SqlBackend
with catalog/records/cache/ledger/session on SQLAlchemy Core, SqlGenerationRegistry with a lease
row); `engine.py` builds `postgres` and `sqlalchemy`; `core/config.py` `PostgresBackend.vector`
(`index exact|hnsw`, HNSW parameters, `min_filter_recall`) and a validated `text_search_config`;
capability rows: postgres everything but graph `contracts` (graph `planned`), sqlalchemy catalog/
records/cache/ledger/session `contracts` (lexical, vector `planned`); pip extra `sql`
(`sqlalchemy>=2.0,<3`), `postgres` pinned `<4`; CI installs `sql,postgres`, services job `sql`.
Tests: `tests/conformance/test_postgres_conformance.py` (23 `external`),
`tests/conformance/test_sql_conformance.py` (12 sql[sqlite] every push, 12 sql[postgres]
`external`, 2 dialect-independent), `tests/unit/test_postgres_adapter.py` (13, fakes),
`tests/contract/test_arms.py` (+2: engine builds both kinds lazily / fails closed without a DSN;
engine end to end with sqlalchemy storage on SQLite), `tests/test_external.py` v1
`test_postgres_real` replaced by the v2 `test_postgres_engine_end_to_end` (neo4j tests stay skipped
individually); `test_backends.py`/`test_config_v2.py` matrix and validation rows updated (the three
"postgres storage is planned" rows replaced by planned-graph/communities, sqlalchemy lexical/dense,
bad `text_search_config` and bad `vector.index` rows, plus a positive storage-on-postgres/sqlalchemy
test). Non-external 473 → 506 passed; external 36 passed, 2 skipped against
`pgvector/pgvector:0.8.0-pg17` (PostgreSQL 17.6, vector 0.8.0); ruff clean; schema regenerated;
wheel/sdist build ok (both packages in the wheel, `sql` extra in METADATA). Docs: CONFIGURATION
(extras, two backend rows, validation text), ARCHITECTURE, VALIDATION (real-service entry),
services README.
Deviations / interpretations: (1) Package path `backends/sql/` as instructed by the coordinator
(plan §4 says `backends/sqlalchemy/`); the config kind stays `sqlalchemy`. (2) Naming: PostgreSQL
identifiers are capped at 63 bytes, so the per-generation schema is `<prefix>_<dataset>_<generation>`
only when it fits and the dataset is a lowercase identifier, else `<prefix>_g<sha256[:20]>`; generic
SQL tables are always `<prefix>_<sha256(dataset/generation)[:12]>_<suffix>`; both record the mapping
in a generations table. (3) Lexical terms are OR-combined `plainto_tsquery` per `\w+` token (FTS5
semantics, no user syntax) instead of the plan's `websearch_to_tsquery`, which ANDs terms and
interprets `or`/`-`/quotes from user text; phrase = `phraseto_tsquery`, evaluated per column (title,
body) so phrases never span the boundary. (4) Measures/groups are computed over matched rows with the
shared `_scope` helpers (as sqlite and mongodb do); predicates, counts, `exists` and `values` run in
SQL. (5) Typed rows keep the raw text in `text_value` (memory-fake semantics for eq on temporal
strings); postgres compares temporal values via `timestamptz`, generic SQL via UTC epoch seconds
(no portable tz-aware type). (6) HNSW gate: `refresh` under `vector.index: hnsw` builds partial
per-namespace indexes, runs `ANALYZE`, then measures recall@10 of the **forced** index path (seq,
bitmap and sort plans disabled, so a lower bound) against exact on ≤32 sampled stored vectors,
unfiltered and with an allow-list; below `min_filter_recall` it raises, so that build never
activates. Searches use `hnsw.iterative_scan = strict_order` (pgvector ≥ 0.8; older versions fail
closed). (7) Matrix `max_dimensions` for postgres stays 2000 (HNSW limit); exact accepts 16000.
`analyzer_id` flag is `pg_ts_config`; the per-generation value is `PostgresBackend.analyzer_id(gen)`.
(8) sqlalchemy declares `vector` as `planned` (coordinator brief) where the plan table says "none";
either way configuration rejects it. (9) `adapters/postgres.py` is kept: the unused v1
`store.py`/`retrieval.py` still import it; no v1-only test of it remains.
Follow-ups: record the lexical `analyzer_id` in the generation manifest (write path); projections on
a backend other than the catalog's (e.g. sqlite lexical over a postgres catalog) need the
multi-backend write-path slice (today a typed build failure); MySQL/MariaDB conformance (container
and a binary-collation story for `eq`); dialect-gated lexical for sqlalchemy; retire
`store.py`/`retrieval.py`/`adapters/postgres.py` together; cross-engine lexical agreement report.

### 2026-11-03 — Merge `v2/phase-0` (8b2c5bf) into the postgres/sql branch — DONE
Engine dispatch builds sqlite, memory, mongodb, postgres, sqlalchemy, neo4j, opensearch and
elasticsearch (the last refusal is gone; the `else` branch only guards an unknown kind).
`test_backends` lists every served pair (postgres except graph, sqlalchemy catalog/records/cache/
ledger/session, Lucene catalog/lexical/vector/records, neo4j graph, mongodb except vector); no adapter
kind is wholly `planned` any more. `compat/config_v1.downgrade` keeps every mainline refusal and also
refuses non-default postgres `vector`/`text_search_config`/`timeout_seconds` (v1 cannot express them).
The v1 `store.py`, `retrieval.py` and `adapters/postgres.py` (with `adapters/__init__.py`) were
deleted: nothing on mainline imported them. The v1 neo4j cases in `tests/test_external.py` were
already retired on mainline; only the v2 postgres engine test remains there. Docs list each adapter
once; services README and the services workflow install `dev,postgres,sql,neo4j,mongodb,opensearch,
elasticsearch`. Tests: ruff clean; non-external 638 passed; external postgres + sql + engine 36 passed
against `pgvector/pgvector:0.8.0-pg17` (container removed afterwards).

### 2026-11-03 — P1 declarative pipeline, harness through the engine, exact_id arm (branch `worktree-agent-a102da5b471458440`, base `0fb3ac0`) — DONE
Commit `v2(phase-1/pipeline-harness)` (`54c244b`), to be merged by the coordinator. Landed:
`pipeline/spec.py` (`PipelineRuntime`, `choose`, `RequestRejected`), `pipeline/validate.py`
(`config_diagnostics` + `raise_for`: every static problem in one `ConfigError` with `.diagnostics`;
`decision_for_mode`; `probe_report`), `arms/exact_id.py`, `runtime/turns.py` (`SessionTurns`),
`runtime/builtin_tools.py` (`search`, `get_document`, `list_values`, `aggregate`; `ToolCall`),
`runtime/builtin_actions.py` (`LedgerAuditLog`, `ingest_delta`, `invalidate_cache`; `ActionCall`);
`core/config.py` (`pipelines`, `default_pipeline`, `tools`, `actions`, `budgets.session`,
`sessions.memory.exclude_shown`, `ExactIdArm.alias_fields/casefold/max_ids`; `coherent` delegates to
`pipeline/validate.py`), `core/request.py` (`pipelines`, `modes`), `core/types.py`
(`FieldSpec.pattern` for `id` fields, `SessionRecord.usage`), `core/errors.py`
(`ConfigError.diagnostics`), `models.py` (`SearchRequest.pipeline`/`mode`), `engine.py` (pipelines,
sessions, `call_tool`, `run_action`, `EngineOperations`, exact_id pins/unknowns), `probe()` on
sqlite/memory/mongodb, CLI (`doctor --probe`, `session`, `tool`, `action`, `--session/--pipeline/
--mode`), `mcp.py` (two tool arguments). Tests 573 → 607 (+34: `tests/unit/test_pipeline_validation.py`
13, `tests/contract/test_pipeline_harness.py` 16, `tests/contract/test_doctor_probe.py` 5; mongodb probe
asserted inside two existing unit tests). Ruff clean, schema regenerated. Evidence: synthetic /
contract-mocked only.
Deviations / interpretations: (1) `pipelines.<name>` entries are **overlays** deep-merged onto
`pipeline` (the plan's sketch has one `pipeline` block; the brief's full-spec form is accepted too,
since a full entry overrides every key). The JSON schema shows the resolved `PipelineConfig` shape,
so editors may flag a partial entry's missing `arms` although validation accepts it. (2) Named
pipelines may only use the `dense`/`lexical`/`graph`/`community` backends of the default pipeline
(`projection_mismatch`/`projection_missing`): the write path projects only those, and changing it
was out of scope for shared files. (3) `mode` is interpreted as a decision-kind override
(`rules|classifier|llm|cascade`); `cascade` only on a cascade pipeline, `classifier` only where the
pipeline declares an artifact/component. (4) Static validation is aggregated, but pydantic field
errors (unknown keys, type errors) still stop before the model validator, so those are reported
first-error-only as before; the single-error message format is unchanged (`path: message`). (5) Tools
and actions are built-ins only (plugin tools/actions not loadable yet); the LLM decision agent does
not call tools (registry is ready); tool calls run in their own context (`Engine.call_tool`), not
inside an answer. (6) Actions audit on `storage.ledger` as append-only versions of one ledger job
`audit:<dataset>`; duplicate detection replays that job (O(audit size), acceptable for rare
operator actions); `promote_generation`/`rollback_generation` not implemented. (7) Per-request budgets
are the existing `budgets.*`; per-session limits are new (`budgets.session`); an exhausted session
refuses every further turn, even one that would need no model call. (8) `SessionRecord.usage` added
(JSON-serialised by all three stores, default empty). (9) `exact_id` reports unknown ids as
`UnsupportedConstraint(reason="value_not_observed")` appended **after** `AnswerContract.enforce` so
they never suppress the contract's own aggregate-intent flagging; an id outside the caller's ACL or
in `exclude_ids` is reported unknown (existence is not revealed). The arm has no `backend` key; its
capability is `catalog` through `storage.catalog`. (10) A `rerank` pass keeps pins first. (11)
`tests/test_protocol.py` sets `PYTHONPATH` for the MCP subprocess to the package under test (the
`.venv` editable install points at the main checkout, which broke worktree runs once the dumped
config gained keys).
Follow-ups: HTTP/MCP surfaces for sessions, tools and actions (serving slice; `api.py` maps
`RequestRejected` to 422 already); planner tool use through `ToolRegistry.schemas()`; plugin tools
and actions via entry points; write path projecting arms of named pipelines; concurrent turns on one
session are last-writer-wins on the turn list; MongoDB/OpenSearch `probe()` against the containers in
the services job; `release *` CLI stubs (Phase 1 row) not added.

### 2026-11-03 — Merge `v2/phase-0` (`16a3a17`) into the pipeline-harness branch — DONE
Conflicts: `engine.py` imports only (backend construction for all eight kinds came from mainline
unchanged; pipelines/sessions/tools wiring kept), `docs/VALIDATION.md` and this log (both sides kept,
mainline entries first). Added `probe()` delegating to the existing typed `ping()` on postgres,
sqlalchemy (`backends/sql`) and neo4j; opensearch/elasticsearch already had `probe()`, so every
backend kind is probed by `doctor --probe` (none reported `no_probe`). Schema regenerated with
`su_multi_arm.core.schema.write_all`. Tests: 675 passed, 116 deselected (`-m 'not external'`; +3
probe-delegation tests in `tests/contract/test_doctor_probe.py`); ruff clean. The new probes were not
run against containers (synthetic stub only). `ingestion/write.py` untouched.

### 2026-11-03 — P1 multi-backend write path (branch `worktree-agent-ac96c0b0d16d60e31`, base `16a3a17`) — DONE
Commit `v2(phase-1/multi-backend-write)` (`7d9f36f`), to be merged by the coordinator. Landed:
`ingestion/generations.py` (placement table projection → backends; `GenerationSet` with holders in
activation order, activation mechanism per holder `current|pointer|registry|none`, pointer read /
activate / restore, `check` for pinning, manifest entry); `ingestion/write.py` (scope mirrors on
secondary holders, records/graph/communities written to every target, per-target validation incl.
mirrors, records targets, graph memberships + sampled `find_entities`, community member lists vs
memberships, lexical `analyzer_id`; `_activate` secondaries first / `CURRENT` last with reverse-order
compensation and `ActivationError`; failure record gains `staged` pairs and `compensation`; `pin()`;
`delete_generation`; cleanup and protection limited to generation-scoped holders); `core/errors.py`
`GenerationDisagreement`, `GenerationInUse`; `backends/extras.py::GenerationIdentity` implemented by
memory, sqlite, postgres, sqlalchemy, mongodb, opensearch/elasticsearch, neo4j; OpenSearch (and so
Elasticsearch) implements `GenerationPointer` (`activate_generation`, `deactivate_generation` removing
only aliases that still name the generation); every generation registry gains `deactivate(gen)`
(memory/ES also `current_generation`); `engine.py`: two lines, `write_path.current()` →
`write_path.pin()` in `_context_for`/`answer` (no other engine edits).
Tests: non-external 638 → 659 (`tests/contract/test_multi_backend_write.py` 20,
`tests/unit/test_opensearch_backend.py` +1); `test_write_path` projection-set assertion now includes
`graph`/`communities`; `test_graph_backend_seams.build` clears the fake's recorded scopes after the
build (validation reads are not arm reads; same edge cases). External: new
`tests/services/test_multi_backend_engine.py` 1 passed (PostgreSQL catalog, OpenSearch lexical, Neo4j
graph); existing external suites for those three services 68 passed, 8 skipped. Containers started
for the run and removed with `rm -sf` (docker ps identical before/after). Ruff clean; `Config`
unchanged, schema not regenerated. Docs: CONFIGURATION ("Selecting a backend per projection" with
two examples, activation/pinning/manifest/clean-up), ARCHITECTURE ("Generations across backends"),
VALIDATION.
Deviations / interpretations: (1) **Scope mirror**: every lexical/vector/graph facet resolves scope
against its own backend's documents (and sqlite/postgres/mongo/Lucene refuse chunks of unknown
documents), so a secondary holder serving `catalog` receives the generation's documents + chunks; it
is validated and never read as the catalog. A lexical/dense arm on a backend without catalog is a
`ConfigError`. (2) Placement beyond the plan text: records also go to `pipeline.arms.structured.
backend` and graph to `storage.communities`/community arm backend when they differ (the community
arm reads mentions from its own backend; before this slice a community backend without the graph
arm had no mentions). (3) Secondary backends without their own pointer object (sqlite, memory,
postgres, sqlalchemy, mongodb) are activated through **their own generation registry** (manifest +
`CURRENT`), so disagreement is detectable on every holder; their `delete_generation` does not consult
that pointer (only neo4j/Lucene refuse an active generation natively), but `WritePath.delete_generation`
checks every pointer first. (4) Pinning reads one pointer per secondary per request (a network call
for Lucene/Neo4j/SQL/Mongo); single-backend configs read none. Generations built before this slice
have no secondary pointers and are refused once a projection lives off the catalog's backend
(rebuild). (5) `index_fingerprint` is unchanged (still keyed by the slot map) so existing indices keep
their identity. (6) `delete_generation` lives on `WritePath` (not `Engine`) to keep engine edits to
pinning; manifests are kept as audit records. (7) OpenSearch still has no generation registry, so it
cannot be `storage.catalog` (now a typed `ConfigError` instead of an `AttributeError`).
Follow-ups: OpenSearch registry (share the Elasticsearch one); real-service runs with MongoDB and
Elasticsearch as secondary holders; `Engine`/CLI surface for `delete_generation` and a `status`
view of every pointer; Phase 2 `release verify/gc` should treat manifests whose data was deleted;
session pins (`protect`) are per process — a multi-process pin registry is needed before `gc`.

### 2026-11-03 — P1 serving parity and streaming (branch `worktree-agent-a11ed89e7490c5073`, base `4a15f73`) — DONE
Commit `v2(phase-1/serving)` (`c456375`), to be merged by the coordinator. Landed: `runtime/events.py`
(typed `AnswerEvent` union, `AnswerStream` worker/queue with `cancel()`/`next_event(timeout)`,
`error_category`), `Engine.answer_stream` + `Engine.list_values` (engine edits limited to the answer
path: `_retrieve`/`_answer`/`_generate` take an optional event sink/delta callback; `answer` and
`answer_stream` share `_answer_turn`), `Budget.cancel()` + `core/errors.RunCancelled`, provider
streaming (`HttpCall.post_stream`, `iter_sse`, `AnthropicStream`, `ChatStream`,
`complete_json_stream`/`complete_text_stream`, `JsonModel.call_stream`/`streams`, `RoleSpec.streaming`,
`ModelCapabilities.streaming`), `serving/http.py` (moved from `api.py`, which re-exports),
`serving/sse.py`, `serving/auth.py`, `serving/mcp.py` (moved from `mcp.py`, which re-exports),
`serving/discovery.py`, `serving/parity.tool_differences(schema, model)`, CLI `mcp --transport
stdio|streamable_http --host --port --token-env`, `testing/provider_fakes.py`. Tests 675 → 712 (+37:
`tests/contract/test_streaming_providers.py` 18, `test_answer_stream.py` 9, `test_serving_http.py` 9,
`tests/parity/test_serving_parity.py` 1; `test_protocol.py`, `test_interfaces.py`,
`test_request_parity.py` updated for the larger MCP tool set and per-tool drift). Ruff clean; schema
regenerated (no drift). Evidence: synthetic / contract-mocked model; transports real on loopback.
Deviations / interpretations: (1) Event names follow plan §3.15 (`decision`, not "plan"); the
plan's `unsupported_constraints` event is folded into `contract` (status, violations, unsupported
constraints, `tokens_superseded`); `guard` is not emitted (guards are Phase 2). (2) Token deltas are the
generator's raw reply fragments — JSON text in structured-output modes — typed `provisional: true`;
the validated, contract-checked answer exists only in `done`. (3) Tokens stream only when the
generator role declares `capabilities.streaming: true` (the existing, default-off declaration);
otherwise the stream simply has no `token` events and the call is the non-streamed one (visible in
`/v2/capabilities.answer_streaming`); the provider itself refuses a stream call on an undeclared role.
`openai_compatible` streams send `stream_options.include_usage` like `openai`. (4) `decision` is
emitted live; `arm_result` per arm after each round's parallel execution; `review_round` after the
review stage finished (post hoc), then `evidence`. (5) Python `answer_stream` raises request,
pipeline/mode and session errors eagerly (HTTP maps them to status codes before the stream starts);
run failures end the stream with an `error` event (HTTP/MCP replace the message by a generic text
except for cancelled/budget/deadline). (6) Auth built-ins: loopback `LocalResolver` (principal
`anonymous`, scopes search/answer/tools) and `BearerTokenResolver` (principal `bearer`, plus
`actions`); the plan's `static_api_keys` built-in was not added (OAuth/API keys outside core per
default #6; injected via `principal_resolver=`). (7) `/v2/values` has `limit` ≤ `page_size_max`
but no cursor (paging is Phase 2 per the Phase 1 row); `/ready` also returns 503 when no generation
is active and does not expose probe error text. (8) MCP: `lookup_record` is `get_document`;
`capabilities` is the single discovery tool (datasets, pipelines/modes, filterable/listable fields,
tools); `get_document`/`aggregate` are registered only when enabled in `tools.enabled`;
`list_values` mirrors `/v2/values`. Every MCP search/answer/tool call is bound to an engine session
created per (MCP session id, principal) — one per stdio process — so turns are recorded (no config
switch to turn this off). MCP resources (active manifest) not added. (9) HTTP actions answer 403
for `denied`, 200 otherwise (the `ActionResult.status` carries `failed`). (10) Heartbeat interval is a
`create_app(heartbeat_seconds=...)` argument (15 s), not config.
Follow-ups: `release *` CLI stubs and `/v2/values` cursor paging; MCP `notifications/cancelled`
test; streaming for planner/reviewer stages (generator only today); `serving.mcp` session-binding
switch if operators want stateless MCP; `tests/smoke_wheel.py` exercises only MCP `search` over
stdio (unchanged).
### 2026-11-03 — P1/mongodb instant-based temporal fields (conformance fix) — DONE
Branch `v2/phase-0`. `backends/mongodb/_db.py`: `field_row` uses `_scope.instant` (canonical UTC text
+ epoch seconds), `predicate_filter(..., spec=)` routes temporal `eq`/`in`/bounds through
`_scope.temporal_key` (date fields: UTC-day window / truncated bounds; non-temporal value vs temporal
field → impossible; text bounds only on string rows); `catalog.py` records pass the field spec.
Real-service `test_mongodb_conformance.py` 16 passed (was 2 failing); new driver-free parity unit
test; tests 733 → 734 (`not external`), ruff clean, no schema change. Deviation: the existing unit
assertion "naive stamps are never guessed" was replaced by the B1 storage rule (naive = UTC).
Other adapters: none found with the gap (sqlite/memory/postgres/sql use `_scope`; opensearch and
elasticsearch share `_lucene_common.temporal_key` and were re-run after B1; neo4j holds no records).
Mongo test container removed afterwards.
### 2026-11-03 — a1 release-review fixes (B1–B6 except README, N1–N5, N13) — DONE
Branch `v2/phase-0`. Done: B1 `Engine._canonical_record` goes through `documents_in` (ACL-scoped
`iter_scope`). An unresolved id is removed from `DecisionRequest.record_context` and from the
generator's `caller_context_unverified`. It is reported as `UnsupportedConstraint(value_not_observed)`
and `trace.record_context`, and unknown and out-of-scope ids look identical. Tests:
`tests/contract/test_record_context_acl.py` (planner and generator payloads). B2: the review hop labels
its round with the first `RetrievalStep` query (else the request query) and runs structured steps
through the normal scope path. Tests: `tests/contract/test_review_stage.py` (engine, HTTP, SSE,
`answer_stream`). B3: `pipeline/validate.py` rejects any non-empty guards list (`not_executed`). B4:
BUSINESS_SUMMARY and MIGRATION were deleted and `docs/ADOPTING.md` was added. Example source (a vendor tracker name) →
`tracker` (`tracker:rounding`). Mapping example field names were neutralised (`Article.Title`,
`Issue__text`, ...). Wording was fixed in CONFIGURATION, VALIDATION, normalize.py and two historical
review files. A tracked-file denylist test was added (`tests/unit/test_release_identity.py`; it excludes
`docs/plans/reviews/`). B5: version 2.0.0a1, `--version`, `http.py` uses `__version__`, extras
`openai`/`anthropic` (empty, httpx), `local`, `all`; ci.yml installs `openai,anthropic`. B6:
`examples/v2/*.yaml` with load tests and a local end-to-end test, v1 examples labelled as the upgrade
path, CONFIGURATION/PLAN/VALIDATION/ARCHITECTURE updated. N1: `_scope.MAX_MATCHED_DEFAULT`/
`matched_cap`/`check_matched`, `max_matched` attribute on the sqlite, postgres, sqlalchemy, mongodb and
memory backends, plus a conformance check. N2: `scope_result` returns `bounded` when a predicate is
unsupported, and `AggregateEvidence.exact()` also requires no unsupported predicates. N3: catalog
`registry` flag in the capability matrix, `STORAGE_CAPABILITY["catalog"] = ("catalog", "registry")`.
N4: `core.errors.IndexNotReady` (a ValueError subclass; raised by `WritePath.current`); HTTP 499, 502
and 503; SSE category `unavailable`. N5: `except SuError: raise` before a catch narrowed to
`(ProviderError, httpx.HTTPError)`.
Deviations: (1) the review hop does not receive the reviewer's follow-up text, so it uses the first
retrieval step's query for the round label, as the review suggested. (2) `max_matched` on the
SQL-family and Mongo adapters is an instance attribute (default 10000), not a config key, so the
Config schema did not change. (3) An existing test was changed for N2 and N5: the opensearch unit
assertion on `exactness == "exact"` with a dropped predicate is now `bounded`. The fail-policies test
for "planner HTTP 500" now raises a `ProviderError` instead of a bare `RuntimeError` (bare
RuntimeErrors now propagate). (4) The `SU_*` env-var prefix, the package/dist name `su_multi_arm` /
`su-multi-arm`, and the `su` index prefixes in tests were kept (owner decision pending). README is owned
by the docs agent and was not edited.
Tests: 734 → 758 (`not external`, this commit; 763 in a working tree that also holds the docs agent's 5 capability-table tests), ruff clean, schema unchanged, wheel 2.0.0a1 builds.
Follow-ups: the B7 consolidated real-service run must include the new N1/N2 conformance checks on
postgres, mongodb, opensearch and elasticsearch; the untracked `a1_release_review.md` names the origin
services (excluded from the denylist test as a review record; neutralise it before a public push);
N6–N12, N14–N16 open; history rewrite before publishing (N13).

### 2026-11-03 — Landing README, MkDocs site, generated capability tables — DONE
Commit `4d760f4` on `v2/phase-0`. Landed: `README.md` (landing page), `assets/logo.svg` (+ symlink
`docs/assets/logo.svg`), `mkdocs.yml`, `docs/{index,quickstart,serving,schema,backends,providers,contributing}.md`,
`scripts/gen_capability_tables.py` (`--check`), `scripts/mkdocs_hooks.py`, `.github/workflows/docs.yml`,
`CITATION.cff`, `docs` extra in `pyproject.toml`, `tests/unit/test_capability_tables.py` (+5).
Tests: 766 passed `not external` (includes concurrent release-fix work); ruff clean; `mkdocs build --strict`
clean; every documented code block executed verbatim in a fresh clone + venv (see VALIDATION).
Interpretations: (1) the quickstart uses `examples/v2/local.yaml` (added by the release-fix slice);
(2) backend evidence is derived from the test tree (suite exists and is `external`), not from run
results; provider evidence is fixed policy text in the script; (3) links leaving `docs/` are
rewritten to GitHub by a build hook rather than editing existing docs or relaxing validation;
(4) `docs/plans/reviews/` is excluded from the site; (5) the public name lives in `mkdocs.yml`
`site_name`, the README title and `CITATION.cff`. Follow-ups: enable GitHub Pages (source: Actions)
when the repository is published; the Docs link and badges resolve only after that.
### 2026-11-03 — a1 release evidence (release review B7) — DONE
Branch `v2/phase-0`, source under test `8f6dd56`. Done: (1) consolidated real-service run — the five
`su-multi-arm-tests` containers (digests in VALIDATION "2.0.0a1 release evidence"), `wait_ready.py`
all ready, `pytest -m external -rs` with every `SU_TEST_*` service variable set: 100 passed, 17
skipped (16 undeclared kit capabilities on OpenSearch/Elasticsearch, 1 local-model test with
`SU_TEST_LOCAL_LLM_URL` unset), 0 failed, first run; the N1/N2 `check_records` additions passed on
postgres, sqlalchemy-on-postgres, mongodb, opensearch and elasticsearch. (2) `scripts/lexical_agreement.py`
and `docs/LEXICAL_AGREEMENT.md` (5 engines, 23 queries, 0 errors; identical match sets within analyzer
family, 18/23 across stemmed/unstemmed families). Unit test `tests/unit/test_lexical_agreement_script.py`
(metrics, query kinds, unset engines listed). (3) Stack torn down with `down -v` after confirming it was
idle (no foreign indices, sessions or databases); unrelated containers unchanged. (4) VALIDATION status
block and evidence section; local-model smoke recorded as the coordinator's run (1 passed,
`qwen2.5:1.5b`, OpenAI-compatible endpoint).
Fix: `tests/unit/test_release_identity.py::test_tracked_files_carry_no_origin_service_names` failed at
`8f6dd56` because the committed test file matches its own `DENYLIST` line; it now excludes its own path.
Deviations: the agreement script indexes identical pre-chunked documents through each adapter's
`catalog` + `lexical` facets (not the full `WritePath`), so the comparison isolates lexical semantics;
chunk-level metrics (document-level would be near-trivial on 7 documents). `docs/LEXICAL_AGREEMENT.md`
is not added to the `mkdocs.yml` nav (owned by the docs agent).
Tests: `not external` 762 passed + 1 failed → 766 passed (3 new, 1 fixed; working tree includes the docs
agent's capability-table tests); external 100 passed / 17 skipped; ruff clean; Config unchanged.
Follow-ups: add `LEXICAL_AGREEMENT.md` to the docs nav; hosted Anthropic/OpenAI remain mocked.
