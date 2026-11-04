# Release-gate review: 2.0.0a2 patch

Date: 2026-11-04. Target: branch `v2/phase-0` at `756b345`, diff `public-main..v2/phase-0`
(public-main = `a58df97`, the published 2.0.0a1 tree). Inputs: `reviews/a1_completeness_review.md`
(risks R1–R3), `reviews/qa_2.0.0a1.md` (QA-01..16), and the three 2.0.0a2 execution-log entries
(slices S, D, W) with their deviations. No source was modified. All runs used a `git archive`
export under `~/su_mutliarm_tests/claude/review_a2/` (`PYTHONDONTWRITEBYTECODE=1`, external
`--basetemp`), not the checkout.

## VERDICT: FIX-FIRST

The three parallel slices fix most of what they claim, and they coexist correctly. Write leases
and fencing hold **across processes** on all five registries. Session CAS holds across processes on
sqlite, PostgreSQL, SQLAlchemy and **MongoDB**. Incremental embeddings stay correct across deletes
and re-adds. The ingest USD cap is reserved before each call. ruff, schema drift and the wheel
quickstart are clean. Six blocking items remain:

- The QA-01 blocker is fixed only for string and enum qualifiers. A numeric qualifier ("priority 3")
  still yields a passed, wrong count.
- Removing `extraction.ontology` silently changed every index fingerprint, so no 2.0.0a1 index
  opens under a2.
- The real-service suite has one red test.
- CI's bare `pytest -q` fails at collection.
- WriteConflict and SessionConflict are not mapped to 409.
- The release still identifies itself as 2.0.0a1.

Each fix is small (roughly 1–2 engineer-days in total).

## Blocking findings

| Id | File:line | Finding | Evidence | Required change |
|---|---|---|---|---|
| B1 | `src/su_multi_arm/decision/qualifiers.py:77` | **QA-01 is not fixed for non-string qualifiers.** The candidate universe keeps only `str` observed values (`if isinstance(v, str)`). Integer, number and boolean values of filterable fields are never qualifier candidates, whether or not the field is `listable`. | Probe (llm planner replayed offline, QA corpus). "How many priority 3 tickets are open?" with the planner scope `status=open` gives `contract: passed`, value **12** `exact`, `unsupported: []`. The truth is 4. The same result with `priority` marked `listable: true`. The string/enum/time variants are correctly bounded (see the verification table). | Make numeric and boolean values candidates. Match a number word-bounded next to the field name or its label ("priority 3", "severity 2"), or match any observed value of a low-cardinality numeric listable field. Otherwise flag a bare number in an aggregate question as `qualifier_not_applied` when no predicate binds a numeric field. Add the "priority 3" regression to `tests/contract/test_scope_qualifiers_and_filters.py`. |
| B2 | `src/su_multi_arm/core/config.py:509` (`ExtractionConfig`, `ontology` removed); `src/su_multi_arm/extraction/base.py:94` (`recipe_fingerprint` hashes `cfg.model_dump()`) | **Every 2.0.0a1 index is refused under 2.0.0a2, and the message is misleading.** Removing the `ontology` key from `ExtractionConfig` changes the extractor recipe hash, and with it the index fingerprint. Slice W excluded the new embedder `pricing_key` "so existing generations stay valid", so this break is unintended and has no record. | Ingested `examples/v2/local.yaml` with the a1 tree, then ran a2 `search`: `error: index not ready: index/config fingerprint mismatch: restore original config or rebuild explicitly`. Every fingerprint component is identical except `extractor` (`d292f2…` vs `0c487f…`), and the only difference in the dumped extraction config is the missing `ontology` key. | Keep the a1 fingerprint input stable: hash `ontology: None` explicitly, or pin the recipe dict so removed keys hash as before. Add a golden test: the fingerprint of each shipped example equals its 2.0.0a1 value. If a rebuild is wanted instead, gate it with an `INDEX_FORMAT` bump, a specific `IndexNotReady` message and a release note. |
| B3 | `tests/services/test_neo4j_engine.py:162` | **The real-service suite is red.** The test monkeypatches `registry.activate` with `refuse(manifest)`, but the write path now calls `activate(manifest, expected=...)`. The TypeError is caught as an activation failure, so the asserted message never appears. Slice W updated two contract monkeypatches but ran only "conformance + multi-backend engine" externally. | External run on the five containers: **107 passed, 1 failed, 18 skipped** (`test_failed_loads_never_move_the_pointer`: `refuse() got an unexpected keyword argument 'expected'`). | `def refuse(manifest, **_)`, then re-run `pytest -m external` in full and record the result in VALIDATION. |
| B4 | `tests/unit/test_write_leases.py:32`, `tests/contract/test_record_context_acl.py:21`, `pyproject.toml` `[tool.pytest.ini_options]` | **QA-16 is not fully fixed: `pytest -q`, the CI and CONTRIBUTING command, fails at collection in a fresh editable install.** `tests/` has no `__init__.py`, so pytest puts `tests/`, not the repo root, on `sys.path`. `from tests.… import` then fails under the console-script entry point. `python -m pytest` hides this because it adds the current directory. One of the two modules (`test_write_leases.py`) is new in a2. | Fresh `[all,dev]` venv, no `SU_TEST_*`: bare `pytest -q` gives `2 errors during collection` (`ModuleNotFoundError: No module named 'tests'`). `python -m pytest -q` gives 937 passed, 124 skipped. | Add `pythonpath = ["."]` to `[tool.pytest.ini_options]` (or add `tests/__init__.py`, or use relative imports). Verify with the bare `pytest -q` in a clean venv, as CI runs it. |
| B5 | `src/su_multi_arm/serving/http.py:73-81`, `:142`, `:238` | **The new conflict errors have no conflict status.** `WriteConflict`/`IngestConflict` subclass `BackendError` and so map to **503** "unavailable". On `POST /v2/actions/ingest_delta` the action runner catches them and HTTP returns **200** with `status: failed` and the raw `IngestConflict: …` text. `SessionConflict` (CAS attempts exhausted) is a `RuntimeError` and lands in the generic **422**. MCP maps all three to a generic tool error. Slices S and W both left this as an open follow-up. | Code read: there is no `WriteConflict`/`SessionConflict` branch in `http_error`, and the action status is `403 if denied else 200`. | Map `WriteConflict` (and so `IngestConflict`) and `SessionConflict` to **409**, with a `reason` and no backend text, before the `BackendError` branch. Return 409 from the action route when the failure is a `WriteConflict`. Keep `MissingSecret` at 502 (acceptable). Add HTTP tests for both. |
| B6 | `pyproject.toml:7`, `src/su_multi_arm/__init__.py:13`, `CITATION.cff:7`, `README.md:35`, `docs/index.md:16`, `docs/VALIDATION.md:3-19` | **The release identity is still 2.0.0a1.** The wheel builds as `su_multi_arm-2.0.0a1-py3-none-any.whl`, the same name as the published artifact. `su-multi-arm --version` prints `2.0.0a1`, and so does the FastAPI app version. The repo has no CHANGELOG. VALIDATION has three per-slice a2 entries but no 2.0.0a2 status header or release-evidence entry (full external run, image digests). | `python -m build` output; `su-multi-arm --version`; `create_app(...).version` all show `2.0.0a1`. | Bump to `2.0.0a2` in pyproject, `__version__`, CITATION (and its date), the README/index alpha banners and the VALIDATION status header. Add a CHANGELOG (or a VALIDATION "2.0.0a2 release" entry) listing the fixed findings, the deviations and the B2 decision, with the external counts and digests below. |

## Non-blocking findings

| Id | Area | Finding | Recommendation |
|---|---|---|---|
| N1 | QA-04 detector precision (`generation/statements.py`) | Versions, dates, ids, ports and "Error 500" are correctly ignored. Ordinary how-to prose is still detected as statements: "A 404 error means…" → count; "Windows 10 users…" → count; "One customer reported…" / "Submit one ticket per issue" → count; "If there is no error message…" / "No errors were found in the log" → absence; "The only file you need…" / "the complete list of supported currencies" → exhaustive list. Under the default `abstain_with_reason`, a detected statement abstains the whole answer (the claim-strip exception does not apply). Slice D's paid rerun showed 0/25 abstentions on one synthetic corpus, so the real-world rate is unmeasured. | Apply rule 5 to count/absence statements only when the unit noun is a structured target or a question unit, or when the question has an aggregate intent. Exclude "<number> error/users" adjacency and "there is no <noun> message". Measure the abstain rate on a larger narrative set. |
| N2 | QA-01 residual (contract) | The number stated in the answer text is not compared with the aggregate value. "There are 99 open CloudTax tickets" with a `count` claim on an exact scope whose value is 4 gives `contract: passed`. This is recorded as a slice D follow-up. | Compare the stated numeral with the cited aggregate's value (an exact-set guard, Phase 2), at least for single-value counts. |
| N3 | QA-01 time qualifier | A time qualifier counts as applied when *any* predicate constrains *any* datetime field, whatever its value ("created in 2024" with a predicate `≥ 2025-01-01` passes the qualifier check). | Check that the predicate's range intersects the parsed period, or document this as a known limit. |
| N4 | QA-02 residual | A well-typed request filter with an unobserved value (`product: "CloudTaxx"`, or `"cloudtax"` on a case-sensitive field) gives `value 0, exact`, `zero_match: false`. An absence claim on it passes the contract. `ValueResolver` computes `zero_match` for request filters, but the aggregate the engine reports does not carry it (open since a1, S3/S4 N11). | Surface `zero_match_fields` on the aggregate and the AnswerResult. Treat a zero-match caller filter as non-exhaustive for absence claims. |
| N5 | Ingest budget | (a) `max_usd` is enforced only when set. Hosted embedders have no default write-path cap (`max_embedding_calls` defaults to `None`), and review item 3 asked to require one. (b) The overshoot is bounded only by the estimate: a provider that reports more input tokens than the bytes/3 estimate can exceed the cap by one call (synthetic probe: $200 committed against a $0.75 cap after one call). Unreported usage is committed at the estimate, which is conservative. | Warn or refuse when a priced embedder has no `max_usd`/`max_embedding_calls`. Record the overrun in the manifest as `budget.overrun`. |
| N6 | Session holds | A session-budget hold is the whole clipped request limit (deviation S-c). With `budgets.session` set, concurrent turns of one session are refused with 429. MCP binds all calls of one connection to one session, so concurrent MCP tool calls hit this. A created-but-never-consumed `answer_stream` keeps its hold until deadline + 300 s. | Document this for MCP. Consider holding the request's expected usage rather than its ceiling. |
| N7 | Leases | Expiring leases (Mongo, SQL, ES) compare `expires_at` across writer clocks, so cross-host skew is not handled (slice W lists a multi-host test as a follow-up). The file registry no longer waits (deviation W-b), so two overlapping CLI ingests now fail fast with `WriteConflict(held)`. | Document both. A server-time `expires_at` (`$currentDate`, `now()`) would remove the skew. |
| N8 | Public-repo hygiene | `.claude/agents/parity-evaluator.md` and `qa-tester.md` (new) name the operator-local key file (path redacted in 2.0.0a2) and describe reproducing "the old service's" workload against customer data held outside the repo. They contain no secret, host or customer name, but they are private workflow, not package content. The other new paths (`~/su_mutliarm_tests/qa/...` in the QA report, VALIDATION and execution log) follow the AGENTS.md convention. | Drop the key-file path, or keep `.claude/agents/` out of the public tree. |
| N9 | HTTP actions | Action failures return `f"{type(exc).__name__}: {exc}"` to the client (pre-existing). With a2 this includes lease/backend text (`write_conflict:held: elasticsearch.lock: …`). The rest of the HTTP surface promises "no provider, source or config text reaches the client". | Return the category and reason only; keep the detail in the audit record. |
| N10 | Lexical | Own-word matching now works (`Straße`, `ﬁle`, `İstanbul`, full-width, Greek). `STRASSE` and `straße` still do not match each other (FTS5 `unicode61`). | Document this. Optional fold table for `ß`→`ss` on both index and query. |

## Per-finding verification

Legend: **fixed**: code read, slice test passes, and an independent variant also passes.
**partial**: the original reproduction passes but a variant fails.

| Finding | Status | How verified (beyond the slice's test) |
|---|---|---|
| R1 leases/fencing | fixed | Engine-level **two-process** probe on file (sqlite), PostgreSQL, SQLAlchemy-on-PG, MongoDB and Elasticsearch registries. (1) Writer A holds the lease for 6 s and writer B starts 3 s later: B gets `WriteConflict(held)` on all five, and A activates. (2) A gets SIGSTOP past a 4 s ttl, B ingests, then A gets SIGCONT: on SQL, Mongo and ES, A fails `IngestConflict(lost)` and B's generation stays CURRENT. On PG and file, A keeps its lock (advisory/flock are bound to the session or file) and B is refused `held`. The CAS kit passed on the real servers. |
| R2 incremental embeddings | fixed | Delete-only build: 0 embedded, and d5 has no vector hit. Re-adding d5: 0 embedder calls, vector reused, score 1.0 on the original chunk id. Clone under a new id with the original deleted in the same build: reused, d9 hit, no d5 hit (memory and sqlite). |
| R3 unconsumed config | fixed (but see B2) | `ontology`, `judge` and `cache_gate` are refused with "not implemented". `pipeline.arms.cache` is refused at validation. Removing `ontology` broke the index fingerprint (B2). |
| QA-01 | **partial (B1)** | String qualifier (CloudTax unbound): bounded, abstained. Lower-case "cloudtax": bounded. Wrong value (LedgerOne for CloudTax): bounded. Enum status unbound: bounded. Time ("in March 2025") unbound: bounded. Correct scope: passed, 4 exact. Streamed answer: abstained in `done`. **Integer qualifier: passed with the wrong population (B1).** |
| QA-02 | fixed for the stated cases (N4 residual) | `priority: true` → `type_mismatch`, bounded. `"3"` and `3.0` → coerced, 12 exact (documented). `created_at: "2025-01-05"` → `timezone_missing`. `"yesterday"` and `20250105` → `unparseable_bound`. Undeclared `prodcut` → `field_not_filterable`, bounded. List or dict filter values → 422 (pydantic). The unobserved value is N4. |
| QA-03 | fixed | `engine.py:691` and `decision/base.py:270` pass `structured_targets()`. The 3 quickstart questions are decided (QA test). |
| QA-04 | fixed (precision N1) | Detector probe: versions, dates, ids, ports and "Error 500" are not flagged. False positives are listed in N1. |
| QA-05 | fixed | Two processes × 15 threaded turns on one session: 30/30 turns and version 30 on sqlite, PostgreSQL, SQLAlchemy and **MongoDB** (container). The kit `check_session` CAS passed on the real servers. |
| QA-06 | fixed (N5) | Default policy: a build over `max_usd` fails `incomplete_stage` and no generation is activated. With unreported usage, 1 call is committed at the estimate before refusal. With over-reported usage, there is a one-call overshoot (N5). The per-request `max_llm_calls` is not used on the write path. |
| QA-07 | fixed (N10) | 10 Unicode variants; own-word matching works for all of them. |
| QA-08 | fixed (offline); paid evidence from slice D | Schema enum and claim-strip read in code. Not re-run with paid calls. |
| QA-09 | fixed | QA test passes; aggregates carry `predicates`. |
| QA-10 | fixed | QA test passes. Stdout upgrade does not rebase (deviation S-f, documented). |
| QA-11 | fixed | QA test passes; `_report` exit codes read in code. |
| QA-12 | fixed | Scheme is compared case-insensitively and the token in constant time (code read). |
| QA-13 | fixed (by interpretation D-1) | `LlmPlan` is strict-expressible. A native planner validates and sends `strict: true` (QA test). |
| QA-14 | fixed | Deferred keys raise `MissingSecret` (HTTP 502) on first use of that pipeline. `doctor` lists them. |
| QA-15 | fixed | `StrictSchemaError` names `oneOf`/`discriminator` (QA test). |
| QA-16 | **partial (B4)** | No services, `python -m pytest -q`: 937 passed, 124 skipped, 0 errors. Bare `pytest -q`: 2 collection errors. |

## Integration of the three slices

| Path | Contract `stripped` | Session CAS + hold | Lease / ingest budget | Notes |
|---|---|---|---|---|
| `Engine.answer` | yes (`models.py:265`) | `_begin` reserve, release on failure, `_finish` CAS | — | |
| `answer_stream` | `ContractEvent.status` includes `stripped`; tokens are always marked superseded | same, via `_begin`/`_finish` in the worker | — | Never-consumed stream: N6 |
| tools (`call_tool`) | — | `_begin`/`_finish` | — | Filter check is in `aggregate_in` (`engine.py:1325`) |
| HTTP | serialised | 429 on budget; `SessionConflict` → 422 (**B5**) | `WriteConflict` → 503; action → 200 failed (**B5**) | `MissingSecret` → 502 (acceptable) |
| MCP | serialised | generic tool error | generic tool error | |
| CLI ingest | — | — | `WriteConflict(held)`/`IngestConflict` → `error: …`, exit 1 | |
| v1 config upgrade | — | — | v1 `max_llm_calls` maps to `budgets.ingest.max_llm_calls` only when set | Upgrade tests pass. a1 **indexes** do not open (B2). |
| Schema | — | — | — | `docs/config.schema.json` and `config_v1.schema.json`: no drift |

## Runs

| Run | Result |
|---|---|
| `ruff check src tests` | All checks passed |
| `python -m pytest -q`, no services, `[all,dev]` editable venv | **937 passed, 124 skipped**, 0 failed |
| bare `pytest -q` (CI/CONTRIBUTING command), same venv | **2 collection errors** (B4) |
| `pytest -q -m external` with all six `SU_TEST_*` variables, five containers | **107 passed, 1 failed, 18 skipped** (935 deselected). Failure: B3. Skips: undeclared capabilities, paid QA-08, local LLM. |
| Image digests (resolved IDs) | opensearch 2.19.1 `sha256:72fe2fc8…`; elasticsearch 8.17.4 `sha256:413d8df6…`; pgvector 0.8.0-pg17 `sha256:40b40496…`; neo4j 5.26.0-community `sha256:5a015e53…`; mongo 7.0.16 `sha256:c630c593…` |
| `python -m build` → `~/su_mutliarm_tests/claude/review_a2/dist` | `su_multi_arm-2.0.0a1-py3-none-any.whl` + `.tar.gz` (B6: wrong version) |
| Fresh venv, wheel with CI extras `[dev,api,mcp,faiss,communities,sql,postgres,openai,anthropic]`; README quickstart | CLI ingest/search/aggregate/answer/evaluate/doctor all exit 0 (search top hits `case:certificate`, `kb:certificate`; aggregate exact; answer `passed`, generation disabled). The Python flow prints `complete` and the same hits. `--version` and the FastAPI version are `2.0.0a1`. |
| Containers | Started as `su-multi-arm-tests` and removed with `down -v`. Unrelated containers were unchanged before and after. |

## Public-repo hygiene of the diff

No credentials, private hosts, IPs, customer or origin names were found. Fixture product names
(CloudTax, LedgerOne, SearchDesk) are synthetic. Home-relative workspace paths follow the AGENTS.md
convention. One item to clean up before the push: N8 (`.claude/agents/*` key-file path and the
private parity workflow).
