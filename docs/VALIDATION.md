## Fallback overlay into the arms — 2026-11-04 (branch `v2-logs-overlay` from `4d5a652`; A2 L5/B7)

Evidence level: **contract** (synthetic overlay rows written with the fallback slice's
`OverlayStore` on one gold day; no model called).

- `tests/contract/test_logs_overlay_arms.py` (5 tests): 23 `provisional` heads (signature from a
  learned entry), 1 `model_line` head (no signature, model-extracted endpoint/method/status/latency)
  and 1 `parsed_filled` head (model-filled exception class) on day 9. The derived snapshot carries
  them as aggregate rows and the day's history shard as `extraction_source: model_assisted` rows
  (`applied_extraction == ["model_assisted"]`); signature answers include `payload.opaque` (23) and
  `unknown` (1) and state the model-assisted share; `extraction: deterministic` excludes them;
  deterministic per-service totals and the deterministic coverage are unchanged (no head counted
  twice), the model-assisted counts and `extracted_coverage` are separate and higher; the model
  endpoint reaches the 5xx ranking and the filled exception class the exception ranking; no graph
  edge comes from a model row (no `serves`/`raises` edge, no edge for the provisional signature).
- Found and fixed: a newly sealed overlay did not change the compose identity, so compose stayed
  `unchanged`; the identity now includes the window days' snapshot identities.
- `test_derived_archive` adjusted: a snapshot still holds no line-level rows (no head ids, reply
  shas or per-line values), but a model-extracted value can now appear as an aggregate dimension,
  like a deterministic endpoint template.
- Full suite: 2359 passed, 95 skipped (not external); ruff clean.

## History tier H1, lineage H2, read latency — 2026-11-04 (branch `v2-logs-h1` from `dacd166`; A2 rev 4 §4.8, §11.R4)

Evidence level: **synthetic / contract** on sqlite, **real-service (isolated)** for the history rows
on OpenSearch 3.9.0 (own compose project `su-l5-claude`, removed afterwards; synthetic gold data).
No paid call.

- Contract (`tests/contract/test_logs_history_tier.py`, 17 tests): with the tier, the generation holds
  only window kinds (125 documents for the 10-day gold) and references one day shard per served day;
  all 11 surface-parity questions give the same rows and rendering as without the tier; imported day
  rows equal the derived snapshot (signature lines per day, unit counts), including a **format-1**
  snapshot whose partition has expired (A1 kinds only, analytics `source_unavailable`); appending a
  day writes that day's rows only; a library change that alters no document reparses (delta) and
  writes 0 rows; a change of one day's documents writes one new shard, and the previous generation
  still reads its own shard (shard filter); gc keeps every shard a registry generation references
  and removes the rest; graph links cover the history with per-day weights from the shards; a
  supersede decision yields one `signature_lineage` row and answers fold the superseded signature
  into its successor; `signature_history` holds one row per signature and library identity.
- OpenSearch (`tests/services/test_history_tier_opensearch.py`, `external`): the history rows in a
  shard-tagged OpenSearch collection answer all 11 questions exactly like the sqlite tier; graph
  weights present; gc deletes a dead shard's rows by query (0 left).
- Day N+1 append (`scripts/bench_log_tools.py --days 180 --append`, external workspace): 180 pulled
  days (90,701 history rows, generation 125 documents), then day 181 pulled and composed: **504 rows
  written, all for day 181; every older shard unchanged**; 1.6 s (sqlite) / 12.9 s (OpenSearch
  history rows); peak RSS of the whole benchmark process 663 MiB / 786 MiB.
- Read latency, same questions with fixed 10-day windows, p95 over 20 calls, one process:

  | tool | sqlite 10 d | sqlite 180 d | OpenSearch 10 d | OpenSearch 180 d |
  |---|---:|---:|---:|---:|
  | log_top | 4.3 | 9.1 | 11.4 | 22.6 |
  | log_new | 2.2 | 2.0 | 2.9 | 2.1 |
  | log_affected | 2.2 | 4.6 | 8.0 | 9.7 |
  | log_trend | 3.6 | 8.1 | 8.1 | 13.7 |
  | log_compare | 2.3 | 4.3 | 7.5 | 15.0 |
  | log_tenants | 2.0 | 2.8 | 10.4 | 5.2 |
  | log_around | 2.7 | 3.7 | 6.1 | 8.4 |
  | log_endpoints | 2.7 | 5.7 | 8.4 | 13.6 |
  | log_signature | 2.8 | 3.6 | 6.3 | 5.6 |
  | log_coverage | 11.4 | 15.6 | 48.5 | 58.4 |
  | graph_neighbourhood | 11.3 | 36.3 | 35.1 | 61.2 |

  Every log tool is far under 200 ms and the graph neighbourhood under 500 ms at 180 days (before
  H1: `log_compare` 232 ms, `log_coverage` 433 ms). Ratio 180 d / 10 d: OpenSearch 0.5–2.0x for every
  tool (within 2x); sqlite 0.9–2.25x for the log tools (`log_top` 2.1x, `log_affected` 2.1x,
  `log_trend` 2.25x, `log_endpoints` 2.1x over 2x) and 3.2x for the graph neighbourhood. The gold
  generator's error ramp (svc-api logs `d` errors on day `d`) makes the last 10 days of the 180-day
  run hold more rows than the 10-day run's window, so part of that ratio is data volume, not
  history size; not separated in this run.
- Full suite: 2289 passed, 94 skipped (not external); ruff clean.

## Logs analytics profile, log intents and tools, MCP log tools, count semantics — 2026-11-04 (A2 L5a–L5d, M1, B2+B3; base `v2/logs` `106d4b0` merged with `fdb7c4a` and `42627b0`)

Evidence level: **synthetic / contract** (fake source, sqlite backend, hash embedder, scripted
planner; no model and no real service called). The gold set is the in-repo generator
`testing/fixtures/logs/gold` (10 days x 4 tenants x 6 services, planted structure; expected answers
computed from the generator's plan, never from the engine).

- Gold questions (`tests/contract/test_logs_gold_questions.py`): 66 cases over the 10 intents plus
  incidents, all equal to the plan: per-day top services by level (18), top error signatures per
  service (16), row-count order != sum order, FATAL tenants, exception classes, new signatures per
  day (9; first seen over all served history), affected tenants (per window and per day), signature
  template and examples, per-day trends with the missing day listed `source_unavailable` (never
  zero), hourly trend on the incident day, period comparisons (3), FATAL tenants on the
  `level_field` basis and the token-only service rendered "lines containing 'error'", the planted
  incident (the only one; 3 cells, exact), endpoint latency regression (max 904 ms), 5xx (20 lines),
  per-row p95 (sum of p95 refused, `measure_unsupported`), coverage equal to a recount; the capped
  unit is `approximate` with its sample method; endpoint ranking without a service is refused.
- Surface parity (`tests/contract/test_logs_surface_parity.py`): 11 plain-language questions give the
  same rows and rendering in `rules`, `llm` and `cascade` modes (planner never called) as
  `Engine.call_tool`; an llm-selected intent (scripted reply) equals the tool call; the HTTP tool route
  and in-process MCP are byte-identical to the tool call; an MCP stdio subprocess and streamable HTTP
  mounted at `/mcp` on the HTTP app return the same structured output and list the same tools;
  `Host: attacker.example` on `/mcp` is refused; all log tools carry `readOnlyHint: true`.
- Scope guard: kind-less, negated and multi-kind scopes refused with `intent_required` on the
  request aggregate, the Python, HTTP and MCP `aggregate` tool and an llm free scope; a single-kind
  scope passes. Single-tenant refusal in config validation, `Engine`, `create_app`, `create_mcp`.
- Graph (`test_logs_graph_and_communities.py`): every header-link edge's quote names both endpoints;
  per-day weights from the day documents; `raises` gives exactly the planted exception classes;
  exemplar facts are `fact`/`trigger_in_span` for a user entry and `co_occurrence`/`unchecked` for a
  learned entry (B7); `co_fires_with` edges only when `co_firing` is admitted. Co-firing incidents
  are byte-identical under `PYTHONHASHSEED` 0, 1 and 4242 and recover the planted incident only.
- Delta reparse (`test_logs_delta_reparse.py`): 12 seeded random library changes (1–3 of 6
  mutation kinds: steal, residue parse, retire, edit, annotate, noise edit): the delta reparse equals
  the full reparse in matches, aggregates and cells, and re-matches fewer rows than the partition
  holds; a format change is a full reparse (class A). A library change composed on day 10 of the gold
  window keeps >= 95 % of the dated day documents by id and content (delta mode on every partition).
  A format-1 partition in the window is reparsed (full) before compose; the derived snapshot round
  trip carries the typed-value aggregates. The property test is seeded `random`, not `hypothesis`
  (not installed).
- Count semantics (`test_count_semantics_b2b3.py`, client path): worst-row exactness, applied basis
  and extraction, `$measure` ranking by the sum (row count order differs), bucket-of-sum per day, hour
  buckets. The Lucene native path flags `bucket.of` as unsupported (bounded) and reads the role
  fields through a terms aggregation: **not run against OpenSearch/Elasticsearch in this slice**.
- Grouped-sum answers (`test_grouped_sum_contract.py`): stated group sums, their total, the number of
  groups and the population pass; a wrong number is replaced by the deterministic rendering
  (`stripped`), or abstains when no rendering exists. Contract level with a real memory-adapter
  result; no live model.
- Read latency (`scripts/bench_log_tools.py`, sqlite client path, one process, 20 calls each,
  external workspace): 10 days (4,582 documents): log tools p95 4–28 ms, graph neighbourhood 6 ms.
  180 days (`keep_days: 180`, 90,771 documents, build 189 s): log tools p95 70–433 ms (8 of 10 within
  200 ms; `log_compare` 232 ms and `log_coverage` 433 ms are not), graph neighbourhood 122 ms
  (within 500 ms). The "within 2x of the 10-day baseline" acceptance is **not met**: the sqlite typed
  scope is linear in the generation's records; the H1 shard filter that would bound it is not
  implemented. Before the sqlite owner-index fix the 10-day `log_around` took 1.8 s.
- Full suite: 2272 passed, 94 skipped (not external); ruff clean.

## Raw tier hardening and the interim derived archive — 2026-11-04 (branch `v2-logs-raw-hardening`, base `fdb7c4a`; A2 §11.0, §11.R4)

Evidence level: **synthetic** (contract tests on a local sqlite backend). No real service, no paid call.

- Raw tier: a group- or world-writable parent is refused; a raw path under the installed package
  is refused; `retention verify` fails on a raw file with mode 0640 or 0400 and on a raw day older
  than `raw_days`; an AST import-graph test asserts that only `ingestion/raw_reparse.py` imports the
  raw store, that `schedule.py` and `learning/` never do, and that raw content is read only there
  and by `privacy scan --raw`; a redaction change re-derives exactly the days still in the raw tier
  (4-day test: days 2–3 re-derived, day 1 listed `stale_redaction`).
- Derived archive: snapshot round trip with fallback-overlay rows (identity change → rewrite,
  unchanged → no rewrite), format-1 snapshots read and marked, the `serve_days` bound (7 days,
  keep 3: 3 served, 7 archived), `max_serve_rows`, window rollups equal to the masked window's day
  rows and smaller than all served days, and the multi-identity flag after a library change.
- Compose cost over **60 synthetic days** (`derived: forever`, default `serve_days` 10, 1 tenant x
  2 services x 4 lines a day): compose 0.105 s average for days 11–20 and 0.14 s for days 51–60;
  73 documents served on day 60; every unchanged day taken from the day-document cache; 0.13 GiB
  peak RSS for the whole test process. Small synthetic days: this shows the bound (no growth with
  history), not production throughput.
- Host guard: API-only process with `allowed_hosts` behind a LAN forwarder: the forwarder's Host
  passes, another Host gets 421, a cross-origin write 403; a loopback API without a list stays
  unguarded.

## Deployment-run package fixes, B11 safe redaction, retention scopes — 2026-11-04 (branch `v2/logs`, base `106d4b0`)

Evidence level: **synthetic and mocked**, plus three real-service checks on throwaway local
containers. No paid call, no private data: every bug from the private run reports was reproduced
with synthetic names and lines.

- Unit and contract tests (synthetic): probe PIT opt-in and `disallow`, one privilege request,
  request log, index-name prefix / `before_service` / `after_env` / `unparsed: exclude`,
  continuation rules and library continuation entries, multiline-aware `patterns coverage|mine`,
  noise precedence, full lint report, record-key filters, grammar false positives (both reproduced
  first), aggregate `measure`, manifest counts, retention semantics, safe redactors (each named
  redactor including the payload classes, custom redactors, ReDoS refusal, idempotence, timeout fails
  closed), leak scan, B11 through `run-daily` (CLI against a localhost fake cluster), egress
  admission of stamped records, serving pins (a stream holds its generation while a concurrent
  retention run reports it pinned), `serve` binding, readiness and logged 503 causes, retention
  scopes over 13 synthetic days (raw 3, partitions 10, derived forever: day-1 rollups still
  queryable; derived days count variant), the raw tier (modes, purposes, reparse `--from-raw`, no
  reader module references it, scan skips raw unless `--raw`).
- Louvain determinism: the base code gave 20, 18, 19, 20 communities on one synthetic graph under four
  `PYTHONHASHSEED` values; after the fix the output of both strategies is byte-identical across
  four seeds (subprocess test).
- RE2: the redaction, B11 pipeline and package-run test files pass with google-re2 1.1.20251105 from
  the isolated copy in the external workspace (100 passed, 1 skipped: the missing-engine test).
- PostgreSQL (`pgvector:0.8.0-pg17`, throwaway container): `tests/conformance/test_postgres_conformance.py`
  25 passed, including `test_records_use_native_types` (the failure was a test-ordering bug: the kit's
  text-count check rewrote the records before the type query).
- OpenSearch 3.9.0 (throwaway container): the new end-to-end test (dataset `my_data`, prefix with
  `_`: build, activate with pointer read-back, query) fails at base `106d4b0` with the
  "foreign index" BackendError and passes after the fix; `tests/services/test_opensearch_adapter.py`
  passes (7 passed together).
- N10 compose scale (`scripts/compose_scale.py`, sqlite, external workspace, 10 days x 100 units x
  1000 signatures = 10^6 aggregate rows, 1,113,100 documents): **1096.5 s compose, 3.94 GiB peak RSS**
  (budget: 20 min, 8 GB; met). Before the change: 1118.8 s, 10.28 GiB (previous entry). At 10^5 rows:
  1.07 GiB at base, 0.92 GiB with copy-on-write redaction only.
- Not measured: redaction throughput on a real log volume, the `after_env` grammar on a real index
  inventory, derived-forever growth over months, multi-shard or security-enabled clusters.



## Day partitions, compose, retention and pins — 2026-11-04 (addendum A1 slices L3a + L3b)

Evidence level: **implemented; synthetic and contract-mocked, with the ledger retention operations
and pins also run against throwaway PostgreSQL and MongoDB containers.** No log cluster was
contacted (the source is the in-process fake cluster), no paid call was made, and no customer data
was used.

- **Suite.** `python -m pytest -m 'not external'` gives **1856 passed, 73 skipped** (base `a27ab5a`:
  1779 passed, 73 skipped). Bare `pytest -q` gives 1859 passed and 198 skipped (external tests skip
  without their services). `ruff check src tests scripts` is clean; `docs/config.schema.json` was
  regenerated with `core.schema.write_all`; the golden index fingerprints are unchanged.
- **11 synthetic days, `keep_days: 10`, `keep_generations: 2`**
  (`tests/contract/test_retention_11_days.py`, sqlite backend, 6 indices x 16+ lines per day). Every
  run succeeds and verifies. After day 11: day 1 is a tombstone; the generations of days 9 and 10
  (both list day 1) are deleted under rule (a), so the active generation is the only one left; no
  remaining catalog document holds a day-1 line hash; the day-1-tagged ledger reply, every
  unreferenced `extraction:`/`embedding:`/`pattern_library:` key and the deleted generations' job
  entries are gone; cache entries of deleted generations are gone; the day-1 session expired; run
  reports rotate to the 10 window days; audit records, manifests, reports and the tombstone hold
  no day-1 line or source id. An injected unreferenced ledger entry holding a day-1 source id makes
  `verify` fail with both the content and the reference finding.
- **run-daily** (`tests/contract/test_run_daily.py`): refused without a redactor before any request
  (the fake cluster records none); partitions hold only redacted text; source counts equal the
  generator's truth; signature window counts equal the parsed matches; a second run pulls and
  composes nothing; a crash after two of six units resumes with no duplicate record and the same
  documents as a clean run; another source query on an open partition is refused; the schedule
  lock refuses a second run while compose takes the write lease itself; a library change reparses
  the day (`v2`) before compose; the spawn parse pool gives byte-identical matches and aggregates;
  `settle` re-pulls the one grown unit; salted extraction keys of a library change go with their
  generation.
- **Timezone mapping** (`tests/unit/test_log_day_window.py`): index-date and event-time windows,
  23- and 25-hour DST days in `America/New_York`, an eastern zone, and an end-to-end event-time
  day whose unit counts equal the fake cluster's lines inside the local day, read from two UTC
  index days.
- **Pins** (`tests/unit/test_pins_and_gc.py`): TTL, release and sweep on memory, the sqlite sidecar
  and SQL; a pin written by a **separate process** blocks `gc` (`pinned_expired_generation`, day
  kept, verify fails) until it expires, then `gc` deletes and verify passes; bounded retry;
  `ProcessPins` writes once for 100 requests and drops a non-current generation at refcount 0; a
  deletion that a backend leaves incomplete keeps the day and its ledger references and fails verify;
  compose failure skips expiry.
- **Real-service (throwaway containers `pgvector/pgvector:0.8.0-pg17`, `mongo:7.0.16`, removed
  afterwards).** PostgreSQL, MongoDB and `sql[postgres]` conformance including the new ledger
  retention checks, and the PostgreSQL pin test: 69 passed. One failure,
  `test_records_use_native_types`, fails identically at base `a27ab5a` (pre-existing, unrelated).
- **Compose scale (N10, `scripts/compose_scale.py`, external workspace, sqlite, hash embedder).**
  10 days x 100 units x 1000 signatures = **10^6 aggregate rows**, 1,113,100 documents, one
  compose in **1118.8 s (18.6 min, within the 20 min budget)** with **peak RSS 10.28 GiB (above the
  8 GB budget: N10 is not met on memory)**. 10^5 rows: 117.4 s, 1.07 GiB; 10^4 rows: 27.2 s, 0.27
  GiB. The write path holds the whole corpus and its chunks in memory, and the per-document
  ledger status entries (one sqlite transaction each) dominate the wall time. Streaming or
  per-partition delta compose (addendum §4.3, deferred "only if the scale acceptance fails") is
  the follow-up. Measured on one workstation (32 cores, NVMe), external workspace, regex engine.
- **Not established.** Any real log cluster, real redaction (slice B11; the tests inject a
  synthetic e-mail/IPv4 redactor), the learning step, communities, serving-path pin wiring, and
  the pull window.

## Read-only log sources and synthetic log fixtures — 2026-11-04 (addendum A1 slices L0 + L1)

Evidence level: **implemented, synthetic and contract-mocked only.** No real Elasticsearch or
OpenSearch cluster was contacted. No container ran, no paid call was made, and no customer data was
used.

- **Suite.** `python -m pytest -m 'not external'` gives **1627 passed** (base `9ba767f`: 1189 passed).
  `ruff check src tests` is clean, `docs/config.schema.json` was regenerated with
  `core.schema.write_all`, and the wheel ships `sources/` and `testing/fixtures/logs/` (including
  the ReDoS corpus YAML).
- **Allow-list** (`tests/unit/test_source_allowlist.py`, 351 cases).
  - Every combination of seven methods and 46 paths that is not in the §3.1.1 table is refused, and
    the fake cluster records **zero** requests (303 of the 351 cases).
  - Path tricks are refused, among them percent-encoded `_all`, `..` and `?`, double encoding,
    undiscovered or hidden indices and `//`.
  - Refused bodies include scripts at any depth, `runtime_mappings`, aggregations, `from` and
    oversize pages.
  - Query parameters are allow-listed and keepalives are bounded.
  - `DELETE` closes only ids that this transport opened and that the cursor lists, and never `_all`.
  - Redirects are not followed. Credentials and the URL never appear in errors, `repr` or request
    records.
- **Self-check and capabilities** (`test_source_self_check.py`). A granted write privilege or
  `cluster:all` gives a warning, or an error under `require_read_only_role`. A privilege endpoint
  returning 403, 404 or 500 gives `unknown`. OpenSearch is checked through the account endpoint.
  A 403 on `GET /` is non-fatal. The version floor and the `_alias` fallback for releases before
  7.9 are covered.
- **Pulls** (`tests/contract/test_source_pull.py`), against the in-process fake cluster.
  - Counts equal the generator's truth for each index: `level_field` where the mapping has `level`,
    `token_match` elsewhere.
  - Scroll and `search_after` (with resume) return every document exactly once.
  - An expired scroll restarts the unit without duplicates. An expiry beyond `max_restarts` is a
    typed error.
  - Scrolls are closed on completion, when the generator is abandoned, by the exit hook, and on a
    real `SIGTERM` in a subprocess.
  - The open-context limit is refused before I/O, and context fetches are capped per partition.
  - Mapping plus multiline over a pulled day reproduces the generator's head, continuation and
    fragment counts.
- **Sampling** (`test_source_sampling.py`). Largest-remainder quotas sum to the cap. Hourly
  strata, with an `outside` stratum, sum to the unit count. A seeded pull is reproducible. The
  partition budget stops the pull and marks it `capped`.
- **Fixtures** (`test_synthetic_logs.py`). The generator's digest is pinned. The three line styles,
  noise, multiline fragments and synthetic-only PII shapes (`example.invalid`, 192.0.2.0/24) are
  covered. The ReDoS corpus is well-formed, and its nested-quantifier case grows super-linearly under
  Python `re`, a small timing check of a few milliseconds.
- **CLI.** `sources check|discover|count|probe` ran against the fake cluster served on 127.0.0.1.
  The probe report contains no log line and no URL.
- **Not established.** Behaviour against any real Elasticsearch 7.x or OpenSearch 2.x. The isolated
  container tier named in addendum §4 L1 was not run. Throughput, permission to scroll or PIT, and
  `random_score` on a real cluster are what `sources probe` is for; an operator runs it outside this
  repository. Redaction of pulled lines (slice B11) and persistence of day partitions (L3a) are not
  implemented.

Release candidate: `2.0.0a3` (slice a3/contract-providers). This release makes `on_violation: strip`
the default and native structured output the Anthropic default. It also fixes the local-model QA
findings QQ-01 to QQ-06. CHANGELOG.md lists the changes, and the "2.0.0a3 contract and providers"
entry at the end has the evidence.

- **Synthetic local suite**: `python -m pytest -m 'not external'` gives **1069 passed** (the base at
  `b99eae2` gave 1031 passed and 1 skipped). Bare `pytest -q` gives 1072 passed and 123 skipped.
  `ruff check src tests` is clean.
- **First real Anthropic run.** One small smoke under the QA spend guard: `claude-haiku-5-5` and
  `claude-sonnet-5-5`, 44 requests, **$0.047** by the guard's ledger. It covered native
  `output_config.format`, forced tool use with `$defs`, prompt-cache write and read, thinking and
  truncation, LLM extraction with grounding, a planned answer, and a streamed answer.
- **Refusal** (`stop_reason: refusal`) is still contract-mocked only. No prompt was sent to provoke
  one.
- **Not measured**: answer quality or relevance on any non-synthetic corpus, and Anthropic
  behaviour beyond these single calls.

## 2.0.0a3 fixes from the final parity run — 2026-11-04

The final private parity run found five package defects; all five are fixed. CHANGELOG.md
(2.0.0a3, Fixed) lists them.

**Evidence level: synthetic and mocked only.** No paid or real-service call was made, and the
parity run was not repeated with the fixes.

- **Suite.** `pytest -m 'not external'` went from 1189 to **1225 passed**. Bare `pytest -q` gives
  1228 passed and 124 skipped. `ruff check src tests` is clean.
- **Golden index fingerprints are unchanged.** `examples/v2/hosted_models.yaml` now declares
  `structured_output: none` for its planner; chat roles are not part of the fingerprint.
- **Structured output**, mocked HTTP (`tests/contract/test_structured_output_fallback.py`):
  - the pre-flight counts the plan schema at 37 optional and 23 union-typed properties, over the
    ceilings of 24 and 16;
  - a registry-default Sonnet 5.x planner resolves to prompt mode with a warning, and a declared
    `native` one is refused;
  - a mocked 400 "compiled grammar is too large" is `ProviderSchemaTooComplex`. It produces exactly
    one recorded switch for a registry-default role and none for a declared one. Another 400 is
    never switched.
  - Whether the real API accepts every schema under the ceilings is **not verified**. The ceilings
    are the documented limits; the real 400 was observed for the plan schema only.
- **Answer-number check.** Synthetic phrasings of a cited denominator or ratio population pass,
  modelled on the parity run's patterns ("out of", "denominator is", "as the denominator", "among",
  "over", "%", "/"). Wrong counts next to a population, and the denominator stated alone, still
  abstain (`tests/unit/test_answer_text_contract.py`).
- **Rules path.** Synthetic questions with a dropped ordinal, state word or threshold abstain with
  `qualifier_not_applied`. "X-mention" is a topic. The existing grammar cases are unchanged
  (`tests/unit/test_query_compiler.py`).
- **Sort by count.** A planner scope sorting groups by `count` or by its measure compiles to the
  ranking and executes on the memory store; the ill-defined cases are refused.
- **Precomputed verify.** Rows built from the engine template pass the default spot check, and rows
  of the bare text fail it. With `input: chunk_text` the reverse holds (hash embedder,
  `tests/unit/test_precomputed_vectors.py`).

## OpenSearch 3.x support and the Lucene result window — 2026-11-04 (branch `v2/phase-0`, base `cf242ce`)

**Evidence level: real-service (isolated local containers) plus mocked transport.** All data is the
synthetic conformance kit and a synthetic 300-document corpus. No paid call was made.

Servers (compose project `su-multi-arm-tests`, all on 127.0.0.1, security plugin disabled):

| Service | Image | Digest | Lucene | Bundled JDK |
|---|---|---|---|---|
| `opensearch` (19200) | `opensearchproject/opensearch:2.19.1` | `sha256:72fe2fc84be8295906b8efca020b46c58ed45c8da60cd9b8b49e1991e38e89a4` | 9.12.1 | 21.0.6 |
| `opensearch3` (19202, profile `opensearch3`) | `opensearchproject/opensearch:3.9.0` | `sha256:3b55f6b6249cfdb92b52ee59adc8a266203ba9ef9f5d82544fad6f1af2f3609e` | 10.5.1 | 25.0.4.1 |
| `elasticsearch` (19201) | `docker.elastic.co/elasticsearch/elasticsearch:8.17.4` | `sha256:413d8df69823e9f41f6008f025fcbaf31a5849ab9c74894731d7ded65543cd56` | — | — |

3.9.0 was the latest stable 3.x tag on Docker Hub at the time of the run.

- **OpenSearch conformance** (`pytest -m external -k opensearch tests/services/test_opensearch_adapter.py
  tests/conformance`): **20 passed, 8 skipped** on 2.19.1 (`SU_TEST_OPENSEARCH_MAJOR=2`) and
  **20 passed, 8 skipped** on 3.9.0 (`SU_TEST_OPENSEARCH_MAJOR=3`). The skips are the capabilities
  OpenSearch does not declare (graph, ledger, session, cache). This covers exact and HNSW vectors,
  the alias pointer, and the records checks.
- **Elasticsearch conformance** (`-k elasticsearch tests/conformance`): **18 passed, 8 skipped**.
  The base `94d978e` code gave 4 failed on the same server (the result-window defect below).
- **Result-window defect, found by this run.** On base `94d978e`, `test_capability[opensearch-records]`,
  `[opensearch_hnsw-records]` and `test_run_all[opensearch|opensearch_hnsw]` failed on 2.19.1 with
  HTTP 400 "Result window is too large ... [10001]", and the same 4 failed on Elasticsearch. The
  structured arm's text count pages `lexical.search` with 10000-hit pages, and the look-ahead hit
  made `from + size` 10001. Fixed in the shared `OpenSearchLexical.search`, with a mocked
  regression test (`test_lexical_paging_never_crosses_the_result_window`).
- **Cross-major comparison** (workspace script, synthetic 300 documents, 5 lexical queries plus 1
  scoped, 5 vector queries, exact and HNSW): lexical rankings were identical on 6 of 6 queries. Every
  2.19.1 score was **exactly 2.2 ×** the 3.9.0 score over 143 hits, which is `k1 + 1` (3.0 replaced
  `LegacyBM25Similarity` with `BM25Similarity`). Exact and HNSW+rescore vector results were
  identical across majors (max |score difference| 0), and HNSW+rescore equalled exact on 5 of 5
  queries on both.
- **nmslib.** On 3.9.0, creating a `knn_vector` index with `engine: nmslib` returned
  `mapper_parsing_exception` ("cannot be used for new index creation in OpenSearch from 3.0.0"). The
  same request succeeded on 2.19.1. The adapter writes `lucene`. The probe guard for a blocked engine
  is unit-tested only.
- **3.x changes checked and not affecting the adapter.** These come from the 3.0.0 release notes and
  the k-NN plugin notes for 3.0–3.9:
  - The index-level `knn.algo_param.*` and `knn.space_type` settings and `knn.plugin.enabled` were
    removed. The adapter sets only `index.knn` and the per-field `method`.
  - `_bulk batch_size` was removed. The adapter does not use it.
  - Keyword `term` queries no longer score by default. The adapter uses keyword terms only in filter
    context.
  - Derived source for vectors is on by default for `index.knn: true` indices. The chunk updates and
    the painless re-denormalisation passed on 3.9.0.
  - Painless `cosineSimilarity` over `knn_vector` doc values (the exact path) is unchanged.
  - JDK 21 is the minimum. The image bundles it.
  - The security demo configuration needs `OPENSEARCH_INITIAL_ADMIN_PASSWORD`, which is not needed
    with `DISABLE_SECURITY_PLUGIN` and `DISABLE_INSTALL_DEMO_CONFIG`.
- **Suite.** `pytest -m 'not external'` gives **1237 passed**. The base `cf242ce` gave 1224 passed
  and 1 failed: a hygiene test on an execution-log line, fixed here. Bare `pytest -q` with the 2.19.1
  and Elasticsearch containers up gives **1278 passed, 86 skipped**. `ruff check src tests` is clean.
- **Not covered.** These were not tested:
  - OpenSearch 3.0–3.8 and 2.x other than 2.19.1;
  - an in-place 2.x → 3.x upgrade of an existing index (expected to keep working, with BM25 scores
    rescaled);
  - the multi-backend engine test (`tests/services/test_multi_backend_engine.py`), which needs
    PostgreSQL and Neo4j, against 3.x;
  - security-enabled clusters.

## 2.0.0a2 status — 2026-11-04 (historical)

Release candidate: `2.0.0a2` (branch `v2/phase-0`). This release fixes the 2.0.0a1 QA findings
(QA-01 to QA-16), the 2.0.0a1 completeness-review risks R1 to R3 and the 2.0.0a2 release-review
blockers B1 to B6. [CHANGELOG.md](https://github.com/Taranjeet-SU/su_multiarm/blob/main/CHANGELOG.md)
lists the changes. The scope of Phase 0 and Phase 1 is unchanged from 2.0.0a1.

Evidence by level. The "2.0.0a2 release evidence" entry at the end has the detail:

- **Synthetic local suite**: `python -m pytest -m 'not external'`, **1018 passed**, plus the bare
  `pytest -q` that CI runs in a fresh venv with the CI extras: 1007 passed, 131 skipped, 0 errors.
- **Real-service conformance**: one consolidated `-m external` run on the five disposable containers
  gave **108 passed, 18 skipped, 0 failed**. Image digests are recorded.
- **2.0.0a1 index compatibility**: an index built by the published 2.0.0a1 tree opens and serves
  search under 2.0.0a2. A golden test pins the 2.0.0a1 fingerprint of every shipped example.
- **Paid providers**: OpenAI (`gpt-4o-mini`) was exercised for real in the 2.0.0a2 QA-08 rerun
  (slice D entry). **Anthropic has still not completed a real call.** The 2.0.0a2 smoke got HTTP 429
  on its first request and spent $0.00.
- **Local-model provider smoke**: `tests/services/test_local_llm.py` passed against a small local
  model (2026-11-03). It was not re-run for 2.0.0a2.
- **Not measured**: relevance or answer quality on any non-synthetic corpus, Anthropic behaviour,
  scaling, and contention between writers on different hosts.

The sections that follow, starting with the 0.1.0a1 alpha report, are kept as dated history; their
version numbers and test counts describe the code at that date, not this release.

## Vector import, source order and source slots — 2026-11-04 (branch `parity/review-fix`)

Scope: synthetic unit tests only (`tests/unit/test_precomputed_vectors.py`); no customer data, no paid
calls. Covered: import matched by key with the provider never called; missing-row error vs
`on_missing: embed`; audit refusals (dimension, count, duplicate key, non-finite, zero row, vectors
digest, keys digest) with nothing activated; relocating the files keeps the index fingerprint and
recipe while a changed digest changes both, and no path appears in the recipe or manifest; manifest
records both digests, positional pairing and the imported input; a permuted keys file passes the file
audit (documented as undetectable without `verify`) and fails the spot check, while the correct
pairing passes it; FTS re-index with duplicate ids keeps one row per chunk; vector upsert replace path
keeps ids and rows aligned; source slots never displace a pinned exact-id hit and are traced; v1
downgrade refuses `source_order`/`source_slots`. The 2.0.0a1 golden index fingerprints are unchanged.
An aggregate-only production-workload parity summary is kept outside this repository; it is not a
claim of this release.

## 0.1.0a1 alpha report — 2026-10-11 (historical)

Release: `0.1.0a1`, Python 3.13.5 on Linux. Exact installed package versions are in
[validation_environment.json](validation_environment.json). This report distinguishes actual local
and service tests from HTTP contract fixtures. No customer corpus or paid LLM request was used.

### Automated suite (0.1.0a1)

**40 tests passed**, including 3 real external-database tests, when their test environment variables
were supplied. The normal suite skips those 3 tests when disposable database endpoints are absent.
Ruff lint and formatting checks pass. The GitHub Actions matrix for Python 3.11/3.12/3.13 is supplied;
it has not been run on GitHub because the repository has not been published. Only Python 3.13.5 was
executed locally in this session.

Test coverage includes:

- Dense, lexical, structured count/date/grouping, graph, community, provenance and canonical dedup.
- Complete character coverage across field-aware chunks, real source titles/URLs, mapping and redaction.
- Upserts/deletes remove stale active text, graph facts and community memberships; extraction reuse;
  failed ingestion preserves the prior generation; incompatible recipes are rejected.
- Filters and seed-record exclusions; valid/invalid dates and missing counts; SQL field allowlists;
  planner filters cannot override request scope.
- All fusion options preserve the configured union; explicit final caps; Faiss/NumPy rank/score parity.
- Combined NER/POS/tag and LLM extraction keeps semantic relation endpoint types distinct.
- Separate context and original question; capability validation; no pointless agentic `missing=none`
  retry; bounded two-round follow-up with a cited answer.
- KB Details/resolution inclusion; bad generator citation IDs rejected; bad extraction evidence rejected.
- Community summary citations validated, budget omissions reported, fabricated references abort ingestion.
- Call-budget hard stops; model response truncation failures; embedding response order/count validation;
  OpenAI-compatible and Anthropic HTTP request/usage contracts using mocked responses.
- API auth, context-header/body parity and conflicts, unchanged no-context requests; MCP tool calls and
  actual stdio client/server initialization/tool invocation across a subprocess.
- Evaluation does not treat unlabelled queries as relevance failures.

### Actual backend and model checks

| Component | Evidence level | Result |
|---|---|---|
| SQLite catalog/FTS/aggregates/local graph | Real local storage/query tests | Passed |
| NumPy hash demo vectors | Real local computation | Passed; intentionally nonsemantic |
| Faiss exact cosine | Real library/index + NumPy parity/filter tests | Passed |
| PostgreSQL FTS + pgvector | Real disposable PostgreSQL 17 container | Passed build/query/filter/delete |
| Neo4j | Real disposable Neo4j 5 container | Passed mentions, explicit two-hop FACT traversal and excluded-bridge behavior |
| Louvain | Real NetworkX seeded clustering | Passed membership/retrieval |
| sentence-transformers | Real all-MiniLM-L6-v2 CPU inference | Passed embedding/ingestion/retrieval smoke |
| GLiNER | Real gliner_small-v2.1 CPU inference | Passed grounded person/organization/location extraction |
| spaCy | Real en_core_web_sm CPU inference | Passed NER and configured POS extraction |
| Anthropic / OpenAI-compatible chat | Mocked HTTP contract, schema/usage/truncation tests | Passed; no paid model-account validation |
| Remote embedding API | Mocked HTTP shape/order/error checks | Passed; no paid provider validation |
| Optional cross-encoder reranker | Implemented, no real-weight smoke in this session | Not certified on a particular model |
| LLM community summaries | Grounding/budget tests with deterministic model fixtures | Passed; quality needs a real-model held-out evaluation |
| FastAPI | TestClient and clean-wheel actual localhost server | Passed |
| MCP | Actual stdio client/server protocol | Passed |

Test images used:

- `pgvector/pgvector:pg17`, digest `sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d`.
- `neo4j:5-community`, digest `sha256:c7d25c0eeebfe125718d58b72ed5d663c85d0479047733845eb2237c67ce8069`.

These were separate containers on loopback ports 25432 and 27687. No production database was altered.
The test credentials in the test instructions are disposable examples, not production secrets.

### Synthetic semantic smoke

The six-document, three-question example retrieved all six labelled target documents within the top
five, with MRR 1.0. Median search time in that small CPU run was approximately **7.8 ms**, excluding
initial model loading/indexing. See [semantic_smoke.json](semantic_smoke.json). These figures are a
functional smoke test on a tiny synthetic corpus; they say nothing about relevance on any production corpus,
large-corpus latency, planner quality, or answer correctness. Reporting “100% relevance” from this
exercise would be misleading.

The [GLiNER smoke](gliner_smoke.json) extracted Alice/person, Acme/organization and California/location
from a synthetic sentence in about 5.9 seconds including model loading. That is not warm throughput
or a corpus-level NER accuracy measurement. The [spaCy smoke](spacy_smoke.json) also passed NER/POS extraction on a synthetic sentence.
Models ran on CPU; no GPU inference was used.

### Distribution checks

Wheel and source distribution build successfully. A fresh virtual environment without system site
packages installs the wheel plus API/MCP extras, passes `pip check`, and runs CLI ingestion, retrieval,
aggregates, deletion, an authenticated localhost API, and an MCP subprocess. The script is
`tests/smoke_wheel.py`. Archives were inspected for runtime indexes, virtual environments and `.env`
files; none were included. LICENSE and NOTICE are included in both distributions.

### Remaining release limits

- No customer relevance/answer benchmark or production traffic migration; no parity claim.
- No paid Sonnet/Haiku evaluation, extraction predicate factuality score, or community-summary quality score.
- No ANN/scaling/concurrency soak benchmark, stream ingestion, automatic external cleanup or rollback CLI.
- No per-user authorization platform, broad PII compliance guarantee, global request deadline or dollar cap.
- No independently verified answer judge, DSPy optimizer, hierarchical community/global map-reduce pipeline,
  GLiNER2 adapter, third-party plugin registry or existing-vector importer.

The implementation status and next steps are tracked in PLAN.md; [ADOPTING.md](ADOPTING.md) describes evaluating the package on your own corpus.

## Public-data local-model diagnostics — 2026-10-12

These checks used unmodified runtime source at `36d2394`, isolated working directories under the
owner's external test workspace, public data with recorded provenance, local model inference, and
CPU sentence-transformer embeddings. Dataset files, qrels, models, indexes, raw responses and
private endpoint configuration remain outside this repository. No paid pipeline inference,
service restart, model eviction or deployment was performed.

Codex and an actual Claude CLI collaborator proposed ten datasets and agreed to shortlist HotpotQA,
HybridQA, SciFact, QASPER and WikiTableQuestions. Only the first two were executed. The main pipeline
model was the installed `qwen2.5:14b-instruct`; embeddings were `all-MiniLM-L6-v2`, revision
`1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, on CPU. Six separate extraction probes used the already-loaded
`qwen3.8:27b`; these were not a second complete model evaluation.

**Neither original full-corpus semantic-extraction/cited-report pipeline completed successfully.**

| Dataset and frozen diagnostic scope | Original pipeline outcome | Explicitly separate follow-on checks |
|---|---|---|
| HotpotQA: 8 development questions, 72 pooled supplied context documents including distractors, 197 chunks | Strict ingestion failed on invalid extraction JSON. Per-document preflight accepted 1 document and rejected 71 at their first failing chunk. | Full-corpus source-title entity diagnostic with reports disabled after report-grounding failure; one grounded relation fragment; all retrieval arms, supported typed aggregates, raw/planned/agentic answering and independent community-report attempts. |
| HybridQA: 10 development questions, 11 released tables including 2 distractors, 313 table/row/passage records, 807 chunks | Strict ingestion failed on relation grounding. Independent all-chunk checks accepted 405 and rejected 402; 72 whole documents passed and 241 did not. | Full-corpus spaCy/report-disabled diagnostic; accepted-document LLM-extraction diagnostic with reports disabled after another grounding failure. The latter persisted 34 relation occurrences and 9 communities. All arms and answer modes were attempted. |

The follow-on configurations do not substitute for a successful original pipeline. A real local
provider transport is exercised here; this is stronger evidence than the earlier mocked chat
contracts, but it does not certify every local model, commercial provider or storage backend.

Independent peer checks reproduced the dataset mappings, selected gold references, retrieval
results, official-form answer scoring and structured counts. All 33 numeric aggregate probes in the
full HybridQA diagnostic matched expectations computed directly from raw tables; three source-date
probes returned null because the source strings do not satisfy the timestamp contract. The reduced
snapshot loses rows and therefore does not preserve full-table answers. Natural-language count probes yielded 0/9 correct answers in both HybridQA diagnostics despite
working explicit aggregates. Planner-inferred nonexistent table IDs produced confident zero counts,
and an unsupported numeric-total request was misrepresented using a record count. These are semantic
planning/scope failures, not incorrect count arithmetic.

In the full-corpus HotpotQA entity-only diagnostic, raw answer calls passed schema/citation-ID checks
on 8/8 questions; official answer exact match was 1/8 and F1 was approximately 0.252. Both reviewers
judged 5/8 answers semantically correct. These are different measures: verbose correct answers can
fail exact-form scoring, and valid citation IDs do not establish answer correctness. Planned and
agentic answer calls succeeded on 3/8 and 2/8 respectively. No sentence-support/joint HotpotQA score
or population accuracy claim is made.

Independent attempts on nine HotpotQA title-entity communities yielded four reports passing
reference validation, four rejected for grounding and one rejected for invalid JSON. The four
accepted reports contained eight claims and twelve exact source references. Reference validity
alone did not establish complete semantic support for every claim. The initial community-probe
harness used an incorrect database-column key and made no model calls; its corrected second version
is the source of these results. That initial harness error is not a package failure.

Separate HybridQA report attempts accepted 7/9 reports with 17 claims; one failed grounding and one
exhausted the completion cap. Independent review verified all 34 quoted references in the accepted
reports. These reports were not installed into an active snapshot and are not a semantic-quality score.

Graph review verified exact quote/endpoint containment for all 34 persisted HybridQA relation
occurrences and demonstrated one- and two-hop retrieval of additional documents in the reduced
snapshot. It also found an unsupported factual predicate extracted from a title, repeated across
five stored occurrences of one cached row-title extraction. The one-hop new-document example
depends on that unsupported edge; an independently verified two-hop example uses supported row-text
predicates. Thus successful traversal and verbatim quotes must not be represented as graph factual
precision or natural-question relevance. Repeated title extraction is also an efficiency and
evidence-duplication concern.

Peer review identified a measurement error in the reduced HybridQA harness: intersecting gold with
the surviving snapshot changes the evaluation denominator. Its conditional traced-node R@5 of 0.5
uses only 3 labelled questions and 4 surviving gold records. With all 10 original questions and
26 original gold records retained, macro R@5 is 0.125 and macro full-pool recall is approximately
0.1833. The corrected original-gold values are primary; traced-node labels themselves are weak.

Remaining blockers include reliable extraction/model recipes, explicit failure recovery and
coverage reporting, schema-constrained provider output, executable planner schemas, semantic
predicate/claim support, complete answer citations, generic context fields and typed structured
relationships. Strict grounding should remain; silent fallback, claim dropping or corpus reduction
would hide these failures. The six larger-model probes passed three titles and failed three body
chunks, including two completion-cap stops; they do not rank model quality.

Scope limits: 18 selected development questions, bounded corpus pools, source-derived aggregate and
graph/community probes, differing retrieval/prompt/budget configurations, no full-dataset or
leaderboard claim, and no scaling or production measurements. HotpotQA terms/provenance were
recorded from its [project](https://hotpotqa.github.io/) and pinned distribution. HybridQA's
[project](https://hybridqa.github.io/) states dataset CC BY 4.0; its separately hosted linked-table
repository has no standalone license, so the upstream Wikipedia/HybridQA terms caveat remains in
the external provenance record. This report contains aggregate findings only.

Regression checks for this session: **37 passed, 3 external service checks deselected**; Ruff check
of `src` and `tests` passed. All test temporary/cache paths were external and repository bytecode
writes were disabled. Runtime/config contracts and release artifacts were not changed, so this
session did not repeat the earlier wheel/sdist release validation.

Both the Claude execution owner and peer reviewer accepted the denominator, predicate-support,
answer-format and causal-interpretation corrections. Codex incorporated their amendments in the
external joint gap list and acceptance criteria. All original failures and review disagreements or
qualifications remain recorded. The original full-pipeline readiness conclusion is unchanged.

## Span-anchored grounding and predicate support — 2026-11-03 (branch `v2/phase-0`)

First Phase 0 slice of `docs/plans/v2_implementation_plan.md`, addressing the two ingestion failures
measured in the 2026-10-12 public-data diagnostics: exact-substring quote matching aborting builds on
case/whitespace drift, and relation predicates never being validated.

Implemented: `grounding.py` (NFKC/casefold/whitespace normalisation with a per-character offset map;
`locate()` returns verbatim source spans at `exact` or `normalised` level; ambiguous matches rejected
by default; matches never begin or end inside an expanded character group), `predicate_support.py`
(stemmed trigger check with negation window; unsupported predicates stored as `co_occurrence`, never
as facts), span-anchored `GroundedExtraction` output, catalog columns for offsets/match level/edge
kind/support, `index_format` 3 (previous snapshots and extraction caches are not reused), graph
traversal restricted to `fact` edges.

Evidence level: **synthetic unit and engine tests only.** `pytest -m 'not external'`: 64 passed,
3 deselected (previously 40). Fixture categories: exact, case-only, whitespace-only, NBSP, ligature,
full-width Latin, combining marks (decomposed and composed), sharp-s, ambiguous reject/first,
no-match, expanded-group boundary, 500 random offset-map property cases; predicate support on
camel/underscore predicates, declared triggers, three negation shapes; engine tests reproduce the
public-data shapes synthetically (drifted quote grounds at `normalised` level with verbatim span; the
title-only `hosted` relation across five records becomes five `co_occurrence` rows and zero facts;
the graph arm does not traverse them). `ruff check src tests` passed.

Not demonstrated: any change in acceptance rate on real corpora. That requires the owner-authorised
re-run of the frozen public cohorts (plan open decision 16). Relocation to another field of the same
record (`relocated` level), fact dedupe across inherited title chunks, and the quarantine/partial
ingestion policies are not yet implemented.

## Real-service test infrastructure — 2026-11-03

Phase 1 preparation (plan §6.1 real-service tier, §6.2 `services` job): `tests/services/docker-compose.yml`,
`tests/services/wait_ready.py`, `tests/services/README.md` and `.github/workflows/services.yml`.

Evidence level: **infrastructure verification only.** This entry shows that the throwaway stack
starts, reports healthy, answers protocol-level probes on non-default loopback ports and tears down
cleanly on one Linux host (Docker 29.0.4, Compose v2.40.3). No V2 adapter exists yet, so no backend
conformance test ran against OpenSearch, Elasticsearch or MongoDB; the three pre-existing
`external` tests (v1 PostgreSQL/pgvector and Neo4j adapters, `tests/test_external.py`) were run against
the stack as a smoke check of the wiring only. The GitHub Actions job has not been executed because the
repository is not published.

| Image (pinned tag) | Resolved digest |
|---|---|
| `opensearchproject/opensearch:2.19.1` | `sha256:72fe2fc84be8295906b8efca020b46c58ed45c8da60cd9b8b49e1991e38e89a4` |
| `docker.elastic.co/elasticsearch/elasticsearch:8.17.4` | `sha256:413d8df69823e9f41f6008f025fcbaf31a5849ab9c74894731d7ded65543cd56` |
| `pgvector/pgvector:0.8.0-pg17` | `sha256:40b404964359299eefdd5f8518facf1886c562848cf4de13b6eaf91cb70c2b87` |
| `neo4j:5.26.0-community` | `sha256:5a015e53de1895e7eee1574ae0325cf8c4b89587222778108c594bdd45a474b5` |
| `mongo:7.0.16` | `sha256:c630c59342c1493d50345136df2af14a76b9e827dd5316bfabee07a0880a5f3a` |

Measured on the cold start (all five images pulled, about 3.6 GB on disk):

- `up -d --wait --wait-timeout 420` took 17 min 26 s wall clock, of which about 17 min 15 s was the
  image pull on this host's network; all five containers were created at 06:13:48 UTC and reported
  healthy within 11 s of creation (Mongo and PostgreSQL first, Neo4j last).
- `wait_ready.py` run 16 s after creation: every service ready on the first probe (0.0–0.1 s).

| service | endpoint | status | after | detail |
|---|---|---|---|---|
| elasticsearch | 127.0.0.1:19201 | ready | 0.0 s | cluster green |
| mongo | 127.0.0.1:27018 | ready | 0.1 s | ping ok |
| neo4j | 127.0.0.1:17687 | ready | 0.1 s | bolt 5.4 |
| opensearch | 127.0.0.1:19200 | ready | 0.0 s | cluster green |
| postgres | 127.0.0.1:15432 | ready | 0.0 s | accepting connections |

- Resident memory while idle and healthy: OpenSearch 975 MiB and Elasticsearch 998 MiB (512 MiB JVM
  heap each, 1.5 GiB container cap), Neo4j 827 MiB, MongoDB 376 MiB, PostgreSQL 67 MiB. Neo4j ran this
  check under a 1 GiB cap; the committed file raises it to 1.5 GiB because 827 MiB idle leaves too little
  headroom for test load. That raised value has not itself been re-measured.
- `pytest -q -m external` with `SU_TEST_POSTGRES_DSN`, `SU_TEST_NEO4J_URI`, `SU_TEST_NEO4J_PASSWORD`
  pointed at the stack: 3 passed, 75 deselected, 1.4 s. Run from the working tree with `PYTHONPATH=src`
  (the package is not installed in the interpreter used) while another agent's uncommitted Phase 0
  changes were present under `src/`; the three external tests themselves were unchanged.
- `down -v` completed in 10 s. `docker ps` before and after was identical (four unrelated containers
  on this host were never stopped); zero containers, networks or volumes labelled with the compose
  project remained. Because PostgreSQL, Neo4j and MongoDB data directories are tmpfs, no anonymous
  volume was created in the first place.
- Not changed on the host: `vm.max_map_count` stayed at 65530; both Lucene engines accept this in
  `discovery.type=single-node` mode. Nothing was exposed beyond loopback.
- Nothing failed. The only deviation from the plan's §6.2 image list is that tags are pinned to specific
  versions instead of major-version floats, and MySQL is omitted because it is not a first-release backend.

Not demonstrated: any adapter behaviour on OpenSearch, Elasticsearch or MongoDB; the `services` CI job
on GitHub; behaviour under the test load of the Phase 1 conformance suite; behaviour on a runner with
less memory than this host (the stack needs about 3.3 GiB resident idle, about 5.5 GiB of caps).

## Kernel: types, metadata schema, component/registry, config v2, backend contracts, runtime — 2026-11-03 (branch `v2/phase-0`, slice P0/S2)

Implemented (plan §2.3 layout, §3.1, §3.1a, §3.2, §3.3, §3.4, §3.5, §5): `core/{types,component,
registry,config,schema,errors}.py`, `compat/config_v1.py` (v1 model moved here; `upgrade`,
`downgrade`, `upgrade_text`), `backends/{contracts,capabilities,memory}.py`, `runtime/{context,
session,memory,tools,actions,budget,trace,acl}.py`, CLI `config upgrade|validate|schema`,
regenerated `docs/config.schema.json` (now v2) and new `docs/config_v1.schema.json`.

Evidence level: **synthetic unit tests and the in-memory fake only; no real service, no model
call.** `pytest -m 'not external'`: 154 passed, 2 skipped (`mcp` package absent in the interpreter
used for this run; both are the existing MCP tests), 3 deselected — previously 62 passed / 2 skipped
in the same interpreter (64 passed when `mcp` is installed). The 92 new tests cover: span/source
equality, containment and edge-kind invariants; metadata type/nullability/enum/timezone violations by
category and filterable/listable/groupable gating; registry registration, diagnostics, unknown
plugin, non-conforming plugin, pickle/dotted-path and sha256-mismatch rejection; config v2 invariants
(20 rejection cases with key paths, native structured output, plugin name rules, classifier artifact
rules, code references anywhere in the file); every `examples/*.yaml` upgrading deterministically
(byte-identical output, `downgrade(upgrade(v1))` equal to the v1 model modulo `arms.enabled` order
and the same v1 `index_fingerprint()` — N6), schema drift for both files, the CLI subcommands; the
fake adapter satisfying all eight protocols, matrix consistency against the config `kind` union,
generation scoping, paging, phrase/boolean/scope pushdown, refresh visibility, exact vector search
with namespaces, typed scope execution (count, count_distinct, sum, first_seen, bucket, group_by,
half-open date intervals, timezone-missing bounds reported as unsupported, zero-match flag, ACL
denominators), listable-only value enumeration, fact-only traversal with provenance, cache-key prefix
and TTL, ledger sequence/raw replies, session expiry; budget exhaustion for calls/tokens/USD (price
table required, failed call releases its reservations), deadline, trace masking of query/passages/
unsupported-constraint text/follow-up queries with a raw opt-in, principal-scoped sessions (403-class
error for another principal), `generation_changed` on the trace, episodic re-validation, audited
idempotent actions with surface gating, tool registry schemas. `ruff check src tests` passed.

Not demonstrated: any engine behaviour on the new contracts (arms, write path, sqlite adapter are
the next slices); real adapters against the frozen contracts; plugin loading from an installed
distribution (entry points are exercised with a monkeypatched entry-point list); any production
outcome.

### Kernel review fixes — 2026-11-03 (branch `v2/p0-s2-fix`, slice P0/S2-fix)

Addresses `docs/plans/reviews/p0_s2_kernel_review.md` (verdict REVISE). B1: the code-reference
scanner now requires a dotted module part for `module:attr` factories, so tagged model ids
(`mistral:latest`, `llama3:instruct`) and bare artifact names (`intent.onnx`) are accepted while
`pkg.module:Class`, `a.b.c:Klass` and pickle suffixes stay rejected. B2: the capability matrix carries
a status per `(adapter, capability)`; only pairs v1 code serves are non-`planned` (`sqlite` all
eight as `legacy` pending S3, `postgres` vector + lexical, `neo4j` graph without memberships); config
validation rejects `planned` pairs (`storage.catalog|ledger|sessions: pg`) and `community`/
`storage.communities` on a graph backend without memberships (`neo4j`). B3: plan rev 3 amends §3.1a,
§3.5 and §5 to the frozen code; `upgrade()` reports `lossy_upgrade` diagnostics for v1 settings v2
cannot carry and `config upgrade --strict` refuses them. Also fixed: page cap aligned with
`candidates` (over-limit is a `BackendError`), `SessionNotFound` typed error, the fake honours
`unknown_fields: reject` and gates non-filterable/undeclared predicates (`field_not_filterable`),
datetime `range` bounds report `unparseable_bound`/`timezone_missing`, unknown vector ids are a
`BackendError`, the vacuous USD `pricing_key` config check replaced by `ChatRole.price_key()`,
trace masking adds `highlights`, `title`, `snippet`, `content`, `question`.

Evidence level: **synthetic unit tests and the in-memory fake only.** Interpreter: repository
`.venv` (Python 3.13.5, `mcp` installed). `pytest -q -m 'not external'`: **169 passed, 3 deselected**
(S2 in the same interpreter: 156 passed, 3 deselected; the earlier 154/2 figure was the anaconda
interpreter without `mcp`). `ruff check src tests` passed. Schema files unchanged (no `Config` field
changed; drift check green).


## Decision agent, typed scope, answer contract, review stage, selection split — 2026-11-03 (branch `v2/phase-0`, slice P0/S3)

Implemented (plan §3.7, §3.7.1, §3.7.2, §3.7.3, §3.9, §3.9a, §3.9b): `decision/{intent,base,
values,scope,rules,classifier,llm,cascade}.py`, `selection/{types,collapse,limits,union,ordering,
diversity,pins,packing}.py`, `generation/contract.py`, `pipeline/review.py`,
`core/config_decision.py` (`stage_policies`), `core/config_selection.py` (ordering kinds, `packing`),
regenerated `docs/config.schema.json`.

Evidence level: **synthetic unit, contract and parity tests against the in-memory fake and
monkeypatched model calls only; no real service, no model call, no production outcome.**
`pytest -m 'not external'`: 300 passed, 3 deselected (previously 156 passed). The 144 new tests
cover: every row of the resolution table (enum, observed-value, `exists`, cap-0 id fields, caller
filters applied as given with `zero_match`, numeric/boolean parse, range bounds incl. offset policy,
half-open `date_interval` on datetime and date fields executed on the fake, `missing`, non-filterable,
group/sort flags, every measure rule with the "never substituted by count" check); every row of the
fail-policy table (pre-pass never fails; rules/classifier/llm/cascade standalone and as cascade
stages with default and configured outcomes; `raw_all_arms` carries one flag per aggregate intent
and `decision_fallback: true`); classifier artifact rules (pickle, `.joblib`, dotted path,
`module:attr`, sha256 mismatch, missing file, missing/invalid runtime, label not an arm, default off);
the LLM planner with a monkeypatched `JsonModel.call` (observed values only for listable fields under
the cap, compiled scope executed exactly on the fake, nine failure categories, total-as-count refused,
budget reserve/commit and trace event); the answer contract (count/exhaustive-list/absence against
exact or exhaustive `ScopeResult`s, engine-injected constraints, generator cannot set them, violation
handling both ways); the selection stages (membership preserved under all seven orderings, explicit
`max_documents` cut, collapse/limits/merge semantics, diversity with short-group fallback, pins,
packing fairness, min-per-record omission, configured field priority beating `details`); exact
parity of `reciprocal_rank`/`per_arm`/`rrf`/`minmax`/`zscore` with `fusion.fuse` on fixed inputs
with and without weights and caps; the review stage with scripted reviewers (both v1 agentic test
intents, all six stop reasons, re-plan through the rules agent, `raw_all_arms` re-plan refused, one
vote per record per arm across hops, masked follow-up queries, default `JsonModelReviewer`); and
the nine NL-count probe shapes from the HybridQA diagnostic reproduced as synthetic requests through
the rules agent and a `rules → llm` cascade whose planner is scripted to fabricate (invented
identifier, unobserved value, total-as-count, non-filterable qualifier, offset-less calendar bound,
unbound topic, absence of an unobserved value, earliest without a time basis, non-existent table):
every shape abstains under `fail_closed` and yields a raw plan with non-empty unsupported
constraints under `raw_all_arms`, and the contract refuses a scripted count answer in both modes
(zero numbers in the returned answer). Exactly resolvable shapes still produce exact counts, sums,
first-seen and lists on the fake. `ruff check src tests` passed.

Not demonstrated: any of this wired into the engine (the arms/engine slice consumes
`DecisionAgent.decide`, `selection.select`, `AnswerContract.enforce` and `ReviewStage.run`); a real
planner or reviewer model; a real classifier runtime (none ships); intent grammar coverage beyond the
synthetic English questions in the tests; retrieval quality of any ordering (parity, not relevance,
was checked).

## Storage and arms on the contracts, write path with policies, neutral profiles — 2026-11-03 (branch `v2/phase-0`, slice P0/S4)

Implemented (plan §3.5 sqlite row, §3.8, §3.12 relocated level + dedupe, §3.13 as needed, §3.14,
§3.14.1, Phase 0 rows "sqlite adapter on contracts", "Five arms as Components", "Ingestion write
path on contracts + per-projection validation", "Neutral prompts + profiles + voice option"):
`backends/sqlite/` (eight facets), `backends/_scope.py`, `backends/extras.py`,
`testing/backend_conformance.py` + `tests/conformance/`, `ingestion/{write,policy,release}.py`,
`extraction/` package (v2 `BuiltinExtractor`, `relocated` grounding, `dedupe.py`), `communities/`,
`arms/` (five components), `engine.py` on config v2 with `Engine(config, components=[...])`,
`profiles/` (`neutral`, `support_example`, neutral prompts, voices), `core/request.py`.

Evidence level: **synthetic unit, contract and conformance tests only; no real service, no model
call, no production outcome.** `pytest -m 'not external'`: 229 passed, 3 deselected (the external
postgres/neo4j tests are additionally skip-marked: those adapters are not on the v2 contracts yet),
previously 156 passed. `ruff check src tests` passed. `tests/smoke_wheel.py` (CLI ingest/search/
answer/serve/MCP over a v1 JSON config upgraded in memory) passed in the same interpreter.

What the tests show:
- Conformance kit v0 (7 capability checks + cache + protocol check) passes identically on the memory
  fake, sqlite (numpy) and sqlite (faiss): scope pushdown with no out-of-scope hit, determinism,
  phrase semantics under the `phrase` flag, stable non-overlapping pagination, generation isolation,
  refresh visibility, catalog reads bounded by requested ids, typed scope (count, count_distinct,
  sum, first_seen, bucket, group_by, half-open date intervals, naive bounds reported as unsupported,
  ACL denominators, listable-only values), fact-only traversal with provenance, cache-key prefix and
  TTL, ledger ordering and raw replies, session expiry.
- Write path (memory and sqlite): every projection validated before activation; a failed embedding
  stage or a projection count mismatch keeps the old generation active, removes the staged generation
  and records a failure manifest; metadata violations fail closed by category; delete/update rebuild
  with ledger reuse; fingerprint drift refused unless `replace` + `rebuild`.
- Policies: `strict` aborts on the first rejected unit (raw reply still kept); `quarantine` keeps the
  document in catalog/lexical/dense/structured, excludes all of its extraction outputs from graph and
  communities and records `coverage.extraction` by category; `partial` refuses activation without the
  flag, marks the stopped stage incomplete with processed/remaining/first unprocessed unit, replays
  earlier successes from the ledger, and arms depending on that stage refuse unless
  `pipeline.allow_incomplete_arms` (results then flagged `incomplete`).
- S1 follow-ups: five rows inheriting the same title collapse to one `co_occurrence` edge with five
  occurrences (never traversed); a quote found in another field of the same record grounds at the
  `relocated` level with verbatim field offsets; a quote found nowhere still fails closed.
- Arms: each arm reproduces the alpha's `test_core.py` behaviours on the v2 engine (filters and
  exclusions applied inside every arm before selection, document dedup, grounded traversal, community
  membership flagged as non-causal, typed aggregates with exactness and denominator); the 30 alpha
  tests are replaced by `tests/contract/test_engine_support_example.py` under the `support_example`
  profile (same edge cases; `test_relative_config_paths` is covered by `test_config_v2.py`).
- Domain decoupling: support, research-note and collection-record corpora run through one neutral
  engine; custom fields are chunked, retrieved and packed; custom context keys reach planner and
  generator and unknown keys are rejected; built-in prompts contain no domain-authority vocabulary;
  renaming source labels leaves hits, scores and arm pools identical; a field named `details` never
  outranks a configured field; `generation.voice` appends a register instruction only when set.

Not demonstrated: ranking quality or relevance (tiny synthetic fixtures); postgres/neo4j on the
contracts; real provider calls; the Phase 0 external evidence re-run over frozen cohorts (owner
decision pending); any production outcome. Lexical ranking parity across engines is not claimed
(FTS5 bm25 vs the fake's overlap score differ by design).

## Engine integration: harness end to end — 2026-11-03 (branch `v2/phase-0`, slice P0/S5)

Implemented (plan §2.2 request path, §3.7–§3.9b wiring, §3.15 parity, Phase 0 acceptance):
`engine.py` now runs `RunContext` → intent pre-pass → decision agent (`rules` | `classifier` |
`llm` | `cascade`, fail policy from config) → arms as components → `selection.select` →
`selection.pack` → optional `ReviewStage` → generation → `AnswerContract` with engine-populated
`unsupported_constraints` and `coverage`. The v1 planner/fusion/agentic code paths and the typed
stubs for `best_arm_rank`/`stable_interleave` are gone (`fusion.fuse` stays as the parity oracle).
`models.AnswerResult`/`SearchResult` gained additive fields; `mcp.py` exposes typed filters,
`exclude_ids` and the typed aggregate with a start-up parity check (`serving/parity.py`); HTTP adds
`/v2/search` and `/v2/answer`.

Evidence level: **synthetic unit, contract and parity tests only; no real service, no real model
call, no production outcome.** `pytest -m 'not external'`: 429 passed, 3 deselected (previously
386). `ruff check src tests` passed. CLI smoke in the external workspace over `examples/local.yaml`
(v1 file upgraded in memory): `doctor`, `ingest`, `search --request examples/aggregate.json`
(first_seen aggregate next to retrieval hits), `answer "how many cases are there"` (abstains with
`cases: field_not_filterable`; no number).

What the tests show (`tests/contract/test_engine_harness.py`, `tests/parity/test_request_parity.py`,
ported `tests/contract/test_engine_support_example.py` and `test_domains.py`):
- The nine NL-count probe shapes through `Engine.answer`, for `rules` and `llm` (fabricating
  planner reply) under `fail_closed` and `raw_all_arms`, with a generator fake that would state "7
  matching records" as a count claim: every answer is `insufficient_evidence`, contains no digit,
  carries non-empty `unsupported_constraints`, and no aggregate was executed. Under `fail_closed`
  no arm runs and no model is called; under `raw_all_arms` the raw plan runs on the non-structured
  arms and the contract abstains with `claim_without_exact_scope` + `aggregate_intent_without_scope`.
  Prose numerals are not screened (Phase 2 `exact_set` guard), so this evidence covers claims the
  generator declares, not free text.
- An exactly resolvable count ("How many records have status open?") still counts through the
  engine with `exactness: exact` and the count claim passes; an explicit typed `group_count`
  aggregate runs next to retrieval.
- Review through the engine: a scripted reviewer injected with `Engine(config, components=[...])`
  re-plans a follow-up through the `rules` agent with the request filters unchanged, merges new
  hits through selection (`sufficient` after one hop); a follow-up that is an unbound count stops
  the loop with `replan_abstained`. The two v1 agentic tests run on the integrated engine with the
  `llm` agent (`no_actionable_gap`, `hop_limit`, follow-up hits merged, citations checked).
- `ordering: reciprocal_rank` reproduces the alpha's `per_arm` order and scores (including source
  weights) on the shipped `examples/local.yaml` for five queries; every ordering kind preserves the
  union and `max_documents` is the only cut.
- Packing: `fair_share` truncates every record to its share with none omitted and none under
  `min_per_record`; `greedy` starves the tail; `selection.packing.field_priority` beats
  `generation.evidence_fields`, which beats the document's own order.
- `AnswerResult.coverage` is `None` for a strict complete generation and carries policy, document
  counts, incomplete stages and the manifest note under `partial`.
- Parity: the same request (typed filters, `exclude_ids`, record context, typed aggregate) through
  the Python API, `/search`, `/v2/search` and the MCP `search` tool returns the same hit order and
  aggregate; both MCP tool schemas match `SearchRequest` field-for-field, and the checker detects the
  alpha's `dict[str, str]` filters and a dropped `exclude_ids`.
- `support_example` fixtures: outcome unchanged (same hits, aggregates, evidence order, prompts); a
  generator that cites unsupplied evidence now abstains with `unknown_evidence` recorded (or raises
  under `on_violation: fail`) instead of a bare `ValueError`.

Not demonstrated: ranking quality; the classifier agent through the engine (interface only, no
runtime); `cascade` through the engine (unit-tested in `test_count_probes.py`); streaming;
`capabilities`/`list_values`/`lookup_record` MCP tools; real providers; any production outcome.

## MongoDB adapter on the contracts — 2026-11-03 (Phase 1, branch `worktree-agent-abd12a2ec9a5ab040`)

Implemented (plan §3.5 `mongodb` row, Phase 1 "mongodb adapter"): `backends/mongodb/` with the
`catalog`, `lexical`, `records`, `cache`, `ledger` and `session` facets, a generation registry with
an atomic pointer document, predicate pushdown, a `mongodb` pip extra and the engine wiring for
`kind: mongodb`. `graph` is not declared; `vector` stays `planned` (Atlas-only; see below).

Evidence level: **real-service conformance against a disposable community-edition MongoDB container,
synthetic fixtures only; no model call, no production outcome.**

- Service: `mongo:7.0.16` (`sha256:c630c59342c1493d50345136df2af14a76b9e827dd5316bfabee07a0880a5f3a`),
  `mongod` reports `db version v7.0.16`; started alone from `tests/services/docker-compose.yml`
  (`up -d --wait mongo`, healthy; `wait_ready.py --only mongo`: ready on the first probe), pymongo
  4.19.0, the repository's `.venv` interpreter with `PYTHONPATH=src`.
- `pytest -q -m external -k mongo`: **16 passed** (`tests/conformance/test_mongodb_conformance.py`),
  1.3 s. The kit's `check_catalog`, `check_lexical`, `check_records`, `check_ledger`,
  `check_session`, `check_cache` and `run_all` all passed unchanged against the server: request-ordered
  gets bounded by the requested ids, stable non-overlapping pagination, scope pushdown on
  `iter_scope` and on lexical search (predicates reach the server through a `$lookup` into the
  generation's documents), phrase never matching non-contiguous tokens, `must`/`must_not`, refresh
  visibility, generation isolation and typed errors for unknown generations, typed scope results
  identical to sqlite/memory (count, casefold eq, ACL denominators, zero-match flag, first_seen,
  group_by, sum, count_distinct, month buckets, half-open date intervals, naive bounds reported as
  `timezone_missing`, missing, range, negated in, listable-only values), cache-key prefix and TTL
  expiry, ledger ordering and raw replies, session expiry. Additional checks: generation-scoped
  collection names and drop-on-delete, `values` cardinality cap failing closed, TTL indexes present,
  registry pointer switched only by `activate` with a recorded failure leaving it untouched, write
  lease refused while held, `from_config` reading the URI from the environment, an unreachable server
  surfacing as `BackendError(mongodb, generation)`.
- Unit (no server): 12 tests in `tests/unit/test_mongodb_adapter.py` for query compilation, text
  search strings, naming, paging and driver-error mapping through a thin fake client (the fake is
  evidence of error mapping only). `pytest -q -m 'not external'`: 398 passed (386 before).
- Teardown: only the `mongo` service was stopped and removed (`docker compose ... rm -sf mongo`);
  another agent's OpenSearch container of the same compose project was left running; `docker ps`
  before and after otherwise identical.

Known limits, stated rather than claimed away:
- MongoDB text scores are term-frequency `textScore` values, not BM25; the conformance kit checks
  membership, scope, determinism and paging, never score parity. Phrase matching is MongoDB's
  case-insensitive contiguous match on the raw text, which is at least as strict as token adjacency.
- Vector search is **not** implemented or tested: it exists only on MongoDB Atlas, which this
  repository cannot exercise; the capability stays `planned` and configuration naming it is rejected.
- Measures and groups are computed over the matched rows with the shared `_scope` helpers (the rows
  are fetched anyway for `matched_ids`); only predicates, counts, `exists` and `values` run server-side.
- The engine wiring for `kind: mongodb` is constructed but no end-to-end engine run on MongoDB was
  performed in this slice; the write path on an external catalog is the multi-backend write-path slice.

## S3/S4 review fixes — 2026-11-03 (branch `v2/p0-review-fix`, base `9f26b88`)

Addresses `docs/plans/reviews/p0_s3_s4_review.md` (verdict REVISE). B1: temporal comparisons are
instant-based on both adapters (`backends/_scope.py::instant/temporal_key`; sqlite stores declared
and inferred temporal values as UTC instants with a sortable canonical text; `assume_utc` naive
values and bounds read as UTC; a `date` field compares calendar dates and truncates datetime bounds
to their UTC date; the resolver accepts a datetime bound on a date field by the same rule); the
conformance kit now carries an `assume_utc` field with naive stored values and a `date` field with
datetime bounds and asserts identical `ScopeResult`s on memory, sqlite and sqlite+faiss. B2:
`source` is an identity key (bypasses the schema while undeclared); `unknown_fields: reject` ingests
a declared schema and rejects a genuinely undeclared key as `metadata.unknown_field`. B3: documents
a stopped stage never reached are `extraction: not_processed:<stage>` with `graph: incomplete`,
listed in `coverage.stages.<stage>.documents_not_processed`, never `accepted`; the previous test
that codified `accepted` is corrected. B4: the intent grammar fires `total`/`list`/`absence`/
`earliest`/`latest` only next to a structured target (canonical unit, generic record words, declared
field names, `time_basis` labels) supplied by the decision agent from `Capabilities`; twelve plain
questions containing those words are retrieval decisions and the nine HybridQA probe shapes still
abstain or flag. Non-blocking: community arm checks entity/report overlap before any catalog paging
(N1); `PathEdge.occurrences` surfaces deduplicated occurrences (N2); `review.enabled` with
`max_hops: 1` is a configuration error (N4); sqlite `date_buckets: true`, `filter_pushdown: false`
(N5); the new generation is protected and the previous one released on activation (N6); the
rejected-document chunk counter no longer double counts (N7); a vendor product name removed from
the example profile (N9); the two execution-log inaccuracies corrected (N10).

Evidence level: **synthetic unit, contract and conformance tests only.** `pytest -q -m 'not
external'` with `PYTHONPATH=src`: **418 passed, 3 deselected** (386 before; +32); `tests/conformance`
31 passed for memory, sqlite, sqlite_faiss; `ruff check src tests` clean; schema files unchanged
(drift check green). Not demonstrated: N3/N8/N11/N12 (engine-wiring and contract-surface items left
to the integration slice), any real service or model call.
## OpenSearch adapter on the frozen contracts — 2026-11-03 (branch `worktree-agent-a1c794b75ac6aff4c`, slice P1/opensearch)

Implemented (plan §3.5 `opensearch` row, Phase 1 "opensearch adapter" row, §6.1 real-service tier):
`backends/opensearch/` (`adapter.py`, `transport.py`, `catalog.py`, `search.py`),
`backends/_lucene_common.py`, matrix rows (`catalog`, `lexical`, `vector`, `records` = `contracts`),
engine wiring for `kind: opensearch`, pyproject extra `opensearch`, services workflow install step,
`tests/unit/test_opensearch_backend.py`, `tests/conformance/` parametrised over `opensearch` and
`opensearch_hnsw` (`external`), `tests/services/test_opensearch_adapter.py`.

Evidence level: **real-service conformance on one containerised OpenSearch, plus fake-transport unit
tests; no model call, no production outcome, no relevance claim.**

| Item | Value |
|---|---|
| Image (pinned tag) | `opensearchproject/opensearch:2.19.1` |
| Resolved digest | `sha256:72fe2fc84be8295906b8efca020b46c58ed45c8da60cd9b8b49e1991e38e89a4` |
| Server as reported by `GET /` | distribution `opensearch`, version `2.19.1`, Lucene `9.12.1`, security plugin disabled, single node, 512 MiB heap |
| Client | `opensearch-py` 3.2.0 (extra pins `>=2.8,<4`), Python 3.13.5 (`.venv`), Docker 29.0.4, Compose v2.40.3 |
| Stack | `tests/services/docker-compose.yml`, service `opensearch` only, host port 19200 (loopback); another agent's `mongo` service of the same compose project was running, so only the `opensearch` service was started and removed by this slice |

Results (interpreter `.venv`, `-o pythonpath=src`, pytest temp under `~/su_mutliarm_tests/claude/`):

| Suite | Command | Result | Evidence |
|---|---|---|---|
| Conformance kit on the real service | `pytest -q -m external tests/conformance -k opensearch` | **12 passed, 8 skipped, 0 failed** (2 adapter parametrisations × {protocols, catalog, lexical, vector, records, run_all}; the 8 skips are graph/cache/ledger/session, which the adapter does not declare) | real service |
| Adapter real-service checks | `pytest -q -m external tests/services/test_opensearch_adapter.py` | **6 passed**: server identity; generation-named indices, alias activation atomic, a staged second generation never touches the alias, failed-build deletion leaves the alias, delete refused for the served generation, old generation stays until deleted; mapping-version mismatch / foreign index / knn-method mismatch are typed errors; highlights and field filter; unreachable server is `server unavailable`; HNSW + exact rescore returns the same ids and scores (5 d.p.) as exact search on 60 random 8-d vectors under a document allow-list; records scan cap (`max_matched`) is a typed error | real service |
| Fake-transport unit tests | `pytest -q tests/unit/test_opensearch_backend.py` | **14 passed**: construction rules and `from_config` env handling; index naming and dataset refusal; typed field layout; every predicate op's DSL incl. casefold, negate, numeric/date/string ranges, half-open intervals; scope pushdown shapes; lexical templates never parse syntax; bulk shapes for documents, chunks and vectors; bulk item failure typed; vector validation; HNSW opt-in request sequence; lexical paging; error mapping (no reply, 404 generation, 400 refused, mapping mismatch, foreign index); index creation mapping; probe; atomic alias request and delete guards; records requests | synthetic |
| Whole non-external suite | `pytest -q -m 'not external'` | **401 passed, 29 deselected** (previously 386 passed, 3 deselected in the same interpreter; +14 unit, +1 config case; the 26 extra deselections are the new `external` conformance parametrisations) | synthetic |
| `ruff check src tests` | | passed | — |
| `docs/config.schema.json` | regenerated from `core.config.Config` | no drift (`Config` unchanged; `LuceneBackend` already existed) | — |

Observed engine facts the adapter relies on (probed by hand against the same container before
implementation and then by the suites): `cosineSimilarity` in a `script_score` works on
`knn_vector` fields without `index.knn` (exact mode builds no graph); the `knn` query with a
`filter` on a Lucene-engine HNSW field honours the filter (its raw score is `(1 + cos) / 2`, which is
why ANN scores are never returned without the exact rescore); a `terms` aggregation ordered by `_key`
on a one-shard index returns exact, ordered keys; `search_after` on the `id` keyword gives stable
pages; a missing index is `404 index_not_found_exception`.

Not demonstrated: behaviour on Elasticsearch (separate adapter, not written); the `services` GitHub
job (repository not published); the multi-backend write path calling `activate(gen)` (the engine's
release step still activates through the file registry only; the alias switch is exercised by the
adapter tests, not by `ingest`); any lexical ranking parity with FTS5 or PostgreSQL (none is claimed;
the cross-engine agreement report of plan §3.5 is not produced yet); HNSW recall on anything larger
than the 60-vector fixture; the plan's `cardinality`/`date_histogram` aggregations (deliberately not
used, see the execution log).


## Model providers and capability registry — 2026-11-03 (Phase 1, slice P1/providers)

`providers/` package: `ChatProvider` protocol with `anthropic` (Messages API; native structured output
as one forced tool; `cache_control` on system and tool blocks), `openai` (Chat Completions; native =
strict `json_schema`; passive caching read from `prompt_tokens_details.cached_tokens`),
`openai_compatible` (request hints only as declared) and `none`; embeddings `hash`,
`sentence_transformers`, `openai`, `openai_compatible`; per-`(provider, model-pattern)` capability
registry consulted by `ChatRole` validation and again at provider construction; normalised usage
(input incl. cached and cache-write tokens, output, reasoning) committed to `Budget` (calls, tokens,
USD via the price table) and emitted as masked `model_call` trace events.

What the tests show (all with `httpx.MockTransport`, no network): request shapes per provider and
mode (tool definition + forced `tool_choice`, `cache_control` placement, strict schema conversion,
`json_object`/`json_schema` hints only where declared, output-token parameter names,
`reasoning_effort` only on declared models, no `Authorization` header for a keyless local server);
usage parsing incl. Anthropic cache read/write and OpenAI cached tokens; 401/403/429/5xx/529/400,
timeouts, connection errors and non-JSON bodies mapped to typed errors with exactly one request (no
retry) and no response body or key in the message; truncation, refusal and a native reply without
its tool call are failures; schema-invalid replies are `SchemaViolation` with the raw reply; a
strict-incompatible schema and a missing key fail before any request or reservation; budget
exhaustion (calls, output ceiling) blocks the request; USD accounting with cached/cache-write rates;
planner failures map to `timeout`/`schema_violation`/`provider_error`; config rejects every
undeclared `native`/`json_mode`/`json_schema`/`controlled`/`reasoning_effort` pair with a key path.

Evidence level: **contract-mocked only for Anthropic and OpenAI** — the wire formats are taken from
the providers' published API documentation and were not exercised against the paid services (no
paid call authorised, assumed defaults #3 and #14); in particular, which JSON-schema keywords
OpenAI strict mode accepts for our schemas, and Anthropic's handling of `$defs` in `input_schema`,
are unverified. The local OpenAI-compatible smoke (`tests/services/test_local_llm.py`, `external`)
was **not run** in this session (`SU_TEST_LOCAL_LLM_URL` unset; no model server was started).
`pytest -q -m 'not external'` with `PYTHONPATH=src`: **558 passed, 20 deselected** (473 before;
+85: `tests/contract/test_providers.py`, `tests/unit/test_provider_capabilities.py`); ruff clean;
`docs/config.schema.json` regenerated.

## Elasticsearch adapter on the frozen contracts — 2026-11-03 (branch `worktree-agent-adc476aefbf7e9366`, slice P1/elasticsearch)

Implemented (plan §3.5 `elasticsearch` row, Phase 1 row "elasticsearch adapter"):
`backends/elasticsearch/` built on the OpenSearch adapter classes over `backends/_lucene_common.py`
(the OpenSearch classes were made engine-parametrised: typed errors carry `self.kind`, and four
override hooks exist for the transport, the index body, the vector field mapping/score script and the
ANN candidate query). Elasticsearch-specific: official-client transport (`transport.py`), `probe`
(8.x only; OpenSearch and other majors refused), `dense_vector` fields (exact `index: false` +
`script_score` cosine by default; float `hnsw` opt-in with candidates from the top-level `knn`
search and the same exact rescore), no `index.knn` setting, and an alias-backed
`generation_registry()` (`registry.py`: manifests, failure records and a create-only ingestion lock
in `<prefix>__registry`; `activate` is the adapter's atomic `_aliases` switch; `current()` resolves
the `docs` alias). Engine dispatch for `kind: elasticsearch`, pip extra `elasticsearch`, capability
matrix rows `contracts` for `catalog`/`lexical`/`vector`/`records`. Shared-module change: the typed
`t.*` fields and temporal predicates now use `_scope.temporal_key` (naive stamps and date-only strings
are UTC instants, `date` fields compare calendar dates, temporal `eq` is instant equality), which the
kit's `assume_utc`/`date` checks require; it applies to OpenSearch too.

Evidence level: **real-service conformance on one throwaway container plus synthetic unit tests
with a fake transport; no model call, no production outcome, no load or recall measurement.**

Real service: `docker.elastic.co/elasticsearch/elasticsearch:8.17.4`
(`sha256:413d8df69823e9f41f6008f025fcbaf31a5849ab9c74894731d7ded65543cd56`, the digest recorded by
the infrastructure entry above), started service-scoped (`up -d --wait elasticsearch`) from
`tests/services/docker-compose.yml` on Docker 29.0.4 / Compose v2.40.3, client `elasticsearch`
8.19.3, server version `8.17.4` confirmed by `probe`. Teardown stopped and removed only that
container; `docker ps` otherwise matched the pre-run state (the OpenSearch and MongoDB containers of
the same compose project were not running during this run). No `su_test_*` index remained.

`pytest -q -m external -k elasticsearch` (worktree source on `PYTHONPATH`, after the merge of
`v2/phase-0` at 74b8595): **16 passed, 8 skipped** in 3.7 s. Through `tests/conformance/conftest.py`
the kit runs over `elasticsearch` and `elasticsearch_hnsw`: protocol check (declared set ==
implemented set == `{catalog, lexical, vector, records}`), the four kit checks and `run_all` for
each (the 8 skips are the graph/cache/ledger/session checks the adapter does not declare, skipped
explicitly). `tests/conformance/test_elasticsearch_conformance.py` adds the server probe, generation
isolation + listing + delete, the registry alias switch with a failed build that writes its manifest
and failure record and is removed without the alias moving (and the alias target refusing deletion,
and the lock being exclusive across registry objects), and exact-vector parity with the memory fake
(same membership, order and cosine scores within 1e-5 on a seeded 4×8 fixture with an exclusion
scope). The kit's records check covers typed scope incl. casefold `eq`, ACL denominators, zero-match,
`first_seen`, `group_by`, `sum`, `count_distinct`, monthly `bucket`, half-open `date_interval`, naive
bounds reported `timezone_missing`, `assume_utc` instants across offsets, `date`-field truncation and
unparseable bounds, `missing`, `range`, negated `in`, `exists`, listable-only `values`; the lexical,
vector and catalog checks cover scope pushdown with no out-of-scope hit, determinism, phrase
contiguity under the `standard` analyzer, boolean `must`/`must_not`, stable non-overlapping
pagination, generation isolation, `refresh` visibility, bounded catalog reads, exact cosine order,
namespace isolation and the dimension/duplicate-id rejections.

Unit (`tests/unit/test_elasticsearch_backend.py`, fake transport, no server): 10 tests on the
Elasticsearch-specific parts (probe gate for 7.x/9.x/OpenSearch, `from_config` and the registry's
dataset requirement, index body without `index.knn`, `dense_vector` exact/HNSW mappings and the
mapping-mismatch error, exact cosine script with the field literal, `knn` candidates then exact
rescore, every typed error naming `elasticsearch`, the client transport's `ApiError`/connection
mapping and ndjson content type, the atomic alias actions with a failed build never issuing
`_aliases`, lock conflict and stale-lock takeover). `pytest -q -m 'not external'`: 498 passed on the
merged tree (`tests/unit/test_opensearch_backend.py` included, 84 passed across the four touched unit
modules), `ruff check src tests` clean, `docs/config.schema.json` unchanged by this slice.

Not demonstrated: the OpenSearch external suite after the shared-module temporal-key change (its
container was not running; its fake-transport unit tests pass); HNSW recall on anything larger than
the kit fixture; behaviour under ingestion load; multi-shard indices; Elasticsearch 9.x; secured or
TLS clusters; the `services` CI job on GitHub; an end-to-end `ingest` through `Engine` on this
backend (the engine now constructs it, but the write path's `activate` through
`generation_registry()` is exercised by the adapter tests, not by `ingest`). The lexical cross-engine
agreement report (plan §3.5) has not been produced.


## Neo4j graph adapter on the contracts — 2026-11-03 (Phase 1, branch `worktree-agent-abe51c291233d8741`)

Scope: `backends/neo4j/` (graph facet with memberships and communities, generation pointer node),
graph cases added to the conformance kit, engine wiring for `kind: neo4j`, `BaseArm.graph_scope`
(predicate resolution for document-id-only graph backends), write-path pointer activation/rollback;
v1 `adapters/neo4j.py` and its two v1 external tests removed.

Evidence level: **real-service** for the adapter and the engine's graph slot, against
`neo4j:5.26.0-community` (`sha256:5a015e53de1895e7eee1574ae0325cf8c4b89587222778108c594bdd45a474b5`,
server agent `Neo4j/5.26.0`, Bolt 5.8, Python driver 6.3.1) started alone from
`tests/services/docker-compose.yml` (`up -d --wait neo4j`, removed afterwards with `rm -sf neo4j`;
other containers of the project left running). Synthetic fixtures only.

- Conformance (`tests/conformance/test_neo4j_conformance.py`, 10 `external` tests, all passed):
  `check_graph` (now shared with memory, sqlite and sqlite+faiss, which also pass it): one- and
  two-hop `fact` traversal; co-occurrence never traversed by default (also at three hops) and
  traversed only when requested; edge kind, support, status, span offsets/text/match level, full
  provenance and every occurrence surfaced unchanged; direction, per-hop predicate filter, limit,
  unknown seed; hop bound `0` empty and `4, -1, "2", 2.0, True` refused with a typed error;
  eligibility as allow-list, deny-list and empty allow-list on edges, mentions and seed lookup;
  scope predicates refused (neo4j) or honoured (sqlite/memory), never ignored; memberships, mention
  attributes (canonical id, aliases), communities round-trip; dangling endpoint rejected with the
  graph unchanged; unknown generation typed. Plus: `MERGE` idempotency (re-load leaves paths,
  mentions and memberships identical), a rejected call rolls back entities written in the same call,
  generation isolation, pointer refused for an unloaded generation and left unchanged, pointer and
  `protect` both block `delete_generation`, namespace isolation, unreachable server and wrong
  password as `BackendError(neo4j, …)`.
- Engine (`tests/services/test_neo4j_engine.py`, 3 `external` tests, all passed; replaces the v1
  `test_neo4j_real` / `test_neo4j_explicit_multihop_relations`): graph and community arms on neo4j
  return the same documents and provenance as the sqlite graph facet for three queries, and for
  `source: case` filters (predicates resolved through the catalog); deletions visible after
  re-ingest with the pointer following the new generation; two-hop fact traversal over a bridge
  document and its exclusion cutting the path; a failing graph load and a failing `CURRENT` switch
  both leave the previous generation active and the pointer on it, with the failed load cleaned up.
- Non-external: `tests/unit/test_neo4j_adapter.py` (17, fake driver: only the fixed templates ever
  run and hostile values travel as parameters, fact template cannot match co-occurrence, hop/
  predicate refusals before any statement, constraint/unavailable/auth errors typed with rollback,
  activation/delete refusals, config) and `tests/contract/test_graph_backend_seams.py` (7, memory
  fakes: predicate resolution parity with native pushdown for graph and community arms, cap → typed
  error, pointer moves only on successful activation and is moved back or removed on failure).
  `pytest -q -m 'not external'`: **496 passed** (473 before at `783d821`); `ruff check src tests`
  clean; `docs/config.schema.json` regenerated (`backends.*.namespace` for neo4j).

Known limits, stated rather than claimed away:
- Neo4j evaluates eligibility only as document-id lists. With request filters or ACL predicates the
  arms page the catalog for the eligible ids first, so catalog reads for that step are bounded by the
  eligible set (cap 10000, then a typed error), not by the hits (principle 2.1.7 holds only for
  unfiltered requests on this backend).
- One write transaction per `upsert` call: atomic, but the whole generation's graph must fit the
  server's transaction memory. Very large loads would need a staged multi-transaction load behind the
  existing `state = loaded` marker; not implemented.
- Re-loading an identical mention/relation/membership updates it in place on neo4j, while sqlite and
  memory append a duplicate relation row; the conformance kit therefore does not assert duplicate
  behaviour.
- Ranking/relevance is not measured here; parity with sqlite on a tiny synthetic corpus is evidence of
  identical semantics, not of retrieval quality. No vector index on neo4j (Phase 5).

## PostgreSQL and generic SQL adapters on the contracts — 2026-11-03 (Phase 1, branch `worktree-agent-ac47e981ef8e1495f`, base `783d821`)

Implemented (plan §3.5 `postgres` and `sqlalchemy` rows, Phase 1): `backends/postgres/` (catalog,
FTS lexical, pgvector exact with opt-in HNSW behind a filter-recall gate, typed records with native
`double precision`/`timestamptz` value columns, cache, ledger with raw replies, sessions; one schema
per generation; registry with a `CURRENT` pointer row and an advisory write lock; graph `planned`)
and `backends/sql/` (config kind `sqlalchemy`; SQLAlchemy Core; catalog, records, exact cache,
ledger, sessions; lexical/vector `planned`); engine wiring for both kinds; pip extra `sql`.

Evidence level: **real-service conformance against a disposable PostgreSQL + pgvector container and
a real SQLite database, synthetic fixtures only; no model call, no production outcome.**

- Service: `pgvector/pgvector:0.8.0-pg17` (`sha256:40b404964359299eefdd5f8518facf1886c562848cf4de13b6eaf91cb70c2b87`),
  server `PostgreSQL 17.6`, extension `vector` 0.8.0; started alone from
  `tests/services/docker-compose.yml` (`up -d --wait postgres`, healthy; `wait_ready.py --only
  postgres`: ready on the first probe); psycopg 3.3.6, SQLAlchemy 2.0.39 (SQLite through the
  standard library driver, PostgreSQL through `postgresql+psycopg`).
- Conformance kit, postgres (`tests/conformance/test_postgres_conformance.py`, `external`): 23
  passed — protocols, `check_catalog`, `check_lexical`, `check_vector`, `check_records`,
  `check_ledger`, `check_session`, `check_cache`, `run_all` with exact vectors **and** `run_all` with
  `vector_index: hnsw`, plus: one schema per generation and `DROP SCHEMA` on delete (cache keys of
  the generation removed), digest schema names for long/odd datasets, analyzer recorded per
  generation (`pg_ts_english`; a `simple` generation keeps `simple` when read by an `english`
  backend; an unknown configuration fails the write with a typed error), stemming and stop-word
  behaviour, hostile tsquery characters treated as plain tokens, exact vector order and scores equal
  to a NumPy cosine computation on 60×16 random vectors under a predicate scope, unknown/mis-sized/
  non-finite vectors refused, `values` cardinality cap failing closed, `timestamptz` storage of an
  `assume_utc` value, byte-order paging, registry pointer switched only by `activate` with a recorded
  failure leaving it untouched, advisory lock refused while held, `from_config`, unreachable server
  as `BackendError(postgres, get)`.
- HNSW gate (synthetic 400×32 normal vectors, 32 sampled queries × {unfiltered, allow-list}):
  measured recall@10 of the forced index path = **1.0** over 64 query/filter pairs with the defaults
  (`m=16`, `ef_construction=64`, `ef_search=100`); with deliberately degenerate settings (`m=2`,
  `ef_construction=4`, `ef_search=1`) it measured 0.24 and `refresh` failed with the typed
  `filter recall ... < min_filter_recall` error (the test asserts the failure at a 1.0 floor). Scope
  is never widened under ANN. This is a synthetic smoke of the gate, not a recall benchmark.
- Conformance kit, generic SQL (`tests/conformance/test_sql_conformance.py`): 12 cases per dialect
  (protocols, `check_catalog`, `check_records`, `check_ledger`, `check_session`, `check_cache`,
  `run_all`, generation tables and isolation, cardinality cap, byte-order paging, registry pointer
  and lease incl. stale-lease takeover, ping/dialect) — **sql[sqlite] 12 passed** (runs on every
  push) and **sql[postgres] 12 passed** (`external`), plus 2 dialect-independent tests
  (`from_config`, unusable URLs / unreachable database / over-long ids as typed errors). Tested
  dialects are exactly SQLite and PostgreSQL; MySQL/MariaDB and others are untested.
- Engine end to end (`tests/test_external.py::test_postgres_engine_end_to_end`, `external`, passed):
  the support example corpus ingested with catalog, records, ledger, sessions, cache, dense and
  lexical on postgres (communities on local sqlite): manifest `complete`, all projections verified,
  dense and lexical hits, filter pushdown, delete-and-rebuild switches `CURRENT` while the old
  generation is retained. Non-external contract test: records/ledger/sessions/cache on `sqlalchemy`
  (SQLite) through the engine, count aggregate 4/4.
- Unit (no server): 13 tests in `tests/unit/test_postgres_adapter.py` for naming, SQL and tsquery
  compilation (bound parameters only), and driver-error mapping through a thin fake connection
  (typed errors with operation names, no retry, broken connection discarded, validation before any
  I/O, held lock refused) — evidence of error mapping only.
- Totals: `pytest -q -m 'not external'` **506 passed** (473 at `783d821`); `-m external` on the
  postgres/sql/engine files **36 passed, 2 skipped** (neo4j, v1-only); `ruff check src tests` clean;
  `docs/config.schema.json` regenerated (`backends.*.vector` for postgres). Teardown: only the
  `postgres` service was stopped and removed; no other container of the project was touched.

Known limits, stated rather than claimed away:
- FTS scores are `ts_rank_cd` cover density, not BM25, and analysis differs from FTS5/Lucene
  (stemming, stop words); only membership, scope, determinism, phrase/boolean semantics and paging
  are asserted. No cross-engine agreement report yet.
- Measures and groups are computed over the matched rows with the shared `_scope` helpers on both
  adapters (rows are fetched for `matched_ids` anyway); predicates, population counts, `exists` and
  `values` run in SQL. Plan wording "SQL aggregates, date buckets, top-N" is therefore met by
  identical results, not by SQL `GROUP BY`.
- Projections on a backend other than the catalog's (e.g. sqlite lexical over a postgres catalog)
  fail the build with a typed error; that is the multi-backend write-path slice.
- The generation manifest does not yet record the lexical `analyzer_id`; the adapter exposes it
  (`PostgresBackend.analyzer_id(gen)`).

## Declarative pipelines, harness through the engine, `exact_id` arm — 2026-11-03 (Phase 1, slice P1/pipeline-harness)

Scope: named pipelines (`pipeline` + `pipelines` overlays, `default_pipeline`) with per-request
`pipeline`/`mode` overrides checked against `request.pipelines`/`request.modes`; aggregated static
validation (`pipeline/validate.py`) and aggregated component validation at engine construction;
`doctor --probe` (backend `probe()` on sqlite, memory, mongodb, opensearch; registry-declared
provider pairs; no model calls); sessions through `Engine.search/answer(session_id=...)` with a
per-session budget, generation-switch re-validation and turn recording; memory never evidence;
read-only built-in tools (`search`, `get_document`, `list_values`, `aggregate`) and allowlisted,
ledger-audited actions (`ingest_delta`, `invalidate_cache`); the `exact_id` arm (alias map,
`alias_fields`, `id`-field `pattern`s, pins, unknown identifiers reported).

What the tests show (synthetic four-record fixture, in-memory and sqlite backends, fake generator
and planner that book one call each on the request budget):
`tests/unit/test_pipeline_validation.py` (13) — overlays merge and round-trip, a profile overlay
contributes pipelines (explicit config wins), one config with eight different faults reports all of
them with their key paths, the named-pipeline projection rule, allowlists naming unknown pipelines
or unbuildable modes, budget coherence errors and the review-bound warning, tools/actions limited to
built-ins, `exact_id` identifier-source and alias-field checks, `FieldSpec.pattern` rules.
`tests/contract/test_pipeline_harness.py` (16) — a request selects an allowed pipeline and a mode
(`rules` → `llm` switches the agent); unlisted pipeline/mode is `RequestRejected`; session turns
recorded with hashes and IDs, principal binding (`AccessDenied`), closed/unknown session
(`SessionNotFound`); a re-ingest between two turns pins each answer to its own generation, reports
`generation_changed {from, to}` and the dropped ID; a generator citing the previous turn's record
abstains with `unknown_evidence` and its payload has no memory; the session budget spans turns and
refuses the third; tools respect a tenant ACL (`get_document` hides another tenant's record,
`list_values` reports truncation, `aggregate` refuses undeclared fields); actions: not allowlisted →
denied and audited, `ingest_delta` audited `started` then `applied` with generation before/after,
duplicate key skipped, failure recorded, HTTP/MCP need `serving.actions.allow` and the `actions`
scope; `exact_id`: pre-pass routes `REC-1003` to an `exact_id` step, the match is pinned first,
aliases resolve, `REC-9999` is an unsupported constraint, a misspelt alias or wrong case matches
nothing (unless `casefold`), an alias outside the caller's ACL or an excluded id is reported unknown,
`pins: []` keeps the hit unpinned. `tests/contract/test_doctor_probe.py` (5) — probe report on
memory and sqlite with every `JsonModel` entry point patched to fail if called; a failing probe is
reported (`probe: failed`); CLI `doctor --probe` on sqlite; sessions persist in the sqlite store
across engine instances and the CLI `session`/`search --session`/`tool`/`action` commands match the
Python API; the action audit is read back from the ledger by a new engine. MongoDB `probe()` is
covered with the fake client in `tests/unit/test_mongodb_adapter.py` (success and typed failure).

Evidence level: **synthetic / contract-mocked only.** No real service was started for this slice:
the MongoDB and OpenSearch probes were not exercised against containers, no model provider was
called, and nothing here measures retrieval quality. `pytest -q -m 'not external'` with
`PYTHONPATH=<worktree>/src`: **607 passed, 46 deselected** (573 before; +34); ruff clean;
`docs/config.schema.json` regenerated with `su_multi_arm.core.schema.write_all`.
`tests/test_protocol.py` now starts the MCP subprocess with `PYTHONPATH` pointing at the package
under test, so a worktree run no longer imports an editable install from another checkout.

### Merge with `v2/phase-0` at `16a3a17` — 2026-11-03
After the merge, `doctor --probe` calls `probe()` on all eight backend kinds (postgres, sqlalchemy,
neo4j delegate to their `ping()`). Evidence: delegation unit-tested with a stub only; no container
probe run in this session. `pytest -m 'not external'`: 675 passed, 116 deselected; ruff clean.

## Multi-backend write path — 2026-11-03 (Phase 1, slice P1/multi-backend-write)

Scope: each generation-scoped projection (catalog, records, lexical, vector, graph, communities) on
any backend that serves it; scope mirrors on secondary holders; per-target validation; activation
in a defined order with compensation; per-request generation pinning with refusal on disagreement;
manifest with every backend's generation identity and lexical analyzer id; `delete_generation`
across backends. Closes the two postgres follow-ups listed just above (projections off the
catalog's backend; analyzer id in the manifest).

Evidence levels:

- **Synthetic, in-process (every push):** `tests/contract/test_multi_backend_write.py` (20 tests) on
  memory + sqlite combinations through `Engine.ingest/search/answer`: two mixed placements return
  arm results identical (documents, chunk ids, provenance, order) to a single-backend reference of the
  same kinds, unfiltered and with a request filter pushed down on the mirrors, plus identical
  `group_count` aggregates; manifest contents (activation order, roles, mechanisms, identities,
  `analyzer_id` `python_word`/`fts5_unicode61`, verified mirror projections, secondary registry
  pointers and manifests, no generation data on the ledger/session/cache backend); records written
  to both `storage.records` and the structured arm's backend; activation order (every secondary
  before `CURRENT`); injected activation failure on a lexical holder, a graph holder and the
  authoritative `CURRENT` (moved pointers restored in reverse, `CURRENT` unchanged, staged generation
  removed everywhere, reads still served from the old generation); first-build failure removes every
  moved pointer; a failed compensation is recorded, its cleanup refused and readers refused with
  `GenerationDisagreement`; a secondary registry switched or removed by hand refuses `search` and
  `answer`; `delete_generation` refusals (CURRENT, named by a secondary pointer, protected — nothing
  deleted) and deletion on every holder; `ConfigError` for a lexical arm on a backend that cannot
  take a mirror; registry `deactivate` on file, memory and SQLAlchemy-on-SQLite registries.
  `tests/unit/test_opensearch_backend.py` +1 (fake transport: `activate_generation`,
  `deactivate_generation` removes only aliases still naming the generation, `generation_identity`).
  Non-external suite 638 → 659 passed; ruff clean; `Config` unchanged (schema not regenerated).
- **Real service (this machine, Docker):** `tests/services/test_multi_backend_engine.py` (1 test,
  `external`): catalog, records, dense and runtime stores on PostgreSQL, lexical on OpenSearch, graph
  and communities on Neo4j through `Engine` — manifest identities (`pg_ts_english` schema,
  OpenSearch indices and `lucene_standard`, Neo4j key), hits from every arm, request filter pushed
  down on the OpenSearch mirror and resolved for Neo4j, injected `CURRENT` failure with the alias and
  the pointer node restored (`left_for_cleanup` empty, failed generation absent from both), alias
  switched back by hand → `GenerationDisagreement`, `delete_generation` refusals and deletion on all
  three services. **1 passed.** The existing external suites for these three services were re-run
  with the change: 68 passed, 8 skipped (undeclared capabilities), elasticsearch/mongodb/local-LLM
  deselected. Images: `pgvector/pgvector:0.8.0-pg17` (`sha256:40b40496…`),
  `opensearchproject/opensearch:2.19.1` (`sha256:72fe2fc8…`), `neo4j:5.26.0-community`
  (`sha256:5a015e53…`); the three containers were started for the run and removed afterwards.

Not verified here: MongoDB and Elasticsearch as secondary holders against a real server (their
registry `deactivate` and the Elasticsearch registry `current_generation` are unit-trivial but not
service-tested); concurrency between two writers on different secondary backends (one write lock,
held on the catalog's registry); crash between two pointer switches (process-failure isolation only;
a crash leaves a disagreement that readers refuse until the next build or an operator realigns it).

## Serving parity and streaming — 2026-11-03 (Phase 1, slice P1/serving)

Scope: `Engine.answer_stream` (typed events, provisional token deltas, contract on the final answer,
cancellation), SSE streaming for the anthropic/openai/openai_compatible providers, the HTTP surface
(`/v2/answer/stream`, sessions, tools, actions, values, capabilities, `/health`, `/ready`), the
`PrincipalResolver` hook, MCP stdio and streamable HTTP with the tool set mirroring HTTP, session
binding and the start-up schema-drift check.

What the tests show (synthetic four-record corpus; memory and sqlite backends; the generator is a
loopback OpenAI-compatible fake, `testing/provider_fakes.py::FakeChatServer`, serving JSON and SSE):
`tests/contract/test_streaming_providers.py` (18) — OpenAI/OpenAI-compatible/Anthropic event streams
(text, Anthropic tool-use `input_json_delta`) give the same parsed reply and usage as the
non-streamed call; `length`/`max_tokens`, a missing `finish_reason`/`[DONE]`/`message_stop`, garbage
chunks, Anthropic `error` events (overloaded, rate limit) and deltas for unknown blocks are typed
failures and still recorded; a JSON reply to a stream request is refused (no fallback); a role
without `capabilities.streaming` fails before any request; `provider: none` never streams; a
cancelling delta consumer closes the call (`error_category: cancelled`); SSE parsing (comments,
multi-line data, CRLF). `tests/contract/test_answer_stream.py` (9) — event order `decision →
arm_result → evidence → token… → contract → done` with contiguous sequence numbers; the `done`
result equals `Engine.answer` (timings/ids removed); no deltas for a non-streaming role or without a
generator; a generator that streams a fabricated count claim → tokens, then `contract: abstained`
with `tokens_superseded`, final answer abstains; request problems raise before any event; cancel
after the first token → terminal `error: cancelled`, budget cancelled, the fake server sees the
connection closed before the reply ended; provider unreachable → `error: provider_connection`;
streamed turns recorded on the session. `tests/contract/test_serving_http.py` (9) — bearer
resolver (401/200, unset token variable fails closed), a custom header resolver with scopes (403)
and a tenant ACL applied to search and `/v2/values`; `/v2/answer` and `/answer` equal the Python
result; SSE names/ids/order and the final result equal `/v2/answer`; heartbeat comments while the
model is slow; error events carry a generic message; a raw-ASGI client disconnect (ASGI spec 2.4,
where Starlette itself would not listen) cancels the run, its budget and the provider stream;
session create/get/delete with principal binding (403/404); tools (enabled list, 404 for a disabled
tool, 422 for invalid input, 403 without scope); actions applied/duplicate/denied and audited;
values (`listable` only, truncation, `page_size_max`); `/ready` 200, 503 when a backend probe
raises (probe text not exposed), 503 without an active generation.
`tests/parity/test_serving_parity.py` (1) — one `SearchRequest` through the Python API,
`answer_stream`, HTTP, HTTP SSE, an MCP **stdio subprocess** and an MCP **streamable-HTTP** server
(uvicorn on loopback) yields identical `AnswerResult`s (and identical `SearchResult`s over both MCP
transports); MCP progress notifications carry the stream events in order with tokens; each MCP
session is bound to its own engine session holding both turns. `tests/parity/test_request_parity.py`
also checks every MCP tool mirror against its input model.

Evidence level: **synthetic / contract-mocked transports.** HTTP, SSE and both MCP transports ran for
real on loopback; the model is a fake, Anthropic/OpenAI streaming is checked against mocked event
streams in the documented shapes only (no paid provider called; owner decision #14), and nothing here
measures answer quality. Not tested: MCP client-side request cancellation (the handler's `finally`
cancels the stream, but no test sends `notifications/cancelled`), an in-flight provider read blocked
before its first byte (bounded by the role timeout, not interruptible), OAuth adapters (outside core).
`pytest -q -m 'not external'`: **712 passed, 116 deselected** (675 before; +37); ruff clean;
`docs/config.schema.json` regenerated with `su_multi_arm.core.schema.write_all` (no drift: `Config`
unchanged — `capabilities.streaming` already existed).

## MongoDB instant-based temporal fields (conformance fix) — 2026-11-03 (branch `v2/phase-0`, slice P1/mongodb-fix)

The MongoDB adapter was written before the S3/S4 review fix B1 (instant-based temporal scope) and
its external suite was not re-run afterwards: `tests/conformance/test_mongodb_conformance.py::
test_capability[records]` and `::test_run_all` failed with `assume_utc: naive stored values compare
as UTC instants`. `backends/mongodb/_db.py` now follows the shared `_scope` storage rule like
sqlite/postgres/sql/Lucene: typed rows classify temporal strings with `_scope.instant` (aware stamps
converted to UTC, naive `assume_utc` stamps and date-only strings read as UTC) and store the canonical
UTC ISO text plus epoch seconds; range bounds and temporal `eq`/`in` on typed records go through
`_scope.temporal_key` with the field spec (instant equality across offsets; `date` fields truncate
bounds to the UTC date and match equality on the UTC-day window); a non-temporal value against a
temporal field never matches; text bounds compare only string rows (temporal vs non-temporal is
incomparable, as in `_scope.compare`). Document scope (no schema) compares instants without date
truncation, as on the other adapters.

Evidence level: **real-service conformance on the existing throwaway test container plus a
driver-free unit test; no production outcome.** Service `mongo:7.0.16` (container
`su-multi-arm-tests-mongo-1` of the test compose project, digest as recorded above), pymongo 4.19.0.
`pytest -q -m external tests/conformance/test_mongodb_conformance.py`: **16 passed** (both previously
failing checks included). New unit test
`tests/unit/test_mongodb_adapter.py::test_temporal_fields_compare_as_instants_like_the_shared_scope_rules`
evaluates the compiled filters over the stored typed rows with a minimal in-test evaluator of the
emitted MongoDB operator subset and asserts parity with `_scope.matches` (the memory reference) on
the kit's `assume_utc`/`require_offset`/`date` records (13 predicates); it fails against the previous
`_db.py`. `pytest -q -m 'not external'`: **734 passed, 117 deselected** (733 before; +1); ruff clean;
no `Config` change. The container was removed afterwards (`compose rm -sf mongo`).

Other adapters checked for the same gap: sqlite, memory, postgres and generic SQL use the shared
`_scope` helpers and were written or re-run after B1; opensearch shares `_lucene_common`
(`temporal_key`) and its suite was re-run with the multi-backend write path; elasticsearch was
re-verified after B1; neo4j holds no records. No other adapter was found with the gap.

## Release-review fixes for 2.0.0a1 — 2026-11-03 (branch `v2/phase-0`, slice a1-fix)

Fixes for the release review (`docs/plans/reviews/a1_release_review.md`) B1–B6 (README excluded) and
N1–N5, N13. B1: `record_context.record_id` is resolved through the caller's ACL-scoped catalog read;
an out-of-scope or unknown id is dropped from the planner and generator payloads, traced
(`record_context.used: false`) and reported as `value_not_observed`. B2: a re-planned review hop that
starts with a structured step no longer crashes (`engine.answer`, `/v2/answer`, `/v2/answer/stream`).
B3: a non-empty `pipeline.guards` / `pipelines.*.guards` is a `ConfigError` (`not_executed`). B4:
origin-service and vendor names were removed from tracked files, and `docs/BUSINESS_SUMMARY.md` and
`docs/MIGRATION.md` were replaced by `docs/ADOPTING.md`. B5: version `2.0.0a1` in pyproject,
`__version__`, the HTTP app and `--version`; extras `openai`, `anthropic` (empty), `local`, `all`. B6:
`examples/v2/{local,mixed_backends,hosted_models}.yaml`, CONFIGURATION/PLAN/VALIDATION headers. N1:
SQL-family, Mongo and memory record scans fetch at most `max_matched + 1` rows and refuse above the
cap with a typed `BackendError`. N2: a scope with a dropped predicate is `bounded`, and the contract
never treats one as exact. N3: `opensearch` as `storage.catalog` is a validation error
(`catalog.registry` flag). N4: HTTP 503 for backend, generation and index-state errors, 502 for
provider errors, 499 for cancellation. N5: the `llm` decision agent re-raises package errors
(`RunCancelled`, `BackendError`) instead of mapping them to `provider_error`.

Evidence level: **synthetic unit, contract and conformance tests with fakes and the
memory/sqlite/SQLAlchemy-on-SQLite adapters; no real-service run.** The N1 cap and N2 exactness checks
were added to `testing/backend_conformance.py::check_records`. They ran on memory, sqlite,
sqlite_faiss and sqlalchemy-on-sqlite. They were **not** run against the PostgreSQL, MongoDB,
OpenSearch or Elasticsearch containers in this slice. That run belongs to the consolidated
release-commit run (B7). `pytest -q -m 'not external'`: **758 passed, 117 deselected** (734 before).
Ruff is clean. The `Config` schema is unchanged (regenerated with `core.schema.write_all` and diffed).
The wheel builds as `su_multi_arm-2.0.0a1-py3-none-any.whl` and declares the new extras. The new
tests fail against the previous code (B1, B2 checked by stashing the engine change).

## Landing README, documentation site and generated capability tables — 2026-11-03 (branch `v2/phase-0`)

Evidence level: **synthetic, offline execution of every documented command**; no real-service or
hosted-provider run. Scope: `README.md`, `docs/{index,quickstart,serving,schema,backends,providers,contributing}.md`,
`mkdocs.yml`, `scripts/gen_capability_tables.py`, `scripts/mkdocs_hooks.py`, `.github/workflows/docs.yml`,
`CITATION.cff`, `assets/logo.svg`.

- Every bash, Python, YAML and JSON block in the README quickstart and serving sections (and the
  identical blocks in `docs/quickstart.md`, `docs/serving.md`, `docs/schema.md`) was executed
  verbatim in a fresh clone of `8f6dd56` with a fresh virtual environment in the external test
  workspace, with no `*_API_KEY` variable set: `pip install -e '.[dev]'`; the six CLI commands on
  `examples/v2/local.yaml`; the Python ingest/search/answer example (status `complete`, three hits
  with their arms, contract `passed` with generation disabled); `config validate` of the 20-line
  mixed-backend YAML (valid, no warnings, no database driver installed; no service contacted);
  `config schema`; `pip install -e '.[api]'` and `'.[mcp]'`; the HTTP block (`/v2/search` returned hits,
  `/v2/answer/stream` emitted `decision` through `done`); the MCP stdio client configuration (an MCP
  client launched exactly that command and called `search`); and the streamable-HTTP command with
  `YOUR_CONFIG.yaml` replaced by `examples/v2/local.yaml` plus the documented `serving.mcp` block.
  Substitutions: the clone URL pointed at a local path (the public repository was not used), and the
  server examples ran on loopback ports 8099/8100. The "runs fully offline" callout rests on this run.
- Backend and provider tables are generated from `backends/capabilities.py`, the provider registry and
  the embedder literal; `tests/unit/test_capability_tables.py` (5 tests) fails on drift, on a backend
  kind without an evidence mapping, and on a "real-service conformance" claim whose test file is not
  marked `external`. "Real-service conformance" in the tables means such a suite exists; the recorded
  runs are the dated entries above.
- `mkdocs build --strict` (mkdocs 1.6.1, mkdocs-material 9.8.0) completed with no warnings.
  `scripts/mkdocs_hooks.py` rewrites links that leave `docs/` (three `../examples/...` links in
  `CONFIGURATION.md`) to the repository on GitHub instead of relaxing link validation.
- Not verified: GitHub Actions `docs.yml` and the Pages deployment (repository not yet published), the
  CI and star badges, rendering on github.com (Mermaid was checked only as emitted `class="mermaid"`
  blocks), and `pip install "su-multi-arm[all]"` from an index (not published; marked "once published").

## 2.0.0a1 release evidence — 2026-11-03 (branch `v2/phase-0`, release review B7)

Evidence level: **real-service** for the adapters below (disposable local containers from
`tests/services/docker-compose.yml`, project `su-multi-arm-tests`, ports on 127.0.0.1 only, started,
used and removed with `down -v` in this session); **synthetic** corpus throughout.

Source under test: commit `8f6dd56` (the release-review fixes `0cbbbfc` plus its log entry). The
evidence commit that records this section adds only tests, a script and docs; no file under `src/`
changed between the run and that commit.

Images (`docker image inspect --format '{{index .RepoDigests 0}}'`):

| service | tag | digest |
|---|---|---|
| OpenSearch | `opensearchproject/opensearch:2.19.1` | `sha256:72fe2fc84be8295906b8efca020b46c58ed45c8da60cd9b8b49e1991e38e89a4` |
| Elasticsearch | `docker.elastic.co/elasticsearch/elasticsearch:8.17.4` | `sha256:413d8df69823e9f41f6008f025fcbaf31a5849ab9c74894731d7ded65543cd56` |
| PostgreSQL + pgvector | `pgvector/pgvector:0.8.0-pg17` | `sha256:40b404964359299eefdd5f8518facf1886c562848cf4de13b6eaf91cb70c2b87` |
| Neo4j | `neo4j:5.26.0-community` | `sha256:5a015e53de1895e7eee1574ae0325cf8c4b89587222778108c594bdd45a474b5` |
| MongoDB | `mongo:7.0.16` | `sha256:c630c59342c1493d50345136df2af14a76b9e827dd5316bfabee07a0880a5f3a` |

`tests/services/wait_ready.py`: all five ready (Elasticsearch and OpenSearch cluster green, Mongo ping,
Neo4j Bolt 5.4, PostgreSQL accepting connections).

**Consolidated run** — `pytest -m external -p no:cacheprovider -rs` with every `SU_TEST_*` service
variable from `tests/services/README.md` set and `SU_TEST_LOCAL_LLM_URL` unset:
**100 passed, 17 skipped, 0 failed** (763 deselected), green on the first run and identical on a
second run with `-rA`. No code fix was needed.

| adapter / suite | passed | skipped |
|---|---|---|
| opensearch (kit, exact kNN) | 6 | 4 |
| opensearch_hnsw (kit) | 6 | 4 |
| elasticsearch (kit, exact kNN) | 6 | 4 |
| elasticsearch_hnsw (kit) | 6 | 4 |
| elasticsearch extra (`test_elasticsearch_conformance.py`) | 4 | 0 |
| opensearch adapter (`tests/services/test_opensearch_adapter.py`) | 6 | 0 |
| postgres (`test_postgres_conformance.py`, incl. `run_all` exact and HNSW) | 23 | 0 |
| sqlalchemy on postgres (`test_sql_conformance.py[postgres]`) | 12 | 0 |
| mongodb (`test_mongodb_conformance.py`) | 16 | 0 |
| neo4j (`test_neo4j_conformance.py`) | 10 | 0 |
| neo4j engine (`tests/services/test_neo4j_engine.py`) | 3 | 0 |
| multi-backend engine (`tests/services/test_multi_backend_engine.py`) | 1 | 0 |
| postgres engine (`tests/test_external.py`) | 1 | 0 |
| local model (`tests/services/test_local_llm.py`) | 0 | 1 |

Skip reasons: the kit skips capabilities an adapter does not declare — OpenSearch and Elasticsearch
(both kNN modes) do not declare `graph`, `ledger`, `session` or `cache` (16); the local-model test
skips because `SU_TEST_LOCAL_LLM_URL` was deliberately unset in this run (1, see below).

The release-review N1/N2 conformance additions in `check_records` (a dropped predicate makes the result
`bounded`; a scope above `max_matched` is a typed `BackendError`, a scope at the cap answers exactly)
passed on postgres, sqlalchemy-on-postgres, mongodb, opensearch and elasticsearch (both kNN modes) in
this run; previously they had run only on memory, sqlite and sqlalchemy-on-sqlite.

**Local-model provider smoke** (run separately by the coordinator on 2026-11-03, not in this session):
`tests/services/test_local_llm.py` 1 passed against `qwen2.5:1.5b` served through an
OpenAI-compatible endpoint on a local GPU host. A small model was chosen because that host was nearly
full; this shows the provider wire contract works against a real local server, not answer quality.

**Lexical cross-engine agreement** (informational, plan §3.5; full table in
[LEXICAL_AGREEMENT.md](LEXICAL_AGREEMENT.md)): `scripts/lexical_agreement.py` indexed the same 19 chunks
(7 shipped example documents) into sqlite (`fts5_unicode61`), postgres (`pg_ts_english`), opensearch and
elasticsearch (`lucene_standard`) and mongodb (`mongo_text_english`), and ran 23 fixed queries (15
terms, 4 phrase, 4 boolean). Zero query errors on any engine. The complete match sets were identical
on all 23 queries within each analyzer family (sqlite / opensearch / elasticsearch, unstemmed; postgres
/ mongodb, English-stemmed) and on 18/23 across families (membership Jaccard 0.915); the five
differing queries are stemming cases (`certificates`, `expired`, `applying`, phrase `effective date`,
boolean `certificate -expired`). Mean Kendall tau on shared top-10 items ranges from 0.135
(sqlite / postgres) to 1.000 (opensearch / elasticsearch). On a 7-document fixture these numbers
describe analyzer behaviour only.

Still mocked or unmeasured: hosted Anthropic and OpenAI providers (HTTP contract fixtures only; no paid
request was made); relevance or answer quality on any non-synthetic corpus; scaling and concurrency.

Local suite at the evidence commit: `pytest -m 'not external'` 766 passed (includes the docs agent's
uncommitted capability-table tests in the working tree). Before this slice the same tree gave 762
passed and 1 failed: the tracked-file denylist test matched its own `DENYLIST` line once the test
file was committed (it had passed while untracked); the test now excludes its own file.


## 2.0.0a2 serving/config slice — 2026-11-04 (QA-05, 07, 10, 11, 12, 14, 16; review risk R3)

Evidence level: **synthetic and mocked**, plus one small real-service check. Fixes for the
2.0.0a1 QA findings named above (`docs/plans/reviews/qa_2.0.0a1.md`); each fixed finding's
`xfail(strict=True)` marker in `tests/qa/test_qa_2_0_0a1.py` was removed and the test now passes.

- QA-05 sessions: every session write is a compare-and-set on `SessionRecord.version`; the
  session-budget clip is a per-turn hold taken in the same CAS. Tests: 30 concurrent `search` turns
  on one session on sqlite and memory keep 30 turns; 30 concurrent `answer` turns under
  `budgets.session.max_llm_calls: 3` make at most 3 model calls (the generator is a fake that sleeps
  50 ms to force overlap); store-level CAS tests on memory, sqlite, sqlalchemy-on-sqlite (conformance
  kit) and a MongoDB document fake. **Real service**: the kit's `check_session` (now with the CAS
  checks) passed on PostgreSQL and SQLAlchemy-on-PostgreSQL against the local throwaway stack
  (`tests/services/`, already running, isolated random prefixes). MongoDB CAS was **not** run against a
  server: its conformance fixture drops a shared database name and the stack was in use by another run.
- QA-07 sqlite lexical: the query is no longer `str.casefold`ed (FTS5 `unicode61` folds it as it
  folded the text). `Straße`, `ﬁle`, `Œuvre` and Greek capitals match their own indexed words;
  `strasse`/`file` do not (unicode61 expands neither). `scripts/lexical_agreement.py` (sqlite only,
  no `SU_TEST_*` variable set): per-query results byte-identical before and after the change on the
  23-query list, so `docs/LEXICAL_AGREEMENT.md` stays valid.
- QA-10 `config upgrade --output` rebases relative paths for the output's directory (CLI test from
  another directory opens the v1 index). QA-11 schema errors are aggregated with key paths; the CLI
  prints concise errors with exit codes 1/2/3 and a traceback only with `--debug`. QA-12 the bearer
  scheme is case-insensitive. QA-14 keys used only by non-default pipelines/modes are checked on
  first use; `doctor` reports `missing_keys`.
- QA-16: `pytest -q` in a venv with every extra installed and **no** services: 823 passed, 115
  skipped, 12 xfailed, 0 errors, measured before the 4 `tests/unit/test_service_probe.py` tests
  were added and before the throwaway stack was started by another run (before the fix: 27 failed,
  46 errors from the OpenSearch/Elasticsearch conformance and OpenSearch services tests). The external search-engine tests now skip with the
  reason when their variable is unset and the default stack does not answer.
- Risk R3: `extraction.ontology`, `models.roles.judge`, `models.roles.cache_gate` and
  `pipeline.arms.cache` are refused at validation ("not implemented in this release"); a unit test
  walks every Config field and requires a reader (name-based).

Local suite at the slice commit: `pytest -q -m 'not external'` 824 passed, 12 xfailed (before: 766
passed, 20 xfailed at `95bb183`); ruff clean; `docs/config.schema.json` regenerated.
Not covered: MongoDB CAS on a real server; multi-process (as opposed to multi-thread and
multi-manager) session contention; hosted providers (all model calls are fakes).

## Decision agent, typed scope, request filters, answer contract, generation — 2026-11-04 (2.0.0a2, slice a2/decision-contract)

Fixes for QA-01, QA-02, QA-03, QA-04, QA-08, QA-09, QA-13 and QA-15 of
[the 2.0.0a1 QA report](plans/reviews/qa_2.0.0a1.md). Evidence levels:

- **Synthetic / mocked (offline)**: `pytest -m 'not external'` 846 passed, 9 xfailed (was 766 passed,
  20 xfailed). The QA reproductions for QA-01, QA-02 (3), QA-03 (3), QA-04, QA-09, QA-13 and QA-15
  now pass without `xfail`; model replies are replayed through a mocked HTTP transport. New tests:
  `tests/unit/test_answer_text_contract.py` (statement detection incl. non-count numbers: versions,
  dates, ids, decimals, percentages, durations, step numbers, quotations from evidence; strip/abstain/
  fail; absence needs an empty exact scope; per-request answer schema),
  `tests/contract/test_scope_qualifiers_and_filters.py` (the recorded QA-01 shape and four variants,
  exact scopes stay exact, request filters, caller-typed aggregates, plain retrieval, QA-09
  predicates, every QA-02 filter class, the aggregate tool, QA-03 on `examples/v2/local.yaml` and the
  v1 `examples/local.yaml` through search, answer, stream, the `search` tool and HTTP), and strict
  schema cases in `tests/unit/test_provider_capabilities.py` (every refused keyword, the accepted
  keyword set, every schema the package sends, validation and `doctor --probe` reporting). The nine
  NL-count probe tests still pass (their raw-fallback violation list now also names
  `answer_text_without_exact_scope`).
- **Real provider (paid, OpenAI `gpt-4o-mini`, QA-08 only)**: the same 25 plain how-to questions as
  the QA report (10 questions twice on the QA's synthetic 305-document corpus with its config:
  `rules` decision, native generator, neutral profile; plus the 5 questions of
  `test_qa08_narrative_questions_mostly_answer` on the committed synthetic fixture). Before (QA report,
  2.0.0a1): 24 of 25 abstained. After (this slice): **0 of 25 abstained**, all 25 `contract: passed`,
  every claim typed `narrative` (the per-request schema offered no other kind because no aggregate
  evidence was supplied), no invented ids. One reply (1 of 25) had an empty `answer` string with a cited
  narrative claim and passed; the contract now rejects that (`empty_answer`, unit-tested offline, not
  re-run against the provider), so the effective rate for that sample is 1 of 25. The opt-in
  `test_qa08_narrative_questions_mostly_answer` passed once more against the provider afterwards (5
  questions). Spend through the QA transport-level guard and ledger
  (`~/su_mutliarm_tests/qa/qa_ledger.jsonl`, pessimistic $2/$8 per 1M tokens): 30 calls, $0.3425
  (tags `a2_qa08` $0.2909, `a2_qa08_pytest` $0.0516); cumulative OpenAI QA spend $0.8641 of the $2.00
  cap; no call refused. Anthropic was not used (the supplied key is invalid).

Not measured: answer correctness or relevance of the 25 answers (only the contract outcome was
recorded), the QA-04 text detector's precision on real model output beyond these 25 replies,
languages other than English (the detector yields no statements; typed claims still bind), and
Anthropic native tool use with the per-request enum schema.

## Write path: leases, fencing, incremental embeddings, ingestion budget — 2026-11-04 (slice 2.0.0a2/W)

Fixes completeness-review risks R1 and R2 (`docs/plans/reviews/a1_completeness_review.md`) and QA-06
(`docs/plans/reviews/qa_2.0.0a1.md`). R1: every generation registry has an owner-token write lease
with heartbeat renewal, expiry only after missed renewals, owner-only renewal/release (Elasticsearch
no longer deletes another holder's lock document), and activation as a compare-and-set on the
expected previous generation (`ingestion/lease.py`, ARCHITECTURE "Writers"). R2: vectors are reused
by exact input and embedding recipe and streamed in `batch_size` windows; the manifest reports
`embedded`/`reused_embeddings` and what is still rebuilt (`incremental`). QA-06: `budgets.ingest`
(`max_llm_calls` default 10000, `max_embedding_calls`, `max_usd`) replaces the per-request cap on the
write path, with reservations before every LLM and embedding call.

Evidence level:

* **Synthetic unit/contract tests** (`pytest -m 'not external'`, bytecode off, external basetemp):
  **797 passed, 126 deselected, 19 xfailed** (before: 766 passed, 117 deselected, 20 xfailed; the
  QA-06 test lost its `xfail` and passes). New: `tests/unit/test_write_leases.py` (two concurrent
  writer threads on sqlite and memory — exactly one activates, the other gets `WriteConflict(held)`,
  its retry keeps every document; a writer whose lease expired and was taken over fails
  `write_conflict:lost`; a moved base fails `write_conflict:stale_base`; lease/CAS checks on the
  memory, file, SQLAlchemy-on-SQLite, MongoDB (fake collection) and PostgreSQL (fake connection)
  registries), `tests/unit/test_incremental_embeddings.py` (one changed document re-embeds only its
  changed chunks, counted at the embedder; a title change re-embeds that document; a change of prefix,
  dimension, model or revision re-embeds all; unchanged recipe reuses all; embedding-call budget;
  corrupt stored vector fails the build; vectors streamed per window), `tests/unit/test_ingest_budget.py`
  (offline `httpx.MockTransport`: 16 extraction calls succeed under a per-request cap of 2; the ingest
  cap stops at 3; `ingest.max_usd` stops extraction with committed spend ≤ the cap; embedding requests
  counted and priced from reported usage; unpriced spending role refused at construction; v1
  upgrade mapping), plus Elasticsearch lease/alias-fence unit tests with a fake transport.
* **Real-service tests** (throwaway `su-multi-arm-tests` containers, started with
  `up -d --wait postgres mongo elasticsearch opensearch neo4j` and removed with `rm -sf` of exactly
  those services): `pytest -m external tests/conformance tests/services/test_multi_backend_engine.py`
  **98 passed, 16 skipped (undeclared kit capabilities), 0 failed**, including the new lease/CAS tests
  on PostgreSQL (advisory lock seen in `pg_locks`), SQLAlchemy-on-PostgreSQL, MongoDB, Elasticsearch
  (lease document with `if_seq_no`, alias `must_exist` fence, concurrent-switch refusal) and the
  OpenSearch alias and Neo4j pointer compare-and-set. Images: `opensearchproject/opensearch@sha256:72fe2fc8…`,
  `elasticsearch@sha256:413d8df6…`, `pgvector/pgvector@sha256:40b40496…`, `neo4j@sha256:5a015e53…`,
  `mongo@sha256:c630c593…`.
* **Not measured**: no hosted provider was called (USD accounting is checked against an operator
  price table with mocked usage); no throughput or memory measurement at scale; multi-process
  writer contention was exercised with threads (and, on the servers, with separate registry objects),
  not with separate hosts.

Ruff clean. `docs/config.schema.json` regenerated with `core.schema.write_all` (adds `budgets.ingest`
and `models.roles.embedder.pricing_key`; the embedder `pricing_key` is excluded from the index
fingerprint, so existing generations stay valid).

## 2.0.0a2 release evidence — 2026-11-04 (branch `v2/phase-0`, release-review fixes)

Source: the release-review fix commits on `v2/phase-0` after `70dca3f`. Inputs: the blocking and
non-blocking items of `docs/plans/reviews/a2_release_review.md`. The review's own runs at `756b345`
were:

- `python -m pytest -q` with no services: 937 passed, 124 skipped.
- Bare `pytest -q`: 2 collection errors.
- External run on five containers: 107 passed, 1 failed, 18 skipped.

All runs below used `PYTHONDONTWRITEBYTECODE=1` with temporary paths under
`~/su_mutliarm_tests/claude/a2fix/`.

| Run | Result |
|---|---|
| `ruff check src tests` | All checks passed |
| `python -m pytest -q -m 'not external'` (repo venv) | **1018 passed**, 126 deselected. Before the fixes, at `70dca3f`: 934 passed, 1 skipped |
| Fresh venv from `git archive HEAD` with the CI extras `[dev,api,mcp,faiss,communities,sql,postgres,openai,anthropic]`, bare `pytest -q`, no `SU_TEST_*` | **1007 passed, 131 skipped, 0 errors**. With `-o pythonpath=` the collection fails with 2 errors (B4 reproduced and fixed). The first fresh run also had 8 errors: the Elasticsearch and OpenSearch conformance modules imported the client without the extra. They now skip unless a server URL is set. |
| `pytest -q -m external -rs`, all six `SU_TEST_*` variables, five containers (`su-multi-arm-tests`) | **108 passed, 18 skipped, 0 failed** (1018 deselected). Skips: undeclared capabilities (16), paid QA-08 (1), local LLM not configured (1) |
| Image IDs | elasticsearch 8.17.4 `sha256:413d8df69823e9f41f6008f025fcbaf31a5849ab9c74894731d7ded65543cd56`; mongo 7.0.16 `sha256:c630c59342c1493d50345136df2af14a76b9e827dd5316bfabee07a0880a5f3a`; neo4j 5.26.0-community `sha256:5a015e53de1895e7eee1574ae0325cf8c4b89587222778108c594bdd45a474b5`; opensearch 2.19.1 `sha256:72fe2fc84be8295906b8efca020b46c58ed45c8da60cd9b8b49e1991e38e89a4`; pgvector 0.8.0-pg17 `sha256:40b404964359299eefdd5f8518facf1886c562848cf4de13b6eaf91cb70c2b87` |
| Containers | Started with `up -d --wait` and removed with `down -v`. `docker ps` listed the same four unrelated containers before and after. |
| `su-multi-arm --version` (fresh venv); `python -m build` | `su-multi-arm 2.0.0a2`; `su_multi_arm-2.0.0a2-py3-none-any.whl` + `.tar.gz` (sdist includes CHANGELOG.md) |

**Blockers.**

- **B1 (synthetic, engine-level).** The reviewer's repro now gives `bounded` with
  `qualifier_not_applied: "priority 3"` and `contract: abstained`. The repro is "How many priority
  3 tickets are open?" with a planner scope of `status=open`, on 36 records where the truth is 4.
  Variants:
  - The same holds with `priority` listable, and for "have priority 3", "priority is 3", "priority
    above 2" and "priority: 3".
  - A scope or filter that applies the qualifier (`priority=3`, `"3"`, request filter, or `range
    lower 3`) stays exact at 4.
  - A wrong value (2) is flagged.
  - Unit tests cover `version 2.1` (number), `severity high` (non-listable string), `escalated` /
    `escalated false` (boolean), an id field, comparisons, and a negative list of 11 plain questions.

  The nine count probes (`tests/unit/test_count_probes.py`) and the plain-retrieval tests are
  unchanged and pass.
- **B2 (local, both trees).** `examples/v2/local.yaml` was ingested with the published 2.0.0a1 tree,
  a `git archive` of `public-main` = `a58df97`. `search` under 2.0.0a2 then exits 0 with top hits
  `case:certificate`, `kb:certificate`. Without the fix the same index gives `index/config fingerprint
  mismatch` (exit 3). The fingerprints of all 8 shipped examples are equal across the two trees,
  computed with backend paths fixed. `tests/unit/test_index_fingerprint_golden.py` pins them. With the
  fix reverted, 8 of its 9 tests fail.
- **B3.** The external run above has 0 failed (`test_failed_loads_never_move_the_pointer` passes).
- **B4.** See the fresh-venv row above.
- **B5 (contract, TestClient).**
  - `ingest_delta` while another writer holds the lease returns 409 with
    `result.category: write_conflict`, `reason: held` and no backend text. The audit record keeps the
    detail.
  - Invalid arguments return 422 (`invalid_request`).
  - `WriteConflict` and `SessionConflict` on `/v2/search` return 409 with a typed `detail`.
  - MCP `safe_message` returns `write_conflict: <reason>` / `session_conflict:
    cas_attempts_exhausted`.
- **B6.** `2.0.0a2` in pyproject, `__version__`, `--version`, the FastAPI app, CITATION (with
  `date-released`), and the README and docs banners. CHANGELOG.md was added.

**Non-blocking items done (synthetic).**

- **N1:** the reviewer's detector false positives are a negative fixture list. Real statements still
  fire, for example "Only 12 tickets", "Overall, 12 tickets", "That is the complete list of open
  tickets".
- **N2:** a stated count that differs from the cited exact aggregate is `answer_number_mismatch`
  ("99 open records" against 12 abstains; "12 of 36 records" passes). One existing harness test
  stated "Three" against a count of 2. Its text was corrected to "Two".
- **N4:** `product: "CloudTaxx"` and `"cloudtax"` (case-sensitive field) are reported as
  `value_not_observed`, the count is not exact, and an absence answer abstains.
- **N5:** a hosted embedder without `budgets.ingest.max_usd` or `max_embedding_calls` is refused at
  construction. Loopback `openai_compatible` and local embedders are accepted.
- **N8:** the key-file path was removed from `.claude/agents/*`.

**Not done:** N3, N6, N7, N9 beyond the action route, and N10. They are recorded as known
limitations in CHANGELOG.md.

**Real Anthropic smoke: not completed.** This was the first real Anthropic attempt with a key that
lists models.

- **Setup.** `claude-haiku-5-5` ran through the QA spend guard (`~/su_mutliarm_tests/qa/`, Anthropic
  cap $2.00, this run's budget $0.50) on a synthetic two-document corpus.
- **Result.** The first request was the LLM extraction call: native `tool_use`, `cache_control`
  on the system block and tool, `max_tokens` 1024. It returned **HTTP 429**. The package failed the
  build fail-closed: `IngestError`, category `provider`, no retry, and the provider body is not echoed.
- **Stopped.** Following the run rules, nothing was retried and no further call was made.
- **Spend.** The ledger shows 1 reservation ($0.0014) settled at **$0.00**. Cumulative Anthropic
  spend is $0.00 over 3 attempts: two 401s on 2026-11-03 and this 429.
- **Not established.** The run cannot tell a rate limit from a missing quota, because the guard does
  not log bodies. Still unsettled: whether `input_schema` with `$defs` is accepted (`LlmPlan` has 6
  `$defs`, `Answer` has 1), native planner and generator calls, streaming, and cache read/write token
  reporting. The Anthropic provider stays at "contract-mocked" evidence.

## Query-compiler operations (structured-question parity) — 2026-11-04 (slice parity/query-compiler)

Evidence level: **synthetic and mocked only**. No model call and no real service was used.
- **Records adapters.** `testing/backend_conformance.py::check_query_operations` holds every records
  adapter to the same results for ranking with ties, `nth`, calendar buckets, cross-tab, period
  comparison, shares, `count_distinct` with missing/null values (review B1), numeric measures with null
  coverage, negated states with their unknown count, lists with totals, record-identity predicates and
  an unresolved text condition. `check_text_count` checks whole-corpus text matches on every adapter
  with a lexical index. Ran here on memory, sqlite (numpy and faiss) and SQLAlchemy-on-SQLite; the
  PostgreSQL, MongoDB, OpenSearch and Elasticsearch modules run the same checks but were **not run**
  in this slice (no isolated service stack was started; they skip without one).
- **Grammar and planner.** `tests/unit/test_query_compiler.py` (41 tests) covers calendar
  expressions against a fixed anchor, zone handling, the rules grammar end to end over the in-memory
  store, refusals (median, ambiguous per-group measure, unstructured double windows, residual words,
  share without numerator), the strict-expressible planner schema with a mocked planner, the engine's
  text-topic count through the structured arm, the answer contract's number check and the packing cap.
- **Not measured.** The second historical service's structured-question gap (26/45) was not re-measured: that needs the
  live planner and the private corpus. The grammar is English-only and covers the phrasings listed in
  `docs/CONFIGURATION.md`; other phrasings still abstain with an `UnsupportedConstraint`.
- Suite: `pytest -q -m 'not external'` 1073 passed (was 1032 tests); `pytest -q` (with
  `-o pythonpath=src` in the worktree) 1076 passed, 123 skipped; ruff clean; golden index
  fingerprints unchanged.

## 2.0.0a3 contract and providers — 2026-11-04 (slice a3/contract-providers)

**Synthetic and mocked evidence** (offline, `httpx.MockTransport` replays of the observed reply
shapes):

- `tests/qa/test_qa_qwen_2_0_0a3.py` has 13 tests for QQ-01 to QQ-04 and QQ-06. They came from the
  local-model QA run (Ollama 0.33.0 `/v1`, a 27B open-weights model, synthetic support corpus). The
  four proposed failing tests from that run pass unchanged after the fix: they XPASS under their
  original `xfail(strict)` markers.
- `tests/unit/test_contract_strip_default.py` has 8 tests for the `strip` default.
- `tests/contract/test_anthropic_a3.py` has 16 tests: native default, `output_config.format`
  request/reply, the thinking minimum, the v1 raise, thinking-token usage, `stop_details`, and every
  package schema converting for `output_format`.
- Suite: 1031 passed and 1 skipped at the base, then **1069 passed** (`-m 'not external'`). Bare
  `pytest -q` gives 1072 passed and 123 skipped. The golden index-fingerprint test is green.

**Found while fixing QQ-06 (synthetic):** "there are 111 open CloudTax tickets" was not detected as
a count at all when the question was "How many open CloudTax tickets are there?". The cause: "open"
became a question unit, so "111 open" was rejected as a label and the farther unit was never tried.
The detector now retries the farther unit.

**Real Anthropic smoke (first successful real calls).**

Setup: `~/su_mutliarm_tests/claude/a3_smoke/smoke.py` ran through the QA spend guard
(`~/su_mutliarm_tests/qa/`). The guard's caps were Anthropic $2.00 total and $0.40 for this tag. The
guard now prices Sonnet at $2/$10 per 1M tokens and still prices cache reads at the full input rate,
so its ledger is pessimistic. Models: `claude-haiku-5-5` and `claude-sonnet-5-5`; no other model was
called. Corpus: 6 synthetic tickets. Each step ran once.

Results:

- **Planner, native `output_config.format`, Sonnet 5.5.** The `LlmPlan` reply was valid, with 2
  steps. Stop reason `end_turn`, 3081 input / 87 output tokens.
- **Planner, forced `tool_use`, Haiku 5.5.** The registry kind was overridden in the harness. The
  `LlmPlan` schema with `$defs` was accepted as `input_schema`, and the tool input validated. Stop
  reason `tool_use`, 0 thinking tokens: a forced tool call does not think.
- **Prompt caching (`controlled`, Haiku 5.5).** Two identical calls with a 42.7k-character system
  prompt. The first reported `cache_creation_input_tokens` 13873. The second reported
  `cache_read_input_tokens` 13873. Both are normalised into `Usage` (input 13898 including cached
  tokens).
- **Thinking (Haiku 5.5, `max_tokens` 1024, text mode).** Arithmetic prompt: `end_turn`, 469 output
  tokens, of which 464 were thinking (`Usage.reasoning_tokens`).
  - In an exploratory raw call before the fix, `max_tokens` 128 ended with `stop_reason:
    max_tokens`, all 128 tokens thinking and no text block. This is why the 1024 minimum exists.
  - Sonnet 5.5 did not think on the same prompt.
- **Schema limits found on the real API.** `output_config.format` refuses `minimum`/`maximum` on
  integers and `maxItems` on arrays with HTTP 400. It accepts `minLength`/`maxLength`, `anyOf`
  with null, `default` and `$defs`. These findings set what `output_format_schema` drops.
- **LLM extraction with grounding (ingest, Haiku 5.5 `output_format`).** 6 documents and 12 chunks:
  12 extraction calls, all `end_turn`, 117 to 379 thinking tokens each. 17 entities and 1 relation
  were accepted under the strict grounding policy, with 0 rejected.
- **Planned answer (Sonnet planner, Haiku generator).**
  - First run: the contract **abstained** (`factual_without_citations` plus
    `answer_text_without_exact_scope`). The raw reply was not captured on that run.
  - Second run, same question, raw reply recorded: `contract: passed`. 4 narrative claims, citations
    `case:0003` and `case:0000`, `insufficient_evidence: false`.
  - The model's output varies between runs. The first abstention is a fail-closed outcome, not a
    package error.
- **Streamed answer (`answer_stream`, Haiku generator `streaming: true`, `output_format`).** Events
  arrived as decision, arm_result, evidence, 50 token deltas, contract, then done.
  `contract: passed`, and the answer cites `case:0002` (the BOM fix).
- **Refusal.** `stop_reason: refusal` was not exercised; it remains contract-mocked.
- **Spend.** 44 requests: 37 Haiku 200s, 5 Sonnet 200s and 2 Haiku 400s from the schema-limit
  probes. The ledger shows **$0.0467** ($0.0342 Sonnet, $0.0125 Haiku). Cumulative Anthropic QA
  spend is $0.0467 of the $2.00 cap.

## Kuzu graph backend, typed traversal templates, hierarchical communities — 2026-11-04

Scope: `backends/kuzu/` (embedded Kuzu 0.11.3, extra `kuzu`; `mode: native` and read-only
`mode: attach`), `core/config_graph.py` + `arms/traversal.py` (typed traversal templates selected by
a `GraphIntent`), `communities/hierarchy.py` (`louvain_hierarchical`) and `communities/stats.py`.

Evidence level:

- **Synthetic, in-process (default test run).** The graph conformance kit passes on the native kuzu
  adapter with no service (`tests/conformance/test_kuzu_conformance.py`: fact-only traversal, hop
  bound, document-id eligibility, predicates refused, provenance/occurrences, memberships, communities,
  MERGE idempotency, atomic rejected upsert, generation isolation, compare-and-set pointer, namespaces
  sharing one file). One traversal-template fixture gives **identical results on the in-memory fake,
  sqlite and kuzu** (`tests/conformance/test_traversal_templates.py`); the kit's graph check now runs
  the template cases on every graph backend. Attach mode is tested against a synthetic external Kuzu
  database with its own schema (`tests/unit/test_kuzu_attach.py`): mapping validated against the
  store catalog, unverified/quote-less/undocumented edges never surfaced, scope applied per row,
  batched and per-node reads equal, writes refused, only built statements run. An engine run on
  sqlite + native kuzu (`tests/contract/test_graph_templates_engine.py`) ingests with a stand-in
  extractor, stores hierarchical communities with stats in kuzu, and a planner-selected template
  returns path provenance; an undeclared template abstains.
- **Not run:** neo4j template identity (`external`; no Neo4j test server was up), Leiden (Louvain
  dendrogram used instead; plan open decision 7).
- **Found and avoided:** Kuzu 0.11.3 `regexp_extract` returned a previous row's value for a
  non-matching row when combined with filters (non-deterministically). The attach adapter derives
  document ids in Python and runs no regex functions in Kuzu.
- Counts: `-m 'not external'` **1068 passed** (base 1031 passed, 1 skipped); bare `pytest -q`
  1071 passed, 124 skipped; ruff clean; golden index fingerprints unchanged (new community recipe
  keys enter the fingerprint only when set).

**Private parity measurement (aggregate only; store, mapping and questions stay outside the
repository).** A copy of an earlier service's Kuzu knowledge-graph store (~0.96 GB; ~600k nodes,
~1.4M relationships) was attached read-only through a private mapping (28 node tables, 30
relationship tables, communities). For the 36-question parity set, the graph queries the earlier
service actually executed in a recorded run (frozen plans; $0, no model call) were replayed as typed
templates through the package graph arm against the package catalog of the same corpus:

| | earlier service (recorded) | package, attached store |
|---|---|---|
| questions with graph queries | 12 | 11 replayed (1 used model-written Cypher: not replayable by design) |
| questions with graph evidence | 11 | 8 |
| graph evidence documents | 46 (as delivered) | 1,116 distinct (1,123 paths, all edges with quote + provenance; all in the package catalog) |
| overlap with the earlier evidence documents | — | 14 of 46 (4 questions) |
| retrieval targets reached through graph evidence | 0 | 1 (already in the package dense/lexical pool; 0 added) |
| community evidence | 48 unverified summaries (24 questions) | 0: the store's communities carry no quote and no entity members, so no grounded claim exists |

Read-out: graph evidence is now measurable through the package. Entity-neighbour and path questions
reproduce (overlap on 4 of 7 such questions); the 5 symptom/error "troubleshoot" replays (4 questions) return
nothing because the package resolves seeds by entity-name tokens while the earlier service matched
substrings of symptom descriptions; community summaries are not used because they are unverified.
Latency of the replayed graph steps: p50 1.0 s, max 53.5 s (a two-hop `connect` through hub nodes).
This is evidence about evidence volume and overlap only, not answer quality or relevance.

## Lexical `bm25` engine and request-path latency — 2026-11-04 (branch `worktree-agent-a0dd61f541791a8d6`, slice parity/lexical-latency)

Evidence levels: **synthetic** (unit, contract and conformance tests) and **measured on a private
corpus** (sanitised aggregates only; corpus, queries and per-query results stay outside the
repository). No paid model call was made.

- **Synthetic.** `rank_bm25.BM25Okapi` parity on a seeded 240-row Zipf corpus (words in more than
  half of the rows, so the idf epsilon floor applies; a word in exactly half; repeated and unknown
  query tokens): every score vector is bit-identical (`np.array_equal`; max |Δ| ≤ 1e-9 asserted),
  the idf table and `average_idf` are equal, and the ranked list is every row containing a query
  token by (−score, chunk id). The conformance kit passes on `sqlite_bm25` (and on `sqlite_faiss`
  with the new bitset scope). Tests: `not external` 1032 → 1066 passed; ruff clean; the 2.0.0a1
  golden index fingerprints are unchanged (the engine keys enter the recipe only with `bm25`).
- **Measured, private support corpus** (58.7k records, 521k chunks, 1024-d precomputed vectors,
  CPU only, shared host with load average ~15), 630 labelled queries replayed with frozen planner
  queries against the earlier recorded service lists (paired bootstrap 10k):
  - FTS5 path after the latency changes: all 630 lexical, dense, RRF and union lists are identical
    to the recorded package run (the latency work changes no ranking).
  - `bm25` alone (`ascii_alnum`, k1 1.5, b 0.75, ε 0.25): lexical R@10 0.668 / MRR 0.383 vs the
    service 0.698 / 0.426 — no better than FTS5 (0.676 / 0.389). The scorer was not the gap.
  - `bm25` + `verbatim_boost` + `pin_identifiers` (defaults) with front records kept first under
    `source_slots`: lexical R@10 **0.703** / MRR **0.426** (Δ +0.005 [−0.011, +0.022] /
    +0.000 [−0.018, +0.019]); fused RRF R@10 **0.837** / MRR **0.639** vs 0.826 / 0.645
    (Δ +0.011 [−0.000, +0.023] / −0.006 [−0.021, +0.008]). Without the `leads` ordering fix the
    same policies gave 0.672 / 0.384 (the slots re-ordered the front records).
- **Latency** (warm p50 / p95, retrieval only, query embedding on CPU included):
  - before (FTS5, 2 threads): 1.21 / 2.47 s — encode 0.39, vector search 0.24, two arm collapses
    0.24, FTS5 search 0.72 s;
  - after, `bm25`, 2 threads: **0.74 / 1.32 s** — encode 0.44, vector search 0.16, collapses 0.12,
    lexical search **0.028** s; 8 threads (630 queries): **0.41 / 1.03 s** (dense step 0.34,
    lexical step 0.11); excluding the query encode, ~0.25–0.29 s p50;
  - the FTS5 path with the same request-path changes: 1.10 / 2.32 s (2 threads) — the FTS5 query
    itself (0.88 s) stays the bottleneck.
  - **Target p50 ≤ 0.3 s not met** with the query encoder on CPU (0.19–0.44 s in-process). The p95
    tail is Python's full garbage collection over the ~0.5M objects the transformer stack loads
    (0.5 s per full collection); `gc.freeze()` after warm-up in the serving process cut the arm
    collapse p95 from 0.63 to 0.17 s in a 60-query check. The package does not do this itself.
- **Not established.** Relevance on other corpora; the policies' effect without `source_slots`;
  `bm25` build cost beyond this one ingest (886 s and peak RSS 10.4 GB, against 833 s and 10.4 GB
  for the FTS5 build of the same corpus; the segment held 23.4M postings and 229k terms);
  concurrent-request latency.

## Pattern extractor and deterministic pattern learning — 2026-11-04 (addendum A1, slices L2 and L4a)

Evidence level: **implemented, synthetic and contract-tested only.** No real log data, no source
cluster and no model provider was used, and nothing was measured on a non-synthetic corpus.

- **Suite.** `ruff check src tests` is clean. `pytest -m 'not external'` gives:
  - **1293 passed, 73 skipped** with the repository venv, which has no `google-re2`, so the RE2-parametrised cases
    skip;
  - **1366 passed** with `google-re2` 1.1.20251105 added from an isolated `--target` directory in the
    external workspace (the shared venv was not changed).

  The base at `9ba767f` gave 1189 passed.
- **Engine behaviour checked by tests**:
  - RE2 refuses backreferences and lookaround;
  - spans are character offsets on non-ASCII text, and `\w` is ASCII under both engines;
  - `Set.SearchSet` selects candidates, and only those are searched;
  - `(a+)+$` over 5 000 characters is linear under RE2;
  - a `regex`-engine timeout is typed and recorded as non-deterministic;
  - a missing engine is an error, with no fallback.
- **Lint**: a 13-pattern synthetic ReDoS corpus covering the four rules, the RE2-subset refusals, and
  every load-time lint class, including the `$attribute` rule.
- **Contract (sqlite write path)**:
  - approving a pattern makes the next build re-extract (0 reused) and adds its relation, while the
    index fingerprint stays unchanged;
  - deprecating the pattern removes the relation;
  - a library edit and restart keep `current()` serving;
  - `pattern_provenance` and the snapshot are recorded;
  - a lint error fails the build before any write;
  - the golden fingerprint test is unchanged and green.
- **Bootstrap (synthetic generator, 700 lines, 7 templates, 4 services)**. Coverage went from 0.143
  (one approved message pattern) to 1.0 with 24 synthesised proposals: 6 templates × 4 services, keyed
  per `(service, level)`. There were no rejections and no evictions, and every proposal is lint-clean.
  This shows the miner and synthesiser work on a fixture designed for them. It says nothing about
  coverage on real logs, which is an L0/L4 benchmark item.
- **Not established**: throughput per engine, the miner on public log corpora, and gate precision
  against real drift. The LLM proposer (L4b) is not built.

## Production-workload parity, final paired run — 2026-11-04 (2.0.0a3, commit `cec3ffb`)

Evidence level: **measured on private corpora** (aggregate only; corpora, questions, gold, prompts,
configs and per-query results stay outside this repository). Two previously served workloads were
replayed through the package on one CPU host; the old services were not called. The old side is the
services' own recorded results, with their own gold sets, metric definitions and denominators.
Answers on both sides were graded blind by the same small model judge (`claude-haiku-4-5`), each
alone under a random id. Paid model spend is capped; nothing in scope was skipped for budget.

**Corpus A** (support corpus: ~59k records / 521k chunks, four source types; imported 1024-d
vectors, alignment-audited; sqlite `lexical_engine: bm25` with `verbatim_boost` and
`pin_identifiers`; per-arm 20 with `source_slots` and `source_order`):

| Measure | Old | Package | Verdict |
|---|---|---|---|
| Frozen retrieval set (630 queries), dense R@10 / MRR | 0.673 / 0.357 | 0.678 / 0.384 | met |
| Lexical R@10 / MRR | 0.698 / 0.426 | 0.700 / 0.424 | met (CI includes 0) |
| Fused (RRF) R@10 / MRR | 0.826 / 0.645 | 0.834 / 0.637 | met (CI includes 0) |
| Diagnostic set (27 targets), hits @20 / @10 / pool | 12 / 12 / 15 | 12 / 9 / 13 | @20 met; @10 and pool not met |
| Retrieval latency p50, CPU, warm (frozen plan, in-process) | 0.10–0.27 s | 0.49 s (8 threads, `gc.freeze`) / 0.83 s (2 threads) | not met (CPU query encoding ~0.4 s; lexical 0.24 s) |
| Answer latency p50, planner + generator | 14–20 s | 16.9 s | met |
| Answers fully solving (36, rubric), newer planner + newer small generator | 12 | 6 | not met |
| Same, frozen planner output + newer small generator | 12 | 8 | not met; most of the loss is the generator model |
| Same, frozen planner output + the old generator model (2.0.0a2 run) | 12 | 15 | met |
| Graph evidence through a read-only attached Kuzu store | 12 questions, 46 documents, 0 targets | 36 questions, 6,232 documents, 1 target (0 beyond dense/lexical) | functional parity met; relevance difference not measurable |

**Corpus B** (ticket analytics: ~1.7k records; 100 owner questions with expected answers; old served
answers exist for 45):

| Measure | Old | Package | Verdict |
|---|---|---|---|
| Correct, questions with old answers (45) | 42 | 18 | not met |
| Correct, remaining questions (55) | not recorded | 4 | not measurable against the old service |
| Deterministic `rules` path (no model): exact counts correct / exact but wrong | – | 18 / 4 | the 4 are silent wrong "exact" numbers |
| Answer latency p50 | 10.3 s | 7.3 s | met |

**Gaps found** (classified):
- **Package:**
  - native `output_format` for the planner schema is rejected by the provider (grammar too large); prompt mode was used;
  - `answer_number_mismatch` treats a cited denominator as the stated count (10 abstentions with correct numbers);
  - the rules grammar returns exact aggregates while dropping qualifiers such as "second", mention filters and "active";
  - planners sort by the measure name, which the compiler rejects;
  - the vector-import spot check cannot use the producer's input template;
  - lexical/encode latency is above the old service.
- **Configuration:** ranking fields were not declared `sortable` for corpus B; corpus B used the FTS lexical engine.
- **Model change:** the newer small generator declines more often (fewer unsupported claims, fewer solved).

Corpus B's dense embedding model differs from the served one (an owner decision).

## LLM pattern-learning loop and index-name grammar — 2026-11-04 (addendum A1, slice L4b and the L1 parser fix)

Evidence level: **implemented, synthetic and mocked only.** Every model reply came from a scripted
fake provider served through `httpx.MockTransport` (the real `openai` / `openai_compatible` provider
code, metering and structured-output paths, but no network, no key and no spend). No real model, no
real log data and no source cluster was used. Nothing here measures proposal quality.

- **Suite.** `ruff check src tests` is clean. `pytest -m 'not external'` gives:
  - **1849 passed, 91 skipped** with the repository venv (no `google-re2`; the RE2 parameters skip);
  - **1940 passed, 0 skipped** with `google-re2` 1.1.20251105 from the isolated `--target`
    directory in the external workspace.

  The base `a27ab5a` gave 1778 passed, 74 skipped (repository venv).
- **Egress (tests):** plain lines, and lines redacted under another privacy configuration, are refused
  before any request; a residual IPv4 shape refuses the run (not dropped); a config without redaction
  is refused; a hosted role without `allow_hosted` is refused at config time and at run time; records
  from a `LogMapping` without a redactor are refused. In the request bodies no synthetic e-mail, IP,
  host or tenant value appears, only `[REDACTED_*]` markers and per-run aliases. That test first
  found the cluster template carrying a host value; the loop now pseudonymises before mining.
- **Gate (tests, both engines):** each rejection class through the loop, with nothing written:
  invented example id, matching negative (lint), over-broad (`out_of_cluster`), partial cluster
  (`cluster_coverage`), stealing an approved pattern's lines (`no_regressions`), ReDoS lint, too few
  literals, too little support, backreference; novelty against a human-disabled pattern; a fix of a
  disabled target is invalid; a fix that drops target positives fails `fix_target`.
- **Promotion and writer (tests):** `manual` keeps passing proposals in `proposed.yaml` (coverage
  0.16 before and after, 0.88 if approved, on a 25-line fixture); `auto` on RE2 learns both with
  `approve` records and the reparse reaches 0.88; `auto` on the `regex` engine waits for a human;
  `propose` never promotes; a fix of a learned pattern is a `supersede`; a fix of a human pattern
  waits and a human promote supersedes it. `library.yaml` is byte-identical in every case. A human
  save between read and rename is retried and kept. Two runs with the same fake replies give
  byte-identical files and reports. Git: only the changed system file is committed, with author
  `system:pattern_learner`; uncommitted hand edits stop the writer.
- **Spend (tests):** run and day call caps; a USD run cap refuses before the request; committed spend
  is the reported usage ($0.52 per call at the test price); the day ledger persists across runs and
  refuses the next run; two ledger handles on one file reserving concurrently grant exactly the cap.
- **Index-name grammar (tests):** synthetic deployment-prefixed and numbered infrastructure-prefixed
  names, and underscored service names, parse; undeclared prefixes, ambiguous readings, missing or
  invalid dates and vocabulary misses are `unparsed` with typed reasons.
- **Not established:** proposal quality, cost per day or coverage gain on real logs with a real
  model; the general spend ledger on other ledger backends (B10); raw-reply tagging by partition.

## Native aggregation pushdown for the Lucene records facets — 2026-11-04 (branch `v2/logs`, base `a27ab5a`)

Evidence level: **real-service on throwaway containers** (OpenSearch 2.19.1, OpenSearch 3.9.0 and
Elasticsearch 8.17.4 from `tests/services/docker-compose.yml`), **synthetic data only**, plus
fake-transport unit tests. No paid call; no private or production data; nothing was measured on a
non-synthetic corpus or on a multi-node or security-enabled cluster.

- **Suite.** `ruff check src tests scripts` is clean. `pytest -m 'not external'` gave **1804 passed,
  73 skipped**; the base gave 1778 passed, 74 skipped. The default stays `records.aggregation: client`,
  and the golden index fingerprints are unchanged because `records` is excluded from the backend recipe.
- **Conformance, both modes** (`tests/conformance/` with `-m external`):
  - the records kit runs natively on the new `opensearch_native` and `elasticsearch_native` adapters;
  - `test_lucene_native_aggregation.py` compares client and native on 176 specs × {no ACL, ACL} =
    352 `ScopeResult`s per server. It covers every measure alone and grouped, cross-tabs with nulls,
    calendar groups in `Europe/Berlin`, ranking with ties, periods (one with an unparseable bound),
    numerators (one unsupported), the three distinct-count units, the record list over typed,
    mixed-type and absent fields, negated-state null counts, and unsupported text;
  - all 352 results were **identical** (model equality and JSON dump) on each server;
  - the ACL check: a tenant-hidden record with a marker in every field never appears in any
    native aggregate, group, tie, id or list;
  - the caps check: `matched_ids_cap` gives a truncated, flagged list; `max_groups` gives a bounded
    result; above `distinct_exact_cap` the result is `approximate` with the reason in `basis`.
  - Runs: OS 2.19.1 + ES 8.17.4, 59 passed; OS 3.9.0 (`SU_TEST_OPENSEARCH_MAJOR=3`), 29 passed. The
    skips are other services and the env-gated scale test.
- **Scale** (`scripts/bench_native_aggregation.py`, 200 000 seeded synthetic log records, one
  records index, ACL on 10 of 20 tenants, so about 99.6k records match; median of 3; shared host; client path
  with `max_matched` raised so it can answer; ingest 9–12 s per server). Latency in seconds,
  client → native (default `matched_ids_cap` 10000); the last column is native with
  `matched_ids_cap: 100`:

  | Question (matched) | OS 2.19.1 | OS 3.9.0 | ES 8.17.4 | native, ids cap 100 (OS2 / OS3 / ES) |
  |---|---|---|---|---|
  | count (99.6k) | 2.49 → 0.065 | 2.53 → 0.065 | 2.44 → 0.041 | 0.002 / 0.002 / 0.002 |
  | count errors (16.8k) | 0.31 → 0.067 | 0.30 → 0.070 | 0.29 → 0.046 | 0.002 / 0.002 / 0.003 |
  | group by service | 2.58 → 0.067 | 2.61 → 0.071 | 2.27 → 0.047 | 0.004 / 0.003 / 0.006 |
  | top 5 services by errors | 0.33 → 0.071 | 0.32 → 0.079 | 0.26 → 0.047 | 0.004 / 0.003 / 0.004 |
  | cross-tab service × level | 2.55 → 0.069 | 2.59 → 0.075 | 2.34 → 0.056 | 0.005 / 0.005 / 0.006 |
  | errors per day | 0.33 → 0.070 | 0.35 → 0.073 | 0.28 → 0.049 | 0.003 / 0.004 / 0.004 |
  | avg latency by service | 2.58 → 0.071 | 2.66 → 0.069 | 2.69 → 0.044 | 0.004 / 0.004 / 0.004 |
  | distinct users (20k values, exact) | 2.55 → 0.086 | 2.62 → 0.093 | 2.29 → 0.066 | 0.023 / 0.022 / 0.024 |
  | week over week | 2.74 → 0.069 | 2.82 → 0.072 | 2.44 → 0.042 | 0.003 / 0.003 / 0.003 |
  | error share | 2.51 → 0.067 | 2.61 → 0.067 | 2.17 → 0.042 | 0.002 / 0.002 / 0.002 |
  | sum latency | 2.52 → 0.066 | 2.61 → 0.067 | 2.28 → 0.041 | 0.002 / 0.002 / 0.002 |
  | last seen 5xx (19.9k) | 0.68 → 0.072 | 0.68 → 0.072 | 0.32 → 0.046 | 0.003 / 0.003 / 0.003 |

  - Speed-up: 4–61× at the default ids cap, mostly the fetch of 10 000 ids, and up to ~1000× with the
    cap at 100.
  - The client cost grows with the matched count, while native stays roughly flat.
  - Every result **agreed** (equal, with float sums within a relative 1e-9; the native id list is the
    prefix of the client list and flagged truncated).
  - Every result was bit-identical except "avg latency by service" on OS 3.9.0 and ES 8.17.4. Those
    averages of non-integral doubles differ in the last ulps: the engine sums in its own order. This is
    a documented limitation.
- **Found and fixed by the scale run.** On OpenSearch 3.9.0 the count-ordered `terms` path returned a
  wrong top 5 (363 for a true 373, and one group missing). The cause: 3.x searches even a one-shard
  index in concurrent segment slices (`search.concurrent_segment_search.mode: auto`). The reply
  reported `doc_count_error_upper_bound` 648 at size 16, and the 352-spec fixture is too small to
  trigger it. The adapter now trusts a terms reply only when the engine reports error 0, grows the
  request otherwise, and gives a bounded result past `max_groups`. After the fix the 200k run is
  identical on 3.9.0. The regression is covered by a unit test and by the env-gated
  `tests/services/test_native_aggregation_scale.py` (`SU_TEST_AGG_SCALE_DOCS=200000`: 2 passed on
  OS 3.9.0 and ES 8.17.4).
- **Not established**:
  - multi-shard or multi-node indices (the adapter creates one-shard indices; terms ranking relies on
    the engine's error report, not on the shard count);
  - security-enabled clusters;
  - corpora above 200k records;
  - concurrent-request latency;
  - `percentiles` (not in the measure contract);
  - DST-gap zones for calendar buckets (the engine and Python may label a bucket whose local
    midnight does not exist differently; not tested).

## Web UI MVP (read-only) — 2026-11-04 (addendum A2 §4.4, branch `v2/logs` base `106d4b0`)

Evidence level: **synthetic + mocked model**. No real service, no real model, no customer data.

- **Python (mount and guards)**: `tests/contract/test_ui_mount.py`, 21 tests. A synthetic bundle (and
  the real built bundle when present) is served at `/ui` with the strict CSP and security headers;
  assets are public, while every `/v2/*` route the UI calls answers 401 without the bearer token;
  path traversal outside the bundle is a 404; `Host: attacker.example` gets 421 and a foreign
  `Origin` on a write or on the answer stream gets 403; `nosniff` is on `/v2/*` responses (errors
  included); `ui.enabled` without auth, without the wheel or with an empty bundle refuses to start;
  `serve` resolves host and port from `serving.http.bind`; the core distribution excludes `ui/`.
- **Frontend (vitest, jsdom)**: 40 tests. The XSS corpus (`<script>`, `<img onerror>`, `<svg
  onload>`, iframes, `javascript:`/`data:`/`vbscript:` URLs with case and whitespace tricks,
  markdown-image and link tricks, `<style>`, ANSI escapes, RTL overrides, a 20 000-character token)
  rendered through hit cards, every evidence tab, aggregate cards and the trace panel: no script,
  iframe, image or style element, no `on*` attribute, no non-http(s) `href`, `window.__xss` never set.
  A source guard rejects raw-HTML APIs and `localStorage`/cookie use. Also the SSE parser (chunk
  boundaries, CRLF split across chunks, heartbeats), typed error mapping, contract-shape rejection, a
  missing terminal event, token storage policy, feature detection, chart series (no coercion of
  non-numeric values), graph construction (no edge between co-mentioned entities; server edges to
  unknown nodes dropped) and trace spend (unpriced calls counted, not summed).
- **Browser smoke (manual harness, Playwright chromium, not in CI)**: `ui/e2e/serve_synthetic.py`
  built 2 synthetic log days with `LogsPipeline.run_daily` (fake cluster), served them with the
  loopback `FakeChatServer` as the streaming generator under bearer auth, and `ui/e2e/smoke.mjs`
  entered the token, streamed an answer (contract `passed`, citation jump), opened the evidence, graph
  and trace tabs, charted `group_count` by service (structured arm, exactness `exact`) and explored the
  entity `redis` in the graph explorer: **0 CSP violations, 0 page errors**, 6 screenshots (kept in the
  external workspace). The first run found cytoscape injecting an inline `<style>`; it is now
  pre-empted (rule in the bundle CSS), with no CSP relaxation.
- **Not established**: Playwright in CI, accessibility audit (axe), behaviour on a real deployment or
  a real model, the A2 inspect routes (`/v2/info`, graph neighbourhood, communities, patterns, spend,
  runs), log-intent views, `aggregates[].rendering` sentences (shown when present; the L5 slice adds
  them), and the large-graph layout.

## Accounted coverage and LLM fallback extraction — 2026-11-04 (addendum A2 rev 2–4, slices C1–C4; branch base `fdb7c4a`)

**Evidence level: synthetic data and mocked providers only** (`httpx.MockTransport` on the
OpenAI-compatible and OpenAI wire formats). No real model was called, no paid key was used, and no
real log was read. No coverage number below is a deployment result.

- **Accounting** (`tests/contract/test_fallback_daily.py`, 7 tests, real puller over the fake log
  cluster): with `multiline.max_lines: 2` the pulled documents include joined, overflow and fragment
  documents, and identities Q, D and H hold for every unit. A source wrapper that hands over one
  document fewer than the puller counted refuses the seal (`AccountingImbalance`, identity Q); the
  version stays open and `logs coverage` reports `ok: false`. A missing head fails identity H. The
  exact, `hourly_quota`, partition-capped and `random_score` pulls all balance (probe in the
  external workspace).
- **Fallback** (`tests/unit/test_fallback_extraction.py`, 27 tests; `test_fallback_realrun_fixes.py`,
  10 tests), on an 18-head synthetic day:
  - labelled clusters become `provisional` with `line[start:end] == value` for every value;
  - invalid labels (unknown group, wrong type) and a regex citing an unsupplied line are rejected by
    the gate;
  - the field-gap `fix` passes `fix_target` and keeps pattern values; the declined-fix line tier
    asks only for the gap;
  - hallucinated quotes, quotes containing a pseudonym and unknown reply ids are dropped;
  - an out-of-range value is `range_invalid`;
  - day 2 with new line ids makes **0 cluster calls** through the cache and is re-gated;
  - a pending proposal is reused without a call, while a changed template gets one;
  - the call cap gives `budget_deferred` and the retry resolves it;
  - no route gives `capacity_deferred` and the retry resolves it;
  - a down local route is skipped;
  - the paid route is unused by default and stops at the provider total cap;
  - a paid route without finite daily caps is refused at load;
  - an e-mail or tracking-id line is `egress_refused` on its own, and the run is refused above the
    share;
  - the stop file pauses between calls;
  - the report is persisted per cluster and stage;
  - two runs with the fixed mock give byte-identical overlay content;
  - the overlay keeps H, and `rows()` returns the same accounting.
- **Daily path**: `run_daily` runs `fallback` after the seal and writes an overlay. A failed
  fallback leaves heads `fallback_pending` and compose still builds. `apply_decisions` (RE2, auto
  promotion) reparses once into v2 and rebuilds the overlay with **0** calls.
- **Throughput**: per-route `calls_per_min`/`tokens_per_s` is measured around each call and reported
  per run. In the harness it measures the mock transport only and says nothing about a model; the
  real measurement belongs to the deployment.
- **Totals**: 44 new tests. The full suite (`-m 'not external'`) gives 2253 passed and 1 skipped
  (a base test that skips when google-re2 is installed), with `ruff check src tests` clean, on the
  merged base `fdb7c4a`.
- **Not established**: any real-model behaviour (label quality, regex quality, reply validity rate,
  cost per day); compose reading overlays (the L5 arm mapping); `ScopeResult.applied_extraction`;
  `learning.bootstrap` with measured throughput and `learn measure-throughput`; LAN tier and
  `egress.allow_lan`; time windows and invalid-reply limits per route; the `serving.answers` spend
  scope; a counted `rest` for identity S; the 11-day gold set (E1).

## Web UI follow-up: inspect routes, pattern review, ops and log views — 2026-11-04 (A2 S1a/S1b, U2, U3)

Evidence level: **synthetic + contract fakes**. No real deployment, model or customer data.

- `tests/contract/test_inspect_routes.py` (9): on a 2-day synthetic logs generation, `/v2/info`
  reports window, date basis, timezone, served days and one library identity; run reports are
  content-free and newest first; spend is `available: false` without a ledger and exact (micro-USD)
  with one; the patterns view equals the generation snapshot even after the working `library.yaml`
  is overwritten; the review queue lists synthetic noise proposals with examples, matched lines,
  gate checks and effective status, and leaves every library file byte-identical; the graph
  neighbourhood on this generation names the seed and draws no edge (the fixture has no relations),
  and against a contract fake store it returns only stored edges with bounded, deduplicated spans;
  missing communities are reported, not zero; `inspect` scope and the single-tenant 403 hold.
- `tests/contract/test_ui_forwarder.py` (2): a forwarder name passes the Host list on any port and
  its Origin is accepted when Host is rewritten; look-alike hosts, other ports on loopback, `null`
  and non-http origins are 403; unknown Hosts 421.
- vitest 54: review view (XSS strings inert in examples, gate details and reasons; copied command
  equals the shlex-quoted builder), served/archived days and multi-library note, schema-driven tool
  forms (no coercion), tool output → aggregate card with the server's `rendering`.
- Playwright smoke: review queue, runs & spend and communities pages added; 0 CSP violations.
- **Not established**: the real L5 log tools (not on this branch; the Logs view is hidden without
  them), real proposals at the owner's scale (63 noise proposals: rendering cost not measured), the
  rev 3 fallback-state/tier/burn panels (data not present), axe accessibility.

## Fallback rev 4: route tiers, bootstrap, throughput, answers scope, E1b gold set — 2026-11-04 (merged on `dacd166`)

**Evidence level: synthetic data and mocked providers only.** The gold set uses an oracle model built
from the generator's truth. It measures the machinery (clustering, gate, grounding, caps, backlog,
egress), not a model. Throughput figures time the mock transport.

- **E1b gold set** (`tests/contract/test_fallback_gold.py`, 4 synthetic days, 6 planted templates,
  3 singletons, 4 field gaps, 3 injection lines and 2 PII/tracking lines per day):

  | Run | Slot precision | Slot recall | Values span-grounded | PII/tracking refused | Injection patterns | Backlog (deferred clusters, heads) per day |
  |---|---|---|---|---|---|---|
  | no cap | 1.0 | 1.0 (268/268) | 300/300 | 8/8 | 0 | 0,0 all days |
  | cluster cap 4 | 1.0 | 1.0 (268/268) | 300/300 | 8/8 | 0 | (3, 13) on day 1, then 0 |
  | model obeys the injection (`^.*$`) | 1.0 | 1.0 | 300/300 | 8/8 | 0 (gate refuses) | 0 |

  The gold set found a ranking defect. The cluster cap counted clusters that cost nothing (pending
  proposals, cached replies), so the same top clusters filled the cap every day and the deferred
  ones never got a slot (backlog grew 13 → 64 heads over 4 days). The cap now counts only clusters
  that need a call.
- **Routes** (`tests/unit/test_fallback_routes_bootstrap.py`, 13 tests):
  - `lan` needs `allow_lan`, both at config load and at run time before any call;
  - a `lan` route takes work after a down local route, at USD 0;
  - `local` must be loopback;
  - a route outside its UTC window is skipped;
  - a route is marked down after its invalid-reply limit.
- **Bootstrap**:
  - off by default;
  - an unmeasured start is refused;
  - reaching a paid route without a finite `learning.bootstrap.max_usd_per_day` is refused at load;
  - `measure-throughput` (2 calls on the mock) records `throughput.json`, writes nothing to the
    library, and its replies make the next run's clusters free;
  - the measured cap binds clusters that need a call;
  - paid routes are skipped during bootstrap.
- **`serving.answers`** (`tests/unit/test_answers_spend.py`, 2 tests): two answer calls commit on the
  `serving.answers` day ledger and the provider total. The third is refused by the persistent day
  cap before its request, and the request budget is released. Config load refuses a paid answer role
  whose provider has a lifetime cap without a finite answers cap.
- **`patterns learn` run control** (`tests/unit/test_pattern_learning_run_control.py`, 3 tests):
  - the stop file pauses between calls (not a failure);
  - `--output` progress is written per cluster;
  - pending templates are not re-asked.
  The existing day-cap test now sees run 2 skip the two pending templates, and the persisted day cap
  is still asserted in run 3.
- **Merges**:
  - `42627b0`: snapshot round trip through `OverlayStore.rows()`; the snapshot holds row aggregates,
    never field values.
  - `dacd166`: match rows keep L5's `pattern_owner`/`pattern_reviewed`/`gaps`, and the accounting
    keys are `account_owner`/`required_gaps`.
- **Overlay selection**: a second full run under the `regex` engine found that `OverlayStore.rows()`
  without an identity broke ties on `created_at` by identity text, so a rebuilt overlay could lose to
  the stale one under one clock value. It now breaks ties on the manifest's write time, with a test.
  The gold scorer reads the current identity.
- **Suite on the merged head**: `regex` engine (no google-re2 on the path): 2338 passed, 95 skipped,
  0 failed. With the isolated google-re2 path: 2457 passed, 1 skipped and 1 failed. The failure is `test_mcp_stdio_subprocess_and_mounted_streamable_http_agree`, whose
  subprocess environment drops that path. The same file passes 24/24 when parent and child use the
  same engine. `ruff check src tests` is clean.
- **Not established**: real-model label and regex quality, reply validity, cost per day and real
  throughput per tier (the deployment measures these with `measure-throughput`); compose reading
  overlays into rollups (`applied_extraction` from overlays); a counted `rest` for identity S.

## Logs branch integration: history tier merge and a fresh `[dev]`-only test run — 2026-11-04 (branch `v2/logs`)

Evidence level: **synthetic / contract** (test-suite runs only; no service, no paid call).

- Merge of `v2-logs-h1` (H1/H2 and latency, built from `dacd166`) into `v2/logs` at `312e314`: no
  textual conflicts; the fallback slice's `account_owner`/`required_gaps` row keys and L5's
  `pattern_owner`/`pattern_reviewed`/`gaps` both stay. Regenerated `docs/config.schema.json`: no change.
  Full development venv (networkx, regex, fastapi, uvicorn, mcp; no google-re2), `-m "not external"`:
  **2355 passed, 95 skipped, 0 failed**, identical skip set with and without the test fixes below.
- Fresh venv with `pip install "<git archive of HEAD>[dev]"` (core + dev only), bare `pytest -q` from
  the archive directory: before the fixes 59 failed and 89 errors (at `312e314`, as reported); after
  them **1791 passed, 651 skipped, 0 failed, 0 errors**. The skips are tests that need an optional
  extra (communities, a pattern engine, api, mcp, backend clients); the ConfigError for a logs
  profile without networkx and the typed `PatternEngineUnavailable` are unchanged.
- Limitation: the fresh run covers what the core package does without extras; everything behind an
  extra is covered only by the full-venv run (and google-re2 paths by neither).

## Web UI design pass: theme, live timeline, charts, overview — 2026-11-04 (A2 U4-design)

Evidence level: **synthetic + mocked model** (the 10-day synthetic gold set, the loopback fake
streaming model). No real deployment, model or customer data; no paid calls.

- `tests/contract/test_ui_theme.py` (20): default and deployment themes become `theme.css` with
  custom properties only and an SVG logo with its own `default-src 'none'` policy; injection-shaped
  colours, font stacks, series, control characters, logo paths outside the theme directory and
  logos with script, event attributes, `foreignObject`, entities or external references refuse the
  start; the committed `ui/public/theme.css` equals the generated default.
- vitest 62: timeline steps advance only from events (no reasoning text written by the UI), an
  error marks the final step; inline citation chips; answer numbers with exactness and rendering;
  charts (sorting, ≈ hatching, line gaps for days without composed data, quantised heatmap,
  coverage numbers printed apart); log-row adapters; XSS corpus inert in every new component.
- Browser tour (`ui/e2e/serve_synthetic.py --fixture gold --theme …` + `ui/e2e/tour.mjs`, local
  Playwright chromium): overview, streamed log question, structured and lexical tabs, graph tab and
  explorer (stored `native_link` edges), log shortcuts, pattern review, runs: **0 CSP violations,
  0 page errors**. Screenshots and the reference-console comparison stay in the external workspace.
- **Not established**: the deployment theme on the real server, axe accessibility, large graphs
  (> 80 nodes fall back to a grid layout), the with-model coverage numbers (the gold set reports
  regex-only coverage; the gauge shows "model n/a").

## Real-deployment defect fixes: level answers vs source counts, regex length, stored examples, theme path, redactors, anchor and vocabulary, schema violations, index suffixes, planner context, Enter to submit — 2026-11-04 (branch `v2/logs`)

Evidence level: **synthetic and mocked** (the 10-day synthetic gold set through the real pipeline
with the fake source and the sqlite backend; scripted planners and loopback fake providers; jsdom
for the UI). The defects were seen on a real deployment; they are reproduced here with synthetic
data only, and none of the fixes has been re-run on that deployment. No model was called.

- `tests/contract/test_logs_gold_questions.py` (+8): the gold set now plants unparsed FATAL lines in
  its token-only service (2 + 1 on D9, 1 on D8). Level answers carry `source_counts` equal to a
  recount of the plan (source lines per service/tenant, unparsed share, basis and its exactness);
  D8 is answered "No FATAL-level lines in the parsed rows … bounded", never "none … exact"; fully
  parsed ERROR answers stay exact; a level without a count query is named in the notes; the source
  counts equal a pinned `unit_day` aggregate and a kind-less scope over `count_fatal` is still
  refused by the scope guard. `test_logs_surface_parity.py` now also compares `source_counts`
  across the decision modes, the tool call, HTTP and MCP (stdio and streamable HTTP).
- `tests/unit/test_bootstrap_synthesis.py` (+1 per engine): a 60-uuid template synthesises a regex
  over 4,096 characters; it is rejected as `regex_too_long` and the other cluster still yields a
  proposal.
- `tests/unit/test_pattern_learning_loop.py` (+3 per engine): bodies carrying their own host and
  tenant values (one in upper case) yield stored examples (proposed, learned and the decision copy)
  with aliases only, and the library still loads; a regex narrower than the aliases drops those
  examples and, with none left, writes nothing (scripted fake provider, no network).
- `tests/unit/test_config_cli_a2.py` (+2): a relative `serving.ui.theme` resolves against the config
  file from another working directory; `config validate` and `doctor` refuse a missing theme
  (`path: serving.ui.theme`); `rebase_paths` moves the theme with the config.
- `tests/unit/test_redaction_more_redactors.py` (new, 22 on the `regex` engine; RE2 is not installed
  in this venv, so the RE2 runs are not established here): phone numbers in seven international and
  keyed shapes and five near-misses kept (timezone offsets, versions, counts, 20 digits); person
  names in JSON, quoted and unquoted `key=value` text with key boundaries and a configured list;
  13–19 digit ids hashed and joinable, 12 and 20 digits kept; lists of record objects masked whole
  while scalar lists stay; product prose masked to the end of the line with later names kept
  visible, token-bounded and idempotent; config coupling of `product_prose`/`prose_products`;
  stamps change with the configured lists. The existing idempotence and safe-subset tests now
  include the new redactors.
- Vocabulary, error rate and the default day (synthetic): `test_log_intents_detect.py` (+12: failure
  words, named levels, error-rate phrasing, a level word inside a signature id, the error-rate
  scopes and refusal, the undated anchor vs a named window vs `as_of`) and
  `test_logs_gold_questions.py` (+2: error rate per service equals a recount of the plan; an
  undated question on the gold engine, whose latest day is far before the real clock, answers the
  latest served day and says so, while "yesterday" is `source_unavailable` and `bounded`). Two
  existing expectations changed by decision: "Top errors for svc-api" and "Errors for svc-api
  yesterday vs today" now resolve to level ERROR and FATAL.
- `tests/unit/test_index_name_grammar.py` (+4): a numeric service suffix becomes `deployment_id` with
  and without a service vocabulary and with a deployment prefix before the service; short digit
  runs stay in the service; the rule is off by default (synthetic names).
- `tests/contract/test_answer_stream.py` (+2, streaming and not): a loopback fake generator replies
  with a schema-violating answer; the stream ends with `done`, the evidence is returned with an
  abstaining answer, and one warning names `citations (list_type)` and the stop reason without the
  reply text (mocked provider over loopback HTTP).
- `tests/contract/test_logs_planner_context.py` (new, 6; scripted planner, no model call): "what are
  issues that need my attention" plans `log_top` with ERROR and FATAL in cascade and llm modes
  without a planner call; a phrasing the rules do not know reaches the planner in a cascade, whose
  payload carries the pack (arms, kinds, library entries incl. `process.crashed`, served days,
  vocabularies, all ten intents with input schemas, few-shot) and whose choice is compiled like a
  tool call; the pack is within its bound (about 11.7k characters on the gold set) and holds no
  API key value, key variable name, endpoint or line text; a tight bound cuts lists in order and
  names the cuts; a free scope from the planner is still refused (`intent_required`); a
  deployment few-shot file replaces the profile set and an invalid one is a `ConfigError` at load.
- UI: vitest 65 passed (+3 in `ui/src/test/keys.test.tsx`: Enter submits, Shift+Enter keeps the
  newline default, `isComposing`/keyCode 229 never submit; a Logs shortcut runs on Enter; the
  Patterns filter fields reload on Enter like the Reload button). Built with `npm ci` and
  `npm run build` in a copy under the external workspace; the rebuilt static bundle is committed.
  No browser run of this change.
- Full suite after all fixes: `not external` 2442 passed, 99 skipped, 0 failed (project venv, the
  `regex` pattern engine; google-re2 is not installed there, so RE2-only tests skip). Fresh
  `[dev]`-only install of the `git archive` of the branch head in an isolated venv (non-editable;
  the package imported from site-packages): 1814 passed, 563 skipped, 0 failed, 0 errors. ruff clean.
