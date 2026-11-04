# Configuration and usage

The full generated schema is [config.schema.json](config.schema.json). Unknown keys are errors.
There is no implicit inheritance between YAML examples: each is a complete configuration with defaults.
Use `Config.model_json_schema()` to regenerate the schema after changing public options.

## Installation options

| Extra | Installs / enables |
|---|---|
| `faiss` | Faiss CPU exact cosine |
| `postgres` | psycopg 3 for the `postgres` backend; the database needs the pgvector extension for vectors |
| `sql` | SQLAlchemy 2 for the generic `sqlalchemy` backend (plus the dialect's driver, e.g. psycopg for PostgreSQL) |
| `neo4j` | Neo4j driver; database is external |
| `elasticsearch` | official Elasticsearch 8.x client for the `elasticsearch` backend kind; server is external |
| `semantic` | sentence-transformers embedding and cross-encoder support |
| `spacy` | spaCy; separately install a licensed language pipeline |
| `gliner` | GLiNER; downloads the configured model on first use |
| `communities` | NetworkX Louvain |
| `api` | FastAPI/Uvicorn |
| `mcp` | official MCP Python SDK, stdio and streamable-HTTP transports |
| `mongodb` | PyMongo for the `mongodb` backend |
| `opensearch` | opensearch-py for the `opensearch` backend |
| `local` | sentence-transformers for local embedder/reranker models (same as `semantic`) |
| `openai`, `anthropic` | nothing extra: both providers speak HTTP through `httpx`, a core dependency; the extras exist so install commands that name them resolve |
| `all` | every optional backend, model and serving extra above (not `dev`) |
| `dev` | tests, lint and build tools |

For example: `pip install -e '.[faiss,semantic,api]'`. Base install supports the offline example.
Optional libraries are imported only when their provider is used. Missing dependencies fail explicitly.
No existing database server, local model, API key or GPU endpoint is auto-discovered.

## Choose execution mode and models

The engine consumes configuration v2 (`schema_version: 2`, see below). The v2 examples are in
[examples/v2/](../examples/v2/); the top-level `examples/*.yaml` are v1 files kept to exercise the
deterministic upgrade. `pipeline.decision.kind: rules` is the former
`raw` mode (no planner call, the query goes unchanged to every enabled arm); `kind: llm` is the former
`planned` mode (one planner call with validated steps); `kind: llm` plus `pipeline.review.enabled:
true` is the former `agentic` mode (bounded reviewer follow-ups in `answer()`, available with any
decision kind). Generation is enabled by configuring `models.roles.generator`. An explicit typed
`aggregate` on the request always runs as given on the structured arm; a natural-language count is
answered only when the decision agent resolves it to an exact typed scope, otherwise the request
abstains (`on_failure: fail_closed`) or runs narratively with the count flagged as unsupported
(`raw_all_arms`). No mode guesses a query from natural language.

[anthropic.yaml](../examples/anthropic.yaml) shows independent Sonnet routing and Haiku extraction,
review and answering. Its example model IDs were checked against the
[Anthropic model list](https://platform.claude.com/docs/en/models/overview), but paid calls were not
made as part of the default tests. Replace each model ID with the version you have evaluated and can
access. Changing a model used only for query-time roles does not require reindexing. Changing the
extraction or embedding model does.

### Model providers and the capability registry

Chat roles (`models.roles.planner|reviewer|extractor|generator|community`) take
`provider: anthropic | openai | openai_compatible` (a role that is not configured is `none`: a stage
that needs it fails with a typed error). API keys are read only from the environment variable named
by `api_key_env` (required for `anthropic` and `openai`); no key is ever stored in a config.
`models.roles.judge` and `models.roles.cache_gate` are refused with "not implemented in this release"
until the evaluation harness (Phase 4) and the cache arm (Phase 2) consume them.

A key is required at engine construction only when ingest or the default pipeline's own decision
mode needs it. The key of a role used only by a named (non-default) pipeline or a non-default
request mode, such as an `openai` planner of a `planned` pipeline, is checked when a request first
selects that pipeline/mode: unset, the request fails with `MissingSecret` before any work (HTTP 502);
`ingest` and requests on other pipelines run without it. `doctor` lists every unset key under
`missing_keys` (`{"pipeline:<name>": {"<role>": ["<VAR>"]}, "ingest": {...}}`).

Each role declares what it relies on under `capabilities`, and validation checks the declaration
against the per-`(provider, model-pattern)` registry in `su_multi_arm/providers/capabilities.py`:

| `structured_output` | Wire behaviour | Allowed for |
|---|---|---|
| `none` (default) | schema appended to the system prompt; reply validated strictly | every provider |
| `json_mode` | `response_format: {type: json_object}` + prompt schema | `openai`, `openai_compatible` |
| `json_schema` | `response_format: json_schema` with `strict: false` + prompt schema | `openai`, `openai_compatible` |
| `native` | Anthropic: one forced tool whose `input_schema` is the schema; OpenAI: `json_schema` with `strict: true` (schema converted to strict form) | registry-declared pairs only: Anthropic `claude-3*`, `claude-{sonnet,opus,haiku}-4*`/`-5*`; OpenAI `gpt-4o*` (not `gpt-4o-2024-05-13`), `gpt-4.1*`, `gpt-5*`, `o1*` (not `o1-mini`/`o1-preview`), `o3*`, `o4-mini*`; never `openai_compatible` |

`prompt_caching: controlled` (Anthropic only) sends `cache_control: {type: ephemeral}` on the system
block and the tool definition; `passive` (OpenAI and compatible servers) records the cached tokens the
provider reports and claims no control. `reasoning_effort` is accepted only for registry-declared
reasoning models (`o1*`, `o3*`, `o4-mini*`, `gpt-5*`) and for `openai_compatible` (sent as
`reasoning_effort`). `openai` always uses `max_completion_tokens`; `anthropic` always uses
`max_tokens`; `max_tokens_parameter` applies to `openai_compatible` servers. An undeclared
capability fails validation with the key path; nothing is downgraded at run time. For OpenAI
`native` (strict `json_schema`) roles, validation and `doctor --probe` also convert every schema the
role sends (planner `LlmPlan`, generator `Answer`, reviewer `Review`, extractor `Extraction`,
community `CommunityReport`) to strict form and reject the role when one is not strict-expressible.
Strict form refuses (typed `StrictSchemaError`, before any request) every keyword outside the
supported set (`type`, `properties`, `required`, `additionalProperties: false`, `items`, `enum`,
`const` with a sibling `type`, `anyOf`, `$ref`, `$defs`, `description`, `pattern`, `format`,
`minLength`, `maxLength`, `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`,
`multipleOf`, `minItems`, `maxItems`; `title`/`default` are dropped): so `oneOf`, `allOf`,
`discriminator`, `uniqueItems`, a typeless `const`, an `anyOf` mixed with other constraining keywords
or at the root, and free-form mappings are refused — declare `json_schema` for such a role. Every
schema the package sends is strict-expressible as of 2.0.0a2 (the planner's step filters are a list of
`{field, value}` pairs and a step is one flat object rather than a discriminated union).

Every call is strict: one HTTP request, no automatic retry, no repair call. 401/403, 429, 5xx,
timeouts, connection errors, non-JSON bodies, truncation (`max_tokens`/`length`), refusals and a
native call without its tool call are typed `ProviderError`s with a `category`; a reply that does not
match the schema is a `SchemaViolation`. The caller's fail policy decides what happens next. Each
call records requested and returned model, mode, usage (input, output, cached and cache-write
tokens), stop reason and timing; on the request path the usage is committed to the budget (USD via
`budgets.price_table`, including optional `cached_input_per_million` and
`cache_write_input_per_million` rates) and emitted as a masked `model_call` trace event. When a
server reports no usage, the reservation estimate (input chars/4, output `max_tokens`) is kept.

Anthropic (native structured output and controlled caching):

```yaml
models:
  roles:
    planner:
      provider: anthropic
      model: claude-sonnet-4-5          # pin the id you evaluated
      api_key_env: ANTHROPIC_API_KEY
      max_tokens: 2048
      pricing_key: claude-sonnet-4-5    # key into budgets.price_table
      capabilities: {structured_output: native, prompt_caching: controlled}
```

OpenAI (strict JSON schema, passive caching):

```yaml
models:
  roles:
    generator:
      provider: openai
      model: gpt-4.1-mini
      api_key_env: OPENAI_API_KEY
      max_tokens: 2048
      capabilities: {structured_output: native, prompt_caching: passive}
    planner:                            # LlmPlan is strict-expressible since 2.0.0a2
      provider: openai
      model: gpt-4.1-mini
      api_key_env: OPENAI_API_KEY
      capabilities: {structured_output: native}
```

Local OpenAI-compatible server (Ollama `/v1`, vLLM, llama.cpp server, LM Studio):

```yaml
models:
  roles:
    extractor:
      provider: openai_compatible
      base_url: http://127.0.0.1:11434/v1
      model: "<local-model-id>"
      max_tokens: 2048
      max_tokens_parameter: max_tokens  # if the server requires that name
      timeout_seconds: 120
      capabilities: {structured_output: json_schema}   # only if the server supports it; else none
    embedder:
      provider: openai_compatible
      base_url: http://127.0.0.1:11434/v1
      model: "<local-embedding-id>"
      dimensions: 768
```

`openai_compatible` without `base_url` keeps the v1 default (`https://api.openai.com/v1`); set it for
a local server. A local endpoint may omit `api_key_env`. Embedding roles take `provider: hash |
sentence_transformers | openai | openai_compatible`; `openai` requires `api_key_env` and sends
`dimensions` for `text-embedding-3*` models. Remote embedding dimensions are checked against
`dimensions`. Set `query_prefix` / `document_prefix` for models that require them. For local
sentence-transformers, `device` defaults to CPU; select a CUDA device explicitly if desired. Long
local-model inputs fail rather than silently truncating. Provider-side truncation for remote
embeddings cannot be verified by the generic interface. v1 files keep `provider: openai_compatible`
for OpenAI (v1 has no `openai` provider; `config downgrade` refuses a v2 `openai` role).

Evidence level: Anthropic and OpenAI request/response shapes, caching and usage are
**contract-mocked only** (no paid call was made); the local path has an optional `external` smoke
(`tests/services/test_local_llm.py`, `SU_TEST_LOCAL_LLM_URL` + `SU_TEST_LOCAL_LLM_MODEL`).

`pipeline.decision.prompt`, `pipeline.review.prompt` and `pipeline.generation.prompt` are inline
replacements (v1 `routing.prompt` / `generation.prompt` upgrade to them). Alternatively use
`prompt_file`, relative to the YAML file. Replacing a prompt also replaces its grounding rules, so keep
the rules you need; the generator prompt must keep asking for `claims` with their kinds and evidence
IDs, because the answer contract is enforced on them. A schema instruction is appended by the
provider regardless of prompt customization (except in `native` mode, where the schema travels as
the tool definition or strict `response_format`).

## Storage/backend configuration

Backends are named instances under `backends:`; `storage:` names the authoritative backend per
non-arm capability (`catalog`, `records`, `ledger`, `sessions`, `cache`, `communities`) and each arm
names its own backend. In this release the engine wires these kinds on the frozen contracts:

| Kind | Capabilities | Notes |
|---|---|---|
| `sqlite` (local default) | all eight | `path` (state directory), `vector_engine: numpy|faiss`, `timeout_seconds`. Per generation: `snapshots/<gen>/catalog.sqlite` (documents, chunks, FTS5 `unicode61` — the query is tokenised and case-folded by the same tokenizer as the text, so `Straße` and `ﬁle` match their own words but not `strasse`/`file`, typed field rows for scope pushdown, records, mentions, relations with provenance and occurrences, memberships, communities) plus `vectors.npy`/`chunk_ids.json` (and `vectors.faiss`); `runtime.sqlite` holds the cache, the ingestion ledger with raw model replies and sessions. `CURRENT` and `manifest.json` are owned by the generation registry, never read by the adapter. |
| `memory` | all eight | In-process fake for tests and contract suites; not persistent. |
| `postgres` (extra `postgres`, psycopg 3; pgvector for vectors) | `catalog`, `lexical`, `vector`, `records`, `cache`, `ledger`, `session` | `dsn_env` (environment variable holding the DSN; never the DSN itself), `table_prefix`, `text_search_config` (default `english`), `vector` (`index: exact\|hnsw`, `hnsw_m`, `hnsw_ef_construction`, `hnsw_ef_search`, `min_filter_recall`, default 0.95), `timeout_seconds` (connect timeout and `statement_timeout`). **One schema per generation**, `<prefix>_<dataset>_<generation>` when that fits PostgreSQL's 63-byte identifier limit and the dataset is a lowercase identifier, otherwise `<prefix>_g<sha256(dataset/generation)[:20]>`; it holds documents, chunks, typed field rows (native `double precision` and `timestamptz` columns), records, the FTS table and the pgvector tables, and `delete_generation` is one `DROP SCHEMA ... CASCADE`. Schema `<prefix>` holds `generations` (dataset, generation, schema name, text-search configuration, metadata schema), `meta` (the `CURRENT` pointer row, replaced by a single-row upsert only by `activate`; failed builds write `failure:<gen>` and never touch it; manifests), `cache`, `ledger`, `jobs`, `raw_replies`, `sessions`. The write lock is a session-level `pg_try_advisory_lock` (held = typed error, never a wait). Typed scope, ACL predicates and id allow/deny lists compile to bound-parameter `EXISTS` subqueries; temporal values compare as UTC instants (naive = UTC, dates by calendar day); `values` is `SELECT DISTINCT` under `max_values_cardinality` (10000; more is a typed error). Lexical: title and body `tsvector`s built with the generation's recorded configuration (`analyzer_id` `pg_ts_<config>`); terms = any term, `phrase` = `phraseto_tsquery` per column, `boolean` = terms plus `must` phrases minus `must_not` phrases; every user token is a bound parameter, never tsquery syntax; scores are `2*ts_rank_cd(title)+ts_rank_cd(body)`, **not BM25**; stop words never match and stemming conflates inflections (FTS5 differs). Vector: L2-normalised, inner product = cosine; **exact** by default (scope in the `WHERE` clause, order `(distance, chunk_id)`); `vector.index: hnsw` builds a partial HNSW index per namespace at `refresh` and then measures recall@10 of the forced index path against exact search (unfiltered and with an allow-list filter); below `min_filter_recall` the refresh fails with a typed error so that build never activates; HNSW searches use `hnsw.iterative_scan = strict_order` (pgvector >= 0.8). Up to 2000 dimensions with HNSW, 16000 exact. `graph` is `planned`. |
| `sqlalchemy` (extra `sql`, SQLAlchemy 2 + driver) | `catalog`, `records`, `cache`, `ledger`, `session` | `url_env` (environment variable holding the SQLAlchemy URL, e.g. `sqlite:///state/rel.db` or `postgresql+psycopg://...`), `table_prefix`, `timeout_seconds`. SQLAlchemy Core only (portable types, delete+insert instead of dialect upserts). Per generation: tables `<prefix>_<token>_{docs,chunks,dfields,records,rfields}` with `token = sha256(dataset/generation)[:12]` (the readable name does not fit the 63/64-byte identifier limits; `<prefix>_generations` maps it); runtime tables `<prefix>_{generations,meta,cache,ledger,jobs,raw_replies,sessions}`; `CURRENT` pointer row replaced in one transaction only by `activate`; write lock = lease row (held = typed error; a lease older than 600 s is taken over). Temporal values are stored as UTC epoch seconds (no portable timezone-aware type). Ids are limited to 512 characters (typed error beyond). **Tested dialects: SQLite and PostgreSQL** (the conformance kit runs on both); any other dialect builds and `ping()` reports `dialect_tested: false` — MySQL/MariaDB in particular compare text with the column collation (usually case-insensitive), which would change `eq`. `lexical` and `vector` are dialect-specific and `planned`; configuration naming them is rejected. |
| `mongodb` (extra `mongodb`, pymongo) | `catalog`, `lexical`, `records`, `cache`, `ledger`, `session` | `uri_env` (environment variable holding the connection URI; never the URI itself), `database`, `collection_prefix`, `timeout_seconds` (server selection, connect and socket timeouts). Per generation: `<prefix>_<dataset>_<generation>_{documents,chunks,lexical,records}` collections; `<prefix>_generations` lists known generations with their metadata schema; `<prefix>_{cache,ledger,jobs,raw_replies,sessions}` are prefix-wide; `<prefix>_meta` holds the generation pointer (`_id: CURRENT`, replaced atomically only by `activate`; failed builds write `failure:<gen>` and never touch it), manifests and the write lease. Typed scope, ACL predicates and id allow/deny lists are pushed down as query predicates over typed field rows (temporal values stored as UTC instants under the shared rule: aware stamps converted, naive `assume_utc` stamps and dates read as UTC, `date` fields compared by UTC calendar day); `exists` is one `find_one`; `values` groups distinct values server-side under `max_values_cardinality` (default 10000, exceeding it is a typed error, never a truncated list). Lexical search uses the server's `english` text index (`analyzer_id: mongo_text_english`): phrase and `must`/`must_not` are supported, scores are MongoDB `textScore` values and **not BM25**, so no score parity with other engines is claimed. The cache has a server-side TTL index on top of the clock-based expiry; sessions get a TTL index only when the backend is built with `session_ttl_seconds` (Python API). No `graph`; `vector` stays `planned` because MongoDB vector search exists only on Atlas and is not verified here, so a config naming `(mongodb, vector)` is rejected. |
| `opensearch` | `catalog`, `lexical`, `vector`, `records` | `hosts_env` (environment variable holding one or more comma-separated `http(s)://host:port` URLs), `index_prefix`, `knn: {method: exact|hnsw, exact_rescore: true}`, `timeout_seconds`. Needs `pip install 'su-multi-arm[opensearch]'` (`opensearch-py`); OpenSearch 2.x. Per generation three indices `<index_prefix>_<dataset>_<generation>_{docs,chunks,records}` (one shard, strict mappings tagged with the adapter's mapping version); dataset aliases `<index_prefix>_<dataset>_{docs,chunks,records}` are switched atomically by `OpenSearchBackend.activate(gen)`, and `delete_generation` refuses the generation an alias serves. Datasets must match `[a-z0-9][a-z0-9_-]*` (index names are never mangled). See "OpenSearch semantics" below. |
| `elasticsearch` | `catalog`, `lexical`, `vector`, `records` | Same options as `opensearch` (`hosts_env`, `index_prefix`, `knn`, `timeout_seconds`); needs `pip install 'su-multi-arm[elasticsearch]'` (official `elasticsearch` 8.x client) and an Elasticsearch 8.x server (other majors and OpenSearch are refused by `probe`). The adapter is the OpenSearch adapter's classes over `backends/_lucene_common.py` with the engine-specific parts replaced: transport, probe, `dense_vector` fields (exact: `index: false` + `script_score` cosine; `knn.method: hnsw`: float HNSW without quantisation, candidates from the top-level `knn` search with the scope as pre-filter, then the same exact rescore), and no `index.knn` setting. Same three indices per generation, same dataset aliases switched atomically by `activate(gen)`, same typed-field pushdown, lexical and records semantics as the section below. `generation_registry()` (what the write path needs to own `storage.catalog`) keeps manifests, failure records and the ingestion lock in `<index_prefix>__registry` and resolves `CURRENT` through the `docs` alias. |
| `neo4j` (extra `neo4j`, official driver) | `graph` (with `memberships`) | `uri`, `user`, `password_env` (environment variable holding the password; never the password itself), `database`, `namespace` (default `su_multi_arm`; letters, digits, `_`, `.`, `-`), `timeout_seconds` (connection, pool acquisition and per-transaction timeout). Fixed labels; every node and edge carries the generation key `g = <namespace>\|<dataset>\|<generation>`, so namespaces and generations never touch each other. Mentions are `SuGraphMention` nodes (span offsets, quote, match level, canonical id, aliases), entities `SuGraphEntity` nodes, relations one relationship type per edge kind (`SU_FACT`, `SU_CO_OCCURRENCE`, ...) carrying predicate, `edge_kind`, `support`, status, span, provenance and every occurrence; communities are `SuGraphCommunity` nodes with their report, memberships `SuGraphMembership` nodes. Loads are `MERGE`-idempotent and one explicit transaction per call (a dangling relation endpoint or any server error rolls the whole call back; never retried). Traversal runs only fixed parameterised Cypher templates, one statement per hop, `fact` edges unless other kinds are requested, the hop bound a validated integer 0..3. Eligibility is pushed down as a document-id allow/deny list only (`predicate_scope: false`): the graph and community arms first resolve request filters and ACL predicates to document ids through the catalog (at most 10000 eligible documents; more is a typed error, never a truncated list). `<namespace>\|<dataset>` has one `SuGraphPointer` node naming the active generation; the write path moves it just before the authoritative `CURRENT` switch, moves it back if that switch fails, and a failed load never reaches it; `delete_generation` refuses the generation it names. Needs Neo4j 5.23 or later (scoped `CALL` subqueries); tested on 5.26 community. No catalog: `storage.catalog` must name another backend. |

An adapter may be named only for the capabilities it serves: naming `opensearch` or
`elasticsearch` for `ledger`, `sessions`, `cache`, `communities` or the `graph`/`community` arms,
`mongodb` for `vector`, `postgres` for `graph`/`community`/`storage.communities`, `sqlalchemy` for
`lexical`/`dense`/`graph`, or `neo4j` for anything but `graph`/`community`/`storage.communities`,
fails validation ("does not declare capability" or "status: planned"). Every adapter on the
contracts runs the same conformance kit (`su_multi_arm.testing.backend_conformance`,
`tests/conformance/`; the sqlalchemy suite runs on SQLite on every push; the mongodb, opensearch,
elasticsearch, neo4j, postgres and sqlalchemy-on-PostgreSQL suites are `external` and need the
service stack in `tests/services/`). A postgres or sqlalchemy backend whose DSN/URL variable is unset
fails engine construction with a typed error; no connection is opened before the first operation. Any
projection may live on any backend that serves its capability (next section). Vector
search is exact unless an adapter documents an explicit opt-in (`postgres` `vector.index: hnsw`,
Lucene `knn.method: hnsw`). Typed scope (filters, ACL predicates, aggregates) is pushed down into the
backend; no request loads the corpus.

#### OpenSearch and Elasticsearch semantics (per-engine, not parity with FTS5 or PostgreSQL)

- **Lexical**: BM25 over the Lucene `standard` analyzer (`analyzer_id: lucene_standard`; tokenised,
  lowercased, no stemming), `most_fields` over the chunk `text` and the document `title` (boost 2).
  `kind: phrase` is a `multi_match` phrase query: it matches only tokens contiguous *as analysed by
  Lucene*. `kind: boolean` adds `must`/`must_not` phrase clauses. User text is never parsed as query
  syntax (no `query_string`); it is bounded to 128 whitespace-separated words. Hits carry Lucene
  highlight fragments. Ordering is `_score` then `chunk_id`, so results are deterministic; scores and
  ranks are not comparable with other engines (plan §3.5).
- **Vector**: vectors are L2-normalised on write and stored in `knn_vector` fields (one per
  namespace, dimension fixed by the first write, up to 16000). `knn.method: exact` (default,
  plan decision 5) scores every eligible chunk with an exact cosine `script_score`, so results are
  identical to the sqlite/memory adapters for the same vectors and scope. `knn.method: hnsw` (opt-in)
  builds a Lucene-engine HNSW graph (`index.knn: true`), retrieves `max(50, 4k)` candidates through
  the native `knn` query with the eligibility filter applied inside the graph search, then rescores
  those candidates exactly; `exact_rescore: false` is refused (ANN scores are never returned). The
  method is recorded in the chunks index mapping; opening a generation built with the other method
  is a typed error.
- **Eligibility pushdown**: `Scope` (allow-list, exclusions, typed predicates incl. range/date and
  `missing`) compiles to a native `bool.filter` on typed fields denormalised onto every chunk
  (`s.*` keyword with a `lowercase`-normalized `.fold` sub-field for `casefold_match`, `n.*` double,
  `b.*` boolean, `t.*` epoch-millis comparison keys from `backends/_scope.py::temporal_key` (aware
  stamps as UTC instants, naive stamps and date-only strings as UTC, `date` fields as calendar dates
  so a datetime bound truncates to its UTC date; temporal `eq` is instant equality), `p` = non-null
  fields);
  nothing is post-filtered. `casefold_match` therefore uses Lucene `lowercase`, not Python
  `str.casefold` (they differ for a few characters such as `ß`).
- **Records**: the ACL and every supported predicate are pushed down as the same filter; the
  denominator is a `_count`; the matched records are scanned in id order (`search_after`) and the
  measure is computed by the shared `backends/_scope.py` code, so every `ScopeResult` is `exact` and
  identical to the other adapters. A scope matching more than `max_matched` records (default 10000,
  constructor argument) is a typed `BackendError`, never a truncated result; approximate
  `cardinality` aggregations are not used. `values()` is a key-ordered `terms` aggregation capped at
  10000 (`limit` above the cap is an error); `exists()` is a `_count`.
- **Consistency**: catalog `get`/`get_chunks` are real-time; `iter_scope` and the counts refresh the
  generation's indices first; records are written with `refresh=true`; lexical/vector writes become
  visible on `refresh(gen)` (the write path calls it). No retries: an unreachable server, a refused
  query, a bulk item failure, a missing generation or a mapping-version mismatch is a `BackendError`
  naming the operation.

### Selecting a backend per projection

Each generation-scoped projection is written to the backend(s) the configuration selects:

| Projection | Written to |
|---|---|
| catalog (authoritative) | `storage.catalog` — its generation registry holds `CURRENT` |
| records | `storage.records` and `pipeline.arms.structured.backend` (both validated when they differ) |
| lexical | `pipeline.arms.lexical.backend` |
| vector | `pipeline.arms.dense.backend` |
| graph (mentions, relations, memberships) | `pipeline.arms.graph.backend`, `pipeline.arms.community.backend` and `storage.communities` |
| communities | `storage.communities` and `pipeline.arms.community.backend` |

`storage.ledger`, `storage.sessions` and `storage.cache` may name any backend serving them; they are
not generation-scoped (cache keys carry the generation prefix) and take no part in staging,
activation or pinning. Every backend other than the catalog's that holds a projection and serves the
`catalog` capability also receives a **scope mirror** of the generation's documents and chunks:
lexical, vector and graph facets evaluate request filters and ACL predicates against their own
backend's rows, so the mirror is what lets them push scope down. The mirror is validated (counts and
sampled chunks) and never read as the catalog. A lexical or dense arm on a backend that cannot take a
mirror is a `ConfigError` at engine construction. Neo4j (no catalog) evaluates document-id lists only,
so the arms resolve predicates through the authoritative catalog first. A catalog needs a generation
registry: `opensearch` cannot be `storage.catalog` yet (typed `ConfigError`); `elasticsearch` can.

Example: catalog, records, dense and the runtime stores on PostgreSQL, lexical on OpenSearch, graph
and communities on Neo4j (the combination exercised by `tests/services/test_multi_backend_engine.py`):

```yaml
schema_version: 2
dataset: demo
backends:
  pg:    {kind: postgres, dsn_env: SU_POSTGRES_DSN, table_prefix: demo}
  os:    {kind: opensearch, hosts_env: SU_OPENSEARCH_HOSTS, index_prefix: demo}
  graph: {kind: neo4j, uri: bolt://127.0.0.1:7687, password_env: SU_NEO4J_PASSWORD, namespace: demo}
storage: {catalog: pg, records: pg, ledger: pg, sessions: pg, cache: pg, communities: graph}
pipeline:
  arms:
    dense:      {backend: pg}
    lexical:    {backend: os}
    structured: {backend: pg}
    graph:      {backend: graph}
    community:  {backend: graph}
```

A local variant with the search projections in their own sqlite directory and the runtime stores in
another:

```yaml
backends:
  local:   {kind: sqlite, path: ./state/catalog}
  search:  {kind: sqlite, path: ./state/search}
  runtime: {kind: sqlite, path: ./state/runtime}
storage: {catalog: local, records: local, ledger: runtime, sessions: runtime, cache: runtime, communities: local}
pipeline:
  arms:
    dense:   {backend: search}
    lexical: {backend: search}
```

**Activation and pinning.** After every projection validates, the secondary backends are switched
first, each through its own mechanism (OpenSearch/Elasticsearch: the dataset aliases; Neo4j: the
pointer node; sqlite, memory, PostgreSQL, SQLAlchemy, MongoDB: their own registry's `CURRENT`
file/row/document, with the manifest written next to it), in projection order (vector, lexical,
records, graph, communities); the catalog's `CURRENT` moves last. If any step fails, every pointer
already moved is put back to what it named before (or removed on a first build), `CURRENT` is left
unchanged and the failure record carries `category: activation:<backend>`, `compensation` (per
backend, restored or not), `staged` (every `(backend, projection)` pair written) and
`left_for_cleanup`. Every request pins `CURRENT` and checks that each secondary pointer names the
same generation; a disagreement (a compensation that could not complete, an alias switched by hand)
refuses the request with `GenerationDisagreement` naming each backend's generation — never a mixed
read. A generation built before this release has no secondary pointers; rebuild it
(`ingest --replace`) once a projection lives off the catalog's backend.

The manifest records, under `generations`, the `activation_order` and per backend: `kind`,
`projections`, `role` (`authoritative` | `secondary`), `scope_mirror`, `activation`
(`current` | `pointer` | `registry` | `none`), the generation key, the backend's physical `identity`
(schema, index names and aliases, collection prefix, table token, Neo4j key and pointer, snapshot
folder) and, for the lexical holder, `analyzer_id` (`pg_ts_<config>` as recorded for the generation,
`lucene_standard`, `fts5_unicode61`, `mongo_text_english`); `projections.lexical.analyzer_id`
repeats it. `Engine.write_path.delete_generation(generation)` removes a generation from every
backend that held it, refusing with `GenerationInUse` (nothing deleted) the `CURRENT` generation,
one a secondary pointer still names, or one protected (pinned) on the first backend asked; later
per-backend errors are returned in `left_for_cleanup`. The manifest stays as the audit record.

### Generations, ingestion policies and partial builds

Ingestion stages a new generation through every projection on every backend that holds one,
validates each target (row counts against the catalog, a sample of ids round-tripping through
catalog, scope mirrors, records, lexical and vector search, graph memberships and sampled entities,
community member lists against the memberships) and only then activates (above). A failed build leaves the old generation active, removes the staged
generation and writes `failures/<generation>.json` naming the failing unit, its category and any
projection left for cleanup; `status` lists them.

`extraction.policy` decides what a rejected extraction unit does (plan §3.14.1):

| Policy | Behaviour |
|---|---|
| `strict` (default) | the first failing chunk, document or report aborts the build |
| `quarantine` | a document whose chunk extraction fails (grounding, endpoint, schema, report grounding) stays in the catalog, lexical, dense and structured projections; **all** of its extraction outputs are excluded from the graph, community and report stores; its status records `extraction: rejected:<category>` and the manifest carries `coverage.extraction = {accepted, rejected_by_category, documents_without_graph}` |
| `partial` | as `quarantine`, plus a stage stopped by a provider or budget failure (LLM extraction, embeddings, community reports) is recorded as `coverage.stages.<name> = incomplete{processed, remaining, reason, first_unprocessed}`; activation needs `ingest --allow-partial` (`Engine.ingest(..., allow_partial=True)`); arms that depend on an incomplete stage refuse to run unless `pipeline.allow_incomplete_arms: true`, in which case their results and the answer trace carry `coverage.incomplete: true` |

No policy drops a unit silently or turns a rejected relation into a fact. Provider or budget failures
are never quarantined under `strict` or `quarantine`. Validated extraction outputs and every raw
model reply (valid or not) are kept in the ledger, so an unchanged chunk is reused across builds
without inference (`reused_extractions` in the manifest). Embeddings are reused the same way: a
chunk whose exact embedded input (title, field name and text) and embedding recipe (provider, model,
revision, dimensions, endpoint, `document_prefix`, normalisation) were embedded before is not sent
to the embedder again (`embedded` / `reused_embeddings` in the manifest). A title change re-embeds
that document's chunks; a recipe change re-embeds everything. The manifest's `incremental` entry
states what is still rebuilt on an upsert (catalog, lexical, records, vector rows, graph,
communities and community reports).

#### Ingestion budget (`budgets.ingest`)

```yaml
budgets:
  max_llm_calls: 12            # per request (search/answer), never applied to ingestion
  price_table: prices.yaml     # required for any max_usd
  ingest:                      # one whole build (ingest, upsert, the ingest_delta action)
    max_llm_calls: 10000       # extraction + community-report calls; null = no cap
    max_embedding_calls: null  # embedding provider requests (one per batch_size window with a new input)
    max_usd: null              # LLM + embedding spend of the build
```

Every LLM call on the write path reserves one call, its input estimate, the `max_tokens` ceiling and
(when priced) the USD of that estimate before it is sent, and commits the provider-reported usage
afterwards; an embedding request reserves one call and the USD of a chars/4 input estimate. Local
embedders (`hash`, `sentence_transformers`) cost nothing; a hosted embedder is priced by
`models.roles.embedder.pricing_key` (default: its model id). With `ingest.max_usd` set, every role
that can spend on the write path (the LLM extractor, the community role, a hosted embedder) must be
priced in the table — otherwise engine construction fails with `ConfigError`. Exhaustion stops the
stage with category `budget` (the build fails under `strict`/`quarantine`; under `partial` the stage
is `incomplete` and needs `--allow-partial`); nothing is downgraded or retried. The manifest's
`budget` entry reports the limits, LLM calls, embedding calls, committed USD and any exhaustion.

**A hosted embedder needs an explicit cap (2.0.0a2).** When the dense arm is enabled and the
embedder bills per call (`provider: openai`, or `openai_compatible` on a host other than
`localhost`/`127.0.0.1`/`::1`), engine construction fails with `ConfigError` (`budgets.ingest`)
unless `budgets.ingest.max_usd` (with `budgets.price_table`) or `budgets.ingest.max_embedding_calls`
is set. There is no implicit default cap.

**Upgrade from v1.** v1 had one `max_llm_calls`, used both per request and as the cap of a whole
build. `config upgrade` maps it to `budgets.max_llm_calls`; when the v1 file sets it explicitly it is
also written to `budgets.ingest.max_llm_calls` (the build keeps the cap the operator chose); an unset
v1 value leaves the v2 ingest default (10000).

#### Concurrent writers

One build at a time holds a dataset's write lease; a second `ingest` is refused immediately with
`WriteConflict` (`write lock is held by another writer`) and should be retried after the first one
finishes. A build whose lease was lost (its process stalled past the lease expiry and another writer
took over) or whose starting generation is no longer `CURRENT` fails with `IngestConflict`
(failure category `write_conflict:lost|stale_base`) and never activates; retry it.

## Input mapping and redaction

Ingest canonical JSONL, or a JSON array. See [documents.jsonl](../examples/documents.jsonl).
For other schemas use exact field mapping; [mapping.yaml](../examples/mapping.yaml) demonstrates a
CRM-style export with dotted and double-underscore field names. Do not confuse the human title with an issue summary. Map every relevant text
field, especially Details and resolution, instead of selecting only a digest. Dotted source keys are
literal keys, not nested JSON paths. Nested records require a normalization step first.

```bash
su-multi-arm --config examples/mapping.yaml ingest examples/mapped_documents.json
```

Missing configured text fields become empty strings and show up in the status report. IDs are
required. URLs must be absolute HTTP(S) or null; a relative URL is rejected so a connector must resolve
it against a known, verified original-source base URL. The package does not invent source links.

`privacy.redact_emails: true` masks emails in title and text before storage. `privacy.patterns`
accepts reviewed regex patterns by label; `privacy.metadata_fields` selects metadata eligible for the
same rules. IDs, URLs and unselected metadata are preserved. This is not full PII detection. Do not
add broad digit/identifier rules that destroy error codes, tax rates, county IDs or technical field
names. Review redacted samples and the manifest's counts. Patterns themselves are trusted config.

## Extraction and communities

`extraction.extractors` combines `gazetteer`, `regex`, `spacy`, `gliner`, `llm` (v1 names
`providers`/`tags` upgrade automatically):

```yaml
extraction:
  extractors: [gazetteer, regex]
  gazetteer:
    country: [Canada, United States]
    state: [California, Texas]
    county: [Orange County, Travis County]
  patterns:
    error: '\bERR-[0-9]+\b'
```

A tag must actually appear in its passage; the package does not infer that an article concerns a
county merely because that county exists. spaCy NER/POS and GLiNER add surface tags; only LLM extraction
adds explicit semantic relations in this release. Extracted names/quotes and relation endpoints must
be grounded. Empty results remain empty, with audit counts. Do not treat this validation as a full
semantic factuality check. GLiNER2 is researched but not supported as a provider yet.

Grounding is span-anchored. A model quote is located in its chunk at the `exact` level or, after the
configured normalisation, at the `normalised` level, or in another field of the same record at the
`relocated` level; the stored span is always verbatim source text at its field offsets. Identical
relations (same endpoints, predicate and span text) across chunks that inherit the same text collapse
into one edge whose `occurrences` keep every source span and provenance. Ambiguous relation quotes are rejected unless `ambiguous: first`. A relation is
stored as a `fact` only when its predicate, or a declared trigger phrase, is evidenced inside the
quote without an intervening negation; otherwise it is kept as a `co_occurrence` edge that the graph
arm does not traverse. Changing any of these settings changes the index fingerprint.

```yaml
extraction:
  grounding:
    normalisation: [nfkc, casefold, whitespace]   # [] means exact matches only
    ambiguous: reject                             # or first
  predicate_triggers:
    hosted: [held in, venue]                      # extra evidence phrases per predicate
  stemmer_language: english                       # any Snowball language
```

Communities use overlapping entity groups by default; `communities.strategy: louvain` clusters the
bipartite document/entity graph with a fixed seed. Minimum document count is configurable. Both
strategies have passage citations. Set `models.roles.community` to enable precomputed claim
summaries; `communities.prompt` and the role's `evidence_budget.chars` configure them. Each claim must cite supplied
passages with valid document IDs, chunk IDs and verbatim quotes. Reports expose omitted chunks and a
partial status when their input budget excludes evidence. These checks validate references, not every
semantic inference. Hierarchical/global map-reduce reporting is not implemented.

## Requests, filters and context

```json
{
  "query": "Find similar cases",
  "filters": {"source": "case", "product": "CloudTax"},
  "record_context": {
    "record_id": "case:certificate",
    "subject": "Certificate expired",
    "description": "California transactions started charging tax"
  }
}
```

`record_context` is optional. Which keys it may carry is configuration, not code: `request.context_keys`
lists the accepted keys (none under the `neutral` profile; the `support_example` profile declares
`record_id`, `record_type`, `subject`, `description`, `product`, `version`, `error_message`, `status`,
`resolution`), `request.context_max_bytes` / `context_max_field_chars` bound its size (8192 bytes and
4000 characters by default) and `request.canonical_fields` names the document fields or metadata
echoed back as the canonical record when `record_id` (or the configured `record_id_key`) names a
stored document. The canonical record is read through the caller's access scope (the same
ACL-scoped catalog read as every other path): an id that is unknown in the active generation or
outside the principal's scope is dropped (it never reaches the planner or the generator), recorded
as `trace.record_context = {"record_id": "unresolved", "used": false}` and reported as an
`unsupported_constraints` entry (`value_not_observed`); the two cases are indistinguishable to the
caller. Canonical local data is passed separately from caller claims; the seed record is excluded
from candidates. `rules` decisions leave the query unchanged, so descriptive context affects
query generation only with an `llm` decision.

HTTP can alternatively use `X-Record-Context` containing the same JSON object. Conflicting body and
header values are rejected. MCP uses the structured argument. No-context calls remain valid.
Explicit request filters cannot be overridden by the planner. Planner step filters must name a
`metadata_schema` field marked `filterable` (or `id`/`source`/`dataset`/`kind`) and only narrow the request.

Request filters are validated against `metadata_schema` before anything executes (search, answer,
stream, the `aggregate` tool, HTTP and MCP alike):

| Filter | Outcome |
|---|---|
| declared field, valid value | applied as typed equality |
| unambiguous coercion: `"3"` on an `integer` field, `"2.5"` on `number`, `"true"`/`"false"` on `boolean`, an integer on a `string`/`id` field | coerced, applied, recorded in `trace.request_filters.coercions` (field and kind only) |
| type mismatch (`"high"` on an integer, `true` on an integer), value outside an `enum` | `UnsupportedConstraint` (`type_mismatch` / `value_not_observed`) |
| a `string`/`id` value on a `listable` field under its `max_catalog_cardinality` that no record holds (a typo, or the wrong case on a case-sensitive field) | applied as given, but reported as `UnsupportedConstraint` (`value_not_observed`); the aggregate is `bounded`, so a zero count or an absence claim on it is refused (2.0.0a2) |
| `datetime`/`date` value that does not parse, or lacks an offset under `require_offset` | `UnsupportedConstraint` (`unparseable_bound` / `timezone_missing`); a valid one is pushed down as given |
| undeclared field, `unknown_fields: allow` (default) | applied as **untyped string equality**, listed in `trace.request_filters.untyped`, and reported as `UnsupportedConstraint` (`field_not_filterable`) |
| undeclared field, `unknown_fields: reject` | `UnsupportedConstraint` (`field_not_filterable`) |

An invalid filter is never dropped (the scope never silently widens): it is applied as given, so it
usually matches nothing, and every aggregate computed under it is reported `bounded` with the
constraint attached (a typed `aggregate` request with such a filter abstains in the decision). An
invalid filter therefore never yields an exact zero, and the answer contract refuses a count or
absence answer built on it.

A structured aggregate request:

```json
{
  "query": "When was the first CloudTax case recorded?",
  "filters": {"source": "case", "product": "CloudTax"},
  "aggregate": {"operation": "first_seen", "field": "created_at"}
}
```

Temporal semantics are instant-based and identical on every adapter: a `datetime` value is stored as a
UTC instant (an explicit offset is converted; under `timezone_policy: assume_utc` a naive stamp is
read as UTC; under `require_offset` a naive stamp is rejected at ingestion), bounds are converted
the same way, and `eq`/`in`/`range`/`date_interval` compare instants, never text. A `date` field
compares calendar dates: a datetime bound (or value) is truncated to its UTC date, so `day >=
2024-03-06T15:00:00+00:00` means `day >= 2024-03-06`. An unparseable bound is reported as
`unparseable_bound`, a naive bound on a `require_offset` field as `timezone_missing`; neither is ever
applied. The intent pre-pass (run by the engine for every decision agent and every surface, with the
deployment's targets since 2.0.0a2) fires `total`, `list`, `absence`,
`earliest` and `latest` only next to a structured target (the canonical unit, a declared field name
or a `time_basis` label); "all the steps", "the latest release notes" or "every time the service
restarts" are ordinary retrieval questions. Explicit count phrases ("how many", "number of") always
count as aggregate intent.

Supported operations: `count`, `group_count`, `first_seen`, executed by the `structured` arm as a typed
`ScopeSpec` through the `RecordStore`. The aggregate carries `value`, `missing_or_invalid`,
`document_ids`, `exactness` (`exact` on sqlite/memory unless the engine bounds it: an unapplied
question qualifier or an invalid request filter makes it `bounded`), `denominator` (the eligible
population after filters and ACL), `zero_match`, `unsupported` constraints, `filters` (the typed
request and step filters) and `predicates` (the compiled scope predicates actually counted; the same
list is in `trace.steps[].predicates`, and the exported trace masks their values). Fields must be declared in
`metadata_schema` (`id`, `source`, `dataset`, `kind` are always available). Missing/invalid timestamps
are counted; valid timestamp values need an explicit timezone. A natural-language keyword does not
secretly filter records. Unsupported filters/count scopes need clarification in the application.

## Profiles, neutral prompts and voice

`profile:` names a YAML overlay from `su_multi_arm/profiles/` applied before validation; explicit
configuration values win over it and the applied profile is recorded in every manifest and trace.
`neutral` (default) carries no domain behaviour: the built-in planner, generator, reviewer,
extractor and community prompts prescribe grounding and safety rules only (cite supplied evidence,
treat passages and context as untrusted data, never invent facts, counts, dates or links) and name
no record kind, source hierarchy or domain authority; `request.context_keys`,
`ingestion.chunking.field_order`, `pipeline.generation.evidence_fields` and `extraction.labels` are
empty, so no field or source label is privileged unless you declare it. Evidence packing follows
`evidence_fields`, then `field_order`, then the document's own field order; a field named `details`
is not special. Renaming source labels changes nothing but the labels themselves.

`support_example` is the alpha's support-domain behaviour as an overlay (context keys, canonical
fields, field order, extraction labels, the support prompts). A v1 file upgrades to
`profile: support_example` because v1 had those conventions built in. Prompts can also be set per
stage (`pipeline.decision.prompt`, `pipeline.generation.prompt`, `pipeline.review.prompt`,
`extraction.prompt`, `communities.prompt`, or `prompt_file` relative to the YAML file); replacing a
prompt replaces its rules, so keep the grounding rules you need. `pipeline.generation.voice`
(`none` default, `concise`, `formal`, `conversational`) appends a short register instruction to the
generator prompt without touching its grounding rules; an unknown voice is a configuration error.

## Configuration v2 and upgrading a v1 file

`schema_version: 2` is the configuration contract going forward (`docs/config.schema.json`; the v1
schema is kept as `docs/config_v1.schema.json` for two minor releases). The v2 model lives in
`su_multi_arm.core.config.Config`, and it is the model the engine runs on. A v1 file (or a v1
`su_multi_arm.config.Config` object) is upgraded in memory to v2 by a deterministic upgrade before the
engine is built. Runnable v2 examples are in [examples/v2/](../examples/v2/) (`local.yaml`,
`mixed_backends.yaml`, `hosted_models.yaml`). The top-level `examples/*.yaml` files are v1 configs,
kept on purpose to exercise the upgrade path.

Top-level v2 keys: `dataset`, `profile` (`neutral` or `support_example`), `plugins` (entry-point names in
the `su_multi_arm.components` group; nothing else can name code), `backends` (named instances with a
`kind`: `sqlite`, `memory`, `mongodb`, `opensearch`, `elasticsearch`, `postgres`, `sqlalchemy`,
`neo4j`), `storage`
(authoritative backend per non-arm capability: `catalog`, `records`, `ledger`, `sessions`, `cache`,
`communities`), `metadata_schema` (typed fields: `string|integer|number|boolean|datetime|date|enum|id`,
with `filterable`, `listable`, `groupable`, `sortable`, `nullable`, `enum_values`, `time_basis`,
`timezone_policy`, `casefold_match`, `max_catalog_cardinality`), `models.roles` (`planner`,
`reviewer`, `extractor`, `generator`, `community`, `embedder`, `reranker`;
each chat role declares `capabilities.structured_output` and `prompt_caching`, and an optional
`pricing_key`), `pipeline` (`decision`, `arms`, `selection`, `review`, `guards`, `generation`,
`concurrency`), `extraction`, `communities`, `ingestion` (`chunking`, `input_mapping`), `sessions`,
`budgets` (`max_llm_calls`, token limits, `max_usd` with an operator-supplied `price_table`,
`deadline_seconds`, the whole-build `ingest` limits), `privacy` (adds `trace_masking`), `request` (context keys and canonical fields)
and `serving`.

Validation is static and fails closed with the dotted key path: unknown keys; an arm or storage slot
naming a backend whose kind does not declare the needed capability, or declares it only as `planned`
in `backends/capabilities.py` (today `postgres` serves everything but `graph`; `sqlalchemy` serves `catalog`, `records`, `cache`,
`ledger` and `session`; `neo4j` serves `graph` with community memberships; `mongodb` everything but `vector`; `opensearch` and
`elasticsearch` `catalog`, `lexical`, `vector`, `records`; `sqlite` and `memory` all eight); a role used by a selected
component that is not configured; `structured_output`, `prompt_caching` or `reasoning_effort`
not declared for the role's `(provider, model)` in `providers/capabilities.py`; `sessions.memory.semantic`; `gliner`/`llm` extraction without
`extraction.labels`; any non-empty `pipeline.guards` / `pipelines.*.guards` (guards are Phase 2: no
guard runtime ships in 2.0.0a1, so a configured guard is refused rather than silently skipped); a
`storage.catalog` backend without a generation registry (`opensearch`); a `max_usd` without `price_table`; a
non-localhost HTTP bind without bearer auth. Any string naming a pickled artifact (`.pkl`, `.joblib`,
`.pickle`, `.dill`) or a `pkg.module:attr` factory is rejected anywhere in the file (model ids with
a tag such as `mistral:latest` and bare artifact file names such as `intent.onnx` are accepted); a classifier
decision agent accepts only an ONNX `artifact` pinned by `sha256` with a manifest, or a `component`
name listed under `plugins`. `pipeline.decision.on_failure` defaults to `fail_closed`.

Upgrade a v1 file:

```bash
su-multi-arm --config examples/local.yaml config upgrade --output local_v2.yaml
su-multi-arm --config local_v2.yaml config validate
su-multi-arm --config local_v2.yaml config schema   # JSON schema; add --v1 for the legacy model
```

The mapping is the table in `compat/config_v1.py::MAPPING` (the v1 built-in support conventions →
`profile: support_example`; `arms.dense numpy|faiss` → the `local` sqlite backend's `vector_engine`; `pgvector` → a `pg` postgres backend; `arms.graph neo4j` → a
`graph` backend; `routing.mode raw|planned|agentic` → `pipeline.decision {kind: rules|llm}` plus
`pipeline.review.enabled`; `fusion.strategy rrf` → `selection.ordering reciprocal_rank`;
`extraction.providers tags` → `extractors gazetteer`; `arms.sql_fields` → `metadata_schema.fields` of
type `string` with a warning to declare real types; `max_llm_calls` → `budgets.max_llm_calls`, and also `budgets.ingest.max_llm_calls` when set explicitly).
Loading a v1 file with the v2 loader emits one warning listing the applied mappings. Settings v2
cannot carry (a `provider: none` model block with non-default fields, an `evidence_char_budget` for
a stage without a model) are reported as `lossy_upgrade` warnings — never dropped silently — and
`config upgrade --strict` refuses them. Paths in a config are relative to the YAML file, so
`config upgrade --output` rewrites every relative path (`backends.<sqlite>.path`, `prompt_file`,
`alias_map`, `price_table`) for the output file's directory and lists them in a header comment;
absolute and `~` paths are kept. Printed to stdout (no `--output`) the paths stay as written, so save
that output next to the v1 file. The upgrade is
deterministic, and `compat.config_v1.downgrade(upgrade(v1))` reproduces the v1 `index_fingerprint()`,
so an upgraded config can still open its existing v1 generation (checked for every shipped example).

### `pipeline.decision`, `pipeline.selection`, `pipeline.review`, `pipeline.generation.contract`

`pipeline.decision` chooses the decision agent (`kind: rules | classifier | llm | cascade`). A
deterministic **intent pre-pass** always runs first (English-only grammar: count / total / list /
absence / earliest / latest, exact identifiers from the alias-map patterns, quoted phrases,
follow-up markers) and its result reaches every agent and the answer contract. `rules` turns an
aggregate intent into a typed structured scope only when every qualifier in the question binds to an
observed value of a listable field (or an enum value) or to a request filter; anything left over is an
`UnsupportedConstraint` and the agent fails rather than counting a wider population. A `total` needs
a declared numeric field named in the question and is never answered with a record count;
`earliest`/`latest` need a declared `time_basis` field. `llm` plans with `models.roles.planner`
through a strict per-arm step schema (only a `structured` step carries a scope), sees observed values
only for listable fields under `max_catalog_cardinality`, and every step is validated (enabled arm,
filterable field, no override of request filters, values resolved through the schema and
`RecordStore.exists`).

Qualifier coverage (every agent, 2.0.0a2): a structured step an agent compiled from the question must
account for every qualifier in it. Candidates are found deterministically (English): an observed value
of a listable filterable field or an enum value that occurs verbatim in the question, a calendar
expression (a year, an ISO date, a month with a day or year, a quarter, `yesterday`/`today`,
`last|this|past|previous <unit>`), a quoted phrase, and a filterable field named next to a value of
its type (2.0.0a2: `priority 3`, `version 2.1`, `severity high`, `priority is 3`, `priority above 2`;
a number for `integer`/`number`, `true`/`false`/`yes`/`no` for `boolean`, a word for `string`/`id`,
an enum value for `enum`; a `boolean` field named alone, "escalated tickets", counts too). A field
qualifier is applied by a predicate or request filter that constrains that field to that value (a
comparison word or a bare boolean field: any predicate on the field). A candidate is applied when a predicate or a
request filter on a field holding that value constrains it to that value (a calendar expression: any
predicate on a declared `datetime`/`date` field; a quoted phrase: a predicate value equal to it). A
structured step on a question without any aggregate operator is flagged as a whole. Every unapplied
candidate is an `UnsupportedConstraint` with reason `qualifier_not_applied`, the step's aggregate is
reported `bounded`, and the answer contract then refuses count, exhaustive-list and absence claims on
it. Nothing is re-planned. Caller-typed scopes (`aggregate` in the request) are the caller's and are
not checked. Limitation: free-text topic words of an aggregate question ("how many CloudTax tickets
mention rounding") are not candidates for `llm` (the `rules` agent refuses residual words itself).

`classifier` is interface-only: it maps a question to one non-structured arm,
never produces a scope, ships without weights and defaults off; it accepts only an ONNX `artifact`
pinned by `sha256` with a `manifest` (`features`, `labels` = arm names, `threshold`) or a plugin
`component`, and an inference runtime must be injected through the Python API. `cascade` runs
`stages` in order and the first `decided` stage wins.

Fail policy (plan §3.7.1): `on_failure: fail_closed` (default; the agent abstains with the failure
category and the flagged constraints) or `raw_all_arms` (explicit opt-in: the raw question on every
enabled non-structured arm, no scope, `decision_fallback: true` in the trace and one unsupported
constraint per detected aggregate intent, so the answer can only be narrative). Inside a cascade,
`stage_policies.{rules,classifier,llm}` is `fallback_to_next_stage` (default for `rules` and
`classifier`) or `abstain` (default for `llm`); when every stage fails the cascade's `on_failure`
applies. Request filters are immutable for every agent; a caller filter is applied as given and a
zero match is reported, not rewritten.

`pipeline.selection` splits today's fusion into stages: `collapse` (`document` default), `per_arm`,
`union: preserve`, `ordering` (`reciprocal_rank` default = the alpha's `per_arm` order, which is
also accepted as a deprecated alias; `rrf`, `minmax`, `zscore`, `best_arm_rank`,
`stable_interleave`), `arm_weights`/`source_weights`, `max_documents` (the only membership cut),
`diversity: {field, min_per_value}` (opt-in; keeps leading slots for every present group value;
short groups contribute what they have and are listed in the trace), `pins` (`exact_id` records to
the front) and `packing: {budget_chars, field_priority, fairness: fair_share | greedy,
min_per_record}`. Packing renders record fields in the configured `field_priority` order and then in
document order — there is no built-in `details`/`resolution` preference. Ordering never changes
union membership.

`pipeline.review` (`enabled`, `max_hops`) runs the reviewer role between selection and generation.
Stop reasons are `sufficient`, `no_actionable_gap`, `repeated_query`, `hop_limit`,
`budget_exhausted` and `replan_abstained`; a follow-up query is re-planned through the configured
decision agent with the same filters, ACL and pinned generation, and a re-plan that is not
`decided` (including `raw_all_arms`) stops the loop. New hits merge with one vote per record per arm.

`pipeline.generation.contract.on_violation` (`abstain_with_reason` default, `strip`, `fail`) governs
the answer contract: every citation must be supplied evidence; `count`, `exhaustive_list` and
`absence` claims need an aggregate evidence whose `ScopeResult` is exact (absence also exhaustive and
empty: a zero count, an empty list); under an aggregate intent with no exact scope the answer may not
carry a count or exhaustive-list claim and `unsupported_constraints` is non-empty; a factual answer
needs citations and non-empty text.

The **answer text** is checked too (2.0.0a2). A deterministic, English-only detector finds count
statements (a number followed within three content words by a unit noun — the canonical unit, the
generic record words, common record nouns such as tickets/cases/issues, and the nouns after "how
many"/"number of"/"list all" in the question — or "a total of N"), exhaustive-list statements
("complete/full list", "these are all the <units>", "the only <units>", "no other <units>"; under a
`list` intent also "are as follows" and an enumeration of two or more items) and absence statements
("there are no <units>", "no <units> were found", "none of the <units>", "zero <units>"). Numbers that
are part of identifiers, versions, dates, times, decimals or percentages, numbers separated from the
unit by a function word ("2 decimal places for invoices"), and number-and-unit phrases quoted verbatim
from a supplied passage are not statements. Every statement needs a typed claim of the same kind (an
absence statement may use a `count` claim) backed by an exact scope; otherwise the violation is
`answer_text_without_exact_scope`. A backed count statement must also state the value of the exact
aggregate its count claim cites (a scalar value, a group count, or the population after "of" as in
"12 of 36 records"); a different number is `answer_number_mismatch`. Precision exclusions: a number
other than one directly before a singular unit is a label ("a 404 error"); a number right after a
product-like capitalised name is a version ("Windows 10 users"); a unit followed by a head noun it
modifies is not the counted population ("there is no error message"); "the only <unit>" needs a
plural unit; "complete list of <noun>" needs `<noun>` to be a unit. Answers in other languages are
checked on their typed claims only.

Violation handling: `abstain_with_reason` returns `insufficient_evidence: true` with the violated
rules. One exception keeps narrative answers usable with small models: when the answer text states no
count, list or absence, a non-narrative claim the rules reject is **stripped** from `claims` (its
citations too unless still used) and the narrative answer is returned with `contract: stripped` and the
violation recorded with `action: strip`. `strip` additionally removes the sentences that carry an
unbacked statement (abstaining when nothing remains). `fail` raises on any violation. Unknown evidence
ids, missing citations and an empty answer always abstain. With native structured output the generator
schema is narrowed per request: cited ids are an enum of the supplied evidence ids, and non-narrative
claim kinds are offered only when aggregate evidence was supplied.

`unsupported_constraints` is populated by the engine from the decision, the request-filter validation
and the scope results — the generator cannot set it. `AnswerResult` keeps the v1 fields and adds
`unsupported_constraints`, `coverage` (from the pinned generation's manifest under
`quarantine`/`partial`), `violations` (`rule`, `detail`, `action`) and `contract` (`passed` |
`stripped` | `abstained`); `SearchResult` adds `unsupported_constraints`. Both traces carry `decision` (agent,
outcome, policy, failure category, `decision_fallback`), `selection` (effective policy), `reasons`
per record, `model_calls` and, for answers, `review` rounds, `evidence` packing and `contract`.

The effective packing priority is `selection.packing.field_priority`, then
`pipeline.generation.evidence_fields` (what v1 `generation.evidence_fields` and the
`support_example` overlay set), then `ingestion.chunking.field_order`, then the document's own
field order. The character budget is `models.roles.generator.evidence_budget.chars` when set, else
`selection.packing.budget_chars`.

### Named pipelines and per-request overrides (`pipelines`, `default_pipeline`, `request`)

`pipeline:` is the pipeline named `default`. `pipelines:` adds named variants; each entry is an
overlay deep-merged onto `pipeline` before validation (set an arm to `null` to disable it there), so
it can change the decision agent, arms, selection, review, guards and generation. A profile overlay
may contribute `pipelines` like any other key; explicit config values win.

```yaml
pipeline:
  arms: {dense: {backend: local}, lexical: {backend: local}, structured: {backend: local}}
pipelines:
  lexical_only: {arms: {dense: null, structured: null}}
  planned: {decision: {kind: llm}, review: {enabled: true, max_hops: 2}}
default_pipeline: default          # or a named pipeline
request:
  pipelines: [lexical_only, planned]   # what SearchRequest.pipeline may select
  modes: [llm]                         # what SearchRequest.mode may select (rules|classifier|llm|cascade)
```

`SearchRequest.pipeline` and `SearchRequest.mode` (also CLI `--pipeline`/`--mode`, MCP tool
arguments) select a pipeline and override its decision kind. The default pipeline and a pipeline's
own decision kind are always allowed; anything not in the allowlists is a `RequestRejected` error
(HTTP 422), never ignored. `mode: rules|llm` drops cascade stages; `classifier` needs the pipeline's
own artifact/component; `cascade` is only the pipeline's own cascade. Traces record `pipeline` and
`mode`.

Validation runs every check and reports **all** problems at once (`ConfigError.diagnostics`, path of
the first in `ConfigError.path`). Schema-level problems (unknown keys, wrong types, values out of
range, unimplemented keys) are aggregated the same way, one `Diagnostic` per pydantic error with its
key path. The CLI prints one `error: ...` line (for configuration errors one indented
`<path>: <message>` line per problem) and exits 2 for an invalid configuration, 3 when the index is
not ready and 1 for any other failure; `--debug` shows the Python traceback instead.
Keys for features that do not exist in this release are refused rather than accepted and never read:
`extraction.ontology` (Phase 3), `models.roles.judge` (Phase 4), `models.roles.cache_gate` and
`pipeline.arms.cache` (Phase 2), `sessions.memory.semantic`, and non-empty `guards`. The semantic
checks are: backend kinds available; every `storage` slot names a backend that
serves it; every arm of every pipeline names a backend whose adapter serves the arm's capability;
model roles a pipeline uses are configured; guards, tools, actions and plugin components exist in a
registry; named pipelines use the `dense`/`lexical`/`graph`/`community` backends of the default
pipeline (the write path projects only those — a different backend is `projection_mismatch`, an arm
the default lacks is `projection_missing`); `request.pipelines` exist and every `request.modes` entry
is buildable on every selectable pipeline; the budget pays for one pass (`budget_incoherent`) and a
warning when review rounds may stop with `budget_exhausted`. At engine construction every
component's `validate()` of every pipeline runs, again aggregated.

### Sessions, memory and budgets (`sessions`, `budgets.session`)

```yaml
sessions:
  store: local            # a backend declaring `session`; default storage.sessions
  ttl_seconds: 3600
  memory: {episodic: true, store_text: false, exclude_shown: false}
budgets:
  max_llm_calls: 12       # per request
  session: {max_llm_calls: 40, max_input_tokens: null, max_output_tokens: null, max_usd: null}
```

`Engine.create_session(principal)` returns an id; `search`/`answer(..., session_id=...)` (CLI
`--session`, `session create|show|close`) binds the request to it. A session belongs to
`(principal, dataset)`: another principal gets `AccessDenied`, an unknown/expired/closed id
`SessionNotFound` — never a fresh session. Each turn stores the query hash, the shown record IDs,
the answer id (the trace id), the trace id and the generation (`store_text: true` also keeps the
query text). Each request pins one generation; after a switch the session continues, shown IDs
missing from the new generation are dropped, and the answer trace reports `session.generation_changed
{from, to}` and `session.dropped_ids`. `exclude_shown` adds already-shown IDs to `exclude_ids`
(listed in `session.exclusions_added`). Memory is never evidence: it never reaches the generator,
and a citation of an ID that is not in the current turn's evidence is an `unknown_evidence`
contract violation. `budgets.session` limits the sum of all turns (calls, tokens, USD — USD needs
`price_table`); each request's limits are clipped to what is left and an exhausted session refuses
the turn with `BudgetExhausted` before anything runs. `sessions.memory.semantic` is rejected.

Concurrent turns on one session (HTTP and MCP serve concurrently) are safe. Every session write is
a compare-and-set on a `version` in the stored record (all five session stores: memory, sqlite,
mongodb, postgres, sqlalchemy); a turn's usage and its turn entry are merged into the freshest record,
never written from the snapshot read at turn start. With `budgets.session` set, the clip is also a
**hold**: the turn's clipped request limits are reserved on the session record in the same
compare-and-set, so racing turns see each other's holds and together never exceed the session
budget. At turn end the hold is replaced by the actual usage. A hold of a turn that never finished
(a stream that was never read, a killed worker) stops counting after the request deadline plus 300 s.
Because a hold is the whole clipped request limit, set per-request `max_*` limits below the session
limits if turns of one session should run in parallel; otherwise later turns are refused with
`BudgetExhausted` while earlier ones run.

### Tools and actions (`tools`, `actions`)

```yaml
tools: {enabled: [search, get_document, list_values, aggregate]}   # default: all four
actions:
  allow: [ingest_delta, invalidate_cache]   # default: none
  audit_retention_seconds: 604800            # idempotency window
serving: {actions: {allow: [invalidate_cache]}}   # remote surfaces; must be a subset of actions.allow
```

Tools are read-only built-ins run by `Engine.call_tool(name, args, principal, session_id)` (CLI
`tool NAME --args JSON`) in the caller's context (pinned generation, ACL, budget, trace span
`tool.<name>`): `search` (same pipeline as `Engine.search`), `get_document` (exact ids within the
access scope; unknown and invisible ids are both reported as `missing`), `list_values` (only
`listable` fields; `truncated: true` instead of silently cutting), `aggregate` (typed `ScopeSpec`
on declared fields; returns exactness and denominator). Config can only name these registered
classes — there is no code loading. Actions are writes, run by `Engine.run_action(action, args,
idempotency_key=..., principal, surface)` (CLI `action NAME --args JSON --idempotency-key K`): only
names in `actions.allow` are registered (anything else is `denied`), HTTP/MCP additionally need
`serving.actions.allow` and the principal's `actions` scope, and every call — denials included — is
audited on `storage.ledger` (job `audit:<dataset>`) with a `started` record written before execution
and the final status after it; a repeated `idempotency_key` in the window is `skipped_duplicate`.
Built-ins: `ingest_delta` (`{documents: [...], delete_ids: [...]}` through the single write path; it is an upsert into a new generation: embeddings and extractions of unchanged chunks are reused, every other projection incl. graph, communities and community reports is rebuilt — see the manifest `incremental` entry; outputs add `embedded`/`reused_embeddings`)
and `invalidate_cache` (the active generation's cache entries). `promote_generation` /
`rollback_generation` arrive with release ops (Phase 2).

### `exact_id` arm (identifier lookups)

```yaml
metadata_schema:
  fields:
    - {name: external_id, type: id, pattern: '\bREC-\d{4}\b', listable: false, max_catalog_cardinality: 0}
pipeline:
  arms:
    exact_id: {alias_map: aliases.yaml, alias_fields: [external_id], casefold: false, max_ids: 20}
  selection: {pins: [exact_id]}   # default
```

`aliases.yaml` is `{aliases: {"Project Phoenix": doc-17, PHX: doc-17}}` (YAML or `.json`; at most
10000 entries; an alias mapping to two ids is an error). Identifiers are exactly the matches of the
`pattern` of `id` fields and of the alias keys; both populate `Capabilities.id_patterns`, so the
intent pre-pass detects lookups and the `rules` agent plans an `exact_id` step. Each identifier
resolves through the alias map, as a document id, and through `alias_fields` (an `eq` predicate on
the record store under the ACL); the result is confirmed inside the request scope (ACL, filters,
exclusions) on the pinned generation. Matches are pinned to the front by `selection.pins` (reason
`pin:exact_id`). Unmatched identifiers — including ones that exist only outside the caller's scope —
are reported in `trace.exact_id.unknown` and as `unsupported_constraints` (`value_not_observed`),
never fuzzily matched; only `casefold: true` relaxes case. An `exact_id` arm without any id pattern
or alias map is a validation error.

### `doctor --probe`

`su-multi-arm --config c.yaml doctor --probe` constructs the engine (every validation above), calls
`probe()` on every backend (sqlite: runtime database and SQLite version; memory: in-process;
mongodb, postgres, sqlalchemy, neo4j: server ping and version; opensearch, elasticsearch: server identity) and reports each chat role's
declared capabilities next to its registry entry and, per role, `strict_schemas`: `ok` or the
strict-form problems of the schemas a native OpenAI role sends (a problem fails the probe). It makes
**no model calls** (`model_calls: 0`) and exits non-zero when a backend probe fails.

## Serving

```bash
su-multi-arm --config examples/local.yaml serve --port 8099
curl -s http://127.0.0.1:8099/v2/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"exemption certificate"}'
curl -sN http://127.0.0.1:8099/v2/answer/stream \
  -H 'Content-Type: application/json' \
  -d '{"query":"exemption certificate"}'
```

```yaml
serving:
  http: {bind: "127.0.0.1:8099", auth: none, page_size_max: 50}  # auth: bearer_env + token_env off localhost
  mcp: {transports: [stdio, streamable_http]}                    # default [stdio]
  actions: {allow: [invalidate_cache]}                           # remote actions; subset of actions.allow
models:
  roles:
    generator: {provider: openai_compatible, model: my-local-model, base_url: "http://127.0.0.1:11434/v1",
                capabilities: {streaming: true}}               # token deltas only when declared
```

HTTP routes (request bodies are the Python models; `/search` and `/answer` stay as the v1
compatibility router):

| Route | Scope | What |
|---|---|---|
| `POST /v2/search`, `POST /v2/answer` | `search` / `answer` | `SearchResult` / `AnswerResult` |
| `POST /v2/answer/stream` | `answer` | server-sent events, see below |
| `POST /v2/sessions`, `GET`/`DELETE /v2/sessions/{id}` | any | session of the calling principal |
| `GET /v2/tools`, `POST /v2/tools/{name}` | `tools` | enabled read-only tools; body = the tool's input model |
| `POST /v2/actions/{name}` | `actions` | `{args, idempotency_key, reason}`; needs `serving.actions.allow`; audited; 403 when denied |
| `GET /v2/values/{field}?limit=` | `search` | distinct values of a `listable` field within the ACL scope; `limit` ≤ `page_size_max` |
| `GET /v2/capabilities` | `search` | dataset/generation, pipelines and modes, fields and flags, tools, remote actions |
| `GET /health` | none | liveness only |
| `GET /ready` | none | probes every backend, reports the provider capability pairs and the active generation; 503 when a probe fails or no generation is active (probe error text stays in `doctor --probe`) |

Headers: `X-Session-Id` binds search/answer/tool calls to a stored session of the principal (404
unknown/expired, 403 another principal's); `X-Record-Context` (JSON) must agree with the body.
Failures: 401, 403, 404, 409 conflict (`{"detail": {"error": "write_conflict", "reason": "held" |
"lost" | "stale_base" | "not_owner"}}` when another writer holds or took the dataset's write lease, or
`{"error": "session_conflict", "reason": "cas_attempts_exhausted"}`), 422 (request rejected or
invalid), 429 budget exhausted, 499 run cancelled,
502 model provider failure, 503 backend failure, generation disagreement or no usable index
(nothing ingested, v1-built generation, recipe fingerprint mismatch), 504 deadline. Every failure
other than a request-shaped 422 gets a generic message: provider, source and config text never
reaches the client. The SSE `error` event uses the category `unavailable` for the 503 cases. A
failed action (`POST /v2/actions/{name}`) answers with its category's status (409 conflict, 422
invalid arguments, 429 budget, 502 provider, 503 backend, 500 otherwise) and `result.category` /
`result.reason`; the exception text stays in the audit record. MCP tools report the same conflicts
as `write_conflict: <reason>` / `session_conflict: cas_attempts_exhausted` tool errors.

**Streaming.** `/v2/answer/stream` (and `Engine.answer_stream` in Python) emits, in order:
`decision` → `arm_result`… → `review_round`… → `evidence` → `token`… → `contract` → `done`
(the full `AnswerResult`, identical to `/v2/answer`), or a terminal `error` (`category` +
generic message). Each SSE message has `id` = sequence number, `event` = name and `data` = the event
JSON; `: heartbeat` comments are sent while waiting (15 s). `token` events are **provisional**: raw
fragments of the generator's reply (JSON text in structured-output modes), sent before validation
and before the `AnswerContract`; the `contract` event says whether the contract passed or abstained
and that streamed tokens were superseded, and only `done` is authoritative. Tokens are streamed only
when the generator role declares `capabilities.streaming: true` (anthropic, openai,
openai_compatible); otherwise, and for `provider: none`, the stream has no `token` events. A client
disconnect cancels the run: the run's budget refuses further reservations, the provider stream is
closed and the session turn records the usage spent so far.

**Authentication** is a `PrincipalResolver` hook (`su_multi_arm.serving.auth`). Built in: no auth on
loopback (principal `anonymous` with `search`/`answer`/`tools`), or one bearer token read from the
variable in `serving.http.token_env` (`auth: bearer_env`) or `serve --token-env` (principal `bearer`
with `search`/`answer`/`tools`/`actions`). The scheme is case-insensitive (`Bearer`, `bearer`; RFC
9110 §11.1); the token is compared exactly, in constant time. The CLI requires a token when binding outside localhost;
deploy TLS and per-user controls in front. OAuth2/OIDC, mTLS or per-client API keys stay outside the
core: implement `resolve(headers) -> Principal` (raise `Unauthenticated` to refuse) and pass it as
`create_app(engine=..., principal_resolver=...)` or `create_mcp(engine=..., principal_resolver=...)`;
configuration never names a resolver class.

Ingestion is CLI/Python only (or the audited `ingest_delta` action when explicitly allowlisted).
`doctor` reports local index/config compatibility; `doctor --probe` and `/ready` probe every backend
but never call a model, so they do not prove model access.

**MCP.** stdio: configure the command `su-multi-arm` with arguments
`["--config", "/absolute/path/to/config.yaml", "mcp"]`. Streamable HTTP:
`su-multi-arm --config cfg.yaml mcp --transport streamable_http --port 8100` (endpoint `/mcp`;
`--token-env` required off localhost; the transport must be listed in `serving.mcp.transports`).
Tools: `search`, `answer` (exactly the `SearchRequest` fields — `query`, typed `filters`,
`exclude_ids`, `record_context`, typed `aggregate`, `pipeline`, `mode`; `answer` sends the stream
events as progress notifications when the client passes a progress token), `get_document` and
`aggregate` (when enabled in `tools.enabled`), `list_values`, `session_get`, `session_delete`,
`capabilities`. Every call runs in an engine session bound to the MCP session id (one per stdio
process) and the calling principal; `session_delete` closes it and the next call opens a new one.
The server refuses to start if a tool schema drifts from the model it mirrors. This is not the
existing production HTTP gateway on port 7007.
