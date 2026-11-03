# Adopting su_multi_arm: evaluating it on your own corpus

This package is a library and a pair of serving surfaces. It does not migrate or replace any search
service you run today, and nothing in this repository says how it will perform on your data. Treat
any adoption as an experiment that has to produce its own evidence. The steps below describe that
experiment. Keep your corpus, labels, prompts with private examples, and credentials outside this
repository.

## 1. Prepare a private, frozen export

- Export the records you are allowed to process to a private location. Keep the stable source
  IDs, human titles, absolute URLs, every relevant body field, and the metadata you will filter or
  count on.
- Map the export with `ingestion.input_mapping` (exact field names; see
  [CONFIGURATION.md](CONFIGURATION.md#input-mapping-and-redaction)). Check each source type on its
  own. Measure the null rate of title, body, date and URL. Decide on redaction (`privacy`) before any
  remote extraction or embedding call.
- Declare a typed `metadata_schema`: types, `filterable`, `listable`, `groupable`, `time_basis`. Typed
  scope and exact counts depend on it, and a field declared only as `string` cannot support date
  measures.

## 2. Build in isolation

- Start from [examples/v2/local.yaml](../examples/v2/local.yaml) (offline, sqlite) or
  [examples/v2/mixed_backends.yaml](../examples/v2/mixed_backends.yaml) (PostgreSQL, OpenSearch and
  Neo4j). Point every backend at a dedicated database, schema, index prefix or namespace. Never
  point it at a store that serves traffic.
- Run `config validate` and `doctor --probe` first. Then `ingest` and `status`. Check the coverage
  report for each document (fields, chunks, extraction, community membership). A missing graph
  relation is not automatically an error, because the text may not contain one.
- Record the exact embedding recipe (model, revision, dimensions, prefixes, chunking). The index
  fingerprint refuses a mismatched recipe; it does not convert one.

## 3. Freeze an evaluation set

- Write the questions and the gold record IDs before you look at results. Include exact-term and
  error-string queries, identifier lookups, count and earliest-date questions, record-context
  ("similar to this record") requests, unknown identifiers, empty documents, and questions whose
  answer sits in each source type, so that a source weight cannot hide a regression.
- Keep a held-out split that you do not use while tuning configuration or prompts.

## 4. Parity checklist

Compare your current system and one or more su_multi_arm configurations on the same frozen set.
Report each item separately.

| Check | What to record |
|---|---|
| Per-arm retrieval | Recall@5/10/20/100 and MRR for each arm (dense, lexical, structured, graph, community), plus the final selected pool. Report macro and micro averages and the number of unlabelled queries. |
| Exact aggregates | For count, group and earliest-date questions: the value, the denominator, and the `exactness` of each `ScopeResult` against a hand-checked answer. A natural-language count that has no exact scope must abstain or be flagged. It must never be estimated. |
| Decision agents | `rules`, `llm` and `cascade` on the same set: plan validity, abstention rate, and the `unsupported_constraints` raised. |
| Answers | Citation validity (the contract does this automatically), support, completeness and safe abstention. Use human review or an independent judge, never the generator alone. |
| Access control | With your `AccessPolicy`, every hit, aggregate, value listing and `record_context` lookup stays inside the principal's scope. Test with principals that should see nothing. |
| Latency and cost | p50/p95 per stage, from the trace. Provider usage per request from `model_calls`. Run a load test at your expected concurrency. |
| Failure behaviour | Backend down, provider timeout, budget exhausted, deadline exceeded. Each must surface as a typed error or an explicit abstention, never as a silent partial answer. |

## 5. Decide

Expose real traffic only after the parity table supports it. Use a reversible canary and keep the
existing service available. Publishing results is a separate decision. Record the actual scope and
limits of the evaluation alongside any numbers.
