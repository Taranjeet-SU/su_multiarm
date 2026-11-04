# Review: V2 plan addendum A1 (log analytics, sources, pattern extractor), revision 2

Reviewed: `docs/plans/v2_addendum_logs_and_patterns.md` rev 2 (in place, uncommitted) and the
revised private deployment companion (read only; nothing from it is reproduced here beyond generic,
non-identifying observations). Previous round: `docs/plans/reviews/addendum_logs_review_r1.md`
(REVISE, B1–B11). The new claims were checked against the source at `cccd8d2`. Date: 2026-11-04.

VERDICT: REVISE

Revision 2 resolves ten of the eleven blocking findings, and the fixes are concrete contracts
with named tests. B1, B2, B3, B4, B5, B6, B8, B9, B10 and B11 are resolved. B7 is only partly
resolved. Taking the library identity out of the live fingerprint fixes the serving breakage, but
the write path's extraction reuse key is derived from that same recipe fingerprint
(`ingestion/write.py:566`, `extraction:{recipe}:{chunk_id}`). After a library change, compose
therefore reuses the cached extraction output of every unchanged chunk. The generation would serve
relations from deprecated patterns and miss those from newly approved ones, which defeats the
"approve → reparse → re-run" requirement. The fix is a one-paragraph contract change: put the
pattern provenance into the reuse key (not the live fingerprint) and add one test. The remaining
items are non-blocking.

## Status of round-1 blocking findings

| ID | Status | Evidence in rev 2 | Remaining note |
|---|---|---|---|
| B1 read-only source | **Resolved** | §3.1.1 gives an exact table of `(method, anchored path)` rows. `DELETE` is allowed only on `/_search/scroll` and `/_pit` (and the OpenSearch equivalents), only with ids from `cursor.opened_ids`, never `_all`. `{idx}` grammar has no leading `_`, `/`, `*` or `,`. Query-parameter and body allow-lists refuse scripts. No redirects. `SourceRequestRefused` is raised before any I/O. `_has_privileges` self-check (error under `require_read_only_role`, the log-profile default). `close()` runs in `finally`, `atexit` and signal handlers, with keepalive ≤ 2m. L1 tests rewritten. | OpenSearch row 11 (`/_plugins/_security/api/account`) returns roles, not privileges, so the OpenSearch self-check is weaker. Stating that is enough. |
| B2 count exactness | **Resolved** | §3.9.1: per-basis rendering ("N lines containing 'error'", never "N errors"), the three-part coverage condition for signature counts, and `FieldSpec.role` plus worst-row `ScopeResult.exactness` plus `applied_basis`, with `answer_level_claim_on_proxy` named. `FieldSpec` exists today (`core/types.py:262`) and has no `role`, so this is additive. | — |
| B3 structured analytics | **Resolved** | §3.9.2: `Sort("$measure")` with `top_n` after the sort, `bucket.of` inner measure, `hour` granularity, on all 7 records adapters plus the kit (6 ed). Each question is mapped to a rollup with a cardinality bound. A compose warning fires when a rollup exceeds `max_matched`, and refusal stays typed. Gold test with row-count order ≠ sum order. The adapter list matches the code (Elasticsearch reuses `OpenSearchRecords`, `backends/elasticsearch/adapter.py:54`). | n6: "tenants affected by S" is bounded at about 10⁴, which equals `max_matched`. |
| B4 retention vs `keep_generations` | **Resolved** | §3.8.3 rule (a): every non-active generation whose manifest lists D is deleted regardless of `keep_generations`. Verify enumerates the partitions of every remaining manifest. The 11-day test runs with `keep_generations: 2`. A live pin produces `pinned_expired_generation`, and verify fails loudly rather than deleting. | — |
| B5 gc pins and lease | **Resolved** | §3.8.1: `run-daily` holds only the schedule lock. Compose (`WritePath.ingest`), `gc` and `delete_generation` each take the registry lease per call, which matches `write.py:226,334` and the non-reentrant lease. §3.8.4: a persisted `PinRegistry` with TTL, honoured by `gc` (`GenerationPinned`), with a cross-process test. F9 is credited (P2 5 + 1). | n9: the per-request pin write cost. |
| B6 derived state outlives retention | **Resolved** | Reference-based ledger GC through per-generation `ledger_refs`. Learning raw replies are tagged with all contributing partitions. Exemplars are kept out of the embedding input as an asserted retention invariant. The store table now covers cache (generation-prefixed `invalidate`, which matches `sqlite/adapter.py:119-120`), sessions (`store_text` refused; the key exists at `core/config.py:581`), traces (`trace_masking: privacy`), content-free manifests, reports and audit, proposals, and `proposed.yaml`. Verify is content-based, from tombstone samples. | n2 (size of `ledger_refs`), n3 (per-generation ledger job entries), n8 (synthesised regex literals). |
| B7 library identity vs fingerprint | **Partly resolved: still blocking** | Resolved: provenance is recorded in partition and generation manifests, the library is snapshotted per generation, serving never reads the working files, `pattern_library` is omitted from the dump when `None` (the golden test `tests/unit/test_index_fingerprint_golden.py` exists, and the index fingerprint hashes `extractor.recipe_fingerprint()` and `privacy.model_dump(...)`, `write.py:154-168`, so omitting unset or default keys preserves it), and the false A1_RECIPE_KEYS claim is removed. **Not resolved:** §3.4.3 hashes "only static options … the library contents and the engine version are not hashed". `WritePath._extract` keys reuse as `extraction:{recipe_fingerprint[:16]}:{chunk_id}` and returns the cached `ExtractionOutput` on a hit (`write.py:566-577`). Chunk ids hash the text, not the library (`extraction/chunking.py:33`). After an approval, deprecation or supersede, every `signature`/`issue_report`/header chunk whose text did not change gets its **old** entities and relations back. The N11 reparse recomputes partition aggregates but not this compose-time extraction. | **Required change:** derive the extraction reuse key (and the `ledger_refs` it feeds) from `recipe_fingerprint + pattern_provenance` (library identity, engine, engine version, validator version), which stays outside the live index fingerprint. Equivalently, add a `reuse_salt()` to the extractor contract that the write path appends. Test `test_library_change_invalidates_extraction_reuse`: approve a pattern that matches an unchanged exemplar; the next compose shows the new relation, a deprecated pattern's relation is gone, and `test_index_fingerprint_golden.py` is still green. |
| B8 header endpoints vs containment | **Resolved** | §3.4.2: facts are line-scoped (endpoints and trigger in one line, a `line` chunk recipe, so `endpoints_contained` is satisfied unchanged). `$attribute` edges are `attribute_links` → `native_link`/`unchecked` within one header chunk. The validator skips non-fact/co_occurrence kinds (`core/types.py:214`), and `unchecked` is forbidden only for `fact` (`:163`). The lint error for `$attribute` with `trigger_group` is in place. Every kind has a `header`. End-to-end validation test named. | n1: the `line` recipe vs the default chunk size. |
| B9 library write protocol | **Resolved** | §3.3 and §3.6.3: users own `library.yaml`, which the system never opens for writing. The system appends only to `proposed.yaml` and `decisions.yaml`, and decisions are replayed at load (supersede shadows its target). The sha CAS is re-checked before `os.replace` and verified after. Git commit runs with `SystemFileDirty`. `revision` is advisory. Promotion and retirement are append-only records. The CLI-only rule is stated. Tests for a concurrent save and a byte-identical `library.yaml` are named. | The residual race is limited to hand edits of system files, is acknowledged, and is recoverable from git. Acceptable. |
| B10 daily cost cap | **Resolved** | §3.6.4: a persistent `SpendLedger` with a single conditional `UPDATE` reservation, scopes for learning, bootstrap (total and monthly), community reports and a global daily cap. Day is in `logs.timezone`. The provider wrapper refuses calls without a reservation. Crashed reservations count as spent. Two-process test named. +3 ed. | n4: adapter coverage. |
| B11 redaction contract | **Resolved** | §3.2.3: `PrivacyConfig` gains `redactors` (named built-ins), `engine: legacy\|safe` and `max_line_bytes`. `safe` compiles user patterns with the library validator and RE2. `sources:` and the log profile require `safe`. Redaction runs in `LogMapping` before partition, exemplars, queue and prompts, and the prompt builder re-checks. New fields are omitted from the dump at their defaults, which keeps the fingerprint: `privacy` is hashed through `model_dump` (`write.py:161`). The deployment-style config validates against the declared `Literal`. | — |

## Remaining blocking findings

| ID | Plan section | Finding | Evidence | Required change |
|---|---|---|---|---|
| B7 (residual) | §3.4.3, §3.8.3 `ledger_refs` | Library changes do not invalidate compose-time extraction reuse, so stale relations and entities are served after approve, deprecate or supersede. | `ingestion/write.py:566-577` (reuse keyed by recipe fingerprint); §3.4.3 "library contents and the engine version are not hashed". | Salt the extraction reuse key with `pattern_provenance` (outside the live fingerprint) and add the test described above. |

No new blocking findings were raised on unchanged sections. The B7 residual is in a section rev 2
changed: it is a consequence of the B7 fix, not something round 1 missed.

## Non-blocking findings (new in round 2)

| ID | Section | Finding | Suggested change |
|---|---|---|---|
| n1 | §3.4.2 `line` recipe | "lines are ≤ `max_line_bytes` ≤ chunk size" does not hold at the defaults: `ingestion.chunking.size` defaults to 1600 characters (`core/config.py:550`), while `max_line_bytes` defaults to 8192. A long line would be split, and its line-scoped facts silently lost. Multiline records (up to `multiline.max_bytes`) also need a stated rule. | The `line` recipe emits one chunk per line regardless of `size`, or config validation requires `max_line_bytes ≤ size`. State whether a reassembled multiline record is one "line". |
| n2 | §3.8.3 `ledger_refs` | The window holds 10⁵–10⁶ documents, so a key set in each generation manifest (registry JSON) would be tens of MB per manifest. | Store references in a ledger table `(generation, key)` and keep a count and hash in the manifest. |
| n3 | §3.8.3 | Per-generation ledger job entries (`ledger.append(gen, …)`, read by `status()` at `write.py:198-203`) for deleted generations are not in the store table. They hold document ids, not line text. | Purge them with the generation, or list them as content-free. |
| n4 | §3.6.4, §3.8.4, §3.12 | `SpendLedger` is specified for sqlite/postgres and `PinRegistry` for sqlite/postgres/file. Ledger and registry backends also include sql, mongodb, memory and (for registries) Elasticsearch. | Implement them, or have config validation refuse `logs:`/`learning.patterns` on backends without them (no silent fallback). |
| n5 | §4.2 | The arithmetic is slightly off. 328 − 61 + (92…97) = 359–364, not 360–365. B10 sits on stream A but gates L4b on stream B. Stream B (B11 → L2 → B2+B3 → L4a → L4b → L5 = 50 ed) plus shared docs is about 11 weeks, against "about 10". The usable-deployment figure (67 ed; stream A critical at 37 ed ≈ 7.4 weeks, with L3a waiting on L2 at about day 16) is consistent. | Correct the range. Move B10 to stream B or note the dependency. Say "10–11 weeks". |
| n6 | §3.9.2 | The "tenants affected by S" bound (~10⁴ `signature_day` rows) equals the default `max_matched`. | Add a `signature_tenant_window` rollup, or bound it per day by default. |
| n7 | private companion (generic) | One runbook line still says spend is "enforced by the meter". | Point it to the spend ledger and `retention report`. |
| n8 | §3.8.3 `proposed.yaml` | Synthesised regexes carry literal tokens from mined (redacted) lines, and those tokens outlive retention. | List `proposed.yaml` literals under O7 in `retention report`, or `expire` them with their clusters. |
| n9 | §3.8.4 | A pin per request means a registry write on every request (file or SQLite contention on the serving path). | Allow a per-process pin per generation, renewed by a heartbeat, with an in-process refcount. Keep per-request pins only where a request can outlive the process lease. |

## Requirements coverage (round 2)

| Requirement | Status | Section |
|---|---|---|
| Daily pull from a 7.x cluster, read-only | covered | §3.1, §3.8.1 |
| Extraction into db / KG / communities | covered (pending B7 residual for KG freshness) | §3.4, §3.7, §3.9.2, §3.10 |
| Rolling ~10-day retention, deleting derived state everywhere | covered | §3.8.3–3.8.4 |
| Pattern extractor as a first-class extractor kind | covered | §3.3–3.4, §3.12 |
| Pattern file maintained by user and LLM | covered | §3.3, §3.6.3 |
| LLM names issues, proposes new or fixed regexes, re-runs | partial: re-run is correct for aggregates, but compose-time reuse is stale (B7 residual) | §3.6, §3.7 |
| Learning cost caps | covered | §3.6.4 |
| No auto-applied learning without explicit policy | covered | §3.6.2, §3.12 |
| Fail-closed grounding | covered | §3.4.2, §3.9.1 |
| ReDoS safety | covered | §3.2.3, §3.4.1 |
| No secrets on command lines | covered | §3.8.1 |
| Volume: store signatures/aggregates, not lines | covered | §3.1.3, §3.7 |
| Analytics answerable exactly from aggregates | covered | §3.9 |
| Synthetic-only tests | covered | §5 |
| Estimates | covered (minor arithmetic, n5) | §4 |
| Consistency with the completeness review | covered (F9 and Phase 2 re-base) | §4.2 |

## New claims verified against code

| Claim | Result |
|---|---|
| The golden fingerprint test exists and stays green when unset keys are omitted | `tests/unit/test_index_fingerprint_golden.py` exists. `index_fingerprint` hashes `extractor.recipe_fingerprint()` and `privacy.model_dump(exclude={"trace_masking"})` (`write.py:154-168`), so omitting `pattern_library` when `None` and the new privacy fields at their defaults preserves it. Plausible as specified; it must be implemented with explicit `exclude` logic. |
| Lease split: compose and `gc` take the lease themselves | Consistent with `write.py:226` (`ingest`) and `:334` (`delete_generation`), and with `lease.py` being non-reentrant. |
| A pin registry is needed beyond in-process `protect()` | Confirmed: `sqlite/adapter.py:43,107-114` keeps an in-process `set`. |
| Reference-based ledger purge covers extraction and embedding keys | Key formats match (`write.py:572`, `:631`). Gaps: B7 residual (reuse salt) and n2/n3. |
| `PrivacyConfig` additions are additive | Current fields are `redact_emails`, `patterns`, `metadata_fields` and `trace_masking` (`core/config.py:646-652`). The additions do not collide. |
| Structured contract changes apply to 7 records adapters | `execute` exists in memory, sqlite, sql, postgres, mongodb and opensearch. Elasticsearch reuses `OpenSearchRecords` (`elasticsearch/adapter.py:54`). The count of 7 matches. |
| `FieldSpec.role` is new | `FieldSpec` (`core/types.py:262-277`) has no `role`, so the change is additive. |
| `store_text` exists and can be refused in the log profile | `core/config.py:581`, `engine.py:205`. |
| Cache invalidation per deleted generation | `sqlite/adapter.py:119-120` invalidates the generation prefix on delete. |
| `google-re2` verification (1.1.20251105; `re2.Set`; ASCII classes) | Not re-run here (package not installed in this environment). Accepted as the plan's own recorded evidence. VALIDATION should carry it with scope. |
| Estimates: phases sum to 98, credits to 22, usable milestone 67 ed | Arithmetic checked: 98, 22 and 67 are correct. The baseline range is off by one (n5). |
