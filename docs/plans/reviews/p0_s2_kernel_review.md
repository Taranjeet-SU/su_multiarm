# Review: Phase 0 / S2 kernel (commits `e4f2ff1`, `9b89f0f`, branch `v2/phase-0`)

Reviewer: plan-reviewer agent applied to code, 2026-11-03. Artifact: `git diff 7a7f3eb..9b89f0f`.
Checked against `docs/plans/v2_implementation_plan.md` §2.1, §3.1, §3.1a, §3.2, §3.3, §3.4, §3.5, §5
and the S2 entry in `docs/plans/execution_log.md`. Nothing in the source was modified.

Evidence gathered: `ruff check src tests` clean; `pytest -q -m 'not external'` → **156 passed, 3
deselected** (interpreter `.venv`, Python 3.13.5, `mcp` present; the commit message's 154/2 was the
same suite without `mcp`); a probe script run with `python -I` against the slice
(`~/su_mutliarm_tests/claude/review_s2/probe.py`) exercising config rejection paths, every
`examples/*.yaml` upgrade and fingerprint round-trip, schema drift, and every facet of the in-memory
fake. Findings below cite `file:line` in the committed tree.

## VERDICT: REVISE

The slice is substantially what the plan asked for: all eight protocols exist with the §3.5
operation sets (plus explicit paging, `get_chunks`, `get_raw`), the in-memory fake behaviourally
satisfies each of them, config v2 fails closed with key paths on every invariant listed in §3.3,
every shipped example upgrades byte-deterministically and round-trips its v1 fingerprint, the schema
files have no drift, the runtime objects are real typed contracts (reservation budget with USD via an
operator price table, deadline, masked trace, pinned `GenerationId`, principal-scoped sessions,
audited idempotent actions, read-only tool registry), and nothing in the slice retries, falls back,
or imports code outside the `plugins:` entry-point path. Three things block: the code-reference
scanner rejects legitimate values (any `name:tag` model id such as Ollama's `mistral:latest`, and a
bare `intent.onnx` artifact path) with a misleading "dotted import path" error — a valid config is a
wrong build; the capability matrix is not honest at the capability level (config validation accepts
`storage.catalog: pg` or `communities: neo4j` although neither v1 nor v2 code serves them); and the
plan's frozen-contract text (§3.1a, §3.5, §5) was not amended for the freeze-time deviations, so the
next implementer slices (sqlite adapter, arms) would build to a §3.5 that no longer matches the frozen
code. All three are cheap to fix.

## Blocking findings

| Id | Plan section | Finding | Evidence | Required change |
|---|---|---|---|---|
| B1 | §3.2 / §3.7.3 artifact rules; §3.3 "no value can name code" | The config-wide code-reference scan treats any `identifier:identifier` string as a `module:attr` factory. Model ids with a tag (`mistral:latest`, `llama3:instruct`, `phi3:mini` — the exact shape of the local `openai_compatible` extractor the plan's §3.3 sketch shows) are rejected at `models.roles.*.model` with "dotted import paths are not loadable from configuration". In `names_code=True` mode a bare relative artifact file name (`intent.onnx`) is also rejected as a dotted import because `DOTTED_IMPORT` matches `name.ext`. Both are valid configs turned into misleading errors. | `src/su_multi_arm/core/component.py:129-148` (`DOTTED_FACTORY`, `DOTTED_IMPORT`, `check_code_reference`), `:165-171` (`ArtifactRef.check_path`); `src/su_multi_arm/core/config.py:606` (scan over the whole dump). Probe: `[ollama tag model 'mistral:latest'] RAISED ArtifactError`, `[bare relative 'intent.onnx'] RAISED ArtifactError`, while `models/intent.onnx` passes. | Tighten `DOTTED_FACTORY` to require a dotted module part (`^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+:[A-Za-z_]\w*$`) and/or exempt `models.roles.*.model` from the factory rule; in `names_code` mode skip the dotted-import rule when the suffix is in `ALLOWED_ARTIFACT_SUFFIXES`. Add regression tests for `mistral:latest`, `llama3:instruct`, `intent.onnx` (accepted) alongside the existing rejections. |
| B2 | §3.5 capability matrix ("honest status"); §2.1.4 no silent fallback; §3.3 storage/arm validation | `status` is per adapter, but the declared capability sets for `legacy` adapters include contracts that exist in neither v1 nor v2 code: `postgres` declares `catalog`, `records`, `cache`, `ledger`, `session` (v1 `adapters/postgres.py` is vector + FTS only, as the matrix's own note says); `neo4j` declares `graph` for `storage.communities` and the `community` arm although v1 only exports facts. `Config.coherent` therefore validates `storage: {catalog: pg, ledger: pg, sessions: pg}` and `arms.community: {backend: neo4j}` with nothing behind them, contradicting the docs claim that an unavailable capability is a validation error. | `src/su_multi_arm/backends/capabilities.py:79-92` (postgres), `:108-113` (neo4j), `:158-159` (`declares` ignores status); `src/su_multi_arm/core/config.py:571-579`, `:609-614`. Probe: `[postgres as ledger/sessions/catalog] OK -> catalog='pg' ... ledger='pg' sessions='pg'`, `[neo4j as communities] OK`. | Record evidence per `(adapter, capability)` (e.g. a `status` or `implemented_in: contracts|v1|planned` flag inside each capability's `Flags`, or a `planned_capabilities` set) and make `Config._require_capability` reject pairs that are `planned`. `sqlite` may keep all eight only because the S3 port is in the same phase; say so in its note. Update `test_matrix_is_consistent_with_contracts_and_config_kinds` and `docs/CONFIGURATION.md` accordingly. |
| B3 | Phase 0 freeze rule ("any change after the freeze requires a changelog entry here and a conformance-kit update"); §3.1a; §3.5; §5 | The frozen code deviates from the plan text in ways the next slices will build against, but the plan was not amended: `Catalog.get_chunks`, `LedgerStore.get_raw`, `LexicalIndex.search -> PageResult[ScoredChunk]` (plan: `list[ScoredChunk]`), `VectorIndex.upsert/search(namespace=...)`, the `Scope` (pushdown) vs `ScopeSpec` (typed query) split, per-capability facets, `MetadataSchema.unknown_fields`, and the narrowed N6 round-trip guarantee (deviation 8). The `implementer` agent is briefed to build "exactly to the plan's module contracts"; S3/arms would read a stale §3.5. Deviation 8 is also silent at upgrade time: a v1 file that set `community.evidence_char_budget` or a provider-`none` model block with non-default fields upgrades without warning and `legacy_fingerprint()` then differs from the v1 fingerprint (probe: `roundtrip equal: False`). | `src/su_multi_arm/backends/contracts.py:69,84-86,93-101,155` vs plan lines 361-369; `src/su_multi_arm/core/types.py:292,411-416,444-451`; `src/su_multi_arm/compat/config_v1.py:303-317,384-387` (only the `sql_fields` warning exists); `docs/plans/execution_log.md` S2 deviations (1)-(4), (8). | Add a rev-3 changelog row and amend §3.1a (`unknown_fields`), §3.5 (method list, `PageResult`, `namespace`, facets, `Scope`/`ScopeSpec`), and §5 (round-trip holds for the v1-effective subset; list the lossy fields). In `upgrade()`, detect the lossy condition and append a warning (or refuse with `ConfigError` when `strict=True`) so the fingerprint change is never silent. |

## Non-blocking findings

| Id | Finding | Evidence | Suggestion |
|---|---|---|---|
| N1 | The USD/`pricing_key` config check is vacuous: `role.model` has `min_length=1`, so `not (role.pricing_key or role.model)` never fires. The runtime rule lives in `Budget.reserve_call` (raises when `pricing_key` is falsy). | `src/su_multi_arm/core/config.py:591-594`; `src/su_multi_arm/runtime/budget.py:162-164` | Either implement the documented default (`pricing_key or model`) where the engine builds reservations, or drop the no-op check. |
| N2 | Trace masking is key-name based; `highlights` (passage fragments on `ScoredChunk`), `title`, `snippet`, `content`, `question` are not in `SENSITIVE_KEYS`. | `src/su_multi_arm/runtime/trace.py:23-44`; `src/su_multi_arm/core/types.py:500` | Add those keys; document that arms must wrap free text in `Sensitive(...)`. |
| N3 | `Page.size` is capped at 1000 while `pipeline.arms.{dense,lexical}.candidates` allows 10000; `MemoryLexical.search(k=5000)` raises a pydantic `ValidationError` (not `BackendError`) from `Page(size=k)`. | `src/su_multi_arm/core/types.py:480`; `src/su_multi_arm/core/config.py:256,266`; `src/su_multi_arm/backends/memory.py:282` | Align the caps or page internally; contract failures must be `BackendError`. |
| N4 | The fake's `RecordStore.upsert` pre-filters to declared keys, so `unknown_fields: reject` is never enforced for records; `execute` applies predicates on undeclared/non-filterable fields without a `field_not_filterable` unsupported entry. Acceptable if `ScopeSpec` compilation gates, but the conformance kit should assert it. | `src/su_multi_arm/backends/memory.py:340,356-362`. Probe: undeclared key accepted under `reject`; predicate on `zzz` applied. | Honour the schema in the fake and add a conformance row. |
| N5 | `MemoryVector.search` silently uses `chunk_id` as `document_id` when the chunk is unknown to the catalog, so scope pushdown is evaluated against the wrong id. | `src/su_multi_arm/backends/memory.py:325-327` | Raise `BackendError` (the fake claims to enforce adapter invariants). |
| N6 | `_compare` falls back to lexicographic comparison when a `range` bound on a datetime field fails to parse; only `date_interval` reports `timezone_missing`. | `src/su_multi_arm/backends/memory.py:70-81,356-362` | Report `unparseable_bound`/`timezone_missing` for `range` on datetime/date fields too. |
| N7 | `SessionManager.get` raises a bare `KeyError` for unknown/expired sessions. | `src/su_multi_arm/runtime/session.py:58-59` | Use a typed `SuError` so serving maps 404 vs 403 deterministically. |
| N8 | Tests: `Catalog.get_chunks` is never called (0 hits in `tests/`); `test_memory_is_not_evidence...` asserts the absence of method names (tautological); `stats["catalog.get.ids"] == 3` counts requested ids, not a hits-bounded read, so the §2.1.7 conformance check still needs an arm on top; `test_fake_adapter_satisfies_every_frozen_protocol` relies on `runtime_checkable` (attribute presence only) — compensated by the behavioural tests. | `tests/unit/test_backends.py:69-73,99`; `tests/unit/test_runtime.py:172-177` | Add a `get_chunks` assertion and replace the `dir()` check with a type-level assertion (no `Evidence` kind for memory, which `test_types.py:97-98` already covers). |
| N9 | v2 `ExtractionConfig.labels` and `ChunkingConfig.field_order` defaults still carry support-domain vocabulary; `neutral` should ship empty per §3.1a/§3.3. | `src/su_multi_arm/core/config.py:398,427` | Move to the `support_example` profile overlay when the profiles slice lands. |
| N10 | `Config.fingerprint()` hashes resolved absolute paths and inlined prompt text, so it differs per checkout. Fine for traces; must not become a generation identity. | `src/su_multi_arm/core/config.py:616-618,646-660` | Note in the docstring; keep `recipe_fingerprint()` as the only index identity. |
| N11 | `sqlite` declares `vector.namespaces: False`; the Phase 2 semantic cache tier (N3 in the round-1 response) needs a `VectorIndex` namespace on the local default. | `src/su_multi_arm/backends/capabilities.py:70` | Decide before S3 whether the sqlite port adds namespaces. |
| N12 | Budget overruns (`commit(actual > estimate)` pushing `reserved` over the limit) are recorded in `usage` but not emitted as a trace event. | `src/su_multi_arm/runtime/budget.py:173-182` | Emit an `overrun` event from the caller or return a flag. |
| N13 | `validate_config` surfaces only the first pydantic error. | `src/su_multi_arm/core/config.py:631-643` | Consider aggregating; operator ergonomics only. |
| N14 | `docs/VALIDATION.md` records 154 passed / 2 skipped without naming the interpreter; this run gives 156/0 with `mcp` installed. | `docs/VALIDATION.md` S2 section | Record the interpreter and `mcp` presence with the numbers. |

## Contract coverage (plan requirement → status)

| Requirement | Status | Where |
|---|---|---|
| §2.1.1 generation pinned per request incl. hops/cache | covered | `runtime/context.py:24-49` (`RunContext.generation`, frozen dataclass; `generation_changed` recorded) |
| §2.1.4 no silent fallback (unavailable backend/capability is a validation error) | partial | `core/config.py:571-579` enforces kind/capability; **B2** (legacy capabilities over-declared) |
| §2.1.6 public-repo hygiene | covered | diff grep for customer names/private endpoints/credentials: none; fixtures synthetic (`CloudTax`, `SearchDesk`, `AACTA`) |
| §2.1.7 no full-corpus loads; catalog reads bounded by hits | partial (contract only) | `backends/contracts.py:10-11` docstring; fake counts ids (`memory.py:215-216`); the conformance check needs an arm (N8) |
| §3.1 `Span` text == source slice; containment; `Relation` invariants; `entailment_checked` reserved; `Evidence` has no cache kind; `GenerationId` | covered | `core/types.py:29-71,139-155,196-218,221-229,232-243`; `tests/unit/test_types.py` |
| §3.1a `FieldSpec` (all 12 fields) and `MetadataSchema` (fields, canonical_unit, equivalence_field) + violations by category; filterable/listable/groupable/sortable gating; `neutral` ships none | covered (+ `unknown_fields` deviation, see B3) | `core/types.py:252-366`; `tests/unit/test_metadata_schema.py` |
| §3.2 `Component` protocol (`name`, `kind`, `config_model`, `validate(env, probe)`, `run`, `recipe_fingerprint`); registries; entry points only for `plugins:`; no import-by-string; no pickles | covered (B1 is over-rejection, not under-rejection) | `core/component.py:88-116,135-148,157-184`; `core/registry.py:78-98,110-115`; `tests/unit/test_component_registry.py` |
| §3.3 top-level keys; unknown keys rejected with path; arm/storage capability checks; exactly one authoritative backend per slot; role-required checks; `native` structured output rejected when undeclared; `on_failure` default `fail_closed` in schema; `memory.semantic` rejected; static (no network) | covered | `core/config.py:111-119,220-247,529-614,631-643`; schema default verified in `docs/config.schema.json`; `tests/unit/test_config_v2.py:39-140` |
| §3.3 effective config + profile recorded in trace | covered | `runtime/context.py:37-45` (`profile`, `config_fingerprint`) |
| §3.3 schema regeneration, no drift | covered | `core/schema.py`; `tests/unit/test_config_v2.py:226-232`; probe `drift ... False/False` |
| §3.4 `RunContext` fields (principal, session, memory, budget, trace, generation, deadline, acl), immutable | covered | `runtime/context.py:23-68` |
| §3.4 `Session` keyed (principal, dataset, id); other principal → 403-class; IDs/hashes not text unless `store_text`; generation switch recorded, episodic re-validated | covered | `runtime/session.py:40-83`; `runtime/memory.py:34-75`; `tests/unit/test_runtime.py:128-155` |
| §3.4 `Memory` working/episodic; never evidence; `semantic` rejected | covered | `runtime/memory.py`; `core/config.py:454-463` |
| §3.4 `Tool` with `input_model/output_model`, `invoke -> ToolResult`; same registry for agents and MCP | covered (built-in tools arrive with arms) | `runtime/tools.py:23-83` |
| §3.4 `ActionRequest/ActionResult/AuditRecord`; audit before + finalise after; `skipped_duplicate`; HTTP/MCP default denied unless allow-listed and `actions` scope | covered | `runtime/actions.py:30-173`; `tests/unit/test_runtime.py:180-215` |
| §3.4 `Budget.reserve/commit`; kinds calls/tokens/USD/wall-clock; USD requires price table; exhaustion typed and recorded | covered | `runtime/budget.py:89-199`; `tests/unit/test_runtime.py:35-91` |
| §3.4 `Trace` nested spans, JSONL export, masking covers query/passages/prompts/`constraint_text`/`suggestion`/follow-up | covered (N2 for extra keys) | `runtime/trace.py:23-44,139-167`; `tests/unit/test_runtime.py:94-116` |
| §3.4 `AccessPolicy.constraints(principal)`; missing attribute fails closed | covered | `runtime/acl.py:39-64` |
| §3.5 eight protocols with listed operations incl. `refresh`, paging, `exists/values`, `put_raw`; flags | covered (+ additions, see B3) | `backends/contracts.py:61-178` |
| §3.5 matrix: adapter → contracts + flags, `contracts|legacy|planned`, used by validation | partial | `backends/capabilities.py`; **B2** |
| §3.5 invariants in the fake: generation scoping, prefix-keyed cache, paging, destructive guard, listable-only values, refresh visibility | covered | `backends/memory.py`; `tests/unit/test_backends.py:97-267` |
| §5 `load_config` accepts v1 via deterministic `upgrade` with warning; `config upgrade` writes v2; mapping table rows | covered | `compat/config_v1.py:257-487`; `core/config.py:663-689`; `cli.py:109-142`; `tests/unit/test_config_v2.py:145-248` |
| §5 / N6 fingerprint round-trip `upgrade(v1) -> downgrade -> same index_fingerprint` | covered for all five examples; narrowed silently for v1-ineffective fields | probe: all `examples/*.yaml` True; counter-example True→False (**B3**) |
| Phase 0 S2 test rows (types, metadata, registry, config+upgrade+drift, contracts+matrix, runtime exhaustion/deadline/masking) | covered | 92 new tests; all rows have a test; see N8 for weak spots |

## Claims verified against code

| Claim (commit message / execution log / docs) | Result |
|---|---|
| "eight frozen capability protocols" | True: `backends/contracts.py` defines 8 `runtime_checkable` Protocols; `CONTRACT_NAMES` has 8 entries |
| "in-memory fake satisfying every protocol" | True behaviourally: `caps.conformance(MemoryBackend())` = all 8; every method except `get_chunks` is exercised by tests; probe called `get_chunks` path indirectly only — N8 |
| "capability matrix with honest status" | False at capability granularity — B2 |
| "unknown keys are errors" with key path | True: `pipeline.arms.lexical.bogus: unknown key`, `bogus: unknown key` |
| "every `examples/*.yaml` upgrades deterministically; fingerprint round-trip" | True for all five (byte-identical output, `legacy_fingerprint()` == v1 `index_fingerprint()`, rewritten v2 re-validates to the same config) |
| "pickle/dotted-path artifacts rejected" | True (`clf.pkl`, `sklearn.pipeline.Pipeline`, `pkg.module:Clf`, `../evil` all rejected); over-rejection in B1 |
| "plugin references only via entry points" | True: `plugins` names must match `^[a-z][a-z0-9_]{0,63}$`; `load_plugins` loads only listed names; `check_plugins_registered` reads metadata only; a plugin that shadows a built-in name raises `RegistryError` |
| "structured_output: native always rejected" | True (`NATIVE_STRUCTURED_OUTPUT` empty; explicit message) |
| "`profile` accepts only `neutral`; `extraction.policy` only `strict`; planned kinds rejected" | True with explicit messages |
| "`config.py` re-exports the v1 model; engine unchanged" | True: v1 model moved verbatim (diff shows only module path/import changes); `su_multi_arm.config.load_config` unchanged |
| "`docs/config.schema.json` is now v2; `config_v1.schema.json` keeps v1; no drift" | True (`schema.drift` False for both) |
| "USD requires a price table; exhaustion raises `BudgetExhausted`" | True; also a failed `reserve_call` releases its partial reservations |
| "no engine behaviour runs on the new contracts yet" | True; nothing in `engine.py`/`retrieval.py` imports `core`/`backends`/`runtime` |
| "nothing silently falls back, retries or loads arbitrary code" | True for the slice: no `retry`, `import_module`, `__import__`, `pickle`, `eval`; only `yaml.safe_load`; the one broad `except Exception` (`actions.py:165`) records the failure in the audit and returns `failed` |
| "ruff check src tests passed" | True |
| Tests "154 passed, 2 skipped" | 156 passed, 3 deselected in this interpreter (`mcp` present); consistent |
