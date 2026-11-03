# Review: Phase 0 / S3 + S4 as merged (commits `084fab0`, `91d8cd6`, `893e668`, `ecccac5`, `9f26b88`, branch `v2/phase-0`)

Reviewer: plan-reviewer agent applied to code, 2026-11-03. Artifact: `git diff 9b89f0f..9f26b88 -- src tests`
(106 files, +10317/-1281). Scope: the S3 modules (`decision/`, `selection/`, `generation/contract.py`,
`pipeline/review.py`), the S4 modules (`backends/sqlite/`, `backends/_scope.py`, `backends/extras.py`,
`testing/backend_conformance.py`, `ingestion/{write,policy,release}.py`, `extraction/`, `communities/`,
`arms/`, `profiles/`) and the merge-side edits. The concurrent engine wiring (S5) is out of scope;
`engine.py` was read only where tests depend on it. Nothing in the source was modified. Because the
working tree was being edited concurrently (`models.py` modified, `models.Plan` already removed), every
probe ran against `git archive 9f26b88` with `PYTHONPATH` pointed at the extracted tree.

Checked against `docs/plans/v2_implementation_plan.md` §3.5, §3.7, §3.7.1, §3.7.2, §3.8, §3.9, §3.9a,
§3.9b, §3.12, §3.14, §3.14.1, the Phase 0 table and acceptance list, and the S3/S4/merge entries in
`docs/plans/execution_log.md`.

Evidence gathered: `ruff check src tests` clean; `pytest -q -m 'not external' -p no:cacheprovider
--basetemp=~/su_mutliarm_tests/claude/pytest_tmp` with `PYTHONDONTWRITEBYTECODE=1` → **386 passed, 3
deselected** (`.venv`, Python 3.13); `tests/conformance` passes for `memory`, `sqlite`, `sqlite_faiss`.
Two probe scripts (`~/su_mutliarm_tests/claude/review_s3s4/probe_backends.py`, `probe_writepath.py`)
exercised the in-memory fake and the sqlite adapter side by side on 22 edge cases, the write path under
`unknown_fields: reject`, `assume_utc` values end to end, the `partial` per-document status, the
community arm's catalog reads, and the default `rules` agent on five ordinary retrieval questions.
Findings cite `file:line` in the committed tree.

**VERDICT: REVISE**

The slices are substantially what the plan asks for: the sqlite adapter structurally satisfies all eight
frozen protocols and passes the same conformance kit as the fake; every row of the fail-policy and
resolution tables has a test; the `AnswerContract` cannot be bypassed by the generator; ordering is
proven membership-preserving and bit-for-bit equal to the alpha `fuse`; the review loop is bounded with
every stop reason recorded and never re-plans to raw-all-arms; the graph arm traverses `fact` edges only;
the five-occurrence `hosted` case collapses to one `co_occurrence` with five provenance entries; the nine
probe shapes abstain or flag under both policies with no number produced. Four defects block: the typed
scope silently returns wrong "exact" counts on sqlite (and diverges from the fake) for `assume_utc`
fields; `unknown_fields: reject` makes every ingest fail because the write path injects `source` into the
canonical record; under `partial` an unprocessed document is reported as `extraction: accepted`, and a
test codifies it; and the default decision agent abstains on ordinary retrieval questions that merely
contain "every", "all the", "total", "latest" or "earliest".

## Blocking findings

| id | plan section | finding | evidence | required change |
|---|---|---|---|---|
| B1 | §3.5, §3.7.2, Phase 0 "sqlite adapter on contracts" | Temporal comparisons are not instant-based in either store and the two stores disagree. Under `timezone_policy: assume_utc` a naive stamp is accepted at ingestion (`core/types.py:344-350`) but `backends/_scope.py:22-30` returns `None` for it, so `compare()` falls back to string comparison (`_scope.py:38-43`) while sqlite stores it as `kind='string'` (`sqlite/_db.py:97-100`) and the compiled aware bound only matches `kind='datetime'` rows (`_db.py:116-124`). The resolver converts naive *bounds* to UTC (`decision/values.py:115`) but nothing converts naive *values*. Probe: two documents, one naive and one aware, both on 2025-01-02; `count since 2025-01-01Z` → memory **2**, sqlite **1**, both `exactness="exact"`, `unsupported=[]`. A `date` field queried with a datetime bound likewise matches on memory and not on sqlite. The conformance kit only declares `require_offset` fields (`testing/backend_conformance.py:48-56`) so it cannot see this; the S3 fixture declares `updated_at` as `assume_utc` but no record carries a value for it (`tests/conftest.py:72,79-103`). | `probe_backends.py` cases "assume_utc aware bound vs naive stored", "date field with aware datetime bound"; `probe_writepath.py` §2 | Normalise temporal values once at write time under the field's policy (naive → UTC for `assume_utc`; reject for `require_offset`, as today) and store the epoch form so both stores compare instants; make a `date`-typed field with a datetime bound an `unparseable_bound` (or truncate deterministically) in `unsupported_reason`; add an `assume_utc` field and a `date` field to the conformance `SCHEMA` with naive stored values and assert identical `ScopeResult`s on both adapters. A typed-scope answer is only allowed to say `exact` when it is. |
| B2 | §3.14, §3.1a | `unknown_fields: reject` cannot ingest anything. The write path builds the canonical record from `document_values(d)` (`ingestion/write.py:299`), which adds `id`, `source`, `dataset`, `kind`; `RECORD_KEYS` bypasses only `id`, `dataset`, `kind` (`backends/_scope.py:86,121-126`), so `source` is always an undeclared key and `records.upsert` raises. Document-level validation passed one line earlier (`write.py:185-188`) and the failure is categorised `backend:memory.upsert`, not `metadata.unknown_field`. No test exercises `unknown_fields: reject` through the write path. | `probe_writepath.py` §1: `IngestError: backend:memory.upsert ... record kb:a: source unknown_field: undeclared` | Treat `source` like the other identity keys (bypass while undeclared), or validate records with exactly the rule used for documents; add a write-path test with `unknown_fields: reject` that ingests and that rejects a genuinely undeclared metadata key with category `metadata.unknown_field`. |
| B3 | §3.14.1 ("No policy ever drops a unit silently"), owner probe (b) | Under `partial`, a document the extraction stage never reached is recorded as `extraction: accepted` with `graph: no_entities_found`. `ExtractionPolicy.document_status` returns `accepted` for any id not in `rejected` (`ingestion/policy.py:104-106`); the status log calls it for every document (`write.py:347`) after `_extract` returned early (`write.py:412-417`). `tests/unit/test_write_path.py:238` asserts `status()["documents"]["kb:rounding"]["extraction"] == "accepted"` for the very document listed in `documents_without_graph` on the next line, codifying the misreport. The manifest's `coverage.stages` is correct; the per-document surface an operator reads is not. | `probe_writepath.py` §3: `d2 status: accepted` while `documents_without_graph=['d2']` | Add a third per-document state (e.g. `not_processed:<stage>`) set when a stage stops before the document, make `graph` read `incomplete` rather than `no_entities_found` for it, and fix the test to assert that state. |
| B4 | §3.7 (rules: "everything else is the raw question on every enabled non-structured arm"), §3.7.1, Phase 1 shippability | The default decision agent (`DecisionConfig().kind == "rules"`, `on_failure: fail_closed`) abstains on ordinary retrieval questions. The pre-pass grammar fires on bare `every`, `all (of )?the`, `total`, `latest`, `most recent`, `oldest`, `newest`, `first time` (`decision/intent.py:25-37`); `RulesAgent.attempt` routes any aggregate intent to `_aggregate` (`decision/rules.py:62`), which raises `unresolvable_value` whenever residual words cannot bind to a field (`rules.py:115,123`). Measured on the committed tree with a one-field schema: "What happens every time the service restarts?", "Show me all the steps to renew a certificate", "Why does the export fail after every upgrade?", "What is the total cost of ownership described in the guide?", "Which article explains the latest release notes?" → all five `abstain / unresolvable_value`. None of these is a count; the `AnswerContract` already prevents a fabricated number on a narrative answer. No negative fixture of plain questions containing these words exists in `tests/unit/test_intent.py` or `test_fail_policies.py`. | probe output in this review's evidence; `tests/unit/test_intent.py:162-181` has only positive aggregate cases plus two clearly non-aggregate sentences | Either narrow the grammar (require the aggregate phrase to govern the canonical unit or a declared field, e.g. `list all <unit>`, `total <numeric field>`, `latest <time_basis label>`), or have `rules` return a decided retrieval plan carrying one `UnsupportedConstraint` per unbound aggregate intent when non-structured arms are enabled and residual content words remain, abstaining only for purely aggregate questions. Add a negative fixture of at least ten plain questions containing these words that must be `decided` with a retrieval plan, and keep the nine probe shapes abstaining. |

## Non-blocking findings

| id | section | finding | suggestion |
|---|---|---|---|
| N1 | §3.8, principle 2.1.7 | The community arm pages the catalog for every stored community before testing relevance: `graph.communities(gen)` loads the whole table, then `_eligible` runs `iter_scope` per community (`arms/community.py:45-46`); the seed/overlap check comes after. Probe: a query with no entity overlap still made 3 `iter_scope` calls on a 30-document corpus; cost grows with community count, not hits. `GraphExtras.memberships`/`communities` are themselves whole-table reads. | Check seed-entity intersection first and only page members of candidate communities; consider `communities(gen, entity_ids)` in `extras`. |
| N2 | §3.12, §3.5 | Dedupe keeps every occurrence (`extraction/dedupe.py`) and sqlite stores them (`sqlite/graph.py:110`), but `PathEdge` has no `occurrences` (`core/types.py:522-530`) and `_edge` drops them, so graph evidence shows one span for a five-occurrence edge. | Changelog candidate for §3.5: add `occurrences` to `PathEdge` (or expose through `extras`). |
| N3 | §3.7 | `RulesAgent`/`LlmAgent` read `deps.backends.get("records")` as a *facet* (`decision/rules.py:56`, `decision/llm.py:111`) while arms treat `deps.backends` as adapter instances keyed by backend name and `getattr(backend, capability)`. The engine wiring must bridge two conventions. | Pick one: pass the records facet under `deps.components["records"]` or resolve `storage.records` → adapter → `.records` in the agents. |
| N4 | §3.9b | `ReviewStageConfig.max_hops ge=1` (`pipeline/review.py:47`) with `range(1, max_hops)` (`:134`) means `enabled: true, max_hops: 1` silently never reviews. Matches the alpha (`routing.max_hops - 1`) but is a no-op config. | `ge=2`, or a `validate()` warning. |
| N5 | §3.5 | Capability flags drift from behaviour: sqlite `records` declares `date_buckets=False` (`backends/capabilities.py:113`) while `bucket` is implemented in `_scope.measure_of`; sqlite `vector` declares `filter_pushdown=True` (`:112`) for a post-filter scan over the full vector file under a restrictive scope (`sqlite/search.py:210-230`). | Align the flags; the scan is acceptable for the local default but should not be advertised as pushdown. |
| N6 | §3.14 | `WritePath.ingest` protects the previous generation (`write.py:193`) and never unprotects it after activation; the newly active generation is never protected in-process. Harmless today (no `gc`), wrong once `release.gc` lands. | Protect the new generation on activate, unprotect the old one. |
| N7 | §3.14 | Per-projection validation covers catalog, records, lexical, vector; graph and community projections have no count/round-trip check. `_extract` double-counts `processed` for a rejected document (`write.py:427`). | Add graph/community counts via `extras`; fix the counter. |
| N8 | §3.7, profiles | Two planner prompts exist (`decision/llm.py::PLANNER_PROMPT`, `profiles/prompts.py::PLANNER_PROMPT`); the agent uses its own unless `LlmConfig.prompt` is set, so the `support_example` overlay's `pipeline.decision.prompt` only takes effect if S5 threads it through. | Keep one constant; wire `pipeline.decision.prompt` → `LlmConfig.prompt`. |
| N9 | hygiene | `profiles/support_example.yaml:40` names a vendor issue-tracker product in a built-in prompt; `tests/unit/test_config_v2.py:196` and the v1 example keep that product name as a source weight. Pre-existing alpha vocabulary, but the repo is meant to be vendor-neutral. No private endpoints, credentials or customer names found in the diff (only loopback addresses, `example.org`, public API defaults). | Replace with "ticket digests" / a neutral source label. |
| N10 | execution log | S3 deviation (8) says the two v1 agentic tests "stay in `tests/test_core.py`"; S4 deleted that file and they now live in `tests/contract/test_engine_support_example.py:318,334`. Merge note (2) says the page-size cap lives in `backends/_scope.py`; it lives in `core/types.py::Page` (`le=10000`) and is converted to `BackendError` in each adapter. | Correct the log. |
| N11 | §3.9a | The "zero count with absent value flagged" acceptance is met only by `ScopeResult.zero_match: true` and a free-text `Decision.note`; `AnswerResult` has no structured zero-match field, so serving layers cannot render it without parsing the note. | Carry `zero_match_fields` on `AnswerResult` (engine-injected like `unsupported_constraints`). |
| N12 | §3.14 | Extraction reuse keys on `extraction:<recipe>:<chunk.id>` (content-derived chunk id, good) but a *relocated* span references a host chunk of another field; if only that other field changes, the reused output points at a stale chunk id. | Include the record's other-field chunk ids (or a document content hash) in the reuse key when relocation is enabled. |

## Requirements coverage

| requirement | status | where |
|---|---|---|
| §3.5 sqlite adapter: all eight capabilities on the frozen contracts, one facet each | covered | `backends/sqlite/{catalog,search,graph,runtime}.py`; `caps.conformance(adapter) == all eight` (`tests/conformance`) |
| §3.5 conformance kit v0 over fake + sqlite (scope pushdown, determinism, phrase flag, paging, generation isolation, refresh, bounded catalog reads, exactness, fact-only traversal, cache prefix/TTL, ledger raw replies, session expiry) | covered | `testing/backend_conformance.py`; passes for `memory`, `sqlite`, `sqlite_faiss` |
| §3.5 same semantics fake vs sqlite for typed scope | **partial** | identical on 19/22 probed cases; temporal cases diverge (B1) |
| §3.7 `DecisionAgent`/`Decision`/`ScopeSpec`, intent pre-pass, `rules`, `classifier`, `llm`, `cascade` | covered (B4 on default behaviour) | `decision/*.py`; `tests/unit/test_{intent,fail_policies,llm_agent,count_probes}.py` |
| §3.7.1 every fail-policy row, `fail_closed` default in schema, `raw_all_arms` = raw plan on non-structured arms + one `UnsupportedConstraint` per aggregate intent, filters immutable | covered | `decision/base.py:158-227`; `test_fail_policies.py` (one test per row, schema default asserted at `:100`) |
| §3.7.2 every resolution row; unsupported total never a count; zero match with absent caller value flagged; cardinality-capped observed values; `max_catalog_cardinality: 0` never enumerated | covered at the resolver/compiler level | `decision/values.py`, `decision/scope.py`; `test_scope_values.py` |
| §3.7.3 classifier artifact contract: pickle/dotted/unsigned rejected, sha256 verified and traced, runtime injected only | covered | `core/component.py:133-183`, `decision/classifier.py`; `test_classifier_artifacts.py` |
| §3.8 five arms as `Component`s with `ArmResult(hits, aggregates, paths, reports, diagnostics)`; ACL scope never widened; graph `fact` only with edge provenance; community = membership + grounded report objects | covered (N1, N2) | `arms/*.py` (`graph.py:41` hard-codes `edge_kinds=["fact"]`); `tests/contract/test_arms.py` on memory + sqlite |
| §3.8 arms refuse incomplete stages unless `pipeline.allow_incomplete_arms` | covered | `arms/base.py:79-91`; `test_arms.py::test_arms_refuse_incomplete_stages_unless_allowed` |
| §3.9 selection stages; `reciprocal_rank` = alpha `per_arm`; membership preserved under ordering; packing fairness and configured field priority | covered | `selection/*.py`; `test_selection.py`; `tests/parity/test_ordering_parity.py` (ids, scores, arms, chunk ids equal to `fuse` for all five strategies, weighted and capped) |
| §3.9a `AnswerContract` rules 1-4; generator cannot set `unsupported_constraints`; count/exhaustive/absence need exact (absence: exhaustive) `ScopeResult`; `on_violation` default abstain | covered (literal is `fail`, not `error`: logged deviation) | `generation/contract.py`; `test_answer_contract.py::test_generator_cannot_set_unsupported_constraints` |
| §3.9b review loop bounded by `max_hops`; stop reasons `sufficient / no_actionable_gap / repeated_query / hop_limit / budget_exhausted` (+ `replan_abstained`); re-plan through the decision agent, never raw-all-arms; filters/ACL immutable; one vote per record per arm | covered (N4) | `pipeline/review.py`; `tests/contract/test_review_stage.py` |
| §3.12 `relocated` level; endpoint containment; predicate support; dedupe keeps occurrence provenance | covered (N2) | `extraction/{base,grounding,dedupe}.py`; `test_write_path.py:306-373` |
| §3.14 single write path; per-projection validation; failure leaves old generation active with failure manifest; raw replies kept valid or not; ledger reuse | covered (B2, N6, N7) | `ingestion/write.py`; `test_write_path.py:85-167` |
| §3.14.1 `strict` aborts and leaves old generation; `quarantine` excludes extraction outputs from graph/community/report stores and records per-category coverage; `partial` refuses activation without flag and marks stages incomplete; no silent drop or downgrade | **partial** | strict/quarantine verified (`test_write_path.py:180-239`); partial activation gate verified; per-document status misreports unprocessed documents (B3) |
| Phase 0 acceptance: existing tests replaced with equivalents under `support_example` | covered | `tests/contract/test_engine_support_example.py` (30 tests) |
| Phase 0 acceptance: five `hosted` occurrences → one `co_occurrence` with five provenance entries | covered | `test_write_path.py:306-323` |
| Phase 0 acceptance: nine probe shapes abstain/flag, zero fabricated numbers under both policies | covered | `tests/unit/test_count_probes.py` (36 parametrised cases + exact-shape positives) |
| Phase 0 acceptance: three synthetic domains through one engine; prompts carry no domain authority; `support_example` overlay isolates the support vocabulary; custom context keys survive; `generation.voice` optional | covered (N8, N9) | `profiles/`; `tests/contract/test_domains.py` |
| Public-repo hygiene | covered (N9) | grep over `src`, `tests`, `docs/plans` |

## Claims verified against code

| claim (source) | result |
|---|---|
| Merge: "386 after the S3 merge; ruff clean" | verified: 386 passed, 3 deselected; ruff clean |
| S4: sqlite implements all eight capabilities; conformance kit passes with numpy and faiss | verified structurally and by `tests/conformance` (faiss present in `.venv`) |
| Merge (2): record-store gating (`unknown_fields: reject`, non-filterable → `field_not_filterable`, `missing` on non-nullable → `field_not_nullable`, naive datetime bound → `timezone_missing`) applies to fake and sqlite alike | verified for all four (identical results on both adapters); the page cap lives in `core/types.py::Page`, not `_scope.py` (N10) |
| Merge (3): identity keys bypass the schema only while undeclared | verified (`_scope.py:121-126`); but `source` is not an identity key (B2) |
| S4 (3): operations outside the frozen protocols live in `backends/extras.py`, not `contracts.py`; arms fail with a typed error when absent | verified (`extras.require`, `arms/graph.py:12-16`, `community.py:41`) |
| S4 (10): aggregate denominator = ACL + request filters | verified (`arms/structured.py:266-272` passes both as the `acl` argument) |
| S4 (11): lexical phrase boost with DF guard | verified (`arms/lexical.py:29-36`) |
| S4: strict aborts on first rejected unit, staged generation removed, old generation active | verified (`write.py:205-215`, `policy.py:71-74`; `test_write_path.py:180-189`) |
| S4: quarantine keeps the document in catalog/lexical/dense/structured and excludes its extraction outputs from graph/community | verified (`test_write_path.py:191-205`, incl. communities) |
| S4: partial refuses activation without `allow_partial`, marks stage incomplete with first unprocessed unit | verified (`write.py:198-200`; `test_write_path.py:216-239`), except per-document status (B3) |
| S3 (1): `selection.ordering` default `reciprocal_rank` reproduces alpha `per_arm`; `rrf` keeps its formula | verified (`tests/parity/test_ordering_parity.py`) |
| S3 (5): `on_violation` literal is `fail` (plan says `error`) | verified (`generation/contract.py:95`) |
| S3 (6): review treats a `raw_all_arms` re-plan as `replan_abstained` | verified (`pipeline/review.py:163-166`; `test_review_stage.py:105-107`) |
| S3 (7): `date_interval` on a `require_offset` datetime rejects calendar-shaped bounds with `timezone_missing`; `assume_utc`/`date` accept | verified at the resolver (`values.py:128-150`; `test_scope_values.py:109-131`); store-side comparison of `assume_utc` values is the defect in B1 |
| S3 (8): the two v1 agentic tests "stay in `tests/test_core.py`" | not as stated: `tests/test_core.py` was deleted in S4; equivalents are in `tests/contract/test_engine_support_example.py:318,334` and `tests/contract/test_review_stage.py` (N10) |
| S3: classifier ships as interface + artifact contract, no inference dependency, fails `validate()` without an injected runtime | verified (`classifier.py:471-481`; `test_classifier_artifacts.py:47-55`) |
| S3: `raw_all_arms` never runs the structured arm and flags every aggregate intent | verified by model validator (`decision/base.py:127-133`) |
| Plan §3.8: graph traverses `fact` edges only in Phase 0 | verified (`arms/graph.py:41`; conformance `check_graph` asserts co-occurrence only when requested) |
| Plan §3.9a: generator cannot set `unsupported_constraints` | verified (`Answer` is `extra=forbid`; schema has no such property; `test_answer_contract.py:280-286`) |
| Plan §3.9: ordering-only changes never change union membership | verified for all seven kinds (`test_selection.py:40-46`); `max_documents` is the explicit, traced exception |
| Plan §3.12: dedupe keeps all occurrence provenance | verified in storage (`write.py` manifest counts, `test_write_path.py:306-323`); not surfaced through `PathEdge` (N2) |
| Chunk ids are content-derived so unchanged chunks reuse extractions across generations | verified (`extraction/chunking.py:384-387`) |
| No customer names, private endpoints, credentials in the diff | verified; one vendor product name in `support_example.yaml:40` (N9) |
