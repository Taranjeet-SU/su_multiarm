# Code review: parity feature branch (`a0241af..b99eae2`)

Commits reviewed: feb3732 (vector import + alignment audit, strict source order, scalable sqlite vector
index), c8a1952 (per-arm source slots, linear-time FTS replace), d04047d (2.0.0a1 fingerprint
preservation), 16a3e23 (packing reserves budget for aggregates), 45e93f2 (count_distinct by field).
Reviewer: independent code review, 2026-11-04. No source modified.

VERDICT: REVISE

Most of the branch is sound. Source order and source slots keep membership and are deterministic. The
bounded Faiss search returns the same results as the exact numpy path on tie-free data. Packing cannot
overflow. The 2.0.0a1 golden index fingerprints still match. ruff passes, and the full suite passes:
1035 passed, 123 skipped. Three defects block approval. (1) The count_distinct fix still gives a wrong
"exact" number when records are missing the field or hold null for it. That is the same kind of silent
miscount the commit set out to fix, and the shared code path makes every records adapter get it wrong
in the same way. (2) The "alignment audit" for imported vectors cannot detect a vector row that is
paired with the wrong key. The keys file, which is the only thing tying vector rows to text, has no
hash pin and is not recorded in the manifest, so the documented provenance claim does not hold.
(3) An index built from imported vectors gets an index fingerprint that includes the host's absolute
file paths. That breaks the documented rule that index identity does not depend on the checkout, and
moving the files makes a valid index refuse to serve.

## Blocking findings

| id | area | finding | evidence | required change |
|----|------|---------|----------|-----------------|
| B1 | 45e93f2 `core/types.py` Measure; `backends/_scope.py::measure_of` | `count_distinct` over a declared field counts every record that lacks the field as its own distinct value, and counts an explicit `null` as one more distinct value. It still reports `missing_or_invalid=0` and `exactness="exact"`. The commit fixed "fell back to record ids" for the whole measure, but the same id fallback is still applied per record. This logic is shared by memory, sqlite, sql, postgres, mongodb and opensearch/elasticsearch (all call `scope_result` → `measure_of`), so every records adapter is wrong in the same way, and group_by buckets are affected too. | `measure_of`: `len({r.values.get(unit, r.id) for r in rows})`. Probe with values A, B, 3 records missing the field, 2 nulls: `measure_of(ScopeSpec(measure=Measure(kind="count_distinct", field="organization")), …)` returns `(6, 0)`. Expected 2 distinct values, with 5 records reported missing. The added test only uses rows where every record has the field. | When `unit` is a declared field (not the canonical unit and not `id`), leave records whose value is missing or `None` out of the distinct set and count them in `missing_or_invalid`. Keep the id fallback only for the canonical unit or `equivalence_field` semantics. Document the null policy. Add a unit test with missing and null values, plus a `testing/backend_conformance.py` case so every adapter is held to the same semantics. |
| B2 | feb3732 `ingestion/precomputed.py`, `PrecomputedVectors` config | The audit does not prove that rows line up with content. A row is linked to its key only by its position in two separate files. `sha256` pins only the `.npy` file. The keys file has no pin and its digest is not recorded in the manifest. A keys file that is reordered or regenerated against a different vector dump passes every check (counts, dimensions, finite, non-zero, unique), and ingest then builds an index of silently wrong vectors. The module docstring says a vector "is accepted only when it can be tied to one chunk" and that the manifest digest "lets a later reader check the provenance". Neither holds for the keys half of the import. | `PrecomputedVectors.__init__`: `file_sha256(vectors_path)` only; `self.index[key] = row` comes from the line number; `PrecomputedAudit.manifest()` has no keys digest. Tests reverse both files together (`write_vectors`), so a mismatched pair is never tested. | At minimum: record the keys-file digest in the manifest and allow it to be pinned (`keys_sha256`, or one `sha256` covering both files). Correct the docstring and schema description to say that row↔key correspondence comes from position and cannot be verified from the files alone. Add a test where the keys file is permuted relative to the vectors and check that the result is detected or explicitly documented as undetectable. Recommended: a provider spot check (re-embed N sampled matched chunks with the configured query model and require cosine ≥ τ). This also covers the "rows come from the query model" gap that the docstring admits. |
| B3 | feb3732 / d04047d `ingestion/write.py::index_fingerprint` | When `precomputed` is set, the index fingerprint hashes `embedder.model_dump()`, which includes `precomputed.vectors` and `precomputed.keys` as resolved absolute paths (`_resolve_paths`). Index identity therefore depends on host and checkout, which contradicts `Config.fingerprint`'s documented contract that absolute paths are "not an index identity". If the import files are moved, or the state is served from another host or path, `current()` raises `IndexNotReady("index/config fingerprint mismatch")`. Without `sha256`, the reverse also happens: different vector content at the same path keeps the same identity. The embedding reuse recipe already excludes the paths (`include={"sha256","on_missing"}`), so the two identities disagree. | `write.py:166` excludes only `pricing_key` (plus `precomputed` when it is unset). `core/config.py` `_resolve_paths` makes both paths absolute. `Config.fingerprint` docstring at `core/config.py:856-860`. | Exclude `vectors` and `keys` paths from the index fingerprint and use content identity instead: the configured `sha256` (and keys digest per B2). Consider requiring `sha256` whenever `precomputed` is set, so identity is always content-based. Add a test that relocating the import files keeps the fingerprint, while changing the pinned digest changes it. The 2.0.0a1 golden values are unaffected because the block is still excluded when unset. |

## Non-blocking findings

| id | area | finding / recommendation |
|----|------|--------------------------|
| N1 | 16a3e23 packing | Overflow is not possible: extras are admitted only while `used + len ≤ budget`, and documents are allocated from `budget - used`. Documents can now be starved, though. Aggregates are reserved first with no cap, and one large aggregate (a `bucket` measure over a long range, or an ungrouped `top_n`-less `group_by` value dict), or several, can use the whole budget so that every document is omitted. Omission is reported in `omitted_ids`, so it is not silent, but this reverses the earlier priority for every config without a knob. Recommend a cap on the reservation (for example `packing.aggregate_reserve_max` as a fraction or chars), or a guarantee that the top document keeps `min_per_record`. Add a test for "large aggregate + documents". |
| N2 | c8a1952 FTS replace | The replace is correct for re-indexing the same chunk set: the fresh generation catalog plus `DELETE … IN (temp)` leaves no duplicates (probe: re-index of 50 chunks, 3000→3000 rows). There is one regression against the old per-chunk delete+insert: a chunk id repeated *within one* `index()` call is inserted twice. The probe ran `index(gen, chunks + chunks[:5])`: 3000→3005 rows, 5 duplicate `chunk_id`s, which would double-count in BM25. This cannot happen through `WritePath`, because chunk ids are content-addressed and unique per corpus, but it can through the `LexicalIndex` contract. Deduplicate `selected` by id. Deletes and updates are handled by building a new generation, not by `index()`, and that is unchanged. No test covers FTS replace; add one for re-index and duplicate input. |
| N3 | feb3732 vector upsert | The replace path is correct. Probe: an upsert over pending ids and over stored ids keeps ids and rows aligned, and replaced ids move to the end as before. However, `set(pending_ids)` is rebuilt on every batch, so ingest is still O(n²/batch) in Python. Probe: 2,000×64-row batches (128k rows) took 8.8 s, which projects to about 2 minutes at 500k. Keep a persistent id→position dict or set alongside `_pending`. No unit test covers the replace/compaction path; add one. |
| N4 | feb3732 Faiss bounded search | Exactness checked: on tie-free imported Gaussian vectors (3000 chunks, 60 queries × 4 scopes × k∈{1,10,100,2000}), Faiss and numpy matched except for 13 adjacent swaps at k=2000 with no scope. The swapped pairs differed by ≤3e-8, which is float noise; the old full-depth Faiss path had the same behaviour. Note that `IndexFlat` scans all N vectors whatever `k` is, so the depth-growth loop can run up to about log₈(N/1024)+1 *full* scans for a selective scope (4 at 500k). For `document_ids` scopes, scoring the eligible rows directly would be exact and cheaper. Cached Faiss indexes (and `_loaded`) are released only by `forget`, so a long-lived server keeps one copy per generation it has served. |
| N5 | c8a1952 source slots | Deterministic (`(-score, record_id)`) and membership-safe within `per_arm`. Two behaviours should be documented or traced. (a) Slots also *reorder* each arm (slot picks first), so a low-score slotted record gets rank 1 under rank-based fusion even when the budget is not binding. (b) Slots apply to the `exact_id` arm too, and with `len(source_slots) ≥ per_arm` they can push a pinned exact hit out of a large exact_id pool. Consider exempting pinned candidates. `SelectionTrace` records `source_order` but not `source_slots`; add it. `source_slots` is not validated against `per_arm` (it is silently truncated to `per_arm`). |
| N6 | feb3732 source order | Stable, membership unchanged, runs after the `max_documents` cut and before pins (verified in `ordering.py:109` and `select`). Minor inconsistency: sorting maps `None` source to `""`, but the reason tag checks `record.source in position`, so listing `""` sorts None-source records without tagging them. |
| N7 | precomputed vs embedding input | The text the engine embeds is `EMBEDDING_INPUT` (`{title}\n{field}: {text}` plus `document_prefix`). Imported rows are keyed on `chunk.text` alone and almost always embed a different input. The manifest's `embedding` block still implies the engine template. Record `"input": "imported (unknown template)"` (or an operator-declared template) in the manifest/recipe when `precomputed` is set. |
| N8 | precomputed.py hygiene | `ProjectionError = PrecomputedVectorError` aliases a name that means a different class in `write.py`, which is confusing. Also three blank lines before the class. The `matched`/`embedded` counters are not meaningful after a mid-stage failure (they count windows already processed); the manifest is only written on success, so this is cosmetic. |
| N9 | docs | No CHANGELOG, ARCHITECTURE or VALIDATION entries for the three new config surfaces (`embedder.precomputed`, `selection.source_order`, `selection.source_slots`) or for the packing priority change. `docs/config.schema.json` was regenerated (the test in `test_config_v2.py` passes). The commit messages cite figures from a "production-size replay" (1,225 vs 68): these are aggregate counts with no names, so they are acceptable, but keep such provenance out of public docs unless they are sanitized as AGENTS.md requires. |
| N10 | config_v1 downgrade | Correct: downgrade refuses `precomputed`, and `EmbeddingConfig` excludes it. `source_order`/`source_slots` are not refused on downgrade and are silently dropped in the v1 config. Consider refusing them when they are non-empty, to match the precomputed handling. |

## Requirements coverage (review brief)

| requirement | status | notes |
|---|---|---|
| Alignment audit proves row/id/content alignment | partial (B2) | key↔chunk alignment is proven by (document, field, sha256(text)), and the count/dimension/finite/zero/duplicate/digest checks hold. Row↔key alignment depends on position and is not covered by any pin. |
| Strict source order + slots preserve membership and determinism | covered | Verified in code and tests. Slot side effects are in N5. |
| Packing reservation cannot starve documents or overflow | partial (N1) | Cannot overflow. Can starve documents (reported, but uncapped). |
| count_distinct semantics incl. nulls on every records adapter | missing (B1) | All adapters share `measure_of`, so they are consistent with each other, but wrong for missing and null values. |
| Scalable vector index exactness vs old path | covered | Probe shows exact results apart from float-noise tie swaps. Perf notes are in N3/N4. |
| FTS replace correctness on deletes/updates | covered (N2) | Deletes and updates go through a fresh generation. Re-index is idempotent, except for duplicate ids within one call. |
| Golden fingerprint stability | covered | `tests/unit/test_index_fingerprint_golden.py` passes. The embedding reuse recipe is unchanged when unset. B3 concerns the set case only. |
| Tests for each | partial | Missing: FTS replace, vector replace path, count_distinct nulls, permuted keys file, path-independent fingerprint, large aggregate vs documents. |
| Hygiene (no customer data) | covered | Diff is synthetic only. Grep for URLs, emails, home paths, secrets and vendor names found nothing. |

## Claims verified against code

| claim | result |
|---|---|
| "audit refuses row/key count, dimension, non-finite, zero, duplicate-key and digest mismatches before writing" | True. The audit runs in `_embed` before the first upsert, and the tests confirm nothing is activated. |
| "matched by (document, field, sha256 of exact text), never by position" | True for chunk↔key. False for key↔row, which is matched by line position (B2). |
| "manifest records the vector file's digest so a later reader can check provenance" | Only the vectors digest is recorded; the keys digest is missing (B2). |
| "precomputed enters the fingerprint only when set; 2.0.0a1 goldens unchanged" | True (golden test passes). When set, it brings in absolute paths (B3). |
| "source_order never changes membership; runs before pins" | True. |
| "source_slots: best remaining record of the named source per slot, then fill by score" | True. It also reorders the arm (N5). |
| "linear-time FTS replace" | True: one `DELETE … IN` over a temp PK table. Duplicates within one call are a regression (N2). |
| "block-append upserts (was quadratic)" | Partly. The matrix restack is gone, but the per-batch `set(pending_ids)` is still quadratic (N3). |
| "bounded search depth grows under a scope filter", exact | True. Results equal numpy up to float-noise swaps (N4). |
| "aggregate size reserved first; output order unchanged; oversize aggregate omitted" | True. No overflow; documents can be starved (N1). |
| "count_distinct field becomes the distinct key; field+different unit rejected" | True, but the per-record id fallback still miscounts missing and null values (B1). |

## Commands run

- `.venv/bin/python -m ruff check src tests`: all checks passed.
- `.venv/bin/python -m pytest -q -p no:cacheprovider --basetemp ~/su_mutliarm_tests/claude/parity_review/bt` (with `PYTHONDONTWRITEBYTECODE=1`): 1035 passed, 123 skipped, 5 warnings in 71.6 s. faiss 1.14.3 is installed, so the Faiss test ran.
- Synthetic probes (scripts kept outside the repo; working dirs under `~/su_mutliarm_tests/claude/parity_review/`): count_distinct with missing and null values; Faiss vs numpy over imported Gaussian vectors; FTS re-index and duplicate input; vector upsert replace path and batch timing.
