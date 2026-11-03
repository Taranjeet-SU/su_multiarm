# Independent review — V2 implementation plan, round 2

Reviewer: plan-reviewer agent (independent of the author). Date: 2026-11-03.
Subject: `docs/plans/v2_implementation_plan.md`, revision 2 (1,093 lines). Round-1 review:
`docs/plans/reviews/v2_plan_review_r1.md` (verdict REVISE, B1–B8).
Method: re-read the revised plan in full; checked each round-1 blocking finding against the revised
section it cites; re-verified unchanged code claims against `src/su_multi_arm` at `36d2394` where a
revised section relies on them; recomputed every phase total from the row estimates; re-ran the
hygiene grep (vendor/customer terms, private-evaluation numbers, home paths).

## VERDICT: APPROVE

All eight round-1 blocking findings are resolved in the revised sections, with contracts that an
engineer can implement and a third party can test: the contract freeze now precedes the first
adapter and the five arms and ingestion write path have rows, estimates and tests; the lexical
identity criterion is replaced by per-engine conformance semantics plus an informational agreement
report; the decision fail policy has a stated schema default and a per-agent/per-stage table, backed
by a Phase 0 `AnswerContract` that refuses count/exhaustive-list/absence claims without an exact
`ScopeResult`; `MetadataSchema`/`FieldSpec` and the `ValueResolver` table close the typed-scope gap;
grounding keeps the endpoint-containment check and specifies the offset map and the three extraction
policies; classifier artifacts are restricted to registered plugins or sha256-verified ONNX; the
reviewer loop is a defined component with the migration row corrected; and the Phase 0 grounding
acceptance is a CI-runnable fixture pack with the external re-run correctly demoted to
owner-authorised evidence. Totals sum (61 + 102 + 61 + 56 + 48 = 328 ed), the critical path is
internally consistent (≈ week 13 for `2.0.0a1` at 5 ed/week with three streams), and the plan is
clean of vendor/customer terms, private-evaluation numbers and home paths. The remaining items below
are clarifications and small additions, none of which would cause a wrong build, an unsafe default or
an untestable phase; they can be folded into revision 3 or into the Phase 0 changelog without another
review round.

## Resolution of round-1 blocking findings

| Id | Status | Evidence in revision 2 |
|---|---|---|
| B1 — arms, write path and contract freeze missing/ordered wrongly | **Resolved** | Phase 0 rows "Backend contracts + capability matrix (**freeze**)" (4 ed), "sqlite adapter on contracts" (4), "Five arms as Components" (8, incl. minimal `graph` with `TraversalSpec`), "Ingestion write path on contracts (sqlite) + per-projection validation" (5); Phase 1 rows "Multi-backend write path" (5), "`doctor --probe` + CLI v2" (3), "Storage selectors" (1); §3.8 states all five arms port in Phase 0; Phase 3 traversal row reduced to extensions (2 ed); §4.1 chain starts at "contracts freeze (16) → sqlite (4) → arms (8) + write path (5)". The neo4j Phase 1 row now tests the Phase 0 minimal spec, consistent with the acceptance "graph passes … on sqlite and neo4j". |
| B2 — lexical membership identity across engines untestable | **Resolved** | Phase 1 acceptance (B2-revised): identity required only for `dense` (same vectors, exact) and `structured` (exact operations, declared tolerance for approximate aggregations); `lexical` must pass the per-engine semantics defined in §3.5 (scope pushdown, determinism, phrase flag, stable paging, generation isolation, refresh visibility, recorded `analyzer_id`) and the cross-engine agreement report (`tests/services/test_lexical_agreement.py`) is recorded in `VALIDATION.md` "as information, not as a pass/fail gate". Consistent with §8 and notes §12. |
| B3 — fail policy not per type, no default, Phase 1 fabrication path | **Resolved** | §3.7.1: "Default everywhere: `fail_closed`… The generated schema carries this default"; table covers `intent`, `rules` (standalone/cascade), `classifier` (standalone/cascade), `llm`, `cascade` with failure condition, default, permitted outcomes and `Decision` contents; `raw_all_arms` is the only fail-open outcome and carries `scope=None` plus one `UnsupportedConstraint` per detected aggregate intent; §3.9a `AnswerContract` ships in Phase 0 (2 ed) and is enforced by the engine before guards: rules (1)–(4), `unsupported_constraints` engine-populated ("the generator cannot set it"), `on_violation: abstain_with_reason` default; new regression scenario `fail_open_no_counts`; Phase 0 acceptance requires zero fabricated numbers "under both `fail_closed` and `raw_all_arms`". §3.3 validation invariants list the schema default. |
| B4 — no MetadataSchema; ValueResolver undefined for ranges/high cardinality | **Resolved** | §3.1a `FieldSpec`/`MetadataSchema` (type incl. `datetime/date/enum/id`, `nullable`, `enum_values`, `filterable/listable/groupable/sortable`, `max_catalog_cardinality`, `time_basis`, `timezone_policy`, `casefold_match`, `canonical_unit`, `equivalence_field`) with the consumers listed; §3.7.2 table defines resolution per predicate type: enum membership, catalog match under the cap, `RecordStore.exists` above it, type-parse-only for numeric/boolean, offset/granularity rules for `range`/`date_interval` with half-open boundaries, `missing` requires nullable, unknown/non-filterable → `field_not_filterable`, `measure` type requirements and "never substituted by `count`"; IDs/free text (`max_catalog_cardinality: 0`) are never enumerated to a model; `RecordStore` gained `exists`/`values`; Phase 0 row (6 ed) tests "every row of the resolution table". The explicit-user-filter vs model-invented-value distinction (`zero_match: true`) is a good addition. |
| B5 — endpoint containment dropped; offset map and `partial` undefined | **Resolved** | §3.1 invariant: a `fact`/`co_occurrence` relation's span "contains both endpoint entity spans (offset containment, same chunk)" and endpoints resolve to typed entities from the same chunk; §3.12 "Endpoint containment" paragraph with failure categories; "Grounding with an offset map" specifies `normalise(source) -> (norm_text, index_map)` per code point (NFKC expansions, casefold `ß→ss`, whitespace runs incl. NBSP, combining marks), the `start/end` mapping and the asserted invariant `Span.text == source[start:end]`, with an eleven-case fixture list and a property test in the Phase 0 row; §3.14.1 defines `strict`, `quarantine`, `partial` by failing unit, behaviour, manifest/exposure and activation rule. |
| B6 — classifier allows pickle / dotted-import loading | **Resolved** | §3.7.3: only a registered entry-point plugin listed under `plugins:` or the built-in `onnx_classifier` with `{model_path, sha256, manifest}`; pickled artifacts and dotted import paths are validation errors; callables only via `Engine(config, components=[...])`; `tests/unit/test_classifier_artifacts.py` named; §3.2 reiterates "no pickled artifacts"; Phase 0 component row and Phase 1 decision row test it; §8 risk row updated. |
| B7 — reviewer/follow-up loop had no v2 component | **Resolved** | §3.9b `pipeline/review.py::ReviewStage` (`kind="reviewer"`, post-selection, role `reviewer`, `Review{sufficient, missing, followup_query}`, `max_hops`, five stop reasons, re-plan through the decision agent with immutable filters/ACL and pinned generation, one vote per record per arm); Phase 0 row (3 ed) moves the two existing agentic tests; §5 migration row now maps `planned → {kind: llm}` + `review.enabled: false` and `agentic → {kind: llm}` + `review {enabled: true, max_hops}`; `pipeline.review` in the config sketch; `reviewer` added to `ComponentKind`. |
| B8 — Phase 0 replay over cached raw replies not reproducible | **Resolved** | Phase 0 acceptance now uses the grounding fixture pack with the offset-map property test, synthetic reproductions of the public `hosted` and NL-count probe shapes, and labels the local-model re-run "External evidence (owner-authorised, not a CI gate)" with raw replies cached this time (open decision 16); §3.14 adds `LedgerStore.put_raw(key, raw_reply, valid)` and "raw provider replies are stored for every call, valid or invalid"; §5 `migrate-snapshot` row now states "accepted extractions only — v1 never stored rejected raw replies", which matches `store.py::ingest`. |

## New contracts introduced by the revision — implementability and testability

| Contract | Verdict | Notes |
|---|---|---|
| `MetadataSchema`/`FieldSpec` (§3.1a) | Implementable, testable | Closed type set, explicit flags, per-field cardinality cap and timezone policy map directly to Pydantic validation, `RecordStore.upsert` DDL and config gating. See N3 (multi-valued fields) and N7 (two selectors for the records backend). |
| Fail-policy table (§3.7.1) | Implementable, testable | Each row is a unit test (Phase 0 row states "every row of the fail-policy table"). The intent pre-pass "never fails" is the right property for the contract to lean on; see N5 for language scope. |
| `ValueResolver` table (§3.7.2) | Implementable, testable | Every row has a failure reason code. Request-time ACL filtering of the per-generation catalog is covered by `RecordStore.values(gen, field, acl, limit)`. |
| `AnswerContract` (§3.9a) | Implementable, testable | Rules (1)–(4) are deterministic post-checks on typed output; the generator cannot set `unsupported_constraints`. Residual: claim `kind` is model-labelled until the Phase 2 numeral screen — see N1. |
| `locate()` index map (§3.12) | Implementable, testable | Per-code-point map with the stated `start/end` rule and property test. Boundary snapping inside an expansion should be stated — see N2. |
| `strict`/`quarantine`/`partial` (§3.14.1) | Implementable, testable | Activation rules and manifest exposure are explicit. Rejection granularity under `quarantine` needs one sentence — see N4. |
| Classifier artifact rules (§3.7.3) | Implementable, testable | Requires an `onnxruntime` extra not yet in the extras list — see N6. |
| `ReviewStage` (§3.9b) | Implementable, testable | Stop reasons enumerated; one missing case — see N8. |
| Phase 0 acceptance (revised) | Testable by a third party | All CI-gated items are synthetic or in-repo; external evidence is labelled and gated on owner approval. |

## Estimates and critical path

| Check | Result |
|---|---|
| Phase totals from row estimates | P0 = 61, P1 = 102, P2 = 61, P3 = 56, P4 = 48 — all match the stated totals; sum 328 ed. |
| §4.1 chain figures | "contracts freeze (16)" = types 3 + metadata 2 + component 3 + config 4 + contracts 4; "six adapters (39)" = 6+7+8+6+6+6; "parallel ×3 ≈ 14" is 39/3 rounded. |
| Calendar to `2.0.0a1` | P0 critical chain 16 → 4 → 8 (write path 5 parallel) = 28 ed ≈ 5.6 weeks; P1 chain 3 + 5 + 14 + 6 + 9 = 37 ed ≈ 7.4 weeks; total ≈ 13 weeks at 5 ed/week with three streams, as stated. The 8 ed integration buffer is off the chain; a realistic range is 13–14 weeks. |
| Later milestones | P2 chain 12 + 6 + 5 = 23 ed after a1 → ≈ week 18–19 (stated 19); P3 overlaps; P4 harness can start after P2, benchmarks need b1 (week 25) → 2.0 ≈ week 28–30 (stated 30). Single-engineer 328/5 ≈ 66 weeks ≈ 15 months, as stated. |
| Plausibility of revised rows | Providers 11, serving 9, buffer 8, opensearch 8 are within reason for a senior engineer; Phase 1 is still the largest risk (§8 acknowledges). |

## Hygiene

- Grep of the plan for vendor/customer terms (historical product, customer and model-name terms) returns nothing.
- Private-evaluation numbers removed: no `n/27` counts or latency figures remain; §6.4 marks private findings with † and carries direction only; §3.11 says "private evaluation, not reproducible here".
- The owner's home path is gone; §6.1 refers to "the external test workspace designated in `AGENTS.md`".
- "historical support-search services" is used for the originating systems. `git status` shows no file other than the plan and review directories touched.

## Non-blocking findings (revision 3 or Phase 0 changelog)

| Id | Section | Finding | Suggested change |
|---|---|---|---|
| N1 | §3.9a | Claim `kind` is assigned by the generator; a model can label a count as `narrative` and rule (3) will not catch it until the Phase 2 `exact_set` numeral screen. The decision-layer fixes (ValueResolver, measure rules) close the measured failure regardless, so this is residual, not blocking. | Add to rule (3), Phase 0: when the pre-pass detected an aggregate intent and no exact `ScopeResult` exists, any numeral in `answer`/`short_answer` outside a cited aggregate evidence string is a violation (≈ 0.5 ed). |
| N2 | §3.12 offset map | A match that starts or ends inside a multi-char expansion (e.g. the `f` of `ﬁ`) maps to the whole source code point; the invariant still holds but the span widens. | State that spans snap outward to source code-point boundaries and are recorded `match_level: normalised`; add one fixture for it. |
| N3 | §3.1a | `FieldSpec` has no multi-valued type (tags/labels lists are common metadata). | Either reject list-valued metadata at ingestion with a clear category, or add `multi_valued: bool` with `in`/`eq` semantics defined as "any element". |
| N4 | §3.14.1 | `quarantine` says the document's "extraction outputs are excluded"; it is not stated whether rejection is per unit (drop the failed relation, keep accepted entities of the same chunk) or per document (all-or-nothing). Coverage counts depend on the choice. | Pick one (per-document is simpler and matches today's status model) and state it; add the alternative as a later option if needed. |
| N5 | §3.7 intent pre-pass | The aggregate/identifier grammar is implicitly English-only. | State the language scope and make the pattern set profile-configurable (`neutral` ships English). |
| N6 | §3.7.3, §3.12, Phase 1 packaging row | New dependencies are not in the extras list: `onnxruntime` for `onnx_classifier`, `snowballstemmer` for predicate triggers. | Add a `classifier` extra; decide whether the stemmer is a core dependency (pure Python, permissive) or part of an `extraction` extra, and list it. |
| N7 | §3.3 | The records backend is selected twice: `storage.records` and `pipeline.arms.structured.backend`. | Validate that they agree, or drop the per-arm key for `structured`. |
| N8 | §3.9b | If the re-plan of a follow-up query returns `abstain` (e.g. `rules` with no match), no stop reason covers it. | Add `replan_abstained` to the stop reasons. |
| N9 | Phase 0 | Phase 0 acceptance needs `search`/`answer` to run end-to-end for the 40 tests, but engine v2 (`pipeline/engine.py`) is a Phase 1 row. | State that Phase 0 keeps a thin transitional engine (today's `engine.py` adapted to call the components) and that Phase 1 replaces it. |
| N10 | Phase 1 acceptance | `dense` identity "on opensearch and elasticsearch (exact rescore)" holds only when candidates are exact (`script_score` over the scoped set), not HNSW + rescore; order identity across float32/pgvector/Lucene needs a tie rule. | State "exact mode" for the identity run and "order identical up to a documented score tolerance with `id` tie-break". |
| N11 | §3.5 invariants | "destructive operations are limited to `delete_generation(gen)` for a generation that is not `CURRENT` and not pinned" is enforced by the caller (`release.py`), since adapters "never read `CURRENT`". | Say the caller enforces it; adapters only refuse when told the generation is active. |

## Claims re-verified against code for this round

| Claim | Result |
|---|---|
| v1 `extraction_cache` holds accepted extractions only; rejected raw replies were never stored (§5 `migrate-snapshot` row, B8) | True: `store.py::ingest` inserts into `extraction_cache` only after `extractor.extract` succeeds; any exception removes the staged directory. |
| Endpoint-in-quote check exists today and is kept (§3.1, B5) | True: `extraction.py` rejects a relation unless `subject`/`object` appear in `relation.quote` and resolve to a unique typed entity. |
| Reviewer loop semantics carried into `ReviewStage` (§3.9b) | True: `engine.py::answer` implements `max_hops`, stop reasons `sufficient | no_actionable_gap | repeated_query | hop_limit`, and one vote per record per arm through `fuse`. |
| `Retriever` loads the whole corpus per request (§2.1.7) | True: `retrieval.py::__init__` reads all `documents` and `chunks`; `allowed()` scans all documents. |
| `per_arm` ordering = reciprocal rank (§3.9) | True: `fusion.py` default branch. |
| Row estimates sum to stated totals | True (61/102/61/56/48 = 328). |
