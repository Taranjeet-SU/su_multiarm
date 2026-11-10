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

Chat roles (`models.roles.planner|reviewer|extractor|generator|community|query_writer`) take
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
`reasoning_effort`).

Anthropic thinking and effort (2026-11-09; per model, from the registry, which records the thinking
types and effort levels the provider's model-capabilities endpoint reported):

| role key | `claude-sonnet-5*` | `claude-haiku-5*` | `claude-haiku-4*` | other Anthropic ids |
|---|---|---|---|---|
| `thinking: off` | sent as `{type: between_tools}` (it cannot disable thinking) | `{type: disabled}`, only with `effort` unset or at most `high` | no `thinking` field (it does not think unless asked) | rejected |
| `thinking: adaptive` | `{type: adaptive}` | `{type: adaptive}` | rejected | rejected |
| `thinking: between_tools` | `{type: between_tools}` | rejected | rejected | rejected |
| `thinking: enabled` + `thinking_budget_tokens` | rejected | rejected | `{type: enabled, budget_tokens}` (budget below `max_tokens`; a budget alone means `enabled`; not with native forced tool use: set `structured_output: none`) | rejected |
| `effort: low|medium|high|xhigh|max` | `output_config.effort` | `output_config.effort` | rejected | rejected |

Unset keys send nothing (the model's default; Haiku 5.x thinks by default). Each call record and
`model_call` trace event carries `thinking` (the type sent, `not_sent` when `off` sends no field),
`thinking_requested` and `effort`. The wire form `between_tools` follows the capability name the
endpoint reports; it has not been exercised in a paid call yet (contract-mocked). `thinking` and
`effort` are Anthropic keys; other providers keep `reasoning_effort`.

`measure_first_token: true` (needs `capabilities.streaming: true`) streams the role's calls even when
no client consumes the deltas, so the call record carries `first_token_seconds` (call start to the
first reply text or JSON fragment; thinking is not a reply token). Answers carry
`trace.generator_latency` (`seconds`, `first_token_seconds` (`null` for a non-streamed call),
`stream`, `thinking`, `effort`, `reasoning_tokens`) for the last generator call.

`claude-haiku-4-5` as the generator: native structured output is forced tool use (no thinking, no
effort levels); price it through the operator's `budgets.price_table` (no prices are bundled), e.g.
`claude-haiku-4-5: {input_per_million: 1.0, output_per_million: 5.0}` (the rate the owner supplied
on 2026-11-08; check the provider's current price list). `openai` always uses `max_completion_tokens`; `anthropic` always uses
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
  | `share` numerator per group (`group_by`) | not offered natively (2026-11-09) | `not_supported_by_backend`, `bounded`; use `records.aggregation: client` |
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

#### Moving a store: `index adopt`

The index fingerprint covers each backend's recipe, and a file backend's recipe includes its `path`,
so a state directory moved or copied to another path is refused ("index/config fingerprint
mismatch"). Instead of a rebuild, adopt it explicitly:

```bash
su-multi-arm --config new.yaml index adopt --from-config old.yaml [--dry-run]
```

`adopt` checks that the active generation of the store `new.yaml` points at was built by
`old.yaml`'s recipe (its fingerprint), and that the two index recipes differ **only** in location
fields: `backends.<name>.path`, or the path of an imported file pinned by a digest (for example
`communities.imported.path` next to its `sha256`) whose file at the new path still has that digest.
Anything else (chunking, embedder, privacy, extraction, a digest, a backend kind) is refused. It then
re-stamps the active generation's manifest for `new.yaml` (fingerprint, config and legacy
fingerprints) in every registry that holds it, under the write lease, with a `relocations` audit
entry (time, both fingerprints, every changed field and verified file), and checks the generation
pins. `--dry-run` reports the same checks without writing. Precomputed embedding imports enter the
fingerprint by digest only, so moving those files needs no adoption.

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

### Safe redaction engine (`privacy.engine: safe`)

```yaml
privacy:
  engine: safe                       # legacy (default): redact_emails + patterns with Python re
  regex_engine: re2                  # re2 (extra `patterns`) or regex (development; per-call timeout)
  redactors: [email, ipv4, ipv6, bearer_token, jwt, api_key, aws_key, url_credentials, uuid, request_id,
              llm_text, content, query, tracking_id, record_payload, pg_row]
  actions: {query: hash}             # per built-in: mask (default) or hash
  record_fields_allow: [city]        # record_payload: drop names from the built-in entity field list
  # record_fields: [customer_ref]    # ... or replace the list
  # person_name_fields: [author, assignee]  # person_name: replace the key list (case-insensitive)
  # record_list_fields: [customers]  # record_list: replace the list keys
  # prose_products: [Kafka, Example Search]  # product_prose: required with it; the names stay visible
  custom:                            # user redactors, validated like library patterns
    - {name: ticket, regex: '\bTKT-[0-9]{4,8}\b', action: hash}
  hash_key_env: SU_REDACT_KEY        # optional key of the hash action (keyed sha256)
```

Every redactor, built-in or user, must be in the RE2 subset and pass the pattern library's ReDoS
lint; a pattern that does not is a configuration error at load. The configured engine runs them;
a missing engine is a typed error and a `regex`-engine timeout fails the redaction (the text is
refused, never passed through). Built-ins run in a fixed order, then `custom`, then the legacy
`patterns` (as case-insensitive masks).

| Redactor | Replaces | With |
|---|---|---|
| `email`, `ipv4`, `ipv6`, `jwt`, `aws_key`, `hex_secret` (opt-in, flags hashes) | the shape | `[REDACTED_<NAME>]` |
| `bearer_token`, `api_key` (`api_key=`, `password:`, `token=`, `sk-…`, `ghp_…`, `xoxb-…`), `url_credentials`, `query_values` | the value (the key and the URL scheme stay) | `[REDACTED_<NAME>]` |
| `uuid`, `request_id` | the id | `[UUID:<hash>]`, `[REQUEST_ID:<hash>]` |
| `llm_text` | `prompt=`, `completion=` … values (to the end of the line when unquoted) and JSON `messages`, `content`, `prompt`, `completion`, `choices` values | `<llm_text>` |
| `content` | retrieved documents: JSON `_source`, `body`, `passage`, `snippet`, `page_content`, `highlight` … values (whole objects), `passage:` lines | `<content>` |
| `query` | end-user query and spell-check words: `q=`, `query=`, `search_term=`, `didyoumean=`, `suggestion=` … | `<query>`, or a hash with `actions: {query: hash}` |
| `tracking_id` | `Cookie:`/`Set-Cookie:` header values (to the end of the line), `_ga`, `sessionid`, `visitor_id`, `device_id` … | `[TRACKING_ID:<hash>]` |
| `record_payload` | values of named JSON fields of structured records (names, tax ids, addresses, contact data; objects and arrays whole) | `<record>` |
| `phone` | phone numbers: `+<country code>` with digit groups (spaces, dots, dashes, an area code in parentheses), `(415) 555-0100`, any shape after `phone`/`mobile`/`tel`/`fax`/`msisdn` keys; 7–15 digits (8+ after `+`) | `[REDACTED_PHONE]` |
| `long_numeric_id` | 13–19 digit runs (card, account and national ids; also epoch-millisecond timestamps) | `[LONG_NUMERIC_ID:<hash>]` |
| `person_name` | person-name values of JSON fields and `key=value` text (`name`, `firstName`, `lastName`, `fullName`, `displayName`, `userName`, `author` and snake_case forms; `person_name_fields` replaces the list); an unquoted value takes up to three following capitalised words | `[REDACTED_PERSON_NAME]` |
| `record_list` | a JSON array of objects under a list key (`customers`, `accounts`, `records`, `items`, `results` …; `record_list_fields` replaces the list), whole | `<record>` |
| `product_prose` | the free text after a configured product or technology name (`prose_products`), to the end of the line; the names stay visible and a later name does not stop the masking (`Kafka <prose> Redis <prose>`) | `<prose>` |
| `pg_row` | PostgreSQL row data: `Key (cols)=(values)`, `Failing row contains (values)`, logged parameters `$1 = '…'` | `<value>` (column names stay) |

Quoted values are replaced inside their quotes, so JSON stays parseable. Redaction is idempotent:
a token or marker is never redacted again. `<hash>` is 12 hex digits of a sha256 of the value
(keyed with `hash_key_env` when set). Under `engine: safe` the same redactor runs on ingested
documents (title, fields, `metadata_fields`), on log records before any partition write (message,
stack, string `extra` values) and on every prompt line of the pattern learner. The redaction recipe
has a stamp (`safe:<sha16>`) recorded in partition manifests with replacement counts per redactor;
the learner's egress policy admits only lines with the current stamp. The new keys are omitted from
the index fingerprint while they hold their defaults; changing the redaction changes the index
identity. `engine: safe` has no schema_version 1 equivalent.

**Leak scan.** `su-multi-arm --config c.yaml privacy scan PATH… [--redactors a,b] [--max-findings N]
[--output report.json]` scans text files, `.gz` files and SQLite databases (every text column) with
the configured safe redactors (or the named ones, or every built-in under `engine: legacy`) and
exits 1 on any hit. The report lists path, location (line, or `table.column#rowid`) and redactor per
finding, never the matched text. Like every built-in, the five redactors above `pg_row` are off
until listed in `redactors`. The pattern learner's egress detector also checks `phone` and
`record_list` shapes; it does not use `long_numeric_id`, `person_name` or `product_prose` (ids,
timestamps and component names), so enable those as redactors where they apply. Known false positives: `api_key` flags the lease `"token"` in
generation manifests, and `hex_secret` flags the package's own sha ids.

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

**Stages per line**, in this fixed order (priority orders entries within a stage, never across
kinds): `format` (first match by priority, then id) hands its `then` group to the next stages; a
`noise` entry with `precedence: before_message` that matches marks the line benign and ends matching
(no message, no annotations: health checks and heartbeats that a broad message pattern would
otherwise sign); `message` (first match sets the signature); every matching `annotate` pattern adds
entities; without a message match, a `noise` entry with the default precedence (`after_message`)
marks the line benign. `precedence` is valid on `noise` entries only, and an unset value leaves the
library identity unchanged. The resulting status is one of `parsed`, `noise`,
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
`coverage` and `mine` measure what ingest measures: each input line (or JSONL row) is one log
document, joined by the multiline rules of the configured source (`--source`, default the only one)
plus the library's `continuation` entries; continuation lines join their head, head-less ones are
`fragment`s (reported, excluded from the coverage target, never mined). A JSONL row may carry
`ts` (ISO 8601 with offset) and `container`/`file` attributes, which order the rows and separate the
streams; a `text` with embedded newlines is one record whose first line is the head.
`--no-multiline` matches every line as a head. `patterns lint` prints every issue (also as
structured `issue_rows`) and exits 1 when the library has errors.
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
   carry them. The examples stored with a written entry (`learned.yaml`, `proposed.yaml` and the
   decision record) are pseudonymised the same way (case-insensitive, token-bounded); an example
   the entry's regex no longer matches after that is left out and counted in
   `provenance.examples_dropped_after_pseudonymisation`, and an entry with no positive example left
   is not written.
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

### LLM fallback extraction and accounted coverage (`learning.extraction`, `logs.accounting`)

**Preview (addendum A2 rev 2–4, slices C1–C4).** Evidence so far: synthetic data and mocked
providers only. The fallback runs **after** a day partition is sealed. It handles the heads the
pattern library could not parse (residue) and the heads an approved pattern matched without a
required field (field gaps). Its result is an **overlay** next to the sealed version; the sealed
partition itself is never changed.

```yaml
logs:
  accounting:
    required_fields: {http: [status_code, endpoint, latency_ms]}   # per signature category
    validators:                                                    # range_invalid when violated
      status_code: {min: 100, max: 599}
      endpoint: {regex: '^/'}
    targets: {deterministic: 0.90, extracted: 0.99, info_tier: 0.80, core_services: []}
learning:
  patterns: {spend_ledger: state/spend.sqlite, max_calls_per_day: 150}   # cluster calls run here
  extraction:
    enabled: true
    fields: {status_code: integer, endpoint: string, latency_ms: integer, dependency: string}
    template: {max_clusters_per_day: 150, min_support: 3, max_lines_per_prompt: 8, write_proposals: true}
    fill: {enabled: true, max_per_day: 200}
    lines: {enabled: false, max_per_day: 200, batch_size: 8, max_line_chars: 2000}
    grounding: {levels: [exact, normalised]}       # relocated is refused; no fuzzy matching
    egress: {allow_hosted: false, max_refused_share: 0.01, pseudonymise: [tenant, host]}
    budget: {max_usd_per_day: null, max_usd_per_run: null, max_calls_per_day: 400}
    stop_file: null                                # default <ledger dir>/.fallback.stop
models:
  routes:                                          # ordered; empty = models.roles.<role> alone
    pattern_learner:
      - {name: local_a, role: {provider: openai_compatible, model: local-model, base_url: http://127.0.0.1:8000/v1}}
      - {name: local_b, role: {provider: openai_compatible, model: local-model, base_url: http://gpu.localhost:8000/v1},
         capacity_url: http://gpu.localhost:8000/capacity}
      - {name: paid, role: {provider: anthropic, model: <model id>, api_key_env: ANTHROPIC_API_KEY}}
    log_extractor:
      - {name: local_a, role: {provider: openai_compatible, model: local-model, base_url: http://127.0.0.1:8000/v1}}
    allow_paid_fallback: false                     # a paid route after a local one is used only when true
budgets:
  price_table: prices.yaml
  providers: {anthropic: {max_usd_total: 2.00}}    # lifetime cap per provider key (persistent ledger)
```

- **Final status per head.** One of `parsed`, `parsed_filled`, `provisional`, `model_line`,
  `noise` or `unresolved` with one reason: `format_only`, `no_cluster`, `budget_deferred`,
  `below_rank`, `model_declined`, `gate_failed`, `grounding_failed`, `type_invalid`,
  `range_invalid`, `egress_refused`, `timeout`, `truncated_unmatched`, `fallback_pending`,
  `capacity_deferred`, `not_attempted`, `field_gap` or `provider_error`. With the fallback enabled,
  residue heads are sealed `fallback_pending` until their overlay exists.
- **Accounting.** The counting unit is the source document. The seal is refused
  (`AccountingImbalance`) when the documents the puller handed over differ from the per-stratum
  fetched counts (Q), from `malformed + heads + joined + fragments + overflow` (D), or when the heads
  differ from the sum of final statuses (H). `rest` is derived in this release, so identity S is
  reported, not enforced. Units stored before this release report `recorded: false`.
- **Cluster first.** One `pattern_learner` call per residue cluster labels the synthesised
  regex's groups (`v1…`, `<mask>1…`) or proposes a regex. Either goes through the full gate on the
  current lines, and values are group spans. Per-line calls (`log_extractor`) return quotes that must
  be located in their own line, or the field is dropped. A model value never overwrites a pattern
  value.
- **Spend.** Local (loopback) routes record calls and tokens at USD 0, and USD caps apply to paid
  routes only. A paid route is refused at load unless `budgets.price_table`,
  `budgets.providers.<provider>.max_usd_total` and finite daily and run USD caps for its scope are
  all set. Reaching any cap stops the calls (`budget_deferred`); there is no fallback to another
  provider. A route that fails its health or capacity probe is skipped. With no route available,
  items are `capacity_deferred` and the next run retries them.
- **Reply cache.** Replies are keyed by template sha (or line sha), service, level, provider, model,
  prompt version and schema version. A hit reuses only labels, regex and quotes; the gate and the
  grounding run again on the current lines. Replies tagged only with expired partitions are purged.
- **Stop.** Creating the stop file, or sending SIGTERM, stops the run between calls. The rest is
  `capacity_deferred` and the run state is `paused`.
- **Coverage.** `su-multi-arm logs coverage --date D [--from D0]` prints the regex-only and
  with-model numbers side by side, per service and tier, with fragment accounting and the owner
  targets. It exits 1 when a day is open (for example a refused seal) or an identity fails.

**Route tiers, windows and limits (rev 4).** Each `models.routes.<role>[]` entry can declare:

- `tier`: `local` (must be loopback), `lan` (a self-hosted `openai_compatible` server on the
  private network, which needs `egress.allow_lan: true`) or `paid`. When unset, the tier is `local`
  for a loopback role and `paid` otherwise.
- `windows`: a list of UTC `HH:MM-HH:MM` windows (a window may wrap midnight). Outside every
  window the route is skipped (`outside_window`).
- `max_invalid_replies`: default 3. After that many schema violations in a run the route is marked
  down and later items go to the next route.

`local` and `lan` calls record calls and tokens at USD 0. Every paid route is refused at load unless
the provider has a finite `max_usd_total` and every scope that can reach it (`learning.patterns`,
`learning.extraction`, `learning.bootstrap` when enabled) has finite daily caps.

```yaml
learning:
  bootstrap:                       # off by default
    enabled: false
    require_measured_throughput: true   # refuse to start without `patterns measure-throughput`
    days: 3
    max_clusters_per_day: 150       # bounded further by measured calls/hour x hours_per_day
    hours_per_day: 8
    max_usd_per_day: null           # required (finite) when a paid pattern_learner route exists
serving:
  answers:                          # answer-time spend scope, off by default
    enabled: false
    max_usd_per_day: null           # required when a paid answer role's provider has a lifetime cap
    max_calls_per_day: 10000
    stale_reservation_seconds: 3600 # a hold still open after this long (crashed process) is released, audited
```

Every spend ledger (learning scopes, `serving.answers`, provider lifetime caps) keeps one
`spend_open` row per reservation. A reservation left open by a crashed or killed process is released
after a bounded time (`serving.answers.stale_reservation_seconds`; 3600 s for the learning ledgers)
on the next reservation or when a ledger is opened: the USD hold is returned, the call count stays
(the call may have been sent), and the release is written to the ledger's `spend_audit` table and
logged as a warning. A commit arriving after its hold was released still records the spend
(`late_commit` in the audit), without subtracting the hold twice. Holds taken by an earlier version (no
`spend_open` row) are not released automatically.

- **Bootstrap** sends cluster calls to self-hosted routes only; paid routes report
  `bootstrap_self_hosted_only`. The cap counts only clusters that need a model call: a cluster with a
  pending proposal or a cached reply never takes a slot.
- **`su-multi-arm patterns measure-throughput --tier local|lan|paid --clusters 50 --date D`** makes
  real calls on one tier over the residue of a sealed day, under every cap and the egress policy. It
  records calls/hour and tokens/s in `throughput.json` next to the spend ledger. The replies go to
  the reply cache, and nothing is written to the library.
- **`serving.answers`**: planner, generator, reviewer and community calls reserve on a persistent
  per-day ledger under scope `serving.answers`, and on `budgets.providers.<provider>.max_usd_total`
  when the provider has one. A refusal comes before the request.
- **`patterns learn|propose`** also stops cleanly between calls when
  `learning.patterns.stop_file` (default `<library>/.learn.stop`) exists or SIGTERM arrives. The run
  is then `paused` (exit 0). `--output` is rewritten after every cluster, and a template with a
  pending proposal gets no new call.

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
`resolution`), `request.context_max_bytes` / `context_max_field_chars` bound its size (131072 bytes of
UTF-8 JSON — enough for nine 4000-character fields of any text — and 4000 characters per field by
default; a larger context is refused with 422 naming the limit) and `request.canonical_fields` names the document fields or metadata
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
`metadata_schema`. The record keys `id`, `source`, `dataset` and `kind` are always available, as
request filters and in compiled scopes (string equality; a non-string value is `type_mismatch`).
Each aggregate also names its `measure` (`{"kind": "count", "of": "records"}`, or e.g.
`{"kind": "sum", "field": "lines"}` for a grouped sum, whose `operation` stays `group_count`). Missing/invalid timestamps
are counted; valid timestamp values need an explicit timezone. A natural-language keyword does not
secretly filter records. Unsupported filters/count scopes need clarification in the application.

### Per-request evidence budget (`SearchRequest.evidence_chars`, `request.evidence_chars_max`)

An answer request may carry `evidence_chars` (500–2,000,000): the characters of packed evidence
handed to the generator for this request, replacing the configured budget
(`models.roles.generator.evidence_budget.chars`, else `pipeline.selection.packing.budget_chars`).
It is **opt-in**: unless `request.evidence_chars_max` is set, a request carrying it is rejected
(422 on HTTP; `RequestRejected` in Python), and a value above the maximum is rejected too, never
clipped. The maximum may be above the configured budget (a deployment allows larger evidence on
request) or below it. The effective value is recorded as `trace.evidence.budget_chars`. MCP
`search`/`answer` accept the same argument.

```yaml
request:
  evidence_chars_max: 120000   # default null: per-request budgets refused
```

### Paged listings (`Engine.search_page`, `POST /v2/search/page`)

`search_page` wraps an ordinary `SearchRequest` with listing controls and returns a `SearchPage`.
The search runs exactly as `Engine.search` (which is unchanged); the listing is computed over the
**retrieved candidates** — the selection's bounded hit list, never the corpus:

```json
{
  "request": {"query": "billing invoices", "filters": {"source": "case"}},
  "page": 2, "page_size": 10,
  "sort": "created_at", "order": "desc",
  "any_of": {"status": ["open", "pending"], "project": ["Alpha"]},
  "facets": ["project", "kind"], "facet_scope": "candidates", "facet_limit": 20,
  "text_chars": 8000,
  "include_items": true
}
```

| Control | Meaning |
|---|---|
| `page`, `page_size` | 1-based page over the candidates; `page_size` ≤ `serving.http.page_size_max`. The response has `total` (candidates), `total_relation: "candidates"` (the corpus may hold more matches) and `pages`. Items keep their overall `rank`. |
| `sort`, `order` | `relevance` (selection order) or a metadata field declared `sortable` (dates/datetimes compare as instants, naive values read as UTC; numbers numerically; strings case-folded). Records without a usable value go last in both orders and are counted in `sort_missing`; ties keep relevance order. Any other field is rejected. |
| `any_of` | Multi-value filters: OR within a field, AND across fields, AND with `filters`. Pushed down to every arm, aggregate and value listing as `in`/`eq` predicates of the run's access scope (the same path as ACL constraints), so retrieval itself is narrowed. Each value is validated like `filters`; an unobserved or invalid value is applied as given and reported in `unsupported_constraints`. Fields must be declared (or a record key) and filterable. |
| `facets`, `facet_scope`, `facet_limit` | Terms counts of `groupable` fields. `candidates` (default): counts over the candidates, `exactness: bounded`, with `missing` (no value) and `other` (beyond `facet_limit`). `filtered_corpus`: a `group_by` aggregate over the records matching `filters`, `any_of` and the access scope, **without** the query text; exactness comes from the records backend (`exact` on sqlite/memory). |
| `text_chars` | Budget for the hit texts on the page, spent in page order; a cut text is `text_truncated` and listed in `text_budget.truncated_ids`. |
| `include_items` | `false`: totals and facets only (a "filter options" call). |

Each item carries the hit plus its catalog `metadata` (read once, bounded by the hit count).

### Legacy QA compatibility layer (`su_multi_arm.compat.legacy_qa`)

A FastAPI app that serves an earlier question-answering HTTP contract on package engines, so an
existing web UI or MCP gateway can run unchanged against the package. It is configured by a
**profile** (YAML/JSON, data only; it never names Python objects), separate from the engine
configuration, and started with
`python -m su_multi_arm.compat.legacy_qa --profile PROFILE.yaml --bind 127.0.0.1:8110` (loopback
only unless `--allow-non-loopback`; the adapter has no inbound authentication).

```yaml
schema_version: 1
datasets:
  - id: kb                     # legacy dataset id
    label: Knowledge base
    default: true
    config: cfg/kb.yaml        # package configuration (relative to the profile)
    modes:                     # legacy mode -> allowlisted SearchRequest pipeline/mode
      hybrid: {}
      agentic: {pipeline: agentic}   # unmapped: a request for it is refused (422)
    citations: evidence        # evidence: every packed item, numbered | cited: only cited items (numbers kept)
    citation_url: external_first     # external_first | source_first | external_only
    content_source: {ticket: Support ticket, doc: Documentation}   # Document.source -> label
    record_context: {keys: {recordId: record_id, subject: subject}} # targets must be in request.context_keys
    source:                    # GET /source record view
      enabled: true            # needs the get_document tool
      id_pattern: '^(ticket:[0-9]+|https://docs\.example\.org/.+)$'
      sections: [{field: description, heading: Description}]
      repeated_field_prefix: comment_    # comment_1, comment_2 -> "Comment 1", "Comment 2"
      section_limit: 40
      metadata_keys: [status, created_date]
      title_fallback_field: description  # title missing -> first line, title_derived: true
      hide: [{field: visibility, values: [internal]}]   # visibility predicate, on every response (below)
      redact: true             # credential-like strings masked in every outgoing text (below)
      ui_link:                 # optional: links an unmodified earlier UI record page can render
        dataset: old_kb        # the dataset token that page accepts (URL parameter only)
        channels: [ui]         # X-QA-Channel values that get alias links (others: canonical links)
        id_pattern: '^case:[A-Za-z0-9_-]+$'    # ids that page accepts; every alias is checked
        rules: [{package_prefix: 'ticket:', alias_prefix: 'case:t-'}]  # reversible; GET /source resolves back
    search:                    # POST /api/search
      enabled: true
      date_field: created_date # sortBy date/post_time (must be sortable)
      facets: [{key: status, field: status, label: Status}]   # fields must be groupable
      facet_scope: candidates
      default_page_size: 20
      guidance: "..."          # static usage text (default: neutral grounding text); null: none
      enrich: {enabled: false, max_results: 10, snippet_chars: 500, summary_chars: 300}  # model notes, metered
    session_cache:             # follow-ups answered from the session's prior evidence (default off)
      enabled: false
      ttl_seconds: 3600
      max_sessions: 5000       # in-memory anchors (a restart empties them; next turn runs a full search)
      max_evidence: 40
      record_id_patterns: ['(?<=#)\d{3,6}\b']  # an id the anchor does not contain -> full search
      # followup_cues: [...]   # regexes; default: summarize/shorten/explain-that/"the second step"/...
  - id: old_kb
    retired: true              # never listed; every call naming it -> 422 "not available"
models: [{id: fast, provider: anthropic, label: model-name}]   # GET /api/models (informational)
default_model: fast
research_files: {clusters: files/clusters.json}   # optional read-only JSON for /api/clusters|evolution|replay-runs
clusters: {dataset: kb, field: status, top_groups: 20, representatives: 5, representative_sort: created_date}
                               # /api/clusters from package data (metadata groups; wins over research_files)
warmup: true                   # start-up warm-up (background): the query embedder + Engine.warmup (pinned units, live log strata, vectors)
warmup_timeout_seconds: 300    # a warm-up still running after this is a /ready problem (still not ready)
sessions: true                 # X-QA-Session / body session_id bind a package session
request_log: logs/requests.jsonl   # one summary row per call (question hash and length, never text)
heartbeat_seconds: 10
max_concurrent_answers: 16     # answers in flight (/api/qa + /api/qa/stream); one more -> 503 + Retry-After
answer_deadline_seconds: 170   # cancel an answer after this (504); keep it below every caller's timeout
session_sweep_seconds: 600     # delete expired package sessions at most this often
```

**Serving behaviour.**

* **Sessions.** An `X-QA-Session` value whose package session expired (`sessions.ttl_seconds` of the
  engine configuration) gets a fresh session (`session_cache.restarted: true`, decision
  `session_restarted`, `session_restarted` in the request log); it is never refused. Expired
  sessions are deleted every `session_sweep_seconds`.
* **Admission, deadline, disconnect.** At most `max_concurrent_answers` answers run at once; one more
  is refused with 503 `{detail}` and `Retry-After`. An answer is cancelled when the client
  disconnects (`/api/qa` as well as the stream; logged as 499 with the USD spent so far) and when
  `answer_deadline_seconds` passes (504). The engine's `budgets.deadline_seconds` applies too; the
  shorter wins. Health, readiness, catalog, models, research files, clusters and the source view run
  on their own small thread limiter (or need no thread), so they answer while answers are in flight.
* **Errors.** An unclassified failure (an adapter or engine bug) is a 500 whose detail names a
  request id; the traceback is logged with that id. A model reply that does not parse is 502 on every
  route. A stream that fails after it started still ends with an `error` event.
* **Readiness.** `GET /ready` (also `/ds/<id>/ready`, `?dataset=`) answers 200 only when every
  (named) served dataset has an active generation, its warm-up is done (when `warmup` is on: a
  failure reads `warm-up failed (<error class> in <adapter.operation>; see the server log)`, a run
  past `warmup_timeout_seconds` reads `warm-up timed out after N s (…; still running)` and turns
  ready when it ends), every
  API-key variable the routed pipelines need is set, and the `serving.answers` day and provider
  lifetime caps have headroom; otherwise 503 with the problems. `/health` stays liveness (200 = up)
  and carries `ready` (and per dataset `readiness`).
* **Masking and visibility on every surface.** `source.redact` masks credential-like strings in every
  outgoing text — answer, citation titles and snippets, per-arm panels, search results, evidence and
  facts, streamed tokens (released only at word boundaries so a secret is never streamed in pieces)
  — not only on the record page; it is pattern-based (redact before indexing to keep secrets from the
  generator). `source.hide` removes hidden records from every list (citations, panels, search
  results and evidence; numbering is kept), and an answer whose packed evidence contained a hidden
  record is **withheld** (fixed notice, `insufficient_evidence: true`, `compat.withheld`, a warning).
  A non-empty `hide` needs the `get_document` tool.
* **Planner failures** are visible: `warnings`, `compat.planner_failure` and the request log
  (`plan_outcome`, `decision_failure`); an abstained plan is reported with
  `insufficient_evidence: true`, never as a grounded answer.
* **Request log** rows add `request_id` (also the `X-Request-Id` response header), `trace_id`,
  `forwarded_for` (`X-Forwarded-For`), `user_agent` (160 characters), `peer`, `plan_outcome`,
  `insufficient_evidence` and, when they occur, `decision_failure`, `withheld`, `cancelled`, `refused`.

Start-up fails closed when the profile relies on something the engine lacks (source view without
`get_document`, a `record_context` target outside `request.context_keys`, a facet field that is not
`groupable`, a `date_field` that is not `sortable`, a served dataset without engine or config,
`search.enrich` or `session_cache` without a generator role, `source.hide` without `get_document`,
`clusters` on a field that is not
`groupable` or without the `aggregate`/`get_document` tools, overlapping `ui_link` prefixes, a
`ui_link.dataset` token equal to a served dataset id). Every route is also served under
`/ds/<dataset>/...` (the prefix names the dataset; `GET /ds/<id>/health` is that dataset's health:
200 up, 503 down, 410 retired), so a client with one base URL per dataset sees each one's status.
The routes, translations and every field reported as unsupported are described in
`docs/ARCHITECTURE.md` ("Legacy QA compatibility layer").

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
group key, a tie at the `top_n` boundary listed in `ties`; since 2026-11-09 a group whose key has
no value is never ranked or kept by a `top_n`/`nth` cut unless the scope has a `missing` predicate
on that field, a group-key `sort` puts it last, and a `top_n`/`nth`/`order` combined with a `sort` on
a group key is refused as `measure_unsupported` instead of ranking keys; a `time_basis` given as a
role label such as `created` names the one temporal field that declares it); group a `datetime`/`date` field by
`granularity: day|month|year` (calendar buckets in `metadata_schema.timezone`); cross-tab two
`group_by` fields; compare `periods` (half-open windows on its `time_basis`); give each group's
share (`with_share`) or a share with a `numerator` (with `group_by`, since 2026-11-09, the
numerator's share **within each group**, rankable by `order`/`top_n`/`nth` with ties reported, each
group carrying its `numerator` and `count`); keep only groups with at least `min_count` records
(`HAVING COUNT(*) >= N`, applied before ranking; needs `group_by`); count distinct values (`count_distinct`: null
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
its type (2.0.0a2: `priority 3`, `version 2.1`, `priority is 3`, `priority above 2`; a number for
`integer`/`number`, `true`/`false`/`yes`/`no` for `boolean`, an enum value for `enum`; a `boolean`
field named alone, "escalated tickets", counts too). Since V3.2 a word after a `string`/`id` label
counts only when it is bound to the field: an explicit operator (`severity = high`, `owner: alex`,
`equals`, a comparison), an identifier-shaped token containing a digit (`ref TR-9`), or a value the
resolver knows (observed for a listable field, otherwise confirmed by the record store). Prose after
a field label ("source association", "service is healthy") is not a constraint. A value or field
qualifier preceded by `not`, `non-`, `never`, `except`, `excluding`, `without`, `other than`,
`rather than`, `instead of` or an `n't` contraction is negated ("not a report"): only a predicate
with `negate: true` excluding that value applies it; a positive predicate or a request filter never
does. A field
qualifier is applied by a predicate or request filter that constrains that field to that value (a
comparison word or a bare boolean field: any predicate on the field). A candidate is applied when a predicate or a
request filter on a field holding that value constrains it to that value (a calendar expression: any
predicate on a declared `datetime`/`date` field; a quoted phrase: a predicate value equal to it). A
structured step on a question without any aggregate operator is flagged as a whole (`rules`,
`classifier`); the `llm` agent instead drops such a step before compilation (V3.2): see below. Every unapplied
candidate is an `UnsupportedConstraint` with reason `qualifier_not_applied`, the step's aggregate is
reported `bounded`, and the answer contract then refuses count, exhaustive-list and absence claims on
it. Nothing is re-planned. Caller-typed scopes (`aggregate` in the request) are the caller's and are
not checked. Limitation: free-text topic words of an aggregate question ("how many CloudTax tickets
mention rounding") are not candidates for `llm` (the `rules` agent refuses residual words itself).

Unsolicited planner scopes (V3.2, `kind: llm` and the `llm` cascade stage). When the question has
no aggregate or untargeted-list intent, contains no aggregate operator word (count, how many,
number(s) of, total, sum, list, enumerate, average, median, minimum/maximum, top N, most common,
percentage, share of, breakdown, grouped by, distribution, earliest/latest/oldest/newest, is/are
there any/no, versus, compare ...) and the request carries no typed `aggregate`/`scope`, a planner
`structured` scope step is an operation nobody asked for: it is dropped (not compiled, not
executed) and recorded in `trace.decision.dropped_steps` (`reason: unsolicited_structured`) and a
`planner_steps_dropped` event. The planner's retrieval steps run as planned; if none remain,
`trace.decision.planner_fallback` is `retrieval_after_dropped_steps` and the original query runs on
the enabled `lexical` and `dense` arms under the request filters (neither enabled: the decision fails
as `invalid_step` and the configured `on_failure` applies). `decision_fallback` still means
`raw_all_arms` only. Any operator word keeps the step and every existing guard (qualifier coverage,
the logs scope guard, the answer contract), so a requested count is never silently widened.

Question qualifiers of a dropped unsolicited scope (2026-11-08, typed-query plan M1 follow-up). A
qualifier the question carries (`decision/qualifiers.py`) that the dropped scope had bound is never
lost silently: a positive exact value on a filterable field that the `ValueResolver` resolves (not
a declared state, not contradicting a request filter, one value per field) becomes a typed `eq`
filter of the fallback `lexical`/`dense` steps (`dropped_steps[].carried_filters`); every other bound
qualifier (negated, comparison, calendar, quoted, state, contradicting, ambiguous, or any qualifier
the planner's own retrieval steps do not all filter on) is an `UnsupportedConstraint`
(`qualifier_not_applied`) on the decision and `dropped_steps[].unapplied`. Draft predicates that bind
no question qualifier were invented by the planner and stay dropped.

`pipeline.decision.on_step_failure` (`llm` planner and the `llm` cascade stage only; default
`abstain`): what a structured step that does not compile (`unresolvable_value`,
`unsupported_measure`) does to the decision. `abstain` keeps today's behaviour (the decision fails and
`on_failure` applies). `partial` drops that step as `dropped_steps[].reason: step_not_executable`,
adds its constraints and one `aggregate_intent_without_scope` per aggregate intent to the decision's
unsupported constraints, and runs the other steps. `retrieval_only` drops every structured step once
one fails and runs the planner's retrieval steps. When nothing remains, the
`retrieval_after_dropped_steps` fallback runs (neither `lexical` nor `dense` enabled: the step failure
stands). Caller-typed aggregates/scopes, disabled arms, overridden request filters and coherence
errors still fail closed under every value; the answer contract keeps refusing count, list and
absence claims without an exact scope. Not expressible in v1.

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
the front) and `packing: {budget_chars, field_priority, metadata_fields, passage_selection: prefix |
question_windows, fairness: fair_share | greedy, min_per_record, aggregate_reserve_max,
metadata_max_share, metadata_protected}`. Aggregate evidence is reserved before documents, up to
`aggregate_reserve_max` of the budget (default 0.5; an aggregate beyond it is omitted and listed in
`omitted_ids`, the cap is traced as `aggregate_reserve_chars`). Packing renders record fields in the configured `field_priority` order and then in
document order — there is no built-in `details`/`resolution` preference. Ordering never changes
union membership.

`passage_selection` defaults to `prefix`: a truncated record keeps its rendered opening, as in
earlier releases. `question_windows` is opt-in: the engine supplies the question, and a truncated
record can instead carry up to three bounded literal windows selected by lexical/English
resolution or time cues. Each window retains canonical field offsets and packed-passage offsets;
gaps and truncation are explicit. This heuristic does not establish a resolution, chronology or
cause, and a real-data packing-only replay showed mixed results, so it is not the default. The
trace records `passage_recipe` (`prefix_v1` or `question_windows_v1`, the configured recipe) and
`windowed_ids` (records whose passage actually came from windows). Configured field priority
breaks ties.

`metadata_fields` defaults to `[]` and accepts unique canonical metadata identifiers. Only
those exact scalar values enter evidence; no unrestricted metadata dump or time inference occurs.
Compact serialized metadata wrappers count against each record's character allocation.
`metadata_max_share` (default `0.25`, the V3.2 setting; `0 < x <= 1`) caps that cost: when a
record's passage and metadata together exceed its allocation, keys are dropped from the end of the
`metadata_fields` order until the wrapper fits `metadata_max_share` of the allocation; values are
never truncated. The record is kept (its passage uses the rest) and is listed in
`metadata_omitted_ids` with its dropped keys in `metadata_dropped_keys` (before V3.2 such a record
was omitted). `1.0` lets metadata use the whole allocation. The interim default `1.0` (2.0.0a3
after the V3.2 paid evaluation) was a regression found by the M7 real-data run: allowlisted
metadata took most of the evidence budget and the passage share fell from about two thirds to
about a fifth; with the plain `0.25` cap, on the other hand, a question lost the time, environment
or identity metadata it needed. `metadata_protected` (default `[]`; every entry must be in
`metadata_fields`) names the keys the cap never drops: they are kept first, still count toward the
record's allocation, and only the other keys are dropped (reverse allowlist order). Protected keys
that alone exceed the share are kept (`metadata_protected_over_cap_ids`); protected keys that alone
exceed the whole record allocation are kept and the passage text is trimmed to nothing
(`metadata_protected_overflow`: record id -> characters over the allocation; the evidence total
exceeds the budget by exactly that sum). Keep the protected list short. Trace fields
`metadata_characters` and `evidence_characters` distinguish metadata from passage cost.
`metadata_show_null` (default `[]`; every entry must be in `metadata_fields`; 2026-11-09) packs
those keys as `null` when a record has no value, so the generator sees that a field such as a
resolution is empty instead of not seeing the field. These
character counts do not bound the entire provider prompt or tokenizer output. Aggregate passages
include at most eight existing contributor IDs with matched-record total, generation and explicit
upstream/local truncation flags; this is aggregate-level lineage, not per-group membership.

`pipeline.review` (`enabled`, `max_hops`) runs the reviewer role between selection and generation.
Stop reasons are `sufficient`, `no_actionable_gap`, `repeated_query`, `hop_limit`,
`budget_exhausted` and `replan_abstained`; a follow-up query is re-planned through the configured
decision agent with the same filters, ACL and pinned generation, and a re-plan that is not
`decided` (including `raw_all_arms`) stops the loop. New hits merge with one vote per record per arm.

`pipeline.generation.contract.on_violation` (`strip` default since 2.0.0a3, `abstain_with_reason`, `annotate` (opt-in, 2026-11-09), `fail`) governs
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

2026-11-08 parity fixes to the text check and the claim kinds:

- **Sample lists next to an exact count (`examples` claims).** The generator's claim kinds include
  `examples` (offered whenever aggregate evidence is supplied): one claim that cites between 1 and
  10 record (non-aggregate) evidence ids and states no completeness. Next to a `count` claim backed
  by an exact aggregate, and when the answer text frames the block as examples ("for example",
  "examples", "sample", "such as", "including", "for instance"), it backs the enumerated block of a
  list question ("exactly 35 records match; examples: …"). It never backs a count, an absence or a
  completeness phrase: "the complete list", "these are all the records", "no other records" still
  need an `exhaustive_list` claim on an exact scope. An `examples` claim without record citations,
  with more than 10 ids or claiming completeness is `examples_claim_invalid` and is stripped.
- **Disclaimers are not completeness claims.** A completeness phrase that its sentence denies
  ("this is not a complete list", "no complete list … is available", "the full list is not
  available", "does not claim they are the full set") or only advises ("to get the full list, run
  …", "Re-run … with a full list …", sentences marked as advice or suggestion) is not an
  exhaustive-list statement. "Not only is this the complete list" still is.
- Not statements either: a unit used as a field label ("Zero match: false"), a double negative
  ("no zero match"), records described by a missing value ("6 records have no priority recorded" is
  a count, not an absence), references to the supplied evidence ("none of the ticket passages
  mention …"), and auxiliaries after "how many" ("How many are classified as …": "are" is no unit).

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
- `annotate` (opt-in, 2026-11-09; the earlier service's behaviour): the generator's evidence-cited
  answer is kept and one deterministic limitation note per violated claim or statement is appended
  under `Limitations:` ("I can't verify this count is exact for all records: \"...\"", "This list
  may not be complete: ...", "I can't confirm that no matching records exist: ..."), returned with
  `contract: annotated`, `limitations` (the notes) and `action: annotate` on the violations. Hard
  guarantees stay: unknown evidence (also an inline `[id]` naming nothing supplied), missing
  citations and an empty answer abstain; a stated number that differs from the exact aggregate is
  removed with its sentence and the note states the exact result's rendering (`action: strip`); an
  aggregate claim that cites nothing, an `examples` claim citing no record and an unbacked statement
  whose paragraph cites no supplied evidence are removed (noted). Trade-off: `annotate` answers more
  questions the way the earlier service did (an unverified count is shown with a caveat), while
  `strip` never shows a count it cannot back with an exact scope. Use `strip` where an unverified
  number must not reach a reader; `annotate` where a cited, caveated answer is preferred to a refusal.
- `fail` raises on any violation.

Under `strip`, `abstain_with_reason` and `fail`, unknown evidence ids, missing citations, an empty answer and a stated number that
differs from the cited exact aggregate (`answer_number_mismatch`) abstain (under `annotate` the number is
removed and corrected as above). Since 2026-11-09 (corpus-B rerun B4): inline `[id]` markers of
supplied evidence are citations (an id-shaped marker with a supplied id's prefix that names nothing
supplied is `unknown_evidence`); a count statement with a number but no typed `count` claim is backed
when its number is a value of an exact aggregate the answer cites; and these are not record counts:
the size of a cited result ("1 row", "3 rows"), the ids a cited aggregate retains ("8 record IDs"),
a threshold or requested N the question states ("with at least 20 tickets", "the next two"), a number
inside a phrase the question quotes, a count of the cited aggregates ("two aggregate results") and a
row-count parameter of advice. A count statement whose number no cited exact aggregate holds is
still unbacked. With native structured output the generator
schema is narrowed per request: cited ids are an enum of the supplied evidence ids, and non-narrative
claim kinds are offered only when aggregate evidence was supplied.

`unsupported_constraints` is populated by the engine from the decision, the request-filter validation
and the scope results — the generator cannot set it. `AnswerResult` keeps the v1 fields and adds
`unsupported_constraints`, `coverage` (from the pinned generation's manifest under
`quarantine`/`partial`), `violations` (`rule`, `detail`, `action`), `contract` (`passed` |
`stripped` | `abstained` | `annotated`) and `limitations` (the `annotate` notes); `SearchResult` adds `unsupported_constraints`. Both traces carry `decision` (agent,
outcome, policy, failure category, `decision_fallback`), `selection` (effective policy), `reasons`
per record, `model_calls` and, for answers, `review` rounds, `evidence` packing and `contract`.

The effective packing priority is `selection.packing.field_priority`, then
`pipeline.generation.evidence_fields` (what v1 `generation.evidence_fields` and the
`support_example` overlay set), then `ingestion.chunking.field_order`, then the document's own
field order. The character budget is `models.roles.generator.evidence_budget.chars` when set, else
`selection.packing.budget_chars`.

### Typed projection and the `typed_query` arm (`projection`, `pipeline.arms.typed_query`; preview)

Off by default; with `projection` absent or `enabled: false` nothing changes (index recipe,
write path, requests). The `llm` planner plans typed steps only with
`pipeline.decision.typed_query.enabled` (below); otherwise this block only builds and serves the
projection to direct arm calls.

```yaml
metadata_schema:
  fields:
    - {name: tenant, type: string, listable: true, description: "Owning tenant key."}
    - {name: status, type: enum, enum_values: [open, closed], description: "Workflow state at capture."}
    - {name: parent_id, type: id, description: "Document id of the parent ticket of a comment."}
projection:
  enabled: false
  backend: local                     # a declared sqlite backend (serves "relational")
  tables:
    ticket: {kind: ticket, columns: [tenant, status]}
    comment: {kind: comment, columns: [tenant, parent_id], descriptions: {parent_id: "Parent ticket."}}
    # free-text columns (2026-11-09): copied from the document title or a text field
    # ticket: {kind: ticket, columns: [tenant, status], text_columns: {
    #   subject: {source: title, description: "Ticket subject line; match mentions with LIKE."},
    #   description: {source: fields.description, description: "Original customer description; LIKE only."}}}
  relations:
    comment_parent: {from: comment.parent_id, to: ticket.id, cardinality: many_to_one, description: "A comment belongs to one ticket."}
pipeline:
  arms:
    typed_query: {row_limit: 200, max_groups: 500, timeout_ms: 2000, max_text_ids: 10000, text_backend: null}
```

Rules: each table holds the documents of one `kind` (key = document id); columns name declared
metadata fields; every column needs a reviewed `description` (`FieldSpec.description` or the
table's `descriptions` override; they are planner data, never instructions); under
`access.policy: tenant` every table carries the access column; relations are declared
`many_to_one` from a child column to a parent's `id` (no self joins, no other cardinality).
`pipeline.arms.typed_query` requires `projection.enabled`. Not available with `logs:` and not
expressible in v1. `FieldSpec.description` is optional elsewhere and has no runtime effect.

`text_columns` (optional, per table, at most 16; 2026-11-09) adds free-text columns that no
metadata field declares: `source` is `title` (the document title) or `fields.<name>` (one of the
document's text fields), copied verbatim (an empty or absent text is `NULL`); `description` is
required. A text column name must be an identifier that is neither a table column nor a declared
metadata field. Text columns are filterable `TEXT` and nothing else: never listed, grouped, sorted
or enumerated (no values reach a prompt), and the SQL surface matches them only with
`LIKE`/`instr` (an `=`/`IN` comparison is refused, `free_text_equality`, repairable). They follow
the tenant split like every column and are part of the projection's spec fingerprint (adding one
needs a rebuild; a projection without text columns keeps its fingerprint). Keep them out of
`packing.metadata_fields`: they are for counting mentions, the passages already carry the text.

Mentions in several text columns (2026-11-09): an `OR` group made only of `LIKE`/`instr` matches on
text columns that is a conjunct of the outermost `WHERE` (`(subject LIKE '%x%' OR description LIKE
'%x%')`) is reported `filter: any_of` with a group number. It applies the question's quoted phrase
(or, for a mention question, a field value) it contains when every needle of the group is a word of
the question (an unrequested term would widen the count) and, for several distinct needles, the
question joins them with "or" ("mention 'a', 'b' or 'c'"); a question with "and"/"both" never is.
A mixed `OR` (a text match or a status comparison), a negated group or one inside a subquery stays
`other` (unapplied). A distinctive word of exactly one observed value ("Ledgerly" for
`market_place__ledgerly`: a part of at least four letters of one value of one field) is a value
qualifier too, so a SQL count that omits that filter stays `bounded`. When an exact SQL result
matched text columns and an exact structured text-scope count (a literal match over every indexed
chunk) states other numbers, the engine keeps only one exact: for a mention question
("mention", "contain", "about" ...) the text-column result; the other is `bounded` with
`conflicting_count` (without a mention cue both are `bounded`; trace event `conflicting_counts`).

When enabled, every ingest (full, upsert, delete, reparse) builds the projection of the new
generation into the projection backend's `snapshots/<generation>/projection/` folder: `all.sqlite`,
one `t_<sha256>.sqlite` per tenant value under `access.policy: tenant`, and `manifest.json` (rows,
rejected values, dangling parent keys, tenant files, hashes). The projection is part of the index
recipe (changing `projection` needs a rebuild) and is removed with its generation
(`delete_generation`, retention `gc`). A value that fails its field type is stored as `NULL` and
counted, never coerced. The build holds the projection rows in memory.

`pipeline.arms.typed_query` limits: `row_limit` (rows of a lookup/list; more is `result_truncated`
and `bounded`), `max_groups` (groups of an unranked group count, and the largest `top_n`),
`timeout_ms` (per statement, read-only connection), `max_text_ids` (documents a `text` condition may
resolve to; more is refused) and `text_backend` (a backend serving `lexical`; unset: text conditions
are reported `not_supported_by_backend`). The engine binds the arm to `projection.backend` and
`storage.records` (value grounding).

#### Typed planning on the `llm` planner (`pipeline.decision.typed_query`; preview, typed-query plan M6)

```yaml
pipeline:
  decision:
    kind: llm                       # or a cascade with an llm stage
    typed_query: {enabled: false, repair_attempts: 0}   # repair_attempts: 0 | 1
    on_step_failure: abstain        # abstain | partial | retrieval_only (applies to typed steps too)
  arms:
    typed_query: {row_limit: 200}
```

`enabled: true` needs the llm planner and `pipeline.arms.typed_query` on the same pipeline
(validation error otherwise); `repair_attempts: 1` needs `enabled`. Per request the planner is
offered `arm: typed_query` only when the pinned generation has a sealed projection of the
configured spec (trace `typed_query_offered`; otherwise nothing changes for that request). Without
`enabled` the planner payload and wire schema are byte-identical to earlier releases.

What the planner sees (`typed_query` in its payload, untrusted data): the tables with their kind
and description, **described columns only** (type, reviewed description, allowed operations,
groupable/sortable/additive, time basis) and the declared relations with their descriptions; up to
20 sample values per column only for filterable `listable` fields under `max_catalog_cardinality`,
read through the principal's ACL (never the key, a non-listable or a high-cardinality field); no
row counts. The step's wire shape is `typed: {analysis, answerability, gaps, query}`: `analysis`
(at most 600 characters, traced masked, never compiled) comes first; `answerability` is
`answerable`, `partial` (gaps listed, the result is `bounded`) or `not_answerable` (gaps listed, no
query). Every table, column, relation, operation and measure name in `query` is an enum of the
catalog; a reply naming anything else is a `schema_violation`, also from providers that do not
enforce the schema. Values are grounded by the `ValueResolver` (ACL-scoped; unknown values are
refused, never fuzzy-matched) when the decision compiles the draft for validation; the arm
compiles it again with the principal's ACL before executing.

Outcomes: `not_answerable` is a recorded `DroppedStep(reason: planner_not_answerable)` plus
`planner_not_answerable` and `aggregate_intent_without_scope` constraints (no count is ever
backed); with nothing else planned the decision falls back to the original query on lexical/dense
(`retrieval_after_dropped_steps`), or abstains with `no_plan` when neither is enabled. A draft that
does not compile fails the decision (`abstain`, default) or is dropped as `step_not_executable`
under `partial`/`retrieval_only`; a draft that contradicts a request filter always fails closed
(`invalid_step`). With `repair_attempts: 1` the typed compile errors (path, reason, allowed names)
are sent back once; the re-plan is used only if its typed steps still bind every question
qualifier the failed draft bound (or it marks the step `not_answerable`); otherwise the original
plan and the step-failure policy apply (trace `typed_query_repair`). A typed aggregate on a question
without an aggregate operator word, or one missing a question qualifier, is `bounded`.

Evidence: typed results are packed through the aggregate path (`source: structured`, `arm:
typed_query`) inside the aggregate reserve of the packing budget (a result that does not fit is
omitted and listed in `omitted_ids`): at most 50 rows, each value cited as `(document_id, field)`
with the generation in `contributor_lineage`, or at most 50 groups; a result with more is
`bounded` (`result_truncated`). The full result stays on `SearchResult.aggregates`. Typed results
have no deterministic rendering yet; the generator states them and the `AnswerContract` checks
count claims against the exact typed value.

#### `mode: sql_surface` (model-written SQL behind the guard; opt-in)

```yaml
models:
  roles:
    planner: {provider: openai_compatible, model: local-model, base_url: "http://127.0.0.1:8000/v1"}
pipeline:
  arms:
    typed_query:
      mode: sql_surface                # default: ir (the compiled typed IR)
      row_limit: 200
      timeout_ms: 2000
      sql_surface:
        role: planner                  # models.roles.<role> writes the SQL (planner | reviewer | generator)
        repair_attempts: 1             # 0 | 1: one re-prompt with the typed parse/validation/database error
        max_sql_chars: 4000
        sample_values: 5               # observed values shown per listable column (0: none)
        list_values_up_to: 64          # a column with at most this many values shows all of them (0: samples only)
        group_lineage_max: 10          # groups with at most this many records list their record ids (0: off)
        max_joins: 3
        max_subqueries: 4
```

Ported from NVIDIA Auto-Ontology's catalog + text-to-SQL flow (Apache-2.0; see `NOTICE`). Needs
the extra `pip install 'su-multi-arm[sql-surface]'` (`sqlglot`); without it the arm's validation
fails engine construction with `extra_missing`. The selected chat role must be configured
(`projection_incoherent` otherwise). A model writes one SQLite `SELECT` over the per-principal
catalog (tables, columns, types, reviewed descriptions, ACL-scoped sample values, declared joins)
or answers `not_answerable`. The arm refuses comments, several statements, anything but a
`SELECT` (CTEs allowed; no `UNION`, `WITH RECURSIVE`, `PRAGMA`, `ATTACH`, writes), names outside
the catalog, qualified table names (`main.t`), functions outside the list (`COUNT`, `SUM`, `AVG`,
`MIN`, `MAX`, `TOTAL`, `GROUP_CONCAT`, `LOWER`, `UPPER`, `LENGTH`, `SUBSTR`, `TRIM`, `INSTR`,
`ABS`, `ROUND`, `COALESCE`, `IFNULL`, `NULLIF`, `IIF`, `CAST`, `DATE`, `DATETIME`, `JULIANDAY`,
`STRFTIME`), window functions and cross/natural/right joins. Every filter value must be a stored
value for the principal (enum values, observed values, or an exact lookup); unknown values are
reported `value_not_observed`, never fuzzy-corrected. Since 2026-11-09 (B5) a value equal to one
stored value under the documented normalization (casefold; whitespace, punctuation and `_` as one
separator; a plural fold; the distinctive word of one multi-part value such as `Ledgerly` for
`market_place__ledgerly`; `decision/value_match.py`) executes as that stored value and is recorded
on the checked value (`as_written`, `rule`); two or more matches are refused (`ambiguous_value`).
An `=` on a bare column that no rule grounds moves to the one other column that grounds it only when
the user's own question contains the value and does not name the original column (recorded as
`column_as_written`); the planner's structured scopes use the same rules (trace
`value_canonicalized`). Request filters are never rewritten. `LIKE` is accepted only on free-text columns. The
principal's ACL and the request filters are bound into every table, all values are parameters,
results are capped at `row_limit`, and execution is read-only on the principal's tenant file. In
the default `ir` mode the arm refuses SQL input, and in `sql_surface` mode it refuses typed drafts.
Results are flagged `sql_derived` with the executed parameterised SQL in `sql_surface` lineage.

Corpus-B fixes (2026-11-09):
- **Values.** A listable column with at most `list_values_up_to` (default 64) ACL-scoped observed
  values, or an enum with at most that many declared values, shows every value, marked
  `(all values)`; larger columns show `sample_values` samples. `max_catalog_cardinality` and the
  ACL still decide what is enumerable at all; the access column, ids and free text show none. The
  prompt adds deterministic `VALUE HINTS`: stored catalog values whose words appear in the question
  as a phrase ("double charge" -> `general__double_charge`). Hints only name candidates; every
  compared value is still grounded by the guard.
- **Rankings.** An aggregate `ORDER BY` key that the `SELECT` list does not carry is projected
  (`sort_value`; the evidence lists it under `sort_projected`). A statement with a `LIMIT` below
  `row_limit` and an aggregate `ORDER BY` reads up to 50 rows past the `LIMIT` and keeps the rows
  whose measure ties the last row within it (`tie: true` on those rows; a secondary key such as a
  name never breaks the tie). With an `OFFSET`, the row before it is read too, to report a tie
  with it. `sql_ties: {columns, limit, after_limit, before_offset, complete}` reports the result;
  more ties than the window is `result_truncated` (`bounded`).
- **Share numerators.** A comparison that is the only condition of a conditional measure of the
  outermost statement (`COUNT(CASE WHEN c THEN 1 END)`, `SUM/TOTAL/AVG(CASE WHEN c THEN x ELSE 0
  END)`, `IIF(c, x, 0)`) is recorded as `filter: numerator` and applies the question's qualifier
  when the question asks a share/ratio. `COUNT(CASE WHEN c THEN 1 ELSE 0 END)` (counts every row),
  `OR` and multi-branch `CASE` never count.
- **Rendering.** Every executed result carries `rendering` (`SQL result: column=value, ... (exact;
  N rows)`, ties and truncation stated). When the contract strips an answer's unbacked number it
  renders the cited SQL result, never an aggregate the answer did not cite.
- **SQL writer budget.** `validate` warns (`sql_writer_max_tokens_low`) when the `sql_surface.role`
  has `max_tokens` below 2048; 3000 is recommended (a 1500 ceiling stopped a combined query).

Corpus-B B7 fixes (2026-11-09):
- **Ratio parts.** A rate/ratio/share/percentage column of the outermost statement (its outermost
  division divides two aggregates, or two column expressions in an unaggregated statement; `CAST`,
  `NULLIF` and constant factors such as `100.0 *` are peeled) carries its numerator and denominator:
  a part the statement already selects (the same expression) is reused, any other is added as a
  visible column `<ratio>_numerator` / `<ratio>_denominator`. `sql_ratios` lists them (`parts_added`)
  and the rendering states each ratio as `N of D (rate)` (for example `open_share=2 of 3 (0.6667)`).
  A `DISTINCT` statement is left as written. The writer prompt asks for the parts too.
- **Small-group lineage.** A `GROUP BY` statement over one catalog table (no join, subquery, CTE or
  `DISTINCT`) whose group keys are all selected gets one more bounded, read-only read on the same
  connection and the same ACL/filter-bound source: the record ids of every returned group with at
  most `group_lineage_max` (default 10; 0 disables) contributing records, taken through the
  statement itself (its `WHERE`, `HAVING`, `ORDER BY`, `LIMIT` and `OFFSET`). Such rows carry
  `contributor_ids`, the rendering appends `(records: a, b)`, the evidence reports
  `lineage: groups` and `group_lineage: {status, max_group_size, groups_with_ids}`, and the packed
  evidence's `contributor_lineage.scope` is `small_group_contributors`. A failed or inconsistent read
  attaches nothing (`status: error`); the result stands. A filtered scalar aggregate keeps up to
  `max(8, group_lineage_max)` contributor ids.
- **Literal value words.** A question phrase that is a whole stored value as written ("open" for
  `status = open`) is a `VALUE HINTS` entry marked `(literal match)`, and the writer prompt asks to
  filter on exactly that value rather than a set of related values unless the question names them.
- **Exact first.** When the evidence holds both exact and bounded/approximate aggregates, the exact
  ones are packed first and every aggregate carries `precedence` (`primary`, or `secondary` with
  the `exact_ids` it yields to); the built-in generator prompt asks to answer from the exact result
  when both answer the same question. Nothing is dropped or relabelled by this ordering.

**Planner routing.** With `pipeline.decision.typed_query.enabled: true` (the `llm` planner) and
`mode: sql_surface`, the planner is offered `arm: typed_query` with the same described catalog and
ACL-scoped samples as in mode `ir`, but its `typed` object is `{analysis, answerability, gaps,
question}`: a self-contained sub-question for the SQL route, no IR and no identifiers. Nothing is
compiled at decision time. Before the plan's steps execute, the engine sends each sub-question to
`sql_surface.role` (one call plus at most `sql_surface.repair_attempts` repair round), with the
request's ACL and filters, and the arm validates and executes the SQL as above. Every SQL call
is metered like other model calls (stage `sql_surface` in `trace.model_calls`, the run's
`budgets`); config validation counts one SQL call per answer (`max_llm_calls` must allow planner
+ SQL writer + generator). The surrounding pipeline is the one of mode `ir`: the planner's
retrieval steps, packing (at most 50 result rows with `(document_id, table.field)` citations, plus
the parameterised SQL and its fingerprint), the generator and the `AnswerContract`. Question
qualifiers the executed SQL does not express (no grounded comparison on the field) and an
aggregate result for a question without an aggregate operator make the result `bounded`
(`qualifier_not_applied`). A route that does not execute is recorded as a `DroppedStep`:
`planner_not_answerable` (the SQL writer answered not answerable), `sql_refused` (guard or
database refusal), `sql_unsupported` (a filter value not stored for the principal) or `sql_error`
(provider, schema, budget or availability failure; constraint `sql_route_failed`).
`pipeline.decision.on_step_failure` then decides: `abstain` (default) fails the decision (or
`raw_all_arms` when `on_failure` says so), `partial` drops the step, `retrieval_only` drops every
structured and typed step; when nothing remains, the original query runs on the enabled
lexical/dense arms (`planner_fallback: retrieval_after_dropped_steps`). The planner's own
`not_answerable` behaves as in mode `ir`. `pipeline.decision.typed_query.repair_attempts` (the IR
re-plan) is refused in this mode; use `sql_surface.repair_attempts`.

Comparing the two modes on the same deployment (only the arm's mode differs):

```yaml
pipeline:
  decision:
    kind: llm
    on_step_failure: partial           # the same for both runs
    typed_query: {enabled: true}       # repair_attempts: 0 (an IR-only re-plan)
  arms:
    typed_query:
      mode: ir                         # run 1; run 2: mode: sql_surface
      sql_surface: {role: planner, repair_attempts: 1}
```

**Separate typed router (`routing: router`; opt-in).** `pipeline.decision.typed_query.routing`
is `planner` (default; the behaviour above, unchanged) or `router`. With `router` the main planner
request does not include the typed catalog or the `typed_query` arm (it is byte-identical to a
deployment without typed queries). Instead, before the planner, one small metered call (stage
`typed_router` in `trace.model_calls`, role `pipeline.decision.typed_query.router_role`: `planner`
(default) | `reviewer` | `generator` | `query_writer`, which must be configured) receives the question, the request
filters and a compact catalog summary (table names and described column names; no samples) and
returns `{route: typed | retrieval, sub_question, reason}`. On `typed` the sub-question runs on the
typed route before the planner (mode `ir`: one typed-plan call, stage `typed_ir`; mode
`sql_surface`: the SQL route above), with the request's ACL at execution; an executed result is
packed ahead of the retrieval evidence and the planner still runs for retrieval context, so the
answer gets both. Qualifiers of the original question the result does not express make it
`bounded`. A typed route that does not execute is a recorded `DroppedStep` (its constraints and
`aggregate_intent_without_scope` per aggregate intent are added, as `on_step_failure: partial`
would) and the planner runs as usual; a failed router call is a `DroppedStep` `typed_router_error`.
On `retrieval` only the planner runs. `max_llm_calls` must allow router + typed writer
(+ `sql_surface.repair_attempts`) + planner + generator (5 with the defaults in mode
`sql_surface`). `routing: router` needs `enabled: true` and `repair_attempts: 0`.

2026-11-08 parity fixes:

- **`models.roles.query_writer`** is a chat role for the router and the SQL writer
  (`router_role: query_writer`, `sql_surface.role: query_writer`). Their reply schemas
  (`TypedRoute`, `SqlDraft`) are small and flat, so a registry-default `native` mode stays native.
  The `planner` role is moved to prompt mode at config time when its `structured_output` is a
  registry default, because `LlmPlan` is over the provider's `output_config.format` limits; a router
  or SQL writer sharing that role then also runs in prompt mode. `validate` / `doctor` report the
  resolution per role. v1 cannot express the role (`config downgrade` refuses it).
- **`sql_surface.repair_attempts: 1`** also covers a SQL-writer reply that violates the `SqlDraft`
  schema: one re-prompt with the violated field paths (never the reply). The round is shared with
  the SQL repairs (at most one per route). The SQL writer's prompt example is JSON.
- **`typed_query.parallel`** (default `true`): the router chain (router call, then the SQL writer or
  the typed IR call) runs in a worker thread while the planner call runs; the planner request does
  not depend on the route. Both meter through the run's budget; the trace's `model_calls` order and
  the merge are those of the sequential run. It is sequential with `parallel: false` or when the
  remaining call budget cannot cover the router chain and the planner together (so which call a
  `budget_exhausted` hits stays deterministic). Near a USD or token limit, the order in which the
  two concurrent reservations arrive decides which call is refused; use `parallel: false` when that
  must be reproducible.
- **Planner abstention with an executed route.** When the planner abstains (for example its reply
  violates the plan schema) but the routed typed result executed and the question asks an
  aggregate (an intent or any aggregate operator word; no caller-typed aggregate), the typed step is
  the plan (`policy_applied: typed_route_only`; `failure` and `note` record the abstention) and the
  generator answers from it. Without an aggregate question the result is still discarded
  (`typed_route_discarded`).
- **Covered aggregate flags.** A failed typed route adds `aggregate_intent_without_scope` per
  aggregate intent; once an executed step produced an exact aggregate (the contract's rule-3
  condition), those per-intent flags are removed from the decision and the answer
  (`aggregate_flags_covered` trace event). The route's own constraints stay.
- **Contributor lineage of SQL aggregates.** A scalar aggregate over one table (no join, grouping,
  subquery, DISTINCT, HAVING or LIMIT) carries `contributors` (the rows it ranges over, under the
  same ACL and filters) and up to 8 of their ids in `document_ids` (`lineage: computed`). Other
  shapes carry none (`lineage: not_derivable`); a failed lineage read is `lineage: error` and the
  result stands. Lineage never changes the result's exactness.

**Arm cards (`pipeline.decision.arm_cards: true`; opt-in, default `false`).** Generated,
domain-neutral descriptions of every enabled arm (purpose, when to use and not, the step fields and
their JSON shape from the reply schema, configured limits, returned evidence) are added to the
planner payload (`arm_cards`), the router payload (`routes`, plus column types, descriptions and
declared relations in the catalog summary) and the SQL writer prompt (a limits and refusal block).
Disabled arms never appear. Off, every request is unchanged. Measured on the synthetic test
deployment (3 tables, lexical/dense/structured + typed, `tests/contract/test_arm_cards.py`):
planner 4,382 -> 9,595 characters, router 1,281 -> 4,996, SQL writer 7,939 -> 8,531 (system prompt
plus serialised payload; roughly a quarter as many tokens).

Evaluation keys for the router with the SQL surface and protected metadata:

```yaml
pipeline:
  decision:
    kind: llm
    on_step_failure: partial
    arm_cards: false                   # optional; true adds the generated arm cards
    typed_query: {enabled: true, routing: router, router_role: query_writer}   # parallel: true (default)
  arms:
    typed_query:
      mode: sql_surface
      sql_surface: {role: query_writer, repair_attempts: 1}   # the repair also covers a schema violation
  selection:
    packing:
      metadata_fields: [captured_at, environment, account]   # your allowlist
      metadata_protected: [captured_at, environment]         # never dropped by the share cap
      metadata_max_share: 0.25
models:
  roles:
    query_writer: {provider: anthropic, model: <model>, api_key_env: <ENV>}   # structured_output unset: native
budgets:
  max_llm_calls: 5                     # router + SQL writer + repair + planner + generator
```

An evaluation harness can also call the route directly, without the planner:

```python
from su_multi_arm.sql_surface import SqlSurfacePlanner, engine_session, role_model
session = engine_session(engine, principal)                     # pinned generation, principal's ACL
result = SqlSurfacePlanner(session).plan(question, session.catalog, role_model(engine))
result.status       # executed | not_answerable | refused | unsupported | error
result.evidence     # the arm's evidence (rows with citations, or sql_derived aggregates)
```

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

### Worst-case spend check, provider retries and question grounding (M0; `budgets.plan_check`, `models.roles.<role>.on_incomplete` / `retry_transient`, `question`)

```yaml
budgets:
  max_usd: 0.25            # per request (needs price_table)
  max_llm_calls: 5
  plan_check: warn         # off | warn (default) | refuse
models:
  roles:
    generator:
      provider: anthropic
      model: <small model>
      max_tokens: 6000
      on_incomplete: retry_shorter          # fail (default) | retry_shorter | retry_larger
      # incomplete_retry_max_tokens: 8192   # only with retry_larger; must exceed max_tokens
      retry_transient: {attempts: 1, backoff_seconds: 2}   # unset (default): no retry
question:
  timezone: UTC            # the question clock (IANA); default UTC
  vocabulary:
    terms:
      - {term: crawl, match: prefix, field: service, values: [crawler, crawler_scheduler]}
      - {term: slow, field: symptom, note: "the logs record failure signals, not latency"}
    # file: vocabulary.yaml   # a YAML list of terms, relative to this config
    # sha256: <64 hex>        # required with file; a mismatch refuses to start
```

**`budgets.plan_check`** (`runtime/plan_cost.py`) computes, once at engine startup, the worst-case
answer spend `W` of every pipeline and compares the largest with `budgets.max_usd`. Call slots:
the llm planner (`1 + schema_repair_attempts + decision.typed_query.repair_attempts`), the typed
router and typed IR / SQL writer (`1 + sql_surface.repair_attempts`), the reviewer
(`review.max_hops`), the generator (`1`, plus one retry when `on_incomplete` is not `fail`, plus
`pipeline.generation.schema_repair_attempts`) and the `community` role when configured. For each slot `I = (prompt + evidence + catalog characters)
/ 3` tokens and `O = max_tokens` (the retry ceiling for `retry_larger`); `W` is the sum of the
`max_llm_calls` most expensive slots. Transient-retry slots are reported separately
(`worst_case_with_transient_usd`). The prompt part is measured from the built-in or configured
system prompt plus the reply schema; evidence is the generator's `evidence_budget` (else
`selection.packing.budget_chars`), raised to `request.evidence_chars_max` when set; the planner's
catalog is the metadata schema plus `logs.intents.planner_context_max_chars` (under the two-halves
planner, which never sends that context, the source cards and an operations-half allowance instead;
see "Staging round 2 fixes on the planner side"). Arm cards, typed and
SQL catalogs, the question and record context cannot be measured from configuration and are listed
in `unmeasured`, so `W` is a lower bound for deployments that use them: compare it with the
`input_tokens` the `model_call` trace events report. `warn` logs a warning and adds a `plan_check`
note to `GET /ready` (readiness is unchanged); `refuse` raises `ConfigError` (`budgets.plan_check`)
when `W > max_usd` or a role has no price. Existing configurations start unchanged.

**`on_incomplete` / `retry_transient`** are the only provider retries the package makes, and both
are off by default. A reply cut at the output ceiling (`stop_reason: max_tokens`, `finish_reason:
length`) is retried once: `retry_shorter` sends the same ceiling with an instruction to answer more
briefly, `retry_larger` sends `incomplete_retry_max_tokens`; a second cut is `ProviderIncomplete`.
`retry_transient` retries HTTP 5xx/overloaded, 429, timeouts, connection errors and a stream that
ended before its final event (without a `max_tokens` stop) up to `attempts` times after
`backoff_seconds`, only while the request deadline leaves the backoff plus one second. Every attempt
is a separate call reserved on the request budget; a retry the budget refuses leaves the original
error. Auth errors, bad requests, refusals and schema violations are never retried, and the model is
never changed. The `model_call` trace event and the failure log line carry `attempt`,
`retry_reason`, and `incomplete_cause` (`max_tokens` | `stop` | `stream_end`), so a 502 says whether
the reply was truncated or the stream was cut. Streamed calls: after deltas were emitted, a retry's
deltas are not forwarded (streamed tokens are provisional; `done` carries the answer).

**`question`** applies to typed log intents (`logs.intents.enabled`), for rules, cascade and llm
plans alike (`decision/log_question.py`):

- an outage/presence question ("is there any outage", "any errors", "did ... fail") without a
  period reads the last `logs.collection.serving.default_view_days` days instead of today, and
  says so;
- a clock hour without a date ("at 2", "around 14:00", "2 pm") is read in `question.timezone` on
  the named day, else at its most recent occurrence, and converted to `logs.timezone`. An hour from
  1 to 12 without am/pm is ambiguous: both readings are kept and stated. `log_around` runs once per
  reading; a day-level intent covers the readings' days, states that day rollups do not isolate the
  hour, and reports its aggregate `bounded` (`qualifier_not_applied`);
- vocabulary terms ground surface words to indexed service values (only values the generation
  has indexed; a term naming only unindexed services refuses the scope rather than answering for
  every service). A service named in the question wins. `symptom` terms are stated, never filtered
  on, and are not required as literal words of incident records.

`question.timezone` is separate from `serving.ui.display_timezone`, which stays display-only.
Domain terms belong in deployment configuration or the pinned file, not in the package.

An outage investigation whose window overlaps more incidents than one selection reads (500) is
answered with the most recent ones, a "Bounded selection" note and a per-client incident count over
the whole window (its exactness as the backend reports it), instead of a 503. A backend that cannot
express the selection still refuses.

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

### Retrieval-parity features (2026-11-09; all opt-in)

Five additions for deployments that replace an earlier support QA service. Every key defaults to
the previous behaviour; none calls a model at ingest.

**Cross-encoder rerank (`models.roles.reranker`, `pipeline.selection.rerank`, `pipeline.selection.reranking`).**

```yaml
models:
  roles:
    reranker: {model: BAAI/bge-reranker-v2-m3, device: auto, dtype: auto, batch_size: 32, max_length: 512}
pipeline:
  selection:
    rerank: true
    reranking: {top_k_in: 60, top_k_out: null, timeout_ms: 3000, on_error: fusion_order, passage_chars: 1900}
```

The stage runs after ordering, diversity, `source_order` and pins (`selection/rerank.py`). It scores
`(query, title + retrieved passage[:passage_chars])` for the first `top_k_in` unpinned records;
pinned records stay first and the rest keep fusion order after the reranked head. `top_k_out` is
an explicit cut (reported as `dropped`). `device: auto` uses CUDA when available, else the CPU; an
explicit `cuda` that is unavailable is a load error, never a silent CPU fallback. `dtype: auto` is
float16 on CUDA. `timeout_ms` bounds waiting for the scorer plus the scoring call; the first model
load is measured separately (`load_seconds`). `on_error: fail` (default) raises `RerankError`;
`fusion_order` keeps the fusion order and records `status: failed_open` and the reason (`timeout`,
`scoring_failed`, `bad_response`) in `trace.selection.rerank_detail` and each record's reasons. One
scoring call runs at a time; a call that overran its budget keeps running in a daemon thread until
it returns and holds the scorer, so later calls time out within their own budget instead of
queueing work behind it (measured: without this, 45 abandoned CPU calls kept a process busy ~10 min).
`reranking` without `rerank: true` is a validation error.

**Segment arm (`pipeline.arms.segment`, `su-multi-arm segments build`).** Child records of a parent
record (a description, a comment, a resolution note) in a sealed sidecar index; they never enter the
catalog, counts or the projection.

```jsonl
{"id": "seg-1", "parent_id": "ticket:42", "kind": "resolution", "text": "Restarting the worker fixed it.", "field": "resolution", "seq": 3}
```

```bash
su-multi-arm --config deploy.yaml segments build --input segments.jsonl --out state/segments-v1 --anchor required
```

```yaml
pipeline:
  arms:
    segment: {path: state/segments-v1, candidates: 200, kinds: [], lexical: true, lexical_candidates: 400, rrf_k: 60, max_segments_per_parent: 3}
```

The build reads every parent from the active generation (a missing parent fails the build), locates
each segment verbatim in its declared parent `field` (`--anchor required`: a row without a field or
not found fails; `optional`: stored `unanchored` and counted), embeds the text with
`models.roles.embedder` and seals `segments.sqlite` (rows + FTS5), `vectors.npy` and `manifest.json`
(digests, dataset, generation, embedder recipe). The arm refuses a missing, tampered or
dataset/embedder-mismatched index. At query time it ranks every segment of the configured `kinds`
(exact cosine; `lexical: true` adds a BM25 list fused by reciprocal rank), walks the ranking and
admits parents through the catalog with the request scope (filters, exclusions, ACL predicates are
evaluated on the parent) until `candidates` parents are found, so restrictive scopes stay exact. One
hit per parent; its provenance lists up to `max_segments_per_parent` segments (`segment_id`,
`segment_kind`, `parent_id`, `field`/`start`/`end`, `anchor: verified|unanchored`, `score`, `via`).
An anchored segment whose text is no longer at its offsets in the serving generation is dropped and
counted (`stale_anchors` in the arm report under `trace.reports`). Rebuild the index after the
parents change. The planner sees the arm as `segment` (arm cards describe it).

**Imported communities with grounded claims (`communities.strategy: imported`, `community.claim_hits`).**

```yaml
communities:
  strategy: imported
  imported: {path: communities.jsonl, sha256: <64 hex>, on_ungrounded: drop_claim, extractive_sentences: 3, min_members: 1}
pipeline:
  arms:
    community: {backend: local, claim_hits: true, claim_min_terms: 2}
```

Each JSONL row is `{id, label, level, parent_id, members: [document ids], claims: [{text, evidence:
[{document_id, quote}]}], summary, stats}`. Members absent from the generation are counted and
dropped; a community left with fewer than `min_members` is dropped. A claim is stored only when every
quote occurs verbatim in a chunk of its cited document and that document is a member (the stored
reference carries the chunk id); otherwise it is dropped and counted (`on_ungrounded: fail` fails the
build). `summary` prose is never stored (counted as `summary_refused`). `extractive_sentences: n`
gives a community without any grounded claim up to `n` verbatim member sentences (those sharing the
most terms with its label) as self-citing claims (`origin: extractive`). The manifest records
`community_import` counts. With `claim_hits: true` the community arm also returns, for each eligible
claim sharing at least `claim_min_terms` content terms with the query, its cited member passages as
hits whose provenance carries `community_id`, `label`, `member_count`, `level`, the claim and the
quote (`kind: community_claim`). Claims citing a member outside the request scope are never shown
(existing rule); a partially visible community keeps the neutral label.

**One-to-one record attributes from an external SQLite file (`ingestion.attach`).**

```yaml
ingestion:
  attach:
    - {path: kg.sqlite, sha256: <64 hex>, table: ticket_insights, key_column: ticket_id, document_id: 'ticket:{key}',
       columns: {outcome: kg_outcome, request_type: kg_request_type}, on_conflict: fail}
metadata_schema:
  fields:
    - {name: kg_outcome, type: string, filterable: true, listable: true, groupable: true, description: "..."}
```

At ingest the file is checked against `sha256`, opened read-only and immutable, and only the
declared columns of the declared table or view are read (quoted identifiers; no SQL text from
configuration). A duplicate key, an undeclared target field (validation) or a document that already
carries a target key (`on_conflict: fail`; `keep_document` keeps the document's value and counts it)
fails the build. `NULL` values are not attached. The manifest's `attach` lists rows, matched,
unmatched and orphan rows per source. Attached fields are ordinary metadata: filters, ACL, the
structured arm and the typed projection (`projection.tables.*.columns` with descriptions) apply. An
incremental ingest attaches only the documents it receives; re-ingest with `--replace` after the
source changes. Many-to-many facts belong in a graph (below).

**Attached graph stores behind the graph arm (`graph.path_evidence`).** A `kuzu` backend in
`mode: attach` is read-only: the write path no longer targets it (it is not a graph projection
holder, nor part of the index recipe), so an engine can ingest its catalog and serve the graph arm
from the attached store in one configuration. Free-text traversal over an attached store returns
paths whose mention spans are not catalog chunks; `pipeline.arms.graph.path_evidence: true` adds
the documents the traversed edges are anchored in as hits, with the edge quotes, predicates and
provenance (the shape typed templates already return). Off by default.

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
      prefix: null                     # optional literal every name starts with (e.g. logs); stripped
      deployment_prefixes: [search-admin, os-data-{n}]   # optional, longest first; {n} = digits
      deployment_position: leading     # or before_service: <tenant>-<env>-<deployment>-<service>;
                                       # or after_env: env-anchored, the deployment is anything between
                                       # the env (vocabulary) and the service (vocabulary)
      unparsed: include                # or exclude: not a unit, counted as excluded_scope.unparsed_name
      prefix_fields: [tenant, env]     # one token each unless the field has a vocabulary
      vocabularies:                    # service and any prefix field; literal names, '_' allowed
        service: [ingest-worker, query-gateway, gateway, scheduler, slack_bot]
      service_suffix: none             # numeric: ml-service-1658254295 = service ml-service + deployment_id
      service_suffix_min_digits: 6     # shortest digit run read as a deployment id
    counts:
      errors: {levels: [ERROR, FATAL], tokens: [error, fatal]}
    multiline:
      continuation: [java_frame, python_frame, traceback_header, python_traceback, caused_by, ellipsis_more, closing_bracket]
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
    disallow: []                       # request families this deployment forbids: [pit]
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
- `disallow: [pit]` removes the PIT rows for this source: such a request is refused before any I/O.

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
   `deployment` attribute, or `none` when the name has no deployment prefix. With
   `deployment_position: leading` (default) it follows the literal prefix; with `before_service` it
   sits between the prefix fields and the service (`logs-acme-prod-ops-admin-kafka-…` reads tenant
   `acme`, env `prod`, deployment `ops-admin`, service `kafka`). Under `before_service` every
   service vocabulary entry that ends the name is tried, and a name that fits more than one way is
   `ambiguous`;
4. the `service` segment at the end, and the `prefix_fields` segments in order. A segment with a
   vocabulary takes the longest entry that fits (`query-gateway` before `gateway`). A segment
   without one is exactly one token without the separator, so `slack_bot` is one token under `-`. With
   `service_suffix: numeric`, a trailing run of at least `service_suffix_min_digits` digits after the
   service is read first as the `deployment_id` attribute (`none` without one), so
   `acme-prod-ml-service-1658254295-2026.03.01` is service `ml-service`, deployment id `1658254295`.

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

**Multiline.** A continuation document joins the open head of the same `(container, file)` within
`max_gap_ms`; a head-less one is a `fragment` (counted, sampled, excluded from coverage). Rules:
`java_frame`, `python_frame` (indented or column 0), `traceback_header`, `caused_by`,
`ellipsis_more`, `indented` (opt-in), `closing_bracket` (a line of only `}`, `]`, `)`, `});`, `},`)
and `python_traceback`. The last is contextual: the chained-exception lines always continue, and
inside an open traceback the source line after a frame, the `^~~~` marker lines and the final
`SomeError: message` line continue too; outside a traceback such a line stays a head, so a lone
`ValueError: …` log line is never hidden as a fragment. The approved `continuation` entries of the
pattern library are applied in addition wherever the library is loaded (log pulls and `patterns
coverage|mine`); they take no `applies_to` selectors. A library change does not re-join records
already stored in a partition (reparse re-matches the stored heads).

**Not in this release.** PIT-based pulls (PIT is probed only, on request). Sliced scroll.
Data-stream backing indices (skipped with a diagnostic).

**CLI.**

```bash
su-multi-arm --config c.yaml sources check                       # capabilities + self-check
su-multi-arm --config c.yaml sources discover --date 2026-01-05  # units and parsed attributes
su-multi-arm --config c.yaml sources count --date 2026-01-05     # per-unit counts and bases
su-multi-arm --config c.yaml sources probe --date 2026-01-05 --max-units 3 --max-docs 1000 --output probe.json
su-multi-arm --config c.yaml sources probe --date 2026-01-05 --probe-pit --request-log requests.jsonl
```

`probe` puts a bounded load on the cluster and writes an aggregate-only JSON report:

- capabilities and the self-check;
- per-unit counts and bases, field presence and index-name parse results;
- `random_score` support (`--no-random-score` skips it) and, only with `--probe-pit`, PIT support:
  the PIT check runs when the server's version supports PIT and `disallow` does not list `pit`
  (an unknown version, e.g. after a 403 on `GET /`, counts as unsupported); `pit_check` says why it
  was skipped;
- scroll throughput on one unit of at most `--max-docs` documents;
- request timings for each allow-list row.

`--request-log PATH` (every `sources` command) appends one content-free JSON line per request
(method, allow-list row, path template, status, milliseconds, bytes; never an index name, body or
host) and a summary row with the number sent. The privilege endpoint is asked once per command.

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
    raw: {enabled: false, path: ./raw-copies, compression: gzip}   # unredacted copies; see "Raw copies"
  parse: {workers: 1, batch_size: 2000, max_exemplars_per_signature: 3, max_unparsed: 2000}
  compose: {signature_hour: false, max_exemplars_per_signature: 3}
  retention:
    keep_days: 10                      # (alias partition_days) masked partitions: N complete days
    raw_days: 3                        # raw copies (only with partitions.raw.enabled); < keep_days
    derived: window                    # window (default) | forever | <days >= keep_days>
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

**Redaction is required.** Nothing is pulled without a redactor. The CLI uses the safe redactor
of `privacy.engine: safe` (see "Safe redaction engine"); under `engine: legacy` `run-daily`,
`backfill` and `settle` refuse with `RedactionRequired` before any request. The Python API
(`LogsPipeline(..., redactor=...)`) also accepts an injected redactor. The partition manifest
records the redaction stamp and the replacements per redactor.

**Window.** `keep_days: N` keeps the **N most recent complete days** before today (in
`logs.timezone`): `today - N` through `today - 1`. Today is not counted. With `keep_days: 10` the
run on day 12 deletes day 1 and serves days 2–11. `logs run-daily` composes exactly this window
(ending yesterday), refuses a day older than `today - N`, and runs retention as of today;
`retention report|verify|run --date D` takes D as "today".

**Retention scopes.** Each data class has its own clock:

| Class | Setting | What |
|---|---|---|
| raw copies | `raw_days` | unredacted pulled documents (`partitions.raw`, off by default) |
| partitions | `keep_days` / `partition_days` | masked records, matches, unparsed queue |
| derived | `derived` | the composed arm data: day and hour rollups, the signature catalog and its masked exemplar lines, graph nodes and edges, communities, every projection's documents |

`derived: window` (default) is the single-window behaviour: derived data leaves with its
partition. `derived: forever` keeps every composed day in the serving generation after its
partition expired; `derived: <days>` keeps that many. Compose keeps a masked derived snapshot of
each day (`<partitions>/derived/<source>/<day>.json.gz`: aggregates, hourly cells, exemplars) and
builds every derived day from it; window rollups then cover every derived day. A derived day whose
partition is gone cannot be reparsed under a new library: it is served as composed, listed under
`logs.derived` in the manifest with its library identity. A downsampling hook (hourly cells to
daily for old days) exists in `logs_compose.downsample` and keeps everything today.

**A1 amendment (retention tiers).** Rule (a) of the addendum (a generation that lists an expired
day is deleted) and the content-based `retention verify` apply to the **masked tier**: the
partitions and the generations' `logs.partitions` lists. Under `derived: window` that is the whole
generation, as before. Under `forever` or a day count, the days that only the derived scope keeps are
listed under `logs.derived` and checked against the derived cutoff instead; their masked lines are
reported (`retained_derived_days`), not counted as leaks. The raw tier has its own clock and checks.

**Interim derived archive (A2 §4.8.6).** The per-day snapshots under
`<partitions>/derived/<source>/<day>.json.gz` are the derived archive until the history tier (H1)
lands, and its import source. Nothing refuses `derived ≠ window`, on any backend.

- A snapshot has `derived_format: 2` and the identity `sha(partition key, version, library identity,
  partition format, fallback identity, compose format)`. It holds the aggregates, hourly cells,
  masked exemplars, every other aggregate-level artifact of the partition (typed-value aggregates
  such as endpoint, exception and coverage data, when the partition format has them), the fallback
  overlay's rows (`v<n>/fallback/<identity>/`), and a content-free accounting summary; never
  line-level rows. Missing inputs are `source_unavailable`, never zero. It is rewritten whenever its
  identity changes while the partition exists (a reparse, a new overlay).
- Format-1 snapshots (written before the format field) are read and marked `derived_format: 1,
  legacy: true`; their typed inputs, accounting and overlay are `source_unavailable`.
- **Serving bound.** Compose serves the masked window plus derived days only within
  `logs.history.serve_days` (default `keep_days`) and, optionally, `logs.history.max_serve_rows`
  aggregate rows (the oldest derived days are left out first). Older snapshots stay archived but
  unserved, so compose cost is bounded by `serve_days`, not by history: each day's documents are
  cached by snapshot identity and only new or changed days are recomputed.
- Window rollups (`signature_window`, `signature_tenant_window`, first/last seen) cover the masked
  window only.
- When the served days span more than one library identity, the manifest's `logs` section carries
  `library_identities` and `identity_span` (`the served days span N library identities`) for
  renderings.

```yaml
logs:
  retention: {keep_days: 10, raw_days: 3, derived: forever}
  history: {serve_days: 30, max_serve_rows: 2000000}
```

**Privacy note.** With `derived: forever`, masked exemplar lines (up to
`compose.max_exemplars_per_signature` per signature and day) and the rollups are kept indefinitely;
`retention verify` lists those days under `retained_derived_days` instead of treating their lines as
leaks, and `retention report` lists them under `scopes.derived`.

**Raw copies (`logs.partitions.raw`).** Off by default. When enabled, the puller writes every pulled
unit **unredacted, exactly as the source returned it** to `raw.path` before redaction, so `logs
reparse --date D --from-raw` can re-run redaction, multiline assembly and parsing without the
source (for example after adding a redactor). This means raw log text (addresses, tokens, prompts,
customer content) sits on disk for `raw_days`. Safeguards:

- the directory is mode `0700` and the files `0600`; `retention verify` fails on any other mode
  (wider or narrower) and on a raw day older than `raw_days`;
- `raw_days` must be shorter than `keep_days`; `raw.path` must lie outside the partition store, the
  backends, the pattern library and the served files (the installed package and the web UI
  bundle), and its parent directory must not be group- or world-writable (`RawLocationUnsafe`);
- every access goes through `ingestion/raw_reparse.py`, the only importer of the raw store: the
  puller's write, `logs reparse --from-raw` and the re-derivation below (the only reads),
  retention's listing and expiry, and the `privacy scan --raw` audit. `schedule.py` and `learning/`
  never import the raw store; ingest, indexing, compose, egress and serving have no path to it (an
  import-graph test asserts these rules);
- **a redaction change re-derives only days still in the raw tier**: when the redaction stamp of the
  current run differs from a sealed partition's, `run-daily` re-runs redaction and parsing from raw
  for the days the raw tier still holds (step `rederive`), and lists the others as
  `stale_redaction`; they keep their old redaction and are never re-pulled silently;
- `privacy scan` skips the raw path; `privacy scan --raw` audits it on purpose;
- `retention run` deletes days older than `raw_days`, `retention report` lists them.

`logs reparse --date D` (without `--from-raw`) parses the retained redacted records again under the
current library. `--from-raw` refuses with `RawUnavailable` when raw copies are disabled or a unit
is missing; pull the units again with `logs settle`.

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

**Not in this release.** The pull window (`limits.window`) and learning-loop proposals with their
`expire` records (L4b). Co-firing communities arrived with the logs analytics profile below
(`incident` documents replace A1's `issue_report`).

### Collection policy and long-term episode archive (`logs.collection`)

One declared block states when to pull, which keyword families are in force, how long each data
class lives and how far back keyword/episode aggregates are served. The defaults reproduce the
behaviour before the block existed (no heavy-pull window, no keyword files, archive off, episode
queries of at most 31 days). A synthetic example is in
[`log_policy/collection_policy.example.yaml`](log_policy/collection_policy.example.yaml) with its
keyword family [`log_policy/keywords.example.yaml`](log_policy/keywords.example.yaml).

| key | default | meaning |
| --- | --- | --- |
| `schedule.summary_refresh_minutes` | `15` | interval of the (deployment's) continuous refresh of today's per-index summaries |
| `schedule.heavy_window_utc` | `null` | `{start: "HH:MM", end: "HH:MM"}` in UTC, inside one UTC day (`start < end`); heavy pulls, day close and import run only inside it. `null`: unrestricted |
| `schedule.day_close_delay_minutes` | `0` | a UTC day is complete this long after midnight UTC; must end before the window closes |
| `schedule.close_after_days` | `1` | day D is closed into the archive in the heavy window of D + N (until then it is served provisional when `provisional.enabled`); `1` is the behaviour before I1, plan K proposes `3` |
| `schedule.grace_days` | `2` | D + `close_after_days` + `grace_days` without a complete set of full-day snapshots: D is archived **partial** from the best capture per scope (flagged, missing and stale scopes listed), or recorded as a gap when nothing was captured |
| `keywords.files` | `[]` | keyword-family files `{path, sha256}` in precedence order; each file is YAML `family`, `phrases`, `rules` (the `SummaryRule` shape) |
| `keywords.effective_from` | `null` | first UTC day of the pinned recipe; required to change the recipe once days are archived |
| `keywords.gap_seconds` | `300` | quiet gap that separates two observed episodes |
| `archive.enabled` | `false` | keep keyword/episode day aggregates forever as append-only day shards |
| `archive.path` | `<partitions>/archive/<source>.episodes.sqlite` | archive file; never under `<partitions>/history` or the staging path |
| `archive.max_text_chars` | `null` | cut archived text fields (exemplars, summaries) to this length; aggregate fields and `evidence_refs` citations are always kept |
| `staging.path` | `null` | collector handoff directory: `<path>/<YYYY-MM-DD>/*.json` summary snapshots |
| `staging.delete_after_import` | `true` | delete a staged day once its archived shard is verified |
| `serving.max_window_days` | `31` | longest day range one episode query may span; above 31 needs the archive or the history tier |
| `serving.default_view_days` | `3` | default range of `issue_history` (and the value `/v2/capabilities` reports as `logs.default_view_days` for the UI); a default, not a serving limit; at most `max_window_days` |
| `provisional.enabled` | `false` | serve every unclosed day from the provisional store (per-scope snapshot versions, one flagged projection file per day version); needs `archive.enabled` |
| `provisional.path` | `<partitions>/provisional/<source>` | provisional store root; never under `<partitions>/history`, containing the archive, or overlapping `staging.path` |

Validation refuses: a window across midnight or with `end <= start`; a close delay that never lets
a day close inside the window; `archive.enabled` without keyword files or without
`retention.derived: forever` (aggregates kept forever are declared, never implied); the archive
together with `logs.history.tier` (separate collection modes: separate datasets); unknown keys.

**Keyword recipe.** `logs policy` and the archive read each keyword file and compare its sha256
with the pin. A changed file is refused under its old pin. Re-pinning starts a new recipe only with
`keywords.effective_from` after the last archived day; a day whose recipe differs from the archived
days of the current epoch is refused, and so is a snapshot whose compiled summary recipe differs
from the pinned families. Each archived day records its recipe identity.

**Retention per data class** (reusing `logs.retention`):

| class | bound |
| --- | --- |
| raw copies | `raw_days` (default 3) |
| masked partitions, live serving window, unclassified examples | `keep_days` (default 10) |
| serving generations | `keep_generations` (default 2, active included) |
| keyword/episode day aggregates (archive) | forever (`derived: forever`); never garbage-collected |
| staging/handoff copies | deleted once their day is archived and verified |

The per-source request-rate cap stays where the transport enforces it,
`sources.<name>.limits.requests_per_second`; `logs policy report` shows it.

**Commands.**

- `logs policy report`: the effective policy, the keyword files (pinned vs actual sha256), the
  recipe, retention per class, the archive state (days, gaps, recipe epochs) and the planned jobs.
  `--emit systemd` prints timer/service unit text derived from the policy (nothing is installed;
  the refresh job's command is the deployment's collector, shown as `@COLLECTOR_REFRESH_COMMAND@`).
- `logs policy verify`: exit 1 when a closed day since the first archived day has neither a rollup
  nor a gap record, an archived day or archive log entry was deleted (hash chain), generations
  exceed `keep_generations`, masked partitions, raw copies or live-window rows are older than their
  bound, a staged copy of an archived day remains, a keyword file no longer matches its pin, the
  archive serving pointer (or, before I1, the active generation) does not serve every archived day,
  an archived day has no history tables or a history row changed after it was written (row-hash
  chain), an unclosed day is past `close_after_days + grace_days`, or a provisional ledger entry
  names a missing projection file.
- `logs policy import`: inside the heavy window, close the due days in order (one day per pass,
  oldest first; a pending day stops the pass): day D is due at D + `close_after_days`; it closes
  complete when every staged snapshot covers the whole day and every scope listed in the day's
  `_expected.json` (`{"scopes": [index, ...]}`, optional, written by the collector) is staged; after
  `grace_days` more it closes partial (or becomes a gap record). Each close streams the snapshots
  (bounded memory), writes the day's typed history rows (`episode_piece`, `issue_day`,
  `coverage_day`, row-hash `history_log`) and backfills them for days archived before I1. Then the
  archive serving pointer advances (O(1): the closed days are served from the archive, their
  provisional entries are dropped), the live window is pruned and the serving generation (rule
  catalog + live window, bounded by `keep_days`, never by the number of archived days) is
  published. Outside the window it does nothing (`outside_heavy_window`). A later complete set for a
  partial day is reported under `refused` and its staging copy is kept (append-only).
- `logs policy refresh` (with `provisional.enabled`): store the staged snapshots of every unclosed
  day as per-scope versions (a snapshot window must start at the day's midnight and end within it;
  the recipe must match the pins), rebuild the projection file of each changed day (oldest first)
  and remove replaced files after an hour. Light: no generation is rebuilt. `report --emit systemd`
  adds it to the refresh job after the collector command.
- `logs policy record-gap --date D --reason TEXT`: an explicit gap record for a day without a rollup.

**`issue_history` (tool and log intent, archive generations only).** Arguments: `window` or
`start`/`end`, `as_of`, `tenant`, `service[]`, `signature_id[]`, `category[]`, `group_by` (up to 4 of
`tenant`, `env`, `service`, `signature_id`, `category`, `day`, `month`; default
`[category, signature_id]`), `top_n` (default 50), `list_episodes` (0-200 most recent pieces). Each
day of the range is read from exactly one tier (archive pointer, else provisional ledger, else gap
or missing); per tier one SQLite `GROUP BY` over `issue_day`, merged over the disjoint day sets
(no EAV row is loaded, so `MAX_MATCHED` does not apply). Rows carry `episodes`, `occurrences`,
`first_failure`, `last_failure`, `active_days`, `tiers`, `partial`, `provisional`; `history.coverage`
lists archive, partial (with missing scopes), provisional (with `as_of`), gap and missing days. An
episode continued across midnight counts once, on its first day in the range. Refused for callers
with access-policy constraints (operator-only) and for archive days without history tables. With
`logs.intents.enabled` and the archive, the rules decision sends count/which/when questions about
outages, incidents or failures over an explicit period to it (a word naming a category value or its
leading token, such as `ML` for `ml_serving`, becomes a category filter when `category` is listable).
- `logs policy gate`: exit 1 outside the heavy window (for a collector's heavy steps).

**Success heartbeats and `log_liveness` (O6, 2026-11-09).** A keyword family whose rules have
`category: success` records success events (a crawl, index build or job completed, a report
delivered, a scheduler tick, an auth success). Its file is listed **after** every failure family in
`keywords.files`: success rules must follow every failure rule (`load_keyword_recipe` and
`compile_summary` refuse otherwise), so a line matching a failure rule stays a failure and each
failure rule's filter is the one it was before. Success rules need `metadata_schema` field
`category` declared filterable (refused otherwise): the failure views exclude `category: success`
by it. Adding the family changes the recipe, so it is a new epoch: set `keywords.effective_from` to
the first day captured with it (days archived before keep their shards and recipe; a day of the new
recipe without the new epoch is refused). The collector's optional per-snapshot `heartbeats` block
(5-minute buckets `{rule_id, bucket_start_ms, count, first, last}`, `heartbeat_bucket_seconds`) must
reconcile exactly with the snapshot's minute series, or the snapshot is refused.

| key | default | meaning |
| --- | --- | --- |
| `liveness.cadence_days` | `7` | days of success heartbeats `log_liveness` reads when the call names no `lookback_days`; at most `serving.max_window_days` |
| `liveness.min_intervals` | `3` | intervals between success runs a cadence needs; fewer: `cadence_unknown` (or `continuous` when the runs never fall quiet for longer than `keywords.gap_seconds`) |
| `liveness.halt_factor` | `3.0` | `halted` when no success for more than this factor x the median interval between success-run starts |

Failure views without success rows: `issue_history` (unless `category: [success]` is asked; the
excluded success messages are stated), `incident_episodes`, the summary-mode `log_top` /
`log_trend` (unless a rule is named), outage investigations and shared-error communities (none is
built from success rules). The coverage row counts the failure side (`keyword_documents`,
`rule_documents`, `episodes`) and the success side apart (`success_documents`, `success_episodes`,
`success_keyword_counts`).

**`log_liveness` (tool and log intent, archive generations only).** Arguments: `tenant`,
`service[]`, `event[]` (success rule ids or their event part, e.g. `crawl_completed`; other rules are
ignored and stated), `lookback_days`, `as_of`, `at` (the instant "now" is read at; default the
current time, or the end of a past `as_of` day), `recovery`, `top_n`. Rows per tenant x env x
service x success rule: `last_success`, `observed_until` (the scope's latest captured instant: a
collection that is behind is never read as a halt), `quiet`, `cadence` (`median_interval`,
`continuous` or unknown), `intervals`, `status` (`halted`, `running`, `cadence_unknown`, or
`no_heartbeats` for a named client/service captured on heartbeat days without a heartbeat of the
asked kinds). With `recovery`: the failure episodes of the lookback and the first success heartbeat
of the same client/env/service after each (`recovered_at`, `time_to_recovery` from the first
failure; `not_recovered`, `heartbeats_absent`, `predates_heartbeats`). Days captured without the
success rules (another recipe, before `keywords.effective_from`) and gap/missing days are listed and
never read as a halt; a lookback entirely before heartbeat recording answers "unknown". Refused
without success rules and for callers with access-policy constraints. The rules decision sends
"has X halted / stopped / is X running", "when did X last complete" and "was it fixed
automatically / did it recover" questions to it. Enable it with `tools.enabled: [..., log_liveness]`
for the tool route.

### Logs analytics profile (`profile: logs_example`; addendum A2 §4.1)

The profile overlays the metadata schema, projections, extraction and tools of a daily log
deployment. The deployment supplies `sources:`, `logs:` (source, partitions path), `backends`,
`storage` and `extraction.pattern_library.path`; explicit values win over the overlay.

```yaml
profile: logs_example
extraction: {pattern_library: {path: ./patterns, engine: re2}}   # log_links: true from the profile
logs:
  source: cluster
  partitions: {path: ./partitions}
  retention: {keep_days: 10, derived: forever}                    # derived: forever from the profile
  compose:
    kinds: {log_day: true, log_hour: true, endpoint_day: true, exception_day: true, coverage_day: true, incident: true}
    exemplars: per_tenant            # signature documents hold the template; examples per tenant
    endpoints: {normalise: path_template, max_per_service: 500}
  communities:
    co_firing: {min_support_cells: 3, min_lift: 2.0, unit: tenant_env_hour, split_above: 12, seed: 42}
  intents: {enabled: true, default_window: today}
  serving: {allow_adhoc_scopes: false}
serving:
  mcp: {transports: [stdio, streamable_http], log_tools: true, mount_on_http: false, token_env: null}
```

| Key | Default | Meaning |
|---|---|---|
| `logs.compose.kinds.*` | all `false` (profile: existing analytics kinds `true`, `shared_error` stays `false`) | the analytics kinds; their typed values come from format-2 partitions (older partitions are reparsed first) |
| `logs.compose.exemplars` | `signature` (profile: `per_tenant`) | `per_tenant`: no document holds two tenants' lines |
| `logs.compose.endpoints.max_per_service` | 500 | endpoint templates kept per (day, unit); the rest fold into `__other__` (row `approximate`) |
| `logs.communities.co_firing` | as above | lift over cells, per tenant; components above `split_above` signatures are split by the deterministic Louvain constructor |
| `logs.compose.kinds.shared_error` | `false` | opt-in daily shared-pattern communities from actual joint ERROR/FATAL hourly rows; separate from incidents |
| `logs.communities.shared_error.min_tenants` | 2 | minimum distinct tenants meeting observed support for the same signature on one partition day; minimum allowed 2 |
| `logs.communities.shared_error.min_lines_per_tenant` | 1 | minimum observed `lines_pulled` per tenant, summed across that tenant's services; estimated counts cannot satisfy this threshold |
| `logs.communities.shared_error.max_groups` | 2000 | maximum daily signature groups, ranked by observed support; dropped group count is reported |
| `logs.communities.shared_error.exclude_signatures` | `[error, generic.error, error.generic, generic_error, error_word]` | exact signature IDs excluded as catch-alls; replace with the deployed library's generic signatures as appropriate |
| `logs.intents.enabled` | `false` | typed log intents for every decision agent, the log tools, and the scope guard; requires the analytics kinds and `exemplars: per_tenant` |
| `logs.intents.default_window` | `today` | the window when a question or tool call names none; anchored on the latest served day when today has no composed data (stated in the rendering). Only a caller's `as_of` disables the anchor: the engine's clock default (Ask, stream, planner) does not |
| `logs.intents.planner_context` | `true` | the llm planner (alone or in a cascade) gets the logs context pack: what each arm stores, a pattern-library summary, served dates, value vocabularies, the intents with input schemas, few-shot examples |
| `logs.intents.planner_context_max_chars` | `24000` | JSON size bound of the pack; lists are cut in a fixed order and named in `truncated` |
| `logs.intents.cascade_to_planner` | `true` | in a cascade, when the rules find no log intent the llm stage plans (earlier stages skipped, traced) |
| `logs.intents.few_shot` | profile: 8 synthetic examples | `{question, intent, args}` planner examples, validated against the intent input models |
| `logs.intents.few_shot_file` | — | YAML list of the same shape, relative to the config file; replaces `few_shot` (checked at load) |
| `logs.serving.allow_adhoc_scopes` | `false` | `true` turns the `intent_required` guard off |
| `extraction.pattern_library.log_links` | `false` | header ontology edges (`emits`, `runs`, `serves`, `raises`, `co_fires_with`); enters the extraction reuse salt |
| `communities.strategy: declared` | — | one community per `community_id` metadata value (incidents and opted-in shared-error supporting documents) |
| `pipeline.arms.graph.edge_kinds` | `[fact]` | edge kinds the free-text graph mode traverses (`co_firing` only deliberately) |
| `access.policy` | `none` | `tenant`: `StaticPolicy` on `access.field` with `unrestricted` operator ids; **refused** under the logs profile (single-tenant serving, A2 B4) |
| `metadata_schema.fields[].role` | — | `exactness` (worst row wins), `basis`, `extraction` (distinct values returned) |
| `metadata_schema.fields[].additive` | `true` | `false`: sum/avg refused (`measure_unsupported`), e.g. a per-row p95 |
| `serving.mcp.log_tools` | `true` | the enabled log tools are MCP tools (`readOnlyHint: true`) |
| `serving.mcp.mount_on_http` | `false` | MCP streamable HTTP at `/mcp` on the HTTP app (needs `streamable_http`; DNS-rebinding protection) |
| `serving.mcp.token_env` | — | a separate bearer token for the mounted `/mcp` |
| `logs.history.tier` | `false` (profile: `true`) | H1: day documents in append-only, content-addressed day shards; the generation holds the window shard and the shard refs |
| `logs.history.backend` | — | a declared backend (records capability, e.g. `opensearch`) for the history rows; default a sqlite file under the partition store |

The profile also declares `count_<query>` and `basis_<query>` fields for every count query of the
logs source. Tools (`tools.enabled`): `log_top`, `log_new`, `log_affected`, `log_trend`,
`log_compare`, `log_tenants`, `log_around`, `log_endpoints`, `log_signature`, `log_coverage`,
`incidents`, `graph_neighbourhood`; each takes its typed input model on `POST /v2/tools/{name}` and
on MCP, and returns rows, exactness, basis, `source_unavailable` days and a deterministic `rendering`.
Level questions also return `source_counts` (the source's own per-unit level counts with their
basis and the unparsed share; see ARCHITECTURE "Level questions against the source's counts"). A
level is cross-checked by its single-level count query, else by a query over exactly the asked
levels, else by the smallest query that includes it (its other levels are compared too); a level no
query covers is `bounded` whenever lines of the same units have no parsed level. Define a
single-level count query (e.g. `counts: {fatal: {levels: [FATAL], tokens: [fatal]}}`) for every
level operators ask about, so each level is checked on its own. `log_top metric: error_rate` divides
the source counts of the levels by the unit's source total (`unit_day` `lines_total`) and refuses
when no count query counts exactly those levels. With `index_name.service_suffix: numeric`,
`log_top`/`log_tenants` take `deployment_id` and `by: deployment`. A `log_top` ranking by service,
tenant or deployment at asked levels ranks by the source counts when the parse is incomplete
(`ranked_by: source_counts`; rows carry `lines` = source lines, `lines_parsed`, `lines_unparsed`,
`unparsed_share`); this needs count queries whose sum is exactly the asked levels (a single-level
query per level, or one over exactly the set). A signature ranking keeps the parsed rows and names
the services with the most unparsed lines. An answer whose evidence is only such aggregates is the
deterministic rendering (no configuration); with a `generator` role, one sentence restating the
rendering's numbers may follow it (one extra generator call per such answer, under the run budget).
`log_trend` and `log_compare` take `tenant`; `log_compare` always compares its periods in date
order. In the rules grammar "customers", "clients" and "accounts" mean tenants. A deployment's
`few_shot_file` should include tenant ("which customers …") and comparison examples; the context
pack's rules state both regardless of the file.

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

`serve` binds to `serving.http.bind` (and, under `auth: bearer_env`, takes `serving.http.token_env`)
unless `--host`, `--port` or `--token-env` override them. The Host allow-list and Origin check
(`421` for another Host, `403` for a cross-origin write) are active when the UI is enabled, when MCP
is mounted on the HTTP app, or when `serving.allowed_hosts` is non-empty: an API-only process behind
a LAN forwarder lists the forwarder's name there. The standalone MCP streamable-HTTP app applies the
same guard when `allowed_hosts` is set. With a `logs:` section every request
pins the generation it reads in the cross-process pin registry (`logs.pins`), so retention in
another process does not delete it mid-request. A generator reply that violates the answer
schema is logged on the `su_multi_arm.engine` logger (violated field paths and error types, the
provider's stop reason; never the reply or the evidence) and the run still ends with `done`: the
retrieved evidence and an abstaining answer ("No answer was generated (…: schema_violation)"),
never a bare `error` event. A 503 logs its cause (exception class, adapter,
operation; never request content) on the `su_multi_arm.serving` logger, and `/ready` reports
`not_ready` with the cause when the active generation cannot be served (the same check a request
makes: fingerprint and every backend pointer).

`serve` runs the **start-up warm-up** (`Engine.warmup`) in a background thread: it takes the pin a
request takes and pre-opens what the first request would load — every pinned unit of a partitioned
collection (members verified, slices checked on disk), the live log strata (unit readers, served-row
masks, unit catalogs, dense matrices; pending vectors are reported, never filled) and a single sqlite
store's lexical segment and vector matrix. No model call and no ranking. `/ready` is 503 until it is
done (`warmup` in the body; `reason` names a failure or a run past `--warmup-timeout`, default
300 s); requests are served meanwhile and pay the cold loads themselves. `--no-warmup` turns it
off (`/ready` is then not gated on it). Python embedders: `create_app(..., warmup=True,
warmup_timeout_seconds=...)`.

```yaml
serving:
  http: {bind: "127.0.0.1:8099", auth: none, page_size_max: 50}  # auth: bearer_env + token_env off localhost
  mcp: {transports: [stdio, streamable_http]}                    # default [stdio]
  actions: {allow: [invalidate_cache]}                           # remote actions; subset of actions.allow
  ui:                                                            # web UI at /ui (wheel su-multi-arm-ui)
    enabled: false                                               # true needs auth: bearer_env or an injected resolver
    mount: /ui
    display_timezone: null                                       # IANA name; display only
    session_storage_token: false                                 # token in sessionStorage (never localStorage/cookie)
    allow_unauthenticated_loopback: false                        # explicit single-user opt-out (doctor warns)
    theme: null                                                  # JSON theme file, relative to this config; checked by config validate/doctor
  allowed_hosts: []                                              # extra Host names for the allow-list (UI enabled)
models:
  roles:
    generator: {provider: openai_compatible, model: my-local-model, base_url: "http://127.0.0.1:11434/v1",
                capabilities: {streaming: true}}               # token deltas only when declared
```

`serve` takes host and port from `serving.http.bind` unless `--host`/`--port` are given. With
`serving.ui.enabled`, every request passes a Host allow-list (`127.0.0.1`, `localhost`, `[::1]`, the
bind host, `serving.allowed_hosts`; otherwise 421) and a foreign `Origin` on a non-GET route or on the
answer stream gets 403. Every `/v2/*` response carries `X-Content-Type-Options: nosniff`. The UI, its
CSP and its views are described in [Serving → Web UI](serving.md#web-ui).

HTTP routes (request bodies are the Python models; `/search` and `/answer` stay as the v1
compatibility router):

| Route | Scope | What |
|---|---|---|
| `POST /v2/search`, `POST /v2/answer` | `search` / `answer` | `SearchResult` / `AnswerResult` |
| `POST /v2/search/page` | `search` | `SearchPage`: page/sort/facets/`any_of` over the retrieved candidates (see "Paged listings") |
| `POST /v2/answer/stream` | `answer` | server-sent events, see below |
| `POST /v2/sessions`, `GET`/`DELETE /v2/sessions/{id}` | any | session of the calling principal |
| `GET /v2/tools`, `POST /v2/tools/{name}` | `tools` | enabled read-only tools; body = the tool's input model |
| `POST /v2/actions/{name}` | `actions` | `{args, idempotency_key, reason}`; needs `serving.actions.allow`; audited; 403 when denied |
| `GET /v2/values/{field}?limit=` | `search` | distinct values of a `listable` field within the ACL scope; `limit` ≤ `page_size_max` |
| `GET /v2/capabilities` | `search` | dataset/generation, pipelines and modes, fields and flags, tools, remote actions |
| `GET /health` | none | liveness only |
| `GET /ready` | none | probes every backend, reports the provider capability pairs and the active generation; 503 when a probe fails, no generation is active or the start-up warm-up (`serve`) is not done (probe error text stays in `doctor --probe`) |

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


### UI navigation profiles and source-summary fields

`serving.ui.navigation_profile` is `standard` by default: all operational views remain
in navigation, with unavailable capabilities labeled. `focused` limits navigation to
Overview (when log tools exist), Ask and Graph. Deep links remain addressable in both
profiles. This is presentation configuration, not an authorization boundary.

The optional Python `ingestion.episode_summaries.compile_summary` helper accepts
`field_map=LogFieldMap(message="event.text", timestamp="event.at")`, using the same
validated source field paths as read-only ingestion. Default paths remain `message`
and `@timestamp`. Both summary and bounded evidence requests use the mapping, and
changing it changes the compilation recipe. This helper compiles and validates
source requests; it does not install a collection scheduler or private connector.

## Schema v3: unified collection (S0, 2026-11-09; validated, not served yet)

`schema_version: 3` accepts every v2 key with the same meaning plus three blocks of the unified
merge plan (`docs/plans/unified_merge.md` §7). In this release they are **validated only**: `config
validate`, `collection describe` and `identity describe` read them, but an engine (`serve`, `search`,
`doctor`, the Python API) refuses a configuration that sets any of them, before opening a backend,
rather than serving the members as one store. They become served increment by increment (partition
core, live log slices, identity, links).

Migration: a v2 file is loaded unchanged and keeps its index fingerprint. A v2 file that sets
`collection`, `crosssource` or `communities.layers` is refused with a message naming the key; set
`schema_version: 3` to use them. A v3 file without these blocks behaves exactly like its v2 twin
(same recipe, same fingerprint). The existing `sources` key (read-only log sources) is unchanged.

```yaml
schema_version: 3
collection:
  mode: partitioned                       # the only mode
  members:
    support:                              # read-only adoption of a validated store (M1b)
      kind: support
      sources: [ticket, doc]              # values of the `source` field its documents carry
      record_kinds: [ticket, doc]
      key: adopted                        # adopted | snapshot | day | month
      cadence: adopted                    # adopted | minutes | hourly | daily | weekly | close_day | manual
      producer: {kind: adopted}           # write_path | log_host | upload | adopted (+ source: <sources.name>)
      id_namespaces: ["ticket:", "https://docs.example.org/kb/"]
      identity_namespace: support.account
      time_roles: {ticket: {event_time: created_at, update_time: resolved_at}}
      canonical_units: {ticket: ticket, doc: doc}
      embed_fields: {ticket: [title, description]}
      field_map: {customer_ref: account, url: null}   # core field -> member field (null: declared absent)
      adopt: {store: support-store, original_config: support.yaml, original_config_sha256: <64 hex>}
      required: true
    logs:
      kind: logs
      sources: [logs]
      record_kinds: [incident, episode_cell, signature, signature_day]
      key: day
      cadence: minutes                    # the live log path (provisional slices)
      producer: {kind: log_host}
      id_namespaces: ["logs:"]
      catalog_kinds: []                   # `signature` is always a catalog kind
    incident:                             # a new dataset built by the shared write path (§8.3b)
      kind: incident
      sources: [incident]
      record_kinds: [incident_event, incident_ticket, incident_rca, incident_pattern]
      key: snapshot
      cadence: manual
      producer: {kind: write_path}
      id_namespaces: ["incident:"]
      identity_namespace: incident.monitor
      field_map: {customer_ref: tenant_candidate, status: incident_status, url: url, component: null}
      time_roles: {incident_event: {event_time: incident_started_at}, incident_ticket: {event_time: incident_created_at}}
      embed_fields: {incident_event: [summary], incident_ticket: [rca], incident_rca: [root_cause]}
      vectors: online                     # import (default) | online: never consult the precomputed import
      metadata_fields: [incident_monitor, tenant_candidate, incident_started_at, incident_created_at, incident_status, incident_has_rca]
  embedding_space: {id: example-space-1, template: engine_template}
  lexical: {support: {stats: adopted}, logs: {stats: query_time}}   # adopted | query_time | member_rrf
  selection: {source_quotas: {ticket: 0.3, doc: 0.2, logs: 0.4}}
  layers:
    - {name: support_segments, kind: segment_sidecar, sources: [ticket], arms: [segment], path: segments/manifest.json, sha256: <64 hex>}
  slices: {compaction: {refresh_seconds: 300, hourly_seconds: 3600, day_base_hours: 6}, max_units_per_day: 19, embed_lag_seconds: 900, gc_grace_seconds: 3600}
  id_directory: {path: registry/ids.sqlite}
crosssource:
  identity: {registry_path: private/identity.sqlite, sha256: <64 hex>, auto_match: exact, fuzzy_min_confidence: 0.8}   # auto_match: exact | fuzzy
  links:
    window_before_days: 1
    window_after_days: 7
    max_per_record: 5
    show_candidates: labelled             # labelled | hidden
    review: {role: reviewer, usd_ceiling: 10.0, approval_ref: <who approved, when>}
communities:
  layers:
    - {name: support, strategy: imported, sources: [ticket, doc]}   # imported | declared | shared_errors | unified_epoch
```

Rules checked at load (each failure names its key):

- members: identifier ids; `sources`, `record_kinds` and `id_namespaces` non-empty and unique; no id
  namespace of one member is a prefix of another's; `time_roles`, `canonical_units`, `embed_fields`
  and `catalog_kinds` name declared record kinds; `field_map` keys are core fields (`source`, `kind`,
  `title`, `text`, `event_time`, `customer_ref`, `service`, `component`, `product`, `version`,
  `status`, `url`, `visibility`); log-line text (`exemplars` of `signature` kinds) is never in
  `embed_fields` (retention invariant); `visibility: operator` only; `adopt` exactly with
  `key: adopted`, `producer.kind: adopted` and `cadence: adopted`; `producer.source` names a
  configured `sources` entry.
- `crosssource.identity.auto_match` (owner rule change 2026-11-10): `exact` (default) joins `matched` aliases
  only; `fuzzy` also joins `candidate` aliases resolved to one customer with confidence ≥
  `fuzzy_min_confidence` (0.5–1.0, default 0.8), recorded as `fuzzy:<rule>` (`identity describe`: `fuzzy_aliases`,
  `fuzzy_rules`). Excluded, unmatched, contested and ambiguous aliases never join; the registry file is not
  changed. Customer grounding of the operations half, links and the SQL identity views all read this view.
- write-path member settings (`vectors`, `metadata_fields`; outage onboarding 2026-11-10): only with
  `producer.kind: write_path` and not the live log member. `vectors: online` embeds every chunk of the
  member with `models.roles.embedder` and the engine template, never consulting
  `embedder.precomputed` (whose `on_missing: error` would refuse a new dataset's text); the member is
  recorded in the collection's embedding space, as the live log member's text is. `metadata_fields`
  must be declared in `metadata_schema`, and `time_roles` must name fields of it: the member store
  is sealed with the collection schema restricted to these fields (plus `source`, `kind` and the
  equivalence field), so the knowledge half's schema carries the member's own fields and never the
  log member's; a document with another metadata key is refused at `collection register`; the build
  makes no typed projection (a write-path member serves none) and keeps only the `ingestion.attach`
  entries whose target fields it declares. A write-path member's own `embed_fields` now override
  `ingestion.projections.embed_fields` for its kinds. All three settings enter the member's
  partition recipe (`content.member_build`) of a write-path member; members without them, and log-host or live log members (whose slice ledger is keyed by the recipe), keep today's recipe. A new
  source joins the knowledge half by listing it in `crosssource.planner.knowledge_sources`.
- `lexical.<member>`: `adopted` only for (and the default of) an adopted member; `member_rrf` only for
  `day`/`month` members; default `query_time`.
- `selection.source_quotas`: shares in (0, 1], summing to at most 1, naming declared sources.
- `layers`: unique names, declared sources, exactly one of `backend` (a `graph_attach` layer names the
  kuzu `mode: attach` backend) or `path` + `sha256`.
- `slices`: read only with a `cadence: minutes` member; `max_units_per_day` at least what the
  compaction ladder produces (1 base + 6 hourly + 12 five-minute = 19 by default);
  `gc_grace_seconds` at least `budgets.deadline_seconds`.
- `crosssource.links.review`: `role` is a configured chat role, `approval_ref` is required,
  `usd_ceiling` at most 10.0 (the approved shared review ceiling).
- `communities.layers`: need a `collection`, declared sources, and `layer` (when set) naming a
  collection layer. They enter the index recipe only when set.

Relative paths (`adopt.store`, `adopt.original_config`, layer `path`, `id_directory.path`,
`crosssource.identity.registry_path`) resolve against the YAML file's directory.

**Path resolution fix (S0).** `ingestion.attach[].path`, `communities.imported.path` and
`pipeline(s).arms.segment.path` now also resolve against the YAML file's directory (before, a relative
value resolved against the working directory). Absolute paths are unchanged. The index fingerprint is
unchanged: `communities.imported.path` enters the recipe as written (the file is pinned by `sha256`),
and the other two never entered it.

## Identity registry (M2b-1, 2026-11-09; `crosssource.identity`, `crosssource.links.review`)

No new configuration keys. The `identity` commands read `crosssource.identity.registry_path` (or
`--registry`), and `identity review` reads `crosssource.links.review` (`role`, `usd_ceiling`,
`approval_ref`) and `budgets.price_table`. The registry and the review ledger are private files:
keep them outside the repository. Since M2b-2 the engine serves `crosssource` (customer grounding
through the pinned registry, the two-halves planner, labelled link display; see below).

```bash
su-multi-arm --config c.yaml identity candidates --create --customers customers.yaml \
    --documents tickets.jsonl --namespace support.account --field account --kind ticket
su-multi-arm --config c.yaml identity candidates --values observed.yaml      # {namespaces: {<ns>: {values: {raw: count}, missing_records: [id]}}}
su-multi-arm --config c.yaml identity candidates --seed-customers --documents tickets.jsonl --namespace support.account --field account
su-multi-arm --config c.yaml identity decide --namespace logs.tenant --raw <raw> --action match --customer <id> \
    --rationale "..." --evidence-new-id ev-1 --evidence-kind web --evidence-url https://... --evidence-retrieved 2026-11-09 --evidence-quote "..."
su-multi-arm --config c.yaml identity review --dry-run                       # queue + worst case; no model call, no ledger written
su-multi-arm --config c.yaml identity review --max-usd 0.50 [--limit N]      # paid: needs the shared ceiling + price table
su-multi-arm --config c.yaml identity report                                 # per-namespace counts with denominators, owner/review queues
su-multi-arm --config c.yaml identity export --out identity-r12.yaml         # never overwrites; prints sha256 for pinning
su-multi-arm --config c.yaml identity import --from export.yaml --registry new.sqlite
```

- Namespaces look like `source.field` (`support.account`, `logs.tenant`, `analytics.company_id`,
  `tracker.customer`). Customers come from `--customers` (`[{customer_id, name}]`) or
  `--seed-customers` (each organization name becomes a customer unless a casefold-equal one exists).
- Only `exact`/`casefold` unique hits auto-match; any other match needs `decide --action match` with
  evidence **and** an `agree` review. `decide` always needs evidence (`--evidence-id`,
  `--evidence-file` or one inline entry); `web` evidence needs a URL, retrieval date and quote. A
  `refutes` review makes the alias `contested` (no join) until an `--authority owner` decision.
- `identity review` needs `crosssource.links.review` and a price for its role. The ledger
  (`review_ledger.sqlite` next to the registry, or `--ledger`) records the ceiling and approval
  reference at first use; editing `usd_ceiling` upwards later has no effect (lowering does). Every
  call reserves its worst case first; `--max-usd` caps the run.
- Any mutating command changes the registry digest: output shows `pinned: mismatch` and cross-source
  joins stay disabled until `crosssource.identity.sha256` is updated (or pin an export file).

## Cross-source links (M3, 2026-11-09; `crosssource.links`)

No new configuration keys. `links build` reads `crosssource.links.{window_before_days,
window_after_days, max_per_record}` (defaults 1, 7, 5 without the block) and the pinned identity
registry (`crosssource.identity.{registry_path, sha256}`, or `--registry` + `--registry-sha256`);
`links show` reads `show_candidates` and `max_per_record`; `links review` reads
`crosssource.links.review` and `budgets.price_table` like `identity review`. The link store is a
private file passed with `--store`; serving pins it with `crosssource.links.{store_path, sha256}` (M2b-2, below).

```bash
su-multi-arm --config c.yaml links build --create --store links.sqlite \
    --documents members.jsonl --documents live-days.jsonl            # pinned documents, in pin order
su-multi-arm --config c.yaml links review --store links.sqlite --documents ... --dry-run   # queue + worst case; no call
su-multi-arm --config c.yaml links review --store links.sqlite --documents ... --max-usd 0.50 [--limit N]
su-multi-arm --config c.yaml links decide --store links.sqlite --link lnk-... --action reject --rationale "..."
su-multi-arm --config c.yaml links report --store links.sqlite      # by status/pair/tier, retractions, queues
su-multi-arm --config c.yaml links show --store links.sqlite --record ticket:1003 [--sha256 <pinned>]
su-multi-arm --config c.yaml links export --store links.sqlite --out links-l12.yaml   # never overwrites; prints sha256
```

- `--documents` are JSONL `Document` exports of the pinned collection; a later file's version of an
  id wins (pass archive members first, then each live day's pinned slices in ledger order).
- An unpinned or mismatching identity registry disables the customer/time-window builder (reported
  as `identity_digest_mismatch` / `identity_not_configured`); explicit references and mention rules
  still build.
- Link review uses the **same** ledger file as identity review (`review_ledger.sqlite` next to the
  identity registry, or `--ledger`): one cumulative ceiling, recorded with its approval reference at
  first use.
- Every build, review and decision changes the store digest; a pinned `LinkView` serves no links
  until it is re-pinned.

## Partitioned collections: members, compose and serving (M1a, 2026-11-09)

From M1a an engine **serves** a `schema_version: 3` configuration with `collection.mode:
partitioned` when it declares no adopted member, no `collection.layers`, no `communities.layers` and
no `crosssource` block (those stay refused until their increments; M1b serves adoption and three
layer kinds, see "Adopted members and attached layers" below). No configuration key was added;
M1a reads `collection.members.<id>.{kind, sources, record_kinds, key, id_namespaces,
catalog_kinds}`, `collection.embedding_space`, `collection.lexical.<id>.stats`,
`collection.selection.source_quotas` and `collection.id_directory.path`.

Requirements of the partition core (each refused at engine construction with its key path):

- catalog, records and the lexical/dense/structured/exact-id arms use **one sqlite backend**
  (`storage.catalog`), with `lexical_engine: bm25` (`fts5` is refused for new members);
- `projection.enabled: false`, no `logs` section, and no `graph`, `community`, `segment` or
  `typed_query` arm in any pipeline.

Layout: the member registry lives at `<storage.catalog backend path>/collection/` (`members/`,
`generations/`, and `id_directory.sqlite` unless `collection.id_directory.path` is set).

Workflow (Python API or CLI):

```bash
su-multi-arm --config unified.yaml collection register tracker.jsonl --member tracker --key snapshot:2026-11-08
su-multi-arm --config unified.yaml collection register logs-day.jsonl --member logs --key day:2026-11-04 --keyword-epoch kw-e1
su-multi-arm --config unified.yaml collection register signatures.jsonl --member logs --key catalog
su-multi-arm --config unified.yaml collection compose                       # every live member
su-multi-arm --config unified.yaml collection compose --unit tracker/snapshot:2026-11-08 --expected <generation>
su-multi-arm --config unified.yaml collection status
su-multi-arm --config unified.yaml search "crawler fetch timeout"
```

- `register` builds one member with the shared write path and records its ids in the id directory;
  documents must carry one of the member's `sources`, one of its `record_kinds` and an id under one of
  its `id_namespaces`; keys follow the member's `key` scheme (`snapshot:YYYY-MM-DD`; `day:`/`month:`
  for `key: day`; `month:` for `key: month`); catalog kinds (`signature` and `catalog_kinds`) go to
  the `catalog` key only. A document id already live in another member is refused unless the new
  member names that unit with `--replaces` (its overlapping ids are then superseded).
- `compose` activates a unified generation by reference (no rebuild); `--expected` is a
  compare-and-set on the active generation (`none` for the first). The active generation is checked
  against the **collection recipe** only (dataset, member list and their namespaces/sources/key
  schemes/catalog kinds, embedding space, index format); changing a member's chunking or keyword
  epoch does not invalidate it.
- `ingest`/`delete` are refused for a partitioned configuration.

Ranking: lexical candidates are ranked per member (stratum) and fused by reciprocal rank across
members, under `collection.selection.source_quotas` when declared (each member's share is the sum of
its sources' shares, renormalised over the members in the request's scope). A request scoped to one
member's sources returns exactly what that member alone would. `collection.lexical.<id>.stats`:
`query_time` (default) ranks a member's units with statistics summed at query time (equal to one
store of the same documents); `member_rrf` fuses its units by rank.

### Live log path settings as read by the slice ledger (M2a, 2026-11-09)

No key was added or changed by M2a; `docs/config.schema.json` is unchanged. The live log path
modules (`ingestion/log_slices.py`, `log_members.py`, `text_vectors.py`, `handoff.py`,
`backends/sqlite/query_stats.py`) read these existing S0 keys once the partition core wires them
(after M1a):

| Key | Read by | Effect |
|---|---|---|
| `collection.slices.compaction.{refresh_seconds, hourly_seconds, day_base_hours}` | `LogSliceLedger.due` | the compaction ladder: finished hours → hourly slice, every `day_base_hours` (and after the day ends) → day base |
| `collection.slices.max_units_per_day` | `LogSliceLedger.seal` | a seal that would pin more units for a day is refused (`SliceRefused`, compaction behind) |
| `collection.slices.gc_grace_seconds` | `LogSliceLedger.gc` / `retire` | a replaced or retired slice is deleted only when no slice pin holds it and it was retired longer ago than this |
| `collection.members.<id>.embed_fields` | `LogSliceLedger`, `text_vectors.embed_rows` | the only kinds/fields embedded; log-line text is refused again at embedding time |
| `collection.members.<id>.catalog_kinds` (+ `signature`) | `LogSliceLedger` | catalog kinds stay out of slices except first-seen documents (`catalog_provisional`) |
| `collection.embedding_space` | `TextVectorStore` | the store is pinned to one space; another model, template or epoch is refused |

The member's `PartitionRecipe` supplies the bm25 analyzer (`analyzer.engine: bm25`, tokenizer, k1, b,
epsilon; optional `content.bm25` for `lowercase`/`stemming`/`include_title`) and the chunking
(`chunking.size`, `chunking.overlap`, `field_order`); a ledger refuses a recipe other than the one it
was created with (a recipe change opens a new epoch with a new ledger).

### The live log member in a served collection (M2a wiring, 2026-11-09)

No key was added or changed (`docs/config.schema.json` unchanged). A member with `cadence: minutes`
is now **served** as the live log member: its archive members, catalog member and the slices of the
live window answer searches together with the other members.

```yaml
collection:
  mode: partitioned
  members:
    logs:
      kind: logs
      sources: [logs]                       # exactly one source (one slice ledger)
      record_kinds: [incident, signature, signature_day, episode_cell, summary_coverage, unit_day, unclassified_example]
      key: day                              # archive day/month members + slices per day
      cadence: minutes                      # the live log path
      producer: {kind: log_host}
      id_namespaces: ["logs:"]
      embed_fields: {incident: [title, summary], unclassified_example: [exemplars]}
  slices: {gc_grace_seconds: 3600}          # >= budgets.deadline_seconds
```

Refused at engine construction (`ConfigError` naming the key): a live member with more than one
source, a `key` other than `day`, `collection.lexical.<id>.stats` other than `query_time`; the v2
`logs` block (log intents/history) on a partitioned collection (M2b). Building the live member with
`Engine.build_member` is refused (its units come from the slice ledger: `LiveLogs.ledger(member)`
for refreshes/compaction, `LiveLogs.close_day(member, day)` to close a day and compose once).

What the configuration decides for the live member: the member's partition recipe (chunking,
analyzer and the catalog backend's `bm25` options), `embed_fields`, `catalog_kinds`,
`collection.slices`, `metadata_schema` (every slice record is validated as the write path validates
records), `collection.embedding_space` (the text-hash vector store's space; its `template` decides
the embedded text — `engine_template` = the write path's `title + field + text`),
`collection.id_directory` (the ledger mirrors registrations into it),
`budgets.deadline_seconds` (+60 s: the request lease of the slice pins) and
`sessions.ttl_seconds` (+ the deadline: a session's hold — every slice its turns read stays pinned
while the session is alive, renewed by each turn, released by closing the session).

## Adopted members and attached layers (M1b, 2026-11-09)

From M1b a partitioned collection may **adopt an existing store read-only** as a member and serve
that store's companion stores as **attached layers**. New key: `collection.layers[].member`. M1b
reads `collection.members.<id>.adopt.{store, original_config, original_config_sha256}`,
`collection.layers[]` and `communities.layers[]` (`strategy: imported` only).

```yaml
schema_version: 3
backends:
  local: {kind: sqlite, path: /srv/unified, lexical_engine: bm25}    # registry + runtime facets
  kz: {kind: kuzu, path: /srv/support/graph.kuzu, mode: attach, attach: {mapping: /srv/support/mapping.yaml}}
collection:
  mode: partitioned
  members:
    support:
      kind: support
      sources: [ticket, doc]
      record_kinds: [ticket, doc]
      key: adopted
      cadence: adopted
      producer: {kind: adopted}
      id_namespaces: ["ticket:", "https://docs.example.org/"]
      adopt: {store: /srv/support/store, original_config: /srv/support/support.yaml, original_config_sha256: <sha256 of that file>}
      required: true
  layers:
    - {name: support_graph, kind: graph_attach, backend: kz, sources: [ticket, doc], arms: [graph], member: support}
    - {name: support_segments, kind: segment_sidecar, path: /srv/support/segments, sha256: <collection layers>, sources: [ticket], arms: [segment]}
    - {name: support_communities, kind: communities_imported, path: /srv/support/communities.jsonl, sha256: <file sha256>, sources: [ticket, doc], arms: [community], member: support}
communities:
  strategy: imported            # as in the original configuration
  imported: {path: /srv/support/communities.jsonl, sha256: <file sha256>}
  layers: [{name: support, strategy: imported, sources: [ticket, doc], layer: support_communities}]
```

```bash
su-multi-arm --config unified.yaml collection adopt --member support   # read-only; registry only
su-multi-arm --config unified.yaml collection layers                   # digests to pin (sha256)
su-multi-arm --config unified.yaml collection compose
```

- `adopt` checks the original configuration's sha256, that it names the declared store as its sqlite
  catalog and serves the collection's `dataset`, and that the store's active manifest carries the
  fingerprint the original configuration yields (recomputed without opening any backend). A moved or
  re-stamped store is refused (the store path is part of today's recipe; use `index adopt` on a copy
  if a store must move). Documents must match the member's `sources`, `record_kinds` and
  `id_namespaces`. The store is never written; only the registry under the unified catalog backend
  changes. Re-running `adopt` on an unchanged store is a no-op.
- The unified configuration should keep the original's arm, model, metadata-schema and projection
  settings so the adopted member is served exactly as today (the typed projection is served when it
  is the only member's; several members' projections as one SQL surface wait for M4).
- `layers[].member` binds `graph_attach` (the attached graph must declare the adopted store's
  generation in its mapping, or `any_generation`) and `communities_imported` (the file must be the one
  the store imported) to an adopted member; a `segment_sidecar` layer's `path` must be the segment
  arm's `path`. `sha256` pins a file layer (a directory: the digest `collection layers` prints);
  for `graph_attach` it is optional (the mapping digest and the database listing are pinned at
  compose). `required: true` makes an unavailable layer refuse the compose; otherwise it is left out
  and its arm returns no hits with a `layer_unavailable` report (coverage incomplete).
- The graph arm must read a `graph_attach` layer's backend, the community arm one
  `communities_imported` layer through the catalog backend, the segment arm a `segment_sidecar`
  layer; anything else is refused at engine construction with its key path.
- Search traces of a partitioned collection carry `collection` (members and layers with status).

## Two-halves planner, link pinning and the unified dataset switch (M2b-2, 2026-11-09; `crosssource.planner`, `crosssource.links.{store_path, sha256}`)

```yaml
schema_version: 3
crosssource:
  identity: {registry_path: private/ids.sqlite, sha256: <64 hex>}
  links:
    review: {role: reviewer, approval_ref: <approval>}
    store_path: private/links-l12.yaml      # pinned link store (sqlite or export); both or neither
    sha256: <64 hex>
  planner:
    enabled: true                           # default false: every request unchanged
    knowledge_sources: [ticket, doc]        # Document.source values; empty on a partitioned collection = members not produced by a log host
    customer_namespaces: [support.account]  # registry namespaces whose raw values name customers in questions
    tenant_namespace: logs.tenant           # matched tenants a named customer filters the operations half to
    fallback: deterministic_plan            # or abstain (today's on_failure policy)
    max_operations_calls: 3                 # per-tenant calls of the operations half
```

- `planner.enabled` needs no other key. Without `logs.intents.enabled` there is no operations half
  (staged cutover step 1: every answer keeps today's plan and evidence and gains `coverage` with
  `logs: not_served`). With log intents, questions with an operations intent get both halves (see
  docs/ARCHITECTURE.md, M2b-2). The operations half reads `question.vocabulary`,
  `question.timezone` and `logs.collection.serving.default_view_days` (M0 keys, unchanged).
- A customer named in a question joins only through **matched** aliases of the pinned registry; a
  customer without a matched tenant gets no operations half (stated); a registry digest mismatch
  resolves no customer (stated).
- `fallback: deterministic_plan` replaces a planner failure after its repair (`schema_repair_attempts`)
  with a labelled plan; it never replaces an unresolvable value, an exhausted budget or a caller-typed
  aggregate/scope.
- `links.store_path` resolves against the YAML file's directory; a missing file or a digest mismatch
  serves no links (stated in `coverage.links` and `limitations`). Re-pin after every build, review or
  decision (`links export` prints the sha256).

Legacy QA profile (data only):

```yaml
datasets:
  - id: support-qa
    config: support.yaml            # today's configuration (served while the switch is off)
    unified:
      enabled: false                # staged cutover: true serves the same id from the merged view
      config: unified.yaml          # schema v3 with crosssource.planner.enabled (refused otherwise)
```

Routes and envelope keys are unchanged; a switched dataset adds `coverage`, per-citation `source`,
`links`, `link_status`, and `view: unified` on `/health?dataset=<id>`. Rollback: `enabled: false`
(or the previous profile) and restart.

## Staging fixes on the planner / coverage / serving side (2026-11-10; `crosssource.planner.scope_knowledge_steps`)

```yaml
crosssource:
  planner:
    enabled: true
    scope_knowledge_steps: true     # default; false = unscoped knowledge steps (log records may enter the hits)
collection:
  members:
    support:
      identity_namespace: support.account
      field_map: {customer_ref: organization}               # read by `links build` and `identity candidates`
      time_roles: {ticket: {event_time: created_date}}      # the ticket time of tier-B customer links
```

- **Source-scoped knowledge half.** When the collection holds a source outside
  `planner.knowledge_sources` (a log member, or one store with a `logs` block), every
  knowledge-half step on lexical, dense, structured (and graph / community / segment unless only
  layers of knowledge sources serve them) executes under `source in knowledge_sources`. The plan
  is not rewritten; the log units are not opened; the operations half alone reads logs. `exact_id`
  is not scoped, and a request filter on `source` is applied as given. Without such a source
  nothing changes. Coverage adds `knowledge_scope: {sources, arms}`.
- **Coverage.** `plan.halves` labels each step by the sources it reads (a ticket count is the
  knowledge half). `sources.logs` carries `candidates` / `evidence` counts and the pinned log
  `members` and live `slices`; its status is `not_served` only when no log member is pinned and
  no log record is in the hits — otherwise `intents_not_served` (log members pinned, no
  `logs.intents`) or `records_only` (log records in the hits without an operations half).
- **Declared field names.** `links build` reads a kind's customer field from
  `field_map.customer_ref` (under the member's `identity_namespace`; `null` = no customer) and the
  event time from `time_roles.<kind>.event_time` or `field_map.event_time`; undeclared kinds keep
  `account` / `tenant` / `customer` / `company_id` and `created_at`. The build report's `fields`
  states which names were read. `identity candidates --documents --namespace ...` takes `--field`
  from the same declaration when it is omitted.
- **Spend.** A model call whose connection to the provider (or the egress proxy) was never
  established (`ConnectError`, `ConnectTimeout`, `ProxyError`) is released from the
  `serving.answers` day ledger and provider total instead of being booked at its estimate; its
  request call slot still counts. The trace's `model_call` event says `sent: false`.
- **`identity describe`** reports `served` (joins enabled and read by `crosssource.planner` or a
  pinned `crosssource.links`), `joins_enabled`, `consumers`, `registry_version`,
  `joinable_aliases`, or the `reason` joins are disabled.
- **Legacy profile.** On a dataset served from the unified view, records with `source: logs` are
  labelled `Logs` unless `content_source` names them (the profile wins); other datasets unchanged.
  The legacy `plan` block gains `fallback: deterministic_plan` when the planner fallback ran.

## Staging fixes on the data / retrieval side (2026-11-10; `collection.members.<id>.adopt.embedding_space`)

- The v2 `logs` block is accepted on a partitioned collection with exactly one live log member
  (`cadence: minutes`); `logs.history.tier` is refused there, and the member's `producer.source`, when
  set, must equal `logs.source`. The episode archive file and provisional store are not opened: closed
  days are the member's archive day members and unclosed days its live slices. `log_liveness` still
  needs `logs.collection.keywords.files` (the success-heartbeat rules).
- `projection` with a live log member validates: the projection is the one non-log member's; the
  log member's SQL tables (`issue_day`, `coverage_day`) and identity views join it automatically
  for all-rows principals.
- `collection.members.<id>.adopt.embedding_space: <store space id>` (new, optional; needs a declared
  `collection.embedding_space` with a different id): declares the adopted store's space equivalent.
  Run `su-multi-arm collection verify-space --member <id>` (re-embeds a stratified sample of
  `embedding_space.equivalence.sample` chunks; mean/min cosine thresholds) before `collection compose`.

## Staging round 2 fixes on the planner side (2026-11-10; `crosssource.planner.operations_terms`)

```yaml
crosssource:
  planner:
    enabled: true
    operations_terms: [log, logs, service, services, crawl, crawler, error, errors, outage, ...]  # default list; see the schema
question:
  vocabulary:
    terms:
      - {term: report, field: service, values: [report-worker]}               # match: word (default): "report", not "reported"
      - {term: crawl, match: prefix, field: service, values: [crawler]}       # prefix: "crawl", "crawling", "crawler", "crawled"
```

- **`operations_terms`** (new; default: a list of log/operations words — log(s), service(s), crawl
  and its inflections, error(s), outage(s), downtime, incident(s), exception(s), failure(s),
  signature(s), endpoint(s), heartbeat(s), latency, timeout(s), stack trace, 5xx, cron, indexing,
  indexer). A question gets an operations half only when it names one of these (whole word or
  phrase, case-insensitive, no prefix matching: list each inflection) **or** a failure symptom of
  the unified detector ("unauthorized", "timed out", "stuck", "queued", "failed", "slow", "down",
  …). Generic words ("new", "mentions", "increase", "trend") are not operations evidence, so "How
  many are new?" stays a support question and never triggers `log_new`. Terms are lower-case
  words/phrases (`[a-z0-9][a-z0-9 _-]{0,47}`), unique; an empty list leaves the symptoms only. A
  log-only deployment (no `crosssource.planner`) is unchanged.
- **`question.vocabulary` matching.** `match: word` (the default) matches the term as a whole word
  or phrase (`report` never matches "reported"; spaces, `_` and `-` between parts are
  interchangeable). `match: prefix` is the explicit opt-in to continuations (`crawl` → "crawling",
  but also `report` → "reported"); use it only for stems whose continuations all mean the same
  thing, and list other forms as separate `word` terms.
- **Support-only questions keep today's plan** on a partitioned collection with `logs.intents`: the
  log scope guard (`intent_required`) applies only to structured scopes that can read a log member
  (a member declaring a log kind, produced by a log host or fed every few minutes); a knowledge-half
  step executed under `source in knowledge_sources` (or a step / request filter naming only
  non-log sources) is not guarded. The knowledge half's value listings (planner values, grounding,
  qualifier checks) read the knowledge sources only. When the planner's reply fails on a question
  without an operations half and the typed router executed a route that answers its aggregate, the
  route is the plan (`typed_route_only`), as without the log member; the labelled
  `deterministic_plan` fallback is used only otherwise.
- **`budgets.plan_check` under the two-halves planner.** The logs planner context
  (`logs.intents.planner_context`) is never sent by the two-halves planner, so its bound
  `planner_context_max_chars` is not counted; the planner catalog counts the source cards (measured
  from configuration) plus 1,500 characters per possible operations call
  (`2 × max_operations_calls + 2`). Review hops usually dominate `W`: lower `review.max_hops` (or
  the reviewer's evidence) for a pipeline whose `W` still exceeds `max_usd`.
- **Support-only questions get today's model inputs** (staging round 5, 2026-11-10). A question
  that gets no operations step (no operations word or failure symptom, log intents off, a
  caller-typed aggregate, or a deterministic operations half that planned nothing; it is planned
  before the router and the planner) is a support question: its planner payload, router payload,
  SQL writer catalog and answer-contract targets are those of the support store alone. The planner and the contract read
  the metadata schema without the fields that no knowledge member's own sealed schema declares (the
  log member's fields); the SQL writer sees the typed projection's tables only, without the log
  and identity views (the statement is still guarded and executed over the request's whole
  surface). An operations question keeps the cross-source SQL catalog; its knowledge half also
  plans over the knowledge fields only. Nothing to configure.
- **Two-halves answers show the operations half as rendered.** When a question has an operations
  half (planned or refused), the answer starts with `**Current <operation sources>:**` (for
  example `Current logs:`): every operations aggregate's result rows and rendering, verbatim and
  cited, and the operations half's notes (window, question-clock reading, services read, refusals
  and limitation reasons). The generator does not receive the operations aggregates as evidence;
  it receives that section as `operations_shown` (no ids) with a fixed rule, and its text follows
  under `**Past <knowledge sources>:**` (for example `Past tickets/docs:`). Before the answer
  contract runs, sentences, claims and the short answer of the generated part are removed when
  they assert a current halt, outage, recovery or cause that no operations item states
  (`operations_claim_unbacked`; a halted or recovered liveness row states one, an outage or a
  cause never is), present earlier records as current or recent incidents
  (`records_presented_as_current`), or restate the operations evidence (`operations_restated`).
  Removals are recorded as violations with action `strip`; under
  `generation.contract.on_violation: annotate` each is also stated as a limitation, and `fail`
  raises. The labels come from the configured source names; the operations section is shown even
  when the generator abstains.
- **The operations section opens with one reading line** (staging round 6, 2026-11-10). Directly
  under `**Current <operation sources>:**`: `As of <time>; window <start> to <end>; clock <zone>;
  services read: <services>; tenant: <tenant>.` All of it comes from the executed operations
  aggregates: *as of* is the latest data time they report (a provisional day's as-of time, a
  liveness row's `observed until`; without one, the window's last day with "(closed days only)");
  every distinct window is listed (`windows ... and ...`); the clock is `question.timezone`, with
  `log times in <logs.timezone>` added when the two differ; the services are the bound `service`
  arguments (`all (no service filter)` when an aggregate read without one); the tenant is shown
  only when one was bound. No line when nothing was read (every operations call refused; the
  refusal note says why). The generator's copy (`operations_shown`) carries the same line. Nothing
  to configure.
- **`pipeline.generation.schema_repair_attempts`** (`0` default, or `1`; staging round 6, N15).
  With `1`, a generator reply that fails the answer schema gets one more call to the same role:
  the same payload plus `schema_repair` (the previous reply and its validation paths) and a fixed
  repair suffix on the system prompt (`profiles/prompts.GENERATOR_REPAIR_PROMPT`), not streamed.
  It is an ordinary metered call: it reserves on `budgets.max_llm_calls`, `max_usd`, the deadline
  and the `serving.answers` ledger, and its `model_call` trace record says `attempt: 2`,
  `retry_reason: schema_violation`; trace events `generator_schema_repair` (`started`,
  `schema_valid` / `failed` with the reason, or `skipped` for a previous reply over 64,000
  characters). A repair the budget or deadline refuses, a provider failure of the repair and a
  second violation all leave today's answer ("No answer was generated (... schema_violation)";
  on a two-halves answer the operations section still stands). `budgets.plan_check` counts the
  repair as one more generator slot (the evidence plus the previous reply, at most `max_tokens`).
  Opt-in like the planner's `decision.schema_repair_attempts`; a request never gets more than one.
- **`annotate`: a count taken from a cited aggregate is kept with its note.** An unbacked count
  statement whose number is a value of an aggregate the answer cites (citations or claims) is kept
  with "I can't verify this count is exact …", whether or not its paragraph carries an inline
  `[id]`; before, such a statement was removed as uncited when the marker sat elsewhere.
