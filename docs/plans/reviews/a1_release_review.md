# 2.0.0a1 release-gate code review

Reviewed: branch `v2/phase-0` at `1d8d783` (as briefed). While this review ran, the branch moved to
`2987bbe` (`beb62c5` + `2987bbe`: MongoDB temporal conformance fix, docs only otherwise); findings
below hold at both commits unless stated. Scope: `git diff main..v2/phase-0` (243 files), plan
`docs/plans/v2_implementation_plan.md` §2.1, §4 Phase 0/1, §5, §6, and `docs/plans/execution_log.md`
deviations/assumed defaults. Real-service results were reviewed from `docs/VALIDATION.md` only (no
containers were started or stopped).

## Verdict

**VERDICT: FIX-FIRST**

The kernel, contracts, six adapters, providers, decision agents, pipelines, harness objects and both
serving surfaces are real and well tested. Local gates pass: `ruff check src tests` clean;
`pytest -m 'not external'` 733 passed at `1d8d783` and 734 at `2987bbe`. One earlier run had a
transient sqlite `disk I/O error` that did not come back in 8 reruns (N12). The wheel and sdist
build, the core wheel installs with only `[dev]`, and the README quick start works end to end in a
fresh venv. Seven problems still block the release:

- B1: a cross-slice ACL bypass.
- B2: a crash in the agentic review path on a valid plan.
- B3: configured guards validate but never run (silent ignore).
- B4: the originating service names appear in public docs.
- B5: the release identity is still `0.1.0a1`, and the plan's provider extras are missing.
- B6: the README and parts of the docs describe the v1 alpha.
- B7: three Phase 1 acceptance items have no evidence recorded at the release commit.

Each fix is small (hours, not days), except B7, which depends on the real-service run the
coordinator is doing now.

## Blocking findings

| id | file:line | finding | evidence | required change |
|---|---|---|---|---|
| B1 | `src/su_multi_arm/engine.py:518-534` (`_canonical_record`), used at `:554` (decision request) and `:536-540`/`:942` (generator payload) | **ACL bypass through `record_context`.** The caller sets `record_context.record_id`. The engine then loads that record with `catalog.get(ctx.generation, [record_id])` **without the principal's `ctx.constraints()`** and sends its title, fields and metadata to the planner (a remote LLM). It also sends them to the generator as `canonical_record`, so the answer text can repeat them. Every other read path (arms, `exact_id`, `documents_in`, `values_in`) applies the ACL, so this is a cross-slice gap. | Repro (scratchpad test): engine with `StaticPolicy(field="source")`, principal `tenant="article"`, `record_context={"record_id": "case:certificate"}`. The hits are correctly all `article`, but the planner payload contains the full out-of-scope `case:certificate` record (title, description, resolution, product, status). | Resolve the canonical record through an ACL-scoped read: `catalog.iter_scope(ctx.generation, Scope(predicates=ctx.constraints(), document_ids=[record_id]), Page(size=1))`, the same way `documents_in` does. When the record is out of scope, treat it as not found and record that in the trace. Add a contract test that covers both the planner and the generator payload. |
| B2 | `src/su_multi_arm/engine.py:1007` | **The review hop crashes when a re-planned plan starts with a structured step.** `query=decision.plan.steps[0].query` assumes a `RetrievalStep`, but `StructuredStep` has no `query`. The LLM planner may legally return `[{"arm":"structured",...}, ...]` on a follow-up. The result is `AttributeError`, which `serving/http.py:call` does not map (HTTP 500), which becomes an `internal` stream error, and which reaches MCP as a generic `ToolError`. | Repro: `llm_engine(..., mode="agentic")`. The second planner reply is `plan({"arm":"structured","scope":{"measure":{"kind":"count"}}}, {"arm":"lexical","query":"rounding"})`, and it raises `AttributeError: 'StructuredStep' object has no attribute 'query'`. | Use the follow-up query that the review stage asked for, or the first `RetrievalStep`'s query (`next((s.query for s in steps if hasattr(s, "query")), None)`). Add a regression test in `tests/contract/test_review_stage.py`. |
| B3 | `src/su_multi_arm/core/config.py:43,440-452`; `src/su_multi_arm/pipeline/validate.py` (guard check); `engine.py` (no guard stage) | **Configured guards are silently not executed.** `BUILTIN_GUARDS` is empty, but `pipeline.guards` accepts any name listed under `plugins:`. `load_config` only checks that the entry point exists, and the engine never loads or runs a guard. An operator who lists a safety guard gets a valid config, a working server and no guard. This breaks principle 4 and AGENTS.md ("do not silently ignore"). | `grep -rn guards src/su_multi_arm` shows no consumer outside config/validation. `docs/CONFIGURATION.md:521` says guards are allowed "when listed plugins". | Until Phase 2 ships the guard runtime, reject a non-empty `pipeline.guards` (and `pipelines.*.guards`) with a `ConfigError` that gives the path ("guards are not executed in this release"). Do the same for any other `plugins:`-named component kind that the engine cannot load. |
| B4 | `docs/BUSINESS_SUMMARY.md:40`, `docs/MIGRATION.md:3`, `docs/VALIDATION.md:68` | **Public-repo hygiene: the originating services are named.** The text names the two originating services directly (three occurrences). The repo is about to be pushed publicly. | `git ls-files \| xargs grep -nIiE '<origin names>'` | Replace these with neutral wording ("the historical support-search services"). Add the terms to the docs hygiene denylist (plan §6.2 `docs` job; see N10). Also check `main` history before publishing (see N13). |
| B5 | `pyproject.toml:7`, `src/su_multi_arm/__init__.py:13`, `src/su_multi_arm/serving/http.py:76` (`FastAPI(version="0.1.0a1")`); `pyproject.toml:14-28` | **Release identity and extras.** The candidate builds as `su_multi_arm-0.1.0a1`, not `2.0.0a1`. Plan Phase 1 lists the extras `openai`, `anthropic` and `local`, and none of them exist. The plan's own acceptance command `pip install '<pkg>[opensearch,anthropic,mcp]'` prints `WARNING: su-multi-arm 0.1.0a1 does not provide the extra 'anthropic'` (verified in a fresh venv). | Wheel name in `~/su_mutliarm_tests/claude/review_a1/dist`; pip output. | Set the version to `2.0.0a1` from one source (and have `http.py` use `__version__`). Add `openai = []` and `anthropic = []`; they can stay empty because the providers use `httpx`, so pip stops warning. Add `local = ["sentence-transformers>=3.0"]` or alias it to `semantic`. Re-run the clean-wheel smoke. |
| B6 | `README.md` (whole "What is configurable?" section), `docs/CONFIGURATION.md:494-497`, `docs/PLAN.md:6-20`, `docs/VALIDATION.md:1-10`, `examples/` | **The docs describe the v1 alpha.** The README lists only SQLite/PostgreSQL/Neo4j/Faiss backends and "Routing: raw query, one-pass planned search, or bounded agentic" (v1 `routing.mode`). It does not mention OpenSearch, Elasticsearch, MongoDB, generic SQL, the decision agents, pipelines, sessions/tools/actions, SSE, or MCP streamable HTTP. CONFIGURATION says "the engine still consumes the v1 model … so the shipped examples remain v1 files", which is no longer true. `docs/PLAN.md` is titled "Release 0.1.0a1 scope". The VALIDATION header says "Release 0.1.0a1 … 40 tests passed". Every file in `examples/` is a v1 config; there is no runnable v2 example for any first-release backend. | Read-through; `grep -l "schema_version: 2" examples` is empty. CONFIGURATION has one 14-line v2 snippet, which validates. | Rewrite the README for 2.0.0a1: backends with their operation sets, providers, decision agents, pipelines, harness, serving, extras and the quick start. Fix CONFIGURATION:494-497. Mark PLAN.md as superseded by `docs/plans/v2_implementation_plan.md`, or update it. Add a dated 2.0.0a1 summary at the top of VALIDATION.md. Ship at least `examples/local_v2.yaml` (output of `config upgrade`) and one multi-backend v2 example (e.g. postgres + opensearch + neo4j, env-var names only). |
| B7 | `docs/VALIDATION.md:608, 641-642, 711, 832, 994-1016` | **Phase 1 acceptance evidence is missing at the release commit.** (a) The lexical cross-engine agreement report (plan Phase 1 acceptance, §3.5) "has not been produced". (b) Real-service conformance was recorded per slice at different commits, and `1d8d783` itself failed MongoDB `records` conformance (fixed in `beb62c5`), so the candidate must be at least `2987bbe`. (c) The real-service smoke on a local OpenAI-compatible server ("one … smoke per role", Phase 1 Providers row) was "not run". | Quoted VALIDATION lines. | Record the coordinator's consolidated run at the release commit: every adapter's conformance suite plus `tests/services/*`, with image digests. Produce the lexical agreement report on the shared synthetic corpus. Either run `tests/services/test_local_llm.py`, or record an owner-approved deviation for (a) and (c) in `execution_log.md`. Tag 2.0.0a1 only from that commit. |

## Non-blocking findings

| id | where | finding | suggestion |
|---|---|---|---|
| N1 | `backends/sqlite/catalog.py:145-160`, `backends/postgres/catalog.py:185-200` (same pattern in `sql`, `mongodb`) | `RecordStore.execute` loads every matched record row into Python, and `matched_ids` (all of them) is returned in `SearchResult.aggregates[].document_ids`. An unfiltered `count` over a large corpus is a whole-corpus load on the request path (principle 7). The Lucene adapters bound this with `max_matched`; the SQL-family and Mongo adapters do not. | Push `count`/`group_count` down to SQL/aggregation and cap `matched_ids`. At minimum, apply the same `max_matched` typed error on every adapter. |
| N2 | `backends/_scope.py:253-263`, `generation/contract.py:86-95` | `ScopeResult.exactness` stays `"exact"` even when predicates were dropped as `unsupported`, and contract rule 2 accepts a `count` claim when `exact()` is true. Today the decision layer rejects non-filterable planner predicates first (`invalid_step`), so no engine path produces this. It is a latent hole in defence in depth. | Return `exactness: "bounded"` (or require `not unsupported` for `count`/`exhaustive_list`) when any predicate was not applied. |
| N3 | `ingestion/write.py:101-104` vs `core/config.py:749-764` | `config validate` accepts `storage.catalog: <opensearch>` (verified), but `Engine` construction then refuses it ("no generation registry"). The refusal is typed and documented at CONFIGURATION:253, but it comes late. | Move the generation-registry requirement into `config_diagnostics` (capability flag `catalog.registry`). |
| N4 | `serving/http.py:40-59` | `BackendError`, `GenerationDisagreement`, `ProviderError`, "no active index" and `RunCancelled` all become **422** with the generic text. These are server or dependency failures, not client errors. | Map backend and generation errors to 503, provider errors to 502, and cancellation to 499/408. Keep the generic detail. |
| N5 | `decision/llm.py:147` | `except (ProviderError, RuntimeError, ValueError, ...)` turns `RunCancelled` and `BackendError` (both `RuntimeError`) into `DecisionFailure("provider_error")`. Under the opt-in `raw_all_arms` policy, a backend outage during value resolution then fails open, and the failure is mislabelled. | Re-raise `RunCancelled`/`BackendError` (or map them to their own categories) before the broad catch. |
| N6 | `cli.py:163`, `serving/auth.py:75-84` | `serve` ignores `serving.http.bind` and `auth: bearer_env` in its non-loopback check. It requires `--token-env` even when the config sets bearer auth, while `mcp` accepts the config. `create_app(config)` used outside the CLI resolves `LocalResolver` whatever the bind address. Both fail safe, but they are inconsistent. | Use one check: config `serving.http` plus CLI override. Document that `create_app` without a resolver is loopback-only. |
| N7 | `runtime/session.py:71-79`, `runtime/turns.py:103-120` | Session updates are last-writer-wins. Two concurrent requests in one session can lose turns and under-count the session budget. | Add an optimistic version (CAS) on the session record, or serialise per-session turns. |
| N8 | `serving/mcp.py` (answer progress), `serving/sse.py` | Provisional `token` deltas, which may later be superseded by a contract abstention, reach remote clients. This is documented and flagged (`provisional: true`, `tokens_superseded`). | Consider a `serving.stream_tokens` switch, default off for remote surfaces. |
| N9 | `src/su_multi_arm/config.py:1-5`, `extraction/legacy.py` (re-exported by `extraction/__init__.py:4-5`), `fusion.py` (tests only), `docs/ARCHITECTURE.md:38` (`Retriever`) | v1 leftovers. The `config.py` docstring says the v1 model "remains the v1 model that the current engine consumes". The v1 `Extractor` is still public API. | Fix the docstrings, mark `legacy`/`fusion` as compat/test-only or move them under `compat/`, and fix ARCHITECTURE:38. |
| N10 | `.github/workflows/ci.yml` | No clean-wheel install smoke, no schema-drift job separate from pytest, no docs hygiene denylist job (plan §6.2; it would have caught B4). `ingest` writes `examples/state` inside the checkout. The `[openai,anthropic]` extras are not installed. | Add a docs job with the denylist grep, run `tests/smoke_wheel.py`, and use `$RUNNER_TEMP` for state. |
| N11 | `cli.py:165`, `cli.py:174` | `serve` without the `[api]` extra fails with a raw `ModuleNotFoundError: uvicorn` traceback (verified in the fresh `[dev]` venv). | Show a one-line error naming the extra. |
| N12 | `tests/contract/test_engine_support_example.py::test_planner_context_separate_and_capability_enforced` | One transient `IngestError: sqlite.runtime: disk I/O error` on the first run (while the coordinator was committing). It did not recur in 8 reruns. | Watch for it in CI. If it recurs, check `basetemp` isolation and sqlite WAL on the fixture path. |
| N13 | `docs/CONFIGURATION.md:341`, `ingestion/normalize.py:13`, `examples/mapped_documents.json` (`Issue__c`, `Knowledge.Title`), `examples/*.yaml`/`documents.jsonl` (a tracker product name), `docs/VALIDATION.md:75` ("live GPU services"); git history | Secondary hygiene. A CRM vendor's field naming and a tracker product name (prior review N9) are still present, and there is a reference to private infrastructure. `main` history contains the B4 names. | Neutralise the remaining names. Before the public push, either rewrite history or publish from a fresh root commit. |
| N14 | `execution_log.md:723`, `:49` | MCP resources (active manifest) are not added, and MySQL is omitted from the sqlalchemy dialects. Both are recorded deviations from plan §3.15 and the Phase 1 sqlalchemy row. | List them in the release notes as known gaps. |
| N15 | `docs/VALIDATION.md:211-212` | The Phase 0 external evidence (B8 bounded local-model re-run over the frozen public cohorts) has not been run. The plan says it is not a CI gate and needs owner authorisation. | Track it as an open owner decision in the release notes. |
| N16 | `compat/config_v1.py:255-290` | The upgrade warning prints the whole mapping table rather than the changes that apply to the given file. | Emit only the rows that apply. |

## Acceptance table

### Phase 0 acceptance (plan §4)

| Item | Status | Evidence |
|---|---|---|
| The 40 v1 tests pass or are replaced with the same edge cases; support fixtures under `support_example` | met | `tests/contract/test_engine_support_example.py` (30 tests, "replaces …" headers); profile `support_example` |
| Notes §4 acceptance 1–5 | met (synthetic) | `tests/unit/test_fail_policies.py`, `tests/unit/test_answer_contract.py`, `tests/unit/test_intent.py` |
| Grounding fixture pack and offset-map property test | met | `tests/test_grounding.py` (fixture categories plus 500 random property cases) |
| Five `hosted` occurrences → one `co_occurrence` edge with five provenance entries | met | `tests/unit/test_write_path.py::test_five_inherited_title_rows_collapse_to_one_co_occurrence_with_five_occurrences`; `tests/test_grounding.py:184-204` |
| Nine NL-count probes → abstain or flagged, zero fabricated numbers, under `fail_closed` and `raw_all_arms` | met | `tests/unit/test_count_probes.py::test_rules_agent_never_fabricates_a_count`, `::test_cascade_with_fabricating_planner_never_fabricates_a_count` |
| v1 config upgrade round-trips its fingerprint | met | `tests/unit/test_config_v2.py::test_every_example_upgrades_deterministically_and_round_trips_its_fingerprint`; `config validate examples/local.yaml` works in the fresh venv |
| External B8 re-run over frozen public cohorts | not met (owner-gated, not a CI gate) | VALIDATION.md:211-212 (N15) |

### Phase 1 acceptance (plan §4)

| Item | Status | Evidence |
|---|---|---|
| Every adapter passes conformance for every declared capability on a real container | partially met | Per-slice runs recorded (VALIDATION.md:489-536, 565-612, 647-713, 714-770, 771-841, 994-1016). `1d8d783` failed MongoDB records conformance (fixed in `beb62c5`). No consolidated run at the release commit (B7) |
| Arm on a backend lacking the capability / two backends for one storage slot → validation error with path | met | `tests/unit/test_pipeline_validation.py::test_validation_aggregates_every_problem_with_paths`; `core/config.py:749-764`. N3 is the exception (OpenSearch catalog is refused only at engine construction) |
| `dense` identical membership and order on sqlite, postgres, opensearch and elasticsearch (exact) | partially met | `testing/backend_conformance.py::check_vector` asserts fixed expected order on every adapter, so they are transitively identical on a 4-vector fixture. No dedicated cross-engine corpus run |
| `structured` identical `ScopeResult`s on every records adapter and dialect | partially met | `check_records` asserts fixed expected results per adapter; sqlalchemy is tested on SQLite and PostgreSQL only (MySQL omitted, recorded deviation) |
| `lexical` per-engine conformance plus a cross-engine agreement report in VALIDATION.md | partially met | Per-engine conformance passes; the report is **not produced** (B7a) |
| `graph` one- and two-hop predicate-supported traversal on sqlite and neo4j | met | VALIDATION.md:714-770 (neo4j real service); `tests/contract/test_arms.py` |
| HTTP and MCP accept and return the same schemas | met | `tests/parity/test_request_parity.py`, `tests/parity/test_serving_parity.py` (stdio and streamable-HTTP subprocess/loopback); start-up drift check `serving/mcp.py:check_drift` |
| `pip install '<pkg>[opensearch,anthropic,mcp]'` in a clean venv | partially met | Installs, but warns that the `anthropic` extra does not exist (B5) |
| VALIDATION.md records each adapter as real-service tested with image digests | met per slice | Digest table at VALIDATION.md:229-235; consolidated release entry pending (B7) |

### Owner first-release list

| Requirement | Status | Evidence / note |
|---|---|---|
| OpenSearch | covered | `backends/opensearch/`; catalog, lexical, vector, records; cannot be `storage.catalog` (N3) |
| Elasticsearch | covered | `backends/elasticsearch/` with a generation registry |
| Generic SQL | covered (catalog, records, cache, ledger, session) | `backends/sql/`; lexical and vector are `planned` (rejected by validation) |
| PostgreSQL | covered | `backends/postgres/`; FTS, pgvector exact, records, cache, ledger, session |
| Neo4j | covered | `backends/neo4j/`; graph plus memberships |
| MongoDB | covered | `backends/mongodb/`; vector is `planned` (Atlas only) |
| OpenAI / Anthropic / local providers | covered (code); real-service local smoke not run | `providers/openai.py`, `anthropic.py`, `openai_compatible.py`, `embeddings.py`; mocked contracts plus streaming. B7c, and B5 for the extras |
| Configurable decision agent (rules / classifier / LLM) | covered | `decision/rules.py`, `classifier.py` (ONNX contract, no weights), `llm.py`, `cascade.py`; fail policies with `fail_closed` as the default |
| Declarative pipelines | covered | `pipeline/spec.py`, named pipelines, request allowlists |
| Sessions / memory / tools / actions | covered (minimal) | `runtime/session.py`, `turns.py`, `builtin_tools.py`, `actions.py`; `tests/contract/test_pipeline_harness.py::test_memory_is_never_evidence_a_prior_turn_id_cannot_be_cited`. N7 (concurrency) |
| API and MCP serving | covered | HTTP v2, SSE, MCP stdio and streamable HTTP; live loopback smoke done in this review |
| pip-installable with extras | partially covered | Core installs with no optional extras (verified); version and extras issues (B5) |
| Cache arm, guards, incremental ingestion, learning, ontology | not in scope (Phases 2–4) | **But** configured guards are silently accepted (B3) |

### Cross-slice integration checks (gate item 2)

| Check | Result |
|---|---|
| Generation pinning on every request path | **holds.** `_begin`/`_context_for` pin once per request through `WritePath.pin()` (`GenerationSet.check`). `search`, `answer`, `answer_stream`, review hops (same ctx), `call_tool` → `search_in`/`documents_in`/`values_in`/`aggregate_in`, `list_values`, HTTP and MCP all go through the engine. Generation-switch test: `test_generation_switch_mid_session_is_pinned_per_answer_and_reported` |
| AnswerContract on every path that returns an answer | **holds.** `answer` and `answer_stream` share `_answer_turn` → `pipe.contract.enforce`. The `done` event carries the enforced result. There is no answer-producing tool. Planner abstain and disabled generator also pass through the contract |
| Budget and cancellation on stream disconnect | **holds** for SSE (`sse.py:event_source` finally → `cancel`) and MCP (`mcp.py` answer finally). Tests: `test_client_disconnect_cancels_the_run_and_its_budget`, `test_cancel_stops_the_run_closes_the_provider_stream_and_cancels_the_budget`. Non-streaming `/v2/answer` does not cancel on disconnect (acceptable). See N5 |
| Session memory never cited | **holds.** The generator payload is question, record context and packed evidence only (`engine.py:942`); memory only feeds `exclude_ids`. Tests listed above |
| ACL on every read | **fails**: B1 |
| Capability validation vs engine construction | Mostly consistent; N3 |
| v1 config upgrade through the final engine | **holds.** `Engine(ConfigV1)` upgrades in memory; the README quick start on v1 `examples/local.yaml` passes |
| Config schema matches runtime | **holds.** `config schema` output is identical to `docs/config.schema.json` and `docs/config_v1.schema.json` |
| Dead v1 modules or imports | Minor leftovers only (N9); no import of the removed `store.py`/`retrieval.py`/`adapters/` |
| Consistent error typing | Typed hierarchy in `core/errors.py`; HTTP mapping is coarse (N4); decision catch is too broad (N5); B2 surfaces as an untyped 500 |

### Safety (gate item 3)

| Check | Result |
|---|---|
| No code loading from config | **holds.** No `import_module`/`eval`/pickle paths; `core/component.py` rejects pickle and `module:attr`; `trust_remote_code=False` (`engine.py:687`, `providers/embeddings.py:62`); `np.load(allow_pickle=False)`; resolvers are Python-injected only |
| No generated SQL/Cypher | **holds.** Typed `ScopeSpec`/`TraversalSpec` compilers with bound parameters; identifiers are digest-derived or validated config |
| No silent fallback/retry/repair | **holds** except B3 (guards). `raw_all_arms` is opt-in and flagged |
| Grounding fails closed | **holds** (strict default; quarantine and partial are explicit and recorded) |
| Secrets by env-var name only | **holds** (`*_env` fields, `core.config.secret`); no credentials in tracked files beyond disposable test passwords for the compose stack |
| Safe auth default on a non-loopback bind | **holds** for the CLI and config validation; an unset token env fails closed (`BearerTokenResolver("")` raises). N6 |

### Packaging and README (gate items 4 and 6), reproduced in `~/su_mutliarm_tests/claude/review_a1`

- `python -m build` from `git archive 1d8d783` gives `su_multi_arm-0.1.0a1.tar.gz` and `-py3-none-any.whl` (163 files; profiles YAML included).
- In a fresh venv (Python 3.13.5), `pip install 'wheel[dev]'` pulls in only pydantic, PyYAML, numpy, httpx, snowballstemmer and the dev tools. `import su_multi_arm` works, `su-multi-arm --help` works, and `config validate examples/local.yaml` returns valid (v1 upgraded, with a warning).
- README quick start (`ingest`, `search`, `search --request aggregate.json`, `status`, `evaluate`) passes in the `[dev]` venv. `answer` returns the disabled-generator abstention with `contract: passed`. `serve` needs `[api]` (N11). With `[api,mcp]`, a live `serve` on loopback answered `/ready` 200, `/v2/search` and `/v2/capabilities`, and `/v2/answer/stream` emitted `decision → arm_result×4 → evidence → contract → done`.

## Claims verified against code

| Claim | Result |
|---|---|
| `answer_stream`'s `done` carries the same `AnswerResult` as `answer` | True: same `_answer_turn` (`engine.py:957-994`) |
| Token deltas are provisional and superseded by the contract | True (`runtime/events.py:69-86`) |
| SSE and MCP errors do not leak provider/config text | True (`sse.py:public_error`, `mcp.py:safe_message`) |
| Actions are denied on remote surfaces unless allowlisted and the principal has the `actions` scope | True (`runtime/actions.py:119-128`) |
| `doctor --probe` makes no model calls | True (`pipeline/validate.py` docstring and code; `test_probe_reports_backends_and_provider_pairs_without_model_calls`) |
| Lucene records scans are bounded | True (`opensearch/catalog.py:230-245`, `max_matched`); not true for the SQL-family and Mongo adapters (N1) |
| "Every read is ACL-scoped" (`runtime/acl.py` docstring) | **False** for `_canonical_record` (B1) |
| "Guards that are neither built-in nor listed plugins are rejected" (CONFIGURATION:521) | True, but listed plugin guards are then never run (B3) |
| "The engine still consumes the v1 model" (CONFIGURATION:494-497; `config.py` docstring) | **False**: the engine is v2 (B6, N9) |
| `opensearch` cannot be `storage.catalog` (CONFIGURATION:253) | True, but only at engine construction (N3) |
| ruff clean; `pytest -m 'not external'` green | True: 733 passed at `1d8d783`, 734 at `2987bbe`; one transient failure (N12) |
