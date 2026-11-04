# 2.0.0a1 post-release completeness review

Reviewed: branch `v2/phase-0` at `a8d4ff4` (same tree as the published single commit on `main`).
Date: 2026-11-03. Scope: plan `docs/plans/v2_implementation_plan.md` rev 3 (all sections; §4 Phase 0
and Phase 1 tables and acceptance, §5, §7 R1–R26, §9), `docs/plans/execution_log.md` (every entry),
all five files in `docs/plans/reviews/`, `docs/VALIDATION.md` ("2.0.0a1 status" and "2.0.0a1 release
evidence"), and the source. Each log or review claim used here was checked against the code with
grep/read. No source file was changed.

Evidence gathered in this review (all in the external test workspace, synthetic data only):

| Check | Result |
|---|---|
| `pytest -m 'not external'` (bytecode off, external basetemp) | **766 passed**, 117 deselected, 0 failed |
| `ruff check src tests` | clean |
| Wheel built from `git archive HEAD`, installed in a fresh venv as `su_multi_arm-2.0.0a1-py3-none-any.whl[opensearch,anthropic,mcp]` | installs without warnings; `pip check` clean; `su-multi-arm --version` → `2.0.0a1` (plan Phase 1 acceptance command, verified here for the first time after the B5 fix) |
| Probe 1: config keys for features that do not exist yet | `pipeline.arms.cache` → `ConfigError` (correct); `extraction.ontology`, `models.roles.judge`, `models.roles.cache_gate` → **valid config and engine constructed, never consumed** (risk R3) |
| Probe 2: one-document upsert on the 6-document example (sqlite, hash embedder) | full ingest embedded 16 chunks; a 1-document upsert without `--replace` embedded **16 of 16** chunks again (risk R2) |

Real-service evidence was not re-run. It is taken from the VALIDATION entry: 100 passed, 17 skipped,
0 failed at source commit `8f6dd56`, with image digests recorded.

Status legend: **done**: the row's contract and tests exist and match the plan. **Partial**: some
of the row is missing; what is missing is listed. **Not done**: absent. **Deferred-with-record**:
absent or changed, with a dated deviation or follow-up in the execution log or a review.

---

## 1. Phase 0 and Phase 1, row by row

### 1.1 Phase 0 (plan §4, 61 ed)

| # | Row | Status | Evidence | Missing / note |
|---|---|---|---|---|
| 0.1 | Canonical types with spans | done | `core/types.py:29` `Span`, `:146` `Relation`, `:168` `Event`, `:176` `StructuredRecord`, `:190` `Membership`, `:197` `ExtractionOutput`; `tests/unit/test_types.py` | `Event` and `StructuredRecord` have **no producer** (extractors are Phase 3). `Relation.occurrences` was added (S4 deviation 1). |
| 0.2 | Metadata schema | done | `core/types.py:262` `FieldSpec`, `:310` `MetadataSchema` (incl. `unknown_fields`); `tests/unit/test_metadata_schema.py` | — |
| 0.3 | Component + registry | **partial** | `core/component.py` (artifact rules), `core/registry.py:78` `Registry.load_plugins`; `tests/unit/test_component_registry.py` | `load_plugins` is **never called outside tests**. Config `plugins:` is only checked for existence (`core/config.py:840-843`). No config-listed third-party component (guard, classifier `decision.component`, tool, action) is ever loaded. Arms and extractors are closed literals, so they cannot be plugins at all. Pickle and dotted paths are rejected (done). |
| 0.4 | Config v2 + v1 upgrade + schema | done | `core/config.py`, `compat/config_v1.py`, `core/schema.py`; `tests/unit/test_config_v2.py` (round-trip, drift) | `on_violation` literal is `fail`, not the plan's `error` (S3 deviation 5, never reconciled). The S2-fix N13 follow-up is still open: only the first pydantic field error is reported. |
| 0.5 | Backend contracts + capability matrix (freeze) | **partial** (deferred-with-record) | `backends/contracts.py`, `backends/capabilities.py`; plan rev 3 changelog | The operations the arms and the write path need (`Countable`, `GraphExtras`, `GenerationPointer`, `GenerationIdentity`) live in `backends/extras.py`, outside the frozen contracts. The plan §3.5 changelog was never amended for them (S4 follow-up "promote extras", unassigned). The freeze rule ("any change … requires a changelog entry") is not met. |
| 0.6 | sqlite adapter on contracts | done | `backends/sqlite/`; `tests/conformance/test_backend_conformance.py` (memory, sqlite, sqlite_faiss) | — |
| 0.7 | Five arms as Components | done | `arms/{dense,lexical,structured,graph,community}.py`; `tests/contract/test_arms.py` | The community arm still reads every community per request (risk R6; review S3/S4 N1 was only partly fixed). |
| 0.8 | Write path + per-projection validation + policies | done | `ingestion/write.py`, `ingestion/policy.py`; `tests/unit/test_write_path.py` | S3/S4 N12 is still open: the extraction reuse key ignores relocation. |
| 0.9 | Runtime context, budget, trace | done | `runtime/context.py`, `runtime/budget.py`, `runtime/trace.py:167` `export` | Budget overruns are not emitted as a trace event (kernel review N12, open). `commit()`'s docstring says "overruns are recorded" (`budget.py:199`), but nothing records them. |
| 0.10 | Span-anchored grounding with offset map | done | `extraction/grounding.py`; `tests/test_grounding.py` (fixture categories + 500 property cases) | The fixtures are inline in the test file, not in `testing/fixtures/grounding/` (layout only). |
| 0.11 | Predicate support + edge kinds + dedupe | done | `extraction/predicate_support.py`, `extraction/dedupe.py`; `test_write_path.py` five-occurrence case | `PathEdge.occurrences` exists (`core/types.py:525`). |
| 0.12 | Intent pre-pass, typed scope, values, fail policies | done | `decision/{intent,scope,values,base}.py`; `tests/unit/test_{intent,scope_values,fail_policies,count_probes}.py` | Zero-match is not carried on `AnswerResult` (S3/S4 N11, open; only in aggregate evidence, `engine.py:118`). |
| 0.13 | Answer contract | done | `generation/contract.py`; `tests/unit/test_answer_contract.py` | Prose numerals inside `narrative` claims are not screened (Phase 2 `exact_set`, recorded). |
| 0.14 | Review stage | done | `pipeline/review.py`; `tests/contract/test_review_stage.py` (incl. release B2 regression) | — |
| 0.15 | Selection split | done | `selection/*.py`; `tests/unit/test_selection.py`, `tests/parity/test_ordering_parity.py` | `selection/phrase.py` moved to Phase 3 (S3 deviation 9). |
| 0.16 | Neutral prompts + profiles + voice | **partial** (deferred-with-record) | `profiles/neutral.yaml`, `support_example.yaml`, `profiles/prompts.py:57` voices; `tests/contract/test_domains.py` | `generation/generator.py` does not exist as a component; the engine calls `JsonModel` directly (S5 follow-up, unassigned). `research_example`/`collections_example` exist only as test fixtures. |
| 0.17 | §5 migration: legacy-read of v1 generations and `migrate-snapshot` (R26) | **not done** (no record) | `ingestion/write.py:141` refuses a v1 generation ("run ingest --replace --rebuild"); no `migrate-snapshot` CLI (`cli.py:20-74`) | Plan §5 promises read-only legacy mode for dense/lexical/structured and a no-inference re-grounding migration. Neither exists, and no execution-log entry records the deviation. v1 users must rebuild. |

Phase 0 acceptance:

| Item | Status | Evidence |
|---|---|---|
| 40 v1 tests replaced with the same edge cases under `support_example` | met | `tests/contract/test_engine_support_example.py` |
| Notes §4 acceptance 1–5 | met (synthetic) | `test_fail_policies.py`, `test_answer_contract.py`, `test_intent.py` |
| Grounding fixture pack + offset-map property test | met | `tests/test_grounding.py` |
| Five `hosted` occurrences → one `co_occurrence` edge with five provenance entries | met | `tests/unit/test_write_path.py` |
| Nine NL-count probe shapes → no fabricated number under `fail_closed` and `raw_all_arms` | met (shapes reconstructed, S3 deviation 2) | `tests/unit/test_count_probes.py` |
| v1 config upgrade round-trips its fingerprint | met | `test_config_v2.py::test_every_example_upgrades_deterministically_and_round_trips_its_fingerprint` |
| External B8 bounded local-model re-run over frozen cohorts | **deferred-with-record** | owner decision 16; VALIDATION "Span-anchored grounding" entry; release review N15 |

### 1.2 Phase 1 (plan §4, 102 ed)

| # | Row | Status | Evidence | Missing / note |
|---|---|---|---|---|
| 1.1 | Conformance kit (full) | **partial** | `testing/backend_conformance.py` (`check_catalog` … `check_session`); fake runs every push | The §2.1.7 check ("catalog reads per request bounded by hits") is adapter-level only (`backend_conformance.py:113`, `tests/unit/test_backends.py:127`). No arm- or engine-level check exists (kernel review N8 follow-up, open). |
| 1.2 | Multi-backend write path + per-projection validation | done | `ingestion/generations.py`, `ingestion/write.py`; `tests/contract/test_multi_backend_write.py` (20); `tests/services/test_multi_backend_engine.py` (pg + OpenSearch + Neo4j, real) | MongoDB and Elasticsearch as secondary holders were never run against real services (recorded). |
| 1.3 | postgres adapter | done | `backends/postgres/`; `test_postgres_conformance.py` 23 real | HNSW recall gate samples ≤32 vectors (recorded "lower bound"). |
| 1.4 | sqlalchemy adapter | **partial** (deferred-with-record) | `backends/sql/`; conformance on SQLite + PostgreSQL | MySQL/MariaDB were never tested. Dialect-gated lexical is `planned` (`capabilities.py`, sqlalchemy `lexical`). The package path is `backends/sql/`. |
| 1.5 | opensearch adapter | **partial** (deferred-with-record) | `backends/opensearch/`; kit 6+6 real passes | No generation registry, so it cannot be `storage.catalog` (`capabilities.py` `registry=False`). Aggregations are a bounded scan, not native `cardinality`/`date_histogram` (exact, recorded). The shared Lucene classes are not hoisted out of `opensearch/`. |
| 1.6 | elasticsearch adapter | done | `backends/elasticsearch/`; 16 passed real | No `tests/services` adapter test mirroring OpenSearch (recorded follow-up). |
| 1.7 | neo4j adapter | done | `backends/neo4j/`; 10 + 3 real | Predicate scope resolves through catalog `iter_scope` (≤10000 ids), so §2.1.7 holds only for unfiltered requests (recorded). |
| 1.8 | mongodb adapter | done | `backends/mongodb/`; 16 real | Atlas vector stays `planned` (decision 8). Measures are computed in Python over matched rows (recorded). |
| 1.9 | Providers | **partial** | `providers/{openai,anthropic,openai_compatible,embeddings,capabilities}.py`; `tests/contract/test_providers.py`, `test_streaming_providers.py` | Hosted OpenAI and Anthropic are contract-mocked only (decision 14). The local smoke is one test, not "one per role". No Responses API, no native `ollama`. Streaming covers the generator only. Embedding usage is not in the request budget. `sentence_transformers` embedder and reranker have **no test of any kind** (risk R7). |
| 1.10 | Pipeline spec + engine v2 + validation | done | `pipeline/spec.py`, `pipeline/validate.py`, `engine.py`; `tests/unit/test_pipeline_validation.py`, `tests/contract/test_pipeline_harness.py` | Named pipelines may use only the default pipeline's projection backends (recorded deviation). |
| 1.11 | `doctor --probe` + CLI v2 | **partial** | `pipeline/validate.py:268` `probe_report` (backends probed; `model_calls: 0`); `tests/contract/test_doctor_probe.py` | The plan's per-role minimal provider call is not implemented; declared capabilities are checked against the static registry only. The `release *` CLI stubs are absent (recorded). |
| 1.12 | Storage selectors | done | `core/config.py:133` `StorageConfig`; validation tests | — |
| 1.13 | Runtime: session, memory, tools, actions | **partial** | `runtime/{session,memory,tools,actions,turns,builtin_tools,builtin_actions}.py`; `test_pipeline_harness.py` | `promote_generation`/`rollback_generation` actions are absent (recorded → Phase 2). Plugin tools/actions cannot be loaded (0.3). Session updates are last-writer-wins (release N7). Turn lists are unbounded (risk R4). `ingest_delta` is a full rebuild (risk R2). |
| 1.14 | Decision agents | done | `decision/{rules,llm,cascade,classifier}.py`; `tests/unit/test_classifier_artifacts.py`, `test_llm_agent.py` | The classifier works only with a Python-injected runtime. A config-only classifier (ONNX artifact or plugin) fails at construction, which is fail-closed and matches the row text. ONNX inference is in Phase 2. |
| 1.15 | `exact_id` arm | done | `arms/exact_id.py`; harness tests | — |
| 1.16 | Serving | **partial** | `serving/{http,sse,mcp,auth,parity,discovery}.py`; `tests/contract/test_serving_http.py`, `test_answer_stream.py`, `tests/parity/*` | MCP resources (active manifest) are absent. The `static_api_keys` resolver is absent (decision 6). There is no MCP `notifications/cancelled` test. Non-streaming `/v2/answer` does not cancel on disconnect. All recorded. |
| 1.17 | Packaging, CI, docs | **partial** | `pyproject.toml` (version and extras); `.github/workflows/{ci,docs,services}.yml` | `ci.yml` has no clean-wheel smoke (`tests/smoke_wheel.py` never runs in CI and still uses v1 example + MCP `search` only). There is no evidence-level/private-number docs lint. Schema drift runs inside pytest, not as its own job. CI ingests the v1 `examples/local.yaml` and writes `examples/state` into the checkout (release N10, open). |
| 1.18 | Integration buffer | n/a | — | — |

Phase 1 acceptance:

| Item | Status | Evidence / gap |
|---|---|---|
| Every adapter passes conformance for every declared capability on a real container | **partial** | 100 passed / 17 skipped at `8f6dd56`; sqlalchemy never ran on MySQL |
| Arm on incapable backend / double storage owner → validation error with path | met | `test_pipeline_validation.py`; release N3 fixed (`catalog.registry` flag) |
| `dense` identical membership and order on sqlite, pg, OpenSearch, ES (exact) | **partial** | Only transitively, through fixed expected order on the kit's 4-vector fixture (`check_vector`); no cross-engine corpus run |
| `structured` identical `ScopeResult`s on every records adapter and dialect | **partial** | Per-adapter fixed expectations in `check_records`; MySQL missing |
| `lexical` per-engine conformance + agreement report | met | `docs/LEXICAL_AGREEMENT.md` via `scripts/lexical_agreement.py` (the plan's `tests/services/test_lexical_agreement.py` was replaced by the script) |
| `graph` predicate-supported 1- and 2-hop on sqlite and neo4j | met | `tests/contract/test_arms.py`, `test_neo4j_conformance.py` |
| HTTP and MCP same schemas | met | `tests/parity/test_request_parity.py`, `test_serving_parity.py` |
| `pip install '<pkg>[opensearch,anthropic,mcp]'` in a clean venv | met | verified in this review (see table at top) |
| VALIDATION records each adapter as real-service tested with digests | met | VALIDATION "2.0.0a1 release evidence" |

### 1.3 Counts

| Scope | done | partial | not done | deferred-with-record (subset of partial/not done, or standalone) |
|---|---|---|---|---|
| Phase 0 rows (17, incl. §5 migration) | 13 | 3 | 1 | 2 rows partial-with-record (0.5, 0.16); 0.17 has no record |
| Phase 0 acceptance (7) | 6 met | 0 | 0 | 1 (B8 re-run) |
| Phase 1 rows (17, buffer excluded) | 9 | 8 | 0 | 2 rows wholly recorded (1.4, 1.5); the rest are partly recorded, partly unrecorded |
| Phase 1 acceptance (9) | 6 met | 3 | 0 | 0 |

---

## 2. Owner requirements: where each one stands

| Owner requirement | 2.0.0a1 state | Scheduled (plan row) | Gap not planned anywhere |
|---|---|---|---|
| Harness-like modular architecture: components, registries | Delivered for built-ins: `Component` lifecycle, registries, `RunContext` | — | **Plugin loading from config is not wired** (`Registry.load_plugins` never called; arms/extractors closed literals). No plan row covers it. |
| Sessions / memory | Delivered, minimal (episodic, `exclude_shown`, principal binding, TTL on read) | Phase 2 "Sessions/memory persistence + follow-ups" | Session purge (`SessionManager.expire()` has no caller), turn-list cap |
| Tools / actions | Delivered (4 tools, 2 actions, audit, idempotency, remote deny-by-default) | Phase 2 release row (`promote`/`rollback`) | LLM planner tool use; plugin tools/actions (S1 pipeline follow-ups, unassigned) |
| OpenSearch, Elasticsearch | Delivered: catalog/lexical/vector/records; real-service tested | — | OpenSearch generation registry (cannot be `storage.catalog`); unassigned |
| SQL (generic) | Partial: catalog/records/cache/ledger/session on SQLite + PostgreSQL | Plan Phase 1 row (MySQL, dialect lexical) is in the shipped phase but undelivered | MySQL conformance and dialect lexical are now **unassigned** |
| PostgreSQL | Delivered (FTS, pgvector exact + gated HNSW, records, cache, ledger, session) | graph `planned` | — |
| Neo4j | Delivered (graph + memberships) | Phase 3 traversal extensions; vector Phase 5 | — |
| MongoDB | Delivered except vector | Atlas vector: decision 8 / Phase 5 | — |
| "All major dense DBs" | pgvector, OpenSearch/ES kNN, local NumPy/Faiss | Qdrant, Milvus/Weaviate, LanceDB: Phase 5 (decision 2) | — |
| Extraction: triplets via LLM | Delivered (LLM extractor with grounded relations, predicate support) | — | — |
| Extraction: NER (spaCy, GLiNER), POS (spaCy) | Code present in `extraction/base.py:154-182`; **no v2 test of any kind** | Phase 3 extractors row "real-service GLiNER/spaCy smoke" | Untested in the shipped release |
| Extraction: events, transactional records | Types only (`Event`, `StructuredRecord`); records are document metadata only (`ingestion/write.py:402`) | Phase 3 "Extractors: events, records, membership, GLiNER2, tiered" | — |
| Communities | `entity`, `louvain` + grounded reports | Phase 3 hierarchical communities + stats | — |
| OpenAI / Anthropic / local models | Code delivered; hosted mocked; local OpenAI-compatible smoke 1 test | decision 14 (paid smoke) | Local embedder/reranker (`sentence_transformers`) untested |
| Easy pipeline configuration | Delivered: config v2, profiles, named pipelines, schema, `examples/v2/` | — | Silent acceptance of unconsumed keys (`extraction.ontology`, `judge`, `cache_gate`) |
| MCP and API serving | Delivered: HTTP + SSE, MCP stdio + streamable HTTP, parity | Phase 2 paging/facets/hardening | MCP resources (unassigned) |
| pip-compatible package | Delivered (2.0.0a1 wheel + extras; verified here); not on an index | — | Publishing (owner) |
| Configurable decision agent (rules / classifier / LLM) | rules, llm, cascade delivered; classifier = interface, Python-injected runtime only | Phase 2 "Classifier decision adapter (full)" | Plugin classifier via `decision.component` validates but is never loaded (covered by the plugin gap) |
| Cache arm | Not delivered (`pipeline.arms.cache` correctly rejected) | Phase 2 "Cache arm" (decision 15) | — |
| Learning module from failure batches | Not delivered | Phase 4 "Learning module" | — |

---

## 3. Deferred follow-ups, deduplicated and assigned

Sources: execution-log entries (S1, P1-prep, S2, S2-fix, S3, S4, merges, S5, mongodb, p0-review-fix,
opensearch, providers, elasticsearch, neo4j, postgres/sql, pipeline-harness, multi-backend write,
serving, mongodb-fix, a1-fix, README, release evidence); reviews (kernel, S3/S4, a1 release). Items
confirmed closed in code are left out (e.g. relocated level, quarantine/partial, ACL on
`record_context`, guards rejection, version/extras, OpenSearch suite re-run, lexical agreement report).

| # | Follow-up | Source | Assigned |
|---|---|---|---|
| F1 | Promote `backends/extras.py` protocols into §3.5 with a changelog entry and kit update | S4 | **unassigned** |
| F2 | Recipe-composed index fingerprint from every component | S2, S4 | **unassigned** |
| F3 | `generation/generator.py` as a component | S5 | **unassigned** |
| F4 | Plugin loading for components listed under `plugins:` (tools, actions, guards, classifier) | pipeline-harness; this review | **unassigned** |
| F5 | LLM planner tool use through `ToolRegistry.schemas()` | pipeline-harness | **unassigned** |
| F6 | Write path projects arms of named pipelines | pipeline-harness | **unassigned** |
| F7 | Session concurrency (CAS or per-session serialisation) | pipeline-harness; release N7 | Phase 2 sessions row |
| F8 | `promote_generation`/`rollback_generation` actions, `release *` CLI, `delete_generation`/pointer `status` surface | pipeline-harness, multi-backend, serving | Phase 2 release row |
| F9 | Multi-process pin registry before `gc`; `verify/gc` with deleted manifests | multi-backend | Phase 2 release row |
| F10 | OpenSearch generation registry (shared with ES); hoist Lucene classes to `backends/lucene/` | ES, multi-backend | **unassigned** |
| F11 | Real-service runs with MongoDB and ES as secondary holders; ES/OpenSearch as `storage.catalog` end to end; ES `tests/services` adapter test | mongodb, ES, multi-backend | **unassigned** |
| F12 | MySQL/MariaDB conformance + binary-collation `eq`; dialect-gated sqlalchemy lexical | P1-prep, postgres/sql, release N14 | **unassigned** (was Phase 1) |
| F13 | HNSW filter-recall gate on a larger fixture (pg, OpenSearch, ES) | opensearch, ES, postgres | Phase 5 ANN benchmarks (decision 5) |
| F14 | Neo4j staged multi-transaction load; predicate pushdown by mirroring typed fields | neo4j | Phase 5 (scale) |
| F15 | Atlas vector search | mongodb | decision 8 / Phase 5 |
| F16 | Provider: per-role probe call; Responses API; streaming for planner/reviewer; embedding usage in request budget | providers, serving | **unassigned** (probe was Phase 1); budget part → Phase 2 trace/cost row |
| F17 | Write-path `CallBudget` enforces tokens/USD | providers | Phase 2 ingestion row |
| F18 | Hosted real-provider smoke | providers, release evidence | decision 14 |
| F19 | B8 grounding re-run on frozen cohorts | S1, release N15 | decision 16 |
| F20 | Budget overrun trace event | kernel N12 | Phase 2 trace/cost row |
| F21 | Aggregate all pydantic errors in `validate_config` | kernel N13, pipeline-harness | **unassigned** |
| F22 | §2.1.7 per-request hits-bounded catalog-read check (arm/engine level) | kernel N8, S2-fix | **unassigned** (was Phase 1 kit) |
| F23 | `zero_match_fields` on `AnswerResult` | S3/S4 N11 | **unassigned** |
| F24 | Relocation-aware extraction reuse key | S3/S4 N12 | Phase 2 ingestion row |
| F25 | Records-facet convention in decision agents (`deps.backends["records"]` is a facet, arms use adapters) | S3/S4 N3 | **unassigned** |
| F26 | Community arm: candidate lookup by entity instead of reading every community | S3/S4 N1 (partly fixed) | **unassigned** |
| F27 | `on_violation: fail` vs plan `error` reconciliation | S3 deviation 5 | **unassigned** (doc/plan) |
| F28 | `/v2/values` cursor paging | serving | Phase 2 serving row |
| F29 | MCP resources (active manifest); MCP `notifications/cancelled` test; `serving.mcp` session-binding switch | serving, release N14 | **unassigned** |
| F30 | `serving.stream_tokens` switch for remote surfaces | release N8 | Phase 2 serving hardening |
| F31 | Single auth check for `serve` (config `bearer_env` + CLI); one-line error when `[api]` is missing | release N6, N11 | **unassigned** |
| F32 | v1 leftovers: `config.py` docstring still says "the current engine consumes" v1 (`src/su_multi_arm/config.py:3`); `extraction/legacy.py` public; `fusion.py` | release N9 | **unassigned** |
| F33 | CI: clean-wheel smoke (and update `smoke_wheel.py` to v2 + full MCP tool set), docs evidence-level/private-number lint, `$RUNNER_TEMP` state, v2 example in CI | release N10, S5 | Phase 2 docs row (proposed) |
| F34 | `config upgrade` warning prints only applicable rows; prompt-file inlining | release N16, S2 | **unassigned** |
| F35 | Watch the transient sqlite `disk I/O error` | release N12 | **unassigned** (monitor) |
| F36 | Re-measure Neo4j under the 1.5 GiB cap | P1-prep | **unassigned** |
| F37 | Enable GitHub Pages; badges | README | owner |
| F38 | `exact_set` guard screening prose numerals | S3, S5 | Phase 2 guards row |
| F39 | §5 legacy-read + `migrate-snapshot` (not recorded anywhere) | this review | **unassigned**; needs an owner decision (implement or drop from §5) |

Unassigned: 23 items (F1–F6, F10–F12, F16 probe part, F21–F23, F25–F27, F29, F31, F32, F34–F36, F39).

---

## 4. Risks in the shipped code that no review caught

Each item was checked in code; none appears in any file under `docs/plans/reviews/`.

| # | Risk | Evidence | Impact |
|---|---|---|---|
| R1 | **Lease write locks have no renewal or fencing, and activation is unconditional.** The Mongo and SQL registries take over a lease older than 600 s (`backends/mongodb/registry.py:25,41-44`, `backends/sql/registry.py:28,61`); Elasticsearch after 3600 s (`elasticsearch/registry.py:22,141`). Nothing renews the lease during an ingest. `activate` overwrites `CURRENT` without a compare-and-set (`mongodb/registry.py:82-87`). Elasticsearch `_release` deletes the lock document whoever holds it (`elasticsearch/registry.py:150-152`). | An LLM-extraction ingest that runs longer than the TTL lets a second writer start. Both read the old corpus and both activate; documents from one writer are silently missing from the active generation. On Elasticsearch, the first writer's `finally` also removes the second writer's lock. PostgreSQL (advisory lock) and the sqlite file registry (`flock`) are not affected. |
| R2 | **Every upsert, and the `ingest_delta` action, is a full rebuild.** `ingest` loads the whole previous corpus into memory (`ingestion/write.py:195`, `_load_corpus` `:321`), re-chunks, and **re-embeds every chunk** (`:411`, `:553`). Probe 2: a 1-document upsert re-embedded 16/16 chunks. Embedding calls are not budgeted on the write path (F16/F17). | The action name and docs suggest a delta. With a hosted embedder, each one-record change re-pays the corpus and has no USD cap. RAM bounds corpus size. The action can be allowlisted on HTTP/MCP. Phase 2 plans real delta ingestion, but the shipped action should be renamed or documented as a rebuild until then. |
| R3 | **Configured features that are silently ignored** (the same class as release B3). `extraction.ontology` is accepted, path-resolved (`core/config.py:468,813`) and never read anywhere. `models.roles.judge` and `cache_gate` are accepted with no consumer. Probe 1: valid config, engine constructed. | An operator who sets an ontology to restrict predicates gets no restriction and no warning. This breaks principle 2.1.4 and the AGENTS.md "do not silently ignore" rule. |
| R4 | **Session records grow without bound.** `SessionManager.touch` appends a turn and rewrites the whole record every turn (`runtime/session.py:68-73`), with no cap. MCP binds every call to one engine session per stdio process or MCP session (`serving/mcp.py:76-97`), and the TTL resets on every touch. `SessionManager.expire()` has no caller, so expired rows are never purged (only Mongo has an optional TTL index). The `SessionBinder._bound` map is never evicted. | A long-lived agent connection makes each write O(turns). On MongoDB it eventually exceeds the 16 MB document limit, after which every call fails. Session tables grow indefinitely on sqlite, PostgreSQL and SQL. |
| R5 | **Model drift is invisible to the index fingerprint.** `EmbedderRole.revision` is optional with no warning (`core/config.py:216`). The fingerprint records `revision: None`. The reranker has no revision field and is downloaded lazily on the first request (`engine.py:710`) without a lock. | An upstream model update changes query vectors against stored document vectors with an identical fingerprint, so relevance degrades silently. A first-request download adds latency and lets concurrent requests race to load the model. |
| R6 | **Community arm reads every community per request.** It runs `graph.communities(gen)` (`arms/community.py:45`; sqlite `SELECT * FROM communities`, neo4j equivalent) before any relevance test. | With the `entity` strategy, the community count grows with the corpus, so this is a corpus-proportional read on the request path (principle 2.1.7). Review S3/S4 N1 reordered the catalog reads but did not remove this read. |
| R7 | **Untested shipped code paths for local models.** The v2 spaCy NER/POS and GLiNER extractor branches (`extraction/base.py:154-182`), the `sentence_transformers` embedder (`providers/embeddings.py:49-68`) and the cross-encoder rerank (`engine.py:704-716`) have no unit, fake or real test (the grep finds only config-scanner strings). | These are the owner's "local models" and "NER/POS/GLiNER" requirements. The only real smokes were run against the v1 extractor in 0.1.0a1. |
| R8 | **Capability flags are advisory and some are wrong.** Flags such as `date_buckets`, `top_n` and `filter_pushdown` are not enforced anywhere except `predicate_scope`, `analyzer_id`, `registry` and `memberships`. OpenSearch and ES declare `date_buckets=False` while the shared scan computes buckets. | Low today. It becomes a correctness issue once the planner or validation starts trusting flags. |
| R9 | **Plugins validate but never load** (see 0.3). `decision.component` and plugin guards (now rejected) pass `load_config` when the entry point exists, and the engine then fails at construction with "no classifier runtime". | Fail-closed, so there is no silent ignore. But the extension mechanism the architecture is built around does not work from configuration. |

---

## 5. Recommended Phase 2 work list (ordered)

The plan's Phase 2 rows (61 ed) stay as written. The list below orders them and folds in the
unassigned follow-ups and the risks above. Estimates are engineer-days; new items are marked **+**.

| Order | Item | Folds in | ed |
|---|---|---|---|
| 1 | **+** Write-lock fencing: renewable leases with a generation token, CAS on `CURRENT` (expected previous) in Mongo, SQL and ES registries; ES release only its own lock; contract test with two writers | R1 | 3 |
| 2 | **+** Reject unconsumed config: `extraction.ontology` (until Phase 3), `judge`/`cache_gate` roles without a consumer; a validation test that walks the schema for keys with no reader | R3 | 1 |
| 3 | **+** Rename or document `ingest_delta` as a rebuild until real delta lands; require `budgets` for hosted embedders on the write path | R2 (interim), F17 | 1 |
| 4 | Ingestion jobs, ledger, delta, budgets (plan row) incl. embedding reuse by exact input, relocation-aware reuse key, write-path token/USD enforcement | R2, F17, F24 | 12 |
| 5 | Sessions/memory persistence (plan row) **+** turn cap/rolling window, periodic `expire()`, binder eviction, CAS on session record | R4, F7 | 5 + **2** |
| 6 | Release manifests, promote/rollback/GC (plan row) incl. actions, `release *` CLI, pointer `status`, multi-process pin registry | F8, F9 | 5 + **1** |
| 7 | Guard framework + built-ins (plan row) incl. `exact_set` prose numerals | F38 | 9 |
| 8 | ACL hook (plan row) | — | 4 |
| 9 | Cache arm (plan row; decision 15) | owner requirement | 6 |
| 10 | Trace/cost export (plan row) incl. overrun event and embedding usage in request budget | F20, F16 (budget) | 4 |
| 11 | Classifier decision adapter, full (plan row) **+** wire `Registry.load_plugins` for config-listed components (classifier, guards, tools, actions) with an end-to-end plugin test | F4, R9 | 3 + **3** |
| 12 | Serving hardening + paging/sort/facets (plan row) incl. `/v2/values` cursor, `stream_tokens` switch, MCP resources, cancel test, single auth check, `[api]` missing error | F28–F31 | 8 + **2** |
| 13 | **+** Backend completion carried over from Phase 1: OpenSearch registry, MySQL conformance + dialect lexical for sqlalchemy, Mongo/ES secondary-holder real runs, ES services test | F10–F12 | 6 |
| 14 | **+** Local-model test coverage: fake-module unit tests for spaCy/GLiNER/sentence-transformers/cross-encoder, real CPU smokes in the external workspace, required/warned `revision`, locked reranker load at construction | R5, R7 | 3 |
| 15 | **+** Request-path bounds: community candidate lookup by entity ids; per-request hits-bounded catalog-read conformance check | R6, F22, F26 | 2.5 |
| 16 | **+** Contract hygiene: §3.5 changelog for `extras` protocols, records-facet convention, `on_violation` naming, `zero_match_fields`, pydantic error aggregation, v1 leftovers/docstrings, upgrade warning rows | F1, F21, F23, F25, F27, F32, F34 | 3 |
| 17 | **+** Provider per-role `doctor --probe` call (opt-in, budget-capped) | F16 (probe) | 2 |
| 18 | Docs + VALIDATION (plan row) **+** CI: clean-wheel smoke on v2, docs lint, `$RUNNER_TEMP` state | F33 | 5 + **1.5** |
| 19 | **Owner decision needed:** §5 legacy-read + `migrate-snapshot` (implement ≈5 ed, or drop it from the plan and document "rebuild required") | F39 | 0 / 5 |

Total: plan Phase 2 61 ed + added 31 ed (36 with F39) ≈ **92–97 ed**. Items 1–3 are small and should
land as an `a1.post` patch before any multi-writer or hosted-embedder deployment. Deferred beyond
Phase 2: F2, F3, F5, F6 (Phase 3, with the extractor/generator component work); F13–F15 (Phase 5);
F18, F19, F37 (owner).
