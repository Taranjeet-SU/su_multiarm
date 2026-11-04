# 2.0.0a3 release review

Scope: `git diff 4f014d1..7996c7b` (published 2.0.0a2 `origin/main` to `v2/phase-0`, 98 files,
+11,186/-380). Reviewer: independent architect review. No product code was changed. No services
or containers were touched. No model calls were made.

VERDICT: REVISE

The merged code is in good shape. Ruff is clean, the full suite passes, the wheel and sdist build,
`docs/config.schema.json` equals `write_all` output, and the 2.0.0a1 golden index fingerprints
hold. I re-ran the cec3ffb merge from both parents. Only `selection/limits.py` and
`backends/sqlite/search.py` conflicted, and the hand resolution keeps both sides' behaviour.
Kuzu attach left a store byte-identical after a full read session, and it fails closed on predicate
scope. The bm25 and bitset paths apply ACL, allow/deny and field eligibility before the top-k.

Three things stop the release:

- **B1: the sdist is wrong when built from this working tree.** It ships a second, older copy of
  the repository.
- **B2: the CHANGELOG claims a neo4j result that was never run.**
- **B3: an opt-in bm25 tokenizer silently drops `must` and phrase words.** This widens results
  that the structured arm then reports as exact topic counts.

Each fix is small.

## Commands run

All runs used the `~/su_mutliarm_tests/review_a3/` workspace with `PYTHONDONTWRITEBYTECODE=1`.

| Command | Result |
|---|---|
| `.venv/bin/python -m ruff check src tests` | All checks passed |
| `.venv/bin/python -m pytest -q --basetemp ~/su_mutliarm_tests/review_a3/tmp -p no:cacheprovider` | **1190 passed, 124 skipped**, 5 warnings, 111 s |
| Targeted: `test_kuzu_attach`, `test_kuzu_conformance`, `test_bm25_lexical`, `test_communities_hierarchy`, `test_traversal_templates` with `-rs` | 50 passed, 1 skipped (neo4j template identity: no server on 127.0.0.1:17687) |
| `.venv/bin/python -m build --outdir ~/su_mutliarm_tests/review_a3/dist .` | `su_multi_arm-2.0.0a3.tar.gz` (1.9 MB, 664 entries) and `su_multi_arm-2.0.0a3-py3-none-any.whl` (520 kB). See B1. |
| `write_all(<tmp>)` then `cmp` against `docs/` | `config.schema.json` and `config_v1.schema.json` identical |
| Merge re-run of 713989f + 5513f7e in a throwaway worktree (removed afterwards) | Conflicts only in `search.py`, `limits.py` and three docs files. Every auto-merged file (engine, config, write, collapse, ...) equals cec3ffb. |

## Blocking findings

| ID | Plan / doc section | Finding | Evidence / reproduction | Required change |
|---|---|---|---|---|
| B1 | Release build (AGENTS.md: wheel/sdist build for release changes; public-repo hygiene) | **The sdist contains `.claude/worktrees/agent-a0dd61f541791a8d6/`.** That is a full second checkout of the repository at 5513f7e: 321 files, including its own `pyproject.toml`, `CHANGELOG.md` and `src/`. Hatch includes it because the directory is untracked but not ignored. `.gitignore` has no `.claude/worktrees/` entry, and `pyproject.toml` has no sdist exclude. An a3 sdist built from this tree would publish a stale, divergent copy of the source. The wheel is clean. | `tar tzf ~/su_mutliarm_tests/review_a3/dist/su_multi_arm-2.0.0a3.tar.gz \| grep -c .claude/worktrees` prints `321`. `git status` shows `?? .claude/worktrees/`. | Add `.claude/worktrees/` to `.gitignore`, or add `[tool.hatch.build.targets.sdist] exclude = [".claude/worktrees"]`, or both. Rebuild and assert that the sdist has no `worktrees` entry, ideally with a release-identity test that lists the sdist. Build release artifacts from a clean checkout. |
| B2 | CHANGELOG.md:93 versus docs/VALIDATION.md (Kuzu section, "Not run") and AGENTS.md ("Distinguish implemented features, mocked contracts, real-service integration checks...") | CHANGELOG.md:93 says: "The results are the same on sqlite, neo4j, kuzu and the in-memory backend." VALIDATION.md lists **neo4j template identity as not run**, because no Neo4j server was up. In this review the neo4j case was skipped as well. That makes it a public integration claim with no evidence behind it. | `pytest -rs tests/conformance/test_traversal_templates.py` gives `SKIPPED ... test_traversal_templates.py:53: neo4j test server not reachable`. | Change the sentence to "identical on sqlite, kuzu and the in-memory backend (synthetic, in-process); neo4j is covered by the same kit but was not run for 2.0.0a3". Alternatively, run the external neo4j case against an isolated container and record the result. |
| B3 | ARCHITECTURE.md lexical contract; CHANGELOG "Literal topic counts ... (`text_backend`)"; AGENTS.md "Do not silently ignore..." | **bm25 boolean `must`/`must_not` terms and phrase words that the analyzer reduces to no tokens are silently dropped.** `search.py:303-310` skips a `must` whose analysis is empty (`if need:`), and the phrase path at `search.py:292-300` verifies only the surviving tokens. Under `bm25.tokenizer: ascii_alnum`, any non-ASCII word disappears. The query then matches documents that do not contain it. `arms/structured.py::text_query` builds exactly this kind of boolean `must` query for topic counts and reports the result with exact semantics. The default `unicode_word` tokenizer is not affected. The setting is opt-in, but it is documented and validated. | `$SCRATCH/bm25_must.py` builds d1 "tax refund for 東京 office", d2 "tax refund for the berlin office", d3 "unrelated", then runs `LexicalQuery(kind="boolean", text="東京 tax", must=["東京","tax"])`. Output: `unicode ['d1']`, `ascii ['d2', 'd1']`. d2 has no 東京. | When a `must` term or phrase word analyzes to zero tokens, return no match, or raise a typed `BackendError` that the structured arm turns into an `unsupported` constraint (`exactness: bounded`). Do the same for a phrase whose `\w+` words do not all survive analysis. State the `must_not` policy explicitly. Add a unit test with `tokenizer: ascii_alnum` and a non-ASCII `must` word. |

`$SCRATCH` is
`/tmp/claude-1001/-home-taranjeet-bakshi-su-multi-arm/7ff26791-28c0-42e8-951d-ff2b25db8445/scratchpad`.
The script uses `tests/unit/test_bm25_lexical.py::build`.

## Non-blocking findings

| ID | Location | Finding | Reproduction / evidence | Suggested change |
|---|---|---|---|---|
| N1 | `arms/structured.py:152`, `backends/capabilities.py:104` | A topic-count `basis` names the lexical analyzer from the static capability matrix. Under `lexical_engine: bm25` it says `fts5_unicode61`, but the generation manifest records `bm25_okapi:<tokenizer>:<case>:<stem>`. The answer evidence therefore mislabels how the words were matched. | `caps.flags('sqlite','lexical')['analyzer_id']` returns `fts5_unicode61`. `SqliteBackend(p, lexical_engine='bm25').analyzer_id(gen)` returns `bm25_okapi:unicode_word:lower:none`. | Use `backend.analyzer_id(gen)` when the backend has one, which is already what `GenerationSet.analyzer_id` does. |
| N2 | `compat/config_v1.py:587-640` (docstring: "anything else is a `ConfigError`") | `downgrade()` silently drops `lexical_engine: bm25`/`bm25`, `lexical.verbatim_boost`, `lexical.pin_identifiers`, `graph.templates`/`ontology` and `structured.text_backend`. `Config.legacy_fingerprint()` is built on `downgrade()`, and the manifest's `legacy_fingerprint` for a bm25 config collides with the FTS5 config's. Legacy-read is not implemented (`current()` refuses `schema_version != 2`), so serving is unaffected. The execution log already lists this as a follow-up. | Take `examples/v2/local.yaml` and set `lexical_engine: bm25` and `verbatim_boost.enabled`. `downgrade()` then succeeds with `arms.lexical == "sqlite"`, and `legacy_fingerprint` equals the FTS5 config's (`True`). | Refuse non-default values of these keys in `downgrade()`, as was done for `source_slots`/`precomputed`. List them in the CHANGELOG line "The v1 downgrade refuses ...". |
| N3 | CHANGELOG.md:112; CHANGELOG.md:8/209 | The "Fixed" bullet says "bounded search depth grows under a scope filter". The shipped code says the opposite (`search.py:478-479`: "the scope is a row bitset applied before the top-k, so the depth never has to grow"), and the next bullet also describes the bitset. Separately, there is no `[2.0.0a3]:` link reference, and the empty `[Unreleased]` heading has no link either. | `grep -n depth src/su_multi_arm/backends/sqlite/search.py` | Drop the stale clause and add the compare link. |
| N4 | `core/config.py` `ChatRole.coherent` and `AnswerContractConfig`; `compat/config_v1.py::upgrade` | v1 configs adopt both a3 defaults silently. Upgraded Anthropic roles become `native` (`output_config.format` on Haiku/Sonnet 5.x), and the contract becomes `strip`. The extractor's output mode is not part of the extractor recipe, so an a2 index stays "valid" while new documents are extracted in a different wire mode. | `load_config("examples/anthropic.yaml")` resolves all five roles to `native` with `on_violation` = `strip`. The golden fingerprint is unchanged. | Add an "Upgrading" note to the CHANGELOG. Consider having `upgrade()` pin `structured_output: none` and `on_violation: abstain_with_reason` so v1 behaviour is preserved, with a warning diagnostic. |
| N5 | `core/config.py` `KuzuBackend` (no `recipe()`); `ingestion/write.py::_backend_recipe` | The kuzu index recipe is the full `model_dump`. Tuning `timeout_seconds` or `buffer_pool_mb` invalidates the index, and attach mode fingerprints the mapping **path** but not its content (`require_verified`, patterns, `generations`). Attach is also not reachable through `Engine.ingest`: graph `upsert` is refused and its activation is `none`. It is a parity and measurement tool, which ARCHITECTURE says, but CONFIGURATION does not say how to serve with it. | Read `write.py:156-175` against `KuzuBackend` at `config.py:172-186`. | Give `KuzuBackend` a `recipe()` that excludes operational knobs and, for attach, hashes the mapping content. Document the supported serving path for attach. |
| N6 | `tests/unit/test_kuzu_attach.py` | Nothing tests that an attached store is unmodified. I checked it by hand: it is byte-identical and gains no new files after traverse, `find_entities` and communities. That guarantee is the main claim of attach mode. | `$SCRATCH/attach_ro.py ~/su_mutliarm_tests/review_a3/attach_ro` prints `UNCHANGED`. | Add a test that hashes the store files (and lists the directory) before and after a read session. |
| N7 | `backends/sqlite/bm25.py:160-187` | A segment is rewritten in place (`np.save` on existing files, `meta.json` last, not atomic). A crash mid-write, or an in-process reader holding mmaps during a rebuild of the same generation, can see torn files. Only a pre-publication generation is affected. | Code read. | Write to a temporary folder and `os.replace` it. Alternatively, record a content digest in `meta.json` and verify it on load. |
| N8 | `backends/sqlite/adapter.py:77-107` | With predicates (always, under ACL), `document_mask` scans every matching document id in the catalog. It does this once per request per facet (lexical and vector). That costs O(eligible docs) per query, which is fine at the measured 58.7k but grows linearly. | Code read. | Cache masks per (generation, canonical scope hash) with a small LRU. |
| N9 | `selection/limits.py:17-34` | The hand-merged order (pinned, then `leads`, then slots, then score) is correct. It has no unit test with pinned and `leads` candidates in the same arm. Without `source_slots`, pinned candidates are not protected from the per-arm budget. That is unchanged from a2. | `limit(pool, 3, ['x'])` returns `['p','l','s']`, `limit(pool, 1, ['x'])` returns `['p']`, and `limit(pool, 3)` returns `['h','l','s']` (pinned `p` dropped). | Add the combined test. Document that pins are honoured in `limit` only under slots, or protect them always. |
| N10 | `docs/plans/v2_implementation_plan.md:340, 579, 776`; §3.13 | The plan still gives `abstain_with_reason` as the contract default and `leiden_hierarchical` as the strategy. The owner decision (`strip`) and the Louvain substitution appear only in CHANGELOG and VALIDATION. | grep | Add a dated plan amendment line so the plan and the code agree. |
| N11 | README.md:35, 293 versus VALIDATION.md (bm25 and Kuzu sections) | README says relevance on real corpora "has not been measured for v2". VALIDATION now records aggregate R@10/MRR on a private corpus. | grep | Reword: "measured only as sanitised aggregates on one private corpus; no public benchmark claim". |
| N12 | `docs/plans/execution_log.md:1004, 1150`; VALIDATION.md private-parity paragraphs | Hygiene is acceptable: aggregates only, and no names, endpoints, keys or IPs other than `127.0.0.1` in tests. The log does point to private report paths under `~/su_mutliarm_tests/parity/...` and to an "SU 45-question" set, and VALIDATION gives the private store's shape (0.96 GB, ~600k nodes, 28/30 tables). | grep of added lines for IPs, URLs, keys, emails and vendor/customer terms: only `127.0.0.1` and the repo's GitHub URL. | Optional: replace home-path report locations with "kept outside the repository". |
| N13 | `communities/stats.py` | `size`, `trend` and `top_entities` (entity **names**) are computed over all members, whatever their ACL. They are stored but not surfaced yet. | `grep stats arms/community.py` finds no use. | Before any arm or tool surfaces `stats`, recompute or filter them under the request ACL, or mark them as restricted. |
| N14 | `arms/structured.py:69-83` together with the bm25 `_bm25_search` | Paging a topic match to exhaustion re-scores the whole segment on every page (O(pages × N)). It stays bounded by `MAX_TEXT_CHUNKS`. | Code read. | For the text-match path, compute the eligible match set once and page over it. |

## Merge-resolution check (cec3ffb)

- `selection/limits.py`: the kuzu-side parent (713989f) had pinned-first from review-fix 57c92af,
  and the lexical-side parent (5513f7e, based on b99eae2) had leads-first. The merge result puts
  pinned first, then `leads` not already pinned, then slots, then score. Nothing from either side is
  lost, the no-slot path is identical on both sides, and the docstring describes the merged order.
- `backends/sqlite/search.py` `SqliteLexical.index`: the only conflicting hunk was the `selected`
  line. The resolution keeps the dict-by-id dedupe (review-fix) and the `bm25 = ...` engine switch
  (lexical). The one-scan `_fts_replace`, the per-document title cache and the `bm25_source` insert
  are all present. In `SqliteVector`, the persistent `known` id set (review-fix) and the block
  upserts, rowmap and Faiss cache (lexical) are both present.
- The auto-merged files `engine.py` (`Candidate.leads` plus `source` and `pinned`), `core/config.py`,
  `ingestion/write.py` and `selection/collapse.py` are byte-equal to a fresh auto-merge.

## Requirements coverage

| Requirement (task brief, AGENTS.md, plan) | Status | Where |
|---|---|---|
| Kuzu attach is read-only | covered | `kuzu/adapter.py:69-74` (`read_only=True`, never creates); `attach.py:293-298, 667`; transaction refused; fixed-statement allow-list. Manual byte-identity check passes (N6: add a test). |
| Scope/ACL enforcement in bm25 and bitset paths | covered (see B3 for term handling) | `adapter.py:77-107` uses the same `scope_sql` as FTS; `_eligible_rows` applies the mask and fields before `top_rows`; `phrase_rows` is scoped; identifier pins use `inputs.scope` plus an eq predicate; conformance kit `sqlite_bm25` (predicate, `exclude_ids`, empty allow-list). Kuzu refuses predicates (`graph.py:42-48`), and the arms resolve them through the catalog. |
| Generation/fingerprint identity for bm25 | covered | `SqliteBackend.recipe()` adds the engine keys only for bm25 (golden unchanged); segment `meta.settings` checked on load; the manifest records `analyzer_id`. See N1 (basis label) and N7 (atomicity). |
| Defaults and backward compatibility | partial | Golden fingerprints hold (8/8). The a3 defaults are documented. The v1 downgrade gaps are N2, and the v1 upgrade silently adopting the a3 defaults is N4. |
| CHANGELOG accuracy | partial | B2 (neo4j claim), N3 (stale depth bullet, missing link). The other claims match the code. |
| Schema equals `write_all` | covered | Both schema files are identical. |
| Public-repo hygiene | partial | The diff is clean (N12 is optional). The sdist leaks a worktree (B1). |
| ruff, full pytest, build | covered | See "Commands run". |

## Claims verified against code

| Claim | Result |
|---|---|
| bm25 scores equal `rank_bm25.BM25Okapi` | True: the test ran (rank_bm25 present), not skipped. |
| bm25 applies scope as a document bitset before the top-k | True: `search.py:_eligible_rows`, `bm25.top_rows`. |
| bm25 engine keys enter the recipe only when selected (a1 golden unchanged) | True: `config.py` `SqliteBackend.recipe`; golden test passes. |
| A segment built with other settings is refused | True: `Bm25Segment.__init__` compares `meta.settings`. |
| `verbatim_boost` refused without an analyzer-exposing index | True: `LexicalArm.validate` emits `verbatim_boost_unsupported`. |
| Kuzu attach opens read-only and never creates a database | True: adapter and test; manual byte-identity check. |
| Attach: only statements built from the validated mapping run | True for `run()` (`session.allowed`). Catalog introspection at open (`_raw`) bypasses the allow-list, but runs on a read-only database with names taken from the store. |
| Identical template results on sqlite, neo4j, kuzu and memory | **False for neo4j** (not run): B2. |
| Vector search: bitset with Faiss `IDSelectorBitmap` | True: `search.py:493-509`. |
| "bounded search depth grows under a scope filter" | **False** (stale): N3. |
| v1 downgrade refuses `source_order`/`source_slots`/`precomputed` | True, but incomplete for the other new keys: N2. |
| Anthropic default `native` for capable models, `none` otherwise | True: `default_structured_output`. |
| `on_violation` default `strip`; an aggregate question still abstains | True: `contract.py` `_apply(aggregate_question=...)`. |
| Haiku 5.x `max_tokens < 1024` rejected; v1 upgrade raises it with a warning | True: `check_role`, `config_v1.upgrade`. |
| `docs/config.schema.json` regenerated | True: byte-identical to `write_all`. |
| Version 2.0.0a3 everywhere | True: pyproject, `__init__`, CITATION, README, docs/index; release-identity test passes. |
