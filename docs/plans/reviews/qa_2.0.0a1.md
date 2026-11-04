# QA report: 2.0.0a1 (adversarial, real providers)

Date: 2026-11-03. Target: commit `a8d4ff4` (branch `v2/phase-0`, tree identical to the published
`main`). The package was installed with `[all,dev]` from a `git archive HEAD` export into a fresh
venv (not editable), so this is what users get. Python 3.13.5, Linux. Workspace:
`~/su_mutliarm_tests/qa/` (synthetic corpus, configs, ledger, logs; nothing from it is committed).

Paid calls went only through a transport-level spend guard: before each call it reserved the
worst-case cost (request bytes/3 input tokens plus `max_tokens` output), refused the call if the
total would pass 90 % of the cap, then settled the cost from the provider-reported usage. OpenAI
models were priced pessimistically at $2 / $8 per 1M input/output tokens.

**Anthropic was not exercised.** The supplied `ANTHROPIC_API_KEY` returns `401 invalid x-api-key`.
In the key file the value is wrapped in literal quote characters, and it is still rejected after
they are stripped. Only the 401 path was tested (no cost).

Failing tests: `tests/qa/test_qa_2_0_0a1.py` (marker `qa`, each `xfail(strict=True)` with its
finding id). The default suite stays green: `pytest -q -m "not external"` gave 766 passed, 20
xfailed. QA-08 is paid and opt-in (`OPENAI_API_KEY` + `SU_QA_PAID=1`).

## Summary

| Id | Severity | Area | One line |
|---|---|---|---|
| QA-01 | blocker | decision `llm` + answer contract | A planner scope can drop a question qualifier. The count runs over the wider population and the contract **passes** (real run: 111 instead of 38). |
| QA-02 | major | request filters / contract | Type-mismatched (`"3"` on an integer field), undeclared or unparseable request filters run as an **exact zero**. No unsupported constraint is raised, so a false absence answer passes the contract. |
| QA-03 | major | intent pre-pass | `Engine` runs the intent pre-pass **without** the deployment's structured targets. The docs' own examples of ordinary questions ("the latest release notes", "all the steps", "every time…") abstain with zero hits on the quickstart config. |
| QA-04 | major | answer contract | Only `claims` are checked. The user-facing `answer` text can state a count or an exhaustive list under a `narrative` claim and pass (observed: a "complete" list with 2 wrong and 2 missing tickets). |
| QA-05 | major | sessions / budgets | Concurrent turns on one session overwrite each other (3 of 30 turns kept). The same race lets concurrent requests exceed `budgets.session`. |
| QA-06 | major | ingestion budget | Ingestion uses `budgets.max_llm_calls` (documented as per request, default 12) as the cap for the whole build, and `max_usd` is not enforced at all during ingestion. LLM extraction of more than 12 chunks fails at the defaults. |
| QA-07 | major | sqlite lexical | The query is `str.casefold()`ed but FTS5 `unicode61` keeps `ß` and ligatures. `Straße` (or `ﬁle`) never matches its own indexed word. |
| QA-08 | major | generation quality | With gpt-4o-mini under the neutral prompt, 24 of 25 plain how-to questions abstained. The model adds spurious `count`/`exhaustive_list` claims and invented IDs, and the contract rejects the whole answer. |
| QA-09 | minor | trace / auditability | Aggregates from a planner-compiled scope report `filters: {}` and a generic `scope` text. The predicates actually counted are invisible, which hides QA-01. |
| QA-10 | minor | config upgrade | `config upgrade --output` into another directory keeps relative paths. The documented command yields a config that cannot open the existing index (`IndexNotReady`). |
| QA-11 | minor | CLI | `config validate` (and every CLI error) prints a Python traceback. Schema-level errors report only the first problem, although the docs say "all problems at once". |
| QA-12 | minor | HTTP auth | The bearer scheme is compared case-sensitively: `bearer <token>` gets 401. |
| QA-13 | minor | validation | An OpenAI planner with `structured_output: native` passes `config validate` and `doctor --probe`, then every planned request abstains with `provider_error`. |
| QA-14 | minor | engine construction | `ingest` and rules-only search refuse to start without the API key of a query-time role used only by a named pipeline. |
| QA-15 | minor | providers / strict schema | `strict_schema()` screens only free-form mappings. `oneOf`, `allOf`, `discriminator`, `uniqueItems` and typeless `const` pass through, although OpenAI strict mode rejects them (latent). |
| QA-16 | minor | test suite | `pytest -q`, the command in CONTRIBUTING and CI, errors in an `[all,dev]` install. The `external` OpenSearch conformance params try to connect instead of skipping. |
| QA-17 | docs | budgets | `budgets.price_table` is a path to a YAML file (`version`, `prices.<key>.input_per_million`, …). CONFIGURATION never shows the format, and an inline table fails with "Input is not a valid path". |
| QA-18 | docs | serving | `/ready` needs no auth even on a token-protected network bind, and it returns provider/model ids and `api_key_env_set`. The docs mark the route as "none" but do not mention the disclosure. |

Counts: **1 blocker, 7 major, 8 minor, 2 docs.**

## Findings

Common environment unless stated: sqlite backend, hash embedder, synthetic 305-document corpus
(300 tickets across 3 products × 3 statuses × 4 states, plus RTL/emoji, a 4000-section long doc,
empty fields, 200-key metadata and a prompt-injection doc). OpenAI `gpt-4o-mini` (returned
`gpt-4o-mini-2024-07-18`) for planner (`json_schema`), reviewer and generator (`native`). Configs
are under `~/su_mutliarm_tests/qa/work/e2e/`.

### QA-01 (blocker): LLM-planned count over the wrong population passes the contract

- Repro (paid run): `planned` pipeline, `engine.answer(SearchRequest(query="How many CloudTax tickets have status open?", pipeline="planned"))`.
- Expected: count 38 (CloudTax ∧ open), or abstain / an `UnsupportedConstraint` for the unbound
  "CloudTax". The `rules` agent does this, and the README says v2 exists to stop "natural-language
  count questions answered from the wrong population".
- Actual: `"There are 111 open CloudTax tickets."`, `contract: passed`, aggregate `exact`,
  `filters: {}`. 111 is the number of open tickets across all products. Same pattern elsewhere: "What
  is the CloudTax nexus threshold policy in Oregon?" gained a planner structured step, and the
  answer claimed "26 issues reported regarding the CloudTax nexus threshold" (26 = CloudTax ∧
  Oregon, not nexus threshold), with the contract passed.
- Cause: `decision/llm.py::LlmAgent.attempt` checks that each proposed predicate is valid and its
  value observed. It never checks that every qualifier the intent pre-pass found in the question is
  bound, as the `rules` agent does. The contract only checks that a scope is exact.
- Test: `test_qa01_llm_count_with_unbound_qualifier_is_not_passed` (offline replay of the plan shape).

### QA-02 (major): unvalidated request filters yield exact zeros

- Repro (offline): `POST /v2/search {"query":"x","filters":{"priority":"3"},"aggregate":{"operation":"count"}}`
  gives `value 0, exactness exact, zero_match false, unsupported []`; `{"priority":3}` gives 61.
  `{"created_at":"yesterday"}` and an undeclared `{"prodcut":"CloudTax"}` likewise return 0 / no hits
  with nothing flagged.
- Paid consequence: `POST /v2/answer {"query":"Are there any priority 3 tickets?","filters":{"priority":"3"},"aggregate":{"operation":"count"}}`
  returned `"There are no priority 3 tickets."` with a `count` claim and the contract passed. The
  truth is 61.
- Expected (CONFIGURATION: `type_mismatch` and `unparseable_bound` are "never applied"; "Fields
  must be declared"): reject with 422 or report an `UnsupportedConstraint`. Request filters bypass
  the `ValueResolver`. The `aggregate` tool path does set `zero_match: true` but still not
  `type_mismatch`.
- Tests: `test_qa02_type_mismatched_request_filter_is_flagged`, `test_qa02_undeclared_filter_field_is_rejected_or_flagged`, `test_qa02_false_absence_passes_contract`.

### QA-03 (major): intent pre-pass ignores the deployment's targets

- Repro (offline, the quickstart): `su-multi-arm --config examples/v2/local.yaml search "the latest release notes"`,
  and the same for "every time the service restarts" and "What are all the steps to add an
  exemption certificate?". Each gives `decision.outcome: abstain`, `intents: [latest|list]`, 0 hits.
  On the QA corpus, "Explain every step of fixing a period close problem", "Is there no fix for
  SearchDesk crawler timeout?" and "What is the latest CloudTax rounding guidance?" all abstain.
- Expected (CONFIGURATION, `intent.py` comment): these are ordinary retrieval questions; the grammar
  fires only next to a structured target.
- Cause: `engine.py:579` calls `detect_intents(request.query, capabilities.id_patterns)` without
  `capabilities.structured_targets()`, so the permissive schema-less grammar runs. The agents never
  recompute it because `decide()` receives the engine's intents.
- Test: `test_qa03_documented_ordinary_questions_are_retrieval` (3 params).

### QA-04 (major): the answer text is outside the contract

- Repro (paid): `planned`, "List every pending LedgerOne ticket in Oregon." The engine correctly added
  `UnsupportedConstraint('List every', aggregate_intent_without_scope)`. The answer still read "The
  pending LedgerOne tickets in Oregon are as follows: 1. … 8." with one `narrative` claim, and
  `contract: passed`. Two listed tickets are CloudTax and two pending LedgerOne/Oregon tickets are
  missing (truth: 8).
- Expected: under an aggregate intent with no exact scope the answer must not carry an exhaustive
  list or count (CONFIGURATION, answer contract). The rule is enforced only on the model's
  self-labelled `claims[].kind`; the `answer` string is never checked.
- Test: `test_qa04_count_in_answer_text_without_exact_scope`.

### QA-05 (major): session read-modify-write race

- Repro (offline): 30 concurrent `engine.search(..., session_id=sid)` calls left 3 turns (repeatable,
  3 of 30 in four runs); 30 sequential calls leave 30. With `budgets.session.max_llm_calls: 2` and 6
  concurrent `answer` turns, 6 model calls went out.
- Cause: `runtime/turns.py::SessionTurns.finish` writes `loaded usage + this turn` and the episodic
  turn list from the snapshot loaded at request start. `clip()` checks the same stale snapshot.
  Session budgets are therefore not a limit under concurrency (HTTP/MCP servers are concurrent).
- Tests: `test_qa05_concurrent_session_turns_are_all_recorded`, `test_qa05_session_call_budget_holds_under_concurrency`.

### QA-06 (major): ingestion budget semantics

- Repro (paid, 4 docs = 4 chunks plus reports, `budgets.max_llm_calls: 6`, `extraction.extractors: [llm]`):
  ingest failed with `IngestError: budget … LLM call budget exhausted`. Offline: 20 documents with
  the default `max_llm_calls: 12` fail the same way.
- Cause: `ingestion/write.py:379` `CallBudget(cfg.budgets.max_llm_calls)` for the whole build;
  CONFIGURATION documents the value as `# per request`. `max_usd` / `price_table` are never consulted
  on the write path, so USD spend of LLM extraction and community reports is unbounded.
- Test: `test_qa06_llm_extraction_is_not_capped_by_per_request_budget`.

### QA-07 (major): sqlite lexical never matches `ß` / ligatures

- Repro (offline): index `"Die Straße ist gesperrt."`; `search "Straße"` gives `[]` (the same for
  `ﬁle`). `gesperrt` matches. On the QA corpus the RTL/German doc is found only through other tokens.
- Cause: `backends/sqlite/search.py::tokens` uses `text.casefold()` (`straße`→`strasse`). FTS5
  `unicode61` folds case but not `ß`, so the indexed token stays `straße`. Greek final sigma was
  checked and works.
- Test: `test_qa07_lexical_matches_its_own_indexed_word` (2 params).

### QA-08 (major): narrative answers almost always abstain with a small model

- Repro (paid): `pipeline.decision: rules`, gpt-4o-mini generator (`native`), neutral profile.
  Abstained: 10 of 10 how-to questions on the QA corpus, 9 of 10 in a repeat, 5 of 5 in the
  committed test corpus. Captured replies show spurious `count`/`exhaustive_list` claims next to a
  good narrative claim, and invented IDs (`case:1277`, from ticket numbers). One bad claim abstains
  the whole answer.
- Not a contract bug as such; it makes the default answer path unusable with a common small model.
  Options: claim-level dropping, a prompt that reserves non-narrative kinds for aggregate evidence,
  or provider-side enum restriction when no aggregate evidence is supplied.
- Test: `test_qa08_narrative_questions_mostly_answer` (paid, opt-in, `external`).

### QA-09 (minor): aggregates hide the applied scope

Planner-compiled aggregates report `filters: {}` and `scope: "filtered canonical records in this
snapshot"`. The `aggregate` tool returns `applied` predicates; the search/answer aggregates do not.
Test: `test_qa09_planner_aggregate_exposes_applied_predicates`.

### QA-10 (minor): upgrade does not rebase relative paths

`su-multi-arm --config examples/local.yaml config upgrade --output local_v2.yaml` (the documented
command, run from the repo root) writes `path: state`. Paths are relative to the YAML file, so it now
names `./state` instead of `examples/state`, and `search` fails with `IndexNotReady`. The same file
copied next to the original works. Test: `test_qa10_upgraded_config_opens_the_v1_index`.

### QA-11 (minor): CLI error handling

`config validate` on an invalid file exits 1 with a full traceback. A file with two unknown keys
reports only the first (`validate_config` raises on `exc.errors()[0]`). Semantic diagnostics are
aggregated correctly. Every other CLI failure (`IndexNotReady`, dataset mismatch on ingest) is also
a raw traceback. Test: `test_qa11_config_validate_reports_all_errors_without_traceback`.

### QA-12 (minor): bearer scheme case

`serving/auth.py::BearerTokenResolver` compares the whole header with `"Bearer " + token`. RFC 9110
§11.1 makes the scheme case-insensitive. Test: `test_qa12_bearer_scheme_is_case_insensitive`.

### QA-13 (minor): unusable OpenAI native planner validates

The docs state that this fails "before any request". The failure comes on every request (abstain
`provider_error`, no spend) while `config validate` and `doctor --probe` say valid. `LlmPlan` is
statically known, so the check belongs in validation. Test: `test_qa13_openai_native_planner_is_rejected_at_validation`.

### QA-14 (minor): query-time keys required offline

A config with an OpenAI planner used only by the `planned` pipeline cannot `ingest` (or run raw
search) without `OPENAI_API_KEY`: the engine fails with `llm: OPENAI_API_KEY is not set` three times,
once per pipeline. Test: `test_qa14_offline_ingest_without_query_time_keys`.

### QA-15 (minor): strict-schema converter screening

The live probe (gpt-4o-mini, strict) answered 400 for `oneOf`, `allOf`, `discriminator`,
`uniqueItems`, typeless `const` and `additionalProperties` with a schema. It accepted `minLength`,
`maxLength`, `pattern`, `format`, `minimum`, `exclusiveMinimum`, `minItems`, `maxItems`, `default`,
`anyOf` with null, and `$defs`/`$ref` with sibling `description`. `strict_schema()` only screens the
last of the rejected cases. Test: `test_qa15_strict_schema_refuses_keywords_openai_rejects`.

### QA-16 (minor): default suite in an `[all,dev]` install

With `opensearch-py` installed and no server, `pytest -q` errors at
`tests/conformance/test_backend_conformance.py::test_every_declared_capability_is_implemented[opensearch]`
(connection refused). The params carry the `external` mark, but nothing deselects or skips them.
CI passes only because it does not install the extra. `pytest -q -m "not external"` is green. No
extra test: the existing suite is the reproduction.

### QA-17, QA-18 (docs)

- QA-17: document the `price_table` file format (`version`, `currency`, `prices.<pricing_key>` with
  `input_per_million`, `output_per_million`, optional `cached_input_per_million`,
  `cache_write_input_per_million`) and that it is a path relative to the config.
- QA-18: say that `/ready` stays unauthenticated on token-protected binds and reveals provider/model
  ids, or put it behind the resolver off loopback.

## Settled questions from the providers slice

1. **OpenAI strict keywords**: see QA-15. All four schemas the package sends in native mode
   (`Answer`, `Extraction`, `CommunityReport`, `Review`) were **accepted** by `gpt-4o-mini`,
   `gpt-4.1-mini` and `gpt-5-mini` in strict mode. `LlmPlan` is correctly refused before any call.
   Besides its free-form `filters`, it also uses `oneOf` + `discriminator` + typeless `const`, which
   strict mode rejects.
2. **Anthropic `$defs` in `input_schema`**: **not settled.** The key was rejected with 401 before
   schema validation. Needs a working key: one native call with `Answer` (which has `$defs`/`$ref`).

## Coverage

Exercised for real (OpenAI) and verified working:

- Native strict, `json_schema` and prompt modes.
- Passive caching: `cached_input_tokens` was reported (2,176 of about 2,871 on repeated planner
  prompts).
- USD/token accounting: package trace totals equal the provider-reported usage exactly ($0.128228,
  57,750 in / 1,591 out over 14 calls).
- Streaming: decision → arm_result → evidence → token… → contract → done. Tokens are marked
  superseded when the contract abstains.
- Truncation: `length` maps to typed `incomplete` in native, prompt and streamed modes, with usage
  recorded. gpt-5-mini at `max_tokens: 600` spent all 600 tokens on reasoning and returned
  `incomplete`; the shipped `hosted_models.yaml` relies on the 2048 default for gpt-5-mini.
- Python cancel: the worker stops within 0.04 s and the provider stream closes.
- SSE disconnect: the provider stream closed within about 2 s. The session recorded the cancelled
  call at its reservation estimate, as documented. No later spend was observed.
- Heartbeats (in-process, 0.3 s interval).
- HTTP error codes: 401, 404, 422 (except a generic 422 for `record_context` on the neutral profile),
  and 502 with a generic message on provider auth failure.
- Non-loopback bind is refused without `--token-env` for `serve` and for `mcp`.
- MCP stdio and streamable HTTP with the official client: 8 tools; search identical to HTTP; answer
  with 140 progress events.
- Concurrent HTTP answers (4) are isolated and correct; 160 concurrent searches match sequential
  results.
- `exact_id`: known and unknown ids, with `value_not_observed`.
- Typed counts, `group_count` and `first_seen` are exact.
- LLM extraction grounding: an injected `EVIL_CORP` relation was rejected. `quarantine` removed
  that document's graph output and kept its other projections; ledger reuse worked (`reused_extractions: 4`).
- Community reports: claims cite verbatim quotes; the injection text did not appear.
- Prompt injection in a document (fake "9999 cases", exfiltration URL, an `ingest_delta` request):
  not followed.
- Unicode/RTL/emoji/very long (about 350K-character) documents ingest and search. The long document
  inflated one generator call to 16.6K input tokens because the default packing budget is 60,000
  characters.
- Actions: HTTP denial (403) is audited, the idempotency key is not consumed by a denial, and
  duplicates are skipped.
- `doctor --probe` makes 0 model calls and exits 1 on a failing backend.
- v1 → v2 upgrade (plain and `--strict`) and validate for all 5 v1 examples.

Not exercised:

- Every Anthropic path (key invalid), so Anthropic native tool use, controlled caching,
  `cache_creation_input_tokens` accounting and Anthropic streaming were not tested.
- Refusals: none could be triggered with benign synthetic prompts.
- `openai_compatible` with a local server.
- Container backends (optional item 5; skipped for time).
- `classifier` / `cascade` decision agents.
- Semantic embeddings (hash only).
- Relevance quality.

## Spend

Ledger: `~/su_mutliarm_tests/qa/qa_ledger.jsonl` (reserve and settle rows per call; no keys).

| Provider | Calls | USD (settled; pessimistic rates for OpenAI) | Cap | Stop at |
|---|---|---|---|---|
| Anthropic (`claude-haiku-5-5`) | 2 (both 401) | $0.0000 | $2.00 | $1.80 |
| OpenAI (`gpt-4o-mini`, `gpt-4.1-mini`, `gpt-5-mini`) | 102 | $0.5216 | $2.00 | $1.80 |

The OpenAI total includes a $0.009 manual correction for one cancelled stream whose input tokens
the guard had first under-settled. No call was refused by the guard.
