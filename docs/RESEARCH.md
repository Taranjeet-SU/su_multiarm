# Research and design decisions

Research date: 2026-10-11. This package draws design lessons from the primary sources below;
it does not copy their implementations or promise their benchmark results. Vendor configuration
and available adapters change, so the package's tested support matrix is authoritative for this release.

## 1. Configuration as the public API

[Mem0 configuration](https://docs.mem0.ai/open-source/configuration) separates model, embedding,
vector-store and reranker providers. This is a useful usability pattern: the common engine should
not contain branches for every deployment's secrets, source IDs or model names. Our configuration
has independent model roles, typed backend choices and optional dependencies. We do not import
Mem0's memory-update semantics: support documentation remains authoritative and answers do not
silently become new facts.

[Cognee configuration](https://docs.cognee.ai/setup-configuration/overview) separates relational,
vector, graph, embedding and extraction concerns. Its documentation also describes environment
loading and cached configuration behavior. We use an explicit configuration object with paths
resolved against its file. Constructing or importing the package makes no remote calls and does
not load a private `.env`. Keys are referenced by environment-variable name. Catalog and derived
artifacts are separate logical objects, kept in one generation for consistency.

## 2. Dense retrieval and backend choice

[Faiss metric guidance](https://github.com/facebookresearch/faiss/wiki/MetricType-and-distances)
explains the relationship between normalized vectors, inner product and cosine similarity. We
normalize both queries and document embeddings and validate vector shape and finiteness. A manifest
fingerprint includes embedding model, prefixes, dimensions and revision. A large model with a
mismatched recipe is not an upgrade; index/query compatibility must be checked first.

[pgvector](https://github.com/pgvector/pgvector) supports exact search and approximate indexes;
filtering can affect approximate candidate recall. The first adapter uses exact cosine with the
eligible passage IDs filtered before ranking. Faiss also uses exact search. This establishes a
correctness baseline before adding HNSW/IVF tuning. It is not the final performance design for
million-passage corpora. [Qdrant filtering](https://qdrant.tech/documentation/search/filtering/)
reinforces the need for consistent payload constraints; Qdrant is a future adapter, not a selectable
provider in this release.

## 3. Lexical search and candidate preservation

[SQLite FTS5](https://www.sqlite.org/fts5.html) provides BM25 ranking, with lower native scores
representing better matches. We negate those scores into a consistent higher-is-better interface.
Literal user terms are escaped rather than treated as arbitrary FTS syntax. Title and body are
indexed, and all matching passages are considered before canonical-document collapse. That fixes
the structural failure where many chunks or case digests crowd useful KBs out of a small candidate
window. It can cost more latency; the tradeoff is documented rather than hidden.

[PostgreSQL full-text controls](https://www.postgresql.org/docs/current/textsearch-controls.html)
describe normalized lexemes, query parsing and ranking. The adapter uses English text search and
parameterized queries. It is not byte-for-byte equivalent to SQLite's tokenizer/ranker, and the
sample does not claim multilingual parity. A future language-specific configuration needs its own
migration and evaluation.

[Haystack DocumentJoiner](https://docs.haystack.deepset.ai/docs/documentjoiner) illustrates that
concatenation, score merging and rank fusion are distinct operations. Our default retains a
canonical-document union from up to 20 results per requested arm. RRF, min-max and a normal-CDF
transform of z-scores are alternatives, with source and arm weights configurable. All strategies
return the union unless an explicit final cap is set. A source preference changes ordering; it must
not be advertised as a universal relevance improvement.

## 4. Grounded extraction and graph construction

[GLiNER](https://github.com/urchade/GLiNER) accepts caller-specified entity labels. It is useful for
jurisdiction/product/error extraction, not a drop-in relevance reranker. Our adapter preserves
surface spans and rejects input beyond its configured model window rather than silently declaring
complete extraction. [GLiNER2](https://github.com/fastino-ai/GLiNER2) broadens structured extraction;
it was reviewed, but has no dedicated tested adapter in this release. It should be added behind a
contract test rather than presented as interchangeable with GLiNER.

[spaCy linguistic features](https://spacy.io/usage/linguistic-features) separate POS tags, named
entities and syntactic features. We expose NER and configured POS tokens as grounded tags. Neither
POS nor shared tags establishes a causal relationship. The LLM extractor is separate and validates
verbatim evidence for entity mentions and explicit directed relations. These checks prove passage
support exists; they cannot prove that a model's predicate interpretation is correct. Human or
independent-model evaluation remains necessary.

[Neo4j's Python query guidance](https://neo4j.com/docs/python-manual/current/query-simple/)
supports parameter binding and explicit database selection. Our adapter stores generation-scoped
entities, passage mentions and evidence-bearing FACT edges. Traversal is bounded and uses a fixed
query template; user/generated Cypher is never executed. A read route is not an authorization
boundary. Deploy with an appropriate database account and isolate environments.

## 5. Communities and aggregation

[GraphRAG outputs](https://microsoft.github.io/graphrag/index/outputs/) preserve community,
entity, relationship and text-unit links. [Global search](https://microsoft.github.io/graphrag/query/global_search/)
uses community information for broad corpus questions. This supports the original idea of building
useful derived structures before query time, provided citations and refresh rules are reliable.
Our first implementation creates overlapping entity-topic communities or seeded Louvain groups,
with document membership and passage evidence. Optional LLM summaries validate each claim's
source references and report excluded input. This is not GraphRAG's hierarchical reports,
map-reduce global answering or DRIFT implementation. Those are explicit follow-on work.

[NetworkX Louvain](https://networkx.org/documentation/stable/reference/algorithms/generated/networkx.algorithms.community.louvain.louvain_communities.html)
provides a seeded community method. Our Louvain graph connects documents and extracted entities;
clusters are organizational aids, not proof that all members describe the same defect. Grouped
counts and earliest recorded dates instead use canonical records and supplied metadata. Missing
or invalid dates are counted, and “earliest in the corpus” is not “first occurrence in the world.”

## 6. Model interfaces and bounded orchestration

[OpenAI chat completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
and [embedding creation](https://developers.openai.com/api/reference/resources/embeddings/methods/create)
provide useful compatibility interfaces for remote and local serving. We use chat-completions for
the explicitly named OpenAI-compatible adapter, not as a claim of universal provider compatibility.
[Anthropic Messages](https://platform.claude.com/docs/en/api/messages/create) is a separate adapter,
allowing Sonnet routing and Haiku answering/extraction by configuration. JSON outputs are schema
validated; truncated completions fail. There are no hidden retries or silent model substitutions.
Actual token usage and provider time are recorded; no invented token-to-dollar estimate is emitted.

The [MCP server tutorial](https://modelcontextprotocol.io/docs/2026-07-28/develop/build-server)
uses the official Python SDK. Our optional MCP entrypoint exposes search and answer over stdio.
An HTTP gateway, tenant authorization and administrative dashboard are deployment concerns; the
package does not copy the private deployment's gateway or secrets.

## 7. Lessons carried over from the existing project

- Preserve original title, URL, Issue, Details, description and resolution separately. Summaries
  cannot replace missing procedures in answer evidence.
- Measure each arm and their union. More arms are not proof of better ranking. Distinguish recall
  at a displayed cutoff from pool coverage and answer correctness.
- Preserve seed-record context as data; do not concatenate instruction-like wrappers into search
  text. No-context API requests must still work.
- Filter before selection, canonicalize document IDs, retain arm/chunk provenance, and report
  truncation separately from retrieval misses.
- Never equate successful ingestion, entity count, or a tiny synthetic demo with 90% relevance.
- Reject incompatible indexes and unsuccessful provider outputs. Do not manufacture graph facts
  or hide unsupported capabilities as an empty success.
- Keep query generation, retrieval, review and generation timing separate. Concurrent arm durations
  are not additive wall-clock latency.
- Avoid repeated agentic calls for `missing=none`; require an actionable gap and a new query.
- Freeze held-out labels and use an independent answer evaluator. This release implements retrieval
  metrics; automated answer judging and DSPy optimization are not silently enabled.

## Decision

Build a small, inspectable standalone core with real local and external adapters, explicit limits,
synthetic examples and reproducible tests. Expand provider breadth and scalable indexing after the
correctness contracts are stable. Do not package private production data, weights, API keys or
question-specific relevance fixes in an open-source distribution.
