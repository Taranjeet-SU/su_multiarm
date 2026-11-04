# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[PEP 440](https://peps.python.org/pep-0440/) version numbers. Evidence for each release (synthetic,
mocked or real-service) is recorded in [docs/VALIDATION.md](docs/VALIDATION.md).

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

[2.0.0a2]: https://github.com/Taranjeet-SU/su_multiarm/compare/v2.0.0a1...v2.0.0a2
[2.0.0a1]: https://github.com/Taranjeet-SU/su_multiarm/releases/tag/v2.0.0a1
