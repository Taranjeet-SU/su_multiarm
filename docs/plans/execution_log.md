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
