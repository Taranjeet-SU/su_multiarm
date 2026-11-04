# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[PEP 440](https://peps.python.org/pep-0440/) version numbers. Evidence for each release (synthetic,
mocked or real-service) is recorded in [docs/VALIDATION.md](docs/VALIDATION.md).

## [Unreleased]

### Log features

- **LLM pattern learning** (addendum A1, slice L4b). `su-multi-arm patterns learn` runs one loop:
  mine the unparsed lines, ask `models.roles.pattern_learner` (Anthropic, OpenAI or
  `openai_compatible`, structured output) for one proposal per cluster, gate it deterministically,
  write it under `extraction.pattern_library.promotion`, and report coverage before and after.
  `patterns propose` writes proposals to `proposed.yaml` only. `--dry-run` sends, spends and writes
  nothing; `--max-usd` lowers the run cap.
  - Egress policy (`learning.patterns.egress`): only lines processed by the configured `privacy`
    redaction leave the process, a residual e-mail/IP/token shape refuses the run before any call,
    `pseudonymise` attributes become per-run aliases, and a hosted role needs `allow_hosted: true`.
  - Spend caps per run and per calendar day (`max_calls_*`, `max_usd_*`), reserved before every call;
    the day caps are kept in a SQLite spend ledger. No retries.
  - The gate gains `cluster_coverage`, `out_of_cluster` (absolute over-breadth bound, default 0)
    and `fix_target`. A passing fix of a learned pattern is written as `supersede`.
  - Human decisions `patterns promote|deprecate|disable <id>` (CLI only, recorded in
    `decisions.yaml`). A human disable or edit always wins; the system never writes `library.yaml`.
  - Optional git commit of the system files (`learning.patterns.git_commit`), refused while a system
    file has uncommitted hand edits.
  - Evidence: synthetic lines with a scripted fake provider only (docs/VALIDATION.md).
- **Index-name grammar** (`sources.<name>.index_name`). Optional `prefix`, optional
  `deployment_prefixes` (`{n}` = digits; recorded as the `deployment` attribute) and vocabularies for
  any segment. A name that does not fit exactly once is `unparsed` with a typed `reason`.
- **Configuration.** New keys `learning.patterns`, `models.roles.pattern_learner`,
  `extraction.pattern_library.gate.min_cluster_coverage` / `max_out_of_cluster`, and the
  `index_name` grammar keys; the config schema is regenerated.

### Changed

- **Index-name parsing no longer guesses.** A segment without a vocabulary is exactly one token, so a
  multi-token tenant (previously "the first prefix field keeps extra separators") now needs a tenant
  vocabulary, and a name with an undeclared left-side prefix is `unparsed` instead of being read
  into the tenant.

### Added

- **Read-only log sources (`sources:`; addendum A1 slices L0 and L1).**
  - Component kind `source` and the `elasticsearch_rest` adapter (Elasticsearch ≥ 7.9, OpenSearch
    ≥ 1.0) behind an exact allow-list transport, which refuses every non-listed request before any
    I/O.
  - A `_has_privileges` read-only self-check (`require_read_only_role`).
  - Discovery with the `_alias` fallback, and per-index count queries built from the mapping
    (basis `level_field` or `token_match`).
  - Scroll and `search_after` pulls with close hygiene and stratified caps (`hourly_quota`,
    `random_score`).
  - Index-name parsing by service vocabulary, longest suffix first.
  - `LogRecord` mapping and multiline assembly.
  - `su-multi-arm sources check|discover|count|probe`.
  - Synthetic fixtures: a deterministic log generator, a fake cluster and a ReDoS corpus.
  - Evidence: synthetic and contract-mocked only (docs/VALIDATION.md).

- **`pattern` extractor** (addendum A1, slice L2). Set `extraction.extractors: [pattern, …]` with
  `extraction.pattern_library`. It has:
  - an explicit `re2` engine (extra `patterns`) or the `regex` development engine (extra
    `patterns-lite`), with no fallback between them;
  - an RE2-subset validator and ReDoS lint;
  - a four-file library: human `library.yaml`, plus system-owned and append-only `learned.yaml`,
    `proposed.yaml` and `decisions.yaml`;
  - a loader with owner precedence and decisions replay (a human `disable` is final);
  - an RE2 `Set` prefilter;
  - line-scoped facts, header-scoped `native_link` attribute links, and residue composition when listed
    first;
  - a CAS-safe writer for the system files;
  - the read-only CLI `patterns lint|coverage|mine`.
- **Pattern provenance.** Generation manifests record `pattern_provenance` and a content-addressed
  library snapshot. Extraction reuse keys of the `pattern` extractor carry a salt derived from the
  library, so library changes re-extract while index fingerprints stay unchanged. Other extractors keep
  their keys.
- **Pattern learning, deterministic part** (slice L4a). It adds:
  - a Drain-style template miner with bounded memory;
  - a regex synthesiser;
  - bootstrap coverage without an LLM;
  - the validation gate;
  - `pattern_library.promotion: auto | manual` (default `auto`).
- **Configuration.** New keys `extraction.pattern_library` and `extraction.extractors: pattern`; the
  config schema is regenerated. Configs that do not select `pattern` keep their index fingerprint.
- **Daily log partitions and compose (`logs:`; addendum A1 slice L3a).**
  - A partition store per source and day (`ingestion/partitions.py`): resumable atomic units, sealed
    immutable versions, `reparse`/`repull` as `v+1`, tombstones.
  - Partition-stage parsing with an optional spawn process pool, signature and unit aggregates with
    row exactness, hourly co-firing cells, exemplars and the unparsed queue.
  - Compose of the window into a generation with a library identity check (stale partitions are
    reparsed first) and a compose identity (unchanged windows are not rebuilt); per-day and window
    documents with a rendered `header` field.
  - `logs run-daily|backfill|settle|compose` under a schedule lock separate from the write lease.
    Pulling refuses a mapping without a redactor; the CLI has none until slice B11, so the three
    pulling commands refuse today.
  - Partition clocks `index_date` and `event_time` (a reporting day in `logs.timezone`, DST-aware,
    read from every UTC index day it overlaps).
  - Write path: the `line` chunk recipe (`ingestion.line_recipe`), projection exclusions
    (`ingestion.projections`), `manifest_extra`. Both keys enter the index recipe only when set.
- **Retention (`logs.retention`, `logs.pins`; slice L3b).** Rule (a) `gc` of every generation that
  lists an expired day, cross-process pins with TTL on the file (sqlite sidecar), memory, SQL and
  PostgreSQL registries, per-process heartbeat pins, ledger tags / `ledger_refs` / `purge` on the
  memory, sqlite, sqlalchemy, postgres and mongodb ledgers, cache invalidation, session expiry,
  audit records, `retention report|verify|run` with a content-based verifier. `logs` is refused on
  MongoDB and Elasticsearch catalogs (no pins).
- `scripts/compose_scale.py`: the compose scale benchmark (synthetic aggregate rows on SQLite).

- **Native aggregation pushdown for the `opensearch` and `elasticsearch` records facets.**
  - New `backends.<name>.records` block: `aggregation: client | native` (default `client`, unchanged
    behaviour and fingerprints), `matched_ids_cap`, `max_groups`, `distinct_exact_cap`. It is a
    query-time setting and is excluded from the index recipe.
  - Under `native`, scope operations compile to server-side aggregations (`backends/_lucene_aggs.py`):
    - `track_total_hits` for counts;
    - `composite` paging for groups and cross-tabs (nulls via `missing_bucket`);
    - `date_histogram` with the schema `time_zone` for calendar groups and buckets;
    - a count-ordered `terms` request for `top_n`/`nth`, trusted only when the engine reports
      `doc_count_error_upper_bound: 0` (OpenSearch 3.x concurrent segment slices report an error);
    - `filters` for periods;
    - `filter` sub-aggregations for share numerators and null counts;
    - `stats` for sum/avg/min/max and `top_hits` for first/last seen;
    - exact `count_distinct` by pair paging, with `cardinality` above the cap labelled `approximate`.
    The ACL is the query of every aggregation request, and nothing is scanned or `max_matched`-bound.
  - `ScopeResult.matched_ids_truncated` (additive, default `false`): under `native`, `matched_ids` is
    the first `matched_ids_cap` ids and the flag says more matched. The structured arm passes it on
    as `document_ids_truncated` (kept out of the prompt). `AggregateEvidence.empty()` also requires
    `total == 0`.
  - More than `max_groups` groups is a `bounded` result. A group key that the index keeps apart but
    Python equates is a typed `BackendError`.
  - Conformance: new kit `testing/aggregation_parity.py` (client/native parity, ACL isolation,
    native caps). The records kit also runs natively (`opensearch_native`, `elasticsearch_native`).
    Results are identical on OpenSearch 2.19.1, 3.9.0 and Elasticsearch 8.17.4.
  - `scripts/bench_native_aggregation.py` measures client and native latency (docs/VALIDATION.md).

## [2.0.0a3] - 2026-11-04

Owner decisions on the answer contract and Anthropic defaults, fixes for the local-model QA findings
(QQ-01 to QQ-06), and the first real Anthropic run (a small, spend-capped smoke; see
docs/VALIDATION.md).

The release also folds in the parity work on precomputed vectors, source ordering, the typed query
compiler, the Kuzu graph backend and the BM25 lexical engine.

### Changed

- **`pipeline.generation.contract.on_violation` defaults to `strip`.** A narrative answer (the
  question asks no count, list, total, earliest/latest or absence) loses only the sentences that
  carry an unbacked count, list or absence statement, and each removal is recorded
  (`contract: stripped`, `action: strip`). An unbacked statement that answers an aggregate question
  still abstains, as do unknown evidence, missing citations, empty answers and stated numbers that
  differ from the cited exact aggregate. Set `on_violation: abstain_with_reason` for the 2.0.0a2
  behaviour.
- **Anthropic roles default to native structured output** when the registry declares the model
  capable (every Claude 3 and later family) and the role does not set `structured_output`. Set
  `structured_output: none` to keep prompt mode. `capabilities.structured_output` is now unset
  (`null`) in the schema until the role resolves it.
- Haiku 5.x and Sonnet 5.x use `output_config.format` as their native form. The wire schema drops
  the numeric bounds and `maxItems` the API refuses, and the reply is still validated with the full
  schema. Other Claude families keep forced tool use.
- Models that think by default (Haiku 5.x) need `max_tokens` of at least 1024. A smaller value is
  rejected. A v1 file is upgraded with the value raised and a `max_tokens_raised` warning
  (`--strict` refuses). `examples/anthropic.yaml` sets its reviewer to 1024.
- The answer contract checks the text of an answer flagged `insufficient_evidence` too (QQ-06).
- Packing reserves the evidence budget for aggregate evidence before documents (documents are still
  emitted first); an aggregate larger than the whole budget is reported in `omitted_ids`.
- `count_distinct` named by `field` counts distinct values of that field; `field` with a different
  `unit` is rejected.
- The v1 downgrade refuses non-empty `source_order` / `source_slots` and `precomputed`.

### Added

- `ProviderLimitIgnored` (category `output_limit_ignored`): a reply that reports more output tokens
  than the role's `max_tokens` fails instead of being used (QQ-01). The docs make
  `max_tokens_parameter: max_tokens` required for Ollama.
- Total/sum statements of an amount, with or without a currency or unit, are count statements in the
  answer-text check even without a numeric field (QQ-03).
- `Intents.untargeted`: "list all/every", "show me all" or "enumerate" applied to a common record
  noun that is not a declared target. The text is checked as a list answer. The unsupported constraint
  and a planner's bounded structured step name the undeclared noun, instead of saying the question
  asks for no list (QQ-04).
- Anthropic usage records thinking tokens (`output_tokens_details.thinking_tokens`) as
  `reasoning_tokens`. A truncation names the thinking share, and `stop_details` is recorded.
  OpenAI-compatible calls record the size of a server's separate thinking text as `reasoning_chars`
  (QQ-05; Ollama's thinking attribution is documented).
- `providers.output_format_schema`. The `capabilities` registry gains `thinks_by_default` and
  `min_max_tokens`.
- `models.roles.embedder.precomputed`: import document vectors computed elsewhere (`vectors` .npy +
  `keys` JSONL of `{document_id, field, text_sha256}`). Both files are pinned by required digests
  (`sha256`, `keys_sha256`), audited before any write (counts, dimension, finite, non-zero, unique
  keys) and matched to chunks by document, field and exact-text digest. The row/key pairing is
  positional; the optional `verify` spot check (`sample`, `min_cosine`, `seed`) re-embeds sampled
  chunks with the configured provider and detects a mispaired or foreign keys file. Index identity
  and the embedding recipe use the digests, never the file paths. The manifest records
  `precomputed_vectors` (digests, pairing, matched/embedded/unused rows, spot check) and
  `embedding_recipe`.
- `selection.source_order`: strict, stable source order of the selected union, applied before
  exact-id pins; membership unchanged.
- `selection.source_slots`: reserved source positions inside each arm's `per_arm` budget, then fill by
  score. Pinned (exact-id) candidates are kept first and never displaced. Recorded in the selection
  trace.
- Query-compiler operations on every records adapter (shared `backends/_scope.py` semantics):
  group ranking (`order`, `top_n`, `nth`, boundary `ties`), calendar group buckets (`granularity`),
  cross-tab, period comparison (`periods`), group shares and share numerators, record lists with
  `total`, `unknown` null counts under negated predicates, `basis` labels.
- Literal topic counts (`ScopeSpec.text`) resolved by the structured arm through the lexical index
  over the whole eligible corpus (`pipeline.arms.structured.text_backend`).
- Calendar expressions against a request anchor (`as_of`) in `metadata_schema.timezone`
  (`decision/windows.py`, `ValueResolver.window`); declared value sets (`fields[].states`).
- `rules` query grammar (`decision/grammar.py`) and planner scope drafts (`windows`, `periods`, ...)
  for these operations; new intent kinds `rank`, `breakdown`, `measure`, `share`, `compare`.
- `pipeline.selection.packing.aggregate_reserve_max` (default 0.5) caps the aggregate reservation.
- **Embedded Kuzu graph backend** (extra `kuzu`): `mode: native`, with the same contract as `neo4j`,
  and `mode: attach`, which opens an existing Kuzu store read-only through a declared mapping file.
  An edge without a quote or document id, or without a verified quote under
  `require_verified`, is never surfaced. Writes to an attached store are refused.
- **Typed graph traversal templates** (`pipeline.arms.graph.templates`): `neighbors`, `chain`,
  `connect`, `mentioned_in`. A decision agent selects a declared template by name, never by writing
  Cypher. A conformance test checks identical results on sqlite, kuzu and the in-memory backend; the
  neo4j case is in the same test but needs a service and has not been run for this release.
- `communities.strategy: louvain_hierarchical` (extra `communities`): a seeded multi-level Louvain
  dendrogram with deterministic per-community stats (size, trend, top entities).
- **SQLite `lexical_engine: bm25`**: an Okapi BM25 inverted index built at ingest into each
  generation and read through memory maps. It scores like `rank_bm25.BM25Okapi`, including the idf
  epsilon floor, and applies scope as a document bitset. FTS5 stays the default.
- Lexical arm options `verbatim_boost` and `pin_identifiers` (both off by default; `verbatim_boost`
  needs `lexical_engine: bm25`). A selection `Candidate.leads` flag keeps an arm's front records ahead
  of `source_slots`.
- **OpenSearch 3.x support** for the `opensearch` backend; 2.x keeps working. `probe` accepts 2.x
  and 3.x, reports the `major`, and refuses an HNSW engine the detected major cannot create (3.0
  blocks new `nmslib` indices; the adapter writes `lucene`, which both accept). The index layout,
  mappings and query DSL are unchanged, so generation identities and golden fingerprints are the
  same. On 3.x, BM25 scores are the 2.x scores divided by 2.2 (`BM25Similarity` replaces
  `LegacyBM25Similarity`); rankings and every fusion policy are unaffected. The conformance suite
  passed against `opensearchproject/opensearch:2.19.1` and `3.9.0`. `tests/services/docker-compose.yml`
  gains an `opensearch3` service (profile `opensearch3`, port 19202), and `SU_TEST_OPENSEARCH_MAJOR`
  pins which major the tests expect.

### Fixed

- OpenSearch and Elasticsearch lexical paging no longer asks for more than the 10000-hit result
  window. The structured arm's text conditions page with 10000-hit pages, and the look-ahead hit
  made the request 10001, an HTTP 400 on every records check against a real server. A page at the
  window edge now decides `next_cursor` from an exact `_count`, and a page beyond the window is a
  typed `BackendError`, never a shortened ranking.
- A number directly after a declared field name ("priority 3 tickets") is no longer read as a count.
  Before, such a number made a correct exact answer fail `answer_number_mismatch` (QQ-02).
- A count whose nearest following word was a question noun read as a label was missed ("111 open
  CloudTax tickets" when the question made "open" a unit). The farther unit is now checked.
- SQLite lexical index: FTS rows are replaced with one scan (the per-chunk delete was quadratic) and a
  chunk id repeated within one `index()` call is indexed once.
- SQLite vector index: block-append upserts with a persistent id set (no per-batch restack or set
  rebuild); the Faiss index is cached per generation; a scope filter is applied as a row bitset before
  the top-k, so the search depth never has to grow.
- `count_distinct` over a declared field excluded neither missing nor null values (each counted as
  a distinct value); they are now excluded and reported in `missing_or_invalid` (parity review B1).
- With sqlite `lexical_engine: bm25` and `tokenizer: ascii_alnum`, a required or phrase word the
  analyzer drops now matches nothing, and a topic count with such a word is reported as bounded with
  the reason instead of exact (release review a3 B3). Topic-count evidence names the generation's
  recorded analyzer.
- Vector search applies scope as a document bitset (Faiss `IDSelectorBitmap`). Arms no longer parse
  whole documents to build hits, and selection no longer deep-copies candidates (lower retrieval
  latency).

The following fixes address findings from the final parity run. Evidence is synthetic and mocked
only; no paid call was made.

- **Native structured output on oversized schemas.** Anthropic refused the planner's plan schema as
  `output_config.format` with HTTP 400 "compiled grammar is too large".
  - A deterministic pre-flight counts the wire schema against `OUTPUT_FORMAT_LIMITS` (24 optional
    and 16 union-typed properties).
  - A role whose `native` is only the registry default now resolves to prompt mode for an oversized
    schema at config time, with a `structured_output_fallback` warning. A declared `native` is a
    validation error.
  - The 400 is the typed `ProviderSchemaTooComplex` (`schema_too_complex`). A registry-default role
    makes one explicit switch to prompt mode, recorded as `structured_output_fallback` in the call
    record and the `model_call` trace event. A declared `native` fails.
  - `examples/v2/hosted_models.yaml` declares `structured_output: none` for its Sonnet 5.x planner.
- **`answer_number_mismatch` misread populations.** A number that names a ratio's population is no
  longer compared with the count:
  - "out of 2048 records", "a denominator of 2,048", "denominator: 2048", "2,048 records as the
    denominator";
  - "12/36", "40% of 2048", and the 50 in "12 of 50".
  
  The cited denominator also matches after a preposition ("among 2,048 records"), and so does the
  sum of the group counts. A wrong count beside a population, or the denominator stated alone as
  the count, still abstains.
- **The rules path dropped qualifiers but labelled the result `exact`.** It now abstains with
  `qualifier_not_applied` for:
  - an ordinal other than `first` outside a ranking ("who is second for X");
  - `active`/`inactive` when no field declares it;
  - a numeric comparison bound to no field ("with >= 20 records").
  
  A topic cue no longer starts inside a hyphenated word: "X-mention <unit>" is the topic X, and
  "invoice-mention rate" is no longer "mention rate". A topic made only of grammar words ("contain
  the most active records") is not a text match.
- **Sorting a group ranking by count was refused.** A planner scope that sorts a grouped scope by
  `count` (or by its measure or the measure's field) compiles to the group ranking (`order`). Without
  `group_by`, or with a contradicting `order`, it is `measure_unsupported`. The planner prompt says so.
- **The precomputed `verify` spot check re-embedded the wrong text.** `verify.input` now defaults to
  `engine_template` and re-embeds the engine's own `{title}\n{field}: {text}` input (`chunk_text`
  keeps the bare text). The manifest's spot-check report records the input. The index identity is
  unchanged.

## [2.0.0a2] - 2026-11-04

Fixes for the 2.0.0a1 QA findings (`docs/plans/reviews/qa_2.0.0a1.md`, QA-01 to QA-16), the
2.0.0a1 completeness-review risks R1 to R3, and the 2.0.0a2 release review
(`docs/plans/reviews/a2_release_review.md`, B1 to B6 and part of N1 to N10).

### Added

- Write leases with fencing on every generation registry (file, memory, PostgreSQL, SQLAlchemy,
  MongoDB, Elasticsearch). A second writer is refused with `WriteConflict(held)`. A writer that lost
  its lease, or whose base generation moved, fails with `IngestConflict` and never switches `CURRENT`.
  Secondary pointers switch by compare-and-set (R1).
- Incremental embeddings. Vectors are reused by recipe and exact input, and the manifest reports
  `embedded` and `reused_embeddings` (R2).
- `budgets.ingest` (`max_llm_calls`, `max_embedding_calls`, `max_usd`) caps a whole build,
  separately from the per-request budget. The embedder `pricing_key` (QA-06).
- Session writes are compare-and-set on `SessionRecord.version`, with per-turn session-budget holds
  (QA-05).
- Qualifier coverage of agent-compiled scopes. An unapplied question qualifier makes the aggregate
  `bounded` with `qualifier_not_applied` (QA-01). Candidates are observed or enum values, calendar
  expressions and quoted phrases. Since this release they also include a filterable field named next
  to a value of its type ("priority 3", "version 2.1", "severity high"; review B1).
- Request filters are validated against `metadata_schema` (QA-02). A string or id value on a
  listable field that no record holds is reported as `value_not_observed`, and its aggregate is not
  exact (review N4).
- The answer contract checks the answer text for count, exhaustive-list and absence statements
  (QA-04). A count stated in the text must equal the cited exact aggregate (`answer_number_mismatch`,
  review N2). Claim-level stripping, and a generator schema restricted to the supplied evidence ids
  (QA-08).
- HTTP 409 with a typed body (`{"error": "write_conflict" | "session_conflict", "reason": ...}`) for
  write and session conflicts. A failed action answers with its category's status and
  `result.category` / `result.reason`. MCP returns `write_conflict: <reason>` /
  `session_conflict: cas_attempts_exhausted` tool errors (review B5).
- A golden test pins the 2.0.0a1 index fingerprint of every shipped example (review B2).
- `CHANGELOG.md`.

### Changed

- A hosted embedder on the dense arm (`openai`, or `openai_compatible` on a non-loopback host) now
  needs an explicit `budgets.ingest.max_usd` or `budgets.ingest.max_embedding_calls`. Without one,
  engine construction fails with `ConfigError` (review N5).
- `extraction.ontology`, `judge`, `cache_gate` and `pipeline.arms.cache` are refused with "not
  implemented" instead of being accepted and ignored (R3).
- The file generation registry no longer waits for a lock. An overlapping writer gets
  `WriteConflict(held)`.
- Answer-text detector precision (review N1). The following are no longer statements: "a 404
  error", "Windows 10 users", "there is no error message", "the only file you need" and "the complete
  list of supported currencies".
- The pytest configuration sets `pythonpath = ["."]`, so a bare `pytest -q` collects in a fresh
  install (QA-16, review B4). The Elasticsearch and OpenSearch conformance modules skip without their
  extra unless a server URL is configured.
- Agent workflow docs no longer name an operator-local key file (review N8).

### Fixed

- Planner scopes that drop a numeric, boolean or field-named qualifier no longer produce a passed,
  wrong count (review B1).
- Indexes built by 2.0.0a1 open under 2.0.0a2. Removing `extraction.ontology` had changed the
  extractor recipe hash. The recipe now keeps the keys 2.0.0a1 hashed (review B2). No rebuild is
  needed, and `INDEX_FORMAT` is unchanged (3).
- Real-service test monkeypatches accept the `expected=` argument of `activate` (review B3).
- The intent pre-pass gets the deployment's structured targets on every surface (QA-03). Aggregates
  expose the predicates they counted (QA-09). Config upgrade, CLI exit codes, bearer-scheme
  comparison, deferred secrets and strict-schema errors (QA-10 to QA-15). Unicode lexical matching
  (QA-07).

### Known limitations

- The Anthropic provider has not completed a real call. The 2.0.0a2 smoke got HTTP 429 on its first
  request. See VALIDATION.
- Expiring leases compare writer clocks (no cross-host skew handling). A time qualifier counts as
  applied when any predicate constrains any datetime field (review N3). Session holds reserve the
  request ceiling (review N6). `STRASSE` and `straße` do not match (review N10).

## [2.0.0a1] - 2026-11-03

First alpha of the v2 rewrite: Phase 0 and Phase 1 of `docs/plans/v2_implementation_plan.md`.

### Added

- v2 kernel and `schema_version: 2` configuration with a v1 upgrade path (`config upgrade`).
- Backends: SQLite, PostgreSQL (with pgvector), generic SQL (SQLAlchemy), OpenSearch,
  Elasticsearch and MongoDB, plus Neo4j for the graph arm. A shared conformance kit.
- Providers: OpenAI, Anthropic and OpenAI-compatible, with a capability registry and native
  structured output where declared.
- Decision agents (`rules`, `classifier`, `llm`, `cascade`), declarative pipelines, an `exact_id`
  arm, deterministic selection, an optional review stage and the answer contract.
- Sessions, read-only tools, audited actions, HTTP with SSE streaming, and MCP (stdio and streamable
  HTTP).

[2.0.0a3]: https://github.com/Taranjeet-SU/su_multiarm/compare/v2.0.0a2...v2.0.0a3
[2.0.0a2]: https://github.com/Taranjeet-SU/su_multiarm/compare/v2.0.0a1...v2.0.0a2
[2.0.0a1]: https://github.com/Taranjeet-SU/su_multiarm/releases/tag/v2.0.0a1
