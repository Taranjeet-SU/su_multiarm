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
| `kuzu` | embedded Kuzu (`kuzu>=0.11,<0.12`) for the `kuzu` graph backend; no service |
| `elasticsearch` | official Elasticsearch 8.x client for the `elasticsearch` backend kind; server is external |
| `semantic` | sentence-transformers embedding and cross-encoder support |
| `spacy` | spaCy; separately install a licensed language pipeline |
| `gliner` | GLiNER; downloads the configured model on first use |
| `communities` | NetworkX Louvain |
| `patterns` | `google-re2` for the `pattern` extractor's `re2` engine (linear-time matching, `Set` prefilter) |
| `patterns-lite` | the `regex` package for the `pattern` extractor's development `regex` engine |
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
| `none` (default except below) | schema appended to the system prompt; reply validated strictly | every provider |
| `json_mode` | `response_format: {type: json_object}` + prompt schema | `openai`, `openai_compatible` |
| `json_schema` | `response_format: json_schema` with `strict: false` + prompt schema | `openai`, `openai_compatible` |
| `native` | Anthropic: `output_config.format` (`claude-haiku-5*`, `claude-sonnet-5*`) or one forced tool whose `input_schema` is the schema (other Claude 3+ families); OpenAI: `json_schema` with `strict: true` (schema converted to strict form) | registry-declared pairs only: Anthropic `claude-3*`, `claude-{sonnet,opus,haiku}-4*`/`-5*`; OpenAI `gpt-4o*` (not `gpt-4o-2024-05-13`), `gpt-4.1*`, `gpt-5*`, `o1*` (not `o1-mini`/`o1-preview`), `o3*`, `o4-mini*`; never `openai_compatible` |

**Anthropic defaults (2.0.0a3).** An `anthropic` role that does not set `capabilities.structured_output`
gets `native` when the registry declares the model capable (every Claude 3 and later family), and
`none` otherwise; set `structured_output: none` explicitly to keep prompt mode. A validated role
always carries the resolved value (a config dump writes it). The registry decides the wire form:
`output_config.format` for Haiku/Sonnet 5.x (a JSON-schema-constrained text reply; the wire schema
drops `minimum`/`maximum`/`exclusiveMinimum`/`exclusiveMaximum`/`multipleOf`/`maxItems` and
`minItems` above 1, which the API refuses, and closes every object; the reply is still validated with
the full schema; a free-form mapping is refused with `StrictSchemaError` before any call), forced tool
use elsewhere (`$defs`/`$ref` are sent as they are; the real API accepts them).

**Oversized schemas and `output_config.format`.** The API refuses a schema whose compiled grammar is
too large (HTTP 400 "compiled grammar is too large"; the planner's plan schema is one). A deterministic
pre-flight counts the wire schema against the documented ceilings (`providers.base.OUTPUT_FORMAT_LIMITS`:
24 optional properties, 16 union-typed properties, each `$defs` entry once):

- a role whose `native` is only the **registry default** resolves to `none` (prompt mode) at config
  time for a schema over a ceiling. `validate` and `doctor` report it as a
  `structured_output_fallback` warning. A config dump writes the resolved `none`.
- a **declared** `structured_output: native` for such a schema is a validation error
  (`schema_not_strict`). Declare `none` for that role instead; `examples/v2/hosted_models.yaml`
  does this for its Sonnet 5.x planner.
- at run time the 400 is the typed `ProviderSchemaTooComplex` (`schema_too_complex`). A
  registry-default role sends that call once more in prompt mode. This is one explicit switch, not a
  retry: it appears as `structured_output_fallback` (`from`, `to`, `reason`, `schema`) in the call
  record and in the `model_call` trace event, and both calls are budgeted. Later calls with the same
  schema go straight to prompt mode and carry the same record. A declared `native` fails with the
  typed error.

**Models that think by default.** Anthropic Haiku 5.x thinks before it answers; the thinking tokens
count against `max_tokens` and are reported as `Usage.reasoning_tokens`
(`usage.output_tokens_details.thinking_tokens`). The registry declares a minimum `max_tokens` of
**1024** for such models: a smaller value is rejected at validation (`max_tokens: ... thinks by
default ... below the minimum 1024`); a v1 file is upgraded with the value raised to 1024 and a
`max_tokens_raised` warning (`config upgrade --strict` refuses instead). The role default (2048) is
above the minimum. Measured in the 2.0.0a3 smoke: 117–379 thinking tokens per LLM-extraction call and
464 for a short arithmetic prompt, so keep generator and extractor roles at 2048 or more. A reply
that still hits the ceiling is `ProviderIncomplete` and names the thinking share ("truncated at
max_tokens 1024 (1024 of 1024 output tokens were thinking)"). A forced tool call does not think.

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
      max_tokens_parameter: max_tokens  # required for Ollama (see below)
      timeout_seconds: 120
      capabilities: {structured_output: json_schema}   # only if the server supports it; else none
    embedder:
      provider: openai_compatible
      base_url: http://127.0.0.1:11434/v1
      model: "<local-embedding-id>"
      dimensions: 768
```

**Output ceiling on local servers (QQ-01).** Ollama's `/v1` (0.33.0 observed) ignores
`max_completion_tokens` — the default parameter name — and honours `max_tokens`: **set
`max_tokens_parameter: max_tokens` for Ollama.** Whatever the server, a reply that reports more
output tokens than the role's `max_tokens` is a typed `ProviderLimitIgnored` failure (category
`output_limit_ignored`; the call is still metered), never a silently accepted long reply.

**Thinking usage on local servers (QQ-05).** Thinking-capable local models (for example Qwen-family
models served by Ollama) return their thinking beside the reply (`message.reasoning`), outside the
answer. Observed on Ollama 0.33.0: `completion_tokens` then counts the visible reply only, no
`reasoning_tokens` are reported, yet thinking does consume the `max_tokens` ceiling (a truncated
reply can arrive with empty content and `finish_reason: length`). The package records the thinking
text's size as `reasoning_chars` in the call record; token usage and budgets for such servers are a
lower bound, and their `max_tokens` must leave room for the thinking.

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
| `sqlite` (local default) | all eight | `path` (state directory), `vector_engine: numpy|faiss`, `timeout_seconds`, `lexical_engine: fts5|bm25` and `bm25` settings (see "Lexical engine `bm25`" below; with `bm25` the FTS5 table stays empty and `snapshots/<gen>/bm25/` holds the segment). Vector and `bm25` search apply the scope as a document bitset over the generation's rows (exact top-k over eligible rows; Faiss through an `IDSelectorBitmap`). Per generation: `snapshots/<gen>/catalog.sqlite` (documents, chunks, FTS5 `unicode61` — the query is tokenised and case-folded by the same tokenizer as the text, so `Straße` and `ﬁle` match their own words but not `strasse`/`file`, typed field rows for scope pushdown, records, mentions, relations with provenance and occurrences, memberships, communities) plus `vectors.npy`/`chunk_ids.json` (and `vectors.faiss`); `runtime.sqlite` holds the cache, the ingestion ledger with raw model replies and sessions. `CURRENT` and `manifest.json` are owned by the generation registry, never read by the adapter. |
| `memory` | all eight | In-process fake for tests and contract suites; not persistent. |
| `postgres` (extra `postgres`, psycopg 3; pgvector for vectors) | `catalog`, `lexical`, `vector`, `records`, `cache`, `ledger`, `session` | `dsn_env` (environment variable holding the DSN; never the DSN itself), `table_prefix`, `text_search_config` (default `english`), `vector` (`index: exact\|hnsw`, `hnsw_m`, `hnsw_ef_construction`, `hnsw_ef_search`, `min_filter_recall`, default 0.95), `timeout_seconds` (connect timeout and `statement_timeout`). **One schema per generation**, `<prefix>_<dataset>_<generation>` when that fits PostgreSQL's 63-byte identifier limit and the dataset is a lowercase identifier, otherwise `<prefix>_g<sha256(dataset/generation)[:20]>`; it holds documents, chunks, typed field rows (native `double precision` and `timestamptz` columns), records, the FTS table and the pgvector tables, and `delete_generation` is one `DROP SCHEMA ... CASCADE`. Schema `<prefix>` holds `generations` (dataset, generation, schema name, text-search configuration, metadata schema), `meta` (the `CURRENT` pointer row, replaced by a single-row upsert only by `activate`; failed builds write `failure:<gen>` and never touch it; manifests), `cache`, `ledger`, `jobs`, `raw_replies`, `sessions`. The write lock is a session-level `pg_try_advisory_lock` (held = typed error, never a wait). Typed scope, ACL predicates and id allow/deny lists compile to bound-parameter `EXISTS` subqueries; temporal values compare as UTC instants (naive = UTC, dates by calendar day); `values` is `SELECT DISTINCT` under `max_values_cardinality` (10000; more is a typed error). Lexical: title and body `tsvector`s built with the generation's recorded configuration (`analyzer_id` `pg_ts_<config>`); terms = any term, `phrase` = `phraseto_tsquery` per column, `boolean` = terms plus `must` phrases minus `must_not` phrases; every user token is a bound parameter, never tsquery syntax; scores are `2*ts_rank_cd(title)+ts_rank_cd(body)`, **not BM25**; stop words never match and stemming conflates inflections (FTS5 differs). Vector: L2-normalised, inner product = cosine; **exact** by default (scope in the `WHERE` clause, order `(distance, chunk_id)`); `vector.index: hnsw` builds a partial HNSW index per namespace at `refresh` and then measures recall@10 of the forced index path against exact search (unfiltered and with an allow-list filter); below `min_filter_recall` the refresh fails with a typed error so that build never activates; HNSW searches use `hnsw.iterative_scan = strict_order` (pgvector >= 0.8). Up to 2000 dimensions with HNSW, 16000 exact. `graph` is `planned`. |
| `sqlalchemy` (extra `sql`, SQLAlchemy 2 + driver) | `catalog`, `records`, `cache`, `ledger`, `session` | `url_env` (environment variable holding the SQLAlchemy URL, e.g. `sqlite:///state/rel.db` or `postgresql+psycopg://...`), `table_prefix`, `timeout_seconds`. SQLAlchemy Core only (portable types, delete+insert instead of dialect upserts). Per generation: tables `<prefix>_<token>_{docs,chunks,dfields,records,rfields}` with `token = sha256(dataset/generation)[:12]` (the readable name does not fit the 63/64-byte identifier limits; `<prefix>_generations` maps it); runtime tables `<prefix>_{generations,meta,cache,ledger,jobs,raw_replies,sessions}`; `CURRENT` pointer row replaced in one transaction only by `activate`; write lock = lease row (held = typed error; a lease older than 600 s is taken over). Temporal values are stored as UTC epoch seconds (no portable timezone-aware type). Ids are limited to 512 characters (typed error beyond). **Tested dialects: SQLite and PostgreSQL** (the conformance kit runs on both); any other dialect builds and `ping()` reports `dialect_tested: false` — MySQL/MariaDB in particular compare text with the column collation (usually case-insensitive), which would change `eq`. `lexical` and `vector` are dialect-specific and `planned`; configuration naming them is rejected. |
| `mongodb` (extra `mongodb`, pymongo) | `catalog`, `lexical`, `records`, `cache`, `ledger`, `session` | `uri_env` (environment variable holding the connection URI; never the URI itself), `database`, `collection_prefix`, `timeout_seconds` (server selection, connect and socket timeouts). Per generation: `<prefix>_<dataset>_<generation>_{documents,chunks,lexical,records}` collections; `<prefix>_generations` lists known generations with their metadata schema; `<prefix>_{cache,ledger,jobs,raw_replies,sessions}` are prefix-wide; `<prefix>_meta` holds the generation pointer (`_id: CURRENT`, replaced atomically only by `activate`; failed builds write `failure:<gen>` and never touch it), manifests and the write lease. Typed scope, ACL predicates and id allow/deny lists are pushed down as query predicates over typed field rows (temporal values stored as UTC instants under the shared rule: aware stamps converted, naive `assume_utc` stamps and dates read as UTC, `date` fields compared by UTC calendar day); `exists` is one `find_one`; `values` groups distinct values server-side under `max_values_cardinality` (default 10000, exceeding it is a typed error, never a truncated list). Lexical search uses the server's `english` text index (`analyzer_id: mongo_text_english`): phrase and `must`/`must_not` are supported, scores are MongoDB `textScore` values and **not BM25**, so no score parity with other engines is claimed. The cache has a server-side TTL index on top of the clock-based expiry; sessions get a TTL index only when the backend is built with `session_ttl_seconds` (Python API). No `graph`; `vector` stays `planned` because MongoDB vector search exists only on Atlas and is not verified here, so a config naming `(mongodb, vector)` is rejected. |
| `opensearch` | `catalog`, `lexical`, `vector`, `records` | `hosts_env` (environment variable holding one or more comma-separated `http(s)://host:port` URLs), `index_prefix`, `knn: {method: exact|hnsw, exact_rescore: true}`, `timeout_seconds`, `records: {aggregation: client|native, matched_ids_cap: 10000, max_groups: 10000, distinct_exact_cap: 100000}` (query-time; not part of the index recipe; see "Records aggregation" below). Needs `pip install 'su-multi-arm[opensearch]'` (`opensearch-py`); OpenSearch 2.x or 3.x (`probe` refuses other majors and other distributions; tested on 2.19.1 and 3.9.0). Per generation three indices `<index_prefix>_<dataset>_<generation>_{docs,chunks,records}` (one shard, strict mappings tagged with the adapter's mapping version); dataset aliases `<index_prefix>_<dataset>_{docs,chunks,records}` are switched atomically by `OpenSearchBackend.activate(gen)`, and `delete_generation` refuses the generation an alias serves. Datasets must match `[a-z0-9][a-z0-9_-]*` (index names are never mangled). See "OpenSearch semantics" below. |
| `elasticsearch` | `catalog`, `lexical`, `vector`, `records` | Same options as `opensearch` (`hosts_env`, `index_prefix`, `knn`, `timeout_seconds`, `records`); needs `pip install 'su-multi-arm[elasticsearch]'` (official `elasticsearch` 8.x client) and an Elasticsearch 8.x server (other majors and OpenSearch are refused by `probe`). The adapter is the OpenSearch adapter's classes over `backends/_lucene_common.py` with the engine-specific parts replaced: transport, probe, `dense_vector` fields (exact: `index: false` + `script_score` cosine; `knn.method: hnsw`: float HNSW without quantisation, candidates from the top-level `knn` search with the scope as pre-filter, then the same exact rescore), and no `index.knn` setting. Same three indices per generation, same dataset aliases switched atomically by `activate(gen)`, same typed-field pushdown, lexical and records semantics as the section below. `generation_registry()` (what the write path needs to own `storage.catalog`) keeps manifests, failure records and the ingestion lock in `<index_prefix>__registry` and resolves `CURRENT` through the `docs` alias. |
| `neo4j` (extra `neo4j`, official driver) | `graph` (with `memberships`) | `uri`, `user`, `password_env` (environment variable holding the password; never the password itself), `database`, `namespace` (default `su_multi_arm`; letters, digits, `_`, `.`, `-`), `timeout_seconds` (connection, pool acquisition and per-transaction timeout). Fixed labels; every node and edge carries the generation key `g = <namespace>\|<dataset>\|<generation>`, so namespaces and generations never touch each other. Mentions are `SuGraphMention` nodes (span offsets, quote, match level, canonical id, aliases), entities `SuGraphEntity` nodes, relations one relationship type per edge kind (`SU_FACT`, `SU_CO_OCCURRENCE`, ...) carrying predicate, `edge_kind`, `support`, status, span, provenance and every occurrence; communities are `SuGraphCommunity` nodes with their report, memberships `SuGraphMembership` nodes. Loads are `MERGE`-idempotent and one explicit transaction per call (a dangling relation endpoint or any server error rolls the whole call back; never retried). Traversal runs only fixed parameterised Cypher templates, one statement per hop, `fact` edges unless other kinds are requested, the hop bound a validated integer 0..3. Eligibility is pushed down as a document-id allow/deny list only (`predicate_scope: false`): the graph and community arms first resolve request filters and ACL predicates to document ids through the catalog (at most 10000 eligible documents; more is a typed error, never a truncated list). `<namespace>\|<dataset>` has one `SuGraphPointer` node naming the active generation; the write path moves it just before the authoritative `CURRENT` switch, moves it back if that switch fails, and a failed load never reaches it; `delete_generation` refuses the generation it names. Needs Neo4j 5.23 or later (scoped `CALL` subqueries); tested on 5.26 community. No catalog: `storage.catalog` must name another backend. |
| `kuzu` (extra `kuzu`, embedded) | `graph` (with `memberships`) | `path` (database file, relative to the config; default `state/graph.kuzu`), `mode: native\|attach`, `namespace`, `timeout_seconds` (per-query timeout), `buffer_pool_mb`. **`native`**: the package's own embedded store with the same contract and semantics as `neo4j` (fixed tables `SuGraphEntity`/`SuGraphMention`/`SuGraphMembership`/`SuGraphCommunity`/`SuGraphGeneration`/`SuGraphPointer`, one relationship table per edge kind, every row keyed by `g = <namespace>\|<dataset>\|<generation>`; `MERGE`-idempotent loads in one explicit transaction; fixed parameterised Cypher only; `fact` edges unless other kinds are requested; document-id allow/deny eligibility, `predicate_scope: false`; a per-dataset pointer row moved by the write path with compare-and-set). The conformance kit runs in-process (no service). **`attach`** (`attach: {mapping: <file>}`): an **existing** Kuzu database opened read-only and mapped onto the contract by a declared mapping file (`backends/kuzu/attach.py::AttachMapping`): `dataset`, `generations` (or `any_generation: true`), `nodes` (`table`, `type`, `key`, `name`, `lookup`, `document: {column, pattern, template}`, `mention_text`), `edges` (`table`, `predicate`, `edge_kind`, `support`, `quote`, `source`, `document_pattern`/`document_template`, `verified`, `status`, `provenance` columns), optional `communities` (`table`, `label`, `level`, `summary`, `quote`, `quote_source`, `member_edge`) and `require_verified` (default true). Every table and column is checked against the store's catalog when the backend opens; only statements built from the validated mapping run; an edge without a quote, without a document id, or (with `require_verified`) without a verified quote is never surfaced; the edge span anchors the stored quote (`field: <REL>.<quote column>`), not package catalog offsets. Writes (`upsert`, `put_communities`, `delete_generation`) are refused. Keep private mappings outside the repository. |

An adapter may be named only for the capabilities it serves: naming `opensearch` or
`elasticsearch` for `ledger`, `sessions`, `cache`, `communities` or the `graph`/`community` arms,
`mongodb` for `vector`, `postgres` for `graph`/`community`/`storage.communities`, `sqlalchemy` for
`lexical`/`dense`/`graph`, or `neo4j`/`kuzu` for anything but `graph`/`community`/`storage.communities`,
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
  ranks are not comparable with other engines (plan §3.5). OpenSearch 3.x scores with Lucene
  `BM25Similarity` where 2.x used `LegacyBM25Similarity`: the same index gives 3.x scores equal to
  the 2.x scores divided by `k1 + 1 = 2.2`, with identical rankings (measured on a synthetic corpus,
  docs/VALIDATION.md). Every fusion and ordering policy (`rrf`, `minmax`, `zscore`, rank-based) is
  invariant to that factor; raw lexical scores shown in `arm_hits` are not comparable across majors.
- **Vector**: vectors are L2-normalised on write and stored in `knn_vector` fields (one per
  namespace, dimension fixed by the first write, up to 16000). `knn.method: exact` (default,
  plan decision 5) scores every eligible chunk with an exact cosine `script_score`, so results are
  identical to the sqlite/memory adapters for the same vectors and scope. `knn.method: hnsw` (opt-in)
  builds a Lucene-engine HNSW graph (`index.knn: true`), retrieves `max(50, 4k)` candidates through
  the native `knn` query with the eligibility filter applied inside the graph search, then rescores
  those candidates exactly; `exact_rescore: false` is refused (ANN scores are never returned). The
  method is recorded in the chunks index mapping; opening a generation built with the other method
  is a typed error. The HNSW engine is always `lucene`, which both majors accept for new indices
  (OpenSearch 3.0 blocks index creation with `nmslib`); `probe` (`doctor --probe`) refuses an engine
  the detected major cannot create. The exact path and the index layout are the same on 2.x and 3.x,
  so the server major is reported by `probe` and not recorded in the generation identity.
- **Eligibility pushdown**: `Scope` (allow-list, exclusions, typed predicates incl. range/date and
  `missing`) compiles to a native `bool.filter` on typed fields denormalised onto every chunk
  (`s.*` keyword with a `lowercase`-normalized `.fold` sub-field for `casefold_match`, `n.*` double,
  `b.*` boolean, `t.*` epoch-millis comparison keys from `backends/_scope.py::temporal_key` (aware
  stamps as UTC instants, naive stamps and date-only strings as UTC, `date` fields as calendar dates
  so a datetime bound truncates to its UTC date; temporal `eq` is instant equality), `p` = non-null
  fields);
  nothing is post-filtered. `casefold_match` therefore uses Lucene `lowercase`, not Python
  `str.casefold` (they differ for a few characters such as `ß`).
- **Records** (`records.aggregation: client`, the default): the ACL and every supported predicate
  are pushed down as the same filter; the denominator is a `_count`; the matched records are scanned
  in id order (`search_after`) and the measure is computed by the shared `backends/_scope.py` code,
  so every `ScopeResult` is `exact` and identical to the other adapters. A scope matching more than
  `max_matched` records (default 10000, constructor argument) is a typed `BackendError`, never a
  truncated result. `values()` is a key-ordered `terms` aggregation capped at 10000 (`limit` above
  the cap is an error); `exists()` is a `_count`.
- **Records aggregation** (`records.aggregation: native`): the scope operations are compiled to
  aggregations on the server (`backends/_lucene_aggs.py`), so a scope over millions of records is
  answered without fetching them and `max_matched` does not apply. The same filter (ACL included)
  is the query of every aggregation request. The compilation:

  | Operation | Native form | Exactness |
  |---|---|---|
  | count, `total`, `zero_match` | `track_total_hits: true` (a lower-bound total is a typed error) | exact |
  | `group_by` (one field or a cross-tab of two), nulls | `composite` paging over `v.<field>` with `missing_bucket`, keys decoded to their JSON type | exact |
  | calendar groups (`granularity`), `bucket` measure | `date_histogram` (`calendar_interval`, `time_zone` = `metadata_schema.timezone`) | exact |
  | `top_n` / `nth` of a count over one field | `terms` ordered by count, grown until the boundary rank is decided and the engine reports `doc_count_error_upper_bound: 0` (OpenSearch 3.x searches even the one-shard index in concurrent segment slices) | exact; more than `max_groups` needed is `bounded` |
  | other ranking, `order`, group-field sort, `with_share` | composite enumeration, then the shared ranking code | exact |
  | `periods` | `filters` aggregation | exact |
  | `share` numerator, null counts of negated predicates | `filter` sub-aggregations | exact |
  | sum / avg / min / max | `stats` on `n.<field>` | engine double sums (integral values reported as integers) |
  | first_seen / last_seen | `value_count` + `top_hits` sorted by `t.<field>`, then id | exact at millisecond resolution |
  | `count_distinct` | `composite` paging of (group, unit) pairs up to `distinct_exact_cap` | exact; above the cap `cardinality` (HyperLogLog++), `exactness: approximate`, the reason in `basis` |
  | record list (`limit`) | sorted search of at most `limit` hits, missing values last, ties by id | exact |

  Nothing is cut silently. `matched_ids` holds the first `matched_ids_cap` matched ids (id order) and
  `matched_ids_truncated: true` says more matched; `total` is always the full count. The default cap
  (10000) equals the client path's `max_matched`, so wherever the client path answers, native gives
  the same ids. More than `max_groups` groups gives a `bounded` result with no groups and an
  `UnsupportedConstraint` naming the cap. A group key that the index stores apart but Python treats
  as equal (`3` and `3.0`, or `1` and `true`, in one undeclared field) is a typed `BackendError`.
  The answer contract is unchanged: an `approximate` or `bounded` result never backs a count, list or
  absence claim, and an absence claim also needs `total == 0`. `percentiles` is not compiled: the
  measure contract has no percentile kind. The conformance kit gives identical results for both modes
  on OpenSearch 2.19.1, 3.9.0 and Elasticsearch 8.17.4 (`tests/conformance/test_lucene_native_aggregation.py`).
- **Result window**: lexical pages stay within `index.max_result_window` (10000, `from + size`). A
  page that ends at the window edge decides whether more hits exist from an exact `_count`; a page
  beyond the window is a typed `BackendError`, never a shortened ranking. A structured text
  condition matching more than 10000 chunks on these engines is therefore refused ("narrow the query
  or scope") rather than counted.
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

`extraction.extractors` combines `pattern`, `gazetteer`, `regex`, `spacy`, `gliner`, `llm` (v1 names
`providers`/`tags` upgrade automatically; `pattern` has its own section below):

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

### Pattern extractor (`pattern`, `extraction.pattern_library`)

The `pattern` extractor matches lines (log lines or any line-oriented text) against a **pattern
library**: a directory of files with typed named groups that produce span-anchored entities and
line-scoped relations. It is selected together with `extraction.pattern_library`; one without the other
is a configuration error.

```yaml
extraction:
  extractors: [pattern, gazetteer]       # pattern first: later extractors see only the residue
  pattern_library:
    path: patterns/                      # library directory (relative to the config file)
    engine: re2                          # re2 (extra `patterns`) | regex (extra `patterns-lite`, development only)
    max_line_bytes: 8192                 # each line is cut here (UTF-8, on a character boundary) before matching
    mode: line                           # every line of a chunk is matched on its own
    promotion: auto                      # auto | manual; default auto (owner decision 2026-11-04)
    min_literal_chars: 4                 # lint warning / gate refusal below this many literal characters
    regex_timeout_ms: 100                # per-search timeout of the regex engine
    gate: {min_support: 3, require_re2: true, min_cluster_coverage: 0.9, max_out_of_cluster: 0}
```

**Engines.** `re2` uses `google-re2`: matching is linear in the line length and each stage uses an
RE2 `Set` to select the candidate patterns before full matching. `regex` is a development fallback:
patterns are compiled ASCII-only and every search has a timeout. A timed-out line gets status `timeout`,
and the provenance records `engine_deterministic: false`. The engine is never chosen implicitly: when
the configured engine's package is missing, `validate`/`doctor` report `pattern_engine_unavailable`
and the build fails. There is no fallback to the other engine.

**Library files** (one directory):

| File | Owner | Written by |
|---|---|---|
| `library.yaml` | human | humans only. The system never writes it. |
| `learned.yaml` | system (humans may edit) | the CAS-safe writer, append-only |
| `proposed.yaml` | system | the writer, append-only |
| `decisions.yaml` | system | the writer (promotion policy or CLI), append-only |

`library.yaml` is a mapping (`library`, `format_version: 1`, `engine`, `vocabularies`, `masks`,
`patterns`). The system files are YAML sequences. A synthetic example:

```yaml
library: example-core
format_version: 1
vocabularies: {service: [ingest-worker, query-gateway]}
masks: {num: '-?[0-9]+(?:\.[0-9]+)?'}
patterns:
  - id: fmt.kv_v1
    kind: format                         # index_name | format | continuation | message | annotate | noise
    status: approved
    priority: 100
    regex: '^ts=(?P<ts>[0-9T:.\-]+Z) lvl=(?P<level>[a-z]+) comp=(?P<component>[a-z0-9_.]+) msg="(?P<body>[^"]*)"$'
    entities: [{group: component, type: component}]
    then: {field_from_group: body}       # the message stage matches this group
  - id: msg.dependency.connect_failed
    kind: message
    status: approved
    priority: 50
    applies_to: {level: [error, warn]}   # selectors: a format group of that name, else document metadata
    regex: '(?P<client>[a-z]+-client) (?P<trigger>failed to connect|refused by) (?P<dep>kafka|redis)(?: after (?P<ms>{{mask:num}}) ?ms)?'
    signature: {id: dependency.connect_failed, category: connectivity}
    fields: {ms: {type: number}}
    entities: [{group: client, type: component}, {group: dep, type: dependency}]
    relations: [{subject: client, predicate: fails_to_connect, object: dep, trigger_group: trigger}]
    attribute_links: [{subject: $service, predicate: emits, object: $signature}]
    examples: {positive: ["db-client failed to connect redis after 30 ms"], negative: ["db-client connected to redis"]}
```

**Stages per line.** `format` (first match by priority, then id) hands its `then` group to `message`
(first match sets the signature). Every matching `annotate` pattern adds entities. Without a message
match, a `noise` pattern marks the line benign. The resulting status is one of `parsed`, `noise`,
`format_only`, `unparsed` or `timeout`; multiline assembly assigns `fragment` upstream. Coverage is
`(parsed + noise) / (lines - fragments)`.

**Relations.** Relations are line-scoped: both endpoints are entity groups of the message match or of
its format match, and the trigger group is on the same line. The relation span is the smallest span
covering them. With a matched trigger group the relation is a `fact`, unless a negation word lies in the
span. Without a trigger group the predicate-support check decides. Anything unsupported is stored as a
`co_occurrence`. **Attribute links** (`$service`, `$tenant`, `$env`, `$signature`) are emitted only from a
document's `header` field. They are `native_link` edges with `support: unchecked`, created when the
document's `signature_id` belongs to the pattern and both endpoint values occur in the same header
chunk. Typed `fields` are not written to the graph; they are available to partition-stage consumers.
Chunks cut by the default chunker can split a line; the planned `line` chunk recipe keeps one record
per chunk.

**Residue.** When `pattern` is listed first, each following extractor runs only on the lines the
library did not parse. Parsed and noise lines are blanked and offsets are kept. When nothing is left,
the following extractors are skipped for that chunk. A later extractor whose match reaches into a blanked
line fails the chunk (`grounding`); nothing is repaired. When `pattern` is not first, every extractor
sees the whole chunk.

**Loader and decisions.** Entries are read in the order user, then learned, then proposed. An id
defined by more than one owner resolves to the first (`info` diagnostic): a human entry wins over a
learned one, and a learned entry over a pending proposal. `decisions.yaml` records are then replayed in
order:

- `approve` promotes an entry.
- `deprecate` retires it.
- `supersede {id, target}` shadows the target without editing it.
- `expire` drops an unpromoted proposal.
- `disable` is a human-only record (`by: user:<name>`). It is final and wins over every other record and
  file.

A system record (`by: system:<name>`) never changes a human-owned entry, and a system `disable` is not
applied. Both are reported as warnings.

**Lint** (load-time errors) covers:

- the RE2 subset: no backreferences, lookaround, atomic groups, possessive quantifiers, `\Z`, `{,n}`,
  `\p`, unnamed capture groups, or flags other than `i`/`m`/`s`;
- the ReDoS rules:
  1. star height ≤ 1;
  2. no quantified alternation with branches sharing a first character, or with an empty branch;
  3. no adjacent unbounded repeats over intersecting classes;
  4. every unbounded repeat is followed by an anchor or by a character it cannot consume, so `error
     (?P<code>[0-9]+)` must end in `$` or a literal;
- duplicate ids within one owner, and unknown kinds, statuses and field types;
- undeclared groups and relation endpoints that are not entity groups;
- a relation with a `$attribute` endpoint (with or without `trigger_group`): attribute links belong
  under `attribute_links`;
- examples that fail their own pattern, and unresolved `{{vocab:}}`/`{{mask:}}` templates;
- ambiguity: same kind, priority and overlapping selectors, with each pattern matching the other's
  positives;
- unredacted email, IPv4, JWT, bearer-token or access-key shapes in examples.

Warnings cover too few literal characters, a pattern unanchored at both ends, and a system file
edited outside the writer. Lint errors in a proposal that is not approved become warnings and the entry
stays inert. Approving it makes them errors again. The lint walks the standard library's regular
expression parse tree; that parser folds some alternations (`x|x` becomes a class), so the `regex`
engine's timeout remains the runtime guard there, and RE2 is linear regardless.

**Identity, fingerprint and reuse.** Only `engine`, `max_line_bytes` and `mode` enter the index
recipe; while `pattern_library` is unset the recipe is unchanged. The library contents and the engine
version are build provenance. Each build records `pattern_provenance` in the generation manifest
(`library_identity`, which is the sha256 of the effective approved entries, plus `engine`,
`engine_version`, `validator_version` and `engine_deterministic`). It stores the effective library
snapshot content-addressed in the ledger (`pattern_library_snapshot`). Extraction reuse keys carry a
salt derived from that provenance and the applied decisions
(`extraction:{recipe}:{salt}:{chunk}`), so approving or deprecating a pattern re-extracts on the next
build without changing the index fingerprint. Other extractors keep the unsalted key. The library is
read when a build starts and by `doctor --probe`; serving never reads the library files.

**Promotion (`promotion: auto | manual`, default `auto`).** A learned proposal must pass the
deterministic gate (`learning/patterns/gate.py`), which checks:

- `re2_compile`: validator plus RE2 compile (the `regex` engine cannot pass while `require_re2` is
  set);
- `lint`: entry lint, the ReDoS rules and at least `min_literal_chars` literal characters;
- `cited_examples`: the cited example lines are supplied evidence lines (not invented), positives match,
  negatives do not, and the entry's examples are exactly those lines;
- `no_regressions`: no match on held-out lines that other approved patterns match today;
- `min_support`: at least `gate.min_support` matched lines and that many cited positives;
- `novelty`: not a duplicate regex (including disabled and deprecated entries);
- learning loop only (`patterns learn|propose`): `cluster_coverage` (matches at least
  `gate.min_cluster_coverage` of the whole cluster), `out_of_cluster` (matches at most
  `gate.max_out_of_cluster` unparsed lines outside the cluster; default 0) and, for a fix,
  `fix_target` (the target is approved and every one of its positive examples still matches).

Under `auto`, a passing `message` proposal is appended to `learned.yaml` (approved, priority below every
approved entry of its kind, gate summary as `evidence`) with an `approve` record in `decisions.yaml`
(`by: system:auto` for the miner, `system:pattern_learner` for the LLM loop). A passing fix of a
system-owned pattern is recorded as `supersede {id: fix, target: old}`. The record copies the example
lines. Auto-promotion also needs a gate run on RE2. Under `manual`, and always for other kinds, for a
gate run on the `regex` engine and for fixes of human-owned patterns, the proposal is appended to
`proposed.yaml` and waits for a human decision (`patterns promote`). A failing proposal is not written.
The deterministic miner and synthesiser and the LLM learning loop (below) are the two sources of
proposals. A human review step on top of `auto` is planned as an option.

**Writer.** The writer appends only to the three system files and never opens `library.yaml`. It uses
a sha256 compare-and-swap: it reads, writes a temporary file and fsyncs it, re-checks the sha, runs
`os.replace`, and verifies the result. A concurrent save restarts the append. A file that is no longer
a YAML sequence is refused (`SystemFileCorrupt`), and persistent contention raises
`SystemFileConflict`. With `learning.patterns.git_commit: true` and a library directory inside a git
work tree, the writer refuses to start while a system file has uncommitted changes
(`SystemFileDirty`: commit or revert the hand edits) and commits exactly the changed system files
afterwards, with the author `system:pattern_learner` (or `system:cli` for human decisions).
`library.yaml` is never added or committed by the system.

**CLI.** `su-multi-arm --config c.yaml patterns lint` prints statuses, provenance and issues.
`patterns coverage --input lines.txt [--attr service=x --attr level=error]` prints line status counts.
`patterns mine --input lines.txt` runs the deterministic template miner and synthesiser and prints the
proposals (status `proposed`) with coverage before and after. These three write nothing. `--input` may
also be a `.jsonl` file of `{"id", "text", "attributes"}` objects. The learning commands
(`patterns propose|learn`) and the human decisions (`patterns promote|deprecate|disable <id>`) are
described under "Pattern learning" below; they are CLI-only, not `Action`s, with no HTTP/MCP surface.

**`regex` and `pattern`.** `extraction.patterns` (the `regex` extractor) still runs on Python `re` in
this release, so its results and existing fingerprints are unchanged.
`su_multi_arm.extraction.patterns.sugar.regex_entries` gives the equivalent `annotate` entries for the
planned switch, and fails for an expression outside the RE2 subset or the ReDoS rules.

### Pattern learning (`learning.patterns`, `models.roles.pattern_learner`)

The learning loop asks a chat model to propose patterns for the lines the library does not parse.
The deterministic gate decides; the promotion policy decides where a passing proposal is written.

```yaml
models:
  roles:
    pattern_learner:                     # anthropic | openai | openai_compatible, structured output
      provider: openai_compatible
      model: local-model
      base_url: http://127.0.0.1:8000/v1 # loopback: allowed without hosted egress
privacy:
  redact_emails: true                    # at least one redaction is required: only redacted lines leave
  patterns: {IPV4: '\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b'}
learning:
  patterns:
    max_clusters_per_run: 10             # clusters asked about per run (largest x level weight first)
    max_lines_per_cluster: 8             # example lines sent per cluster
    nearest_patterns: 3                  # approved patterns shown to the model
    max_calls_per_run: 10
    max_calls_per_day: 50                # persisted per calendar day in `timezone`
    max_usd_per_run: 0.25                # USD caps need budgets.price_table
    max_usd_per_day: 1.0
    timezone: UTC
    spend_ledger: state/learning_spend.sqlite   # default <library>/.spend_ledger.sqlite
    git_commit: false                    # commit learned/proposed/decisions in a git work tree
    egress:
      allow_hosted: false                # true is required for a hosted (non-loopback) role
      pseudonymise: [tenant, host]       # attribute values replaced by per-run aliases
```

**One run** (`patterns learn --input lines.jsonl`):

1. **Egress.** Every input line is redacted with the configured `privacy` redaction and stamped. Only
   stamped lines may reach the model. A line that still shows an e-mail, IPv4, JWT, bearer-token or
   access-key shape after redaction refuses the whole run (`EgressRefused`) before any call; lines are
   never silently dropped or repaired. A config without any redaction is refused. A hosted role
   (anything but `openai_compatible` on a loopback host) needs `egress.allow_hosted: true` and both
   USD caps. Values of the `pseudonymise` attributes are replaced by stable per-run aliases in the
   attributes and in the line text before mining, so neither the prompt lines nor the cluster template
   carry them.
2. **Match** every line with the current library: coverage *before*. The `unparsed` and `format_only`
   bodies form the queue.
3. **Mine** the queue (the Drain-style miner), rank clusters by size times a level weight (error 3,
   warn 2, other 1) and keep the top `max_clusters_per_run` with at least `gate.min_support` lines.
4. **Propose.** One structured call per cluster (reply schema `PatternProposalReply`: `action`
   `new | fix | noise | none`, `regex`, typed groups, entities, relations, cited positive and negative
   line ids, `issue_name`, `issue_category`, `rationale`). Each call is reserved first against the run
   caps and the persistent day caps; a refused reservation stops the run before the request. A provider
   error stops the run; a reply that violates the schema is recorded for its cluster. Nothing is
   retried. `issue_name` and `rationale` are inert metadata in the entry's provenance; the regex is the
   only part of a reply that is used, and only after the gate.
5. **Gate** every entry (the checks above, including `cluster_coverage`, `out_of_cluster` and
   `fix_target`).
6. **Write** passing entries under `promotion` (`patterns propose` always writes to `proposed.yaml`).
   A fix of a pattern that is disabled or deprecated is invalid: a human decision always wins.
7. **Reparse**: coverage *after* (with the reloaded library) and *after if approved* (pending
   proposals evaluated as approved).

The report (JSON on stdout, or `--output report.json`) lists each cluster's outcome (`learned`,
`proposed`, `rejected` with the failed gate checks, `invalid`, `invalid_reply`, `declined`,
`not_sent`, `provider_error`), what was written, the spend of the run and of the day, and the git
commit. The command exits 1 when the run stopped early (budget or provider error).

`--dry-run` stops before any model call: it reports the clusters, the line ids that would be sent, the
prompt size and the estimated USD, spends nothing and writes nothing (no ledger file either).
`--max-usd X` lowers the per-run USD cap for one run; it can never raise it.

**Human decisions.** `patterns promote <id>` approves a proposed (or deprecated) entry; a fix is
promoted as `supersede`, so its target is shadowed without editing `library.yaml`. `patterns deprecate
<id>` retires an approved entry. `patterns disable <id>` is final. Each appends one record
`by: user:<name>` (`--by name`, default `cli`; `--reason text`) to `decisions.yaml`; a decision that
does not fit the entry's state is refused and nothing is written.

**Evidence.** Synthetic lines and a scripted fake provider only (docs/VALIDATION.md); no real model
has been run through the loop yet.

Communities use overlapping entity groups by default; `communities.strategy: louvain` clusters the
bipartite document/entity graph with a fixed seed. Minimum document count is configurable.
`communities.strategy: louvain_hierarchical` (extra `communities`) keeps the seeded Louvain dendrogram
as levels 0 (coarsest) to 2 with `parent_id` links; `communities.hierarchy: {levels: 1..3, resolution,
min_split}` tunes it (a parent with fewer than `min_split` documents is not split). `communities.stats:
{time_basis: <datetime/date field>, granularity: month|year, top_entities: N}` adds deterministic
per-community statistics from declared fields only: `size`, `entities`, `members_by_kind`, a `trend`
(bucket counts, least-squares slope, `missing_or_invalid`) over `time_basis`, and `top_entities` by
lift (support >= 2 in communities with more than one document). Both blocks enter the index recipe only
when set, so existing generations keep their fingerprint. Hierarchy and statistics are stored with the
community (under the report's `_community` key) on sqlite, neo4j, kuzu and the in-memory backend. Both
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
`timezone_policy`, `casefold_match`, `max_catalog_cardinality`, `states`; schema-level `canonical_unit`,
`equivalence_field`, `unknown_fields`, `timezone`), `models.roles` (`planner`,
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

Query-compiler operations (2.0.0a3). A typed scope (`ScopeSpec`) can also: rank groups by their
measure value (`order: desc|asc`, `top_n`, `nth` for "second most"; dense ranks, ties broken by the
group key, a tie at the `top_n` boundary listed in `ties`); group a `datetime`/`date` field by
`granularity: day|month|year` (calendar buckets in `metadata_schema.timezone`); cross-tab two
`group_by` fields; compare `periods` (half-open windows on its `time_basis`); give each group's
share (`with_share`) or a share with a `numerator`; count distinct values (`count_distinct`: null
and missing values are excluded and reported in `missing_or_invalid`, except the `equivalence_field`,
where a record without one is its own unit); list records (`limit`, `sort`; `total` is always
reported, "showing N of total"); and match a literal topic (`text: {kind: phrase|all_terms, text,
role: scope|numerator}`), which the structured arm counts over every matching chunk of the lexical
index of `pipeline.arms.structured.text_backend` (default: the arm's own backend when it serves
lexical) under the request's eligibility scope: a whole-corpus literal match labelled with its
analyzer, never a ranked top-k; more than 50,000 matching chunks is a typed refusal. Every result
carries `basis` labels (count unit, time basis, window, text match, null coverage). Numeric measures
exclude records without a numeric value and report them. A negated predicate (or a `negate` state)
includes records with a null value and reports how many in `unknown`.

`metadata_schema.fields[].states` names value sets (`{name: open, values: [done, closed], negate:
true}`); an agent-compiled `eq` on a state name becomes that set (a caller's request filter stays
literal). `metadata_schema.timezone` (IANA) is the zone calendar expressions and buckets are read
in. Calendar expressions (`decision/windows.py`, English: `today`, `yesterday`, `this|last|previous
week|month|quarter|year`, `past|last N days|weeks|months|years`, `past month`, `ytd`, `since
<year|month>`, ISO dates/months/years, `March 5, 2024`, `March 2024`, a month name = its most recent
occurrence that has begun) resolve against the request anchor `as_of` (search/answer request field;
default: today in that zone) into half-open windows. Without a declared `timezone`, a calendar
expression on a `require_offset` datetime field is refused (`timezone_missing`); date fields and
`assume_utc` fields read it in UTC. The `rules` grammar (`decision/grammar.py`) maps English
phrasings onto these operations using only schema words (field names, `time_basis` labels, states,
enum/observed values): `by|per|for each <field>`, `by month`, `which <field> has the most`, `second
most common <field>`, `top N <fields>`, `average|min|max|total <numeric field>` (`median` is refused),
`distinct <field>`, `what percentage of X are Y`, `X vs Y` periods, `between X and Y`, `N most recent
<unit>`, `top N <unit> by <field>`, `not|excluding <value>`, `A or B`, `<numeric field> above N`,
`mentioning|containing|about <words>`, `<word>-mention <unit>` and quoted phrases. Anything left over
is still an `UnsupportedConstraint`. Some qualifier words are grammar vocabulary, so the residual check
does not catch them. When no operation consumes one of them, it is `qualifier_not_applied` and the agent
abstains (2.0.0a3 parity fix; before, the count came back labelled `exact`):

- an ordinal other than `first` outside a ranking ("who is second for X");
- `active`/`inactive` when no field declares it as a state or value;
- a numeric comparison bound to no field ("with >= 20 records", "at least 3").

A topic cue never starts inside a hyphenated word, and a topic made only of grammar words
("contain the most active records") is not a text match.

The `llm` planner's scope draft carries the same operations plus calendar `windows`/`periods` as
expressions (resolved by `ValueResolver.window`); the plan schema stays strict-expressible. A draft
that sorts a grouped scope by `count` or by its measure is compiled to the group ranking
(`order: desc|asc` from `sort.descending`; 2.0.0a3 parity fix). The names accepted are `count`,
`record_count`, `group_count`, `records`, `measure`, `value`, the measure kind, or a numeric measure's
field, unless a sortable field of that name exists. Without `group_by` (records have no count), or with
an `order` that contradicts the direction, the draft is `measure_unsupported`.

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
min_per_record, aggregate_reserve_max}`. Aggregate evidence is reserved before documents, up to
`aggregate_reserve_max` of the budget (default 0.5; an aggregate beyond it is omitted and listed in
`omitted_ids`, the cap is traced as `aggregate_reserve_chars`). Packing renders record fields in the configured `field_priority` order and then in
document order — there is no built-in `details`/`resolution` preference. Ordering never changes
union membership.

`pipeline.review` (`enabled`, `max_hops`) runs the reviewer role between selection and generation.
Stop reasons are `sufficient`, `no_actionable_gap`, `repeated_query`, `hop_limit`,
`budget_exhausted` and `replan_abstained`; a follow-up query is re-planned through the configured
decision agent with the same filters, ACL and pinned generation, and a re-plan that is not
`decided` (including `raw_all_arms`) stops the loop. New hits merge with one vote per record per arm.

`pipeline.generation.contract.on_violation` (`strip` default since 2.0.0a3, `abstain_with_reason`, `fail`) governs
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
aggregate its count claim cites: a scalar value, a group count or value, the sum of the group counts,
the matched total, or the denominator when a preposition introduces it ("12 of 36 records", "among
2,048 records", "over 2,048 records in the snapshot"). A different number is `answer_number_mismatch`.
A number that names the population of a ratio is not compared at all (2.0.0a3 parity fix). This covers
"out of 2048 records", "a denominator of 2,048", "denominator: 2048", "2,048 records as the
denominator", "12/36 records", "40% of 2048 records" and "12 of 50 records" (for the 50). The count in
the same sentence is still compared, and "there are 36 records" stated alone with the denominator 36
is still a mismatch. Precision exclusions: a number
other than one directly before a singular unit is a label ("a 404 error"); a number right after a
product-like capitalised name is a version ("Windows 10 users"); a unit followed by a head noun it
modifies is not the counted population ("there is no error message"); "the only <unit>" needs a
plural unit; "complete list of <noun>" needs `<noun>` to be a unit. Answers in other languages are
checked on their typed claims only.

2.0.0a3 additions to the text check: a **total/sum statement** of an amount — "total"/"sum"/"combined
…" followed within six words by a copula ("is", "was", "comes to", "amounts to", "equals") and an
amount with an optional currency symbol or code or unit ("The total amount for project Delta is 4,200
USD", "the sum was $1,250.50") — is a count statement whether or not a numeric field is declared, and
its amount must match the cited exact aggregate (QQ-03). A number directly after a **declared field
name** is that field's value, not a count ("priority 3 tickets", QQ-02). When the nearest unit after a
number reads as a label, a farther unit within the three-word gap is still checked ("111 open CloudTax
tickets" when the question made "open" a unit). The model's `insufficient_evidence: true` no longer
exempts the text: a hedged answer that still states a count, list or absence is checked like any
other (QQ-06). "List all/every", "show (me) all/every" or "enumerate" followed by a common record noun
that is **not** a declared target ("List every pending ticket" when `ticket` is not the canonical
unit) is an untargeted list intent: the text is checked as a list answer, the unsupported constraint
names the undeclared noun (`aggregate_intent_without_scope`), and a planner's structured step for it is
bounded with that reason instead of "no aggregate operator" (QQ-04).

Violation handling (`on_violation`):

- `strip` (**default since 2.0.0a3**): a non-narrative claim the rules reject is removed from `claims`
  (its citations too unless still used). When the question asks **no** aggregate (no count, list,
  total, earliest/latest or absence intent, targeted or not), the sentences of the answer text that
  carry an unbacked count/list/absence statement are removed as well, and the narrative remainder is
  returned with `contract: stripped` and every removal recorded (`action: strip`). When the question
  **does** ask an aggregate, an unbacked statement in the text is the answer itself, so the answer
  abstains instead. If nothing remains, or no citation remains for a factual answer, it abstains.
- `abstain_with_reason` (the 2.0.0a2 default): returns `insufficient_evidence: true` with the violated
  rules. One exception keeps narrative answers usable with small models: when the answer text states
  no count, list or absence, a rejected non-narrative claim is stripped from `claims` and the
  narrative answer is returned with `contract: stripped`.
- `fail` raises on any violation.

Under every policy, unknown evidence ids, missing citations, an empty answer and a stated number that
differs from the cited exact aggregate (`answer_number_mismatch`) abstain. With native structured output the generator
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

### Graph traversal templates (`pipeline.arms.graph.templates`)

A decision agent may select a **declared** traversal template by name on a graph step; it never
writes a query language. Each template compiles to calls on the frozen `GraphStore.traverse`
contract and the graph lookups, so it means the same thing on sqlite, neo4j, kuzu and the in-memory
backend (`tests/conformance/test_traversal_templates.py`).

```yaml
pipeline:
  arms:
    graph:
      backend: kg
      templates:
        fixes:        {kind: neighbors, description: "symptom -> resolved_by -> fix", seed_types: [symptom],
                       hop: {predicates: [resolved_by], direction: out}, hops: 1}
        troubleshoot: {kind: chain, seed_match: tokens, seed_types: [symptom],
                       steps: [{predicates: [has_symptom], direction: in}, {predicates: [has_root_cause, resolved_by], direction: out}]}
        related:      {kind: connect, hop: {direction: any}, hops: 2}
        mentioned_in: {kind: mentioned_in}
      ontology:       # optional; templates naming unknown predicates/types or unreachable chains are refused
        entity_types: [issue, symptom, cause, fix]
        predicates: {has_symptom: {subject_types: [issue], object_types: [symptom]}, resolved_by: {object_types: [fix]}}
```

Kinds: `neighbors` (one hop group, `hops` 1..3), `chain` (1..3 ordered hops, each with its own
predicates/direction/edge kinds), `connect` (paths of at most `hops` from the first seed ending at the
second), `mentioned_in` (span-anchored mentions of the seeds). Seeds: a stored entity id resolves to
itself; otherwise `seed_match: exact` keeps entities whose name tokens equal the seed's and `tokens`
keeps entities whose name tokens are all in the seed text; `seed_types`/`target_types` filter by
entity type; `limit` (default 50) caps the paths (reported as `truncated`, never hidden). Edge kinds
default to `fact`; co-occurrence is reached only by a template that requests it. The `llm` planner
sees `graph_templates` (name -> description) and may return a graph step with `template` and `seeds`;
an undeclared template is an `invalid_step` decision failure. The graph arm returns the paths and one
document hit per evidence document with each edge's quote, predicate, edge kind, support and
provenance. An attached kuzu store exposes its mapping as an ontology; the arm's `validate`
reports templates that do not fit it.

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

### Lexical engine `bm25` (sqlite) and lexical front policies

```yaml
backends:
  local:
    kind: sqlite
    lexical_engine: bm25          # default fts5
    bm25: {k1: 1.5, b: 0.75, epsilon: 0.25, tokenizer: unicode_word, lowercase: true, stemming: none, include_title: false}
pipeline:
  arms:
    lexical:
      backend: local
      candidates: 2000
      verbatim_boost: {enabled: false, window_tokens: 6, min_windows: 2, common_df: 5000, max_window_hits: 300, max_candidates: 1200, max_records: 5, quoted: true}
      pin_identifiers: {enabled: false, fields: [], max_records: 5}
```

`lexical_engine: bm25` replaces the FTS5 table with an Okapi BM25 inverted index built at ingest
into the generation (`snapshots/<gen>/bm25/`: memory-mapped postings, term frequencies, idf,
row lengths and row→document ordinals; the vocabulary is the `bm25_terms` table of the
generation's catalog). Scores are those of `rank_bm25.BM25Okapi` over the same token lists, bit for
bit: ATIRE idf `log(N − df + 0.5) − log(df + 0.5)`, idf below zero replaced by
`epsilon × mean(idf)`, and a repeated query token counted again. A chunk matches when it contains a
query token; equal scores rank by chunk id. `tokenizer`: `unicode_word` (`\w+`) or `ascii_alnum`
(`[a-z0-9]+` on lower-cased text; non-ASCII letters split words); `stemming`: `none` or a Snowball
algorithm (`english`, `porter`, …); `include_title` prepends the document title's tokens to each of
its chunks (FTS5 indexes the title as a weighted column; `bm25` does not unless asked). Every
`bm25` setting is index recipe: the keys enter the index fingerprint only when `bm25` is selected
(FTS5 indexes keep their fingerprint), changing one requires a rebuild, and a segment built with
other settings is refused (typed `BackendError`). The generation manifest records the analyzer as
`bm25_okapi:<tokenizer>:<case>:<stemming>`. Scope (ACL, filters, exclusions) is applied as a
document bitset before the top-k; `phrase` and `boolean` queries check adjacency on the candidate
chunks' text.

Front policies (off by default) put documents ahead of the scored lexical list, with provenance
`lexical_front` and scores above the best term score. With `selection.source_slots` these records
stay first in the lexical arm's list (selection candidate flag `leads`) and the slots mix the rest;
they are not selection pins:

- `pin_identifiers`: identifiers in the query (matches of `id` field `pattern`s in
  `metadata_schema`; `fields` narrows which) resolved by exact equality on that document field
  inside the request scope. Unlike the `exact_id` arm it does not pin in selection; it orders the
  lexical arm's own list. A named field that is not an `id` field with a pattern is a validation
  error.
- `verbatim_boost`: documents whose text holds a quoted string of the query (8–300 characters
  between quotes) or at least `min_windows` of its `window_tokens`-token windows verbatim (error
  messages, pasted excerpts). A window counts only if one of its tokens is in at most `common_df`
  chunks; a window found in more than `max_window_hits` chunks is ignored as boilerplate; a window
  whose tokens co-occur in more than `max_candidates` chunks is not verified (counted as skipped in
  the trace event `lexical.verbatim_boost`). It needs a lexical index that exposes its analyzer and
  term statistics (sqlite `lexical_engine: bm25`); with any other index it is a validation error.

### `doctor --probe`

`su-multi-arm --config c.yaml doctor --probe` constructs the engine (every validation above), calls
`probe()` on every backend (sqlite: runtime database and SQLite version; memory: in-process;
mongodb, postgres, sqlalchemy, neo4j: server ping and version; opensearch, elasticsearch: server identity) and reports each chat role's
declared capabilities next to its registry entry and, per role, `strict_schemas`: `ok` or the
strict-form problems of the schemas a native OpenAI role sends (a problem fails the probe). It makes
**no model calls** (`model_calls: 0`) and exits non-zero when a backend probe fails.

### Read-only log sources (`sources`)

`sources.<name>` declares a read-only log source. In this release the source is used only by the
`su-multi-arm sources` command. The engine never opens a source on the request path, and nothing
pulled is persisted yet (day partitions arrive in a later slice). The one kind is
`elasticsearch_rest`: Elasticsearch 7.9 or later, or OpenSearch 1.0 or later, over plain REST with
no client library. Older 7.x releases discover indices through `/{expr}/_alias`.

```yaml
sources:
  logs:
    kind: elasticsearch_rest
    url_env: LOG_SOURCE_URL            # base URL; never put credentials in the URL
    auth: basic                        # none | basic | api_key
    username_env: LOG_SOURCE_USER
    password_env: LOG_SOURCE_PASSWORD  # or api_key_env with auth: api_key
    verify_tls: true                   # ca_bundle: ./ca.pem (relative to this file)
    indices: ["*-{date}"]              # {date} is filled with index_name.date_format
    timezone: UTC                      # what the partition day means
    require_read_only_role: true       # a granted write privilege fails `sources check`
    mapping:
      timestamp: "@timestamp"
      message: message
      level: level                     # optional; per index, only where the field exists
      host: host.name
      container: kubernetes.container.name
      file: log.file.path
      offset: log.offset
      extra: [trace_id]                # declared passthrough only
      drop_fields: []                  # never requested
    index_name:                        # [<prefix>-][<deployment>-]<tenant>-<env>-<service>-<YYYY.MM.DD>
      separator: "-"
      date_format: "%Y.%m.%d"
      prefix: null                     # optional literal every name starts with (e.g. logs)
      deployment_prefixes: [search-admin, os-data-{n}]   # optional, longest first; {n} = digits
      prefix_fields: [tenant, env]     # one token each unless the field has a vocabulary
      vocabularies:                    # service and any prefix field; literal names, '_' allowed
        service: [ingest-worker, query-gateway, gateway, scheduler, slack_bot]
    counts:
      errors: {levels: [ERROR, FATAL], tokens: [error, fatal]}
    multiline:
      continuation: [java_frame, python_frame, traceback_header, caused_by, ellipsis_more]
      max_gap_ms: 2000
      max_lines: 200
      max_bytes: 16384
      fragment_sample_rate: 0.01
    pull:
      cursor: scroll                   # or search_after with a unique `tiebreaker` field
      page_size: 500
      keepalive: 1m                    # at most keepalive_max (default 2m, never above 10m)
      records_query: errors            # the records tier pulls documents matching this count query
      max_docs_per_unit: 20000         # above this the unit is stratified
      max_docs_per_partition: 500000   # the pull stops and is marked capped
      sample_method: hourly_quota      # or random_score
      seed: 0
      max_concurrency: 1               # open contexts per process <= max_concurrency x slices
      slices: 1
      max_restarts: 1                  # an expired cursor restarts its stratum at most this often
    limits: {requests_per_second: 2, timeout_seconds: 30, max_response_bytes: 67108864, max_context_per_partition: 200}
    max_line_bytes: 8192
```

**What the source may send.** Every request is checked against an exact allow-list before any
I/O. The allowed requests are:

- resolve, alias, mapping, count, search and scroll;
- opening and closing a PIT;
- `GET /` and the privilege endpoint.

A path, method, query parameter or body key outside that list raises `SourceRequestRefused`, and
nothing is sent. Further rules:

- `{idx}` must be a name that discovery returned. Wildcards are allowed only in discovery and in
  `_count`.
- Bodies may not contain scripts, runtime fields, aggregations or write operations.
- `DELETE` may only close a scroll or PIT that this source opened and that the cursor lists, and
  never `_all`.
- Redirects are not followed, and there are no retries.

**Self-check.** `sources check` asks `_security/user/_has_privileges` whether the account holds
`write`, `delete`, `index` or `manage` on the configured patterns, or `cluster:all`. A granted
privilege is a warning, or an error (exit 1) under `require_read_only_role`. If the endpoint is not
available, the result is reported as `unknown` and the allow-list remains the guarantee. OpenSearch
reports `unknown` because its account endpoint lists roles, not privileges. A 403 on `GET /` is
recorded and is not fatal.

**Index names.** Each discovered index name is parsed with the `index_name` grammar, left to
right:

1. the trailing date (`date_format`; `%Y`, `%m`, `%d` and literal separators);
2. the optional literal `prefix`, which every name must then start with;
3. the optional deployment prefix: the longest entry of `deployment_prefixes` that matches, where
   `{n}` stands for digits (`os-data-{n}` matches `os-data-1-…`). It is recorded as the
   `deployment` attribute, or `none` when the name has no deployment prefix;
4. the `service` segment at the end, and the `prefix_fields` segments in order. A segment with a
   vocabulary takes the longest entry that fits (`query-gateway` before `gateway`). A segment
   without one is exactly one token without the separator, so `slack_bot` is one token under `-`.

A name that does not fit, or that fits both with and without its deployment prefix, is `unparsed`
with a typed `reason` (`no_date`, `invalid_date`, `prefix`, `segments`, `service`, `vocabulary`,
`ambiguous`), `unknown` attributes and a `source_index_name_unparsed` diagnostic. The parser never
guesses: a multi-token tenant needs a tenant vocabulary, and an undeclared left-side prefix is not
folded into the tenant.

**Counts.** Count queries are built for each index from its mapping:

- a terms query on `mapping.level` (or its `.keyword` sub-field) where that field exists, recorded
  with basis `level_field`;
- otherwise a word query on the message field, with basis `token_match`;
- an index that has neither is a `SourceSchemaDrift` error, never a zero.

**Units and sampling.**

- A dated expression (`{date}`) gives one unit per index: the index date selects the day. An
  undated expression gives units sliced to the day window by event time.
- When a unit's records-tier count exceeds `max_docs_per_unit`, the pull is stratified.
  `hourly_quota` gives each hour of the window, plus one `outside` stratum, a quota proportional to
  its count. Within an hour, documents are ordered by `random_score` on `_seq_no`. `random_score`
  instead caps the whole unit.
- The sample plan records the method, seed, and each stratum's counts, quotas and fetched documents.
  Estimates from it are approximate and labelled as such.

**Secrets.** The URL and credentials are read from the named environment variables when the first
request is made. They never appear in a config value, an error, a request record or a report. The
HTTP client ignores proxy environment variables.

**Not in this release.** Redaction of pulled lines (`LogMapping` accepts an injected redactor and
reports `redaction: none` without one). PIT-based pulls (PIT is probed only). Sliced scroll.
Data-stream backing indices (skipped with a diagnostic). User-defined continuation patterns.

**CLI.**

```bash
su-multi-arm --config c.yaml sources check                       # capabilities + self-check
su-multi-arm --config c.yaml sources discover --date 2026-01-05  # units and parsed attributes
su-multi-arm --config c.yaml sources count --date 2026-01-05     # per-unit counts and bases
su-multi-arm --config c.yaml sources probe --date 2026-01-05 --max-units 3 --max-docs 1000 --output probe.json
```

`probe` puts a bounded load on the cluster and writes an aggregate-only JSON report:

- capabilities and the self-check;
- per-unit counts and bases, field presence and index-name parse results;
- PIT and `random_score` support;
- scroll throughput on one unit of at most `--max-docs` documents;
- request timings for each allow-list row.

The report contains no log line or field value. Keep reports outside any repository. `sources`
never builds an engine. Open cursors are closed on exit, on `SIGTERM` and on `SIGINT`. A context
that a hard kill leaks expires after the bounded keepalive.

### Daily log partitions, compose and retention (`logs`)

`logs` turns one source into day partitions and composes the retained window into the serving
generation (addendum A1 slices L3a and L3b). It needs the `pattern` extractor, the `line` chunk
recipe for the `exemplars` field, and a catalog backend whose generation registry has pins
(`sqlite`, `memory`, `postgres`, `sqlalchemy`; `logs` is refused on `mongodb` and
`elasticsearch` catalogs, where pins are planned).

```yaml
ingestion:
  line_recipe:                         # one chunk per log record (records separated by a blank line)
    fields: [exemplars]
    max_chunk_chars: 24576             # >= sources.<s>.max_line_bytes + multiline.max_bytes
  projections:                         # catalog and records always hold every document
    exclude_kinds:
      lexical: [unit_day, signature_day, service_day, signature_service_day, signature_tenant_window, signature_hour]
      vector: [unit_day, signature_day, service_day, signature_service_day, signature_window, signature_tenant_window, signature_hour]
      graph: [unit_day, signature_day, service_day, signature_service_day, signature_window, signature_tenant_window, signature_hour]
    embed_fields: {signature: [template]}   # exemplars (log-line text) are never embedded
logs:
  source: logs                         # a name under sources:
  timezone: Europe/Berlin              # what "today" means; the event_time clock cuts days here
  partitions:
    path: ./partitions                 # relative to this file; keep it outside any served directory
    clock: index_date                  # or event_time
  parse: {workers: 1, batch_size: 2000, max_exemplars_per_signature: 3, max_unparsed: 2000}
  compose: {signature_hour: false, max_exemplars_per_signature: 3}
  retention:
    keep_days: 10
    keep_generations: 2                # the active one included; rule (a) overrides it
    max_pin_wait_seconds: 900
    pin_retry_seconds: 15
    verify_sample: 1000
  pins: {ttl_seconds: 120, heartbeat_seconds: 30}
  schedule: {lock_ttl_seconds: 3600}
  settle: {max_growth: 0.05}
```

**Partition clock and timezone.**

- `index_date` (default): a partition is one index date of the source, read in
  `sources.<s>.timezone`. Documents carry `date` = the index date, `date_basis: index_date` and
  `date_timezone` = the source timezone. A reporting day in another timezone is not re-cut.
- `event_time`: a partition is one calendar day in `logs.timezone`, from local midnight to the next
  local midnight (23 or 25 hours across a DST change). The source is asked for every index date
  that window overlaps, and each unit is sliced to the window by event time. Documents carry
  `date_basis: event_time` and `date_timezone` = `logs.timezone`. With `timezone: UTC` this is the
  addendum's `event_time_utc`.
- Every header and every dated document states its basis and timezone, so an exactness claim can
  name them.

**What a day becomes.** Each record's head line is matched once by the pattern library. A sealed
partition holds the redacted records per unit, the per-record matches (no text), the day's unit
and signature aggregates, the hourly co-firing cells (signature x deployment x hour), up to
`max_exemplars_per_signature` exemplars per signature and the unparsed queue. Compose turns the
window into documents:

| Kind | One per | Notes |
|---|---|---|
| `unit_day` | day x unit | exact source counts per count query and basis |
| `signature_day` | day x unit x signature | exact only under the coverage condition, else `approximate (method)` |
| `service_day` | day x service | sums, tenants affected |
| `signature_service_day` | day x service x signature | trend rollup |
| `signature_hour` | hour x service x signature | opt-in (`compose.signature_hour`) |
| `signature_window` | signature | first/last seen, lines in the window |
| `signature_tenant_window` | signature x tenant | tenants affected by a signature |
| `signature` | signature | `template` (from the library entry) and `exemplars` (redacted lines, `line` recipe) |

Every document has a rendered `header` field (`kind: … | date: … (basis, tz) | service: … | …`).

**Commands.**

```bash
su-multi-arm --config c.yaml logs run-daily [--date YYYY-MM-DD]   # pull, parse, seal, compose, retention, verify
su-multi-arm --config c.yaml logs backfill --from 2026-01-01 --date 2026-01-10
su-multi-arm --config c.yaml logs settle --date 2026-01-05        # re-pull units that grew
su-multi-arm --config c.yaml logs compose --date 2026-01-05       # sealed partitions only
su-multi-arm --config c.yaml retention report|verify|run [--date YYYY-MM-DD]
```

`run-daily` holds only the schedule lock (`<partitions>/.schedule.lock`); compose and `gc` take the
registry write lease themselves. A second run while one holds the lock is refused
(`WriteConflict(held)`). Each unit is stored atomically, so a crashed pull resumes at the next unit
without duplicates. A day that is already sealed with the same source query and library is neither
pulled nor composed again. Compose reparses any partition whose library identity is stale, from its
retained records, before building.

**Redaction is required.** Nothing is pulled without a redactor: `run-daily`, `backfill` and
`settle` refuse with `RedactionRequired` before any request. The safe redaction contract (slice
B11) is not in this release, so the CLI refuses these three commands today; the Python API
(`LogsPipeline(..., redactor=...)`) accepts an injected redactor.

**Retention.** When a day leaves the window:

- every non-active generation whose manifest lists the day is deleted (rule (a)), whatever
  `keep_generations` says; `keep_generations` bounds only in-window rollback generations;
- a generation pinned by a live serving process (cross-process pins, TTL) is not deleted; retention
  retries up to `max_pin_wait_seconds`, then reports `pinned_expired_generation` and keeps the
  day's artifacts, and `verify` fails;
- the cache is invalidated per deleted generation; the ledger drops the generation's job entries,
  every reply tagged with the day (`partition:<source>:<day>`), and every `extraction:`,
  `embedding:`, `raw:` or `pattern_library:` entry no remaining generation references
  (`ledger_refs`);
- the partition becomes a content-free tombstone holding hashes of its lines and source ids;
- sessions expire with a TTL no longer than the window; run reports rotate at the window length;
  a content-free audit record of each deleted generation goes to `<partitions>/audit/`.

If compose fails, retention is skipped for that run: nothing is deleted before its replacement
serves. `retention verify` checks the remaining manifests, the ledger references and tags, and
hashes every text-bearing row of the remaining generations, the ledger and the live partitions
against the tombstones. `retention report` lists what is kept, what the next run deletes, and the
approved pattern examples kept beyond the window (owner decision O7).

**Not in this release.** The pull window (`limits.window`), learning-loop proposals and their
`expire` records (L4b), co-firing communities and `issue_report` documents (L5).

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
