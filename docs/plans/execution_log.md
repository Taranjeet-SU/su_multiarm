### 2026-11-04 — A2/L5-B7: fallback overlay into the arms — DONE (contract evidence)

Branch `v2-logs-overlay` from `4d5a652`.

**Done.** `log_rollups.overlay_aggregates` turns the sealed overlay's model-assisted heads into
content-free aggregate rows in the derived snapshot (`overlay.aggregates`): `provisional` and
`model_line` heads become `log_day`/`log_hour` rows with their status and signature (`unknown` when
the model named none) and feed `endpoint_day`/`exception_day` with their typed values; `parsed_filled`
heads feed only their model-filled fields. Every such row has `extraction_source: model_assisted`
(deterministic rows: `deterministic`), and per-field sources are counted (`fields`). `coverage_day`
adds `lines_provisional`, `lines_model_line`, `lines_parsed_filled` and `extracted_coverage`; the
deterministic `coverage` is unchanged. Model rows produce no graph edges (header links and the
history shard summaries skip them). Log intents: totals read deterministic rows only (they count
every head once); signature questions read `parsed` plus `provisional`/`model_line` rows;
`extraction: deterministic` restricts to deterministic rows; the rendering states the model-assisted
share when `applied_extraction` includes it. The compose identity now includes the window days'
snapshot identities, so a new overlay recomposes.

**Deviations.** `log_affected` reads `log_day` (which has the model rows) instead of `signature_day`.
`signature_catalog`, `signature_window` and `graph_link` stay deterministic. A provisional row's
exactness and scale follow the deterministic rows of its unit and level.

### 2026-11-04 — A2/H1 + H2: history tier and lineage — DONE except the overlay into the arms (synthetic/contract; OpenSearch real-service for the history rows)

Branch `v2-logs-h1` from `dacd166`.

**Done.**
- `ingestion/history.py`: content-addressed append-only day shards (`<day>:<sha>` over the day's
  documents; a `source_key` (snapshot identity + recipe) map makes unchanged days O(1) and a reparse
  that changes no document a no-op); sqlite store (rows + EAV fields, indexed kind/day/shard) and a
  records-backend store (OpenSearch: one shard-tagged collection, `terms` shard filter, delete-by-query
  gc) with the shard index in a sqlite sidecar; `logs.history.tier`, `logs.history.backend`.
- Compose with the tier: day documents go to shards (only new shards written), the generation is the
  window shard (signatures, examples, window rollups, catalog, endpoint window, incidents,
  `graph_link`, `signature_history`, `signature_lineage`) and lists `logs.history.shards`; the
  catalog, links and history are built from shard summaries (no row reads for old days); gc by
  reference after every compose. Imports from the bug-fix slice's derived snapshots (formats 1, 2).
- Reads: `Engine.records_execute` routes day kinds to the shard set of the pinned generation (log
  tools, `aggregate` tool, structured arm, `documents_in`); a scope mixing day and window kinds is
  refused (bounded). Graph: `graph_link` edges carry the history; per-day weights from the shards,
  bounded to `days` (default 30) from the generation's last day.
- H2: `signature_lineage` from supersede decisions (the replay's `id` supersedes `target`),
  `signature_history` per (signature, library identity, imported days included); log tools expand a
  signature filter to its predecessors and fold rows into the successor, rendered "folds superseded
  signatures: a -> b".
- Latency: `log_compare` reads the union of its periods only; the population is the referenced
  shards' row counts. Numbers in VALIDATION.

**Deviations.**
- The window shard is the generation's own build (not a separate store object).
- OpenSearch history rows keep typed values only; `documents()` rebuilds day documents from them
  (metadata, no header text).
- Graph weights are bounded to the last 30 days by default (`graph_neighbourhood.days`); the
  edges cover the whole history.
- The sqlite 180/10-day ratio is over 2x for four log tools (2.1–2.25x) and for the graph
  neighbourhood (3.2x); OpenSearch is within 2x. See VALIDATION for the data-volume confound.
- `signature_lineage` uses the current library's decisions for every served day (a decision's
  `decided_at` is kept on the row); per-day replays of older library versions are not stored.

**Not done / follow-ups.**
- Overlay into the arms (`model_line` -> `unknown` bucket, `extraction_source` per field): waits for
  the fallback slice's merge.
- Elasticsearch history rows were not run; the 4 GB / 10 min append bound was measured at 180 days
  of the gold generator only (0.66–0.79 GiB, 1.6–12.9 s).

### 2026-11-04 — A2/L5a–L5d + M1 + B2+B3: logs analytics profile, log intents and tools, MCP log tools — DONE except H1/H2 and overlay reading (synthetic/contract evidence)

Branch `worktree-agent-af419a9d91385c4e2`, base `106d4b0`, merged `fdb7c4a` and `42627b0`. Built to
A2 rev 1, then the rev 2/3/4 deltas (§11.0, §11.1, §11.R4) as they arrived.

**Done.**
- Arm mapping (profile `logs_example`): dense = `signature` (masked template, regex in metadata) and
  `incident`; lexical = signatures, per-tenant `signature_examples`, incidents; structured = `log_day`,
  `log_hour`, `endpoint_day`, `endpoint_window`, `exception_day`, `coverage_day`,
  `signature_catalog` + A1 kinds; graph = header links (`emits`, `runs`, `serves`, `raises`) with
  span provenance and per-day weights, reviewed-entry facts, `co_fires_with` (`co_firing`);
  communities = per-tenant co-firing incidents (`communities.strategy: declared`).
- Partition format 2 (typed values with spans and gaps, `pattern_owner`/`pattern_reviewed`,
  `deployment` + exactness in cells, analytics aggregates, `patterns.json` digest); compose reparses
  an older format. Delta reparse with change classes A–H (`ingestion/reparse.py`).
- B2+B3: `FieldSpec.role`, `FieldSpec.additive`, `applied_basis`/`applied_extraction`, `$measure`,
  `bucket.of`, `hour`. Lucene native path: roles via terms aggregation, `bucket.of` flagged.
- B5: `StructuredStep.intent: LogIntentCall`, `DecisionAgent.log_step` before every agent (rules,
  llm, cascade), llm `log_intent`/`log_args`, `require_intent`; the engine runs intents through
  `log_tools.run_intent`; aggregates carry `intent`, `args`, `rows`, `rendering`.
- R2-B4 scope guard (`decision/log_guard.py`) with `logs.serving.allow_adhoc_scopes`; n4 single-tenant
  refusal in config, `Engine.__init__`, `create_app`, `create_mcp`.
- M1: MCP log tools (generated signatures, drift-checked, `readOnlyHint`), `mount_on_http` at `/mcp`
  (`streamable_http_path="/"`, parent lifespan, SDK transport security, `serving.mcp.token_env`).
- Live-deployment number-check bug: grouped sums, group count, population accepted; every aggregate
  has a deterministic `rendering` that replaces model numbers the aggregate does not back.
- sqlite typed-scope latency fix (owner-index hint).
- H2 partial: answers state "spans N library identities" from the manifest's `identity_span`.

**Deviations.**
- Profile name `logs_example` (the task's name; the plan says `profile: logs`).
- Kinds `log_day`/`log_hour` added (day/hour × tenant × env × service × level × status ×
  signature); several intents use them instead of the plan's `service_day`/`signature_window`
  because those carry no level. `log_new` reads `signature_catalog` (all served history, owner
  decision) instead of `signature_window`.
- Analytics kinds default off; the profile turns them on (generic logs configs unchanged).
- Co-firing: connected components of the kept-pair graph; Louvain (`louvain_partitions`, copied
  verbatim from the bug-fix slice, merged identical) only splits components above `split_above`
  (default 12): Louvain on a small complete bipartite block scatters a planted incident one
  signature per community (measured).
- One derived-history mechanism: my carried-forward documents were removed in favour of the bug-fix
  slice's per-day derived snapshots and day-document cache; the analytics kinds are part of the
  cached day build; the snapshot already carries the typed aggregates (round-trip test).
- `log_tenants` uses `unit_day` source counts when a count query covers the level, else `log_day`.
- The property test uses seeded `random` (no `hypothesis` in the venv).
- `test_request_parity` iterates only the MCP tools that exist (log tools are absent there).

**Not done / follow-ups.**
- **H1 history tier** (day/window shards, shard manifests, append-only history collections with a
  shard filter on SQLite and OpenSearch, gc by reference, day-181 acceptance): not started. The
  180-day read latency shows why it is needed (linear in records; 2 of 10 tools over 200 ms).
- **H2 lineage** (`signature_lineage`, `signature_history`, folding superseded signatures): not
  started beyond the identity-span sentence.
- Compose reading the fallback overlay (`model_line` → `unknown` bucket, `extraction_source` per
  field): the fallback slice has not landed on this base.
- `line` kind, classifier-agent mapping (the shared `log_step` covers it), `doctor` checks for the
  profile, OpenSearch/Elasticsearch runs of the new kinds and B3 operations, CI job at `mcp==1.10.0`.
- Test counts: before 1951 passed / 91 skipped; after 2272 passed / 94 skipped (not external).

### 2026-11-04 — logs/raw-hardening + interim derived archive (A2 §11.0, §11.R4 bug-fix slice) — DONE (synthetic)

Branch `v2-logs-raw-hardening` from `fdb7c4a`. Commits: `a96ca4f` (raw hardening, A1 amendment
docs) and the follow-up commit (raw gateway module, derived archive, serve bound, Host guard).

**Done.**
- Raw tier: parent not group/world-writable (`RawLocationUnsafe`); path outside the partition store,
  backends, library and the served directories (installed package, UI bundle); verify requires
  exactly 0700/0600 and no day older than `raw_days`; `ingestion/raw_reparse.py` is the only
  importer of `raw_store` (open, write, read, reparse, rederive, audit); `RawStore` has no read
  method; class G: `rederive` step re-derives only in-raw days, others `stale_redaction`.
- A1 rule (a) and the content verify scoped to the masked tier (docs: A1 §3.8.3 amended by A2
  rev 3/4). No refusal of `derived ≠ window` (rev 4, n-r4-1).
- Snapshots: `derived_format: 2`, identity `sha(key, version, library identity, partition format,
  fallback identity, compose format)`, payload with every aggregate-level artifact, typed-value
  aggregates when present, overlay rows (`v<n>/fallback/<identity>/`), accounting;
  `source_unavailable` for missing inputs; format-1 read and marked.
- `logs.history.serve_days` (default `keep_days`) and `max_serve_rows`; window rollups over the
  masked window; `library_identities`/`identity_span` in the manifest; day documents cached by
  snapshot identity (only new or changed days rebuilt).
- Host guard when the UI is enabled, MCP is mounted on the HTTP app, or `allowed_hosts` is
  non-empty; `create_app(bind=)`; the standalone MCP HTTP app is guarded when `allowed_hosts` is set.

**Deviations.**
- (a) `serving.mcp.mount_on_http` does not exist at `fdb7c4a` (MCP runs as its own server); the guard
  reads it with `getattr` so it takes effect when that key lands, and no unused key was added.
- (b) The overlay layout is read generically (`v<n>/fallback/<identity>/**/*.json`, newest identity
  name) because `ingestion/overlay.py` has not landed; the fallback slice exposes the final interface.
- (c) "Typed-value aggregates" are every aggregate-level `.json` artifact of the sealed version
  (format 1 has none, so `typed_inputs` is `source_unavailable`).
- (d) `max_serve_rows` counts unit rows, signature rows and hourly cells of each served day; the
  masked window is always served, derived days are dropped oldest first.
- (e) The compose bound is the serve window plus the day-document cache; the write path still
  builds one generation from the served documents (shards are H1).

**Tests.** `not external`: 2095 passed at `a96ca4f` → **2105 passed, 94 skipped**; new `tests/contract/test_derived_archive.py`
(8 tests, including the 60-day cost test), raw hardening tests in `test_retention_scopes_and_raw.py`,
Host guard tests in `test_serving_pins_and_bind.py`. ruff clean; schema regenerated with `write_all`.

### 2026-11-04 — logs/package-run: deployment-run fixes, B11 safe redaction, retention scopes and raw tier, N10 — DONE (synthetic/mocked; postgres, OpenSearch 3.9.0 and RE2 checks on isolated local resources)

Base `106d4b0` (`v2/logs`). Commits: `63171b7` (fixes, B11, N10, O1–O3, B8 pins, serve bind) and the
follow-up commit with the raw tier, retention scopes, N16 and docs. No paid call; every bug was
reproduced with synthetic names and lines.

**Done.**
- P1 probe: PIT check opt-in (`--probe-pit`), skipped for an unknown/old version or
  `sources.<n>.disallow: [pit]` (rows refused by the transport before I/O). P14: one privilege
  request per source. P18: `--request-log` (content-free JSONL with a summary row).
- P2/P3/N16 index names: `prefix` stripped (already at base; now tested end to end through
  discovery); `deployment_position: before_service` (declared prefixes) and `after_env`
  (env-anchored, any deployment between the env and service vocabularies); `unparsed: exclude`
  counts `excluded_scope.unparsed_name` (discover/count/probe output, partition step report).
- P4: `closing_bracket` and the contextual `python_traceback` rule (defaults); approved library
  `continuation` entries applied in `assemble(..., library=)` at pull time; the lint refuses
  selectors on continuation entries. P5: `patterns coverage|mine` assemble multiline records
  (source rules + library entries, JSONL `ts`/`container`/`file`), report fragments; `bootstrap`
  takes `fragments`. P6: `noise` `precedence: before_message` stage; identity unchanged when unset.
  P7: `patterns lint` returns every issue and exits 1.
- P10 record keys filterable (request filters and compiled scopes). P11: group-by + time word and
  enum value inside a field identifier no longer qualifiers. P12 `measure` on aggregates. P13
  `native_link_relations`, no `base_url` for local embedders.
- Retention semantics: `keep_days` = N most recent complete days (`expiry_cutoff(today, N)`), one
  reference day (today) for run-daily, retention and report rotation.
- Postgres native types: test-ordering bug (kit's text-count check rewrote the records); the kit
  exposes `record_rows()` and the test re-upserts before the type query.
- O1 `docs_index_parts` (literal prefix); activation reads every pointer back before `CURRENT`
  (B9). O2: 503 causes logged, `/ready` runs the request pin. O3: `communities.base.louvain_partitions`
  (integer-relabelled, sorted graph), shared by `louvain` and `louvain_hierarchical`.
- B8: `Engine.process_pins` (with `logs:`) pins every request's generation from `_begin` to
  `_finish` (search, answer, streams, tools, values; so HTTP, SSE, MCP). `serve` honours
  `serving.http.bind`/`token_env`.
- B11: `privacy/redactors.py`, `privacy/scan.py`, PrivacyConfig `engine`, `redactors`, `custom`,
  `regex_engine`, `regex_timeout_ms`, `hash_key_env`, `actions`, `record_fields[_allow]`; wired into
  `LogMapping` (message, stack, extra), `LogsPipeline.from_engine` (CLI), ingest
  (`redact_document`), `LineRedactor`/`lines_from_records` (stamped lines admitted as is);
  `privacy scan [--raw]`.
- Raw tier and scopes: `ingestion/raw_store.py`, `logs.partitions.raw`, `retention.raw_days`,
  `partition_days` alias, `derived: window|forever|<days>`, derived snapshots and
  `logs_compose.downsample` hook, `logs reparse [--from-raw]`, retention run/verify/report per scope.
- N10: batched catalog/records writes, catalog-only chunks reduced to shapes, copy-on-write
  redaction, compose frees day inputs: 10^6 rows in 1096.5 s at 3.94 GiB (met).

**Deviations.**
- (a) `sources:` configs are **not** required to use `engine: safe` (addendum §3.2.3); the pulling
  commands refuse at run time instead, as the task specified. `PrivacyConfig.max_line_bytes` was
  not added (the source's `max_line_bytes` already bounds lines).
- (b) The safe engine without google-re2 runs on the `regex` development engine only when
  configured (`regex_engine: regex`); there is no automatic fallback.
- (c) Payload redactors (`llm_text`, `content`, `query`, `record_payload`, `pg_row`,
  `tracking_id`) match the key with RE2-safe patterns and extend the value with linear Python scans
  (quoted strings, brackets, SQL literals, end of line), because RE2 repeats stop at 1000.
  `api_key` flags the lease `"token"` in generation manifests during scans (documented false
  positive); redaction counts are `[name, count]` pairs so they do not look like assignments.
- (d) Retention: chose "N complete days" (today excluded): day 1 is deleted on day 12 with
  `keep_days: 10`, not on day 11 as the private plan's A8 expected.
- (e) Derived data is kept as per-day masked snapshots in the partition store (`derived/`), and
  compose rebuilds the serving generation from them; it is not an append-only delta into the
  backends. Coordination with the L5 delta reparse / content-addressed day docs is open: they can
  key on the same per-day snapshots. Derived days whose partition expired cannot be reparsed under a
  new library (served as composed, identity listed).
- (f) Raw copies are written per unit during the pull; a library change does not re-join records
  already stored (reparse re-matches stored heads; `--from-raw` re-joins).
- (g) Library `continuation` entries take no `applies_to` selectors (lint error).
- (h) `ProcessPins` starts a heartbeat thread in every `Engine` with a `logs:` section.
- (i) P3b, P8 (bounded idiom documented by the lint messages; not relaxed), P9 (composite signature
  ids), P15 (token-aware chunk cap), P16 (dev version on unreleased builds), P17 (TLS fingerprint
  pin) were not done: feature or packaging decisions, not fixes of wrong behaviour.

**Tests.** `not external`: 1951 passed, 91 skipped at base → **2062 passed, 94 skipped** (final run
after this entry). RE2 (isolated google-re2): redaction/B11/package-run files 100 passed, 1
skipped. External: postgres conformance 25 passed; OpenSearch 3.9.0 e2e + adapter 7 passed (e2e
fails at base). ruff clean; schema regenerated with `write_all`. New test files:
`tests/unit/test_package_run_fixes.py`, `test_redaction_safe_engine.py`,
`tests/contract/test_deployment_run_engine_fixes.py`, `test_logs_safe_redaction.py`,
`test_serving_pins_and_bind.py`, `test_retention_scopes_and_raw.py`,
`tests/services/test_opensearch_underscore_dataset.py`. Changed: `test_retention_11_days.py`,
`test_pins_and_gc.py` (as-of = today), `test_log_day_window.py`, postgres conformance.

**Follow-ups.** P8/P9/P15/P16/P17; redaction throughput on real volumes; derived-forever growth and
the downsampling policy; L5 alignment on derived day docs; a CI leg with google-re2.

### 2026-11-04 — A1/L4b: LLM pattern-learning loop and writer, plus the L1 index-name grammar fix — DONE (synthetic/mocked evidence)
Branch `worktree-agent-a3278fda379576f1e` (base `a27ab5a`, `v2/logs`), commit `a586d0b`. Scope: addendum
`v2_addendum_logs_and_patterns.md` rev 3, §3.6.1–3.6.3, and the index-name parser fix (L1).

**Done.**

- `learning/patterns/propose.py`: the `PatternProposalReply` schema (no free-form mappings; strict
  for OpenAI, within the Anthropic `output_config.format` limits), a neutral system prompt, nearest
  approved patterns, one `JsonModel` call per cluster, and `proposal_entry` (a reply becomes a
  `proposed` entry or a typed `invalid`/`declined` reason; never repaired).
- `egress.py`: `LineRedactor` (the configured `privacy` redaction plus a stamp; a config without
  redaction is refused), `EgressPolicy.admit` (only `RedactedLine`s with the current stamp; a residual
  e-mail/IPv4/JWT/bearer/AWS shape refuses the run), `check_role` (`allow_hosted`), `Pseudonymiser`,
  and `lines_from_records` (refuses a `LogMapping` with `redaction: none`; closes L1 open item b for
  L4b).
- `spend.py`: `DaySpendLedger` (SQLite; conditional `UPDATE` under `BEGIN IMMEDIATE`; integer
  micro-dollars; a crash leaves the reservation counted) and `LearnerMeter` (the `CallMeter`: run
  `Budget` then day ledger before the request; commits reported usage, or the estimate).
- `loop.py`: `learn` (egress → match → mine → rank → propose → gate → write → reparse, `dry_run`),
  `LearningSession` and `session_from_config` (`--max-usd` only lowers the run cap).
- Gate: `cluster_coverage`, `out_of_cluster` (absolute bound) and `fix_target`, only when the loop
  supplies the evidence. Promotion: a passing fix of a system entry is written as `supersede`;
  `human_decision` appends `approve` (or `supersede` for a fix), `deprecate` and `disable` records.
- `writer.py`: `git_work_tree`, `dirty_system_files`, `require_clean_system_files`
  (`SystemFileDirty`), `commit_system_files` (only the changed system files; author
  `system:<actor>`).
- CLI: `patterns propose|learn --input [--attr] [--dry-run] [--max-usd] [--output]` (exit 1 when the
  run stopped early) and `patterns promote|deprecate|disable <id> [--by] [--reason]`. `--input`
  also takes `.jsonl` (`id`, `text`, `attributes`).
- Config: `core/config_learning.py` (`learning.patterns`, cross-checks in `config_diagnostics`),
  `models.roles.pattern_learner` (in `chat_roles()` and `role_schemas()`),
  `pattern_library.gate.min_cluster_coverage`/`max_out_of_cluster`, `spend_ledger` path resolution.
  Schema regenerated with `core.schema.write_all`.
- Index-name grammar (`IndexNameConfig`, `IndexNameParser`): optional `prefix`, optional
  `deployment_prefixes` (`{n}` = digits; `deployment` attribute), vocabularies for any segment
  (longest entry first), one token per segment without a vocabulary, and both readings tried: a name
  that fits zero or two times is `unparsed` with a typed `reason`.
- Docs: CONFIGURATION (pattern learning section, gate, writer/git, CLI, index-name grammar),
  ARCHITECTURE, CHANGELOG (`[Unreleased]` "Log features" and "Changed"), VALIDATION, main-plan
  changelog row.

**Deviations.**

- (a) **Spend ledger scope.** B10 has not landed. The per-day cap is a minimal SQLite
  `DaySpendLedger` in `learning/patterns/spend.py` for the `learning.patterns` scope only (path
  `learning.patterns.spend_ledger`, default `<library>/.spend_ledger.sqlite`), not the general
  `SpendLedger` on every ledger backend under `ingestion/spend.py`. No `reconcile`; a stale
  reservation stays counted for that day. Placed outside `ingestion/` to stay clear of the parallel
  L3a/L3b slice.
- (b) **Egress redaction engine.** B11 has not landed, so lines are redacted with today's privacy
  engine (`privacy.redact_emails` + `privacy.patterns`, Python `re`). The "re-run built-in redactors"
  defence is the library's PII-shape check, which refuses rather than redacts.
- (c) **Pseudonymisation happens before mining**, not only on prompt lines: a test found the cluster
  template carried a host value. The gate still evaluates the redacted, non-pseudonymised bodies.
- (d) **Dry run stops before the model call** (no spend, no writes, no ledger file). It reports
  clusters, line ids, prompt size and estimated USD, not gate results. This is the safer reading of
  "a dry-run mode".
- (e) **`patterns propose`** is the loop with the policy forced to `manual` (always `proposed.yaml`);
  `patterns learn` uses the configured policy.
- (f) The CLI also covers the human decisions `promote|deprecate|disable` (as the task asked);
  `supersede` and `expire` remain record types written by the system or by `promote` of a fix.
- (g) **Index-name behaviour change.** The old rule "the first prefix field keeps extra separators"
  and "service = the rest without a vocabulary" were guesses. They are gone: two L1 tests were
  replaced (`big-tenant-…` now uses a tenant vocabulary; the `_`-separator case now asserts `unparsed`
  for a multi-token service). The old vocabulary validator already accepted `_`; the grammar tests
  pin `slack_bot` explicitly.
- (h) Not done from §3.6.1: raw replies are not stored, so they are not tagged with contributing
  partitions (needs L3a partitions); the novelty check is regex-sha only (no 0.98 overlap measure);
  the "parsed sample (≥ 20 000 lines)" for stealing is the input's parsed lines plus approved
  positives.
- (i) The L2/L4a test `test_a_fix_may_match_its_target_lines` now also asserts the new `fix_target`
  failure (the fix there drops two target positives); the original edge case (target lines exempt
  from `no_regressions`) is still asserted.

**Tests.** `not external`: base `a27ab5a` 1778 passed, 74 skipped → **1849 passed, 91 skipped**
(repository venv, no google-re2) and **1940 passed, 0 skipped** with google-re2 1.1.20251105 from the
isolated `--target` directory. Bare `pytest -q`: 1852 passed, 215 skipped. ruff clean. New files:
`tests/unit/test_pattern_learning_loop.py`, `tests/contract/test_patterns_learn_cli.py`,
`tests/unit/test_index_name_grammar.py`, `tests/pattern_learning_support.py` (scripted
`httpx.MockTransport` provider).

**Follow-ups.**

- A real-model smoke of `patterns learn` (loopback model first; hosted only within the owner's paid
  QA budget) and a public-corpus benchmark of proposal quality and cost per day.
- B10: the general spend ledger on every ledger backend, with `reconcile`, and `learning.patterns`
  moved onto it.
- B11: the safe redaction engine; the egress stamp then names it.
- L3a: run the loop on partition unparsed queues and tag stored raw replies with their partitions.
- The "after if approved" coverage does not shadow the targets of pending fixes.



### 2026-11-04 — A1/L3a + A1/L3b: day partitions, compose, run-daily, retention, pins — DONE (synthetic/contract; ledger and pins real-service on PostgreSQL and MongoDB containers; N10 memory budget not met)

Branch `worktree-agent-a5655a86a55ddb2f6` (base `a27ab5a`, `v2/logs`), commits `92fd93b`, `00591d2`,
`b2881d2`. Scope: addendum rev 3
§3.5, §3.7, §3.8.1–3.8.4.

**L3a done.**

- `core/config_logs.py`: `logs` (source, timezone, partitions path and clock, parse, compose,
  retention, pins, schedule, settle), `ingestion.line_recipe`, `ingestion.projections`. Cross-key
  rules in `Config.logs_coherent`: known source, `pattern` extractor, line recipe with `exemplars`,
  the n1 bound (`max_line_bytes + multiline.max_bytes <= max_chunk_chars`), the embedding
  invariant (no log-line field embedded), `trace_masking: privacy`, no `store_text`, and a catalog
  registry with pins (refused on mongodb/elasticsearch).
- `ingestion/partitions.py`: `FilePartitionStore` with `begin` (resume only with the same source
  query sha, else `PartitionConflict`), atomic `put_unit`, `seal`, `reparse`/`repull` as `v+1`,
  `expire` to a content-free tombstone (line and source-id hashes, minus lines of live days), and
  the schedule lock (`flock`, a `LeaseStore`).
- `ingestion/logs_parse.py`: head-line matching, unit and signature rows with row exactness under
  the coverage condition (scaled `approximate` estimates otherwise), hourly cells, exemplars,
  unparsed queue; a spawn process pool whose workers must report the parent's library identity.
- `ingestion/rollups.py`: per-day and window documents of every §3.7 kind except `issue_report`,
  each with a rendered `header` (date basis and timezone included).
- `ingestion/logs_compose.py`: `day_window` (`index_date` and `event_time` clocks, DST-aware,
  overlapping UTC index dates), identity check with reparse of stale partitions (N11), compose
  identity (an unchanged window is not rebuilt), build through `WritePath.ingest(replace=True)`
  with a `logs` manifest section.
- `ingestion/schedule.py`: `LogsPipeline.run_daily|backfill|settle` under the schedule lock;
  refuses a mapping without a redactor before any request; per-unit resume; content-free run
  reports rotated at the window length; `logs_cli` for `su-multi-arm logs …` and `retention …`.
- Write path: `line` chunk recipe (`extraction/chunking.py`), projection exclusions and
  `embed_fields`, `manifest_extra`, `chunking_recipe()` (both new keys enter the index recipe only
  when set; golden fingerprints unchanged).
- Source: `ElasticsearchRestSource.discover(window, index_dates=…)` resolves a dated expression for
  each overlapping index date and slices units to the window (event-time clock).

**L3b done.**

- `backends/extras.py`: `LedgerRetention` (`put_raw(tags=)`, `tag_raw`, `raw_tags`, `tagged`,
  `add_refs`/`refs`/`referencing_generations` on a `ledger_refs` side table, `purge(PurgeSpec)`,
  `drop_job`, `jobs`, `raw_entries`) on the memory, sqlite, sqlalchemy, postgres and mongodb
  ledgers; conformance kit `check_ledger_retention`.
- `WritePath` records every ledger key a generation uses (salted `extraction:` keys, `embedding:`,
  `raw:` and the `pattern_library:` snapshot) in `ledger_refs`, with `ledger_refs_count`/`_sha` in
  the manifest; failed builds and `delete_generation` drop their refs.
- `ingestion/pins.py`: TTL pins on the file registry (sqlite sidecar), memory, SQL and PostgreSQL
  registries; `PinsUnsupported` elsewhere; `ProcessPins` (one heartbeat pin per process and
  generation, refcounted; `for_config`).
- `ingestion/retention.py`: rule (a) plan and `run` (pins honoured with a bounded retry, cache
  invalidation, job entries, tag and reference purge, tombstones, session expiry, audit records),
  `report` (including O7 approved examples), content-based `verify`.

**Deviations.**

- (a) **Redactor.** B11 has not landed, so there is no configured redactor. `run-daily`,
  `backfill` and `settle` refuse with `RedactionRequired` before any request; the CLI therefore
  refuses them today. The Python API accepts an injected redactor (the tests use a synthetic
  e-mail/IPv4 redactor). `compose` and `retention` work from the CLI.
- (b) **Clock name.** The clocks are `index_date` and `event_time` (with `logs.timezone`);
  the addendum's `event_time_utc` is `event_time` with `timezone: UTC`.
- (c) **Raw records** are kept per unit as gzip JSON (`units/<unit>.json.gz`), not one
  `raw.jsonl.zst`: zstd is not a package dependency.
- (d) **`seal(key, parsed)`** takes the parse result, and `reparse(key, library, parse)` takes the
  parse function, instead of the addendum's argument-free signatures.
- (e) **Unreferenced purge** spares tagged entries (learning replies live until a tagged
  partition expires). It is skipped, and recorded in the retention report, while any remaining
  generation predates `ledger_refs`; verify then does not count unreferenced keys as orphans.
- (i) "Ledger on the 5 adapters plus memory" is read as every adapter that serves a ledger:
  sqlite, sqlalchemy, postgres and mongodb, plus memory. OpenSearch, Elasticsearch, neo4j and kuzu
  serve no ledger.
- (f) **`signature` documents are one per signature**, so a `$service` attribute link (which needs
  `service` in the document metadata) is not emitted from them; the graph wiring is L5.
- (g) **Not in this slice:** `limits.window` pause, the learning step (reported `not_run`; L4b),
  proposals and their `expire` records (L4b), `issue_report` and co-firing communities (L5),
  serving-path wiring of `ProcessPins` into `Engine` (the class and its config are here).
- (h) Generation manifests deleted with a sqlite generation (the file registry keeps them in the
  snapshot folder) are preserved as content-free audit records under `<partitions>/audit/`.

**Tests.** `not external`: 1779 → **1856** passed, 73 skipped (+77). ruff clean, schema regenerated
with `write_all`, golden fingerprints unchanged. New: `tests/unit/test_log_partitions.py`,
`test_log_day_window.py`, `test_logs_config_and_recipes.py`, `test_logs_parse.py`,
`test_pins_and_gc.py`, `tests/contract/test_run_daily.py`, `test_retention_11_days.py`,
`test_logs_cli.py`, `tests/logs_support.py`; postgres pins test in
`tests/conformance/test_postgres_conformance.py`; ledger retention in the conformance kit.
Real-service: postgres, mongodb and sql[postgres] conformance (with ledger retention) and the
postgres pin test passed against throwaway `pgvector:0.8.0-pg17` and `mongo:7.0.16` containers
(69 passed; the one failure, `test_records_use_native_types`, fails identically at base `a27ab5a`).

**Scale (N10).** `scripts/compose_scale.py`, sqlite, external workspace: 10^6 synthetic aggregate rows
(1,113,100 documents) composed in 1118.8 s (within 20 min) with peak RSS 10.28 GiB, **above the
8 GB budget, so N10 is not met on memory**; 10^5 rows: 117 s, 1.07 GiB. Follow-up: streaming or
per-partition delta compose (§4.3).

**Follow-ups.** B11 redactor wiring into `logs_cli`; `ProcessPins` in the serving path; L4b
learning step, proposal expiry and `raw:` tagging of learning replies; L5 graph wiring of attribute
links and communities; the pull window; streaming/delta compose for the N10 memory budget; the
pre-existing postgres native-types failure.

### 2026-11-04 — addendum A1 L0 + L1: read-only log sources, synthetic log fixtures — DONE (synthetic/mocked only)
Branch `worktree-agent-a37fff270f8499b43` (base `9ba767f`, `v2/phase-0`), commit `f6e6ec6`.

**Done.**

- **`sources/`.**
  - `contracts.py`: `SourceCapabilities`, `SourceWindow`, `SourceUnit`, `UnitCount`, `SourceCursor`,
    `SourceDoc`, `SourceBatch`, the `Source` protocol, and the shared minimal types `SamplePlan`,
    `Stratum` and `PartitionBudget`.
  - `errors.py`: the nine typed errors of §3.1.
  - `transport.py`: the §3.1.1 allow-list on the percent-decoded path, per-row parameters and bodies,
    before any I/O. Refused: scripts, runtime fields and aggregations, unbounded keepalives,
    redirects and env proxies. `DELETE` only for own and cursor-listed ids. Typed HTTP errors, no
    retries.
  - `elasticsearch_rest.py`: capability probe (403 on `/` tolerated); `_has_privileges` self-check
    (OpenSearch account endpoint gives `unknown`); `_resolve`/`_alias` discovery; per-index count
    queries from the mapping; scroll and `search_after` pulls with a stratum restart on cursor
    expiry; partition budget; capped context; PIT probe.
  - `cursor.py`: open-context registry; atexit and signal close.
  - `sampling.py`: `hourly_quota` with an `outside` stratum, and `random_score`.
  - `logs.py`: index-name parser, `LogRecord`, `LogMapping`.
  - `multiline.py` and `probe.py`.
- **CLI:** `su-multi-arm sources check|discover|count|probe`.
- **Config:** `ComponentKind` gains `source`; `core/config_sources.py` adds the `sources` key; the
  schema is regenerated.
- **`testing/fixtures/logs/`:** the generator (three line styles, fragments, noise, synthetic PII
  shapes), `FakeLogCluster` and `redos_corpus.yaml`.
- **Docs:** CONFIGURATION, ARCHITECTURE, VALIDATION, CHANGELOG, and the main-plan changelog row.

**Deviations.**

- (a) **PIT is probed but not used for pulls.** A PIT search needs `POST /_search` without an index,
  and that path is not in the §3.1.1 table. Pulls use scroll or `search_after`, which matches the
  task. Adding that row is an owner or reviewer decision.
- (b) **The B11 rule "any config with `sources:` requires `privacy.engine: safe`" is not enforced.**
  B11 has not landed. `LogMapping` takes an injected redactor and reports `redaction: none` without
  one. L1 persists nothing, and the probe reports aggregates only. **L3a and L4b must refuse a
  mapping whose redaction is `none`.**
- (c) **No user regex in this slice.**
  - Index names are parsed with a literal vocabulary (longest suffix first) and a closed
    `%Y/%m/%d` date format, instead of a library `index_name` entry.
  - Multiline uses a closed set of built-in continuation rules, with the head defined as "not a
    continuation".
  - `assemble()` accepts `is_head`/`is_continuation` predicates for the L2 library.
- (d) **`LogRecord` gains fields:** `file` (the multiline key `(container, file)`), `truncated`
  (§3.4.2 n1), `source_id`/`index` (retention verify) and `lines`.
- (e) **Unit time slices.** A dated index expression gives units without a slice (index-date
  clock). An undated expression gives units sliced to the window (event time). The partition clock
  itself is L3a.
- (f) **`Source.pull` takes an optional `budget`.** It also builds the `SamplePlan` itself when none
  is given (`plan_sample`).
- (g) **The ReDoS corpus is a YAML fixture with a loader.** RE2 is not installed in the dev venv,
  so the corpus is checked for shape and for super-linear growth under Python `re` only.
- (h) **`sources` is a full-config key.** The CLI loads the whole config but never builds an engine.

**Tests.** `not external`: 1189 → **1627 passed**. ruff is clean, the golden fingerprints are
unchanged, and the wheel includes the new files. New test files:

- `tests/unit/test_source_allowlist.py` (351 cases, including a 303-case method × path matrix with
  zero requests reaching the fake);
- `test_source_self_check.py`, `test_log_mapping.py`, `test_source_sampling.py`,
  `test_synthetic_logs.py`;
- `tests/contract/test_source_pull.py`, with CLI runs against a localhost fake and a SIGTERM
  subprocess;
- `tests/sources_support.py`.

**Note.** The venv's editable install points at the main checkout. Tests in a worktree need
`PYTHONPATH=src`, or they import the main checkout's code.

**Follow-ups.**

- The real-service tier: isolated ES 7.x and OpenSearch 2.x containers with synthetic data. Not run.
- The PIT pull row (see a).
- Sliced scroll (`pull.slices` only bounds open contexts today).
- Data-stream backing indices (skipped with a diagnostic).
- B11 redaction wiring and the `engine: safe` requirement.
- L2 replaces the built-in continuation rules and the vocabulary parser with library kinds.


### 2026-11-03 — P1 Neo4j graph adapter on the contracts (branch `worktree-agent-abe51c291233d8741`) — DONE
Commit `v2(phase-1/neo4j)` (`6e8f98a`), to be merged by the coordinator. Landed: `backends/neo4j/`
(`_cypher.py` every statement as a constant, hop templates precomputed per edge-kind set from the
closed `EdgeKind` set; `adapter.py` Neo4jBackend with explicit non-retrying transactions, error
mapping, template allow-list, pointer node, `protect`/`delete_generation`, `drop_namespace` test
helper; `graph.py` facet + GraphExtras + communities as nodes); `backends/capabilities.py` neo4j
graph `contracts` with `memberships: true`, new flag `predicate_scope` (true for memory/sqlite,
false for neo4j); `backends/extras.py::GenerationPointer`; `ingestion/write.py::_activate` (secondary
pointers move before `CURRENT`, moved back/removed on failure); `arms/base.py::graph_scope` used by
graph + community arms; shared `backends/_scope.py::validated_hops` (sqlite, memory, neo4j);
`engine.py` builds `kind: neo4j`; `core/config.py` `Neo4jBackend.namespace` (downgrade refuses a
non-default namespace); kit `check_graph` rewritten with `graph_fixture()`; v1 `adapters/neo4j.py`
deleted, `store.py`/`retrieval.py` v1 neo4j branches now raise, two v1 neo4j tests removed from
`tests/test_external.py` (their cases moved to `tests/services/test_neo4j_engine.py`). Docs:
CONFIGURATION (neo4j row), ARCHITECTURE, VALIDATION (real-service entry), services README; schema
regenerated.
Tests: non-external 473 → 496 passed (+17 `tests/unit/test_neo4j_adapter.py`, +7
`tests/contract/test_graph_backend_seams.py`, +1 config memberships test replacing two parametrised
cases that used neo4j as the "no memberships" example, now done with a monkeypatched matrix row);
external neo4j 13 passed (10 conformance, 3 engine) against `neo4j:5.26.0-community`
(`sha256:5a015e53…`); memory/sqlite/sqlite_faiss pass the extended graph check; ruff clean.
Deviations / interpretations: (1) Neo4j cannot evaluate document predicates (no catalog), so the
arms resolve predicate scopes to document ids via `catalog.iter_scope` before calling the graph
facet; catalog reads for that step are bounded by the eligible set (cap 10000 → typed error), not
by hits — principle 2.1.7 holds only for unfiltered requests on this backend. (2) "Generation-scoped
labels" are implemented as fixed labels + a generation-key property `g` (labels cannot be
parameterised; interpolating them would mean generated Cypher). (3) `TraversalSpec.edge_kinds` is
honoured as on sqlite (fact by default; co-occurrence only when requested — the arm never requests
it); fact-only is structural (separate relationship types). (4) The pointer node is a secondary
pointer next to the catalog registry's authoritative `CURRENT`; adapters still never read it on the
request path. (5) One transaction per `upsert` call (atomic; size bounded by server tx memory).
(6) MERGE makes re-loads idempotent on neo4j while sqlite/memory append duplicate relation rows;
the kit does not assert duplicate behaviour. (7) Hop validation added to sqlite/memory so the shared
kit can assert typed refusal for specs built with `model_construct`. (8) `CALL (n) {…} IN
TRANSACTIONS` requires Neo4j ≥ 5.23 (documented).
Follow-ups: staged multi-transaction load for very large graphs; optional predicate pushdown by
mirroring typed document fields onto neo4j (would restore 2.1.7 for filtered requests); doctor
probe for neo4j (`ping()` exists); vector index on neo4j stays Phase 5; the services CI job already
installs the `neo4j` extra and runs `-m external`, so the new suites run there unchanged.

### 2026-11-03 — P1 PostgreSQL and generic SQL adapters on the contracts (branch `worktree-agent-ac47e981ef8e1495f`, base `783d821`) — DONE
Commit `v2(phase-1/postgres-sql)` (`15d4f3a`), to be merged by the coordinator. Landed:
`backends/postgres/` (`adapter.py` PostgresBackend with seven facets and a lazily opened connection,
`_db.py` naming/DDL/typed rows/predicate and tsquery compilation, `catalog.py` Catalog + RecordStore,
`search.py` FTS LexicalIndex + pgvector VectorIndex with `HnswSettings` and `filter_recall`,
`runtime.py` cache/ledger/sessions, `registry.py` PostgresGenerationRegistry with a `CURRENT` pointer
row and a `pg_try_advisory_lock` write lock); `backends/sql/` (config kind `sqlalchemy`; SqlBackend
with catalog/records/cache/ledger/session on SQLAlchemy Core, SqlGenerationRegistry with a lease
row); `engine.py` builds `postgres` and `sqlalchemy`; `core/config.py` `PostgresBackend.vector`
(`index exact|hnsw`, HNSW parameters, `min_filter_recall`) and a validated `text_search_config`;
capability rows: postgres everything but graph `contracts` (graph `planned`), sqlalchemy catalog/
records/cache/ledger/session `contracts` (lexical, vector `planned`); pip extra `sql`
(`sqlalchemy>=2.0,<3`), `postgres` pinned `<4`; CI installs `sql,postgres`, services job `sql`.
Tests: `tests/conformance/test_postgres_conformance.py` (23 `external`),
`tests/conformance/test_sql_conformance.py` (12 sql[sqlite] every push, 12 sql[postgres]
`external`, 2 dialect-independent), `tests/unit/test_postgres_adapter.py` (13, fakes),
`tests/contract/test_arms.py` (+2: engine builds both kinds lazily / fails closed without a DSN;
engine end to end with sqlalchemy storage on SQLite), `tests/test_external.py` v1
`test_postgres_real` replaced by the v2 `test_postgres_engine_end_to_end` (neo4j tests stay skipped
individually); `test_backends.py`/`test_config_v2.py` matrix and validation rows updated (the three
"postgres storage is planned" rows replaced by planned-graph/communities, sqlalchemy lexical/dense,
bad `text_search_config` and bad `vector.index` rows, plus a positive storage-on-postgres/sqlalchemy
test). Non-external 473 → 506 passed; external 36 passed, 2 skipped against
`pgvector/pgvector:0.8.0-pg17` (PostgreSQL 17.6, vector 0.8.0); ruff clean; schema regenerated;
wheel/sdist build ok (both packages in the wheel, `sql` extra in METADATA). Docs: CONFIGURATION
(extras, two backend rows, validation text), ARCHITECTURE, VALIDATION (real-service entry),
services README.
Deviations / interpretations: (1) Package path `backends/sql/` as instructed by the coordinator
(plan §4 says `backends/sqlalchemy/`); the config kind stays `sqlalchemy`. (2) Naming: PostgreSQL
identifiers are capped at 63 bytes, so the per-generation schema is `<prefix>_<dataset>_<generation>`
only when it fits and the dataset is a lowercase identifier, else `<prefix>_g<sha256[:20]>`; generic
SQL tables are always `<prefix>_<sha256(dataset/generation)[:12]>_<suffix>`; both record the mapping
in a generations table. (3) Lexical terms are OR-combined `plainto_tsquery` per `\w+` token (FTS5
semantics, no user syntax) instead of the plan's `websearch_to_tsquery`, which ANDs terms and
interprets `or`/`-`/quotes from user text; phrase = `phraseto_tsquery`, evaluated per column (title,
body) so phrases never span the boundary. (4) Measures/groups are computed over matched rows with the
shared `_scope` helpers (as sqlite and mongodb do); predicates, counts, `exists` and `values` run in
SQL. (5) Typed rows keep the raw text in `text_value` (memory-fake semantics for eq on temporal
strings); postgres compares temporal values via `timestamptz`, generic SQL via UTC epoch seconds
(no portable tz-aware type). (6) HNSW gate: `refresh` under `vector.index: hnsw` builds partial
per-namespace indexes, runs `ANALYZE`, then measures recall@10 of the **forced** index path (seq,
bitmap and sort plans disabled, so a lower bound) against exact on ≤32 sampled stored vectors,
unfiltered and with an allow-list; below `min_filter_recall` it raises, so that build never
activates. Searches use `hnsw.iterative_scan = strict_order` (pgvector ≥ 0.8; older versions fail
closed). (7) Matrix `max_dimensions` for postgres stays 2000 (HNSW limit); exact accepts 16000.
`analyzer_id` flag is `pg_ts_config`; the per-generation value is `PostgresBackend.analyzer_id(gen)`.
(8) sqlalchemy declares `vector` as `planned` (coordinator brief) where the plan table says "none";
either way configuration rejects it. (9) `adapters/postgres.py` is kept: the unused v1
`store.py`/`retrieval.py` still import it; no v1-only test of it remains.
Follow-ups: record the lexical `analyzer_id` in the generation manifest (write path); projections on
a backend other than the catalog's (e.g. sqlite lexical over a postgres catalog) need the
multi-backend write-path slice (today a typed build failure); MySQL/MariaDB conformance (container
and a binary-collation story for `eq`); dialect-gated lexical for sqlalchemy; retire
`store.py`/`retrieval.py`/`adapters/postgres.py` together; cross-engine lexical agreement report.

### 2026-11-03 — Merge `v2/phase-0` (8b2c5bf) into the postgres/sql branch — DONE
Engine dispatch builds sqlite, memory, mongodb, postgres, sqlalchemy, neo4j, opensearch and
elasticsearch (the last refusal is gone; the `else` branch only guards an unknown kind).
`test_backends` lists every served pair (postgres except graph, sqlalchemy catalog/records/cache/
ledger/session, Lucene catalog/lexical/vector/records, neo4j graph, mongodb except vector); no adapter
kind is wholly `planned` any more. `compat/config_v1.downgrade` keeps every mainline refusal and also
refuses non-default postgres `vector`/`text_search_config`/`timeout_seconds` (v1 cannot express them).
The v1 `store.py`, `retrieval.py` and `adapters/postgres.py` (with `adapters/__init__.py`) were
deleted: nothing on mainline imported them. The v1 neo4j cases in `tests/test_external.py` were
already retired on mainline; only the v2 postgres engine test remains there. Docs list each adapter
once; services README and the services workflow install `dev,postgres,sql,neo4j,mongodb,opensearch,
elasticsearch`. Tests: ruff clean; non-external 638 passed; external postgres + sql + engine 36 passed
against `pgvector/pgvector:0.8.0-pg17` (container removed afterwards).

### 2026-11-03 — P1 declarative pipeline, harness through the engine, exact_id arm (branch `worktree-agent-a102da5b471458440`, base `0fb3ac0`) — DONE
Commit `v2(phase-1/pipeline-harness)` (`54c244b`), to be merged by the coordinator. Landed:
`pipeline/spec.py` (`PipelineRuntime`, `choose`, `RequestRejected`), `pipeline/validate.py`
(`config_diagnostics` + `raise_for`: every static problem in one `ConfigError` with `.diagnostics`;
`decision_for_mode`; `probe_report`), `arms/exact_id.py`, `runtime/turns.py` (`SessionTurns`),
`runtime/builtin_tools.py` (`search`, `get_document`, `list_values`, `aggregate`; `ToolCall`),
`runtime/builtin_actions.py` (`LedgerAuditLog`, `ingest_delta`, `invalidate_cache`; `ActionCall`);
`core/config.py` (`pipelines`, `default_pipeline`, `tools`, `actions`, `budgets.session`,
`sessions.memory.exclude_shown`, `ExactIdArm.alias_fields/casefold/max_ids`; `coherent` delegates to
`pipeline/validate.py`), `core/request.py` (`pipelines`, `modes`), `core/types.py`
(`FieldSpec.pattern` for `id` fields, `SessionRecord.usage`), `core/errors.py`
(`ConfigError.diagnostics`), `models.py` (`SearchRequest.pipeline`/`mode`), `engine.py` (pipelines,
sessions, `call_tool`, `run_action`, `EngineOperations`, exact_id pins/unknowns), `probe()` on
sqlite/memory/mongodb, CLI (`doctor --probe`, `session`, `tool`, `action`, `--session/--pipeline/
--mode`), `mcp.py` (two tool arguments). Tests 573 → 607 (+34: `tests/unit/test_pipeline_validation.py`
13, `tests/contract/test_pipeline_harness.py` 16, `tests/contract/test_doctor_probe.py` 5; mongodb probe
asserted inside two existing unit tests). Ruff clean, schema regenerated. Evidence: synthetic /
contract-mocked only.
Deviations / interpretations: (1) `pipelines.<name>` entries are **overlays** deep-merged onto
`pipeline` (the plan's sketch has one `pipeline` block; the brief's full-spec form is accepted too,
since a full entry overrides every key). The JSON schema shows the resolved `PipelineConfig` shape,
so editors may flag a partial entry's missing `arms` although validation accepts it. (2) Named
pipelines may only use the `dense`/`lexical`/`graph`/`community` backends of the default pipeline
(`projection_mismatch`/`projection_missing`): the write path projects only those, and changing it
was out of scope for shared files. (3) `mode` is interpreted as a decision-kind override
(`rules|classifier|llm|cascade`); `cascade` only on a cascade pipeline, `classifier` only where the
pipeline declares an artifact/component. (4) Static validation is aggregated, but pydantic field
errors (unknown keys, type errors) still stop before the model validator, so those are reported
first-error-only as before; the single-error message format is unchanged (`path: message`). (5) Tools
and actions are built-ins only (plugin tools/actions not loadable yet); the LLM decision agent does
not call tools (registry is ready); tool calls run in their own context (`Engine.call_tool`), not
inside an answer. (6) Actions audit on `storage.ledger` as append-only versions of one ledger job
`audit:<dataset>`; duplicate detection replays that job (O(audit size), acceptable for rare
operator actions); `promote_generation`/`rollback_generation` not implemented. (7) Per-request budgets
are the existing `budgets.*`; per-session limits are new (`budgets.session`); an exhausted session
refuses every further turn, even one that would need no model call. (8) `SessionRecord.usage` added
(JSON-serialised by all three stores, default empty). (9) `exact_id` reports unknown ids as
`UnsupportedConstraint(reason="value_not_observed")` appended **after** `AnswerContract.enforce` so
they never suppress the contract's own aggregate-intent flagging; an id outside the caller's ACL or
in `exclude_ids` is reported unknown (existence is not revealed). The arm has no `backend` key; its
capability is `catalog` through `storage.catalog`. (10) A `rerank` pass keeps pins first. (11)
`tests/test_protocol.py` sets `PYTHONPATH` for the MCP subprocess to the package under test (the
`.venv` editable install points at the main checkout, which broke worktree runs once the dumped
config gained keys).
Follow-ups: HTTP/MCP surfaces for sessions, tools and actions (serving slice; `api.py` maps
`RequestRejected` to 422 already); planner tool use through `ToolRegistry.schemas()`; plugin tools
and actions via entry points; write path projecting arms of named pipelines; concurrent turns on one
session are last-writer-wins on the turn list; MongoDB/OpenSearch `probe()` against the containers in
the services job; `release *` CLI stubs (Phase 1 row) not added.

### 2026-11-03 — Merge `v2/phase-0` (`16a3a17`) into the pipeline-harness branch — DONE
Conflicts: `engine.py` imports only (backend construction for all eight kinds came from mainline
unchanged; pipelines/sessions/tools wiring kept), `docs/VALIDATION.md` and this log (both sides kept,
mainline entries first). Added `probe()` delegating to the existing typed `ping()` on postgres,
sqlalchemy (`backends/sql`) and neo4j; opensearch/elasticsearch already had `probe()`, so every
backend kind is probed by `doctor --probe` (none reported `no_probe`). Schema regenerated with
`su_multi_arm.core.schema.write_all`. Tests: 675 passed, 116 deselected (`-m 'not external'`; +3
probe-delegation tests in `tests/contract/test_doctor_probe.py`); ruff clean. The new probes were not
run against containers (synthetic stub only). `ingestion/write.py` untouched.

### 2026-11-03 — P1 multi-backend write path (branch `worktree-agent-ac96c0b0d16d60e31`, base `16a3a17`) — DONE
Commit `v2(phase-1/multi-backend-write)` (`7d9f36f`), to be merged by the coordinator. Landed:
`ingestion/generations.py` (placement table projection → backends; `GenerationSet` with holders in
activation order, activation mechanism per holder `current|pointer|registry|none`, pointer read /
activate / restore, `check` for pinning, manifest entry); `ingestion/write.py` (scope mirrors on
secondary holders, records/graph/communities written to every target, per-target validation incl.
mirrors, records targets, graph memberships + sampled `find_entities`, community member lists vs
memberships, lexical `analyzer_id`; `_activate` secondaries first / `CURRENT` last with reverse-order
compensation and `ActivationError`; failure record gains `staged` pairs and `compensation`; `pin()`;
`delete_generation`; cleanup and protection limited to generation-scoped holders); `core/errors.py`
`GenerationDisagreement`, `GenerationInUse`; `backends/extras.py::GenerationIdentity` implemented by
memory, sqlite, postgres, sqlalchemy, mongodb, opensearch/elasticsearch, neo4j; OpenSearch (and so
Elasticsearch) implements `GenerationPointer` (`activate_generation`, `deactivate_generation` removing
only aliases that still name the generation); every generation registry gains `deactivate(gen)`
(memory/ES also `current_generation`); `engine.py`: two lines, `write_path.current()` →
`write_path.pin()` in `_context_for`/`answer` (no other engine edits).
Tests: non-external 638 → 659 (`tests/contract/test_multi_backend_write.py` 20,
`tests/unit/test_opensearch_backend.py` +1); `test_write_path` projection-set assertion now includes
`graph`/`communities`; `test_graph_backend_seams.build` clears the fake's recorded scopes after the
build (validation reads are not arm reads; same edge cases). External: new
`tests/services/test_multi_backend_engine.py` 1 passed (PostgreSQL catalog, OpenSearch lexical, Neo4j
graph); existing external suites for those three services 68 passed, 8 skipped. Containers started
for the run and removed with `rm -sf` (docker ps identical before/after). Ruff clean; `Config`
unchanged, schema not regenerated. Docs: CONFIGURATION ("Selecting a backend per projection" with
two examples, activation/pinning/manifest/clean-up), ARCHITECTURE ("Generations across backends"),
VALIDATION.
Deviations / interpretations: (1) **Scope mirror**: every lexical/vector/graph facet resolves scope
against its own backend's documents (and sqlite/postgres/mongo/Lucene refuse chunks of unknown
documents), so a secondary holder serving `catalog` receives the generation's documents + chunks; it
is validated and never read as the catalog. A lexical/dense arm on a backend without catalog is a
`ConfigError`. (2) Placement beyond the plan text: records also go to `pipeline.arms.structured.
backend` and graph to `storage.communities`/community arm backend when they differ (the community
arm reads mentions from its own backend; before this slice a community backend without the graph
arm had no mentions). (3) Secondary backends without their own pointer object (sqlite, memory,
postgres, sqlalchemy, mongodb) are activated through **their own generation registry** (manifest +
`CURRENT`), so disagreement is detectable on every holder; their `delete_generation` does not consult
that pointer (only neo4j/Lucene refuse an active generation natively), but `WritePath.delete_generation`
checks every pointer first. (4) Pinning reads one pointer per secondary per request (a network call
for Lucene/Neo4j/SQL/Mongo); single-backend configs read none. Generations built before this slice
have no secondary pointers and are refused once a projection lives off the catalog's backend
(rebuild). (5) `index_fingerprint` is unchanged (still keyed by the slot map) so existing indices keep
their identity. (6) `delete_generation` lives on `WritePath` (not `Engine`) to keep engine edits to
pinning; manifests are kept as audit records. (7) OpenSearch still has no generation registry, so it
cannot be `storage.catalog` (now a typed `ConfigError` instead of an `AttributeError`).
Follow-ups: OpenSearch registry (share the Elasticsearch one); real-service runs with MongoDB and
Elasticsearch as secondary holders; `Engine`/CLI surface for `delete_generation` and a `status`
view of every pointer; Phase 2 `release verify/gc` should treat manifests whose data was deleted;
session pins (`protect`) are per process — a multi-process pin registry is needed before `gc`.

### 2026-11-03 — P1 serving parity and streaming (branch `worktree-agent-a11ed89e7490c5073`, base `4a15f73`) — DONE
Commit `v2(phase-1/serving)` (`c456375`), to be merged by the coordinator. Landed: `runtime/events.py`
(typed `AnswerEvent` union, `AnswerStream` worker/queue with `cancel()`/`next_event(timeout)`,
`error_category`), `Engine.answer_stream` + `Engine.list_values` (engine edits limited to the answer
path: `_retrieve`/`_answer`/`_generate` take an optional event sink/delta callback; `answer` and
`answer_stream` share `_answer_turn`), `Budget.cancel()` + `core/errors.RunCancelled`, provider
streaming (`HttpCall.post_stream`, `iter_sse`, `AnthropicStream`, `ChatStream`,
`complete_json_stream`/`complete_text_stream`, `JsonModel.call_stream`/`streams`, `RoleSpec.streaming`,
`ModelCapabilities.streaming`), `serving/http.py` (moved from `api.py`, which re-exports),
`serving/sse.py`, `serving/auth.py`, `serving/mcp.py` (moved from `mcp.py`, which re-exports),
`serving/discovery.py`, `serving/parity.tool_differences(schema, model)`, CLI `mcp --transport
stdio|streamable_http --host --port --token-env`, `testing/provider_fakes.py`. Tests 675 → 712 (+37:
`tests/contract/test_streaming_providers.py` 18, `test_answer_stream.py` 9, `test_serving_http.py` 9,
`tests/parity/test_serving_parity.py` 1; `test_protocol.py`, `test_interfaces.py`,
`test_request_parity.py` updated for the larger MCP tool set and per-tool drift). Ruff clean; schema
regenerated (no drift). Evidence: synthetic / contract-mocked model; transports real on loopback.
Deviations / interpretations: (1) Event names follow plan §3.15 (`decision`, not "plan"); the
plan's `unsupported_constraints` event is folded into `contract` (status, violations, unsupported
constraints, `tokens_superseded`); `guard` is not emitted (guards are Phase 2). (2) Token deltas are the
generator's raw reply fragments — JSON text in structured-output modes — typed `provisional: true`;
the validated, contract-checked answer exists only in `done`. (3) Tokens stream only when the
generator role declares `capabilities.streaming: true` (the existing, default-off declaration);
otherwise the stream simply has no `token` events and the call is the non-streamed one (visible in
`/v2/capabilities.answer_streaming`); the provider itself refuses a stream call on an undeclared role.
`openai_compatible` streams send `stream_options.include_usage` like `openai`. (4) `decision` is
emitted live; `arm_result` per arm after each round's parallel execution; `review_round` after the
review stage finished (post hoc), then `evidence`. (5) Python `answer_stream` raises request,
pipeline/mode and session errors eagerly (HTTP maps them to status codes before the stream starts);
run failures end the stream with an `error` event (HTTP/MCP replace the message by a generic text
except for cancelled/budget/deadline). (6) Auth built-ins: loopback `LocalResolver` (principal
`anonymous`, scopes search/answer/tools) and `BearerTokenResolver` (principal `bearer`, plus
`actions`); the plan's `static_api_keys` built-in was not added (OAuth/API keys outside core per
default #6; injected via `principal_resolver=`). (7) `/v2/values` has `limit` ≤ `page_size_max`
but no cursor (paging is Phase 2 per the Phase 1 row); `/ready` also returns 503 when no generation
is active and does not expose probe error text. (8) MCP: `lookup_record` is `get_document`;
`capabilities` is the single discovery tool (datasets, pipelines/modes, filterable/listable fields,
tools); `get_document`/`aggregate` are registered only when enabled in `tools.enabled`;
`list_values` mirrors `/v2/values`. Every MCP search/answer/tool call is bound to an engine session
created per (MCP session id, principal) — one per stdio process — so turns are recorded (no config
switch to turn this off). MCP resources (active manifest) not added. (9) HTTP actions answer 403
for `denied`, 200 otherwise (the `ActionResult.status` carries `failed`). (10) Heartbeat interval is a
`create_app(heartbeat_seconds=...)` argument (15 s), not config.
Follow-ups: `release *` CLI stubs and `/v2/values` cursor paging; MCP `notifications/cancelled`
test; streaming for planner/reviewer stages (generator only today); `serving.mcp` session-binding
switch if operators want stateless MCP; `tests/smoke_wheel.py` exercises only MCP `search` over
stdio (unchanged).
### 2026-11-03 — P1/mongodb instant-based temporal fields (conformance fix) — DONE
Branch `v2/phase-0`. `backends/mongodb/_db.py`: `field_row` uses `_scope.instant` (canonical UTC text
+ epoch seconds), `predicate_filter(..., spec=)` routes temporal `eq`/`in`/bounds through
`_scope.temporal_key` (date fields: UTC-day window / truncated bounds; non-temporal value vs temporal
field → impossible; text bounds only on string rows); `catalog.py` records pass the field spec.
Real-service `test_mongodb_conformance.py` 16 passed (was 2 failing); new driver-free parity unit
test; tests 733 → 734 (`not external`), ruff clean, no schema change. Deviation: the existing unit
assertion "naive stamps are never guessed" was replaced by the B1 storage rule (naive = UTC).
Other adapters: none found with the gap (sqlite/memory/postgres/sql use `_scope`; opensearch and
elasticsearch share `_lucene_common.temporal_key` and were re-run after B1; neo4j holds no records).
Mongo test container removed afterwards.
### 2026-11-03 — a1 release-review fixes (B1–B6 except README, N1–N5, N13) — DONE
Branch `v2/phase-0`. Done: B1 `Engine._canonical_record` goes through `documents_in` (ACL-scoped
`iter_scope`). An unresolved id is removed from `DecisionRequest.record_context` and from the
generator's `caller_context_unverified`. It is reported as `UnsupportedConstraint(value_not_observed)`
and `trace.record_context`, and unknown and out-of-scope ids look identical. Tests:
`tests/contract/test_record_context_acl.py` (planner and generator payloads). B2: the review hop labels
its round with the first `RetrievalStep` query (else the request query) and runs structured steps
through the normal scope path. Tests: `tests/contract/test_review_stage.py` (engine, HTTP, SSE,
`answer_stream`). B3: `pipeline/validate.py` rejects any non-empty guards list (`not_executed`). B4:
BUSINESS_SUMMARY and MIGRATION were deleted and `docs/ADOPTING.md` was added. Example source (a vendor tracker name) →
`tracker` (`tracker:rounding`). Mapping example field names were neutralised (`Article.Title`,
`Issue__text`, ...). Wording was fixed in CONFIGURATION, VALIDATION, normalize.py and two historical
review files. A tracked-file denylist test was added (`tests/unit/test_release_identity.py`; it excludes
`docs/plans/reviews/`). B5: version 2.0.0a1, `--version`, `http.py` uses `__version__`, extras
`openai`/`anthropic` (empty, httpx), `local`, `all`; ci.yml installs `openai,anthropic`. B6:
`examples/v2/*.yaml` with load tests and a local end-to-end test, v1 examples labelled as the upgrade
path, CONFIGURATION/PLAN/VALIDATION/ARCHITECTURE updated. N1: `_scope.MAX_MATCHED_DEFAULT`/
`matched_cap`/`check_matched`, `max_matched` attribute on the sqlite, postgres, sqlalchemy, mongodb and
memory backends, plus a conformance check. N2: `scope_result` returns `bounded` when a predicate is
unsupported, and `AggregateEvidence.exact()` also requires no unsupported predicates. N3: catalog
`registry` flag in the capability matrix, `STORAGE_CAPABILITY["catalog"] = ("catalog", "registry")`.
N4: `core.errors.IndexNotReady` (a ValueError subclass; raised by `WritePath.current`); HTTP 499, 502
and 503; SSE category `unavailable`. N5: `except SuError: raise` before a catch narrowed to
`(ProviderError, httpx.HTTPError)`.
Deviations: (1) the review hop does not receive the reviewer's follow-up text, so it uses the first
retrieval step's query for the round label, as the review suggested. (2) `max_matched` on the
SQL-family and Mongo adapters is an instance attribute (default 10000), not a config key, so the
Config schema did not change. (3) An existing test was changed for N2 and N5: the opensearch unit
assertion on `exactness == "exact"` with a dropped predicate is now `bounded`. The fail-policies test
for "planner HTTP 500" now raises a `ProviderError` instead of a bare `RuntimeError` (bare
RuntimeErrors now propagate). (4) The `SU_*` env-var prefix, the package/dist name `su_multi_arm` /
`su-multi-arm`, and the `su` index prefixes in tests were kept (owner decision pending). README is owned
by the docs agent and was not edited.
Tests: 734 → 758 (`not external`, this commit; 763 in a working tree that also holds the docs agent's 5 capability-table tests), ruff clean, schema unchanged, wheel 2.0.0a1 builds.
Follow-ups: the B7 consolidated real-service run must include the new N1/N2 conformance checks on
postgres, mongodb, opensearch and elasticsearch; the untracked `a1_release_review.md` names the origin
services (excluded from the denylist test as a review record; neutralise it before a public push);
N6–N12, N14–N16 open; history rewrite before publishing (N13).

### 2026-11-03 — Landing README, MkDocs site, generated capability tables — DONE
Commit `4d760f4` on `v2/phase-0`. Landed: `README.md` (landing page), `assets/logo.svg` (+ symlink
`docs/assets/logo.svg`), `mkdocs.yml`, `docs/{index,quickstart,serving,schema,backends,providers,contributing}.md`,
`scripts/gen_capability_tables.py` (`--check`), `scripts/mkdocs_hooks.py`, `.github/workflows/docs.yml`,
`CITATION.cff`, `docs` extra in `pyproject.toml`, `tests/unit/test_capability_tables.py` (+5).
Tests: 766 passed `not external` (includes concurrent release-fix work); ruff clean; `mkdocs build --strict`
clean; every documented code block executed verbatim in a fresh clone + venv (see VALIDATION).
Interpretations: (1) the quickstart uses `examples/v2/local.yaml` (added by the release-fix slice);
(2) backend evidence is derived from the test tree (suite exists and is `external`), not from run
results; provider evidence is fixed policy text in the script; (3) links leaving `docs/` are
rewritten to GitHub by a build hook rather than editing existing docs or relaxing validation;
(4) `docs/plans/reviews/` is excluded from the site; (5) the public name lives in `mkdocs.yml`
`site_name`, the README title and `CITATION.cff`. Follow-ups: enable GitHub Pages (source: Actions)
when the repository is published; the Docs link and badges resolve only after that.
### 2026-11-03 — a1 release evidence (release review B7) — DONE
Branch `v2/phase-0`, source under test `8f6dd56`. Done: (1) consolidated real-service run — the five
`su-multi-arm-tests` containers (digests in VALIDATION "2.0.0a1 release evidence"), `wait_ready.py`
all ready, `pytest -m external -rs` with every `SU_TEST_*` service variable set: 100 passed, 17
skipped (16 undeclared kit capabilities on OpenSearch/Elasticsearch, 1 local-model test with
`SU_TEST_LOCAL_LLM_URL` unset), 0 failed, first run; the N1/N2 `check_records` additions passed on
postgres, sqlalchemy-on-postgres, mongodb, opensearch and elasticsearch. (2) `scripts/lexical_agreement.py`
and `docs/LEXICAL_AGREEMENT.md` (5 engines, 23 queries, 0 errors; identical match sets within analyzer
family, 18/23 across stemmed/unstemmed families). Unit test `tests/unit/test_lexical_agreement_script.py`
(metrics, query kinds, unset engines listed). (3) Stack torn down with `down -v` after confirming it was
idle (no foreign indices, sessions or databases); unrelated containers unchanged. (4) VALIDATION status
block and evidence section; local-model smoke recorded as the coordinator's run (1 passed,
`qwen2.5:1.5b`, OpenAI-compatible endpoint).
Fix: `tests/unit/test_release_identity.py::test_tracked_files_carry_no_origin_service_names` failed at
`8f6dd56` because the committed test file matches its own `DENYLIST` line; it now excludes its own path.
Deviations: the agreement script indexes identical pre-chunked documents through each adapter's
`catalog` + `lexical` facets (not the full `WritePath`), so the comparison isolates lexical semantics;
chunk-level metrics (document-level would be near-trivial on 7 documents). `docs/LEXICAL_AGREEMENT.md`
is not added to the `mkdocs.yml` nav (owned by the docs agent).
Tests: `not external` 762 passed + 1 failed → 766 passed (3 new, 1 fixed; working tree includes the docs
agent's capability-table tests); external 100 passed / 17 skipped; ruff clean; Config unchanged.
Follow-ups: add `LEXICAL_AGREEMENT.md` to the docs nav; hosted Anthropic/OpenAI remain mocked.
### 2026-11-04 — 2.0.0a2 slice S: sessions, sqlite lexical, config/CLI, HTTP auth, test suite, unimplemented keys — DONE
Branch `worktree-agent-a1faeba6a919e4dad`, base `95bb183`, commit `06868bd`. Fixed QA-05, 07, 10, 11, 12,
14, 16 (`docs/plans/reviews/qa_2.0.0a1.md`) and completeness-review risk R3; their 7 strict xfails
(9 params) removed and passing.
Done: (1) `SessionStore.touch(..., expected_version)` CAS on the new `SessionRecord.version` in memory,
sqlite, sqlalchemy, postgres and mongodb (`backends/_sessions.py`; SQL stores update `WHERE data =
<raw row read>`, Mongo filters on the raw `data`, memory holds a lock); `SessionConflict`;
`SessionManager.update` (64 in-process lock stripes, 50 bounded CAS attempts with jitter); turns
merged into the freshest record; `SessionTurns.reserve/release` hold the clipped request limits on
`SessionRecord.reservations` (expiry = deadline + 300 s); engine `_begin` reserves and releases on
failure. (2) Lexical arm `query_terms` and sqlite `tokens` keep the spelling (FTS5 folds); sqlite graph
keeps casefolded entity tokens. (3) `core.config.rebase_paths` + `upgrade_text(source_dir,
target_dir)`; CLI `--debug`, `_report` (exit 1/2/3); `validate_config` aggregates every pydantic error
into `ConfigError.diagnostics`. (4) `BearerTokenResolver` case-insensitive scheme. (5) Engine defers
`missing_secret` diagnostics of decision/review components of non-default `(pipeline, mode)` pairs,
re-checks on first use (`MissingSecret`), `Engine.missing_keys()` in `doctor`. (6) `tests/service_probe.py`
gates the OpenSearch/Elasticsearch external tests (skip when unset and unreachable, fail when
configured). (7) `models.roles.judge`, `models.roles.cache_gate`, `extraction.ontology` removed from the
schema and refused with "not implemented in this release"; `pipeline.arms.cache` refused at validation.
Configured-but-unused keys found (name-based walk of every Config field, `tests/unit/test_config_cli_a2.py`):
`models.roles.judge`, `models.roles.cache_gate`, `extraction.ontology` (now refused),
`pipeline.arms.cache` (was refused only at engine construction; now at validation) and
`pipeline.decision.artifact.manifest.features` (classifier manifest metadata; allowlisted, see follow-ups).
Deviations: (a) frozen §3.5 contract amended (`touch` gains `expected_version`; plan changelog row
added). (b) A CAS loss is re-applied to the fresh record up to 50 times; this is optimistic-concurrency
control, not a retry-until-pass of a check: the budget check runs again on every attempt and a turn
that no longer fits is refused. (c) A session-budget hold is the whole clipped request limit, so
concurrent turns of one session are refused while earlier holds are live unless per-request limits
are below the session limits (documented in CONFIGURATION). (d) `pipeline.arms.cache` stays in the
schema (the downgrade path references it) but fails validation. (e) A missing deferred key surfaces
as `MissingSecret` (ProviderError, HTTP 502), consistent with other provider credential failures.
(f) `config upgrade` without `--output` (stdout) does not rebase paths.
Tests: `pytest -q -m 'not external'` 766 passed + 20 xfailed (base) → 824 passed + 12 xfailed;
`pytest -q` with every extra installed and no services: 27 failed + 46 errors → 823 passed, 115
skipped, 0 errors (before the 4 service-probe tests); ruff clean; schema regenerated. Real service: kit
`check_session` with CAS passed on PostgreSQL and SQLAlchemy-on-PostgreSQL (throwaway stack started by
another run; random prefixes). `scripts/lexical_agreement.py` sqlite-only output byte-identical before/after.
Follow-ups: MongoDB session CAS against a real server (its conformance fixture drops a shared
database name, so it was not run while another agent used the stack); classifier
`manifest.features` has no consumer (check against the injected runtime with the Phase 2 classifier
row); session record growth (R4) and binder eviction are untouched; MCP/HTTP mapping of
`SessionConflict` (exhausted CAS attempts) falls to the generic 422 path; the other agents' QA xfail
removals in `tests/qa/test_qa_2_0_0a1.py` will touch the same file (trivial merge).

### 2026-11-04 — 2.0.0a2/D decision agent, typed scope, request filters, answer contract, generation — DONE
Branch `worktree-agent-ac35389efbcc8cb3a`, base `95bb183`, commit `0158c13`. Fixes QA-01, 02, 03, 04,
08, 09, 13, 15 from `reviews/qa_2.0.0a1.md`. Done: `decision/qualifiers.py` (candidates: observed
listable/enum values verbatim in the question, calendar expressions, quoted phrases) run by
`DecisionAgent.decide`/`CascadeAgent.decide` on every agent-compiled `StructuredStep`; unapplied
candidates → `UnsupportedConstraint(qualifier_not_applied)` on `StructuredStep.unapplied` and the
decision; the engine bounds that step's aggregate. `values.py::check_request_filters` (coercion
table, `FilterCheck`), applied in `Engine._retrieve` and `aggregate_in`. Engine pre-pass gets
`structured_targets()`. `generation/statements.py` + `AnswerContract` rule 5
(`answer_text_without_exact_scope`), absence needs an empty exact scope, `empty_answer` rule,
`on_violation: strip`, `contract: stripped`, `Violation.action`. `contract.answer_schema` (enum ids,
narrative-only kinds without aggregate evidence) sent when `JsonModel.enforces_schema()`; generator
prompt tightened (neutral). Aggregates/trace steps carry `predicates`; trace event
`structured_scope` with `Sensitive` values. `strict_schema` raises `StrictSchemaError` (allowlist of
keywords, typeless const, mixed/root anyOf, free-form mappings); `LlmPlan` = flat `LlmStep` +
`LlmFilter` pairs (mapping filters still accepted); `pipeline/validate.py::role_schemas`,
`strict_schema_problems`, diagnostic `schema_not_strict`, `probe_report[...]["strict_schemas"]`.
Deviations/interpretations: (1) QA-13 fixed by making the schema strict-expressible (preferred), so
`test_qa13_*` now asserts a native OpenAI planner validates and plans (rewritten, renamed);
validation still rejects a native role whose sent schema is not strict (unit test via
monkeypatched `role_schemas`). (2) QA-15 test asserts the typed `StrictSchemaError` instead of a
`None` return; the old unit assertion `strict_schema(Open) is None` became `pytest.raises`. (3) An
invalid request filter is applied as given (never dropped/widened) and reported; a typed
`aggregate` request with one abstains in the decision (ScopeCompiler), a plain search returns its
(usually empty) hits flagged. Undeclared filters under `unknown_fields: allow` are untyped string
equality **and** reported (`field_not_filterable`), since their aggregate cannot be exact. (4)
Qualifier check skips caller-typed aggregates/scopes; a structured step on a question without an
aggregate operator is flagged as a whole (covers the QA-01 "nexus threshold" example); free-text
topic words of aggregate questions are not `llm` candidates (documented limitation). (5) QA-08
claim stripping applies under the default `abstain_with_reason` only when the answer text contains no
statement; unknown ids still abstain (the enum prevents them under native output). (6)
`test_qa04_*` question changed to "List every pending LedgerOne record." because with QA-03 fixed
"ticket" is not a structured target in that schema (the contract assertion is unchanged; the text
check abstains with or without the intent). (7) Added `empty_answer` after the paid rerun showed one
empty answer passing. (8) The nine-probe harness test and `test_answer_contract.py` now expect the
extra `answer_text_without_exact_scope` violation (same edge cases). Config change: `on_violation`
literal (+`strip`); schema regenerated with `core.schema.write_all`.
Tests: `not external` 766 passed / 20 xfailed → 846 passed / 9 xfailed; ruff clean. Paid QA-08
(gpt-4o-mini, via `~/su_mutliarm_tests/qa` spend guard): 25 questions 24/25 → 0/25 abstained (1
empty answer, now rejected offline); `test_qa08_*` passed (xfail removed); spend $0.3425, cumulative
OpenAI $0.8641.
Follow-ups: count-value matching (a stated number vs the aggregate value) is not checked; Anthropic
native with the enum schema untested (key invalid); detector precision on more real output; QA-05,
06, 07, 10, 11, 12, 14 belong to the parallel slices.

### 2026-11-04 — 2.0.0a2/W write path: leases, fencing, incremental embeddings, ingest budget — DONE
Branch `worktree-agent-afb94827b31c0dc78` (base `95bb183`), commit `4fd530a`. Done: (1) R1 — `ingestion/lease.py`
(owner token, heartbeat every ttl/4, lost after a whole ttl without renewal, owner-only renew/release,
`Lease.check` renews synchronously before activation); leases on the file (non-blocking `flock`; was a
blocking wait), memory, PostgreSQL (advisory lock, `pg_locks` heartbeat), SQLAlchemy and MongoDB
(conditional row/document writes), Elasticsearch (`if_seq_no`/`if_primary_term`; release only by owner).
`activate(manifest, expected=...)` compare-and-set on every registry; secondary pointers CAS on what they
named before (Neo4j `POINTER_ACTIVATE_IF`, OpenSearch/ES `must_exist` fence on the expected docs index);
compensation only while the pointer still names the failed generation. Typed `WriteConflict(reason)`
(`core/errors.py`, a `BackendError`) and `IngestConflict` (`IngestError` + `WriteConflict`, failure
category `write_conflict:<reason>`). (2) R2 — ledger-backed embedding reuse keyed by
`(recipe sha, sha256(exact input))`, streamed per `batch_size` window, manifest `embedded`,
`reused_embeddings`, `incremental`; `ingest_delta` docstring/CONFIGURATION/outputs state what is
incremental. (3) QA-06 — `budgets.ingest` (`ingestion/budget.py`, `IngestMeter`/`RoleMeter`), embedder
`pricing_key`, price check at construction; v1 `max_llm_calls` → also `budgets.ingest.max_llm_calls`
when set explicitly. Schema regenerated; ARCHITECTURE, CONFIGURATION, VALIDATION updated.
Deviations: (a) `budgets.ingest.max_llm_calls` defaults to 10000 (a finite fail-closed cap; the plan
names `ingestion: {max_usd}` — the task's `budgets.ingest` shape was used); (b) the FileGenerationRegistry
lock no longer waits (typed refusal, as every other registry); (c) `activate()` without `expected`
keeps the unconditional switch for direct registry use/tests (`release.ANY`); the write path always
passes `expected`; (d) `extras.GenerationPointer.activate_generation` gained an optional `expected`;
injected pointers without it are switched after a read-and-compare (§3.5 changelog item); (e) the
embedder `pricing_key` is excluded from the index fingerprint so existing generations stay valid;
(f) the corpus (documents + chunks) is still loaded in memory on upsert; only vectors are streamed.
Tests replaced: the ES unit `test_registry_lock_conflicts_are_typed_and_stale_locks_expire` →
`test_registry_lease_conflicts_are_typed_and_stale_leases_expire` + non-owner release test (same held
/ stale-takeover edges, plus legacy lock documents); `test_budget_exhaustion_is_a_stage_failure_under_partial`
now sets `budgets.ingest.max_llm_calls`; two contract monkeypatches accept `expected`.
Tests: `not external` 766 passed / 20 xfailed → **797 passed / 19 xfailed** (QA-06 xfail removed);
external (throwaway postgres, mongo, elasticsearch, opensearch, neo4j; `rm -sf` afterwards):
conformance + multi-backend engine **98 passed, 16 skipped, 0 failed**; ruff clean.
Follow-ups: HTTP mapping of `WriteConflict` to 409 (serving owner); GC of `embedding:` ledger entries;
streaming the corpus on upsert and real delta for graph/communities/reports (Phase 2 ingestion row);
multi-host writer contention test; OpenSearch registry (still none) would reuse the ES lease code.

### 2026-11-04 — 2.0.0a2 release-review fixes (B1–B6 + cheap non-blocking) — DONE (Anthropic smoke BLOCKED: HTTP 429)
Branch `v2/phase-0`, after `70dca3f` (review committed as `docs: 2.0.0a2 release review`). Done:
B1 `decision/qualifiers.py` `field` qualifiers (filterable field named next to a typed value; comparisons;
bare boolean fields; a year on an integer field is not also a time qualifier); B2 `extraction/base.py`
`A1_RECIPE_KEYS` keeps `ontology: None` in the extractor recipe, golden test
`tests/unit/test_index_fingerprint_golden.py` (8 examples, values computed from `public-main`), a1-built
index opened under a2 via CLI; B3 `**_` on `activate` monkeypatches (neo4j engine, multi-backend engine,
graph seams); B4 `pythonpath = ["."]`, plus ES/OpenSearch conformance modules skip without their extra
unless the URL is set (found by the fresh-venv bare run); B5 `core.errors.failure_category`/`FAILURE_STATUS`,
HTTP 409 typed body for `WriteConflict`/`IngestConflict`/`SessionConflict`, action route answers with the
failure category's status and `result.category/reason` (exception text only in the audit record; N9 for
actions), MCP typed messages; B6 `2.0.0a2` everywhere, `CHANGELOG.md`, VALIDATION status header + "2.0.0a2
release evidence". Non-blocking: N1 detector exclusions (singular-unit labels, product-version names, head
nouns, singular "the only", "complete list of <non-unit>"), N2 `answer_number_mismatch`, N4
`value_not_observed` for unobserved filter values on listable fields, N5 `require_ingest_cap` for hosted
embedders, N8 key-file path removed from `.claude/agents/*` (and redacted in the committed review).
Deviations: (a) N5 is enforced at engine construction (WritePath), not in `validate_config`, so v1/v2
configs still load and `config validate/upgrade` work; (b) failed actions other than conflicts also map
to non-200 statuses (422/429/502/503/500), not only 409; `ActionResult` gained `category`/`reason`
(additive) and keeps `error` detail for in-process callers; (c) N2 accepts the aggregate's denominator
only after "of" ("12 of 36"); (d) one existing harness test text changed from "Three" to "Two" (it stated
a number different from its aggregate, which N2 now refuses); (e) "hosted" = `openai`, or
`openai_compatible` with a non-loopback host.
Tests: `not external` 934 passed / 1 skipped → **1018 passed**; fresh venv bare `pytest -q` 1007 passed,
131 skipped, 0 errors; external (five containers) **108 passed, 18 skipped, 0 failed**; ruff clean;
schema regenerated (no drift).
Anthropic smoke: first call (extraction, native tool_use + cache_control) → HTTP 429; stopped, no retry;
$0.00 spent (ledger). `$defs`, streaming and cache-token reporting remain unverified on the real API.
Follow-ups: rerun the Anthropic smoke when the key has quota; N3 (time-range intersection), N6, N7
(server-time lease expiry), N10; measure the detector abstain rate on a larger narrative set.

### 2026-11-04 — parity/query-compiler: structured-question operations — DONE (synthetic/mocked evidence)
Branch `worktree-agent-ad7184475f36fdbf0`, base `b99eae2`, commit `df872d4`. Goal: close the SU
structured-question gap (rankings, nth, windows, comparisons, shares, distinct, measures, lists,
topic counts) generically. Done: `ScopeSpec` (`order`, `nth`, `granularity`, `periods`,
`with_share`, `numerator`, `text`, `limit`) and `ScopeResult` (`total`, `listed`, `ties`, `unknown`,
`numerator`, `basis`; `GroupRow.rank/share`) — all additive; semantics in `backends/_scope.py` so
every records adapter (memory, sqlite, sqlalchemy, postgres, mongodb, opensearch, elasticsearch)
shares them; kit `check_query_operations` (all records adapters) and `check_text_count` (records +
lexical). `decision/windows.py`, `ValueResolver.window` (anchor + `metadata_schema.timezone`),
`FieldSpec.states`, `decision/grammar.py` (rules), planner `ScopeDraft` (`windows`, `periods`, ...),
new intent kinds `rank|breakdown|measure|share|compare`, structured-arm text resolution
(`text_backend`), `SearchRequest.as_of` (+ MCP args), contract number check, generator/planner prompt
lines (neutral). Coordinator extras: review B1 (count_distinct nulls; unit test + kit case) and N1
(`packing.aggregate_reserve_max`, default 0.5, traced `aggregate_reserve_chars`).
Deviations: (1) relative windows are a separate draft field (`ScopeDraft.windows`/`periods` as
expressions), not a new `Predicate` op, so no unresolved expression can reach an adapter; (2)
`metadata_schema.timezone` defaults to None (undeclared): calendar expressions on `require_offset`
datetime fields are refused (`timezone_missing`), date/`assume_utc` fields read UTC — keeps the
"calendar qualifier without an offset" probe failing closed; (3) ranks are dense (1,1,2) and `nth`
keeps every tied group; "which X has the most" compiles to `nth: 1` (all tied leaders); (4) negated
predicates/states include null values (as every adapter already did) and report them in `unknown`
rather than excluding them; (5) text topics are chunk-level literal matches through
`LexicalIndex.search` paged to exhaustion (cap 50,000 chunks, typed refusal above) — no new lexical
adapter method, to stay out of the lexical slice; (6) `unsupported_reason` now accepts `eq`/`in` on
undeclared record keys (`id`, `source`, `dataset`, `kind`); (7) a declared state wins over an
observed value of the same text for agent-compiled predicates; caller filters stay literal; (8)
packing default cap changes behaviour (aggregates above half the budget are omitted, reported).
Tests replaced: the "exhaustive list over an unbound topic" probe (unit + engine harness) now asks
"List all billing records" (same edge: an unbound topic word), because "records about billing" is now
a labelled literal text match; the packing test that relied on the uncapped reservation sets
`aggregate_reserve_max: 1.0`. Tests: `not external` 1032 → **1073 passed**; `pytest -q -o
pythonpath=src` 1076 passed / 123 skipped; ruff clean; schema regenerated (`write_all`); golden
fingerprints unchanged. Not run: PostgreSQL/MongoDB/OpenSearch/Elasticsearch conformance (no
isolated stack); SU 45-question re-measure (needs live planner + private corpus).
Follow-ups: `engine.aggregate_in` (tool path) does not resolve `text` (reported unsupported);
"week" granularity; per-period breakdowns; non-English grammar; v1 downgrade silently drops the new
settings (cf. review N10).

### 2026-11-04 — 2.0.0a3/a3 contract-providers: strip default, Anthropic native default, QQ-01..06, real Anthropic smoke — DONE
Branch `worktree-agent-abd4d0094f6cca9a3` (base `b99eae2`), commit `c0634b6`.

**Done**

1. **Owner decisions.**
   - `on_violation` defaults to `strip` in config and in `AnswerContract()`. When the question asks no
     aggregate (targeted or untargeted), unbacked count/list/absence sentences of a narrative answer
     are stripped and recorded. When it does ask one, an unbacked statement abstains. A factual
     answer with no citation left abstains; an `insufficient_evidence` answer may keep no citations.
   - Anthropic roles with no `structured_output` resolve to `native` for registry-capable models.
     `CapabilityDeclaration.structured_output` defaults to `None` and resolves in `ChatRole`, so an
     explicit `none` survives an `exclude_defaults` dump.
   - Registry `native_kind: output_format` (`output_config.format`) for `claude-haiku-5*` and
     `claude-sonnet-5*`; `tool_use` elsewhere. `base.output_format_schema` drops the bounds the API
     refuses and closes objects.
   - Registry `thinks_by_default` and `min_max_tokens` (1024) for Haiku 5.x, checked by `check_role`
     in config and at provider construction. The v1 upgrade raises the value with a
     `max_tokens_raised` warning; strict mode refuses.
   - Anthropic `thinking_tokens` is recorded as `reasoning_tokens`, truncation messages name the
     thinking share, and `stop_details` is recorded.
2. **QQ findings.**
   - QQ-01: `ProviderLimitIgnored` (`output_limit_ignored`) is raised in `_chat._exchange` when the
     reported output exceeds `max_tokens`. CONFIGURATION documents that Ollama requires
     `max_tokens_parameter: max_tokens`.
   - QQ-02: a number after a declared field name is a value label (`detect_statements(fields=)`).
   - QQ-03: total/sum amount statements (`Statement.amount`) are matched against the exact aggregate.
   - QQ-04: `Intents.untargeted`, `asks_list` and `asks_aggregate`, plus
     `intent.untargeted_constraint`. `check_qualifiers` uses it instead of `NO_OPERATOR`.
   - QQ-05: OpenAI-compatible calls record `reasoning_chars`, and the docs explain Ollama's thinking
     attribution.
   - QQ-06: the `insufficient_evidence` exemption is removed for statements and number mismatches.
   - Also fixed: when the nearest unit after a number reads as a label, the farther unit is retried.
     Before, "111 open CloudTax tickets" was undetected when the question made "open" a unit.
3. **Real Anthropic smoke** (first real calls): Haiku 5.5 and Sonnet 5.5, 44 requests, **$0.0467**
   by the guard ledger. Planner `output_format` and `tool_use` with `$defs`, streaming, cache
   write/read tokens, thinking and truncation, LLM extraction with grounding, and the planned answer
   are all verified. Refusal remains mocked. The QA guard (`~/su_mutliarm_tests/qa`, outside the
   repo) gained Sonnet pricing and a per-tag cap (`QA_TAG_CAP_USD`).
4. Version 2.0.0a3, CHANGELOG, VALIDATION (status and entry), CONFIGURATION, ARCHITECTURE. Schema
   and capability tables regenerated; the generator script gained per-pattern evidence and an
   accurate OpenAI note.

**Deviations**

- (a) The thinking minimum is a rejection only. A role default derived from the registry was tried
  and dropped: `exclude_defaults` dumps lost the explicit 2048 and reloads changed it.
- (b) v1 configs are raised with a warning rather than rejected, so v1 files keep loading.
  `examples/anthropic.yaml` reviewer 512 → 1024 keeps the example's upgrade/downgrade round trip
  exact.
- (c) `output_format` is declared only for the two 5.x families the smoke verified. Opus 5 and the
  4.x families stay `tool_use`.
- (d) QQ-04 is not a new aggregate kind. Untargeted list operators fire only on common record nouns
  (`COUNT_NOUNS`, moved to `decision/intent.py` and re-exported from `statements`), so "list all the
  steps" is unaffected.
- (e) `untargeted_constraint` reuses the reason `aggregate_intent_without_scope`; no new reason
  literal was added.

**Tests replaced**

- `test_anthropic_prompt_mode_request_shape_and_usage` and
  `test_anthropic_text_stream_folds_usage_from_start_and_delta_events` now set
  `structured_output: none` explicitly. They cover the same prompt-mode edges.
- Release identity is set to 2.0.0a3.

**Tests:** `not external` 1031 passed and 1 skipped → **1069 passed**. Bare `pytest -q` gives 1072
passed and 123 skipped. ruff is clean and the golden fingerprint test is green.

**Follow-ups**

- Provoke a real refusal only with a benign documented trigger, if one exists.
- Measure the narrative strip rate on a real QA set.
- Opus 5 `output_format` is unverified (0 rpm).
- `reasoning_chars` is not folded from streams.
- `max_tokens_parameter` is not enforced for Ollama by host detection: documented plus a runtime
  fail-closed check.

### 2026-11-04 — parity/kuzu-graph: Kuzu graph backend, typed traversal templates, hierarchical communities — DONE (neo4j template identity not run)
Branch `worktree-agent-a8eb44081b2f714d2` (base `b99eae2`), commit `7c9613a`. Done: (1) `backends/kuzu/` — `KuzuBackend`
(native: fixed Cypher in `_cypher.py`, `g`-keyed tables, MERGE-idempotent one-transaction load, one relationship table per edge
kind, provenance + occurrences, document-id eligibility with `predicate_scope: false`, CAS pointer row, `delete_generation`,
namespaces) and `KuzuAttachedBackend` (read-only existing store + declared `AttachMapping` file; catalog-validated; built
statements only; quote/verified/document rules; per-node paging; communities read once; writes refused); capability row `kuzu`
(graph, memberships); extra `kuzu`; engine/config wiring (`backends.<n>: {kind: kuzu, mode, path, attach.mapping}`, paths
relative to the config). (2) `core/config_graph.py` (TraversalTemplate, GraphOntology, GraphIntent) + `arms/traversal.py`;
`RetrievalStep.graph`, `ArmInput.graph`, `Capabilities.graph_templates`, LLM planner `template`/`seeds` (undeclared →
`invalid_step`); graph arm returns path provenance as document hits; the conformance kit's graph check runs the template
cases on every graph backend. (3) `communities/hierarchy.py` (`louvain_hierarchical`, seeded dendrogram, levels 0–2,
`parent_id`) and `communities/stats.py` (size, members_by_kind, trend over a declared `time_basis`, top entities by lift);
`Community.level/parent_id/stats` stored under the report's `_community` key. Private parity run: see
`~/su_mutliarm_tests/parity/REPORT.md` §11 and the VALIDATION.md entry (aggregate only).
Deviations: (a) Kuzu was a Phase 5 item; brought forward for parity, graph only. (b) No ontology contract exists yet
(Phase 3): a minimal `GraphOntology` view lives on `pipeline.arms.graph.ontology` and attached mappings expose one;
`extraction.ontology` stays refused. (c) Leiden replaced by the Louvain dendrogram (`louvain_hierarchical`, plan open
decision 7); strategy name differs from the plan's `leiden_hierarchical` on purpose. (d) Hierarchy/stats are stored inside
the stored report JSON (`_community` key) instead of new columns, so existing rows/indexes stay readable. (e) Attach-mode
spans anchor the stored quote (`<REL>.<col>`, offsets 0..len), not package catalog offsets; attached facts carry
`support: unchecked` as declared by the mapping (PathEdge allows it; Relation would not). (f) Attach `find_entities`
builds an in-memory name index of `lookup` tables once (bounded by `max_lookup_rows`); under a scope an entity needs an
eligible mention. (g) `GraphExtras`-optional `entity_types(ids)` used by templates when a facet offers it.
Tests: `not external` 1031 passed / 1 skipped → **1068 passed**; bare `pytest -q` 1071 passed, 124 skipped; ruff clean;
schema regenerated with `core.schema.write_all`; golden fingerprints unchanged. New: `tests/conformance/test_kuzu_conformance.py`,
`test_traversal_templates.py` (neo4j case `external`, not run: no server), `tests/unit/test_kuzu_attach.py`,
`test_communities_hierarchy.py`, `tests/contract/test_graph_templates_engine.py`; `test_backends.py` matrix expectations
extended with `kuzu`.
Follow-ups: run the neo4j template identity test on the service stack; seed resolution by description substring (the
measured troubleshoot gap) would need a typed "match text" lookup in GraphExtras; `connect` over hub nodes is slow on
large attached stores (53 s); community reports from external stores need grounded quotes to count; incremental community
augmentation (Jaccard attach, stale flags) not done.

### 2026-11-04 — parity/lexical-latency: sqlite `bm25` lexical engine, lexical front policies, request-path latency — DONE (latency target not met)
Branch `worktree-agent-a0dd61f541791a8d6` (base `b99eae2`, `v2/phase-0`). Done: (1) `backends/sqlite/bm25.py`
— Okapi BM25 segment per generation, built at `refresh` from the indexed chunks in chunk-id order
(memory-mapped `offsets/post_rows/post_freqs/idf/doc_len/row_doc/row_field.npy`, id lists as JSON,
vocabulary in the generation catalog's `bm25_terms`); scores bit-identical to `rank_bm25.BM25Okapi`
(first-occurrence term order, same running idf sum and epsilon floor, repeated query tokens counted);
a row matches when it holds a query token, ties by chunk id; `phrase`/`boolean` verify adjacency on
candidate text; settings mismatch / missing segment are typed `BackendError`s. Config:
`backends.<sqlite>.lexical_engine: fts5|bm25` + `bm25: {k1, b, epsilon, tokenizer: unicode_word|ascii_alnum,
lowercase, stemming: none|<snowball>, include_title}`; `SqliteBackend.recipe()` adds the keys to the index
fingerprint only for `bm25` (golden fingerprints unchanged); manifest `analyzer_id` `bm25_okapi:…`.
(2) Scope pushdown as a document bitset (`SqliteBackend.document_mask`) for bm25 and vector search:
Faiss `IDSelectorBitmap` / NumPy masked exact top-k, no depth growth and no per-candidate catalog reads;
vector row→document map built once per generation (re-built when the catalog file changes).
(3) Lexical arm policies, off by default: `verbatim_boost` (quoted strings and N-token windows with DF,
boilerplate and verification guards; needs `analyze`/`doc_freqs`/`phrase_rows`, else a validation error)
and `pin_identifiers` (id-field patterns, exact field equality inside the scope); front hits carry
`lexical_front` provenance and selection keeps them first under `source_slots` (`Candidate.leads`).
Repeated query words reach only an index that declares `scores_query_repeats`. (4) Request path:
`extras.HitParts` / `SqliteCatalog.hit_parts` (arm collapse without whole-document parses), selection
collapse without deep copies. (5) Write-path lexical validation samples only words the index analyzer
keeps (an ASCII tokenizer drops non-ASCII words).
Deviations: (a) the task's "zero-score hits dropped" was not kept: the `LexicalIndex` conformance
contract requires every chunk with a query term, so idf-0 rows (a token in exactly half the rows) match
with score 0 and rank last; (b) `Candidate.leads` is a selection change outside the lexical arm, needed
because `source_slots` re-ordered front records (measured: lexical MRR 0.384 → 0.426 with it); it is
inert unless an arm marks hits `lexical_front`; (c) the vector row map reads `chunks(id, document_id)`
once per generation and process (an index structure like `chunk_ids.json`, not a per-request read);
(d) `gc.freeze()` is not applied by the package (process-global); documented as a deployment note.
Tests: `not external` 1032 → **1066 passed** (new `tests/unit/test_bm25_lexical.py`, `sqlite_bm25`
conformance adapter, `sqlite_bm25` arm contract parametrisation); ruff clean; schema regenerated.
Measured (private corpus, 630 queries; numbers in `~/su_mutliarm_tests/parity/reports/lexical_latency_2026-11-04.json`):
bm25 + policies lexical R@10/MRR 0.703/0.426 vs service 0.698/0.426 (FTS5 0.676/0.389; bm25 alone
0.668/0.383); fused 0.837/0.639 vs 0.826/0.645; FTS5 lists unchanged 630/630 by the latency work.
Latency p50/p95: before 1.21/2.47 s (2 threads) → bm25 0.74/1.32 s (2 threads), 0.41/1.03 s
(8 threads); lexical search 0.72 → 0.028 s. **Target p50 ≤ 0.3 s not met**: the CPU query encode
(0.19–0.44 s in-process) dominates; without it ~0.25–0.29 s.
Follow-ups: query-encode cost (ONNX/quantised encoder, or encode cache for repeated queries); GC tail
(serving-level `gc.freeze()` option); `bm25` segment for incremental (delta) ingest is a full rebuild;
`hit_parts` document heads still parse the stored document JSON (a heads column would avoid it).

### 2026-11-04 — A1/L2 + A1/L4a: `pattern` extractor (with reuse salt), template miner and synthesiser — DONE (synthetic/contract evidence)

Branch `worktree-agent-a7b9050a7409e5f0b` (base `9ba767f`, `v2/phase-0`). Scope: addendum
`v2_addendum_logs_and_patterns.md` rev 3, §3.3–3.4, §3.6.1 rows 1–3, plus the owner policy amendment
of 2026-11-04 (LLM/system auto-promotion with a human in the loop later).

**L2 done.**

- `extraction/patterns/engine.py`:
  - an explicit `re2` engine (`google-re2`; integer group spans; RE2 `Set` prefilter) and a `regex`
    engine (`regex.ASCII`, per-call timeout, `deterministic: False`);
  - `PatternEngineUnavailable` when the package is missing, with no fallback;
  - the RE2-subset validator and ReDoS lint (star height, quantified alternation, adjacent unbounded
    repeats, unbounded-repeat follow) on the stdlib parse tree, plus an RE2 compile when `re2` is
    importable.
- `library.py`:
  - four files: `library.yaml` (human, never written); `learned.yaml`, `proposed.yaml` and
    `decisions.yaml` (system, append-only YAML sequences);
  - a merge with owner precedence (user > learned > proposed, with an `info` diagnostic);
  - decisions replay (`approve | deprecate | supersede | expire | disable`): a human `disable` is final,
    and system records never change human entries;
  - every lint class of §3.3, including the `$attribute` rule and PII shapes in examples;
  - inactive proposals keep lint errors as warnings;
  - hand-edit detection via `.writer_state.json`;
  - `library_identity` and the snapshot.
- `extractor.py`: staged matching (format → message → annotate / noise), selectors on format groups
  then document metadata, line truncation at `max_line_bytes`, line-scoped facts (a trigger group plus
  a negation check, otherwise `PredicateSupport.check`), header-scoped `native_link` attribute links,
  and the residue.
- `coverage.py` and `writer.py` (sha CAS append, `SystemFileRefused`/`Corrupt`/`Conflict`).
- `sugar.py`: `regex` → `annotate` entries.
- `BuiltinExtractor` gains:
  - a `pattern` dispatcher member, with residue composition when it is listed first (later extractors
    see blanked parsed lines; a guard fails the chunk if a span reaches into one);
  - `routing` on `ExtractionResult`;
  - `reuse_salt()` and `prepare_build()`;
  - static-only validation unless `probe`.
- `WritePath`:
  - loads the library before the lease, so a lint error fails the build before any write;
  - salts the reuse key `extraction:{recipe}:{salt[:16]}:{chunk}` only for a non-empty salt;
  - records `pattern_provenance` and `pattern_library_snapshot` (a content-addressed ledger raw entry).
- Config `extraction.pattern_library` (`PatternLibraryRef`: path, engine, max_line_bytes, mode,
  promotion, min_literal_chars, regex_timeout_ms, gate). Only `engine`/`max_line_bytes`/`mode` enter
  the recipe, and the key is omitted while unset. Paths resolve relative to the config file.
- Extras `patterns` (google-re2) and `patterns-lite` (regex).
- CLI `patterns lint|coverage|mine` (read-only).

**L4a done.** `learning/patterns/`:

- `mining.py`: a Drain-style miner per `(service, level)` with masks, depth 3, LRU eviction under
  `max_clusters`, and `max_children`/`max_examples`/`max_tokens` bounds; it is deterministic.
- `synthesis.py`: an anchored template regex with typed groups, rejected with a reason (never loosened).
- `bootstrap.py`: coverage before and after, with no LLM.
- `gate.py`: `run_gate` / `run_configured_gate` with the checks `re2_compile`, `lint`, `cited_examples`,
  `no_regressions`, `min_support` and `novelty`.
- `promotion.py`: `submit` under `auto` (default; `message` only, RE2 gate, not a fix of a human
  entry) appends to `learned.yaml` plus an `approve` record; `manual` and ineligible proposals go to
  `proposed.yaml`; a failed gate writes nothing.

**Deviations.**

- (a) The owner amendment supersedes the addendum's manual-only promotion. There is a new
  `learned.yaml`, `promotion: auto|manual` (default `auto`) and a human `disable` op. Main-plan
  changelog row added (§3.16 policy amendment).
- (b) No generation artifact directory exists yet. The library snapshot is stored content-addressed in
  the ledger and named in the manifest, instead of `library.snapshot.json` in a release sha manifest.
- (c) The ReDoS lint uses the stdlib parser, which folds some alternations (`x|x` becomes a class).
  The `regex` engine's timeout and RE2 linearity remain the runtime guards. Rule 4 is applied to every
  unbounded repeat, not only those inside groups, and reaching the end of an unanchored pattern
  violates it. Masks are linted as `^(?:mask)$` and again in context.
- (d) `regex` still runs on Python `re` (O10 says the switch is in 2.1). `sugar.regex_entries` is the
  mapping and fails rather than changing matches.
- (e) Attribute-link endpoint values come from document metadata (`service`, `tenant`, `env`,
  `signature_id`) and must occur in the `header` chunk. The header renderer is L3a.
- (f) `mode` has one value (`line`), because the addendum names `mode` as a recipe key without
  defining it.
- (g) Miner depth defaults to 3 rather than Drain's 4: depth 4 split templates whose second token
  varies.
- (h) Without a format match, the message stage runs on the whole line.
- (i) The writer's git commit step (§3.6.3 step 7) and the `promote|deprecate|disable` CLI are left
  for L4b.

**Tests.**

- `not external`: 1189 → **1293 passed, 73 skipped** without google-re2 (the RE2 params skip), and
  **1366 passed** with google-re2 1.1.20251105 from an isolated `--target` directory in
  `~/su_mutliarm_tests/claude/l2/`.
- Bare `pytest -q` (no google-re2): 1296 passed, 197 skipped.
- ruff is clean; the schema was regenerated with `core.schema.write_all`; the golden fingerprints are
  unchanged.
- New test files:
  - `tests/unit/test_pattern_engine.py`
  - `tests/unit/test_pattern_library.py`
  - `tests/unit/test_pattern_extractor_contract.py`
  - `tests/unit/test_library_writer_cas.py`
  - `tests/unit/test_pattern_gate_promotion.py`
  - `tests/unit/test_bootstrap_synthesis.py`
  - `tests/contract/test_pattern_write_path.py`
  - fixture `tests/pattern_fixtures.py` (synthetic generator and library; a stand-in until the L0
    generator lands)

**Follow-ups.**

- L4b: LLM proposer and egress, git commit of system files, CLI promote/deprecate/disable, and the
  human-review option over `auto`.
- L3a: `line` chunk recipe, header renderer, partitions, typed-field consumers, request-time snapshot
  readers.
- L3b: `ledger_refs` for salted keys.
- Add `google-re2` to CI so the RE2 cases run there.
- Throughput per engine and miner coverage on a public log corpus (benchmark tier).

### 2026-11-04 — 2.0.0a3/parity-fixes: package defects from the final parity run — DONE (synthetic/mocked evidence)

Base `94d978e` (v2/phase-0), commit `85e4f05`. No paid or real-service call.

Done:
1. **Native structured output on oversized schemas.**
   - `providers.base`: `OUTPUT_FORMAT_LIMITS` (24 optional and 16 union-typed properties on the wire
     schema), `output_format_complexity` / `output_format_problems`, and `ProviderSchemaTooComplex`
     (HTTP 400 whose body names the grammar or complexity; the body is never echoed).
   - `ModelRoles.fit_native_schemas`: a registry-default `native` role resolves to `none` for an
     oversized schema (`ChatRole.structured_output_note`), and `validate`/`doctor` report a
     `structured_output_fallback` warning. A declared `native` gets `schema_not_strict` (an error).
   - `HttpChatProvider._schema_exchange`: a call-time pre-flight, plus one recorded switch to prompt
     mode after the typed 400. Both apply to registry-default roles only. The record is
     `structured_output_fallback` in the call record and the `model_call` trace.
   - `examples/v2/hosted_models.yaml` planner: `structured_output: none`.
2. **`answer_number_mismatch`.**
   - `statements.population_frame` covers "out of", "denominator is/of/:", "as the denominator",
     ratio "N/M", "% of" and "N of M": such a number is not compared.
   - The denominator matches after a preposition (of/among/over/across/within/from, not "total
     of"), and so does the sum of the group counts.
3. **Rules grammar.**
   - These are `qualifier_not_applied` and the agent abstains (`unresolvable_value`):
     - a free ordinal (2nd–10th);
     - `active`/`inactive` that is not a declared state or value;
     - a numeric comparison bound to no field.
   - The topic cue no longer starts inside a hyphenated word, and "X-mention <unit>" is the topic X.
   - A topic made only of grammar words is ignored.
4. **`ScopeCompiler` sort by count.** On a grouped scope, `sort` by
   count/record_count/group_count/records/measure/value, by the measure kind or by a numeric
   measure's field becomes `order`. Without `group_by`, or with a contradicting `order`, it is
   `measure_unsupported`. The planner prompt mentions it.
5. **Precomputed `verify.input`.** `engine_template` (the default) re-embeds `{title}\n{field}:
   {text}`; `chunk_text` re-embeds the bare text. It is recorded in the spot-check report and is
   not in the index identity.

Deviations:
- (a) The task says a number equal to the aggregate's denominator must not trigger a mismatch. The
  existing test "there are 36 open records" (denominator 36, count 12) abstains, and that stays: a
  bare denominator stated as the count is a real wrong count. The denominator therefore matches only
  after a population preposition or inside a ratio frame. This covers every phrasing seen in the
  parity run.
- (b) `ChatRole.__eq__` ignores the private resolution provenance, so a dumped and reloaded config
  (which declares the resolved mode) stays equal to the original.
- (c) The complexity ceilings are Anthropic's documented limits. The real 400 was observed for the
  plan schema only, so whether every under-limit schema is accepted is not verified.
- (d) The `verify.input` default changed to `engine_template`. This is allowed because `precomputed`
  is unpublished (new in a3). Rows of the bare text need `input: chunk_text`.

Tests: `not external` 1189 → **1225 passed**; `pytest -q` 1228 passed, 124 skipped; ruff clean;
schema regenerated; golden fingerprints unchanged. New and changed tests:
- new: `tests/contract/test_structured_output_fallback.py`;
- added cases in `test_answer_text_contract.py`, `test_query_compiler.py` and
  `test_precomputed_vectors.py`;
- `test_anthropic_a3.py`: the planner is now expected to be over the ceilings.

Follow-ups:
- A real-API check of the pre-flight ceilings, with one call on a schema at the limit.
- Re-run the parity answer paths on both private corpora with these fixes.
- Text-mention ratio operations, which the parity report lists as a gap, are still not implemented.
- The parity report's config gaps (`sortable` organization/module/rca, the SU config on bm25) are
  deployment configuration, not package changes.

### 2026-11-04 — a3/opensearch3: OpenSearch 3.x support next to 2.x, Lucene result-window paging — DONE (real-service on 2.19.1, 3.9.0, ES 8.17.4)

Base `94d978e`, rebased onto `cf242ce`. Code commit `5e41469`.

Done:
1. **3.x differences checked.** Sources: the OpenSearch 3.0.0 release notes, the k-NN plugin notes
   for 3.0 alpha/beta through 3.9, and the core PR #17306. Each was then checked against the
   servers. The ones that matter:
   - Lucene 10 (10.5.1 in 3.9.0).
   - JDK 21 minimum (the image bundles 25).
   - `BM25Similarity` replaces `LegacyBM25Similarity` by default, for every index the server
     serves. Measured: scores are exactly 1/2.2 of 2.x, with identical rankings.
   - `nmslib` index creation is blocked from 3.0 (verified: `mapper_parsing_exception`).
   - The index-level `knn.algo_param.*`, `knn.space_type` and `knn.plugin.enabled` settings were
     removed. The adapter does not use them.
   - Derived vector source is the default.
   - Keyword `term` queries no longer score. The adapter uses them in filter context only.
   - `_bulk batch_size` was removed (not used).
   - The security demo config needs `OPENSEARCH_INITIAL_ADMIN_PASSWORD`, which the compose
     settings avoid by disabling security.
2. **`probe`** accepts 2.x and 3.x and returns `major`. It raises a typed error for an HNSW engine
   the major blocks (`adapter.KNN_ENGINES`, `search.HNSW_ENGINE = "lucene"`).
3. **No layout or identity change.** Mappings, `_meta`, the DSL and `generation_identity` are the
   same on both majors, and the 2.x golden fingerprints are unchanged.
4. **Result-window fix** in the shared `OpenSearchLexical.search` (also used by Elasticsearch),
   with a mocked regression test.
5. **Compose.** New `opensearch3` service (3.9.0, profile `opensearch3`, port 19202, digest in a
   compose comment). New `SU_TEST_OPENSEARCH_MAJOR`, and `wait_ready.py --only opensearch3`.
6. **Docs.** Updated CONFIGURATION, ARCHITECTURE, the services README, the generated backend
   tables, CHANGELOG 2.0.0a3 (Added, Fixed) and VALIDATION.

Deviations:
- (a) The server major is not recorded in the generation identity or the manifest. The layout is
  identical, and the BM25 scale is a property of the serving server, not of the index: a 2.x index
  served by 3.x scores the 3.x way. Every fusion and ordering policy is invariant to a constant
  factor. This is documented in CONFIGURATION ("OpenSearch semantics").
- (b) `knn.method` names no engine, so there is no configured engine to validate. The guard checks
  the engine the adapter writes, at probe time (`doctor --probe`). The adapter does not probe
  automatically before writes (unchanged).
- (c) The coordinator asked for the result-window fix mid-task. It refuses with a typed error
  beyond 10000 lexical hits. It does not enumerate further with search_after/PIT. A structured text
  condition matching more than 10000 chunks on Lucene engines is therefore refused, not counted.
- (d) Fixed a pre-existing hygiene failure at `cf242ce`: an origin service name in an earlier log
  entry.

Tests:
- `not external`: 1224 passed + 1 failed at `cf242ce` → **1237 passed**.
- Bare `pytest -q` (2.19.1 and ES up): 1278 passed, 86 skipped.
- OpenSearch external: 20 passed, 8 skipped on each major. Base on 2.19.1: 4 failed.
- ES external: 18 passed, 8 skipped. Base: 4 failed.
- ruff is clean. The schema is unchanged (Config not touched).

Follow-ups:
- Enumeration beyond 10000 lexical hits on Lucene engines (search_after with PIT), if structured
  text counts need it.
- Not yet tested:
  - 3.x with the multi-backend engine test, which needs PostgreSQL and Neo4j;
  - an in-place 2.x → 3.x upgrade;
  - security-enabled clusters.
- CI (`services.yml`) still starts only the default profile. Adding a 3.x leg is a CI-cost decision.

### 2026-11-04 — logs/native-aggs: native aggregation pushdown for the Lucene records facets — DONE (real-service on OS 2.19.1, 3.9.0, ES 8.17.4; synthetic data)

Base `a27ab5a` (`v2/logs`). Code commit `22e70c3`.

Done:
1. **`LuceneBackend.records`** (`core/config.py`): `aggregation: client | native` (default `client`),
   `matched_ids_cap` (10000), `max_groups` (10000), `distinct_exact_cap` (100000). The block is
   query-time: `LuceneBackend.recipe()` excludes it, so index identities and golden fingerprints are
   unchanged. The schema is regenerated with `write_all`. Constructor arguments and `from_config` are
   updated on both adapters.
2. **`backends/_lucene_aggs.py`** (`NativeAggregation`) compiles every `ScopeSpec` operation to
   aggregations, with the semantics of `_scope.scope_result`. The group ordering is factored out as
   `_scope.order_groups` and shared by both paths; the client path is unchanged.
   - Group keys are composite over `v.<field>` and decoded back to their JSON type.
   - A per-field kind probe (`s`/`n`/`b`/`t` exists counts) handles mixed-type and absent fields.
3. **`matched_ids` decision.**
   - The list stays in the contract. Under native it is the first `matched_ids_cap` ids in id order,
     and the new additive `ScopeResult.matched_ids_truncated` says more matched. `total` is always
     the full count.
   - The default cap equals the client `max_matched`, so wherever the client path answers, native
     returns the same ids. Where the client path would refuse, native answers with a flagged prefix.
   - The structured arm passes the flag as `document_ids_truncated`, which stays out of the prompt;
     `scope_result_of` round-trips it.
   - `AggregateEvidence.empty()` also requires `total == 0`. The other contract guarantees are
     unchanged: approximate and bounded results never back count, list or absence claims.
   - The exact_id arm uses a prefix of at most `max_ids` ids, which is identical while the cap
     is ≥ `max_ids`.
4. **Honest exactness.**
   - Counts, composite enumeration and pair-paged distinct counts are exact.
   - `cardinality` is used only above `distinct_exact_cap`, labelled `approximate` with the reason
     in `basis`.
   - More than `max_groups` groups gives a `bounded` result with an `UnsupportedConstraint`.
   - Python-equal keys that the index stores apart are a typed `BackendError`.
   - A lower-bound hit total is a typed error.
5. **Conformance.**
   - The records kit runs natively (`opensearch_native`, `elasticsearch_native`). The kit's
     `max_matched` check is mode-aware: native counts exactly and flags the capped ids.
   - New kit `testing/aggregation_parity.py`: 352 client/native comparisons, ACL isolation, caps.
   - New tests: `tests/conformance/test_lucene_native_aggregation.py`, the env-gated
     `tests/services/test_native_aggregation_scale.py`, and 25 fake-transport unit tests.
6. **Docs**: CONFIGURATION (compilation table), ARCHITECTURE, capability flag
   `native_aggregation: optional` with regenerated `docs/backends.md`, CHANGELOG `[Unreleased]`,
   VALIDATION (scale numbers).

Deviations:
- (a) Addendum §3.9.2/§4.3 deferred pushdown aggregation in favour of rollups. This slice implements
  it as an opt-in, so rollups remain the default plan.
- (b) `percentiles` is not compiled: `MeasureKind` has no percentile kind and the contract was not
  extended.
- (c) The task suggested "nested composite" for cross-tabs. One composite with two sources is used:
  it is equivalent and pages once.
- (d) "Over-cap gives a typed bounded result" is implemented for groups and for ranking past
  `max_groups`. Distinct counts go `approximate` instead of bounded, as the task specified.
- (e) The terms ranking path is trusted only on the engine's own `doc_count_error_upper_bound == 0`.
  The 200k scale run on OpenSearch 3.9.0 showed that one shard is not enough there: concurrent
  segment slices are the default. This was found by the scale run, fixed, and regression-tested.
- (f) The containers ran under compose project `su-ma-claude-aggs` on ports 19210–19212, so they
  could not collide with parallel agents. They are removed.

Tests:
- `not external`: 1778 passed at base → **1804 passed**, 73 skipped.
- External: OS 2.19.1 + ES 8.17.4, 59 passed. OS 3.9.0, 29 passed.
- Scale test with `SU_TEST_AGG_SCALE_DOCS=200000`: 2 passed (OS 3.9.0, ES 8.17.4).
- Benchmark at 200k on all three servers: every question agrees, native is 4–61× faster.
- ruff is clean.

Follow-ups:
- Multi-shard or multi-node clusters and security-enabled clusters are untested.
- Floating-point sum and avg can differ from the client path in the last ulps.
- Sub-millisecond first/last-seen ties are broken by id (index resolution).
- DST-gap calendar labels are untested.
- `percentiles` would need a measure-contract change.
- The other records adapters (sqlite, postgres, mongodb) still scan. Pushdown there is a separate
  slice.

### 2026-11-04 — A2/U1-MVP: read-only web UI, Host allow-list, CSP, `serve` bind fix — DONE (synthetic + mocked model)

Base `v2/logs` at `106d4b0`; commit `cbc06f5`. Built to addendum A2 rev 2 §4.4 / §11.3 under an owner
deadline: the MVP views against the **existing API only**; the full-scope views follow.

Done:
- `ui/` (Vite 8, React 18, TypeScript, cytoscape, zod; Node 24 in `.nvmrc`; committed
  `package-lock.json`): Ask (SSE via fetch stream, Stop aborts the run, provisional tokens dimmed,
  `done` authoritative, citation chips jump to evidence, contract badge with violations), evidence tabs
  per arm (structured: exactness badge with a symbol, basis, denominator, total, truncated-ids flag,
  `rendering` sentence when present, chart), graph explorer (graph-arm provenance; uses
  `/v2/graph/neighbourhood` only when advertised), charts (`group_count` by a groupable field), trace
  panel, settings. View registry (`ui/src/views/registry.tsx`) is the extension point; optional
  routes/tools are feature-detected from `/openapi.json` and `GET /v2/tools`.
- Wheel `su-multi-arm-ui` (`ui/python`, hatch hook refuses an empty bundle, `manifest.json` with
  sha256, `THIRD_PARTY_NOTICES.txt`); extra `su-multi-arm[ui]`; core sdist excludes `ui/`.
- `serving/ui.py` (mount, CSP + security headers, containment, `ui-config.json`),
  `serving/hosts.py` (B10 Host allow-list 421, Origin 403, N12 `nosniff` on `/v2/*`), `inspect`
  scope on built-in principals (B4), `serve` honours `serving.http.bind` and `bearer_env` (N20),
  `cli.build_parser()` factored out (for future copy-command round-trip tests), `doctor` reports the UI.
- CI job `ui` (npm ci, build, vitest, UI wheel). Docs: README, serving.md (Web UI), CONFIGURATION,
  CHANGELOG, VALIDATION; schema regenerated.

Deviations (safer interpretation, recorded):
- Static `/ui/*` assets are served without a credential (rev 2 §4.4.2 says so); "requires auth" is
  enforced on every `/v2/*` call and by refusing `ui.enabled` without bearer/injected resolver.
- Built bundle is **committed** (owner deadline: offline wheel build from `git archive`); CI rebuilds it.
- Runtime validation uses zod schemas written by hand, not `ajv` standalone validators generated from
  `scripts/gen_ui_schema.py` (N11): follow-up, with the schema drift test.
- No markdown rendering at all (stricter than `react-markdown` + `skipHtml`); charts are dependency-free
  SVG instead of recharts; no Tailwind / TanStack Query.
- MCP mounted in the HTTP app (N14, separate MCP token) is owned by the L5/M1 slice per rev 2 §11.1.8;
  not done here.

Tests: pytest `not external` 1958 passed, 105 skipped (21 new in `tests/contract/test_ui_mount.py`;
base not re-measured separately). vitest 40 passed. ruff clean. Manual Playwright smoke
(`ui/e2e/serve_synthetic.py` + `ui/e2e/smoke.mjs`, local cached chromium): 0 CSP violations, 0 page
errors. Both wheels build from `git archive` of the commit.

Follow-ups: Playwright e2e in CI with `securitypolicyviolation` assertion; S1a inspect routes
(`/v2/info`, spend, runs, patterns view) and S1b (graph neighbourhood, communities); Overview tiles
(`log_top`, `log_new`), Patterns, Incidents, Runs/spend dashboards and log-intent shortcuts
(feature-detected); `shlex.quote` copy commands with round-trip test; ajv validators + schema drift
test; axe accessibility audit; large-graph layout.

## A2 C1–C4 — accounted coverage and LLM fallback extraction (2026-11-04, Claude; base `106d4b0` then merged `fdb7c4a`)

**Status: done at the mocked-contract level.** Commits `72cc431`, `f744347`, the merge `955977b`,
`bdb1149` and `feb2977`. Built to A2 rev 2 §4.2 with the rev 3/4 deltas and the owner decisions
listed below. The draft plan was used from the external workspace and is not committed.

**Done**
- C1 accounting: `ingestion/accounting.py`. The counting unit is the source document. Identities Q
  (per-stratum fetched vs `min(quota, count)`, documents handed over = Σ fetched), D (`docs_pulled =
  malformed + heads + joined + fragments + overflow`) and H (`heads = Σ final_status`) are checked in
  `FilePartitionStore.seal` (refused seal, version stays open). S is reported with a derived `rest`.
  `UnitArtifacts.docs_pulled`/`malformed` are recorded by `_pull_unit`; older units report
  `recorded: false`. Match rows carry `final_status`, `reason`, `pattern_owner`,
  `extraction_source` and `gaps`; the manifest carries `accounting`. Validators give `range_invalid`.
  Coverage keeps regex-only and with-model numbers apart, with `deterministic_reviewed`,
  `fragment_share` and owner targets per service × tier.
- C2 cluster tier and overlay:
  - `learning/extraction/slots.py`: labels on `vN`/`maskN` (N3), or a model regex, through the full
    gate on today's lines; group-span values; the existing writer.
  - `ingestion/overlay.py`: per-unit atomic files, the manifest written last, H re-checked, and
    `OverlayStore.rows()` as the read API.
  - Reply cache keyed by template sha, service, level, provider, model, prompt and schema version
    (N4). Labels and regex are re-anchored and re-gated.
  - `run_daily` gains `fallback` (today plus deferred, missing or stale in-window days, ranked
    together) and `apply_decisions` (one reparse into v+1, overlay rebuilt with 0 calls). A failed
    fallback is recorded and compose still runs.
- C3 fill and tail: `fill.py` (gated `fix` per `(pattern, gap set)`, then line quotes for the gaps
  only) and `lines.py` (capped tail, quotes grounded in their own line, `exact`/`normalised`,
  ambiguous only when identical at every occurrence).
- C4 spend and routes:
  - `models.routes.<role>` lists ordered routes with health and capacity probes. No route available
    gives `capacity_deferred`. A paid route after a local one needs `allow_paid_fallback`.
  - `TotalSpendLedger` holds total-period caps per provider key, with a projected-exhaustion alert.
  - `LearnerMeter(role=…, totals=…)`; local routes record calls and tokens at USD 0.
  - R3-B2: a paid route is refused at config load without a price table, a finite provider total and
    finite daily and run USD caps.
  - Per-route throughput (calls/min, tokens/s) and the caller state (`ok`, `paused`,
    `paid_exhausted`, `capacity_limited`) are in the run report.
- Real-run fixes:
  - case-insensitive, token-bounded pseudonymiser;
  - the B11 built-ins (cookie, session and tracking values) in the egress shape check;
  - per-line `egress_refused` with `max_refused_share`;
  - stop file or SIGTERM gives a clean stop between calls;
  - the run report is persisted through a progress callback;
  - a pending proposal for the same template is reused without a call;
  - model-labelled noise becomes a `noise` proposal for a human.
- `logs coverage --date D [--from D0]` exits 1 when a day is open or an identity fails.
  `docs/config.schema.json` was regenerated with `write_all`.

**Tests:** 44 new (`tests/unit/test_fallback_extraction.py` 27, `test_fallback_realrun_fixes.py` 10,
`tests/contract/test_fallback_daily.py` 7). The full suite gives 2253 passed and 1 skipped (a base
test, skipped when google-re2 is installed) on the merged base; without this slice that is 2209 (2253 − 44,
derived, not a separate run). `ruff check src tests` is clean. No pre-existing test was changed or removed.

**Deviations from the plan**
1. Extra unresolved reasons: `not_attempted` (fallback disabled), `field_gap` (no fill resolved the
   gap) and `provider_error` (a call failed, never retried). With the fallback enabled, `format_only`
   heads are sealed `fallback_pending`, because they are residue too.
2. Identity D keeps `malformed` (the mapper drops malformed documents), as rev 3 confirms.
   `independent_rest` is not offered: a counted `rest` needs a source-side `_count` (bug-fix/source
   slice), so S is reported, not enforced.
3. The reply cache is its own SQLite file next to the spend ledger, not ledger `raw:` entries. The
   line-cache key also includes service and level (stricter than rev 3).
4. `out_of_cluster` evidence only counts residue the entry's `applies_to` can see. Otherwise the same
   template in another service (a separate cluster) always failed the gate. The `novelty` refusals
   that remain (same regex proposed for another service) follow the existing L4a rule.
5. `LineExtractionReply` carries no `entities` (B7: `model_line` feeds no graph edges).
6. Routes have a `local`/`paid` tier from loopback detection. The `lan` tier, `egress.allow_lan`,
   per-route time windows and invalid-reply limits are not implemented. A probe *command* is not
   offered (config never names code); probes are HTTP GET only.
7. Run caps are per route meter; the day and total caps are shared through the persistent ledgers.

**Follow-ups (not done)**
- Compose reading overlays (L5 B7 arm mapping), `ScopeResult.applied_extraction`, and `coverage_day`
  documents.
- Rev 4: `learning.bootstrap` (disabled by default, `require_measured_throughput`), `learn
  measure-throughput --tier --clusters 50`, the `serving.answers` spend scope, and the uncapped-paid
  check extended to `learning.bootstrap` and `serving.answers` chains.
- The `lan` tier and time windows; the 11-day synthetic gold set E1/E1b with backlog trajectory; a
  two-process test of `TotalSpendLedger` (same SQL pattern as the tested day ledger); the L4b
  `patterns learn` CLI does not yet use the stop file, progress persistence or the pending-proposal
  skip (only the fallback path does).
- Real-model evaluation (local first, paid only within the owner's caps) in the external workspace.

### 2026-11-04 — A2/S1a+S1b, U2, U3 (follow-up to the UI MVP): inspect routes, pattern review, ops views, log shortcuts — DONE (synthetic)

Base `v2/logs` at `fdb7c4a` (MVP merged). Commits: `806c660` inspect routes, `c40558b` pattern
review view + runs/spend + communities views, `78855b2` LAN forwarder origins + doctor TLS warning +
served/archived days, `6f39af0` log shortcuts and overview tiles. (`17dae49`, copy-CLI commands and
the Playwright CI job, was already merged upstream.)

Done:
- `serving/inspect.py`: `/v2/info`, `/v2/runs`, `/v2/spend`, `/v2/patterns/view` (generation
  snapshot from the ledger), `/v2/patterns/review` (review queue), `/v2/graph/neighbourhood`,
  `/v2/graph/edges/{id}`, `/v2/communities[/{id}]`; scope `inspect`; single-tenant 403 under an
  access policy (B4); absent data → `available: false` + reason.
- UI: Patterns (review queue first: proposals with regex, redacted examples, matched lines, gate
  checks; shlex-quoted promote/deprecate/disable commands, text fallback without the clipboard API;
  learned entries; generation snapshot; decision history), Runs & spend (generation info, served vs
  archived days, multi-library note, run steps, spend ledger), Communities, Logs (tiles + shortcuts
  from tool input schemas; hidden until `log_*` tools exist).
- Hosts: Origins on `serving.allowed_hosts` names accepted (forwarder may rewrite Host); `doctor`
  warns off-loopback without TLS.

Deviations:
- `/v2/patterns/review` reads the library **working files** (read-only, shas reported): proposals
  exist to be reviewed before any generation, and no compose-time capture exists yet (plan §4.3 wants
  generation-only reads for the patterns view; `/v2/patterns/view` keeps that rule).
- `owner` in `/v2/patterns/view` is `null` when the snapshot records no author (not inferred).
- `archived_days` is `null` until the manifest records archived days (the H1/history slice);
  `library_identities` is computed from the served partitions.
- Rev 3 fallback-state banner, per-tier call counts, paid burn and per-tier retention bytes, and
  the Trends history windows are **not built**: their data (C-slice ledger tiers, history tier) is
  not on this branch; the views will read them when the routes/fields exist.

Tests: pytest `not external` 2088 passed, 108 skipped (new: `test_inspect_routes.py` 9,
`test_ui_forwarder.py` 2). vitest 54 passed. ruff clean. Playwright smoke (synthetic logs engine,
fake streaming model, 2 synthetic noise proposals): 9 screenshots, 0 CSP violations, 0 page errors.
The CI `ui-e2e` job has not run on GitHub yet.

## A2 C1–C4 rev 4 follow-ups and merges (2026-11-04, Claude)

**Merges.**
- `e95f6b0`: `42627b0` (raw tier, versioned snapshots). `schedule.py` uses only
  `raw_reparse.open_raw`. The derived snapshot reads overlays through `OverlayStore.rows()`; the
  base snapshot test now seals a real overlay, and there is an end-to-end round-trip test.
- `1b6fd8c`: `dacd166` (L5 profile), resolved against the v2/logs head. The accounting row keys
  were renamed to `account_owner`/`required_gaps` so L5's `pattern_owner`/`pattern_reviewed`/`gaps`
  keep their meaning. Delta-reparse reuse requires `final_status`.

**Done (commits `1b4ea4b`, `1ee7971`, `91e7eee`, `1132caa`, docs commit).**
1. `learning.bootstrap`: off by default, `require_measured_throughput`, self-hosted routes only,
   cap = min(150, measured calls/hour × `hours_per_day`). A finite `max_usd_per_day` is required when
   a paid route can be reached.
2. `su-multi-arm patterns measure-throughput --tier --clusters 50 --date` (`throughput.json`).
3. `serving.answers` spend scope (`runtime/answers_spend.py`, `RunBudgetMeter`, `RunContext.spend`),
   plus the fail-closed config rule for capped paid providers at answer time.
4. Route `tier` (`local`/`lan`/`paid`), `egress.allow_lan`, UTC `windows`, `max_invalid_replies`.
5. `patterns learn|propose`: stop file and SIGTERM (`paused`, exit 0), `--output` persisted per
   cluster, pending-proposal skip.
6. E1b synthetic fallback gold set with an oracle model; it found and fixed the cap-counting defect
   (free clusters no longer take slots).
7. `OverlayStore.rows()`/`latest()` break `created_at` ties on the manifest's write time. This was
   found by the full suite under the `regex` engine (2338 passed, 0 failed) after the merges; with
   google-re2 the suite gives 2457 passed and 1 failed (the stdio-subprocess re2 path artifact).

**Tests:** 23 more in this round (routes/bootstrap 13, answers 2, learn run control 3, gold 4 and the
snapshot round trip in `test_fallback_daily.py` 1). One pre-existing test changed:
`test_the_day_cap_refuses_before_any_request_and_persists_across_runs` (run 2 skips pending
templates; the persisted cap is still asserted in run 3). The base snapshot test changed from a fake
`ov-1/rows.json` to a sealed overlay.

**Deviations.**
- The command is `patterns measure-throughput`, not `learn measure-throughput`: `learn` is a
  `patterns` subcommand in this CLI.
- `serving.answers` covers single answer-time roles. There are no serving route chains yet, so
  "serving chains" is the rule for roles whose provider has a lifetime cap.
- The tier is recorded per route in the run report, not per stored value.

**Follow-ups.**
- Compose mapping overlays into rollups and `applied_extraction` (L5/B7).
- A counted `rest` (source slice).
- Real-model and throughput measurement on the deployment, local first.
- The MCP parity test's subprocess env could append to `PYTHONPATH` instead of replacing it, so
  isolated engines carry over (owner of that test).

## Logs branch integration: H1/H2 merge and fresh-install test run (2026-11-04, Claude)

**Done.**
- `19ad82b` merges `v2-logs-h1` (`b705f83`) into `v2/logs` (`312e314`) with `--no-ff`. Git merged it
  without conflicts. CHANGELOG, VALIDATION and this log keep both sides. Both key sets stay: the
  fallback's `account_owner`/`required_gaps` and L5's `pattern_owner`/`pattern_reviewed`/`gaps`.
  The schema regenerated through `su_multi_arm.core.schema.write_all` with no diff.
- `e6d3b79` fixes the tests for a fresh `[dev]`-only install (bare `pytest -q` from a `git archive`):
  - The logs gold set (`tests/logs_gold_support.gold_config`) skips without networkx or a pattern
    engine.
  - `QUESTIONS` moved to `tests/logs_gold_support.py`. The history-tier tests imported it from the
    fastapi-importing surface-parity module.
  - Regex-engine tests skip through the existing `engine_or_skip`. This covers the redaction helper,
    the learn CLI `fake` fixture, the run-control `session()`, the 10 learning-loop tests that use
    the default engine, and the graph B7 test.
  - Lazy fastapi/uvicorn imports use `importorskip`.
- The 2 `TypeError: string indices` are not a product bug. Without the regex package,
  `patterns learn` correctly exits 1 with `error: PatternEngineUnavailable ...`. The tests then
  indexed that text as JSON. The tests now skip on the missing extra.
- One real test bug: `test_raw_inside_the_installed_package_is_refused` pointed at `ROOT/src/...`.
  On a non-editable install the imported package is in site-packages, so the check correctly did
  not fire there. The test now targets `su_multi_arm.__file__`.

**Tests.** Full venv `not external`: 2355 passed, 95 skipped (the same before and after the test
fixes; only the line numbers in the skip reasons moved). Fresh `[dev]`-only: 1791 passed, 651
skipped, 0 failed, 0 errors (was 59 failed / 89 errors). No test was removed. No assertion was
weakened.

**Notes.** With the anaconda `pytest` on `PATH` instead of the venv's, `tests/qa` qa10/qa11 fail.
Their subprocess runs `sys.executable`, which has no `su_multi_arm`. This comes from the
environment, not the code: run with `.venv/bin/python -m pytest`.

### 2026-11-04 — A2/U4-design: theme system, live timeline, answer card, charts, overview dashboard — DONE (synthetic)

Base `v2/logs` at `e6d3b79`. Commits: `326f428` theme system and visual system, `89d426a` timeline
and answer card (+ graph mini-map, legend), `5279aaf` chart set, `29647d4` overview dashboard, typed
graph styling, gold-fixture tour. Owner feedback "too bare bone": studied the private reference
console read-only (layout and interaction ideas only; no code or strings copied) and the dataviz and
frontend-design guidance (palette validated: WARN pairs only, so every chart uses direct labels).

Deviations / notes:
- Fonts: the CSP forbids remote fonts, so a theme names locally installed families only.
- Brand colour is never a chart mark (an orange brand fails mark contrast); charts use `series`.
- The heatmap is assembled from one `log_top by tenant` call per service (top 20 each): a blank
  cell means "not in that service's top 20", stated under the chart.
- The neighbourhood route now accepts every stored `EdgeKind` (was `fact`/`co_occurrence`).
- The timeline shows no model narrative: the API exposes none (tokens are the raw provisional reply).

Tests: pytest `not external` 2338 passed, 110 skipped; vitest 62 passed; ruff clean; tour and smoke
0 CSP violations, 0 page errors.

### 2026-11-04 — Logs: real-deployment defect fixes (levels vs source counts, P19, stored examples, theme path, redactors, anchor/vocabulary, schema violations, index suffixes, planner context, Enter to submit) — DONE (synthetic/mocked)

Base `v2/logs` at `002b213`. Commits: `971cabd` level answers read source counts + P19
`regex_too_long`; `0d7692a` pseudonymised stored examples + theme path; `cf35e24` five redactors;
`b7e3ff4` undated anchor, failure vocabulary, `error_rate`; `d75e3a7` numeric index-name suffix;
`ba25ffa` generator `schema_violation` logged and answered; `f36b856` logs planner context pack;
`dc041b5` UI Enter to submit; `0e22e7c` and the docs commit: docs only. All reproductions use the
synthetic generators; no private line, tenant or host name was copied.

Done:
1. Level questions (`log_top` by service/tenant/signature, `log_trend`, `log_compare`,
   `log_tenants`) return `source_counts` per (service, tenant, level) from `unit_day`
   `count_<query>` (basis `level_field` exact, `token_match` approximate; lines answered, parsed,
   unparsed). Source > answered gives `bounded` with the reason; the rendering lists the counts and
   says "No … in the parsed rows" instead of "none … exact". Gold set plants unparsed FATAL lines.
2. P19: `synthesise` rejects regexes over `MAX_REGEX_CHARS` (4096) as `regex_too_long`; the
   fallback's labelled entry catches the same validation error.
3. Stored examples (learned, proposed, decisions) pseudonymised with the run's `Pseudonymiser` in
   the learning loop and the fallback template tier.
4. `serving.ui.theme` in `_resolve_paths`/`_path_slots`; `config validate` and `doctor` load it.
5. Redactors `phone`, `long_numeric_id` (hash), `person_name` (`person_name_fields`), `record_list`
   (`record_list_fields`), `product_prose` (`prose_products`); off by default like every built-in.
6. Undated questions anchor on the latest served day when today is not composed ("latest available
   day: …"); uncomposed days of a named window are `source_unavailable`.
7. Failure words map to ERROR+FATAL; `log_top metric: error_rate`.
8. Generator `SchemaViolation` logged (paths, error types, stop reason; no reply text) and answered
   with evidence plus an abstain; `SchemaViolation.paths`/`stop_reason`.
9. `index_name.service_suffix: numeric` → `deployment_id`.
10. `decision/log_context.py` planner pack; `logs.intents.{planner_context, planner_context_max_chars,
    cascade_to_planner, few_shot, few_shot_file}`; profile ships 8 synthetic few-shot examples.
11. UI: Enter submits (Ask, Logs, Patterns), Shift+Enter newline, no submit while composing.

Deviations:
- `log_affected` has no `level` argument, so the source-count cross-check covers the level
  intents listed above; `log_top` by `exception_class`/`endpoint` is not cross-checked (no level
  dimension).
- The bounded rule compares source counts with what the rows count; a `level_field` unit whose
  FATAL lines are unparsed but carry the level field is reported (`lines_unparsed`) but not bounded
  for by-service/tenant answers, since those rows already count them.
- "errors" (plural) now means ERROR+FATAL as instructed; two gold `QUESTIONS` expectations changed.
- The default anchor applies only to questions with no window, start/end or `as_of`: a named
  relative window ("yesterday") stays relative to today, otherwise "yesterday" would shift.
- `_days_unavailable` now also lists days after the latest composed day (they used to count as
  "none, exact"); a window with no composed day is `bounded`.
- `product_prose` interpretation: the free text after an allow-listed product or technology name is
  masked to the end of the line; the names stay visible and do not end the masking. The owner's
  intent was ambiguous; this is the reading implemented and documented.
- The egress detector does not use `long_numeric_id`, `person_name` or `product_prose` (ids,
  epoch-ms timestamps and `name=` component keys would refuse whole learning runs); it does use
  `phone` and `record_list`.
- Stored examples that no longer match the regex after pseudonymisation are left out and counted
  (`provenance.examples_dropped_after_pseudonymisation`); with none left the entry is not written.
- Cascade under the logs profile skips the stages before `llm` when the rules find no log intent
  (`cascade_to_planner`, traced as `stages_skipped`); the rules' log-intent detection still runs
  first in every mode.
- The UI bundle was built from a copy under `~/su_mutliarm_tests/claude/ui_build` (npm cache there
  too) to keep `node_modules` out of the worktree; asset hashes changed for every chunk.

Tests: pytest `not external` 2442 passed, 99 skipped (was 2338 passed, 110 skipped at the base on
the logs branch per the U4 entry; this venv has no google-re2). Fresh `[dev]`-only:
1814 passed, 563 skipped, 0 failed, 0 errors. vitest 65 passed (was 62). ruff clean. One existing
test lost and regained `engine_or_skip("re2")` between `0d7692a` and `cf35e24` (an editing slip,
fixed in `cf35e24`).

Follow-ups:
- Re-run the FATAL, default-anchor, vocabulary and planner-context questions on the deployment
  (local model first) and record the measured outcome.
- RE2 runs of the new redactors and pseudonymisation tests (google-re2 not installed here).
- `log_tenants` takes one level, so "tenants with errors" asks ERROR only; a multi-level variant.
- A browser (Playwright) pass of the Enter-to-submit change.

### 2026-11-04 — Logs fix4: second real-deployment smoke (Ask-path day, FATAL-only source counts, error_rate, deployment_id) — DONE (synthetic/mocked)

Base `v2/logs` at `6f8e6a4`. Commits: `9bebebf` (code, tests, CHANGELOG, ARCHITECTURE,
CONFIGURATION) and the docs commit (VALIDATION, this entry). Synthetic reproductions only.

Done:
1. `DecisionRequest.as_of_source` (`request` | `clock`, set by `Engine._decide`); `pinned_as_of()`.
   Rules `detect(pin=False)` and the llm planner keep a clock day out of the log intent's arguments
   (a planner echo of `time_anchor` is dropped; the payload carries `time_anchor_source` and the
   log rule says not to copy it). Undated Ask/stream/planner questions anchor on the latest served
   day; relative days read the clock; an unserved day is `source_unavailable` with
   `latest_available_day` and "No composed data for index date …". `Engine._clock` is the one
   clock (tests pin it).
2. `runtime/log_tools.count_plan` + `source_counts`: every level/mix is cross-checked (single-level,
   exact-set, or smallest superset query, `coverage`); uncovered levels give `unknown_level` and
   `bounded` when units have lines with no parsed level. `bounded_by` lists every reason.
3. `log_top metric: error_rate` compiles over `unit_day`: source count fields / `lines_total`,
   units without them excluded and named, refusal when no count query counts exactly the levels;
   per-row `basis`/`rate_exactness`, output exact or approximate by basis.
4. `deployment_id` on unit_day/signature_day and the analytics kinds; profile field; `log_top`
   (`by: deployment`, `deployment_id`), `log_tenants` (`by`, `deployment_id`), `log_trend`
   (`deployment_id`); rules "deployments"/"instances"; planner pack vocabulary. `COMPOSE_FORMAT` 2.

Deviations:
- A scope groups by two fields at most, so a by-deployment answer groups (service, deployment_id)
  and its source counts are per (service, deployment_id), not per tenant.
- For a level no count query covers, the plan's "use unit source counts" is read as: compare with
  the units' lines of unknown level (and their share of `lines_total`); this makes WARN answers
  bounded on gold days whose token-only units hold raw lines (two existing parametrised cases).
- A superset query (ERROR+FATAL for a FATAL question) bounds the answer when the source counts more
  ERROR+FATAL lines than the rows: an ERROR-only answer is bounded by svc-legacy's unparsed FATAL
  lines too, because the combined count cannot separate them.
- `error_rate` refuses rather than excluding units when a level has no exact count query (the field
  is missing for every unit); units lacking `lines_total` or a count field are excluded and named.
- The error rate's exactness follows its basis, not `unit_day` `row_exactness`: a capped unit's
  source counts are exact even though its pulled rows are sampled.
- `COMPOSE_FORMAT` bumped 1 → 2 (not asked): without it the day-document cache would serve cached
  rows without `deployment_id` after an upgrade.
- When source counts cannot be read (backend error) a level answer is now `bounded` (was a note only).

Tests: `not external` 2473 passed, 99 skipped (was 2442 / 99). ruff clean. Schema unchanged
(`write_all` produced no diff). Fresh `[dev]`-only install of the archive of `9bebebf`
(isolated venv, non-editable, package from site-packages): 1826 passed, 582 skipped, 0 failed, 0
errors (the gold-set contract tests skip there: no communities extra).

Follow-ups:
- Re-run the smoke on the deployment (recompose once for `deployment_id`), and the persona eval.
- `log_tenants` still takes one level; `log_coverage`/`log_endpoints` do not take `deployment_id`.
- The token phrase in `source_counts` uses the level names as the words; the query's own `tokens`
  are not in the compile context.

### 2026-11-04 — Logs fix5: live follow-ups (source-count rankings, rendered ranking answers) and persona-eval fixes — DONE (synthetic/mocked; item 6 not done)

Base `v2/logs` at `84bf49d`. Commits: `beec634` (rankings by source counts, rendered answers),
`af9dffd` (labelled counts, sentence splitter, tenant words/filters, compare order), and the docs
commit (VALIDATION, this entry). Synthetic reproductions only.

Done:
1. `runtime/log_tools.source_ranking`: `log_top` by service/tenant/deployment at asked levels ranks
   by the source counts when any group has more source than parsed lines (`ranked_by`,
   `lines`=source, `lines_parsed`, `lines_unparsed`, `unparsed_share`); signature rankings state the
   unparsed share and top unparsed services. `GoldSpec.error_flood` plant.
2. `Engine._rendered_aggregates` + `AnswerContract.rendered_answer`: aggregate-only answers are the
   renderings with exactness labels; optional one-sentence generator narrative checked by
   `numbers_outside` (`answer_number_mismatch`, strip). `AGGREGATE_NARRATIVE_PROMPT`.
3. Labelled counts on approximate/bounded aggregates pass (`has_label`, `labelled_backing`);
   generator prompt asks for labels.
4. Rules: customer/client/account words → tenant; planner pack rules and profile few-shot.
5. `tenant` on `log_trend`/`log_compare`; change words → compare; periods in date order.
7. `_SENTENCE` no longer splits inside identifiers/decimals.

Deviations:
- Source ranking applies only when the parse is incomplete (title of the request); a complete
  parse keeps the parsed ranking so a token_match source count never replaces exact parsed rows.
  Needs count queries over exactly the asked levels (a superset query would over-count).
- Rendered answers apply to log-intent aggregates always, and to generic aggregates only without a
  generator or when nothing was packed; generic aggregates that fit still go to the generator.
- The narrative is dropped (not cut) when it has more than one sentence; traced, no violation rule.
- `_levels_of` now treats a `log_top` without `by` as by service (it skipped the cross-check).
- Item 6 (planner schema repair re-prompt, then fallback) NOT implemented: the slice ground rules
  forbid repair and fallback; needs an explicit owner decision.
- Item 7 cause: the sentence splitter (identifiers with dots); `max_tokens` truncation not measured.
- Per-tenant comparison ("which tenant increased") is not an intent: `periods` cannot be grouped.

Tests: `not external` 2523 passed, 99 skipped (was 2473/99). ruff clean. Schema unchanged. Fresh
`[dev]`-only install of `af9dffd`: 1859 passed, 736 skipped, 0 failed, 0 errors.

Follow-ups:
- Re-run the persona eval and the smoke on the deployment (local model first).
- Owner decision on planner schema-violation handling; a per-tenant comparison intent; a
  share-of-total intent ("what share of errors comes from X").

## V3.2 candidate: decision precision and metadata share — 2026-11-08 (branch `v2/phase-0`, `e077137`)

Done:
1. `llm` agent drops unsolicited structured scope steps (no intent, no operator word, no caller
   aggregate/scope), recorded on `Decision.dropped_steps` / `Decision.fallback` and traced; lone
   drop falls back to lexical/dense with the original query (request filters applied by the engine).
2. Field-qualifier binding for string/id fields; negation cues produce negated candidates.
3. Neutral planner prompt guidance (retrieval for lookups, structured only for aggregates, negation).
4. `PackingConfig.metadata_max_share` (0.25) with per-record `metadata_dropped_keys`.

Deviations:
- "No aggregate intent" also requires no aggregate operator word (`mentions_aggregate_operator`),
  because the targeted pre-pass misses e.g. "Count open tickets" and "numbers by component"; those
  keep the old behaviour (guards intact). Broader than the diagnosis asked; safer.
- The metadata share binds only when passage + metadata exceed the record allocation; a record that
  fits entirely keeps all keys (a literal per-allocation cap would strip metadata from short records).
- `metadata_omitted_ids` now lists records that lost keys (records are kept), not omitted records.
- `no` is not a negation cue (absence operator). Positive planner retrieval filters contradicting a
  negated phrase are not checked (follow-up).

Tests: unit+contract 2703 passed, 99 skipped, 2 pre-existing failures (was 2676/99/2). Ruff clean.
Schema regenerated. Wheel/sdist built externally.

Follow-ups: real-data rerun of the 23 dev questions to measure the refusal change; retrieval-filter
vs negated-qualifier check.

## Typed-column query route MVP M1, M3-M5 — 2026-11-08 (branch `v2/phase-0`, plan `typed_query_arm.md` rev 2)

Commits: `e05a5d9` (M1), `0e0efb2` (coordinator addition: `metadata_max_share` default 1.0 and
the V3.2 paid-evaluation aggregates in VALIDATION), `f44f1d1` (M3), `b2630f1` (M4), `915101e` (M5),
plus this log entry. Evidence level: synthetic only (repository fixtures, external workspace).

Done:
1. M1: `pipeline.decision.on_step_failure` / `LlmConfig.on_step_failure` (`abstain` default |
   `partial` | `retrieval_only`); `DroppedStep.reason` gains `step_not_executable` and
   `planner_not_answerable` (reserved); aggregate intents of dropped steps stay unsupported.
   V3.2 follow-up: qualifiers a dropped unsolicited scope had bound are carried as typed `eq`
   retrieval filters (positive, filterable, resolved, one value per field, no request-filter
   conflict) or reported as `qualifier_not_applied` (`DroppedStep.carried_filters`/`unapplied`).
2. M3: `core/types_typed.py`, additive `UnsupportedReason` values, `typed_query` in `ALL_ARMS`,
   `FieldSpec.description`, `projection` config + `pipeline.arms.typed_query`, cross-key validation,
   v1 refusal, schema regenerated.
3. M4: `projection/{spec,builder,catalog,store}.py`; `relational` capability/contract on sqlite and
   memory with a conformance check; write-path hook, generation placement, verification before
   activation, deletion with the generation.
4. M5: `decision/typed_query.py`, `decision/typed_sql.py`, `arms/typed_query.py`; engine binding.

Deviations from the plan:
- `on_step_failure` is a flat `pipeline.decision.on_step_failure` key (the existing decision keys
  are flat; the plan wrote `pipeline.decision.llm.on_step_failure`). Valid only with an llm stage.
- `retrieval_only` drops every structured step (including ones that compiled) once one fails;
  their `DroppedStep.reason` is `step_not_executable` with a detail naming the policy. Log-intent
  steps are dropped too under `retrieval_only`. Retrieval-step filter errors still fail closed.
- Dropped-step qualifiers: when the planner's own retrieval steps remain, carried filters are not
  injected into them; a bound qualifier they do not all filter on is reported unsupported.
- `pipeline.decision.typed_query` (`enabled`, `repair_attempts`) is not added: it is the M6 planner
  surface and would be a key with no reader. `share` and temporal (calendar) groups are refused in
  the typed MVP (`measure_unsupported`); `select` defaults to every projectable grain column.
- `relational` was added to `CAPABILITIES`, so `memory` serves it too (bytes in state) to keep the
  "memory/sqlite declare every capability" invariant; README/docs capability tables regenerated;
  the doctor-probe contract set gained `relational`.
- `INCREMENTAL["rebuilt"]` gains `projection` only in manifests of projection-enabled builds (the
  constant is unchanged so existing manifests stay byte-identical).
- Dangling child keys are counted per file (`dangling_by_file`), and a child-grain join is `bounded`
  only when the granted file has some; NULL parent keys count as unresolvable.
- Contributors: at most 8 ids per aggregate overall, not per group. Ranking ties at a `top_n` cut
  are flagged (`ties_at_cut`, bounded) rather than listed.
- Canary/differential suites use seeded `random` (5,000 queries each), not hypothesis (not
  installed). M1 tests patch `LlmAgent.call_model` (existing V3.2 convention) rather than a fake
  `JsonModel`.
- `casefold_match` in SQL uses SQLite `lower()` (ASCII folding) vs Python `casefold()` in the record
  stores; equal on ASCII only (documented).

Tests: unit+contract 2,787 passed, 99 skipped, 2 pre-existing failures (was 2,703/99/2);
conformance 72 passed, 144 skipped. Ruff clean. Wheel/sdist 2.0.0a3 built in the external workspace.

Follow-ups / open:
- M2 and M7 (fresh-set evaluations, gate G1) need the owner's budget and pre-registration; M6
  (planner schema, catalog enums, descriptions/samples, answerability, repair) not started.
- Engine does not yet pack typed evidence (no plan step reaches the arm before M6).
- The projection build is in memory (L4 streaming); per-group contributors; non-ASCII casefold.

## Typed-column query route M6: planner integration — 2026-11-08 (branch `v2/phase-0`, plan `typed_query_arm.md` rev 2)

Base `e1e925b`. Commits: `8d683ef` (M6 code, tests, schema, ARCHITECTURE/CONFIGURATION/VALIDATION)
plus this log entry. Evidence level: synthetic and mocked only (fake planner/generator).

Done:
1. Config `pipeline.decision.typed_query: {enabled: false, repair_attempts: 0}` (needs the llm
   planner and the pipeline's `typed_query` arm; repair needs `enabled`); `LlmConfig.typed_repair_attempts`.
2. `decision/typed_plan.py`: `TypedPlanning`, `typed_payload` (described columns, operations,
   relation descriptions, ACL-scoped samples <= 20 for listable fields under cap, no row counts),
   `typed_step_model` (catalog enums for every identifier), `typed_key`, qualifier helpers,
   failure-category mapping, repair prompt.
3. `capability_plan_schema(arms, log_intents, typed=None)`; `LlmStep._coherent` handles
   `typed_query`; `LlmAgent` payload/attempt/repair; `TypedStep` in the `Plan` union;
   `Capabilities.typed` (excluded); `check_qualifiers` covers typed steps; raw_all_arms never runs
   typed steps.
4. Engine: `_typed_planning` per request (trace `typed_query_offered`), `_run_step` for
   `TypedStep` (request ACL + immutable filters), `_typed_extra` packing (<= 50 rows with
   `(document_id, field)` citations or <= 50 groups; more -> `bounded`, `result_truncated`); arm
   evidence gains `denominator`.
5. Tests: `tests/unit/test_typed_planner.py` (29), `tests/contract/test_typed_query_engine.py` (10),
   `tests/contract/test_typed_question_suite.py` (36 frozen questions + size check).

Deviations from the plan / task:
- Answerability keeps the M3 contract (`answerable | partial | not_answerable` + `gaps`) rather
  than a two-valued `not_answerable_with_catalog`; `PlannedTypedStep` now requires non-empty gaps
  (the reason) for `partial`/`not_answerable` and caps each gap at 300 characters. `partial`
  executes and is reported `bounded` with a `qualifier_not_applied` constraint.
- Model text (`analysis`, `gaps`) is traced masked (`Sensitive`) and never put into
  `DroppedStep.detail` or user-facing constraints (they carry counts and fixed wording only).
- `not_answerable` with nothing else planned uses the existing recorded
  `retrieval_after_dropped_steps` fallback (lexical/dense, original query), or abstains with
  `no_plan` when neither arm is enabled.
- Under `partial`/`retrieval_only` every typed compile error is droppable (the M1 structured rule
  only drops `unsupported_measure`/`unresolvable_value`), per plan §5.4; contradicted request
  filters still fail closed. `retrieval_only` drops typed steps with structured ones.
- Repair rule: accepted only if the re-plan's typed steps bind every qualifier the failed drafts
  bound, or the planner now says `not_answerable`; a re-plan without a typed step is rejected; a
  failed repair call leaves the original plan (traced). Request filters cannot be dropped because
  the compiler always applies them.
- Typed aggregates on questions without an aggregate operator word get `NO_OPERATOR` (bounded),
  as structured scopes do; typed lookups/lists do not. A field named without a value ("the status
  of ...") counts as expressed when it is selected or grouped.
- "Sensitive fields": the schema has no sensitivity flag, so samples use the existing gate only
  (filterable + listable + under `max_catalog_cardinality`, ACL-scoped); the key is never sampled.
- Typed evidence uses `PackExtra.source = "structured"` (the generator schema and contract treat
  that source as aggregate evidence) with `arm: typed_query` in the passage. No deterministic
  rendering for typed results (generator path).
- Rules agent unchanged (the plan's single-table rules drafts are not implemented).
- The decision-time compile uses the request ACL too (validation); the arm recompile remains
  authoritative.

Tests: unit+contract 2,863 passed, 99 skipped, 2 pre-existing failures (was 2,787/99/2);
conformance 72 passed, 144 skipped. Ruff clean. Schema regenerated. Wheel/sdist 2.0.0a3 built in
the external workspace. Housekeeping: a few ad-hoc `python -c` checks during the slice ran without
`PYTHONDONTWRITEBYTECODE` and wrote ignored `__pycache__` files into the repo; those files were
removed.

Follow-ups / open:
- M2 and M7 (fresh evaluations, gate G1) need the owner's budget and pre-registration.
- Deterministic renderings for typed results; per-table samples; rules-agent single-table drafts;
  Anthropic output-format complexity of the typed plan schema on large catalogs (falls back to
  prompt mode when over the ceiling; not measured on a real catalog).

## log-policy/P1 — declared collection policy and long-term episode archive (2026-11-08)

Branch `v2/phase-0`, commit `7cbd249`. Evidence level: synthetic only (unit and contract tests;
no source, deployment or live service touched).

Done:
1. Config `logs.collection` (`core/config_log_policy.py`): `schedule.{summary_refresh_minutes,
   heavy_window_utc{start,end}, day_close_delay_minutes}`, `keywords.{files[{path,sha256}],
   effective_from, gap_seconds}`, `archive.{enabled, path, max_text_chars}`,
   `staging.{path, delete_after_import}`, `serving.max_window_days`. Retention per class reuses
   `logs.retention`; the archive requires `derived: forever`. Paths resolve against the config
   file. Schema regenerated.
2. `ingestion/episode_archive.py`: append-only day shards of the summary aggregate kinds in their
   own sqlite file (H1 layout via `SqliteHistoryStore.day_kinds`), hash-chained archive log with a
   head file, gap records, staging import, live-window rows pruned at `keep_days`, `publish`.
3. `ingestion/log_policy.py`: pinned keyword recipe, schedule rules, planned jobs and systemd text,
   report and verify; CLI `logs policy report|verify|import|record-gap|gate`, `--emit systemd`.
4. `Engine.history_route` (archive or compose history), records router enabled for the archive;
   `incident_episodes` window bound from config, midnight stitching, epoch/gap notes, `category` in
   rows; `summary_documents(..., citations=True)` adds `evidence_refs` (off by default).
5. Tests: `tests/unit/test_log_policy.py` (34), `tests/contract/test_episode_archive_serving.py` (6).
   `test_retention_scopes_and_raw.py`: `log_policy.policy_cli` added to the pinned raw-gateway
   importers (it lists raw day keys for `verify`; no reads).

Deviations / decisions:
- New `logs.collection` block rather than spreading keys over `logs.schedule`/`logs.history`:
  `logs.schedule` stays the lock TTL; the per-source request-rate cap is not duplicated, the policy
  reports `sources.<name>.limits.requests_per_second` (already enforced by the transport).
- The episode archive is a separate file and history store, not the compose history file: compose
  garbage-collects its history by generation reference, which would delete archived days. The
  archive refuses `gc`; archive + `logs.history.tier` in one dataset is refused.
- Trimming applies when a day is archived (shards are immutable), not when it later ages out; the
  live window (`keep_days`) keeps every document untrimmed in the generation for search/graph/
  communities, while structured scopes over archive kinds always read the archive (no double count).
- Episodes are closed per UTC day (never across a day boundary); a cross-midnight episode is two
  archived rows joined at read time by `incident_episodes` (within `gap_seconds`).
- "Recapture" on a keyword change is an explicit epoch (`keywords.effective_from` after the last
  archived day); archived days are never rewritten.
- A day closes only from full-day snapshots (`[D 00:00Z, D+1 00:00Z)`); partial rolling snapshots
  must be re-captured for the whole day in the heavy window.

Tests: unit+contract 3,063 passed, 1 skipped, 1 failed (was 3,023/1/1; the failure is the
pre-existing docs denylist hit in `docs/plans/unified_su_platform.md`). `google-re2` installed into
the local venv, so `test_episode_summaries` runs and passes. Ruff clean. Wheel/sdist 2.0.0a3 built
in the external workspace.

Follow-ups: pushdown of `first_hour`/`last_hour` for archive scans; communities over archived
incidents beyond the live window; a reference collector that writes full-day staging snapshots;
measurement at real archive sizes.

## SQL surface of `typed_query` (NVIDIA Auto-Ontology port) — 2026-11-08 (branch `v2/sql-surface` from `v2/phase-0` `e1e925b`)

Owner decision 2026-11-08: copy NVIDIA Auto-Ontology's catalog + text-to-SQL generation as an
opt-in `sql_surface` mode of the `typed_query` arm, executed behind our safety layer. Commit
`2331950` (code, tests, docs, schema) plus this entry. Evidence level: synthetic + mocked model
only. Done in an isolated worktree; M6 (planner integration) was in progress elsewhere and is not
touched.

Provenance: upstream NVIDIA/auto-ontology `c447003f72a822301e927196a4ecb5b597c308d5` (read-only
checkout in the external workspace; `LICENSE` Apache-2.0 verified, `THIRD_PARTY_NOTICES.md` and
`OPEN_SOURCE_NOTICE` read). Adapted files keep the NVIDIA copyright + SPDX header and a
"Modified by su_multi_arm contributors, 2026-11-08" note: `sql_surface/catalog.py`
(`semantic/models.py`, `utils/embedding_rows.py`, `formatters_util.py`), `sql_surface/prompts.py`
(`text_to_sql/prompts.py`, `agents/sql_reconstruction.py`), `sql_surface/values.py`
(`agents/sql_value_validation.py::extract_value_checks`), `sql_surface/planner.py`
(`agents/sql_from_semantic.py`, `sql_reconstruction.py`, `sql_unconstructable.py`,
`text_to_sql/models.py::SQLGenerationModel`, the MCP `check_answerable` idea). Ours:
`sql_surface/guard.py`, `sql_surface/executor.py`. `NOTICE` and `docs/THIRD_PARTY_NOTICES.md` added.

Done:
1. Config: `pipeline.arms.typed_query.mode: ir | sql_surface` (default `ir`) and
   `pipeline.arms.typed_query.sql_surface {role, repair_attempts 0|1 (default 1), max_sql_chars,
   sample_values, max_joins, max_subqueries}`; cross-key check: the role must be a configured chat
   role; `validate` reports `extra_missing` without sqlglot. Extra `su-multi-arm[sql-surface]`
   (`sqlglot>=30,<31`; also in `all`). Schema regenerated.
2. Catalog per principal from the sealed `ProjectionCatalog` + `FieldSpec.description`; samples only
   for listable/enumerable non-access columns through `ValueResolver.observed` (ACL-scoped).
3. Guard: comment-free single `SELECT` (CTEs), node/function/join allowlists, catalog tables by bare
   name, `qualify` against the catalog, literal grounding (enum/observed/exists; no fuzzy, no
   rewrite), request filters + ACL bound into every table reference, named parameters, outer
   `LIMIT min(model, row_limit + 1)`, hidden id/access columns for row-level results, a fail-closed
   "every table bound" invariant.
4. Execution via `projection/store.open_readonly` (new optional `functions=` allowlist; default list
   and `json_each` unchanged for the IR) on the file `TypedQueryArm.file_for` selects (absent/empty
   grant: an empty database of the projection schema); returned-row tenant check; evidence with
   (document id, table, field, generation) citations or `sql_derived` aggregates with the
   parameterised SQL, masked params, fingerprint and `acl_key` in `sql_surface` lineage.
5. Planner: `SqlSurfacePlanner(session).plan(question, catalog, role) -> SqlSurfaceResult`;
   `engine_session(engine, principal)`, `open_session(arm, ctx, acl)`, `role_model(engine)`.
6. Hooks in existing modules (small, additive): `ArmInput.sql`, `UnsupportedReason`
   `sql_refused`, `TypedQueryArm._run`/`validate` dispatch by mode, `open_readonly(functions=)`,
   `projection_problems` role check.

Deviations from the plan / task:
- Plan §3/§11 and L1 said model SQL is parsed into the IR and never executed. Per the owner's
  2026-11-08 decision the SQL is executed, but only after validation, ACL/filter binding and full
  parameterisation by the guard (never as written). Recorded in ARCHITECTURE.
- Set operations (`UNION`/`INTERSECT`/`EXCEPT`) are refused (task: "exactly one SELECT"); upstream
  allowed them. Repairable, so a model can rewrite; the file layer was also tested with a raw UNION.
- Qualified names (`main.t`, 3-part) are refused (repairable), not stripped.
- Unknown filter values are terminal (`unsupported`, never repaired); a case-only near miss names
  the stored value but is not rewritten. `LIKE`/`instr` are accepted only on free-text columns.
- Datetime literals: only `YYYY`/`YYYY-MM`/`YYYY-MM-DD` (ranges) or the exact stored form are
  accepted, because the stored form compares lexicographically; equality with a partial value is
  refused.
- Upstream's three retries on an empty model reply, error classification, table discovery, value
  probing/substitution and empty-result repair are not ported (no retry-until-pass, no repair of
  values).
- Samples are per field across kinds (the `ValueResolver` universe), as for the IR.
- `role` is limited to `planner | reviewer | generator`; the default budget is a `CallBudget` of
  `1 + repair_attempts` calls (a request meter can be passed).
- Aggregates carry no contributor ids (typed IR aggregates do); exactness is `bounded` whenever a
  referenced column has rejected values in the generation (manifest counts, conservative).

Tests: unit+contract 2,924 passed, 99 skipped, 2 pre-existing failures (base `e1e925b` in the same interpreter, extracted without git:
2,787 passed, 100 skipped, 1 failed: `test_release_identity` skips without git). New: 137
(`tests/unit/test_sql_surface_guard.py` 95, `tests/unit/test_sql_surface_catalog.py` 9,
`tests/contract/test_sql_surface.py` 33). Canary (ACL binding off, 5,000 seeded statements): 3,115
executed (1,709 rows, 1,406 aggregates), 1,885 refused, zero cross-tenant rows/ids/values. Ruff
clean. Wheel/sdist 2.0.0a3 built in the external workspace; the wheel imports and refuses cleanly
without sqlglot. Tests run with `sqlglot` 30.30.0 installed into the external workspace only
(`pip --target`), `PYTHONPATH=<worktree>/src:<workspace>/site`; without sqlglot they skip.

M7 entry point: config `pipeline.arms.typed_query: {mode: sql_surface, sql_surface: {role:
planner, repair_attempts: 1}}` with `projection.enabled` and `models.roles.planner`; then
`session = engine_session(engine, principal)`;
`SqlSurfacePlanner(session).plan(question, session.catalog, role_model(engine))`.

Follow-ups: planner routing (M6 integration of the mode), contributor ids for SQL aggregates,
evaluation on the fresh set (M7, owner budget), a broader SQLite subset (set operations,
window functions) only on measured need, sqlglot upper-bound review on each release.

## `typed_query` SQL route wired to the `llm` planner — 2026-11-08 (branch `v2/phase-0`, base `de56e47`)

Commit `a889ccc` (code, tests, docs) plus this entry. Evidence level: synthetic and mocked only
(fake `JsonModel.call`, one `httpx.MockTransport` run through the real provider and meter).

Done:
1. Planner: `TypedPlanning.mode` (`ir` | `sql_surface`, from the arm); in `sql_surface` the payload
   section is `{mode, catalog, rule}` (same described catalog/samples as `ir`, `SQL_ROUTE_RULE`) and
   the wire step's `typed` is `PlannedSqlRoute {analysis, answerability, gaps, question}`
   (`core/types_typed.py`; key `SQL_ROUTE_KEY`). `TypedStep` carries exactly one of `draft` |
   `question`; nothing is compiled at decision time; planner `not_answerable` unchanged from M6.
2. Execution: `sql_surface/route.py` + `Engine._route_sql` (before the plan's steps run, in
   `_retrieve`, so search, answer and stream share it): session with the request's ACL and
   filters, `RunBudgetMeter(ctx, "sql_surface", role.price_key())`, `SqlSurfacePlanner.plan`, the
   arm's guard/executor. Executed evidence is cached per step for `_run_step`; packing via
   `Engine._sql_extra` (<= 50 rows, `(document_id, table.field)` citations, parameterised SQL).
   Review-hop routes run in place; a non-executed one is a refused, bounded aggregate record.
3. Failures: `DroppedReason` gains `sql_refused`, `sql_unsupported`, `sql_error` (writer
   `not_answerable` -> `planner_not_answerable`); `UnsupportedReason` gains `sql_route_failed`;
   `route.apply_failures` applies `on_step_failure` (abstain -> `on_failure` abstain/raw_all_arms;
   partial; retrieval_only; recorded `retrieval_after_dropped_steps`, else abstain).
4. Qualifiers on the executed SQL (`route.executed_unapplied`); the guard records
   `checked_values[].filter` (`positive` | `negated` | `other`); only conjuncts of the outermost
   WHERE/HAVING count. `NO_OPERATOR` for an aggregate result of a question without an operator.
5. Config: budget validation counts one SQL call per answer in this mode;
   `pipeline.decision.typed_query.repair_attempts: 1` with `mode: sql_surface` is refused (no
   reader). SQL evidence gains `denominator`. No new keys; schema regenerated without change.
6. Tests: `tests/contract/test_sql_route_engine.py` (29), `tests/unit/test_sql_route_planner.py`
   (11), `tests/unit/test_sql_surface_guard.py` +10; `test_sql_surface.py` lineage assertion updated
   for the new `filter` key (same coverage).

Deviations from the task / plan:
- The SQL writer's `not_answerable` (execution time) follows `on_step_failure` as the task states,
  so under the default `abstain` it abstains; the planner's decision-time `not_answerable` keeps
  the M6 behaviour (recorded drop + retrieval fallback whatever the policy). With `partial` or
  `retrieval_only` both modes behave the same.
- Routing runs in an engine pre-execution hook (not inside the arm): the policy must be able to
  drop other steps and add a fallback before anything executes; the arm's API is unchanged.
- Error status maps to a new `UnsupportedReason` `sql_route_failed` (no existing reason fitted);
  `BudgetExhausted`/`DeadlineExceeded` from the SQL call are route errors (`budget_exhausted` /
  `timeout`), not request failures.
- Qualifier coverage is checked against the executed SQL rather than the sub-question text
  (stricter); conservative for SQL that filters in derived tables or subqueries (bounded).
- `max_llm_calls` must now allow planner + SQL writer + generator in this mode (validation).

Tests: unit+contract 3,258 passed, 1 skipped, 0 failed (HEAD `de56e47` extracted without git:
3,207 passed, 2 skipped). Ruff clean. Run with `sqlglot` 30.30.0 from the external workspace.

Evaluation keys (ir vs sql_surface, everything else equal): `pipeline.decision.kind: llm`,
`pipeline.decision.typed_query.enabled: true`, `pipeline.decision.on_step_failure` (same value
in both runs), `pipeline.arms.typed_query.mode: ir | sql_surface`,
`pipeline.arms.typed_query.sql_surface.{role, repair_attempts}`; `projection.enabled` and
`models.roles.planner` as for M6.

Follow-ups: M7 fresh-set comparison (owner budget); contributor ids for SQL aggregates;
deterministic renderings of typed/SQL results; the IR re-plan and the SQL repair are different
mechanisms (repair parity is a choice of the evaluation, not equal by construction).

## Typed router, protected metadata, arm cards (M7 follow-ups) — 2026-11-08 (branch `v2/phase-0`, base `3b4329e`)

Commit `7402465` (code, tests, docs, schema) plus this entry. Evidence level: synthetic and mocked
only (fake `JsonModel.call`; `httpx.MockTransport` through the real provider). Motivation: M7
real-data aggregates (metadata share 1.0 cut the passage share; typed catalog in the planner request
raised schema violations and the planner rarely chose `typed_query`).

Done:
1. `selection.packing.metadata_protected` (subset of `metadata_fields`, default `[]`): never dropped
   by the share cap, still counted; traces `metadata_protected_over_cap_ids`,
   `metadata_protected_overflow` (protected keys over the whole allocation: passage trimmed to
   nothing, overflow characters recorded). `metadata_max_share` default back to 0.25 (1.0 recorded
   as the M7 regression in CONFIGURATION/ARCHITECTURE). Unprotected keys drop in reverse allowlist order.
2. `pipeline.decision.typed_query.routing: planner | router` (default `planner`, unchanged) and
   `router_role` (`planner` default | `reviewer` | `generator`). `decision/typed_router.py`
   (`TypedRoute {route, sub_question, reason}`, compact `catalog_summary`, stage `typed_router`;
   IR draft call stage `typed_ir`); `Engine._typed_route` (before the planner, request ACL/filters),
   `_apply_typed_route` (pre-executed first step, original-question qualifier check, DroppedStep on
   failure), `pack(..., leading=)` + trace `leading_ids`. `DroppedReason` + `typed_router_error`,
   `typed_route_discarded`; `UnsupportedReason` + `typed_route_failed`. Budget validation: router +
   writer (+ SQL repair) + planner + generator. Cross-key: router role must be configured.
3. `pipeline.decision.arm_cards` (opt-in): `decision/arm_cards.py` cards per enabled arm with
   wire-schema input shapes and config limits, in the planner payload (`arm_cards`), the router
   payload (`routes` + detailed catalog) and the SQL writer prompt (limits block via
   `route.run(..., arm_cards=)` / `SqlSurfacePlanner(limits=)`).
4. `ProjectionRelation` dumps `from` (`serialize_by_alias`): `Config.model_dump` round-trips.

Deviations / decisions:
- "Equivalent to partial": a non-executed typed route adds its constraints and one
  `aggregate_intent_without_scope` per aggregate intent even when the planner planned its own
  structured count (as `apply_failures` does under `partial`). This can make the contract refuse a
  structured count after a failed route; chosen as the fail-closed reading of the task.
- A router call failure adds a `DroppedStep` (`typed_router_error`) but no constraint (no typed
  step was chosen). An executed route is used only on a `decided` planner decision; under
  abstain/raw_all_arms it is recorded `typed_route_discarded` (raw_all_arms never runs typed steps).
- Mode `ir` under the router needs a second call (stage `typed_ir`, the router role) to write the
  draft; the router schema stays three fields. A filter-contradicting IR draft is dropped
  (`step_not_executable`) instead of failing the decision (the main-planner path fails closed).
- Budget validation counts the SQL repair round for the router (the task said "+repair"); the
  planner-routing count is unchanged (repairs not counted there).
- `routing: router` refuses `typed_query.repair_attempts: 1` and `router_role` is refused unless
  routing is `router` (no keys without a reader). The router runs only when the request's decision
  mode uses the llm.
- Arm cards: the structured card has no config limits (none apply beyond the payload's
  `filterable_fields`); enum shapes list up to 12 values. Token sizes are chars/4 estimates.
- "Kept first" for protected keys is read as "kept with priority"; evidence metadata keeps the
  allowlist order (no reordering).

Tests: unit+contract 3,309 passed, 1 skipped, 0 failed (was 3,258/1/0). New:
`tests/unit/test_metadata_protected.py` 10, `tests/contract/test_typed_router_engine.py` 29,
`tests/contract/test_arm_cards.py` 12; `test_question_packing.py` default assertion updated (0.25,
same coverage). Ruff clean. Schema regenerated. Measured synthetic prompt sizes (chars, off -> on
cards): planner 4,382 -> 9,595; router 1,281 -> 4,996; SQL writer 7,939 -> 8,531.

Evaluation keys (router + sql_surface + protected metadata): `pipeline.decision.kind: llm`,
`pipeline.decision.on_step_failure: partial`, `pipeline.decision.typed_query: {enabled: true,
routing: router, router_role: planner}`, optional `pipeline.decision.arm_cards: true`,
`pipeline.arms.typed_query: {mode: sql_surface, sql_surface: {role: planner, repair_attempts: 1}}`,
`pipeline.selection.packing: {metadata_fields: [...], metadata_protected: [...],
metadata_max_share: 0.25}`, `budgets.max_llm_calls >= 5`; `projection.enabled` and
`models.roles.planner` as before.

Follow-ups: real-data rerun (router accuracy, planner violations, passage share with protected
keys); measured token counts with the provider tokenizer; whether the `partial`-equivalent
aggregate flag should be suppressed when the planner's own structured step backs the count.

## unified-kg/I1 — day ledger, provisional days, tiered `issue_history` — 2026-11-08 (branch `v2/phase-0`, base `10c3a4e`)

Commit `6c72d8f`. Evidence: synthetic only.

Done:
1. Config `logs.collection`: `schedule.close_after_days` (1), `schedule.grace_days` (2),
   `provisional.{enabled, path}`, `serving.default_view_days` (3). Refusals: provisional without
   archive, default_view_days > max_window_days, provisional path overlapping staging/history/archive.
2. `ingestion/provisional.py` (per-scope versions, projection file per day version, CURRENT.json,
   age-bounded GC, lock), `logs policy refresh`, refresh job in `planned_jobs`/`--emit systemd`.
3. `ingestion/day_spill.py` + `SummaryBuilder` (streaming `summary_documents`, identical docs and
   shard ids); `close_day` consumes an iterator one snapshot at a time.
4. `ingestion/episode_history.py`: typed tables, count rule, `history_log` row-hash chain (checked by
   `check_chain`/verify), `tier_map`, `TieredAggregate`.
5. Archive: partial close, in-order `import_staging` with `_expected.json`, overdue gap, pre-I1
   backfill, hour pushdown, `archive_days` additive columns, `closed_partial` log entries, manifest
   `logs.ledger`, archive serving pointer (`<archive>.serving.json`) used by `history_route`,
   `issue_history` and verify; `publish` advances it first.
6. `issue_history` LogIntent + tool (runtime/issue_history.py), rules detection gated by
   `Capabilities.log_history`, `history` field on `LogToolOutput`, capabilities `logs` block.

Deviations / decisions:
- `close_after_days` defaults to 1 (behaviour before I1), not the plan's 3; the ELK config sets 3.
- Ledger split: archived days are pinned by the archive serving pointer (log seq), not only by the
  generation manifest, so a closed day is served without rebuilding the search generation
  (coordinator requirement: publish work proportional to one day). Provisional days are pinned by
  the provisional `CURRENT.json`, read once per request before the pointer. Manifest key is
  `logs.ledger` (`logs.days` was already a list). `history_route` re-reads the pointer per call
  (a pointer advance during one request can add a day between two steps of that request).
- `live_publish_interval_minutes` not added (no reader in I1: provisional days are not put into the
  search generation). The live generation publish is still a full rebuild of the `keep_days` window
  (bounded by keep_days, independent of N); making it incremental is I2 (`text_route`).
- `incident_episodes`, search, graph and communities do not see provisional days; `issue_history`
  is the one endpoint over provisional + archived days in I1.
- An archive day closed while D-1 has no typed rows gets `boundary_unknown` instead of refusing an
  out-of-order close; import itself closes strictly in order and stops at the first pending day.
- "ML" grounding: a question word equal to a category value's leading token selects those values
  (deterministic, stated in notes); needs `category` listable.
- UI: no bundle rebuild (needs npm install); the client-issue view already defaults to "last 3
  days"; `/v2/capabilities` now reports `logs.default_view_days` for a later UI change.
- Close memory test asserts a Python-heap (tracemalloc) bound, not RSS.

Tests: unit+contract 3,357 passed, 1 skipped, 0 failed (was 3,309/1/0). New:
`tests/unit/test_episode_history.py` 36, `tests/contract/test_issue_history_tiers.py` 12. Ruff clean;
schema regenerated.

Follow-ups: real-data shadow week (close RSS/time at ~80k rows/day, 3/6/12-month p95, real
cross-midnight counts); incremental live-generation publish (I2); provisional days in
`incident_episodes`/SQL route (I4); UI bundle reading `default_view_days`.

## su-compat — legacy QA contract adapter, paged listings, per-request evidence budget — 2026-11-08 (branch `v2/su-compat` from `v2/phase-0` `7402465`, commit `7355613`)

Owner-approved compatibility layer so the earlier web UI and MCP gateway can run on the package
(one served dataset; the retired dataset answers "not available").

Done:
1. `compat/legacy_qa/` (new): `profile.py` (data-only `LegacyProfile`; datasets, retired ids, mode
   routes, link policy, content-source labels, record-context key map, source view, search facets,
   research files, request log), `app.py` (`create_legacy_app`; `/api/qa`, `/api/qa/stream`,
   `/api/search`, `/source`, `/api/source`, `/api/datasets`, `/api/models`, `/api/clusters`,
   `/api/evolution`, `/api/replay-runs`, `/health`), `envelope.py` (legacy envelope, `[id]`→`[n]`,
   panels, `compat` block), `stream.py` (SSE translation, incremental decoding of the structured
   reply's `answer` string, held-back unfinished markers), `search.py`, `source.py` (visibility
   predicate, redaction), `errors.py`, `__main__.py` (loopback-only CLI).
2. Package gaps closed (opt-in, defaults unchanged): `runtime/listing.py` + `Engine.search_page` +
   `POST /v2/search/page` (pagination/total over candidates, date sort, multi-value `any_of`
   pushed down with the access constraints, facet counts with scope/exactness, text budget);
   `SearchRequest.evidence_chars` + `request.evidence_chars_max` (also MCP args);
   `EvidenceEvent.items`. Engine edits are two small hooks (`_check_request`, `_pack`), the
   `search_page` method after `search`, and one emit line.
3. Tests: `tests/unit/test_legacy_qa_units.py` (12), `tests/contract/test_legacy_qa_compat.py` (18),
   `tests/contract/test_search_page.py` (9), synthetic legacy request fixtures
   `tests/fixtures/legacy_qa/requests.json` (19 bodies). unit+contract: 3,348 passed, 1 skipped,
   0 failed (base 3,309 passed, 1 skipped); parity 23 passed; ruff clean; schema regenerated.
4. Private staging (outside the repo): adapter :8110 on a copy of the parity SU store (bge-m3
   imported vectors; Sonnet 5.5 planner, Haiku 5.5 generator), UI copy :3100, gateway copy :7110.
   Browser smoke 3/3, gateway 81/81 own tests + 7/7 staging checks, 10-question structural
   side-by-side; spend US$0.42 (package ledger) under a US$2.00 cap. See `docs/VALIDATION.md`.

Deviations:
- Listing controls are a separate request model/route (`SearchPageRequest`, `/v2/search/page`)
  instead of new `SearchRequest` fields on `/v2/search` (plan §3.15): keeps `SearchRequest`,
  `filters` typing and the decision code untouched while other slices edit them. Not on MCP yet.
- Multi-value filters ride on the run context's access constraints (`dataclasses.replace(ctx,
  acl=...)`) rather than on `SearchRequest.filters` (scalar); they are pushed down everywhere ACL is.
- `total` and candidate facets count retrieved candidates (labelled `total_relation: candidates`,
  `exactness: bounded`); corpus-wide counts only for `facet_scope: filtered_corpus` (no query text).
- `/api/qa` drains `answer_stream` (to get the packed evidence ids for numbering) instead of
  calling `answer`; results are identical by the stream contract.
- The sqlite backend path is part of the index recipe, so the staging store copy needed an operator
  manifest relocation (private script, copy only). A generic relocation command is a follow-up.

Follow-ups: `search_page` on MCP; per-dataset `/health`; record page for datasets without URLs in
the earlier UI; case-segment/community evidence, graph node export, `enrich`, guidance/facts/notice,
session-cache answers, reranker parity (see VALIDATION known gaps); store relocation command.

## Parity-rerun defect fixes (corpus B) — 2026-11-08 (branch `v2/phase-0`, base `9938551`)

Source: the 2026-11-08 production-workload parity rerun (corpus B = ticket analytics; earlier
service 42/45, package 15/45 on q001-q045). Private evidence stayed in the external workspace; the
repo has synthetic equivalents only. Commit `5c5e19c`. Evidence: synthetic tests + an offline
contract replay of recorded replies (no new model calls).

Done (defect -> fix -> regression test):
1. `answer_text_without_exact_scope` on exact counts with sample lists / disclaimers (51 of 100
   violations in T1). Structural: claim kind `examples` (models `ClaimKind`; offered in the
   generator schema with aggregate evidence; generator prompt asks for it), contract rule 6
   (`examples_claim_invalid`; backs only a framed enumerated block next to an exact count claim).
   Text: `statements.completeness_disclaimed` (denied/advised completeness is no statement), field
   labels / double negatives / "records have no X recorded" / passage references / auxiliaries
   after "how many". Tests: `tests/unit/test_parity_fixes_contract.py` (34).
2. SQL writer non-JSON replies (25/64 `schema_violation`): `models.roles.query_writer` (router and
   SQL writer; `router_role` / `sql_surface.role` accept it; `role_schemas` = `TypedRoute`,
   `SqlDraft`, both fit `output_config.format`, so a registry-default `native` stays native);
   `sql_surface.repair_attempts` also covers a `SqlDraft` schema violation (field paths only); the
   SQL writer prompt example is JSON (the upstream `thought:` layout was what the model copied).
   Tests: `test_sql_surface.py` (+3, 1 updated), `test_parity_fixes_engine.py` (role tests),
   `test_typed_router_engine.py` / `test_sql_route_engine.py` (two-violation drop).
3. `typed_route_discarded` on planner abstention (3 cases, all plan-schema violations): an executed
   routed result on an aggregate question becomes the plan (`policy_applied: typed_route_only`,
   failure + note recorded). Tests: `test_typed_router_engine.py` (kept x2 incl. schema violation,
   still discarded without an aggregate, caller-typed aggregate abstains).
4. `aggregate_intent_without_scope` after a failed route although the planner's own structured step
   was exact: `Engine._cover_aggregate_flags` (after execution and before the contract) drops the
   per-intent flags when an exact aggregate exists (rule-3 condition). Tests:
   `test_parity_fixes_engine.py` (exact drops, bounded keeps).
5. `unsolicited_structured` dropped legitimate count steps (21 in T1): `_AGGREGATE_CUE` adds
   ranking/superlative/ordinal, range, distinct, existence and trend phrasings; the rank intent
   accepts "(has) raised/filed/opened/... the most". Tests: `test_parity_fixes_decision.py`.
6. Rules exact-but-wrong: (a) a ranking compiled to one total is refused (`measure_unsupported`);
   grammar reads "which X currently has / has opened / filed the most" (fixed record-verb list);
   (b) mention counts (`mention`, `about`, `X-mention` ...) are `bounded` with
   `qualifier_not_applied`; quoted phrases / "containing" stay exact. Tests:
   `test_parity_fixes_decision.py`; `test_query_compiler.py` two mention cases now assert
   `bounded` (and an exact quoted variant: same edge cases).
7. Latency: the router chain runs concurrently with the planner (`typed_query.parallel`, default
   true; `Trace.attached` keeps the worker's events under the caller's span so `model_calls` order
   and the merge are the sequential ones); sequential under `parallel: false` or a call budget that
   cannot cover both. Tests: barrier proof, sequential cases, planner-error wait
   (`test_parity_fixes_engine.py`); order assertions in `test_typed_router_engine.py` made
   order-insensitive between the planner and the router chain (chain order and trace order still
   exact).
8. SQL contributor lineage: scalar single-table aggregates carry `contributors` + up to 8
   `document_ids` (`lineage: computed`), read under the same bound ACL/filters; packed lineage
   scope `aggregate_contributors`. Tests: `test_sql_surface.py` (+3), `test_sql_route_engine.py`.

Corpus-B rerun keys: `models.roles.query_writer: {provider: anthropic, model: claude-sonnet-5-5,
api_key_env: ANTHROPIC_API_KEY, max_tokens: 1500, timeout_seconds: 90}` (structured_output unset ->
native); `pipeline.decision.typed_query: {enabled: true, routing: router, router_role:
query_writer, parallel: true}`; `pipeline.arms.typed_query.sql_surface: {role: query_writer,
repair_attempts: 1}`; `budgets.max_llm_calls: 5` unchanged.

Deviations / decisions:
- New role name `query_writer` (the plan has none); the planner role's pre-flight is unchanged
  (a router/SQL writer on `planner` still runs in prompt mode).
- Repair is the existing single `sql_surface.repair_attempts` round, shared by SQL and schema
  repairs (never two rounds).
- "Covered" (defect 4) = any exact, non-refused aggregate without dropped predicates (the contract's
  rule-3 condition), not a per-intent-kind match; untargeted-list flags (QQ-04) are never dropped.
- Mention counts are labelled `bounded` (the package's label for "answers a different question"),
  not `approximate`.
- Concurrency determinism under USD/token limits is not guaranteed (documented; `parallel: false`).
- H04 (M8) was `bounded` through a `qualifier_not_applied` flag; lineage alone does not change
  exactness, so that case is not shown fixed.

Tests: unit+contract 3,488 passed, 1 skipped, 0 failed (baseline 3,396 passed, 0 failed; +81 in the three new files, the rest new cases in existing files). Ruff clean. Schema regenerated. Offline replay of recorded generator replies against the new contract (private workspace): T1 abstained 36 -> 13, T3 17 -> 5 (contract only, not correctness).

Follow-ups: corpus-B rerun with the keys above (correctness, SQL-writer violation rate in native
mode, latency); per-intent-kind coverage for defect 4; mention semantics on the `llm` planner's text
scopes (only the rules path is bounded now).

## Corpus-B loss-analysis fixes F1-F11 — 2026-11-09 (branch `v2/phase-0`, base `bcebd31`)

Source: the private per-question loss analysis of the corpus-B R3 rerun (candidate `bcebd31`;
q001-q045 31/45 vs the earlier service 42/45). Every defect is reproduced synthetically in the repo;
question text, names and values stayed in the external workspace. Commit `174af45`. Evidence:
synthetic tests + a $0 contract replay of recorded replies (no model calls).

Done (fix -> where -> tests):
- F1 `AnswerContract._number_matches` reads numeric cells of SQL/typed rows (`row_values`) and, for
  "N ids" statements only, the retained id count; `Engine` passes rows/columns on the generator
  path; executed SQL evidence carries `rendering` (`generation/rendering.render_sql_rows`);
  `_rendered` never falls back to an aggregate the answer did not cite while it cited an exact one.
  Tests: `test_corpus_b_fixes.py` (F1 x7), `test_corpus_b_engine.py` (3).
- F2 `statements.completeness_disclaimed`: modal hypotheticals after the phrase, gerund start +
  requirement verb, parenthetical advice label. Tests: 5 disclaimers, 3 claims kept.
- F3 guard `_project_order` (unprojected aggregate ORDER BY -> `sort_value`), tie window
  (`TIE_WINDOW` 50, `peek_before` for OFFSET); executor `_ties`, `sql_ties`, `tie` row flag,
  overflow `result_truncated`; prompt rule (select the measure, no arbitrary tie-break). Tests: 8.
- F4 guard `_numerator_position` (`filter: numerator|numerator_negated`); `route.executed_unapplied`
  accepts it for share intents only. Tests: 9 positions + route. One existing guard case updated
  (`sum(CASE ... ELSE 0)` is now `numerator`) and a multi-branch CASE case added (stays `other`).
- F5 intent grammar: `spans/covers the most`, `appears/occurs most often`, `next (two) highest`,
  `second-most`, `+`/`/`/`&` inside gap words, plural share words. Tests: 6 + 3 negatives + 1.
- F6 `projection.tables.<t>.text_columns` (`title` | `fields.<name>`, description required, max 16,
  no clash/shadowing): synthesized non-listable string `FieldSpec`, `TableSpec.text_sources`
  (fingerprint unchanged without them), `ProjectionBuilder.add(records, documents)` (refuses a batch
  without documents), write path passes the batch documents, catalog marks free text, guard refuses
  `=` (`free_text_equality`, repairable), text patterns record their filter position, a positive
  LIKE applies a quoted phrase and, with a mention cue, a value it contains. Tests: 7 unit + 3 engine.
- F7 `sql_surface.list_values_up_to` (default 64) -> all ACL-scoped values, `values_complete`,
  `(all values)` in the prompt; `prompts.value_hints` (catalog values only). Tests: 3.
- F8 `_scope.rank_groups`/`order_groups`: NULL-key groups unranked and never cut into a top-N/nth
  unless a `missing` predicate asks; group-key sort by value with NULL last;
  `ScopeCompiler` refuses top_n/nth/order with a group-key sort. Tests: 4.
- F9 `ScopeCompiler`: a `time_basis` role label maps to its single declaring field. Test: 1.
- F10 `packing.metadata_show_null`. Test: 1. F11 `validate` warning `sql_writer_max_tokens_low`
  (`sql_surface.role` under 2048). Test: 1.

Tests: unit+contract 3,555 passed, 1 skipped, 0 failed (baseline 3,488 passed). Ruff clean. Schema
regenerated. $0 contract replay (aggregates only): q001-q045 passed 27 -> 35, stripped 12 -> 5,
abstained 6 -> 5; q046-q100 passed 23 -> 23, stripped 21 -> 19, abstained 11 -> 13 (two strip ->
abstain moves on ranking questions now detected by F5 whose recorded SQL still has the pre-fix
`bounded` label; one more in q001-q045).

Corpus-B rerun config (private workspace): `projection.tables.ticket.text_columns: {subject:
{source: title, description: ...}, description: {source: fields.description, description: ...}}`
(no data prep needed; rebuild the store); `pipeline.arms.typed_query.sql_surface.list_values_up_to:
64` (default); `models.roles.query_writer.max_tokens: 3000`; `packing.metadata_fields` + `resolution`
and `packing.metadata_show_null: [resolution]`.

Deviations / decisions:
- F3 ties are kept on the aggregate ORDER BY keys only (all keys when none is an aggregate);
  row-level statements get no tie window; with OFFSET only a tie with the preceding row is reported
  (that row is not added).
- F4 numerator positions count only for share/ratio intents (plan); a CASE-filtered measure under a
  plain rank intent stays unapplied.
- F6 LIKE results keep the existing SQL exactness (exact unless a qualifier is unapplied); a value
  qualifier is applied by a LIKE only with a mention cue (as `rules._MENTION` plus "contain"),
  otherwise it stays `qualifier_not_applied`. The rules path's mention counts stay `bounded`.
- F8 refuses (does not re-rank) a top-N sorted by the group key: no repair on the planner's behalf.
- F10 is a generic opt-in key, not a `resolution`-specific rule. F11 is a warning at the ChatRole
  default (2048), not a default change; 3000 recommended in docs.

Follow-ups: corpus-B paid rerun with the keys above; frozen-plan SQL replay on the rebuilt store
(the contract replay cannot show F3-F7); value-hint quality on non-English values; tie reporting in
the generator prompt.

## su-compat parity — legacy UI/API parity gaps — 2026-11-09 (branch `v2/su-compat-parity` from `v2/phase-0` `bcebd31`, commit `4a8ef84`)

Owner-approved (2026-11-09): close the remaining UI/API parity gaps of `compat/legacy_qa` so the
earlier web UI and MCP gateway work fully on the package. Evidence: synthetic contract tests + a
private staging smoke (5 paid calls, US$0.130).

Done (gap -> change -> test):
1. Record links the unmodified UI renders: `source.ui_link` (profile; reversible prefix alias rules,
   UI dataset token, channels, alias/rest patterns); `envelope.source_link/citation_url` take the
   request channel; `GET /source` resolves only rule-produced aliases, then the dataset's id pattern
   and visibility rule. Staging: ticket citations open the UI record page (title + 8 sections).
   Tests: `test_ui_channel_citations_*`, `test_source_resolves_ui_aliases_*`, `test_ui_link_*`,
   unit `test_ui_link_alias_round_trip_and_bounds`.
2. Search extras (`search.py`): `facts` = the search's own aggregate renderings citing numbered
   structured blocks `S1..`; deterministic `guidance` and `notice` (limitation); `enrich` via new
   `runtime/result_notes.py` (one metered generator call; per-result attribution, invented ids
   dropped); `compat.usage` for the search path. Tests: `test_search_guidance_*`,
   `test_search_facts_*`, `test_search_enrich_*`.
3. Session follow-ups: new `Engine.answer_from_evidence` (`runtime/evidence_answer.py`; ids re-read
   in scope at the pinned generation, packed in order, answer contract, provenance in
   `trace.evidence_answer`) + `compat/legacy_qa/session_cache.py` (in-memory anchors with TTL/LRU;
   deterministic gate). Tests: `test_follow_up_*`, `test_answer_from_evidence_*`, unit gate/store.
4. `/health` per dataset (`?dataset=`, `/ds/<id>/health`: 200/503/410 retired; combined map) and a
   `/ds/<id>/...` prefix for every route (`DatasetPrefix` middleware); `/api/clusters` from package
   data (`clusters:` profile key; aggregate + get_document tools; labelled metadata groups);
   evolution/replay runs stay pending (no package equivalent). Tests: `test_health_per_dataset_*`,
   `test_clusters_from_package_metadata_groups`.
5. Agentic envelope from the trace (`agentic_block`: first round = DAG stage, review stop reason and
   verdict = grading, hop rounds = corrective trace). Test: `test_agentic_block_maps_rounds_review_and_hops`.
6. Latency: adapter stage timings (`timings.stages["adapter:*"]`), start-up embedder warm-up
   (`warmup: true`). Measured adapter overhead 1-5 ms; the first-request embedder load (3.65 s, $0
   profile) moved to start-up. Tests: `test_adapter_stage_timings_are_reported`, `test_warmup_*`.

Deviations:
- Session follow-ups use a deterministic cue gate instead of the earlier service's model gate, and
  no standalone-question rewrite (both reported in `compat.unsupported`); unclear turns run a full
  search. The anchor store is in memory (a restart empties it).
- A provider/budget error during a follow-up is returned as the legacy error (fail closed); the
  earlier service fell back to a full search on `cache_error`. A generator-declared insufficiency
  (`cache_declined`) does run the full search, recorded in `session_cache.decision`.
- The follow-up payload is the new question plus the anchor question labelled as context; the
  earlier answer is never sent (package rule: memory never reaches the generator). Observed once in
  staging: the model framed the reply as "the earlier answer in three bullets" without having seen it.
- Retired-dataset health is HTTP 410 (fail closed for status-code monitors); the staging gateway
  therefore shows it as "unavailable (HTTP 410)", not "retired".
- `Engine.answer_from_evidence` describes its id lookup with a one-step `exact_id` plan (a decided
  `Decision` needs a plan); no arm runs (`trace.rounds == []`).
- Staging-only changes (private, outside the repo): profile `citation_url: external_first`,
  `ui_link` (ticket ids aliased into the UI page's case-id scheme), `session_cache`, `search.enrich`, `clusters`; the recorded
  legacy clusters/evolution files are no longer served; the gateway copy uses `/ds/<id>` base URLs;
  `run/adapter.sh` points at this worktree. The live UI/gateway/nginx were not touched.

Tests: `tests/contract/test_legacy_qa_parity.py` (17, new), `tests/unit/test_legacy_qa_units.py`
(+3, 15). Full `-m "not external"`: 3,446 passed, 51 skipped, 3 failed (before: 3,426 passed, 51 skipped, the same 3 failed). The 3 failures (`tests/qa/test_qa_2_0_0a1.py` qa01, qa04, qa13: planner schema name and count-in-answer contract checks) fail identically on the untouched base `bcebd31` and are outside this slice. ruff clean. `docs/config.schema.json` unchanged
(`Config` not changed; the profile is not part of it).

Follow-ups: thinking control for the Anthropic generator role (the dominant latency: ~11 s to first
token), a persistent anchor store, a model gate option for follow-ups, the gateway reading the body
`status` of non-2xx health replies, speculative retrieval in parallel with the planner (engine).

## SU retrieval parity: port of the earlier service's retrieval features — 2026-11-09 (branch `v2/su-retrieval-parity`, base `bcebd31`)

Owner-approved port ("do all the relevant old features"). Kept in new modules plus small hooks so it
merges with the concurrent work on `v2/phase-0` (no changes to generation/contract, sql_surface,
decision/intent or the typed catalog). Evidence: synthetic tests + a private $0 retrieval check
(VALIDATION.md, same date).

Done (all opt-in, defaults unchanged):
1. Rerank stage `selection/rerank.py` (engine `_select` hook): `models.roles.reranker.{device,dtype,
   batch_size,max_length}`, `pipeline.selection.reranking.{top_k_in,top_k_out,timeout_ms,on_error,
   passage_chars}`; `trace.selection.rerank_detail`. Replaces the inline CrossEncoder call (same
   default: scores everything, raises on error; one difference for existing `rerank: true` users:
   the passage is now bounded to `passage_chars`, default 1,900, as the earlier service did).
2. Segment arm: `segments/` (sealed sidecar index, `segments build` CLI) + `arms/segment.py`; arm name
   `segment` registered in config, capabilities (catalog), ARM_ORDER, RETRIEVAL_ARMS, LLM_ARMS (the
   planner schema narrows to enabled arms, so nothing changes without the arm) and arm cards.
3. `communities.strategy: imported` (`communities/imported.py`, write-path hook) and
   `pipeline.arms.community.claim_hits` / `claim_min_terms`.
4. `ingestion.attach` (`ingestion/attach.py`, write-path hook, config validation of target fields);
   attached Kuzu stores excluded from write targets/recipe (`generations.attached_read_only`);
   `pipeline.arms.graph.path_evidence`.
5. Fixes found by the private run: the sentence-transformers provider now holds its lock over
   tokenizer + encode ("Already borrowed" when dense and segment arms encode concurrently); the
   rerank budget no longer queues abandoned CPU calls behind a single worker (one call at a time in a
   daemon thread, later calls time out within their own budget, nothing blocks process exit).

Ported vs skipped (earlier service, retrieval path only):
- Ported: cross-encoder rerank of the fused list (earlier: top 50 -> 10); case-segment arm (dense +
  segment FTS fused by RRF, per-ticket dedupe); community summaries as an arm (grounded claims only);
  KG v2 SQLite attributes (1:1 tables) for the SQL route; KG v2 Kuzu graph through the graph arm.
- Already covered by the package: exact ticket lookup (`exact_id`), phrase/identifier boosts (lexical
  front policies), source priority (`source_order`/`source_slots`), named-ticket history (record-level
  packing of the whole ticket), doc page expansion (record-level packing).
- Skipped, with reasons: `case_topic_aggregate` (counts from reranked retrieval; conflicts with the
  exact-scope contract; would need a `bounded` retrieval-estimate aggregate design); mined-resolution
  fallback (no such table in corpus B); glossary/synonym query expansion (off by default in the
  earlier service); text2cypher and the earlier fixed Cypher intents (declared graph templates are the
  package equivalent; templates for ticket events/causes/fixes not written yet); community v1 MiniLM
  summary ranking (the summaries are ungrounded; claim-term matching is used instead); ticket comments
  as segments (not in the package records, so they cannot be anchored; needs comments ingested as a
  field first).

Deviations / decisions:
- Segments are a sidecar index, not catalog records, so counts, projection and the other arms are
  unaffected; the arm checks the parent against the request scope. Anchors are verified at query time
  against the serving generation (stale ones are dropped and counted).
- The KG SQL tables are exposed as one-to-one metadata columns joined at ingest (then projected like
  any field), not as extra SQL tables: the projection/SQL guard assume record-keyed tables with
  declared fields (other agent's area). Many-to-many KG facts are served by the attached Kuzu graph.
- `on_error: fusion_order` is an operator-chosen, always-traced degradation (`failed_open`), never
  the default.
- Community import keeps the external label (a name, not a claim); free summaries are refused.
- The embedder device is part of the index fingerprint, so the private segment index was built on
  GPU through the Python API with the same recipe while serving stays `device: cpu`.

Tests: unit+contract 3,522 passed, 1 skipped, 0 failed (baseline on `bcebd31`: 3,488 passed, 1 skipped, 0 failed).
New: test_rerank_stage.py 14, test_retrieval_parity_features.py 16, test_graph_attach_engine.py 3,
test_embedding_concurrency.py 1. Ruff clean. Schema regenerated.

Private deployment (workspace `su-retrieval-parity/`, not in the repo): `cfg/su_parity_rules.yaml`
(rules, $0 check) and `cfg/su_parity_answer.yaml` (llm planner + SQL route with the KG columns;
`doctor` valid, index ready) on copies of the 2026-11-04 store; results in VALIDATION.md.

Follow-ups: answer-quality run with the llm variant (owner budget); graph templates for the earlier
ticket-event/cause/fix intents; comments as an ingested field so they can be anchored segments;
a bounded retrieval-estimate aggregate if topic counts are still wanted; CPU rerank latency on an
unloaded host; rebuild segment indexes automatically after an ingest (today: manual `segments build`).

## su-contract-parity — B4 contract fixes, `on_violation: annotate`, Anthropic thinking control — 2026-11-09 (branch `v2/phase-0`, base `96a1b0e`, commit `369083e`)

Owner task: let the SU deployment behave like the earlier SU service and close the remaining
corpus-B (B4) losses. Evidence: synthetic/mocked tests, $0 provider capability metadata, $0 private
contract replay of the recorded B3/B4 generator outputs. No paid calls.

Done:
1. Contract defects (each with a synthetic reproduction in `tests/unit/test_contract_b4_fixes.py`):
   - q028 (exact count refused, regressed vs B3): the generator typed no `count` claim. A count
     statement whose number is a value of a cited exact aggregate is now backed
     (`AnswerContract._implicitly_backed`); inline `[id]` markers of supplied evidence are citations
     (`with_inline_citations`), an id-shaped unknown marker is `unknown_evidence`.
   - q011/q034/q042/q043/q045 (ranking/ratio refused or stripped): `result_statements` drops result
     sizes ("1 row"), retained-id counts, the question's thresholds / requested N / quoted phrases,
     counts of the cited aggregates and advice row parameters before the check; the denominator
     population frame takes "Denominator in the snapshot: N"; an OFFSET result's rendering states
     its rank position (q011's generator read the one-row OFFSET result as the leader).
   - q001 (complete status breakdown bounded then stripped): "<target> breakdown|distribution|split"
     is a breakdown intent, so `NO_OPERATOR` no longer bounds it.
   - q037/q044 (conflicting counts): guard `filter: any_of` for an OR group of text matches that is
     a WHERE conjunct; the route applies the quoted phrase it contains; `Engine._reconcile_counts`
     demotes the all-chunk text-scope count (`conflicting_count`) for mention questions (both without
     a mention cue). Structured text-scope aggregates carry `text_scope: true`.
   - q041 (multi-term mention + "active"): quoted phrases lose trailing punctuation; a multi-needle
     OR group applies only for an "or" question whose every needle is a question word; a distinctive
     word of one observed value ("Ledgerly" for `market_place__ledgerly` in the tests) is a value
     qualifier (`Qualifier.token`), so the recorded B4 SQL, which omitted the module filter, stays
     bounded (verified by the relabel replay) instead of becoming a wrong exact 4.
2. `pipeline.generation.contract.on_violation: annotate` (`AnswerContract._annotate`; `contract:
   annotated`, `AnswerResult.limitations`, `trace.contract.limitations`, legacy envelope
   `compat.limitations`, `Violation.action: annotate`). Default stays `strip`.
3. Role keys `thinking`, `thinking_budget_tokens`, `effort`, `measure_first_token`; registry fields
   `thinking_types`, `thinking_off`, `effort_levels`, `off_max_effort` (`capabilities.thinking_wire`,
   `thinking_problems`); call records/`model_call` events carry `thinking`, `thinking_requested`,
   `effort`, `stream`, `first_token_seconds`; answers carry `trace.generator_latency`.
4. Haiku 4.5 generator: the existing `claude-haiku-4*` entry (native forced tool use) covered by a
   contract test with a $1/$5 per MTok operator price table; the private price table already had the
   entry (no bundled prices in the repository, by design).

Replay ($0; q001-q045; passed/stripped/abstained/annotated): B4_P1 recorded in the run 29/10/6/-;
strip with recorded labels 41/1/3/-; strip with this slice's engine relabelling 41/1/3/-; annotate
with relabelling 41/-/-/4. B3_P1: recorded 27/11/7/-; strip relabel 39/5/1/-; annotate relabel
39/-/-/6. Scripts and per-item outputs: private workspace `contract-annotate/replay/`.

Deviations / decisions:
- Owner's "two different counts for the same claim never both exact" is implemented for text
  counts only (text-column SQL vs all-chunk text scope): deciding that two non-text counts answer the
  same claim needs a scope comparison the engine does not have; such pairs are unchanged.
- In annotate mode an unbacked statement whose paragraph cites no supplied evidence is removed
  (noted), not annotated: "no uncited claims about records". Number corrections cite the rendering of
  every exact aggregate the answer cites (at most 3).
- `thinking: off` on Haiku 4.5 sends nothing (the owner's rule "no thinking param unless budget
  given"); `enabled` with native forced tool use is rejected at validation (the API refuses forced
  tool use with thinking). The `between_tools` wire form follows the capability name the endpoint
  reports and is unverified against a real call.
- First-token latency is measurable only for streamed calls: `measure_first_token: true` streams the
  generator internally (needs `capabilities.streaming: true`); a non-streamed call reports `null`.
- Test edits: `tests/unit/test_answer_text_contract.py::test_absence_needs_an_exhaustive_empty_scope`
  reused text citing an unsupplied `[aggregate:1]`; it now cites the supplied id and asserts the stale
  marker is `unknown_evidence` (same edge case, plus the new one). `tests/serving_support.VOLATILE`
  adds `stream`, `first_token_seconds`, `generator_latency` (a streamed and a plain answer still compare equal otherwise).
- q043's numerator inside a derived table stays `other` (F4 limitation, unchanged).

Tests: unit+contract+qa 3,708 passed, 2 skipped, 0 failed (baseline 3,642 passed, 2 skipped). New:
test_contract_b4_fixes.py 25, test_contract_annotate.py 10, test_sql_text_any_of.py 5,
test_contract_annotate_engine.py 3, test_anthropic_thinking.py 23. Ruff clean. Schema regenerated.

SU run with annotate + Haiku 4.5 generator + thinking off (keys):
`pipeline.generation.contract.on_violation: annotate`;
`models.roles.generator: {provider: anthropic, model: claude-haiku-4-5, api_key_env: ANTHROPIC_API_KEY,
max_tokens: 2600, thinking: off, capabilities: {streaming: true}, measure_first_token: true,
evidence_budget: {chars: 40000}}` (native tool use by default; no `effort`: Haiku 4.5 has none);
`budgets.price_table` with `claude-haiku-4-5: {input_per_million: 1.0, output_per_million: 5.0}`.

Follow-ups: one paid smoke of `thinking` wire forms (Sonnet `between_tools`, Haiku 5.5 `disabled`
+ effort) and a measured first-token latency; a paid B4 rerun under annotate; q043 numerators in
derived tables; judge the replayed outcomes.

## b5-fixes — corpus-B B5 package-side losses: value grounding, superseded counts, bounded zeros, per-group shares — 2026-11-09 (branch `v2/phase-0`, base `50f47e7`, commit `8aa04cd`)

Owner task: fix the four package-side corpus-B losses of run B5 (q020, q023, q041, q043). Evidence:
synthetic unit/contract tests; $0 private replay of the recorded B5 SQL statements and planner
scopes on a byte copy of the B5 store, and of the recorded B5 answers through the contract. No paid
call, no judge call, no re-generation.

Done:
1. q020-type (value on the wrong field / spelling): `decision/value_match.py` rule table (exact,
   casefold, normalized separators, plural fold, distinctive word of one multi-part value); used by
   `ValueResolver` for agent values (enum and observed universes) and by the SQL guard (literal
   rewritten before binding; `checked_values[].as_written/rule`; `ambiguous_value` refusal). The
   cross-field move (`ValueResolver.other_field`) needs the user's question (`resolver.question`, set
   by the llm agent; `ArmInput.question` / `SqlSurfaceSession.grounding` from `route.run(...,
   grounding=request.query)`), the value as a word sequence of it, the original field not named, and
   exactly one other field grounding it; recorded (`from_field`, `column_as_written`, trace
   `value_canonicalized`). Both routes use the same rules, so a value the SQL route refuses is tried
   with the same canonicalization by the planner's structured scope (independently compiled).
2. q023-type: `Engine._supersede_counts` (run by `_reconcile_counts`): count-only question, one
   exact scalar count value -> other scalar counts `superseded_by` it, excluded from packing, the
   rendered path and the contract (kept in `retrieval.aggregates`). `AggregateEvidence.scalar_count`;
   `AnswerContract._count_target` turns an unbacked count statement/claim contradicting it into
   `answer_number_mismatch` (strip: rendering fallback; annotate: removed + correction note citing
   the exact record).
3. q041-type: the moved value + status IN + OR group of LIKEs compiles exact (synthetic and replay);
   `_bound_aggregate` relabels stale `(exact` renderings (`relabel_bounded`, bounded zero "not an
   exact zero for the question"); structured bounded zero rendering says so too; annotate removes a
   zero count without an exact scope (`NOTE_ZERO`).
4. q043-type: guard `_effective_roots` (derived table / CTE as the only source counts as the outer
   statement: positions and numerators); `ScopeSpec.min_count` / `ScopeDraft.min_count`; a share
   numerator with `group_by` is the share within each group (`GroupRow.numerator`, rendering "0.3 (12
   of 40)"), ranked by `order`/`top_n`/`nth` with ties; `sort` named `ratio`/`percentage`/... ranks a
   share; the Lucene native path reports a per-group numerator `not_supported_by_backend`.

Replay ($0): q020 SQL refused -> exact 71 (expected 71), planner scope not executable -> exact 71;
q023 bounded 24 superseded by exact 35, recorded text corrected (annotate note / strip rendering);
q041 SQL refused -> exact 3 (expected 3), the structured bounded 0 superseded and relabelled; q043
recorded B5 SQL (OFFSET 2 reading of "next two") and planner scope (group-key sort) unchanged, the
B4 derived-table SQL now exact with the expected organizations, a corrected structured scope correct
with the tie reported. Contract replay of the 45 recorded B5 answers (partial reconstruction for
annotated ones: B5's dump has no tool-use text): strip 42/2/1/-, annotate 42/-/-/3 (passed /
stripped / abstained / annotated). Scripts and outputs: private workspace `b5-fixes/replay/`.

Deviations / decisions:
- The normalization is applied to agent values on every agent (rules, llm, SQL guard, IR
  compiler), not only on refusals: a value equal to a stored value under the rule table is that
  value. Request filters stay literal. Three tests asserted the old case-sensitive refusal and were
  replaced by equivalents: `test_scope_values.py::test_enum_value_must_be_declared` and
  `::test_string_under_cap_matches_observed_values_with_casefold` (the variant is now grounded and
  recorded; an unknown value is still refused), `test_sql_surface_guard.py::test_a_case_variant_is_rewritten_to_the_stored_value_and_recorded`
  (was `..._near_miss_..._is_not_replaced`; the refused row uses a value no rule grounds) and
  `tests/contract/test_sql_surface.py::test_unknown_values_are_never_repaired_or_replaced` (same
  invariant on `'billing ops'`).
- Derived-table positions: `test_corpus_b_fixes.py` F4 row and `test_sql_surface_guard.py` filter
  position row now expect `numerator`/`positive` for a sole-source derived table; a join row added to
  each keeps the `other` edge case.
- `test_typed_planner.py` BASELINE (planner request bytes) re-recorded for one intended change: the
  new optional `ScopeDraft.min_count` field in the wire schema. The built-in planner prompt is not
  changed (a prompt line was drafted and reverted to keep that diff minimal).
- The cross-field move is refused by the typed IR compiler (the arm recompiles IR drafts without the
  question, so a move would validate at decision and fail at execution).
- "Same claim" for superseding is restricted to count-only questions and scalar counts (a SQL result
  counts when its outer statement selects one `COUNT(...)`); two differing exact counts supersede and
  correct nothing.
- q043's "next two" (ranks 2-3 vs 3-4) is a router/SQL-writer reading, not fixed in the package.

Tests: unit+contract+qa 3,762 passed, 2 skipped, 0 failed (baseline 3,708 passed, 2 skipped). New:
test_b5_fixes.py 37, test_b5_sql.py 12, test_b5_engine.py 3 (+2 parametrized rows). Ruff clean.
Schema regenerated (no change: `Config` untouched).

B6 run config: no new key is needed; the fixes are default behaviour. Optional, private, protocol
change: the SU planner prompt file could state "a ratio among groups with at least N records:
group_by + measure share + numerator + order desc + min_count N; never sort by the group key", and the
router/SQL-writer prompts could state that "next N" after an unnamed anchor starts at rank 2.

Follow-ups: a paid B6 rerun to measure the four questions; conformance rows for `min_count` and
per-group numerators in `testing/backend_conformance.py`; a native Lucene per-group numerator.

## b7-fixes — corpus-B B7 answer-quality gaps and QA-07/QA-08 — 2026-11-09 (branch `v2/phase-0` worktree, base `1348a6d`, commits `4f92546`, `601b0e0`, `03419fd`)

Done:
1. QA-07 (`generation/statements.py`): a singular possessive unit ("one ticket's", "a record's") is
   no count/absence statement; plural possessives and plain counts still are.
2. QA-08 (`generation/contract.py::strip_sentences`, used by strip and annotate): line-wise removal
   keeping list markers, bold `**Label:**`, headings, blockquotes and table cells; an emptied item,
   or one whose label carries the statement, is dropped whole; `\n{3,}` collapsed.
3. SQL ratio parts (`sql_surface/guard.py::_ratio_parts`, executor `sql_ratios`, rendering "N of D
   (rate)"); writer prompt asks for the parts; projection rule carved out.
4. Small-group lineage (`guard._group_keys` / `_group_lineage_statement`, executor
   `_group_contributors`): `contributor_ids` per returned group with <= `sql_surface.group_lineage_max`
   (new, default 10) records; `lineage: groups`; filtered scalar ids up to max(8, threshold).
5. Exact-first (`Engine._primary_exact`): exact aggregates packed first with `precedence`; the
   rendered path leads with exact renderings; generator prompt: prefer exact over bounded.
6. Generator completeness rule refined: only records present in the evidence, "N shown of M".
7. SQL writer literal-value rule + `(literal match)` VALUE HINTS.

Deviations / decisions:
- Ratio validation is structural (the parts are the division's own operands, or the same selected
  expression), not an arithmetic re-check of returned rows (rounding and outer scale factors make a
  numeric check produce false alarms). `DISTINCT` statements are left unchanged.
- Group lineage only for one catalog table with every group key selected (no join/subquery/CTE);
  NULL keys match with `IS`. More rows than `fetched * threshold` or a failed read attaches nothing
  (`group_lineage.status: error`), never a partial list.
- The exact-first ordering does not drop or relabel anything (no "same question" detection is
  attempted); the bounded aggregate keeps its evidence with a `secondary` note.
- `contract._ROW_KEYS` accepts `contributor_ids` so row cells still back numbers; landed in the
  contract-strip commit (same file).
- Tests whose expected renderings changed (small grouped results now list records):
  `test_corpus_b_fixes.py` F1 (old string kept under `group_lineage_max: 0`) and F3,
  `test_corpus_b_engine.py` (2), `test_contract_annotate_engine.py` (1); the grouped row of
  `test_sql_surface.py::test_no_contributor_lineage_is_claimed_for_other_shapes` moved to
  `test_a_grouped_count_lists_small_groups_not_a_scalar_lineage` (old behaviour under threshold 0).

Tests: unit+contract+qa 3,763 passed, 11 skipped, 0 failed (base `1348a6d` in the same venv: 3,728
passed, 12 skipped). New: `tests/unit/test_b7_answer_quality.py` 30, `tests/qa/test_qa_20261109.py` 4,
`test_sql_surface.py` +1. Ruff clean. Schema regenerated (`group_lineage_max`). $0 private replay of
recorded B7 SQL: ratio parts on q043/q045, 4 contributor ids on q040's group, q038 unchanged.

Follow-ups: a paid rerun to measure the prompt rules (literal values, ratio parts, exact-first,
"N shown of M"); `compat/legacy_qa/envelope.py::sql_block` turns SQL row dicts into key lists
(`list(r)`), pre-existing and now also showing `contributor_ids` — the compat owner should map
`row["values"]` (not edited here: concurrent work on compat); q040's "of N" total needs the planner or
SQL to compute it (lineage does not add totals).

## cutover-fixes — legacy QA adapter pre-cutover hardening — 2026-11-09 (branch `v2/phase-0` worktree, base `1348a6d`, commits `2e80adb`, `eff1dfb`, `3744b5e`)

Owner task: fix the pre-cutover findings of the 2026-11-09 architecture QA (B1, M1-M7, M9, M10) and
package QA (QA-01..QA-14 except QA-07/08) in the legacy QA adapter and serving layer. Evidence:
synthetic unit/contract tests with loopback fakes; no live service, no paid call.

Done:
1. Sessions (B1/QA-01): expired deterministic session swept and re-created (`restarted`), periodic sweep.
2. Stream (QA-02): surrogate pairs; lone surrogates replaced; a stream always ends with `final`/`error`.
3. Errors (M3/QA-03): category-based mapping on every route, logged 500 with request id, 502 for
   schema violations everywhere, content-free reason in the stream failure log line.
4. Disconnect + deadline (M1/QA-04): `/api/qa` cancels on disconnect (499 + usd logged) and at
   `answer_deadline_seconds` (default 170, 504).
5. Starvation + admission (M2/QA-05): cheap routes async / own limiter; `max_concurrent_answers` (16, 503).
6. `record_context` (M4): UTF-8 bytes, default 131072, `RequestRejected` naming the limit.
7. Readiness (M5): `/ready`; `ready` in `/health`.
8. Planner failure (M6): warning, `compat.planner_failure`, `insufficient_evidence`, log fields.
9. Redaction/visibility (M7/QA-06): `outgoing.py` on every response; hidden evidence withholds the answer.
10. Spend reservations (M10): `spend_open`/`spend_audit`, bounded release.
11. Minors: QA-09, QA-10, QA-11, QA-12, QA-14 (MCP `extra=forbid`), M9 (`cases`, `rerank_ms`);
    uvicorn `timeout_graceful_shutdown=20`; coordinator add-on: SQL panel rows given as mappings
    are values in column order, `contributor_ids` rendered as a comma-joined column.
12. `index adopt --from-config` (QA-13).

Deviations:
- QA-13: a plain copy is still refused until `index adopt` runs (explicit, audited, as the task
  specified); the QA test was rewritten to run adopt (`tests/contract/test_index_adopt.py`).
- Admission refusal is 503 (task) rather than the review's suggested 429.
- `source.hide` now applies everywhere; the shared test fixtures no longer hide `kind: report`
  (it would withhold most fixture answers). The two tests that exercise the rule
  (`test_source_view_sections_visibility_and_redaction`, `test_source_resolves_ui_aliases_only_through_their_token`)
  set it explicitly, and `test_hidden_records_are_removed_everywhere...` covers the new surfaces.
- A withheld answer streams no tokens and its `final` reports `answer_rewritten: true`.
- Stale-hold release returns the USD only; the call count stays. Holds from before this change have
  no `spend_open` row and are not released automatically.
- Readiness treats a warm-up that is still running as not ready.

Tests: `-m 'not external'` 3,634 passed, 57 skipped (baseline 3,578 passed, 57 skipped). New:
test_legacy_qa_hardening.py 25, test_legacy_qa_hardening_units.py 23, test_index_adopt.py 4,
test_mcp_unknown_arguments.py 4. Ruff clean. Schema regenerated (`serving.answers.stale_reservation_seconds`,
`request.context_max_bytes` default). External QA harness: 17/20 (QA-07/08 belong to the concurrent
contract change; the relocation test needs the explicit adopt step).

Follow-ups (not code in this slice): B2/B3 nginx routes, B4 production profile, M8 UI drop-in,
M11 owner decision; optional inbound Host guard / bearer token for the adapter; community hits in
the legacy `community` panel; m1 log rotation.

## F0 — unified merge synthetic fixtures — 2026-11-09 (branch `v2/phase-0` worktree, base `90271d4`, commit `6153deb`)

Task: increment F0 of `docs/plans/unified_merge.md` rev 3 (plus two coordinator re-review notes: real
log id behaviour; gap-fill merge with tombstones). Evidence: synthetic only; no service, no model call.

Done:
1. `src/su_multi_arm/testing/fixtures/unified/collection.py`: pure builder of the committed tree
   `tests/fixtures/unified/` (`build_files`/`write_files`/`load`): members support (adopted:
   tickets `ticket:{n}`, docs keyed by URL), tracker (`snapshot:2026-11-08`), logs (`month:2026-10`,
   `day:2026-11-04` epoch `kw-e1`; `day:2026-11-05/06` epoch `kw-e2`, which splits a rule), analytics
   (`day:2026-11-07/08`); provisional days 2026-11-07..09 as slices + `logs/ledger.yaml`; shared
   metadata schema (`unknown_fields: reject`); manifest expectations (tiers, collisions, chains A–D,
   gap merge, routing, question clock with a half-hour-offset zone).
2. Real log ids: log documents are built with `ingestion.rollups._document`; signature ids repeat in
   every log member/slice; incident/signature_day ids are day-free; chain B is one id in archive day
   2026-11-06 and the 2026-11-07 slices (`expected.collisions.archive_and_live`); chain C supersedes
   across days (the later version keeps `date: 2026-11-08`); D supersedes within a day.
3. Gap-fill merge: 2026-11-07 `h13` slice holds two groups 360 s apart (two incident ids, two
   episode-row ids); late lines close the gap (each step <= 300 s); the `day` rebuild slice replaces
   `base`+`h13`, keeps E1's id and `retires` E2's incident and signature_day ids
   (`expected.gap_merge.before/after/tombstones`).
4. Identity registry (every status: matched via exact/casefold/curated+review, candidate incl. the
   environment-suffix trap, contested with a refuting review, ambiguous casefold/token-set
   collisions, excluded, unmatched, missing) with the expected per-namespace report.
5. Links: 12 candidates, tiers A/B/C, all 7 statuses, 6 pair types; planted A/B links;
   5 negatives (outside window, contested/candidate/ambiguous alias, dangling click).
6. Traps (duplicate id across members, slice for an archived day, line-text embed, foreign embedding
   space, v2 config with `collection`, unroutable ids) and `onboarding/analytics.yaml` (D1–D11).
7. `live_shape.py`: committed template + side files with the block structure of the live support
   config (bm25+faiss sqlite, kuzu attach mapping, two `ingestion.attach` tables, imported
   communities, precomputed BGE-M3-shaped vectors, projection, typed_query sql_surface + router,
   segment arm, graph/community arms, llm planner with arm cards, `agentic` review pipeline,
   120k evidence ceiling, USD budget 0.25/7 calls, answer spend ledger); `materialize()` builds the
   sqlite/npy side files and fills digests/paths. Values invented; synthetic price table.
8. `log_day.py`: deterministic large-day generator (`python -m su_multi_arm.testing.fixtures.unified.log_day --out …`),
   288 five-minute refresh files of upserts + `manifest.json`; refuses the repository and non-empty
   targets.

Deviations / findings:
- Fixture code lives in the package (`testing/fixtures/unified/`, as the existing log generator) so
  M1a/M2a scripts can import it; data stays in `tests/fixtures/unified/`. The plan's
  `tests/fixtures/onboarding/` example is at `tests/fixtures/unified/onboarding/analytics.yaml`.
- The plan's invariant "document ids unique across all members and slices of a pinned view" (§6.1)
  conflicts with the real log ids: the rule catalog repeats in every day member, and an episode
  crossing from a closed day into the live window keeps its id in both tiers. The fixture exposes
  both (`expected.collisions`) without choosing a rule; M1a must decide (e.g. catalog kinds as a
  per-source layer; archive-vs-live precedence for chain B). The monolithic smoke ingest uses
  latest-wins only as a smoke, not as a ruling.
- Episode-row (`episode:`) ids in `expected.gap_merge` are computed for the fixture scope
  `{index, tenant, env, service}`; the real snapshot scope may hold more keys.
- `load_config` does not rebase `ingestion.attach[].path`, `communities.imported.path` or
  `pipeline.arms.segment.path` onto the config directory (they resolve against the working
  directory); the live-shape materializer writes absolute paths there. Not changed (core config,
  fingerprint-relevant); worth a follow-up.
- The live-shape planner role triggers the existing "structured_output resolved to none (prompt
  mode)" warning, as the live shape does; the test asserts it.

Tests: full `-m 'not external'` 3,673 passed, 58 skipped (before: 3,642 passed — the 31 new tests are
the only change; no existing test edited). New: `tests/unit/test_unified_fixtures.py` 23,
`tests/unit/test_unified_log_day.py` 8. Ruff clean. No Config change (schema not regenerated).
600k day generated in `~/su_mutliarm_tests/claude/f0-20261109/logday-600k`: 600,002 chunks, 185,675
documents, 712,819 versions, max 3,613 upserts/refresh, 45,195 unique of 133,460 embeddable texts,
54 MB, 20.7 s; rerun byte-identical.

Follow-ups: M1a decides the id-collision rule; the path-rebase gap above; M2a consumes the generator
(slices, compaction, text-hash vector store) and measures §6.7 on it.

## M0 — unified merge log-side fixes — 2026-11-09 (worktree branch from `90271d4`, commit `bfd01a5`)

Plan: docs/plans/unified_merge.md rev 3, M0 (§6.11, §6.12, §6.13, §9, §10.1). Code only; no
deployment, no log-host change, no live service, no paid call. Evidence: synthetic/contract-mocked.

Done:
1. `budgets.plan_check: off | warn | refuse` (default `warn`), `runtime/plan_cost.py`: §6.13 slots
   incl. planner `schema_repair_attempts` + `decision.typed_query.repair_attempts` (re-plan), typed
   router + typed IR / SQL writer (`1 + repair_attempts`), `review.max_hops`, generator + one
   `on_incomplete` retry, the `community` answer role, transient-retry slots reported beside W.
   Runs in `Engine.__init__` (so `serve`, CLI and Python API engines); `GET /ready` carries a
   `plan_check` note (readiness unchanged). The legacy QA adapter's `/ready` is not changed (its
   engines still log the startup warning).
2. `models.roles.<role>.on_incomplete: fail | retry_shorter | retry_larger`
   (+ `incomplete_retry_max_tokens`) and `retry_transient: {attempts 1-2, backoff_seconds}`,
   `providers/_chat.py`. Each attempt is a separate reserved/traced call (`attempt`,
   `retry_reason`); `incomplete_cause: max_tokens | stop | stream_end` (+ `stream_stop_reason`)
   on every incomplete call record, trace event and failure log line.
3. `question.timezone` (question clock, default UTC) and `question.vocabulary` (inline terms or a
   sha256-pinned YAML file; mismatch refuses to load/start), `decision/log_question.py`, applied in
   `DecisionAgent.bind_log_question` on every log-intent path (rules/cascade `log_step`, outage
   path, issue_history, llm `_log_step`): default view (`logs.collection.serving.default_view_days`)
   for undated outage/presence questions; bare hours read in the question clock with both 12-hour
   readings (one `log_around` step per reading; day-level intents cover the readings' days and are
   `bounded` with the statement); service terms grounded to indexed values only (else refused);
   symptom terms stated and skipped by literal-term filters. Statements reach the rendering via
   `StructuredStep.notes`/`unapplied` (`engine._run_log_step`).
4. Over-limit incident selection (`runtime/log_investigation.py`) answered bounded with a note and a
   per-client incident table instead of the 503; an unsupported selection still refuses.

Deviations from the plan / task:
- Config placement (coordinator re-review): all new fields live in `core/config_m0.py`;
  `core/config.py` changes by one import, `ChatRole(RoleResilience)`, `BudgetsConfig.plan_check`,
  `Config.question`, and vocabulary path resolution. The plan's `decision.vocabulary` is
  `question.vocabulary` (the root has no `decision` key; `pipeline.decision` is per pipeline), and
  the question clock is `question.timezone`, not `serving.ui.display_timezone` (display-only).
- `on_incomplete` adds `retry_larger` beside the plan's `retry_shorter` (task: "larger max_tokens
  or continuation"); continuation (assistant prefill) is not implemented: it does not compose with
  forced tool use / `output_config.format`.
- §6.13 says `min(evidence_chars(r), request.evidence_chars_max)`; a request may raise its evidence
  budget up to `evidence_chars_max`, so the larger value is used (safer). Prompt sizes are measured
  from the built-in/configured system prompts + reply schema; arm cards, typed/SQL catalogs and the
  question are listed as `unmeasured` (W is a lower bound for deployments that use them).
- A stream cut before its final event without a `max_tokens` stop is treated as transient
  (`retry_transient`), not as `on_incomplete`.
- `runtime/log_shared_errors.py` keeps its over-limit refusal (only vocabulary stripping changed).
- No edits to `decision/values.py` or `decision/log_intents.py`.

Tests: unit + contract + qa 3,560 passed, 14 skipped, 0 failed (baseline `90271d4`, same venv:
3,504 passed, 15 skipped). New: `tests/unit/test_plan_cost.py` 15, `tests/unit/test_log_question.py`
22, `tests/contract/test_provider_resilience.py` 13, `tests/contract/test_m0_log_answers.py` 5.
No existing test changed. Ruff clean. `docs/config.schema.json` regenerated (also v1 schema
rewritten unchanged by `write_all`).

Follow-ups: deployment of M0 on the log host only in an owner-approved window (twin dataset first,
rollback venv kept); read `incomplete_cause` of real 502s from the serve log before choosing
`retry_shorter` vs `retry_larger`; measure the log planner's real `input_tokens` and compare with W;
a reviewed vocabulary file for the log deployment (private); the legacy QA `/ready` could expose
the plan note; `log_shared_errors` bounded selection if it is seen in the serve log.

## S0 — unified merge skeleton (lane L0) — 2026-11-09 (worktree branch from `ec38b4a`, commits `22fa7b8`, `28aefd6`)

Plan: docs/plans/unified_merge.md rev 3.3, S0 (§7, §10.7.2–10.7.4), plus R2 (`recipe_of`) and the
F0 path follow-up. Evidence: synthetic only; no service, no model call.

Done:
1. Schema v3: `core/config_collection.py` (`collection`: `mode: partitioned`, `members.<id>`
   {kind, sources, record_kinds, cadence, key, producer, id_namespaces, identity_namespace,
   time_roles, canonical_units, catalog_kinds, field_map, visibility, embed_fields, adopt, required},
   `embedding_space`, `lexical.<member>.stats`, `selection.source_quotas`, `layers[]`, `slices`,
   `id_directory`; `communities.layers[]` model), `core/config_crosssource.py` (`identity`,
   `links`), `core/config_selection.py` (`check_source_quotas`). `core/config.py`: `schema_version:
   2 | 3`, loader accepts 3, v2 + `collection`/`crosssource`/`communities.layers` refused (before the
   block is validated) with a migration message, v3 cross-key rules, v3 path resolution,
   `communities.recipe()` adds `layers` only when set, `Config.fingerprint()` omits unset v3 blocks,
   `legacy_fingerprint` refuses v3. Schema regenerated.
2. Path fix (F0 follow-up): `ingestion.attach[].path`, `communities.imported.path`,
   `pipeline(s).arms.segment.path` resolve against the YAML file; `rebase_paths` covers them.
   `ImportedCommunities.recipe()` hashes the path as written (private `_declared_path`), so no index
   fingerprint changes.
3. `core/types_members.py` (frozen): `PartitionRecipe` (+`fingerprint()`), `EmbeddingSpaceRef`,
   `AnalyzerRecipe`, `FacetLocation` (registry-relative only), `MemberRef` (tiers adopted / snapshot /
   archive / partial / catalog; key forms `adopted:<hex>`, `snapshot:`, `day:`, `month:`, `catalog`;
   `unit_id`, `day()`, `covers()`), `SliceRef` (levels five_minute/hourly/base/day; `replaces`,
   `supersedes`, `tombstones`, `catalog_provisional`, `excluded_stats`, `dense_pending`),
   `IdDirectoryEntry` (`superseded_by` xor `deleted_by`, `catalog_provisional` only in slices, `live`),
   `LayerRef`, `PinnedView` (generation, members, `slices: {member: {day: (ids)}}`, layers,
   `history_seq`, `registry_version`, `link_version`; one tier per member-day incl. month members),
   `StratumRanking`; protocols `IdDirectory`, `SliceLedger`, `StratumScorer`;
   `DEFAULT_CATALOG_KINDS = ("signature",)`, `is_catalog_kind`, `canonical_sha256`.
4. `runtime/collection.py`: `CollectionView.for_config` → `SingleMemberView` (`member_ids`, `pin`,
   `describe`); `engine.py`: one attribute `self.view` (+ import), constructed first, unused by every
   path.
5. `ingestion/recipes.py`: `recipe_of(config)`, `fingerprint_of`, `write_slots`,
   `extractor_fingerprint` — no write path, backend or provider.
6. `cli_collection.py` (`collection describe|compose|register|status|adopt`) and `cli_identity.py`
   (`identity describe|candidates|review|report`), wired in `cli.py`; config-only; pending operations
   raise `NotImplementedInRelease` (exit 1).

Deviations:
- `recipes.recipe_of` (plan: M1a/LA) landed in S0 at the coordinator's request; LA owns
  `ingestion/recipes.py` from M1a.
- Inert = **refused by the engine**: `CollectionView.for_config` raises `ConfigError` for
  `collection`, `crosssource` or `communities.layers` (before any backend opens), so a partitioned
  config is never served as one monolithic store. `config validate` and the describe stubs accept
  it. Each lane relaxes the refusal for what it serves.
- Members gained `sources` (the `source` values a member carries; needed for quotas, layers and the
  support stratum = ticket + doc, R1) and `required` (§6.12 `/ready`); `MemberRef` gained the
  `catalog` tier and `catalog_kinds`; `PinnedView.slices` is keyed by member (a day is archived for
  one member while another source serves slices for it, e.g. F0 analytics vs logs).
- The `embed_fields` retention validator (plan: LB, M2a) is in S0 (F0 trap `line_text_embedded`).
- `visibility` accepts only `operator` (operator-only phase); `crosssource.links.review.usd_ceiling`
  `le 10.0` and `approval_ref` required (NB15); `slices.gc_grace_seconds >=
  budgets.deadline_seconds`; `slices` only with a `cadence: minutes` member.
- §7's `serving.question_timezone` / `decision.vocabulary` are M0's `question.timezone` /
  `question.vocabulary` (unchanged).
- Existing tests changed: `test_config_v2.py` (refused example is now `schema_version: 4`; schema
  `schema_version` `enum [2, 3]`), `test_config_cli_a2.py` (the inert v3 keys are allowlisted as
  not yet read; the reading increment removes them).

Tests: unit + contract + qa 3,694 passed, 14 skipped (before, same venv: 3,591 passed, 14 skipped);
conformance + parity + root 125 passed (before 125). New: `test_config_v3_schema.py` 47,
`test_types_members.py` 19, `test_collection_seams.py` 11, `test_config_relative_paths.py` 5,
`test_index_fingerprint_golden.py` +21 (recipe_of golden on all 8 examples, == constructed
`index_recipe` on 6 examples + the live shape, v3 re-declaration, trace fingerprint). Ruff clean.

Follow-ups: LA wires `recipe_of` into `WritePath.index_recipe` (or keeps the golden equality) and
adds the decomposition; each lane removes its keys from the `test_config_cli_a2.py` allowlist and
relaxes `runtime/collection.unserved_blocks` when it serves them; M1a decides how F0's
archive-vs-live collision (chain B) maps to `PinnedView`'s one-tier rule.

## M2b-1 — identity registry v1 (lane LD) — 2026-11-09 (worktree branch from `2d4fed0`, commit `f58882a`)

Plan: docs/plans/unified_merge.md rev 3.3 §6.9 (identity), §6.12 (digest mismatch), §6.13 (worst
case), §10.7 touch list M2b-1. Evidence: **synthetic only** (F0 fixtures); the reviewer is a fake
`ChatProvider`; no service, no paid model call.

Done:
1. `crosssource/identity_candidates.py`: deterministic rules — `exact`/`casefold` unique hits
   auto-match (owner rule tenant name = organization name); `curated`, `environment_suffix`,
   `token_set_jaccard` (>= 0.6) and `token_prefix` are candidate-only; >= 2 customers -> `ambiguous`;
   no value -> `missing` (record ids kept).
2. `crosssource/identity.py`: private append-only SQLite registry (triggers refuse UPDATE/DELETE;
   `registry_version` per mutation; tables customer, curated, classification, evidence, decision,
   review, alias). Decisions `match | exclude | reject` need evidence (`web`: URL + retrieval date +
   quote); a non-exact match is `matched` only with a coordinator decision **and** an `agree` review;
   `refutes` -> `contested` (owner queue), only an owner decision resolves it; casefold auto-matches
   are reviewed non-gating. Report per namespace with value and (when counts are known) record
   denominators, owner/review queues. Export YAML/JSON with content digest (never overwrites);
   import of exports and of the F0 fixture format (deterministic claims re-checked, derived statuses
   must reproduce, else nothing is written). `IdentityView.pinned(path, sha256)`: only `matched`
   aliases yield a `customer_id`; missing file / digest mismatch / edited export -> joins disabled.
   `review_queue`, `review_plan` (dry run), `run_review` (strict reply schema, citations limited to
   the supplied fields/evidence, stops at the first refusal/provider error/invalid reply).
3. `crosssource/review_ledger.py`: shared append-only ledger (ceiling, reservation, settlement);
   ceiling + approval_ref recorded at first use from `crosssource.links.review`; a higher configured
   figure is ignored and reported, a lower one applies; `approve_ceiling` (new approval ref, cap
   US$10) is the only raise; worst-case reservation (chars/3 input, max_tokens output) under
   `BEGIN IMMEDIATE`; unsettled reservations stay counted; `ReviewMeter` (CallMeter) + `RunCap`.
4. `cli_identity.py`: `identity describe | candidates | decide | review [--dry-run] | report | export
   | import`; config-only.

Deviations:
- Task text said "§6.5-identity"; the identity contract is §6.9 (used).
- `token_prefix` rule added (not in §6.9's list): F0's traps need it (`eastvale` -> two Eastvale
  customers = ambiguous; `alderbank-prod` -> suffix-stripped `alderbank` -> `Alderbank Ltd`). It is
  candidate-only and requires the value's tokens to be the *leading* tokens of the name. F0 rule
  labels for the two ambiguous entries (`casefold`/`token_set_jaccard` at 0.5) are not reproduced;
  statuses, customers and candidate sets are.
- Coordinator decisions carry rule `decision` (F0 export uses `curated`); `import` accepts both.
- "Sonnet reviews every non-exact match": casefold auto-matches are queued too but non-gating.
- The review ledger path is not a config key (L0 owns `core/config_crosssource.py`): default
  `review_ledger.sqlite` next to the registry, `--ledger` overrides. No `Config` change, so no schema
  regeneration.
- The engine's v3 refusal is **not** loosened: no request path serves identity joins yet (M2b-2/M4
  wire `IdentityView`). `docs/VALIDATION.md` not edited (L0-owned, §10.7.1); evidence recorded here.
- Existing tests changed: `test_collection_seams.py` (the `identity review/report` "not
  implemented" rows removed; replaced by `test_identity_registry.py` CLI tests);
  `test_config_cli_a2.py` (`crosssource.links.review.{usd_ceiling,approval_ref}` removed from the
  unread allowlist).

Tests: unit + contract + qa + rest (`-m 'not external'`): 3,900 passed, 58 skipped (before, same
venv and worktree at `2d4fed0`: 3,831 passed). New: `test_identity_candidates.py` 14,
`test_identity_registry.py` 43 (incl. 4 CLI), `test_review_ledger.py` 14. Ruff clean.

Follow-ups: real namespaces need a customer list (ticket organizations; `--seed-customers`), value
exports per namespace, coordinator decisions with evidence, `crosssource.links.review` + a priced
reviewer role, then a review run with `--max-usd` <= 1.00 (M2b identity pool) and re-pinning the
sha256. Stale ledger reservations of a crashed run stay counted (no release command yet); an
`uncertain` review is not re-queued automatically; a CLI for `approve_ceiling` is not exposed.

## M4 guard — one SQL surface: setup contract, enforced join allowlist, units (lane LE) — 2026-11-09 (worktree branch from `2d4fed0`, commit `99bf21c`)

Plan: docs/plans/unified_merge.md rev 3.3, §6.8 and §10.7 (M4 guard, N2: `sql_surface/*` only;
`projection/store.py` untouched). Evidence: synthetic only (SQLite files built from the F0 unified
collection in the test workspace); no service, no model call.

Done:
1. `sql_surface/setup.py` (new): `SqlSurfaceSetup` (attachments `alias`/absolute `path`/role;
   views with `source`, `kind`, columns with type/`unit`/physical name, parts each pinned by
   `eq | le | in` typed-literal pins; declared join paths; access column; pin labels).
   `open_surface`: fresh in-memory connection, `ATTACH ... mode=ro`, physical/pin column check,
   `CREATE TEMP VIEW` per view (table-qualified refs, parts `UNION ALL`), column check, then
   `query_only`, the SELECT/READ authorizer (view columns; attached-table columns only on behalf of
   the declaring view; `SQL_FUNCTIONS`) and the deadline; failures raise `SqlSetupError`
   (`attachment_missing | view_mismatch | setup_failed`). `setup_catalog` (per-source tables,
   units, `join_policy: enforced`, pins), `SetupValues` (table-aware grounding under the ACL),
   `execute_setup` (executor's returned-row check for a tenant).
2. `guard.py`: `_check_joins` (enforced catalogs: every `ON` conjunct, correlated subquery
   equality and `IN`/`=` subquery semi-join on a non-aggregate is a bare-column equality along a
   declared path, else `join_not_declared`, repairable, allowed paths listed; `id = id` self-join
   and computed-measure comparisons are not joins); `_check_units` (`mixed_units`); grounder hooks
   for a `table_aware` value source; request filters per table; hidden access column only for
   tables that have it; `GuardedSql.join_paths`.
3. `catalog.py`: `SqlTable.source`, `SqlColumn.unit`, `SqlJoin`, `SqlCatalog.joins/join_policy/
   pins`, `join_pairs()`, `has_units()`; `render()` adds "TABLES PER SOURCE", the enforced join
   list, a `unit` column and a units rule; `sealed()` keeps projection-catalog fingerprints
   identical (new fields dropped from the digest at their defaults; test asserts it).
4. `executor.py`: `run_statement(conn, guarded, timeout, lineage)` shared with the setup.
5. Docs: ARCHITECTURE.md "Multi-source SQL setup" section. No `Config` change (no schema regen).

Deviations:
- **Join enforcement is per catalog** (`join_policy`): setup catalogs enforce; projection
  catalogs keep `shown` because existing tests (`test_b5_sql.py`, `test_corpus_b_fixes.py`) and the
  live single-file surface accept joins on undeclared columns (`a.module = t.module`). Flipping the
  projection default is an M4-completion decision after reviewing the live service's executed SQL.
- **Bare key stays `id`** (guard contract: hidden ids, lineage, citations): the plan's
  `ticket.doc_id` / `customer.customer_id` keys are views' `id` columns mapped from the physical
  column (`physical: doc_id`); join paths name `id`.
- Correlated subqueries and `IN (SELECT x)` semi-joins are also checked (the plan names only `ON`);
  without it a semi-join bypasses the allowlist.
- Every view part must carry at least one pin (fail closed; I4 "refuses unpinned shapes").
- The value cross-field rewrite (`other_field`) is not offered on a table-aware source (it reads
  one field-keyed schema).
- `docs/VALIDATION.md` is L0-owned (§10.7.4); its entry is left to the merge (text below).

Tests: full tree (unit + contract + qa + conformance + parity + root) 4,290 passed, 45 skipped
(before, same venv with `sqlglot` 30.30.0: 4,183 passed, 45 skipped). New (107):
`test_sql_setup_contract.py` 41 (order attach/views → query_only → authorizer via a recording
connection; connection-level denials; `mode=ro`; model SQL ATTACH/CREATE/UNION/qualified/system
refusals; pins over history, superseded slices, snapshots, registry and link versions; pin and
setup validation; fail-closed setup; ACL per table, `acl_not_applicable`, returned-row check;
request filters; grounding; catalog text; fingerprints incl. unchanged projection digest),
`test_sql_join_allowlist.py` 36, `test_sql_units.py` 30. Ruff clean. Existing sql_surface tests
unchanged and passing (ratios, contributor_ids lineage, exact-first included).

VALIDATION entry for L0 (synthetic): M4 guard — setup-before-authorizer, model SQL cannot
attach/create/union, pins on every view, `join_not_declared`, `mixed_units` verified on synthetic
F0-derived files only; no live or real-service evidence; the ≥ 15 frozen cross-source SQL
questions gate (§10.6) is not run (M4 completion).

M4 completion still needs: the attach list of a pinned collection view from `projection/store.py`
(after M1b) and per-member projection files / registry / links files as setup inputs; wiring the
setup into `typed_query` (`run_sql_surface` evidence with pins and `join_paths`, routes, catalog
per principal, `projection_unavailable` mapping of `SqlSetupError`); materialising `customer_id`
through the pinned registry version at query time (the fixture precomputes it); a decision on
`join_policy` for projection catalogs; the frozen ≥ 15 cross-source SQL questions; key uniqueness
for views whose ids repeat across archive and slice (F0 chain B).

## M1a — unified merge partition core (lane LA) — 2026-11-09 (worktree branch from `2d4fed0`, commit `7664b2a`)

Plan: docs/plans/unified_merge.md rev 3.3, M1a (§6.1, §6.2, §6.5, §10.2, §10.7.3). Evidence:
synthetic only (F0 fixture, examples corpus; sqlite bm25, hash embedder); no service, no model call.

Done:
1. `ingestion/id_directory.py` — `SqliteIdDirectory` implements the frozen `IdDirectory` protocol:
   `register` (one transaction, O(new unit) indexed probes; duplicate non-catalog id → `IdConflict`,
   nothing written; exceptions: same-member same-day slice supersede, `replaces=` units, and
   `catalog_provisional` carriers, taken over by `<member>/catalog`), `supersede`, `tombstone`
   (idempotent; unknown id refused), `retire`, `purge`, `resolve`/`resolve_many` (one indexed lookup
   filtered to `view.unit_ids()`; a mark by a unit outside the view does not hide the row),
   `shadowed(unit, view)`, `live_ids(member, day)`, `stamp()` for cache keys. Open failure →
   `BackendError` (fail closed).
2. `ingestion/members.py` — `MemberRegistry` (`<catalog sqlite path>/collection/`): `build` checks
   the declaration (sources, record kinds, id namespaces, key scheme; catalog kinds only under key
   `catalog`), runs today's `WritePath` into `members/<member>/<key>/<build>/store`, registers ids,
   then writes `member.json` (`MemberRef`: partition id = sha(ids + content + recipe), location-free
   `PartitionRecipe`, counts, facet locations, sha256 of the store manifest and CURRENT); any failure
   removes the build and its ids. `catalog_documents` (latest-wins), `verify`, `retire`.
3. `ingestion/compose.py` — `compose` (O(members), CAS on the unified `CURRENT` under the lease;
   refuses undeclared/adopted/unregistered/retired members, directory row count ≠ member counts,
   digest mismatch, foreign embedding space, analyzer differing within a source, `fts5`; merged
   coverage/documents/status); `PartitionedWritePath` (`current()` compares only the collection
   recipe; `ingest`/`delete_generation` refused).
4. `ingestion/recipes.py` — `collection_recipe`/`collection_fingerprint`, `partition_recipe`
   (location-free member part of `recipe_of`), `embedding_space_ref`, `analyzer_recipe`,
   `unified_fingerprint`.
5. `backends/sqlite/members.py` — `PartitionedSqliteBackend` (+ `PartitionedCatalog`,
   `PartitionedRecords`, `StratifiedLexical`, `MergedVector`, `MemberStratumScorer`): see
   ARCHITECTURE "Unified merge M1a". `arms/stratified.py`: RRF fusion with largest-remainder quota
   slots, `StratumRanking` builder.
6. `backends/sqlite/search.py` (`_bm25_candidates` refactor, `rank_rows`, `stratum_part`,
   `vocabulary`), `bm25.py` (`scores_with`, `okapi_idf`, `stratum_idf`, `average_idf_of`),
   `catalog.py` (`SqliteRecords.matched` with exclusions) — default code paths produce identical
   results (whole suite green).
7. `runtime/collection.py` — `PartitionedView`, `unserved_blocks` now lists adoption, layers,
   `crosssource`, `communities.layers`; `partitioned_refusals` (one sqlite write backend, bm25, no
   projection/logs, no graph/community/segment/typed_query arm). `engine.py` — wraps the sqlite
   backend, `PartitionedWritePath`, `build_member`, `compose`. `cli_collection.py` — `register`,
   `compose`, `status` (`adopt` still M1b).

Equivalence-kit results (`tests/contract/test_partition_equivalence.py`, 23/23): single member ==
monolithic for support, tracker, analytics (2 units) and the example corpus (fused + per-arm hits
with exact scores, aggregates, answer results and packed evidence, records); source-scoped requests
in the 9-unit F0 collection == that member alone; support lexical/dense candidates identical with
and without the other members; log stratum (5 units) == one store (ids/order exact, scores ≤ 1e-9);
dense top-40 == monolithic; 5 records specs, listings (page sizes 1/7/50/500), routed get, values
and counts == monolithic.

Deviations / rulings:
- **Support stratum**: adoption is M1b, so the equivalence kit builds support as a write-path
  `snapshot:` member; R1 (adopted store, today's files) is tested again by M1b's
  `test_support_stratum_equality.py`.
- **Stratum = member id.** All units of one member form one stratum; `stats: query_time` (default)
  sums N, token totals and query-term df over the units minus shadowed rows and recomputes BM25Okapi
  idf; the epsilon floor uses the **exact** union-vocabulary average idf, cached per pinned unit set
  (not the declared lag approximation; LB may switch to a maintained `average_idf` for slices).
  Exact ids/order vs one store; scores ≤ 1e-9 (summation order). A single unconstrained unit is
  served by today's search unchanged.
- **Dense** is an exact merge across units (one embedding space enforced at compose), not
  per-source stratified lists (coordinator: "dense exact top-k merge over flat indexes"); ties follow
  one store's corpus order (score, document id, in-unit rank).
- **Foreign embedding space** is refused at compose (fail closed) instead of "member refused by the
  dense arm, labelled": labelled per-member degradation needs the coverage envelope (M1b/M2b).
  Likewise a digest mismatch at serve time refuses the request (`BackendError`) instead of marking
  only that member unavailable.
- **Records** units are pruned by the ACL only (the population/denominator counts every readable
  record); scope pruning applies to catalog, lexical, vector. Time-range pruning of day members is
  not implemented (source/kind pruning only).
- **Unit ids** in the directory are `MemberRef.unit_id` (`<member>/<key>`): a key is registered once;
  rebuilding it needs `retire` + `purge` (or a new key with `replaces=`). Catalog member versions
  therefore cannot coexist under the same unit id — LB/M2a: rebuild the catalog at close with
  `retire(old)` after the new generation is active, or extend the unit id with a version (frozen
  type change, not done).
- **Chain B (F0 archive-vs-live episode id)**: the directory refuses the duplicate unless the slice
  is registered with `replaces=["logs/day:<closed day>"]` (latest version wins for the continuing ids
  only; the archive keeps serving all others). Test: `test_id_directory.py`.
- `IdDirectory.register` gained keyword-only `member=` (slice ids carry no member) and `replaces=`
  in the implementation; the frozen protocol signature is unchanged (runtime-checkable by name).
- `chunk_id → unit` routing for hit reads uses a bounded in-process owner map filled by this
  backend's search results; unknown chunk ids are probed on every pinned unit (indexed `IN`).
- Existing tests changed: `test_collection_seams.py` (collection is served: the view test expects
  `["crosssource"]`; doctor on the fts5 v3 config reports the fts5 refusal; `collection compose`
  removed from the "later increments" list; +1 partitioned-view test), `test_config_v3_schema.py`
  (the adopted-member refusal path is `collection.members.support.adopt`), `test_config_cli_a2.py`
  (`collection.id_directory` removed from the unread allowlist).

Tests: full `-m 'not external'` 3,893 passed, 58 skipped (before, same venv: 3,831 passed, 58
skipped). New: `tests/contract/test_partition_equivalence.py` 23, `test_partition_scope_pruning.py`
8, `tests/unit/test_id_directory.py` 12, `test_members_compose.py` 10, `test_recipe_decomposition.py`
6, `test_partition_caches.py` 2, `test_cli_collection.py` 1; shared helpers `tests/partition_support.py`.
Ruff clean. No `Config` change (schema not regenerated).

For M1b / LB:
- M1b: adoption plugs in as a unit whose `open()` reads the adopted store read-only (the pool keys by
  `partition_id`); relax `unserved_blocks` for `adopt`/`layers`; projection/listing/values per member
  (records already merge with one `scope_result`); coverage labels for unavailable members.
- LB: slices are further units of the log stratum — add them to `PinnedView.slices`, open them in
  `PartitionedSqliteBackend` (today `units()` iterates members only), register with
  `member="logs"`; `SqliteLexical.stratum_part`/`vocabulary`/`rank_rows` and
  `MemberStratumScorer` are the hooks for query-time stats (`backends/sqlite/query_stats.py` can wrap
  them); tombstones via `IdDirectory.tombstone(ids, by=slice)`, the diff base is `live_ids`.
- Follow-ups: unit pool eviction for retired members; time-range pruning; a recipe golden for
  `partition_recipe`; `core/config.py` field comment still says "validated but not served" (L0 file).

## O6 — success heartbeats and `log_liveness` (lane LC follow-up to M0) — 2026-11-09 (worktree branch from `2d4fed0`, commit `2d6fecc`)

Plan: docs/plans/unified_merge.md decision O6 (record success heartbeats); task: package side of the
staged collector change (private proposal, not quoted). Evidence: synthetic only; no service, no
log host, no paid call.

Done:
1. `ingestion/episode_summaries.py`: `SUCCESS`, `check_precedence` (success rules after every
   failure rule; also in `compile_summary`), `success_phrases`, `heartbeat_buckets`,
   `check_heartbeats` (the collector's 5-minute block must equal the buckets of the minute series).
   `SummaryBuilder`: success runs as `incident`/`signature_day` with `category: success`,
   `episode_status: success_heartbeat`, no evidence and no community; success `episode_cell` and
   `signature` rows carry `category: success`; no `shared_error` from success rules; coverage
   counts the failure side (`keyword_documents`, `rule_documents`, `episodes`) and the success side
   apart (`success_documents`, `success_episodes`, `success_keyword_counts`). Failure-only recipes:
   byte-identical documents (golden digest computed with the package at `2d4fed0`).
2. Failure views exclude `category: success` by default: `issue_history` (`HistorySpec.exclude`,
   SQL `IS NULL OR NOT IN`, excluded messages stated; `category: [success]` asks for them),
   `incident_episodes`, summary `log_top`/`log_trend` (unless a rule is named), outage
   investigation selection (`episode_tools.failure_only`).
3. `log_liveness` intent/tool (`decision/log_intents.py` `LivenessInput`, `detect_liveness`;
   `runtime/liveness.py`): last success, observed-until (scope capture horizon), median interval
   between run starts (or `continuous`), `halted` above `halt_factor` x cadence, `no_heartbeats`
   for named scopes, recovery (first success after a failure episode, time to recovery),
   pre-heartbeat days (other recipe via `episode_history.day_recipes`, before
   `keywords.effective_from`) stated and excluded from intervals. Config `logs.collection.liveness`
   {`cadence_days` 7, `min_intervals` 3, `halt_factor` 3.0}. `load_keyword_recipe` refuses success
   rules before failure rules and success rules without a filterable `category` field.

Edits outside LC-owned files (minimal, listed): `decision/log_intents.py` (LC→LF shared: new input
model, intent name, compile refusal branch, `detect_liveness` called first inside
`detect_issue_history`; no change to existing detectors), `core/config.py` (one `LOG_TOOLS` entry),
`core/config_log_policy.py` (`LivenessConfig` + one validator line), `ingestion/log_policy.py`
(LB-owned: 6 lines in `load_keyword_recipe`), `ingestion/episode_history.py` (unowned I1 module:
`exclude`, `pieces`, `horizons`, `day_recipes`, `excluded_occurrences`), `ingestion/episode_summaries.py`,
`runtime/{episode_tools,issue_history,log_tools}.py`. Not touched: `decision/base.py`,
`engine.py`, `ingestion/{episode_archive,provisional,slices*,pins}.py`, `crosssource/*`,
`sql_surface/*`, `arms/*`.

Deviations:
- The 5-minute heartbeat buckets are **validated, not stored** as their own rows: a new archive
  kind would need `episode_archive.py` (LB) and the archive refuses unknown kinds. Liveness reads the
  success runs (same minute series, `keywords.gap_seconds` resolution), which carry last success and
  interval information; intervals shorter than `gap_seconds` are not visible (stated).
- Detection lives inside `detect_issue_history` (liveness first) so `decision/base.py` is unchanged;
  it runs only on episode-archive generations, as `issue_history` does.
- Recovery is temporal co-occurrence in one tenant/env/service scope (any asked success rule), not
  operation-correlated; stated in every recovery answer.
- `log_liveness` is a typed intent/tool, not exposed to a separate UI view.

Tests: unit + contract + qa + conformance + parity + root (`-m 'not external'`) 3,873 passed, 58
skipped (before, same venv on a `git archive` of `2d4fed0`: 3,830 passed, 59 skipped). New:
`tests/unit/test_success_heartbeats.py` 30, `tests/contract/test_log_liveness.py` 12, fixture
`tests/heartbeat_support.py`. No existing test changed. Ruff clean. `docs/config.schema.json`
regenerated.

Log-host configuration (owner applies with the collector change): pin `policy/15-success.yaml`
last in `logs.collection.keywords.files` and set `keywords.effective_from` to the first day captured
entirely with it (archive the old-recipe days under the old pin first); `metadata_schema` field
`category` filterable (refused otherwise); optional `logs.collection.liveness`; add `log_liveness`
to `tools.enabled` for the tool route; deploy this package to the serving and policy venvs before
the first new-recipe snapshot is refreshed/archived (the old package does not exclude success rows).

Follow-ups: LF may move liveness detection into its unified intents (M2b-2); a stored 5-minute
heartbeat kind if sub-`gap_seconds` cadences matter (LB, archive kinds); per-customer cadence needs
regex fields on matched lines (not collected); real-data evaluation of halt thresholds after go-live.

## M2a — live log path producer (lane LB) — 2026-11-09 (worktree branch from `2d4fed0`, code commit `717e5d1`)

Plan: docs/plans/unified_merge.md rev 3.3, M2a (§6.7, §6.5 log stratum, §6.10, §10.4, touch list
§10.7.3). Evidence: synthetic only (tests on F0 fixtures + seeded interleavings; benchmark on the F0
600k-chunk generator in `~/su_mutliarm_tests/claude/m2a-20261109/`); local BGE-M3 only.

Done:
1. `ingestion/log_slices.py` — `LogSliceLedger` implementing the S0 `SliceLedger`: delta slices
   sealed with sha256 (`handoff.seal_unit`), `supersedes`, **tombstones** (B12, from `full_day`
   builds), `catalog_provisional` (N3; known catalog docs excluded, changed ones parked for close),
   `excluded_stats`, per-row **versions**, compaction 5 min → hourly → base (`compact`, `due`,
   `run_due`) with the `max_units_per_day` bound, `release_day`, `vector`/`vector_for` (one tier per
   day), `gc`/`retire` (slice pins + `gc_grace_seconds`), `receive` (sealed handoff, lists re-checked
   against the local state), optional `IdDirectory` mirroring (fail closed), `embed_pending_slices`,
   `collapse_upserts`, unit format (`write_unit`, `UnitReader`).
2. `ingestion/log_members.py` — `close_day` (archive day member + per-source catalog member,
   latest wins, none when unchanged), `build_month_member` (one row per id, unique-text dedupe),
   `member_ref_of` (verified `MemberRef`), `catalog_shas`.
3. `backends/sqlite/query_stats.py` — `QueryTimeScorer` (S0 `StratumScorer`): query-time `N`, token
   totals, per-term `df` over served rows; `average_idf_lag` declared; `DenseLogScorer`; `LogUnits`
   (reader cache keyed by unit id + seal), `ServedRows` (served-row masks cached per pinned set).
4. `ingestion/text_vectors.py` — text-hash float16 store pinned to one embedding space, crash-safe
   appends, `embed_rows` (declared fields only; log-line text refused), `embed_missing`, `pick_device`.
5. `ingestion/handoff.py` — `seal_unit`, `verify_unit`, resumable `transfer`.
6. `ingestion/pins.py` — `SlicePins` (`slicepin:<slice>#<holder>` rows in the generation pins' store;
   works with `ProcessPins`), `slice_pins(registry)`.
7. `scripts/bench_log_slices.py` — the §6.7 benchmark (simulated timeline, writer/dense worker,
   gc on a simulated clock, close + transfer + verify).
8. Docs: ARCHITECTURE (M2a section), CONFIGURATION (which S0 keys the ledger reads; no key changed,
   schema unchanged), VALIDATION (M2a entry with the benchmark table).

Benchmark vs §6.7 targets (details in VALIDATION): lexical refresh-to-servable p95 0.38–0.59 s
(≤ 5 min ✓); dense p95 2.6 s (≤ 15 min ✓); log activations 1/day by construction (≤ 6 ✓, not a
compose measurement); compose **not measured** (LA) — member build 12.6–14.8 s background, transfer
0.23 s, verify 0.17 s; first request after slice +0.43 s p95 (≤ +1 s ✓); live-window lexical p95 0.45 s
cold, dense p95 0.52 s (≤ 3 s ✓) but dense **max 7.1 s** after a base compaction (cold matrix);
vector disk 0.44 GB for the replayed day (≤ 0.5 GB ✓, close: 216k unique texts incl. intermediate
versions); embedding GPU 476/s shared (916/s isolated), CPU 35.7/s (≥ 300 / ≥ 30 ✓); ≤ 18 units/day.

Deviations:
- **Versions decide the served row.** Each row carries the sequence of the slice that first sealed
  its content (compaction keeps it); served = highest version per id, a newer tombstone hides older
  rows. The plan's lists (`supersedes`, `tombstones`, `excluded_stats`) are still recorded and
  checked on `receive`, but query-time statistics use the version masks (exact under compaction
  timing and cross-day continuation). `SliceRef.sequence` is member-global (monotonic per day too).
- `seal(day, level, documents, *, replaces=(), full_day=False, as_of=None)` returns `None` when
  nothing changed (protocol type says `SliceRef`); `replaces` is refused for refreshes (compaction
  uses `compact`). Repeated ids in one call are refused; producers collapse upsert logs first.
- The tombstone diff uses the ledger's own `live` table (authoritative for slices); the
  `IdDirectory` is mirrored (`supersede` → `register` → `tombstone`/`retire`) when given.
- Compaction holds the writer lock for its duration (base ≤ 16 s on the 600k day), so a refresh
  never races a compaction; measured as lexical lag (max 1.16 s).
- An id that continued out of an archived day and then merged away: the ledger remembers archived
  rows for `ARCHIVED_ROW_DAYS = 2` and the archive day member carries the tombstones that still hide
  them (found by the property test).
- Slices carry records, bm25 and embed rows; **typed projection rows are not built per slice** — the
  I1 provisional projection store keeps serving typed rows (M4/LE and LA decide the slice SQL setup).
- Touch-list files not modified: `ingestion/provisional.py`, `episode_archive.py`, `log_policy.py`
  (no change needed for the producer), `runtime/collection.py` (LA→LB ordered; LA has not merged),
  `core/config_collection.py` (S0 already has slice settings, `catalog_kinds` and the `embed_fields`
  retention validator) — so `docs/config.schema.json` is unchanged.
- `tests/contract/test_live_window_engine.py` is `test_live_window_scoring.py` (no engine routing
  yet); property tests use seeded `random` (hypothesis is not installed).
- Benchmark: three earlier days are loaded as one full-day slice each (not replayed); compose and
  activation are not measured.

Tests: full `not external` suite 3,893 passed, 58 skipped (before, same venv, `2d4fed0`: 3,831
passed, 58 skipped); 62 new tests in 9 files. One existing test caught a name clash
(`test_retention_scopes_and_raw.py` forbids a function named `read_unit` outside the raw store):
the handoff reader is `read_sealed`. No existing test changed. Ruff clean.

Wiring after LA (M1a) merges:
1. `runtime/collection.py` partitioned view: `pin()` reads `ledger.vector_for(generation.archives)`
   into `PinnedView.slices[member]` and holds the slices through `ProcessPins(slice_pins(registry))`
   for the request/session.
2. `arms/lexical.py`: the log stratum ranks with `QueryTimeScorer(LogUnits(ledger, member_registry))`;
   fuse strata by RRF under `source_quotas`.
3. `arms/dense.py`: log stratum through `DenseLogScorer` (text-hash store); coverage `dense: pending`
   from its approximations.
4. `ingestion/id_directory.py`: pass LA's directory as `LogSliceLedger(directory=…)`; confirm it accepts
   supersede-then-register for compaction and for cross-day continuation (F0 chain C).
5. Compose: include `close_day` members (day + catalog) in one compose, then `ledger.release_day(day)`;
   feed `ledger.catalog` from `catalog_shas(current catalog member)`.
6. Build the ledger from config (`collection.members.<id>`: `embed_fields`, `catalog_kinds`,
   `collection.slices`, `collection.embedding_space`) with the member `PartitionRecipe` from
   `recipes.py`; relax `runtime/collection.unserved_blocks` for the log member.

Follow-ups: warm a new base's dense matrix in the compaction job (the 7.1 s cold max); vector-store
GC of texts no longer referenced after close (only ~45k final texts/day are needed); a producer CLI
(`logs slices refresh|compact|close|gc`); slice typed projection rows with M4; real-data run and a
shadowed staging week (§10.4).

## M3 — cross-source links tiers A/B, link store, link review, daily ticket snapshot (lane LD) — 2026-11-09 (worktree branch from `39f9336`, commit `ee839f2`)

Plan: docs/plans/unified_merge.md rev 3.3 §6.9 (links), §6.12 (link store unavailable, identity
mismatch), §6.13 (worst-case reservation), §10.6 M3 gates, §10.7 touch list M3 (LD part).
Evidence: **synthetic only** (F0 fixtures: planted links, the five must-not-link traps, gap-filling
merge); the reviewer is a fake `ChatProvider`; no service, no paid model call.

Done:
1. `crosssource/links/signature_mentions.py`: rule catalog from the pinned log documents
   (`signature`/`incident` rows); a mention = the rule id as a token (not inside a longer
   identifier) or the whole rule title (>= 2 words, case/whitespace-insensitive). Family id
   `family:<rule>`.
2. `crosssource/links/builders.py` (`BUILDER_VERSION = links-b1`): input = pinned documents in pin
   order (later version of an id wins). Tier A `explicit_ref` (ticket/tracker text naming a record
   id or URL, incl. a record's `url`; status `accepted`; unresolved id/URL mentions reported as
   `dangling`, never linked). Tier B `customer_window_signature` (ticket<->episode: both customers
   matched through `IdentityView` only, ticket `created_at` in `[first - before, last + after]`,
   signature mentioned; service equality recorded as evidence, not required), `signature_mention`
   (ticket<->family, family<->doc), `title_mention` (ticket<->doc). Disabled identity view disables
   only the customer builder (reported). Per-subject/builder cap `max_per_record` (nearest first),
   one link per (pair, subject, object) with A over B. Report with input counts, skip reasons
   (denominators), dangling sample, cap count. `click_links` stubbed (`until: M7`).
3. `crosssource/links/store.py`: private append-only SQLite (`link_version` per build/review/
   decision; triggers refuse UPDATE/DELETE). Seven F0 statuses; model verdicts bound to the
   evidence digest (rebuild with new evidence re-queues); coordinator vs model -> `contested`;
   owner prevails. Rebuild retracts links not produced again: `tombstoned_subject` /
   `tombstoned_object` (record left the pinned collection) / `not_rebuilt`. Report, YAML/JSON export
   with content digest (never overwrites). `LinkView.pinned(path, sha256, identity=...)`: missing /
   mismatch / edited export -> no links (reason); display classes equal F0's (`shown`, `labelled`
   with label text, `hidden`); `show_candidates: hidden`; `max_per_record`; customer-derived links
   withheld with a limitation when the identity view is disabled or pins another registry version
   than the build; `rows()` for the future `link` SQL view.
4. `crosssource/links/review.py`: queue = served `candidate` rule links without a review of the
   current evidence or a decision; excerpts of both records from the supplied pinned documents
   (fields cut at 1,200 chars, cut stated); allowed citations; worst case reserved on the **shared**
   `review_ledger.py` (scope `links`) and the run cap before sending; stops at ceiling / run cap /
   provider error / invalid citation; records not supplied are reported `document_unavailable`.
5. `cli_links.py`: `links build [--create] | review [--dry-run|--max-usd] | decide | report | show
   | export`; config-only. Ledger default = `review_ledger.sqlite` next to the identity registry
   (same file as identity review).
6. `sources/ticket_snapshot.py`: `TicketRow` (unknown keys refused, timestamps need an offset),
   `ticket_documents` (equal to F0's ticket documents), `snapshot_diff`, `build_snapshot_member`
   (`Engine.build_member` with `replaces=[previous unit]`, removed ids tombstoned via the id
   directory's `tombstone`).

F0 acceptance: all 6 planted tier A/B links of M3 scope are built with F0's tier, builder, pair,
customer, aliases, window and mention (L01 L02 L03 L04 L05 L06); L10 (query->doc click) is the M7
click builder, stubbed and asserted absent; none of the 5 traps is built (outside window, contested
alias, candidate alias, ambiguous alias, dangling object). Extra links built on F0 (true by rule):
ticket:1005 also links the continuation piece of the cross-midnight chain A episode,
ticket:1012 its brightmoor fetch_timeout episode, ticket:1010 the getting-started URL, tracker
ENG-103 -> ticket:1003, and 6 more ticket->family mentions (one by title). Totals: 17 links
(4 A, 13 B), review queue 13.

Deviations:
- Task text said "§6.7-links"; the links contract is §6.9 (used).
- `cli.py` (L0-owned) got the one-line `links` registration and dispatch (3 lines), as for
  `identity`; no other L0 file touched. `tests/unit/test_config_cli_a2.py`: the four
  `crosssource.links.{window_before_days, window_after_days, max_per_record, show_candidates}`
  entries removed from the unread allowlist (now read), as the file's comment prescribes.
- No config key for the link store path/digest (L0 owns `core/config_crosssource.py`): `--store`
  is required and `LinkView.pinned` takes the digest from the caller. Request to L0:
  `crosssource.links.{store_path, sha256}` for serving.
- "incident<->doc" is built as family<->doc (§6.9); an incident reaches docs through its family.
  "ticket<->issue family" read as ticket<->log rule family (tracker issues: tier A explicit refs
  only; M8 adds tracker builders).
- Tier B signature match = rule id or whole rule title only; keyword-epoch splits (one rule
  superseded by two) are not expanded; service match is evidence, not a condition.
- Identity mismatch (§6.12 "link-based answers refused") applied to customer-derived links only;
  explicit references (no identity join) stay served. Safer reading would refuse all; recorded for
  the planner lane (LF) to decide at display.
- Tier-B cap applied at build (and again at display) to bound review spend.
- Daily ticket member: superseding the **adopted** store's ticket portion is not done (M1b
  `members.supersede` not merged; QA100 ticket-subset gate is real-data). Only snapshot->snapshot
  replacement is implemented.
- `docs/VALIDATION.md` not edited (L0-owned, §10.7.1); evidence recorded here.
- `uncertain` verdicts are not re-queued automatically (as identity review).

Tests: full `-m 'not external'` 4,537 passed, 45 skipped (before, same venv on a `git archive` of
`39f9336`: 4,462 passed, 46 skipped; +75 passed = 74 new tests plus one baseline-skipped test that ran here, not investigated). New: `test_link_builders.py` 24, `test_link_review.py` 38
(incl. 3 CLI), `test_link_tombstones.py` 4, `test_ticket_snapshot.py` 8; helper
`tests/links_support.py`. Ruff clean. No `Config` change (schema not regenerated).

To build links on real data (private workspace, owner-approved window):
1. Pinned identity registry (M2b-1 run done, sha256 pinned in `crosssource.identity`).
2. JSONL exports of the pinned documents: tickets (support member/adopted store), docs, log
   `incident` + `signature` documents of the archive members and the pinned live slices, in pin
   order; tracker issues if available. Check `support.account`/`logs.tenant` field names
   (override via `LinkSettings.customer_fields` if they differ).
3. `links build --create --store <private>/links.sqlite --documents ...`; inspect `report`
   (skip reasons, dangling, capped) before any review.
4. `links review --dry-run` gives the exact queue and worst case. Measured on F0: 2.3k-4.0k prompt
   characters per link; worst case US$0.009 (max_tokens 400) / US$0.018 (1,024) per call at
   US$3/US$15 per M tokens. Real tickets with three 1,200-char fields: about 8k characters ->
   worst case about US$0.014 (400) / US$0.024 (1,024) per call; committed cost lower (reported
   usage). The M3 pool (US$4 of the shared US$10 ceiling) covers about 160-280 worst-case calls.
   The §10.6 gate (precision >= 0.85 on a coordinator-adjudicated sample of 50 model-accepted
   links) needs on the order of 60-100 reviewed candidates: about US$1-2.5 worst case with
   `--limit`. Then adjudicate, `links decide`, export and pin.

Follow-ups: L0 config keys for the link store pin; LF renders `LinkView.links_for` on record pages
and citations; M4 reads `LinkView.rows()` for the `link` view; M1b `supersede` -> ticket member
takes over the adopted ticket portion (QA100 gate); epoch-aware signature mentions; M7 click
builder; M9 tier C.

## M1b — read-only adoption, attached layers, per-member projection (lane LA) — 2026-11-09 (worktree branch from `39f9336`, commit `e09f327`)

Plan: docs/plans/unified_merge.md rev 3.3, M1b (§6.2, §6.3, §6.4, §6.5 R1, §6.12, §10.3, §10.7.3).
Evidence: **synthetic** (F0 support documents, live-shape community/segment fixtures, the synthetic
kuzu attach store) for the repository tests, plus a **private real-data $0 equivalence** run over the
live support store adopted read-only in place (no copy; no service touched; no paid call).

Done:
1. `ingestion/adopt_member.py` — `adopt(config, member, registry)`: original configuration pinned by
   sha256 and loaded as v2; store fingerprint recomputed backend-free (`recipes.fingerprint_of`) and
   compared with the store's active manifest (re-stamped/relocated → `AdoptionError`); the original
   must name the store as its sqlite catalog and serve the collection's dataset; sha256 of `CURRENT`
   and every file of `snapshots/<generation>/` (runtime file and write lock excluded: they belong to
   the serving process); ids/kinds/sources read over `mode=ro` and checked against the declaration;
   ids registered in the id directory; `member.json` + `adoption.json` written into the unified
   registry only (symbolic facet location `adopted/<member>`, no host path); idempotent; any failure
   retires/purges the ids and removes the registry folder. `recipes.store_recipe` (the decomposed
   `PartitionRecipe` of one v2 store). `MemberRegistry.adopt`, `supersede` (API for M3), adopted
   `store_dir`/`unit_dir`/`verify`.
2. `backends/sqlite/adapter.py` — `SqliteBackend(readonly=True)`: catalog connections `mode=ro`;
   runtime database, create/index/upsert, lexical rebuild, projection `put_file`/`forget` and
   `delete_generation` refused (`refuse_write`). `projection/store.py` `FileProjectionFiles(readonly)`.
3. `ingestion/layers.py` — `graph_attach` (digest = mapping sha256 + database listing; the mapping must
   declare the adopted store generation), `segment_sidecar` (directory content digest; dataset
   checked), `communities_imported` (file sha256 must equal the adopted store's imported file);
   `layer_ref`/`layer_refs`/`verify`/`recipes`; `LayerBackend` + `MappedGraph` (unified generation →
   bound store generation; writes refused); `LayerUnavailable(Error)`.
4. `ingestion/compose.py` — adopted members composed (fts5 allowed for adopted only); layers pinned
   into `manifest.layers` and the unified fingerprint; optional unavailable layers listed in
   `layers_unavailable`, a required one refuses; `view_of(manifest)` (members + layers).
5. `backends/sqlite/members.py` — adopted units opened read-only with the recorded store settings
   and generation check; `layer`/`layer_unit`/`layer_generation`/`layer_check` (verified once per
   process and generation); `PartitionedGraph` (communities/entities/mentions from the member bound
   by the `communities_imported` layer; traversal refused until M5; `layers()` readers);
   `PartitionedProjection` (one projection-bearing member served unchanged via `store_generation`;
   `attachments`); `coverage(gen)`.
6. `projection/store.py` — `attach_list(facet, gen)` (**the attach list LE needs for M4**:
   `{alias, path (absolute), role, unit}` per pinned member projection file; single store: alias =
   file stem, role `projection`), `served_generation`; `projection/catalog.py` compares the sealed
   manifest with the served generation.
7. `runtime/collection.py` — refusals loosened for adoption, the three layer kinds, imported
   `communities.layers`, graph/segment/community arms bound to layers, typed_query + projection with
   one member; still refused: other layer kinds/strategies, `crosssource`, projection over several
   members, `logs`. `engine.py`: `LayerBackend` wrapping, `trace["collection"]` coverage labels,
   `LayerUnavailableError` → empty arm result with a `layer_unavailable` report (coverage
   incomplete). `arms/segment.py` checks its layer. `communities/base.py` `CommunityLayerReader`
   protocol (for M6), `communities/imported.py` `ImportedCommunityLayer`. `cli_collection.py`:
   `collection adopt --member`, `collection layers`. `core/config_collection.py`:
   `collection.layers[].member` (validated: an adopted member carrying the layer's sources); schema
   regenerated.

Real-data gate (private workspace `m1b-20261109`; details and per-question files there):
- Store adopted **in place, read-only**; sha256 of all 26 live-dependency files (store snapshot +
  `CURRENT`, kuzu database, KG sqlite, segment sidecar, community file, mapping) identical before and
  after every run; the service-owned `runtime.sqlite`/write lock were only stat'ed. Harness guard:
  any writable sqlite connection to a live path raises; today's engine baseline ran with the same
  read-only store backend and its runtime facets redirected to the workspace.
- Original fingerprint recomputed backend-free = the store manifest's (`97f1b492…`); 19 files
  digested; the three layers pinned (`graph_attach`, `segment_sidecar`, `communities_imported`).
- (a) $0 equivalence, today's engine vs the v3 adopted collection, all 100 QA100 questions:
  - replay of the recorded B6 planner / typed-router / SQL-writer outputs (llm decision): **100/100
    identical** (45 questions have recordings and reach the generator: identical packed evidence; the 55
    without recordings fail at the planner identically on both sides);
  - rules decision + every retrieval arm run directly on each question: **100/100 identical**; direct
    candidate lists identical for lexical, dense, graph, segment and community on 100/100 questions;
    **graph evidence kept on all 68 questions where today's graph arm returns evidence** (and on the 9
    rules-decision searches with graph hits);
  - normalisation: only the unified generation id/fingerprint stamped in aggregate lineage (mapped to
    the store's) and timings; nothing else.
  - the first full run differed on 4/100 community-arm results (the `hit_parts` ordering bug below);
    fixed before the commit and rerun on the committed code.
- (b) Paid QA100 non-regression **not run and not needed for M1b**: retrieval hits, per-arm
  candidates (incl. graph evidence), aggregates and the generator's packed evidence are identical
  for every question, so answers can differ only by generator sampling. No paid call was made.

Deviations / rulings:
- **Unavailable members fail closed for the request** (as M1a): a digest mismatch of the adopted
  store raises `BackendError` instead of labelling one member of several; with a single adopted
  member that is the only safe behaviour. Unavailable **layers** degrade per arm (labelled).
- **Coverage labels** are in the search trace (`trace.collection`); the legacy envelope `coverage`
  key is LF's (M2b-2).
- **Canonical unit per kind** (`backends/_scope.py`) and **member value union** (`decision/values.py`)
  were not changed: records/values already merge per member since M1a and a single adopted member has
  one unit; mixed-unit denominators matter once log members join (M2a/M2b follow-up).
- **Graph traversal** over member stores and **projection over several members** stay refused (M5,
  M4 completion); `attach_list` is published for M4.
- `LayerRef` is frozen (L0): the member binding of a layer lives in the configuration
  (`layers[].member`) and in `LayerRef.version` (`<unit>@<store generation>`).
- The aggregate lineage stamps the **unified** generation id/fingerprint (the request pin); the
  equivalence comparisons map those two strings to the store's before comparing (counted) — nothing
  else differs.
- `docs/VALIDATION.md` is L0-owned (§10.7.4): its entry text is below for the merge.
- Regression fixed in M1a code (found by the real-data run): `PartitionedCatalog._chunk_groups`
  reordered mixed remembered/unknown chunk ids, so `hit_parts` could give a document's text to a
  different chunk (community arm `Hit.text=None` on 4/100 questions); order is now the caller's
  (`test_member_listing_values.py` regression test).
- Existing tests changed: `test_collection_seams.py` (describe keys; `collection adopt` parses with
  `--member`; "pending" test replaced by `test_no_collection_command_is_pending` and an adopt refusal
  test), `test_config_v3_schema.py` (the refused-block case is now an unserved `links` layer),
  `test_config_cli_a2.py` (`adopt.original_config{,_sha256}` removed from the unread allowlist).

Tests: full `-m 'not external'` 4,523 passed, 45 skipped (before, same venv at `39f9336`: 4,463 passed, 45 skipped).
New: `tests/unit/test_adopt_member_readonly.py` 10, `tests/contract/test_support_stratum_equality.py`
22, `tests/contract/test_attached_layers_engine.py` 12, `tests/unit/test_member_listing_values.py`
14, `tests/conformance/test_traversal_templates.py` +1; helpers `tests/adopted_support.py`. Ruff clean.

VALIDATION entry for L0: M1b — read-only adoption, attached layers and per-member projection verified
on synthetic fixtures; private real-data $0 equivalence on the live support store adopted read-only in
place (QA100: identical retrieval, per-arm candidates, graph evidence, aggregates and packed evidence
vs today's engine; 45 questions with recorded planner/router/SQL outputs, 100 with the rules decision
and direct arm runs); store files unchanged (sha256). No paid QA run (not needed: identical evidence).

For M2b-2 / LB / LE:
- LE (M4): `projection.store.attach_list(engine.backends[catalog].relational, gen)` gives the pinned
  member projection files (absolute paths, `unit`); `PartitionedProjection` refuses single-file reads
  over several members until the setup is wired.
- LB (M2a): the log member joins as further units; `trace.collection` already lists members/layers;
  `layers_unavailable` semantics and `LayerUnavailableError` are available for shared-error layers.
- LF (M2b-2): the legacy envelope's `coverage` can be built from `trace.collection` (members: unit,
  tier, sources, as-of, status; layers: status/reason). The unified config keeps the original's
  planner/arm settings; the adopted support member answers as today.
- Follow-ups: mixed canonical units in partitioned denominators; per-member labelled degradation for
  required members (§6.12 `/ready`); `index adopt` vs `collection adopt` naming in the CLI help.

## M2b-2 — one planner, two halves, per-source coverage, unified dataset switch (lane LF) — 2026-11-09 (worktree branch from `a44185a`, code commit `6437ba1`)

Plan: docs/plans/unified_merge.md rev 3.3, M2b-2 (§6.11, §6.12, §7, §10.5, §10.7.3 touch list).
Evidence: **synthetic only** — F0 support member (tickets/docs) plus synthetic tickets, a synthetic
episode archive (closed days + one provisional day, failure and success-heartbeat families, F0 log
tenants), the F0 identity registry imported and pinned, a synthetic pinned link store; fake providers
(`JsonModel.call` replies, loopback `FakeChatServer`). No service touched, no paid call. The private
owner questions were read for their shapes only; the repository holds neutral synthetic equivalents.

Done:
1. `decision/two_halves.py` (new) — `TwoHalvesAgent` wraps the pipeline's agent when
   `crosssource.planner.enabled`. Operations half deterministic: today's detectors first
   (`DecisionAgent.log_step`), then `log_intents.detect_operations` (failure symptom about a grounded
   subject: known service/tenant, vocabulary term, registry customer -> `issue_history` over the default
   live view; `log_top` by signature without an archive; no subject -> no operations half). Grounding:
   M0 binding (vocabulary, question clock, `default_view_days`); customers via
   `values.ground_customers` (matched aliases of `IdentityView.pinned` only) -> per-tenant calls, or the
   operations half refused for a customer without a matched tenant (stated, never widened); a bare hour
   on a liveness question states both 12-hour readings and, with "what was the error", adds that day's
   episodes; a liveness question describing a failure adds the failure episodes of the same scope; an
   outage question on an archive reads episodes instead of the graph/community investigation; a
   search-ranking question names search analytics (not served, stated). Knowledge half: the wrapped
   agent with the log capabilities removed (today's decision and llm payload when there is no
   operations half). Merge: `policy_applied: two_halves`; rollup-only log questions without a knowledge
   cue keep the operations half only; a deliberate knowledge refusal keeps the operations half
   (`two_halves_operations_only`); planner failures -> labelled fallback. `source_coverage()` builds
   `trace.source_coverage`.
2. `decision/source_cards.py` (new) — per-source cards from configuration only (half, arms, members,
   cadence, time roles, live window/history/clocks, identity-joined fields) + the planned operations
   half; `llm.py` adds `payload["source_cards"]` only when set (`Capabilities.source_cards`).
3. `decision/fallback_plan.py` (new) — `deterministic_plan`: question on lexical/dense/segment +
   operations steps, `Decision.fallback = deterministic_plan`, failure kept, aggregate intents flagged
   unsupported, step cap respected, `None` when it would be empty; `eligible`: schema_violation,
   provider_error, timeout, invalid_step, no_plan, low_confidence and `raw_all_arms`; never for
   unresolvable values, budgets or caller-typed aggregates/scopes.
4. `decision/log_intents.py` — unified intents: `detect_operations`, `has_symptom`,
   `asks_search_analytics`, `asks_knowledge`, `asks_failure_detail`, `hour_span` (existing detectors
   unchanged). `decision/values.py` — `CustomerGrounding`, `ground_customers` (separate functions).
5. `engine.py` — the agent wrap in `_decide`, `identity_view()`, `link_view()`, `links_for()` (a link
   is shown only when the caller can read the other record), `trace.source_coverage` on search and
   answer results, coverage gaps appended to `AnswerResult.limitations`, `operations_coverage` on log
   aggregates (window, day tiers; excluded from the generator payload).
6. Config: `crosssource.planner` {enabled false, knowledge_sources, customer_namespaces,
   tenant_namespace, fallback, max_operations_calls}; `crosssource.links.{store_path, sha256}` (both or
   neither; path resolved against the config file); `docs/config.schema.json` regenerated.
7. Legacy QA: `DatasetProfile.unified {enabled: false, config}` (staged cutover; a switched dataset
   refuses an engine without the planner); envelope `coverage` + per-citation `source`, `links`
   (display fields; hidden records dropped via the profile rule), `link_status` (`reviewed` |
   `candidate`); `/health?dataset=` `view: unified`. Routes and existing keys unchanged.
8. Docs: ARCHITECTURE (M2b-2 section), CONFIGURATION (planner, link pin, profile switch; M2b-1/M3
   notes updated).

Acceptance on synthetic equivalents of the owner's 7 shapes (tests/contract/test_two_halves_engine.py,
fake providers): halted scheduled crawl -> `log_liveness` (1 halted, 1 running) + failure episodes +
tickets/docs; unauthorized despite correct credentials for a named customer -> customer grounded to
its matched tenant, `issue_history` (default 3-day view, vocabulary service) + earlier fix ticket/doc;
report stuck for hours -> liveness + queue episodes, "hours not isolated" stated; search ranking ->
knowledge half + "search analytics not served" limitation; user cannot log in -> auth episodes + ticket;
slow search / outage -> `issue_history` over the default live view + tickets; failure "at 2" with
"did it recover" -> both readings (02:00/14:00 in the half-hour-offset question clock) stated,
recovery reading + that day's error episodes. Every answer carries per-source coverage. Regression:
8 QA100-shaped support questions on today's support configuration vs the same with the planner on:
byte-identical planner payload, identical decision, hits/scores, packed generator evidence, answer
and trace (only the additive `source_coverage` differs).

Deviations / rulings:
- **Operations half deterministic; the llm planner plans the knowledge half only** (it receives the
  source cards and the operations half; log intents are removed from its payload). Safer reading of
  "one planner + deterministic grounding" (§6.11); no model can widen or invent a log scope.
- **Knowledge-half steps carry no `source` scope** (§6.11 "steps carry source scopes"): the knowledge
  half is exactly today's plan, so QA100 non-regression holds by construction; per-source pools are
  the stratified arms' job (LA).
- Files outside the touch list (minimal, listed): `decision/two_halves.py` (new, LF);
  `decision/base.py` (unowned: `Decision.fallback` gains `deterministic_plan` with its validator;
  `Capabilities.source_cards`); `core/config.py` (L0: two path-resolution lines for
  `links.store_path`); `core/config_crosssource.py` also got `crosssource.planner` (the engine side of
  the switch needs a key; v3 only); `runtime/collection.py` (L0->LA->LB; LB is editing it
  concurrently): `unserved_blocks` no longer refuses `crosssource` (one `if` removed + docstrings) —
  expect a trivial merge conflict.
- `serving/{http,mcp,parity}.py` unchanged: `AnswerResult.trace` already reaches every transport;
  `tests/parity/test_two_halves_parity.py` asserts the coverage is equal on Python, stream, HTTP, SSE.
- The contract engine is a **single-member** v3 configuration with `logs` whose generation holds the
  archive's live window and the support documents (stand-in for LB's log member; partitioned + `logs`
  is still refused). The fixture renames the ticket `status` to `ticket_status`: the log schema owns
  `status` as an enum. Real merged schemas will hit the same field-name collisions (`status`,
  `service`): LA/LB to settle per-member field maps before staging.
- `planner.enabled` without log intents is allowed (staged cutover step 1): no operations half,
  coverage `logs: not_served`.
- Analytics: limitation only (no analytics intent before M7).
- Liveness questions get extra failure episodes (symptom or "what was the error") — beyond the plan
  text, read-only, stated.
- The real dataset id is not written in the repository (release-identity denylist); docs/tests use
  `support-qa`.
- `docs/VALIDATION.md` not edited (L0-owned, §10.7.4); entry text below.

Tests: full `-m 'not external'` 4,712 passed, 45 skipped (before, same venv on a `git archive` of `a44185a`: 4,658
passed, 46 skipped; +54 new tests, one removed refusal case, one baseline-skipped test runs here, not investigated). New: `tests/contract/test_two_halves_engine.py` 17, `tests/unit/test_source_cards.py`
4, `tests/unit/test_fallback_plan.py` 14, `tests/unit/test_customer_grounding.py` 4,
`tests/parity/test_two_halves_parity.py` 1, `tests/unit/test_log_intents_detect.py` +11,
`tests/unit/test_legacy_qa_units.py` +3; helper `tests/two_halves_support.py`. Changed:
`test_collection_seams.py` (crosssource now served: the refusal test became a served test; doctor
expects the fts5 refusal), `test_config_v3_schema.py` (crosssource case removed from the refused
blocks). Ruff clean. Schema regenerated.

Staging steps (no paid call until step 6; nothing live touched before step 8):
1. Wait for LB's log-member wiring (partitioned + `logs`), rebase/merge; build the wheel into a new
   staging venv (live venv untouched).
2. Write `unified.yaml` (schema v3): adopted support member (M1b: `collection adopt --member support`,
   `collection layers`, `collection compose`), the log member and `logs` block from the log
   deployment (intents, archive, `default_view_days: 3`), `question.timezone` (owner's clock),
   `question.vocabulary` file pinned by sha256 (reviewed terms: crawl, MCP, login, report, search,
   symptom words), `crosssource.identity` (M2b-1 registry + sha256), `crosssource.links`
   (`review`, `store_path` + `sha256` of the M3 export), `crosssource.planner {enabled: true,
   fallback: deterministic_plan}`, `budgets.plan_check: warn`, `serving.answers.max_usd_per_day: 5.00`.
   Resolve member field-name collisions (`status`, `service`) first.
3. `config validate`, `doctor` (plan-check W vs `max_usd`), `collection describe`; check
   `source_coverage` on a few $0 searches (`search`, no generator call).
4. $0 equivalence (M1b harness): QA100 replay of recorded planner/router/SQL outputs, today vs unified
   with the planner on: identical payload, plan, per-arm candidates and packed evidence expected for
   every question without an operations intent; list the QA100 questions that do get an operations
   half (each one is a reviewed change).
5. $0 dry run of the owner's 7 questions on staging with `search` (no generator): record each plan's
   halves, intents/args, coverage; coordinator checks against the frozen rubric before any spend.
6. Paid evaluation (below), staging port only, `unified.enabled: true` in a staging copy of the
   legacy profile.
7. Report to the owner (per-half grades, flips, cost, p95).
8. Owner-approved window: flip `unified.enabled` in the live profile, restart; rollback = flag off (or
   the previous profile + venv) and restart; keep the separate log UI until O7.

Paid evaluation plan (M2b line of the ~US$45 pool, §10 O1: ≤ US$13 + identity review ≤ US$1 already
separate):
- Owner set (7 questions, graded per §11.2): rubric and gold frozen by sha256 before the run; one
  unified run via the legacy route (Sonnet 5.5 planner, Haiku 4.5 generator as live) ≈ 7 × W
  (≤ US$0.17 worst case) ≈ US$1.2 worst; Sonnet 5.5 judge per half (14 judgements, blind to build) ≈
  US$0.5; coordinator adjudicates every half with evidence; disagreements listed. Cap US$2.
- QA100 with log members present: same harness and Haiku 4.5 judge as the live baseline; 100 answers
  at mean ≤ US$0.08 (planner + generator; questions with recorded planner outputs replayed at $0) ≈
  US$8 worst; judge ≈ US$0.5; every correct->not-correct flip adjudicated by the coordinator; second
  judge (Sonnet 5.5) on all flips + a random 20 ≈ US$1. Cap US$10.
- Contingency US$1 (one re-run of a failed batch). Total cap US$13, enforced by per-run `--max-usd`
  and the answer ledger; stop on the first 429/502 burst and report.
- Gates (§10.5): ≥ 6/7 operations halves correct or limitation-correct, knowledge halves not worse than
  the live baseline, zero 429/502; QA100 zero unadjudicated correct->wrong flips; p95 ≤ 35 s; mean
  cost ≤ US$0.12/answer.

VALIDATION entry for L0: M2b-2 — two-halves planner, source cards, labelled deterministic fallback,
registry-grounded customers and per-source coverage verified on synthetic fixtures with fake
providers (7 synthetic owner-question shapes answered with both halves and coverage; support
questions without an operations intent: byte-identical planner payload, plan, evidence and answer
with the planner on vs off). No real-data or paid evaluation yet (staging steps above).

Follow-ups: LB log member in the partitioned collection (then rerun the contract on a partitioned
fixture); member field-name collisions; source-scoped knowledge steps if QA100 shows log documents
crowding support evidence; an analytics intent (M7); M10 UI badges/MCP discovery of coverage; real
vocabulary file review for the owner's terms.

## M2a wiring — live log member served by the partitioned collection (lane LB) — 2026-11-09 (worktree branch rebased onto `88d5928`, commits `ebd4a85`, `0f15ff1`)

Plan: docs/plans/unified_merge.md rev 3.3 §6.5–§6.7 (M2a "wiring after LA"). Evidence: synthetic
only (F0 fixtures; F0 600k-chunk generated day in `~/su_mutliarm_tests/claude/lb-wire/`; hash
embedder); no service, no model, no paid call.

Done (items 1–7 of the task):
1. Request pin: `PartitionedWritePath.pin` → `ingestion/log_collection.LiveLogs.pin` reads each live
   ledger's vector once (`vector_for`: archived days dropped) and returns a **request generation id**
   (sha256 of generation + vector) that every facet resolves to the `PinnedView` with slices; slices
   held by slice pins (lease = deadline + 60 s, one registry write per slice per lease, `release`,
   session `hold`). `PartitionedView.pin/release` expose the same.
2. Lexical: `backends/sqlite/log_units.LogStratum` → `QueryTimeScorer` (served-version masks;
   scope/fields/phrase/boolean as candidate eligibility), fused with other strata by `fuse_strata`
   under `source_quotas`; the support stratum's code path is untouched (B4 gate tested).
3. Dense: `DenseLogScorer.rank_vector` (exact, merged by score); pending units → `coverage.dense:
   pending`, `slices[].dense`, dense arm report `dense_pending`, `stats`.
4. Id directory: ledger registers with `member=` and `replaces=` holders (compacted slices, other-day
   slice, archive day member of a continuing id — also between close and release); gc purges;
   compaction no longer calls `supersede` before `register` (it failed on the real directory).
5. Close: `LiveLogs.close_day` = `close_day` → `register_closed` (day replaces the day's slices; catalog
   unit swapped retire+purge+register, restored on failure) → one compose → `release_day`; ledger
   `catalog` = shas of the active generation's catalog member.
6. Ledger built from config (`member_recipe` = partition recipe + bm25 options, embed_fields,
   catalog_kinds, slices, metadata schema, directory, pins); `live_refusals` (one source, `key:
   day`, `query_time`); write-path build of the live member refused; `logs` refusal text updated.
7. Dense cold start: `TextVectorStore.get_unique` (one lookup per distinct text) + `on_registered`
   warm hook for hourly/base units. First search after a base: max 0.86 s (M2a dense max 7.1 s).

Measured (VALIDATION entry): compose 0.007–0.018 s; warm live-window engine search p95 0.073 s;
first after new slice p95 1.00 s (+0.93 s); fresh-process first search 5.8 s (**gap** vs ≤ 3 s).

Deviations / rulings:
- **Request generation id** instead of an engine change (engine.py is LF's): `GenerationId.generation`
  carries the request pin; manifest lookups by that id in engine log helpers return `{}` (logs block
  is refused on partitioned collections anyway).
- **Units carry `catalog.sqlite`** (documents, chunks, document fields, records) so catalog/records
  reuse the sqlite SQL; costs ~13 s more per 185k-document full seal/base and disk. Archive members
  live in the member registry (`members/<m>/<key>/<partition16>/` + unsealed `member.json`;
  `handoff.REGISTRY_FILE` excluded from the seal); `MemberRegistry.verify` checks log units at the
  seal level (bytes verified at seal/receive) so compose stays O(members).
- **Embed template**: `engine_template` spaces embed `title\nfield: text` like the write path (one
  space across members); `test_log_members` month test updated for the composed text.
- **Served rows** in the log stratum are decided by sealed versions, not the id directory; `get` routes
  log ids by the stratum's owner map, others through the directory.
- Touched LA files (needed by the wiring): `ingestion/members.py` (`live`, store_dir/verify for log
  units, refuse build), `ingestion/compose.py` (log-unit manifest, `pin`), `backends/sqlite/members.py`.
- **Clashing field definitions (coordinator question, M2b-2 `status`/`service`)**: no per-member
  namespacing in this wiring. Rule: the collection `metadata_schema` is the single authority for a
  field name across members; every slice/archive record is validated against it at seal and refused
  on violation (`SliceRefused`, test `test_a_record_violating_the_collection_schema_is_refused_at_seal`),
  never coerced. A clash is resolved at declaration: declare the field with the type all members
  satisfy (e.g. `status` as `string`, the log enum kept as a reviewed value list in vocabulary), or the
  producer renames its field (`log_status`, `log_service`) via `field_map`. Member-scoped overlays
  (per-member enums) are a follow-up for M4 (one SQL surface), not done here.
- Session pins are an API (`LiveLogs.hold`) not wired into sessions (engine/serving are LF's).

Tests: full `-m 'not external'` on the rebased tree 4,750 passed, 45 skipped. Before (same venv,
`a44185a`): 4,658 passed, 46 skipped; my branch before the rebase: 4,696 passed, 45 skipped
(+38 new tests). New: `tests/contract/test_live_log_engine.py` 21, `tests/unit/test_live_log_member.py`
17; helpers `tests/live_log_support.py`; `FakeDirectory` follows the real directory's rules.
Ruff clean. No `Config` change (schema regenerated externally: identical).

Follow-ups: cold-process first search (5.8 s) — pre-open pinned units at startup; wire session holds
and `release` into serving (LF); month-member scheduler (M9); slice typed projection rows (M4);
per-member field overlays (M4); real-data run and staging week (§10.4).

## M2a wiring follow-up — start-up warm-up, `/ready` gating, session slice holds — 2026-11-09 (base `c894999`, commit `70e1cc4`)

Evidence: synthetic only (F0 fixtures, fakes; benchmark on the F0 600k-chunk generated day in
`~/su_mutliarm_tests/claude/warmup/bench/full3.json`); no service, no paid call.

Done:
1. Profiled the fresh-process first search (4.7–5.8 s): log-unit dense matrices ~4.6 s, unit readers
   + served-row masks ~1.4 s (parallel lexical thread); catalog.sqlite connections and the id
   directory negligible.
2. `Engine.warmup()` (engine.py): request pin, then `warm(gen)` of each distinct backend the catalog
   and lexical/dense arms read (layers skipped): `PartitionedSqliteBackend.warm` (open every pinned
   unit; write-path member stores `SqliteBackend.warm`; `LogStratum.warm_view` per live member),
   `SqliteBackend.warm` (bm25 segment; vectors: row map + faiss index or exact matrix paged in), the
   answer spend scope. `IndexNotReady` → `skipped`; other errors propagate.
3. `runtime/warmup.WarmUp`: not_run/running/done/failed/timed_out (+`late`), problem text names the
   error class and adapter.operation only (message logged). Legacy adapter: `warm` = embedder +
   `Engine.warmup`, profile `warmup_timeout_seconds` (default 300); `/ready` problem on anything but
   done. v2: `create_app(warmup=, warmup_timeout_seconds=)`, `serve` on by default
   (`--no-warmup`, `--warmup-timeout`); `/ready` body `warmup`, `reason` = the problem.
4. Session holds: `Engine._begin_pinned` → `CollectionView.hold_session(session, request gen,
   sessions.ttl_seconds + budgets.deadline_seconds)`; `LiveLogs.hold` keeps the union of the
   session's turns' slices while its lease is active; `Engine.close_session` →
   `release_session`. Single-member view: no-ops.
5. Bench: `scripts/bench_live_collection.py` measures fresh engines with/without warm-up and fresh
   processes (`--probe`).

Measured: fresh-process first search 5.55 s / 3.54 s without warm-up → **0.050 s** after warm-up
(warm-up 2.9–3.2 s before `/ready`); target ≤ 3 s met for the first search.

Deviations / rulings:
- **Timeout semantics**: a warm-up past its bound is not cancelled (no safe interruption of numpy/
  sqlite loads); it is reported `timed_out` (not ready) and turns `done` + `late` when it ends. A
  failure is never retried and keeps `/ready` 503 until restart (fail closed).
- v2 `create_app` default `warmup=False` (existing embedders/tests unchanged); the `serve` CLI turns
  it on. No `Config` change (schema identical); the bound is a legacy profile field / CLI flag.
- Request leases are not released at request end (concurrent requests share one request id; the
  lease lapses after deadline + 60 s as before). Expired sessions' holds lapse with their lease (the
  legacy sweep does not call `release_session`).

Tests: 4,765 passed, 45 skipped (before: 4,750 passed, 45 skipped; +15). Ruff clean.

Follow-ups: warm units compacted by another process (watch the ledger's registrations in the
serving process); cold-disk warm-up timing on the target host; real-data run (§10.4).

## Staging fixes, planner / coverage / serving side — 2026-11-10 (base `58d930d`, commit `2e9e01b`)

Scope: findings F1, F2, F4, F6, F7 and the fallback label of the private real-data staging run
(2026-11-09). B1/B2/B3/F3/F5 (backends, projection, sql_surface, ingestion, arms) belong to the
concurrent data/retrieval lane and were not touched.

Done:
1. **Source-scoped knowledge half (F2).** `two_halves.scoped_arms` / `TwoHalvesPlanner.knowledge_predicate`;
   one hook in `Engine._run_step` adds `source in knowledge_sources` to the executed scope of every
   knowledge-half step (lexical, dense, structured; graph/community/segment unless only
   knowledge-source layers serve them) when the collection holds a non-knowledge source. Plan,
   planner payload and step trace unchanged; the partitioned backend's unit pruning keeps the log
   units closed. New key `crosssource.planner.scope_knowledge_steps` (default true; schema regenerated).
2. **Coverage by sources read (F1, F2, F7).** `two_halves.step_half`; the `logs` entry carries
   `candidates`/`evidence`, pinned `members` and live `slices`; statuses `not_served` (no log member,
   no log record in the hits), `intents_not_served`, `records_only`; top-level `knowledge_scope`.
3. **Fallback plan.** Kept; labelled additionally in the legacy `plan.fallback`; tests that planned
   questions are identical under `fallback: deterministic_plan` and `abstain`.
4. **Declared fields (F4).** `links/builders.collection_fields` (field_map.customer_ref under
   identity_namespace; time_roles.<kind>.event_time or field_map.event_time), used by
   `LinkSettings.from_config(collection=)` in `links build` and by `identity candidates` when
   `--field` is omitted; build report gains `fields`.
5. **Unsent calls (F6).** `providers/base.NOT_SENT` (ConnectError, ConnectTimeout, ProxyError) →
   `ProviderError.sent = False`; `RunBudgetMeter` commits the call slot but releases the
   `serving.answers` reservation (`AnswerSpend.release`); trace `model_call.sent: false`.
6. **`identity describe` (F7)** reports `served`, `joins_enabled`, `consumers`, `registry_version`,
   `joinable_aliases` or `reason`.
7. **Legacy label (F7).** `DatasetProfile.source_label`: `logs` → `Logs` on a unified-view dataset
   unless `content_source` names it.

Deviations / rulings:
- The scope is an **execution-time** predicate, not a plan rewrite, so plans stay byte-identical
  with and without a log member (the task's verification requirement); it is visible in
  `source_coverage.knowledge_scope` and a `knowledge_scope` trace event.
- `exact_id` is not scoped (a named identifier resolves where it lives); a caller's `source` filter
  wins over the scope. A planner step filter naming `source: logs` is intersected (yields nothing
  from the knowledge half): the operations half alone reads logs.
- Unsent calls still count against the per-request call cap (an attempt), not against USD/tokens.
  The learning scopes' `LearnerMeter` is unchanged (out of scope; follow-up).
- `tests/unit/test_collection_seams.py::test_identity_describe_checks_the_registry_digest` now
  asserts the new describe keys (same edge cases: missing / mismatch / match-but-invalid / v2).

Tests: 4,798 passed, 45 skipped (baseline at `58d930d` run from a `git archive` copy: 4,764 passed,
46 skipped; +33 new: `tests/contract/test_knowledge_scope_engine.py` 15,
`tests/contract/test_planner_fallback_label.py` 7, `tests/unit/test_staging_planner_fixes.py` 11).
Ruff clean. Evidence: synthetic / mocked.

Follow-ups: re-run the private staging ($0 replay + owner questions) after the data/retrieval lane
lands B1–B3 to confirm the 60/100 log-flooded questions are gone and the support stratum is
identical on real data; `LearnerMeter` unsent-call release; an adopted store whose documents carry
a `source` not declared in `member.sources` is refused at adoption, so the scope cannot drop them.

## Staging fixes, data/retrieval side (B1, M4 completion, B2, B3, F3, F5) — 2026-11-10 (rebased onto `d143c6b`, commit `50ef433`)

Plan: docs/plans/unified_merge.md rev 3.3 (§6.2, §6.7, §6.8, §6.11); private staging report
(`unified-staging-20261110`) and its three synthetic repros (now passing). Evidence: synthetic only.

Done:
1. B1 — `runtime/collection.log_refusals` replaces the blanket `logs` refusal (refused: not exactly
   one live log member, `producer.source` ≠ `logs.source`, `logs.history.tier`).
   `runtime/member_logs.py`: the `logs` manifest section of a request pin and `MemberHistory`
   (per-request typed history tables from the pinned `incident`/`summary_coverage` records, read by
   the unchanged `TieredAggregate`). `issue_history`, `log_liveness`, `incident_episodes` (stitching)
   branch on `history.store: collection_member`. `PartitionedRecords.execute`: a member lacking a
   positive predicate's field contributes population only; applied/unsupported per collection schema.
2. M4 completion — `sql_surface/members.py`: the projection member's catalog/grounding/guard/
   single-file execution unchanged (values scoped to the projection member via
   `PartitionedProjection.projection_sources`, also in the IR resolver); with a live log member and an
   all-rows principal: `issue_day`, `coverage_day` (per-unit caches → one served-rows file per request
   pin), `customer_<namespace>` views from the pinned registry, declared join paths,
   `join_policy: cross_source` (guard enforces only when a non-projection table is read); setup errors
   → labelled `projection_unavailable`. `config_projection` no longer refuses logs + projection on a
   partitioned collection; `partitioned_refusals` allows exactly one non-log projection member.
3. B2 — `PartitionedRecords.values`/`exists` skip members whose own sealed schema lacks the field
   (`SqliteRecords.schema`, `population`).
4. B3 — `members.ranked`: per-unit ranking pages `k` > 10,000.
5. F3 — `GraphArm.layer_scope` (+ `LayerBackend.covered_sources`): disjoint `source` → no graph
   evidence, `scope_outside_layer` report, trace event; other predicate scopes narrowed to covered sources.
6. F5 — `adopt.embedding_space` mapping + `ingestion/space_equivalence.py` + `collection verify-space`;
   compose accepts a mapped adopted member only with a passing recorded cosine sample.
7. Coordinator request: `test_a_streamed_call_records_first_token_seconds` uses a fake monotonic clock.

engine.py edits (minimal wiring, listed): `log_manifest` delegates to `member_logs` on a partitioned
view; `capabilities().log_history` is also true on a partitioned view with log intents.

Deviations / rulings:
- `logs.source` names a configured log source, not `Document.source` (log records carry `source:
  logs`); the member link is `producer.source` when set. `logs.collection.archive.enabled` is not
  required on a partitioned collection (the member is the archive); liveness still needs keyword files.
- Liveness "heartbeat-recorded" days come from `success_documents` on coverage records (log units
  record no per-day recipe identity), not from recipe identity equality.
- Per-unit attachments exceed SQLite's 10-database limit on real views, so the log rows of a pin are
  gathered into one file per request pin (kept: last 16). A projection attachment's view part may be
  unpinned (its sealed file is the pin); all other parts keep ≥ 1 pin.
- Literal filters on identity view columns are not grounded (refused); joins are the intended use.
  Tenant principals get the projection surface only (log member is operator-only).
- Existing test changed: `test_member_listing_values.py` refusal case renamed with the new message
  (same edge case: two non-log projection candidates).

Tests: full `-m 'not external'` 4,853 passed, 45 skipped (before at `58d930d`: 4,765 passed, 45
skipped; the rebase onto `d143c6b` brings the planner-side tests). New 55:
`test_staging_fixes.py` 18, `test_staging_log_member.py` 13, `test_staging_sql_surface.py` 17,
`test_space_equivalence.py` 7; helper `tests/staging_support.py`. Ruff clean. Schema regenerated.

Follow-ups: rerun the $0 staging (QA100 replay, owner 7) on this build; measure the per-pin SQL file
build and `MemberHistory` on real volumes; staging config needs `logs.collection.keywords.files` for
liveness and `adopt.embedding_space` + `collection verify-space` if `embedding_space.id` stays declared.

## Staging round 2 fixes, planner side (N1, N2, N5, N6, vocabulary) — 2026-11-10 (base `90d0440`, commit `bbf37aa`, branch `v2/staging-r2-planner`)

Plan: docs/plans/unified_merge.md rev 3.3 (§6.11, M2b-2 two-halves planner,
`crosssource.planner.scope_knowledge_steps`); private staging report round 2 (findings N1, N2, N5,
N6, N7). Evidence: synthetic / mocked only. Goal invariant: support-only questions get today's
plan/answer path on the unified collection.

Done:
1. **N1** — `decision/log_guard.py`: `log_sources(config)` (partitioned: sources of members that
   declare a log kind, are produced by a log host or have `cadence: minutes`; one store: `None`),
   `LogGuard` component, `log_scope_guard(spec, context=, log_sources=)` passes a scope whose
   positive `source` conditions (own + executed: ACL, request filters, knowledge scope) admit no
   log source. Used by `TwoHalvesPlanner._guard` (now its own loop with `knowledge_predicate`),
   the structured arm and `Engine.aggregate_in`.
2. **Value leak** (6 of the 34 N1 questions also had log-value qualifiers): `Capabilities.value_scope`
   set by `knowledge_capabilities` when knowledge steps are scoped; `rules`/`llm` resolvers and
   `check_qualifiers` add it to the value-listing ACL.
3. **N2** — `crosssource.planner.operations_terms` (new key, declared default list; whole-word,
   validated lower-case/unique) + `log_intents.operations_evidence` (terms or `OPS_SYMPTOM`) gate
   `TwoHalvesPlanner.operations` before today's detectors and the unified detector (trace
   `operations_gate`).
4. **N5** — `TwoHalvesRecord.replaced`; `Engine._fallback_replaced` / `_apply_typed_route`: a fallback
   that replaced an abstention on a question without operations steps is withdrawn when the executed
   typed route answers the aggregate (trace `two_halves_fallback_withdrawn`) → today's
   `typed_route_only` decision; operations refusals/notes, if any, stay stated.
5. **N6** — `runtime/plan_cost`: under `crosssource.planner.enabled` the planner catalog counts the
   source cards + `OPERATIONS_HALF_CHARS` (1,500) × (`2·max_operations_calls + 2`) instead of
   `planner_context_max_chars` (the two-halves planner never sends the logs planner context).
6. **Vocabulary (N7)** — no code change needed: `match: word` (default) is already whole-word; tests
   pin it and CONFIGURATION.md documents `prefix` as the opt-in that matches "reported".

Deviations / rulings:
- N6 as specified ("fit W under 0.25 with the existing config") is **not achievable from the planner
  context**: recomputed read-only on the private staging config, W of the pipeline with
  `review.max_hops: 2` goes 0.4174 → 0.4111 (default pipeline 0.2265 → 0.2202). The two review hops
  (≈ US$0.19) dominate; the staging report's attribution to the 24k-char context was wrong. `max_usd`
  not raised; the owner must lower `review.max_hops`/reviewer evidence for that pipeline or approve a
  cap change.
- "Only include logs planner context when the operations half is triggered": the code already never
  sent it under the two-halves planner (knowledge capabilities drop it; source cards are sent with
  an operations half); kept, and pinned by a test.
- The operations gate lists symptoms in code (`OPS_SYMPTOM`, the unified detector's list) and terms
  in config; a vocabulary term alone is not operations evidence (a subject, not an operations ask).
- The guard exemption is partitioned-only: on one store the `source` field is not trusted to
  separate log records.

Files: `src/su_multi_arm/{engine.py, arms/structured.py, core/config_crosssource.py,
decision/{base,llm,rules,log_guard,log_intents,two_halves}.py, runtime/plan_cost.py}`; docs
(CONFIGURATION, ARCHITECTURE, VALIDATION, config.schema.json). Shared files touched minimally:
`engine.py` (guard component, `aggregate_in` guard call, `_apply_typed_route`). Not touched: SQL
surface, log member table builders, `backends/sqlite/members.py`, `runtime/member_logs.py`.

Tests: `-m 'not external'` 4,896 passed, 45 skipped (before at `90d0440`: 4,853 passed, 45 skipped;
+43: `tests/unit/test_staging_r2_planner.py` 31, `tests/contract/test_staging_r2_planner_engine.py`
12 — 10 of the contract tests fail on `90d0440`). Ruff clean. Schema regenerated.

Follow-ups: coordinator $0 rerun of the private QA100 replay (target 45/45 recorded plans identical,
lineage excepted) and owner 7 on this build plus the data-side latency build; the planner payload on
the unified view still carries the common schema's log fields (recorded plans unaffected; a live
planner might plan differently); W of the review pipeline vs `max_usd` (owner decision).

## Staging round 2 latency fixes (N3, N4) — 2026-11-10 (base `90d0440`, branch `v2/staging-r2-latency`, commit `30fa518`)

Plan: docs/plans/unified_merge.md rev 3.3 (§6.7 caches keyed by unit / first request after a new
slice, §6.8 per-pin SQL file); private round-2 staging report (N3, N4) and its latency evidence.
Evidence: synthetic repository tests + a private $0 real-volume re-measure on a copy of the staging
state (search only; no model call left the process; nothing written outside the copy).

Done:
1. N3 — `runtime/member_logs.history_file`: the member's typed history tables (`episode_piece`,
   `issue_day`, `coverage_day` + `history_days`) are derived once per request pin from the pin's
   SQL-surface file (`sql_surface/members.view_file`) for every served day, with the archive
   builder's functions unchanged; per-day chained keys let a new pin copy unchanged days from an
   earlier pin's file (a new slice recomputes its own day and the days after it). `MemberHistory`
   keeps its API (`tiers`, `aggregate`, `subset`, `recorded_days`) and serves `TieredAggregate` from
   one read-only connection to the file. `MAX_DOCUMENTS` (200,000 per-request bound) removed.
2. `sql_surface/members.py`: cache `FORMAT` 2 (hidden `success_recorded` on coverage rows of the
   unit/view files, not exposed by the surface); `_prune` generalised to `prune(folder, keep, kind)`
   (view files and history files pruned separately, 16 each); `_identity_view` parses the identity
   export once per file state (stat + pinned digest key).
3. N4 — `backends/sqlite/members.PartitionedRecords`: per-unit `values` (limit-aware: a longer or
   complete cached list serves a smaller limit) and `exists` caches, LRU 65,536 entries; unscoped
   value lists persisted in `<registry>/sql/unit_values_v1.sqlite` (`UnitValuesFile`, keyed by unit
   key + field declaration digest; read/write errors counted and scanned; rows of units outside the
   latest pin seen are dropped). Counters: `collection.values_unit_scans`, `values_cache_hits`,
   `values_file_hits`, `exists_unit_reads`, `history_files_built/reused`, `history_days_built/reused`.

Real-volume re-measure (private copy, one warm process; before = `90d0440`):
`issue_history` 3 d 46.5 s → 0.009 s (cold pin incl. v2 unit caches: 13.2 s once); 1 tenant 46.4 s
→ 0.005 s; 10 d refused after 97.9 s → 0.02 s; first history request after a new live slice 2.2 s
(1 day recomputed, 9 copied); support-only QA search 12.0 s → 0.6 s median (847 → 0 log-unit value
scans; cold process first search 8.2 s); owner questions 43–58 s → 0.29–0.54 s; restarted process:
history 0.018 s, first support search 4.7 s with 0 log-unit scans, then 0.4 s. Renderings/aggregates
identical except the provisional `source` label and the SQL catalog fingerprint (cache file name).

Deviations / rulings:
- The plan's per-request history construction (staging fix B1) is replaced by a per-pin file; the
  served semantics are unchanged (tested against the previous construction as an oracle). The
  provisional `source` column is now the constant `provisional` (was a random in-memory db name).
- The values file is a cache written from the request path into the collection's own registry
  folder (as the SQL-surface view files already are); never into an adopted store.
- Old `v1_*.sqlite` unit/view caches are left on disk (not pruned: another process on the previous
  build may still read them); safe to delete once no `90d0440` process runs.

Shared files touched (minimal, not planner files): `backends/sqlite/members.py` (PartitionedRecords
only), `sql_surface/members.py`. No planner/guard/decision file touched. No `Config` change (schema
not regenerated).

Tests: full `-m 'not external'` 4,870 passed, 45 skipped (before: 4,853 passed, 45 skipped). New
17 in `tests/contract/test_staging_latency.py` (oracle equivalence 4 windows × 5 specs, no catalog
read on a reused pin, 10-day synthetic member with a new slice recomputing one day, values/exists
caching, values file restart/unreadable/retention, identity view parse-once). Ruff clean.

Follow-ups: make the per-pin view file incremental (≈ 1 s of the 2.2 s after a new slice; plan
target ≤ +1 s); profile the remaining ≈ 4.7 s first-search cost of a restarted process and the
≈ 0.4 s warm support search (qualifier regex compilation over larger value lists, typed-route SQL
catalog setup; planner-side files, other lane); re-run the full private QA100 replay and owner 7
on this build together with the planner/guard lane's fixes.

### 2026-11-10 — staging round 3 fix N9 (typed route qualifier values)

- Real-data staging round 3 on af273bb: 40/45 recorded QA100 plans identical. The 5 differences were
  the typed SQL route's qualifier check (`engine._routed_unapplied`, the route's executed-unapplied
  check and the typed router's resolver) reading log-member values ("full" -> `signature_id`,
  "Search" -> `category`, "exact" -> `row_exactness`), downgrading exact aggregates to bounded.
- Fix: `Engine._knowledge_value_scope()` adds the knowledge-source predicate (as
  `Capabilities.value_scope` does for the knowledge half) to those three resolvers; empty without
  the two-halves planner, so single-store behaviour is unchanged.
- Validation: synthetic contract test in `tests/contract/test_staging_r2_planner_engine.py`; full
  suite 4,914 passed, 45 skipped (m4g venv); ruff clean. Real-data $0 re-run pending.

### 2026-11-10 — staging round 4 fix N10 (declared enum values outside the knowledge scope)

- Real-data staging round 4 on 2213a78: 44/45 recorded QA100 plans identical (q038 float32 dense-score
  noise only). q037 differed: `qualifier_candidates` took the log-only enum `row_exactness`'s declared
  values, so "exact phrase" became an unapplied qualifier and an exact SQL count was labelled bounded.
- Fix: `ValueResolver(value_scoped=...)` marks resolvers carrying the knowledge value scope (knowledge
  half, rules/LLM planners, typed route); for those, declared enum values count as qualifiers only
  when `known_value` finds them in scope. Unscoped resolvers (single store) are unchanged.
- Validation: `tests/unit/test_staging_r4_enum_scope.py` (synthetic); full suite 4,917 passed,
  45 skipped (m4g venv); ruff clean. Real-data $0 re-run pending.

## Staging round 5 fixes: support-only inputs (N12, N13), rendered operations half (N14) — 2026-11-10 (base `faf2c66`, branch `v2/staging-r5-answers`)

Plan: docs/plans/unified_merge.md rev 3.3 §6.11 (two-halves planner, one answer contract); private
round-5 staging report (N12, N13, N14). Evidence: synthetic / mocked only; private paid outputs
read for diagnosis and offline calibration only (nothing copied, no model call, no service).

Root causes:
- **N13 (q078, one SQL call instead of two).** Not the step cap or the operations allowance: for a
  support-only question the knowledge half's planner payload listed every field of the unified
  schema (the log member's ≈ 90 fields: +7.4k input tokens on all 55 questions) and the SQL writer's
  catalog carried the log and identity views (+1.5k tokens). Today's second `sql_surface` call is
  the writer's repair round; on unified the writer, seeing a different catalog, returned
  `not_answerable` (run 1) or a different single statement (run 2). The router's arm text also
  differed (log-intent wording).
- **N12 (q048, count stripped).** Run 1: the generator input was identical lineage aside (2,740 vs
  2,749 tokens; both `typed_route_only`, same SQL row); run 2 differed through the planner's
  different plan (N13 cause: different hits, 19.5k vs 23.7k generator tokens). In both unified runs
  the count sentence sat in a paragraph without an inline `[aggregate:1]`, and `annotate` removes an
  unbacked count statement whose paragraph cites nothing; today's samples put the marker in the
  sentence and kept the count with a note. The contract's targets also came from the unified schema.

Done:
1. `PartitionedRecords.declared_fields(gen, sources)`; `TwoHalvesPlanner.knowledge_fields` /
   `knowledge_schema` (per generation cache) / `asks_operations`; `knowledge_capabilities` narrows
   the schema for every knowledge half.
2. `Engine._support_only` (gate), `TwoHalvesAgent.prepare` (deterministic operations half planned
   before the router and the planner; consumed by `decide`), `_knowledge_capabilities`,
   `_state_support_only`: a request without an operations step gets the knowledge schema for
   intents, planner, review and contract targets, today's router arm text, and a SQL writer session
   with `collection=False` (`route.run` → `open_session` → `sql_surface_parts`: projection catalog
   only; execution unchanged).
3. `AnswerContract._cited_number` (annotate): a count from a cited aggregate is annotated, not removed.
4. N14: `generation/operations.py` (`OperationsSection`, `check`, `compose`, `backing_of`,
   `section_labels`), `profiles/prompts.OPERATIONS_RULE` (domain-neutral), `Engine._operations_section`
   / `_with_operations`, `_generate(operations=)`; new `ContractRule` values
   `operations_claim_unbacked`, `records_presented_as_current`, `operations_restated`.

Deviations / rulings:
- "Missing operations rendering → insert/repair": the section is always composed first rather than
  inserted only when missing, and the operations aggregates are withheld from the generator's
  evidence (it gets `operations_shown` without ids). Flagged sentences are removed, never reworded.
- "Current logs:" / "Past tickets/docs:" labels are derived from the configured source names
  (no new config key, no prompt domain words).
- The annotate change also applies to single-store answers (it removes a dependence on where the
  model puts its inline marker); strip/abstain modes are unchanged.
- Halt/recovery claims are backed only by liveness rows (`halted` / `recovered`); outage and cause
  are never established by the current typed log intents, so such claims are always removed.

Files: `src/su_multi_arm/{engine.py, models.py, backends/sqlite/members.py, decision/two_halves.py,
generation/{contract,operations}.py, profiles/prompts.py, sql_surface/{executor,planner,route}.py}`;
tests `tests/contract/test_staging_r5_{support_parity,operations_answer}.py`,
`tests/unit/test_{operations_answer,staging_r5_contract}.py`; docs ARCHITECTURE, CONFIGURATION,
VALIDATION. Config unchanged (schema identical).

Tests: `-m 'not external'` 4,965 passed, 45 skipped (before at `faf2c66`: 4,916 passed, 46 skipped;
48 new: parity 6, operations answer 7, operations unit 32, contract 3). The parity and contract tests
fail on `faf2c66` (4 of 6 / 1 of 3). Ruff clean.

Follow-ups: coordinator $0 re-check (45/45 recorded QA100, owner 7 search) on this build; paid:
owner set generator + judge re-grade (≈ US$0.7) and the 55 no-plan questions on both sides; check
whether the operations detector should give "most failed tickets"-shaped support counts an
operations half; consider rendering the knowledge part's streamed deltas after the section.

## Staging round 6 polish: operations reading line, generator schema repair (N15) — 2026-11-10 (base `0061c34`, branch `v2/staging-r6-polish`, commit `382b786`)

Plan: private round-6 staging report caveats 1 and 2. Evidence: synthetic / mocked only (no model
call, no service; private report read only).

Done:
1. `generation/operations.header_line` + `OperationsSection.header`; `Engine._operations_section`
   passes `question.timezone` and `logs.timezone`. Line: as-of (latest provisional as-of /
   liveness `observed_until`, else latest window end "closed days only"), distinct windows,
   clock(s), union of bound `service` args ("all (no service filter)" if unbound), bound `tenant`.
2. `pipeline.generation.schema_repair_attempts: Literal[0, 1] = 0`; `Engine._repair_generator_reply`;
   `providers.accounting.RetryBudgetMeter`; `profiles/prompts.GENERATOR_REPAIR_PROMPT`;
   `runtime/plan_cost.py` repair slot. Trace `generator_schema_repair` (started / schema_valid /
   failed / skipped).

Deviations / rulings:
- **Opt-in, default 0** (the task said "add one bounded retry"): kept consistent with M0
  ("nothing is retried by default") and the planner's `decision.schema_repair_attempts`; existing
  configs' behaviour and plan_check `W` are unchanged. The staging config needs
  `pipeline.generation.schema_repair_attempts: 1`.
- The M0 `retry_transient` loop could not be reused directly: validation happens after the
  provider exchange returns, outside `_exchange`. Reused instead: its accounting (separate metered
  call, `attempt`/`retry_reason` in the call record, a retry the budget/deadline refuses leaves the
  original failure standing) and the planner's repair payload shape (`schema_repair`, 64,000-char cap).
- A provider error on the repair also keeps the original schema-violation answer (rather than
  failing the request), per "if the retry fails, keep today's failure behaviour".
- The repair is not streamed (deltas of the failed reply may already have been sent; as M0).
- No reading line when every operations call was refused (nothing was read).

Files: `src/su_multi_arm/{engine.py, core/config.py, generation/operations.py, profiles/prompts.py,
providers/accounting.py, runtime/plan_cost.py}`; `tests/contract/test_staging_r6_polish.py`;
docs ARCHITECTURE, CONFIGURATION, VALIDATION, config.schema.json.

Tests: `-m 'not external'` 4,984 passed, 45 skipped (before 4,966 / 45; 18 new). Ruff clean.

Follow-ups: set `schema_repair_attempts: 1` in the staging config; paid owner-set re-run to see
whether the judge's O-b/O-c objections clear and how often the repair succeeds.

## Outage/incident dataset as a unified member (write-path member settings) — 2026-11-10 (base `2965ec8`, branch `v2/outage-member`)

Plan: `docs/plans/unified_merge.md` §8 (D1–D11) for a new dataset joining the same arms; private
merge plan in the external workspace (owner request 2026-11-10). Evidence: synthetic (repo tests)
plus a private side-by-side staging build (see `docs/VALIDATION.md`).

Done:
1. `CollectionMember.vectors: import | online` (write-path members): `online` embeds the member's
   chunks with the embedder role's model and template and never consults the precomputed import
   (whose `on_missing: error` refused the new text); the member stays in the collection's space.
2. `CollectionMember.metadata_fields`: validated against `metadata_schema` (Config) and `time_roles`;
   the member store is sealed with the restricted schema (`members.member_schema`), so
   `declared_fields` of the knowledge half carries the member's own fields and never the log
   member's (N13 stays closed); undeclared metadata keys are refused at register; no typed
   projection is built and only matching `ingestion.attach` entries are kept;
   `runtime/collection.partitioned_refusals` no longer counts such a member against the
   one-projection rule.
3. A write-path member's own `embed_fields` now apply to its build (before: declared but ignored
   outside the log path).
4. `CollectionMember.build_recipe` → `partition_recipe(...).content.member_build` (write-path members
   with settings only). Log-host / live log members' recipes are unchanged (their slice ledger is
   keyed by the recipe: an early version that recorded the log member's `embed_fields` was refused
   by the staging slice ledger and is covered by a test now).
5. Synthetic fixture `tests/fixtures/onboarding/incident/` + `tests/unit/test_incident_member.py`
   (10 tests); `unified_merge.md` §8.3b worked example; CONFIGURATION; config.schema.json.
6. Owner rule change (2026-11-10, via the coordinator): fuzzy client matches count as matched.
   `crosssource.identity.auto_match: exact | fuzzy` + `fuzzy_min_confidence` (default `exact`, 0.8);
   `IdentityView.from_config` / `pinned(..., auto_match, min_confidence)` also joins `candidate`
   aliases resolved to one customer at or above the threshold (rule `fuzzy:<rule>`, listed in
   `IdentityView.promoted` and `identity describe`); excluded/unmatched/contested/ambiguous never
   join; the registry file is untouched. Engine, SQL identity views, `identity describe` and
   `links` read the option. Plan §6.9 text, CONFIGURATION, schema; 1 new test in
   `test_identity_registry.py`.

Deviations / rulings:
- No planner/two-halves code change was needed: `crosssource.planner.knowledge_sources` already
  accepts any source and the scoped arms read `source in knowledge_sources`.
- D6 (SQL catalog for outage kinds) and D7 (graph node types) deferred: a write-path member carries
  no typed projection (several projections need the multi-source SQL setup), and the graph arm reads
  the attached support graph only. Counts/lists over the new kinds go through the structured arm.
- Fuzzy matching is applied at the pinned view, not in `derive`: the registry stays append-only
  and its digest (the pin) unchanged; a deterministic environment-suffix candidate (0.6) stays
  below the default 0.8 and joins only through a coordinator decision (as in staging).
- Activation finding: adding a member changes the collection recipe, so a running server whose
  configuration lacks the member refuses a generation composed with it. Staging therefore composed
  in a copy of the store; :8160 was not touched.

Tests: `-m 'not external'` (m4g venv) 4,998 passed, 45 skipped (before at `2965ec8`: 4,987 / 45;
11 new: 10 in `test_incident_member.py`, 1 in `test_identity_registry.py`; one existing test's
monkeypatch of `IdentityView.pinned` now forwards keyword arguments). Ruff clean;
`docs/config.schema.json` regenerated.

Staging (private, side by side; see VALIDATION): member built and composed in a copy of the owner
test store (generation `da262952`), served on a second loopback port from `src_60a06d8`; paid
smoke 8+2 questions ≈ US$0.76. Follow-ups: D6 typed projection for the new kinds (the SQL route
answered "which outages had no RCA" from support tickets); check why a customer-named root-cause
question dropped the new records (customer filter on knowledge steps?); D7 graph node types;
identity namespace for the monitor names in the registry; frozen member eval set + owner-set
non-regression; the owner switches the test server to the new cfg/store.
